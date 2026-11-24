"""Team's database: its own SQLite file (team.db in the data dir).

A goal is what you hand the team: a few lines or a whole runbook, for one folder. The manager (Claude or Codex)
splits it into phases; you approve the phases once; then each phase's tasks are planned and worked one at a time.
Every agent run is a row in team_runs. team_comments is the shared thread everyone reads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from ..config import settings

SCHEMA_VERSION = 1
# planning: the manager is outlining. questions: it asked you something. proposed: phases waiting for your
# approval. running: phases being worked. paused: stopped by you or by a problem. done / cancelled: finished.
GOAL_STATUSES = ("planning", "questions", "proposed", "running", "paused", "done", "cancelled")
PHASE_STATUSES = ("todo", "active", "done", "skipped")
# todo: waiting its turn. working: a worker has it. review: the manager is deciding on it.
TASK_STATUSES = ("todo", "working", "review", "done", "failed", "skipped")
ASSIGNEES = ("claude", "codex", "any")
# outline: the manager splits the goal into phases (and plans phase 1's tasks). phase: it plans tasks for a phase
# that has none. wrapup: it checks a finished phase and plans the next. review: it decides on a blocked or failed
# task. work: a worker does a task. ask: one agent's question to the other.
ROLES = ("outline", "phase", "wrapup", "review", "work", "ask")

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    cwd TEXT NOT NULL,
    goal TEXT NOT NULL,                 -- what you asked for (also saved as goal.md next to the run logs)
    runbook TEXT,                       -- a runbook file in the folder, if you pointed at one
    status TEXT NOT NULL DEFAULT 'planning',
    manager TEXT,                       -- who manages: claude or codex
    claude_session TEXT,                -- the manager's conversation, per agent
    codex_session TEXT,
    summary TEXT NOT NULL DEFAULT '',   -- the manager's outline summary, then its final summary
    questions TEXT NOT NULL DEFAULT '', -- what the manager asked you, while status = 'questions'
    waiting TEXT,                       -- why nothing is running, if something should be
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_phases (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES team_goals(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',   -- what the phase covers (for a runbook: which section)
    done_when TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    summary TEXT NOT NULL DEFAULT '',   -- the manager's note when it finished the phase
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_tasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES team_goals(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL REFERENCES team_phases(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    done_when TEXT NOT NULL DEFAULT '',
    assignee TEXT NOT NULL DEFAULT 'any',
    status TEXT NOT NULL DEFAULT 'todo',
    worker TEXT,                        -- who did it last
    claude_session TEXT,                -- each worker's conversation for this task, resumed on revisions
    codex_session TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    report TEXT NOT NULL DEFAULT '',    -- the last worker report
    files TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS team_tasks_phase ON team_tasks (phase_id, position);
CREATE TABLE IF NOT EXISTS team_runs (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES team_goals(id) ON DELETE CASCADE,
    phase_id TEXT,
    task_id TEXT,
    role TEXT NOT NULL,
    agent TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',   -- running, done, failed, stopped, limit
    verdict TEXT,                             -- blocked (workers); approved / revise / skipped (reviews)
    session_id TEXT,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '[]',
    tokens INTEGER NOT NULL DEFAULT 0,        -- new (uncached) input + output tokens, as the agent reported
    git_before TEXT,                          -- the folder's git state when a work run started (JSON)
    log TEXT,
    pid INTEGER,
    pid_started REAL,
    started REAL NOT NULL,
    ended REAL
);
CREATE INDEX IF NOT EXISTS team_runs_goal ON team_runs (goal_id, started);
CREATE TABLE IF NOT EXISTS team_comments (
    id INTEGER PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES team_goals(id) ON DELETE CASCADE,
    task_id TEXT,
    author TEXT NOT NULL,                     -- you, manager, claude, codex, sidus
    agent TEXT,                               -- for manager comments: which agent was managing
    text TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS team_comments_goal ON team_comments (goal_id, created);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
JSON_FIELDS = ("steps", "files")


def open_db(path=None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        c = sqlite3.connect(path or settings.data_dir / "team.db", check_same_thread=False)
        c.row_factory = sqlite3.Row
        if c.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # Sidus 0.7.0 planned per Ideas milestone (team_plans); its tables don't fit goals. Drop them.
            c.executescript("DROP TABLE IF EXISTS team_comments; DROP TABLE IF EXISTS team_runs; "
                            "DROP TABLE IF EXISTS team_tasks; DROP TABLE IF EXISTS team_plans;")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.execute("PRAGMA foreign_keys = ON")
        c.executescript(SCHEMA)
        c.commit()
        _conn = c
    return _conn


def conn() -> sqlite3.Connection:
    return _conn or open_db()


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for key in JSON_FIELDS:
        if key in d:
            d[key] = json.loads(d[key] or "[]")
    return d


def _rows(sql: str, args=()) -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute(sql, args)]


def _one(sql: str, args=()) -> dict | None:
    with _lock:
        return _row(conn().execute(sql, args).fetchone())


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _update(table: str, allowed: set, row_id: str, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if k in allowed}
    with _lock:
        for key, value in fields.items():
            if key in JSON_FIELDS:
                value = json.dumps(value)
            conn().execute(f"UPDATE {table} SET {key} = ? WHERE id = ?", (value, row_id))
        if table != "team_runs":
            conn().execute(f"UPDATE {table} SET updated = ? WHERE id = ?", (time.time(), row_id))
        conn().commit()


def _touch(goal_id: str) -> None:
    conn().execute("UPDATE team_goals SET updated = ? WHERE id = ?", (time.time(), goal_id))


# ---------- goals ----------

def create_goal(title: str, cwd: str, goal: str, runbook: str | None = None) -> dict:
    now, gid = time.time(), _new_id()
    with _lock:
        conn().execute("INSERT INTO team_goals (id, title, cwd, goal, runbook, created, updated) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (gid, title, cwd, goal, runbook, now, now))
        conn().commit()
    return get_goal(gid)


def get_goal(goal_id: str) -> dict | None:
    return _one("SELECT * FROM team_goals WHERE id = ?", (goal_id,))


def list_goals() -> list[dict]:
    return _rows("SELECT * FROM team_goals ORDER BY updated DESC")


def update_goal(goal_id: str, **fields) -> dict:
    _update("team_goals", {"title", "status", "manager", "claude_session", "codex_session", "summary", "questions",
                           "waiting"}, goal_id, fields)
    return get_goal(goal_id)


def delete_goal(goal_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_goals WHERE id = ?", (goal_id,))
        conn().commit()


def goals_to_kick() -> list[str]:
    return [g["id"] for g in _rows("SELECT id FROM team_goals WHERE status IN ('planning', 'running')")]


# ---------- phases ----------

def add_phase(goal_id: str, title: str, details: str = "", done_when: str = "") -> dict:
    now, pid = time.time(), _new_id()
    with _lock:
        position = conn().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM team_phases WHERE goal_id = ?",
                                  (goal_id,)).fetchone()[0]
        conn().execute("INSERT INTO team_phases (id, goal_id, position, title, details, done_when, created, updated) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (pid, goal_id, position, title, details, done_when, now, now))
        _touch(goal_id)
        conn().commit()
    return get_phase(pid)


def get_phase(phase_id: str) -> dict | None:
    return _one("SELECT * FROM team_phases WHERE id = ?", (phase_id,))


def phases(goal_id: str) -> list[dict]:
    return _rows("SELECT * FROM team_phases WHERE goal_id = ? ORDER BY position", (goal_id,))


def update_phase(phase_id: str, **fields) -> dict:
    _update("team_phases", {"title", "details", "done_when", "status", "summary"}, phase_id, fields)
    return get_phase(phase_id)


def delete_phase(phase_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_phases WHERE id = ?", (phase_id,))
        conn().commit()


def clear_phases(goal_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_phases WHERE goal_id = ?", (goal_id,))
        conn().commit()


def _renumber(table: str, parent_col: str, parent: str, row_id: str, position: int) -> None:
    with _lock:
        ids = [r[0] for r in conn().execute(f"SELECT id FROM {table} WHERE {parent_col} = ? AND id != ? "
                                            "ORDER BY position", (parent, row_id))]
        ids.insert(max(0, min(len(ids), position - 1)), row_id)
        for i, rid in enumerate(ids, start=1):
            conn().execute(f"UPDATE {table} SET position = ? WHERE id = ?", (i, rid))
        conn().commit()


def move_phase(phase_id: str, position: int) -> None:
    _renumber("team_phases", "goal_id", get_phase(phase_id)["goal_id"], phase_id, position)


def current_phase(goal_id: str) -> dict | None:
    """The first phase that isn't done or skipped."""
    return _one("SELECT * FROM team_phases WHERE goal_id = ? AND status IN ('todo', 'active') ORDER BY position "
                "LIMIT 1", (goal_id,))


