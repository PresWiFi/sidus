"""The Team goal lifecycle.

planning ──(manager proposes phases + phase 1's tasks)──> proposed ──(you approve)──> running ──> done
    └─(manager asks you questions)─> questions ──(you answer)─> planning

While running, the current phase's tasks go one at a time, in order. A worker that finishes marks its task done
(with "review_each" on, it goes to the manager instead). A worker that fails or says it's blocked sends the task
to the manager, who sends it back with notes, approves it, or skips it. When a phase's tasks are all done, the
manager checks the phase and, in the same run, plans the next phase's tasks. After the last phase, the goal is done.

To keep usage down, the manager runs once to outline, once per phase change, and only otherwise when a task needs
a decision. It uses a smaller model (settings), read-only tools, and a cap on its steps. Long goals and runbooks
stay in files that agents read when they need them; prompts carry only the current phase and task.

Only one run (manager or worker) at a time per folder. Questions between agents (ask_*) run alongside.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..store import all_meta, set_meta
from . import db, parse, procs, usage

LABEL = usage.LABEL
MAX_ATTEMPTS = 3
REPORT_CHARS = 1500
INLINE_GOAL = 4000   # a goal longer than this is only referred to by its file
APPROVED = "Phases approved."


class Refused(Exception):
    """An action that isn't possible in the goal's current state."""


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def goal_file(goal: dict) -> Path:
    return procs.log_dir(goal["id"]) / "goal.md"


def _goal_text(goal: dict, short: bool = False) -> str:
    """The goal for a prompt: inline when it's short, else a pointer to its file."""
    lines = [f"Goal: {goal['title']}"]
    if short:
        lines.append(f"The full goal is in {goal_file(goal)}" + (f"; the runbook is {goal['runbook']}" if goal["runbook"]
                                                                 else "") + ". Read only the part you need.")
    elif len(goal["goal"]) <= INLINE_GOAL:
        lines.append(goal["goal"])
    else:
        lines.append(f"It's long, so it's in {goal_file(goal)}: read it.")
    if goal["runbook"] and not short:
        lines.append(f"Runbook: {goal['runbook']} (in the folder). Follow it.")
    return "\n".join(lines)


def _thread(goal_id: str, task_id: str | None = None, goal_level: bool = False, limit: int = 12) -> str:
    items = db.comments(goal_id, task_id, goal_level=goal_level)[-limit:]
    who = lambda c: "Manager" if c["author"] == "manager" else LABEL.get(c["author"], c["author"].capitalize())
    return "\n".join(f"- {who(c)}: {_clip(c['text'], 800)}" for c in items if c["text"] != APPROVED)


def _phase_label(goal: dict, phase: dict) -> str:
    return f"Phase {phase['position']} of {len(db.phases(goal['id']))}: {phase['title']}"


def _phase_history(goal: dict, before: int) -> str:
    done = [p for p in db.phases(goal["id"]) if p["position"] < before and p["status"] in ("done", "skipped")]
    return "\n".join(f"- {p['title']}: {_clip(p['summary'] or p['status'], 300)}" for p in done[-6:])


def _task_reports(phase: dict) -> str:
    return "\n".join(f"- {t['title']} ({LABEL.get(t['worker'] or '', '-')}, {t['status']}): {_clip(t['report'], 400)}"
                     + (f" Files: {', '.join(t['files'][:10])}" if t["files"] else "")
                     for t in db.tasks(phase["goal_id"], phase["id"]))


# ---------- prompts ----------

TASK_RULES = ("Each task: title; details (a few sentences: what to do, what to watch for); done_when (something "
              "checkable); assignee (claude, codex or any; use any unless one clearly fits better). Each task "
              "should fit one focused session; fewer, larger tasks use less.")


