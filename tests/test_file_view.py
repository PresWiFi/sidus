"""The file viewer's source route: workspace files shown inline, sandboxed."""
from pathlib import Path


def test_view_types_and_sandbox(client, anon, workspace, tmp_path):
    d = Path(workspace) / "viewer"
    d.mkdir(exist_ok=True)
    (d / "r.md").write_text("# Hi\n")
    (d / "page.html").write_text("<script>1</script>")
    (d / "noext").write_text("plain")
    (d / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    get = lambda p, **q: client.get("/api/files/view", params={"path": str(d / p), **q})

    r = get("r.md")
    assert r.status_code == 200 and r.text == "# Hi\n"
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-security-policy"] == "sandbox" and r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].startswith("inline")
    assert get("noext").headers["content-type"] == "text/plain; charset=utf-8"
    # HTML never runs in Sidus's origin; scripts only when asked, still sandboxed (no allow-same-origin).
    r = get("page.html")
    assert r.headers["content-type"].startswith("text/html") and r.headers["content-security-policy"] == "sandbox"
    csp = get("page.html", scripts="true").headers["content-security-policy"]
    assert "allow-scripts" in csp and "allow-same-origin" not in csp
    # Images and PDFs keep their type so the browser renders them.
    assert get("pic.png").headers["content-type"] == "image/png"
    r = get("doc.pdf")
    assert r.headers["content-type"] == "application/pdf" and "content-security-policy" not in r.headers
    assert get("r.md", download="true").headers["content-disposition"].startswith("attachment")

    outside = tmp_path / "x.txt"
    outside.write_text("no")
    assert client.get("/api/files/view", params={"path": str(outside)}).status_code == 403
    assert client.get("/api/files/view", params={"path": str(d / "missing.md")}).status_code == 404
    assert anon.get("/api/files/view", params={"path": str(d / "r.md")}).status_code == 401


def test_bare_path_links_redirect_to_viewer(client, anon):
    from sidus.config import settings
    r = anon.get("/root/claude/context/flip-phone-mfa-setup.md", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/#/view?path=%2Froot%2Fclaude%2Fcontext%2Fflip-phone-mfa-setup.md"
    goal_file = f"{settings.data_dir}/team/46660d2e612d/STATUS.md"
    r = client.get(goal_file, follow_redirects=False)
    assert r.status_code == 302 and "goal=46660d2e612d" in r.headers["location"]
    # Real 404s stay JSON: API paths, folders, unknown routes without an extension.
    assert client.get("/api/team/goals/nope").status_code == 404
    assert client.get("/api/nope.json").json() == {"detail": "Not Found"}
    assert client.get("/root/claude/").status_code == 404
    assert client.get("/nothing").status_code == 404
