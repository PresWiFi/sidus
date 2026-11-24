"""Team's database: its own SQLite file (team.db in the data dir).

A plan is the work for one Ideas milestone: a manager (Claude or Codex) proposes tasks, you approve them, and
workers do them one at a time while the manager reviews each result. Every agent run is a row in team_runs.
team_comments is the shared thread everyone reads: the manager's instructions, workers' reports, your notes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from ..config import settings

# planning: the manager is drafting tasks. questions: the manager asked you something. proposed: tasks waiting for
# your approval. running: tasks being worked. paused: stopped by you or by a problem. done / cancelled: finished.
PLAN_STATUSES = ("planning", "questions", "proposed", "running", "paused", "done", "cancelled")
# todo: waiting its turn. working: a worker has it. review: the manager is checking it.
TASK_STATUSES = ("todo", "working", "review", "done", "failed", "skipped")
ASSIGNEES = ("claude", "codex", "any")
# plan: the manager drafts tasks (or asks you questions). work: a worker does a task. review: the manager looks at
# a task that failed. wrapup: the manager checks the milestone once every task is done. ask: one agent's question
# to the other.
ROLES = ("plan", "work", "review", "wrapup", "ask")

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_plans (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL,
    milestone_id INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    request TEXT NOT NULL DEFAULT '',   -- what you asked for when starting the plan
    status TEXT NOT NULL DEFAULT 'planning',
    manager TEXT,                       -- who manages: claude or codex
    claude_session TEXT,                -- the manager's conversation, per agent
    codex_session TEXT,
    summary TEXT NOT NULL DEFAULT '',   -- the manager's plan summary, then its wrap-up
    questions TEXT NOT NULL DEFAULT '', -- what the manager asked you, while status = 'questions'
    waiting TEXT,                       -- why nothing is running, if something should be
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_tasks (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES team_plans(id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS team_tasks_plan ON team_tasks (plan_id, position);
CREATE TABLE IF NOT EXISTS team_runs (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES team_plans(id) ON DELETE CASCADE,
    task_id TEXT,
    role TEXT NOT NULL,
    agent TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',   -- running, done, failed, stopped, limit
    verdict TEXT,                             -- for reviews: approved / revise / none
    session_id TEXT,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '[]',
    tokens INTEGER NOT NULL DEFAULT 0,        -- new (uncached) input + output tokens, as the agent reported
    git_before TEXT,                          -- the folder's git state when the run started (JSON)
    log TEXT,
    pid INTEGER,
    pid_started REAL,
    started REAL NOT NULL,
    ended REAL
);
CREATE INDEX IF NOT EXISTS team_runs_plan ON team_runs (plan_id, started);
CREATE TABLE IF NOT EXISTS team_comments (
    id INTEGER PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES team_plans(id) ON DELETE CASCADE,
    task_id TEXT,
    author TEXT NOT NULL,                     -- you, manager, claude, codex, sidus
    agent TEXT,                               -- for manager comments: which agent was managing
    text TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS team_comments_plan ON team_comments (plan_id, created);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
JSON_FIELDS = ("steps", "files")


def open_db(path=None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path or settings.data_dir / "team.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
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


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _update(table: str, allowed: set, row_id: str, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if k in allowed}
    with _lock:
        for key, value in fields.items():
            if key in JSON_FIELDS:
                value = json.dumps(value)
            conn().execute(f"UPDATE {table} SET {key} = ? WHERE id = ?", (value, row_id))
        if table in ("team_plans", "team_tasks"):
            conn().execute(f"UPDATE {table} SET updated = ? WHERE id = ?", (time.time(), row_id))
        conn().commit()


# ---------- plans ----------

def create_plan(project_id: int, milestone_id: int, cwd: str, request: str = "") -> dict:
    now, pid = time.time(), _new_id()
    with _lock:
        conn().execute("INSERT INTO team_plans (id, project_id, milestone_id, cwd, request, created, updated) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (pid, project_id, milestone_id, cwd, request, now, now))
        conn().commit()
    return get_plan(pid)


def get_plan(plan_id: str) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM team_plans WHERE id = ?", (plan_id,)).fetchone())


def list_plans() -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute("SELECT * FROM team_plans ORDER BY updated DESC")]


def active_plan(milestone_id: int) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM team_plans WHERE milestone_id = ? AND status NOT IN ('done', "
                                   "'cancelled') ORDER BY created DESC LIMIT 1", (milestone_id,)).fetchone())


def busy_project_plan(project_id: int) -> dict | None:
    """A plan of this project with work in flight (only one runs at a time per project folder)."""
    with _lock:
        return _row(conn().execute("SELECT * FROM team_plans WHERE project_id = ? AND status IN ('planning', "
                                   "'running') ORDER BY created LIMIT 1", (project_id,)).fetchone())


def update_plan(plan_id: str, **fields) -> dict:
    _update("team_plans", {"status", "manager", "claude_session", "codex_session", "summary", "questions", "waiting"}, plan_id, fields)
    return get_plan(plan_id)


def delete_plan(plan_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_plans WHERE id = ?", (plan_id,))
        conn().commit()


def plans_to_kick() -> list[str]:
    with _lock:
        return [r[0] for r in conn().execute("SELECT id FROM team_plans WHERE status IN ('planning', 'running')")]


# ---------- tasks ----------

def add_task(plan_id: str, title: str, details: str = "", done_when: str = "", assignee: str = "any",
             position: int | None = None) -> dict:
    now, tid = time.time(), _new_id()
    with _lock:
        if position is None:
            position = conn().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM team_tasks WHERE plan_id = ?",
                                      (plan_id,)).fetchone()[0]
        else:
            conn().execute("UPDATE team_tasks SET position = position + 1 WHERE plan_id = ? AND position >= ?",
                           (plan_id, position))
        conn().execute("INSERT INTO team_tasks (id, plan_id, position, title, details, done_when, assignee, created, "
                       "updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (tid, plan_id, position, title, details, done_when,
                        assignee if assignee in ASSIGNEES else "any", now, now))
        conn().commit()
    return get_task(tid)


def get_task(task_id: str) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM team_tasks WHERE id = ?", (task_id,)).fetchone())


def tasks(plan_id: str) -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute("SELECT * FROM team_tasks WHERE plan_id = ? ORDER BY position",
                                                (plan_id,))]


def update_task(task_id: str, **fields) -> dict:
    _update("team_tasks", {"title", "details", "done_when", "assignee", "status", "worker", "claude_session",
                           "codex_session", "attempts", "report", "files"}, task_id, fields)
    return get_task(task_id)


def delete_task(task_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_tasks WHERE id = ?", (task_id,))
        conn().commit()


def move_task(task_id: str, position: int) -> None:
    """Moves a task to a 1-based position in its plan, renumbering the rest."""
    task = get_task(task_id)
    with _lock:
        ids = [t["id"] for t in tasks(task["plan_id"]) if t["id"] != task_id]
        ids.insert(max(0, min(len(ids), position - 1)), task_id)
        for i, tid in enumerate(ids, start=1):
            conn().execute("UPDATE team_tasks SET position = ? WHERE id = ?", (i, tid))
        conn().commit()


def clear_tasks(plan_id: str) -> None:
    with _lock:
        conn().execute("DELETE FROM team_tasks WHERE plan_id = ?", (plan_id,))
        conn().commit()


# ---------- runs ----------

def add_run(plan_id: str, role: str, agent: str, *, task_id: str | None = None, reason: str = "") -> dict:
    rid = _new_id()
    with _lock:
        conn().execute("INSERT INTO team_runs (id, plan_id, task_id, role, agent, reason, started) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (rid, plan_id, task_id, role, agent, reason, time.time()))
        conn().execute("UPDATE team_plans SET updated = ? WHERE id = ?", (time.time(), plan_id))
        conn().commit()
    return get_run(rid)


def get_run(run_id: str) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM team_runs WHERE id = ?", (run_id,)).fetchone())


def runs(plan_id: str) -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute("SELECT * FROM team_runs WHERE plan_id = ? ORDER BY started",
                                                (plan_id,))]


def running_run(plan_id: str, *, include_asks: bool = False) -> dict | None:
    with _lock:
        return _row(conn().execute("SELECT * FROM team_runs WHERE plan_id = ? AND status = 'running'"
                                   + ("" if include_asks else " AND role != 'ask'") + " LIMIT 1",
                                   (plan_id,)).fetchone())


def runs_with_status(status: str) -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute("SELECT * FROM team_runs WHERE status = ?", (status,))]


def update_run(run_id: str, **fields) -> dict:
    _update("team_runs", {"status", "verdict", "session_id", "result", "error", "steps", "files", "tokens", "log",
                          "git_before", "pid", "pid_started", "ended"}, run_id, fields)
    return get_run(run_id)


# ---------- comments ----------

def comment(plan_id: str, author: str, text: str, *, task_id: str | None = None, agent: str | None = None) -> dict:
    with _lock:
        cur = conn().execute("INSERT INTO team_comments (plan_id, task_id, author, agent, text, created) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (plan_id, task_id, author, agent, text, time.time()))
        conn().execute("UPDATE team_plans SET updated = ? WHERE id = ?", (time.time(), plan_id))
        conn().commit()
        return dict(conn().execute("SELECT * FROM team_comments WHERE id = ?", (cur.lastrowid,)).fetchone())


def comments(plan_id: str, task_id: str | None = None, *, plan_level: bool = False) -> list[dict]:
    """All of a plan's comments; or one task's; or (plan_level) only those not on a task."""
    sql, args = "SELECT * FROM team_comments WHERE plan_id = ?", [plan_id]
    if task_id:
        sql, args = sql + " AND task_id = ?", args + [task_id]
    elif plan_level:
        sql += " AND task_id IS NULL"
    with _lock:
        return [dict(r) for r in conn().execute(sql + " ORDER BY created, id", args)]