def outline_prompt(goal: dict) -> str:
    notes = _thread(goal["id"], goal_level=True)
    return f"""You manage a small team in Sidus: you plan, and Claude Code and Codex do the work. Keep your own \
usage small: don't write code, and read only what you need to plan.

Folder: {goal['cwd']}
{_goal_text(goal)}
{("Notes so far:" + chr(10) + notes) if notes else ""}

1. Glance at the project's context file (per your instructions) and the code, only as much as planning needs.
2. If something unclear would change the plan, call ask_user with up to 3 short questions and stop. Otherwise don't ask.
3. Call propose_phases with 1-8 phases in order. Each: title; details (what it covers; for a runbook, which \
section or steps); done_when (something checkable). Fewer, larger phases use less. Include first_tasks: the tasks \
for phase 1. {TASK_RULES}

Then end with one line."""


def answers_prompt(answers: str) -> str:
    return f"""The user answered:
{answers}

Now call propose_phases (or ask_user again only if it's essential). Then end with one line."""


def phase_prompt(goal: dict, phase: dict) -> str:
    history = _phase_history(goal, phase["position"])
    return f"""Plan the tasks for the next phase of this goal. Keep it brief; read the code only as much as you need.
Folder: {goal['cwd']}
{_goal_text(goal, short=True)}
{("Earlier phases:" + chr(10) + history) if history else ""}

{_phase_label(goal, phase)}
{phase['details']}
Done when: {phase['done_when'] or '-'}

Call propose_tasks with 1-6 tasks. {TASK_RULES} Then end with one line."""


def wrapup_prompt(goal: dict, phase: dict) -> str:
    following = next((p for p in db.phases(goal["id"]) if p["position"] > phase["position"]
                      and p["status"] == "todo"), None)
    if following:
        nxt = (f"complete_phase with a 1-3 line summary and next_tasks: the tasks for the next phase, "
               f"\"{following['title']}\" ({_clip(following['details'], 600)}; done when: "
               f"{following['done_when'] or '-'}). {TASK_RULES}")
    else:
        nxt = "complete_phase with a 2-4 line summary for the user: this is the last phase, so it finishes the goal."
    return f"""{_phase_label(goal, phase)} has no tasks left.
Done when: {phase['done_when'] or '-'}
Task reports:
{_task_reports(phase)}

Check the phase's "done when", looking at the code only as much as you need. Then call either:
- {nxt}
- or create_task for what's missing in this phase.
End with one line."""


def work_prompt(goal: dict, task: dict, agent: str) -> str:
    phase = db.get_phase(task["phase_id"])
    siblings = db.tasks(goal["id"], phase["id"])
    i = next(k for k, t in enumerate(siblings, start=1) if t["id"] == task["id"])
    earlier = [f"- {t['title']} ({LABEL.get(t['worker'] or '', '?')}): {_clip(t['report'], 300)}"
               + (f" Files: {', '.join(t['files'][:10])}" if t["files"] else "")
               for t in siblings if t["status"] == "done" and t["position"] < task["position"]]
    history = _phase_history(goal, phase["position"])
    thread = _thread(goal["id"], task["id"])
    return f"""You're on a small team in Sidus, doing one task. {LABEL[goal['manager'] or 'claude']} is the \
manager; your teammate is {LABEL[usage.other(agent)]}.
Folder: {goal['cwd']}
{_goal_text(goal, short=True)}
{("Earlier phases:" + chr(10) + history) if history else ""}
{_phase_label(goal, phase)}. {_clip(phase['details'], 800)}

Your task ({i} of {len(siblings)} in this phase): {task['title']}
{task['details']}
Done when: {task['done_when'] or 'the task is complete'}
{("Earlier tasks in this phase:" + chr(10) + chr(10).join(earlier[-6:])) if earlier else ""}
{("Notes on this task:" + chr(10) + thread) if thread else ""}

Do the task, then check it against "done when". If you're stuck, ask_{usage.other(agent)} can help. Follow your \
instructions about context files. If you can't finish, call task_blocked with the reason. End with a short report: \
what you did, how you checked it, and anything left or risky."""


