"""MCP server for Sidus Team. Sidus starts it with every Team run (`python -m sidus.team.mcp`): for Claude Code
through --mcp-config, for Codex through -c mcp_servers.

The tools depend on the run's role (SIDUS_TEAM_ROLE):
  plan    (manager)  propose_tasks, ask_user, comment
  review  (manager)  approve_task, revise_task, skip_task, create_task, comment
  wrapup  (manager)  complete_milestone, create_task, comment
  work    (worker)   ask_claude / ask_codex, task_blocked, comment
It's a thin client over Sidus's /api/team/mcp REST API, signed in with the Team token.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

LABEL = {"claude": "Claude", "codex": "Codex"}
AGENT = os.environ.get("SIDUS_TEAM_AGENT", "claude")
PARTNER = "codex" if AGENT == "claude" else "claude"
ROLE = os.environ.get("SIDUS_TEAM_ROLE", "work")
RUN = os.environ.get("SIDUS_TEAM_RUN", "")
ASK_TIMEOUT = 700

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Sidus is local; the token stays here


def call(tool: str, body: dict, timeout: float = 20):
    try:
        token = Path(os.environ.get("SIDUS_TEAM_TOKEN_FILE", "")).read_text().strip()
    except OSError:
        token = ""
    if not token:
        raise ToolError("Sidus Team isn't set up (no token). Carry on without it; don't retry.")
    url = (os.environ.get("SIDUS_URL") or "http://127.0.0.1:7878").rstrip("/") + f"/api/team/mcp/{tool}"
    req = urllib.request.Request(url, data=json.dumps({"run": RUN, "agent": AGENT, **body}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"null")["result"]
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ToolError(str(detail or f"Sidus returned HTTP {e.code}.")) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise ToolError(f"Can't reach Sidus ({getattr(e, 'reason', e)}). Don't retry in a loop.") from None


server = MCPServer(name="sidus-team", title="Sidus Team",
                   instructions="Tools for working on a Sidus Team plan. Keep messages short.")
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
UPFRONT = {"anthropic/alwaysLoad": True}
Short = lambda n, d: Annotated[str, Field(max_length=n, description=d)]
Assignee = Annotated[Literal["claude", "codex", "any"], Field(description="Who should do it; any = whoever has "
                                                                          "more usage left")]


class TaskIn(BaseModel):
    title: Short(120, "Short task name")
    details: Short(2000, "What to do and what to watch for, in a few sentences") = ""
    done_when: Short(500, "Something the worker can check") = ""
    assignee: Assignee = "any"


def comment(text: Short(4000, "The comment")) -> str:
    """Add a short note to the plan's shared thread (on your task, for workers). Everyone on the plan reads it."""
    return call("comment", {"text": text})


server.tool(annotations=WRITE, meta=UPFRONT)(comment)

if ROLE == "plan":
    @server.tool(annotations=WRITE, meta=UPFRONT)
    def propose_tasks(tasks: Annotated[list[TaskIn], Field(min_length=1, max_length=12)]) -> str:
        """Propose the milestone's tasks, in order. Replaces any you proposed before. The user approves them."""
        return call("propose_tasks", {"tasks": [t.model_dump() for t in tasks]})

    @server.tool(annotations=WRITE, meta=UPFRONT)
    def ask_user(questions: Annotated[list[Short(300, "One short question")], Field(min_length=1, max_length=3)]) -> str:
        """Ask the user up to 3 short questions when the answer changes the plan. Then end your turn."""
        return call("ask_user", {"questions": questions})

if ROLE == "review":
    @server.tool(annotations=WRITE, meta=UPFRONT)
    def approve_task(notes: Short(2000, "Optional short note") = "") -> str:
        """The task is done after all: mark it done."""
        return call("approve_task", {"notes": notes})

    @server.tool(annotations=WRITE, meta=UPFRONT)
    def revise_task(notes: Short(2000, "What to fix, briefly"), assignee: Assignee = "any") -> str:
        """Send the task back to be redone, with notes. You can switch who does it."""
        return call("revise_task", {"notes": notes, "assignee": assignee})

    @server.tool(annotations=WRITE, meta=UPFRONT)
    def skip_task(reason: Short(1000, "Why it isn't needed")) -> str:
        """Skip the task: it isn't needed or can't be done."""
        return call("skip_task", {"reason": reason})

if ROLE in ("review", "wrapup"):
    @server.tool(annotations=WRITE, meta=UPFRONT)
    def create_task(title: Short(120, "Short task name"), details: Short(2000, "What to do") = "",
                    done_when: Short(500, "Something checkable") = "", assignee: Assignee = "any") -> str:
        """Add a task at the end of the plan. Only for work that's needed to meet the milestone."""
        return call("create_task", {"title": title, "details": details, "done_when": done_when, "assignee": assignee})

if ROLE == "wrapup":
    @server.tool(annotations=WRITE, meta=UPFRONT)
    def complete_milestone(summary: Short(2000, "2-4 lines for the user: what was done, anything to check")) -> str:
        """The milestone's done-when is met: mark it done in Ideas and finish the plan."""
        return call("complete_milestone", {"summary": summary})

if ROLE == "work":
    def ask(question: Short(20000, "The question, with the context the other agent needs")) -> str:
        return call("ask", {"question": question}, timeout=ASK_TIMEOUT)

    ask.__doc__ = (f"Ask {LABEL[PARTNER]} when you're stuck or want a quick second opinion. It reads the project but "
                   "changes nothing. Waits for the answer (can take minutes) and uses its plan, so ask only when it "
                   "helps.")
    server.tool(name=f"ask_{PARTNER}", annotations=READ, meta=UPFRONT)(ask)

    @server.tool(annotations=WRITE, meta=UPFRONT)
    def task_blocked(reason: Short(2000, "What stops you, and what you tried")) -> str:
        """You can't finish the task: the manager decides what happens next. Then end your turn with a report."""
        return call("task_blocked", {"reason": reason})


def main() -> None:
    server.run()  # stdio


if __name__ == "__main__":
    main()
