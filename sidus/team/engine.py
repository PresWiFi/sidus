"""The Team plan lifecycle.

planning ──(manager proposes tasks)──> proposed ──(you approve)──> running ──(all done + wrap-up)──> done
    └─(manager asks you questions)─> questions ──(you answer)─> planning

While running, tasks go one at a time, in order. A worker that finishes marks its task done (or, with
"review_each" on, sends it to the manager for review). A worker that fails or says it's blocked sends the task to the
manager, who sends it back with notes, approves it, or skips it. When every task is done, the manager runs once more
to check the milestone and either marks it done in Ideas or adds tasks.

To keep usage down, the manager runs only to plan, to decide on a problem, and to wrap up; it uses a smaller model
(settings), read-only tools, and a cap on its steps. Prompts carry only what the run needs.

Only one run (manager or worker) at a time per project folder. Questions between agents (ask_*) run alongside.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid

from ..ideas import repo as ideas
from ..store import all_meta, set_meta
from . import db, parse, procs, usage

LABEL = usage.LABEL
MAX_ATTEMPTS = 3
REPORT_CHARS = 1500

_lock = threading.RLock()
_stopping: set[str] = set()


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _context(plan: dict) -> tuple[dict, dict]:
    project = ideas.get_project(plan["project_id"])
    milestone = next((m for m in project["milestones"] if m["id"] == plan["milestone_id"]), None)
    if milestone is None:
        raise ideas.NotFound("The milestone this plan is for was deleted")
    return project, milestone


def _goal(project: dict, milestone: dict, plan: dict) -> str:
    lines = [f"Project: {project['title']}" + (f": {_clip(project['summary'], 600)}" if project["summary"] else ""),
             f"Folder: {plan['cwd']}",
             f"Milestone: {milestone['title']}" + (f". Done when: {milestone['done_criteria']}"
                                                   if milestone["done_criteria"] else "")]
    if plan["request"]:
        lines.append(f"The user's request: {plan['request']}")
    return "\n".join(lines)


def _thread(plan_id: str, task_id: str | None = None, plan_level: bool = False, limit: int = 12) -> str:
    items = db.comments(plan_id, task_id, plan_level=plan_level)[-limit:]
    who = lambda c: "Manager" if c["author"] == "manager" else LABEL.get(c["author"], c["author"].capitalize())
    return "\n".join(f"- {who(c)}: {_clip(c['text'], 800)}" for c in items)


# ---------- prompts ----------

def plan_prompt(plan: dict) -> str:
    project, milestone = _context(plan)
    notes = _thread(plan["id"], plan_level=True)
    return f"""You manage a small team in Sidus: you plan, and Claude Code and Codex do the work. Keep your own \
usage small: don't write code, and read only what you need to plan.

{_goal(project, milestone, plan)}
{("Notes so far:" + chr(10) + notes) if notes else ""}

1. Glance at the project's context file (per your instructions) and the code, only as much as planning needs.
2. If something unclear would change the plan, call ask_user with up to 3 short questions and stop. Otherwise don't ask.
3. Call propose_tasks with 1-6 tasks in order. Each: title; details (a few sentences: what to do, what to watch \
for); done_when (something checkable); assignee (claude, codex or any; use any unless one clearly fits better). Each \
task should fit one focused session.

Then end with one line."""


def answers_prompt(answers: str) -> str:
    return f"""The user answered:
{answers}

