"""REST API for Team, under /api/team. The web UI signs in with the cookie (plus X-Sidus on writes); the MCP server
uses the bearer token, which only works for /api/team/mcp/*."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from .. import files
from ..config import settings
from . import db, engine, parse, procs, usage

router = APIRouter(prefix="/api/team")
STATIC = Path(__file__).with_name("static")
MAX_GOAL = 200_000   # characters; a long runbook is fine, it stays in a file


def _text(n: int, required: bool = False):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1 if required else 0, max_length=n)]


Assignee = Literal["claude", "codex", "any"]


async def _call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except engine.Refused as e:
        raise HTTPException(409, str(e))


def _goal(goal_id: str) -> dict:
    goal = db.get_goal(goal_id)
    if not goal:
        raise HTTPException(404, "Goal not found")
    return goal


def _live(run: dict) -> dict:
    """A running run's progress so far, read from its log."""
    if run["status"] != "running" or not run["log"]:
        return run
    p = parse.read(run["agent"], run["log"])
    return {**run, "steps": p["steps"], "files": p["files"], "result": p["text"], "tokens": p["tokens"]}


def _summary(goal: dict) -> dict:
    phases = db.phases(goal["id"])
    runs = db.runs(goal["id"])
    current = next((p for p in phases if p["status"] in ("todo", "active")), None)
    return {**{k: v for k, v in goal.items() if k != "goal"}, "tokens": sum(r["tokens"] for r in runs),
            "phases_done": sum(p["status"] in ("done", "skipped") for p in phases), "phases_total": len(phases),
            "current_phase": current["title"] if current else None,
            "running": any(r["status"] == "running" and r["role"] != "ask" for r in runs)}


def _detail(goal: dict) -> dict:
    tasks = db.tasks(goal["id"])
    phases = [{**p, "tasks": [t for t in tasks if t["phase_id"] == p["id"]]} for p in db.phases(goal["id"])]
    return {**_summary(goal), "goal": goal["goal"], "phases": phases, "runs": [_live(r) for r in db.runs(goal["id"])],
            "comments": db.comments(goal["id"])}


# ---------- overview / settings ----------

@router.get("/overview")
async def overview():
    def run():
        return {"goals": [_summary(g) for g in db.list_goals()[:50]], "usage": usage.status()}
    return await asyncio.to_thread(run)


class SettingsIn(BaseModel):
    five_hour: Annotated[int, Field(ge=1, le=100)] | None = None
    weekly: Annotated[int, Field(ge=1, le=100)] | None = None
    default: Literal["claude", "codex"] | None = None
    manager_claude_model: _text(60) | None = None
    manager_codex_effort: Literal["", "minimal", "low", "medium", "high"] | None = None
    manager_max_turns: Annotated[int, Field(ge=3, le=100)] | None = None
    review_each: bool | None = None


@router.put("/settings")
async def save_settings(body: SettingsIn):
    return usage.save_settings(**body.model_dump(exclude_none=True))


# ---------- goals ----------

# ---------- runbook uploads ----------
# A runbook is uploaded first (raw request body, streamed to disk), then named by its upload id when the goal is
# created, which moves it in with the goal.

UPLOAD_ID = re.compile(r"[0-9a-f]{32}")


def _uploads() -> Path:
    d = settings.data_dir / "team" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clear_old_uploads() -> None:
    for d in _uploads().iterdir():
        try:
            if time.time() - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


@router.post("/uploads")
async def upload(request: Request, name: str):
    try:
        clean = files.clean_name(name)
    except files.FileError as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(_clear_old_uploads)
    upload_id = uuid.uuid4().hex
    folder = _uploads() / upload_id
    folder.mkdir()
    limit, size = settings.max_upload_mb * 1024 * 1024, 0
    try:
        with open(folder / clean, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB (SIDUS_MAX_UPLOAD_MB)")
                await asyncio.to_thread(f.write, chunk)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if not size:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, "The file is empty")
    return {"upload": upload_id, "name": clean, "size": size}


def _uploaded(upload_id: str) -> Path:
    if not UPLOAD_ID.fullmatch(upload_id):
        raise HTTPException(400, "Bad upload id")
    found = list((_uploads() / upload_id).glob("*")) if (_uploads() / upload_id).is_dir() else []
    if len(found) != 1:
        raise HTTPException(400, "Upload not found; upload the runbook again")
    return found[0]


