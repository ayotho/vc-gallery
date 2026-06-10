#!/usr/bin/env python3
"""Safely import completed Higgsfield video jobs into a VC Gallery folder.

This is a conservative wrapper around hf_import.py for polling loops. It
preflights each Higgsfield job against the gallery DB, local files, sidecar-ish
metadata text, and a small seen-state file before any download/import happens.

Default mode is dry-run. Use --apply to import jobs that are completed and not
already represented in the gallery.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hf_import import ImportError_, extract_job_id, hf_get_job, import_hf_asset  # noqa: E402
import vc_gallery_lib as lib  # noqa: E402


JOB_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".webm"}


@dataclass
class SeenIndex:
    uuids: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    filenames: set[str] = field(default_factory=set)
    local_paths: set[str] = field(default_factory=set)
    prompt_keys: set[str] = field(default_factory=set)


def higgsfield_binary() -> str:
    found = shutil.which("higgsfield")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "higgsfield"
    if fallback.exists():
        return str(fallback)
    return "higgsfield"


def norm_text(value: Any) -> str:
    return str(value or "").strip()


def norm_uuid(value: str) -> str:
    return value.lower()


def extract_uuids(text: str) -> set[str]:
    return {norm_uuid(m.group(0)) for m in JOB_ID_RE.finditer(text or "")}


def add_text_signals(index: SeenIndex, text: str) -> None:
    if not text:
        return
    index.uuids.update(extract_uuids(text))
    for token in re.findall(r"https?://[^\s\"'<>]+", text):
        index.urls.add(token.rstrip("),.;"))


def prompt_key(model: str, prompt: str, result_url: str = "") -> str:
    """Weak duplicate signal for reporting only.

    Exact prompt duplicates are not enough to block an import because Ayo often
    intentionally reruns the same prompt. We still surface them as warnings.
    """
    return json.dumps(
        {
            "model": (model or "").strip(),
            "prompt": " ".join((prompt or "").split()),
            "result_name": Path((result_url or "").split("?")[0]).name,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def load_seen_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "jobs": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"version": 1, "jobs": {}}
    if not isinstance(data, dict):
        return {"version": 1, "jobs": {}}
    data.setdefault("version", 1)
    data.setdefault("jobs", {})
    return data


def save_seen_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def build_seen_index(gallery: Path, state: dict[str, Any]) -> SeenIndex:
    index = SeenIndex()
    db_path = lib.db_path_for(str(gallery))
    if Path(db_path).exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                """
                SELECT a.id, a.file_path, a.filename, a.notes,
                       p.prompt_text, p.refs_json,
                       j.provider_job_id, j.source_url
                FROM assets a
                LEFT JOIN prompts p ON p.asset_id = a.id
                LEFT JOIN jobs j ON j.asset_id = a.id
                """
            ):
                filename = norm_text(row["filename"])
                file_path = norm_text(row["file_path"])
                if filename:
                    index.filenames.add(filename)
                    add_text_signals(index, filename)
                if file_path:
                    index.local_paths.add(str(Path(file_path).resolve()))
                    add_text_signals(index, file_path)
                for field_name in ("notes", "refs_json", "provider_job_id", "source_url"):
                    add_text_signals(index, norm_text(row[field_name]))
                source_url = norm_text(row["source_url"])
                if source_url:
                    index.urls.add(source_url)
                provider_job_id = norm_text(row["provider_job_id"])
                if provider_job_id:
                    index.uuids.update(extract_uuids(provider_job_id))
                prompt = norm_text(row["prompt_text"])
                if prompt:
                    index.prompt_keys.add(prompt_key("", prompt))
        finally:
            conn.close()

    # Scan the whole gallery tree for already downloaded results, including
    # Accepted/ and Archive/ folders. Avoid hidden cache dirs for speed/noise.
    skip_dirs = {".visual_chef", ".thumbs", ".drafts", "__pycache__"}
    for path in gallery.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.is_file():
            index.filenames.add(path.name)
            index.local_paths.add(str(path.resolve()))
            add_text_signals(index, path.name)
            if path.suffix.lower() in {".json", ".md", ".txt"}:
                try:
                    add_text_signals(index, path.read_text(errors="ignore")[:200_000])
                except OSError:
                    pass

    for job_id, info in (state.get("jobs") or {}).items():
        index.uuids.add(norm_uuid(job_id))
        if isinstance(info, dict):
            add_text_signals(index, json.dumps(info, ensure_ascii=False))
    return index


def hf_list_recent_video(size: int) -> list[dict[str, Any]]:
    proc = subprocess.run(
        [higgsfield_binary(), "--json", "generate", "list", "--video", "--size", str(size)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    data = json.loads(proc.stdout)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "generations", "data", "results"):
            items = data.get(key)
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
    raise RuntimeError("could not find a list of jobs in Higgsfield list output")


def jobs_from_json_file(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "generations", "data", "results"):
            items = data.get(key)
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
    raise RuntimeError(f"{path} does not contain a supported job list shape")


def canonical_job(job: dict[str, Any]) -> dict[str, Any]:
    """Normalize Higgsfield CLI/MCP job shapes to the fields this script needs."""
    job_id = (
        norm_text(job.get("id"))
        or norm_text(job.get("job_id"))
        or norm_text(job.get("uuid"))
        or extract_job_id(json.dumps(job, ensure_ascii=False))
        or ""
    )
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    results = job.get("results") if isinstance(job.get("results"), dict) else {}
    result_url = (
        norm_text(job.get("result_url"))
        or norm_text(job.get("rawUrl"))
        or norm_text(results.get("rawUrl"))
        or norm_text(results.get("raw_url"))
        or norm_text(results.get("url"))
    )
    return {
        "id": norm_uuid(job_id),
        "status": norm_text(job.get("status") or job.get("state")),
        "model": norm_text(job.get("model") or job.get("job_set_type") or params.get("model")),
        "prompt": norm_text(params.get("prompt") or job.get("prompt")),
        "params": params,
        "result_url": result_url,
    }


def fetch_completed_job(job_id: str, seed: dict[str, Any]) -> dict[str, Any]:
    """Fetch full job detail unless the seed already has enough completed data."""
    c = canonical_job(seed)
    if c["status"] == "completed" and c["result_url"] and c["prompt"]:
        return c
    full = hf_get_job(job_id)
    return canonical_job(full)


def duplicate_reasons(job: dict[str, Any], index: SeenIndex, target_filename: str = "") -> list[str]:
    reasons: list[str] = []
    job_id = norm_uuid(job["id"])
    result_url = norm_text(job.get("result_url"))
    result_name = Path(result_url.split("?")[0]).name if result_url else ""
    if job_id and job_id in index.uuids:
        reasons.append("job UUID already present in DB/local/state")
    if result_url and result_url in index.urls:
        reasons.append("result URL already present in DB/local/state")
    if result_name and result_name in index.filenames:
        reasons.append(f"result basename already exists locally: {result_name}")
    if target_filename and target_filename in index.filenames:
        reasons.append(f"target filename already exists locally: {target_filename}")
    if job_id and any(job_id in p for p in index.local_paths):
        reasons.append("job UUID already appears in a local file path")
    return reasons


def note_state(state: dict[str, Any], job_id: str, status: str, detail: dict[str, Any]) -> None:
    jobs = state.setdefault("jobs", {})
    jobs[norm_uuid(job_id)] = {
        "status": status,
        "updated_at": int(time.time()),
        **detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--recent", action="store_true", help="Read recent completed video jobs from Higgsfield CLI")
    src.add_argument("--jobs-json", help="JSON file from MCP/CLI containing recent generation items")
    src.add_argument("--job", action="append", default=[], help="Specific Higgsfield job UUID or URL; repeatable")
    ap.add_argument("--size", type=int, default=20, help="Recent list size for --recent")
    ap.add_argument("--gallery", required=True, help="VC Gallery folder")
    ap.add_argument("--client", default="BTW_Documentary")
    ap.add_argument("--project", default="EP1")
    ap.add_argument("--shot-id", default="", help="Optional shot id for all imported jobs")
    ap.add_argument("--scene", default="", help="Optional scene; defaults to higgsfield_self_import when shot is blank")
    ap.add_argument("--workflow", default="", help="Override workflow for imported jobs")
    ap.add_argument("--filename", default="", help="Only valid with a single --job")
    ap.add_argument("--status", default="review")
    ap.add_argument("--state-file", default="", help="Default: <gallery>/.visual_chef/hf_safe_import_seen.json")
    ap.add_argument("--include-seen", action="store_true", help="Do not suppress jobs already recorded in the seen-state file")
    ap.add_argument("--apply", action="store_true", help="Actually import non-duplicate completed jobs")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    args = ap.parse_args()

    gallery = Path(args.gallery).resolve()
    if not gallery.exists():
        print(f"gallery does not exist: {gallery}", file=sys.stderr)
        return 2
    state_file = Path(args.state_file) if args.state_file else gallery / ".visual_chef" / "hf_safe_import_seen.json"
    state = load_seen_state(state_file)
    index = build_seen_index(gallery, state)

    seeds: list[dict[str, Any]] = []
    if args.recent:
        try:
            seeds = hf_list_recent_video(args.size)
        except Exception as e:  # noqa: BLE001
            print(f"failed to list Higgsfield jobs: {e}", file=sys.stderr)
            return 3
    elif args.jobs_json:
        try:
            seeds = jobs_from_json_file(Path(args.jobs_json))
        except Exception as e:  # noqa: BLE001
            print(f"failed to read jobs JSON: {e}", file=sys.stderr)
            return 3
    else:
        seeds = [{"id": extract_job_id(j) or j, "status": ""} for j in args.job]

    if args.filename and len(seeds) != 1:
        print("--filename can only be used with a single job", file=sys.stderr)
        return 2

    summary: dict[str, Any] = {"imported": [], "duplicates": [], "suppressed": [], "pending": [], "failed": [], "errors": [], "dry_run": not args.apply}
    for seed in seeds:
        seed_c = canonical_job(seed)
        job_id = seed_c["id"]
        if not job_id:
            summary["errors"].append({"seed": seed, "error": "no job UUID found"})
            continue
        state_hit = (state.get("jobs") or {}).get(job_id)
        if state_hit and not args.include_seen:
            summary["suppressed"].append({"job_id": job_id, "state": state_hit.get("status", "seen")})
            continue
        try:
            job = fetch_completed_job(job_id, seed)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({"job_id": job_id, "error": str(e)})
            continue

        status = (job.get("status") or "").lower()
        if status != "completed":
            bucket = "failed" if status in {"failed", "cancelled", "canceled", "error", "rejected"} else "pending"
            summary[bucket].append({"job_id": job_id, "status": status or "unknown"})
            continue

        target_filename = args.filename
        reasons = duplicate_reasons(job, index, target_filename)
        weak_prompt = prompt_key(job.get("model", ""), job.get("prompt", ""), job.get("result_url", ""))
        prompt_seen = weak_prompt in index.prompt_keys
        if reasons:
            item = {"job_id": job_id, "reasons": reasons}
            if prompt_seen:
                item["warning"] = "exact prompt-like key also seen"
            summary["duplicates"].append(item)
            if args.apply:
                note_state(state, job_id, "duplicate", {"reasons": reasons})
            continue

        if not args.apply:
            summary["imported"].append({"job_id": job_id, "would_import": True, "prompt_seen_warning": prompt_seen})
            continue

        scene = args.scene or ("" if args.shot_id else "higgsfield_self_import")
        notes = json.dumps(
            {
                "imported_by": "hf_safe_import",
                "asset_uuid": job_id,
                "pulled_from": f"https://higgsfield.ai/asset/all/{job_id}",
                "dedupe_preflight": "passed",
                "prompt_seen_warning": prompt_seen,
            },
            ensure_ascii=False,
        )
        try:
            result = import_hf_asset(
                url_or_id=job_id,
                gallery=str(gallery),
                client=args.client,
                project=args.project,
                shot_id=args.shot_id,
                scene=scene,
                workflow=args.workflow,
                filename=target_filename,
                notes=notes,
                status=args.status,
            )
        except ImportError_ as e:
            summary["errors"].append({"job_id": job_id, "code": e.code, "error": e.message})
            continue

        summary["imported"].append(result)
        note_state(state, job_id, "imported", {"asset_id": result["asset_id"], "filename": result["filename"]})
        # Update in-memory index so two jobs in one run cannot collide with each
        # other after the first import.
        index.uuids.add(job_id)
        index.urls.add(result["hf_job_url"])
        index.filenames.add(result["filename"])
        index.local_paths.add(str(Path(result["file_path"]).resolve()))

    if args.apply:
        save_seen_state(state_file, state)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        action = "APPLY" if args.apply else "DRY RUN"
        print(f"{action} | imported={len(summary['imported'])} duplicates={len(summary['duplicates'])} suppressed={len(summary['suppressed'])} pending={len(summary['pending'])} failed={len(summary['failed'])} errors={len(summary['errors'])}")
        for item in summary["imported"]:
            if item.get("would_import"):
                print(f"  WOULD IMPORT {item['job_id']}")
            else:
                print(f"  IMPORTED #{item['asset_id']} {item['filename']} ({item['hf_job_id']})")
        for item in summary["duplicates"]:
            print(f"  DUPLICATE {item['job_id']}: {'; '.join(item['reasons'])}")
        for item in summary["suppressed"]:
            print(f"  SUPPRESSED {item['job_id']}: {item['state']}")
        for item in summary["pending"]:
            print(f"  PENDING {item['job_id']}: {item['status']}")
        for item in summary["failed"]:
            print(f"  FAILED {item['job_id']}: {item['status']}")
        for item in summary["errors"]:
            print(f"  ERROR {item.get('job_id', '?')}: {item.get('error')}")
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
