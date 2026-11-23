"""REST API for Relay, under /api/relay. The web UI signs in with the cookie (plus X-Sidus on writes); the MCP
server uses the bearer token, which only works for /api/relay/mcp/*."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from .. import files
from . import db, routing, runner

router = APIRouter(prefix="/api/relay")
STATIC = Path(__file__).with_name("static")

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50_000)]
Pin = Literal["claude", "codex"] | None


def _thread(tid: str) -> dict:
    thread = db.get_thread(tid)
    if not thread:
        raise HTTPException(404, "Thread not found")
    return thread


def _summary(thread: dict) -> dict:
    turns = db.turns(thread["id"])
    last = next((t for t in reversed(turns) if t["agent"]), None)
    return {**thread, "turns": len(turns), "last_agent": last["agent"] if last else None,
            "running": any(t["status"] == "running" for t in turns),
            "queued": sum(t["status"] == "queued" for t in turns)}


@router.get("/threads")
async def list_threads(archived: bool = False):
    return {"threads": [_summary(t) for t in await asyncio.to_thread(db.list_threads, archived)]}


class ThreadIn(BaseModel):
    name: Name
    cwd: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    pin: Pin = None
    message: Message | None = None


@router.post("/threads")
async def create_thread(body: ThreadIn):
    cwd = os.path.realpath(os.path.expanduser(body.cwd))
    if not os.path.isdir(cwd):
        raise HTTPException(400, f"Folder not found: {cwd}")
    if not files.inside_workspace(cwd):
        raise HTTPException(403, "Outside the configured workspaces (SIDUS_WORKSPACES)")
    thread = db.create_thread(body.name, cwd, body.pin)
    if body.message:
        await asyncio.to_thread(runner.send, thread["id"], body.message)
    return _summary(db.get_thread(thread["id"]))


@router.get("/threads/{tid}")
async def get_thread(tid: str):
    thread = _thread(tid)
    turns = [runner.live(t) for t in await asyncio.to_thread(db.turns, tid)]
    return {**_summary(thread), "turn_list": turns}


class ThreadPatch(BaseModel):
    name: Name | None = None
    pin: Pin = None
    archived: bool | None = None


@router.patch("/threads/{tid}")
async def update_thread(tid: str, body: ThreadPatch):
    _thread(tid)
    fields = body.model_dump(exclude_unset=True)
    if "archived" in fields:
        fields["archived"] = 1 if fields["archived"] else 0
    db.update_thread(tid, **fields)
    if "pin" in fields:
        await asyncio.to_thread(runner.kick, tid)
    return _summary(db.get_thread(tid))


@router.delete("/threads/{tid}")
async def delete_thread(tid: str):
    """Deletes the thread and its logs. The agents' own sessions stay in the sidebar."""
    _thread(tid)
    if db.running_turn(tid):
        raise HTTPException(409, "Stop the running turn first")
    db.delete_thread(tid)
    shutil.rmtree(runner.log_dir(tid), ignore_errors=True)
    return {"ok": True}


class MessageIn(BaseModel):
    text: Message


@router.post("/threads/{tid}/messages")
async def send(tid: str, body: MessageIn):
    if _thread(tid)["archived"]:
        raise HTTPException(409, "This thread is archived")
    return await asyncio.to_thread(runner.send, tid, body.text)


@router.post("/threads/{tid}/stop")
async def stop(tid: str):
    _thread(tid)
    return {"stopped": await asyncio.to_thread(runner.stop, tid)}


@router.post("/turns/{turn_id}/retry")
async def retry(turn_id: str):
    turn = db.get_turn(turn_id)
    if not turn:
        raise HTTPException(404, "Turn not found")
    if turn["status"] not in ("failed", "stopped"):
        raise HTTPException(409, "Only failed or stopped turns can be retried")
    new = db.add_turn(turn["thread_id"], turn["kind"], turn["input"], notes=turn["notes"])
    await asyncio.to_thread(runner.kick, turn["thread_id"])
    return db.get_turn(new["id"])


@router.post("/turns/{turn_id}/cancel")
async def cancel(turn_id: str):
    turn = db.get_turn(turn_id)
    if not turn or turn["status"] != "queued":
        raise HTTPException(409, "Only queued turns can be cancelled")
    db.update_turn(turn_id, status="stopped", error="Cancelled before it started")
    await asyncio.to_thread(runner.kick, turn["thread_id"])
    return {"ok": True}


@router.get("/status")
async def status():
    return await asyncio.to_thread(routing.status)


class SettingsIn(BaseModel):
    five_hour: Annotated[int, Field(ge=1, le=100)] | None = None
    weekly: Annotated[int, Field(ge=1, le=100)] | None = None
    default: Literal["claude", "codex"] | None = None


@router.put("/settings")
async def save_settings(body: SettingsIn):
    return routing.save_settings(**body.model_dump())


# ---------- for the MCP server (bearer token) ----------

class AskIn(BaseModel):
    turn: str
    agent: Literal["claude", "codex"]
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]


@router.post("/mcp/ask")
async def mcp_ask(body: AskIn):
    return {"answer": await asyncio.to_thread(runner.consult, body.turn, body.agent, body.question)}


class HandoffIn(BaseModel):
    turn: str
    agent: Literal["claude", "codex"]
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20_000)] = ""


@router.post("/mcp/handoff")
async def mcp_handoff(body: HandoffIn):
    return {"result": await asyncio.to_thread(runner.handoff, body.turn, body.agent, body.task, body.notes)}


@router.get("/mcp/usage")
async def mcp_usage():
    return await asyncio.to_thread(routing.status)


# ---------- front end ----------

@router.get("/ui.js")
async def ui_js():
    return FileResponse(STATIC / "relay.js", media_type="text/javascript")


@router.get("/ui.css")
async def ui_css():
    return FileResponse(STATIC / "relay.css", media_type="text/css")
