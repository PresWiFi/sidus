"""Bearer token for the Team MCP server. It works only for /api/team/mcp/*; the web UI keeps using the cookie."""
from __future__ import annotations

import hmac
import os
import secrets

from fastapi import Request

from ..config import settings

FILE = "team-token"
MCP_PREFIX = "/api/team/mcp/"
_cached: str | None = None


def path():
    return settings.data_dir / FILE


def get() -> str:
    """The token, created on first use in the data dir (readable only by its owner)."""
    global _cached
    if _cached is None:
        p = path()
        if p.exists():
            _cached = p.read_text().strip()
        if not _cached:
            _cached = secrets.token_urlsafe(32)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(_cached + "\n")
            os.chmod(p, 0o600)
    return _cached


def authorize(request: Request) -> bool:
    if not request.url.path.startswith(MCP_PREFIX):
        return False
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and bool(value.strip()) and hmac.compare_digest(value.strip().encode(),
                                                                                       get().encode())
