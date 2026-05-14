#!/usr/bin/env python3
"""vc_gallery_thumb — generate 480px-wide JPEG thumbnails via ffmpeg.

One generator, two media types. Images get a downscale-only pass; videos get
their first decodable frame extracted as a still. Output lives in
`<db_parent>/.thumb_cache/<sha1>.jpg` so it travels with the SQLite DB.

Library use:
    from vc_gallery_thumb import ensure_thumb
    rel = ensure_thumb(source_abs_path, thumb_cache_dir)   # → "abcdef.jpg" or None

CLI use:
    python3 vc_gallery_thumb.py <source_file> <thumb_cache_dir>
    python3 vc_gallery_thumb.py --warm --db <db_path>      # batch pre-warm

Design constraints:
- ffmpeg only — no Pillow, no extra deps. ffmpeg is already on the studio's
  workstations (used by video_health_check.py).
- Skip-if-fresh: if the thumb exists and its mtime is >= the source's mtime,
  do nothing.
- Stable cache key: sha1(absolute_path). Two episodes with identically named
  files don't collide because the path differs.
- Returns the basename (e.g. `abc123.jpg`), not the full path. The server
  composes the full URL.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_lib as lib  # noqa: E402


THUMB_WIDTH = 480
THUMB_QUALITY = 4   # ffmpeg -q:v scale: 2-5 is good, lower = higher quality
FFMPEG_BIN = "ffmpeg"


def _is_fresh(thumb_path: Path, source_path: Path) -> bool:
    """True if the thumbnail exists and is newer than its source."""
    if not thumb_path.exists():
        return False
    try:
        return thumb_path.stat().st_mtime >= source_path.stat().st_mtime
    except OSError:
        return False


def _generate(source: Path, target: Path, *, is_video: bool) -> bool:
    """Run ffmpeg to produce a thumbnail. Returns True on success."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")

    # Common: scale to width 480, preserve aspect, output a single JPEG.
    # `-frames:v 1` + `-update 1` is required in ffmpeg 8.x for single-image
    # output. `-f image2` is required because our tmp filename ends in
    # `.jpg.part` and ffmpeg cannot infer the format from that suffix.
    vf = f"scale={THUMB_WIDTH}:-2:flags=lanczos"

    if is_video:
        # Seek to 1s to skip black intros; falls back to frame 0 on failure.
        cmd = [
            FFMPEG_BIN, "-y", "-loglevel", "error",
            "-ss", "1.0",
            "-i", str(source),
            "-frames:v", "1", "-update", "1",
            "-vf", vf,
            "-q:v", str(THUMB_QUALITY),
            "-f", "image2",
            str(tmp),
        ]
    else:
        cmd = [
            FFMPEG_BIN, "-y", "-loglevel", "error",
            "-i", str(source),
            "-frames:v", "1", "-update", "1",
            "-vf", vf,
            "-q:v", str(THUMB_QUALITY),
            "-f", "image2",
            str(tmp),
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False

    if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        # Video fallback: retry without the -ss seek (for clips shorter than 1s).
        if is_video:
            cmd = [
                FFMPEG_BIN, "-y", "-loglevel", "error",
                "-i", str(source),
                "-frames:v", "1", "-update", "1",
                "-vf", vf,
                "-q:v", str(THUMB_QUALITY),
                str(tmp),
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                return False
        if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return False

    tmp.replace(target)
    return True


def ensure_thumb(source_path: str | Path, cache_dir: str | Path) -> str | None:
    """Make sure a thumbnail exists. Returns the thumb basename, or None on failure."""
    src = Path(source_path)
    if not src.exists():
        return None

    ext = src.suffix.lower()
    if ext not in lib.MEDIA_EXTS:
        return None

    cache = Path(cache_dir)
    name = f"{lib.thumb_key(str(src.resolve()))}.jpg"
    target = cache / name

    if _is_fresh(target, src):
        return name

    is_video = ext in lib.VIDEO_EXTS
    if _generate(src, target, is_video=is_video):
        return name
    return None


def warm_from_db(db_path: Path, cache_dir: Path, *, limit: int | None = None) -> dict:
    """Pre-generate thumbs for every asset in the DB. Used for batch warm-up."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    q = "SELECT id, file_path FROM assets ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"

    counts = {"ok": 0, "skip_fresh": 0, "fail": 0, "missing_source": 0}
    for row in conn.execute(q):
        src = Path(row["file_path"])
        if not src.exists():
            counts["missing_source"] += 1
            continue
        name = f"{lib.thumb_key(str(src.resolve()))}.jpg"
        target = cache_dir / name
        if _is_fresh(target, src):
            counts["skip_fresh"] += 1
            continue
        ext = src.suffix.lower()
        is_video = ext in lib.VIDEO_EXTS
        if _generate(src, target, is_video=is_video):
            counts["ok"] += 1
            # Also stamp thumb_path + thumb_generated_at in the DB
            conn.execute(
                "UPDATE assets SET thumb_path = ?, thumb_generated_at = strftime('%s','now') WHERE id = ?",
                (name, row["id"]),
            )
            conn.commit()
        else:
            counts["fail"] += 1

    conn.close()
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")

    one = sub.add_parser("one", help="Generate one thumbnail")
    one.add_argument("source")
    one.add_argument("cache_dir")

    warm = sub.add_parser("warm", help="Pre-generate thumbs for all DB rows")
    warm.add_argument("--db", required=True)
    warm.add_argument("--cache-dir", required=True)
    warm.add_argument("--limit", type=int, default=None)

    args = ap.parse_args(argv)

    if args.cmd == "one":
        name = ensure_thumb(args.source, args.cache_dir)
        if name is None:
            print("FAIL", file=sys.stderr)
            return 1
        print(name)
        return 0

    if args.cmd == "warm":
        counts = warm_from_db(Path(args.db), Path(args.cache_dir), limit=args.limit)
        print(" ".join(f"{k}={v}" for k, v in counts.items()))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
