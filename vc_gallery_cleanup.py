#!/usr/bin/env python3
"""vc_gallery_cleanup — remove 0-byte placeholders and their orphan DB rows.

Use after a wrapper crash, rate-limit cascade, or any other case where
`hf_gen_with_sidecar.py` left 0-byte O_CREAT|O_EXCL placeholders on disk.

Default: DRY-RUN. Pass --apply to actually delete files + DB rows.

Examples:
    # see what would be cleaned in the EP8 gallery
    python3 vc_gallery_cleanup.py --gallery ~/Desktop/Client/Dave/BTW/EP8

    # actually clean
    python3 vc_gallery_cleanup.py --gallery ~/Desktop/Client/Dave/BTW/EP8 --apply

    # also remove DB rows whose file_path no longer exists on disk
    python3 vc_gallery_cleanup.py --gallery ~/Desktop/Client/Dave/BTW/EP8 --apply --missing
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_obs as obs  # noqa: E402


def clean_zero_byte_files(gallery: Path, db: Path, *, apply: bool, min_age_sec: float = 0) -> dict:
    """Remove 0-byte media files in the gallery and their DB rows."""
    media_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm"}
    removed_files: list[str] = []
    skipped_recent: list[str] = []
    failed: list[tuple[str, str]] = []
    cutoff = time.time() - min_age_sec

    for p in gallery.iterdir():
        if not p.is_file() or p.suffix.lower() not in media_exts:
            continue
        try:
            st = p.stat()
        except OSError as e:
            failed.append((str(p), str(e)))
            continue
        if st.st_size != 0:
            continue
        if st.st_mtime > cutoff:
            skipped_recent.append(p.name)
            continue
        if apply:
            try:
                p.unlink()
                removed_files.append(p.name)
            except OSError as e:
                failed.append((str(p), str(e)))
        else:
            removed_files.append(p.name)

    # Sync DB: drop rows whose size_bytes=0 (and optionally whose file is gone)
    removed_rows = 0
    if db.exists():
        conn = sqlite3.connect(str(db), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            # Drafts use size_bytes=0 by design — must NOT be swept by cleanup.
            if apply:
                cur = conn.execute(
                    "DELETE FROM assets WHERE (size_bytes = 0 OR size_bytes IS NULL) AND status != 'draft'"
                )
                removed_rows = cur.rowcount or 0
                # Drop any orphan prompts whose asset is gone
                conn.execute("DELETE FROM prompts WHERE asset_id NOT IN (SELECT id FROM assets)")
                conn.commit()
            else:
                cur = conn.execute(
                    "SELECT count(*) FROM assets WHERE (size_bytes = 0 OR size_bytes IS NULL) AND status != 'draft'"
                )
                removed_rows = cur.fetchone()[0]
        finally:
            conn.close()

    log_path = gallery / ".visual_chef" / "visual_chef.jsonl"
    if apply and (removed_files or removed_rows):
        obs.record_event(
            log_path,
            "cleanup.zero_byte",
            source="cleanup",
            severity="info",
            removed_files=len(removed_files),
            removed_rows=removed_rows,
            skipped_recent=len(skipped_recent),
            failed=len(failed),
        )

    return {
        "removed_files": removed_files,
        "removed_rows": removed_rows,
        "skipped_recent": skipped_recent,
        "failed": failed,
    }


def clean_missing_rows(gallery: Path, db: Path, *, apply: bool) -> dict:
    """Drop DB rows whose file_path no longer exists on disk.

    Conservative: only touches rows when gallery_folder MATCHES the parent of
    the file_path — never reaches outside this gallery.
    """
    if not db.exists():
        return {"removed_rows": 0}
    gallery_str = str(gallery.resolve())
    conn = sqlite3.connect(str(db), timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    removed: list[dict] = []
    try:
        # Drafts use synthetic .drafts/*.draft.json paths — exclude them
        rows = conn.execute(
            "SELECT id, filename, file_path, status FROM assets "
            "WHERE file_path LIKE ? AND status != 'draft'",
            (f"{gallery_str}%",),
        ).fetchall()
        for r in rows:
            if not Path(r["file_path"]).exists():
                removed.append({"id": r["id"], "filename": r["filename"], "status": r["status"]})
                if apply:
                    conn.execute("DELETE FROM assets WHERE id = ?", (r["id"],))
        if apply:
            conn.execute("DELETE FROM prompts WHERE asset_id NOT IN (SELECT id FROM assets)")
            conn.commit()
    finally:
        conn.close()

    log_path = gallery / ".visual_chef" / "visual_chef.jsonl"
    if apply and removed:
        obs.record_event(
            log_path,
            "cleanup.missing_on_disk",
            source="cleanup",
            severity="info",
            removed_rows=len(removed),
        )

    return {"removed_rows": len(removed), "ids": [r["id"] for r in removed]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gallery", required=True, help="Gallery folder to clean")
    ap.add_argument("--apply", action="store_true", help="Actually delete (otherwise dry-run)")
    ap.add_argument("--missing", action="store_true", help="Also drop DB rows whose file is gone")
    ap.add_argument("--min-age-sec", type=float, default=0,
                    help="Only clean files older than N seconds (default 0 = all)")
    args = ap.parse_args(argv)

    gallery = Path(args.gallery).expanduser().resolve()
    db = gallery / ".visual_chef" / "visual_chef.db"

    if not gallery.is_dir():
        print(f"ERROR: gallery not found: {gallery}", file=sys.stderr)
        return 1

    print(f"Gallery:  {gallery}")
    print(f"DB:       {db}")
    print(f"Mode:     {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    zero = clean_zero_byte_files(gallery, db, apply=args.apply, min_age_sec=args.min_age_sec)
    verb = "Removed" if args.apply else "Would remove"
    print(f"{verb} {len(zero['removed_files'])} zero-byte file(s) and {zero['removed_rows']} DB row(s).")
    if zero["skipped_recent"]:
        print(f"  Skipped {len(zero['skipped_recent'])} file(s) younger than --min-age-sec")
    if zero["failed"]:
        print(f"  Failed on {len(zero['failed'])}:")
        for p, e in zero["failed"][:5]:
            print(f"    {p}: {e}")

    if args.missing:
        missing = clean_missing_rows(gallery, db, apply=args.apply)
        print(f"{verb} {missing['removed_rows']} missing-on-disk DB row(s).")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to actually clean.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