def revise_prompt(notes: str) -> str:
    return f"""The manager sent this task back:
{notes}

Fix it, check it against "done when", and end with a short report (or call task_blocked)."""


def review_prompt(goal: dict, task: dict) -> str:
    phase = db.get_phase(task["phase_id"])
    run = next((r for r in reversed(db.runs(goal["id"])) if r["task_id"] == task["id"] and r["role"] == "work"), None)
    state = ("stopped with an error" if run and run["status"] == "failed" else
             "said it was blocked" if run and run["verdict"] == "blocked" else "finished it")
    return f"""Task "{task['title']}" ({_phase_label(goal, phase)}) needs your decision. \
{LABEL.get(task['worker'] or '', 'The worker')} {state} (attempt {task['attempts']} of {MAX_ATTEMPTS}).
Done when: {task['done_when'] or '-'}
Report: {_clip(task['report'] or (run or {}).get('error', ''), REPORT_CHARS)}
Files changed: {', '.join(task['files'][:20]) or 'none'}

Decide briefly, looking at the code only if you must: approve_task if it's done, revise_task with short notes (and \
an assignee) to try again, or skip_task if it isn't needed. Add create_task for follow-ups only if essential. Then \
end with one line."""


# ---------- starting runs ----------

_lock = threading.RLock()
_stopping: set[str] = set()


def start_goal(title: str, cwd: str, goal: str, runbook: str | None = None) -> dict:
    g = db.create_goal(title, cwd, goal, runbook)
    goal_file(g).write_text(goal + "\n")
    kick(g["id"])
    return db.get_goal(g["id"])


def _folder_busy(goal: dict) -> bool:
    for other in db.list_goals():
        if other["cwd"] == goal["cwd"] and other["id"] != goal["id"] and db.running_run(other["id"]):
            return True
    return False


def _manager(goal: dict) -> tuple[str | None, str]:
    agent, why = usage.pick(goal["manager"] or "any")
    if agent and goal["manager"] and agent != goal["manager"]:
        db.comment(goal["id"], "sidus", f"{LABEL[agent]} took over as manager: {why}.")
    if agent and agent != goal["manager"]:
        db.update_goal(goal["id"], manager=agent)
    return agent, why


def kick(goal_id: str) -> None:
    """Starts the goal's next run, if it has one to start and nothing is running."""
    with _lock:
        goal = db.get_goal(goal_id)
        if not goal or goal["status"] not in ("planning", "running") or db.running_run(goal_id):
            return
        if _folder_busy(goal):
            return _wait(goal, "Another goal in this folder is running")
        try:
            _next(goal)
        except (OSError, RuntimeError) as e:
            db.update_goal(goal_id, status="paused", waiting=str(e))


def _wait(goal: dict, why: str | None) -> None:
    if goal["waiting"] != why:
        db.update_goal(goal["id"], waiting=why)


def _pause(goal: dict, why: str) -> None:
    db.update_goal(goal["id"], status="paused", waiting=why)


def _manager_run(goal: dict, role: str, prompt_fn, **kw) -> None:
    agent, why = _manager(goal)
    if not agent:
        return _wait(goal, why)
    goal = db.get_goal(goal["id"])
    _start(goal, role, agent, prompt_fn(goal), session=goal.get(f"{agent}_session"), reason=why, **kw)


