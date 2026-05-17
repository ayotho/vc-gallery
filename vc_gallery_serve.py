#!/usr/bin/env python3
"""vc_gallery_serve — local HTTP server for the Visual Chef Gallery dashboard.

Binds 127.0.0.1:8770 by default. Never exposed beyond localhost.

Endpoints:
    GET  /                        → dashboard HTML shell (loads ./EP8 Gallery.html or built-in)
    GET  /healthz                 → {ok, version, current_folder, asset_count}
    GET  /api/folder              → {current, recent}
    POST /api/folder              → set current working folder, auto-scan, return new state
    POST /api/rescan              → rescan current folder
    GET  /api/assets              → {items, total} (filters: status, source_type, model,
                                     workflow, shot_id, scene, media_type, has_prompt, q,
                                     limit, offset)
    GET  /api/assets/<id>         → single asset + prompt + review history
    PATCH /api/assets/<id>        → update status, notes, shot_id, scene, tags
    POST /api/assets/<id>/reviews → log a review event
    POST /api/assets/<id>/open    → reveal asset in Finder (macOS)
    GET  /thumb/<sha>.jpg         → serve thumbnail (generate if missing)
    GET  /media/<filename>        → serve original media
    GET  /sidecar/<filename>      → serve raw sidecar .md

Run:
    python3 vc_gallery_serve.py                    # use last folder from config
    python3 vc_gallery_serve.py --folder /path     # explicit folder
    python3 vc_gallery_serve.py --port 8771

Single-process, single-threaded — fine for one director. SQLite handles its
own locking. All writes go through the same connection.
"""
from __future__ import annotations

import argparse
import contextvars
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_lib as lib  # noqa: E402
import vc_gallery_scan as scan_mod  # noqa: E402
import vc_gallery_thumb as thumb_mod  # noqa: E402
import vc_gallery_obs as obs_mod  # noqa: E402
from jsonl_append import append_jsonl  # noqa: E402

# Wrapper path used by draft.fire endpoint
WRAPPER_SCRIPT = _HERE / "hf_gen_with_sidecar.py"


SERVER_VERSION = "0.1.0"
DEFAULT_PORT = 8770
DEFAULT_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# State — wraps the current working folder, DB connection, paths
# ---------------------------------------------------------------------------

