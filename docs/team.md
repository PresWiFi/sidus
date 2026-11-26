# Team plugin

Give Team a goal or a runbook and a folder. A manager (Claude or Codex) splits the goal into phases, asking you
questions first if it needs to. You approve the phases once. Then Claude Code and Codex work through each phase's
tasks one at a time, and the manager checks each phase before moving on.

It's built to be light on usage. The manager runs once to outline, once per phase (one run that checks the phase
and plans the next), and otherwise only when a task fails or a worker is blocked. It runs on a smaller model with
read-only tools and a step cap. Long goals and runbooks stay in files; prompts carry only the current phase and
task.

## Turn it on

Set `SIDUS_PLUGINS=team` (or add `team` to the list) and restart Sidus. With Docker, add it to `.env`. Sign in to
Claude, and to ChatGPT for Codex, in Settings. With only one signed in, that one does everything.

On first start Sidus creates these in its data folder:

| Path | What it is |
|---|---|
| `team.db` | Goals, phases, tasks, runs and notes (SQLite) |
| `team-token` | The token the agents' Team tools use to reach Sidus. Readable only by you. It works for `/api/team/mcp/*` and nothing else |
| `team/<goal>/` | `goal.md` (the goal as you gave it), an uploaded runbook, and each run's raw output |
| `team/uploads/` | Runbooks uploaded for a goal that wasn't created; cleared after a day |

## How a goal goes

1. **Start.** On the Team page, click **New goal**. Pick a folder, then describe the goal or paste a runbook, and/or
   give a runbook file: upload one, or name one that's already in the folder (e.g. `docs/runbook.md`). Long is fine.
   An uploaded runbook is kept with the goal in Sidus's data folder, not in your repo; open it from the goal page.
2. **Questions.** If something unclear would change the plan, the manager asks you up to 3 short questions and
   waits for your answers.
3. **Approve.** The manager proposes 1-8 phases, each with a "done when", plus the tasks for phase 1. Edit,
   reorder, add or delete phases and phase-1 tasks, then click **Approve and start**. **Re-plan** asks the manager
   again, with a note. This is the only approval.
4. **Work.** Each phase's tasks run in order, one at a time per folder. A task for "either" goes to the agent with
   more room. A worker checks its own work against "done when" and writes a short report. If it can't finish, it
   calls `task_blocked`, and the manager sends the task back with notes (to the same agent or the other), approves
   it, or skips it. After 3 attempts a task fails and the goal pauses for you.
5. **Next phase.** When a phase's tasks are done, the manager checks its "done when". In the same run it either
   finishes the phase and plans the next phase's tasks, or adds the tasks that are missing. After the last phase,
   the goal is done, with a short summary.

You can pause, resume, stop the current run, cancel, add a note to any task (the next worker reads it), retry or
skip a task, add a task to a phase, or skip a phase.

## Files

The goal page lists the files the agents wrote, edited or linked to that still exist, newest first. Those files, and any file path in a report, open in the file viewer. It can read files in `SIDUS_WORKSPACES` and the goal's own folder under the data directory.

## Shared context

Everyone works from the same things:
- The folder itself.
- Your `context/` docs. Claude reads `CLAUDE.md` and Codex reads `AGENTS.md`; keep the two identical.
- The goal file (and the runbook, if you named one). Agents are pointed to it and read the part they need.
- Each finished phase's summary, earlier tasks' reports in the phase, and each task's notes.
- The files each task changed. These come from `git status` before and after each run, so files written by shell
  commands count too, as long as the folder is a git repo. Cache output such as `__pycache__` is left out.

Each worker keeps its own conversation per task, so a task sent back resumes where it left off. The manager keeps
one conversation per goal.

## Usage

An agent is "under" while its 5-hour and weekly use are below the switch point (80% and 90% by default). Work goes to
an agent that's under; past that, to the one with more room. An agent that hits its usage limit mid-task is skipped
until the limit resets, and the other agent continues the task with a note about what was already changed.

Each run shows the tokens it used: new input plus output, not counting cached input. Every run starts with each
CLI's own system prompt and tools, so fewer, larger tasks use less than many small ones. As a reference, a
two-phase goal with one task per phase took 5 runs and about 60k new tokens.

Settings on the Team page:

| Setting | Default | |
|---|---|---|
| Switch point | 80% of 5h / 90% of week | When to move work to the other agent |
| Prefer | Claude | Who goes first when both have room |
| Claude as manager: model | `sonnet` | `--model` for manager runs; empty = Claude Code's default |
| Claude as manager: max steps | 15 | `--max-turns` for manager runs |
| Codex as manager: reasoning effort | `low` | `model_reasoning_effort` for manager runs |
| Manager reviews every task | off | Turn on for a manager check after each task (uses more) |

## Permissions

Workers run unattended with permissions bypassed (`--permission-mode bypassPermissions` for Claude,
`--dangerously-bypass-approvals-and-sandbox` for Codex). Nobody is asked before a file is edited or a command runs, so
use Team only in folders where that's fine, and use git so you can review and undo.

Claude as manager runs with `--permission-mode default` and only Read, Glob and Grep, so it can't change files.
Codex as manager, and Codex answering a question, run unsandboxed (its sandbox doesn't work in Docker) and are told
not to change anything; that isn't enforced.

## Tools the agents get

The `sidus-team` MCP server, with tools by role:

| Role | Tools |
|---|---|
| Manager, outlining | `propose_phases` (with phase 1's tasks), `ask_user`, `comment` |
| Manager, planning a phase with no tasks | `propose_tasks`, `comment` |
| Manager, checking a phase | `complete_phase` (with the next phase's tasks), `create_task`, `comment` |
| Manager, deciding on a task | `approve_task`, `revise_task`, `skip_task`, `create_task`, `comment` |
| Worker | `ask_claude` / `ask_codex` (the other agent answers without changing files), `task_blocked`, `comment` |

## Upgrading from 0.7.0

0.7.0 planned per Ideas milestone. 0.8.0 starts from goals instead and no longer needs Ideas. On first start it
drops 0.7.0's Team tables (plans, tasks, runs, notes); Ideas itself is untouched.