def _next(goal: dict) -> None:
    if goal["status"] == "planning":
        agent, why = _manager(goal)
        if not agent:
            return _wait(goal, why)
        goal = db.get_goal(goal["id"])
        answers = [c for c in db.comments(goal["id"], goal_level=True) if c["author"] == "you"]
        session = goal.get(f"{agent}_session")
        prompt = answers_prompt(answers[-1]["text"]) if session and answers else outline_prompt(goal)
        return _start(goal, "outline", agent, prompt, session=session, reason=why)

    phase = db.current_phase(goal["id"])
    if not phase:
        db.update_goal(goal["id"], status="done", waiting=None)
        return
    if phase["status"] == "todo":
        phase = db.update_phase(phase["id"], status="active")
    tasks = db.tasks(goal["id"], phase["id"])
    review = next((t for t in tasks if t["status"] == "review"), None)
    if review:
        return _manager_run(goal, "review", lambda g: review_prompt(g, review), phase=phase, task=review)
    todo = next((t for t in tasks if t["status"] == "todo"), None)
    if todo:
        last = next((r["agent"] for r in reversed(db.runs(goal["id"])) if r["role"] == "work"), None)
        agent, why = usage.pick(todo["assignee"], current=last)
        if not agent:
            return _wait(goal, why)
        session = todo.get(f"{agent}_session")
        notes = [c for c in db.comments(goal["id"], todo["id"]) if c["author"] in ("manager", "you")]
        prompt = revise_prompt(notes[-1]["text"]) if session and notes else work_prompt(goal, todo, agent)
        return _start(goal, "work", agent, prompt, session=session, phase=phase, task=todo, reason=why)
    if any(t["status"] == "failed" for t in tasks):
        return _pause(goal, "A task failed; retry or skip it to carry on")
    if not tasks:
        tried = [r for r in db.runs(goal["id"]) if r["phase_id"] == phase["id"] and r["role"] == "phase"
                 and r["status"] == "done"]
        if tried:
            return _pause(goal, f"The manager didn't plan tasks for \"{phase['title']}\"; add tasks or resume")
        return _manager_run(goal, "phase", lambda g: phase_prompt(g, phase), phase=phase)
    _manager_run(goal, "wrapup", lambda g: wrapup_prompt(g, phase), phase=phase)


def _start(goal: dict, role: str, agent: str, prompt: str, *, session: str | None = None, phase: dict | None = None,
           task: dict | None = None, reason: str = "") -> None:
    if goal["waiting"]:
        db.update_goal(goal["id"], waiting=None)
    run = db.add_run(goal["id"], role, agent, phase_id=phase["id"] if phase else None,
                     task_id=task["id"] if task else None, reason=reason)
    manager = role in ("outline", "phase", "wrapup", "review")
    mcp = procs.mcp_env(run["id"], agent, role)
    extra = [str(procs.log_dir(goal["id"]))]   # where goal.md is, outside the folder
    if agent == "claude":
        args = procs.claude_cmd(prompt, session=session, new_session=None if session else str(uuid.uuid4()), mcp=mcp,
                                manager=manager, add_dirs=extra)
    else:
        args = procs.codex_cmd(prompt, session=session, mcp=mcp, manager=manager)
    log = procs.log_dir(goal["id"]) / f"{int(time.time())}-{role}-{agent}-{run['id']}.jsonl"
    before = procs.git_state(goal["cwd"]) if role == "work" else None
    try:
        proc = procs.spawn(args, goal["cwd"], agent, log)
    except OSError as e:
        db.update_run(run["id"], status="failed", error=str(e), ended=time.time())
        raise RuntimeError(f"Couldn't start {LABEL[agent]}: {e}") from None
    db.update_run(run["id"], log=str(log), pid=proc.pid, pid_started=procs.started_at(proc.pid),
                  git_before=json.dumps(before) if before else None)
    if task and role == "work":
        db.update_task(task["id"], status="working", worker=agent, attempts=task["attempts"] + 1)
    threading.Thread(target=_watch, args=(run["id"], proc), daemon=True, name=f"team-{run['id']}").start()


def _watch(run_id: str, proc: subprocess.Popen | None) -> None:
    if proc is not None:
        proc.wait()
    else:  # picked up after a restart: not our child, so poll
        while procs.alive(db.get_run(run_id) or {}):
            time.sleep(2)
    try:
        finalize(run_id)
    except Exception as e:
        print(f"Team: couldn't finish run {run_id}: {e}", flush=True)


# ---------- finishing runs ----------

def _tag(session_id: str | None, name: str) -> None:
    if session_id and not (all_meta().get(session_id) or {}).get("name"):
        set_meta(session_id, name=name)


