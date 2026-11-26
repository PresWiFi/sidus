"""Team: whole goals run against stand-in claude/codex scripts that call the real tool endpoints over HTTP, the way
the MCP server does. Plus the parser, agent picking, and the API's rules."""
import json
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
import threading
import time

import pytest
import uvicorn

from sidus.app import app
from sidus.config import settings
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
if role == "outline":
    if "ASKME" in prompt and "The user answered" not in prompt:
        tool("ask_user", questions=["Which database?"])
    else:
        flags = "".join(f for f in (" BLOCKME", " LIMITME") if f.strip() in prompt)
        tool("propose_phases", phases=[{"title": "Phase One", "details": "Sections 1-2", "done_when": "one works"},
                                       {"title": "Phase Two", "done_when": "two works"}],
             first_tasks=[{"title": "Build A", "done_when": "a works", "assignee": "claude"},
                          {"title": "Build B" + flags, "assignee": "codex"}])
elif role == "phase":
    tool("propose_tasks", tasks=[{"title": "Build C", "assignee": "codex"}])
elif role == "wrapup":
    if "Phase 1 of 2" in prompt:
        nxt = [] if os.path.exists("NOPLAN") else [{"title": "Build C", "assignee": "codex"}]
        tool("complete_phase", summary="Phase one done.", next_tasks=nxt)
    else:
        tool("complete_phase", summary="All built.")
elif role == "work":
    task = next((l for l in prompt.splitlines() if l.startswith("Your task")), "")
    if task:
        open(AGENT + "_out.txt", "a").write("x")   # a file written without an edit tool
        os.makedirs("__pycache__", exist_ok=True)
        open("__pycache__/m.cpython-313.pyc", "w").write("x")   # cache output: not listed
    if "BLOCKME" in task:
        tool("task_blocked", reason="missing key")
    if "LIMITME" in task and AGENT == "codex":
        limit = True
    if "Build A" in task and os.environ.get("FAKE_ASK"):
        calls.append("answer:" + tool("ask", question="Is A right?"))
elif role == "review":
    if "attempt 1 of" in prompt:
        tool("revise_task", notes="Use the env var", assignee="claude")
    else:
        tool("approve_task", notes="fine")

out = json.dumps({"agent": AGENT, "role": role, "prompt": prompt, "args": args, "calls": calls})
if AGENT == "claude":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "x.py"}}]}}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": out, "session_id": sid,
                      "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}}))