@router.get("/goals/{goal_id}/runbook")
async def runbook(goal_id: str):
    goal = _goal(goal_id)
    if not goal["runbook"]:
        raise HTTPException(404, "This goal has no runbook")
    path = goal["runbook"] if os.path.isabs(goal["runbook"]) else os.path.join(goal["cwd"], goal["runbook"])
    if not os.path.isfile(path):
        raise HTTPException(404, "The runbook file is gone")
    resp = FileResponse(path, filename=Path(path).name, content_disposition_type="inline")
    resp.headers["Content-Security-Policy"] = "sandbox"  # never run an uploaded HTML/JS file in Sidus's origin
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


class GoalIn(BaseModel):
    title: _text(120, required=True)
    cwd: _text(1024, required=True)
    goal: _text(MAX_GOAL) = ""
    runbook: _text(1024) = ""        # a file in the folder, relative or absolute
    runbook_upload: _text(64) = ""   # or an uploaded runbook (id from POST /uploads)


@router.post("/goals")
async def create_goal(body: GoalIn):
    cwd = os.path.realpath(os.path.expanduser(body.cwd))
    if not os.path.isdir(cwd):
        raise HTTPException(400, f"Folder not found: {cwd}")
    if not files.inside_workspace(cwd):
        raise HTTPException(403, "Outside the configured workspaces (SIDUS_WORKSPACES)")
    runbook = None
    if body.runbook:
        path = os.path.realpath(os.path.join(cwd, body.runbook))
        if not (path == cwd or path.startswith(cwd + os.sep)) or not os.path.isfile(path):
            raise HTTPException(400, f"Runbook not found in the folder: {body.runbook}")
        runbook = os.path.relpath(path, cwd)
    uploaded = _uploaded(body.runbook_upload) if body.runbook_upload else None
    if runbook and uploaded:
        raise HTTPException(400, "Give one runbook: a file in the folder or an upload")
    if not body.goal and not runbook and not uploaded:
        raise HTTPException(400, "Describe the goal or give a runbook")
    goal = body.goal or f"Carry out the runbook {runbook or uploaded.name}."
    detail = _detail(await asyncio.to_thread(engine.start_goal, body.title, cwd, goal, runbook, uploaded))
    if uploaded:
        shutil.rmtree(uploaded.parent, ignore_errors=True)
    return detail


@router.get("/goals/{goal_id}")
async def get_goal(goal_id: str):
    return await asyncio.to_thread(_detail, _goal(goal_id))


class TextIn(BaseModel):
    text: _text(10_000, required=True)


class NoteIn(BaseModel):
    note: _text(5000) = ""


@router.post("/goals/{goal_id}/answer")
async def answer(goal_id: str, body: TextIn):
    _goal(goal_id)
    return _detail(await _call(engine.answer, goal_id, body.text))


@router.post("/goals/{goal_id}/approve")
async def approve(goal_id: str):
    _goal(goal_id)
    return _detail(await _call(engine.approve, goal_id))


@router.post("/goals/{goal_id}/replan")
async def replan(goal_id: str, body: NoteIn):
    _goal(goal_id)
    return _detail(await _call(engine.replan, goal_id, body.note))


@router.post("/goals/{goal_id}/pause")
async def pause(goal_id: str):
    _goal(goal_id)
    return _detail(await _call(engine.pause, goal_id))


@router.post("/goals/{goal_id}/resume")
async def resume(goal_id: str):
    _goal(goal_id)
    return _detail(await _call(engine.resume, goal_id))


@router.post("/goals/{goal_id}/stop")
async def stop(goal_id: str):
    _goal(goal_id)
    return {"stopped": await asyncio.to_thread(engine.stop, goal_id)}


@router.post("/goals/{goal_id}/cancel")
async def cancel(goal_id: str):
    _goal(goal_id)
    return _detail(await _call(engine.cancel, goal_id))


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str):
    _goal(goal_id)
    if db.running_run(goal_id, include_asks=True):
        raise HTTPException(409, "Stop the running work first")
    db.delete_goal(goal_id)
    shutil.rmtree(procs.log_dir(goal_id), ignore_errors=True)
    return {"ok": True}


