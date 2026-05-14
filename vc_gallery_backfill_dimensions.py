#!/usr/bin/env python3
"""Backfill width/height/duration_sec on existing assets via ffprobe.

Usage:
  python3 vc_gallery_backfill_dimensions.py --gallery /path/to/EP8           # dry-run
  python3 vc_gallery_backfill_dimensions.py --gallery /path/to/EP8 --apply   # actually write

The schema has had width/height/duration_sec columns since day one but until
2026-05-14 nothing populated them, so every drawer showed "? × ?". This script
walks every row whose dimensions are still NULL, probes the on-disk file with
ffprobe, and updates the row. Skips files that don't exist on disk (those are
a separate orphans problem).

Idempotent. Re-running re-probes only rows still NULL.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from vc_gallery_scan import probe_media_dimensions  # noqa: E402


def backfill(gallery: Path, apply: bool, verbose: bool = False) -> dict:
    db_path = gallery / ".visual_chef" / "visual_chef.db"
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT id, file_path, filename, width, height, duration_sec
           FROM assets
           WHERE (width IS NULL OR height IS NULL)
             AND file_path IS NOT NULL"""
    ).fetchall()

    total = len(rows)
    probed = 0
    updated = 0
    skipped_missing = 0
    skipped_no_dims = 0
    started = time.time()

    for i, r in enumerate(rows, 1):
        p = Path(r["file_path"])
        if not p.exists():
            skipped_missing += 1
            continue
        dims = probe_media_dimensions(p)
        probed += 1
        if dims["width"] is None and dims["height"] is None and dims["duration_sec"] is None:
            skipped_no_dims += 1
            continue
        if verbose:
            print(f"  [{i}/{total}] {r['filename'][:70]} → {dims['width']}×{dims['height']} {dims['duration_sec'] or ''}s")
        if apply:
            con.execute(
                """UPDATE assets SET
                       width = COALESCE(?, width),
                       height = COALESCE(?, height),
                       duration_sec = COALESCE(?, duration_sec),
                       last_updated_at = strftime('%s','now')
                   WHERE id = ?""",
                (dims["width"], dims["height"], dims["duration_sec"], r["id"]),
            )
            updated += 1
        # Throttle commits to avoid one huge transaction on 2k rows
        if apply and updated % 100 == 0:
            con.commit()
    if apply:
        con.commit()

    elapsed = time.time() - started
    summary = {
        "gallery": str(gallery),
        "total_null": total,
        "probed": probed,
        "updated": updated,
        "skipped_missing_on_disk": skipped_missing,
        "skipped_probe_yielded_nothing": skipped_no_dims,
        "elapsed_seconds": round(elapsed, 1),
        "applied": apply,
    }
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gallery", required=True, help="Path to working gallery folder")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    gallery = Path(args.gallery).expanduser().resolve()
    summary = backfill(gallery, apply=args.apply, verbose=args.verbose)

    print()
    print("=" * 50)
    print("Dimensions backfill")
    print("=" * 50)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if not args.apply and summary["probed"] > 0:
        print()
        print("  ↳ dry-run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