else:
    print(json.dumps({"type": "thread.started", "thread_id": sid}))
    if limit:
        print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}))
        sys.exit(1)
    print(json.dumps({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "y.py"}]}}))
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
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    subprocess.run(["git", "-C", str(folder), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    return {"use": use, "cwd": str(folder)}


def settle(goal_id, until, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        goal = db.get_goal(goal_id)
        if goal["status"] in until and not db.running_run(goal_id, include_asks=True):
            return goal
        time.sleep(0.1)
    raise AssertionError(f"goal is {db.get_goal(goal_id)['status']} ({db.get_goal(goal_id)['waiting']}), runs: "
                         f"{[(r['role'], r['agent'], r['status'], r['error'][:200]) for r in db.runs(goal_id)]}")


def out(run):
    return json.loads(run["result"])


def start(team, goal="Build the widget.", runbook=None):
    return engine.start_goal("Widget", team["cwd"], goal, runbook)


def roles(goal_id):
    return [(r["role"], r["agent"]) for r in db.runs(goal_id)]


# ---------- whole goals ----------

def test_outline_approve_phases_done(team):
    goal = settle(start(team)["id"], ("proposed",))
    assert goal["manager"] == "claude" and engine.goal_file(goal).read_text() == "Build the widget.\n"
    phases = db.phases(goal["id"])
    assert [p["title"] for p in phases] == ["Phase One", "Phase Two"]
    assert [t["title"] for t in db.tasks(goal["id"], phases[0]["id"])] == ["Build A", "Build B"]
    assert db.tasks(goal["id"], phases[1]["id"]) == []
    o = out(db.runs(goal["id"])[0])
    assert o["role"] == "outline" and "sonnet" in o["args"] and "--max-turns" in o["args"]
    assert o["args"][o["args"].index("--tools") + 1] == procs.MANAGER_TOOLS
    assert o["args"][o["args"].index("--add-dir") + 1] == str(procs.log_dir(goal["id"]))
    assert "Goal: Widget\nBuild the widget." in o["prompt"]

    engine.approve(goal["id"])
    goal = settle(goal["id"], ("done",))
    assert roles(goal["id"]) == [("outline", "claude"), ("work", "claude"), ("work", "codex"), ("wrapup", "claude"),
                                 ("work", "codex"), ("wrapup", "claude")]
    runs = db.runs(goal["id"])
    work_a, work_c = out(runs[1]), out(runs[4])
    assert work_a["args"][work_a["args"].index("--permission-mode") + 1] == "bypassPermissions"
    assert "--model" not in work_a["args"]
    assert "Phase 1 of 2: Phase One. Sections 1-2" in work_a["prompt"]
    assert f"The full goal is in {engine.goal_file(goal)}" in work_a["prompt"]
    assert "Earlier phases:\n- Phase One: Phase one done." in work_c["prompt"]
    wrap1 = out(runs[3])
    assert "next_tasks: the tasks for the next phase, \"Phase Two\"" in wrap1["prompt"]
    assert "Files: claude_out.txt, x.py" in wrap1["prompt"] and "pycache" not in wrap1["prompt"]
    assert [p["status"] for p in db.phases(goal["id"])] == ["done", "done"]
    assert goal["summary"] == "All built." and sum(r["tokens"] for r in runs) == 120 * 4 + 60 * 2


def test_phase_without_tasks_gets_planned(team):
    open(f"{team['cwd']}/NOPLAN", "w").close()
    goal = settle(start(team)["id"], ("proposed",))
    engine.approve(goal["id"])
    goal = settle(goal["id"], ("done",))
    assert [r for r, _ in roles(goal["id"])] == ["outline", "work", "work", "wrapup", "phase", "work", "wrapup"]
    phase_run = next(r for r in db.runs(goal["id"]) if r["role"] == "phase")
    assert "Phase 2 of 2: Phase Two" in out(phase_run)["prompt"]


def test_long_goal_stays_in_its_file(team):
    long = "Step.\n" * 1000
    goal = settle(start(team, goal=long)["id"], ("proposed",))
    p = out(db.runs(goal["id"])[0])["prompt"]
    assert "It's long, so it's in" in p and long not in p


def test_questions_then_outline(team):
    goal = settle(start(team, "ASKME please")["id"], ("questions",))
    assert goal["questions"] == "1. Which database?"
    engine.answer(goal["id"], "SQLite")
    goal = settle(goal["id"], ("proposed",))
    o = out(db.runs(goal["id"])[1])
    assert "--resume" in o["args"] and o["prompt"].startswith("The user answered:\nSQLite")


def test_blocked_task_goes_to_manager_and_back(team):
    goal = settle(start(team, "BLOCKME")["id"], ("proposed",))
    engine.approve(goal["id"])
    goal = settle(goal["id"], ("done",))
    runs = db.runs(goal["id"])
    assert (runs[2]["role"], runs[2]["agent"], runs[2]["verdict"]) == ("work", "codex", "blocked")
    assert (runs[3]["role"], runs[3]["verdict"]) == ("review", "revise")
    assert (runs[4]["role"], runs[4]["agent"]) == ("work", "claude")      # reassigned by the manager
    assert "Use the env var" in out(runs[4])["prompt"]


def test_usage_limit_moves_task_to_other_agent(team):
    goal = settle(start(team, "LIMITME")["id"], ("proposed",))
    engine.approve(goal["id"])
    goal = settle(goal["id"], ("done",))
    runs = db.runs(goal["id"])
    assert (runs[2]["agent"], runs[2]["status"]) == ("codex", "limit")
    assert (runs[3]["role"], runs[3]["agent"], runs[3]["status"]) == ("work", "claude", "done")
    assert "hit its usage limit" in out(runs[3])["prompt"] and usage.blocked_until("codex")


def test_codex_manager_and_worker_ask(team, monkeypatch):
    monkeypatch.setenv("FAKE_ASK", "1")
    team["use"]["claude"]["five_hour"] = 95       # Claude past the switch point: Codex has more room
    goal = settle(start(team)["id"], ("proposed",))
    assert goal["manager"] == "codex"
    assert 'model_reasoning_effort="low"' in out(db.runs(goal["id"])[0])["args"]
    engine.approve(goal["id"])
    goal = settle(goal["id"], ("done",))
    ask = next(r for r in db.runs(goal["id"]) if r["role"] == "ask")
    assert ask["agent"] == "codex" and ask["status"] == "done"


def test_one_run_per_folder(team):
    a, b = start(team), start(team, "Another goal.")
    settle(a["id"], ("proposed",))
    settle(b["id"], ("proposed",))
    spans = sorted((r["started"], r["ended"]) for g in (a, b) for r in db.runs(g["id"]))
    assert spans[1][0] >= spans[0][1]


# ---------- parser / picking ----------

def test_parse_tokens(tmp_path):
    c = tmp_path / "c.jsonl"
    c.write_text(json.dumps({"type": "result", "subtype": "success", "result": "ok", "usage": {
        "input_tokens": 3, "cache_creation_input_tokens": 4, "cache_read_input_tokens": 5, "output_tokens": 6}}))
    assert parse.claude(c)["tokens"] == 13      # cache reads left out
    x = tmp_path / "x.jsonl"
    x.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "cached_input_tokens": 4,
                                                                "output_tokens": 2}}))
    assert parse.codex(x)["tokens"] == 5


