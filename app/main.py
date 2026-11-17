r"""
Memories Renewed - File Helper

A small web service that NocoBase calls (workflow "HTTP request" node) to manage
customer order folders on the network share (Z:\).

Endpoints (all JSON; every endpoint except /health needs header X-Api-Key):
    GET  /health                 share reachable? standard folders present?
    GET  /stages                 list the standard stage folders
    POST /projects               create an order folder + _FINAL/_RAW/_TRACKER + stub file
    POST /projects/move          move an order folder from one stage to another
    GET|POST /properties         file count + total size of a folder
    GET|POST /list               files, sizes and modified dates in a folder

Safety rules:
    * Nothing outside SHARE_ROOT is ever touched ("..", absolute paths, etc. are rejected).
    * The standard top-level folders are never renamed, moved or deleted.
    * The helper never deletes anything.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration (environment variables)
# --------------------------------------------------------------------------- #
SHARE_ROOT = Path(os.environ.get("SHARE_ROOT", "/share"))
DISPLAY_ROOT = os.environ.get("DISPLAY_ROOT", "Z:\\")          # how paths are shown to people
API_KEY = os.environ.get("HELPER_API_KEY", "")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))
MAX_FOLDER_NAME = int(os.environ.get("MAX_FOLDER_NAME", "150"))
MAX_LIST_ENTRIES = int(os.environ.get("MAX_LIST_ENTRIES", "5000"))

STANDARD_DIRS = [
    "_Stage 01 - To Be Digitized",
    "_Stage 02 - Digitized",
    "_Stage 03 - Assigned For Editing",
    "_Stage 04 - To Be Reviewed",
    "_Stage 05 - To Be Output",
    "_Stage 06 - To Be Packaged",
    "_Stage 07 - To Be Invoiced",
    "_Stage 08 - To Be Paid",
    "_Stage 09 - To Be Shipped",
    "_Stage 10 - To Be Picked Up",
    "_Stage 11 - Archive",
    "__Inbox - To Do",
    "__Outbox - Done",
]
DEFAULT_STAGE = STANDARD_DIRS[0]
ORDER_SUBDIRS = ["_FINAL", "_RAW", "_TRACKER"]

log = logging.getLogger("file-helper")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Memories Renewed File Helper", version="1.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def require_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        raise HTTPException(500, "HELPER_API_KEY is not configured on the helper")
    if x_api_key != API_KEY:
        raise HTTPException(401, "Missing or wrong X-Api-Key header")


def root() -> Path:
    return SHARE_ROOT.resolve()


def display(p: Path) -> str:
    """Path as people see it, e.g. Z:\\_Stage 01 - To Be Digitized\\folder"""
    rel = p.resolve().relative_to(root())
    parts = [] if str(rel) == "." else list(rel.parts)
    base = DISPLAY_ROOT if DISPLAY_ROOT.endswith(("\\", "/")) else DISPLAY_ROOT + "\\"
    return base + "\\".join(parts)


def relpath(p: Path) -> str:
    rel = p.resolve().relative_to(root())
    return "" if str(rel) == "." else "/".join(rel.parts)


def safe_path(user_path: Optional[str]) -> Path:
    """Turn a path from NocoBase into a real path that is guaranteed to be inside the share.

    Accepts '' (the root), 'stage/folder', 'stage\\folder' or 'Z:\\stage\\folder'.
    """
    s = (user_path or "").strip().replace("\\", "/")
    disp = DISPLAY_ROOT.replace("\\", "/").rstrip("/")
    if disp and s.lower().startswith(disp.lower()):
        s = s[len(disp):]
    s = s.strip("/")
    if re.match(r"^[A-Za-z]:", s):
        raise HTTPException(400, f"Path must be on {DISPLAY_ROOT}")
    p = (root() / s).resolve()
    if p != root() and root() not in p.parents:
        raise HTTPException(400, "Path is outside the share")
    return p


def resolve_stage(value: Optional[str]) -> str:
    """Accept the full folder name, or a short form like '3', '03', 'Stage 03', 'inbox', 'outbox'."""
    if not value:
        return DEFAULT_STAGE
    v = value.strip().replace("\\", "/").strip("/")
    v_low = v.lower()
    for d in STANDARD_DIRS:
        if d.lower() == v_low:
            return d
    m = re.fullmatch(r"(?:_?stage\s*)?0*(\d{1,2})", v_low)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 11:
            return STANDARD_DIRS[n - 1]
    if "inbox" in v_low:
        return "__Inbox - To Do"
    if "outbox" in v_low:
        return "__Outbox - Done"
    raise HTTPException(400, f"Unknown stage '{value}'. Valid: {', '.join(STANDARD_DIRS)} (or 1-11, inbox, outbox)")


_SLASHES = re.compile(r"[/\\|]")              # / \ |  -> " - "
_DROP = re.compile(r'[<>:"?*\x00-\x1f]')         # removed entirely


def clean(part: Union[str, int, None], field: str, required: bool = True) -> str:
    """Make a value safe for a Windows folder name."""
    s = "" if part is None else str(part)
    s = _SLASHES.sub(" - ", s)
    s = _DROP.sub("", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"( - )+", " - ", s).strip(" -").rstrip(". ")
    if required and not s:
        raise HTTPException(400, f"'{field}' is required")
    return s


def parse_date(value: str) -> date:
    """Accept '2026-10-21' or an ISO timestamp (NocoBase datetime fields), in local time."""
    v = (value or "").strip()
    try:
        if len(v) == 10:
            return date.fromisoformat(v)
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(TZ)
        return dt.date()
    except ValueError:
        raise HTTPException(400, f"order_date '{value}' is not a valid date (use YYYY-MM-DD)")


def stat_tree(p: Path) -> dict:
    files = folders = size = 0
    newest = 0.0
    stack = [p]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            folders += 1
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            files += 1
                            size += st.st_size
                            newest = max(newest, st.st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return {"file_count": files, "folder_count": folders, "total_bytes": size,
            "total_size": human(size), "last_modified": iso(newest) if newest else None}


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


def find_order_folders(order_number: str) -> list[Path]:
    """All order folders (in any standard folder) named 'YYYY-MM-DD - <order> - ...'."""
    pat = re.compile(r"^\d{4}-\d{2}-\d{2} - " + re.escape(order_number) + r"( - |$)")
    hits = []
    for d in STANDARD_DIRS:
        sd = root() / d
        if not sd.is_dir():
            continue
        with os.scandir(sd) as it:
            for e in it:
                if e.is_dir() and pat.match(e.name):
                    hits.append(Path(e.path))
    return hits


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class CreateProject(BaseModel):
    order_date: str = Field(..., examples=["2026-10-21"])
    order_number: Union[str, int] = Field(..., examples=["21816"])
    last_name: str = Field(..., examples=["Ballenger"])
    first_name: Optional[str] = Field("", examples=["Noella"])
    description: Optional[str] = Field("", examples=["Slides"])
    stage: Optional[str] = Field(None, description="Defaults to _Stage 01 - To Be Digitized")


class MoveProject(BaseModel):
    folder_name: Optional[str] = Field(None, description="Order folder name (as returned by /projects)")
    order_number: Optional[Union[str, int]] = Field(None, description="Used to find the folder if folder_name is not given")
    from_stage: str
    to_stage: str


class PathRequest(BaseModel):
    path: str = ""
    recursive: bool = False


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    ok = root().is_dir()
    missing = [d for d in STANDARD_DIRS if not (root() / d).is_dir()] if ok else STANDARD_DIRS
    return {"ok": ok and not missing, "share_reachable": ok, "share_root": DISPLAY_ROOT,
            "missing_standard_folders": missing}


@app.get("/stages", dependencies=[Depends(require_key)])
def stages():
    return {"stages": STANDARD_DIRS, "default": DEFAULT_STAGE}


@app.post("/projects", dependencies=[Depends(require_key)])
def create_project(body: CreateProject):
    d = parse_date(body.order_date).isoformat()
    order = clean(body.order_number, "order_number")
    last = clean(body.last_name, "last_name")
    first = clean(body.first_name, "first_name", required=False)
    desc = clean(body.description, "description", required=False)
    stage = resolve_stage(body.stage)

    person = f"{last}, {first}" if first else last
    name = f"{d} - {order} - {person}"
    if desc:
        room = MAX_FOLDER_NAME - len(name) - 3
        if room > 0:
            name += " - " + desc[:room].rstrip(". ")

    stage_dir = root() / stage
    if not stage_dir.is_dir():
        raise HTTPException(503, f"Stage folder not found on share: {stage}")
    target = stage_dir / name

    existing = [p for p in find_order_folders(order) if p != target]
    if existing:
        raise HTTPException(409, f"A folder for order {order} already exists: {display(existing[0])}")

    created = not target.exists()
    for sub in ORDER_SUBDIRS:
        (target / sub).mkdir(parents=True, exist_ok=True)
    stub = target / "_FINAL" / f".{order} - {last}"
    if not stub.exists():
        stub.touch()

    log.info("create order=%s folder=%s created=%s", order, display(target), created)
    return {"created": created, "folder_name": name, "stage": stage,
            "path": display(target), "relative_path": relpath(target),
            "stub_file": stub.name}


@app.post("/projects/move", dependencies=[Depends(require_key)])
def move_project(body: MoveProject):
    src_stage = resolve_stage(body.from_stage)
    dst_stage = resolve_stage(body.to_stage)
    if src_stage == dst_stage:
        raise HTTPException(400, "from_stage and to_stage are the same")

    if body.folder_name:
        name = body.folder_name.strip()
        if "/" in name or "\\" in name or name in (".", "..") or name in STANDARD_DIRS:
            raise HTTPException(400, "folder_name must be just the order folder's name")
        src = root() / src_stage / name
    elif body.order_number not in (None, ""):
        order = clean(body.order_number, "order_number")
        hits = [p for p in find_order_folders(order) if p.parent.name == src_stage]
        if not hits:
            raise HTTPException(404, f"No folder for order {order} in {src_stage}")
        if len(hits) > 1:
            raise HTTPException(409, f"More than one folder for order {order} in {src_stage}")
        src = hits[0]
        name = src.name
    else:
        raise HTTPException(400, "Give folder_name or order_number")

    # Defense in depth: only ever move a folder that sits directly inside a stage folder.
    if src.resolve().parent != (root() / src_stage).resolve():
        raise HTTPException(400, "Can only move order folders that sit directly inside a stage folder")
    if not src.is_dir():
        raise HTTPException(404, f"Folder not found: {display(root() / src_stage)}\\{name}")
    dst_dir = root() / dst_stage
    if not dst_dir.is_dir():
        raise HTTPException(503, f"Stage folder not found on share: {dst_stage}")
    dst = dst_dir / name
    if dst.exists():
        raise HTTPException(409, f"Destination already exists: {display(dst)}")

    try:
        os.rename(src, dst)                     # instant on the same share
    except PermissionError:
        raise HTTPException(423, "Folder is in use (a file may be open on a PC). Close it and retry.")
    except OSError:
        try:
            shutil.move(str(src), str(dst))     # fallback: copy + remove
        except PermissionError:
            raise HTTPException(423, "Folder is in use (a file may be open on a PC). Close it and retry.")

    log.info("move folder=%s from=%s to=%s", name, src_stage, dst_stage)
    return {"moved": True, "folder_name": name, "from_stage": src_stage, "to_stage": dst_stage,
            "path": display(dst), "relative_path": relpath(dst)}


def _properties(path: str) -> dict:
    p = safe_path(path)
    if not p.is_dir():
        raise HTTPException(404, f"Folder not found: {path}")
    result = {"path": display(p), "relative_path": relpath(p), **stat_tree(p)}
    children = []
    with os.scandir(p) as it:
        for e in sorted(it, key=lambda e: e.name.lower()):
            if e.is_dir(follow_symlinks=False):
                s = stat_tree(Path(e.path))
                children.append({"name": e.name, "file_count": s["file_count"],
                                 "total_bytes": s["total_bytes"], "total_size": s["total_size"]})
    result["subfolders"] = children
    return result


@app.get("/properties", dependencies=[Depends(require_key)])
def properties_get(path: str = Query("")):
    return _properties(path)


@app.post("/properties", dependencies=[Depends(require_key)])
def properties_post(body: PathRequest):
    return _properties(body.path)


def _list(path: str, recursive: bool) -> dict:
    p = safe_path(path)
    if not p.is_dir():
        raise HTTPException(404, f"Folder not found: {path}")
    entries, truncated = [], False
    walker = p.rglob("*") if recursive else p.iterdir()
    for e in sorted(walker, key=lambda x: str(x).lower()):
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            break
        try:
            st = e.lstat()
        except OSError:
            continue
        is_dir = e.is_dir() and not e.is_symlink()
        entries.append({
            "name": e.name,
            "relative_path": str(e.relative_to(p)).replace(os.sep, "/"),
            "type": "folder" if is_dir else "file",
            "size_bytes": None if is_dir else st.st_size,
            "size": None if is_dir else human(st.st_size),
            "modified": iso(st.st_mtime),
        })
    files = [x for x in entries if x["type"] == "file"]
    return {"path": display(p), "relative_path": relpath(p), "count": len(entries),
            "file_count": len(files), "total_bytes": sum(x["size_bytes"] for x in files),
            "truncated": truncated, "entries": entries}


@app.get("/list", dependencies=[Depends(require_key)])
def list_get(path: str = Query(""), recursive: bool = Query(False)):
    return _list(path, recursive)


@app.post("/list", dependencies=[Depends(require_key)])
def list_post(body: PathRequest):
    return _list(body.path, body.recursive)