class State:
    """Per-server state. Folder can be swapped at runtime via POST /api/folder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.folder: Optional[Path] = None
        self.db_path: Optional[Path] = None
        self.log_path: Optional[Path] = None
        self.thumb_dir: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        self.last_change_at: float = time.time()
        self._known_files: set = set()
        # Live fire registry — keyed by pid → metadata about a wrapper subprocess
        # the server kicked off via _fire_draft. The HTTP /api/fires endpoint
        # checks proc.poll() against each entry to decide running/completed/failed.
        self._fires: dict = {}  # pid → {asset_id, filename, started_at, log_path, payload_file, proc, finished_at?, exit_code?}
        self._fires_lock = threading.Lock()
        # Selection slot — what the director is looking at right now. Read by
        # peer Claude sessions via GET /api/selection to skip path-copying.
        # Multi-select first-class: list shape from day one.
        self._selection: dict = {
            "asset_ids": [],
            "set_at": 0.0,
            "set_by": "director",
            "folder": None,  # invalidate on folder switch
        }
        self._selection_lock = threading.Lock()

    def register_fire(self, pid: int, info: dict) -> None:
        with self._fires_lock:
            self._fires[pid] = info
        # Fixes #16/H4: persist fire to disk so server restart can reap orphans
        self._persist_fire(pid, info)

    def _persist_fire(self, pid: int, info: dict) -> None:
        """Append a fire record to fires.jsonl for crash recovery."""
        if self.folder is None:
            return
        fires_path = self.folder / ".vc_meta" / "fires.jsonl"
        fires_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "pid": pid,
            "asset_id": info.get("asset_id"),
            "filename": info.get("filename"),
            "started_at": info.get("started_at"),
            "log_path": info.get("log_path"),
            "payload_file": info.get("payload_file"),
        }
        try:
            from jsonl_append import append_jsonl
            append_jsonl(str(fires_path), rec)
        except OSError:
            pass

    def reap_zombie_firings(self, stuck_seconds: int = 1800) -> int:
        """Concern #1 from PR #36 self-review — defensive sweep for rows
        stuck at status='firing' with no live wrapper anywhere.

        Catches the failure modes _replay_fires_on_boot + list_fires miss:
          - kill -9 on the wrapper (no exit code propagated to the registry)
          - Server crash between register_fire and the wrapper writing its DB row
          - Wrapper exits while server is paused/forked, missed by proc.poll
          - Long-finished entry trimmed from the in-memory registry while the
            DB row is still at 'firing' (no transition_fire_status callback)

        Default threshold: 30 minutes. Any wrapper exceeding this is dead —
        Kling/Seedance/NBP all top out around 5 min. Director can refire from
        the demoted 'draft' state.

        Returns the count reaped. Safe to call repeatedly (idempotent).
        """
        if self._conn is None:
            return 0
        try:
            rows = self._conn.execute(
                "SELECT id, last_updated_at, filename FROM assets WHERE status = 'firing'"
            ).fetchall()
        except sqlite3.DatabaseError:
            return 0
        if not rows:
            return 0
        now = time.time()
        # Snapshot live asset_ids from the registry — anything in here is
        # considered "wrapper is still working on it" regardless of stuck time.
        with self._fires_lock:
            live_asset_ids: set[int] = set()
            for info in self._fires.values():
                proc = info.get("proc")
                aid = info.get("asset_id")
                if aid is None:
                    continue
                # Process either still running OR proc handle missing (external
                # fire registered without a Popen handle — see #28). Treat as
                # live if poll() returns None OR there is no proc handle yet.
                if proc is None or proc.poll() is None:
                    live_asset_ids.add(int(aid))
        reaped = 0
        for r in rows:
            asset_id = r["id"]
            if asset_id in live_asset_ids:
                continue  # wrapper still running, leave alone
            stuck_for = now - (r["last_updated_at"] or 0)
            if stuck_for < stuck_seconds:
                continue  # young — wait a bit, wrapper may still come back
            try:
                cur = self._conn.execute(
                    "UPDATE assets SET status = 'draft', "
                    "last_updated_at = strftime('%s','now') "
                    "WHERE id = ? AND status = 'firing'",
                    (asset_id,),
                )
                self._conn.commit()
                if cur.rowcount:
                    reaped += 1
                    _audit("fire.zombie_reaped", {
                        "asset_id": asset_id,
                        "filename": r["filename"],
                        "stuck_seconds": int(stuck_for),
                    })
            except sqlite3.DatabaseError as e:
                print(f"[zombie-reap] failed for asset {asset_id}: {e}", file=sys.stderr)
        if reaped:
            print(f"[zombie-reap] demoted {reaped} stuck firing row(s) → draft", file=sys.stderr)
            self.mark_changed()
        return reaped

    def _replay_fires_on_boot(self) -> None:
        """On folder set, replay fires.jsonl to find orphaned processes.
        Dead PIDs get their tmp payload files cleaned up and their asset
        status transitioned if still stuck on 'firing'."""
        if self.folder is None:
            return
        fires_path = self.folder / ".vc_meta" / "fires.jsonl"
        if not fires_path.exists():
            return
        reaped = 0
        surviving = []
        try:
            with open(fires_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pid = rec.get("pid")
                    if pid is None:
                        continue
                    # Check if process is still alive
                    alive = False
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except (OSError, ProcessLookupError):
                        pass
                    if alive:
                        surviving.append(rec)
                    else:
                        # Dead PID: clean up tmp payload file
                        pf = rec.get("payload_file")
                        if pf and os.path.exists(pf):
                            try:
                                os.unlink(pf)
                            except OSError:
                                pass
                        # Transition asset out of 'firing' if stuck
                        asset_id = rec.get("asset_id")
                        if asset_id is not None:
                            _transition_fire_status(asset_id, -1)
                        reaped += 1
        except OSError:
            return
        # Rewrite fires.jsonl with only surviving entries
        try:
            with open(fires_path, "w", encoding="utf-8") as f:
                for rec in surviving:
                    f.write(json.dumps(rec) + "\n")
        except OSError:
            pass
        if reaped:
            print(f"[fire-registry] reaped {reaped} orphaned fire(s) on boot", file=sys.stderr)

    def list_fires(self, include_finished: bool = True, finished_limit: int = 30) -> list:
        """Return current fires. Polls each subprocess to update status. Drops
        long-finished entries when over finished_limit."""
        now = time.time()
        out = []
        with self._fires_lock:
            # Update + collect
            for pid, info in list(self._fires.items()):
                proc = info.get("proc")
                if info.get("exit_code") is None:
                    if proc is not None:
                        # Server-spawned fire: poll the Popen handle
                        rc = proc.poll()
                        if rc is not None:
                            info["exit_code"] = rc
                            info["finished_at"] = info.get("finished_at") or now
                            _transition_fire_status(info.get("asset_id"), rc)
                    elif info.get("external"):
                        # External fire (#28): no proc handle, check if PID is alive
                        try:
                            os.kill(pid, 0)
                        except (OSError, ProcessLookupError):
                            # Process is dead but never called /complete — treat as failure
                            info["exit_code"] = -1
                            info["finished_at"] = info.get("finished_at") or now
                            _transition_fire_status(info.get("asset_id"), -1)
                # Build a safe dict (drop the proc handle)
                row = {k: v for k, v in info.items() if k != "proc"}
                row["pid"] = pid
                row["state"] = (
                    "running" if info.get("exit_code") is None
                    else ("completed" if info.get("exit_code") == 0 else "failed")
                )
                row["duration_s"] = round((info.get("finished_at") or now) - info["started_at"], 1)
                out.append(row)
            # Garbage-collect very old completed fires
            finished = [(pid, info) for pid, info in self._fires.items() if info.get("exit_code") is not None]
            if len(finished) > finished_limit:
                finished.sort(key=lambda x: x[1].get("finished_at") or 0)
                for pid, _ in finished[: len(finished) - finished_limit]:
                    del self._fires[pid]
        # Newest first
        out.sort(key=lambda r: r["started_at"], reverse=True)
        if not include_finished:
            out = [r for r in out if r["state"] == "running"]
        return out

    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("no working folder set")
        return self._conn

    # ---- Selection slot (multi-select) -------------------------------
    # Ephemeral pointer to which assets the director is looking at right
    # now. Read by peer Claude sessions via GET /api/selection. Lost on
    # server restart (intentional — selection is transient).

    def set_selection(self, asset_ids, source: str = "director") -> dict:
        """Replace the selection slot. Caps at 50 IDs to keep payloads sane."""
        # Defensive int-coerce + dedupe preserving order + cap
        cleaned: list = []
        seen: set = set()
        for raw in (asset_ids or []):
            try:
                a = int(raw)
            except (TypeError, ValueError):
                continue
            if a in seen:
                continue
            seen.add(a)
            cleaned.append(a)
            if len(cleaned) >= 50:
                break
        with self._selection_lock:
            self._selection = {
                "asset_ids": cleaned,
                "set_at": time.time(),
                "set_by": source,
                "folder": str(self.folder) if self.folder else None,
            }
            return dict(self._selection)

    def get_selection(self) -> dict:
        """Return the current selection, invalidating if folder changed."""
        with self._selection_lock:
            sel = dict(self._selection)
        # Cross-folder asset IDs are meaningless. Reset (lazily) on mismatch.
        if sel.get("folder") and sel["folder"] != (str(self.folder) if self.folder else None):
            with self._selection_lock:
                self._selection = {
                    "asset_ids": [],
                    "set_at": 0.0,
                    "set_by": "auto-cleared",
                    "folder": str(self.folder) if self.folder else None,
                }
                return dict(self._selection)
        return sel

    def clear_selection(self) -> dict:
        with self._selection_lock:
            self._selection = {
                "asset_ids": [],
                "set_at": time.time(),
                "set_by": "cleared",
                "folder": str(self.folder) if self.folder else None,
            }
            return dict(self._selection)

    def set_folder(self, folder: Path, *, run_scan: bool = True) -> dict:
        folder = folder.expanduser().resolve()
        if not folder.exists() or not folder.is_dir():
            raise FileNotFoundError(str(folder))
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self.folder = folder
            self.db_path = lib.db_path_for(folder)
            self.log_path = lib.log_path_for(folder)
            self.thumb_dir = lib.thumb_dir_for(folder)
            self.thumb_dir.mkdir(parents=True, exist_ok=True)
            self._conn = lib.connect(self.db_path)
            self._known_files = set()  # reset so watcher reinitializes for new folder
            lib.remember_folder(folder)
        # Selection slot belongs to a specific folder — wipe on swap.
        # Done outside the main _lock to avoid lock-order issues (selection
        # uses its own lock).
        with self._selection_lock:
            self._selection = {
                "asset_ids": [], "set_at": 0.0,
                "set_by": "folder-switched", "folder": str(folder),
            }
        # Fixes #16/H4: replay fire registry to reap orphaned PIDs from prior crash
        self._replay_fires_on_boot()
        # Concern #1 (PR #36 self-review) — sweep any rows stuck at 'firing'
        # with no live wrapper, beyond what _replay_fires can see (kill -9,
        # registry crash, missed transitions). Idempotent, safe to call
        # on every boot + folder switch.
        self.reap_zombie_firings()
        # Issue #27 — boot-time migration: heal any pre-fix 2-row pairs.
        # Older fires created TWO rows (ghost at .drafts/ + real at gallery
        # path). New fires mutate one row, but DBs that existed before the
        # fix can still carry these pairs. Merge them on boot so the
        # gallery shows one row per logical asset.
        self._migrate_draft_pairs_on_boot()
        if run_scan:
            self.rescan()
        self.mark_changed()
        return self.folder_info()

    def _migrate_draft_pairs_on_boot(self) -> None:
        """Merge any pre-fix 2-row pairs: (draft-path ghost) + (real asset).

        For each ghost row (file_path contains '/.drafts/') whose `filename`
        also names a row with a real file_path:
          - Move the real row's media metadata onto the ghost row (preserving
            the ghost's id so the draft.payload history stays linked).
          - Move the prompts + jobs rows from real → ghost.
          - Delete the real row.

        Only fires when BOTH rows exist for one filename, so it's idempotent.
        Logs every merge via the audit log.
        """
        if self._conn is None:
            return
        sep = os.sep
        ghost_marker = f"{sep}.drafts{sep}"
        try:
            ghosts = self._conn.execute(
                "SELECT id, filename, file_path FROM assets WHERE file_path LIKE ?",
                (f"%{ghost_marker}%",),
            ).fetchall()
        except sqlite3.DatabaseError as e:
            print(f"[boot-migrate] query failed: {e}", file=sys.stderr)
            return
        merged = 0
        for g in ghosts:
            real = self._conn.execute(
                "SELECT * FROM assets WHERE filename = ? AND id != ? "
                "AND file_path NOT LIKE ? LIMIT 2",
                (g["filename"], g["id"], f"%{ghost_marker}%"),
            ).fetchall()
            if len(real) != 1:
                # 0 matches → just a draft, no pair to merge. 2+ → ambiguous,
                # skip; the director can resolve manually.
                continue
            r = real[0]
            try:
                # Per-pair transaction (concern #2 from PR #36 self-review) —
                # the merge is 6 statements with the assets.file_path UNIQUE
                # constraint sandwiched between them. If we crash between the
                # DELETE real and the UPDATE ghost, we'd leave the DB without
                # either row owning the file_path AND with both prompts/jobs
                # reparented to the ghost — partially-applied state. Wrapping
                # the whole pair in BEGIN/COMMIT means the next boot sees
                # either (pre-merge: still 2 rows, retry) or (post-merge: 1
                # row, done). No half-merged middle state.
                self._conn.execute("BEGIN")
                # Order matters — assets.file_path is UNIQUE, so we can't move
                # the path onto the ghost while the real row still owns it.
                # Sequence:
                #   1. Clear ghost's existing prompts/jobs (real wins)
                #   2. Reparent real's prompts/jobs → ghost
                #   3. Delete the real row (frees the file_path)
                #   4. UPDATE the ghost with the real row's media metadata
                self._conn.execute("DELETE FROM prompts WHERE asset_id = ?", (g["id"],))
                self._conn.execute("DELETE FROM jobs WHERE asset_id = ?", (g["id"],))
                self._conn.execute(
                    "UPDATE prompts SET asset_id = ? WHERE asset_id = ?",
                    (g["id"], r["id"]),
                )
                self._conn.execute(
                    "UPDATE jobs SET asset_id = ? WHERE asset_id = ?",
                    (g["id"], r["id"]),
                )
                self._conn.execute("DELETE FROM assets WHERE id = ?", (r["id"],))
                self._conn.execute(
                    """UPDATE assets SET
                        file_path = ?, media_type = ?, size_bytes = ?,
                        file_modified_at = ?, source_type = ?, has_sidecar = ?,
                        sidecar_path = ?, status = ?, shot_id = COALESCE(?, shot_id),
                        scene = COALESCE(?, scene), model = COALESCE(?, model),
                        workflow = COALESCE(?, workflow),
                        client = COALESCE(?, client), project = COALESCE(?, project),
                        width = COALESCE(?, width), height = COALESCE(?, height),
                        duration_sec = COALESCE(?, duration_sec),
                        thumb_path = NULL,
                        last_updated_at = strftime('%s','now')
                       WHERE id = ?""",
                    (
                        r["file_path"], r["media_type"], r["size_bytes"],
                        r["file_modified_at"], r["source_type"], r["has_sidecar"],
                        r["sidecar_path"], r["status"], r["shot_id"], r["scene"],
                        r["model"], r["workflow"], r["client"], r["project"],
                        r["width"], r["height"], r["duration_sec"],
                        g["id"],
                    ),
                )
                self._conn.commit()
                merged += 1
            except sqlite3.DatabaseError as e:
                print(f"[boot-migrate] failed for filename={g['filename']}: {e}", file=sys.stderr)
                self._conn.rollback()
        if merged:
            print(f"[boot-migrate] merged {merged} pre-fix draft→fire pairs", file=sys.stderr)

    def rescan(self) -> dict:
        if self.folder is None or self.db_path is None:
            return {"error": "no folder"}
        # Close shared conn during scan to avoid lock contention
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        counts = scan_mod.scan(self.folder, self.db_path, quiet=True)
        with self._lock:
            self._conn = lib.connect(self.db_path)
        # Backfill shot_ids from filenames for any rows still missing them
        backfilled = self._backfill_shot_ids()
        if backfilled:
            counts["shot_ids_backfilled"] = backfilled
        return counts

    def _backfill_shot_ids(self) -> int:
        """Fill in shot_id from filename regex for rows that have NULL shot_id.

        Patch 2026-05-17 (speed): batch the updates with executemany instead of
        one UPDATE per row. A gallery with 500 null-shot rows goes from 500
        round-trips to 1.
        """
        conn = self.conn()
        rows = conn.execute(
            "SELECT id, filename FROM assets WHERE shot_id IS NULL OR shot_id = ''"
        ).fetchall()
        if not rows:
            return 0
        batch = []
        for r in rows:
            shot = scan_mod.extract_shot_id(r["filename"])
            if shot:
                batch.append((shot, r["id"]))
        if batch:
            conn.executemany("UPDATE assets SET shot_id = ? WHERE id = ?", batch)
            conn.commit()
        return len(batch)

    def asset_count(self) -> int:
        if self._conn is None:
            return 0
        try:
            return self._conn.execute("SELECT count(*) FROM assets").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0

    def folder_info(self) -> dict:
        cfg = lib.load_server_config()
        return {
            "current": str(self.folder) if self.folder else None,
            "recent": cfg.get("recent_folders", []),
            "db_path": str(self.db_path) if self.db_path else None,
            "asset_count": self.asset_count(),
            "last_change_at": self.last_change_at,
        }

    def mark_changed(self) -> None:
        self.last_change_at = time.time()


STATE = State()


# ---------------------------------------------------------------------------
# File watcher — polls the active folder, runs incremental scan when the
# filename set changes. Catches new wrapper output AND manual drag-ins.
# ---------------------------------------------------------------------------

class FolderWatcher:
    POLL_INTERVAL = 3.0  # seconds

    def __init__(self, state: State) -> None:
        self.state = state
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                print(f"[watcher] {e}", file=sys.stderr)
            self._stop.wait(self.POLL_INTERVAL)

    def _tick(self) -> None:
        folder = self.state.folder
        if folder is None or not folder.exists():
            return
        # Cheap iterdir — ~3ms for 2000 files
        current: set[str] = set()
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in lib.MEDIA_EXTS:
                current.add(p.name)

        known = self.state._known_files
        if not known:
            self.state._known_files = current
            return

        added = current - known
        removed = known - current

        # Concern #1 (PR #36 self-review) — sweep zombie firing rows on each
        # tick. The query is index-scanned (idx_assets_status) and returns
        # empty in the common case, so it's effectively free. Catches
        # in-session zombies the boot sweep would have to wait for a restart
        # to clear. NOT gated on file changes — zombies can outlive any
        # filesystem activity.
        try:
            self.state.reap_zombie_firings()
        except Exception as e:  # noqa: BLE001
            print(f"[watcher] zombie reap failed: {e}", file=sys.stderr)

        if not (added or removed):
            return

        # Something changed → incremental scan adds new rows, leaves rest alone
        try:
            scan_mod.scan(folder, self.state.db_path, quiet=True)
        except Exception as e:  # noqa: BLE001
            print(f"[watcher] scan failed: {e}", file=sys.stderr)
            return
        self.state._known_files = current
        self.state.mark_changed()
        sys.stderr.write(f"[watcher] +{len(added)} −{len(removed)}\n")


WATCHER = FolderWatcher(STATE)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

# Patch 2026-05-14: added `parent_filename` (IC-C: silently ignored before),
# `pass_num` (variant pass tracking), and `aspect_ratio` so filters cover
# every column the dashboard exposes in the sidebar.
VALID_FILTERS = {
    "status", "source_type", "model", "workflow", "shot_id", "scene",
    "media_type", "client", "project", "has_sidecar",
    "parent_filename", "pass_num", "aspect_ratio",
}


def _extract_hf_url(notes_text: str | None) -> str | None:
    """Parse hf_url out of notes JSON. Returns None if invalid JSON or absent."""
    if not notes_text:
        return None
    try:
        parsed = json.loads(notes_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    url = parsed.get("pulled_from") or parsed.get("hf_url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    # Bare UUID fallback — reconstruct generic asset URL
    uuid = parsed.get("asset_uuid")
    if isinstance(uuid, str) and len(uuid) >= 32:
        return f"https://higgsfield.ai/asset/all/{uuid}"
    return None


def _row_to_asset(row: sqlite3.Row, thumb_dir: Path) -> dict:
    file_path = row["file_path"]
    thumb_name = f"{lib.thumb_key(file_path)}.jpg"
    asset = {
        "id": row["id"],
        "filename": row["filename"],
        "file_path": file_path,
        "media_type": row["media_type"],
        "size_bytes": row["size_bytes"],
        "width": row["width"],
        "height": row["height"],
        "duration_sec": row["duration_sec"],
        "file_modified_at": row["file_modified_at"],
        "first_seen_at": row["first_seen_at"],
        "source_type": row["source_type"],
        "has_sidecar": bool(row["has_sidecar"]),
        "sidecar_path": row["sidecar_path"],
        "status": row["status"],
        "shot_id": row["shot_id"],
        "scene": row["scene"],
        "model": row["model"],
        "workflow": row["workflow"],
        "pass_num": row["pass_num"],
        "variant": row["variant"],
        "client": row["client"],
        "project": row["project"],
        "parent_filename": row["parent_filename"],
        "session": row["session"],
        "session_date": row["session_date"],
        "score": row["score"],
        "notes": row["notes"] or "",
        "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
        "thumb_url": f"/thumb/{thumb_name}",
        "media_url": f"/media/{row['id']}",
        "sidecar_url": f"/sidecar/{row['id']}" if row["sidecar_path"] else None,
        # Higgsfield round-trip link — surfaces .pulled_from from notes JSON
        # (BACKLOG #11). Falls through to jobs.source_url if absent there.
        "hf_url": _extract_hf_url(row["notes"]),
    }

    # Issue #26: surface draft.payload on list responses too so card render
    # can preview the prompt instead of showing a black thumbnail. Drafts have
    # no media yet — the prompt body IS the preview. Cheap because drafts are
    # a small subset and notes is already in the row.
    if asset["status"] == "draft":
        try:
            note_data = json.loads(row["notes"]) if row["notes"] else {}
        except (json.JSONDecodeError, TypeError):
            note_data = {}
        if isinstance(note_data, dict) and note_data.get("is_draft"):
            asset["draft"] = {
                "payload": note_data.get("payload", {}),
                "image_refs": note_data.get("image_refs", []),
                "estimated_cost": note_data.get("estimated_cost"),
                "staged_at": note_data.get("staged_at"),
                "last_edited_at": note_data.get("last_edited_at"),
            }
            # Hide the raw JSON string from clients — the structured `draft` block
            # has everything, and director-facing `notes` should be empty for drafts.
            asset["notes"] = ""

    return asset


def _build_where_no_prompt(params: dict) -> tuple[str, list[Any]]:
    """Like _build_where but skips prompt/q filters and uses 'a.' prefix only.
    Used when we know there's no JOIN on prompts — avoids ambiguous column refs."""
    clauses: list[str] = []
    values: list[Any] = []
    for key in VALID_FILTERS:
        if key in params and params[key]:
            v = params[key][0] if isinstance(params[key], list) else params[key]
            if v == "" or v == "all":
                continue
            clauses.append(f"a.{key} = ?")
            values.append(v)
    if not clauses:
        return "", values
    return " WHERE " + " AND ".join(clauses), values