def test_pick(team):
    assert usage.pick("any") == ("claude", "Claude has more room")
    assert usage.pick("any", current="codex")[0] == "codex"
    usage.block("codex", time.time() + 600)
    agent, why = usage.pick("codex")
    assert agent == "claude" and "out of usage" in why
    usage.block("claude", time.time() + 600)
    assert usage.pick("any")[0] is None


# ---------- API ----------

def test_api(team, client, anon):
    assert client.post("/api/team/goals", json={"title": "x", "cwd": "/", "goal": "y"}).status_code == 403
    assert client.post("/api/team/goals", json={"title": "x", "cwd": team["cwd"]}).status_code == 400
    assert client.post("/api/team/goals", json={"title": "x", "cwd": team["cwd"],
                                                "runbook": "../../etc/passwd"}).status_code == 400
    with open(f"{team['cwd']}/RUNBOOK.md", "w") as f:
        f.write("1. Do it\n")
    r = client.post("/api/team/goals", json={"title": "Rb", "cwd": team["cwd"], "runbook": "RUNBOOK.md"})
    assert r.status_code == 200 and r.json()["runbook"] == "RUNBOOK.md"
    gid = r.json()["id"]
    settle(gid, ("proposed",))
    assert "Runbook: RUNBOOK.md (in the folder)" in out(db.runs(gid)[0])["prompt"]
    detail = client.get(f"/api/team/goals/{gid}").json()
    assert [p["title"] for p in detail["phases"]] == ["Phase One", "Phase Two"] and len(detail["phases"][0]["tasks"]) == 2
    second = detail["phases"][1]["id"]
    assert client.patch(f"/api/team/phases/{second}", json={"position": 1, "done_when": "ok"}).status_code == 200
    assert [p["title"] for p in db.phases(gid)] == ["Phase Two", "Phase One"]
    task = detail["phases"][0]["tasks"][1]["id"]
    assert client.patch(f"/api/team/tasks/{task}", json={"assignee": "any"}).json()["assignee"] == "any"
    assert client.post(f"/api/team/goals/{gid}/phases", json={"title": "Extra"}).status_code == 200
    extra = db.phases(gid)[-1]["id"]
    assert client.delete(f"/api/team/phases/{extra}").json() == {"ok": True}
    assert client.get("/api/team/overview").json()["goals"][0]["phases_total"] == 2
    assert anon.get("/api/team/overview").status_code == 401
    from fastapi.testclient import TestClient
    from sidus.team import token
    bearer = TestClient(app, headers={"Authorization": f"Bearer {token.get()}"})
    assert bearer.get("/api/team/overview").status_code == 401
    r = bearer.post("/api/team/mcp/propose_tasks", json={"run": db.runs(gid)[0]["id"], "agent": "claude",
                                                         "tasks": [{"title": "x"}]})
    assert r.status_code == 409   # that run has finished
    assert client.put("/api/team/settings", json={"review_each": True}).json()["review_each"] is True
    assert client.delete(f"/api/team/goals/{gid}").json() == {"ok": True} and db.get_goal(gid) is None


