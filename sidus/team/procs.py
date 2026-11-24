"""Starting agent runs: command lines, environment, and the background process.

Every run is `claude -p` or `codex exec`, started in its own process group with its output going to a log file
(not a pipe), so it keeps going if Sidus restarts; Sidus picks it up again on start.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import shlex
import subprocess
import sys
from pathlib import Path

import psutil

from .. import plugins
from ..config import settings
from ..providers.pty_run import clean_env
from . import token, usage

SERVER = "sidus-team"
ROOT = Path(__file__).resolve().parents[2]  # folder that contains the sidus package
ASK_TIMEOUT = 600   # seconds an agent waits for the other's answer
TOOL_TIMEOUT = ASK_TIMEOUT + 60
MANAGER_TOOLS = "Read,Glob,Grep"   # the manager reads; it never edits


def log_dir(plan_id: str) -> Path:
    d = settings.data_dir / "team" / plan_id
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def mcp_env(run_id: str, agent: str, role: str) -> dict:
    token.get()  # make sure the token file exists
    return {"SIDUS_URL": sidus_url(), "SIDUS_TEAM_TOKEN_FILE": str(token.path()), "SIDUS_TEAM_RUN": run_id,
            "SIDUS_TEAM_AGENT": agent, "SIDUS_TEAM_ROLE": role, "PYTHONPATH": str(ROOT)}


def claude_cmd(prompt: str, *, session: str | None = None, new_session: str | None = None, fork: bool = False,
               mcp: dict | None = None, manager: bool = False, read_only: bool = False) -> list[str]:
    s = usage.get_settings()
    args = [settings.claude_cmd, *shlex.split(settings.claude_base_args)]
    # Other plugins' MCP servers (e.g. Ideas) go into the same --mcp-config as Team's, so there's one of each flag.
    servers, allowed = {}, []
    if not (manager or read_only):
        extra = plugins.claude_args()
        i = 0
        while i < len(extra):
            flag, value = extra[i], extra[i + 1] if i + 1 < len(extra) else None
            if flag == "--mcp-config" and value:
                servers.update(json.loads(value).get("mcpServers", {}))
                i += 2
            elif flag == "--allowedTools" and value:
                allowed.append(value)
                i += 2
            else:
                args.append(flag)
                i += 1
    args += shlex.split(settings.claude_args)
    args += ["-p", "--output-format", "stream-json", "--verbose"]
    if manager or read_only:
        args += ["--permission-mode", "default", "--tools", MANAGER_TOOLS]
    else:
        args += ["--permission-mode", "bypassPermissions"]
    if manager:
        if s["manager_claude_model"]:
            args += ["--model", s["manager_claude_model"]]
        args += ["--max-turns", str(s["manager_max_turns"])]
    if mcp:
        servers[SERVER] = {"command": sys.executable, "args": ["-m", "sidus.team.mcp"], "env": mcp}
        allowed.append(f"mcp__{SERVER}__*")
    if servers:
        args += ["--mcp-config", json.dumps({"mcpServers": servers}, separators=(",", ":"))]
    if allowed:
        args += ["--allowedTools", ",".join(allowed)]
    if session:
        args += ["--resume", session] + (["--fork-session"] if fork else [])
    elif new_session:
        args += ["--session-id", new_session]
    return args + ["--", prompt]


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)  # strings and lists of strings are valid TOML in JSON form


def codex_cmd(prompt: str, *, session: str | None = None, mcp: dict | None = None, manager: bool = False) -> list[str]:
    from ..providers import codex
    exe = codex.binary()
    if not exe:
        raise RuntimeError("Codex isn't installed. Link ChatGPT in Settings first.")
    args = [exe, "exec"] + (["resume"] if session else [])
    args += ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    effort = usage.get_settings()["manager_codex_effort"]
    if manager and effort:
        args += ["-c", f"model_reasoning_effort={_toml(effort)}"]
    if mcp:
        key = f"mcp_servers.{SERVER}"
        args += ["-c", f"{key}.command={_toml(sys.executable)}", "-c", f"{key}.args={_toml(['-m', 'sidus.team.mcp'])}",
                 "-c", f"{key}.env={_toml(mcp)}", "-c", f"{key}.tool_timeout_sec={TOOL_TIMEOUT}"]
    return args + ["--"] + ([session] if session else []) + [prompt]


def agent_env(agent: str) -> dict:
    env = clean_env({"CODEX_HOME": str(settings.codex_home)})
    env.pop("NO_COLOR", None)
    if agent == "claude":
        env["MCP_TOOL_TIMEOUT"] = str(TOOL_TIMEOUT * 1000)
    return env


def spawn(args: list[str], cwd: str, agent: str, log: Path) -> subprocess.Popen:
    with open(log, "wb") as out, open(log.with_suffix(".err"), "wb") as err:
        return subprocess.Popen(args, cwd=cwd, env=agent_env(agent), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=err, start_new_session=True)


def started_at(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def alive(run: dict) -> bool:
    try:
        p = psutil.Process(run["pid"])
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and (
            not run["pid_started"] or abs(p.create_time() - run["pid_started"]) < 1)
    except (psutil.Error, TypeError):
        return False


def stderr_tail(log: str | None) -> str:
    if not log:
        return ""
    try:
        data = Path(log).with_suffix(".err").read_bytes()[-2000:].decode(errors="replace")
    except OSError:
        return ""
    lines = [l for l in data.splitlines() if l.strip() and "Reading additional input" not in l
             and not l.startswith("Warning: this sign-in")]
    return " ".join(lines[-3:])[:500]


# ---------- what a run changed (git) ----------

def _git(cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode(errors="replace") if r.returncode == 0 else None


def git_state(cwd: str) -> dict | None:
    """HEAD plus a fingerprint of every changed or untracked file, or None outside a git repo."""
    head = _git(cwd, "rev-parse", "HEAD")
    status = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status is None:
        return None
    files = {}
    entries = status.split("\0")
    i = 0
    while i < len(entries) and len(files) < 500:
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":   # a rename is followed by its old path
            i += 1
        path = entry[3:]
        try:
            with open(Path(cwd) / path, "rb") as f:
                files[path] = hashlib.sha1(f.read(4_000_000)).hexdigest()
        except OSError:
            files[path] = None   # deleted
    return {"head": (head or "").strip(), "files": files}


def git_changed(cwd: str, before: dict | None) -> list[str]:
    """Files that changed between `before` (from git_state) and now."""
    after = git_state(cwd)
    if not before or not after:
        return []
    changed = {p for p in set(before["files"]) | set(after["files"])
               if before["files"].get(p, "clean") != after["files"].get(p, "clean")}
    if before["head"] and after["head"] and before["head"] != after["head"]:
        changed |= set((_git(cwd, "diff", "--name-only", before["head"], after["head"]) or "").split())
    return sorted(changed)[:200]
