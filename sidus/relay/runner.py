"""Runs Relay turns: one agent run at a time per thread, in the background, unattended.

Each turn is `claude -p` or `codex exec` with permissions bypassed, started in its own process group with its
output going to a log file (not a pipe), so a turn keeps going if Sidus restarts; Sidus picks it up again on
start. When a turn ends, the next queued one starts. A hand-over or a usage-limit hit queues a turn for the other
agent, with a catch-up of what it missed.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil

from .. import plugins
from ..config import settings
from ..providers.pty_run import clean_env
from ..store import all_meta, set_meta
from . import db, parse, routing, token

LABEL = routing.LABEL
SERVER = "sidus-relay"
ROOT = Path(__file__).resolve().parents[2]  # folder that contains the sidus package
CONSULT_TIMEOUT = 600   # seconds an agent waits for the other's answer
TOOL_TIMEOUT = CONSULT_TIMEOUT + 60
CATCH_UP_CHARS = 8000
REPLY_CHARS = 1500

_lock = threading.RLock()
_stopping: set[str] = set()


def log_dir(thread_id: str) -> Path:
    d = settings.data_dir / "relay" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- prompts ----------

PREAMBLE = """You're working in a Sidus Relay thread. The user talks to one thread, and each turn goes to Claude Code \
or Codex, depending on which plan has usage left. Your partner in this thread is {partner}.

Your sidus-relay tools:
- ask_{partner_id}(question): ask {partner} something, such as a second opinion or a review. It answers without \
changing files.
- hand_off(task, notes): give the rest of the work to {partner}. Use it when {partner} is better suited or you are \
low on usage. After calling it, end your turn with a one-line summary.
- usage(): see how much of each plan is left.

