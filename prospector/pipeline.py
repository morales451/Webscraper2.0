"""Run a prospecting search end to end.

  plan searches (each query × location, or × grid areas)  ──►  Google Maps (API or browser)
        ──►  de-duplicate / skip leads seen in earlier runs  ──►  quick filters
        ──►  crawl website (text, emails, phones, socials, signals)  ──►  check email domains
        ──►  keyword scoring  ──►  optional AI note  ──►  save lead
  afterwards: optional follow-up searches for related categories, keyword suggestions

Maps results stream into a queue while a pool of workers crawls websites in parallel, so
leads show up in the dashboard as soon as each one is ready.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .enrich.mx import MxChecker, order_by_deliverability
from .enrich.website import (
    Renderer,
    SiteCrawler,
    SiteData,
    is_social_url,
    make_http_client,
    normalize_url,
    site_domain,
)
from .geo import Area, Geocoder, split_area
from .icp import ICPProfile
from .scoring import evaluate, prefilter
from .sources import BlockedError, SourceError, make_source
from .sources.base import Place
from .storage import Store
from .suggest import SuggestionCollector, ignore_words_for

MAX_LOG_LINES = 400
FLUSH_INTERVAL = 0.5  # seconds between progress writes to the database
MAX_EXPANSIONS = 3
BLOCK_PAUSE_SECONDS = 120  # wait this long when Google shows a CAPTCHA, then retry once
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿]")

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


@dataclass
class SearchTask:
    query: str               # text sent to the source
    label: str               # shown in the log and stored on each lead
    area: Area | None = None
    base_query: str = ""


def clean_place(place: Place) -> Place:
    for attr in ("name", "address", "phone", "category"):
        setattr(place, attr, _INVISIBLE.sub("", getattr(place, attr) or "").strip())
    place.categories = [_INVISIBLE.sub("", c).strip() for c in place.categories if c]
    if place.website:
        place.website = normalize_url(place.website)
    return place


def lead_record(place: Place, site: SiteData | None, evaluation) -> dict:
    website = (site.url if site and site.ok else place.website) or ""
    site_phones = site.phones if site else []
    return {
        "place_key": place.key,
        "name": place.name,
        "category": place.category,
        "categories": place.categories,
        "address": place.address,
        "phone": place.phone or (site_phones[0] if site_phones else ""),
        "website": website,
        "domain": identity_domain(website) or "",
        "rating": place.rating,
        "reviews": place.reviews,
        "maps_url": place.maps_url,
        "latitude": place.latitude,
        "longitude": place.longitude,
        "business_status": place.business_status,
        "query": place.query,
        "emails": site.emails if site else [],
        "email_checks": site.email_checks if site else {},
        "site_phones": site_phones,
        "socials": site.socials if site else {},
        "signals": site.signals if site else [],
        "site_title": site.title if site else "",
        "site_description": site.description if site else "",
        "site_error": site.error if site else "",
        "pages_crawled": len(site.pages) if site else 0,
        "score": evaluation.score,
        "qualified": evaluation.qualified,
        "reasons": evaluation.reasons,
        "matches": evaluation.matches_dict(),
    }


async def plan_searches(
    profile: ICPProfile, queries: list[str], geocoder: Geocoder | None, log: Callable[[str], None]
) -> list[SearchTask]:
    if not profile.locations:
        return [SearchTask(q, q, None, q) for q in queries]
    tasks: list[SearchTask] = []
    for loc in profile.locations:
        cells: list[Area] = []
        if profile.split_areas and geocoder is not None:
            found = await geocoder.geocode(loc)
            if found is None:
                log(f"Couldn't find '{loc}' on the map — searching it as one area")
            else:
                cells = split_area(found.area, profile.max_areas)
                if len(cells) > 1:
                    log(f"Split '{loc}' into {len(cells)} areas "
                        f"(~{cells[0].width_km:.0f}×{cells[0].height_km:.0f} km each)")
        for q in queries:
            if len(cells) > 1:
                tasks.extend(SearchTask(q, f"{q} in {cell.label}", cell, q) for cell in cells)
            else:
                tasks.append(SearchTask(f"{q} in {loc}", f"{q} in {loc}", None, q))
    return tasks


def related_categories(qualified_categories: list[list[str]], queries: list[str], limit: int = MAX_EXPANSIONS) -> list[str]:
    """Google categories shared by several qualified leads that the profile didn't search for."""
    if len(qualified_categories) < 3:
        return []
    counts: Counter = Counter()
    for cats in qualified_categories:
        counts.update({c.strip().lower() for c in cats if c and c.strip()})
    lowered = [q.lower() for q in queries]
    picks = []
    for cat, n in counts.most_common():
        if n < max(2, round(0.15 * len(qualified_categories))):
            break
        if any(cat in q or q in cat for q in lowered):
            continue
        picks.append(cat)
        if len(picks) >= limit:
            break
    return picks


