"""Team: whole plans run against stand-in claude/codex scripts that call the real tool endpoints over HTTP, the way
the MCP server does. Plus the parser, agent picking, and the API's rules."""
import json
import socket
import sys
import textwrap
import threading
import time

import pytest
import uvicorn

from sidus.app import app
from sidus.config import settings
from sidus.ideas import repo as ideas
from sidus.store import kv_set
from sidus.team import db, engine, parse, procs, usage

FAKE = r'''
import json, os, sys, time, tomllib, urllib.request, uuid
AGENT = os.path.basename(sys.argv[0])
args = sys.argv[1:]
rest = args[args.index("--") + 1:]
env = {}
if AGENT == "claude":
    prompt = rest[0]
    sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1] \
        if "--session-id" in args else str(uuid.uuid4())
    if "--mcp-config" in args:
        env = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["sidus-team"]["env"]
else:
    resume = args[1] == "resume"
    sid, prompt = (rest[0], rest[1]) if resume else (str(uuid.uuid4()), rest[0])
    for i, a in enumerate(args):
        if a == "-c" and args[i + 1].startswith("mcp_servers.sidus-team.env="):
            env = tomllib.loads("env = " + args[i + 1].split("=", 1)[1])["env"]
role = env.get("SIDUS_TEAM_ROLE", "ask")
calls = []

def tool(name, **body):
    token = open(env["SIDUS_TEAM_TOKEN_FILE"]).read().strip()
    req = urllib.request.Request(env["SIDUS_URL"] + "/api/team/mcp/" + name, method="POST",
        data=json.dumps({"run": env["SIDUS_TEAM_RUN"], "agent": AGENT, **body}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())["result"]
    calls.append(name)
    return out

limit = False
if role == "plan":
    if "ASKME" in prompt and "The user answered" not in prompt:
        tool("ask_user", questions=["Which database?"])
    else:
        tool("propose_tasks", tasks=[{"title": "Build A", "done_when": "a works", "assignee": "claude"},
                                     {"title": "Build B" + (" BLOCKME" if "BLOCKME" in prompt else "")
                                      + (" LIMITME" if "LIMITME" in prompt else ""), "assignee": "codex"}])
elif role == "work":
    task = next((l for l in prompt.splitlines() if l.startswith("Your task")), "")
    if "BLOCKME" in task:
        tool("task_blocked", reason="missing key")
    if "LIMITME" in task and AGENT == "codex":
        limit = True
    if task:
        open(AGENT + "_out.txt", "a").write("x")   # a file written without an edit tool
    if "Build A" in task and os.environ.get("FAKE_ASK"):
        calls.append("answer:" + tool("ask", question="Is A right?"))
elif role == "review":
    if "attempt 1 of" in prompt:
        tool("revise_task", notes="Use the env var", assignee="claude")
    else:
        tool("approve_task", notes="fine")
elif role == "wrapup":
    tool("complete_milestone", summary="A and B are built.")

if AGENT == "claude":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "x.py"}}]}}))
    out = json.dumps({"agent": AGENT, "role": role, "prompt": prompt, "args": args, "calls": calls})
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": out, "session_id": sid,
                      "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}}))
else:
    print(json.dumps({"type": "thread.started", "thread_id": sid}))
    if limit:
        print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}))
        sys.exit(1)
    print(json.dumps({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "y.py"}]}}))
    out = json.dumps({"agent": AGENT, "role": role, "prompt": prompt, "args": args, "calls": calls})
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": out}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 10}}))
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """The real app on a local port, so the stand-in agents can call the tool endpoints."""
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield port
    srv.should_exit = True


@pytest.fixture
def team(tmp_path, monkeypatch, workspace, server):
    db.open_db(tmp_path / "team.db")
    for key in ("team_blocked", "team_settings", "team_claude_limits"):
        kv_set(key, {})
    monkeypatch.setattr(settings, "port", server)
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    for name in ("claude", "codex"):
        p = tmp_path / name
        p.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE))
        p.chmod(0o755)
    monkeypatch.setattr(settings, "claude_cmd", str(tmp_path / "claude"))
    monkeypatch.setattr(settings, "claude_base_args", "")
    monkeypatch.setattr(settings, "claude_args", "")
    from sidus.providers import codex
    monkeypatch.setattr(codex, "binary", lambda: str(tmp_path / "codex"))
    monkeypatch.setattr(usage, "linked", lambda agent: True)
    use = {"claude": {"five_hour": 10, "weekly": 10}, "codex": {"five_hour": 20, "weekly": 20}}
    monkeypatch.setattr(usage, "_claude_usage", lambda: use["claude"])
    monkeypatch.setattr(usage, "_codex_usage", lambda: use["codex"])
    folder = workspace / f"teamproj-{tmp_path.name}"
    folder.mkdir(exist_ok=True)
    import subprocess
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    subprocess.run(["git", "-C", str(folder), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    created = ideas.create_project(title="Widget", summary="A widget.", repo_path=str(folder),
                                   milestones=[{"title": "Ship v1", "done_criteria": "A and B work"}])
    project = created["project"]
    return {"use": use, "project": project, "milestone": project["milestones"][0]}


def settle(plan_id, until, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        plan = db.get_plan(plan_id)
        if plan["status"] in until and not db.running_run(plan_id, include_asks=True):
            return plan
        time.sleep(0.1)
    raise AssertionError(f"plan is {db.get_plan(plan_id)['status']}, runs: "
                         f"{[(r['role'], r['agent'], r['status'], r['error']) for r in db.runs(plan_id)]}")


def out(run):
    return json.loads(run["result"])


def start(team, request=""):
    return engine.start_plan(team["project"]["id"], team["milestone"]["id"], request)


# ---------- whole plans ----------

def test_plan_approve_work_wrapup(team):
    plan = settle(start(team)["id"], ("proposed",))
    assert plan["manager"] == "claude"
    tasks = db.tasks(plan["id"])
    assert [t["title"] for t in tasks] == ["Build A", "Build B"] and tasks[1]["assignee"] == "codex"
    first = db.runs(plan["id"])[0]
    o = out(first)
    assert o["role"] == "plan" and "--model" in o["args"] and "sonnet" in o["args"] and "--max-turns" in o["args"]
    assert o["args"][o["args"].index("--tools") + 1] == procs.MANAGER_TOOLS
    assert o["args"][o["args"].index("--permission-mode") + 1] == "default"
    assert "Milestone: Ship v1. Done when: A and B work" in o["prompt"]

    engine.approve(plan["id"])
    assert ideas.get_project(team["project"]["id"])["milestones"][0]["status"] == "doing"
    plan = settle(plan["id"], ("done",))
    runs = db.runs(plan["id"])
    assert [(r["role"], r["agent"]) for r in runs] == [("plan", "claude"), ("work", "claude"), ("work", "codex"),
                                                       ("wrapup", "claude")]
    work_a, work_b = out(runs[1]), out(runs[2])
    config = json.loads(work_a["args"][work_a["args"].index("--mcp-config") + 1])["mcpServers"]
    assert set(config) == {"sidus-ideas", "sidus-team"} and work_a["args"].count("--mcp-config") == 1
    assert work_a["args"][work_a["args"].index("--allowedTools") + 1] == "mcp__sidus-ideas__*,mcp__sidus-team__*"
    assert work_a["args"][work_a["args"].index("--permission-mode") + 1] == "bypassPermissions"
    assert "--model" not in work_a["args"]                       # workers use the normal model
    assert "Your task (1 of 2): Build A" in work_a["prompt"] and "ask_codex" in work_a["prompt"]
    assert "Earlier tasks:\n- Build A (Claude)" in work_b["prompt"]
    assert any('model_reasoning_effort' in a for a in out(runs[2])["args"]) is False  # only the manager is lowered
    assert all(t["status"] == "done" for t in db.tasks(plan["id"]))
    assert db.tasks(plan["id"])[0]["files"] == ["claude_out.txt", "x.py"]
    assert db.tasks(plan["id"])[1]["files"] == ["codex_out.txt", "y.py"]
    assert "Files: claude_out.txt, x.py" in work_b["prompt"]
    assert ideas.get_project(team["project"]["id"])["milestones"][0]["status"] == "done"
    assert plan["summary"] == "A and B are built." and sum(r["tokens"] for r in runs) == 120 * 3 + 60


def test_questions_then_plan(team):
    plan = settle(start(team, "ASKME please")["id"], ("questions",))
    assert plan["questions"] == "1. Which database?"
    engine.answer(plan["id"], "SQLite")
    plan = settle(plan["id"], ("proposed",))
    second = db.runs(plan["id"])[1]
    o = out(second)
    assert "--resume" in o["args"] and o["prompt"].startswith("The user answered:\nSQLite")


def test_blocked_task_goes_to_manager_and_back(team):
    plan = settle(start(team, "BLOCKME")["id"], ("proposed",))
    engine.approve(plan["id"])
    plan = settle(plan["id"], ("done",))
    roles = [(r["role"], r["agent"], r["verdict"]) for r in db.runs(plan["id"])]
    assert roles[2] == ("work", "codex", "blocked") and roles[3] == ("review", "claude", "revise")
    assert roles[4][:2] == ("work", "claude")      # the manager reassigned it
    redo = out(db.runs(plan["id"])[4])
    assert "Use the env var" in redo["prompt"]
    notes = [c["text"] for c in db.comments(plan["id"], db.tasks(plan["id"])[1]["id"])]
    assert "Blocked: missing key" in notes and "Use the env var" in notes


def test_usage_limit_moves_task_to_other_agent(team):
    plan = settle(start(team, "LIMITME")["id"], ("proposed",))
    engine.approve(plan["id"])
    plan = settle(plan["id"], ("done",))
    runs = db.runs(plan["id"])
    assert (runs[2]["agent"], runs[2]["status"]) == ("codex", "limit")
    assert (runs[3]["role"], runs[3]["agent"], runs[3]["status"]) == ("work", "claude", "done")
    assert "hit its usage limit" in out(runs[3])["prompt"] and usage.blocked_until("codex")


def test_manager_switches_when_over_and_worker_ask(team, monkeypatch):
    monkeypatch.setenv("FAKE_ASK", "1")
    team["use"]["claude"]["five_hour"] = 95       # Claude past the switch point: Codex has more room
    plan = settle(start(team)["id"], ("proposed",))
    assert plan["manager"] == "codex"
    o = out(db.runs(plan["id"])[0])
    assert 'model_reasoning_effort="low"' in o["args"]
    engine.approve(plan["id"])
    plan = settle(plan["id"], ("done",))
    ask = next(r for r in db.runs(plan["id"]) if r["role"] == "ask")
    assert ask["agent"] == "codex" and ask["status"] == "done"
    task_a = db.tasks(plan["id"])[0]
    assert any(c["author"] == "codex" for c in db.comments(plan["id"], task_a["id"]))


def test_one_plan_runs_per_project(team):
    ideas.add_milestones(team["project"]["id"], [{"title": "Second"}])
    second_ms = ideas.get_project(team["project"]["id"])["milestones"][1]
    a = start(team)
    b = engine.start_plan(team["project"]["id"], second_ms["id"])
    settle(a["id"], ("proposed",))
    settle(b["id"], ("proposed",))
    starts = sorted((r["started"], r["ended"]) for p in (a, b) for r in db.runs(p["id"]))
    assert starts[1][0] >= starts[0][1]            # the second planning run started after the first ended


# ---------- parser / picking ----------

def test_parse_tokens(tmp_path):
    c = tmp_path / "c.jsonl"
    c.write_text(json.dumps({"type": "result", "subtype": "success", "result": "ok", "usage": {
        "input_tokens": 3, "cache_creation_input_tokens": 4, "cache_read_input_tokens": 5, "output_tokens": 6}}))
    assert parse.claude(c)["tokens"] == 13      # cache reads left out
    x = tmp_path / "x.jsonl"
    x.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "cached_input_tokens": 4, "output_tokens": 2}}))
    assert parse.codex(x)["tokens"] == 5


def test_pick(team):
    assert usage.pick("any") == ("claude", "Claude has more room")
    assert usage.pick("any", current="codex")[0] == "codex"
    assert usage.pick("codex") == ("codex", "")
    usage.block("codex", time.time() + 600)
    agent, why = usage.pick("codex")
    assert agent == "claude" and "out of usage" in why
    usage.block("claude", time.time() + 600)
    assert usage.pick("any")[0] is None


# ---------- API ----------

def test_api_rules(team, client, anon):
    r = client.post("/api/team/plans", json={"project_id": team["project"]["id"],
                                             "milestone_id": team["milestone"]["id"]})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.post("/api/team/plans", json={"project_id": team["project"]["id"],
                                                "milestone_id": team["milestone"]["id"]}).status_code == 409
    settle(pid, ("proposed",))
    detail = client.get(f"/api/team/plans/{pid}").json()
    assert detail["milestone"]["title"] == "Ship v1" and len(detail["tasks"]) == 2 and detail["tokens"] == 120
    tid = detail["tasks"][1]["id"]
    assert client.patch(f"/api/team/tasks/{tid}", json={"position": 1, "assignee": "any"}).status_code == 200
    assert [t["title"] for t in db.tasks(pid)] == ["Build B", "Build A"] and db.tasks(pid)[0]["assignee"] == "any"
    overview = client.get("/api/team/overview").json()
    assert overview["projects"][0]["milestones"][0]["plan"]["id"] == pid
    assert anon.get("/api/team/overview").status_code == 401
    # Tool endpoints: only with the token, and only for a running run of that role.
    from fastapi.testclient import TestClient
    from sidus.team import token
    bearer = TestClient(app, headers={"Authorization": f"Bearer {token.get()}"})
    assert bearer.get("/api/team/overview").status_code == 401
    plan_run = db.runs(pid)[0]["id"]
    r = bearer.post("/api/team/mcp/propose_tasks", json={"run": plan_run, "agent": "claude",
                                                         "tasks": [{"title": "x"}]})
    assert r.status_code == 409   # that run has finished
    assert client.put("/api/team/settings", json={"manager_claude_model": "haiku", "review_each": True}).json()[
        "review_each"] is True
    assert client.delete(f"/api/team/plans/{pid}").json() == {"ok": True}
