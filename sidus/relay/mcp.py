"""MCP server for Sidus Relay. Sidus starts it for each Relay turn (`python -m sidus.relay.mcp`), for Claude Code
through --mcp-config and for Codex through -c mcp_servers.

It's a thin client over Sidus's /api/relay/mcp REST API. Settings come from the environment:
  SIDUS_URL                where Sidus is (default http://127.0.0.1:7878)
  SIDUS_RELAY_TOKEN_FILE   file holding the Relay token
  SIDUS_RELAY_THREAD, SIDUS_RELAY_TURN, SIDUS_RELAY_AGENT   which turn this agent is running
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

LABEL = {"claude": "Claude", "codex": "Codex"}
AGENT = os.environ.get("SIDUS_RELAY_AGENT", "claude")
PARTNER = "codex" if AGENT == "claude" else "claude"
TURN = os.environ.get("SIDUS_RELAY_TURN", "")
ASK_TIMEOUT = 700   # a little longer than Sidus waits for the answer

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Sidus is local; the token stays here


def _url() -> str:
    return (os.environ.get("SIDUS_URL") or "http://127.0.0.1:7878").rstrip("/")


def _token() -> str:
    try:
        value = Path(os.environ.get("SIDUS_RELAY_TOKEN_FILE", "")).read_text().strip()
    except OSError:
        value = ""
    if not value:
        raise ToolError("Relay isn't set up (no token). Carry on without it; don't retry.")
    return value


def call(method: str, path: str, body: dict | None = None, timeout: float = 20):
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{_url()}/api/relay/mcp{path}", data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ToolError(str(detail or f"Sidus returned HTTP {e.code}. Carry on without Relay.")) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise ToolError(f"Can't reach Sidus ({getattr(e, 'reason', e)}). Carry on without Relay; don't retry "
                        "in a loop.") from None


INSTRUCTIONS = (f"You are {LABEL[AGENT]}, working in a Sidus Relay thread with {LABEL[PARTNER]}. Use "
                f"ask_{PARTNER} for a second opinion, hand_off to pass the rest of the work to {LABEL[PARTNER]}, and "
                "usage to see how much of each plan is left.")
server = MCPServer(name="sidus-relay", title="Sidus Relay", instructions=INSTRUCTIONS)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
UPFRONT = {"anthropic/alwaysLoad": True}


def ask(question: Annotated[str, Field(max_length=20000, description="The question, with any context the other "
                                                                     "agent needs (it can read the project)")]) -> str:
    return call("POST", "/ask", {"turn": TURN, "agent": AGENT, "question": question}, timeout=ASK_TIMEOUT)["answer"]


ask.__doc__ = (f"Ask {LABEL[PARTNER]} a question: a second opinion, a review of your plan or code, or something it "
               f"may know better. {LABEL[PARTNER]} works in the same folder, can read files and run read-only "
               "commands, and won't change anything. Waits for the answer (can take a few minutes).")
server.tool(name=f"ask_{PARTNER}", annotations=READ, meta=UPFRONT)(ask)


@server.tool(annotations=WRITE, meta=UPFRONT)
def hand_off(
    task: Annotated[str, Field(max_length=20000, description="What's left to do, stated so it stands on its own")],
    notes: Annotated[str, Field(max_length=20000, description="What you did, what you found, decisions made, "
                                                              "anything the next agent should know")] = "",
) -> str:
    """Hand the rest of this task to the other agent in the thread. It takes over when your turn ends, with your
    task and notes plus a summary of the thread. After calling this, end your turn with a one-line summary."""
    return call("POST", "/handoff", {"turn": TURN, "agent": AGENT, "task": task, "notes": notes})["result"]


@server.tool(annotations=READ, meta=UPFRONT)
def usage() -> dict:
    """How much of each plan (Claude and ChatGPT/Codex) is used, in percent of the 5-hour and weekly limits, and
    where Relay switches agents."""
    data = call("GET", "/usage")
    agents = {LABEL[a]: {k: v for k, v in info.items() if k in ("linked", "five_hour", "weekly", "blocked_until")}
              for a, info in data["agents"].items()}
    return {"you": LABEL[AGENT], "plans": agents,
            "switch_at": {"five_hour": data["settings"]["five_hour"], "weekly": data["settings"]["weekly"]}}


def main() -> None:
    server.run()  # stdio


if __name__ == "__main__":
    main()
