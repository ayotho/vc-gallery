#!/usr/bin/env python3
"""vc_gallery_lib — shared schema, connection, classifier for Visual Chef Gallery v2.

The Gallery v2 stack replaces the per-image markdown sidecar review workflow
with a SQLite-backed local dashboard. This module owns:

- The SQLite schema (single-source-of-truth, idempotent migration)
- DB connection helper (foreign keys ON, WAL journal, busy timeout)
- Asset source-type classifier (generated / raw_manual / derived_crop / editorial)
- Status normalizer (collapses approved→accepted, redo→revise, null→review)
- Sidecar frontmatter parser (reuses the proven build_gallery_html.py pattern)
- File path hashing for thumbnail cache keys

No I/O against the gallery folder lives here — that belongs to vc_gallery_scan.
This module is imported by scan, import_sidecars, thumb, and serve.

Design constraints (locked by plan rippling-petting-micali):
- Stdlib + sqlite3 + PyYAML only. No Flask, no SQLAlchemy.
- DB per episode, written to a local config folder.
- Source folder is read-only — we never write sidecars or media from here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Folder-local state convention
# ---------------------------------------------------------------------------
# Every working folder is self-contained. Hidden state goes in a `.visual_chef`
# directory inside the folder itself. That way EP7, EP8, EP9 — or any other
# project — each carry their own DB / thumbs / audit log, no central registry.

STATE_DIRNAME = ".visual_chef"
DB_FILENAME = "visual_chef.db"
LOG_FILENAME = "visual_chef.jsonl"
THUMB_DIRNAME = ".thumb_cache"


def state_dir_for(folder: str | Path) -> Path:
    return Path(folder).expanduser().resolve() / STATE_DIRNAME


def db_path_for(folder: str | Path) -> Path:
    return state_dir_for(folder) / DB_FILENAME


def log_path_for(folder: str | Path) -> Path:
    return state_dir_for(folder) / LOG_FILENAME


def thumb_dir_for(folder: str | Path) -> Path:
    return state_dir_for(folder) / THUMB_DIRNAME


# ---------------------------------------------------------------------------
# Server-level config — remembers which folder was last opened
# ---------------------------------------------------------------------------

SERVER_CONFIG_DIR = Path.home() / ".config" / "visual_chef"
SERVER_CONFIG_PATH = SERVER_CONFIG_DIR / "server.json"
MAX_RECENT = 8


def load_server_config() -> dict:
    if not SERVER_CONFIG_PATH.exists():
        return {"current_folder": None, "recent_folders": []}
    try:
        return json.loads(SERVER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"current_folder": None, "recent_folders": []}


def save_server_config(cfg: dict) -> None:
    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def remember_folder(folder: str | Path) -> dict:
    """Mark `folder` as the current working folder and bump it to the top of recents."""
    p = str(Path(folder).expanduser().resolve())
    cfg = load_server_config()
    recents = [r for r in cfg.get("recent_folders", []) if r != p]
    recents.insert(0, p)
    cfg["current_folder"] = p
    cfg["recent_folders"] = recents[:MAX_RECENT]
    save_server_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Enums (locked vocabulary)
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

VALID_STATUSES = {
    "review", "accepted", "hero", "revise", "rejected", "alternate", "legacy",
    # draft lifecycle: draft → firing → review (success) or rejected (failure).
    # Fixes #18 — previously jumped straight to 'review' before wrapper exited.
    "draft", "firing",
}

# Aliases the canonical sidecar writer (write_companion_note.py) emits or
# that have appeared historically. Map them to our normalized set on import.
STATUS_ALIASES = {
    "approved": "accepted",
    "redo": "revise",
    "anchor": "hero",
    "unknown": "review",
    "": "review",
    None: "review",
}

VALID_SOURCE_TYPES = {
    "generated", "raw_manual", "derived_crop", "editorial",
    # draft rows are synthetic — no file on disk yet; payload stored in notes JSON
    "draft",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    file_path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER,
    width INTEGER,
    height INTEGER,
    duration_sec REAL,
    file_modified_at REAL,

    source_type TEXT NOT NULL DEFAULT 'raw_manual',
    has_sidecar INTEGER DEFAULT 0,
    sidecar_path TEXT,

    status TEXT NOT NULL DEFAULT 'review',
    shot_id TEXT,
    scene TEXT,
    model TEXT,
    workflow TEXT,
    pass_num INTEGER,
    variant TEXT,
    client TEXT,
    project TEXT,
    parent_filename TEXT,
    session TEXT,
    session_date TEXT,
    score REAL,

    notes TEXT DEFAULT '',
    tags_json TEXT,

    thumb_path TEXT,
    thumb_generated_at REAL,

    first_seen_at REAL DEFAULT (strftime('%s','now')),
    last_updated_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS prompts (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    prompt_text TEXT,
    refs_json TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    reviewer TEXT DEFAULT 'director',
    reviewed_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    provider TEXT,
    provider_job_id TEXT,
    source_url TEXT,
    cost_credits INTEGER
);

CREATE INDEX IF NOT EXISTS idx_assets_status  ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_shot    ON assets(shot_id);
CREATE INDEX IF NOT EXISTS idx_assets_scene   ON assets(scene);
CREATE INDEX IF NOT EXISTS idx_assets_source  ON assets(source_type);
CREATE INDEX IF NOT EXISTS idx_assets_model   ON assets(model);
CREATE INDEX IF NOT EXISTS idx_assets_filename ON assets(filename);
CREATE INDEX IF NOT EXISTS idx_assets_thumb   ON assets(thumb_path);
CREATE INDEX IF NOT EXISTS idx_assets_first_seen ON assets(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_assets_workflow ON assets(workflow);
CREATE INDEX IF NOT EXISTS idx_assets_media_type ON assets(media_type);
CREATE INDEX IF NOT EXISTS idx_reviews_asset  ON reviews(asset_id, reviewed_at DESC);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults and apply schema.

    `check_same_thread=False` lets a single connection be shared across the
    ThreadingHTTPServer worker threads. SQLite's internal locking + our WAL
    journal handle the actual concurrency safely for one writer at a time.
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(p),
        timeout=10.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        PRAGMA busy_timeout = 5000;
    """)
    conn.executescript(SCHEMA_SQL)
    # Migration 2026-05-15: backfill NULL first_seen_at on legacy rows so the
    # `recent` sort works. Pre-existing rows from before this column had
    # NULL values; SQLite's NULL ordering is implementation-defined which
    # quietly broke chronological sort. Backfill from last_updated_at →
    # file_modified_at → now. Idempotent: WHERE IS NULL no-ops on clean DBs.
    conn.execute(
        "UPDATE assets SET first_seen_at = COALESCE(last_updated_at, file_modified_at, strftime('%s','now')) "
        "WHERE first_seen_at IS NULL"
    )
    return conn


