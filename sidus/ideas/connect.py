"""How Claude sessions reach the Ideas MCP server.

Sidus starts every Claude session with `--mcp-config '<json>'`, which names the server (`python -m sidus.ideas.mcp`)
and tells it where Sidus is and where the token file is. The token itself is never put on a command line.

The config is passed inline rather than as a file: Claude Code refuses to start if an --mcp-config file is
missing (for example when sessions run in the host's tmux, outside the container), but only skips a server
that fails to start.
"""
from __future__ import annotations

import ipaddress
import json
import shlex
import sys
from pathlib import Path

from ..config import settings
from . import token

SERVER = "sidus-ideas"   # MCP server name; Claude sees the tools as mcp__sidus-ideas__ideas_*
ROOT = Path(__file__).resolve().parents[2]  # folder that contains the sidus package


def sidus_url() -> str:
    """Where the MCP server can reach this Sidus from the same machine."""
    host = settings.host.strip()
    if host in {"", "0.0.0.0", "::", "*"}:
        host = "127.0.0.1"
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass  # a hostname
    return f"http://{host}:{settings.port}"


def server_entry(*, token_value: str | None = None) -> dict:
    """The stdio server entry. By default it points at the token file; pass token_value to embed the token."""
    env = {"SIDUS_URL": sidus_url(), "PYTHONPATH": str(ROOT)}
    if token_value:
        env["SIDUS_IDEAS_TOKEN"] = token_value
    else:
        env["SIDUS_IDEAS_TOKEN_FILE"] = str(token.path())
    return {"command": sys.executable, "args": ["-m", "sidus.ideas.mcp"], "env": env}


def mcp_config() -> str:
    """The --mcp-config value for Sidus's sessions (JSON, no token)."""
    return json.dumps({"mcpServers": {SERVER: server_entry()}}, separators=(",", ":"))


def claude_args() -> list[str]:
    """Arguments Sidus adds to every Claude session: the server, and permission to use its tools without asking."""
    return ["--mcp-config", mcp_config(), "--allowedTools", f"mcp__{SERVER}__*"]


def add_command(url: str | None = None) -> str:
    """`claude mcp add` command for a Claude Code session started outside Sidus, on this machine."""
    env = server_entry(token_value=token.get())["env"]
    if url:
        env["SIDUS_URL"] = url
    parts = ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio"]
    for key, value in env.items():
        parts += ["-e", f"{key}={value}"]
    parts += [SERVER, "--", sys.executable, "-m", "sidus.ideas.mcp"]
    return " ".join(shlex.quote(p) for p in parts)
