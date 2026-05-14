#!/usr/bin/env python3
"""vc_gallery_obs — observability primitives for the gallery stack.

Single source of truth for:
- Structured event logging (JSONL with stable schema)
- Health snapshots (DB writable, folder mounted, wrapper present)
- Orphan introspection (0-byte placeholders, missing-on-disk rows, stale drafts)
- Recent event tail (for debug endpoints)
- Wrapper failure audit sidecars (`.failed.json` next to where the file would have landed)

Designed so the server (vc_gallery_serve.py), the scanner (vc_gallery_scan.py),
the wrapper (hf_gen_with_sidecar.py), and one-shot scripts (cleanup, test harness)
all share ONE event vocabulary. When something breaks at 2am, the data to debug
it is already on disk.

Event schema (every record has these keys):
    ts            ISO-8601 UTC timestamp
    event         dotted name (e.g. "asset.rename", "wrapper.failed_submit")
    source        which component wrote it: "server" | "scan" | "wrapper" | "cleanup" | "test"
    severity      "info" | "warn" | "error"
    ... plus event-specific fields
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from jsonl_append import append_jsonl  # type: ignore
except ImportError:  # pragma: no cover - jsonl_append must exist alongside
    def append_jsonl(path: str, rec: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record_event(
    log_path: Path | str,
    event: str,
    *,
    source: str,
    severity: str = "info",
    **fields: Any,
) -> None:
    """Append one structured event to the gallery's visual_chef.jsonl."""
    rec: dict[str, Any] = {
        "ts": now_iso(),
        "event": event,
        "source": source,
        "severity": severity,
    }
    rec.update(fields)
    try:
        append_jsonl(str(log_path), rec)
    except OSError as e:
        print(f"[obs] WARN: append failed: {e}", file=sys.stderr)