def finalize(run_id: str) -> None:
    with _lock:
        run = db.get_run(run_id)
        if not run or run["status"] != "running":
            return
        parsed = parse.read(run["agent"], run["log"] or "")
        if run["agent"] == "claude" and parsed["rate_limit"]:
            usage.record_claude_limits(parsed["rate_limit"])
        stopped = run_id in _stopping
        _stopping.discard(run_id)
        error = parsed["error"]
        if stopped:
            status = "stopped"
        elif parsed["limit"]:
            status = "limit"
        elif parsed["finished"] and not error:
            status = "done"
        else:
            status = "failed"
            error = error or procs.stderr_tail(run["log"]) or f"{LABEL[run['agent']]} exited without finishing"
        goal = db.get_goal(run["goal_id"])
        files = parsed["files"]
        if run["role"] == "work" and run["git_before"] and goal:
            # Catches files written by shell commands too, not only edit tools.
            files = sorted(set(files) | set(procs.git_changed(goal["cwd"], json.loads(run["git_before"]))))
        run = db.update_run(run_id, status=status, session_id=parsed["session_id"], result=parsed["text"],
                            error="" if status == "done" else error, steps=parsed["steps"], files=files,
                            tokens=parsed["tokens"], ended=time.time(), pid=None)
        if status == "limit":
            usage.block(run["agent"], parsed["limit_resets"])
        if goal is None or run["role"] == "ask":
            return
        _tag(parsed["session_id"], f"Team · {goal['title']} · {'task' if run['role'] == 'work' else 'manager'}")
        if parsed["session_id"]:
            if run["role"] == "work":
                db.update_task(run["task_id"], **{f"{run['agent']}_session": parsed["session_id"]})
            else:
                db.update_goal(goal["id"], **{f"{run['agent']}_session": parsed["session_id"]})
        if status == "stopped":
            if run["task_id"]:
                db.update_task(run["task_id"], status="todo")
            _pause(goal, "Stopped")
        else:
            {"outline": _after_outline, "phase": _after_phase, "review": _after_review, "wrapup": _after_wrapup,
             "work": _after_work}[run["role"]](db.get_goal(goal["id"]), run, parsed)
    kick_all()


def _after_outline(goal: dict, run: dict, parsed: dict) -> None:
    if goal["status"] != "planning" or run["status"] == "limit":
        return  # a limit: the next kick picks the other agent as manager
    if goal["questions"]:
        db.update_goal(goal["id"], status="questions")
    elif db.phases(goal["id"]):
        db.update_goal(goal["id"], status="proposed", summary=parsed["text"] or goal["summary"])
    else:
        _pause(goal, f"The manager didn't propose phases{': ' + run['error'] if run['error'] else ''}")


def _after_phase(goal: dict, run: dict, parsed: dict) -> None:
    if run["status"] == "failed":
        _pause(goal, f"The manager couldn't plan the phase: {run['error']}")


def _after_work(goal: dict, run: dict, parsed: dict) -> None:
    task = db.get_task(run["task_id"])
    if not task:
        return
    worker = LABEL[run["agent"]]
    if run["status"] == "limit":
        db.update_task(task["id"], status="todo", attempts=max(0, task["attempts"] - 1))
        done = "; ".join(parsed["steps"][-10:])
        db.comment(goal["id"], "sidus", f"{worker} hit its usage limit partway through."
                   + (f" What it had done: {done}." if done else "")
                   + (f" Files it changed: {', '.join(run['files'][:20])}." if run["files"] else "")
                   + " The other agent picks this up; check those changes first.", task_id=task["id"])
        return
    report = parsed["text"] or run["error"]
    files = sorted(set(task["files"]) | set(run["files"]))
    if report:
        db.comment(goal["id"], run["agent"], _clip(report, 4000), task_id=task["id"])
    if run["status"] == "done" and run["verdict"] != "blocked":
        db.update_task(task["id"], status="review" if usage.get_settings()["review_each"] else "done",
                       report=report, files=files)
    elif task["attempts"] >= MAX_ATTEMPTS:
        db.update_task(task["id"], status="failed", report=report, files=files)
    else:
        db.update_task(task["id"], status="review", report=report, files=files)


