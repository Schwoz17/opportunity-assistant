"""
Database layer.

Two modes, chosen automatically:
  - TURSO_DATABASE_URL + TURSO_AUTH_TOKEN set  -> shared cloud database
    (needed once Telegram runs on GitHub Actions and WhatsApp runs on
    Render, since those are two separate machines that must see the
    same pipeline)
  - neither set  -> local opportunities.db file, exactly as before
    (what you've been testing with on your laptop)

Tables:
  opportunities : one row per opportunity (scholarship, internship, job, ...)
  checklist     : requirement items extracted per opportunity
  nudges        : reminder log (never double-send)
  owners        : your chat identity per channel (telegram + whatsapp)
  state         : small key-value store for cross-run bookkeeping
                  (Telegram's last-processed update id, last discovery
                  scan window) — needed because a GitHub Actions run
                  starts fresh each time and remembers nothing on its
                  own local disk.
"""

import os
import sqlite3
import json
import requests
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path(__file__).parent / "opportunities.db"

TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)

STATUSES = ["seen", "interested", "drafting", "submitted", "rejected", "won"]

CATEGORIES = ["scholarship", "internship", "fellowship", "job", "grant",
              "hackathon", "other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    url         TEXT UNIQUE,
    category    TEXT DEFAULT 'other',
    deadline    TEXT,
    status      TEXT DEFAULT 'seen',
    source      TEXT,
    notes       TEXT,
    extraction  TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checklist (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    item           TEXT NOT NULL,
    type           TEXT,
    effort         TEXT,
    required       INTEGER DEFAULT 1,
    done           INTEGER DEFAULT 0,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS nudges (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    sent_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(opportunity_id, kind)
);

CREATE TABLE IF NOT EXISTS owners (
    channel  TEXT PRIMARY KEY,
    chat_id  TEXT NOT NULL,
    added_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# executescript() with multiple ';'-separated statements isn't guaranteed
# across every backend, so the schema is applied one statement at a time —
# works identically on local sqlite3 and on Turso's HTTP API.
_SCHEMA_STATEMENTS = [s.strip() for s in SCHEMA.split(";") if s.strip()]


# ---------------------------------------------------------------------------
# Turso connection over plain HTTP (Turso's "SQL over HTTP" API).
#
# Deliberately NOT using the `libsql` package: it ships as a Rust
# extension with no prebuilt wheel for every Python version, so
# `pip install libsql` tries to compile it from source and fails on any
# Windows machine without a full C++ build toolchain installed. This
# talks to the exact same database using only `requests`, which never
# needs compiling anything.
#
# Reference: https://docs.turso.tech/sdk/http/reference
# ---------------------------------------------------------------------------

def _turso_encode(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _turso_decode(cell):
    if cell is None:
        return None
    t = cell.get("type")
    if t == "null":
        return None
    val = cell.get("value")
    if t == "integer":
        return int(val)
    if t == "float":
        return float(val)
    return val  # text, blob


class _TursoRow:
    """Mimics sqlite3.Row: supports row['col'] and dict(row)."""

    def __init__(self, cols, values):
        self._cols = cols
        self._values = [_turso_decode(v) for v in values]

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._cols.index(key)]
        return self._values[key]

    def keys(self):
        return list(self._cols)


class _TursoCursor:
    def __init__(self, cols, rows, last_insert_rowid):
        self._rows = [_TursoRow(cols, r) for r in rows]
        self._pos = 0
        self.lastrowid = int(last_insert_rowid) if last_insert_rowid is not None else None

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows


class _TursoConnection:
    """Minimal sqlite3-connection-shaped wrapper around Turso's plain
    HTTP API. Every execute() is its own short-lived round trip (Turso
    auto-commits each statement server-side), so commit()/close() are
    no-ops — that keeps this a drop-in match for how db.py already
    uses sqlite3.Connection everywhere below."""

    def __init__(self, database_url: str, auth_token: str):
        base = database_url
        if base.startswith("libsql://"):
            base = "https://" + base[len("libsql://"):]
        elif base.startswith("libsql:"):
            base = "https:" + base[len("libsql:"):]
        self._pipeline_url = base.rstrip("/") + "/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    def execute(self, sql: str, params=None):
        args = [_turso_encode(p) for p in (params or [])]
        body = {"requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": args}},
            {"type": "close"},
        ]}
        resp = requests.post(self._pipeline_url, headers=self._headers,
                             json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        result = data["results"][0]
        if result["type"] == "error":
            msg = str(result.get("error", ""))
            if "constraint" in msg.lower():
                # Raised as sqlite3.IntegrityError so the existing
                # `except sqlite3.IntegrityError:` blocks below catch
                # it identically whether running on Turso or locally.
                raise sqlite3.IntegrityError(msg)
            raise RuntimeError(f"Turso error: {msg}")
        r = result["response"]["result"]
        cols = [c["name"] for c in r.get("cols", [])]
        return _TursoCursor(cols, r.get("rows", []), r.get("last_insert_rowid"))

    def executemany(self, sql: str, seq_of_params):
        last = None
        for params in seq_of_params:
            last = self.execute(sql, params)
        return last

    def commit(self):
        pass

    def close(self):
        pass


def connect():
    if USE_TURSO:
        conn = _TursoConnection(TURSO_URL, TURSO_TOKEN)
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)
    return conn