This runs unattended: nobody can answer questions mid-turn, so make reasonable choices and say what you decided."""


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _turn_summary(t: dict) -> str:
    who = LABEL.get(t["agent"] or "", "An agent")
    lines = []
    if t["kind"] == "message":
        lines.append(f"The user asked: {_clip(t['input'], REPLY_CHARS)}")
    elif t["kind"] == "handoff":
        lines.append(f"Handed to {who}: {_clip(t['input'], REPLY_CHARS)}")
    else:
        lines.append(f"{who} continued: {_clip(t['input'], 300)}")
    if t["result"]:
        lines.append(f"{who} replied: {_clip(t['result'], REPLY_CHARS)}")
    if t["files"]:
        lines.append(f"Files {who} changed: " + ", ".join(t["files"][:30]))
    if t["status"] in ("failed", "stopped"):
        lines.append(f"({who}'s turn {t['status']}{': ' + t['error'] if t['error'] else ''})")
    return "\n".join(lines)


def catch_up(thread_id: str, upto_seq: int, agent: str) -> str:
    """What happened in the thread since `agent` last took a turn (all of it if it never has)."""
    earlier = [t for t in db.turns(thread_id) if t["seq"] < upto_seq and t["status"] not in ("queued", "running")]
    mine = [t["seq"] for t in earlier if t["agent"] == agent and t["status"] in ("done", "failed", "stopped")]
    since = max(mine) if mine else 0
    # Turns that hit the usage limit are summed up in the retry's notes instead.
    missed = [t for t in earlier if t["seq"] > since and t["agent"] != agent and t["status"] != "limit"]
    parts, total = [], 0
    for t in reversed(missed):  # newest first, so the most recent survive the size cap
        s = _turn_summary(t)
        if total + len(s) > CATCH_UP_CHARS:
            parts.append("(Earlier turns left out.)")
            break
        parts.append(s)
        total += len(s)
    return "\n\n".join(reversed(parts))


def build_prompt(thread: dict, turn: dict, agent: str) -> str:
    partner = routing.other(agent)
    parts = []
    if not thread.get(f"{agent}_session"):
        parts.append(PREAMBLE.format(partner=LABEL[partner], partner_id=partner))
    recap = catch_up(thread["id"], turn["seq"], agent)
    if recap:
        parts.append(f"## Catching up\n{LABEL[partner]} handled part of this thread since you last worked on it:\n\n"
                     + recap)
    if turn["kind"] == "message":
        parts.append(("## The user's message\n" if parts else "") + turn["input"])
    elif turn["kind"] == "handoff":
        parts.append(f"## {LABEL[partner]} handed this over to you\nTask: {turn['input']}"
                     + (f"\n\nNotes: {turn['notes']}" if turn["notes"] else ""))
    else:
        parts.append(f"## {LABEL[partner]} hit its usage limit while working on this\n"
                     + (f"What it had done:\n{turn['notes']}\n\n" if turn["notes"] else "")
                     + f"Please carry on with: {turn['input']}")
    return "\n\n".join(parts)


# ---------- commands ----------

def sidus_url() -> str:
    host = settings.host.strip()
    if host in {"", "0.0.0.0", "::", "*"}:
        host = "127.0.0.1"
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    return f"http://{host}:{settings.port}"


def mcp_env(thread_id: str, turn_id: str, agent: str) -> dict:
    return {"SIDUS_URL": sidus_url(), "SIDUS_RELAY_TOKEN_FILE": str(token.path()), "SIDUS_RELAY_THREAD": thread_id,
            "SIDUS_RELAY_TURN": turn_id, "SIDUS_RELAY_AGENT": agent, "PYTHONPATH": str(ROOT)}


def claude_cmd(prompt: str, *, session: str | None, new_session: str | None = None, fork: bool = False,
               mcp: dict | None = None, read_only: bool = False) -> list[str]:
    args = [settings.claude_cmd, *shlex.split(settings.claude_base_args)]
    if not read_only:
        args += plugins.claude_args()
    args += shlex.split(settings.claude_args)
    args += ["-p", "--output-format", "stream-json", "--verbose",
             "--permission-mode", "default" if read_only else "bypassPermissions"]
    if mcp:
        config = {"mcpServers": {SERVER: {"command": sys.executable, "args": ["-m", "sidus.relay.mcp"], "env": mcp}}}
        args += ["--mcp-config", json.dumps(config, separators=(",", ":")), "--allowedTools", f"mcp__{SERVER}__*"]
    if session:
        args += ["--resume", session] + (["--fork-session"] if fork else [])
    elif new_session:
        args += ["--session-id", new_session]
    return args + ["--", prompt]


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)  # strings and lists of strings are valid TOML in JSON form


def codex_cmd(prompt: str, *, session: str | None, mcp: dict | None = None) -> list[str]:
    from ..providers import codex
    exe = codex.binary()
    if not exe:
        raise RuntimeError("Codex isn't installed. Link ChatGPT in Settings first.")
    args = [exe, "exec"] + (["resume"] if session else [])
    args += ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    if mcp:
        key = f"mcp_servers.{SERVER}"
        args += ["-c", f"{key}.command={_toml(sys.executable)}", "-c", f"{key}.args={_toml(['-m', 'sidus.relay.mcp'])}",
                 "-c", f"{key}.env={_toml(mcp)}", "-c", f"{key}.tool_timeout_sec={TOOL_TIMEOUT}"]
    return args + ["--"] + ([session] if session else []) + [prompt]


def agent_env(agent: str) -> dict:
    env = clean_env({"CODEX_HOME": str(settings.codex_home)})
    env.pop("NO_COLOR", None)
    if agent == "claude":
        env["MCP_TOOL_TIMEOUT"] = str(TOOL_TIMEOUT * 1000)
    return env


def _spawn(args: list[str], cwd: str, agent: str, log: Path) -> subprocess.Popen:
    with open(log, "wb") as out, open(log.with_suffix(".err"), "wb") as err:
        return subprocess.Popen(args, cwd=cwd, env=agent_env(agent), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=err, start_new_session=True)


def _tag(session_id: str | None, name: str) -> None:
    if session_id and not (all_meta().get(session_id) or {}).get("name"):
        set_meta(session_id, name=name)


# ---------- scheduling ----------

def send(thread_id: str, text: str) -> dict:
    turn = db.add_turn(thread_id, "message", text)
    kick(thread_id)
    return db.get_turn(turn["id"])


def _last_agent(thread_id: str) -> str | None:
    for t in reversed(db.turns(thread_id)):
        if t["agent"] and t["status"] != "queued":
            return t["agent"]
    return None


def kick(thread_id: str) -> None:
    """Starts the thread's next queued turn if nothing is running."""
    with _lock:
        thread = db.get_thread(thread_id)
        if not thread or thread["archived"] or db.running_turn(thread_id):
            return
        turn = db.next_queued(thread_id)
        if not turn:
            if thread["waiting"]:
                db.update_thread(thread_id, waiting=None)
            return
        agent, reason = routing.choose(thread, turn, _last_agent(thread_id))
        if not agent:
            db.update_thread(thread_id, waiting=reason)
            return
        if thread["waiting"]:
            db.update_thread(thread_id, waiting=None)
        try:
            _start(thread, turn, agent, reason)
        except Exception as e:  # e.g. Codex missing: fail this turn, try the next
            db.update_turn(turn["id"], agent=agent, reason=reason, status="failed", error=str(e),
                           started=time.time(), ended=time.time())
            threading.Thread(target=kick, args=(thread_id,), daemon=True).start()