def _after_review(goal: dict, run: dict, parsed: dict) -> None:
    task = db.get_task(run["task_id"])
    if task and task["status"] == "review" and run["status"] in ("done", "failed"):
        # The manager ended without deciding: don't loop, let the user choose.
        _pause(goal, f"The manager didn't decide on \"{task['title']}\"")


def _after_wrapup(goal: dict, run: dict, parsed: dict) -> None:
    phase = db.get_phase(run["phase_id"]) if run["phase_id"] else None
    if not phase or phase["status"] != "active" or run["status"] == "limit":
        return
    if run["status"] == "failed":
        return _pause(goal, f"The manager couldn't check \"{phase['title']}\": {run['error']}")
    if not any(t["status"] in ("todo", "review") for t in db.tasks(goal["id"], phase["id"])):
        # Ended without completing the phase or adding work: carry on, but say so.
        db.update_phase(phase["id"], status="done", summary=_clip(parsed["text"], 600))
        db.comment(goal["id"], "sidus", f"The manager didn't confirm \"{phase['title']}\" was done; moving on.")
        if not db.current_phase(goal["id"]):
            db.update_goal(goal["id"], status="done", summary=parsed["text"] or goal["summary"])


# ---------- your actions ----------

def _approved(goal_id: str) -> bool:
    return any(c["author"] == "sidus" and c["text"] == APPROVED for c in db.comments(goal_id, goal_level=True))


def approve(goal_id: str) -> dict:
    goal = db.get_goal(goal_id)
    if goal["status"] != "proposed" or not db.phases(goal_id):
        raise Refused("Only proposed phases can be approved")
    db.update_goal(goal_id, status="running", waiting=None)
    db.comment(goal_id, "sidus", APPROVED)
    kick(goal_id)
    return db.get_goal(goal_id)


def answer(goal_id: str, text: str) -> dict:
    if db.get_goal(goal_id)["status"] != "questions":
        raise Refused("The manager isn't waiting for answers")
    db.comment(goal_id, "you", text)
    db.update_goal(goal_id, status="planning", questions="")
    kick(goal_id)
    return db.get_goal(goal_id)


def replan(goal_id: str, note: str) -> dict:
    goal = db.get_goal(goal_id)
    if goal["status"] not in ("proposed", "paused") or db.running_run(goal_id) or _approved(goal_id):
        raise Refused("Only phases that haven't been approved can be re-planned")
    db.clear_phases(goal_id)
    if note:
        db.comment(goal_id, "you", note)
    db.update_goal(goal_id, status="planning", waiting=None, questions="")
    kick(goal_id)
    return db.get_goal(goal_id)


def pause(goal_id: str) -> dict:
    if db.get_goal(goal_id)["status"] in ("planning", "running"):
        db.update_goal(goal_id, status="paused", waiting="Paused by you")
    return db.get_goal(goal_id)


def resume(goal_id: str) -> dict:
    goal = db.get_goal(goal_id)
    if goal["status"] != "paused":
        raise Refused("The goal isn't paused")
    status = "running" if _approved(goal_id) else ("proposed" if db.phases(goal_id) else "planning")
    db.update_goal(goal_id, status=status, waiting=None)
    if status == "running":
        # Give a phase with no tasks another planning attempt.
        phase = db.current_phase(goal_id)
        if phase and not db.tasks(goal_id, phase["id"]):
            for r in db.runs(goal_id):
                if r["phase_id"] == phase["id"] and r["role"] == "phase" and r["status"] == "done":
                    db.update_run(r["id"], status="retried")
    kick(goal_id)
    return db.get_goal(goal_id)


