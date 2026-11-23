"""Relay: routing, the event-stream parser, and whole turns run against stand-in claude/codex scripts."""
import json
import os
import sys
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from sidus.app import app
from sidus.config import settings
from sidus.relay import db, parse, routing, runner, token
from sidus.store import kv_set

FAKE_CLAUDE = r'''
import json, os, sys, time, uuid
args = sys.argv[1:]
prompt = args[args.index("--") + 1] if "--" in args else ""
sid = args[args.index("--resume") + 1] if "--resume" in args else None
if "--fork-session" in args:
    sid = str(uuid.uuid4())
if "--session-id" in args:
    sid = args[args.index("--session-id") + 1]
sid = sid or str(uuid.uuid4())
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "system", "subtype": "init", "session_id": sid})
if "SLOW" in prompt:
    time.sleep(float(os.environ.get("FAKE_SLOW", "3")))
if "CLAUDELIMIT" in prompt:
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": time.time() + 7200,
          "unifiedWindows": {"five_hour": {"utilization": 1.0}, "seven_day": {"utilization": 0.4}}}})
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "half.py"}}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached", "session_id": sid})
    sys.exit(1)
if "EDIT" in prompt:
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}})
emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
      "unifiedWindows": {"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.2}}}})
out = {"agent": "claude", "prompt": prompt, "mode": args[args.index("--permission-mode") + 1],
       "fork": "--fork-session" in args, "mcp": "--mcp-config" in args, "resumed": "--resume" in args}
emit({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(out), "session_id": sid})
'''

FAKE_CODEX = r'''
import json, os, sys, time, uuid
args = sys.argv[1:]
rest = args[args.index("--") + 1:]
resume = args[1] == "resume"
sid, prompt = (rest[0], rest[1]) if resume else (str(uuid.uuid4()), rest[0])
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "thread.started", "thread_id": sid})
if "SLOW" in prompt:
    time.sleep(float(os.environ.get("FAKE_SLOW", "3")))
if "CODEXLIMIT" in prompt:
    emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0}})
emit({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "b.py", "kind": "update"}]}})
out = {"agent": "codex", "prompt": prompt, "resumed": resume, "mcp": any("mcp_servers" in a for a in args)}
emit({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(out)}})
emit({"type": "turn.completed", "usage": {}})
'''


def _script(path, body):
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def relay(tmp_path, monkeypatch, workspace):
    db.open_db(tmp_path / "relay.db")
    kv_set("relay_blocked", {})
    kv_set("relay_settings", {})
    kv_set("relay_claude_limits", {})
    monkeypatch.setattr(settings, "claude_cmd", _script(tmp_path / "claude", FAKE_CLAUDE))
    monkeypatch.setattr(settings, "claude_base_args", "")
    monkeypatch.setattr(settings, "claude_args", "")
    codex_bin = _script(tmp_path / "codex", FAKE_CODEX)
    from sidus.providers import codex
    monkeypatch.setattr(codex, "binary", lambda: codex_bin)
    monkeypatch.setattr(routing, "linked", lambda agent: True)
    usage = {"claude": {"five_hour": 10, "weekly": 10, "at": time.time()},
             "codex": {"five_hour": 20, "weekly": 20, "at": time.time()}}
    monkeypatch.setattr(routing, "_claude_usage", lambda: usage["claude"])
    monkeypatch.setattr(routing, "_codex_usage", lambda: usage["codex"])
    folder = workspace / "proj"
    folder.mkdir(exist_ok=True)
    return {"usage": usage, "cwd": str(folder)}


def wait(thread_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        ts = db.turns(thread_id)
        if ts and all(t["status"] not in ("queued", "running") for t in ts):
            return ts
        time.sleep(0.1)
    raise AssertionError(f"thread still busy: {[(t['agent'], t['status']) for t in db.turns(thread_id)]}")


def result(turn):
    return json.loads(turn["result"])


# ---------- parser ----------

def test_parse_claude_and_codex(tmp_path):
    c = tmp_path / "c.jsonl"
    c.write_text("\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/x/y.py"}},
            {"type": "tool_use", "name": "mcp__sidus-relay__ask_codex", "input": {}}]}},
        "not json",
        {"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "session_id": "s1"},
    ]))
    p = parse.claude(c)
    assert p["session_id"] == "s1" and p["text"] == "Done." and p["finished"] and not p["error"]
    assert p["steps"] == ["Ran `pytest -q`", "Wrote /x/y.py", "Asked Codex"] and p["files"] == ["/x/y.py"]

    x = tmp_path / "x.jsonl"
    x.write_text("\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed"},
    ]))
    p = parse.codex(x)
    assert p["session_id"] == "t1" and p["text"] == "ok" and p["finished"] and not p["error"] and not p["limit"]