# ---------- tasks ----------

def add_task(phase_id: str, title: str, details: str = "", done_when: str = "", assignee: str = "any") -> dict:
    phase = get_phase(phase_id)
    now, tid = time.time(), _new_id()
    with _lock:
        position = conn().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM team_tasks WHERE phase_id = ?",
                                  (phase_id,)).fetchone()[0]
        conn().execute("INSERT INTO team_tasks (id, goal_id, phase_id, position, title, details, done_when, assignee, "
                       "created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (tid, phase["goal_id"], phase_id, position, title, details, done_when,
                        assignee if assignee in ASSIGNEES else "any", now, now))
        _touch(phase["goal_id"])
        conn().commit()
    return get_task(tid)


def get_task(task_id: str) -> dict | None:
    return _one("SELECT * FROM team_tasks WHERE id = ?", (task_id,))


def tasks(goal_id: str, phase_id: str | None = None) -> list[dict]:
    if phase_id:
        return _rows("SELECT * FROM team_tasks WHERE phase_id = ? ORDER BY position", (phase_id,))
    return _rows("SELECT t.* FROM team_tasks t JOIN team_phases p ON p.id = t.phase_id WHERE t.goal_id = ? "
                 "ORDER BY p.position, t.position", (goal_id,))


def update_task(task_id: str, **fields) -> dict:
    _update("team_tasks", {"title", "details", "done_when", "assignee", "status", "worker", "claude_session",
                           "codex_session", "attempts", "report", "files"}, task_id, fields)
    return get_task(task_id)