def stop(goal_id: str) -> bool:
    run = db.running_run(goal_id)
    if not run or not run["pid"] or not procs.alive(run):
        return False
    _stopping.add(run["id"])
    try:
        os.killpg(run["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    return True


def cancel(goal_id: str) -> dict:
    stop(goal_id)
    db.update_goal(goal_id, status="cancelled", waiting=None)
    return db.get_goal(goal_id)


# ---------- tools the agents call (through the MCP server) ----------

def _tool_run(run_id: str, agent: str, roles: tuple[str, ...]) -> tuple[dict, dict]:
    run = db.get_run(run_id)
    if not run or run["status"] != "running" or run["agent"] != agent or run["role"] not in roles:
        raise Refused("That tool isn't available in this run.")
    return run, db.get_goal(run["goal_id"])


def _add_tasks(phase_id: str, tasks: list[dict]) -> int:
    for t in tasks[:12]:
        db.add_task(phase_id, t["title"], t.get("details", ""), t.get("done_when", ""), t.get("assignee", "any"))
    return len(tasks[:12])


def tool_propose_phases(run_id: str, agent: str, phases: list[dict], first_tasks: list[dict]) -> str:
    _, goal = _tool_run(run_id, agent, ("outline",))
    db.clear_phases(goal["id"])
    created = [db.add_phase(goal["id"], p["title"], p.get("details", ""), p.get("done_when", "")) for p in phases[:8]]
    n = _add_tasks(created[0]["id"], first_tasks) if created else 0
    return (f"Proposed {len(created)} phases and {n} tasks for phase 1. The user will review them. End your turn "
            "with one line.")


def tool_propose_tasks(run_id: str, agent: str, tasks: list[dict]) -> str:
    run, _ = _tool_run(run_id, agent, ("phase",))
    db.clear_tasks(run["phase_id"])
    return f"Planned {_add_tasks(run['phase_id'], tasks)} tasks; they start when your turn ends. End with one line."


def tool_ask_user(run_id: str, agent: str, questions: list[str]) -> str:
    _, goal = _tool_run(run_id, agent, ("outline",))
    text = "\n".join(f"{i}. {q}" for i, q in enumerate(questions[:3], start=1))
    db.update_goal(goal["id"], questions=text)
    db.comment(goal["id"], "manager", text, agent=agent)
    return "Sent to the user. End your turn now; you'll get the answers in your next turn."


def tool_comment(run_id: str, agent: str, text: str) -> str:
    run, goal = _tool_run(run_id, agent, ("outline", "phase", "wrapup", "review", "work"))
    db.comment(goal["id"], agent if run["role"] == "work" else "manager", text, task_id=run["task_id"], agent=agent)
    return "Comment added."


def tool_blocked(run_id: str, agent: str, reason: str) -> str:
    run, goal = _tool_run(run_id, agent, ("work",))
    db.update_run(run_id, verdict="blocked")
    db.comment(goal["id"], agent, f"Blocked: {reason}", task_id=run["task_id"])
    return "The manager will look at it. End your turn with a short report of what you did."


def _review_task(run_id: str, agent: str) -> tuple[dict, dict, dict]:
    run, goal = _tool_run(run_id, agent, ("review",))
    return run, goal, db.get_task(run["task_id"])


def tool_approve(run_id: str, agent: str, notes: str) -> str:
    _, goal, task = _review_task(run_id, agent)
    db.update_run(run_id, verdict="approved")
    db.update_task(task["id"], status="done")
    if notes:
        db.comment(goal["id"], "manager", notes, task_id=task["id"], agent=agent)
    return "Task marked done."


def tool_revise(run_id: str, agent: str, notes: str, assignee: str) -> str:
    _, goal, task = _review_task(run_id, agent)
    db.update_run(run_id, verdict="revise")
    db.update_task(task["id"], status="todo", assignee=assignee if assignee in db.ASSIGNEES else task["assignee"])
    db.comment(goal["id"], "manager", notes, task_id=task["id"], agent=agent)
    return "Sent back. End your turn with one line."


def tool_skip(run_id: str, agent: str, reason: str) -> str:
    _, goal, task = _review_task(run_id, agent)
    db.update_run(run_id, verdict="skipped")
    db.update_task(task["id"], status="skipped")
    db.comment(goal["id"], "manager", f"Skipped: {reason}", task_id=task["id"], agent=agent)
    return "Task skipped."


def tool_create(run_id: str, agent: str, title: str, details: str, done_when: str, assignee: str) -> str:
    run, goal = _tool_run(run_id, agent, ("review", "wrapup"))
    phase_id = run["phase_id"] or (db.current_phase(goal["id"]) or {}).get("id")
    if not phase_id or len(db.tasks(goal["id"], phase_id)) >= 20:
        return "Can't add more tasks to this phase."
    db.add_task(phase_id, title, details, done_when, assignee)
    return "Task added at the end of this phase."


def tool_complete_phase(run_id: str, agent: str, summary: str, next_tasks: list[dict]) -> str:
    run, goal = _tool_run(run_id, agent, ("wrapup",))
    phase = db.update_phase(run["phase_id"], status="done", summary=_clip(summary, 2000))
    following = db.current_phase(goal["id"])
    if not following:
        db.update_goal(goal["id"], status="done", summary=summary)
        return "The goal is done. End your turn with one line."
    n = _add_tasks(following["id"], next_tasks) if next_tasks and not db.tasks(goal["id"], following["id"]) else 0
    return (f"\"{phase['title']}\" is done. " + (f"Planned {n} tasks for \"{following['title']}\". " if n else "")
            + "End your turn with one line.")


def tool_ask(run_id: str, agent: str, question: str) -> str:
    """Runs the other agent on a question, without changing files, and returns its answer."""
    run, goal = _tool_run(run_id, agent, ("work",))
    partner = usage.other(agent)
    why = usage.unusable(partner)
    if why:
        return f"{why}, so it can't answer."
    task = db.get_task(run["task_id"])
    prompt = (f"{LABEL[agent]} is working on this task in this folder and asks you the question below. Answer it "
              f"briefly. Don't change any files.\n\nTask: {task['title']}\n{_clip(task['details'], 800)}\n\n"
              f"Question: {question}")
    ask = db.add_run(goal["id"], "ask", partner, phase_id=run["phase_id"], task_id=task["id"],
                     reason=f"Question from {LABEL[agent]}")
    log = procs.log_dir(goal["id"]) / f"{int(time.time())}-ask-{partner}-{ask['id']}.jsonl"
    args = procs.claude_cmd(prompt, read_only=True) if partner == "claude" else procs.codex_cmd(prompt)
    proc = procs.spawn(args, goal["cwd"], partner, log)
    db.update_run(ask["id"], log=str(log), pid=proc.pid, pid_started=procs.started_at(proc.pid))
    try:
        proc.wait(timeout=procs.ASK_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait()
    finalize(ask["id"])
    done = db.get_run(ask["id"])
    db.comment(goal["id"], agent, f"Asked {LABEL[partner]}: {_clip(question, 500)}", task_id=task["id"])
    if done["result"]:
        db.comment(goal["id"], partner, _clip(done["result"], 2000), task_id=task["id"])
        return done["result"]
    return f"{LABEL[partner]} couldn't answer: {done['error'] or 'no reply'}"


# ---------- start-up ----------

def kick_all() -> None:
    for goal_id in db.goals_to_kick():
        try:
            kick(goal_id)
        except Exception as e:
            print(f"Team: {e}", flush=True)


def recover() -> None:
    """On start: re-attach to runs still going from before a restart, finish the rest, start what's next."""
    for run in db.runs_with_status("running"):
        if procs.alive(run):
            threading.Thread(target=_watch, args=(run["id"], None), daemon=True).start()
        else:
            finalize(run["id"])
    kick_all()


def ticker() -> None:
    """Starts work that was waiting for usage to free up."""
    while True:
        time.sleep(30)
        kick_all()
