"""
Red-team — extra attacks discovered during deep audit.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import vc_gallery_serve as srv  # noqa: E402


@pytest.fixture
def temp_gallery(tmp_path):
    folder = tmp_path / "gallery"
    folder.mkdir()
    srv.STATE.set_folder(folder, run_scan=False)
    yield folder
    try:
        if srv.STATE._conn is not None:
            srv.STATE._conn.close()
            srv.STATE._conn = None
    except Exception:
        pass
    srv.STATE.folder = None
    srv.STATE._fires.clear()


def _stage(filename="rt.png"):
    payload = {
        "filename": filename,
        "client": "rt", "project": "rt", "shot_id": "SH",
        "model": "nano_banana_2", "workflow": "rt",
        "payload": {"model": "nano_banana_2", "prompt": "rt", "image": []},
    }
    result = srv._create_draft(payload)
    return result["asset"]["id"]


# ═══════════════════════════════════════════════════════════════════════
# Attack X1 — Dual-registration: wrapper /api/fires overwrites server's
# Popen-handle entry, losing the proc handle.
# ═══════════════════════════════════════════════════════════════════════


def test_attackX1_wrapper_api_fires_overwrites_server_proc_handle(temp_gallery):
    """Sequence:
      1. _fire_draft Popens wrapper, gets proc.pid=N, registers STATE._fires[N]
         with `proc` handle.
      2. Wrapper starts and calls POST /api/fires with {pid: os.getpid()==N, ...}.
      3. /api/fires handler unconditionally writes STATE._fires[N] with
         `external: True` and NO `proc` key.
      4. Now list_fires can no longer poll proc.poll() for this fire.

    Verify the bug exists. Also verify the safety net (os.kill) still detects
    death so transitions still happen eventually.
    """
    asset_id = _stage("rt_dualreg.png")

    captured = {"argv": None}
    def _fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        return proc

    with patch.object(srv.subprocess, "Popen", side_effect=_fake_popen):
        srv._fire_draft(asset_id)

    # Server registered with proc handle
    fire_before = srv.STATE._fires.get(12345)
    assert fire_before is not None
    assert fire_before.get("proc") is not None, "Server should have proc handle"
    assert "external" not in fire_before, "Server-registered fire should not be 'external'"

    # Now simulate the wrapper's /api/fires call (same pid)
    external_info = {
        "asset_id": asset_id,
        "filename": "rt_dualreg.png",
        "shot_id": "SH",
        "model": "nano_banana_2",
        "workflow": "rt",
        "client": "rt",
        "project": "rt",
        "started_at": time.time(),
        "log_path": "/tmp/some.log",
        "payload_file": "/tmp/draft_xxx.json",
        "external": True,
    }
    srv.STATE.register_fire(12345, external_info)

    fire_after = srv.STATE._fires.get(12345)
    if fire_after.get("proc") is None and fire_after.get("external") is True:
        # This is the dual-registration race condition. Severity: MEDIUM
        # — recovery via os.kill still works, but a subtle code smell.
        pytest.fail(
            "MEDIUM: wrapper's POST /api/fires unconditionally overwrites the "
            "server's pre-registration of the same fire, dropping the `proc` "
            "Popen handle. list_fires falls back to os.kill liveness check, "
            "which still works — but the Popen handle is leaked. Fix: in the "
            "/api/fires handler, if STATE._fires[pid] already exists with a "
            "proc handle, MERGE the new fields instead of replacing wholesale."
        )


# ═══════════════════════════════════════════════════════════════════════
# Attack X2 — Server creates payload temp; wrapper deletes on schema fail.
# Verify cleanup happens BEFORE the audit log line, so the audit log path
# refers to a deleted file.
# ═══════════════════════════════════════════════════════════════════════


def test_attackX2_audit_log_references_deleted_payload_file(temp_gallery):
    """The audit log line 'draft.fired' records `payload_file=tmp_path`.
    Then on wrapper exit, the wrapper deletes the file. So any post-mortem
    debugging by reading the audit log's payload_file path will find a
    missing file.

    This is intentional (the file is single-use), but document: if the
    director or a debugger wants to inspect the actual payload that was
    sent, the audit trail is broken once the wrapper exits. Severity LOW.
    """
    # Stage + fire (mock Popen so no real wrapper runs)
    asset_id = _stage("rt_audit.png")

    captured = {"argv": None}
    def _fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        proc = MagicMock()
        proc.pid = 33333
        proc.poll.return_value = 0  # already done
        return proc

    with patch.object(srv.subprocess, "Popen", side_effect=_fake_popen):
        result = srv._fire_draft(asset_id)

    assert result.get("ok")
    payload_file = result.get("payload_file")
    assert payload_file is not None

    # The file should EXIST right now (wrapper hasn't deleted it because
    # we mocked Popen — no actual wrapper ran). In production: after
    # wrapper exits, the file is gone. So the audit log's payload_file
    # is a dangling reference after fire completes.

    # Defense: just confirm the file was created (preconditions).
    assert os.path.exists(payload_file), (
        f"Server should have written payload file; missing: {payload_file}"
    )
    # Cleanup (since the mock means the wrapper never ran/cleaned)
    if os.path.exists(payload_file):
        os.unlink(payload_file)


# ═══════════════════════════════════════════════════════════════════════
# Attack X3 — Fire endpoint passes --asset-id as str(int), but the wrapper
# accepts `type=int`. If someone changes the schema to allow asset_id as
# string in payload (future drift), would this still work?
# ═══════════════════════════════════════════════════════════════════════


def test_attackX3_asset_id_argparse_int_strict():
    """The wrapper's argparse declares --asset-id type=int. If someone passes
    --asset-id "not_a_number" via CLI, argparse should error cleanly.
    Verify."""
    import subprocess
    payload = {
        "filename": "rt.png",
        "client": "rt", "project": "rt", "workflow": "unknown",
        "gallery": "/tmp", "prompt": "rt", "model": "nano_banana_2",
    }
    pf = Path("/tmp/draft_redteam_argparse_test.json")
    pf.write_text(json.dumps(payload))
    try:
        result = subprocess.run(
            [sys.executable, str(_REPO / "hf_gen_with_sidecar.py"),
             "--payload-file", str(pf), "--asset-id", "not_a_number",
             "--dry-run", "--quiet", "--notify-server", ""],
            capture_output=True, text=True, timeout=10,
        )
        # argparse exits with code 2 on bad arg type
        assert result.returncode == 2, (
            f"argparse should reject non-int --asset-id. rc={result.returncode}, "
            f"stderr={result.stderr!r}"
        )
        assert "invalid int value" in result.stderr.lower() or "asset-id" in result.stderr.lower()
    finally:
        if pf.exists():
            pf.unlink()


# ═══════════════════════════════════════════════════════════════════════
# Attack X4 — Negative asset_id (e.g. -1) passes argparse but mutates wrong row
# ═══════════════════════════════════════════════════════════════════════


def test_attackX4_negative_asset_id_falls_through(temp_gallery):
    """argparse type=int allows negative values. Pass --asset-id -1.
    upsert_asset_direct checks `WHERE id = -1` — nothing matches — falls
    through to path-keyed insert. Safe."""
    sys.path.insert(0, str(_REPO))
    import hf_gen_with_sidecar as wrapper

    target = temp_gallery / "rt_neg.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    payload = {
        "filename": "rt_neg.png",
        "client": "rt", "project": "rt", "shot_id": "SH",
        "model": "nano_banana_2", "workflow": "rt.neg",
        "gallery": str(temp_gallery), "prompt": "rt neg",
        "skip_sidecar": True, "status": "review",
    }
    # Negative id
    ok = wrapper._write_db_row(
        payload, target, "rt neg", [],
        job_id="j-neg", asset_id=-1,
    )
    assert ok

    conn = srv.STATE.conn()
    rows = conn.execute("SELECT id, filename, status FROM assets").fetchall()
    # Fallback insert means one row exists with status='review'
    assert len(rows) == 1
    assert rows[0]["id"] != -1  # SQLite assigns a positive rowid
    assert rows[0]["status"] == "review"


# ═══════════════════════════════════════════════════════════════════════
# Attack X5 — Wrapper deletes its payload file but log_path persists.
# If the directory containing the payload is somehow special (the
# server's tempfile.mkstemp uses default tmp dir which on macOS is
# /var/folders/.../T/), check that nothing weird happens.
# ═══════════════════════════════════════════════════════════════════════


def test_attackX5_server_uses_default_tmp_not_slash_tmp(temp_gallery):
    """Red-team F1 regression: wrapper cleanup must match the server's
    tempfile.mkstemp(prefix='draft_<id>_') output by BASENAME, not by
    "/tmp/" substring. On macOS, mkstemp uses TMPDIR=/var/folders/<hash>/T/
    so a substring check on "/tmp/draft_" silently skipped cleanup and the
    file leaked forever.

    FIXED PR #53 (post-redteam): wrapper now matches
    `os.path.basename(pf).startswith("draft_") and pf.endswith(".json")`
    which works on every platform regardless of TMPDIR.

    This test verifies the basename check matches mkstemp's actual output
    on the host platform. If it doesn't match, the cleanup will leak again.
    """
    import tempfile as tmp
    fd, p = tmp.mkstemp(prefix="draft_999_", suffix=".json")
    os.close(fd)
    try:
        # Replicate the wrapper's exact check (hf_gen_with_sidecar.py main()):
        cleanup_matches = (
            os.path.basename(p).startswith("draft_") and p.endswith(".json")
        )
        if not cleanup_matches:
            pytest.fail(
                f"REGRESSION: mkstemp default temp path {p} does not match "
                f"the wrapper's basename-based cleanup heuristic "
                f"(basename startswith 'draft_' AND endswith '.json'). "
                f"File would leak on this platform."
            )
    finally:
        if os.path.exists(p):
            os.unlink(p)
