# Team plugin

Team turns an Ideas milestone into finished work. A manager (Claude or Codex) plans the milestone into tasks, asking
you questions first if it needs to. You approve the tasks, then Claude Code and Codex do them one at a time in the
project's folder. The manager checks the result at the end and marks the milestone done in Ideas.

It's built to be light on usage. The manager only runs to plan, to decide when a task fails or a worker is blocked,
and to wrap up. It runs on a smaller model with read-only tools and a step cap. Workers get only their task, its
thread, and short reports from earlier tasks.

## Turn it on

Set `SIDUS_PLUGINS=ideas,team` and restart Sidus. With Docker, add it to `.env`. Team needs Ideas: the projects,
milestones and folders come from there. Sign in to Claude and to ChatGPT (for Codex) in Settings. With only one
signed in, that one does everything.

On first start Sidus creates these in its data folder:

| Path | What it is |
|---|---|
| `team.db` | Plans, tasks, runs and notes (SQLite) |
| `team-token` | The token the agents' Team tools use to reach Sidus. Readable only by you. It works for `/api/team/mcp/*` and nothing else |
| `team/<plan>/` | Each run's raw output |

## How a plan goes

1. **Plan.** On the Team page, click **Plan** next to a milestone, optionally saying what matters. The manager is
   whichever agent has more usage left; it stays manager for the plan unless it runs out. It reads the project's
   context file and as much of the code as planning needs.
2. **Questions.** If something unclear would change the plan, the manager asks you up to 3 short questions and
   waits for your answers.
3. **Approve.** The manager proposes 1-6 tasks, each with a "done when" and an assignee (Claude, Codex or either).
   Edit, reorder, add or delete tasks, then **Approve and start**. **Re-plan** asks the manager again, with a note.
4. **Work.** Tasks run in order, one at a time per project folder. A task for "either" goes to the agent with more
   room. A worker checks its own work against "done when" and writes a short report. If it can't finish, it calls
   `task_blocked` and the manager decides: send it back with notes (to the same agent or the other), approve it,
   or skip it. After 3 attempts a task fails and the plan pauses for you.
5. **Wrap-up.** When every task is done, the manager checks the milestone's "done when". It then either marks the
   milestone done in Ideas, with a short summary, or adds the tasks that are missing.

You can pause, resume, stop the current run, cancel, add a note to any task (the next worker reads it), and retry or
skip a task.

## Shared context

Everyone works from the same things:
- The project folder itself.
- Your `context/` docs. Claude reads `CLAUDE.md` and Codex reads `AGENTS.md`; keep the two identical.
- The plan: project summary, milestone, your request and answers, and each task's notes.
- Earlier tasks' reports and the files they changed. Changed files come from `git status` before and after each run,
  so files written by shell commands count too, as long as the folder is a git repo.

Each worker keeps its own conversation per task, so a task sent back resumes where it left off. The manager keeps
one conversation per plan.

## Usage

An agent is "under" while its 5-hour and weekly use are below the switch point (80% and 90% by default). Work goes to
an agent that's under; past that, to the one with more room. An agent that hits its usage limit mid-task is skipped
until the limit resets, and the other agent continues the task with a note about what was already changed.

Each run shows the tokens it used: new input plus output, not counting cached input. Expect roughly 15-25k per run
just for each CLI's own system prompt and tools, so fewer, larger tasks use less than many small ones.

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
| Manager, planning | `propose_tasks`, `ask_user`, `comment` |
| Manager, deciding on a task | `approve_task`, `revise_task`, `skip_task`, `create_task`, `comment` |
| Manager, wrap-up | `complete_milestone`, `create_task`, `comment` |
| Worker | `ask_claude` / `ask_codex` (the other agent answers without changing files), `task_blocked`, `comment` |