def _build_where(params: dict) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []

    for key in VALID_FILTERS:
        if key in params and params[key]:
            v = params[key][0] if isinstance(params[key], list) else params[key]
            if v == "" or v == "all":
                continue
            clauses.append(f"a.{key} = ?")
            values.append(v)

    if params.get("has_prompt"):
        v = params["has_prompt"][0] if isinstance(params["has_prompt"], list) else params["has_prompt"]
        if v in ("1", "true", "yes"):
            clauses.append("p.prompt_text IS NOT NULL AND length(p.prompt_text) > 0")
        elif v in ("0", "false", "no"):
            clauses.append("(p.prompt_text IS NULL OR length(p.prompt_text) = 0)")

    if params.get("q"):
        q = params["q"][0] if isinstance(params["q"], list) else params["q"]
        like = f"%{q}%"
        clauses.append(
            "(a.filename LIKE ? OR a.shot_id LIKE ? OR a.notes LIKE ? "
            "OR a.scene LIKE ? OR p.prompt_text LIKE ?)"
        )
        values.extend([like, like, like, like, like])

    if not clauses:
        return "", values
    return " WHERE " + " AND ".join(clauses), values


def _list_assets(params: dict) -> dict:
    conn = STATE.conn()

    limit = int((params.get("limit") or [200])[0])
    offset = int((params.get("offset") or [0])[0])
    limit = max(1, min(limit, 1000))

    # "Latest only" toggle: when latest_per_shot=1, keep only the most recent
    # asset per shot_id. Assets with NULL/empty shot_id are always included
    # (they can't be grouped). Implemented as a CTE so all downstream WHERE,
    # ORDER, LIMIT still work unchanged.
    latest_only = (params.get("latest_per_shot") or [""])[0] in ("1", "true")

    # Only JOIN prompts if a prompt-related filter is active (q or has_prompt).
    # Skipping the JOIN on the common path (browse/filter by status/scene/model)
    # avoids a full scan of the prompts table.
    need_prompt_join = bool(params.get("q") or params.get("has_prompt"))
    if need_prompt_join:
        where, values = _build_where(params)
        base_sql = "FROM assets a LEFT JOIN prompts p ON p.asset_id = a.id" + where
    else:
        where, values = _build_where_no_prompt(params)
        base_sql = "FROM assets a" + where

    # Wrap with latest-per-shot CTE when requested.
    # The CTE must appear BEFORE the SELECT keyword, so it's kept as a
    # separate prefix rather than concatenated into base_sql (which is
    # interpolated after "SELECT ... ").
    cte_prefix = ""
    if latest_only:
        # Pick the newest asset per shot_id (by first_seen_at), plus all
        # assets without a shot_id (they can't be deduplicated).
        cte_prefix = (
            "WITH latest AS ("
            "  SELECT id FROM assets"
            "  WHERE (shot_id IS NULL OR shot_id = '')"
            "  UNION ALL"
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER (PARTITION BY shot_id ORDER BY first_seen_at DESC) rn"
            "    FROM assets WHERE shot_id IS NOT NULL AND shot_id != ''"
            "  ) WHERE rn = 1"
            ") "
        )
        # Inject the CTE filter into the WHERE clause
        latest_filter = " AND a.id IN (SELECT id FROM latest)"
        if where:
            base_sql = base_sql + latest_filter
        else:
            if need_prompt_join:
                base_sql = "FROM assets a LEFT JOIN prompts p ON p.asset_id = a.id WHERE a.id IN (SELECT id FROM latest)"
            else:
                base_sql = "FROM assets a WHERE a.id IN (SELECT id FROM latest)"

    total = conn.execute(f"{cte_prefix}SELECT count(*) {base_sql}", values).fetchone()[0]

    sort = (params.get("sort") or ["recent"])[0]
    # Patch 2026-05-14:
    #   - sort=shot puts NULL/empty shot_ids LAST, not interleaved with the
    #     valid ones (was: alphabetical fallback meant raw_manual drops with
    #     null shot_id polluted the top — VC-C found 269/269 videos this way).
    #   - sort=status tiebreaks by recent so within a status block the newest
    #     surfaces first (VC-C: was just alphabetical).
    # Patch 2026-05-15:
    #   - "recent" now sorts by first_seen_at (when scanner first indexed the
    #     asset) instead of file_modified_at (filesystem mtime). mtime lies
    #     on copies — cp/rsync/Drive-sync preserve the source mtime, so a
    #     brand-new draft can have an mtime from a week ago and never land
    #     at top. first_seen_at is set server-side at insert and never lies.
    order = {
        "recent": "a.first_seen_at DESC",
        "oldest": "a.first_seen_at ASC",
        "name": "a.filename ASC",
        "name-desc": "a.filename DESC",
        "status": "a.status, a.first_seen_at DESC, a.filename",
        "shot": "a.shot_id IS NULL, a.shot_id = '', a.shot_id, a.filename",
        "model": "a.model IS NULL, a.model, a.first_seen_at DESC",
        "id": "a.id ASC",
        "id-desc": "a.id DESC",
    }.get(sort, "a.first_seen_at DESC")

    rows = conn.execute(
        f"{cte_prefix}SELECT a.* {base_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        values + [limit, offset],
    ).fetchall()

    items = [_row_to_asset(r, STATE.thumb_dir) for r in rows]

    # group_by=shot: post-process into shot groups for the "Shots" view.
    # Each group has a shot_id, a "cover" asset (hero > accepted > latest),
    # the count of versions, and the full list of member assets.
    group_by = (params.get("group_by") or [""])[0]
    if group_by == "shot":
        groups: OrderedDict[str, list] = OrderedDict()
        ungrouped: list = []
        for item in items:
            sid = item.get("shot_id") or ""
            if sid:
                groups.setdefault(sid, []).append(item)
            else:
                ungrouped.append(item)

        STATUS_RANK = {"hero": 0, "accepted": 1, "alternate": 2, "review": 3}
        shot_groups = []
        for sid, members in groups.items():
            # Pick cover: hero > accepted > latest by first_seen_at
            cover = min(members, key=lambda m: (
                STATUS_RANK.get(m["status"], 9),
                -(m.get("first_seen_at") or 0),
            ))
            shot_groups.append({
                "shot_id": sid,
                "scene": cover.get("scene") or "",
                "cover": cover,
                "count": len(members),
                "members": members,
            })
        return {
            "items": items, "total": total, "limit": limit, "offset": offset,
            "shot_groups": shot_groups,
            "ungrouped": ungrouped,
        }

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _compare_assets(params: dict) -> dict:
    """Return full asset detail for 2-6 assets for side-by-side comparison.
    Query: GET /api/compare?ids=1,2,3"""
    conn = STATE.conn()
    ids_raw = (params.get("ids") or [""])[0]
    if not ids_raw:
        return {"error": "ids parameter required", "items": []}
    try:
        ids = [int(x.strip()) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        return {"error": "ids must be comma-separated integers", "items": []}
    if len(ids) < 2 or len(ids) > 6:
        return {"error": "Compare requires 2-6 assets", "items": []}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT a.* FROM assets a WHERE a.id IN ({placeholders})", ids
    ).fetchall()
    items = [_row_to_asset(r, STATE.thumb_dir) for r in rows]
    for item in items:
        pr = conn.execute(
            "SELECT prompt_text FROM prompts WHERE asset_id = ?", (item["id"],)
        ).fetchone()
        item["prompt_text"] = pr["prompt_text"] if pr else None
    id_order = {aid: i for i, aid in enumerate(ids)}
    items.sort(key=lambda x: id_order.get(x["id"], 999))
    return {"items": items, "count": len(items)}


def _facet_counts(params: dict | None = None) -> dict:
    """Return counts by status / source_type / media_type for filter chips.

    Patch 2026-05-14: added `scene` and `shot_id` facets so the sidebar can
    offer per-scene/per-shot filtering (VC-C: video reviewers can't group by
    scene without this). `shot_id` cap at 200 distinct values to keep response
    small on big galleries.

    Patch 2026-05-15 (cross-filter facets): facet counts now respect the
    currently active filter set. Each facet's count uses ALL active filters
    EXCEPT its own axis — standard faceted-search behaviour.

    Patch 2026-05-17 (speed): When NO filters are active, skip the per-axis
    exclusion dance and run a single aggregate query. This covers the common
    page-load case (no filters yet) and cuts 7 queries down to 1.
    When filters ARE active, skip the LEFT JOIN on prompts unless a prompt
    filter is actually in use — the join is the expensive part.
    """
    conn = STATE.conn()
    params = params or {}
    out: dict[str, dict[str, int]] = {}

    # Fast path: no active filters → one pass over assets table, no join needed
    active_filters = {k: v for k, v in params.items()
                      if k in VALID_FILTERS and v and v != "all"
                      and not (isinstance(v, list) and (not v or v[0] in ("", "all")))}
    has_prompt_filter = bool(params.get("has_prompt") or params.get("q"))

    facet_cols = ("status", "source_type", "media_type", "model", "workflow", "scene")

    if not active_filters and not has_prompt_filter:
        # Single query: group by each facet in one pass (no WHERE, no JOIN)
        for col in facet_cols:
            sql = (
                f"SELECT {col}, count(*) c FROM assets "
                f"WHERE {col} IS NOT NULL AND {col} != '' "
                f"GROUP BY {col} ORDER BY c DESC"
            )
            out[col] = {r[col]: r["c"] for r in conn.execute(sql)}
        # shot_id
        sql = (
            "SELECT shot_id, count(*) c FROM assets "
            "WHERE shot_id IS NOT NULL AND shot_id != '' "
            "GROUP BY shot_id ORDER BY c DESC LIMIT 200"
        )
        out["shot_id"] = {r["shot_id"]: r["c"] for r in conn.execute(sql)}
        return out

    # Filtered path: per-axis exclusion (cross-filter), but only JOIN prompts
    # if has_prompt or q filter is active.
    need_join = has_prompt_filter
    join_clause = "FROM assets a LEFT JOIN prompts p ON p.asset_id = a.id " if need_join else "FROM assets a "

    for col in facet_cols:
        scoped_params = {k: v for k, v in params.items() if k != col}
        where, values = _build_where(scoped_params) if need_join else _build_where_no_prompt(scoped_params)
        sql = (
            f"SELECT a.{col}, count(*) c "
            f"{join_clause}"
            f"{where}{' AND' if where else ' WHERE'} a.{col} IS NOT NULL AND a.{col} != '' "
            f"GROUP BY a.{col} ORDER BY c DESC"
        )
        out[col] = {r[col]: r["c"] for r in conn.execute(sql, values)}

    # shot_id facet
    scoped_params = {k: v for k, v in params.items() if k != "shot_id"}
    where, values = _build_where(scoped_params) if need_join else _build_where_no_prompt(scoped_params)
    sql = (
        "SELECT a.shot_id, count(*) c "
        f"{join_clause}"
        f"{where}{' AND' if where else ' WHERE'} a.shot_id IS NOT NULL AND a.shot_id != '' "
        "GROUP BY a.shot_id ORDER BY c DESC LIMIT 200"
    )
    out["shot_id"] = {r["shot_id"]: r["c"] for r in conn.execute(sql, values)}
    return out


