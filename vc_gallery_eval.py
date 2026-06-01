#!/usr/bin/env python3
"""vc_gallery_eval — evaluate gallery assets for integrity + consistency.

Turns the manual QA checks (ffprobe integrity, DB<->disk consistency, source_url
presence, thumbnail render, board grouping) into one runnable report. Engine-
agnostic: works on any asset (Higgsfield or fal). Backs the /vc-eval skill.

Usage:
  python3 vc_gallery_eval.py [--gallery PATH|auto] [--scene S] [--shot S]
                             [--status review] [--provider fal] [--json]
Exit code 0 = all checks pass, 1 = one or more failures (CI-friendly).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import vc_gallery_lib as lib  # noqa: E402

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _ffprobe(path: str) -> dict:
    """Return {ok, duration, width, height, vcodec, has_audio} for a media file."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"ok": False}
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = (data.get("format") or {}).get("duration")
    return {
        "ok": v is not None,
        "duration": round(float(dur), 2) if dur else None,
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "vcodec": v.get("codec_name") if v else None,
        "has_audio": a is not None,
    }


def evaluate(gallery: Path, scene=None, shot=None, status=None, provider=None) -> dict:
    db_path = lib.db_path_for(str(gallery))
    conn = lib.connect(db_path)
    conn.row_factory = __import__("sqlite3").Row
    where, params = ["1=1"], []
    if scene:
        where.append("a.scene = ?"); params.append(scene)
    if shot:
        where.append("a.shot_id = ?"); params.append(shot)
    if status:
        where.append("a.status = ?"); params.append(status)
    sql = ("SELECT a.id,a.filename,a.file_path,a.status,a.scene,a.shot_id,a.model,"
           "a.media_type,a.size_bytes,j.provider,j.source_url "
           "FROM assets a LEFT JOIN jobs j ON j.asset_id=a.id "
           f"WHERE {' AND '.join(where)} ORDER BY a.shot_id,a.filename")
    rows = conn.execute(sql, params).fetchall()
    if provider:
        rows = [r for r in rows if (r["provider"] or "") == provider]

    results, shots = [], {}
    for r in rows:
        issues = []
        is_draft = r["status"] == "draft"
        on_disk = bool(r["file_path"]) and os.path.exists(r["file_path"])
        ext = Path(r["filename"]).suffix.lower()
        # drafts legitimately have no file yet
        if not is_draft and not on_disk:
            issues.append("file missing on disk")
        media = None
        if on_disk and ext in VIDEO_EXT:
            sz = os.path.getsize(r["file_path"])
            if sz == 0:
                issues.append("zero-byte file")
            media = _ffprobe(r["file_path"])
            if not media["ok"]:
                issues.append("no decodable video stream")
        elif on_disk and ext in IMAGE_EXT:
            if os.path.getsize(r["file_path"]) == 0:
                issues.append("zero-byte file")
        # generated assets should carry a provider job + source_url
        if r["status"] in ("review", "accepted", "hero") and r["provider"] and not r["source_url"]:
            issues.append("provider set but source_url empty")
        # board grouping
        if not is_draft and not r["shot_id"]:
            issues.append("no shot_id (won't group on board)")
        shots.setdefault(r["shot_id"] or "(none)", 0)
        shots[r["shot_id"] or "(none)"] += 1
        results.append({
            "id": r["id"], "shot_id": r["shot_id"], "status": r["status"],
            "filename": r["filename"], "provider": r["provider"],
            "media": media, "issues": issues, "pass": not issues,
        })
    conn.close()
    passed = sum(1 for x in results if x["pass"])
    return {
        "gallery": str(gallery), "evaluated": len(results),
        "passed": passed, "failed": len(results) - passed,
        "shots": shots, "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gallery", default="auto", help="Gallery path, or 'auto' for server config")
    ap.add_argument("--scene"); ap.add_argument("--shot")
    ap.add_argument("--status"); ap.add_argument("--provider")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    gallery = args.gallery
    if gallery == "auto":
        gallery = (lib.load_server_config() or {}).get("current_folder")
        if not gallery:
            print("✗ no gallery; pass --gallery PATH", file=sys.stderr); return 2
    rep = evaluate(Path(gallery), args.scene, args.shot, args.status, args.provider)

    if args.json:
        print(json.dumps(rep, indent=2)); return 0 if rep["failed"] == 0 else 1

    print(f"vc-eval · {rep['gallery']}")
    flt = " ".join(f"{k}={v}" for k, v in
                   (("scene", args.scene), ("shot", args.shot),
                    ("status", args.status), ("provider", args.provider)) if v)
    print(f"filter: {flt or 'all'}  →  {rep['passed']}/{rep['evaluated']} pass, {rep['failed']} fail\n")
    for x in rep["results"]:
        m = x["media"]
        spec = f" {m['width']}x{m['height']} {m['duration']}s aud={m['has_audio']}" if m and m["ok"] else ""
        flag = "OK " if x["pass"] else "FAIL"
        print(f"  [{flag}] {x['shot_id'] or '-':9} {x['status']:8} {x['filename'][:38]:38}{spec}")
        for i in x["issues"]:
            print(f"         ↳ {i}")
    print(f"\nshots: " + ", ".join(f"{k}×{v}" for k, v in sorted(rep["shots"].items())))
    print("VERDICT:", "PASS" if rep["failed"] == 0 else f"FAIL ({rep['failed']})")
    return 0 if rep["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
