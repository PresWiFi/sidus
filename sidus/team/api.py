"""REST API for Team, under /api/team. The web UI signs in with the cookie (plus X-Sidus on writes); the MCP server
uses the bearer token, which only works for /api/team/mcp/*."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from ..ideas import repo as ideas
from . import db, engine, procs, usage

router = APIRouter(prefix="/api/team")
STATIC = Path(__file__).with_name("static")


def _text(n: int, required: bool = False):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1 if required else 0, max_length=n)]


Assignee = Literal["claude", "codex", "any"]


async def _call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ideas.NotFound as e:
        raise HTTPException(404, str(e))
    except (ideas.Invalid, engine.ToolRefused) as e:
        raise HTTPException(409, str(e))


def _plan(plan_id: str) -> dict:
    plan = db.get_plan(plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    return plan


def _live(run: dict) -> dict:
    """A running run's progress so far, read from its log."""
    if run["status"] != "running" or not run["log"]:
        return run
    from . import parse
    p = parse.read(run["agent"], run["log"])
    return {**run, "steps": p["steps"], "files": p["files"], "result": p["text"], "tokens": p["tokens"]}


def _detail(plan: dict) -> dict:
    try:
        project = ideas.get_project(plan["project_id"])
        milestone = next((m for m in project["milestones"] if m["id"] == plan["milestone_id"]), None)
    except ideas.NotFound:
        project, milestone = None, None
    runs = [_live(r) for r in db.runs(plan["id"])]
    return {**plan, "project": {"id": project["id"], "title": project["title"], "slug": project["slug"]} if project
            else None, "milestone": milestone, "tasks": db.tasks(plan["id"]), "runs": runs,
            "comments": db.comments(plan["id"]), "tokens": sum(r["tokens"] for r in runs),
            "running": any(r["status"] == "running" and r["role"] != "ask" for r in runs)}


# ---------- overview ----------

@router.get("/overview")
async def overview():
    """Ideas projects that have a folder, their open milestones, and Team plans."""
    def run():
        plans = db.list_plans()
        by_milestone = {}
        for p in plans:
            if p["status"] not in ("done", "cancelled"):
                by_milestone.setdefault(p["milestone_id"], p)
        projects = []
        for summary in ideas.list_projects():
            if not summary.get("repo_path") or summary["status"] in ("shipped", "killed"):
                continue
            project = ideas.get_project(summary["id"])
            projects.append({
                "id": project["id"], "title": project["title"], "slug": project["slug"],
                "status": project["status"], "repo_path": project["repo_path"],
                "milestones": [{**m, "plan": by_milestone.get(m["id"])} for m in project["milestones"]
                               if m["status"] in ("todo", "doing")],
            })
        recent = [{**p, "tokens": sum(r["tokens"] for r in db.runs(p["id"]))} for p in plans[:30]]
        names: dict[int, dict] = {}
        for p in recent:
            if p["project_id"] not in names:
                try:
                    proj = ideas.get_project(p["project_id"])
                    names[p["project_id"]] = {"title": proj["title"], **{m["id"]: m["title"] for m in proj["milestones"]}}
                except ideas.NotFound:
                    names[p["project_id"]] = {}
            p["project_title"] = names[p["project_id"]].get("title")
            p["milestone_title"] = names[p["project_id"]].get(p["milestone_id"])
        return {"projects": projects, "plans": recent, "usage": usage.status()}
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


# ---------- plans ----------

class PlanIn(BaseModel):
    project_id: int
    milestone_id: int
    request: _text(5000) = ""


@router.post("/plans")
async def create_plan(body: PlanIn):
    plan = await _call(engine.start_plan, body.project_id, body.milestone_id, body.request)
    return _detail(plan)


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str):
    return await asyncio.to_thread(_detail, _plan(plan_id))


class TextIn(BaseModel):
    text: _text(10_000, required=True)


class NoteIn(BaseModel):
    note: _text(5000) = ""


@router.post("/plans/{plan_id}/answer")
async def answer(plan_id: str, body: TextIn):
    _plan(plan_id)
    return _detail(await _call(engine.answer, plan_id, body.text))


@router.post("/plans/{plan_id}/approve")
async def approve(plan_id: str):
    _plan(plan_id)
    return _detail(await _call(engine.approve, plan_id))


@router.post("/plans/{plan_id}/replan")
async def replan(plan_id: str, body: NoteIn):
    _plan(plan_id)
    return _detail(await _call(engine.replan, plan_id, body.note))


@router.post("/plans/{plan_id}/pause")
async def pause(plan_id: str):
    _plan(plan_id)
    return _detail(await _call(engine.pause, plan_id))


@router.post("/plans/{plan_id}/resume")
async def resume(plan_id: str):
    _plan(plan_id)
    return _detail(await _call(engine.resume, plan_id))


@router.post("/plans/{plan_id}/stop")
async def stop(plan_id: str):
    _plan(plan_id)
    return {"stopped": await asyncio.to_thread(engine.stop, plan_id)}


@router.post("/plans/{plan_id}/cancel")
async def cancel(plan_id: str):
    _plan(plan_id)
    return _detail(await _call(engine.cancel, plan_id))


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: str):
    _plan(plan_id)
    if db.running_run(plan_id, include_asks=True):
        raise HTTPException(409, "Stop the running work first")
    db.delete_plan(plan_id)
    shutil.rmtree(procs.log_dir(plan_id), ignore_errors=True)
    return {"ok": True}


class CommentIn(BaseModel):
    text: _text(10_000, required=True)
    task_id: str | None = None


@router.post("/plans/{plan_id}/comments")
async def add_comment(plan_id: str, body: CommentIn):
    _plan(plan_id)
    if body.task_id and (db.get_task(body.task_id) or {}).get("plan_id") != plan_id:
        raise HTTPException(404, "Task not found")
    return db.comment(plan_id, "you", body.text, task_id=body.task_id)


# ---------- tasks ----------

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


def _editable(task: dict) -> None:
    plan = db.get_plan(task["plan_id"])
    if plan["status"] in ("done", "cancelled"):
        raise HTTPException(409, "This plan is finished")
    if task["status"] in ("working", "review"):
        raise HTTPException(409, "This task is being worked on")


@router.post("/plans/{plan_id}/tasks")
async def add_task(plan_id: str, body: TaskIn):
    plan = _plan(plan_id)
    if plan["status"] in ("done", "cancelled"):
        raise HTTPException(409, "This plan is finished")
    task = db.add_task(plan_id, body.title, body.details, body.done_when, body.assignee)
    await asyncio.to_thread(engine.kick, plan_id)
    return task


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
        plan = db.get_plan(task["plan_id"])
        if plan["status"] == "paused" and plan["waiting"] and "failed" in plan["waiting"]:
            await asyncio.to_thread(engine.resume, plan["id"])
    return db.get_task(task_id)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _editable(task)
    if task["status"] == "done":
        raise HTTPException(409, "Finished tasks stay in the plan")
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


class McpPropose(McpIn):
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


class McpSummary(McpIn):
    summary: _text(2000, required=True)


class McpAsk(McpIn):
    question: _text(20_000, required=True)


async def _tool(fn, *args):
    return {"result": await _call(fn, *args)}


@router.post("/mcp/propose_tasks")
async def mcp_propose(b: McpPropose):
    return await _tool(engine.tool_propose, b.run, b.agent, [t.model_dump() for t in b.tasks])


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


@router.post("/mcp/complete_milestone")
async def mcp_complete(b: McpSummary):
    return await _tool(engine.tool_complete, b.run, b.agent, b.summary)


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
