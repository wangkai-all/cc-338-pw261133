"""SQLite persistence for the withdrawal ledger and the mock services."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active','quarantined','retracted')),
    state_basis     TEXT NOT NULL DEFAULT '',
    state_reason    TEXT NOT NULL DEFAULT '',
    state_changed_at TEXT NOT NULL,
    retracted_at    TEXT,
    restored_at     TEXT,
    manual_hold     INTEGER NOT NULL DEFAULT 0,
    hold_reason     TEXT NOT NULL DEFAULT '',
    review_needed   INTEGER NOT NULL DEFAULT 0,
    review_note     TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_seen_at    TEXT,
    last_content_hash TEXT,
    published_hash  TEXT,
    excerpt         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS batches (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    mode         TEXT NOT NULL DEFAULT 'manual',
    regions_json TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'running',
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scans (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    region        TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'catalog',
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    http_status   INTEGER,
    listing_count INTEGER,
    complete      INTEGER NOT NULL DEFAULT 0,
    anomaly       INTEGER NOT NULL DEFAULT 0,
    detail_json   TEXT NOT NULL DEFAULT '{}',
    parent_scan_id TEXT,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS scans_batch ON scans(batch_id);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id         TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    region          TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    catalog         TEXT NOT NULL,
    listing_http    INTEGER,
    listing_complete INTEGER NOT NULL DEFAULT 0,
    anomaly         INTEGER NOT NULL DEFAULT 0,
    anomaly_reason  TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT 'not_checked',
    body_http       INTEGER,
    body_bytes      INTEGER NOT NULL DEFAULT 0,
    body_checked_at TEXT,
    content_hash    TEXT,
    excerpt         TEXT NOT NULL DEFAULT '',
    detail_json     TEXT NOT NULL DEFAULT '{}',
    UNIQUE(scan_id, item_id)
);
CREATE INDEX IF NOT EXISTS observations_item ON observations(item_id, observed_at);
CREATE INDEX IF NOT EXISTS observations_batch ON observations(batch_id);

CREATE TABLE IF NOT EXISTS notices (
    notice_id   TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL,
    source      TEXT NOT NULL,
    statement   TEXT NOT NULL DEFAULT '',
    effective_at TEXT,
    received_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    item_id      TEXT,
    region       TEXT,
    scan_id      TEXT,
    state        TEXT NOT NULL DEFAULT 'queued'
                 CHECK (state IN ('queued','running','done','failed','cancelled')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_run_at  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    last_error   TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key   TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(state, next_run_at);

CREATE TABLE IF NOT EXISTS outbox (
    id             TEXT PRIMARY KEY,
    item_id        TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    payload_json   TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'queued'
                   CHECK (state IN ('queued','inflight','delivered','dead')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    delivered_at   TEXT,
    last_error     TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    item_id     TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS body_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    excerpt      TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    region       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,
    at           TEXT NOT NULL,
    decision     TEXT NOT NULL,
    basis        TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    policy_json  TEXT NOT NULL DEFAULT '{}',
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS decisions_item ON evidence_decisions(item_id, at);

CREATE TABLE IF NOT EXISTS holds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    released_at TEXT,
    created_by TEXT NOT NULL DEFAULT 'operator'
);
"""

DOWNSTREAM_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS d_items (
    item_id     TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'published'
                CHECK (status IN ('published','withheld')),
    seq         INTEGER NOT NULL DEFAULT 0,
    event_id    TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    excerpt     TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    published_at TEXT,
    last_event  TEXT NOT NULL DEFAULT '',
    permanently_restricted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS d_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    item_id      TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    outcome      TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS d_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def init_downstream_db(path: Path | str) -> None:
    conn = connect(path)
    try:
        conn.executescript(DOWNSTREAM_SCHEMA)
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def rows(conn: sqlite3.Connection, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def row(conn: sqlite3.Connection, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def bump_metric(conn: sqlite3.Connection, key: str, amount: int = 1) -> None:
    conn.execute(
        "INSERT INTO metrics(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
        (key, amount),
    )


def get_metric(conn: sqlite3.Connection, key: str) -> int:
    r = row(conn, "SELECT value FROM metrics WHERE key=?", (key,))
    return int(r["value"]) if r else 0


def setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    r = row(conn, "SELECT value FROM settings WHERE key=?", (key,))
    return str(r["value"]) if r else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
