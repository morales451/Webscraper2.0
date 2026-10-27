"""Run a prospecting search end to end.

  Google Maps search (API or browser)  ──►  de-duplicate / skip leads seen in earlier runs
        ──►  quick filters (rating, reviews, category...)  ──►  crawl website (emails + text)
        ──►  keyword scoring  ──►  save lead

Maps results stream into a queue while a pool of workers crawls websites in parallel, so
leads show up in the dashboard as soon as each one is ready.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .enrich.website import (
    Renderer,
    SiteCrawler,
    SiteData,
    is_social_url,
    make_http_client,
    normalize_url,
    site_domain,
)
from .icp import ICPProfile
from .scoring import evaluate, prefilter
from .sources import SourceError, make_source
from .sources.base import Place
from .storage import Store

MAX_LOG_LINES = 400
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

# Hosts where many unrelated businesses share one domain, so the domain can't identify a business.
SHARED_HOSTS = {"sites.google.com", "business.site", "linktr.ee", "google.com", "yelp.com", "nextdoor.com"}


def identity_domain(website: str) -> str | None:
    """Website domain usable to recognise the same business across runs (None if shared)."""
    if not website or is_social_url(website):
        return None
    domain = site_domain(website)
    return None if domain in SHARED_HOSTS else domain or None


@dataclass
class RunOptions:
    source: str | None = None  # None -> settings (auto/api/browser)
    details: bool = True       # browser source: open every listing for full details
    enrich: bool = True        # crawl websites for emails + keyword matching


def clean_place(place: Place) -> Place:
    for attr in ("name", "address", "phone", "category"):
        setattr(place, attr, _INVISIBLE.sub("", getattr(place, attr) or "").strip())
    place.categories = [_INVISIBLE.sub("", c).strip() for c in place.categories if c]
    if place.website:
        place.website = normalize_url(place.website)
    return place


def lead_record(place: Place, site: SiteData | None, evaluation) -> dict:
    domain = identity_domain((site.url if site and site.ok else place.website) or "") or ""
    return {
        "place_key": place.key,
        "name": place.name,
        "category": place.category,
        "categories": place.categories,
        "address": place.address,
        "phone": place.phone,
        "website": (site.url if site and site.ok else place.website) or "",
        "domain": domain,
        "rating": place.rating,
        "reviews": place.reviews,
        "maps_url": place.maps_url,
        "latitude": place.latitude,
        "longitude": place.longitude,
        "business_status": place.business_status,
        "query": place.query,
        "emails": site.emails if site else [],
        "site_title": site.title if site else "",
        "site_description": site.description if site else "",
        "site_error": site.error if site else "",
        "pages_crawled": len(site.pages) if site else 0,
        "score": evaluation.score,
        "qualified": evaluation.qualified,
        "reasons": evaluation.reasons,
        "matches": evaluation.matches_dict(),
    }


class RunContext:
    """Counters + log for one run, persisted to the store as they change."""

    def __init__(self, store: Store, run_id: int, on_log: Callable[[str], None] | None = None):
        self.store = store
        self.run_id = run_id
        self.on_log = on_log
        self.log_lines: list[dict] = []
        self.counts = {"searches_done": 0, "found": 0, "skipped_seen": 0, "processed": 0, "qualified": 0}

    def log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.log_lines.append({"t": stamp, "msg": message})
        del self.log_lines[:-MAX_LOG_LINES]
        if self.on_log:
            self.on_log(message)
        self.flush()

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] += n
        self.flush()

    def flush(self, **extra) -> None:
        self.store.update_run(self.run_id, log=self.log_lines, **self.counts, **extra)


async def execute_run(
    settings: Settings,
    store: Store,
    run_id: int,
    profile: ICPProfile,
    options: RunOptions | None = None,
    cancel: asyncio.Event | None = None,
    on_log: Callable[[str], None] | None = None,
    on_lead: Callable[[dict], None] | None = None,
) -> dict:
    options = options or RunOptions()
    cancel = cancel or asyncio.Event()
    ctx = RunContext(store, run_id, on_log)
    terms = profile.search_terms()
    store.update_run(run_id, status="running", searches_total=len(terms))

    try:
        source = make_source(settings, options.source, log=ctx.log, cancel=cancel, details=options.details)
    except (SourceError, ValueError) as exc:
        ctx.log(f"Error: {exc}")
        store.update_run(run_id, status="failed", error=str(exc), log=ctx.log_lines)
        return store.get_run(run_id) or {}

    ctx.log(
        f"Starting '{profile.name}': {len(terms)} search(es) via "
        f"{'Google Places API' if source.name == 'api' else 'Google Maps (browser)'}"
    )

    http = make_http_client(settings.request_timeout)
    renderer = Renderer(settings.chromium_path) if (options.enrich and settings.render_js) else None
    crawler = SiteCrawler(http, profile.pages_per_site, settings.respect_robots, renderer)
    crawl_cache: dict[str, asyncio.Future] = {}

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    workers_n = max(1, settings.site_concurrency)
    fatal: list[str] = []
    seen_keys: set[str] = set()

    async def crawl(url: str) -> SiteData:
        # Several listings (e.g. branches of one practice) can share a website; crawl it once.
        key = normalize_url(url)
        if key not in crawl_cache:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            crawl_cache[key] = fut
            try:
                fut.set_result(await crawler.crawl(url))
            except Exception as exc:  # never let one site kill the run
                fut.set_result(SiteData(url=url, domain=site_domain(url), error=f"crawl failed: {exc.__class__.__name__}"))
        return await crawl_cache[key]

    async def producer():
        enqueued = 0
        try:
            for term in terms:
                if cancel.is_set():
                    break
                ctx.log(f"Searching: {term}")
                term_found = 0
                # aclosing() shuts the source's browser workers down if we stop early.
                async with aclosing(source.search(term, profile.max_results_per_search)) as results:
                    async for place in results:
                        if cancel.is_set():
                            break
                        place = clean_place(place)
                        if not place.name or place.key in seen_keys:
                            continue
                        seen_keys.add(place.key)
                        term_found += 1
                        ctx.bump("found")
                        domain = identity_domain(place.website)
                        if profile.skip_seen and store.seen_before(place.key, domain, exclude_run=run_id):
                            ctx.bump("skipped_seen")
                            continue
                        await queue.put(place)
                        enqueued += 1
                        if profile.max_leads and enqueued >= profile.max_leads:
                            ctx.log(f"Reached the limit of {profile.max_leads} leads")
                            return
                ctx.bump("searches_done")
                ctx.log(f"'{term}': {term_found} new listing(s)")
        except SourceError as exc:
            fatal.append(str(exc))
            ctx.log(f"Error: {exc}")
        except Exception as exc:
            fatal.append(f"{exc.__class__.__name__}: {exc}")
            ctx.log(f"Search failed: {exc.__class__.__name__}: {exc}")
        finally:
            for _ in range(workers_n):
                await queue.put(None)

    async def worker():
        while True:
            place = await queue.get()
            if place is None:
                return
            if cancel.is_set():
                continue
            try:
                pre = prefilter(place, profile.filters)
                site = None
                if not pre and options.enrich and place.website:
                    site = await crawl(place.website)
                evaluation = evaluate(place, site, profile, pre)
                record = lead_record(place, site, evaluation)
                store.save_lead(run_id, record)
                if on_lead:
                    on_lead(record)
            except Exception as exc:  # one bad lead shouldn't stop the run
                ctx.log(f"Could not process {place.name}: {exc.__class__.__name__}: {exc}")
                continue
            ctx.counts["processed"] += 1
            if evaluation.qualified:
                ctx.counts["qualified"] += 1
            ctx.flush()

    try:
        await asyncio.gather(producer(), *(worker() for _ in range(workers_n)))
    finally:
        await source.close()
        await http.aclose()
        if renderer is not None:
            await renderer.close()

    c = ctx.counts
    if cancel.is_set():
        status, error = "cancelled", None
        ctx.log("Run cancelled")
    elif fatal and c["processed"] == 0:
        status, error = "failed", fatal[0]
    else:
        status, error = "done", (fatal[0] if fatal else None)
        ctx.log(
            f"Finished: {c['found']} found, {c['skipped_seen']} skipped (seen before), "
            f"{c['processed']} checked, {c['qualified']} qualified"
        )
    ctx.flush(status=status, error=error)
    return store.get_run(run_id) or {}
