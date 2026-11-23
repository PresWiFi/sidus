"""Relay's database: its own SQLite file (relay.db in the data dir).

A thread is one conversation in one folder. Each turn is one run of one agent: a message you sent, a task one
agent handed to the other, or a retry after an agent hit its usage limit.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from ..config import settings

AGENTS = ("claude", "codex")
# queued: waiting to start. running: the agent is working. done / failed / stopped: finished.
# limit: the agent hit its plan's usage limit (the work moves to the other agent).
STATUSES = ("queued", "running", "done", "failed", "stopped", "limit")

SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_threads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cwd TEXT NOT NULL,
    pin TEXT,                      -- NULL = route by usage, or 'claude' / 'codex'
    claude_session TEXT,           -- the thread's Claude Code conversation
    codex_session TEXT,            -- and its Codex conversation
    waiting TEXT,                  -- why the next turn hasn't started, if it's stuck
    archived INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_turns (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES relay_threads(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('message', 'handoff', 'retry')),
    input TEXT NOT NULL,           -- your message, or the task handed over
    notes TEXT NOT NULL DEFAULT '',-- handoff notes, or what the agent that hit its limit had done
    force_agent TEXT,              -- set for handoffs and retries: who must take it
    agent TEXT,                    -- who took it (set when it starts)
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    session_id TEXT,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '[]',
    handoff TEXT,                  -- JSON {task, notes} when the agent handed off during this turn
    consults TEXT NOT NULL DEFAULT '[]', -- questions this turn's agent asked the other one
    log TEXT,
    pid INTEGER,
    pid_started REAL,
    created REAL NOT NULL,
    started REAL,
    ended REAL
);
CREATE INDEX IF NOT EXISTS relay_turns_thread ON relay_turns (thread_id, seq);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def open_db(path=None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path or settings.data_dir / "relay.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def conn() -> sqlite3.Connection:
    return _conn or open_db()


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for key in ("steps", "files", "consults"):
        if key in d:
            d[key] = json.loads(d[key] or "[]")
    if d.get("handoff"):
        d["handoff"] = json.loads(d["handoff"])
    return d


# ---------- threads ----------

def create_thread(name: str, cwd: str, pin: str | None = None) -> dict:
    now = time.time()
    tid = uuid.uuid4().hex[:12]
    with _lock:
        conn().execute("INSERT INTO relay_threads (id, name, cwd, pin, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                       (tid, name, cwd, pin, now, now))
        conn().commit()
    return get_thread(tid)


def get_thread(tid: str) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM relay_threads WHERE id = ?", (tid,)).fetchone())


def list_threads(archived: bool = False) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM relay_threads WHERE archived = ? ORDER BY updated DESC",
                              (1 if archived else 0,)).fetchall()
    return [_row(r) for r in rows]


THREAD_FIELDS = {"name", "pin", "claude_session", "codex_session", "waiting", "archived"}


def update_thread(tid: str, **fields) -> dict:
    fields = {k: v for k, v in fields.items() if k in THREAD_FIELDS}
    with _lock:
        for key, value in fields.items():
            conn().execute(f"UPDATE relay_threads SET {key} = ? WHERE id = ?", (value, tid))
        conn().execute("UPDATE relay_threads SET updated = ? WHERE id = ?", (time.time(), tid))
        conn().commit()
    return get_thread(tid)


def delete_thread(tid: str) -> None:
    with _lock:
        conn().execute("DELETE FROM relay_threads WHERE id = ?", (tid,))
        conn().commit()


# ---------- turns ----------

def add_turn(tid: str, kind: str, text: str, *, notes: str = "", force_agent: str | None = None) -> dict:
    now = time.time()
    turn_id = uuid.uuid4().hex[:12]
    with _lock:
        seq = conn().execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM relay_turns WHERE thread_id = ?",
                             (tid,)).fetchone()[0]
        conn().execute("INSERT INTO relay_turns (id, thread_id, seq, kind, input, notes, force_agent, created) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (turn_id, tid, seq, kind, text, notes, force_agent, now))
        conn().execute("UPDATE relay_threads SET updated = ? WHERE id = ?", (now, tid))
        conn().commit()
    return get_turn(turn_id)


def get_turn(turn_id: str) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM relay_turns WHERE id = ?", (turn_id,)).fetchone())


def turns(tid: str) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM relay_turns WHERE thread_id = ? ORDER BY seq", (tid,)).fetchall()
    return [_row(r) for r in rows]


def turns_with_status(status: str) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM relay_turns WHERE status = ? ORDER BY created", (status,)).fetchall()
    return [_row(r) for r in rows]


TURN_FIELDS = {"agent", "reason", "status", "session_id", "result", "error", "steps", "files", "handoff", "log", "consults",
               "pid", "pid_started", "started", "ended", "notes"}


def update_turn(turn_id: str, **fields) -> dict:
    fields = {k: v for k, v in fields.items() if k in TURN_FIELDS}
    with _lock:
        for key, value in fields.items():
            if key in ("steps", "files", "consults") or (key == "handoff" and value is not None):
                value = json.dumps(value)
            conn().execute(f"UPDATE relay_turns SET {key} = ? WHERE id = ?", (value, turn_id))
        row = conn().execute("SELECT thread_id FROM relay_turns WHERE id = ?", (turn_id,)).fetchone()
        if row:
            conn().execute("UPDATE relay_threads SET updated = ? WHERE id = ?", (time.time(), row[0]))
        conn().commit()
    return get_turn(turn_id)


def running_turn(tid: str) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM relay_turns WHERE thread_id = ? AND status = 'running' "
                                   "ORDER BY seq LIMIT 1", (tid,)).fetchone())


def next_queued(tid: str) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM relay_turns WHERE thread_id = ? AND status = 'queued' "
                                   "ORDER BY seq LIMIT 1", (tid,)).fetchone())


def threads_with_queue() -> list[str]:
    with _lock:
        rows = conn().execute("SELECT DISTINCT t.thread_id FROM relay_turns t JOIN relay_threads h ON h.id = t.thread_id "
                              "WHERE t.status = 'queued' AND h.archived = 0").fetchall()
    return [r[0] for r in rows]
