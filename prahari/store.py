"""Append-only snapshot store. This IS the product's memory.

Rules:
  * Snapshots are NEVER overwritten. A re-fetch for the same as_of date writes a
    new row. The diff engine always reads the latest row per (vendor, as_of).
  * Every row records `source` and `fetched_at` so an evidence record can point
    back at exactly which observation a claim came from.
  * Replay works by restricting reads to `as_of <= cutoff`, which is what makes
    the backtest honest: the scorer physically cannot see the future.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings
from .models import Snapshot, Vendor, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id            TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    domain               TEXT,
    cin                  TEXT,
    gstin                TEXT,
    open_po_value_inr    REAL DEFAULT 0,
    sole_source          INTEGER DEFAULT 0,
    lead_time_days       INTEGER DEFAULT 0,
    critical_component   INTEGER DEFAULT 0,
    category_manager     TEXT,
    crustdata_company_id TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id   TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_vendor_asof ON snapshots(vendor_id, as_of);

CREATE TABLE IF NOT EXISTS registry_facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id  TEXT NOT NULL,
    provider   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    live       INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reg_vendor ON registry_facts(vendor_id, as_of);

CREATE TABLE IF NOT EXISTS cases (
    case_id     TEXT PRIMARY KEY,
    vendor_id   TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    last_update TEXT NOT NULL,
    status      TEXT NOT NULL,          -- open | closed
    band        TEXT,
    entries_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_case_vendor ON cases(vendor_id, status);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id    TEXT PRIMARY KEY,
    vendor_id   TEXT NOT NULL,
    case_id     TEXT,
    created_at  TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    severity    TEXT NOT NULL,
    priority    TEXT NOT NULL,
    suppressed  INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_vendor ON alerts(vendor_id, as_of);

-- hysteresis + cooldown bookkeeping
CREATE TABLE IF NOT EXISTS band_state (
    vendor_id  TEXT PRIMARY KEY,
    band       TEXT NOT NULL,
    score      REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_fires (
    vendor_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    fired_at  TEXT NOT NULL,
    PRIMARY KEY (vendor_id, signal_id, fired_at)
);
"""