# ---------------------------------------------------------------------------
# Sidecar parsing
# ---------------------------------------------------------------------------

def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns ({}, text) if no frontmatter."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 4:]
    try:
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError:
        data = {}
    return data, body.strip()


def wikilink_target(value) -> str:
    """Extract the basename from a wikilink-shaped scalar like '[[file.png]]'."""
    if value is None or isinstance(value, list):
        return ""
    text = str(value).strip().strip("'\"")
    if text.startswith("[[") and "]]" in text:
        text = text[2:text.index("]]")]
    if "|" in text:
        text = text.split("|", 1)[0]
    if "#" in text:
        text = text.split("#", 1)[0]
    return text.strip()


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def normalize_status(raw) -> str:
    """Map any historical status value to our 7-state enum."""
    if raw is None:
        return "review"
    s = str(raw).strip().lower()
    if s in STATUS_ALIASES:
        return STATUS_ALIASES[s]
    if s in VALID_STATUSES:
        return s
    return "review"


def safe_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return ""
    return str(v).strip()


def safe_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def safe_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Classifier — figures out where an asset came from when there's no sidecar
# ---------------------------------------------------------------------------

_CROP_PATTERNS = [
    re.compile(r"^_?crop[_-]", re.I),
    re.compile(r"_crop_", re.I),
    re.compile(r"_train_", re.I),
    re.compile(r"_panel\d", re.I),
    re.compile(r"^slice[ _]?\d", re.I),
    re.compile(r"^frame[ _]?\d", re.I),
    re.compile(r"_grid_", re.I),
    re.compile(r"_master_grid", re.I),
]