def _resolve_ref_to_url(ref: str, gallery: Path | None) -> dict:
    """Turn a ref string into something the UI can render.

    refs come in three shapes from the wild:
      - Absolute or relative filesystem path → serve via /ref?path=…
      - Bare filename → assume it's in the gallery, serve via /ref?path=…
      - https:// URL → pass through as-is
    """
    if not ref:
        return {"raw": "", "kind": "empty"}
    if ref.startswith(("http://", "https://")):
        return {"raw": ref, "kind": "url", "url": ref}
    p = Path(ref)
    # Bare filename → resolve into gallery
    if not p.is_absolute() and gallery is not None and "/" not in ref:
        cand = gallery / ref
        if cand.exists():
            p = cand
    # Build a /ref?path= URL so the browser can request it through the server
    from urllib.parse import quote
    return {
        "raw": ref,
        "kind": "file",
        "filename": p.name,
        "url": f"/ref?path={quote(str(p))}",
        "exists": p.exists() if p.is_absolute() else None,
    }


def _get_asset(asset_id: int) -> Optional[dict]:
    conn = STATE.conn()
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return None
    asset = _row_to_asset(row, STATE.thumb_dir)

    prompt = conn.execute(
        "SELECT prompt_text, refs_json FROM prompts WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    if prompt:
        asset["prompt"] = prompt["prompt_text"] or ""
        raw_refs = json.loads(prompt["refs_json"]) if prompt["refs_json"] else []
        asset["refs"] = raw_refs  # backward compat: stays as list of strings
        asset["refs_resolved"] = [_resolve_ref_to_url(r, STATE.folder) for r in raw_refs]
    else:
        asset["prompt"] = ""
        asset["refs"] = []
        asset["refs_resolved"] = []

    job = conn.execute(
        "SELECT provider, provider_job_id, source_url FROM jobs WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    if job and job["source_url"]:
        asset["job"] = {
            "provider": job["provider"],
            "provider_job_id": job["provider_job_id"],
            "source_url": job["source_url"],
        }
        # Promote job.source_url to hf_url if notes didn't supply one (#11 dual source)
        if not asset.get("hf_url"):
            asset["hf_url"] = job["source_url"]

    # Draft-specific drawer enrichment — the base `draft` block (payload, image_refs,
    # estimated_cost, staged_at, last_edited_at) is now populated by _row_to_asset so
    # list responses can render card previews (Issue #26). Here we add the heavier
    # `image_refs_resolved` block which needs _resolve_ref_to_url + STATE.folder and
    # only matters for the drawer side panel.
    if asset["status"] == "draft" and "draft" in asset:
        try:
            note_data = json.loads(row["notes"]) if row["notes"] else {}
        except (json.JSONDecodeError, TypeError):
            note_data = {}
        asset["draft"]["image_refs_resolved"] = [
            _resolve_ref_to_url(r, STATE.folder)
            for r in (note_data.get("image_refs") or [])
        ]

    # Patch 2026-05-14 (drawer screenshot bug): for non-draft assets, the
    # `notes` column sometimes carries the wrapper's pulled_from metadata
    # JSON ({"pulled_from": ..., "asset_uuid": ..., "shot": ..., ...}) — the
    # HF link is already extracted into asset.hf_url, so leaking the raw JSON
    # into the director's NOTES textarea is just noise. Detect that shape and
    # blank the notes field so the director sees an empty editable space.
    elif row["notes"]:
        try:
            note_data = json.loads(row["notes"])
            if isinstance(note_data, dict) and (
                "pulled_from" in note_data
                or "asset_uuid" in note_data
                or "is_draft" in note_data
            ):
                asset["notes"] = ""
                asset["_system_notes_hidden"] = True  # client can inspect via /api/assets/<id>?show_system=1 later
        except (json.JSONDecodeError, TypeError):
            pass  # plain-text notes — leave as-is

    history = conn.execute(
        "SELECT id, from_status, to_status, note, reviewer, reviewed_at "
        "FROM reviews WHERE asset_id = ? ORDER BY reviewed_at DESC LIMIT 50",
        (asset_id,),
    ).fetchall()
    asset["review_history"] = [dict(r) for r in history]
    return asset


def _inherit_properties(target_id: int, source_id: int) -> Optional[dict]:
    """Copy shot_id and scene from source asset to target asset.

    Lightweight drag-to-inherit: director drags card A onto card B, A picks up
    B's shot_id and scene so it shows up in the correct Shots group and responds
    to Latest Only filtering. No stacks table, no collapsing — just property
    inheritance via drag.
    """
    conn = STATE.conn()
    source = conn.execute("SELECT shot_id, scene FROM assets WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        return None
    target = conn.execute("SELECT id, shot_id, scene FROM assets WHERE id = ?", (target_id,)).fetchone()
    if target is None:
        return None

    updates = {}
    if source["shot_id"]:
        updates["shot_id"] = source["shot_id"]
    if source["scene"]:
        updates["scene"] = source["scene"]

    if not updates:
        return _get_asset(target_id)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [target_id]
    conn.execute(f"UPDATE assets SET {set_clause}, last_updated_at = strftime('%s','now') WHERE id = ?", vals)

    # Audit trail
    note = f"Inherited from #{source_id}: {', '.join(f'{k}={v}' for k, v in updates.items())}"
    conn.execute(
        "INSERT INTO reviews (asset_id, from_status, to_status, note, reviewer) "
        "VALUES (?, (SELECT status FROM assets WHERE id = ?), (SELECT status FROM assets WHERE id = ?), ?, 'director')",
        (target_id, target_id, target_id, note),
    )
    conn.commit()
    return _get_asset(target_id)


UPDATABLE_FIELDS = {"status", "notes", "shot_id", "scene", "score", "project", "client"}
# Issue #33 — `filename` is handled out-of-band via _rename_asset because it
# carries an on-disk move. `file_path` is derived (gallery dir + filename) and
# is never user-settable directly. Everything else outside this set is rejected
# loud (was: silently dropped, see PATCH whitelist bug).
PATCH_ROUTED_FIELDS = {"filename"}
PATCH_DERIVED_FIELDS = {"file_path"}
PATCH_OOB_FIELDS = {"tags", "note"}  # tags goes to tags_json; note is the review log message


def _patch_asset(asset_id: int, payload: dict) -> Optional[dict]:
    """PATCH semantics:

    - Fields in UPDATABLE_FIELDS land directly on the assets row.
    - `tags` is serialized into tags_json (legacy carve-out).
    - `filename` is routed through _rename_asset (atomic DB + on-disk move).
    - `file_path` is rejected — derived from gallery + filename, never settable.
    - Any other key lands in `response._ignored_fields` so silent-drops become
      visible to callers (see #33 — director burned 10 minutes API-spelunking
      because the server happily accepted-then-dropped filename/project/client).
    - If a routed rename fails, the metadata updates are NOT applied — the
      caller should fix the rename and retry, not get a half-applied row.
    """
    conn = STATE.conn()
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return None

    # Surface unknown fields rather than silently dropping (issue #33).
    known = UPDATABLE_FIELDS | PATCH_ROUTED_FIELDS | PATCH_DERIVED_FIELDS | PATCH_OOB_FIELDS
    ignored = [k for k in payload.keys() if k not in known]
    rejected_derived = [k for k in payload.keys() if k in PATCH_DERIVED_FIELDS]

    # Routed rename — apply BEFORE metadata so any rename error short-circuits
    # the rest of the patch (avoids half-applied rows on rename failures).
    rename_warning: Optional[str] = None
    if "filename" in payload:
        new_name = payload["filename"]
        if new_name and new_name != row["filename"]:
            rename_result = _rename_asset({
                "asset_id": asset_id,
                "new_filename": new_name,
                "move_file": True,
            })
            if not rename_result.get("ok"):
                # Don't apply metadata — surface the rename failure to the caller
                # so they can fix it and retry. Preserves the "atomic batch" promise.
                return {
                    "ok": False,
                    "error": f"rename failed: {rename_result.get('error')}",
                    "_ignored_fields": ignored,
                    "_rejected_derived_fields": rejected_derived,
                }
            # Reload the post-rename row so changes-log shows fresh filename.
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    sets: list[str] = []
    values: list[Any] = []
    changes: dict[str, Any] = {}

    for k in UPDATABLE_FIELDS:
        if k not in payload:
            continue
        v = payload[k]
        if k == "status":
            v = lib.normalize_status(v)
            if v not in lib.VALID_STATUSES:
                continue
        sets.append(f"{k} = ?")
        values.append(v)
        changes[k] = v

    if "tags" in payload and isinstance(payload["tags"], list):
        sets.append("tags_json = ?")
        values.append(json.dumps(payload["tags"], ensure_ascii=False))
        changes["tags"] = payload["tags"]

    if sets:
        sets.append("last_updated_at = strftime('%s','now')")
        conn.execute(
            f"UPDATE assets SET {', '.join(sets)} WHERE id = ?",
            values + [asset_id],
        )

        # If status changed, log a review row
        old_status = row["status"]
        new_status = changes.get("status")
        if new_status and new_status != old_status:
            conn.execute(
                "INSERT INTO reviews (asset_id, from_status, to_status, note) VALUES (?, ?, ?, ?)",
                (asset_id, old_status, new_status, payload.get("note", "")),
            )

        _audit("asset.patched", {
            "asset_id": asset_id,
            "filename": row["filename"],
            "changes": changes,
        })

    asset = _get_asset(asset_id)
    if asset is not None:
        # Always attach the diagnostic fields so callers can detect drops.
        # Empty lists are intentional — explicit empty signal beats absence.
        asset["_ignored_fields"] = ignored
        asset["_rejected_derived_fields"] = rejected_derived
    return asset


def _add_review(asset_id: int, payload: dict) -> Optional[dict]:
    conn = STATE.conn()
    row = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return None
    to_status = lib.normalize_status(payload.get("status") or row["status"])
    if to_status not in lib.VALID_STATUSES:
        to_status = row["status"]
    conn.execute(
        "INSERT INTO reviews (asset_id, from_status, to_status, note, reviewer) "
        "VALUES (?, ?, ?, ?, ?)",
        (asset_id, row["status"], to_status, payload.get("note", ""), payload.get("reviewer", "director")),
    )
    if to_status != row["status"]:
        conn.execute(
            "UPDATE assets SET status = ?, last_updated_at = strftime('%s','now') WHERE id = ?",
            (to_status, asset_id),
        )
    _audit("review.added", {"asset_id": asset_id, "to_status": to_status})
    return _get_asset(asset_id)


def _bulk_status_change(payload: dict) -> dict:
    """Change status on multiple assets in one call.
    Body: {asset_ids: [1,2,3], status: "hero", note: "...", reviewer: "director"}

    Patch 2026-05-17 (speed): Fetch all current statuses in one query instead of
    N individual SELECTs. Batch the INSERT/UPDATE using executemany. Cuts a
    100-asset bulk op from ~200 queries to 3.
    """
    ids = payload.get("asset_ids", [])
    if not isinstance(ids, list) or not ids:
        return {"ok": False, "error": "asset_ids must be a non-empty list"}
    to_status = lib.normalize_status(payload.get("status", ""))
    if to_status not in lib.VALID_STATUSES:
        return {"ok": False, "error": f"invalid status: {payload.get('status')}"}
    note = payload.get("note", "")
    reviewer = payload.get("reviewer", "director")
    conn = STATE.conn()

    # Single query to get current statuses for all requested IDs
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, status FROM assets WHERE id IN ({placeholders})", ids
    ).fetchall()
    current_map = {r["id"]: r["status"] for r in rows}

    changed = []
    skipped = []
    review_rows = []
    for aid in ids:
        cur = current_map.get(aid)
        if cur is None or cur == to_status:
            skipped.append(aid)
            continue
        changed.append(aid)
        review_rows.append((aid, cur, to_status, note, reviewer))

    if changed:
        conn.executemany(
            "INSERT INTO reviews (asset_id, from_status, to_status, note, reviewer) VALUES (?, ?, ?, ?, ?)",
            review_rows,
        )
        change_placeholders = ",".join("?" * len(changed))
        conn.execute(
            f"UPDATE assets SET status = ?, last_updated_at = strftime('%s','now') WHERE id IN ({change_placeholders})",
            [to_status] + changed,
        )
        conn.commit()
        _audit("bulk.status_change", {"to_status": to_status, "changed": changed, "skipped": skipped})
        STATE.mark_changed()
    return {"ok": True, "changed": len(changed), "skipped": len(skipped), "changed_ids": changed}


def _open_in_finder(asset_id: int) -> tuple[bool, str]:
    """Return (ok, reason). Reason is empty on success, human-readable on failure.
    Previously returned a bool that was 'true' whenever subprocess.Popen
    succeeded — even if the file_path didn't exist on disk, which made Finder
    do nothing and the user think the button was broken.
    """
    conn = STATE.conn()
    row = conn.execute("SELECT file_path FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return False, "asset not found"
    if sys.platform != "darwin":
        return False, "open in finder only supported on macOS"
    file_path = row["file_path"]
    if not file_path:
        return False, "asset has no file_path"
    p = Path(file_path)
    if not p.exists():
        return False, f"file not on disk: {p.name}"
    try:
        subprocess.Popen(["open", "-R", file_path])
        return True, ""
    except OSError as exc:
        return False, f"open failed: {exc}"


_ETA_BY_PREFIX: dict[str, int] = {
    # Longest prefix first — iteration order matters (sorted at lookup time).
    # Image models
    "kling-image": 30,
    "kling_image": 30,
    # Video models
    "kling": 280,
    "seedance": 110,
    "veo": 220,
    "vee_o": 220,
    # Fast image models
    "nano_banana": 35,
    "nb2": 35,
    "nbp": 35,
    "gpt_image": 60,
    "imagegen": 60,
    "qwen": 45,
}


def _coarse_eta_for_model(model_id: str) -> int:
    """Best-guess wall-clock time in seconds for a single fire of this model.
    Uses longest-prefix-wins matching so 'kling-image' (30s) isn't caught
    by the shorter 'kling' (280s) prefix.
    """
    m = (model_id or "").lower()
    # Sort by prefix length descending — longest match wins.
    for prefix, eta in sorted(_ETA_BY_PREFIX.items(), key=lambda x: -len(x[0])):
        if prefix in m:
            return eta
    return 90


def _audit(event: str, payload: dict, *, severity: str = "info", source: str = "api") -> None:
    """Server-side event recorder.
    Patch 2026-05-14: now routes through obs_mod.record_event so every server
    audit gets `ts` (ISO), `severity`, and `source` fields — previously the
    server bypassed the obs helper and wrote bare `{event, ...payload}` rows,
    which made the JSONL log impossible to filter by severity or actor.

    H1 fix: shadow the reserved keys instead of splatting raw. If a caller's
    payload includes `source` or `severity`, the splat would have crashed with
    `TypeError: got multiple values for argument 'source'`.
    """
    if STATE.log_path is None:
        return
    # Drop reserved keys from payload to avoid the kwarg collision.
    safe = {k: v for k, v in payload.items() if k not in ("source", "severity", "ts", "event")}
    # If the caller stuffed their own severity/source into the payload, honor it.
    eff_severity = payload.get("severity", severity)
    eff_source = payload.get("source", source)
    try:
        obs_mod.record_event(
            STATE.log_path,
            event,
            source=eff_source,
            severity=eff_severity,
            **safe,
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Rename — atomic filename + file_path update
# ---------------------------------------------------------------------------

def _rename_asset(payload: dict) -> dict:
    """Rename an asset on disk AND in the DB, atomically.

    Accepts either:
        {"asset_id": <int>, "new_filename": "..."}
        {"old_filename": "...", "new_filename": "..."}

    Both `filename` AND `file_path` are updated in a single transaction.
    The on-disk file is moved with os.rename. If the destination exists already
    or the old file is missing, the operation fails BEFORE any DB write.
    """
    conn = STATE.conn()
    asset_id = payload.get("asset_id")
    old_filename = payload.get("old_filename")
    new_filename = payload.get("new_filename")
    move_file = bool(payload.get("move_file", True))

    if not new_filename or "/" in new_filename:
        return {"ok": False, "error": "new_filename required, must be a bare filename"}

    if asset_id:
        row = conn.execute(
            "SELECT id, filename, file_path, status FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
    elif old_filename:
        row = conn.execute(
            "SELECT id, filename, file_path, status FROM assets WHERE filename = ? LIMIT 1",
            (old_filename,),
        ).fetchone()
    else:
        return {"ok": False, "error": "provide asset_id or old_filename"}

    if row is None:
        return {"ok": False, "error": "asset not found"}

    old_path = Path(row["file_path"])
    new_path = old_path.parent / new_filename

    # Sanity: a different DB row already owns the destination file_path?
    conflict = conn.execute(
        "SELECT id FROM assets WHERE file_path = ? AND id != ?", (str(new_path), row["id"])
    ).fetchone()
    if conflict:
        return {"ok": False, "error": f"DB row {conflict['id']} already owns {new_filename}"}

    # On-disk move (if requested AND file is present at the old path)
    if move_file:
        if old_path.exists() and not new_path.exists():
            try:
                os.rename(str(old_path), str(new_path))
            except OSError as e:
                return {"ok": False, "error": f"file rename failed: {e}"}
        elif new_path.exists() and old_path.exists():
            return {"ok": False, "error": f"destination already exists on disk: {new_filename}"}
        # If neither exists, allow the DB update to proceed — the caller may be
        # repairing a DB whose file was already moved out-of-band.

    # Atomic DB update — both filename and file_path
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE assets SET filename = ?, file_path = ?, thumb_path = NULL, last_updated_at = strftime('%s','now') WHERE id = ?",
            (new_filename, str(new_path), row["id"]),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        conn.execute("ROLLBACK")
        # Roll back the disk move too if we did one
        if move_file and new_path.exists() and not old_path.exists():
            try:
                os.rename(str(new_path), str(old_path))
            except OSError:
                pass
        return {"ok": False, "error": f"DB update failed: {e}"}

    _audit("asset.renamed", {
        "asset_id": row["id"],
        "old_filename": row["filename"],
        "new_filename": new_filename,
        "old_file_path": str(old_path),
        "new_file_path": str(new_path),
    })
    STATE.mark_changed()
    return {"ok": True, "asset": _get_asset(row["id"])}


# ---------------------------------------------------------------------------
# Drafts — stage video/image fires before spending credits (BACKLOG #12 MVP)
# ---------------------------------------------------------------------------

def _draft_filepath(gallery: Path, filename: str) -> str:
    """Synthetic file_path for a draft row. Lives under .drafts/ so the scanner
    (which walks the top-level only and filters by MEDIA_EXTS) never picks it up.
    """
    return str((gallery / ".drafts" / f"{filename}.draft.json"))


def _create_draft(payload: dict) -> dict:
    """Stage a draft asset row.

    Expected payload:
        {
            "filename": "SH700_kling_v1.mp4",   # required — eventual output filename
            "payload": {                          # required — full wrapper payload
                "model": "kling3_0",
                "prompt": "...",
                "image": ["/path/to/ref1.png"],
                "aspect_ratio": "16:9",
                "duration": 5,
                ...
            },
            "client": "btw_documentary",
            "project": "episode_8",
            "shot_id": "SH700",
            "estimated_cost": 24,
            "model": "kling3_0",
            "workflow": "draft.video"
        }

    Returns the created asset row.
    """
    conn = STATE.conn()
    filename = payload.get("filename")
    inner = payload.get("payload") or {}
    if not filename:
        return {"ok": False, "error": "filename required"}
    if not inner.get("prompt"):
        return {"ok": False, "error": "payload.prompt required"}

    # Collect image refs from the inner payload (multiple shapes)
    image_refs: list[str] = []
    for key in ("image", "start_image", "end_image", "video", "audio", "media", "refs"):
        v = inner.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            image_refs.extend([str(x) for x in v if x])
        elif isinstance(v, str):
            image_refs.append(v)

    # Infer media_type from filename
    suffix = Path(filename).suffix.lower()
    if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
        media_type = "video"
    elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        media_type = "image"
    else:
        media_type = "other"

    # Build the row
    gallery = STATE.folder
    if gallery is None:
        return {"ok": False, "error": "no working folder set"}
    file_path = _draft_filepath(gallery, filename)
    now = time.time()
    notes_blob = json.dumps({
        "is_draft": True,
        "payload": inner,
        "image_refs": image_refs,
        "estimated_cost": payload.get("estimated_cost"),
        "staged_at": now,
        "last_edited_at": now,
    }, ensure_ascii=False)

    try:
        cur = conn.execute(
            """INSERT INTO assets (
                file_path, filename, media_type, size_bytes,
                source_type, has_sidecar, status,
                shot_id, scene, model, workflow, client, project,
                notes, first_seen_at, last_updated_at
            ) VALUES (?, ?, ?, 0,
                'draft', 0, 'draft',
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?)
            """,
            (
                file_path, filename, media_type,
                payload.get("shot_id"), payload.get("scene"),
                payload.get("model") or inner.get("model"),
                payload.get("workflow", "draft.video"),
                payload.get("client"), payload.get("project"),
                notes_blob, now, now,
            ),
        )
        asset_id = cur.lastrowid
        # Also write the prompt body to the prompts table so /api/assets/<id>
        # returns it via the normal prompt path (preserves UI compatibility).
        conn.execute(
            """INSERT INTO prompts (asset_id, prompt_text, refs_json)
               VALUES (?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                   prompt_text = excluded.prompt_text,
                   refs_json = excluded.refs_json""",
            (asset_id, inner.get("prompt", ""), json.dumps(image_refs, ensure_ascii=False)),
        )
        conn.commit()
    except sqlite3.Error as e:
        return {"ok": False, "error": f"DB insert failed: {e}"}

    _audit("draft.staged", {
        "asset_id": asset_id, "filename": filename,
        "model": payload.get("model") or inner.get("model"),
        "ref_count": len(image_refs),
    })
    STATE.mark_changed()
    return {"ok": True, "asset": _get_asset(asset_id)}


def _edit_draft(asset_id: int, payload: dict) -> dict:
    conn = STATE.conn()
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "draft not found"}
    if row["status"] != "draft":
        return {"ok": False, "error": "asset is not a draft"}

    try:
        note_data = json.loads(row["notes"]) if row["notes"] else {}
    except json.JSONDecodeError:
        note_data = {}
    if not isinstance(note_data, dict):
        note_data = {}

    # Allowed edits: replace payload, replace refs, replace prompt, edit cost
    if "payload" in payload:
        note_data["payload"] = payload["payload"]
    if "image_refs" in payload:
        note_data["image_refs"] = payload["image_refs"]
    if "estimated_cost" in payload:
        note_data["estimated_cost"] = payload["estimated_cost"]
    note_data["is_draft"] = True
    note_data["last_edited_at"] = time.time()

    # Update assets.notes + prompts.prompt_text/refs_json
    new_prompt = (note_data.get("payload") or {}).get("prompt", "")
    new_refs = note_data.get("image_refs") or []

    conn.execute(
        "UPDATE assets SET notes = ?, last_updated_at = strftime('%s','now') WHERE id = ?",
        (json.dumps(note_data, ensure_ascii=False), asset_id),
    )
    conn.execute(
        """INSERT INTO prompts (asset_id, prompt_text, refs_json)
           VALUES (?, ?, ?)
           ON CONFLICT(asset_id) DO UPDATE SET
               prompt_text = excluded.prompt_text,
               refs_json = excluded.refs_json""",
        (asset_id, new_prompt, json.dumps(new_refs, ensure_ascii=False)),
    )
    conn.commit()
    _audit("draft.edited", {"asset_id": asset_id})
    return {"ok": True, "asset": _get_asset(asset_id)}


def _delete_draft(asset_id: int) -> dict:
    conn = STATE.conn()
    row = conn.execute("SELECT status, filename FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "draft not found"}
    if row["status"] != "draft":
        return {"ok": False, "error": "refuse: not a draft (use PATCH /api/assets/<id> for normal rows)"}
    conn.execute("DELETE FROM prompts WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    conn.commit()
    _audit("draft.deleted", {"asset_id": asset_id, "filename": row["filename"]})
    return {"ok": True}


def _delete_asset(asset_id: int) -> dict:
    """Hard-delete a single asset row, its prompts/jobs, and its cached thumb."""
    conn = STATE.conn()
    row = conn.execute("SELECT id, filename, file_path, thumb_path, status FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "asset not found"}
    # Clean up thumb cache file
    if row["thumb_path"] and STATE.folder:
        thumb_file = STATE.folder / ".visual_chef" / ".thumb_cache" / row["thumb_path"]
        if thumb_file.exists():
            try:
                thumb_file.unlink()
            except OSError:
                pass
    conn.execute("DELETE FROM prompts WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM jobs WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    conn.commit()
    _audit("asset.deleted", {"asset_id": asset_id, "filename": row["filename"], "status": row["status"]})
    STATE.mark_changed()
    return {"ok": True, "deleted_id": asset_id, "filename": row["filename"]}


def _purge_orphans() -> dict:
    """Delete all asset rows whose file no longer exists on disk."""
    conn = STATE.conn()
    rows = conn.execute("SELECT id, filename, file_path, thumb_path FROM assets").fetchall()
    purged = []
    for r in rows:
        if r["file_path"] and not Path(r["file_path"]).exists():
            # Skip drafts/firing (their file_path points to .drafts/ staging area)
            status = conn.execute("SELECT status FROM assets WHERE id = ?", (r["id"],)).fetchone()
            if status and status["status"] in ("draft", "firing"):
                continue
            if r["thumb_path"] and STATE.folder:
                thumb_file = STATE.folder / ".visual_chef" / ".thumb_cache" / r["thumb_path"]
                if thumb_file.exists():
                    try:
                        thumb_file.unlink()
                    except OSError:
                        pass
            conn.execute("DELETE FROM prompts WHERE asset_id = ?", (r["id"],))
            conn.execute("DELETE FROM jobs WHERE asset_id = ?", (r["id"],))
            conn.execute("DELETE FROM assets WHERE id = ?", (r["id"],))
            purged.append({"id": r["id"], "filename": r["filename"]})
    if purged:
        conn.commit()
        _audit("orphans.purged", {"count": len(purged), "ids": [p["id"] for p in purged]})
        STATE.mark_changed()
    return {"ok": True, "purged": len(purged), "items": purged}


def _transition_fire_status(asset_id: int | None, exit_code: int) -> None:
    """Transition a firing draft based on wrapper exit code.
    Fixes #18 — only called when proc.poll() returns a result.

    Patch 2026-05-15: on failure, return to status='draft' instead of
    'rejected'. Failed fires are usually fixable by the director (schema
    typo, filename collision, stale model_id) — keeping the draft visible
    in the Drafts tab lets them edit the payload and refire. Setting
    'rejected' yanked it out of the Drafts view and surfaced it nowhere
    obvious, so the director thought the draft was gone.
    """
    if asset_id is None:
        return
    try:
        conn = STATE.conn()
        row = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None or row["status"] != "firing":
            return
        new_status = "review" if exit_code == 0 else "draft"
        conn.execute(
            "UPDATE assets SET status = ?, last_updated_at = strftime('%s','now') WHERE id = ?",
            (new_status, asset_id),
        )
        _audit("fire.completed", {
            "asset_id": asset_id,
            "exit_code": exit_code,
            "new_status": new_status,
        })
        STATE.mark_changed()
    except Exception as e:
        print(f"[fire-transition] failed for asset {asset_id}: {e}", file=sys.stderr)


def _fire_draft(asset_id: int) -> dict:
    """Spawn the wrapper for a staged draft.

    The wrapper runs as a detached background process (subprocess.Popen, not
    blocking). The draft row stays as status='draft' until the wrapper writes
    its own row via _write_db_row (then the watcher picks up the new file +
    the rescanner upserts). After the new file lands, the original draft row
    can be deleted by the agent or left as a record.
    """
    conn = STATE.conn()
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "draft not found"}
    if row["status"] != "draft":
        return {"ok": False, "error": "asset is not a draft"}

    try:
        note_data = json.loads(row["notes"]) if row["notes"] else {}
    except json.JSONDecodeError:
        note_data = {}
    payload = (note_data.get("payload") or {}) if isinstance(note_data, dict) else {}
    if not payload.get("prompt"):
        return {"ok": False, "error": "draft has no prompt"}

    # Ensure standard wrapper keys are present
    payload.setdefault("filename", row["filename"])
    payload.setdefault("client", row["client"] or "unknown")
    payload.setdefault("project", row["project"] or "unknown")
    payload.setdefault("workflow", row["workflow"] or "draft.fire")
    payload.setdefault("gallery", str(STATE.folder) if STATE.folder else "")
    payload.setdefault("skip_sidecar", True)
    # Issue #27 — pass the draft's asset_id so the wrapper mutates the existing
    # row instead of inserting a duplicate. Without this, draft→fire→success
    # left a "ghost" row at the .drafts/ path + a "real" row at the gallery
    # path. With it, ONE row flows through draft → firing → review.
    payload["asset_id"] = asset_id

    # Write the payload to a temp file the wrapper can read
    import tempfile
    fd, tmp_path = tempfile.mkstemp(prefix=f"draft_{asset_id}_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fp:
            json.dump(payload, fp, ensure_ascii=False)
    except OSError as e:
        return {"ok": False, "error": f"failed to write payload: {e}"}

    if not WRAPPER_SCRIPT.exists():
        return {"ok": False, "error": f"wrapper not found: {WRAPPER_SCRIPT}"}

    # Compute the wrapper's per-fire log path so the UI can tail it later.
    # Patch 2026-05-14 (C1 follow-up): the wrapper used to append a timestamp
    # to the log filename, so the server's precomputed path was always wrong
    # and the UI's `lines: []` was permanent. Wrapper now accepts --log-path;
    # we pass a deterministic stamped path so both sides agree.
    log_dir = Path.home() / ".cache" / "visual-chef" / "hf_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_stamp = time.strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"{Path(row['filename']).stem}_{log_stamp}_pid{os.getpid()}.log"

    try:
        proc = subprocess.Popen(
            [
                sys.executable, str(WRAPPER_SCRIPT),
                "--payload-file", tmp_path,
                "--quiet",
                "--log-path", str(log_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=open(str(log_path), "a"),  # Fixes #23 — capture import errors
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "error": f"failed to spawn wrapper: {e}"}

    # Register the live fire so /api/fires can report it in real time
    # Patch 2026-05-14: enriched with shot_id, client, project, prompt_head,
    # refs_count, and a coarse eta_s (per VC-B finding). Lets the UI show a
    # meaningful per-fire row without re-reading the temp payload file.
    # Post-audit fix (C2/M1): estimated_cost was being read from `row` — but
    # there is no such column on `assets`; the value lives in the parsed
    # note_data we already have. refs_count was undercounting video drafts
    # (only image/media/refs — wrapper accepts start_image/end_image/video/audio).
    prompt_text = (payload.get("prompt") or "")
    ref_keys = ("image", "start_image", "end_image", "video", "audio", "media", "refs")
    refs_total = 0
    for k in ref_keys:
        v = payload.get(k)
        if isinstance(v, list):
            refs_total += len(v)
        elif isinstance(v, str) and v:
            refs_total += 1
    model_id = payload.get("model") or ""
    eta_s = _coarse_eta_for_model(model_id)
    STATE.register_fire(proc.pid, {
        "asset_id": asset_id,
        "filename": row["filename"],
        "gallery": str(STATE.folder) if STATE.folder else None,
        "model": model_id,
        "workflow": payload.get("workflow"),
        "shot_id": row["shot_id"],
        "client": row["client"],
        "project": row["project"],
        "prompt_head": prompt_text[:120],
        "refs_count": refs_total,
        "estimated_cost": note_data.get("estimated_cost") if isinstance(note_data, dict) else None,
        "eta_s": eta_s,
        "started_at": time.time(),
        "log_path": str(log_path),
        "payload_file": tmp_path,
        "proc": proc,
    })

    _audit("draft.fired", {
        "asset_id": asset_id,
        "filename": row["filename"],
        "pid": proc.pid,
        "payload_file": tmp_path,
        "model": model_id,
        "shot_id": row["shot_id"],
    }, source="api")

    # Transition draft → firing (stays here until wrapper exits).
    # Fixes #18 — previously flipped to 'review' immediately, which meant
    # failed fires left orphan 'review' rows with no media on disk.
    conn.execute(
        "UPDATE assets SET status = 'firing', last_updated_at = strftime('%s','now') WHERE id = ?",
        (asset_id,),
    )
    conn.commit()
    STATE.mark_changed()
    return {"ok": True, "pid": proc.pid, "payload_file": tmp_path}


def _scene_overview() -> dict:
    """Scene-grouped overview showing hero/accepted/alternate counts per scene,
    plus the assets themselves grouped by status. (#31 — scene workspace.)

    Returns {scenes: [{scene, hero: [...], accepted: [...], alternate: [...],
    counts: {hero, accepted, alternate, review, total}}], unassigned_count: N}
    """
    conn = STATE.conn()
    # Get all scenes with their keeper assets in one query
    rows = conn.execute(
        "SELECT a.* FROM assets a "
        "WHERE a.scene IS NOT NULL AND a.scene != '' "
        "AND a.status IN ('hero', 'accepted', 'alternate', 'revise', 'review', 'firing') "
        "ORDER BY a.scene, a.status, a.shot_id, a.first_seen_at DESC"
    ).fetchall()

    # Group by scene
    from collections import OrderedDict
    scenes_map: dict[str, dict] = OrderedDict()
    for r in rows:
        scene = r["scene"]
        if scene not in scenes_map:
            scenes_map[scene] = {"scene": scene, "hero": [], "accepted": [], "alternate": [], "review": [], "counts": {}}
        asset = _row_to_asset(r, STATE.thumb_dir)
        st = r["status"]
        if st == "hero":
            scenes_map[scene]["hero"].append(asset)
        elif st == "accepted":
            scenes_map[scene]["accepted"].append(asset)
        elif st in ("alternate", "revise"):
            scenes_map[scene]["alternate"].append(asset)
        elif st in ("review", "firing"):
            scenes_map[scene]["review"].append(asset)

    # Add counts
    for s in scenes_map.values():
        s["counts"] = {
            "hero": len(s["hero"]),
            "accepted": len(s["accepted"]),
            "alternate": len(s["alternate"]),
            "review": len(s["review"]),
            "total": len(s["hero"]) + len(s["accepted"]) + len(s["alternate"]) + len(s["review"]),
        }

    # Count assets with no scene assigned
    unassigned = conn.execute(
        "SELECT count(*) FROM assets WHERE (scene IS NULL OR scene = '') "
        "AND status NOT IN ('draft', 'rejected', 'legacy')"
    ).fetchone()[0]

    return {
        "scenes": list(scenes_map.values()),
        "total_scenes": len(scenes_map),
        "unassigned_count": unassigned,
    }


def _list_drafts() -> dict:
    conn = STATE.conn()
    rows = conn.execute(
        "SELECT id FROM assets WHERE status = 'draft' ORDER BY first_seen_at DESC"
    ).fetchall()
    items = []
    for r in rows:
        a = _get_asset(r["id"])
        if a:
            items.append(a)
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Static asset serving
# ---------------------------------------------------------------------------

_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
}


def _content_type(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"VisualChefGallery/{SERVER_VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        # Quieter logs — only errors. Args from BaseHTTPRequestHandler can be
        # non-numeric (e.g. "Unsupported method ('HEAD')") so we accept-and-skip.
        try:
            status = int(args[1])
        except (ValueError, IndexError, TypeError):
            return
        if status >= 400:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -------------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _send_file(self, path: Path, *, attachment: bool = False, no_cache: bool = False) -> None:
        if not path.exists() or not path.is_file():
            self._send_error_json(404, "not found")
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", _content_type(path))
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        if no_cache:
            # Used for the dashboard HTML so that fixes ship instantly without
            # the director having to hard-reload. Media files keep their cache
            # because they're large and content-addressable.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- routes --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)

        if path == "/":
            self._serve_dashboard()
            return
        if path == "/healthz":
            self._send_json(200, {
                "ok": True,
                "version": SERVER_VERSION,
                "current_folder": str(STATE.folder) if STATE.folder else None,
                "asset_count": STATE.asset_count(),
            })
            return
        if path == "/api/health":
            if STATE.folder is None or STATE.db_path is None:
                self._send_error_json(409, "no working folder set")
                return
            snap = obs_mod.health_snapshot(
                STATE.folder, STATE.db_path, wrapper_script=WRAPPER_SCRIPT,
            )
            self._send_json(200, snap)
            return
        if path == "/api/debug/orphans":
            if STATE.folder is None or STATE.db_path is None:
                self._send_error_json(409, "no working folder set")
                return
            self._send_json(200, obs_mod.find_orphans(STATE.folder, STATE.db_path))
            return
        if path == "/api/debug/recent-events":
            if STATE.log_path is None:
                self._send_json(200, [])
                return
            n_str = (params.get("n") or ["50"])[0]
            try:
                n = max(1, min(int(n_str), 500))
            except ValueError:
                n = 50
            filt = (params.get("filter") or [None])[0]
            severity = (params.get("severity") or [None])[0]
            include_test = (params.get("include_test", ["0"])[0] not in ("0", "false", ""))
            self._send_json(200, obs_mod.tail_events(
                STATE.log_path, n=n, event_filter=filt,
                include_test=include_test, severity=severity,
            ))
            return
        if path == "/api/fires":
            include_finished = (params.get("include_finished", ["1"])[0] != "0")
            self._send_json(200, {"fires": STATE.list_fires(include_finished=include_finished)})
            return
        m = re.match(r"^/api/fires/(\d+)/log$", path)
        if m:
            pid = int(m.group(1))
            fires = STATE.list_fires(include_finished=True)
            entry = next((f for f in fires if f["pid"] == pid), None)
            if entry is None:
                self._send_error_json(404, "fire not found")
                return
            log_path = Path(entry["log_path"])
            # Patch 2026-05-14: response now carries started_at_iso, log_size,
            # has_failure_audit, failure_class, exit_code so the UI can render
            # a proper progress + error card without extra round-trips. VC-B.
            started_at = entry.get("started_at") or 0
            from datetime import datetime, timezone as _tz
            started_iso = (
                datetime.fromtimestamp(started_at, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if started_at else None
            )
            # Failure audit sidecar lives next to the would-be output file (if any).
            failure_audit = None
            failure_class = None
            try:
                gallery_folder = STATE.folder
                if gallery_folder and entry.get("filename"):
                    fa_path = gallery_folder / f"{entry['filename']}.failed.json"
                    if fa_path.exists():
                        failure_audit = str(fa_path)
                        try:
                            with open(fa_path, "r", encoding="utf-8") as fp:
                                fa_data = json.load(fp)
                            failure_class = fa_data.get("error_class")
                        except (OSError, json.JSONDecodeError):
                            pass
            except Exception:  # noqa: BLE001 — best-effort
                pass
            if not log_path.exists():
                self._send_json(200, {
                    "pid": pid,
                    "state": entry["state"],
                    "started_at_iso": started_iso,
                    "has_failure_audit": failure_audit is not None,
                    "failure_audit_path": failure_audit,
                    "failure_class": failure_class,
                    "exit_code": entry.get("exit_code"),
                    "lines": [],
                    "note": "log not created yet",
                })
                return
            try:
                tail_n = int((params.get("n") or ["80"])[0])
            except ValueError:
                tail_n = 80
            try:
                log_size = log_path.stat().st_size
                with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
                    lines = fp.readlines()
                tail = lines[-tail_n:]
            except OSError as exc:
                self._send_error_json(500, f"log read failed: {exc}")
                return
            self._send_json(200, {
                "pid": pid,
                "state": entry["state"],
                "asset_id": entry.get("asset_id"),
                "filename": entry.get("filename"),
                "shot_id": entry.get("shot_id"),
                "model": entry.get("model"),
                "workflow": entry.get("workflow"),
                "started_at_iso": started_iso,
                "duration_s": entry.get("duration_s"),
                "exit_code": entry.get("exit_code"),
                "log_path": str(log_path),
                "log_size_bytes": log_size,
                "has_failure_audit": failure_audit is not None,
                "failure_audit_path": failure_audit,
                "failure_class": failure_class,
                "lines": [ln.rstrip("\n") for ln in tail],
            })
            return
        if path == "/api/drafts":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            self._send_json(200, _list_drafts())
            return
        if path == "/ref":
            # Serve an arbitrary reference image (by absolute path) so the side
            # panel can render ref thumbs. SECURITY: localhost-only server, but
            # still constrain reads to under the current gallery + standard
            # ref roots to avoid `?path=/etc/passwd` style mishaps.
            raw = (params.get("path") or [""])[0]
            self._serve_ref_file(raw)
            return
        if path == "/api/folder":
            self._send_json(200, STATE.folder_info())
            return
        if path == "/api/selection":
            # Read-side of the selection bridge. Returns full asset detail
            # for every ID currently selected. Dropped IDs (asset deleted
            # since selection) are silently filtered — `count` reflects the
            # survivors. Empty selection returns 200 with count: 0, never 404.
            sel = STATE.get_selection()
            assets = []
            if STATE.folder is not None:
                for aid in sel.get("asset_ids", []):
                    detail = _get_asset(aid)
                    if detail is not None:
                        assets.append(detail)
            self._send_json(200, {
                "folder": sel.get("folder"),
                "set_at": sel.get("set_at"),
                "set_by": sel.get("set_by"),
                "count": len(assets),
                "asset_ids": [a["id"] for a in assets],
                "assets": assets,
            })
            return
        if path == "/api/scenes":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            self._send_json(200, _scene_overview())
            return
        if path == "/api/facets":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            self._send_json(200, _facet_counts(params))
            return
        if path == "/api/assets":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            self._send_json(200, _list_assets(params))
            return

        if path == "/api/compare":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            self._send_json(200, _compare_assets(params))
            return

        m = re.match(r"^/api/assets/(\d+)$", path)
        if m:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            asset = _get_asset(int(m.group(1)))
            if asset is None:
                self._send_error_json(404, "asset not found")
                return
            self._send_json(200, asset)
            return

        m = re.match(r"^/thumb/([0-9a-f]+\.jpg)$", path)
        if m:
            self._serve_thumb(m.group(1))
            return

        m = re.match(r"^/media/(\d+)$", path)
        if m:
            self._serve_media(int(m.group(1)))
            return

        m = re.match(r"^/sidecar/(\d+)$", path)
        if m:
            self._serve_sidecar(int(m.group(1)))
            return

        self._send_error_json(404, f"no route: {path}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/folder":
            payload = self._read_json_body()
            new_folder = payload.get("path")
            if not new_folder:
                self._send_error_json(400, "missing 'path'")
                return
            try:
                info = STATE.set_folder(Path(new_folder), run_scan=bool(payload.get("scan", True)))
            except FileNotFoundError:
                self._send_error_json(404, f"folder not found: {new_folder}")
                return
            _audit("folder.switched", {"to": info["current"]})
            self._send_json(200, info)
            return

        if path == "/api/selection":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            payload = self._read_json_body()
            ids = payload.get("asset_ids")
            if ids is None:
                # Single-ID convenience: accept {"asset_id": N}
                single = payload.get("asset_id")
                ids = [single] if single is not None else []
            if not isinstance(ids, list):
                self._send_error_json(400, "asset_ids must be a list")
                return
            source = payload.get("source") or "director"
            sel = STATE.set_selection(ids, source=source)
            self._send_json(200, {
                "ok": True,
                "asset_ids": sel["asset_ids"],
                "count": len(sel["asset_ids"]),
                "set_by": sel["set_by"],
            })
            return

        if path == "/api/rescan":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            counts = STATE.rescan()
            _audit("folder.rescanned", counts)
            self._send_json(200, {**counts, "current": str(STATE.folder)})
            return

        m = re.match(r"^/api/assets/(\d+)/reviews$", path)
        if m:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            payload = self._read_json_body()
            updated = _add_review(int(m.group(1)), payload)
            if updated is None:
                self._send_error_json(404, "asset not found")
                return
            self._send_json(200, updated)
            return

        m = re.match(r"^/api/assets/(\d+)/inherit$", path)
        if m:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            payload = self._read_json_body()
            source_id = payload.get("source_id")
            if not source_id:
                self._send_error_json(400, "missing 'source_id'")
                return
            result = _inherit_properties(int(m.group(1)), int(source_id))
            if result is None:
                self._send_error_json(404, "asset not found")
                return
            self._send_json(200, result)
            return

        m = re.match(r"^/api/assets/(\d+)/open$", path)
        if m:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            ok, reason = _open_in_finder(int(m.group(1)))
            self._send_json(200 if ok else 400, {"opened": ok, "error": reason})
            return

        if path == "/api/rename":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            payload = self._read_json_body()
            result = _rename_asset(payload)
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
            return

        if path == "/api/draft":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            payload = self._read_json_body()
            result = _create_draft(payload)
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
            return

        m = re.match(r"^/api/draft/(\d+)/fire$", path)
        if m:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            result = _fire_draft(int(m.group(1)))
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
            return

        # POST /api/assets/bulk-status — change status on multiple assets at once
        if path == "/api/assets/bulk-status":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            payload = self._read_json_body()
            result = _bulk_status_change(payload)
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
            return

        # POST /api/fires — register an external fire (agent-spawned wrapper, #28)
        if path == "/api/fires":
            payload = self._read_json_body()
            pid = payload.get("pid")
            if pid is None:
                self._send_error_json(400, "missing 'pid'")
                return
            pid = int(pid)
            info = {
                "asset_id": payload.get("asset_id"),
                "filename": payload.get("filename", ""),
                "shot_id": payload.get("shot_id"),
                "model": payload.get("model"),
                "workflow": payload.get("workflow"),
                "client": payload.get("client"),
                "project": payload.get("project"),
                "started_at": payload.get("started_at") or time.time(),
                "log_path": payload.get("log_path", ""),
                "payload_file": payload.get("payload_file"),
                "external": True,
                # No proc handle — external fires are tracked by pid check + /complete call
            }
            STATE.register_fire(pid, info)
            _audit("fire.registered_external", {"pid": pid, "asset_id": info["asset_id"], "filename": info["filename"]})
            STATE.mark_changed()
            self._send_json(200, {"ok": True, "pid": pid, "registered": True})
            return

        # POST /api/fires/<pid>/complete — mark an external fire as finished (#28)
        m = re.match(r"^/api/fires/(\d+)/complete$", path)
        if m:
            pid = int(m.group(1))
            payload = self._read_json_body()
            exit_code = int(payload.get("exit_code", -1))
            with STATE._fires_lock:
                info = STATE._fires.get(pid)
                if info is None:
                    self._send_error_json(404, "fire not found")
                    return
                info["exit_code"] = exit_code
                info["finished_at"] = payload.get("finished_at") or time.time()
            # Transition the asset row (firing -> review/draft)
            asset_id = info.get("asset_id")
            if asset_id is not None:
                _transition_fire_status(asset_id, exit_code)
            _audit("fire.completed_external", {"pid": pid, "asset_id": asset_id, "exit_code": exit_code})
            STATE.mark_changed()
            self._send_json(200, {"ok": True, "pid": pid, "exit_code": exit_code})
            return

        # POST /api/debug/orphans/purge — delete all rows whose file is gone
        if path == "/api/debug/orphans/purge":
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            result = _purge_orphans()
            self._send_json(200, result)
            return

        self._send_error_json(404, f"no route: {path}")

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        m = re.match(r"^/api/draft/(\d+)$", path)
        if m:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            payload = self._read_json_body()
            result = _edit_draft(int(m.group(1)), payload)
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
            return
        self._send_error_json(404, f"no route: {path}")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        m = re.match(r"^/api/draft/(\d+)$", path)
        if m:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            result = _delete_draft(int(m.group(1)))
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
            return
        if path == "/api/selection":
            sel = STATE.clear_selection()
            self._send_json(200, {"ok": True, "count": 0, "set_by": sel["set_by"]})
            return
        # DELETE /api/assets/<id> — hard-delete a single asset row + its thumb
        m2 = re.match(r"^/api/assets/(\d+)$", path)
        if m2:
            if STATE.folder is None:
                self._send_error_json(409, "no working folder set")
                return
            result = _delete_asset(int(m2.group(1)))
            status = 200 if result.get("ok") else (404 if "not found" in result.get("error", "") else 400)
            self._send_json(status, result)
            return
        self._send_error_json(404, f"no route: {path}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS preflight + capability discovery.
        Patch 2026-05-14: prior server returned 501 'Unsupported method' on
        any OPTIONS request, which broke browser-side tools and any client
        doing a preflight before POST/PUT/DELETE on /api/draft etc.
        """
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        m = re.match(r"^/api/assets/(\d+)$", path)
        if not m:
            self._send_error_json(404, f"no route: {path}")
            return
        if STATE.folder is None:
            self._send_error_json(409, "no working folder set")
            return
        payload = self._read_json_body()
        updated = _patch_asset(int(m.group(1)), payload)
        if updated is None:
            self._send_error_json(404, "asset not found")
            return
        self._send_json(200, updated)

    # -- file serving --------------------------------------------------------

    def _serve_dashboard(self) -> None:
        # Canonical project-agnostic dashboard lives next to this script.
        # The same shell drives every working folder — switching episodes
        # is just changing which folder the server points at.
        canonical = _HERE / "visual_chef_gallery.html"
        if canonical.exists():
            self._send_file(canonical, no_cache=True)
            return
        # Built-in placeholder fallback (if file is missing)
        html = _PLACEHOLDER_HTML
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_thumb(self, name: str) -> None:
        if STATE.thumb_dir is None:
            self._send_error_json(409, "no working folder set")
            return
        target = STATE.thumb_dir / name
        if not target.exists():
            # Reverse-map the hash → source path via DB.
            # Fixes #17/M5: was a full table scan + Python-side filter.
            # Now queries by thumb_path directly (indexed column), falling
            # back to file_path hash only if thumb_path was never populated.
            # Fixes #17/M5: query by indexed thumb_path column first.
            # Fallback scans only rows with NULL thumb_path (pre-backfill).
            match = STATE.conn().execute(
                "SELECT id, file_path FROM assets WHERE thumb_path = ?",
                (name,),
            ).fetchone()
            if match is None:
                # Fallback: compute hash from the requested thumb name and try
                # to find the asset by file_path hash. Limit the scan to avoid
                # O(N) on every missing thumb request.
                sha = name[:-4]  # strip .jpg
                rows = STATE.conn().execute(
                    "SELECT id, file_path FROM assets WHERE thumb_path IS NULL LIMIT 500"
                ).fetchall()
                for r in rows:
                    if lib.thumb_key(r["file_path"]) == sha:
                        match = r
                        # Backfill thumb_path so this row is found via index next time
                        STATE.conn().execute(
                            "UPDATE assets SET thumb_path = ? WHERE id = ?",
                            (name, r["id"]),
                        )
                        STATE.conn().commit()
                        break
            if match is None:
                self._send_error_json(404, "thumbnail not found")
                return
            name2 = thumb_mod.ensure_thumb(match["file_path"], STATE.thumb_dir)
            if not name2:
                self._send_error_json(500, "thumbnail generation failed")
                return
            STATE.conn().execute(
                "UPDATE assets SET thumb_path = ?, thumb_generated_at = strftime('%s','now') WHERE id = ?",
                (name2, match["id"]),
            )
            target = STATE.thumb_dir / name2
        self._send_file(target)

    def _serve_media(self, asset_id: int) -> None:
        row = STATE.conn().execute(
            "SELECT file_path FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            self._send_error_json(404, "asset not found")
            return
        self._serve_with_range(Path(row["file_path"]))

    def _serve_ref_file(self, raw_path: str) -> None:
        """Serve an arbitrary ref image. Path constraints:
        - Must be absolute
        - Must exist
        - Must live under one of: STATE.folder, a common refs root, or /tmp
        Otherwise 403.
        """
        if not raw_path:
            self._send_error_json(400, "missing path")
            return
        p = Path(raw_path).expanduser().resolve()
        # Allowlist roots — localhost dashboard, but still don't let it read
        # outside expected territories.
        #
        # SECURITY PATCH 2026-05-14: previously `_HERE.parent.parent.resolve()`
        # was added as a project-root allow. When the repo lived at
        # `<...>/arsenal/00-utilities/`, parent.parent resolved to the AI
        # visual chef project root — fine. After the 2026-05-14 move to
        # `/Users/ayo/Coding projects/vc-gallery/`, parent.parent resolved to
        # `/Users/ayo/` itself — which would let /ref read `~/Documents`,
        # `~/Downloads`, etc. Dropped entirely; only the gallery folder,
        # `~/Desktop`, `/tmp`, and an explicit env-var allowlist are valid.
        allowed_roots = []
        if STATE.folder:
            allowed_roots.append(STATE.folder.resolve())
            # Fixes #20: ref images live in sibling directories of the gallery
            # folder (e.g. clients/, characters/).  Allow the project root
            # (one level up from the gallery folder) so all project refs resolve.
            proj_root = STATE.folder.resolve().parent
            if proj_root != STATE.folder.resolve():
                allowed_roots.append(proj_root)
        # ~/Desktop kept because that's where the director keeps client
        # working folders.  Cross-platform (was hardcoded to /Users/ayo/).
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            allowed_roots.append(desktop.resolve())
        # Operator can extend via VC_REF_ALLOW_ROOTS (colon-separated absolute paths).
        extra = os.environ.get("VC_REF_ALLOW_ROOTS", "")
        for extra_root in [e.strip() for e in extra.split(":") if e.strip()]:
            er = Path(extra_root).expanduser().resolve()
            if er.exists():
                allowed_roots.append(er)

        # Use proper path-boundary check, NOT str.startswith — otherwise
        # /Users/ayo/Desktop-secrets/foo would pass when root is /Users/ayo/Desktop.
        def _under(child: Path, root: Path) -> bool:
            try:
                child.relative_to(root)
                return True
            except ValueError:
                return False
        ok = any(_under(p, root) for root in allowed_roots)
        if not ok:
            self._send_error_json(403, "path not under any allowed root")
            return
        if not p.exists() or not p.is_file():
            self._send_error_json(404, "ref file not found")
            return
        self._serve_with_range(p)

    def _serve_sidecar(self, asset_id: int) -> None:
        row = STATE.conn().execute(
            "SELECT sidecar_path FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if row is None or not row["sidecar_path"]:
            self._send_error_json(404, "no sidecar")
            return
        self._send_file(Path(row["sidecar_path"]))

    def _serve_with_range(self, path: Path) -> None:
        """Serve a file with HTTP Range support — required for inline <video> playback."""
        if not path.exists() or not path.is_file():
            self._send_error_json(404, "not found")
            return
        size = path.stat().st_size
        range_hdr = self.headers.get("Range")
        ctype = _content_type(path)

        if not range_hdr:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with path.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
        if not m:
            self.send_response(416)
            self.end_headers()
            return
        start = int(m.group(1)) if m.group(1) else 0
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        length = end - start + 1
        if length <= 0:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


# ---------------------------------------------------------------------------
# Placeholder HTML — minimal frontend so the server boots usable
# ---------------------------------------------------------------------------

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Visual Chef Gallery</title>
<style>
  body { background:#0d0d0f; color:#e6e6e6; font: 14px/1.5 -apple-system, Inter, system-ui, sans-serif; margin:0; padding:32px; }
  h1 { font-weight:500; letter-spacing:-0.01em; }
  a { color:#e4a44a; text-decoration:none; }
  .row { display:flex; gap:12px; align-items:center; margin:16px 0; }
  input { background:#161618; color:#e6e6e6; border:1px solid #2a2a2e; padding:8px 12px; border-radius:6px; min-width:520px; font:inherit; }
  button { background:#1d1d20; color:#e6e6e6; border:1px solid #2a2a2e; padding:8px 14px; border-radius:6px; cursor:pointer; font:inherit; }
  button:hover { border-color:#e4a44a; color:#e4a44a; }
  pre { background:#161618; padding:16px; border-radius:6px; overflow:auto; }
  .muted { color:#7a7a82; }
</style>
</head>
<body>
<h1>Visual Chef Gallery — placeholder</h1>
<p class="muted">The dashboard shell isn't installed yet. The API is live.</p>
<div class="row">
  <input id="folder" placeholder="/Users/ayo/Desktop/Client/Dave/BTW/EP8">
  <button onclick="setFolder()">Open folder</button>
  <button onclick="rescan()">Rescan</button>
</div>
<pre id="out">loading…</pre>
<script>
async function status() {
  const r = await fetch('/healthz');
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
async function setFolder() {
  const path = document.getElementById('folder').value.trim();
  if (!path) return;
  const r = await fetch('/api/folder', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path}) });
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
async function rescan() {
  const r = await fetch('/api/rescan', { method:'POST' });
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
fetch('/api/folder').then(r=>r.json()).then(j => {
  if (j.current) document.getElementById('folder').value = j.current;
  status();
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--folder", help="Working folder. Defaults to last-used from config.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--no-scan", action="store_true", help="Skip auto-scan on boot")
    args = ap.parse_args(argv)

    cfg = lib.load_server_config()
    chosen = args.folder or cfg.get("current_folder")
    if chosen:
        try:
            STATE.set_folder(Path(chosen), run_scan=not args.no_scan)
            print(f"working folder: {STATE.folder}", file=sys.stderr)
            print(f"db: {STATE.db_path}", file=sys.stderr)
            print(f"assets: {STATE.asset_count()}", file=sys.stderr)
        except FileNotFoundError as e:
            print(f"WARN: folder not found: {e}. Boot without folder.", file=sys.stderr)
    else:
        print("WARN: no folder set. Open one via POST /api/folder.", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"serving at {url}", file=sys.stderr)
    WATCHER.start()
    print(f"watcher: polling every {FolderWatcher.POLL_INTERVAL}s", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        WATCHER.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