Now call propose_tasks (or ask_user again only if it's essential). Then end with one line."""


def work_prompt(plan: dict, task: dict, agent: str) -> str:
    project, milestone = _context(plan)
    all_tasks = db.tasks(plan["id"])
    n, i = len(all_tasks), next(k for k, t in enumerate(all_tasks, start=1) if t["id"] == task["id"])
    earlier = [f"- {t['title']} ({LABEL.get(t['worker'] or '', '?')}): {_clip(t['report'], 300)}"
               + (f" Files: {', '.join(t['files'][:10])}" if t["files"] else "")
               for t in all_tasks if t["status"] == "done" and t["position"] < task["position"]]
    thread = _thread(plan["id"], task["id"])
    return f"""You're on a small team in Sidus, doing one task. {LABEL[plan['manager'] or 'claude']} is the \
manager; your teammate is {LABEL[usage.other(agent)]}.

{_goal(project, milestone, plan)}

Your task ({i} of {n}): {task['title']}
{task['details']}
Done when: {task['done_when'] or 'the task is complete'}
{("Earlier tasks:" + chr(10) + chr(10).join(earlier[-6:])) if earlier else ""}
{("Notes on this task:" + chr(10) + thread) if thread else ""}

Do the task, then check it against "done when". If you're stuck, ask_{usage.other(agent)} can help. Follow your \
instructions about context files. If you can't finish, call task_blocked with the reason. End with a short report: \
what you did, how you checked it, and anything left or risky."""


def revise_prompt(notes: str) -> str:
    return f"""The manager sent this task back:
{notes}

Fix it, check it against "done when", and end with a short report (or call task_blocked)."""


def review_prompt(plan: dict, task: dict) -> str:
    _, milestone = _context(plan)
    what = {"blocked": "said it was blocked", "failed": "stopped with an error", "review": "finished it"}
    run = next((r for r in reversed(db.runs(plan["id"])) if r["task_id"] == task["id"] and r["role"] == "work"), None)
    state = "failed" if run and run["status"] == "failed" else ("blocked" if run and run["verdict"] == "blocked"
                                                                 else "review")
    return f"""Task "{task['title']}" (milestone: {milestone['title']}) needs your decision. \
{LABEL.get(task['worker'] or '', 'The worker')} {what[state]} (attempt {task['attempts']} of {MAX_ATTEMPTS}).
Done when: {task['done_when'] or '-'}
Report: {_clip(task['report'] or (run or {}).get('error', ''), REPORT_CHARS)}
Files changed: {', '.join(task['files'][:20]) or 'none'}

Decide briefly, looking at the code only if you must: approve_task if it's done, revise_task with short notes (and \
an assignee) to try again, or skip_task if it isn't needed. Add create_task for follow-ups only if essential. Then \
end with one line."""


def wrapup_prompt(plan: dict) -> str:
    project, milestone = _context(plan)
    reports = "\n".join(f"- {t['title']} ({LABEL.get(t['worker'] or '', '-')}, {t['status']}): "
                        f"{_clip(t['report'], 400)}" for t in db.tasks(plan["id"]))
    return f"""Every task for this milestone is finished.
Project: {project['title']}
Milestone: {milestone['title']}. Done when: {milestone['done_criteria'] or '-'}
Reports:
{reports}

Check the milestone's "done when", looking at the code only as much as you need. Then call complete_milestone with \
a 2-4 line summary for the user, or create_task for what's missing. End with one line."""


# ---------- starting runs ----------

def start_plan(project_id: int, milestone_id: int, request: str = "") -> dict:
    project = ideas.get_project(project_id)
    if not project["repo_path"] or not os.path.isdir(project["repo_path"]):
        raise ideas.Invalid("Give the project a folder in Ideas first")
    if not any(m["id"] == milestone_id for m in project["milestones"]):
        raise ideas.NotFound("Milestone not found")
    if db.active_plan(milestone_id):
        raise ideas.Invalid("This milestone already has a plan")
    plan = db.create_plan(project_id, milestone_id, project["repo_path"], request)
    kick(plan["id"])
    return db.get_plan(plan["id"])


def _project_busy(plan: dict) -> bool:
    for other in db.list_plans():
        if other["project_id"] == plan["project_id"] and other["id"] != plan["id"] and db.running_run(other["id"]):
            return True
    return False


def _manager(plan: dict) -> tuple[str | None, str]:
    agent, why = usage.pick(plan["manager"] or "any")
    if agent and plan["manager"] and agent != plan["manager"]:
        db.comment(plan["id"], "sidus", f"{LABEL[agent]} took over as manager: {why}.")
    if agent and agent != plan["manager"]:
        db.update_plan(plan["id"], manager=agent)
    return agent, why


def kick(plan_id: str) -> None:
    """Starts the plan's next run, if it has one to start and nothing is running."""
    with _lock:
        plan = db.get_plan(plan_id)
        if not plan or plan["status"] not in ("planning", "running") or db.running_run(plan_id):
            return
        if _project_busy(plan):
            return _wait(plan, "Another plan in this project folder is running")
        try:
            _next(plan)
        except (ideas.NotFound, ideas.Invalid, RuntimeError) as e:
            db.update_plan(plan_id, status="paused", waiting=str(e))


def _wait(plan: dict, why: str | None) -> None:
    if plan["waiting"] != why:
        db.update_plan(plan["id"], waiting=why)


def _next(plan: dict) -> None:
    if plan["status"] == "planning":
        agent, why = _manager(plan)
        if not agent:
            return _wait(plan, why)
        answers = [c for c in db.comments(plan["id"], plan_level=True) if c["author"] == "you"]
        session = plan.get(f"{agent}_session")
        prompt = answers_prompt(answers[-1]["text"]) if session and answers else plan_prompt(db.get_plan(plan["id"]))
        return _start(plan, "plan", agent, prompt, session=session, reason=why)
    tasks = db.tasks(plan["id"])
    review = next((t for t in tasks if t["status"] == "review"), None)
    if review:
        agent, why = _manager(plan)
        if not agent:
            return _wait(plan, why)
        return _start(plan, "review", agent, review_prompt(db.get_plan(plan["id"]), review),
                      session=plan.get(f"{agent}_session"), task=review, reason=why)
    todo = next((t for t in tasks if t["status"] == "todo"), None)
    if todo:
        last = next((t["worker"] for t in reversed(tasks) if t["worker"]), None)
        agent, why = usage.pick(todo["assignee"], current=last)
        if not agent:
            return _wait(plan, why)
        session = todo.get(f"{agent}_session")
        notes = [c for c in db.comments(plan["id"], todo["id"]) if c["author"] in ("manager", "you")]
        prompt = revise_prompt(notes[-1]["text"]) if session and notes else work_prompt(plan, todo, agent)
        return _start(plan, "work", agent, prompt, session=session, task=todo, reason=why)
    if any(t["status"] == "failed" for t in tasks):
        db.update_plan(plan["id"], status="paused", waiting="A task failed; retry or skip it to carry on")
        return
    agent, why = _manager(plan)
    if not agent:
        return _wait(plan, why)
    _start(plan, "wrapup", agent, wrapup_prompt(plan), session=plan.get(f"{agent}_session"), reason=why)


def _start(plan: dict, role: str, agent: str, prompt: str, *, session: str | None = None, task: dict | None = None,
           reason: str = "") -> None:
    if plan["waiting"]:
        db.update_plan(plan["id"], waiting=None)
    run = db.add_run(plan["id"], role, agent, task_id=task["id"] if task else None, reason=reason)
    manager = role in ("plan", "review", "wrapup")
    mcp = procs.mcp_env(run["id"], agent, role)
    if agent == "claude":
        args = procs.claude_cmd(prompt, session=session, new_session=None if session else str(uuid.uuid4()), mcp=mcp,
                                manager=manager)
    else:
        args = procs.codex_cmd(prompt, session=session, mcp=mcp, manager=manager)
    log = procs.log_dir(plan["id"]) / f"{int(time.time())}-{role}-{agent}-{run['id']}.jsonl"
    before = procs.git_state(plan["cwd"]) if role == "work" else None
    try:
        proc = procs.spawn(args, plan["cwd"], agent, log)
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
        files = parsed["files"]
        if run["role"] == "work" and run["git_before"]:
            plan_cwd = (db.get_plan(run["plan_id"]) or {}).get("cwd")
            if plan_cwd:  # catches files written by shell commands too, not only edit tools
                files = sorted(set(files) | set(procs.git_changed(plan_cwd, json.loads(run["git_before"]))))
        run = db.update_run(run_id, status=status, session_id=parsed["session_id"], result=parsed["text"],
                            error="" if status == "done" else error, steps=parsed["steps"], files=files,
                            tokens=parsed["tokens"], ended=time.time(), pid=None)
        if status == "limit":
            usage.block(run["agent"], parsed["limit_resets"])
        plan = db.get_plan(run["plan_id"])
        if plan is None or run["role"] == "ask":
            return
        project = ideas.get_project(plan["project_id"])
        role = {"plan": "manager", "review": "manager", "wrapup": "manager", "work": "task"}[run["role"]]
        _tag(parsed["session_id"], f"Team · {project['title']} · {role}")
        if parsed["session_id"]:
            if run["role"] == "work":
                db.update_task(run["task_id"], **{f"{run['agent']}_session": parsed["session_id"]})
            else:
                db.update_plan(plan["id"], **{f"{run['agent']}_session": parsed["session_id"]})
        {"plan": _after_plan, "work": _after_work, "review": _after_review, "wrapup": _after_wrapup}[run["role"]](
            plan, run, parsed)
    kick_all()


def _after_plan(plan: dict, run: dict, parsed: dict) -> None:
    plan = db.get_plan(plan["id"])
    if plan["status"] != "planning":
        return
    if run["status"] in ("limit", "stopped"):
        if run["status"] == "stopped":
            db.update_plan(plan["id"], status="paused", waiting="Stopped")
        return  # a limit: the next kick picks the other agent as manager
    if plan["questions"]:
        db.update_plan(plan["id"], status="questions")
    elif db.tasks(plan["id"]):
        db.update_plan(plan["id"], status="proposed", summary=parsed["text"] or plan["summary"])
    else:
        db.update_plan(plan["id"], status="paused",
                       waiting=f"The manager didn't propose tasks{': ' + run['error'] if run['error'] else ''}")


def _after_work(plan: dict, run: dict, parsed: dict) -> None:
    task = db.get_task(run["task_id"])
    if not task:
        return
    worker = LABEL[run["agent"]]
    if run["status"] == "stopped":
        db.update_task(task["id"], status="todo")
        db.update_plan(plan["id"], status="paused", waiting="Stopped")
        return
    if run["status"] == "limit":
        db.update_task(task["id"], status="todo", attempts=max(0, task["attempts"] - 1))
        done = "; ".join(parsed["steps"][-10:])
        db.comment(plan["id"], "sidus", f"{worker} hit its usage limit partway through."
                   + (f" What it had done: {done}." if done else "")
                   + (f" Files it changed: {', '.join(run['files'][:20])}." if run["files"] else "")
                   + " The other agent picks this up; check those changes first.", task_id=task["id"])
        return
    report = parsed["text"] or run["error"]
    files = sorted(set(task["files"]) | set(run["files"]))
    if report:
        db.comment(plan["id"], run["agent"], _clip(report, 4000), task_id=task["id"])
    blocked = run["verdict"] == "blocked"
    if run["status"] == "done" and not blocked:
        db.update_task(task["id"], status="review" if usage.get_settings()["review_each"] else "done",
                       report=report, files=files)
    elif task["attempts"] >= MAX_ATTEMPTS:
        db.update_task(task["id"], status="failed", report=report, files=files)
    else:
        db.update_task(task["id"], status="review", report=report, files=files)


def _after_review(plan: dict, run: dict, parsed: dict) -> None:
    task = db.get_task(run["task_id"])
    if task and task["status"] == "review" and run["status"] == "done":
        # The manager ended without deciding: don't loop, let the user choose.
        db.update_plan(plan["id"], status="paused", waiting=f"The manager didn't decide on \"{task['title']}\"")


def _after_wrapup(plan: dict, run: dict, parsed: dict) -> None:
    plan = db.get_plan(plan["id"])
    if plan["status"] != "running" or run["status"] != "done":
        return
    if not any(t["status"] in ("todo", "review") for t in db.tasks(plan["id"])):
        db.update_plan(plan["id"], status="done", summary=parsed["text"] or plan["summary"])
        db.comment(plan["id"], "sidus", "The manager didn't mark the milestone done in Ideas; check it yourself.")


# ---------- your actions ----------

def approve(plan_id: str) -> dict:
    plan = db.get_plan(plan_id)
    if plan["status"] != "proposed" or not db.tasks(plan_id):
        raise ideas.Invalid("Only a proposed plan with tasks can be approved")
    db.update_plan(plan_id, status="running", waiting=None)
    db.comment(plan_id, "sidus", APPROVED)
    try:
        _, milestone = _context(plan)
        if milestone["status"] == "todo":
            ideas.update_milestone(milestone["id"], {"status": "doing"})
    except ideas.NotFound:
        pass
    kick(plan_id)
    return db.get_plan(plan_id)


def answer(plan_id: str, text: str) -> dict:
    plan = db.get_plan(plan_id)
    if plan["status"] != "questions":
        raise ideas.Invalid("The manager isn't waiting for answers")
    db.comment(plan_id, "you", text)
    db.update_plan(plan_id, status="planning", questions="")
    kick(plan_id)
    return db.get_plan(plan_id)


def replan(plan_id: str, note: str) -> dict:
    plan = db.get_plan(plan_id)
    if plan["status"] not in ("proposed", "paused") or db.running_run(plan_id):
        raise ideas.Invalid("Only a proposed or paused plan can be re-planned")
    if any(t["status"] in ("done", "working") for t in db.tasks(plan_id)):
        raise ideas.Invalid("Some tasks are already done; edit the task list instead")
    db.clear_tasks(plan_id)
    if note:
        db.comment(plan_id, "you", note)
    db.update_plan(plan_id, status="planning", waiting=None, questions="")
    kick(plan_id)
    return db.get_plan(plan_id)


def pause(plan_id: str) -> dict:
    plan = db.get_plan(plan_id)
    if plan["status"] in ("planning", "running"):
        db.update_plan(plan_id, status="paused", waiting="Paused by you")
    return db.get_plan(plan_id)


APPROVED = "Plan approved."


def _approved(plan_id: str) -> bool:
    return any(c["author"] == "sidus" and c["text"] == APPROVED for c in db.comments(plan_id, plan_level=True))


def resume(plan_id: str) -> dict:
    plan = db.get_plan(plan_id)
    if plan["status"] != "paused":
        raise ideas.Invalid("The plan isn't paused")
    status = "running" if _approved(plan_id) else ("proposed" if db.tasks(plan_id) else "planning")
    db.update_plan(plan_id, status=status, waiting=None)
    kick(plan_id)
    return db.get_plan(plan_id)


def stop(plan_id: str) -> bool:
    run = db.running_run(plan_id)
    if not run or not run["pid"] or not procs.alive(run):
        return False
    _stopping.add(run["id"])
    try:
        os.killpg(run["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    return True


def cancel(plan_id: str) -> dict:
    stop(plan_id)
    db.update_plan(plan_id, status="cancelled", waiting=None)
    return db.get_plan(plan_id)


# ---------- tools the agents call (through the MCP server) ----------

class ToolRefused(Exception):
    pass


def _tool_run(run_id: str, agent: str, roles: tuple[str, ...]) -> tuple[dict, dict]:
    run = db.get_run(run_id)
    if not run or run["status"] != "running" or run["agent"] != agent or run["role"] not in roles:
        raise ToolRefused("That tool isn't available in this run.")
    return run, db.get_plan(run["plan_id"])


def tool_propose(run_id: str, agent: str, tasks: list[dict]) -> str:
    _, plan = _tool_run(run_id, agent, ("plan",))
    db.clear_tasks(plan["id"])
    for t in tasks[:12]:
        db.add_task(plan["id"], t["title"], t.get("details", ""), t.get("done_when", ""), t.get("assignee", "any"))
    return f"Proposed {len(db.tasks(plan['id']))} tasks. The user will review them. End your turn with one line."


def tool_ask_user(run_id: str, agent: str, questions: list[str]) -> str:
    _, plan = _tool_run(run_id, agent, ("plan",))
    text = "\n".join(f"{i}. {q}" for i, q in enumerate(questions[:3], start=1))
    db.update_plan(plan["id"], questions=text)
    db.comment(plan["id"], "manager", text, agent=agent)
    return "Sent to the user. End your turn now; you'll get the answers in your next turn."


def tool_comment(run_id: str, agent: str, text: str) -> str:
    run, plan = _tool_run(run_id, agent, ("plan", "work", "review", "wrapup"))
    author = agent if run["role"] == "work" else "manager"
    db.comment(plan["id"], author, text, task_id=run["task_id"], agent=agent)
    return "Comment added."


def tool_blocked(run_id: str, agent: str, reason: str) -> str:
    run, plan = _tool_run(run_id, agent, ("work",))
    db.update_run(run_id, verdict="blocked")
    db.comment(plan["id"], agent, f"Blocked: {reason}", task_id=run["task_id"])
    return "The manager will look at it. End your turn with a short report of what you did."


def _review_task(run_id: str, agent: str) -> tuple[dict, dict, dict]:
    run, plan = _tool_run(run_id, agent, ("review",))
    return run, plan, db.get_task(run["task_id"])


def tool_approve(run_id: str, agent: str, notes: str) -> str:
    run, plan, task = _review_task(run_id, agent)
    db.update_run(run_id, verdict="approved")
    db.update_task(task["id"], status="done")
    if notes:
        db.comment(plan["id"], "manager", notes, task_id=task["id"], agent=agent)
    return "Task marked done."


def tool_revise(run_id: str, agent: str, notes: str, assignee: str) -> str:
    run, plan, task = _review_task(run_id, agent)
    db.update_run(run_id, verdict="revise")
    db.update_task(task["id"], status="todo", assignee=assignee if assignee in db.ASSIGNEES else task["assignee"])
    db.comment(plan["id"], "manager", notes, task_id=task["id"], agent=agent)
    return "Sent back. End your turn with one line."


def tool_skip(run_id: str, agent: str, reason: str) -> str:
    run, plan, task = _review_task(run_id, agent)
    db.update_run(run_id, verdict="skipped")
    db.update_task(task["id"], status="skipped")
    db.comment(plan["id"], "manager", f"Skipped: {reason}", task_id=task["id"], agent=agent)
    return "Task skipped."


def tool_create(run_id: str, agent: str, title: str, details: str, done_when: str, assignee: str) -> str:
    _, plan = _tool_run(run_id, agent, ("review", "wrapup"))
    if len(db.tasks(plan["id"])) >= 20:
        return "This plan already has 20 tasks; finish or skip some first."
    db.add_task(plan["id"], title, details, done_when, assignee)
    return "Task added at the end of the list."


def tool_complete(run_id: str, agent: str, summary: str) -> str:
    _, plan = _tool_run(run_id, agent, ("wrapup",))
    ideas.update_milestone(plan["milestone_id"], {"status": "done", "note": _clip(summary, 2000)})
    db.update_plan(plan["id"], status="done", summary=summary)
    return "Milestone marked done. End your turn with one line."


def tool_ask(run_id: str, agent: str, question: str) -> str:
    """Runs the other agent on a question, without changing files, and returns its answer."""
    run, plan = _tool_run(run_id, agent, ("work",))
    partner = usage.other(agent)
    why = usage.unusable(partner)
    if why:
        return f"{why}, so it can't answer."
    task = db.get_task(run["task_id"])
    prompt = (f"{LABEL[agent]} is working on this task in this folder and asks you the question below. Answer it "
              f"briefly. Don't change any files.\n\nTask: {task['title']}\n{_clip(task['details'], 800)}\n\n"
              f"Question: {question}")
    ask = db.add_run(plan["id"], "ask", partner, task_id=task["id"], reason=f"Question from {LABEL[agent]}")
    log = procs.log_dir(plan["id"]) / f"{int(time.time())}-ask-{partner}-{ask['id']}.jsonl"
    args = (procs.claude_cmd(prompt, read_only=True) if partner == "claude" else procs.codex_cmd(prompt))
    proc = procs.spawn(args, plan["cwd"], partner, log)
    db.update_run(ask["id"], log=str(log), pid=proc.pid, pid_started=procs.started_at(proc.pid))
    try:
        proc.wait(timeout=procs.ASK_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait()
    finalize(ask["id"])
    done = db.get_run(ask["id"])
    db.comment(plan["id"], agent, f"Asked {LABEL[partner]}: {_clip(question, 500)}", task_id=task["id"])
    if done["result"]:
        db.comment(plan["id"], partner, _clip(done["result"], 2000), task_id=task["id"])
        return done["result"]
    return f"{LABEL[partner]} couldn't answer: {done['error'] or 'no reply'}"


# ---------- start-up ----------

def kick_all() -> None:
    for plan_id in db.plans_to_kick():
        try:
            kick(plan_id)
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
