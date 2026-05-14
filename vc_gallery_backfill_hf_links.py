#!/usr/bin/env python3
"""vc_gallery_backfill_hf_links — link existing assets to their Higgsfield jobs.

Older sidecars (1,635 of them on EP8) don't have `hf_job_url:` set because the
wrapper didn't pass the job_id through until today. The wrapper DID, however,
log every submit + complete event to `~/.cache/visual-chef/hf_safety_*.jsonl`.
Each row has `destination: <abs path>` and `job_id: <uuid>`.

This script:
1. Reads every safety log JSONL
2. Builds a map of {destination_path → job_id}, keeping the latest `completed`
   event per file
3. Joins it to the current SQLite DB's `assets.file_path`
4. Upserts into the `jobs` table with provider='higgsfield'
   and source_url='https://higgsfield.ai/asset/all/<job_id>'

Usage:
    python3 vc_gallery_backfill_hf_links.py --db /path/to/visual_chef.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_lib as lib  # noqa: E402

SAFETY_LOG_DIR = Path.home() / ".cache" / "visual-chef"
HF_ASSET_URL = "https://higgsfield.ai/asset/all/{job_id}"


def build_path_to_jobid_map(log_dir: Path) -> dict[str, str]:
    """Walk every hf_safety_*.jsonl and build {abs_path → latest job_id}."""
    mapping: dict[str, str] = {}
    if not log_dir.exists():
        return mapping
    for jsonl in sorted(log_dir.glob("hf_safety_*.jsonl")):
        try:
            with jsonl.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    dest = row.get("destination")
                    job_id = row.get("job_id")
                    status = row.get("status", "")
                    if not dest or not job_id:
                        continue
                    # Prefer the `completed` row, then `submitted`. Don't
                    # overwrite a known-good job_id with a `failed_*` one
                    # at the same path (re-runs of the same target name).
                    if status == "completed":
                        mapping[dest] = job_id
                    elif dest not in mapping and status == "submitted":
                        mapping[dest] = job_id
        except OSError:
            continue
    return mapping


def backfill(db_path: Path) -> dict:
    conn = lib.connect(db_path)
    mapping = build_path_to_jobid_map(SAFETY_LOG_DIR)
    if not mapping:
        return {"path_map_size": 0, "matched": 0, "inserted": 0, "skipped_existing": 0}

    inserted = 0
    skipped = 0
    matched = 0
    for row in conn.execute("SELECT id, file_path FROM assets"):
        job_id = mapping.get(row["file_path"])
        if not job_id:
            continue
        matched += 1
        existing = conn.execute(
            "SELECT source_url FROM jobs WHERE asset_id = ?", (row["id"],)
        ).fetchone()
        if existing and existing["source_url"]:
            skipped += 1
            continue
        url = HF_ASSET_URL.format(job_id=job_id)
        conn.execute(
            """INSERT INTO jobs (asset_id, provider, provider_job_id, source_url)
               VALUES (?, 'higgsfield', ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                   provider = 'higgsfield',
                   provider_job_id = excluded.provider_job_id,
                   source_url = excluded.source_url""",
            (row["id"], job_id, url),
        )
        inserted += 1

    conn.close()
    return {
        "path_map_size": len(mapping),
        "matched": matched,
        "inserted": inserted,
        "skipped_existing": skipped,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="SQLite DB path. Defaults to current folder's .visual_chef/visual_chef.db")
    ap.add_argument("--folder", help="Working folder (derives DB path if --db omitted)")
    args = ap.parse_args()

    if args.db:
        db_path = Path(args.db).expanduser()
    elif args.folder:
        db_path = lib.db_path_for(args.folder)
    else:
        cfg = lib.load_server_config()
        cur = cfg.get("current_folder")
        if not cur:
            print("ERROR: no folder. Pass --db or --folder.", file=sys.stderr)
            return 1
        db_path = lib.db_path_for(cur)

    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    counts = backfill(db_path)
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