def test_parse_limits(tmp_path):
    x = tmp_path / "x.jsonl"
    x.write_text(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}))
    assert parse.codex(x)["limit"]
    c = tmp_path / "c.jsonl"
    c.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 5}}))
    assert parse.claude(c)["limit"] and parse.claude(c)["limit_resets"] == 5


# ---------- routing ----------

def test_routing(relay):
    u = relay["usage"]
    thread = {"pin": None}
    turn = {"kind": "message", "force_agent": None}
    assert routing.choose(thread, turn, None)[0] == "claude"            # more room
    assert routing.choose(thread, turn, "codex")[0] == "codex"          # stays put while under
    u["claude"]["five_hour"] = 85
    agent, why = routing.choose(thread, turn, "claude")
    assert agent == "codex" and "85% of its 5-hour" in why
    u["codex"]["weekly"] = 95
    assert routing.choose(thread, turn, "claude")[0] == "claude"        # both over: more room wins (15 vs 5)
    assert routing.choose({"pin": "codex"}, turn, "claude")[0] == "codex"
    assert routing.choose(thread, {"kind": "handoff", "force_agent": "codex"}, "claude")[0] == "codex"
    routing.block("codex", time.time() + 600)
    assert routing.choose({"pin": "codex"}, turn, None)[0] is None
    agent, why = routing.choose(thread, {"kind": "handoff", "force_agent": "codex"}, "claude")
    assert agent == "claude" and "keeps it" in why
    routing.block("claude", time.time() + 600)
    assert routing.choose(thread, turn, None)[0] is None


def test_settings_clamped(relay):
    assert routing.save_settings(five_hour=150, weekly=0, default="codex") == {"five_hour": 100, "weekly": 1,
                                                                               "default": "codex"}


# ---------- turns ----------

def test_turn_then_switch_with_catch_up(relay):
    t = db.create_thread("Demo", relay["cwd"])
    runner.send(t["id"], "first EDIT")
    first = wait(t["id"])[0]
    assert first["status"] == "done" and first["agent"] == "claude" and first["files"] == ["a.py"]
    r = result(first)
    assert r["mode"] == "bypassPermissions" and r["mcp"] and not r["resumed"]
    assert "Sidus Relay thread" in r["prompt"] and r["prompt"].endswith("first EDIT")
    assert db.get_thread(t["id"])["claude_session"] == first["session_id"]

    relay["usage"]["claude"]["five_hour"] = 90
    runner.send(t["id"], "second")
    second = wait(t["id"])[1]
    assert second["agent"] == "codex" and "90%" in second["reason"]
    p = result(second)["prompt"]
    assert "Catching up" in p and "The user asked: first EDIT" in p and "Files Claude changed: a.py" in p
    assert db.get_thread(t["id"])["codex_session"] == second["session_id"]

    relay["usage"]["claude"]["five_hour"] = 10
    relay["usage"]["codex"]["five_hour"] = 90
    runner.send(t["id"], "third")
    third = wait(t["id"])[2]
    r = result(third)
    assert third["agent"] == "claude" and r["resumed"] and r["prompt"].startswith("## Catching up")
    # Only what Claude missed. (Codex's stand-in reply quotes its own prompt, with escaped newlines.)
    assert "\nThe user asked: second" in r["prompt"] and "\nThe user asked: first EDIT" not in r["prompt"]


def test_usage_limit_moves_work_to_the_other_agent(relay):
    t = db.create_thread("Limit", relay["cwd"])
    runner.send(t["id"], "do it CLAUDELIMIT")
    first, retry = wait(t["id"])
    assert first["status"] == "limit" and first["agent"] == "claude"
    assert retry["kind"] == "retry" and retry["agent"] == "codex" and retry["status"] == "done"
    p = result(retry)["prompt"]
    assert "hit its usage limit" in p and "Edited half.py" in p and "carry on with: do it CLAUDELIMIT" in p
    assert routing.blocked_until("claude")
    runner.send(t["id"], "next")
    assert wait(t["id"])[2]["agent"] == "codex"   # Claude stays blocked


