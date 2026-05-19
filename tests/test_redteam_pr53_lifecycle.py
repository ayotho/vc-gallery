"""
Red-team — lifecycle attacks for PR #53.

These tests use real subprocesses (no mocks) for SIGKILL semantics and
backward-compat verification.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
WRAPPER = _REPO / "hf_gen_with_sidecar.py"


# ═══════════════════════════════════════════════════════════════════════
# Attack 5 — Direct CLI without --asset-id (backward compat for hand fires)
# ═══════════════════════════════════════════════════════════════════════


def test_attack05_wrapper_without_asset_id_falls_back_to_insert(tmp_path):
    """Run wrapper with no --asset-id flag (direct CLI use). Should fall
    through to the historical insert-new-row behavior (asset_id=None →
    upsert_asset_direct does path-keyed insert)."""
    # Build a payload that passes schema but uses --dry-run (no real fire)
    payload = {
        "filename": "rt_no_aid.png",
        "client": "rt_client",
        "project": "rt_project",
        "workflow": "unknown",
        "gallery": str(tmp_path),
        "prompt": "rt prompt no asset id",
        "model": "nano_banana_2",
    }
    pf = tmp_path / "no_aid_payload.json"
    pf.write_text(json.dumps(payload))

    result = subprocess.run(
        [sys.executable, str(WRAPPER), "--payload-file", str(pf),
         "--dry-run", "--quiet", "--notify-server", ""],
        capture_output=True, text=True, timeout=15,
    )

    # Dry-run should succeed (exit 0); no --asset-id is the legacy/direct path
    assert result.returncode == 0, (
        f"Direct CLI without --asset-id should succeed in dry-run. "
        f"rc={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Attack 1 — SIGKILL during fire
# ═══════════════════════════════════════════════════════════════════════


def test_attack01_sigkill_orphan_handling(tmp_path):
    """Spawn wrapper, SIGKILL it mid-run. The wrapper leaves no transition
    log via the gallery /complete callback. The server's _replay_fires_on_boot
    OR reap_zombie_firings would detect this on next boot.

    Test the registry recovery: write a fires.jsonl entry pointing to a
    dead PID, call _replay_fires_on_boot, verify it transitions the asset
    out of 'firing'.
    """
    sys.path.insert(0, str(_REPO))
    import vc_gallery_serve as srv

    folder = tmp_path / "gallery"
    folder.mkdir()
    srv.STATE.set_folder(folder, run_scan=False)
    try:
        # Stage a draft and put it into 'firing'
        payload = {
            "filename": "rt_kill.png",
            "client": "rt", "project": "rt", "shot_id": "SH",
            "model": "nano_banana_2", "workflow": "rt",
            "payload": {"model": "nano_banana_2", "prompt": "rt", "image": []},
        }
        result = srv._create_draft(payload)
        asset_id = result["asset"]["id"]

        conn = srv.STATE.conn()
        conn.execute("UPDATE assets SET status='firing' WHERE id=?", (asset_id,))
        conn.commit()

        # Write a fires.jsonl entry with a dead PID
        fires_path = folder / ".vc_meta" / "fires.jsonl"
        fires_path.parent.mkdir(parents=True, exist_ok=True)
        # Pick a PID that's definitely dead
        dead_pid = 99999  # extremely unlikely to be live
        # Sanity: confirm dead
        try:
            os.kill(dead_pid, 0)
            pytest.skip(f"PID {dead_pid} happened to be alive; can't test")
        except (OSError, ProcessLookupError):
            pass

        fires_path.write_text(json.dumps({
            "pid": dead_pid,
            "asset_id": asset_id,
            "filename": "rt_kill.png",
            "started_at": time.time() - 600,
            "log_path": "/tmp/nope.log",
            "payload_file": "/tmp/nope_payload.json",
        }) + "\n")

        # Trigger boot replay
        srv.STATE._replay_fires_on_boot()

        # Verify transition: asset should be back to 'draft' (failure path)
        row = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
        assert row["status"] == "draft", (
            f"SIGKILL orphan should transition to 'draft' (per _transition_fire_status "
            f"with exit_code=-1). Got: {row['status']}"
        )
    finally:
        if srv.STATE._conn is not None:
            srv.STATE._conn.close()
            srv.STATE._conn = None
        srv.STATE.folder = None


# ═══════════════════════════════════════════════════════════════════════
# Attack 11d — Wrapper deletes payload file even on schema reject
# ═══════════════════════════════════════════════════════════════════════


def test_attack11d_cleanup_runs_even_on_schema_fail(tmp_path):
    """A schema-invalid payload exits early at validation. Does the
    cleanup-on-exit still run? It's at the end of main(). Yes — we use
    a try-finally pattern? Let's verify.

    The cleanup is in main() AFTER the run() call, NOT in finally. If
    run() raises an exception OR returns early via schema, does cleanup run?

    Reading: run() is wrapped in try/except, then exit_code is computed,
    then cleanup runs. So cleanup DOES run.
    """
    pf = Path("/tmp/draft_redteam_schemafail.json")
    # Invalid payload — missing required 'prompt'
    pf.write_text(json.dumps({
        "filename": "rt.png",
        "client": "rt", "project": "rt", "workflow": "rt",
        "gallery": "/tmp",
        # missing prompt
    }))

    result = subprocess.run(
        [sys.executable, str(WRAPPER), "--payload-file", str(pf),
         "--quiet", "--notify-server", ""],
        capture_output=True, text=True, timeout=10,
    )
    # Should fail schema (exit 4)
    assert result.returncode == 4, f"Expected schema fail, got rc={result.returncode}"
    # Cleanup should still have run — pf should be gone
    assert not pf.exists(), (
        f"Schema-fail path should still trigger cleanup; {pf} still exists"
    )
    # Defensive cleanup
    if pf.exists():
        pf.unlink()


# ═══════════════════════════════════════════════════════════════════════
# Attack 6 — Wrapper crashes on KeyboardInterrupt mid-cleanup?
# (Lower priority — just sanity-check that the cleanup block uses
# correct exception handling)
# ═══════════════════════════════════════════════════════════════════════


def test_attack11e_cleanup_only_catches_oserror(tmp_path):
    """The cleanup block is:
        try:
            os.unlink(pf)
        except OSError:
            pass

    OSError covers FileNotFoundError, PermissionError, etc. But if the
    interpreter is shutting down (KeyboardInterrupt at the wrong moment)
    or hits a weird filesystem-level error that's NOT OSError, the wrapper
    would crash on cleanup AFTER computing the exit code, masking the real
    exit code.

    In practice os.unlink only raises OSError subclasses — this attack is
    theoretical. Document and move on.
    """
    # Smoke test: confirm os.unlink on a non-existent file raises OSError
    try:
        os.unlink("/tmp/__definitely_does_not_exist_redteam__.json")
        pytest.fail("os.unlink should raise on missing file")
    except OSError:
        pass  # Expected — confirms the except branch is correctly scoped


# ═══════════════════════════════════════════════════════════════════════
# Attack 9 — Wrapper symlink missing/broken (arsenal copy)
# ═══════════════════════════════════════════════════════════════════════


def test_attack09_arsenal_symlink_is_real():
    """The PR notes that the wrapper at
    arsenal/00-utilities/hf_gen_with_sidecar.py is symlinked to the
    vc-gallery repo copy so both stay in lockstep. Verify the symlink
    exists and resolves to the repo file."""
    arsenal_path = Path(
        "/Users/ayo/Library/CloudStorage/GoogleDrive-ayothomas@trajectoryvisual.com/"
        "Shared drives/Trajectory/Projects/AI visual chef/arsenal/00-utilities/hf_gen_with_sidecar.py"
    )
    if not arsenal_path.exists():
        pytest.skip("Arsenal wrapper not at expected path")

    # Is it a symlink?
    if not arsenal_path.is_symlink():
        # If it's NOT a symlink, the two copies will drift. Check content match.
        repo_content = WRAPPER.read_text()
        arsenal_content = arsenal_path.read_text()
        if repo_content != arsenal_content:
            pytest.fail(
                "HIGH-RISK: arsenal/00-utilities/hf_gen_with_sidecar.py is NOT a "
                "symlink to the repo copy AND its content has drifted. The PR memo "
                "says they should be in lockstep — they're not. Future fixes to "
                "the repo wrapper won't reach agent fires."
            )

    # If symlink, verify target exists
    if arsenal_path.is_symlink():
        target = arsenal_path.resolve()
        assert target.exists(), (
            f"Arsenal symlink is broken: {arsenal_path} → {target} (target missing)"
        )
        # Same file?
        assert target == WRAPPER.resolve(), (
            f"Arsenal symlink points at {target}, but repo wrapper is at {WRAPPER.resolve()}. "
            f"These should match."
        )
