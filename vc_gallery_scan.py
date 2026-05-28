#!/usr/bin/env python3
"""vc_gallery_scan — walk a working gallery folder and upsert assets into SQLite.

Usage:
    python3 vc_gallery_scan.py \\
        --source "/Users/ayo/Desktop/Client/Dave/BTW/EP8" \\
        --db    "clients/BTW_Documentary/Projects/Episode_8/production/config/visual_chef.db"

Options:
    --recurse        Walk subdirectories too (default: top-level only)
    --dry-run        Report counts without writing to DB
    --quiet          Suppress per-file logging

Behavior:
- Identity is the absolute file_path. Re-running scan upserts: only writes
  when size_bytes or file_modified_at has changed.
- Sidecar pairing is by stem match: SH120A_v01.png <-> SH120A_v01.md.
- Frontmatter fields are cached into the assets row for fast filter queries.
- Prompt body + refs land in the `prompts` table (separate, large fields).
- Source_type is classified by filename pattern (see vc_gallery_lib.classify_source_type).
- Every meaningful change is appended to visual_chef.jsonl alongside the DB,
  via the existing flock-safe jsonl_append helper.

This script is read-only against the source folder. It never modifies media
or sidecars on disk.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Local imports — sibling utilities in arsenal/00-utilities/
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_lib as lib  # noqa: E402
import vc_gallery_obs as obs_mod  # noqa: E402
from jsonl_append import append_jsonl  # noqa: E402


# Columns we upsert into the assets table (excluding id/timestamps).
# Patch 2026-05-14: added width, height, duration_sec — schema had them since
# day one but nothing wrote them, so every drawer showed "? × ?". Probed via
# ffprobe inside _row_for. Backfilled for existing rows via vc_gallery_backfill_media_probe.py.
ASSET_COLUMNS = [
    "file_path", "filename", "media_type",
    "size_bytes", "file_modified_at",
    "source_type", "has_sidecar", "sidecar_path",
    "status", "shot_id", "scene", "model", "workflow",
    "pass_num", "variant", "client", "project",
    "parent_filename", "session", "session_date", "score",
    "width", "height", "duration_sec",
    "stack_id", "has_audio",
]

import re as _re

# Regex for extracting shot_id from filenames.
# Matches patterns like SH450, SH1740A, sh120b at the start of the filename
# (before the first underscore or other separator).
# Case-insensitive. Captures the full shot token including optional letter suffix.
_SHOT_ID_RE = _re.compile(r'(?:^|[_\-\s])(SH\d+[A-Z]?)(?=[_\-\s.]|$)', _re.IGNORECASE)


def extract_shot_id(filename: str) -> str | None:
    """Extract a shot_id like 'SH450' or 'SH1740A' from a filename.
    Returns the uppercased shot_id or None if no match."""
    m = _SHOT_ID_RE.search(filename)
    return m.group(1).upper() if m else None


def probe_media_dimensions(media_path) -> dict:
    """Run ffprobe and return {width, height, duration_sec}.

    All three default to None on probe failure. ffprobe handles PNG/JPG/WebP
    /GIF as single-frame streams, so the same code path covers images + video.

    Patch 2026-05-14: addresses the "SIZE ? × ?" complaint. Cheap (<200ms per
    file on local SSD), so we do it at scan time. Aborts after 5s wall-clock
    to keep a single bad file from stalling a 2k-file rescan.
    """
    import subprocess as _subprocess
    import json as _json
    out = {"width": None, "height": None, "duration_sec": None}
    p = Path(media_path)
    # Skip 0-byte files and partial-download .tmp.* fragments — same skip
    # the rest of the scanner uses.
    try:
        st = p.stat()
        if st.st_size == 0 or ".tmp." in p.name:
            return out
    except OSError:
        return out
    try:
        res = _subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json",
                str(p),
            ],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0:
            return out
        data = _json.loads(res.stdout)
        stream = (data.get("streams") or [{}])[0]
        out["width"] = int(stream["width"]) if stream.get("width") else None
        out["height"] = int(stream["height"]) if stream.get("height") else None
        # Try stream duration first (works for video); fall back to format duration
        dur = stream.get("duration")
        if not dur:
            dur = (data.get("format") or {}).get("duration")
        if dur:
            try:
                d = float(dur)
                out["duration_sec"] = d if d > 0 else None
            except (TypeError, ValueError):
                pass
    except (_subprocess.TimeoutExpired, _subprocess.SubprocessError, _json.JSONDecodeError, FileNotFoundError):
        # ffprobe missing on this machine — log once at first miss elsewhere
        return out
    return out


def probe_has_audio(media_path) -> bool | None:
    """Check whether a media file contains an audio stream via ffprobe.
    Returns True/False, or None on probe failure."""
    import subprocess as _subprocess
    p = Path(media_path)
    try:
        if p.stat().st_size == 0 or ".tmp." in p.name:
            return None
    except OSError:
        return None
    if p.suffix.lower() not in lib.VIDEO_EXTS:
        return None
    try:
        res = _subprocess.run(
            ['ffprobe', '-v', 'quiet', '-select_streams', 'a:0',
             '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', str(p)],
            capture_output=True, text=True, timeout=10,
        )
        return bool(res.stdout.strip())
    except (_subprocess.TimeoutExpired, _subprocess.SubprocessError, FileNotFoundError):
        return None


def _iter_media_files(source: Path, recurse: bool):
    """Yield Path objects for media files in source.

    Skips 0-byte files — they're placeholder reservations from the wrapper's
    O_CREAT|O_EXCL claim, not real assets. Picking them up pollutes the
    dashboard with empty cards. The wrapper unlinks them on its own failure
    paths; vc_gallery_cleanup.py sweeps any stragglers.
    """
    iterator = source.rglob("*") if recurse else source.iterdir()
    for p in iterator:
        if not p.is_file():
            continue
        if p.suffix.lower() not in lib.MEDIA_EXTS:
            continue
        try:
            if p.stat().st_size == 0:
                continue
        except OSError:
            continue
        # Skip wrapper's `.tmp` download files (real content lands on rename)
        if p.name.endswith(".tmp") or ".tmp." in p.name:
            continue
        # Skip vc-pipeline conform intermediates — Premiere/Topaz writes them
        # then deletes after consume; if the scanner picks them up we get
        # zombie DB rows pointing at vanished files. Plan-agent audit
        # 2026-05-14 found 12+ such zombies in EP8.
        if p.name.startswith("temp_conform_"):
            continue
        yield p


def _pair_sidecar(media: Path) -> Path | None:
    """Return the sidecar path if a `<stem>.md` exists next to the media."""
    candidate = media.with_suffix(".md")
    return candidate if candidate.exists() else None


def _row_for(media: Path, sidecar: Path | None) -> tuple[dict, dict | None]:
    """Build the assets row (dict) and the optional prompts row for this media."""
    stat = media.stat()
    has_sidecar = sidecar is not None
    sidecar_data = {}
    if sidecar is not None:
        try:
            text = sidecar.read_text(errors="ignore")
            meta, _ = lib.split_frontmatter(text)
            sidecar_data = lib.extract_from_sidecar(meta)
        except OSError:
            sidecar_data = {}

    # Width/height/duration are probed LAZILY in _upsert_asset, not here —
    # otherwise every startup re-ffprobes 2000+ files even when nothing
    # changed (~80s of wasted CPU per server restart). _upsert_asset has the
    # existing-row context to decide if probing is necessary.
    row = {
        "file_path": str(media.resolve()),
        "filename": media.name,
        "media_type": lib.media_type_for(media.suffix),
        "size_bytes": stat.st_size,
        "file_modified_at": stat.st_mtime,
        "source_type": lib.classify_source_type(media.name, has_sidecar),
        "has_sidecar": 1 if has_sidecar else 0,
        "sidecar_path": str(sidecar.resolve()) if sidecar else None,
        "status": sidecar_data.get("status", "review"),
        "shot_id": sidecar_data.get("shot_id") or extract_shot_id(media.name),
        "scene": sidecar_data.get("scene") or None,
        "model": sidecar_data.get("model") or None,
        "workflow": sidecar_data.get("workflow") or None,
        "pass_num": sidecar_data.get("pass_num"),
        "variant": sidecar_data.get("variant") or None,
        "client": sidecar_data.get("client") or None,
        "project": sidecar_data.get("project") or None,
        "parent_filename": sidecar_data.get("parent_filename") or None,
        "session": sidecar_data.get("session") or None,
        "session_date": sidecar_data.get("session_date") or None,
        "score": sidecar_data.get("score"),
        # Placeholders — filled in by _upsert_asset only when needed
        "width": None,
        "height": None,
        "duration_sec": None,
        "stack_id": None,
        "has_audio": None,
    }

    prompt_row = None
    if sidecar_data.get("prompt_text") or sidecar_data.get("refs"):
        prompt_row = {
            "prompt_text": sidecar_data.get("prompt_text") or "",
            "refs_json": json.dumps(sidecar_data.get("refs") or [], ensure_ascii=False),
        }
    # Pass through the Higgsfield link out-of-band (not a column on assets)
    row["_hf_job_url"] = sidecar_data.get("hf_job_url", "")
    row["_hf_job_id"] = sidecar_data.get("hf_job_id", "")
    return row, prompt_row


def _existing_fingerprint(conn, file_path: str) -> tuple | None:
    """Return (id, size_bytes, file_modified_at, source_type, model, width, height, duration_sec, stack_id, has_audio)
    for a known asset, else None.
    """
    cur = conn.execute(
        "SELECT id, size_bytes, file_modified_at, source_type, model, "
        "width, height, duration_sec, stack_id, has_audio FROM assets WHERE file_path = ?",
        (file_path,),
    )
    r = cur.fetchone()
    return tuple(r) if r else None


def _find_renamed_ghost(conn, gallery_dir: str, size_bytes: int, mtime: float) -> int | None:
    """Issue #33 — detect a renamed-on-disk file before inserting a duplicate row.

    Returns the asset id of an existing row that:
      - Lives in the same gallery directory (matched by file_path prefix),
      - Has the same `size_bytes`,
      - Has a `file_modified_at` within 1 second of the new file's mtime
        (POSIX `os.rename` preserves mtime; allow tiny float drift),
      - Has a `file_path` that NO LONGER EXISTS on disk (ghost row).

    Only returns when EXACTLY ONE row matches — ambiguous matches (multiple
    candidates) fall through to normal insert because guessing would risk
    overwriting the wrong row's metadata.

    Caller treats a returned id as "rename detected — UPDATE this row's
    file_path/filename/sidecar_path instead of inserting a fresh row".
    """
    # Same-gallery filter: file_path starts with the gallery dir + sep.
    # Cheap LIKE for the prefix; the exists-check below does the heavy lifting.
    sep = os.sep
    prefix = gallery_dir.rstrip(sep) + sep
    cur = conn.execute(
        "SELECT id, file_path, file_modified_at FROM assets "
        "WHERE size_bytes = ? AND file_path LIKE ? "
        "AND abs(file_modified_at - ?) < 1.0",
        (size_bytes, prefix + "%", mtime),
    )
    candidates = []
    for r in cur:
        # Ghost-check: file_path no longer exists on disk
        if not Path(r["file_path"]).exists():
            candidates.append(r["id"])
            if len(candidates) > 1:
                # Ambiguous — bail out, fall through to normal insert
                return None
    return candidates[0] if len(candidates) == 1 else None


def _upsert_asset(conn, row: dict) -> tuple[int, str]:
    """Insert or update an asset. Returns (asset_id, 'added' | 'updated' | 'unchanged').

    Probes width/height/duration via ffprobe ONLY when:
      - the file is new (no existing row), OR
      - the file changed (size/mtime differ from DB), OR
      - the existing row has NULL dimensions (legacy / backfill miss)

    For the steady state — server restart on an already-scanned gallery —
    we skip the probe entirely. Startup time drops from ~80s to <1s.
    """
    existing = _existing_fingerprint(conn, row["file_path"])
    if existing is None:
        gallery_dir = str(Path(row["file_path"]).parent)

        # Issue #33 — check if this is a renamed version of a row we already
        # have. POSIX rename preserves size+mtime, so a ghost row with the
        # same fingerprint AND a stale file_path is almost certainly the same
        # logical asset renamed on disk. Reconciling preserves status/scene/
        # sidecar metadata instead of creating a duplicate.
        ghost_id = _find_renamed_ghost(
            conn, gallery_dir, row["size_bytes"], row["file_modified_at"]
        )
        if ghost_id is not None:
            conn.execute(
                """UPDATE assets
                   SET file_path = ?, filename = ?, sidecar_path = ?, has_sidecar = ?,
                       size_bytes = ?, file_modified_at = ?, thumb_path = NULL,
                       last_updated_at = strftime('%s','now')
                   WHERE id = ?""",
                (row["file_path"], row["filename"], row["sidecar_path"],
                 row["has_sidecar"], row["size_bytes"], row["file_modified_at"],
                 ghost_id),
            )
            _upsert_job(conn, ghost_id, row)
            return ghost_id, "renamed"

        # Issue #27 (scanner debounce) — if a draft/firing row with the same
        # filename already exists in this gallery, the wrapper owns this
        # file's eventual row. Normally the wrapper's `_write_db_row` mutates
        # that row to status=review when the fire completes. If we observe
        # the real file on disk AND the row is still in 'firing' status, the
        # fire completed but the wrapper never finished its DB write
        # (timeout, crash, container killed, etc). Heal in-place by adopting
        # the firing row: point its file_path at the real file, flip status
        # to review, probe dimensions. Prevents the "stuck Cooking…" card
        # the director was seeing for fired-and-rendered assets.
        owning_draft = conn.execute(
            "SELECT id, status FROM assets "
            "WHERE filename = ? AND status IN ('draft','firing') "
            "AND file_path LIKE ?",
            (row["filename"], gallery_dir.rstrip(os.sep) + os.sep + "%"),
        ).fetchone()
        if owning_draft is not None:
            if owning_draft["status"] == "firing":
                dims = probe_media_dimensions(row["file_path"])
                conn.execute(
                    """UPDATE assets SET
                        file_path = ?, status = 'review',
                        size_bytes = ?, file_modified_at = ?,
                        width = ?, height = ?, duration_sec = ?,
                        sidecar_path = ?, has_sidecar = ?,
                        thumb_path = NULL,
                        last_updated_at = strftime('%s','now')
                       WHERE id = ?""",
                    (row["file_path"], row["size_bytes"], row["file_modified_at"],
                     dims["width"], dims["height"], dims["duration_sec"],
                     row["sidecar_path"], row["has_sidecar"], owning_draft["id"]),
                )
                _upsert_job(conn, owning_draft["id"], row)
                return owning_draft["id"], "updated"
            return owning_draft["id"], "deferred"

        # New file → probe dimensions + audio before insert.
        dims = probe_media_dimensions(row["file_path"])
        row["width"] = dims["width"]
        row["height"] = dims["height"]
        row["duration_sec"] = dims["duration_sec"]
        if row["has_audio"] is None:
            ha = probe_has_audio(row["file_path"])
            row["has_audio"] = (1 if ha else 0) if ha is not None else None
        # Compute stack_id from _v<N> suffix
        base, ver = lib.strip_variant_suffix(Path(row["file_path"]).stem)
        if ver is not None:
            row["stack_id"] = base
        cols = ", ".join(ASSET_COLUMNS)
        placeholders = ", ".join("?" for _ in ASSET_COLUMNS)
        values = [row[c] for c in ASSET_COLUMNS]
        cur = conn.execute(
            f"INSERT INTO assets ({cols}) VALUES ({placeholders})",
            values,
        )
        asset_id = cur.lastrowid
        _upsert_job(conn, asset_id, row)
        return asset_id, "added"

    (asset_id, old_size, old_mtime, old_source_type, old_model,
     old_w, old_h, old_dur, old_stack_id, old_has_audio) = existing

    # Don't let a sidecar-driven re-scan downgrade a `generated` row the
    # wrapper wrote directly. If the existing row already has rich metadata
    # (source_type=generated AND a model), trust it over filename heuristics.
    if old_source_type == "generated" and old_model and row["source_type"] != "generated":
        row["source_type"] = "generated"

    unchanged = (old_size == row["size_bytes"]
                 and abs((old_mtime or 0) - row["file_modified_at"]) < 1.0)

    # Preserve existing dimensions on unchanged files. Probe lazily if NULL.
    if unchanged:
        backfill_sets = []
        backfill_vals = []
        if old_w is None or old_h is None:
            dims = probe_media_dimensions(row["file_path"])
            if dims["width"] is not None or dims["height"] is not None:
                backfill_sets.extend(["width = ?", "height = ?", "duration_sec = COALESCE(?, duration_sec)"])
                backfill_vals.extend([dims["width"], dims["height"], dims["duration_sec"]])
        if old_stack_id is None:
            base, ver = lib.strip_variant_suffix(Path(row["file_path"]).stem)
            if ver is not None:
                backfill_sets.append("stack_id = ?")
                backfill_vals.append(base)
        if old_has_audio is None and Path(row["file_path"]).suffix.lower() in lib.VIDEO_EXTS:
            ha = probe_has_audio(row["file_path"])
            if ha is not None:
                backfill_sets.append("has_audio = ?")
                backfill_vals.append(1 if ha else 0)
        if backfill_sets:
            backfill_vals.append(asset_id)
            conn.execute(
                f"UPDATE assets SET {', '.join(backfill_sets)} WHERE id = ?",
                backfill_vals,
            )
        _upsert_job(conn, asset_id, row)
        return asset_id, "unchanged"

    # File changed → probe + full update.
    dims = probe_media_dimensions(row["file_path"])
    row["width"] = dims["width"]
    row["height"] = dims["height"]
    row["duration_sec"] = dims["duration_sec"]
    if row["has_audio"] is None:
        ha = probe_has_audio(row["file_path"])
        row["has_audio"] = (1 if ha else 0) if ha is not None else None
    if row["stack_id"] is None:
        base, ver = lib.strip_variant_suffix(Path(row["file_path"]).stem)
        if ver is not None:
            row["stack_id"] = base

    set_clause = ", ".join(f"{c} = ?" for c in ASSET_COLUMNS)
    values = [row[c] for c in ASSET_COLUMNS] + [asset_id]
    conn.execute(
        f"UPDATE assets SET {set_clause}, last_updated_at = strftime('%s','now') WHERE id = ?",
        values,
    )
    _upsert_job(conn, asset_id, row)
    return asset_id, "updated"


def _upsert_job(conn, asset_id: int, row: dict) -> None:
    """Populate the jobs table for assets with a Higgsfield link."""
    url = row.get("_hf_job_url") or ""
    job_id = row.get("_hf_job_id") or ""
    if not url:
        return
    conn.execute(
        """INSERT INTO jobs (asset_id, provider, provider_job_id, source_url)
           VALUES (?, 'higgsfield', ?, ?)
           ON CONFLICT(asset_id) DO UPDATE SET
               provider = excluded.provider,
               provider_job_id = excluded.provider_job_id,
               source_url = excluded.source_url""",
        (asset_id, job_id, url),
    )


def _upsert_prompt(conn, asset_id: int, prompt_row: dict | None, has_sidecar: bool = False) -> None:
    """Insert/update or DELETE the prompts row for one asset.

    CRITICAL PATCH 2026-05-14 — was the cause of every wrapper-fired asset
    showing empty References + empty Prompt:

    Previously, this function DELETE'd from `prompts` whenever `prompt_row is
    None`. The scanner builds `prompt_row` from a `.md` sidecar; if the file
    has no sidecar, `prompt_row` is None — but the wrapper writes prompts
    directly via `upsert_asset_direct` (under the 2026-05-13 skip_sidecar=True
    policy). So every rescan blew away the wrapper-written prompts + refs.

    Worse, this happened even on `unchanged` actions — just running `scan`
    on a quiet directory was destructive.

    Fix: only DELETE when a sidecar EXISTED and is now the source of truth
    (i.e. director removed the .md and we should mirror that). Otherwise, if
    there's no sidecar prompt data to write, leave any wrapper-written row
    alone. This unblocks bugs from issue #19 (refs missing) without forcing
    a backfill — going forward, prompts simply stop being wiped.
    """
    if prompt_row is None:
        if has_sidecar:
            # Sidecar exists but carries no prompt/refs → mirror by clearing.
            conn.execute("DELETE FROM prompts WHERE asset_id = ?", (asset_id,))
        # No sidecar → leave wrapper-written prompts untouched.
        return
    conn.execute(
        """INSERT INTO prompts (asset_id, prompt_text, refs_json)
           VALUES (?, ?, ?)
           ON CONFLICT(asset_id) DO UPDATE SET
               prompt_text = excluded.prompt_text,
               refs_json   = excluded.refs_json""",
        (asset_id, prompt_row["prompt_text"], prompt_row["refs_json"]),
    )


def _reconcile_renames(conn, source: Path, log_target: Path) -> int:
    """Detect renames BEFORE the upsert pass to prevent dupe-row pollution.

    For each row whose file_path is missing on disk:
      - If a file with the same filename exists in the gallery, AND
      - That file has same size_bytes, AND
      - That filename's basename is not already pointed at by another row,
      → update the row's file_path to the new location (preserves status, model,
        notes, etc) and skip the upsert pass for that file.

    Conservative: only fires when there's a single unambiguous match. Logs every
    decision to the event log so the director can audit later.
    """
    renames = 0
    source_str = str(source.resolve())
    # Exclude drafts AND firings — they use synthetic .drafts/*.draft.json paths
    # and must not be touched by rename reconciliation. The wrapper owns 'firing'
    # rows exclusively until its _write_db_row mutates them to status='review'
    # via the asset_id-keyed path (issue #27).
    cur = conn.execute(
        "SELECT id, filename, file_path, size_bytes FROM assets WHERE file_path LIKE ? AND status NOT IN ('draft','firing')",
        (f"{source_str}%",),
    )
    db_rows = cur.fetchall()

    # Build {filename: row} for quick lookup
    by_filename = {}
    missing_rows = []
    for r in db_rows:
        by_filename.setdefault(r["filename"], []).append(r)
        if not Path(r["file_path"]).exists():
            missing_rows.append(r)

    # Build {filename: Path} for on-disk files NOT yet in DB
    db_paths = {r["file_path"] for r in db_rows}
    on_disk_unmatched = {}
    for p in source.iterdir():
        if not p.is_file() or p.suffix.lower() not in lib.MEDIA_EXTS:
            continue
        try:
            if p.stat().st_size == 0:
                continue
        except OSError:
            continue
        if str(p.resolve()) not in db_paths:
            on_disk_unmatched.setdefault(p.name, []).append(p)

    for missing in missing_rows:
        candidates = on_disk_unmatched.get(missing["filename"], [])
        if len(candidates) != 1:
            continue  # ambiguous or no match — leave for manual /api/rename
        new_path = candidates[0]
        try:
            new_size = new_path.stat().st_size
        except OSError:
            continue
        if missing["size_bytes"] and new_size != missing["size_bytes"]:
            # Size mismatch — file content actually changed, not just renamed
            continue
        # Apply the rename
        conn.execute(
            "UPDATE assets SET file_path = ?, thumb_path = NULL, last_updated_at = strftime('%s','now') WHERE id = ?",
            (str(new_path.resolve()), missing["id"]),
        )
        renames += 1
        obs_mod.record_event(
            log_target, "scan.rename_reconciled", source="scan",
            asset_id=missing["id"],
            filename=missing["filename"],
            old_file_path=missing["file_path"],
            new_file_path=str(new_path.resolve()),
        )
    return renames


def scan(
    source: Path,
    db_path: Path,
    *,
    recurse: bool = False,
    dry_run: bool = False,
    quiet: bool = True,
    log_path: Path | None = None,
) -> dict:
    """Walk the source folder and upsert every media file."""
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"source folder not found: {source}")

    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "renamed": 0, "deferred": 0}
    started = time.time()

    if dry_run:
        # Dry-run: walk only, no DB writes.
        for media in _iter_media_files(source, recurse):
            counts["added"] += 1  # treat every file as "would be added/updated"
        counts["elapsed_sec"] = round(time.time() - started, 2)
        return counts

    conn = lib.connect(db_path)
    log_target = log_path or (db_path.parent / "visual_chef.jsonl")

    try:
        conn.execute("BEGIN")
        # Pre-pass: reconcile renames before walking files, so the upsert
        # loop doesn't insert a fresh dupe row for the renamed file.
        if not recurse:  # rename detection only at the top level for now
            counts["renamed"] = _reconcile_renames(conn, source, log_target)

        for media in _iter_media_files(source, recurse):
            try:
                row, prompt_row = _row_for(media, _pair_sidecar(media))
            except OSError as e:
                counts["skipped"] += 1
                if not quiet:
                    print(f"SKIP {media.name}: {e}", file=sys.stderr)
                continue

            if row["media_type"] is None:
                counts["skipped"] += 1
                continue

            asset_id, action = _upsert_asset(conn, row)
            # has_sidecar flag controls whether _upsert_prompt is allowed to
            # DELETE existing prompts when prompt_row is None. Wrapper-written
            # prompts MUST survive scans of files that have no sidecar.
            _upsert_prompt(conn, asset_id, prompt_row, has_sidecar=bool(row.get("has_sidecar")))
            counts[action] += 1

            if action != "unchanged":
                if not quiet:
                    print(f"{action:>9}  {row['filename']}")
                obs_mod.record_event(
                    log_target, f"scan.{action}", source="scan",
                    asset_id=asset_id,
                    file_path=row["file_path"],
                    status=row["status"],
                    source_type=row["source_type"],
                )

        orphan_drafts = conn.execute(
            "SELECT id, file_path FROM assets WHERE status = 'draft' AND file_path LIKE ?",
            (str(source.resolve()) + "%",),
        ).fetchall()
        orphan_count = 0
        for od in orphan_drafts:
            if not Path(od["file_path"]).exists():
                conn.execute("DELETE FROM assets WHERE id = ?", (od["id"],))
                conn.execute("DELETE FROM prompts WHERE asset_id = ?", (od["id"],))
                orphan_count += 1
        counts["orphan_drafts_removed"] = orphan_count
        if orphan_count and not quiet:
            print(f"removed {orphan_count} orphan draft rows (source missing)", file=sys.stderr)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    counts["elapsed_sec"] = round(time.time() - started, 2)
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="Working gallery folder")
    ap.add_argument(
        "--db",
        default=None,
        help="SQLite DB path. Defaults to <source>/.visual_chef/visual_chef.db",
    )
    ap.add_argument("--recurse", action="store_true", help="Walk subdirectories")
    ap.add_argument("--dry-run", action="store_true", help="Walk only; no writes")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-file logging")
    args = ap.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    db_path = Path(args.db).expanduser() if args.db else lib.db_path_for(source)

    try:
        counts = scan(
            source,
            db_path,
            recurse=args.recurse,
            dry_run=args.dry_run,
            quiet=args.quiet,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    parts = " ".join(f"{k}={v}" for k, v in counts.items())
    prefix = "DRY-RUN " if args.dry_run else ""
    print(f"{prefix}db={db_path}  {parts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
