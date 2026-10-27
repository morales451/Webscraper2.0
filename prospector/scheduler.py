"""Re-run profiles on a schedule while the dashboard is running.

A profile with ``schedule: daily`` or ``schedule: weekly`` runs again once that much time has
passed since its last scheduled run. With "skip businesses found in earlier runs" on (the
default), each scheduled run only brings in businesses that are new since last time.

The dashboard has to be open for this to work. For runs while it's closed, call the CLI from
your OS scheduler instead (Task Scheduler on Windows, cron/launchd on macOS) — see the README.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from .icp import ICPProfile, ProfileStore
from .storage import Store

INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}
CHECK_EVERY_SECONDS = 60

log = logging.getLogger(__name__)


def next_run_at(profile: ICPProfile, state: dict | None) -> datetime | None:
    interval = INTERVALS.get(profile.schedule or "")
    if interval is None:
        return None
    if not state or not state.get("last_run_at"):
        return datetime.fromtimestamp(0, timezone.utc)  # never run: due right away
    return datetime.fromisoformat(state["last_run_at"]) + interval


def due_profiles(profiles: ProfileStore, store: Store, now: datetime | None = None) -> list[tuple[str, ICPProfile]]:
    now = now or datetime.now(timezone.utc)
    out = []
    for item in profiles.list():
        if "error" in item or not item.get("schedule"):
            continue
        slug = item["id"]
        profile = ICPProfile.model_validate({k: v for k, v in item.items() if k != "id"})
        when = next_run_at(profile, store.schedule_state(slug))
        if when is not None and when <= now:
            out.append((slug, profile))
    return out


async def scheduler_loop(
    profiles: ProfileStore,
    store: Store,
    start_run: Callable[[str, ICPProfile], int],
    is_running: Callable[[str], bool],
) -> None:
    while True:
        try:
            for slug, profile in due_profiles(profiles, store):
                if is_running(slug):
                    continue
                run_id = start_run(slug, profile)
                store.mark_scheduled_run(slug, run_id)
        except Exception:  # keep the loop alive; problems show up on the run itself
            log.exception("scheduler tick failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
