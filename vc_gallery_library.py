#!/usr/bin/env python3
"""vc_gallery_library — read-only Library overlay over a project folder tree.

The Library is an *overlay*, not a store. It lists whatever is on disk every
time it is asked, keeps no database, and never moves, renames or writes a
file inside the project tree. The folder structure is the source of truth.

Project-agnostic model
----------------------
- A **root** is any folder (e.g. a client programme folder on a shared Drive).
- A **unit** is a folder whose children include at least two *stage* folders.
  Units are discovered, never named: an episode, an ad, a campaign — anything.
  If the root itself has stage folders, the root is the single unit.
- A **stage** is a child folder whose name starts with two digits and a
  separator, e.g. ``00_Project management`` … ``05_Archive``. Stage order is the
  numeric prefix, so any studio's numbering scheme works.
- Root children that are not units are listed as **other folders**.

Optional per-root rules live *in the tree itself*, so anyone who opens the
shared folder inherits them: ``<root>/LIBRARY.json``::

    {
      "version": 1,
      "full_cut": {
        "label": "Latest full cut",
        "folders": ["01_Ingest/Client footage"],   # checked in order, per unit
        "date_in_name": "MMDDYY"                    # or YYYYMMDD / YYYY-MM-DD / none
      },
      "skip": ["Adobe Premiere Pro*", "*.PRV"]      # extra fnmatch patterns
    }

Without LIBRARY.json the latest cut is the newest video in the unit's
deliverables stage (the stage whose name contains "deliver"), ranked by an
unambiguous ISO date in the filename, else by modified time.

Cloud-file safety: on Google Drive / iCloud File Provider mounts, files that
are not downloaded carry the ``SF_DATALESS`` flag. The Library reports it
(``local: false``) and never reads such files for thumbnails, so opening the
Library never pulls a whole shared drive down. A file is only downloaded when
the user explicitly opens it.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

LIBRARY_CONFIG_NAME = "LIBRARY.json"
SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)

STAGE_RE = re.compile(r"^(\d{2})[ _\-.]")

KIND_BY_EXT = {
    **{e: "image" for e in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp", ".heic")},
    **{e: "video" for e in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mxf")},
    **{e: "audio" for e in (".wav", ".mp3", ".aif", ".aiff", ".m4a", ".flac")},
    **{e: "project" for e in (".prproj", ".aep", ".drp", ".fcpxml", ".xml", ".edl", ".otio")},
    **{e: "doc" for e in (".md", ".pdf", ".txt", ".csv", ".docx", ".xlsx", ".gdoc", ".gsheet")},
}

# Generic editor/OS clutter. Projects add their own via LIBRARY.json "skip".
DEFAULT_SKIP = [
    ".*", "__MACOSX", "*.PRV", "*.prin", "*.pek", "*.cfa", "*.part",
    "Adobe Premiere Pro Auto-Save", "Adobe Premiere Pro*Previews",
    "Adobe Premiere Pro (Beta)*", ".thumb_cache", ".visual_chef",
]

# Whole-unit walk is bounded so a pathological tree cannot hang a request.
MAX_FILES_PER_UNIT = 20000
CACHE_TTL_SEC = 60.0

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _is_skipped(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _stage_prefix(name: str) -> Optional[int]:
    m = STAGE_RE.match(name)
    return int(m.group(1)) if m else None


def _subdirs(path: Path, skip: list[str]) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return sorted(
                (e for e in it if e.is_dir(follow_symlinks=False) and not _is_skipped(e.name, skip)),
                key=lambda e: _natural_key(e.name),
            )
    except OSError:
        return []


def _stage_dirs(path: Path, skip: list[str]) -> list[os.DirEntry]:
    return sorted(
        (e for e in _subdirs(path, skip) if _stage_prefix(e.name) is not None),
        key=lambda e: (_stage_prefix(e.name), _natural_key(e.name)),
    )


def is_unit(path: Path, skip: list[str]) -> bool:
    return len(_stage_dirs(path, skip)) >= 2


def date_from_name(name: str, pattern: Optional[str]) -> Optional[date]:
    """Parse a version date embedded in a filename. Returns the *last* valid
    match (names like ``ep3_final_061026`` put the date near the end)."""
    stem = Path(name).stem
    found: Optional[date] = None
    pats = []
    if pattern in (None, "", "auto"):
        pats = ["YYYY-MM-DD", "YYYYMMDD"]  # unambiguous only
    elif pattern.lower() != "none":
        pats = [pattern.upper()]
    for p in pats:
        if p == "YYYY-MM-DD":
            rx, order = r"(?<!\d)(20\d{2})[-_.](\d{2})[-_.](\d{2})(?!\d)", "ymd"
        elif p == "YYYYMMDD":
            rx, order = r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", "ymd"
        elif p == "MMDDYY":
            rx, order = r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", "mdy"
        elif p == "DDMMYY":
            rx, order = r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", "dmy"
        elif p == "YYMMDD":
            rx, order = r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", "ymd2"
        else:
            continue
        for m in re.finditer(rx, stem):
            a, b, c = (int(g) for g in m.groups())
            try:
                if order == "ymd":
                    d = date(a, b, c)
                elif order == "mdy":
                    d = date(2000 + c, a, b)
                elif order == "dmy":
                    d = date(2000 + c, b, a)
                else:
                    d = date(2000 + a, b, c)
            except ValueError:
                continue
            found = d
        if found:
            return found
    return found


def load_config(root: Path) -> dict:
    cfg_path = root / LIBRARY_CONFIG_NAME
    cfg: dict = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            cfg = {"_error": f"{LIBRARY_CONFIG_NAME} unreadable: {exc}"}
    cfg.setdefault("full_cut", {})
    cfg["_skip"] = DEFAULT_SKIP + list(cfg.get("skip") or [])
    cfg["_has_file"] = cfg_path.is_file()
    return cfg


def _file_record(entry: os.DirEntry, rel: str, date_pattern: Optional[str]) -> dict:
    st = entry.stat(follow_symlinks=False)
    ext = os.path.splitext(entry.name)[1].lower()
    flags = getattr(st, "st_flags", 0)
    name_date = date_from_name(entry.name, date_pattern)
    return {
        "name": entry.name,
        "rel": rel,
        "kind": KIND_BY_EXT.get(ext, "other"),
        "bytes": st.st_size,
        "modified": st.st_mtime,
        "name_date": name_date.isoformat() if name_date else None,
        "local": not (flags & SF_DATALESS),
    }


def _sort_stamp(rec: dict) -> float:
    if rec.get("name_date"):
        y, m, d = (int(x) for x in rec["name_date"].split("-"))
        return time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))
    return rec["modified"]


def _cached(key: tuple, fn):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------

def resolve_root(raw: str) -> Path:
    if not raw:
        raise ValueError("missing root")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("root must be an absolute path")
    p = p.resolve()
    if not p.is_dir():
        raise FileNotFoundError("root is not a folder")
    return p


def resolve_under(root: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``root``; refuse anything that escapes it."""
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError("path escapes the library root") from exc
    return target


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _walk_files(base: Path, root: Path, skip: list[str], date_pattern, limit: int) -> tuple[list[dict], bool]:
    out: list[dict] = []
    truncated = False
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            if _is_skipped(e.name, skip):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    out.append(_file_record(e, os.path.relpath(e.path, root), date_pattern))
                    if len(out) >= limit:
                        return out, True
            except OSError:
                continue
    return out, truncated


