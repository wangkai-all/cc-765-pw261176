"""SQLite storage: append-only receipts, revisions, isolation records, audit log.

Design rules enforced here (and tested):

* A receipt is never updated or deleted.  A correction inserts a new row whose
  ``revision_of`` names the receipt it replaces; the superseded row stays in
  the database for audit.
* The active set excludes any receipt named by a later revision.  Revisions
  therefore *replace* a contribution instead of adding one.
* Several active receipts for one unit are allowed only across batches (a
  split report); they are pooled before the weight cap is applied.
* An isolation is a recorded decision with its before/after impact; releasing
  it also keeps the record.  No path deletes a receipt.

A fresh demo load creates a new dataset rather than overwriting an earlier one.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .estimators import Summary

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scenario TEXT,
    seed INTEGER,
    truth REAL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    summary_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    period TEXT NOT NULL,
    mean REAL NOT NULL,
    std REAL NOT NULL,
    n_effective REAL NOT NULL,
    range_low REAL NOT NULL,
    range_high REAL NOT NULL,
    reported_total REAL,
    revision_of TEXT,
    received_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'intake',
    injected INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    UNIQUE(dataset_id, summary_id)
);
CREATE INDEX IF NOT EXISTS idx_summaries_ds ON summaries(dataset_id, unit_id, metric, period);
CREATE INDEX IF NOT EXISTS idx_summaries_batch ON summaries(dataset_id, batch_id);

CREATE TABLE IF NOT EXISTS isolations (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    period TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    before_center REAL,
    after_center REAL,
    before_weight REAL,
    after_weight REAL,
    before_trimmed REAL,
    after_trimmed REAL,
    created_at REAL NOT NULL,
    released_at REAL,
    release_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_isolation_ds ON isolations(dataset_id, unit_id);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS simulations (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    params TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Conflict(Exception):
    """A request that would break an invariant (duplicate receipt, etc.)."""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


def create_dataset(conn, *, name: str, scenario: str | None = None, seed: int | None = None,
                   truth: float | None = None, metadata: dict | None = None) -> str:
    ds_id = "ds-" + uuid.uuid4().hex[:10]
    with transaction(conn):
        conn.execute(
            "INSERT INTO datasets (id, name, scenario, seed, truth, metadata, created_at) VALUES (?,?,?,?,?,?,?)",
            (ds_id, name, scenario, seed, truth,
             json.dumps(metadata or {}, ensure_ascii=False), _now()),
        )
    return ds_id


def get_dataset(conn, ds_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metadata"] = json.loads(d["metadata"] or "{}")
    return d


def list_datasets(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["metadata"] = json.loads(d["metadata"] or "{}")
        c = conn.execute(
            "SELECT COUNT(*) AS receipts, COUNT(DISTINCT unit_id) AS units, "
            "COUNT(DISTINCT batch_id) AS batches FROM summaries WHERE dataset_id = ?",
            (row["id"],),
        ).fetchone()
        d.update(dict(c))
        out.append(d)
    return out


def latest_dataset(conn) -> dict | None:
    row = conn.execute("SELECT id FROM datasets ORDER BY created_at DESC LIMIT 1").fetchone()
    return get_dataset(conn, row["id"]) if row else None


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def insert_summary(conn, dataset_id: str, s: Summary, *, source: str = "intake",
                   injected: bool = False, note: str | None = None) -> str:
    row_id = uuid.uuid4().hex
    with transaction(conn):
        conn.execute(
            """INSERT INTO summaries (id, dataset_id, summary_id, unit_id, batch_id, metric, period,
                   mean, std, n_effective, range_low, range_high, reported_total, revision_of,
                   received_at, source, injected, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row_id, dataset_id, s.summary_id, s.unit_id, s.batch_id, s.metric, s.period,
             float(s.mean), float(s.std), float(s.n_effective), float(s.range_low),
             float(s.range_high), None if s.reported_total is None else float(s.reported_total),
             s.revision_of, _now(), source, 1 if injected else 0, note),
        )
    return row_id


def receipt_by_summary_id(conn, dataset_id: str, summary_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM summaries WHERE dataset_id = ? AND summary_id = ?", (dataset_id, summary_id)
    ).fetchone()
    return dict(row) if row else None


def superseded_ids(conn, dataset_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT revision_of FROM summaries WHERE dataset_id = ? AND revision_of IS NOT NULL",
        (dataset_id,),
    ).fetchall()
    return {r["revision_of"] for r in rows}


def active_rows(conn, dataset_id: str, *, metric: str | None = None,
                period: str | None = None, batch_id: str | None = None) -> list[dict]:
    """Active receipts = not named by a revision.  Latest receipt first."""
    clauses = ["dataset_id = ?"]
    params: list = [dataset_id]
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    if period:
        clauses.append("period = ?")
        params.append(period)
    if batch_id:
        clauses.append("batch_id = ?")
        params.append(batch_id)
    rows = conn.execute(
        "SELECT * FROM summaries WHERE " + " AND ".join(clauses)
        + " ORDER BY received_at ASC, rowid ASC",
        params,
    ).fetchall()
    superseded = superseded_ids(conn, dataset_id)
    return [dict(r) for r in rows if r["summary_id"] not in superseded]


def rows_to_summaries(rows) -> list[Summary]:
    return [
        Summary(
            summary_id=r["summary_id"], unit_id=r["unit_id"], batch_id=r["batch_id"],
            metric=r["metric"], period=r["period"], mean=r["mean"], std=r["std"],
            n_effective=r["n_effective"], range_low=r["range_low"], range_high=r["range_high"],
            reported_total=r["reported_total"], revision_of=r["revision_of"],
        )
        for r in rows
    ]