def test_old_plan_tables_are_dropped(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE team_plans (id TEXT); CREATE TABLE team_tasks (id TEXT, plan_id TEXT);")
    c.close()
    db.open_db(path)
    cols = [r[1] for r in db.conn().execute("PRAGMA table_info(team_tasks)")]
    assert "phase_id" in cols and not db.conn().execute(
        "SELECT name FROM sqlite_master WHERE name = 'team_plans'").fetchone()


def test_uploaded_runbook(team, client, monkeypatch):
    r = client.post("/api/team/uploads?name=../Run book.md", content=b"1. Install\n2. Migrate\n")
    assert r.status_code == 200 and r.json()["name"] == "Run book.md"
    upload = r.json()["upload"]
    assert client.post("/api/team/goals", json={"title": "x", "cwd": team["cwd"], "runbook": "a.md",
                                                "runbook_upload": upload}).status_code == 400
    r = client.post("/api/team/goals", json={"title": "Up", "cwd": team["cwd"], "runbook_upload": upload})
    assert r.status_code == 200
    gid = r.json()["id"]
    goal = db.get_goal(gid)
    assert goal["runbook"] == str(procs.log_dir(gid) / "Run book.md") and goal["goal"] == \
        "Carry out the runbook Run book.md."
    assert not (settings.data_dir / "team" / "uploads" / upload).exists()     # moved in with the goal
    settle(gid, ("proposed",))
    prompt = out(db.runs(gid)[0])["prompt"]
    assert f"Runbook: {goal['runbook']}. Follow it." in prompt and "(in the folder)" not in prompt
    got = client.get(f"/api/team/goals/{gid}/runbook")
    assert got.text == "1. Install\n2. Migrate\n" and got.headers["content-security-policy"] == "sandbox"
    # Can't reuse an upload, bad ids, empty files, too-large files.
    assert client.post("/api/team/goals", json={"title": "x", "cwd": team["cwd"],
                                                "runbook_upload": upload}).status_code == 400
    assert client.post("/api/team/goals", json={"title": "x", "cwd": team["cwd"],
                                                "runbook_upload": "../../etc"}).status_code == 400
    assert client.post("/api/team/uploads?name=e.md", content=b"").status_code == 400
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    assert client.post("/api/team/uploads?name=big.md", content=b"x").status_code == 413
    assert list((settings.data_dir / "team" / "uploads").iterdir()) == []


def test_goal_file(team, client, anon, tmp_path):
    """Files agents link to in their reports open in the browser (they used to 404 at /root/...)."""
    gid = start(team)["id"]
    settle(gid, ("proposed",))
    Path(team["cwd"], "docs").mkdir()
    Path(team["cwd"], "docs", "report.md").write_text("# Report\n")
    absolute = str(Path(team["cwd"], "docs", "report.md"))
    for path in (absolute, "docs/report.md"):
        got = client.get(f"/api/team/goals/{gid}/file", params={"path": path})
        assert got.status_code == 200 and got.text == "# Report\n"
        assert got.headers["content-type"] == "text/plain; charset=utf-8"
        assert got.headers["content-security-policy"] == "sandbox"
        assert got.headers["content-disposition"].startswith("inline")
    # The goal's own Team folder (goal.md, STATUS.md) is allowed; other places are not.
    procs.log_dir(gid).mkdir(parents=True, exist_ok=True)
    (procs.log_dir(gid) / "STATUS.md").write_text("ok")
    assert client.get(f"/api/team/goals/{gid}/file", params={"path": str(procs.log_dir(gid) / "STATUS.md")}).text == "ok"
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    for path in (str(outside), "../../../../etc/passwd", str(settings.data_dir / "team.db")):
        assert client.get(f"/api/team/goals/{gid}/file", params={"path": path}).status_code == 403
    assert client.get(f"/api/team/goals/{gid}/file", params={"path": "docs/missing.md"}).status_code == 404
    assert client.get("/api/team/goals/nope/file", params={"path": absolute}).status_code == 404
    assert anon.get(f"/api/team/goals/{gid}/file", params={"path": absolute}).status_code == 401
    # The goal lists the files its agents wrote that still exist, inside the allowed places.
    run = db.runs(gid)[0]
    db.conn().execute("UPDATE team_runs SET files = ? WHERE id = ?",
                      (json.dumps([absolute, str(outside), absolute + ".gone"]), run["id"]))
    db.conn().commit()
    Path(team["cwd"], "docs", "linked.md").write_text("x")
    db.comment(gid, "claude", "Wrote [notes](docs/linked.md:3) and [site](https://example.com).")
    db.comment(gid, "you", "See [mine](docs/missing-mine.md)")
    arts = client.get(f"/api/team/goals/{gid}").json()["artifacts"]
    assert sorted((a["path"], a["name"]) for a in arts) == [
        (str(Path(team["cwd"], "docs", "linked.md")), "docs/linked.md"), (absolute, "docs/report.md")]