def _start(thread: dict, turn: dict, agent: str, reason: str) -> None:
    prompt = build_prompt(thread, turn, agent)
    log = log_dir(thread["id"]) / f"{turn['seq']:04d}-{turn['id']}-{agent}.jsonl"
    mcp = mcp_env(thread["id"], turn["id"], agent)
    session = thread.get(f"{agent}_session")
    if agent == "claude":
        # The thread keeps the ID once the turn has run (finalize), so a turn that fails before Claude saves
        # the conversation doesn't leave the thread pointing at one that doesn't exist.
        args = claude_cmd(prompt, session=session, new_session=None if session else str(uuid.uuid4()), mcp=mcp)
    else:
        args = codex_cmd(prompt, session=session, mcp=mcp)
    proc = _spawn(args, thread["cwd"], agent, log)
    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = None
    db.update_turn(turn["id"], agent=agent, reason=reason, status="running", started=time.time(), log=str(log),
                   pid=proc.pid, pid_started=created)
    threading.Thread(target=_watch, args=(turn["id"], proc), daemon=True, name=f"relay-{turn['id']}").start()


def _alive(turn: dict) -> bool:
    try:
        p = psutil.Process(turn["pid"])
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and (
            not turn["pid_started"] or abs(p.create_time() - turn["pid_started"]) < 1)
    except (psutil.Error, TypeError):
        return False


def _watch(turn_id: str, proc: subprocess.Popen | None) -> None:
    if proc is not None:
        proc.wait()
    else:  # picked up after a restart: not our child, so poll
        while _alive(db.get_turn(turn_id) or {}):
            time.sleep(2)
    try:
        finalize(turn_id)
    except Exception as e:
        print(f"Relay: couldn't finish turn {turn_id}: {e}", flush=True)


def _stderr_tail(log: str | None) -> str:
    if not log:
        return ""
    try:
        data = Path(log).with_suffix(".err").read_bytes()[-2000:].decode(errors="replace")
    except OSError:
        return ""
    lines = [l for l in data.splitlines() if l.strip() and "Reading additional input" not in l]
    return " ".join(lines[-3:])[:500]


def _partial(parsed: dict) -> str:
    bits = parsed["steps"][-15:]
    if parsed["text"]:
        bits.append(f"Last thing it said: {_clip(parsed['text'], 800)}")
    if parsed["files"]:
        bits.append("Files it changed: " + ", ".join(parsed["files"][:30]))
    return "\n".join(f"- {b}" for b in bits)


def finalize(turn_id: str) -> None:
    with _lock:
        turn = db.get_turn(turn_id)
        if not turn or turn["status"] != "running":
            return
        agent, thread_id = turn["agent"], turn["thread_id"]
        parsed = parse.read(agent, turn["log"] or "")
        if agent == "claude" and parsed["rate_limit"]:
            routing.record_claude_limits(parsed["rate_limit"])
        stopped = turn_id in _stopping
        _stopping.discard(turn_id)
        error = parsed["error"]
        if stopped:
            status = "stopped"
        elif parsed["limit"]:
            status = "limit"
        elif parsed["finished"] and not error:
            status = "done"
        else:
            status, error = "failed", error or _stderr_tail(turn["log"]) or f"{LABEL[agent]} exited without finishing"
        db.update_turn(turn_id, status=status, session_id=parsed["session_id"], result=parsed["text"],
                       error="" if status == "done" else error, steps=parsed["steps"], files=parsed["files"],
                       ended=time.time(), pid=None)
        thread = db.get_thread(thread_id)
        if parsed["session_id"]:
            if thread.get(f"{agent}_session") != parsed["session_id"]:
                db.update_thread(thread_id, **{f"{agent}_session": parsed["session_id"]})
            _tag(parsed["session_id"], f"Relay · {thread['name']}")
        if status == "limit":
            routing.block(agent, parsed["limit_resets"])
            db.add_turn(thread_id, "retry", turn["input"], notes=_partial(parsed), force_agent=routing.other(agent))
        elif status == "done" and turn["handoff"]:
            h = turn["handoff"]
            db.add_turn(thread_id, "handoff", h["task"], notes=h.get("notes", ""), force_agent=routing.other(agent))
    kick(thread_id)


