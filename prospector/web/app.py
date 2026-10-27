"""Web dashboard: FastAPI backend + a single-page UI in ./static."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ..config import Settings, load_settings
from ..export import PRESETS, to_csv, to_xlsx
from ..icp import ICPProfile, ProfileStore, slugify
from ..pipeline import RunOptions, execute_run
from ..scheduler import next_run_at, scheduler_loop
from ..storage import LEAD_STATUSES, Store

STATIC_DIR = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    profile_id: str | None = None
    profile: dict[str, Any]  # the (possibly edited) profile to run
    source: str | None = None
    details: bool = True
    enrich: bool = True


class LeadUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None


class BulkUpdate(BaseModel):
    ids: list[int]
    status: str


class JobManager:
    """Runs searches as background asyncio tasks inside the server process."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.tasks: dict[int, asyncio.Task] = {}
        self.cancels: dict[int, asyncio.Event] = {}
        self.profile_of: dict[int, str | None] = {}

    def is_profile_running(self, profile_id: str) -> bool:
        return any(pid == profile_id for rid, pid in self.profile_of.items() if rid in self.tasks)

    def start(self, run_id: int, profile: ICPProfile, options: RunOptions, profile_id: str | None = None) -> None:
        cancel = asyncio.Event()
        self.cancels[run_id] = cancel
        self.profile_of[run_id] = profile_id

        async def runner():
            try:
                await execute_run(self.settings, self.store, run_id, profile, options, cancel)
            except Exception as exc:  # pragma: no cover - last-resort safety net
                self.store.update_run(run_id, status="failed", error=f"{exc.__class__.__name__}: {exc}")
            finally:
                self.tasks.pop(run_id, None)
                self.cancels.pop(run_id, None)
                self.profile_of.pop(run_id, None)

        self.tasks[run_id] = asyncio.create_task(runner())

    def cancel(self, run_id: int) -> bool:
        event = self.cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    async def shutdown(self) -> None:
        for event in self.cancels.values():
            event.set()
        if self.tasks:
            await asyncio.wait(list(self.tasks.values()), timeout=15)