def delete_task(task_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_tasks WHERE id = ?", (task_id,))
        conn().commit()


def move_task(task_id: str, position: int) -> None:
    _renumber("team_tasks", "phase_id", get_task(task_id)["phase_id"], task_id, position)


def clear_tasks(phase_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_tasks WHERE phase_id = ?", (phase_id,))
        conn().commit()


# ---------- runs ----------

def add_run(goal_id: str, role: str, agent: str, *, phase_id: str | None = None, task_id: str | None = None,
            reason: str = "") -> dict:
    rid = _new_id()
    with _lock:
        conn().execute("INSERT INTO team_runs (id, goal_id, phase_id, task_id, role, agent, reason, started) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (rid, goal_id, phase_id, task_id, role, agent, reason,
                                                           time.time()))
        _touch(goal_id)
        conn().commit()
    return get_run(rid)


def get_run(run_id: str) -> dict | None:
    return _one("SELECT * FROM team_runs WHERE id = ?", (run_id,))


def runs(goal_id: str) -> list[dict]:
    return _rows("SELECT * FROM team_runs WHERE goal_id = ? ORDER BY started", (goal_id,))


def running_run(goal_id: str, *, include_asks: bool = False) -> dict | None:
    return _one("SELECT * FROM team_runs WHERE goal_id = ? AND status = 'running'"
                + ("" if include_asks else " AND role != 'ask'") + " LIMIT 1", (goal_id,))


def runs_with_status(status: str) -> list[dict]:
    return _rows("SELECT * FROM team_runs WHERE status = ?", (status,))


def update_run(run_id: str, **fields) -> dict:
    _update("team_runs", {"status", "verdict", "session_id", "result", "error", "steps", "files", "tokens", "log",
                          "git_before", "pid", "pid_started", "ended"}, run_id, fields)
    return get_run(run_id)


# ---------- comments ----------

def comment(goal_id: str, author: str, text: str, *, task_id: str | None = None, agent: str | None = None) -> dict:
    with _lock:
        cur = conn().execute("INSERT INTO team_comments (goal_id, task_id, author, agent, text, created) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (goal_id, task_id, author, agent, text, time.time()))
        _touch(goal_id)
        conn().commit()
        return dict(conn().execute("SELECT * FROM team_comments WHERE id = ?", (cur.lastrowid,)).fetchone())


def comments(goal_id: str, task_id: str | None = None, *, goal_level: bool = False) -> list[dict]:
    """All of a goal's comments; or one task's; or (goal_level) only those not on a task."""
    sql, args = "SELECT * FROM team_comments WHERE goal_id = ?", [goal_id]
    if task_id:
        sql, args = sql + " AND task_id = ?", args + [task_id]
    elif goal_level:
        sql += " AND task_id IS NULL"
    with _lock:
        return [dict(r) for r in conn().execute(sql + " ORDER BY created, id", args)]
