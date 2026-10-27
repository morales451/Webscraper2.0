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
from ..export import to_csv, to_xlsx
from ..icp import ICPProfile, ProfileStore, slugify
from ..pipeline import RunOptions, execute_run
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


class JobManager:
    """Runs searches as background asyncio tasks inside the server process."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.tasks: dict[int, asyncio.Task] = {}
        self.cancels: dict[int, asyncio.Event] = {}

    def start(self, run_id: int, profile: ICPProfile, options: RunOptions) -> None:
        cancel = asyncio.Event()
        self.cancels[run_id] = cancel

        async def runner():
            try:
                await execute_run(self.settings, self.store, run_id, profile, options, cancel)
            except Exception as exc:  # pragma: no cover - last-resort safety net
                self.store.update_run(run_id, status="failed", error=f"{exc.__class__.__name__}: {exc}")
            finally:
                self.tasks.pop(run_id, None)
                self.cancels.pop(run_id, None)

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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.db_path)
    profiles = ProfileStore(settings.profiles_dir)
    jobs = JobManager(settings, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
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
        if "places_api_key" in updates and ("…" in str(updates["places_api_key"]) or updates["places_api_key"] == "set"):
            updates.pop("places_api_key")
        try:
            settings.save_overrides(updates)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return get_settings()

    # ------------------------------------------------------------------ profiles
    @app.get("/api/profiles")
    def list_profiles():
        return profiles.list()

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
        jobs.start(run_id, profile, RunOptions(source=source, details=req.details, enrich=req.enrich))
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
    def _query(run_id, qualified, has_email, has_website, min_score, status, q, sort, desc, limit=None, offset=0):
        return store.query_leads(
            run_id=run_id, qualified=qualified, has_email=has_email, has_website=has_website,
            min_score=min_score, status=status or None, search=q or None, sort=sort,
            descending=desc, limit=limit, offset=offset,
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
        limit: int = Query(200, le=2000),
        offset: int = 0,
    ):
        leads, total = _query(run_id, qualified, has_email, has_website, min_score, status, q, sort, desc, limit, offset)
        return {"total": total, "leads": leads}

    @app.get("/api/leads/export")
    def export_leads(
        format: str = "csv",
        run_id: int | None = None,
        qualified: bool | None = None,
        has_email: bool | None = None,
        has_website: bool | None = None,
        min_score: int | None = None,
        status: str | None = None,
        q: str | None = None,
        sort: str = "score",
        desc: bool = True,
    ):
        leads, _ = _query(run_id, qualified, has_email, has_website, min_score, status, q, sort, desc)
        name = f"leads-run{run_id}" if run_id else "leads"
        if format == "xlsx":
            return Response(
                to_xlsx(leads),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
            )
        return Response(
            to_csv(leads), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )

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