def create_app(settings: Settings | None = None, enable_scheduler: bool = True) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.db_path)
    profiles = ProfileStore(settings.profiles_dir)
    jobs = JobManager(settings, store)

    def start_scheduled(slug: str, profile: ICPProfile) -> int:
        source = settings.resolved_source()
        run_id = store.create_run(profile.model_dump(), slug, source, len(profile.search_terms()), trigger="schedule")
        jobs.start(run_id, profile, RunOptions(source=source), slug)
        return run_id

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ticker = None
        if enable_scheduler:
            ticker = asyncio.create_task(scheduler_loop(profiles, store, start_scheduled, jobs.is_profile_running))
        yield
        if ticker is not None:
            ticker.cancel()
        await jobs.shutdown()
        store.close()

    app = FastAPI(title="Prospector", lifespan=lifespan)
    app.state.settings, app.state.store, app.state.jobs, app.state.profiles = settings, store, jobs, profiles

    def _profile_or_422(data: dict) -> ICPProfile:
        try:
            return ICPProfile.model_validate(data)
        except ValidationError as exc:
            msgs = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
            raise HTTPException(422, "; ".join(msgs))

    # ------------------------------------------------------------------ settings
    @app.get("/api/settings")
    def get_settings():
        return {**settings.public_dict(), "lead_statuses": LEAD_STATUSES}

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any]):
        updates = dict(body)
        # The masked key comes back unchanged from the form; only save a real new value.
        for name in ("places_api_key", "anthropic_api_key"):
            if name in updates and ("…" in str(updates[name]) or updates[name] == "set"):
                updates.pop(name)
        try:
            settings.save_overrides(updates)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return get_settings()

    # ------------------------------------------------------------------ profiles
    @app.get("/api/profiles")
    def list_profiles():
        items = profiles.list()
        for item in items:
            if "error" not in item and item.get("schedule"):
                when = next_run_at(ICPProfile.model_validate({k: v for k, v in item.items() if k != "id"}),
                                   store.schedule_state(item["id"]))
                item["next_run_at"] = when.isoformat() if when else None
        return items

    @app.get("/api/profiles/{slug}")
    def get_profile(slug: str):
        try:
            return {"id": slug, **profiles.get(slug).model_dump()}
        except (KeyError, ValueError):
            raise HTTPException(404, "profile not found")

    @app.post("/api/profiles")
    def create_profile(body: dict[str, Any]):
        profile = _profile_or_422(body)
        slug = slugify(profile.name)
        existing = {p["id"] for p in profiles.list()}
        base, n = slug, 2
        while slug in existing:
            slug, n = f"{base}-{n}", n + 1
        profiles.save(profile, slug)
        return {"id": slug, **profile.model_dump()}

    @app.put("/api/profiles/{slug}")
    def update_profile(slug: str, body: dict[str, Any]):
        profile = _profile_or_422(body)
        try:
            profiles.save(profile, slug)
        except ValueError:
            raise HTTPException(400, "invalid profile id")
        return {"id": slug, **profile.model_dump()}

    @app.delete("/api/profiles/{slug}")
    def delete_profile(slug: str):
        try:
            profiles.delete(slug)
        except ValueError:
            raise HTTPException(400, "invalid profile id")
        return {"ok": True}

    # ------------------------------------------------------------------ runs
    @app.post("/api/runs")
    async def start_run(req: RunRequest):
        profile = _profile_or_422(req.profile)
        try:
            source = settings.resolved_source(req.source)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if source == "api" and not settings.places_api_key:
            raise HTTPException(422, "Add a Google Places API key in Settings to use the API source.")
        run_id = store.create_run(profile.model_dump(), req.profile_id, source, len(profile.search_terms()))
        jobs.start(run_id, profile, RunOptions(source=source, details=req.details, enrich=req.enrich), req.profile_id)
        return store.get_run(run_id)

    @app.get("/api/runs")
    def list_runs(limit: int = 100):
        runs = store.list_runs(limit)
        for r in runs:
            r["active"] = r["id"] in jobs.tasks
        return runs

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        run["active"] = run_id in jobs.tasks
        return run

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int):
        if not jobs.cancel(run_id):
            raise HTTPException(409, "run is not active")
        return {"ok": True}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: int):
        if run_id in jobs.tasks:
            raise HTTPException(409, "stop the run before deleting it")
        store.delete_run(run_id)
        return {"ok": True}

    # ------------------------------------------------------------------ leads
    def _ids(ids: str | None) -> list[int] | None:
        if not ids:
            return None
        try:
            return [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(422, "ids must be comma-separated numbers")

    def _query(run_id, qualified, has_email, has_website, min_score, status, q, sort, desc,
               group_chains=False, ids=None, limit=None, offset=0):
        return store.query_leads(
            run_id=run_id, qualified=qualified, has_email=has_email, has_website=has_website,
            min_score=min_score, status=status or None, search=q or None, sort=sort,
            descending=desc, limit=limit, offset=offset, group_chains=group_chains, ids=_ids(ids),
        )

    @app.get("/api/leads")
    def list_leads(
        run_id: int | None = None,
        qualified: bool | None = None,
        has_email: bool | None = None,
        has_website: bool | None = None,
        min_score: int | None = None,
        status: str | None = None,
        q: str | None = None,
        sort: str = "score",
        desc: bool = True,
        group_chains: bool = False,
        limit: int = Query(200, le=2000),
        offset: int = 0,
    ):
        leads, total = _query(run_id, qualified, has_email, has_website, min_score, status, q, sort, desc,
                              group_chains, None, limit, offset)
        return {"total": total, "leads": leads}

    @app.get("/api/leads/export")
    def export_leads(
        format: str = "csv",
        preset: str = "standard",
        run_id: int | None = None,
        qualified: bool | None = None,
        has_email: bool | None = None,
        has_website: bool | None = None,
        min_score: int | None = None,
        status: str | None = None,
        q: str | None = None,
        sort: str = "score",
        desc: bool = True,
        group_chains: bool = False,
        ids: str | None = None,
    ):
        if preset not in PRESETS:
            raise HTTPException(422, f"preset must be one of {', '.join(PRESETS)}")
        if ids:  # exporting a hand-picked selection: ignore the other filters
            leads, _ = _query(None, None, None, None, None, None, None, sort, desc, False, ids)
        else:
            leads, _ = _query(run_id, qualified, has_email, has_website, min_score, status, q, sort, desc, group_chains)
        name = ("leads-selected" if ids else f"leads-run{run_id}" if run_id else "leads") + (
            "" if preset == "standard" else f"-{preset}")
        if format == "xlsx":
            return Response(
                to_xlsx(leads, preset),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
            )
        return Response(
            to_csv(leads, preset), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )

    @app.post("/api/leads/bulk")
    def bulk_update(body: BulkUpdate):
        try:
            changed = store.update_leads(body.ids, body.status)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"updated": changed}

    # ------------------------------------------------------------------ health
    @app.get("/api/health")
    async def health(browser: bool = True):
        from ..doctor import run_checks

        return await run_checks(settings, include_browser=browser)

    @app.get("/api/leads/{lead_id}")
    def get_lead(lead_id: int):
        lead = store.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        return lead

    @app.patch("/api/leads/{lead_id}")
    def update_lead(lead_id: int, body: LeadUpdate):
        try:
            lead = store.update_lead(lead_id, status=body.status, notes=body.notes)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not lead:
            raise HTTPException(404, "lead not found")
        return lead

    # ------------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app

