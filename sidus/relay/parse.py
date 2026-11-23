"""Reads the event stream an agent writes during a turn.

Claude Code: `claude -p --output-format stream-json --verbose` writes one JSON object per line:
  system/init (session_id), assistant (text and tool_use blocks), user (tool results), rate_limit_event, result.
Codex: `codex exec --json` writes thread.started (thread_id), item.started / item.completed (agent_message,
  command_execution, file_change, mcp_tool_call, ...), turn.completed, turn.failed, error.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MAX_READ = 8 * 1024 * 1024   # a turn's log is read from its end beyond this
MAX_STEPS = 200
LIMIT_WORDS = re.compile(r"usage limit|hit your limit|rate.?limit|limit reached|quota|too many requests|\b429\b", re.I)


def _lines(path: str | Path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - MAX_READ))
            data = f.read()
    except OSError:
        return
    for raw in data.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _result() -> dict:
    return {"session_id": None, "text": "", "steps": [], "files": [], "error": "", "limit": False,
            "limit_resets": None, "rate_limit": None, "finished": False}


def _add_file(out: dict, path) -> None:
    if path and path not in out["files"]:
        out["files"].append(path)


def _step(out: dict, text: str) -> None:
    if len(out["steps"]) < MAX_STEPS:
        out["steps"].append(text)


CLAUDE_FILE_TOOLS = {"Edit": "Edited", "Write": "Wrote", "MultiEdit": "Edited", "NotebookEdit": "Edited"}


def _claude_tool(name: str, inp: dict) -> str:
    if name in CLAUDE_FILE_TOOLS:
        return f"{CLAUDE_FILE_TOOLS[name]} {inp.get('file_path') or inp.get('notebook_path') or ''}".strip()
    if name == "Bash":
        return f"Ran `{_short(inp.get('command', ''), 120)}`"
    if name == "Read":
        return f"Read {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"Searched for {_short(inp.get('pattern', ''), 80)}"
    if name.startswith("mcp__sidus-relay__"):
        return {"ask_codex": "Asked Codex", "hand_off": "Handed off to Codex",
                "usage": "Checked usage"}.get(name.rsplit("__", 1)[1], name)
    if name == "TodoWrite":
        return "Updated its to-do list"
    return f"Used {name}"


def claude(path: str | Path) -> dict:
    out = _result()
    texts: list[str] = []
    for e in _lines(path):
        t = e.get("type")
        if e.get("session_id") and not out["session_id"]:
            out["session_id"] = e["session_id"]
        if t == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    _step(out, _claude_tool(block.get("name", ""), inp))
                    if block.get("name") in CLAUDE_FILE_TOOLS:
                        _add_file(out, inp.get("file_path") or inp.get("notebook_path"))
        elif t == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            out["rate_limit"] = info
            if info.get("status") == "rejected":
                out["limit"], out["limit_resets"] = True, info.get("resetsAt")
        elif t == "result":
            out["finished"] = True
            out["session_id"] = e.get("session_id") or out["session_id"]
            text = e.get("result") or ""
            if e.get("is_error") or (e.get("subtype") and e["subtype"] != "success"):
                out["error"] = _short(text or e.get("subtype") or "Claude stopped with an error", 500)
                if LIMIT_WORDS.search(text):
                    out["limit"] = True
            else:
                out["text"] = text
    if not out["text"] and texts:
        out["text"] = texts[-1]
    return out


def _codex_item(out: dict, item: dict, texts: list[str]) -> None:
    kind = item.get("type")
    if kind == "agent_message" and item.get("text", "").strip():
        texts.append(item["text"])
    elif kind == "command_execution":
        code = item.get("exit_code")
        _step(out, f"Ran `{_short(item.get('command', ''), 120)}`" + (f" (exit {code})" if code not in (0, None) else ""))
    elif kind == "file_change":
        for change in item.get("changes") or []:
            _add_file(out, change.get("path"))
            _step(out, f"{'Created' if change.get('kind') == 'add' else 'Edited'} {change.get('path', '')}")
    elif kind == "mcp_tool_call":
        tool = item.get("tool", "")
        _step(out, {"ask_claude": "Asked Claude", "hand_off": "Handed off to Claude", "usage": "Checked usage"}
              .get(tool, f"Used {item.get('server', '')}/{tool}"))
    elif kind == "web_search":
        _step(out, f"Searched the web for {_short(item.get('query', ''), 80)}")
    elif kind == "error" and item.get("message"):
        _step(out, f"Error: {_short(item['message'], 160)}")


def codex(path: str | Path) -> dict:
    out = _result()
    texts: list[str] = []
    for e in _lines(path):
        t = e.get("type")
        if t == "thread.started":
            out["session_id"] = e.get("thread_id")
        elif t == "item.completed":
            _codex_item(out, e.get("item") or {}, texts)
        elif t == "turn.completed":
            # Codex reports errors it recovered from (e.g. a reconnect) as "error" events; a completed turn wins.
            out["finished"], out["error"], out["limit"] = True, "", False
        elif t in ("turn.failed", "error"):
            msg = (e.get("error") or {}).get("message") if t == "turn.failed" else e.get("message")
            msg = msg or "Codex stopped with an error"
            out["error"] = _short(msg, 500)
            if LIMIT_WORDS.search(msg):
                out["limit"] = True
            if t == "turn.failed":
                out["finished"] = True
    if texts:
        out["text"] = texts[-1]
    return out


def read(agent: str, path: str | Path) -> dict:
    return claude(path) if agent == "claude" else codex(path)