def stop(thread_id: str) -> bool:
    turn = db.running_turn(thread_id)
    if not turn or not turn["pid"] or not _alive(turn):
        return False
    _stopping.add(turn["id"])
    try:
        os.killpg(turn["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    return True


def recover() -> None:
    """On start: re-attach to turns still running from before a restart, finish the rest, start queued ones."""
    for turn in db.turns_with_status("running"):
        if _alive(turn):
            threading.Thread(target=_watch, args=(turn["id"], None), daemon=True).start()
        else:
            finalize(turn["id"])
    for thread_id in db.threads_with_queue():
        kick(thread_id)


def ticker() -> None:
    """Starts queued turns that were waiting for usage to free up."""
    while True:
        time.sleep(30)
        for thread_id in db.threads_with_queue():
            try:
                kick(thread_id)
            except Exception as e:
                print(f"Relay: {e}", flush=True)


# ---------- tools the agents call (through the MCP server) ----------

def handoff(turn_id: str, agent: str, task: str, notes: str) -> str:
    turn = db.get_turn(turn_id)
    if not turn or turn["status"] != "running" or turn["agent"] != agent:
        return "There's no running Relay turn of yours to hand off."
    partner = routing.other(agent)
    if not routing.linked(partner):
        return f"{LABEL[partner]} isn't signed in, so this can't be handed over. Carry on yourself."
    db.update_turn(turn_id, handoff={"task": task, "notes": notes})
    return (f"Handed off. {LABEL[partner]} takes over when your turn ends. End your turn now with a one-line summary "
            "of what you did.")


def consult(turn_id: str, agent: str, question: str) -> str:
    """Runs the other agent on a question, without changing files, and returns its answer."""
    turn = db.get_turn(turn_id)
    if not turn or turn["status"] != "running" or turn["agent"] != agent:
        return "There's no running Relay turn of yours, so the question wasn't sent."
    thread = db.get_thread(turn["thread_id"])
    partner = routing.other(agent)
    if not routing.linked(partner):
        return f"{LABEL[partner]} isn't signed in, so it can't answer."
    if until := routing.blocked_until(partner):
        return f"{LABEL[partner]} is out of usage until {time.strftime('%H:%M', time.localtime(until))}."
    context = catch_up(thread["id"], turn["seq"] + 1, partner)
    prompt = (f"{LABEL[agent]} is working on a task in this folder and asks you the question below. Answer it "
              "directly. Don't change any files: you can read them and run read-only commands."
              + (f"\n\n## What's happened in the thread\n{context}" if context else "")
              + f"\n\n## {LABEL[agent]}'s question\n{question}")
    log = log_dir(thread["id"]) / f"{turn['seq']:04d}-{turn['id']}-ask-{partner}-{uuid.uuid4().hex[:6]}.jsonl"
    if partner == "claude":
        args = claude_cmd(prompt, session=thread.get("claude_session"), fork=True, read_only=True)
    else:
        args = codex_cmd(prompt, session=None)
    proc = _spawn(args, thread["cwd"], partner, log)
    try:
        proc.wait(timeout=CONSULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        answer, parsed = "", parse.read(partner, log)
        result = f"{LABEL[partner]} didn't answer within {CONSULT_TIMEOUT // 60} minutes."
    else:
        parsed = parse.read(partner, log)
        if partner == "claude" and parsed["rate_limit"]:
            routing.record_claude_limits(parsed["rate_limit"])
        if parsed["limit"]:
            routing.block(partner, parsed["limit_resets"])
        answer = parsed["text"]
        result = answer or f"{LABEL[partner]} couldn't answer: {parsed['error'] or _stderr_tail(str(log)) or 'no reply'}"
    _tag(parsed["session_id"], f"Relay · {thread['name']} · question from {LABEL[agent]}")
    current = db.get_turn(turn_id)
    db.update_turn(turn_id, consults=current["consults"] + [{
        "to": partner, "question": question, "answer": answer, "session_id": parsed["session_id"],
        "error": "" if answer else result, "at": time.time()}])
    return result


def live(turn: dict) -> dict:
    """A running turn's progress so far, read from its log."""
    if turn["status"] != "running" or not turn["log"]:
        return turn
    parsed = parse.read(turn["agent"], turn["log"])
    return {**turn, "steps": parsed["steps"], "files": parsed["files"], "result": parsed["text"],
            "session_id": parsed["session_id"] or turn["session_id"]}
