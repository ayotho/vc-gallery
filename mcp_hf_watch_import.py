#!/usr/bin/env python3
"""Poll a Higgsfield/MCP job and import it into VC Gallery when complete.

This is a small reversible bridge for jobs launched outside the VC Gallery
wrapper, such as Higgsfield MCP generations. It does not change the gallery
server, schema, or UI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hf_import import ImportError_, hf_get_job, import_hf_asset  # noqa: E402


def _status_label(job: dict) -> str:
    return str(job.get("status") or job.get("state") or "unknown")


def _is_complete(job: dict) -> bool:
    return _status_label(job) == "completed" and bool(job.get("result_url"))


def _is_failed(job: dict) -> bool:
    status = _status_label(job).lower()
    return status in {"failed", "cancelled", "canceled", "error", "rejected"}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Watch a Higgsfield job id and import it into VC Gallery once complete."
    )
    ap.add_argument("job_id", help="Higgsfield job UUID or asset URL")
    ap.add_argument("--gallery", required=True, help="Gallery folder to import into")
    ap.add_argument("--client", required=True, help="Client slug/name")
    ap.add_argument("--project", required=True, help="Project slug/name")
    ap.add_argument("--shot-id", default="", help="Shot id for VC Gallery")
    ap.add_argument("--scene", default="", help="Scene/segment label")
    ap.add_argument("--workflow", default="i2v", help="Workflow label")
    ap.add_argument("--filename", default="", help="Output filename in gallery")
    ap.add_argument("--notes", default="", help="Notes for the imported asset")
    ap.add_argument("--status", default="review", help="Imported asset status")
    ap.add_argument("--variant", default="", help="Variant label")
    ap.add_argument("--pass-num", type=int, default=1, help="Pass number")
    ap.add_argument("--force", action="store_true", help="Overwrite existing output")
    ap.add_argument("--interval", type=int, default=45, help="Seconds between polls")
    ap.add_argument("--timeout", type=int, default=1800, help="Max seconds to watch")
    ap.add_argument("--json", action="store_true", help="Print final result as JSON")
    args = ap.parse_args()

    started = time.time()
    last_status = None
    while True:
        try:
            job = hf_get_job(args.job_id)
        except RuntimeError as e:
            print(f"✗ unable to read Higgsfield job: {e}", file=sys.stderr)
            return 3

        status = _status_label(job)
        if status != last_status:
            print(f"[watch] {args.job_id} status={status}", file=sys.stderr)
            last_status = status

        if _is_complete(job):
            break

        if _is_failed(job):
            print(f"✗ job ended status={status}", file=sys.stderr)
            return 4

        elapsed = time.time() - started
        if elapsed >= args.timeout:
            print(f"✗ timeout after {int(elapsed)}s waiting for job status={status}", file=sys.stderr)
            return 5

        time.sleep(max(5, args.interval))

    try:
        result = import_hf_asset(
            url_or_id=args.job_id,
            gallery=args.gallery,
            client=args.client,
            project=args.project,
            shot_id=args.shot_id,
            scene=args.scene,
            workflow=args.workflow,
            filename=args.filename,
            notes=args.notes,
            status=args.status,
            variant=args.variant,
            pass_num=args.pass_num,
            force=args.force,
        )
    except ImportError_ as e:
        print(f"✗ import failed: {e.message}", file=sys.stderr)
        return 6

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        mb = result["size_bytes"] / (1024 * 1024)
        print(
            f"✓ imported #{result['asset_id']} {result['filename']} | "
            f"{result['model']} | {mb:.2f}MB | job {result['hf_job_id']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