_EDITORIAL_PATTERNS = [
    re.compile(r"^ep\d+\.(mp4|mov)$", re.I),
    re.compile(r"^\d{3,4}\.(mp4|mov)$", re.I),    # short numeric exports
    re.compile(r"_upscaled[_.]", re.I),
    re.compile(r"_proxy[_.]", re.I),
    re.compile(r"_export[_.]", re.I),
    re.compile(r"^export[_-]", re.I),
]

_GENERATED_HINTS = [
    re.compile(r"^hf_\d", re.I),                   # higgsfield uploads
    re.compile(r"^ayo_", re.I),                    # midjourney via ayo profile
    re.compile(r"^nbp_", re.I),                    # nano banana pro
    re.compile(r"^nb2_", re.I),                    # nano banana 2
    re.compile(r"^gp2_", re.I),                    # gpt image 2
    re.compile(r"^muapi_", re.I),                  # muapi
    re.compile(r"^seedance_", re.I),
    re.compile(r"^kling_", re.I),
    re.compile(r"^cs_sh\d", re.I),                 # createframe shot outputs
    re.compile(r"^multiangle_", re.I),
    re.compile(r"^storyboard_", re.I),
    re.compile(r"^cref_", re.I),
    re.compile(r"^soulcast_", re.I),
]


def classify_source_type(filename: str, has_sidecar: bool) -> str:
    """Return the source_type bucket for an asset.

    Order matters: crop/editorial patterns are more specific than generated-hint
    patterns, so we check them first even when a sidecar exists (a sidecar'd
    crop is still a crop). Files with sidecars otherwise default to 'generated'.
    """
    name = filename.strip()

    for pat in _CROP_PATTERNS:
        if pat.search(name):
            return "derived_crop"

    for pat in _EDITORIAL_PATTERNS:
        if pat.search(name):
            return "editorial"

    if has_sidecar:
        return "generated"

    for pat in _GENERATED_HINTS:
        if pat.search(name):
            return "generated"

    return "raw_manual"


# ---------------------------------------------------------------------------
# Hashing — stable cache key for thumbnails
# ---------------------------------------------------------------------------

