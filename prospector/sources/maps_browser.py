"""Free Google Maps source: drives a headless Chromium through maps.google.com.

This works the way PhantomBuster's "Google Maps Search Export" phantom does: run a search,
scroll the results list until it has enough listings, then open each listing to read its
details (full address, phone, website, rating, category).

Google changes its markup from time to time. The selectors below rely mostly on stable
attributes (``role``, ``data-item-id``, ``aria-label``) with class names only as a fallback.
If Google ever breaks this, the Places API source keeps working.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .base import LogFn, Place, SourceError, parse_float, parse_int

MAPS_SEARCH_URL = "https://www.google.com/maps/search/{query}?hl={lang}"

# Pull every result card currently rendered in the results feed.
_CARDS_JS = r"""
(feed) => Array.from(feed.querySelectorAll('div[role="article"]')).map(c => {
  const a = c.querySelector('a.hfpxzc') || c.querySelector('a[href*="/maps/place/"]');
  if (!a) return null;
  const rating = c.querySelector('span[role="img"][aria-label]');
  const reviews = c.querySelector('span.UY7F9');
  const web = c.querySelector('a[data-value="Website"]');
  const phone = c.querySelector('span.UsdlK');
  const clean = s => (s || '').replace(/·/g, '').replace(/\s+/g, ' ').trim();
  const lines = Array.from(c.querySelectorAll('div.W4Efsd > div.W4Efsd')).map(d =>
    Array.from(d.querySelectorAll(':scope > span')).map(s => clean(s.textContent))
      .filter(t => /[A-Za-z0-9]/.test(t)));
  return {
    href: a.href,
    name: a.getAttribute('aria-label') || clean(a.textContent),
    rating: rating ? rating.getAttribute('aria-label') : null,
    reviews: reviews ? reviews.textContent : null,
    website: web ? web.href : null,
    phone: phone ? clean(phone.textContent) : null,
    lines: lines,
    sponsored: /Sponsored/i.test(c.textContent || ''),
  };
}).filter(Boolean)
"""

# Read the details panel of an opened listing.
_DETAILS_JS = r"""
() => {
  const q = s => document.querySelector(s);
  const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  const label = el => el ? (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim() : '';
  const stripPrefix = s => s.includes(':') ? s.slice(s.indexOf(':') + 1).trim() : s;
  const main = q('div[role="main"]') || document.body;
  const h1 = main.querySelector('h1.DUwDvf') || main.querySelector('h1');
  const f7 = main.querySelector('div.F7nice');
  let rating = null, reviews = null;
  if (f7) {
    const r = f7.querySelector('span[aria-hidden="true"]');
    rating = r ? txt(r) : null;
    const rv = f7.querySelector('span[aria-label*="review" i]');
    reviews = rv ? (label(rv) || txt(rv)) : null;
    if (!reviews) { const m = txt(f7).match(/\(([\d.,\s]+)\)/); if (m) reviews = m[1]; }
  }
  const cat = main.querySelector('button.DkEaL') || main.querySelector('button[jsaction*="category"]');
  const addr = main.querySelector('button[data-item-id="address"]');
  const web = main.querySelector('a[data-item-id="authority"]');
  const phone = main.querySelector('button[data-item-id^="phone:tel:"]');
  const body = main.innerText || '';
  let status = '';
  if (/Permanently closed/i.test(body)) status = 'CLOSED_PERMANENTLY';
  else if (/Temporarily closed/i.test(body)) status = 'CLOSED_TEMPORARILY';
  return {
    name: txt(h1),
    rating: rating,
    reviews: reviews,
    category: txt(cat),
    address: addr ? stripPrefix(label(addr)) || txt(addr) : '',
    website: web ? web.href : '',
    phone: phone ? stripPrefix(label(phone)) || phone.getAttribute('data-item-id').replace('phone:tel:', '') : '',
    status: status,
  };
}
"""

_BLOCK_RESOURCES = {"image", "media", "font"}


def _clean_website(url: str | None) -> str:
    """Google sometimes wraps outbound links in /url?q=... redirects."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url")
        if target:
            return target[0]
    return url


def parse_maps_url(url: str) -> dict:
    """Extract place id and coordinates from a /maps/place/ URL."""
    out: dict = {"place_id": "", "latitude": None, "longitude": None}
    decoded = unquote(url)
    m = re.search(r"!19s(ChIJ[\w-]+)", decoded)
    if m:
        out["place_id"] = m.group(1)
    else:
        m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", decoded)
        if m:
            out["place_id"] = m.group(1)
    lat = re.search(r"!3d(-?\d+\.\d+)", decoded)
    lng = re.search(r"!4d(-?\d+\.\d+)", decoded)
    if lat and lng:
        out["latitude"], out["longitude"] = float(lat.group(1)), float(lng.group(1))
    return out


