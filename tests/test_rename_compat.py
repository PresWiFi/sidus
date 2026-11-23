"""Installs from before the rename (Hangar → Sidus) keep working: old env vars, data dir, cookie, header, tmux options."""
from pathlib import Path

from fastapi.testclient import TestClient

from sidus import auth, config, tmux
from sidus.app import app
from sidus.ideas import mcp


def test_env_prefers_sidus_then_hangar(monkeypatch):
    monkeypatch.delenv("SIDUS_PORT", raising=False)
    monkeypatch.setenv("HANGAR_PORT", "7000")
    assert config.env("PORT") == "7000"
    monkeypatch.setenv("SIDUS_PORT", "7001")
    assert config.env("PORT") == "7001"
    assert config.env("NOT_SET_ANYWHERE", "fallback") == "fallback"


def test_settings_read_old_names(monkeypatch, tmp_path):
    for name in ("WORKSPACES", "AUTORESTORE", "DATA_DIR"):
        monkeypatch.delenv("SIDUS_" + name, raising=False)
    monkeypatch.setenv("HANGAR_WORKSPACES", "/a,/b")
    monkeypatch.setenv("HANGAR_AUTORESTORE", "true")
    monkeypatch.setenv("HANGAR_DATA_DIR", str(tmp_path))
    s = config.Settings.from_env()
    assert s.workspaces == ["/a", "/b"] and s.autorestore and s.data_dir == tmp_path


def test_default_data_dir_keeps_old_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    share = tmp_path / ".local" / "share"
    assert config._default_data_dir() == share / "sidus"
    (share / "hangar").mkdir(parents=True)
    assert config._default_data_dir() == share / "hangar"
    (share / "sidus").mkdir()
    assert config._default_data_dir() == share / "sidus"


def test_mcp_reads_old_env(monkeypatch):
    monkeypatch.delenv("SIDUS_URL", raising=False)
    monkeypatch.delenv("SIDUS_IDEAS_TOKEN", raising=False)
    monkeypatch.setenv("HANGAR_URL", "http://old:7878/")
    monkeypatch.setenv("HANGAR_IDEAS_TOKEN", "tok")
    assert mcp._url() == "http://old:7878"
    assert mcp._token() == "tok"


def test_old_cookie_and_header_still_work():
    c = TestClient(app, headers={"X-Hangar": "1"})
    c.cookies.set(auth.OLD_COOKIE, auth.make_token())
    assert c.get("/api/me").status_code == 200
    cleared = c.post("/api/logout").headers.get_list("set-cookie")
    assert any(h.startswith(auth.OLD_COOKIE + "=") for h in cleared)  # signing out clears the old cookie too


def test_writes_without_either_header_are_refused():
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.make_token())
    assert c.post("/api/logout").status_code == 403


def test_tmux_options_fall_back_to_old_names():
    assert tmux._opt("claude") == "#{?@sidus_claude,#{@sidus_claude},#{@hangar_claude}}"
