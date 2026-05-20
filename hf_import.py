#!/usr/bin/env python3
"""hf_import — pull an existing Higgsfield asset into the local gallery.

USE CASE:
  You generated something via the Higgsfield web UI (or from another machine,
  or someone else's session), and now you want it in YOUR gallery DB as a
  tracked asset with proper metadata — same shape as if hf_gen_with_sidecar
  had fired it locally. No re-fire, no re-spend.

INPUT:
  Either the full asset URL or just the job UUID:
    https://higgsfield.ai/asset/all/<uuid>
    https://higgsfield.ai/...?jobset=<uuid>...   (older shape, falls back)
    <uuid>

WHAT IT DOES:
  1. Extract job_id, call `higgsfield --json generate get <id>`
  2. Pull result_url, prompt, model, params, refs from the response
  3. Download result_url → <gallery>/<filename> (curl; atomic, won't overwrite)
  4. ffprobe for width/height/duration_sec
  5. Write a DB row (source_type=generated) using the same upsert helper
     the wrapper uses — model, prompt, refs, hf_url, dims all preserved
  6. Print one terse success line

CLI:
  python3 hf_import.py <url-or-id> \\
      --client BTW_Documentary \\
      --project Episode_9 \\
      --shot-id SH1620A \\
      --scene more_awareness_dreams_nature \\
      --workflow i2v \\
      [--filename SH1620A_kling_imported_v1.mp4] \\
      [--notes "imported from HF web UI 2026-05-20"] \\
      [--gallery /abs/path/to/gallery]   # default: read from /api/folder
      [--status review] \\
      [--force]                           # overwrite existing file
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.request import urlopen
from urllib.error import URLError

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_lib as lib  # noqa: E402


JOB_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def extract_job_id(arg: str) -> Optional[str]:
    """Pull a UUID out of an HF asset URL or accept a raw UUID."""
    m = JOB_ID_RE.search(arg)
    return m.group(0) if m else None


def hf_get_job(job_id: str) -> dict:
    """Call `higgsfield --json generate get <id>` and return parsed JSON."""
    try:
        proc = subprocess.run(
            ["higgsfield", "--json", "generate", "get", job_id],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise RuntimeError(f"higgsfield CLI failed: {e}") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"higgsfield generate get {job_id} exited {proc.returncode}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"higgsfield returned invalid JSON: {e}") from e


def detect_workflow(params: dict, job_set_type: str) -> str:
    """Heuristic: i2v if start_image media, t2v otherwise; image gens map to
    a sensible image workflow. Director-overridable via --workflow flag."""
    is_video = bool(params.get("duration")) or job_set_type.startswith(("kling", "seedance", "veo", "sora", "cinematic"))
    medias = params.get("medias") or []
    has_start = any((m.get("role") == "start_image") for m in medias if isinstance(m, dict))
    if is_video:
        return "i2v" if has_start else "t2v"
    # Image fallback — director should override for non-default flows
    return "createframe"


def derive_filename(result_url: str, shot_id: Optional[str], model: str, ext_default: str = ".mp4") -> str:
    """Generate a stable filename if --filename not given."""
    basename = Path(result_url.split("?")[0]).name  # strip query string
    # Higgsfield CDN names like `hf_20260519_172158_<uuid>.mp4` — keep that ext
    ext = Path(basename).suffix or ext_default
    if shot_id:
        return f"{shot_id}_{model}_imported{ext}"
    return basename  # use the CDN basename as-is if no shot_id


def download_to(url: str, target: Path) -> int:
    """curl URL → target. Returns size in bytes; raises on failure."""
    proc = subprocess.run(
        ["curl", "-sSL", "--fail", "-o", str(target), url],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"download failed (exit {proc.returncode}): {proc.stderr.strip()}")
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(f"downloaded file is missing or empty: {target}")
    return target.stat().st_size


def ffprobe_dims(path: Path) -> dict:
    """Return {width, height, duration_sec} via ffprobe. Best-effort, all None on failure."""
    out = {"width": None, "height": None, "duration_sec": None}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return out
        data = json.loads(proc.stdout)
        stream = (data.get("streams") or [{}])[0]
        out["width"] = stream.get("width")
        out["height"] = stream.get("height")
        dur = stream.get("duration") or (data.get("format") or {}).get("duration")
        if dur:
            try:
                d = float(dur)
                out["duration_sec"] = d if d > 0 else None
            except (TypeError, ValueError):
                pass
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return out


def gallery_from_server(server_url: str = "http://127.0.0.1:8770") -> Optional[str]:
    """Ask the running gallery server for its current folder."""
    try:
        with urlopen(f"{server_url}/api/folder", timeout=2) as resp:
            data = json.load(resp)
            return data.get("current")
    except (URLError, OSError, json.JSONDecodeError):
        return None


def collect_refs(params: dict) -> list[str]:
    """Pull ref URLs out of params.medias for the DB row. Stores HF CDN URLs
    since the original local refs (if any) aren't available — director can
    swap to local paths later if they have them."""
    refs: list[str] = []
    for m in params.get("medias") or []:
        if isinstance(m, dict):
            url = (m.get("data") or {}).get("url")
            if url:
                refs.append(url)
    return refs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url_or_id", help="Higgsfield asset URL or job UUID")
    ap.add_argument("--client", required=True, help="e.g. BTW_Documentary")
    ap.add_argument("--project", required=True, help="e.g. Episode_9")
    ap.add_argument("--shot-id", default="", help="e.g. SH1620A")
    ap.add_argument("--scene", default="", help="e.g. more_awareness_dreams_nature")
    ap.add_argument("--workflow", default="", help="i2v/t2v/v2v/createframe/... (auto-detected if omitted)")
    ap.add_argument("--filename", default="", help="Override target filename (default: derived from shot_id+model)")
    ap.add_argument("--notes", default="", help="Free-form note for the asset")
    ap.add_argument("--gallery", default="", help="Gallery folder (default: read from /api/folder)")
    ap.add_argument("--status", default="review", help="Initial status (default: review)")
    ap.add_argument("--variant", default="", help="Variant tag")
    ap.add_argument("--pass-num", type=int, default=1, help="Iteration counter")
    ap.add_argument("--force", action="store_true", help="Overwrite existing file")
    ap.add_argument("--server-url", default="http://127.0.0.1:8770", help="Gallery server URL for /api/folder lookup")
    args = ap.parse_args()

    # 1. Extract job_id
    job_id = extract_job_id(args.url_or_id)
    if not job_id:
        print(f"✗ couldn't extract a UUID from: {args.url_or_id}", file=sys.stderr)
        return 2

    # 2. Fetch job metadata
    try:
        job = hf_get_job(job_id)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 3

    status = job.get("status")
    if status != "completed":
        print(f"✗ job {job_id} status={status!r} — only 'completed' jobs can be imported", file=sys.stderr)
        return 4

    result_url = job.get("result_url")
    if not result_url:
        print(f"✗ job {job_id} has no result_url", file=sys.stderr)
        return 4

    model = job.get("job_set_type", "unknown")
    params = job.get("params") or {}
    prompt = params.get("prompt") or ""
    workflow = args.workflow or detect_workflow(params, model)
    refs = collect_refs(params)

    # 3. Resolve gallery folder
    gallery_str = args.gallery or gallery_from_server(args.server_url)
    if not gallery_str:
        print(f"✗ no gallery folder — pass --gallery or start the gallery server", file=sys.stderr)
        return 5
    gallery = Path(gallery_str).resolve()
    if not gallery.exists() or not gallery.is_dir():
        print(f"✗ gallery folder does not exist: {gallery}", file=sys.stderr)
        return 5

    # 4. Determine filename + check collision
    filename = args.filename or derive_filename(result_url, args.shot_id, model)
    target = gallery / filename
    if target.exists() and not args.force:
        print(f"✗ destination exists: {target} — pass --force to overwrite", file=sys.stderr)
        return 6

    # 5. Download
    try:
        size = download_to(result_url, target)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 7

    # 6. ffprobe (videos + images both OK)
    dims = ffprobe_dims(target)

    # 7. Write DB row
    metadata = {
        "status": args.status,
        "source_type": "generated",
        "model": model,
        "workflow": workflow,
        "pass_num": args.pass_num,
        "variant": args.variant,
        "client": args.client,
        "project": args.project,
        "shot_id": args.shot_id,
        "scene": args.scene,
        "notes": args.notes,
        "prompt_text": prompt,
        "refs": refs,
        "hf_job_id": job_id,
        "hf_job_url": f"https://higgsfield.ai/asset/all/{job_id}",
        "has_sidecar": False,
        "width": dims["width"],
        "height": dims["height"],
        "duration_sec": dims["duration_sec"],
    }

    try:
        db_path = lib.db_path_for(str(gallery))
        conn = lib.connect(db_path)
        asset_id, op = lib.upsert_asset_direct(conn, str(target), metadata)
        conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"✗ DB write failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 8

    mb = size / (1024 * 1024)
    dur_str = f"{dims['duration_sec']:.1f}s" if dims.get("duration_sec") else "?"
    res_str = f"{dims.get('width')}x{dims.get('height')}" if dims.get("width") else "?"
    print(
        f"✓ #{asset_id} {op} | {filename} | {model} | {mb:.2f}MB | {res_str} | {dur_str}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
