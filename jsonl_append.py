#!/usr/bin/env python3
"""jsonl_append — flock-safe append to a JSONL file.

Use case: parallel wrappers writing run-ledger / safety-log entries on a
cloud-synced (Drive / iCloud) project folder. Plain shell append (`>>`) can
interleave or truncate under concurrent writers. `fcntl.flock` + `O_APPEND` +
`os.fsync` prevents that.

Designed to back the HF CLI migration's append-only run ledger
(`clients/<client>/<project>/.hf_runs.jsonl`) and the private safety-net
log (`~/.cache/visual-chef/hf_safety_<date>.jsonl`).

Library use:
    from jsonl_append import append_jsonl
    append_jsonl("/path/to/file.jsonl", {"status": "submitted", "job_id": "..."})

CLI use:
    echo '{"k": "v"}' | jsonl_append.py /path/to/file.jsonl
    jsonl_append.py /path/to/file.jsonl --record '{"k": "v"}'

Properties:
- Atomic per-call append (one record == one fcntl-locked write+fsync)
- File mode 0600 on create (no world-readable logs)
- Auto-creates parent directories
- Auto-injects `_appended_at` (epoch float) so callers don't have to
- Exit codes:  0 success · 1 io error · 2 input error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_WIN = sys.platform == "win32"
if _WIN:
    import msvcrt
else:
    import fcntl

DEFAULT_MODE = 0o600


def append_jsonl(path: str | Path, record: dict, mode: int = DEFAULT_MODE) -> None:
    """Append one record to a JSONL file, holding flock during write+fsync.

    Creates the file (and parent dirs) if missing. Sets file mode on creation
    only — does not chmod existing files.
    """
    if not isinstance(record, dict):
        raise TypeError(f"record must be a dict, got {type(record).__name__}")

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    enriched = {**record, "_appended_at": time.time()}
    line = json.dumps(enriched, ensure_ascii=False, separators=(",", ":")) + "\n"

    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if _WIN:
        flags |= os.O_BINARY
    fd = os.open(str(p), flags, mode)
    try:
        data = line.encode("utf-8")
        if _WIN:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            os.write(fd, data)
            os.fsync(fd)
            # lock released on close
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, data)
            os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="flock-safe single-record append to a JSONL file"
    )
    ap.add_argument("path", help="JSONL file path (created if missing)")
    ap.add_argument(
        "--record",
        help="JSON record as a string. If omitted, read from stdin.",
    )
    ap.add_argument(
        "--mode",
        type=lambda s: int(s, 8),
        default=DEFAULT_MODE,
        help="File mode in octal (default 0600)",
    )
    args = ap.parse_args()

    if args.record is not None:
        raw = args.record
    else:
        raw = sys.stdin.read().strip()
        if not raw:
            print("ERROR: no record on stdin and no --record", file=sys.stderr)
            return 2

    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        return 2

    if not isinstance(rec, dict):
        print(
            f"ERROR: record must be a JSON object, got {type(rec).__name__}",
            file=sys.stderr,
        )
        return 2

    try:
        append_jsonl(args.path, rec, mode=args.mode)
    except (OSError, IOError) as e:
        print(f"ERROR: append failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