def thumb_key(file_path: str) -> str:
    """SHA1 of the absolute path. Used as the thumbnail cache filename stem."""
    return hashlib.sha1(file_path.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Asset row construction — used by scanner + sidecar importer
# ---------------------------------------------------------------------------

def media_type_for(ext: str) -> Optional[str]:
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def extract_from_sidecar(meta: dict) -> dict:
    """Pull the canonical fields out of a parsed sidecar frontmatter dict."""
    hf_url = safe_str(meta.get("hf_job_url"))
    return {
        "status": normalize_status(meta.get("status")),
        "shot_id": safe_str(meta.get("shot_id")),
        "scene": safe_str(meta.get("scene")),
        "model": safe_str(meta.get("model")),
        "workflow": safe_str(meta.get("workflow")),
        "pass_num": safe_int(meta.get("pass")),
        "variant": safe_str(meta.get("variant")),
        "client": safe_str(meta.get("client")),
        "project": safe_str(meta.get("project") or meta.get("episode")),
        "parent_filename": wikilink_target(meta.get("parent")) or "",
        "session": safe_str(meta.get("session")),
        "session_date": safe_str(meta.get("session_date")),
        "score": safe_float(meta.get("score")),
        "prompt_text": safe_str(meta.get("prompt")),
        "refs": _extract_refs(meta.get("refs")),
        "hf_job_url": hf_url,
        "hf_job_id": _extract_hf_job_id(hf_url),
    }


def _extract_hf_job_id(url: str) -> str:
    """Pull the UUID from a Higgsfield asset URL."""
    if not url:
        return ""
    # Pattern: https://higgsfield.ai/asset/all/<uuid>
    m = re.search(r"/asset/(?:all/)?([0-9a-f-]{8,})", url)
    return m.group(1) if m else ""


def _extract_refs(refs) -> list[str]:
    if not refs:
        return []
    if isinstance(refs, list):
        out = []
        for r in refs:
            t = wikilink_target(r) if isinstance(r, str) and r.strip().startswith(("[[", "'[[")) else safe_str(r)
            if t:
                out.append(t)
        return out
    if isinstance(refs, str):
        t = wikilink_target(refs) or safe_str(refs)
        return [t] if t else []
    return []


# ---------------------------------------------------------------------------
# Direct upsert — used by the wrapper to write to the DB without a sidecar,
# and by the file watcher to add a single new file mid-session.
# ---------------------------------------------------------------------------

def upsert_asset_direct(
    conn: sqlite3.Connection,
    file_path: str | Path,
    metadata: dict,
    *,
    asset_id: int | None = None,
) -> tuple[int, str]:
    """Insert or update one asset row from a metadata dict.

    metadata keys (all optional unless noted):
      status, shot_id, scene, model, workflow, pass_num, variant,
      client, project, parent_filename, session, session_date, score,
      prompt_text, refs, hf_job_id, hf_job_url, has_sidecar, sidecar_path, notes.

    Handles assets + prompts + jobs in one transaction. Returns
    (asset_id, 'added' | 'updated' | 'mutated').

    `asset_id` kwarg (issue #27 — draft→fire single row):
      When given, UPDATE the row with that id IN PLACE — flip its file_path
      to the new location, set status, write metadata, no matter what the
      old file_path was. Used by the wrapper to mutate a draft row into a
      review row instead of inserting a duplicate. Returns ('mutated').

      The caller is responsible for passing a valid asset_id; if no row
      exists with that id we fall through to the path-keyed code path and
      a fresh INSERT (best-effort fallback so a stale asset_id from a
      backward-compat scenario doesn't break the fire).

      If another row in the DB already points at the new file_path (the
      scanner-race case), that ghost row is DELETED before the UPDATE so
      we never violate the file_path uniqueness constraint.
    """
    import json as _json

    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(str(p))
    stat = p.stat()
    ext = p.suffix.lower()
    media_type = media_type_for(ext)
    if media_type is None:
        raise ValueError(f"unsupported media extension: {ext}")

    has_sidecar = bool(metadata.get("has_sidecar", False))
    # Caller can override classification — useful when the wrapper writes a
    # gen directly to the DB and knows it's `generated` regardless of filename
    # pattern or sidecar presence.
    source_type = metadata.get("source_type") or classify_source_type(p.name, has_sidecar)
    if source_type not in VALID_SOURCE_TYPES:
        source_type = classify_source_type(p.name, has_sidecar)

    # Issue #58 — probe width/height/duration at write time so video metadata
    # is populated immediately, not deferred to a manual rescan. Uses the same
    # probe_media_dimensions from vc_gallery_scan (imported lazily to avoid
    # circular import at module level). Skip if caller already provided dims.
    _caller_w = metadata.get("width")
    _caller_h = metadata.get("height")
    _caller_d = metadata.get("duration_sec")
    if _caller_w is not None and _caller_h is not None:
        dims = {"width": _caller_w, "height": _caller_h, "duration_sec": _caller_d}
    else:
        from vc_gallery_scan import probe_media_dimensions as _probe
        dims = _probe(str(p))

    row = {
        "file_path": str(p),
        "filename": p.name,
        "media_type": media_type,
        "size_bytes": stat.st_size,
        "file_modified_at": stat.st_mtime,
        "source_type": source_type,
        "has_sidecar": 1 if has_sidecar else 0,
        "sidecar_path": metadata.get("sidecar_path") or None,
        "status": normalize_status(metadata.get("status", "review")),
        "shot_id": metadata.get("shot_id") or None,
        "scene": metadata.get("scene") or None,
        "model": metadata.get("model") or None,
        "workflow": metadata.get("workflow") or None,
        "pass_num": safe_int(metadata.get("pass_num")),
        "variant": metadata.get("variant") or None,
        "client": metadata.get("client") or None,
        "project": metadata.get("project") or None,
        "parent_filename": metadata.get("parent_filename") or None,
        "session": metadata.get("session") or None,
        "session_date": metadata.get("session_date") or None,
        "score": safe_float(metadata.get("score")),
        "notes": metadata.get("notes") or "",
        "width": dims["width"],
        "height": dims["height"],
        "duration_sec": dims["duration_sec"],
    }

    # Issue #27 — asset_id-keyed mutate path for draft→fire. Server passes the
    # draft's asset_id; we flip it in place to the real file_path + review
    # status. Preserves the draft's id (history continuity) and avoids the
    # 2-row split.
    if asset_id is not None:
        existing_by_id = conn.execute(
            "SELECT id FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if existing_by_id is not None:
            # If another row owns the target file_path (scanner race or stale
            # ghost from a prior fire), drop it BEFORE the UPDATE so the
            # uniqueness constraint stays clean. The wrapper's row is the truth.
            conn.execute(
                "DELETE FROM assets WHERE file_path = ? AND id != ?",
                (row["file_path"], asset_id),
            )
            cols = list(row.keys())
            set_clause = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(
                f"UPDATE assets SET {set_clause}, last_updated_at = strftime('%s','now') WHERE id = ?",
                [row[c] for c in cols] + [asset_id],
            )
            action = "mutated"
            # Fall through to prompt/jobs upsert below
        else:
            # Stale asset_id (the draft was deleted while the wrapper was
            # running?). Fall back to path-keyed behavior.
            asset_id = None

    if asset_id is None:
        existing = conn.execute(
            "SELECT id FROM assets WHERE file_path = ?", (row["file_path"],)
        ).fetchone()

        if existing is None:
            cols = list(row.keys())
            placeholders = ", ".join("?" for _ in cols)
            cur = conn.execute(
                f"INSERT INTO assets ({', '.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
            asset_id = cur.lastrowid
            action = "added"
        else:
            asset_id = existing["id"]
            cols = list(row.keys())
            set_clause = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(
                f"UPDATE assets SET {set_clause}, last_updated_at = strftime('%s','now') WHERE id = ?",
                [row[c] for c in cols] + [asset_id],
            )
            action = "updated"

    prompt_text = metadata.get("prompt_text") or ""
    refs = metadata.get("refs") or []
    if prompt_text or refs:
        conn.execute(
            """INSERT INTO prompts (asset_id, prompt_text, refs_json)
               VALUES (?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                   prompt_text = excluded.prompt_text,
                   refs_json   = excluded.refs_json""",
            (asset_id, prompt_text, _json.dumps(refs, ensure_ascii=False)),
        )

    hf_url = metadata.get("hf_job_url") or ""
    hf_id = metadata.get("hf_job_id") or ""
    if hf_url:
        conn.execute(
            """INSERT INTO jobs (asset_id, provider, provider_job_id, source_url)
               VALUES (?, 'higgsfield', ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                   provider = excluded.provider,
                   provider_job_id = excluded.provider_job_id,
                   source_url = excluded.source_url""",
            (asset_id, hf_id, hf_url),
        )

    return asset_id, action