class CommentIn(BaseModel):
    text: _text(10_000, required=True)
    task_id: str | None = None


@router.post("/goals/{goal_id}/comments")
async def add_comment(goal_id: str, body: CommentIn):
    _goal(goal_id)
    if body.task_id and (db.get_task(body.task_id) or {}).get("goal_id") != goal_id:
        raise HTTPException(404, "Task not found")
    return db.comment(goal_id, "you", body.text, task_id=body.task_id)


# ---------- phases and tasks ----------

class PhaseIn(BaseModel):
    title: _text(120, required=True)
    details: _text(2000) = ""
    done_when: _text(500) = ""


class PhasePatch(BaseModel):
    title: _text(120, required=True) | None = None
    details: _text(2000) | None = None
    done_when: _text(500) | None = None
    position: Annotated[int, Field(ge=1)] | None = None
    status: Literal["skipped", "todo"] | None = None


def _open_goal(goal_id: str) -> dict:
    goal = _goal(goal_id)
    if goal["status"] in ("done", "cancelled"):
        raise HTTPException(409, "This goal is finished")
    return goal


@router.post("/goals/{goal_id}/phases")
async def add_phase(goal_id: str, body: PhaseIn):
    _open_goal(goal_id)
    return db.add_phase(goal_id, body.title, body.details, body.done_when)


@router.patch("/phases/{phase_id}")
async def update_phase(phase_id: str, body: PhasePatch):
    phase = db.get_phase(phase_id)
    if not phase:
        raise HTTPException(404, "Phase not found")
    _open_goal(phase["goal_id"])
    fields = body.model_dump(exclude_none=True)
    if phase["status"] in ("active", "done") and fields.keys() - {"title", "status"}:
        raise HTTPException(409, "This phase has started; you can rename or skip it")
    if fields.get("status") and (phase["status"] == "done" or db.running_run(phase["goal_id"])):
        raise HTTPException(409, "Stop the running work before skipping a phase")
    position = fields.pop("position", None)
    if fields:
        db.update_phase(phase_id, **fields)
    if position:
        db.move_phase(phase_id, position)
    await asyncio.to_thread(engine.kick, phase["goal_id"])
    return db.get_phase(phase_id)


@router.delete("/phases/{phase_id}")
async def delete_phase(phase_id: str):
    phase = db.get_phase(phase_id)
    if not phase:
        raise HTTPException(404, "Phase not found")
    _open_goal(phase["goal_id"])
    if phase["status"] != "todo":
        raise HTTPException(409, "Only phases that haven't started can be deleted; skip it instead")
    db.delete_phase(phase_id)
    return {"ok": True}


class TaskIn(BaseModel):
    title: _text(120, required=True)
    details: _text(2000) = ""
    done_when: _text(500) = ""
    assignee: Assignee = "any"


class TaskPatch(BaseModel):
    title: _text(120, required=True) | None = None
    details: _text(2000) | None = None
    done_when: _text(500) | None = None
    assignee: Assignee | None = None
    position: Annotated[int, Field(ge=1)] | None = None
    status: Literal["todo", "skipped"] | None = None   # retry or skip a task


@router.post("/phases/{phase_id}/tasks")
async def add_task(phase_id: str, body: TaskIn):
    phase = db.get_phase(phase_id)
    if not phase:
        raise HTTPException(404, "Phase not found")
    _open_goal(phase["goal_id"])
    if phase["status"] in ("done", "skipped"):
        raise HTTPException(409, "This phase is finished")
    task = db.add_task(phase_id, body.title, body.details, body.done_when, body.assignee)
    await asyncio.to_thread(engine.kick, phase["goal_id"])
    return task


