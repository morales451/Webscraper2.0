"""Health checks: is each part of the pipeline working right now?

Google changes Maps' page from time to time, which can silently break the free browser
scraper. Run ``prospector doctor`` (or Settings → Run health check) to find out quickly.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing

from .config import Settings
from .enrich.mx import OK, MxChecker
from .enrich.website import SiteCrawler, make_http_client
from .geo import Geocoder
from .sources import GoogleMapsBrowserSource, GooglePlacesApiSource, SourceError

TEST_QUERY = "coffee shop in Seattle, WA"


async def _timed(coro) -> tuple[dict, float]:
    start = time.monotonic()
    result = await coro
    return result, round(time.monotonic() - start, 1)


async def check_browser(settings: Settings) -> dict:
    source = GoogleMapsBrowserSource(
        headless=True, details=True, concurrency=2, chromium_path=settings.chromium_path,
        user_data_dir=settings.browser_profile_dir,
    )
    try:
        async with aclosing(source.search(TEST_QUERY, 3)) as results:
            places = [p async for p in results]
    except SourceError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "message": f"{exc.__class__.__name__}: {exc}"}
    finally:
        await source.close()
    if not places:
        return {"ok": False, "message": "Google Maps returned no results — its page layout may have changed. "
                                        "Use the Places API source until Prospector is updated."}
    complete = [p for p in places if p.name and p.address and (p.phone or p.website)]
    sample = {"name": places[0].name, "address": places[0].address, "phone": places[0].phone,
              "website": places[0].website, "rating": places[0].rating, "reviews": places[0].reviews}
    if not complete:
        return {"ok": False, "sample": sample,
                "message": "Listings load but details (address/phone/website) are missing — Google may have "
                           "changed the listing page."}
    note = "" if any(p.reviews is not None for p in places) else (
        " Review counts are hidden (Google's limited view) — see `prospector browser-login`.")
    return {"ok": True, "sample": sample, "message": f"Found {len(places)} listings with full details.{note}"}


async def check_api(settings: Settings) -> dict:
    if not settings.places_api_key:
        return {"ok": None, "message": "No API key set (optional)."}
    source = GooglePlacesApiSource(settings.places_api_key)
    try:
        places = [p async for p in source.search(TEST_QUERY, 1)]
    except SourceError as exc:
        return {"ok": False, "message": str(exc)}
    finally:
        await source.close()
    return {"ok": bool(places), "message": f"API key works ({places[0].name})." if places else "No results."}


async def check_website(settings: Settings) -> dict:
    async with make_http_client(settings.request_timeout) as client:
        data = await SiteCrawler(client, max_pages=2, respect_robots=False).crawl("https://www.python.org/")
    if not data.ok:
        return {"ok": False, "message": f"Could not read a test website: {data.error}"}
    return {"ok": True, "message": f"Read {len(data.pages)} pages from a test website."}


async def check_email_dns() -> dict:
    mx = MxChecker()
    try:
        status = await mx.check_domain("gmail.com")
    finally:
        await mx.close()
    return {"ok": status == OK, "message": "Email domain checks work." if status == OK
            else f"Email domain check failed ({status}) — emails won't be verified."}


async def check_geocoder(settings: Settings) -> dict:
    geo = Geocoder(settings.data_dir / "geocache.json")
    try:
        found = await geo.geocode("Seattle, WA")
    finally:
        await geo.close()
    if not found:
        return {"ok": False, "message": "Couldn't reach OpenStreetMap — 'split into areas' will fall back to whole-city searches."}
    return {"ok": True, "message": f"Map lookups work ({found.area.width_km:.0f}×{found.area.height_km:.0f} km for Seattle)."}


async def run_checks(settings: Settings, include_browser: bool = True) -> dict:
    jobs = {
        "website_crawler": check_website(settings),
        "email_check": check_email_dns(),
        "area_split": check_geocoder(settings),
        "places_api": check_api(settings),
    }
    if include_browser:
        jobs["google_maps_browser"] = check_browser(settings)
    names = list(jobs)
    timed = await asyncio.gather(*(_timed(j) for j in jobs.values()), return_exceptions=True)
    out = {}
    for name, result in zip(names, timed):
        if isinstance(result, BaseException):
            out[name] = {"ok": False, "message": f"{result.__class__.__name__}: {result}"}
        else:
            out[name] = {**result[0], "seconds": result[1]}
    return out
