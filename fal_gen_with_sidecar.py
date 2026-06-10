#!/usr/bin/env python3
"""fal_gen_with_sidecar — fire one fal generation into VC Gallery.

This mirrors the Higgsfield wrapper contract used by vc_gallery_serve.py:
payload JSON in, rendered media downloaded into the active gallery, and the
draft row mutated into a review asset via vc_gallery_lib.upsert_asset_direct.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hf_gen_with_sidecar import (  # noqa: E402
    EXIT_AUTH,
    EXIT_DOWNLOAD,
    EXIT_OK,
    EXIT_OTHER,
    EXIT_SCHEMA,
    EXIT_SUBMIT,
    _TeeWriter,
    _download,
    _notify_server,
    _redact_prompt,
    _redact_url,
    _release_placeholder,
    _reserve_filename,
    _split_filename,
    hf_log_path,
)
from jsonl_append import append_jsonl  # noqa: E402


FAL_KLING_O3_R2V = "fal-ai/kling-video/o3/pro/reference-to-video"
# fal rejects reference images larger than 10 MB ("file_too_large"). Studio 4K
# stills are ~30 MB uncompressed PNG, so over-limit refs are re-encoded to a
# high-quality JPEG — full resolution kept (they fit in a few MB). See
# _shrink_image_if_needed; resolution is only reduced as a last resort.
_FAL_MAX_REF_BYTES = 10 * 1024 * 1024
# Safety cap on count so a fat-finger (e.g. count=99) can't trigger runaway spend.
_FAL_MAX_COUNT = 12
EXIT_TO_CLASS = {
    EXIT_OK: "ok",
    EXIT_OTHER: "unexpected",
    EXIT_AUTH: "auth_expired",
    EXIT_SCHEMA: "schema_invalid",
    EXIT_SUBMIT: "submit_failed",
    EXIT_DOWNLOAD: "download_failed",
}
PROJECT_ROOT = _HERE.parent.parent
SAFETY_DIR = Path.home() / ".cache" / "visual-chef"


def ledger_path(client: str, project: str) -> Path:
    return PROJECT_ROOT / "clients" / client / project / ".fal_runs.jsonl"


def safety_log_path() -> Path:
    return SAFETY_DIR / f"fal_safety_{time.strftime('%Y%m%d')}.jsonl"


def _media_values(payload: dict, *keys: str) -> list[str]:
    out: list[str] = []
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        out.extend(str(v) for v in values if v)
    return out


def _load_fal_key_fallback() -> None:
    """If FAL_KEY is missing from the environment (e.g. the gallery server was
    started without sourcing ~/.claude/env.sh), read it from that file read-only
    so unattended fal fires still authenticate."""
    env_file = Path.home() / ".claude" / "env.sh"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("export FAL_KEY=") or stripped.startswith("FAL_KEY="):
                val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    os.environ["FAL_KEY"] = val
                    return
    except OSError:
        return


def _shrink_image_if_needed(path: Path) -> Path:
    """Return the original path if it is within fal's 10 MB reference limit,
    otherwise the HIGHEST-QUALITY copy that fits — preserving resolution wherever
    possible (a >10 MB file is almost always an uncompressed PNG that becomes a
    few MB as a high-quality JPEG at full res, so we lose ~nothing). Resolution is
    only reduced as a last resort. macOS `sips`; original returned if unavailable."""
    try:
        if path.stat().st_size <= _FAL_MAX_REF_BYTES:
            return path
    except OSError:
        return path
    target = int(_FAL_MAX_REF_BYTES * 0.95)  # margin under the hard 10 MB cap
    tmp_dir = Path(tempfile.mkdtemp(prefix="fal_ref_"))

    def _encode(args: list[str], tag: str) -> Path | None:
        out = tmp_dir / f"{path.stem}_{tag}.jpg"
        try:
            subprocess.run(["sips", "-s", "format", "jpeg", *args, str(path), "--out", str(out)],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            return None
        return out if (out.exists() and out.stat().st_size <= target) else None

    # Pass 1 — keep FULL resolution, step JPEG quality down. This handles the
    # common case (huge PNG → small JPEG) with no resolution loss at all.
    for q in (98, 95, 92, 88, 84, 80):
        got = _encode(["-s", "formatOptions", str(q)], f"q{q}")
        if got:
            return got
    # Pass 2 — full-res still over budget (rare): reduce the longest edge, keep
    # quality high. Generous caps so we only shrink as much as strictly needed.
    for max_dim, q in ((4096, 90), (3072, 90), (2560, 88), (2048, 85)):
        got = _encode(["-Z", str(max_dim), "-s", "formatOptions", str(q)], str(max_dim))
        if got:
            return got
    return path


def _upload_or_url(fal_client, value: str) -> str:
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(value).expanduser()
    if not path.exists():
        raise FileNotFoundError(value)
    shrunk = _shrink_image_if_needed(path)
    try:
        return fal_client.upload_file(str(shrunk))
    finally:
        # _shrink_image_if_needed only returns a different path when it wrote a
        # downscaled copy into a fresh temp dir — remove it so fires don't leak.
        if shrunk != path:
            shutil.rmtree(shrunk.parent, ignore_errors=True)


def _build_fal_arguments(payload: dict) -> dict:
    """Map gallery/Higgsfield-style payload fields to fal Kling O3 R2V fields."""
    args: dict = {
        "prompt": payload["prompt"],
        "duration": str(payload.get("duration", 8)),
        "aspect_ratio": payload.get("aspect_ratio", "16:9"),
    }
    if "generate_audio" in payload:
        raw_audio = payload["generate_audio"]
        if isinstance(raw_audio, str):
            args["generate_audio"] = raw_audio.lower() in {"on", "true", "1", "yes"}
        else:
            args["generate_audio"] = bool(raw_audio)
    elif "sound" in payload:
        args["generate_audio"] = str(payload["sound"]).lower() in {"on", "true", "1", "yes"}
    if "negative_prompt" in payload:
        args["negative_prompt"] = payload["negative_prompt"]
    if "cfg_scale" in payload:
        args["cfg_scale"] = float(payload["cfg_scale"])
    if "shot_type" in payload:
        args["shot_type"] = payload["shot_type"]
    return args


def _extract_video_url(result: dict) -> str:
    video = result.get("video") if isinstance(result, dict) else None
    if isinstance(video, dict) and video.get("url"):
        return str(video["url"])
    if isinstance(result, dict) and result.get("video_url"):
        return str(result["video_url"])
    if isinstance(result, dict) and result.get("url"):
        return str(result["url"])
    raise ValueError("fal result did not include video.url")


def _write_db_row(payload: dict, target: Path, prompt: str, refs: list[str], request_id: Optional[str], result_url: str) -> bool:
    try:
        import vc_gallery_lib as lib_local

        db_path = lib_local.db_path_for(payload["gallery"])
        conn = lib_local.connect(db_path)
        metadata = {
            "status": payload.get("status", "review"),
            "source_type": "generated",
            "model": payload.get("model", FAL_KLING_O3_R2V),
            "workflow": payload.get("workflow", "i2v"),
            "pass_num": int(payload.get("pass_num", 1)),
            "variant": payload.get("variant", ""),
            "client": payload.get("client", ""),
            "project": payload.get("project", ""),
            "shot_id": payload.get("shot_id", ""),
            "scene": payload.get("scene") or payload.get("segment", ""),
            "parent_filename": payload.get("parent", ""),
            "session": payload.get("session", ""),
            "session_date": payload.get("session_date", ""),
            "notes": payload.get("notes", ""),
            "prompt_text": prompt or "",
            "refs": refs or [],
            "job_provider": "fal",
            "provider_job_id": request_id or "",
            "source_url": result_url,
            "has_sidecar": False,
        }
        asset_id_raw = payload.get("asset_id")
        asset_id_int: Optional[int] = None
        if asset_id_raw is not None:
            try:
                asset_id_int = int(asset_id_raw)
            except (TypeError, ValueError):
                asset_id_int = None
        lib_local.upsert_asset_direct(conn, str(target), metadata, asset_id=asset_id_int)
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[db-write] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def run(payload: dict, dry_run: bool = False, quiet: bool = False) -> int:
    required = ("gallery", "filename", "prompt")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        print(f"✗ missing required payload keys: {', '.join(missing)}", file=sys.stderr)
        return EXIT_SCHEMA

    model = payload.get("model") or FAL_KLING_O3_R2V
    if not model.startswith("fal-ai/"):
        print(f"✗ not a fal model: {model}", file=sys.stderr)
        return EXIT_SCHEMA

    client = payload.get("client", "unknown")
    project = payload.get("project", "unknown")
    run_id = uuid.uuid4().hex[:12]
    started_at = time.time()
    gallery = Path(payload["gallery"]).resolve()
    filename = payload["filename"]
    target = gallery / filename
    base_event = {
        "run_id": run_id,
        "client": client,
        "project": project,
        "model": model,
        "workflow": payload.get("workflow"),
        "shot_id": payload.get("shot_id", ""),
        "destination": str(target),
        **_redact_prompt(payload.get("prompt", "")),
        "started_at": started_at,
    }

    def emit(status: str, **extra) -> None:
        rec = {**base_event, "status": status, **extra}
        try:
            append_jsonl(ledger_path(client, project), rec)
            append_jsonl(safety_log_path(), rec)
        except OSError as exc:
            print(f"[ledger] WARN: append failed: {exc}", file=sys.stderr)

    refs = _media_values(payload, "image", "start_image", "image_urls", "refs", "media")
    args = _build_fal_arguments(payload)
    if not refs:
        print("✗ this fal model needs at least one reference image (image_urls)", file=sys.stderr)
        emit("failed_no_ref")
        return EXIT_SCHEMA
    if dry_run:
        print(json.dumps({"endpoint": model, "arguments": {**args, "image_urls": refs[:4]}, "target": str(target)}, indent=2))
        emit("dry_run", endpoint=model)
        return EXIT_OK

    try:
        import fal_client
    except ImportError:
        print("✗ fal Python client missing — install with `pip install fal-client` or `pip install fal`", file=sys.stderr)
        return EXIT_SCHEMA

    if not os.environ.get("FAL_KEY"):
        _load_fal_key_fallback()
    if not os.environ.get("FAL_KEY"):
        print("✗ FAL_KEY is not set — run `fal auth login` or export FAL_KEY", file=sys.stderr)
        return EXIT_AUTH

    try:
        uploaded = [_upload_or_url(fal_client, r) for r in refs[:4]]
    except (OSError, FileNotFoundError) as exc:
        print(f"✗ ref upload failed: {exc}", file=sys.stderr)
        emit("failed_ref_upload", error=str(exc))
        return EXIT_SUBMIT
    # This endpoint runs on reference URLs only: all refs (≤4) go to image_urls,
    # referenced as @Image1…@Image4 in the prompt. No forced start/end frame.
    args["image_urls"] = uploaded

    # Quantity: count>1 fires N independent generations (refs uploaded once,
    # reused for every call). Outputs are named v…_1/_2/… via _split_filename.
    requested_count = max(1, int(payload.get("count", 1) or 1))
    if requested_count > _FAL_MAX_COUNT:
        print(f"⚠ count={requested_count} exceeds cap {_FAL_MAX_COUNT}; clamping to avoid runaway spend", file=sys.stderr)
        emit("count_clamped", requested=requested_count, capped=_FAL_MAX_COUNT)
        requested_count = _FAL_MAX_COUNT
    planned = [_split_filename(filename, i, requested_count) for i in range(requested_count)]
    emit("prepared", endpoint=model, requested_count=requested_count)

    def _on_subscribe_timeout(_signum, _frame):
        raise TimeoutError("fal subscribe watchdog fired — queue/websocket stalled")

    def on_queue_update(update):
        if quiet:
            return
        for log in (getattr(update, "logs", None) or []):
            msg = log.get("message") if isinstance(log, dict) else str(log)
            if msg:
                print(msg)

    overall_exit = EXIT_OK  # first non-OK code wins
    completed = 0
    for idx in range(requested_count):
        out_filename = planned[idx]
        tag = f"[{idx + 1}/{requested_count}] " if requested_count > 1 else ""
        try:
            out_target = _reserve_filename(gallery, out_filename, force=payload.get("force", False))
        except FileExistsError:
            print(f"✗ {tag}destination exists: {gallery / out_filename} — pass force:true", file=sys.stderr)
            emit("failed_collision", result_index=idx, destination=str(gallery / out_filename))
            if overall_exit == EXIT_OK:
                overall_exit = EXIT_SUBMIT
            continue

        request_id = ""
        try:
            # Watchdog (2026-06-11): fal_client.subscribe() has NO timeout and
            # can block forever on a stalled queue/websocket — two wrappers from
            # 06-06 were found still alive on 06-10. SIGALRM turns a hang into
            # a normal failed_submit so the process always terminates.
            wait_timeout = int(payload.get("wait_timeout_seconds", 900) or 900)
            signal.signal(signal.SIGALRM, _on_subscribe_timeout)
            signal.alarm(wait_timeout)
            try:
                result = fal_client.subscribe(
                    model, arguments=args, with_logs=True, on_queue_update=on_queue_update,
                )
            finally:
                signal.alarm(0)
            if isinstance(result, dict):
                request_id = str(result.get("request_id") or result.get("id") or "")
            result_url = _extract_video_url(result)
        except Exception as exc:  # noqa: BLE001
            _release_placeholder(out_target)
            print(f"✗ {tag}fal submit/wait failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            emit("failed_submit", result_index=idx, error=f"{type(exc).__name__}: {exc}")
            if overall_exit == EXIT_OK:
                overall_exit = EXIT_SUBMIT
            continue

        if not _download(result_url, out_target):
            _release_placeholder(out_target)
            print(f"✗ {tag}download failed for {out_target.name}", file=sys.stderr)
            emit("failed_download", result_index=idx, redacted_url=_redact_url(result_url))
            if overall_exit == EXIT_OK:
                overall_exit = EXIT_DOWNLOAD
            continue

        size_bytes = out_target.stat().st_size
        # idx 0 mutates the staged draft row (asset_id); extra outputs are new rows.
        row_payload = dict(payload)
        row_payload["asset_id"] = payload.get("asset_id") if idx == 0 else None
        if not _write_db_row(row_payload, out_target, payload["prompt"], refs, request_id, result_url):
            print(f"✓ {tag}{out_target.name} (✗ db write failed)", file=sys.stderr)
            emit("partial_completed_no_db", result_index=idx, size_bytes=size_bytes)
            if overall_exit == EXIT_OK:
                overall_exit = EXIT_OTHER
            continue

        elapsed = round(time.time() - started_at, 1)
        completed += 1
        print(f"✓ {tag}{out_target.name} + db | {round(size_bytes / 1024 / 1024, 2)}MB | fal | {elapsed}s")
        emit("completed", result_index=idx, size_bytes=size_bytes,
             elapsed_seconds=elapsed, redacted_url=_redact_url(result_url))

    if requested_count > 1:
        print(f"— fal batch: {completed}/{requested_count} completed")
    return overall_exit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--payload-file", help="Path to payload JSON")
    g.add_argument("--payload-stdin", action="store_true", help="Read payload from stdin")
    ap.add_argument("--dry-run", action="store_true", help="Validate + plan, no spend")
    ap.add_argument("--quiet", action="store_true", help="Suppress queue log stdout")
    ap.add_argument("--log-path", default=None, help="Explicit per-fire log path")
    ap.add_argument(
        "--notify-server",
        default=os.environ.get("VC_CANVAS_URL", "http://127.0.0.1:8770"),
        help="Gallery server URL to notify on fire start/complete. Empty disables.",
    )
    args = ap.parse_args()

    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"ERROR: stdin is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_SCHEMA

    filename = payload.get("filename", "unknown")
    log_path = Path(args.log_path).expanduser() if args.log_path else hf_log_path(filename)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    log_fp = None
    try:
        log_fp = open(log_path, "w", encoding="utf-8")
        log_fp.write(
            f"# fal_gen_with_sidecar log — {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
            f"# filename: {filename}\n"
            f"# dry_run: {args.dry_run} · quiet: {args.quiet}\n"
            f"# argv: {' '.join(sys.argv)}\n"
            "# ──────────────────────────────────────────────────\n"
        )
        sys.stdout = _TeeWriter(real_stdout, log_fp)
        sys.stderr = _TeeWriter(real_stderr, log_fp)
    except OSError as exc:
        print(f"[warn] log capture disabled: {exc}", file=real_stderr)

    notify_url = getattr(args, "notify_server", "") or ""
    if notify_url and not args.dry_run:
        _notify_server(notify_url, "/api/fires", {
            "pid": os.getpid(),
            "asset_id": payload.get("asset_id"),
            "filename": filename,
            "shot_id": payload.get("shot_id"),
            "model": payload.get("model"),
            "workflow": payload.get("workflow"),
            "client": payload.get("client"),
            "project": payload.get("project"),
            "started_at": time.time(),
            "log_path": str(log_path),
            "payload_file": getattr(args, "payload_file", None),
        })

    try:
        exit_code = run(payload, dry_run=args.dry_run, quiet=args.quiet)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ unhandled exception: {type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = EXIT_OTHER

    if exit_code != EXIT_OK and not args.dry_run:
        cls = EXIT_TO_CLASS.get(exit_code, "unexpected")
        summary = f"✗ {filename} | {cls} | see log for detail (exit {exit_code}) | log: {log_path}"
        try:
            print(summary, file=real_stdout)
        except Exception:
            print(summary)

    if notify_url and not args.dry_run:
        _notify_server(notify_url, f"/api/fires/{os.getpid()}/complete", {
            "exit_code": exit_code,
            "finished_at": time.time(),
        })

    sys.stdout = real_stdout
    sys.stderr = real_stderr
    if log_fp is not None:
        try:
            log_fp.write(f"# ──────────────────────────────────────────────────\n# exit: {exit_code}\n")
            log_fp.close()
        except Exception:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
