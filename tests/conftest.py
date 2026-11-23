"""Test setup. Sidus reads its settings from env vars at import time, so set them before importing it."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="sidus-test-"))
WORKSPACE = _TMP / "workspace"
WORKSPACE.mkdir()
# Drop the host's settings (this may run inside a Sidus container), including pre-rename HANGAR_ names.
for _k in [k for k in os.environ if k.startswith(("SIDUS_", "HANGAR_"))]:
    del os.environ[_k]
os.environ.update({
    "SIDUS_DATA_DIR": str(_TMP / "data"),
    "CLAUDE_CONFIG_DIR": str(_TMP / "claude"),
    "SIDUS_PASSWORD": "test-password",
    "SIDUS_WORKSPACES": str(WORKSPACE),
    "SIDUS_TMUX_SOCKET": str(_TMP / "tmux.sock"),
    "SIDUS_PLUGINS": "ideas",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from sidus import auth  # noqa: E402
from sidus.app import app  # noqa: E402
from sidus.ideas import db, token  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.open_db(tmp_path / "ideas.db")
    yield


# Clients are not used as context managers, so Sidus's startup (usage poller, tmux restore) never runs.

@pytest.fixture
def anon():
    return TestClient(app)


@pytest.fixture
def client():
    """The web UI: signed-in cookie plus the X-Sidus header."""
    c = TestClient(app, headers={"X-Sidus": "1"})
    c.cookies.set(auth.COOKIE, auth.make_token())
    return c


@pytest.fixture
def mcp():
    """The MCP server: bearer token only."""
    return TestClient(app, headers={"Authorization": f"Bearer {token.get()}"})


@pytest.fixture
def workspace():
    return WORKSPACE
