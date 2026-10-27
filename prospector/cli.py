"""Command-line interface.

  prospector serve                              # open the web dashboard
  prospector search --profile dental-implants   # run a saved ICP profile
  prospector search -q "roofer" -l "Austin, TX" -k "commercial roofing|3" -k "metal roof*" --out leads.xlsx
  prospector profiles                           # list saved profiles
  prospector runs                               # list past runs
  prospector export --run 3 --out run3.csv      # export a past run
  prospector search -p roofers --split-areas --max-areas 16   # grid-search big cities
  prospector export --run 3 --preset hubspot --out hubspot.csv
  prospector doctor                             # check Google Maps, websites, email checks still work
  prospector setup                              # download Chromium for the browser scraper
  prospector browser-login                      # sign in to Google in the scraper's browser profile
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .config import load_settings
from .export import to_csv, to_xlsx
from .icp import ICPProfile, ProfileStore
from .pipeline import RunOptions, execute_run
from .storage import Store


def _write_export(path: Path, leads: list[dict], preset: str = "standard") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = to_xlsx(leads, preset) if path.suffix.lower() == ".xlsx" else to_csv(leads, preset)
    path.write_bytes(data)
    print(f"Saved {len(leads)} lead(s) to {path}")


def cmd_search(args, settings) -> int:
    profiles = ProfileStore(settings.profiles_dir)
    if args.profile:
        try:
            profile = profiles.get(args.profile)
        except KeyError:
            print(f"No profile '{args.profile}'. Available: {', '.join(p['id'] for p in profiles.list()) or 'none'}")
            return 2
        profile_id = args.profile
    else:
        if not args.query:
            print("Give --profile NAME or at least one --query.")
            return 2
        profile = ICPProfile(name=args.name or "Quick search", queries=args.query)
        profile_id = None

    updates: dict = {}
    if args.query and args.profile:
        updates["queries"] = args.query
    if args.location:
        updates["locations"] = args.location
    if args.keyword:
        updates["keywords"] = args.keyword
    if args.require:
        updates["required_keywords"] = args.require
    if args.exclude:
        updates["exclude_keywords"] = args.exclude
    if args.per_search:
        updates["max_results_per_search"] = args.per_search
    if args.limit:
        updates["max_leads"] = args.limit
    if args.min_score is not None:
        updates["min_score"] = args.min_score
    if args.include_seen:
        updates["skip_seen"] = False
    if args.split_areas:
        updates["split_areas"] = True
    if args.max_areas:
        updates["max_areas"] = args.max_areas
    if args.expand:
        updates["expand_queries"] = True
    if args.ai_notes:
        updates["ai_notes"] = True
    if args.no_email_check:
        updates["check_emails"] = False
    if updates:
        profile = ICPProfile.model_validate({**profile.model_dump(), **updates})

    if args.visible:
        settings.headless = False

    store = Store(settings.db_path)
    run_id = store.create_run(profile.model_dump(), profile_id, settings.resolved_source(args.source), len(profile.search_terms()))
    options = RunOptions(source=args.source, details=not args.fast, enrich=not args.no_website)

    def on_lead(lead: dict) -> None:
        mark = "✓" if lead["qualified"] else "·"
        score = "" if lead["score"] is None else f"{lead['score']:>3}"
        email = (lead["emails"] or [""])[0]
        print(f"  {mark} {score:>3}  {lead['name'][:40]:<40}  {email}")
        if lead.get("ai_note"):
            print(f"         AI: {lead['ai_note']}")

    try:
        run = asyncio.run(execute_run(settings, store, run_id, profile, options, on_log=lambda m: print(f"» {m}"), on_lead=on_lead))
    except KeyboardInterrupt:
        print("Stopped.")
        store.update_run(run_id, status="cancelled")
        run = store.get_run(run_id) or {}

    leads, _ = store.query_leads(run_id=run_id, qualified=None if args.all else True)
    print(f"\nRun #{run_id}: {run.get('status')} — {run.get('qualified', 0)} qualified of {run.get('processed', 0)} checked")
    if run.get("error"):
        print(f"Problem: {run['error']}")
    if run.get("suggestions"):
        print("Keyword ideas (common on your best leads' sites): "
              + ", ".join(s["term"] for s in run["suggestions"][:10]))
    if args.out:
        _write_export(Path(args.out), leads, args.preset)
    elif leads:
        print(f"Export with: prospector export --run {run_id} --out leads.xlsx")
    return 0 if run.get("status") == "done" else 1


def cmd_profiles(args, settings) -> int:
    items = ProfileStore(settings.profiles_dir).list()
    if not items:
        print(f"No profiles yet in {settings.profiles_dir}")
    for p in items:
        if "error" in p:
            print(f"{p['id']:<28} (invalid file: {p['error']})")
            continue
        print(f"{p['id']:<28} {p['name']}  —  {len(p['queries'])} queries × {len(p['locations']) or 1} locations, {len(p['keywords'])} keywords")
    return 0


def cmd_runs(args, settings) -> int:
    store = Store(settings.db_path)
    for r in store.list_runs(args.limit):
        print(f"#{r['id']:<4} {r['created_at'][:16]}  {r['status']:<9} {r['profile_name'] or '':<30} {r['qualified']}/{r['processed']} qualified")
    return 0


def cmd_export(args, settings) -> int:
    store = Store(settings.db_path)
    leads, _ = store.query_leads(
        run_id=args.run,
        qualified=None if args.all else True,
        has_email=True if args.with_email else None,
        group_chains=args.group_chains,
    )
    _write_export(Path(args.out), leads, args.preset)
    return 0


def cmd_doctor(args, settings) -> int:
    from .doctor import run_checks

    print("Checking each part of the pipeline (takes ~20 seconds)…")
    results = asyncio.run(run_checks(settings, include_browser=not args.skip_browser))
    labels = {"google_maps_browser": "Google Maps (browser)", "places_api": "Google Places API",
              "website_crawler": "Website crawler", "email_check": "Email domain check", "area_split": "Area splitting"}
    failed = False
    for key, result in results.items():
        mark = "✓" if result["ok"] else ("–" if result["ok"] is None else "✗")
        failed |= result["ok"] is False
        print(f"  {mark} {labels.get(key, key):<24} {result['message']}")
    return 1 if failed else 0


def cmd_serve(args, settings) -> int:
    import uvicorn

    host = args.host or settings.host
    port = args.port or settings.port
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    print(f"Prospector dashboard: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        import threading

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("prospector.web.app:create_app", factory=True, host=host, port=port, log_level="warning")
    return 0


def cmd_setup(args, settings) -> int:
    print("Downloading Chromium for Playwright (one-time, ~150 MB)…")
    return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])


def cmd_browser_login(args, settings) -> int:
    """Open the scraper's persistent browser so you can sign in to Google once."""
    from playwright.async_api import async_playwright

    profile_dir = settings.data_dir / "browser-profile"

    async def run():
        async with async_playwright() as pw:
            kwargs = {"headless": False}
            if settings.chromium_path:
                kwargs["executable_path"] = settings.chromium_path
            ctx = await pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://accounts.google.com/ServiceLogin?continue=https://www.google.com/maps")
            print("Sign in to Google in the window that opened, then close the window.")
            closed = asyncio.Event()
            ctx.on("close", lambda *_: closed.set())
            await closed.wait()

    asyncio.run(run())
    settings.save_overrides({"use_browser_profile": True})
    print(f"Saved. The browser scraper will now use {profile_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prospector", description="Find and qualify prospects from Google Maps.")
    parser.add_argument("--version", action="version", version=f"prospector {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="Run a search (saved profile or ad-hoc)")
    s.add_argument("--profile", "-p", help="Saved profile id (see `prospector profiles`)")
    s.add_argument("--name", help="Name for an ad-hoc search")
    s.add_argument("--query", "-q", action="append", help="What to search on Google Maps (repeatable)")
    s.add_argument("--location", "-l", action="append", help="Where to search (repeatable)")
    s.add_argument("--keyword", "-k", action="append", help="Website keyword, optionally 'term|weight' (repeatable)")
    s.add_argument("--require", action="append", help="At least one of these must appear on the site (repeatable)")
    s.add_argument("--exclude", action="append", help="Disqualify if this appears on the site (repeatable)")
    s.add_argument("--per-search", type=int, help="Max Google Maps results per query×location")
    s.add_argument("--limit", type=int, help="Max leads for the whole run")
    s.add_argument("--min-score", type=int, help="Minimum keyword score (0-100) to qualify")
    s.add_argument("--source", choices=["auto", "api", "browser"], help="Listing source (default from settings)")
    s.add_argument("--fast", action="store_true", help="Browser source: don't open each listing (no full address)")
    s.add_argument("--no-website", action="store_true", help="Skip crawling websites")
    s.add_argument("--include-seen", action="store_true", help="Include businesses found in earlier runs")
    s.add_argument("--visible", action="store_true", help="Show the browser window while scraping")
    s.add_argument("--split-areas", action="store_true", help="Search each location as a grid of smaller areas")
    s.add_argument("--max-areas", type=int, help="Max grid areas per location (default 9)")
    s.add_argument("--expand", action="store_true", help="Also search related categories found on qualified leads")
    s.add_argument("--ai-notes", action="store_true", help="Add a one-line Claude fit note to qualified leads")
    s.add_argument("--no-email-check", action="store_true", help="Don't check that email domains accept mail")
    s.add_argument("--preset", choices=["standard", "hubspot", "outreach"], default="standard",
                   help="Export layout for --out")
    s.add_argument("--all", action="store_true", help="Export unqualified leads too")
    s.add_argument("--out", "-o", help="Save results to .csv or .xlsx")
    s.set_defaults(func=cmd_search)

    p = sub.add_parser("profiles", help="List saved ICP profiles")
    p.set_defaults(func=cmd_profiles)

    r = sub.add_parser("runs", help="List past runs")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_runs)

    e = sub.add_parser("export", help="Export leads to CSV/XLSX")
    e.add_argument("--run", type=int, help="Run id (default: every run)")
    e.add_argument("--all", action="store_true", help="Include unqualified leads")
    e.add_argument("--with-email", action="store_true", help="Only leads with an email address")
    e.add_argument("--group-chains", action="store_true", help="One row per website (best-scoring branch)")
    e.add_argument("--preset", choices=["standard", "hubspot", "outreach"], default="standard",
                   help="standard = everything; hubspot = HubSpot import; outreach = cold-email tools")
    e.add_argument("--out", "-o", required=True)
    e.set_defaults(func=cmd_export)

    v = sub.add_parser("serve", help="Start the web dashboard")
    v.add_argument("--host")
    v.add_argument("--port", type=int)
    v.add_argument("--no-browser", action="store_true", help="Don't open the dashboard automatically")
    v.set_defaults(func=cmd_serve)

    d = sub.add_parser("doctor", help="Check that Google Maps scraping, websites and email checks work")
    d.add_argument("--skip-browser", action="store_true", help="Skip the Google Maps browser check")
    d.set_defaults(func=cmd_doctor)

    st = sub.add_parser("setup", help="Download Chromium for the browser scraper")
    st.set_defaults(func=cmd_setup)

    bl = sub.add_parser("browser-login", help="Sign in to Google in the scraper's browser (optional)")
    bl.set_defaults(func=cmd_browser_login)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    return args.func(args, settings) or 0


if __name__ == "__main__":
    sys.exit(main())
