#!/usr/bin/env python3
"""hf_gen_with_sidecar — fire one Higgsfield generation end-to-end.

The keystone of Phase 2. Hides behind hf-runner subagent (boundary).
Single transport call: payload in → image(s) + sidecar(s) + ledger row(s) out.

PIPELINE (resilient, split-submit-then-wait):
   1. parse + schema-validate payload (hf_payload.schema.json)
   2. atomic O_CREAT|O_EXCL filename reservation in gallery (one per output)
   3. ledger row: status=prepared
   4. higgsfield generate create <model> ... --json   (no --wait yet)
   5. parse envelope → capture job_id(s)
   6. ledger row: status=submitted (job_id fsynced before --wait)
   7. for each job: higgsfield generate wait <job_id> --json --timeout <N>s
   8. parse final envelope → result_url
   9. curl result_url → gallery placeholder (per output)
  10. write sidecar via write_companion_note (per output)
  11. ledger row: status=completed (per output, with result_index)
  12. private safety-net log (~/.cache/visual-chef/, 0600, redacted)
  13. one terse line on stdout per output

MULTI-OUTPUT CONTRACT (count > 1):
   Higgsfield's `--batch_size N` (gpt_image_2 / imagegen_2_0) returns N distinct
   job_ids on submit, each with its own result_url after wait. The wrapper:
     - Reserves N filenames upfront: name.png → name_1.png, name_2.png, …, name_N.png
     - Waits + downloads + sidecars EACH job independently
     - Emits one ledger `completed` event per output with `result_index` field
     - Returns success only when ALL outputs land cleanly; first non-OK code
       wins on partial failure (successful outputs remain on disk)
   When count == 1 (or unset) the original single-file path is preserved
   bit-for-bit — filename stays `name.png` (no `_1` suffix).
   Idempotency keys per output derive from parent payload + result index.

EXIT CODES:
   0  success (all outputs landed cleanly)
   1  generic / unexpected
   2  auth expired (exit cleanly so subagent can surface to director)
   3  filename collision (target already exists, no --force)
   4  payload validation failed (no CLI call made)
   5  download failed (job succeeded server-side; recoverable from ledger)
   6  sidecar write failed (image saved, partial success)
   7  CLI submit failed (network / model / quota)
   8  CLI wait failed (job marked failed/cancelled)

Invocation:
   hf_gen_with_sidecar.py --payload-stdin <<< '<json>'
   hf_gen_with_sidecar.py --payload-file payload.json
   hf_gen_with_sidecar.py --payload-stdin --dry-run     # validate + plan, no spend
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from validate_hf_payload import validate as validate_payload  # noqa: E402
from hf_envelope_parse import parse_generate_response  # noqa: E402
from jsonl_append import append_jsonl  # noqa: E402

# ──────────────────────────────────────────────────────────────────
# Exit codes
# ──────────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_OTHER = 1
EXIT_AUTH = 2
EXIT_COLLISION = 3
EXIT_SCHEMA = 4
EXIT_DOWNLOAD = 5
EXIT_SIDECAR = 6
EXIT_SUBMIT = 7
EXIT_WAIT = 8

# Symbolic class tags for the one-line failure summary (Option B — direct-call.md).
# Agents read these to route recovery without parsing exit codes numerically.
EXIT_TO_CLASS = {
    EXIT_OK: "ok",
    EXIT_OTHER: "unexpected",
    EXIT_AUTH: "auth_expired",
    EXIT_COLLISION: "collision",
    EXIT_SCHEMA: "schema_invalid",
    EXIT_DOWNLOAD: "download_failed",
    EXIT_SIDECAR: "sidecar_failed",
    EXIT_SUBMIT: "submit_failed",
    EXIT_WAIT: "wait_failed",
}

DEFAULT_WAIT_TIMEOUT_SECONDS = 600  # 10 min — covers Seedance/Kling/Veo video gens; image gens still finish in <2 min
SUBPROCESS_TIMEOUT_BUFFER = 30  # extra seconds for CLI graceful exit after its --timeout fires


def _notify_server(server_url: str, path: str, payload: dict) -> None:
    """Best-effort POST to the gallery server. Swallows all errors so a dead
    server never breaks a generation. (#28 — agent-fired wrapper visibility.)"""
    if not server_url:
        return
    try:
        from urllib.request import Request, urlopen
        url = server_url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        urlopen(req, timeout=3)
    except Exception:
        pass  # best-effort — never block the gen

# ──────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = _HERE.parent.parent  # arsenal/00-utilities/.. → project root
SAFETY_DIR = Path.home() / ".cache" / "visual-chef"
LOGS_DIR = SAFETY_DIR / "hf_logs"


def safety_log_path() -> Path:
    return SAFETY_DIR / f"hf_safety_{time.strftime('%Y%m%d')}.jsonl"


def hf_log_path(filename: str) -> Path:
    """Per-fire log file at ~/.cache/visual-chef/hf_logs/<stem>_<timestamp>.log.

    Captures the full transcript of one wrapper invocation (validation, dry-run
    plan, CLI argv, stdout/stderr, ledger emissions) so even with --quiet the
    verbose detail is inspectable on demand.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem if filename else "unknown"
    timestamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    return LOGS_DIR / f"{stem}_{timestamp}.log"


class _TeeWriter:
    """File-like writer that writes to multiple sinks. Used to mirror
    stdout/stderr into the per-fire log file while still printing to terminal.

    Resilient to one sink raising (e.g. closed log fp during shutdown) — keeps
    writing to the rest. Implements the minimal `write` + `flush` + `isatty`
    surface needed by code that prints to sys.stdout / sys.stderr.
    """

    def __init__(self, *sinks):
        self.sinks = sinks

    def write(self, data):
        for s in self.sinks:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        for s in self.sinks:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        for s in self.sinks:
            try:
                if s.isatty():
                    return True
            except Exception:
                continue
        return False


def ledger_path(client: str, project: str) -> Path:
    return PROJECT_ROOT / "clients" / client / project / ".hf_runs.jsonl"


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _redact_url(url: Optional[str]) -> str:
    """Strip signed-URL query string. CloudFront URLs carry signature in querystring."""
    if not url:
        return ""
    if "?" in url:
        return url.split("?", 1)[0] + "?<sig-redacted>"
    return url


def _redact_prompt(prompt: str, max_chars: int = 80) -> dict:
    """Truncate prompt for logs + include sha256 of full prompt."""
    if not prompt:
        return {"prompt_preview": "", "prompt_sha256": ""}
    truncated = prompt[:max_chars]
    if len(prompt) > max_chars:
        truncated += "…"
    h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    return {"prompt_preview": truncated, "prompt_sha256": h}


def _idempotency_key(payload: dict, run_id: str, result_index: Optional[int] = None) -> str:
    """sha256(payload_canonical + run_id [+ result_index]).

    Same payload + run_id → same key. When result_index is provided (multi-output
    fan-out), it's mixed in so each output gets a distinct key derived from the
    same parent run.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    suffix = f"|{run_id}" if result_index is None else f"|{run_id}|{result_index}"
    return hashlib.sha256(f"{canonical}{suffix}".encode("utf-8")).hexdigest()[:16]


def _split_filename(filename: str, idx: int, total: int) -> str:
    """Return per-output filename. Single-output → unchanged; multi-output → `name_<idx+1>.<ext>`.

    Backward-compat: when total == 1 the original filename is returned exactly,
    so the existing count=1 path is preserved bit-for-bit. When total > 1, idx
    is 0-indexed and the human-readable suffix is 1-indexed (`_1`, `_2`, …).
    """
    if total <= 1:
        return filename
    p = Path(filename)
    return f"{p.stem}_{idx + 1}{p.suffix}"


def _reserve_filename(gallery: Path, filename: str, force: bool) -> Path:
    """Atomic O_CREAT|O_EXCL placeholder. Returns final path on success.

    Raises FileExistsError on collision (unless force=True replaces).
    Auto-recovers from 0-byte orphans left by previous failed runs (timeout,
    NSFW reject, crash) — those are NOT real collisions.
    """
    gallery.mkdir(parents=True, exist_ok=True)
    target = gallery / filename
    if target.exists():
        # 0-byte orphan from a prior failed run is safe to clear — no real content lost.
        if target.stat().st_size == 0:
            target.unlink()
        elif force:
            target.unlink()
        # else: real file with content — fall through to O_EXCL which will raise
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(target), flags, 0o644)
    os.close(fd)  # zero-byte placeholder
    return target


