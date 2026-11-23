# Relay plugin

Relay is one conversation with two agents. You send messages to a thread, and each turn goes to Claude Code or
Codex, whichever plan has usage left. The two can ask each other questions and hand work to each other, and when
one hits its usage limit partway through a turn, the other picks up where it stopped.

## Turn it on

Set `SIDUS_PLUGINS=relay` (or `ideas,relay`) and restart Sidus. With Docker, add it to `.env`. Both agents need to
be signed in under Settings: Claude, and ChatGPT for Codex. With only one signed in, Relay uses that one.

On first start Sidus creates these in its data folder:

| Path | What it is |
|---|---|
| `relay.db` | Threads and turns (SQLite) |
| `relay-token` | The token the agents' Relay tools use to reach Sidus. Readable only by you. It works for `/api/relay/mcp/*` and nothing else |
| `relay/<thread>/` | Each turn's raw output from the agent |

## How turns run

Turns run in the background, without a terminal: `claude -p` or `codex exec`, in the thread's folder, with
**permissions bypassed** (`--permission-mode bypassPermissions` for Claude, `--dangerously-bypass-approvals-and-sandbox`
for Codex). Nobody is asked before a file is edited or a command runs. Only start threads in folders where that's
fine, and use git so you can review and undo changes.

Each agent keeps its own conversation for the thread (`--resume` / `codex exec resume`). When an agent takes a turn
after the other one has worked, its prompt starts with a catch-up: your messages, the other agent's replies, and the
files it changed. Turns keep running if Sidus restarts; Sidus picks them up again when it starts.

Both agents' sessions also show in the sidebar, named "Relay · <thread>", so you can open the full transcript.

## Which agent takes a turn

1. A turn that was handed over goes to the agent it was handed to. A turn that's picked up after a usage limit
   goes to the other agent.
2. A pinned thread ("Always Claude" / "Always Codex") uses that agent.
3. Otherwise the thread stays with the agent that took the last turn while that plan is under the switch point:
   80% of its 5-hour limit and 90% of its weekly limit by default. Past that, the other agent takes over. If both
   are past it, the one with more room left goes. You can change the switch point on the Relay page.
4. An agent that hit its usage limit isn't used until the limit resets. If both are out, the turn waits and starts
   by itself when one frees up.

Usage numbers come from Claude Code itself (it reports them with every run), Sidus's Claude usage check, and the
ChatGPT usage check.

## Tools the agents get

Every turn gets an MCP server, `sidus-relay`, with three tools:

| Tool | What it does |
|---|---|
| `ask_codex` / `ask_claude` | Asks the other agent a question and waits for the answer (up to 10 minutes). |
| `hand_off(task, notes)` | Passes the rest of the work to the other agent when this turn ends. |
| `usage()` | Both plans' 5-hour and weekly use, and the switch point. |

When asked a question, Claude runs read-only (`--permission-mode default` in a forked copy of its thread
conversation, so writes are refused). Codex runs unsandboxed, because its sandbox doesn't work in Docker, and is
told not to change anything; treat its answers as unsupervised. The agent answering a question doesn't get the
Relay tools, so the two can't ask each other back and forth in a loop.

## Limits

- One turn at a time per thread. Messages sent while a turn runs wait in line.
- Codex's usage numbers are only as fresh as the last ChatGPT usage check (every few minutes), so a fast run can
  go past the switch point before Relay notices.
- Stopping a turn sends it SIGTERM. Whatever it changed stays changed.