def test_both_out_of_usage_waits(relay):
    t = db.create_thread("Wait", relay["cwd"])
    routing.block("claude", time.time() + 600)
    routing.block("codex", time.time() + 600)
    runner.send(t["id"], "hello")
    assert db.turns(t["id"])[0]["status"] == "queued"
    assert "out of usage" in db.get_thread(t["id"])["waiting"]
    kv_set("relay_blocked", {})
    runner.kick(t["id"])
    assert wait(t["id"])[0]["status"] == "done" and not db.get_thread(t["id"])["waiting"]


def test_handoff_and_consult(relay, monkeypatch):
    monkeypatch.setenv("FAKE_SLOW", "4")
    t = db.create_thread("Pair", relay["cwd"])
    turn = runner.send(t["id"], "SLOW work")
    time.sleep(0.3)
    answer = runner.consult(turn["id"], "claude", "Is this right?")
    a = json.loads(answer)
    assert a["agent"] == "codex" and not a["mcp"] and "Claude's question\nIs this right?" in a["prompt"]
    assert "don't change any files" in a["prompt"].lower()
    assert "Handed off" in runner.handoff(turn["id"], "claude", "finish the tests", "half done")
    assert "no running" in runner.handoff(turn["id"], "codex", "x", "")   # not codex's turn
    first, second = wait(t["id"])
    assert first["consults"][0]["to"] == "codex" and first["consults"][0]["answer"] == answer
    assert second["kind"] == "handoff" and second["agent"] == "codex"
    p = result(second)["prompt"]
    assert "Claude handed this over to you\nTask: finish the tests" in p and "Notes: half done" in p


def test_claude_consult_is_read_only_fork(relay, monkeypatch):
    t = db.create_thread("Ask", relay["cwd"])
    runner.send(t["id"], "start")
    wait(t["id"])
    monkeypatch.setenv("FAKE_SLOW", "3")
    relay["usage"]["claude"]["five_hour"] = 95
    turn = runner.send(t["id"], "SLOW codex turn")
    time.sleep(0.3)
    a = json.loads(runner.consult(turn["id"], "codex", "Review?"))
    assert a["agent"] == "claude" and a["mode"] == "default" and a["fork"] and a["resumed"] and not a["mcp"]
    wait(t["id"])


def test_stop_and_recover(relay, monkeypatch):
    monkeypatch.setenv("FAKE_SLOW", "30")
    t = db.create_thread("Stop", relay["cwd"])
    runner.send(t["id"], "SLOW")
    time.sleep(0.3)
    assert runner.stop(t["id"])
    assert wait(t["id"])[0]["status"] == "stopped"
    # A turn left "running" by a Sidus that died, whose process is gone, is finished on start.
    turn = db.add_turn(t["id"], "message", "orphan")
    db.update_turn(turn["id"], status="running", agent="claude", pid=999_999_999, log=str(runner.log_dir(t["id"]) / "none"))
    runner.recover()
    assert db.get_turn(turn["id"])["status"] == "failed"


# ---------- API ----------

def test_api(relay, client, anon):
    assert client.post("/api/relay/threads", json={"name": "x", "cwd": "/"}).status_code == 403
    r = client.post("/api/relay/threads", json={"name": "Api", "cwd": relay["cwd"], "message": "hi"})
    assert r.status_code == 200
    tid = r.json()["id"]
    wait(tid)
    detail = client.get(f"/api/relay/threads/{tid}").json()
    assert detail["turn_list"][0]["status"] == "done" and detail["last_agent"] == "claude"
    assert client.patch(f"/api/relay/threads/{tid}", json={"pin": "codex"}).json()["pin"] == "codex"
    assert client.get("/api/relay/status").json()["agents"]["claude"]["linked"]
    assert anon.get("/api/relay/threads").status_code == 401
    # The MCP token works only for /api/relay/mcp/*.
    bearer = TestClient(app, headers={"Authorization": f"Bearer {token.get()}"})
    assert bearer.get("/api/relay/mcp/usage").status_code == 200
    assert bearer.get("/api/relay/threads").status_code == 401
    wrong = TestClient(app, headers={"Authorization": "Bearer nope"})
    assert wrong.get("/api/relay/mcp/usage").status_code == 401
    assert client.delete(f"/api/relay/threads/{tid}").json() == {"ok": True}
    assert not os.path.exists(runner.log_dir(tid) / "x") and db.get_thread(tid) is None