def write_failure_sidecar(target: Path, payload: dict[str, Any]) -> Path | None:
    """Write a `<target>.failed.json` next to a failed gen so debug context survives.

    Returns the sidecar path on success, None on error. Idempotent — overwrites.
    """
    sidecar = target.parent / f"{target.name}.failed.json"
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": now_iso(),
            "target": str(target),
            **payload,
        }
        sidecar.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        return sidecar
    except OSError as e:
        print(f"[obs] WARN: failure sidecar write failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Health snapshot
# ---------------------------------------------------------------------------

def health_snapshot(
    gallery_folder: Path,
    db_path: Path,
    *,
    wrapper_script: Path | None = None,
) -> dict[str, Any]:
    """Return a single dict describing the health of one gallery."""
    checks: dict[str, Any] = {
        "ok": True,
        "ts": now_iso(),
        "gallery_folder": str(gallery_folder),
        "db_path": str(db_path),
    }

    # 1. Folder exists + readable
    try:
        checks["gallery_folder_exists"] = gallery_folder.is_dir()
        if not checks["gallery_folder_exists"]:
            checks["ok"] = False
    except OSError as e:
        checks["gallery_folder_exists"] = False
        checks["gallery_folder_error"] = str(e)
        checks["ok"] = False

    # 2. DB exists + readable + writable
    checks["db_exists"] = db_path.exists()
    if checks["db_exists"]:
        try:
            conn = sqlite3.connect(str(db_path), timeout=2.0)
            conn.execute("PRAGMA busy_timeout = 2000")
            checks["db_asset_count"] = conn.execute("SELECT count(*) FROM assets").fetchone()[0]
            # Probe writability via a no-op transaction
            conn.execute("BEGIN")
            conn.execute("ROLLBACK")
            checks["db_writable"] = True
            # WAL mode check
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            checks["db_journal_mode"] = mode
            conn.close()
        except sqlite3.Error as e:
            checks["db_writable"] = False
            checks["db_error"] = str(e)
            checks["ok"] = False
    else:
        checks["ok"] = False

    # 3. Wrapper script present
    if wrapper_script:
        checks["wrapper_script"] = str(wrapper_script)
        checks["wrapper_script_present"] = wrapper_script.exists()
        if not checks["wrapper_script_present"]:
            checks["ok"] = False

    # 4. Pollution count — 0-byte placeholders sitting in gallery
    if checks.get("gallery_folder_exists"):
        try:
            zero_byte = 0
            for p in gallery_folder.iterdir():
                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm"}:
                    if p.stat().st_size == 0:
                        zero_byte += 1
            checks["zero_byte_files"] = zero_byte
            if zero_byte > 0:
                checks["zero_byte_warning"] = f"{zero_byte} 0-byte placeholder files in gallery — run cleanup"
        except OSError as e:
            checks["zero_byte_scan_error"] = str(e)

    return checks


# ---------------------------------------------------------------------------
# Orphan introspection
# ---------------------------------------------------------------------------

def find_orphans(gallery_folder: Path, db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Return rows + files that need attention.

    `zero_byte_rows` — DB rows with size_bytes=0 (placeholders that never filled)
    `zero_byte_files` — on-disk 0-byte media files (regardless of DB presence)
    `missing_on_disk` — DB rows whose file_path no longer exists on disk
    `db_orphans_on_disk` — on-disk files with no DB row
    """
    out: dict[str, list[dict[str, Any]]] = {
        "zero_byte_rows": [],
        "zero_byte_files": [],
        "missing_on_disk": [],
        "db_orphans_on_disk": [],
    }

    if not db_path.exists() or not gallery_folder.is_dir():
        return out

    media_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm"}

    conn = sqlite3.connect(str(db_path), timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        # zero_byte_rows — exclude draft rows (synthetic, size_bytes=0 by design)
        rows = conn.execute(
            "SELECT id, filename, file_path, status, model FROM assets "
            "WHERE (size_bytes = 0 OR size_bytes IS NULL) AND status != 'draft'"
        ).fetchall()
        out["zero_byte_rows"] = [dict(r) for r in rows]

        # missing_on_disk — also exclude drafts (their file_path is synthetic)
        all_paths = conn.execute(
            "SELECT id, filename, file_path, status, size_bytes FROM assets WHERE status != 'draft'"
        ).fetchall()
        on_disk_set = set()
        for p in gallery_folder.iterdir():
            if p.is_file() and p.suffix.lower() in media_exts:
                on_disk_set.add(str(p.resolve()))
                if p.stat().st_size == 0:
                    out["zero_byte_files"].append({
                        "filename": p.name,
                        "file_path": str(p),
                        "mtime": p.stat().st_mtime,
                        "age_seconds": time.time() - p.stat().st_mtime,
                    })

        db_paths = set()
        for r in all_paths:
            db_paths.add(r["file_path"])
            if r["file_path"] not in on_disk_set:
                out["missing_on_disk"].append(dict(r))

        for p_str in on_disk_set - db_paths:
            out["db_orphans_on_disk"].append({"file_path": p_str, "filename": Path(p_str).name})
    finally:
        conn.close()

    return out


# ---------------------------------------------------------------------------
# Recent event tail
# ---------------------------------------------------------------------------

TEST_FILENAME_PATTERNS = ("ZZZ_TEST_", "VC_TEST_")
TEST_EVENT_PREFIXES = ("test.",)  # event-NAME prefix only, not filename substring


def _is_test_event(rec: dict) -> bool:
    """Heuristic — test events leave fingerprints in filename/asset_id/source.

    H2 fix: previously `"test."` was matched as a SUBSTRING of `filename`, which
    false-positive'd on `latest.png`, `bestest.png`, `prototest.mp4` etc. Now:
      - filename match requires the uppercased TEST_FILENAME_PATTERNS prefixes
        (those are real test-harness fingerprints)
      - event-NAME match requires the test prefix at the start of `rec.event`
    """
    fn = (rec.get("filename") or "")
    if any(p in fn for p in TEST_FILENAME_PATTERNS):
        return True
    ev = (rec.get("event") or "")
    if any(ev.startswith(p) for p in TEST_EVENT_PREFIXES):
        return True
    src = (rec.get("source") or "")
    if src in ("test", "vc_gallery_test"):
        return True
    return False


def tail_events(
    log_path: Path | str,
    n: int = 50,
    event_filter: str | None = None,
    *,
    include_test: bool = False,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    """Return the last n events from the JSONL log, newest first.

    Patch 2026-05-14:
      - skips obvious test-harness events by default (`ZZZ_TEST_`, `VC_TEST_`,
        `test.` prefixes, or `source=test`). Pass `include_test=True` to see them.
      - new `severity` arg filters by severity level (info|warn|error).
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_filter and event_filter not in (rec.get("event") or ""):
                    continue
                if severity and (rec.get("severity") or "info") != severity:
                    continue
                if not include_test and _is_test_event(rec):
                    continue
                out.append(rec)
    except OSError as e:
        return [{"event": "obs.tail_error", "error": str(e)}]
    return list(reversed(out[-n:]))


# ---------------------------------------------------------------------------
# CLI for quick diagnosis
# ---------------------------------------------------------------------------

def _resolve_gallery_paths(folder_arg: str | None) -> tuple[Path, Path, Path]:
    if folder_arg:
        gallery = Path(folder_arg).expanduser().resolve()
    else:
        env = os.environ.get("VC_GALLERY_ROOT")
        gallery = Path(env).expanduser().resolve() if env else Path.cwd()
    db = gallery / ".visual_chef" / "visual_chef.db"
    log = gallery / ".visual_chef" / "visual_chef.jsonl"
    return gallery, db, log


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="vc-gallery observability CLI")
    ap.add_argument("cmd", choices=["health", "orphans", "tail"], help="What to print")
    ap.add_argument("--gallery", help="Gallery folder (defaults to $VC_GALLERY_ROOT or cwd)")
    ap.add_argument("--n", type=int, default=50, help="For tail: number of records")
    ap.add_argument("--filter", default=None, help="For tail: substring match on event name")
    ap.add_argument("--json", action="store_true", help="Print as JSON instead of human")
    args = ap.parse_args(argv)

    gallery, db, log = _resolve_gallery_paths(args.gallery)

    if args.cmd == "health":
        snap = health_snapshot(gallery, db)
        if args.json:
            print(json.dumps(snap, indent=2, ensure_ascii=False))
        else:
            ok = "✓" if snap.get("ok") else "✗"
            print(f"{ok} gallery: {snap['gallery_folder']}")
            for k, v in snap.items():
                if k in ("ok", "gallery_folder", "ts"):
                    continue
                print(f"    {k}: {v}")
        return 0 if snap.get("ok") else 1

    if args.cmd == "orphans":
        orphans = find_orphans(gallery, db)
        if args.json:
            print(json.dumps(orphans, indent=2, ensure_ascii=False))
        else:
            for k, items in orphans.items():
                print(f"\n[{k}] — {len(items)} items")
                for item in items[:20]:
                    print(f"  {item.get('filename') or item.get('file_path')}")
                if len(items) > 20:
                    print(f"  … and {len(items) - 20} more")
        return 0

    if args.cmd == "tail":
        events = tail_events(log, n=args.n, event_filter=args.filter)
        if args.json:
            print(json.dumps(events, indent=2, ensure_ascii=False))
        else:
            for rec in events:
                ts = rec.get("ts", "?")[:19]
                src = rec.get("source", "?")
                ev = rec.get("event", "?")
                sev = rec.get("severity", "info")
                marker = {"info": " ", "warn": "!", "error": "✗"}.get(sev, "?")
                rest = {k: v for k, v in rec.items() if k not in ("ts", "event", "source", "severity")}
                rest_short = json.dumps(rest, ensure_ascii=False)[:120]
                print(f"{marker} {ts}  {src:8s}  {ev:30s}  {rest_short}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