def active_summaries(conn, dataset_id: str, *, metric=None, period=None, batch_id=None) -> list[Summary]:
    return rows_to_summaries(active_rows(conn, dataset_id, metric=metric, period=period, batch_id=batch_id))


def active_meta(conn, dataset_id: str, *, metric=None, period=None) -> list[dict]:
    rows = active_rows(conn, dataset_id, metric=metric, period=period)
    grouped: dict[tuple, dict] = {}
    for r in rows:
        grouped.setdefault((r["unit_id"], r["metric"], r["period"]), [])
    for r in rows:
        grouped[(r["unit_id"], r["metric"], r["period"])].append(r)
    return rows


def history_for_unit(conn, dataset_id: str, unit_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM summaries WHERE dataset_id = ? AND unit_id = ? ORDER BY received_at ASC, rowid ASC",
        (dataset_id, unit_id),
    ).fetchall()
    superseded = superseded_ids(conn, dataset_id)
    out = []
    for r in rows:
        d = dict(r)
        d["superseded"] = d["summary_id"] in superseded
        out.append(d)
    return out


def batches(conn, dataset_id: str) -> list[dict]:
    """One row per batch, whatever mix of sources it now contains."""
    rows = conn.execute(
        "SELECT batch_id, GROUP_CONCAT(DISTINCT source) AS sources, COUNT(*) AS receipts, "
        "COUNT(DISTINCT unit_id) AS units, MIN(received_at) AS first_seen, MAX(received_at) AS last_seen "
        "FROM summaries WHERE dataset_id = ? GROUP BY batch_id ORDER BY first_seen",
        (dataset_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source"] = (d.pop("sources") or "").split(",")[0]
        out.append(d)
    return out


def batch_receipts(conn, dataset_id: str, batch_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM summaries WHERE dataset_id = ? AND batch_id = ? ORDER BY received_at ASC",
        (dataset_id, batch_id),
    ).fetchall()
    superseded = superseded_ids(conn, dataset_id)
    out = []
    for r in rows:
        d = dict(r)
        d["superseded"] = d["summary_id"] in superseded
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Isolations
# ---------------------------------------------------------------------------


def active_isolation(conn, dataset_id: str, unit_id: str, metric: str, period: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM isolations WHERE dataset_id = ? AND unit_id = ? AND metric = ? AND period = ? "
        "AND released_at IS NULL",
        (dataset_id, unit_id, metric, period),
    ).fetchone()
    return dict(row) if row else None


def add_isolation(conn, dataset_id: str, *, unit_id: str, metric: str, period: str, reason: str,
                  decided_by: str, before_center, after_center, before_weight, after_weight,
                  before_trimmed=None, after_trimmed=None) -> str:
    iso_id = "iso-" + uuid.uuid4().hex[:10]
    with transaction(conn):
        conn.execute(
            """INSERT INTO isolations (id, dataset_id, unit_id, metric, period, reason, decided_by,
                   before_center, after_center, before_weight, after_weight,
                   before_trimmed, after_trimmed, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iso_id, dataset_id, unit_id, metric, period, reason, decided_by,
             _finite(before_center), _finite(after_center), _finite(before_weight),
             _finite(after_weight), _finite(before_trimmed), _finite(after_trimmed), _now()),
        )
    return iso_id


def release_isolation(conn, dataset_id: str, isolation_id: str, *, note: str) -> bool:
    with transaction(conn):
        cur = conn.execute(
            "UPDATE isolations SET released_at = ?, release_note = ? "
            "WHERE id = ? AND dataset_id = ? AND released_at IS NULL",
            (_now(), note, isolation_id, dataset_id),
        )
    return cur.rowcount > 0


def isolations_all(conn, dataset_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM isolations WHERE dataset_id = ? ORDER BY created_at DESC", (dataset_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def active_isolated_units(conn, dataset_id: str, *, metric: str, period: str) -> set[str]:
    rows = conn.execute(
        "SELECT unit_id FROM isolations WHERE dataset_id = ? AND metric = ? AND period = ? "
        "AND released_at IS NULL",
        (dataset_id, metric, period),
    ).fetchall()
    return {r["unit_id"] for r in rows}


def _finite(v):
    if v is None:
        return None
    return float(v) if math.isfinite(float(v)) else None


# ---------------------------------------------------------------------------
# Audit log / simulation cache
# ---------------------------------------------------------------------------


def record_decision(conn, dataset_id: str, kind: str, payload: dict):
    with transaction(conn):
        conn.execute(
            "INSERT INTO decisions (id, dataset_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, dataset_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                            default=str), _now()),
        )


def recent_decisions(conn, dataset_id: str, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decisions WHERE dataset_id = ? ORDER BY created_at DESC LIMIT ?",
        (dataset_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def cache_simulation(conn, dataset_id: str, kind: str, params: dict, result: dict) -> str:
    sim_id = uuid.uuid4().hex[:10]
    with transaction(conn):
        conn.execute(
            "INSERT INTO simulations (id, dataset_id, kind, params, result, created_at) VALUES (?,?,?,?,?,?)",
            (sim_id, dataset_id, kind, json.dumps(params, sort_keys=True, default=str),
             json.dumps(result, ensure_ascii=False, default=str), _now()),
        )
    return sim_id


def latest_simulation(conn, dataset_id: str, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM simulations WHERE dataset_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 1",
        (dataset_id, kind),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"])
    d["result"] = json.loads(d["result"])
    return d
