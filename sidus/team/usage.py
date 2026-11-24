"""Plan usage, and which agent takes a role.

Usage for each plan comes from the freshest of: what Claude Code reported on its last run (rate_limit_event),
Sidus's Claude usage poller, and Sidus's ChatGPT usage poller / Codex's logs. Percentages are 0-100.

An agent is "under" while its 5-hour and weekly use are below the switch point (settings). pick():
1. An agent that's signed out, or blocked after hitting its limit, isn't used.
2. A task assigned to one agent goes to it if it's usable; otherwise to the other one.
3. "any": the current agent while it's under, else one that's under (more room first), else the one with more room.
"""
from __future__ import annotations

import shutil
import time

from ..config import settings
from ..store import kv_get, kv_set

AGENTS = ("claude", "codex")
LABEL = {"claude": "Claude", "codex": "Codex"}
DEFAULTS = {
    "five_hour": 80, "weekly": 90, "default": "claude",
    # Keep the manager cheap: it plans and checks, the workers do the work.
    "manager_claude_model": "sonnet",   # --model for Claude as manager ("" = Claude Code's default)
    "manager_codex_effort": "low",      # model_reasoning_effort for Codex as manager ("" = Codex's default)
    "manager_max_turns": 15,            # cap on a Claude manager run's steps
    "review_each": False,               # manager reviews every finished task (more usage), not only problems
}


def other(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def get_settings() -> dict:
    return {**DEFAULTS, **(kv_get("team_settings") or {})}


def save_settings(**fields) -> dict:
    current = get_settings()
    for key in ("five_hour", "weekly"):
        if fields.get(key) is not None:
            current[key] = max(1, min(100, int(fields[key])))
    if fields.get("default") in AGENTS:
        current["default"] = fields["default"]
    if fields.get("manager_claude_model") is not None:
        current["manager_claude_model"] = str(fields["manager_claude_model"]).strip()[:60]
    if fields.get("manager_codex_effort") in ("", "minimal", "low", "medium", "high"):
        current["manager_codex_effort"] = fields["manager_codex_effort"]
    if fields.get("review_each") is not None:
        current["review_each"] = bool(fields["review_each"])
    if fields.get("manager_max_turns") is not None:
        current["manager_max_turns"] = max(3, min(100, int(fields["manager_max_turns"])))
    kv_set("team_settings", current)
    return current


def record_claude_limits(info: dict) -> None:
    """Saves a rate_limit_event's rate_limit_info from a Claude run."""
    windows = info.get("unifiedWindows") or {}
    out = {"at": time.time()}
    for key, name in (("five_hour", "five_hour"), ("seven_day", "weekly")):
        w = windows.get(key) or {}
        if w.get("utilization") is not None:
            out[name] = round(float(w["utilization"]) * 100, 1)
            out[name + "_resets"] = w.get("resetsAt")
    if info.get("status") == "rejected":
        block("claude", info.get("resetsAt"))
    kv_set("team_claude_limits", out)


def _claude_usage() -> dict:
    from ..usage import parse_time, poller
    best: dict = {}
    polled = poller.usage or {}
    fetched = parse_time(polled.get("fetched_at"))
    if fetched:
        best = {"at": fetched.timestamp()}
        for lim in polled.get("limits") or []:
            name = {"five_hour": "five_hour", "seven_day": "weekly"}.get(lim.get("key"))
            if name:
                best[name] = lim.get("utilization")
                resets = parse_time(lim.get("resets_at"))
                best[name + "_resets"] = resets.timestamp() if resets else None
    reported = kv_get("team_claude_limits") or {}
    if reported.get("at", 0) > best.get("at", 0):
        best = reported
    return best


def _codex_usage() -> dict:
    from ..codex_sessions import codex_index
    from ..codex_usage import codex_poller, newest
    u = newest(codex_poller.usage, codex_index.usage()) or {}
    out = {"at": u.get("as_of")}
    for w in u.get("windows") or []:
        minutes = w.get("window_minutes") or 0
        name = "five_hour" if minutes and minutes <= 600 else "weekly"
        out[name] = w.get("used_percent")
        out[name + "_resets"] = w.get("resets_at")
    return out


def linked(agent: str) -> bool:
    if agent == "claude":
        from ..providers.claude import _credentials
        return bool(shutil.which(settings.claude_cmd)) and bool(_credentials())
    from ..codex_usage import _auth
    from ..providers import codex
    return bool(codex.binary()) and bool(_auth())


def block(agent: str, until: float | None) -> None:
    """Marks an agent as out of usage until `until` (epoch seconds; an hour from now if unknown)."""
    blocked = kv_get("team_blocked") or {}
    blocked[agent] = float(until) if until else time.time() + 3600
    kv_set("team_blocked", blocked)


def blocked_until(agent: str) -> float | None:
    until = (kv_get("team_blocked") or {}).get(agent)
    return until if until and until > time.time() else None


def status() -> dict:
    """Both plans: linked, usage, blocked, and whether each is under the switch point."""
    s = get_settings()
    out = {}
    for agent, u in (("claude", _claude_usage()), ("codex", _codex_usage())):
        five, week = u.get("five_hour"), u.get("weekly")
        out[agent] = {
            "linked": linked(agent), "five_hour": five, "weekly": week,
            "five_hour_resets": u.get("five_hour_resets"), "weekly_resets": u.get("weekly_resets"), "as_of": u.get("at"),
            "blocked_until": blocked_until(agent),
            "under": (five or 0) < s["five_hour"] and (week or 0) < s["weekly"],
            "room": 100 - max(five or 0, week or 0),
        }
    return {"agents": out, "settings": s}


def unusable(agent: str, agents: dict | None = None) -> str | None:
    """None if the agent can run now, else why not."""
    a = (agents or status()["agents"])[agent]
    if not a["linked"]:
        return f"{LABEL[agent]} isn't signed in"
    if a["blocked_until"]:
        return f"{LABEL[agent]} is out of usage until {time.strftime('%H:%M', time.localtime(a['blocked_until']))}"
    return None


def pick(wanted: str, current: str | None = None) -> tuple[str | None, str]:
    """(agent, reason) for a role. wanted is 'claude', 'codex' or 'any'; current is who had it last."""
    st = status()
    agents, s = st["agents"], st["settings"]
    if wanted in AGENTS:
        why = unusable(wanted, agents)
        if why is None:
            return wanted, ""
        if unusable(other(wanted), agents) is None:
            return other(wanted), f"{why}, so {LABEL[other(wanted)]} takes it"
        return None, why
    ready = [a for a in AGENTS if unusable(a, agents) is None]
    if not ready:
        return None, " and ".join(unusable(a, agents) for a in AGENTS)
    under = [a for a in ready if agents[a]["under"]]
    if current in under:
        return current, ""
    if under:
        best = max(under, key=lambda a: (agents[a]["room"], a == s["default"]))
        return best, f"{LABEL[best]} has more room" if len(ready) == 2 else ""
    best = max(ready, key=lambda a: (agents[a]["room"], a == s["default"]))
    return best, f"Both plans are past the switch point; {LABEL[best]} has more room"