def _card_to_place(card: dict, query: str) -> Place:
    lines = card.get("lines") or []
    first = lines[0] if lines else []
    category = first[0] if first else ""
    address = first[-1] if len(first) > 1 else ""
    rating_label = card.get("rating") or ""
    rating = parse_float(rating_label)
    reviews = parse_int(card.get("reviews"))
    if reviews is None:
        m = re.search(r"([\d,.]+)\s+Reviews?", rating_label, re.I)
        reviews = parse_int(m.group(1)) if m else None
    ids = parse_maps_url(card["href"])
    return Place(
        name=card.get("name") or "",
        place_id=ids["place_id"],
        category=category,
        categories=[category] if category else [],
        address=address,
        phone=card.get("phone") or "",
        website=_clean_website(card.get("website")),
        rating=rating,
        reviews=reviews,
        maps_url=card["href"],
        latitude=ids["latitude"],
        longitude=ids["longitude"],
        source="browser",
        query=query,
    )


def _merge_details(place: Place, details: dict) -> Place:
    if details.get("name"):
        place.name = details["name"]
    if details.get("category"):
        place.category = details["category"]
        if details["category"] not in place.categories:
            place.categories.insert(0, details["category"])
    if details.get("address"):
        place.address = details["address"]
    if details.get("website"):
        place.website = _clean_website(details["website"])
    if details.get("phone"):
        place.phone = details["phone"]
    rating = parse_float(details.get("rating"))
    if rating is not None:
        place.rating = rating
    reviews = parse_int(details.get("reviews"))
    if reviews is not None:
        place.reviews = reviews
    if details.get("status"):
        place.business_status = details["status"]
    return place


