"""Which agent takes the next turn.

Usage for each plan comes from the freshest of: what Claude Code reported on Relay's last Claude run
(rate_limit_event), Sidus's Claude usage poller, and Sidus's ChatGPT usage poller / Codex's logs.
Percentages are 0-100.

Rules, in order:
1. A turn that's handed over or retried goes to the agent it names.
2. A pinned thread uses its pinned agent.
3. An agent that's signed out, or blocked after hitting its limit, can't take turns.
4. An agent is "under" while its 5-hour and weekly use are below the thresholds. Stay with the agent that took
   the thread's last turn while it's under (handing over costs a catch-up); otherwise take one that's under,
   the one with more room first.
5. If neither is under, take the one with more room (the thresholds are where to switch, not hard stops).
"""
from __future__ import annotations

import shutil
import time

from ..config import settings
from ..store import kv_get, kv_set
from . import db

LABEL = {"claude": "Claude", "codex": "Codex"}
DEFAULTS = {"five_hour": 80, "weekly": 90, "default": "claude"}


def other(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def get_settings() -> dict:
    return {**DEFAULTS, **(kv_get("relay_settings") or {})}


def save_settings(**fields) -> dict:
    current = get_settings()
    for key in ("five_hour", "weekly"):
        if key in fields and fields[key] is not None:
            current[key] = max(1, min(100, int(fields[key])))
    if fields.get("default") in db.AGENTS:
        current["default"] = fields["default"]
    kv_set("relay_settings", current)
    return current


# ---------- usage ----------

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
    kv_set("relay_claude_limits", out)


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
    reported = kv_get("relay_claude_limits") or {}
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
    blocked = kv_get("relay_blocked") or {}
    blocked[agent] = float(until) if until else time.time() + 3600
    kv_set("relay_blocked", blocked)


def blocked_until(agent: str) -> float | None:
    until = (kv_get("relay_blocked") or {}).get(agent)
    return until if until and until > time.time() else None


def status() -> dict:
    """Both plans: linked, usage, blocked, and whether each is under the thresholds."""
    s = get_settings()
    out = {}
    for agent, usage in (("claude", _claude_usage()), ("codex", _codex_usage())):
        five, week = usage.get("five_hour"), usage.get("weekly")
        out[agent] = {
            "linked": linked(agent),
            "five_hour": five, "weekly": week,
            "five_hour_resets": usage.get("five_hour_resets"), "weekly_resets": usage.get("weekly_resets"),
            "as_of": usage.get("at"),
            "blocked_until": blocked_until(agent),
            "under": (five or 0) < s["five_hour"] and (week or 0) < s["weekly"],
            "room": 100 - max(five or 0, week or 0),
        }
    return {"agents": out, "settings": s}


def _why_over(agent: str, a: dict, s: dict) -> str:
    if (a["five_hour"] or 0) >= s["five_hour"]:
        return f"{LABEL[agent]} is at {a['five_hour']:.0f}% of its 5-hour limit"
    return f"{LABEL[agent]} is at {a['weekly']:.0f}% of its weekly limit"


def choose(thread: dict, turn: dict, last_agent: str | None) -> tuple[str | None, str]:
    """(agent, reason), or (None, why nothing can start now)."""
    st = status()
    agents, s = st["agents"], st["settings"]

    def usable(agent: str) -> str | None:
        """None if the agent can take a turn now, else why not."""
        a = agents[agent]
        if not a["linked"]:
            return f"{LABEL[agent]} isn't signed in"
        if a["blocked_until"]:
            return f"{LABEL[agent]} is out of usage until {time.strftime('%H:%M', time.localtime(a['blocked_until']))}"
        return None

    forced = turn.get("force_agent")
    if forced:
        why = usable(forced)
        if why is None:
            return forced, {"handoff": f"Handed over by {LABEL[other(forced)]}",
                            "retry": f"{LABEL[other(forced)]} hit its usage limit"}.get(turn["kind"], "")
        # A hand-over to an agent that can't take it falls back to whoever can.
        if usable(other(forced)) is None:
            return other(forced), f"{why}, so {LABEL[other(forced)]} keeps it"
        return None, why

    pin = thread.get("pin")
    if pin in db.AGENTS:
        why = usable(pin)
        return (pin, f"Thread is pinned to {LABEL[pin]}") if why is None else (None, why)

    ready = [a for a in db.AGENTS if usable(a) is None]
    if not ready:
        return None, " and ".join(usable(a) for a in db.AGENTS)
    under = [a for a in ready if agents[a]["under"]]
    if last_agent in under:
        return last_agent, f"{LABEL[last_agent]} is still under the switch point"
    if under:
        pick = max(under, key=lambda a: (agents[a]["room"], a == s["default"]))
        if last_agent and last_agent != pick:
            if last_agent in ready:
                return pick, _why_over(last_agent, agents[last_agent], s)
            return pick, usable(last_agent) or ""
        return pick, "" if len(under) == 2 else f"{LABEL[pick]} has more room"
    pick = max(ready, key=lambda a: (agents[a]["room"], a == s["default"]))
    return pick, f"Both plans are past the switch point; {LABEL[pick]} has more room"