def _latest_cut(root: Path, unit_path: Path, cfg: dict) -> Optional[dict]:
    fc = cfg.get("full_cut") or {}
    pattern = fc.get("date_in_name")
    skip = cfg["_skip"]
    folders = fc.get("folders")
    if not folders:
        deliver = [e for e in _stage_dirs(unit_path, skip) if "deliver" in e.name.lower()]
        folders = [deliver[0].name] if deliver else []
    for sub in folders:
        base = unit_path / sub
        if not base.is_dir():
            continue
        files, _ = _walk_files(base, root, skip, pattern, 2000)
        vids = [f for f in files if f["kind"] == "video"]
        if vids:
            vids.sort(key=_sort_stamp, reverse=True)
            best = dict(vids[0])
            best["source_folder"] = sub
            best["others"] = [
                {k: v[k] for k in ("name", "rel", "bytes", "name_date", "modified", "local")}
                for v in vids[1:6]
            ]
            return best
    return None


def library_overview(root_raw: str) -> dict:
    """Units + per-stage counts + latest cut. Cheap enough for one screen."""
    root = resolve_root(root_raw)

    def build() -> dict:
        cfg = load_config(root)
        skip = cfg["_skip"]
        units, others = [], []
        candidates = [root] if is_unit(root, skip) else [Path(e.path) for e in _subdirs(root, skip)]
        for up in candidates:
            if not is_unit(up, skip):
                others.append({"name": up.name, "rel": os.path.relpath(up, root)})
                continue
            stages = []
            for s in _stage_dirs(up, skip):
                files, trunc = _walk_files(Path(s.path), root, skip, (cfg["full_cut"] or {}).get("date_in_name"), MAX_FILES_PER_UNIT)
                kinds: dict[str, int] = {}
                for f in files:
                    kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
                stages.append({
                    "name": s.name,
                    "prefix": _stage_prefix(s.name),
                    "rel": os.path.relpath(s.path, root),
                    "count": len(files),
                    "bytes": sum(f["bytes"] for f in files),
                    "kinds": kinds,
                    "truncated": trunc,
                })
            loose = [
                _file_record(e, os.path.relpath(e.path, root), (cfg["full_cut"] or {}).get("date_in_name"))
                for e in sorted(os.scandir(up), key=lambda e: _natural_key(e.name))
                if e.is_file(follow_symlinks=False) and not _is_skipped(e.name, skip)
            ]
            units.append({
                "name": up.name,
                "rel": os.path.relpath(up, root) if up != root else ".",
                "stages": stages,
                "loose": loose,
                "latest_cut": _latest_cut(root, up, cfg),
            })
        return {
            "root": str(root),
            "config": {
                "file": cfg["_has_file"],
                "error": cfg.get("_error"),
                "full_cut": cfg.get("full_cut"),
            },
            "units": units,
            "other_folders": others,
            "generated_at": time.time(),
        }

    return _cached(("overview", str(root)), build)


