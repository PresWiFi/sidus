"""Settings, read once from environment variables."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _list(value: str) -> list[str]:
    return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]


def env(name: str, default: str | None = None) -> str | None:
    """SIDUS_<name>, or HANGAR_<name> from before the rename (Hangar → Sidus, 2026-11)."""
    for prefix in ("SIDUS_", "HANGAR_"):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


def _default_data_dir() -> Path:
    share = Path.home() / ".local" / "share"
    new, old = share / "sidus", share / "hangar"
    return old if old.is_dir() and not new.exists() else new


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: str = "0.0.0.0"
    hostname: str = ""             # name shown in the UI
    port: int = 7878
    data_dir: Path = field(default_factory=_default_data_dir)
    claude_dir: Path = Path.home() / ".claude"
    codex_home: Path = Path.home() / ".codex"   # Codex CLI's login, config, and session logs
    # Folders new sessions may start in. The first one is the default.
    workspaces: list[str] = field(default_factory=list)
    password: str = ""
    tmux_socket: str = ""             # empty = tmux's default socket
    claude_cmd: str = "claude"
    gh_cmd: str = "gh"                # GitHub CLI, for linking GitHub and cloning repos
    claude_args: str = ""             # extra arguments for every new or resumed session
    claude_base_args: str = ""        # set by the Docker image (permission rules, container notes)
    autorestore: bool = False         # reopen sessions that were running when Sidus last stopped
    usage_poll_minutes: float = 5
    secure_cookies: bool = False      # set when served over HTTPS only
    max_upload_mb: int = 512
    public_url: str = ""              # address people open Sidus at, e.g. https://sidus.example.ts.net (SSO redirect)
    plugins: list[str] = field(default_factory=list)  # optional plugins to turn on, e.g. ["ideas"]

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        s = cls()
        s.host = env("HOST", s.host)
        s.hostname = env("HOSTNAME") or socket.gethostname()
        s.port = int(env("PORT", s.port))
        s.data_dir = Path(env("DATA_DIR") or s.data_dir).expanduser()
        s.claude_dir = Path(e.get("CLAUDE_CONFIG_DIR") or s.claude_dir).expanduser()
        s.codex_home = Path(e.get("CODEX_HOME") or s.codex_home).expanduser()
        s.workspaces = _list(env("WORKSPACES", "")) or [str(Path.home())]
        s.password = env("PASSWORD", "")
        s.tmux_socket = env("TMUX_SOCKET", "")
        s.claude_cmd = env("CLAUDE_CMD", s.claude_cmd)
        s.gh_cmd = env("GH_CMD", s.gh_cmd)
        s.claude_args = env("CLAUDE_ARGS", "")
        s.claude_base_args = env("CLAUDE_BASE_ARGS", "")
        s.autorestore = _bool(env("AUTORESTORE"))
        s.usage_poll_minutes = max(1.0, float(env("USAGE_POLL_MINUTES", s.usage_poll_minutes)))
        s.secure_cookies = _bool(env("SECURE_COOKIES"))
        s.max_upload_mb = max(1, int(env("MAX_UPLOAD_MB") or s.max_upload_mb))
        s.public_url = (env("PUBLIC_URL") or "").strip().rstrip("/")
        s.plugins = [p.lower() for p in _list(env("PLUGINS", ""))]
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s


settings = Settings.from_env()