class RunContext:
    """Counters + log for one run, persisted to the store (throttled) as they change."""

    def __init__(self, store: Store, run_id: int, on_log: Callable[[str], None] | None = None):
        self.store = store
        self.run_id = run_id
        self.on_log = on_log
        self.log_lines: list[dict] = []
        self.counts = {"searches_done": 0, "found": 0, "skipped_seen": 0, "processed": 0, "qualified": 0}
        self._last_flush = 0.0

    def log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.log_lines.append({"t": stamp, "msg": message})
        del self.log_lines[:-MAX_LOG_LINES]
        if self.on_log:
            self.on_log(message)
        self.flush(force=True)

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] += n
        self.flush()

    def flush(self, force: bool = False, **extra) -> None:
        now = time.monotonic()
        if not force and not extra and now - self._last_flush < FLUSH_INTERVAL:
            return
        self._last_flush = now
        self.store.update_run(self.run_id, log=self.log_lines, **self.counts, **extra)


async def _sleep_unless_cancelled(cancel: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def execute_run(
    settings: Settings,
    store: Store,
    run_id: int,
    profile: ICPProfile,
    options: RunOptions | None = None,
    cancel: asyncio.Event | None = None,
    on_log: Callable[[str], None] | None = None,
    on_lead: Callable[[dict], None] | None = None,
    geocoder: Geocoder | None = None,
) -> dict:
    options = options or RunOptions()
    cancel = cancel or asyncio.Event()
    ctx = RunContext(store, run_id, on_log)
    store.update_run(run_id, status="running")

    try:
        source = make_source(settings, options.source, log=ctx.log, cancel=cancel, details=options.details)
    except (SourceError, ValueError) as exc:
        ctx.log(f"Error: {exc}")
        store.update_run(run_id, status="failed", error=str(exc), log=ctx.log_lines)
        return store.get_run(run_id) or {}

    own_geocoder = geocoder is None and profile.split_areas
    if own_geocoder:
        geocoder = Geocoder(settings.data_dir / "geocache.json")
    tasks = await plan_searches(profile, profile.queries, geocoder, ctx.log)
    total_searches = len(tasks)
    store.update_run(run_id, searches_total=total_searches)
    ctx.log(
        f"Starting '{profile.name}': {len(tasks)} search(es) via "
        f"{'Google Places API' if source.name == 'api' else 'Google Maps (browser)'}"
    )

    http = make_http_client(settings.request_timeout)
    renderer = Renderer(settings.chromium_path) if (options.enrich and settings.render_js) else None
    crawler = SiteCrawler(http, profile.pages_per_site, settings.respect_robots, renderer)
    mx = MxChecker() if (options.enrich and profile.check_emails) else None
    noter = None
    if profile.ai_notes:
        if settings.anthropic_api_key:
            from .ai import FitNoter

            noter = FitNoter(settings.anthropic_api_key, settings.ai_model)
        else:
            ctx.log("AI notes are on, but no Anthropic API key is set (Settings) — skipping them")
    collector = SuggestionCollector(
        known_terms=[k.term for k in profile.keywords] + profile.required_keywords + profile.exclude_keywords,
        ignore_words=ignore_words_for(profile.queries, profile.locations),
    )
    crawl_cache: dict[str, asyncio.Future] = {}

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    workers_n = max(1, settings.site_concurrency)
    fatal: list[str] = []
    seen_keys: set[str] = set()
    outcomes: list[tuple[str, bool, int | None, list[str]]] = []  # (key, qualified, score, categories)
    enqueued = 0
    ai_warned = False

    async def crawl(url: str) -> SiteData:
        # Several listings (e.g. branches of one practice) can share a website; crawl it once.
        key = normalize_url(url)
        if key not in crawl_cache:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            crawl_cache[key] = fut
            try:
                site = await crawler.crawl(url)
                if mx is not None and site.emails:
                    site.email_checks = await mx.check_emails(site.emails)
                    site.emails = order_by_deliverability(site.emails, site.email_checks)
                fut.set_result(site)
            except Exception as exc:  # never let one site kill the run
                fut.set_result(SiteData(url=url, domain=site_domain(url), error=f"crawl failed: {exc.__class__.__name__}"))
        return await crawl_cache[key]

    async def run_search(task: SearchTask) -> bool:
        """Run one search; return False when the lead limit was reached."""
        nonlocal enqueued
        ctx.log(f"Searching: {task.label}")
        found_here = 0

        def want(p: Place) -> bool:
            p = clean_place(p)
            if p.key in seen_keys:
                return False
            return not (profile.skip_seen and store.seen_before(p.key, identity_domain(p.website), exclude_run=run_id))

        # aclosing() shuts the source's browser workers down if we stop early.
        async with aclosing(source.search(task.query, profile.max_results_per_search, area=task.area, want=want)) as results:
            async for place in results:
                if cancel.is_set():
                    break
                place = clean_place(place)
                place.query = task.label
                if not place.name or place.key in seen_keys:
                    continue
                seen_keys.add(place.key)
                found_here += 1
                ctx.bump("found")
                if profile.skip_seen and store.seen_before(place.key, identity_domain(place.website), exclude_run=run_id):
                    ctx.bump("skipped_seen")
                    continue
                await queue.put(place)
                enqueued += 1
                if profile.max_leads and enqueued >= profile.max_leads:
                    ctx.log(f"Reached the limit of {profile.max_leads} leads")
                    return False
        ctx.bump("searches_done")
        ctx.log(f"'{task.label}': {found_here} new listing(s)")
        return True

    delay = settings.search_delay
    blocked_once = False

    async def search_with_backoff(task: SearchTask) -> bool:
        """run_search, but on a CAPTCHA pause, slow down and retry once."""
        nonlocal delay, blocked_once
        try:
            return await run_search(task)
        except BlockedError:
            if blocked_once:
                raise
            blocked_once = True
            ctx.log(f"Google showed a CAPTCHA — pausing {BLOCK_PAUSE_SECONDS // 60} minutes, then continuing more slowly")
            await _sleep_unless_cancelled(cancel, BLOCK_PAUSE_SECONDS)
            delay = max(delay * 3, 8.0)
            if cancel.is_set():
                return False
            return await run_search(task)

    async def producer():
        nonlocal total_searches
        try:
            for i, task in enumerate(tasks):
                if cancel.is_set():
                    return
                if i:
                    await _sleep_unless_cancelled(cancel, delay)
                if not await search_with_backoff(task):
                    return
            if profile.expand_queries and not cancel.is_set():
                await queue.join()  # wait until every lead so far has been scored
                extra = related_categories([c for _k, q, _s, c in outcomes if q], profile.queries)
                if not extra:
                    ctx.log("No related categories to add")
                else:
                    ctx.log(f"Also searching related categories: {', '.join(extra)}")
                    more = await plan_searches(profile, extra, geocoder, lambda _m: None)
                    total_searches += len(more)
                    store.update_run(run_id, searches_total=total_searches)
                    for task in more:
                        if cancel.is_set():
                            return
                        await _sleep_unless_cancelled(cancel, delay)
                        if not await search_with_backoff(task):
                            return
        except SourceError as exc:
            fatal.append(str(exc))
            ctx.log(f"Error: {exc}")
        except Exception as exc:
            fatal.append(f"{exc.__class__.__name__}: {exc}")
            ctx.log(f"Search failed: {exc.__class__.__name__}: {exc}")
        finally:
            for _ in range(workers_n):
                await queue.put(None)

    async def process(place: Place) -> None:
        nonlocal ai_warned
        pre = prefilter(place, profile.filters)
        site = None
        if not pre and options.enrich and place.website:
            site = await crawl(place.website)
        evaluation = evaluate(place, site, profile, pre)
        record = lead_record(place, site, evaluation)
        lead_id = store.save_lead(run_id, record)
        if site is not None and site.ok:
            collector.add(place.key, [p for page in site.pages for p in (page.title, page.headings, page.description, page.text)])
        outcomes.append((place.key, evaluation.qualified, evaluation.score, place.categories or [place.category]))
        if noter is not None and evaluation.qualified:
            ai = await noter.note(profile, place, site, [m.term for m in evaluation.matches if not m.term.startswith("NOT: ")])
            if ai:
                record.update(ai_note=ai["note"], ai_score=ai["fit_score"])
                store.update_lead_fields(lead_id, ai_note=ai["note"], ai_score=ai["fit_score"])
            elif noter.disabled_reason and not ai_warned:
                ai_warned = True
                ctx.log(f"AI notes stopped: {noter.disabled_reason}")
        if on_lead:
            on_lead(record)
        ctx.counts["processed"] += 1
        if evaluation.qualified:
            ctx.counts["qualified"] += 1
        ctx.flush()

    async def worker():
        while True:
            place = await queue.get()
            try:
                if place is None:
                    return
                if cancel.is_set():
                    continue
                try:
                    await process(place)
                except Exception as exc:  # one bad lead shouldn't stop the run
                    ctx.log(f"Could not process {place.name}: {exc.__class__.__name__}: {exc}")
            finally:
                queue.task_done()

    try:
        await asyncio.gather(producer(), *(worker() for _ in range(workers_n)))
    finally:
        await source.close()
        await http.aclose()
        if renderer is not None:
            await renderer.close()
        if mx is not None:
            await mx.close()
        if noter is not None:
            await noter.close()
        if own_geocoder and geocoder is not None:
            await geocoder.close()

    # Keyword suggestions: phrases common on the best leads' sites but rare on the rest.
    qualified = [(k, s) for k, q, s, _c in outcomes if q]
    scored = sorted(qualified, key=lambda ks: -(ks[1] or 0))
    best = [k for k, _s in scored[: max(3, round(0.4 * len(scored)))]] if any(s for _k, s in scored) else [k for k, _s in scored]
    suggestions = collector.suggest(best)

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
    ctx.flush(status=status, error=error, suggestions=suggestions)
    return store.get_run(run_id) or {}

