"""Browse, upload, and download files inside the configured workspaces."""
from __future__ import annotations

import mimetypes
import os
import re
import uuid
from pathlib import Path

from .config import settings


class FileError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def inside_workspace(path: str) -> bool:
    real = os.path.realpath(path)
    for w in settings.workspaces:
        root = os.path.realpath(w)
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _check(path: str) -> str:
    if not path or not os.path.isabs(path):
        raise FileError("Path must be absolute")
    if not inside_workspace(path):
        raise FileError("Outside the configured workspaces (SIDUS_WORKSPACES)", 403)
    return os.path.realpath(path)


def listing(path: str) -> dict:
    real = _check(path)
    if not os.path.isdir(real):
        raise FileError("Not a folder", 404)
    entries = []
    try:
        with os.scandir(real) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                except OSError:
                    continue
                entries.append({"name": e.name, "dir": is_dir, "size": None if is_dir else st.st_size,
                                "mtime": st.st_mtime})
    except OSError as e:
        raise FileError(str(e))
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = os.path.dirname(real)
    return {"path": real, "parent": parent if parent != real and inside_workspace(parent) else None,
            "entries": entries[:2000], "truncated": len(entries) > 2000}


def download_path(path: str) -> str:
    real = _check(path)
    if not os.path.isfile(real):
        raise FileError("Not a file", 404)
    return real


_BAD = re.compile(r'[\x00-\x1f/\\]')


def clean_name(name: str) -> str:
    name = _BAD.sub("_", os.path.basename(name.replace("\\", "/"))).strip()
    if name in ("", ".", ".."):
        raise FileError("Bad file name")
    return name[:200]


def unique_target(folder: str, name: str) -> str:
    """folder/name, or folder/'name (2).ext' if that already exists."""
    target = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    n = 2
    while os.path.lexists(target):
        target = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return target


class Upload:
    """Writes to a hidden temp file, then renames into place so half-written files never show up."""

    def __init__(self, folder: str, name: str, overwrite: bool = False):
        self.folder = _check(folder)
        if not os.path.isdir(self.folder):
            raise FileError("Folder not found", 404)
        self.name = clean_name(name)
        self.overwrite = overwrite
        self.tmp = os.path.join(self.folder, f".sidus-upload-{uuid.uuid4().hex}.part")
        self.size = 0
        self.limit = settings.max_upload_mb * 1024 * 1024
        self.f = open(self.tmp, "wb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.limit:
            raise FileError(f"File is larger than {settings.max_upload_mb} MB (SIDUS_MAX_UPLOAD_MB)", 413)
        self.f.write(chunk)

    def finish(self) -> dict:
        self.f.close()
        target = os.path.join(self.folder, self.name) if self.overwrite else unique_target(self.folder, self.name)
        os.replace(self.tmp, target)
        return {"path": target, "name": Path(target).name, "size": self.size}

    def abort(self) -> None:
        try:
            self.f.close()
            os.unlink(self.tmp)
        except OSError:
            pass


# ---------- showing a file in the browser (the file viewer, agent links) ----------

_TEXT_TYPES = {"application/json", "application/xml", "application/x-yaml", "application/yaml", "application/toml",
               "application/x-sh", "application/javascript", "application/x-python-code", "application/sql"}
_HTML = {"text/html", "application/xhtml+xml", "image/svg+xml"}
_NATIVE = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "application/pdf")
# A page an agent wrote may run its scripts, but in an opaque origin: no Sidus cookies, no same-origin API calls.
SCRIPTS_CSP = "sandbox allow-scripts allow-popups allow-forms allow-modals allow-downloads"


def inline_response(path: str, scripts: bool = False, download: bool = False):
    """Text of any kind goes out as text/plain so the browser shows it. HTML and SVG stay sandboxed (scripts only
    when asked, and never with Sidus's origin); images and PDFs keep their type so the browser can render them."""
    from fastapi.responses import FileResponse
    kind, _ = mimetypes.guess_type(path)
    if kind is None or kind in _TEXT_TYPES or (kind.startswith("text/") and kind != "text/html"):
        kind = "text/plain; charset=utf-8"
    resp = FileResponse(path, media_type=kind, filename=Path(path).name,
                        content_disposition_type="attachment" if download else "inline")
    if kind in _HTML:
        resp.headers["Content-Security-Policy"] = SCRIPTS_CSP if scripts else "sandbox"
    elif not kind.startswith(_NATIVE):
        resp.headers["Content-Security-Policy"] = "sandbox"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"   # the file viewer frames HTML pages and PDFs
    resp.headers["Cache-Control"] = "no-store"   # agents rewrite files; always show the current one
    return resp
