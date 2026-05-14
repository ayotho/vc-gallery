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
import sys
import time
from pathlib import Path

# Local imports — sibling utilities in arsenal/00-utilities/
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_lib as lib  # noqa: E402
from jsonl_append import append_jsonl  # noqa: E402


# Columns we upsert into the assets table (excluding id/timestamps).
ASSET_COLUMNS = [
    "file_path", "filename", "media_type",
    "size_bytes", "file_modified_at",
    "source_type", "has_sidecar", "sidecar_path",
    "status", "shot_id", "scene", "model", "workflow",
    "pass_num", "variant", "client", "project",
    "parent_filename", "session", "session_date", "score",
]


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
        "shot_id": sidecar_data.get("shot_id") or None,
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
    """Return (id, size_bytes, file_modified_at, source_type, model) for a
    known asset, else None. Source/model returned so a re-scan doesn't
    downgrade a `generated` row that the wrapper wrote directly to DB."""
    cur = conn.execute(
        "SELECT id, size_bytes, file_modified_at, source_type, model FROM assets WHERE file_path = ?",
        (file_path,),
    )
    r = cur.fetchone()
    return tuple(r) if r else None


def _upsert_asset(conn, row: dict) -> tuple[int, str]:
    """Insert or update an asset. Returns (asset_id, 'added' | 'updated' | 'unchanged')."""
    existing = _existing_fingerprint(conn, row["file_path"])
    if existing is None:
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

    asset_id, old_size, old_mtime, old_source_type, old_model = existing

    # Don't let a sidecar-driven re-scan downgrade a `generated` row the
    # wrapper wrote directly. If the existing row already has rich metadata
    # (source_type=generated AND a model), trust it over filename heuristics.
    if old_source_type == "generated" and old_model and row["source_type"] != "generated":
        row["source_type"] = "generated"

    if old_size == row["size_bytes"] and abs((old_mtime or 0) - row["file_modified_at"]) < 1.0:
        # File unchanged, but still refresh the jobs link in case sidecar gained URL
        _upsert_job(conn, asset_id, row)
        return asset_id, "unchanged"

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


def _upsert_prompt(conn, asset_id: int, prompt_row: dict | None) -> None:
    if prompt_row is None:
        conn.execute("DELETE FROM prompts WHERE asset_id = ?", (asset_id,))
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
    # Exclude drafts — they use synthetic .drafts/*.draft.json paths and must
    # not be touched by rename reconciliation.
    cur = conn.execute(
        "SELECT id, filename, file_path, size_bytes FROM assets WHERE file_path LIKE ? AND status != 'draft'",
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
            "UPDATE assets SET file_path = ?, last_updated_at = strftime('%s','now') WHERE id = ?",
            (str(new_path.resolve()), missing["id"]),
        )
        renames += 1
        try:
            append_jsonl(str(log_target), {
                "event": "scan.rename_reconciled",
                "asset_id": missing["id"],
                "filename": missing["filename"],
                "old_file_path": missing["file_path"],
                "new_file_path": str(new_path.resolve()),
            })
        except OSError:
            pass
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

    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "renamed": 0}
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
            _upsert_prompt(conn, asset_id, prompt_row)
            counts[action] += 1

            if action != "unchanged":
                if not quiet:
                    print(f"{action:>9}  {row['filename']}")
                try:
                    append_jsonl(str(log_target), {
                        "event": f"scan.{action}",
                        "asset_id": asset_id,
                        "file_path": row["file_path"],
                        "status": row["status"],
                        "source_type": row["source_type"],
                    })
                except OSError:
                    # log failure is non-fatal — DB write already happened
                    pass

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