class Store:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = Path(db_path) if db_path else get_settings().db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)

    # --------------------------------------------------------------- vendors

    def upsert_vendor(self, v: Vendor) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT INTO vendors (vendor_id, name, domain, cin, gstin,
                       open_po_value_inr, sole_source, lead_time_days,
                       critical_component, category_manager, crustdata_company_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(vendor_id) DO UPDATE SET
                       name=excluded.name, domain=excluded.domain, cin=excluded.cin,
                       gstin=excluded.gstin, open_po_value_inr=excluded.open_po_value_inr,
                       sole_source=excluded.sole_source, lead_time_days=excluded.lead_time_days,
                       critical_component=excluded.critical_component,
                       category_manager=excluded.category_manager,
                       crustdata_company_id=COALESCE(excluded.crustdata_company_id,
                                                     vendors.crustdata_company_id)""",
                (v.vendor_id, v.name, v.domain, v.cin, v.gstin, v.open_po_value_inr,
                 int(v.sole_source), v.lead_time_days, int(v.critical_component),
                 v.category_manager, v.crustdata_company_id, utcnow()),
            )

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM vendors WHERE vendor_id=?", (vendor_id,)).fetchone()
        return self._row_to_vendor(row) if row else None

    def list_vendors(self) -> list[Vendor]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM vendors ORDER BY name").fetchall()
        return [self._row_to_vendor(r) for r in rows]

    @staticmethod
    def _row_to_vendor(r: sqlite3.Row) -> Vendor:
        return Vendor(
            vendor_id=r["vendor_id"], name=r["name"], domain=r["domain"],
            cin=r["cin"], gstin=r["gstin"],
            open_po_value_inr=r["open_po_value_inr"] or 0.0,
            sole_source=bool(r["sole_source"]), lead_time_days=r["lead_time_days"] or 0,
            critical_component=bool(r["critical_component"]),
            category_manager=r["category_manager"],
            crustdata_company_id=r["crustdata_company_id"],
        )

    # ------------------------------------------------------------- snapshots

    def append_snapshot(self, snap: Snapshot) -> int:
        """Append only. Never updates an existing row."""
        with self.conn() as c:
            cur = c.execute(
                """INSERT INTO snapshots (vendor_id, as_of, payload_json, source, fetched_at)
                   VALUES (?,?,?,?,?)""",
                (snap.vendor_id, snap.as_of, json.dumps(snap.payload, default=str),
                 snap.source, snap.fetched_at),
            )
            return int(cur.lastrowid)

    def snapshots(self, vendor_id: str, as_of_max: str | None = None) -> list[Snapshot]:
        """All snapshots for a vendor, oldest first, optionally truncated at a date.

        `as_of_max` is the backtest guardrail: the scorer cannot see past it.
        """
        q = "SELECT * FROM snapshots WHERE vendor_id=?"
        args: list[Any] = [vendor_id]
        if as_of_max:
            q += " AND as_of <= ?"
            args.append(as_of_max)
        q += " ORDER BY as_of ASC, snapshot_id ASC"
        with self.conn() as c:
            rows = c.execute(q, args).fetchall()
        return [
            Snapshot(vendor_id=r["vendor_id"], as_of=r["as_of"],
                     payload=json.loads(r["payload_json"]), source=r["source"],
                     fetched_at=r["fetched_at"], snapshot_id=r["snapshot_id"])
            for r in rows
        ]

    def latest_two(self, vendor_id: str, as_of_max: str | None = None
                   ) -> tuple[Snapshot | None, Snapshot | None]:
        """(previous, current) — the pair the diff engine consumes.

        Deduplicates by as_of so a re-fetch of the same date does not produce a
        spurious zero-delta pair.
        """
        snaps = self.snapshots(vendor_id, as_of_max)
        by_date: dict[str, Snapshot] = {}
        for s in snaps:
            by_date[s.as_of] = s          # last write for a date wins
        ordered = [by_date[k] for k in sorted(by_date)]
        if not ordered:
            return None, None
        if len(ordered) == 1:
            return None, ordered[-1]
        return ordered[-2], ordered[-1]

    # -------------------------------------------------------- registry facts

    def append_registry_fact(self, vendor_id: str, fact: dict[str, Any]) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT INTO registry_facts
                   (vendor_id, provider, kind, status, as_of, source_url, fetched_at, live, detail_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (vendor_id, fact["provider"], fact["kind"], fact["status"], fact["as_of"],
                 fact["source_url"], fact["fetched_at"], int(fact["live"]),
                 json.dumps(fact.get("detail", {}), default=str)),
            )

    def registry_facts(self, vendor_id: str, as_of_max: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM registry_facts WHERE vendor_id=?"
        args: list[Any] = [vendor_id]
        if as_of_max:
            q += " AND as_of <= ?"
            args.append(as_of_max)
        q += " ORDER BY as_of ASC"
        with self.conn() as c:
            rows = c.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            d["live"] = bool(d["live"])
            out.append(d)
        return out

    # ------------------------------------------------------- band / cooldown

    def get_band_state(self, vendor_id: str) -> tuple[str | None, float | None]:
        with self.conn() as c:
            r = c.execute("SELECT band, score FROM band_state WHERE vendor_id=?",
                          (vendor_id,)).fetchone()
        return (r["band"], r["score"]) if r else (None, None)

    def set_band_state(self, vendor_id: str, band: str, score: float) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT INTO band_state (vendor_id, band, score, updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(vendor_id) DO UPDATE SET
                     band=excluded.band, score=excluded.score, updated_at=excluded.updated_at""",
                (vendor_id, band, score, utcnow()),
            )

    def record_signal_fire(self, vendor_id: str, signal_id: str, at: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO signal_fires (vendor_id, signal_id, fired_at) VALUES (?,?,?)",
                (vendor_id, signal_id, at),
            )

    def last_signal_fire(self, vendor_id: str, signal_id: str) -> str | None:
        with self.conn() as c:
            r = c.execute(
                """SELECT MAX(fired_at) AS f FROM signal_fires
                   WHERE vendor_id=? AND signal_id=?""", (vendor_id, signal_id)).fetchone()
        return r["f"] if r and r["f"] else None

    # ------------------------------------------------------------ case files

    def open_case(self, vendor_id: str) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM cases WHERE vendor_id=? AND status='open' ORDER BY opened_at DESC",
                (vendor_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["entries"] = json.loads(d.pop("entries_json"))
        return d

    def upsert_case(self, case_id: str, vendor_id: str, band: str,
                    entries: list[dict[str, Any]], status: str = "open") -> None:
        now = utcnow()
        with self.conn() as c:
            c.execute(
                """INSERT INTO cases (case_id, vendor_id, opened_at, last_update, status, band, entries_json)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(case_id) DO UPDATE SET
                     last_update=excluded.last_update, status=excluded.status,
                     band=excluded.band, entries_json=excluded.entries_json""",
                (case_id, vendor_id, now, now, status, band, json.dumps(entries, default=str)),
            )

    def list_cases(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM cases ORDER BY last_update DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["entries"] = json.loads(d.pop("entries_json"))
            out.append(d)
        return out

    # ---------------------------------------------------------------- alerts

    def save_alert(self, alert: dict[str, Any], case_id: str | None) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO alerts
                   (alert_id, vendor_id, case_id, created_at, as_of, severity,
                    priority, suppressed, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (alert["alert_id"], alert["vendor_id"], case_id, alert["created_at"],
                 alert["as_of"], alert["severity"], alert["priority"],
                 int(alert.get("suppressed", False)), json.dumps(alert, default=str)),
            )

    def list_alerts(self, include_suppressed: bool = True) -> list[dict[str, Any]]:
        q = "SELECT payload_json FROM alerts"
        if not include_suppressed:
            q += " WHERE suppressed=0"
        q += " ORDER BY created_at DESC"
        with self.conn() as c:
            rows = c.execute(q).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def reset(self) -> None:
        """Drop everything. Used by `prahari seed --fresh`."""
        with self.conn() as c:
            for t in ("alerts", "cases", "signal_fires", "band_state",
                      "registry_facts", "snapshots", "vendors"):
                c.execute(f"DELETE FROM {t}")