def _editable(task: dict) -> None:
    _open_goal(task["goal_id"])
    if task["status"] in ("working", "review"):
        raise HTTPException(409, "This task is being worked on")


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: TaskPatch):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _editable(task)
    fields = body.model_dump(exclude_none=True)
    position = fields.pop("position", None)
    if fields.get("status") == "todo":
        fields["attempts"] = 0
    if fields:
        db.update_task(task_id, **fields)
    if position:
        db.move_task(task_id, position)
    if "status" in fields:
        goal = db.get_goal(task["goal_id"])
        if goal["status"] == "paused" and goal["waiting"] and "failed" in goal["waiting"]:
            await _call(engine.resume, goal["id"])
    return db.get_task(task_id)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _editable(task)
    if task["status"] == "done":
        raise HTTPException(409, "Finished tasks stay in the goal")
    db.delete_task(task_id)
    return {"ok": True}


# ---------- for the MCP server (bearer token) ----------

class McpIn(BaseModel):
    run: str
    agent: Literal["claude", "codex"]


class McpTask(BaseModel):
    title: _text(120, required=True)
    details: _text(2000) = ""
    done_when: _text(500) = ""
    assignee: Assignee = "any"


class McpPhases(McpIn):
    phases: Annotated[list[PhaseIn], Field(min_length=1, max_length=8)]
    first_tasks: Annotated[list[McpTask], Field(min_length=1, max_length=12)]


class McpTasks(McpIn):
    tasks: Annotated[list[McpTask], Field(min_length=1, max_length=12)]


class McpQuestions(McpIn):
    questions: Annotated[list[_text(300, required=True)], Field(min_length=1, max_length=3)]


class McpText(McpIn):
    text: _text(4000, required=True)


class McpReason(McpIn):
    reason: _text(2000, required=True)


class McpNotes(McpIn):
    notes: _text(2000) = ""


class McpRevise(McpIn):
    notes: _text(2000, required=True)
    assignee: Assignee = "any"


class McpCreate(McpIn, McpTask):
    pass


class McpComplete(McpIn):
    summary: _text(2000, required=True)
    next_tasks: Annotated[list[McpTask], Field(max_length=12)] = []


class McpAsk(McpIn):
    question: _text(20_000, required=True)


async def _tool(fn, *args):
    return {"result": await _call(fn, *args)}


def _dump(items) -> list[dict]:
    return [i.model_dump() for i in items]


@router.post("/mcp/propose_phases")
async def mcp_phases(b: McpPhases):
    return await _tool(engine.tool_propose_phases, b.run, b.agent, _dump(b.phases), _dump(b.first_tasks))


@router.post("/mcp/propose_tasks")
async def mcp_tasks(b: McpTasks):
    return await _tool(engine.tool_propose_tasks, b.run, b.agent, _dump(b.tasks))


@router.post("/mcp/ask_user")
async def mcp_ask_user(b: McpQuestions):
    return await _tool(engine.tool_ask_user, b.run, b.agent, b.questions)


@router.post("/mcp/comment")
async def mcp_comment(b: McpText):
    return await _tool(engine.tool_comment, b.run, b.agent, b.text)


@router.post("/mcp/task_blocked")
async def mcp_blocked(b: McpReason):
    return await _tool(engine.tool_blocked, b.run, b.agent, b.reason)


@router.post("/mcp/approve_task")
async def mcp_approve(b: McpNotes):
    return await _tool(engine.tool_approve, b.run, b.agent, b.notes)


@router.post("/mcp/revise_task")
async def mcp_revise(b: McpRevise):
    return await _tool(engine.tool_revise, b.run, b.agent, b.notes, b.assignee)


@router.post("/mcp/skip_task")
async def mcp_skip(b: McpReason):
    return await _tool(engine.tool_skip, b.run, b.agent, b.reason)


@router.post("/mcp/create_task")
async def mcp_create(b: McpCreate):
    return await _tool(engine.tool_create, b.run, b.agent, b.title, b.details, b.done_when, b.assignee)


@router.post("/mcp/complete_phase")
async def mcp_complete(b: McpComplete):
    return await _tool(engine.tool_complete_phase, b.run, b.agent, b.summary, _dump(b.next_tasks))


@router.post("/mcp/ask")
async def mcp_ask(b: McpAsk):
    return await _tool(engine.tool_ask, b.run, b.agent, b.question)


# ---------- front end ----------

@router.get("/ui.js")
async def ui_js():
    return FileResponse(STATIC / "team.js", media_type="text/javascript")


@router.get("/ui.css")
async def ui_css():
    return FileResponse(STATIC / "team.css", media_type="text/css")
