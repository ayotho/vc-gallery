#!/usr/bin/env python3
"""Cache for Higgsfield media uploads.

Maps sha256(file content) -> {media_id, uploaded_at}. Survives across
subagent spawns so the same source image is uploaded to Higgsfield once.

Subcommands:
  check <file>            -> prints media_id, MISS, or STALE
  save <file> <media_id>  -> records the mapping
  invalidate <file>       -> removes the entry (use when HF reports media gone)
  ls                      -> prints all entries (debug)

TTL is 14 days. Higgsfield doesn't publish a retention guarantee, so we expire
defensively; on a STALE result the caller re-uploads.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

CACHE_PATH = Path.home() / ".cache" / "claude-hf-uploads.json"
TTL_SECONDS = 14 * 24 * 60 * 60


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_PATH.parent, prefix=".hf-upload-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def cmd_check(args):
    h = hash_file(args.file)
    cache = load_cache()
    entry = cache.get(h)
    if not entry:
        print("MISS")
        return
    if time.time() - entry["uploaded_at"] > TTL_SECONDS:
        print("STALE")
        return
    print(entry["media_id"])


def cmd_save(args):
    h = hash_file(args.file)
    cache = load_cache()
    cache[h] = {
        "media_id": args.media_id,
        "uploaded_at": int(time.time()),
        "last_path": os.path.abspath(args.file),
    }
    save_cache(cache)
    print("OK")


def cmd_invalidate(args):
    h = hash_file(args.file)
    cache = load_cache()
    if h in cache:
        del cache[h]
        save_cache(cache)
    print("OK")


def cmd_ls(args):
    cache = load_cache()
    if not cache:
        print("(empty)")
        return
    now = time.time()
    for h, entry in sorted(cache.items(), key=lambda kv: kv[1].get("uploaded_at", 0), reverse=True):
        age_h = (now - entry.get("uploaded_at", 0)) / 3600
        stale = " STALE" if (now - entry.get("uploaded_at", 0)) > TTL_SECONDS else ""
        path = entry.get("last_path", "?")
        print(f"{h[:12]}  {entry['media_id']}  {age_h:6.1f}h  {path}{stale}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Print media_id / MISS / STALE for a file")
    p_check.add_argument("file")
    p_check.set_defaults(func=cmd_check)

    p_save = sub.add_parser("save", help="Save hash -> media_id mapping")
    p_save.add_argument("file")
    p_save.add_argument("media_id")
    p_save.set_defaults(func=cmd_save)

    p_inv = sub.add_parser("invalidate", help="Remove cache entry for a file")
    p_inv.add_argument("file")
    p_inv.set_defaults(func=cmd_invalidate)

    p_ls = sub.add_parser("ls", help="List all cache entries")
    p_ls.set_defaults(func=cmd_ls)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