def _release_placeholder(target: Path) -> None:
    """Remove placeholder if it's still zero bytes (i.e. download didn't fill it)."""
    try:
        if target.exists() and target.stat().st_size == 0:
            target.unlink()
    except OSError:
        pass


def _run_higgsfield(args: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run higgsfield CLI. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["higgsfield", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, "", "higgsfield CLI not on PATH"


def _detect_auth_error(stderr: str) -> bool:
    """Heuristic: stderr contains auth-expired markers."""
    if not stderr:
        return False
    markers = ("Not authenticated", "Session expired", "auth login")
    return any(m in stderr for m in markers)


def _build_create_argv(payload: dict) -> list[str]:
    """Translate validated payload into `higgsfield generate create` argv.

    Uses positional model + --flag value pairs. List-typed Python passes
    through; subprocess.run handles escaping.
    """
    model = payload.get("model", "nano_banana_2")
    args = ["generate", "create", model, "--json"]
    args += ["--prompt", payload["prompt"]]
    if "aspect_ratio" in payload:
        args += ["--aspect_ratio", payload["aspect_ratio"]]
    if "quality" in payload:
        args += ["--quality", payload["quality"]]
    if "resolution" in payload:
        args += ["--resolution", payload["resolution"]]
    if "count" in payload and payload["count"] > 1:
        # gpt_image_2 uses batch_size; other models use count
        _count_flag = "--batch_size" if payload.get("model") == "gpt_image_2" else "--count"
        args += [_count_flag, str(payload["count"])]
    if "duration" in payload:
        args += ["--duration", str(payload["duration"])]
    if "mode" in payload:
        args += ["--mode", payload["mode"]]
    if "genre" in payload:
        args += ["--genre", payload["genre"]]
    if "sound" in payload:
        args += ["--sound", payload["sound"]]
    if "seed" in payload:
        args += ["--seed", str(payload["seed"])]
    # Media flags — schema accepts string OR array for each; CLI accepts repeated flags
    # (multi-ref face-swap, multi-character identity, multi-clip stitch, etc.).
    # Per `higgsfield generate create --help`: "Media flags: --image, --start-image, --end-image, --video, --audio."
    _media_flags = (
        ("image", "--image"),
        ("start_image", "--start-image"),
        ("end_image", "--end-image"),
        ("video", "--video"),
        ("audio", "--audio"),
    )
    for payload_key, cli_flag in _media_flags:
        if payload_key in payload:
            values = payload[payload_key] if isinstance(payload[payload_key], list) else [payload[payload_key]]
            for v in values:
                args += [cli_flag, v]
    if "soul_id" in payload:
        args += ["--soul-id", payload["soul_id"]]
    return args


def _download(url: str, target: Path) -> bool:
    """curl URL to target with retry + hard read-back verification.

    Issue #109 (recurring 2026-05-29/30) — wrapper reported '✓ + db | exit: 0'
    but the PNG was absent on disk. Two root-cause candidates:
    1. Transient CDN error that curl considered non-fatal (empty 200 body).
    2. OS page-cache / APFS-sparse returning a stale st_size that made
       stat() think the write succeeded before it was flushed.

    Mitigations:
    1. curl --retry 3 --retry-delay 2 --retry-all-errors so transient
       CDN 5xx/timeouts are retried transparently inside the subprocess.
    2. Post-download: open() + read(1) to force a real disk read — not just
       stat(). This confirms the file is physically on disk, not just in the
       kernel write-back buffer.
    3. If the first attempt yields a zero-byte file (empty 200 race),
       wait 3s then retry once before reporting failure.
    """
    import time as _time
    import sys as _sys

    def _attempt() -> bool:
        try:
            proc = subprocess.run(
                [
                    "curl", "-sSL",
                    "--retry", "3", "--retry-delay", "2",
                    "--retry-all-errors",
                    "-o", str(target), url,
                ],
                capture_output=True,
                text=True,
                timeout=360,
            )
            if proc.returncode != 0:
                return False
            if not target.exists():
                return False
            if target.stat().st_size == 0:
                return False
            # Hard read-back: open() + read(1) forces kernel to confirm
            # the file is readable on disk (not just buffered).
            with open(target, "rb") as fh:
                if not fh.read(1):
                    return False
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    if _attempt():
        return True

    # Retry once after a pause — catches transient CDN races (empty 200 body)
    print("  ⚠ download: first attempt yielded empty/missing file, retrying in 3s…",
          file=_sys.stderr)
    # Remove any zero-byte partial before retry so curl starts clean
    try:
        if target.exists() and target.stat().st_size == 0:
            target.unlink()
    except OSError:
        pass
    _time.sleep(3)
    return _attempt()


def _write_db_row(payload: dict, target: Path, prompt: str, refs: list[str], job_id: Optional[str] = None) -> bool:
    """Write the asset directly to the gallery DB (no sidecar). Used when
    payload['skip_sidecar'] is true — the dashboard becomes the truth.

    Issue #27 — when `payload['asset_id']` is present (set by the server's
    _fire_draft path), the row at that id is MUTATED in place rather than
    inserting a new row. This is what merges the draft and the post-fire
    asset into ONE logical row instead of the historical two-row split.
    """
    try:
        if str(_HERE) not in sys.path:
            sys.path.insert(0, str(_HERE))
        import vc_gallery_lib as lib_local
        db_path = lib_local.db_path_for(payload["gallery"])
        conn = lib_local.connect(db_path)
        hf_url = f"https://higgsfield.ai/asset/all/{job_id}" if job_id else ""
        metadata = {
            "status": payload.get("status", "review"),
            "source_type": "generated",  # wrapper-produced — bypass filename heuristic
            "model": payload.get("model", "unknown"),
            "workflow": payload["workflow"],
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
            "hf_job_id": job_id or "",
            "hf_job_url": hf_url,
            "has_sidecar": False,
        }
        # asset_id arrives as int or numeric string (JSON). Tolerate both.
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
    except Exception as e:  # noqa: BLE001
        # Patch 2026-05-14 (Plan-agent audit adj #2): the bare except used to
        # only print to stderr — when --quiet was set, this disappeared into
        # the log file and the caller silently degraded. Now: structured log
        # + traceback + emit an obs event so /api/debug/recent-events surfaces
        # the failure too.
        import traceback as _tb
        tb = _tb.format_exc()
        print(f"[db-write] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        print(tb, file=sys.stderr)
        try:
            if str(_HERE) not in sys.path:
                sys.path.insert(0, str(_HERE))
            import vc_gallery_obs as _obs  # type: ignore
            from vc_gallery_lib import db_path_for as _db_path_for  # type: ignore
            gallery = payload.get("gallery")
            if gallery:
                log_path = Path(_db_path_for(gallery)).parent / "visual_chef.jsonl"
                _obs.record_event(
                    log_path, "wrapper.db_write_failed",
                    source="wrapper", severity="error",
                    filename=payload.get("filename"),
                    error_class=type(e).__name__,
                    error_message=str(e),
                    target=str(target),
                    workflow=payload.get("workflow"),
                    model=payload.get("model"),
                )
        except Exception:  # noqa: BLE001 — best-effort log; never swallow original
            pass
        return False


def _write_sidecar(payload: dict, target: Path, prompt: str, refs: list[str], job_id: Optional[str] = None) -> bool:
    """Call write_media_sidecar.py to create the .md companion.

    When `job_id` is provided, the sidecar gets an `hf_job_url:` field with
    a click-through to the Higgsfield asset page — used by the gallery
    dashboard to surface an "Open in Higgsfield" action.
    """
    hf_job_url = f"https://higgsfield.ai/asset/all/{job_id}" if job_id else ""
    cmd = [
        "python3",
        str(_HERE / "write_media_sidecar.py"),
        str(target),
        "--client", payload["client"],
        "--project", payload["project"],
        "--model", payload.get("model", "unknown"),
        "--workflow", payload["workflow"],
        "--prompt", prompt,
        "--status", payload.get("status", "review"),
        "--variant", payload.get("variant", ""),
        "--pass", str(payload.get("pass_num", 1)),
        "--shot-id", payload.get("shot_id", ""),
        "--parent", payload.get("parent", ""),
        "--session", payload.get("session", ""),
        "--session-date", payload.get("session_date", ""),
        "--notes", payload.get("notes", ""),
        "--hf-job-url", hf_job_url,
    ]
    for ref in refs:
        cmd += ["--refs", ref]
    for tag in payload.get("extra_tags", []):
        cmd += ["--tag", tag]
    if payload.get("force"):
        cmd += ["--force"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ──────────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────────

def run(payload: dict, dry_run: bool = False, gallery_root: Optional[str] = None, quiet: bool = False) -> int:
    run_id = uuid.uuid4().hex[:12]
    started_at = time.time()
    client = payload.get("client", "unknown")
    project = payload.get("project", "unknown")
    gallery = Path(payload["gallery"]).resolve()
    filename = payload["filename"]
    target = gallery / filename  # primary target for early ledger emission
    timeout = payload.get("wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)
    requested_count = max(1, int(payload.get("count", 1)))

    # Helper for intermediate stdout prints (dry-run plan, count_mismatch warnings).
    # Suppressed in --quiet mode (still captured by the log file via stdout tee).
    # Not used for the final ✓/✗ summary — those always print directly.
    def _say(msg: str) -> None:
        if not quiet:
            print(msg)

    base_event = {
        "run_id": run_id,
        "client": client,
        "project": project,
        "model": payload.get("model"),
        "workflow": payload.get("workflow"),
        "shot_id": payload.get("shot_id", ""),
        "destination": str(target),
        **_redact_prompt(payload.get("prompt", "")),
        "idempotency_key": _idempotency_key(payload, run_id),
        "started_at": started_at,
    }

    def emit(status: str, **extra) -> None:
        rec = {**base_event, "status": status, **extra}
        try:
            append_jsonl(ledger_path(client, project), rec)
            append_jsonl(safety_log_path(), rec)
        except OSError as e:
            print(f"[ledger] WARN: append failed: {e}", file=sys.stderr)

    # ─── 0.5. Normalise 'segment' → 'scene' ───
    # UI label renamed to 'Segment' 2026-05-29 but DB column stays 'scene'.
    # Agents that use the new label pass 'segment' in the payload and hit
    # schema rejection. Normalise before validation so both keys work.
    if "segment" in payload and "scene" not in payload:
        payload["scene"] = payload.pop("segment")
    elif "segment" in payload:
        payload.pop("segment")  # scene already set — drop duplicate

    # ─── 1. Schema validation ───
    errors = validate_payload(payload, vc_gallery_root=gallery_root)
    if errors:
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        emit("failed_validation", errors=errors)
        return EXIT_SCHEMA

    if dry_run:
        argv = _build_create_argv(payload)
        _say(f"[dry-run] target: {target}")
        _say(f"[dry-run] argv:   higgsfield {' '.join(argv)}")
        _say(f"[dry-run] timeout: {timeout}s")
        if requested_count > 1:
            planned = [_split_filename(filename, i, requested_count) for i in range(requested_count)]
            _say(f"[dry-run] count={requested_count} → outputs: {planned}")
        emit("dry_run", argv=argv, requested_count=requested_count)
        return EXIT_OK

    # ─── 2. Reserve filename(s) ───
    # MULTI-OUTPUT KAIZEN [2026-05-07]: when count > 1 we reserve N placeholders
    # upfront (one per anticipated output). This applies the same atomic
    # O_CREAT|O_EXCL guarantee per output that the count=1 path has always had.
    # If reservation fails partway, release the ones we did claim before bailing.
    planned_filenames = [_split_filename(filename, i, requested_count) for i in range(requested_count)]
    reserved_targets: list[Path] = []
    try:
        for fname in planned_filenames:
            reserved_targets.append(_reserve_filename(gallery, fname, force=payload.get("force", False)))
    except FileExistsError:
        # Roll back: release any zero-byte placeholders we already created
        for t in reserved_targets:
            _release_placeholder(t)
        # Identify the offender for a useful error message
        offender = gallery / planned_filenames[len(reserved_targets)]
        print(f"✗ destination exists: {offender} — pass force:true or pick different filename", file=sys.stderr)
        emit("failed_collision")
        return EXIT_COLLISION

    # Backward compat: single-output path keeps the original `target` variable name
    target = reserved_targets[0]

    emit("prepared", requested_count=requested_count)

    # ─── 3. Submit (no --wait) ───
    create_argv = _build_create_argv(payload)
    rc, out, err = _run_higgsfield(create_argv, timeout=60)

    if _detect_auth_error(err):
        for t in reserved_targets:
            _release_placeholder(t)
        print("✗ auth expired — run `higgsfield auth login`", file=sys.stderr)
        emit("failed_auth", stderr=err.strip())
        return EXIT_AUTH

    if rc != 0:
        for t in reserved_targets:
            _release_placeholder(t)
        print(f"✗ submit failed (rc={rc}): {err.strip()[:200]}", file=sys.stderr)
        emit("failed_submit", returncode=rc, stderr=err.strip()[:500])
        return EXIT_SUBMIT

    parsed = parse_generate_response(out)
    if not parsed.get("success"):
        for t in reserved_targets:
            _release_placeholder(t)
        print(f"✗ submit response unparseable: {parsed.get('error_message', '?')}", file=sys.stderr)
        emit("failed_submit_parse", error=parsed)
        return EXIT_SUBMIT

    jobs = parsed.get("jobs", [])
    if not jobs:
        for t in reserved_targets:
            _release_placeholder(t)
        print("✗ submit returned no jobs", file=sys.stderr)
        emit("failed_no_jobs", parsed=parsed)
        return EXIT_SUBMIT

    submit_mode = parsed.get("mode", "submit_only")

    # MULTI-OUTPUT KAIZEN [2026-05-07]: Higgsfield's batch_size returns N
    # distinct job_ids (one per output) on submit. Previously the wrapper only
    # consumed jobs[0] and silently dropped the rest — billed but never
    # downloaded. Now we iterate per output.
    if len(jobs) != requested_count:
        # Mismatch is recoverable per-output but worth flagging — the API may
        # have rejected some, returned extras, or shifted shape. We pair off
        # min(len(jobs), len(reserved_targets)) and release any orphan slots.
        emit(
            "warning_count_mismatch",
            requested_count=requested_count,
            returned_jobs=len(jobs),
            mode=submit_mode,
        )
        print(
            f"  ⚠ requested count={requested_count} but API returned {len(jobs)} jobs; "
            f"processing {min(len(jobs), len(reserved_targets))} pair(s)",
            file=sys.stderr,
        )

    pair_count = min(len(jobs), len(reserved_targets))
    # Release any unused placeholders if jobs returned < reserved
    for t in reserved_targets[pair_count:]:
        _release_placeholder(t)

    # Capture all submitted job_ids in a single ledger row before any --wait.
    submitted_job_ids = [j.get("id") for j in jobs[:pair_count]]
    emit(
        "submitted",
        job_id=submitted_job_ids[0] if submitted_job_ids else None,
        initial_status=jobs[0].get("status") if jobs else None,
        mode=submit_mode,
        all_job_ids=submitted_job_ids,
        requested_count=requested_count,
    )

    # ─── 4. Wait + 5. Download + 6. Sidecar — per output ───
    refs = list(payload.get("refs", []))
    # Add basenames of every source media field as refs, deduped, preserving order.
    # Patch 2026-05-14 (Plan-agent audit):
    #   - Was: only added when src contained "/" — bare gallery-relative sources
    #     like "nbp_father_yelling.png" were silently dropped from refs.
    #   - Was: simple prepend with no dedup — same ref could appear N times if
    #     listed in multiple media fields (image + start_image), or if already
    #     in payload.refs.
    # Now: take basename unconditionally for any non-URL string; insert at the
    # front; dedupe by lowered basename keeping first occurrence.
    media_refs: list[str] = []
    for media_key in ("image", "start_image", "end_image", "video", "audio"):
        if media_key not in payload:
            continue
        srcs = payload[media_key] if isinstance(payload[media_key], list) else [payload[media_key]]
        for src in srcs:
            if not isinstance(src, str) or not src:
                continue
            if src.startswith(("http://", "https://")):
                # Keep URL refs as-is — useful for MJ CDN paste-ins
                media_refs.append(src)
            else:
                media_refs.append(Path(src).name)
    # Stable de-dupe: media refs first (so they appear at the front), then
    # whatever payload.refs already carries.
    seen: set[str] = set()
    deduped: list[str] = []
    for r in media_refs + refs:
        key = r.lower() if isinstance(r, str) else str(r)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    refs = deduped

    overall_exit = EXIT_OK  # first non-OK code wins
    success_lines: list[str] = []

    for idx in range(pair_count):
        job = jobs[idx]
        out_target = reserved_targets[idx]
        out_filename = planned_filenames[idx]
        job_id = job.get("id")
        initial_status = job.get("status")
        initial_url = job.get("result_url")
        out_idem = _idempotency_key(payload, run_id, result_index=idx)
        # Per-output ledger context — overrides destination + idempotency_key
        # for events specific to this output
        out_ctx = {
            "result_index": idx,
            "result_total": pair_count,
            "destination": str(out_target),
            "idempotency_key": out_idem,
            "job_id": job_id,
        }

        # Bind out_ctx via default arg so the closure captures THIS iteration's
        # context, not whatever the loop variable points to at call time.
        def emit_out(status: str, _ctx: dict = out_ctx, **extra) -> None:
            rec = {**base_event, **_ctx, "status": status, **extra}
            try:
                append_jsonl(ledger_path(client, project), rec)
                append_jsonl(safety_log_path(), rec)
            except OSError as e:
                print(f"[ledger] WARN: append failed: {e}", file=sys.stderr)

        # ─── Wait (skip if create+wait already completed) ───
        final_job = job
        if initial_status != "completed" or not initial_url:
            if not job_id:
                _release_placeholder(out_target)
                print(f"✗ [{idx + 1}/{pair_count}] no job_id from submit; cannot wait", file=sys.stderr)
                emit_out("failed_no_job_id")
                if overall_exit == EXIT_OK:
                    overall_exit = EXIT_WAIT
                continue

            wait_argv = ["generate", "wait", job_id, "--json", "--timeout", f"{timeout}s"]
            rc, out, err = _run_higgsfield(wait_argv, timeout=timeout + SUBPROCESS_TIMEOUT_BUFFER)
            if rc != 0:
                err_short = err.strip()[:200] if err else ""
                recoverable = ("Cannot reach" in err_short or "timeout" in err_short or "network" in err_short.lower())
                recover_hint = (
                    f" — recover: `higgsfield generate wait {job_id} --json --timeout 20m` "
                    f"then curl --output {out_target.name} <result_url>"
                ) if recoverable else ""
                print(f"✗ [{idx + 1}/{pair_count}] wait failed (rc={rc}): {err_short}{recover_hint}", file=sys.stderr)
                emit_out("failed_wait", returncode=rc, stderr=err.strip()[:500])
                # Don't release placeholder — job may still be recoverable from ledger
                if overall_exit == EXIT_OK:
                    overall_exit = EXIT_WAIT
                continue
            final = parse_generate_response(out)

            if not final.get("success"):
                print(f"✗ [{idx + 1}/{pair_count}] wait response unparseable: {final.get('error_message', '?')}", file=sys.stderr)
                emit_out("failed_wait_parse", error=final)
                if overall_exit == EXIT_OK:
                    overall_exit = EXIT_WAIT
                continue

            final_jobs = final.get("jobs", [])
            if final_jobs:
                final_job = final_jobs[0]

        result_url = final_job.get("result_url") or initial_url
        final_status = final_job.get("status")

        if final_status != "completed" or not result_url:
            print(f"✗ [{idx + 1}/{pair_count}] job ended status={final_status!r} url={bool(result_url)}", file=sys.stderr)
            emit_out("failed_job_status", final_status=final_status)
            _release_placeholder(out_target)
            if overall_exit == EXIT_OK:
                overall_exit = EXIT_WAIT
            continue

        # ─── Download ───
        if not _download(result_url, out_target):
            print(f"✗ [{idx + 1}/{pair_count}] download failed for {out_target.name} — recoverable: higgsfield generate get {job_id}", file=sys.stderr)
            emit_out("failed_download", redacted_url=_redact_url(result_url))
            # Successful sibling outputs stay on disk; this slot's placeholder
            # only released if it never received bytes.
            _release_placeholder(out_target)
            if overall_exit == EXIT_OK:
                overall_exit = EXIT_DOWNLOAD
            continue

        size_bytes = out_target.stat().st_size
        size_mb = round(size_bytes / 1024 / 1024, 2)

        # ─── Persist metadata: DB write (default) or legacy sidecar ───
        # Policy 2026-05-13 (director-locked): DB-write is the canonical truth.
        # Sidecars are legacy opt-in only — pass `skip_sidecar: false` to re-enable.
        skip_sidecar = bool(payload.get("skip_sidecar", True))
        if skip_sidecar:
            if not _write_db_row(payload, out_target, payload["prompt"], refs, job_id=job_id):
                print(f"✓ [{idx + 1}/{pair_count}] {out_filename} | {size_mb}MB | job {job_id}  (✗ db write failed)", file=sys.stderr)
                emit_out("partial_completed_no_db", size_bytes=size_bytes)
                if overall_exit == EXIT_OK:
                    overall_exit = EXIT_SIDECAR
                continue
        else:
            if not _write_sidecar(payload, out_target, payload["prompt"], refs, job_id=job_id):
                print(f"✓ [{idx + 1}/{pair_count}] {out_filename} | {size_mb}MB | job {job_id}  (✗ sidecar failed)", file=sys.stderr)
                emit_out("partial_completed_no_sidecar", size_bytes=size_bytes)
                if overall_exit == EXIT_OK:
                    overall_exit = EXIT_SIDECAR
                continue

        # ─── Done (per output) ───
        elapsed = round(time.time() - started_at, 1)
        persist_tag = "db" if skip_sidecar else "sidecar"
        line = f"✓ [{idx + 1}/{pair_count}] {out_filename} + {persist_tag} | {size_mb}MB | job {job_id} | {elapsed}s"
        if pair_count == 1:
            # Backward-compatible single-line stdout for count=1 (no [1/1] prefix)
            line = f"✓ {out_filename} + {persist_tag} | {size_mb}MB | job {job_id} | {elapsed}s"
        success_lines.append(line)
        emit_out("completed", size_bytes=size_bytes, elapsed_seconds=elapsed,
                 redacted_url=_redact_url(result_url))

    for line in success_lines:
        print(line)

    return overall_exit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--payload-file", help="Path to payload JSON")
    g.add_argument("--payload-stdin", action="store_true", help="Read payload from stdin")
    ap.add_argument(
        "--gallery-root",
        default=os.environ.get("VC_GALLERY_ROOT"),
        help="Path that gallery must resolve under (default: $VC_GALLERY_ROOT)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Validate + plan, no CLI spend")
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress intermediate stdout prints (dry-run plan, count_mismatch warnings). "
             "Errors still print to stderr; final ✓/✗ summary still prints to stdout. "
             "Full transcript is always captured at ~/.cache/visual-chef/hf_logs/.",
    )
    ap.add_argument(
        "--log-path",
        default=None,
        help="Explicit per-fire log path. When omitted, falls back to the timestamped "
             "path under ~/.cache/visual-chef/hf_logs/. The dashboard server passes this "
             "so the fire registry can locate the log deterministically (otherwise the "
             "server's precomputed path and the wrapper's timestamped one diverge — C1).",
    )
    ap.add_argument(
        "--notify-server",
        default=os.environ.get("VC_CANVAS_URL", "http://127.0.0.1:8770"),
        help="Gallery server URL to notify on fire start/complete (#28). "
             "Set to empty string to disable. Default: $VC_CANVAS_URL or localhost:8770.",
    )
    args = ap.parse_args()

    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"ERROR: stdin is not valid JSON: {e}", file=sys.stderr)
            return EXIT_SCHEMA

    # ─── Per-fire log capture (Option B — direct-call.md) ───
    # Open a log file BEFORE run() so even early failures (validation, collision)
    # land a transcript on disk. Tee stdout + stderr into it.
    filename = payload.get("filename", "unknown")
    # If the dashboard server passed --log-path, honor it so the fire registry
    # and the wrapper's log file paths stay in sync. Falls back to timestamped
    # default for direct CLI invocations.
    if getattr(args, "log_path", None):
        log_path = Path(args.log_path).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        log_path = hf_log_path(filename)
    log_fp = None
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    try:
        log_fp = open(log_path, "w", encoding="utf-8")
        log_fp.write(
            f"# hf_gen_with_sidecar log — {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
            f"# filename: {filename}\n"
            f"# dry_run: {args.dry_run} · quiet: {args.quiet}\n"
            f"# argv: {' '.join(sys.argv)}\n"
            f"# ──────────────────────────────────────────────────\n"
        )
        log_fp.flush()
        sys.stdout = _TeeWriter(real_stdout, log_fp)
        sys.stderr = _TeeWriter(real_stderr, log_fp)
    except OSError as e:
        # Log capture is best-effort; if it fails, fall back to terminal-only.
        print(f"[warn] log capture disabled: {e}", file=real_stderr)
        log_fp = None

    # Notify gallery server that this fire is starting (#28)
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
        exit_code = run(payload, dry_run=args.dry_run, gallery_root=args.gallery_root, quiet=args.quiet)
    except Exception as e:  # noqa: BLE001 — top-level guard so we always emit summary + close log
        print(f"✗ unhandled exception: {type(e).__name__}: {e}", file=sys.stderr)
        exit_code = EXIT_OTHER

    # ─── Final one-line failure summary ───
    # Success cases already printed their ✓ line(s) inside run(). On failure,
    # emit a single canonical summary so the agent sees a consistent shape:
    #   ✗ <filename> | <error_class> | <one-line cause> | log: <path>
    # The verbose cause is in the log file; this line is the agent's "should I
    # retry / re-route / abort" signal.
    if exit_code != EXIT_OK and not args.dry_run:
        cls = EXIT_TO_CLASS.get(exit_code, "unexpected")
        # Best-effort cause — use the most recent stderr line if available.
        # We don't capture it directly here (would require a buffer); the log
        # file holds the full detail. The class tag + log path is sufficient.
        cause = f"see log for detail (exit {exit_code})"
        summary = f"✗ {filename} | {cls} | {cause} | log: {log_path}"
        # Restore real_stdout for the final print so the summary always reaches
        # the terminal, even if something got weird with the tee.
        try:
            print(summary, file=real_stdout)
        except Exception:
            print(summary)

        # Write a .failed.json audit sidecar next to where the file would have
        # landed. Makes the next debug session ("what blew up overnight?") a
        # one-glance answer. Best-effort — never block exit on this.
        try:
            from pathlib import Path as _P
            import vc_gallery_obs as _obs  # type: ignore
            gallery = payload.get("gallery", "")
            fname = payload.get("filename", "unknown")
            if gallery and fname:
                target = _P(gallery) / fname
                _obs.write_failure_sidecar(target, {
                    "exit_code": exit_code,
                    "error_class": cls,
                    "log_path": str(log_path),
                    "filename": fname,
                    "gallery": str(gallery),
                    "model": payload.get("model"),
                    "workflow": payload.get("workflow"),
                    "shot_id": payload.get("shot_id"),
                    "argv": sys.argv,
                })
        except Exception:
            pass  # observability is best-effort

    # Notify gallery server that this fire is complete (#28)
    if notify_url and not args.dry_run:
        _notify_server(notify_url, f"/api/fires/{os.getpid()}/complete", {
            "exit_code": exit_code,
            "finished_at": time.time(),
        })

    # ─── Close log fp + restore stdio ───
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
    sys.exit(main())