class GoogleMapsBrowserSource:
    name = "browser"

    def __init__(
        self,
        headless: bool = True,
        details: bool = True,
        concurrency: int = 3,
        language: str = "en",
        chromium_path: str | None = None,
        user_data_dir: str | Path | None = None,
        log: LogFn | None = None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        self.headless = headless
        self.details = details
        self.concurrency = max(1, concurrency)
        self.language = language
        self.chromium_path = chromium_path or None
        self.user_data_dir = str(user_data_dir) if user_data_dir else None
        self.log = log or (lambda msg: None)
        self.cancel = cancel or asyncio.Event()
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ browser lifecycle
    async def _ensure_context(self):
        async with self._lock:
            if self._context is not None:
                return self._context
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise SourceError("Playwright is not installed. Run: pip install playwright") from exc
            self._pw = await async_playwright().start()
            launch_kwargs: dict = {"headless": self.headless}
            if self.chromium_path:
                launch_kwargs["executable_path"] = self.chromium_path
            context_kwargs = {
                "locale": f"{self.language}-US" if self.language == "en" else self.language,
                "viewport": {"width": 1366, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            try:
                if self.user_data_dir:
                    self._context = await self._pw.chromium.launch_persistent_context(
                        self.user_data_dir, **launch_kwargs, **context_kwargs
                    )
                else:
                    self._browser = await self._pw.chromium.launch(**launch_kwargs)
                    self._context = await self._browser.new_context(**context_kwargs)
            except Exception as exc:
                await self._pw.stop()
                self._pw = None
                msg = str(exc)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    raise SourceError(
                        "Chromium isn't installed for Playwright yet. Run: python -m playwright install chromium"
                    ) from exc
                raise SourceError(f"Could not start Chromium: {msg.splitlines()[0]}") from exc

            async def _route(route):
                if route.request.resource_type in _BLOCK_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()

            await self._context.route("**/*", _route)
            return self._context

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self._pw = self._browser = self._context = None

    # ------------------------------------------------------------------ helpers
    async def _handle_consent(self, page) -> None:
        """Click through Google's cookie-consent interstitial (shown in the EU/UK)."""
        if "consent." not in page.url:
            return
        for label in ("Reject all", "Accept all", "I agree"):
            button = page.get_by_role("button", name=label)
            if await button.count():
                await button.first.click()
                await page.wait_for_load_state("domcontentloaded")
                return
        buttons = page.locator("form button")
        if await buttons.count():
            await buttons.last.click()
            await page.wait_for_load_state("domcontentloaded")

    async def _check_blocked(self, page) -> None:
        if "/sorry/" in page.url or await page.locator("form#captcha-form, #recaptcha").count():
            raise SourceError(
                "Google is showing a CAPTCHA ('unusual traffic'). Wait a while, run with the browser "
                "visible (headless off) to solve it, or add a Google Places API key."
            )

    async def _open(self, page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await self._handle_consent(page)
        await self._check_blocked(page)

    async def _read_details(self, page, place: Place) -> Place:
        url = place.maps_url
        sep = "&" if "?" in url else "?"
        if "hl=" not in url:
            url = f"{url}{sep}hl={self.language}"
        await self._open(page, url)
        try:
            await page.wait_for_selector("div[role='main'] h1", timeout=15000)
        except Exception:
            return place
        try:
            await page.wait_for_selector(
                "button[data-item-id='address'], a[data-item-id='authority'], button[data-item-id^='phone:tel:']",
                timeout=4000,
            )
        except Exception:
            pass
        details = await page.evaluate(_DETAILS_JS)
        return _merge_details(place, details)

    # ------------------------------------------------------------------ search
    async def _collect_cards(self, page, query: str, limit: int) -> list[Place]:
        feed = page.locator('div[role="feed"]')
        places: dict[str, Place] = {}
        stale_rounds = 0
        while len(places) < limit and not self.cancel.is_set():
            cards = await feed.evaluate(_CARDS_JS)
            before = len(places)
            for card in cards:
                if card.get("sponsored"):
                    continue
                p = _card_to_place(card, query)
                places.setdefault(p.key, p)
            if len(places) >= limit:
                break
            if len(places) // 20 > before // 20:
                self.log(f"Loaded {len(places)} listings so far for '{query}'…")
            end_reached = await page.locator("span.HlvSq, p.fontBodyMedium > span > span").filter(
                has_text=re.compile("end of the list", re.I)
            ).count()
            if end_reached:
                break
            stale_rounds = stale_rounds + 1 if len(places) == before else 0
            if stale_rounds >= 6:
                break
            await feed.evaluate("f => f.scrollTo(0, f.scrollHeight)")
            await page.wait_for_timeout(1300 + 300 * min(stale_rounds, 3))
        return list(places.values())[:limit]

    async def search(self, query: str, limit: int) -> AsyncIterator[Place]:
        context = await self._ensure_context()
        page = await context.new_page()
        try:
            url = MAPS_SEARCH_URL.format(query=quote_plus(query), lang=self.language)
            await self._open(page, url)
            try:
                await page.wait_for_selector("div[role='feed'], div[role='main'] h1", timeout=25000)
            except Exception:
                await self._check_blocked(page)
                self.log(f"No Google Maps results for '{query}'")
                return

            if not await page.locator("div[role='feed']").count():
                # Google jumped straight to a single place.
                ids = parse_maps_url(page.url)
                place = Place(name="", source="browser", query=query, maps_url=page.url, **ids)
                await page.wait_for_timeout(1500)
                place = _merge_details(place, await page.evaluate(_DETAILS_JS))
                if place.name:
                    yield place
                return

            cards = await self._collect_cards(page, query, limit)
            self.log(f"Google Maps listed {len(cards)} results for '{query}'")
        finally:
            await page.close()

        if not self.details:
            for place in cards:
                yield place
            return

        queue: asyncio.Queue = asyncio.Queue()
        for place in cards:
            queue.put_nowait(place)
        results: asyncio.Queue = asyncio.Queue()
        done_marker = object()

        async def worker():
            wpage = None
            try:
                wpage = await context.new_page()
                while not self.cancel.is_set():
                    try:
                        place = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        place = await self._read_details(wpage, place)
                    except SourceError as exc:
                        await results.put(exc)
                        return
                    except Exception as exc:  # keep the card data if the details page fails
                        self.log(f"Could not open details for {place.name}: {exc.__class__.__name__}")
                    await results.put(place)
            except Exception as exc:
                await results.put(SourceError(f"Browser worker failed: {exc}"))
            finally:
                if wpage is not None:
                    try:
                        await wpage.close()
                    except Exception:
                        pass
                await results.put(done_marker)

        workers = [asyncio.create_task(worker()) for _ in range(min(self.concurrency, len(cards)))]
        try:
            finished = 0
            while finished < len(workers):
                item = await results.get()
                if item is done_marker:
                    finished += 1
                elif isinstance(item, Exception):
                    raise item
                else:
                    yield item
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