# ---------------------------------------------------------------------------
# Owners (multi-channel identity)
# ---------------------------------------------------------------------------

def lock_owner(channel: str, chat_id: str):
    conn = connect()
    try:
        conn.execute("INSERT INTO owners (channel, chat_id) VALUES (?, ?)",
                     (channel, str(chat_id)))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def get_owner(channel: str) -> str | None:
    conn = connect()
    row = conn.execute("SELECT chat_id FROM owners WHERE channel = ?",
                       (channel,)).fetchone()
    conn.close()
    return row["chat_id"] if row else None


def all_owners() -> dict:
    conn = connect()
    rows = conn.execute("SELECT channel, chat_id FROM owners").fetchall()
    conn.close()
    return {r["channel"]: r["chat_id"] for r in rows}


def is_owner(channel: str, chat_id) -> bool:
    owner = get_owner(channel)
    return owner is not None and owner == str(chat_id)


# ---------------------------------------------------------------------------
# State (small key-value store for cross-run bookkeeping)
# ---------------------------------------------------------------------------

def get_state(key: str) -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row is not None else None


def set_state(key: str, value: str):
    conn = connect()
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------

def add_opportunity(title, url=None, category="other", deadline=None,
                    source="manual", notes=None, extraction=None) -> int:
    if category not in CATEGORIES:
        category = "other"
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO opportunities "
            "(title, url, category, deadline, source, notes, extraction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, url, category, deadline, source, notes,
             json.dumps(extraction) if extraction else None),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT id FROM opportunities WHERE url = ?",
                           (url,)).fetchone()
        return row["id"] if row else -1
    finally:
        conn.close()


def set_status(oid: int, status: str) -> bool:
    if status not in STATUSES:
        return False
    conn = connect()
    conn.execute(
        "UPDATE opportunities SET status = ?, updated_at = datetime('now') "
        "WHERE id = ?", (status, oid))
    conn.commit()
    conn.close()
    return True


def set_category(oid: int, category: str) -> bool:
    if category not in CATEGORIES:
        return False
    conn = connect()
    conn.execute(
        "UPDATE opportunities SET category = ?, updated_at = datetime('now') "
        "WHERE id = ?", (category, oid))
    conn.commit()
    conn.close()
    return True


def set_deadline(oid: int, deadline_iso: str):
    conn = connect()
    conn.execute(
        "UPDATE opportunities SET deadline = ?, updated_at = datetime('now') "
        "WHERE id = ?", (deadline_iso, oid))
    conn.commit()
    conn.close()


def get(oid: int):
    conn = connect()
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?",
                       (oid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_active(category: str | None = None):
    conn = connect()
    if category:
        rows = conn.execute(
            "SELECT * FROM opportunities "
            "WHERE status IN ('seen','interested','drafting') AND category = ? "
            "ORDER BY deadline IS NULL, deadline ASC", (category,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM opportunities "
            "WHERE status IN ('seen','interested','drafting') "
            "ORDER BY deadline IS NULL, deadline ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def url_exists(url: str) -> bool:
    conn = connect()
    row = conn.execute("SELECT 1 FROM opportunities WHERE url = ?",
                       (url,)).fetchone()
    conn.close()
    return row is not None


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------

def add_checklist_items(oid: int, fields: list):
    conn = connect()
    conn.executemany(
        "INSERT INTO checklist (opportunity_id, item, type, effort, required, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (oid, f["field"], f.get("type"), f.get("effort", "quick"),
             1 if f.get("required", True) else 0, f.get("notes"))
            for f in fields
        ],
    )
    conn.commit()
    conn.close()


def get_checklist(oid: int):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM checklist WHERE opportunity_id = ? "
        "ORDER BY effort = 'quick', id", (oid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_item_done(item_id: int, done: bool = True):
    conn = connect()
    conn.execute("UPDATE checklist SET done = ? WHERE id = ?",
                 (1 if done else 0, item_id))
    conn.commit()
    conn.close()


def checklist_progress(oid: int) -> tuple:
    items = get_checklist(oid)
    if not items:
        return (0, 0)
    return (sum(1 for i in items if i["done"]), len(items))


# ---------------------------------------------------------------------------
# Nudges
# ---------------------------------------------------------------------------

def nudge_already_sent(oid: int, kind: str) -> bool:
    conn = connect()
    row = conn.execute(
        "SELECT 1 FROM nudges WHERE opportunity_id = ? AND kind = ?",
        (oid, kind)).fetchone()
    conn.close()
    return row is not None


def record_nudge(oid: int, kind: str):
    conn = connect()
    try:
        conn.execute("INSERT INTO nudges (opportunity_id, kind) VALUES (?, ?)",
                     (oid, kind))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def days_until(deadline_iso: str) -> int | None:
    try:
        d = datetime.strptime(deadline_iso, "%Y-%m-%d").date()
        return (d - date.today()).days
    except (ValueError, TypeError):
        return None