def library_folder(root_raw: str, rel: str) -> dict:
    """Every file under one folder (a stage or an 'other' folder), grouped by
    its immediate sub-folder so the column reads like the tree."""
    root = resolve_root(root_raw)
    base = resolve_under(root, rel or ".")
    if not base.is_dir():
        raise FileNotFoundError("not a folder")

    def build() -> dict:
        cfg = load_config(root)
        files, trunc = _walk_files(base, root, cfg["_skip"], (cfg["full_cut"] or {}).get("date_in_name"), MAX_FILES_PER_UNIT)
        groups: dict[str, list[dict]] = {}
        for f in files:
            inner = os.path.relpath(os.path.join(root, f["rel"]), base)
            head = os.path.dirname(inner)  # full sub-path, e.g. "Video/Upscaled"
            groups.setdefault(head, []).append(f)
        out = []
        for g in sorted(groups, key=lambda k: (k != "", _natural_key(k))):
            items = sorted(groups[g], key=lambda f: _natural_key(f["rel"]))
            out.append({"group": g, "files": items})
        return {"root": str(root), "rel": os.path.relpath(base, root), "groups": out,
                "count": len(files), "truncated": trunc}

    return _cached(("folder", str(root), str(base)), build)


def file_for_serving(root_raw: str, rel: str) -> Path:
    root = resolve_root(root_raw)
    p = resolve_under(root, rel)
    if not p.is_file():
        raise FileNotFoundError("not a file")
    return p


def is_local(path: Path) -> bool:
    try:
        return not (os.stat(path, follow_symlinks=False).st_flags & SF_DATALESS)
    except (OSError, AttributeError):
        return True
