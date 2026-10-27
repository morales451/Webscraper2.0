"""SQLite storage for search runs and the leads they find."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    profile_id TEXT,
    profile_name TEXT,
    profile_json TEXT NOT NULL,
    source TEXT,
    searches_total INTEGER DEFAULT 0,
    searches_done INTEGER DEFAULT 0,
    found INTEGER DEFAULT 0,
    skipped_seen INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    qualified INTEGER DEFAULT 0,
    error TEXT,
    log TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    place_key TEXT NOT NULL,
    name TEXT,
    category TEXT,
    categories TEXT DEFAULT '[]',
    address TEXT,
    phone TEXT,
    website TEXT,
    domain TEXT,
    rating REAL,
    reviews INTEGER,
    maps_url TEXT,
    latitude REAL,
    longitude REAL,
    business_status TEXT,
    query TEXT,
    emails TEXT DEFAULT '[]',
    site_title TEXT,
    site_description TEXT,
    site_error TEXT,
    pages_crawled INTEGER DEFAULT 0,
    score INTEGER,
    qualified INTEGER NOT NULL DEFAULT 0,
    reasons TEXT DEFAULT '[]',
    matches TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, place_key)
);

CREATE INDEX IF NOT EXISTS idx_leads_place_key ON leads(place_key);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_run ON leads(run_id);
"""

JSON_FIELDS = ("categories", "emails", "reasons", "matches")
LEAD_STATUSES = ("new", "contacted", "replied", "won", "lost", "ignored")
RUN_FINAL_STATES = ("done", "failed", "cancelled")

SORTABLE = {
    "score": "score",
    "name": "name COLLATE NOCASE",
    "rating": "rating",
    "reviews": "reviews",
    "created": "id",
    "category": "category COLLATE NOCASE",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            # Runs that were in flight when the app last stopped can never finish.
            self._conn.execute(
                "UPDATE runs SET status='failed', error='interrupted (app was stopped)', finished_at=? "
                "WHERE status IN ('queued','running')",
                (now_iso(),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # ------------------------------------------------------------------ runs
    def create_run(self, profile_json: dict, profile_id: str | None, source: str, searches_total: int) -> int:
        cur = self._exec(
            "INSERT INTO runs (created_at, status, profile_id, profile_name, profile_json, source, searches_total) "
            "VALUES (?, 'queued', ?, ?, ?, ?, ?)",
            (now_iso(), profile_id, profile_json.get("name"), json.dumps(profile_json), source, searches_total),
        )
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "log" in fields and not isinstance(fields["log"], str):
            fields["log"] = json.dumps(fields["log"])
        if fields.get("status") in RUN_FINAL_STATES and "finished_at" not in fields:
            fields["finished_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def get_run(self, run_id: int) -> dict | None:
        row = self._exec("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_dict(row) if row else None

    def list_runs(self, limit: int = 100) -> list[dict]:
        rows = self._exec("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._run_dict(r, with_log=False) for r in rows]

    def delete_run(self, run_id: int) -> None:
        self._exec("DELETE FROM runs WHERE id=?", (run_id,))

    @staticmethod
    def _run_dict(row: sqlite3.Row, with_log: bool = True) -> dict:
        d = dict(row)
        d["profile"] = json.loads(d.pop("profile_json") or "{}")
        log = d.pop("log")
        if with_log:
            d["log"] = json.loads(log or "[]")
        return d

    # ------------------------------------------------------------------ leads
    def seen_before(self, place_key: str, domain: str | None, exclude_run: int | None = None) -> bool:
        """Has this business been found by an earlier run? (matched by place id or website domain)"""
        sql = "SELECT 1 FROM leads WHERE (place_key=?"
        params: list = [place_key]
        if domain:
            sql += " OR domain=?"
            params.append(domain)
        sql += ")"
        if exclude_run is not None:
            sql += " AND run_id<>?"
            params.append(exclude_run)
        return self._exec(sql + " LIMIT 1", tuple(params)).fetchone() is not None

    def save_lead(self, run_id: int, lead: dict) -> int:
        data = {k: v for k, v in lead.items()}
        for f in JSON_FIELDS:
            if f in data and not isinstance(data[f], str):
                data[f] = json.dumps(data[f])
        data["run_id"] = run_id
        data.setdefault("created_at", now_iso())
        data["qualified"] = 1 if data.get("qualified") else 0
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        cur = self._exec(
            f"INSERT OR REPLACE INTO leads ({cols}) VALUES ({marks})", tuple(data.values())
        )
        return int(cur.lastrowid)

    def update_lead(self, lead_id: int, **fields: Any) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in ("status", "notes") and v is not None}
        if "status" in allowed and allowed["status"] not in LEAD_STATUSES:
            raise ValueError(f"status must be one of {', '.join(LEAD_STATUSES)}")
        if allowed:
            cols = ", ".join(f"{k}=?" for k in allowed)
            self._exec(f"UPDATE leads SET {cols} WHERE id=?", (*allowed.values(), lead_id))
        return self.get_lead(lead_id)

    def get_lead(self, lead_id: int) -> dict | None:
        row = self._exec("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return self._lead_dict(row) if row else None

    def query_leads(
        self,
        run_id: int | None = None,
        qualified: bool | None = None,
        has_email: bool | None = None,
        has_website: bool | None = None,
        min_score: int | None = None,
        status: str | None = None,
        search: str | None = None,
        sort: str = "score",
        descending: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        where, params = ["1=1"], []
        if run_id is not None:
            where.append("run_id=?")
            params.append(run_id)
        if qualified is not None:
            where.append("qualified=?")
            params.append(1 if qualified else 0)
        if has_email is not None:
            where.append("emails <> '[]'" if has_email else "emails = '[]'")
        if has_website is not None:
            where.append("COALESCE(website,'') <> ''" if has_website else "COALESCE(website,'') = ''")
        if min_score is not None:
            where.append("COALESCE(score, 0) >= ?")
            params.append(min_score)
        if status:
            where.append("status=?")
            params.append(status)
        if search:
            where.append("(name LIKE ? OR category LIKE ? OR address LIKE ? OR emails LIKE ? OR website LIKE ? OR notes LIKE ?)")
            params.extend([f"%{search}%"] * 6)
        clause = " AND ".join(where)
        total = self._exec(f"SELECT COUNT(*) FROM leads WHERE {clause}", tuple(params)).fetchone()[0]
        order = SORTABLE.get(sort, "score")
        direction = "DESC" if descending else "ASC"
        sql = f"SELECT * FROM leads WHERE {clause} ORDER BY {order} IS NULL, {order} {direction}, id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = self._exec(sql, tuple(params)).fetchall()
        return [self._lead_dict(r) for r in rows], int(total)

    @staticmethod
    def _lead_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for f in JSON_FIELDS:
            try:
                d[f] = json.loads(d.get(f) or "[]")
            except json.JSONDecodeError:
                d[f] = []
        d["qualified"] = bool(d["qualified"])
        return d
