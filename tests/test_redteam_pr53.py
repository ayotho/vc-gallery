"""
Red-team attacks for PR #53 (fix/52-asset-id-via-cli-flag).

Adversarial coverage — designed to BREAK the PR before merge. Each test
documents the attack, the expected behavior, and the observed behavior.
Tests are wrapped to PASS-when-system-behaves-correctly so output is a
clean pass/fail summary.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import vc_gallery_serve as srv  # noqa: E402


# ─── Shared fixtures ─────────────────────────────────────────────────


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
    # Wipe live fires registry between tests
    srv.STATE._fires.clear()


def _stage_draft(filename="rt_test.png", model="nano_banana_2", prompt="rt prompt") -> int:
    payload = {
        "filename": filename,
        "client": "rt_client",
        "project": "rt_project",
        "shot_id": "SH900",
        "model": model,
        "workflow": "redteam.fire",
        "payload": {
            "model": model,
            "prompt": prompt,
            "image": [],
        },
    }
    result = srv._create_draft(payload)
    assert result.get("ok"), f"_create_draft failed: {result}"
    return result["asset"]["id"]


@pytest.fixture
def fake_popen_factory():
    """Yields a function that returns (captured_argvs_list, patch_obj).

    Use when you need to invoke Popen multiple times and inspect all argvs.
    """
    captured_list = []

    def _capture(argv, **kwargs):
        captured_list.append(list(argv))
        proc = MagicMock()
        proc.pid = 90000 + len(captured_list)
        proc.poll.return_value = None  # appears live until we override
        return proc

    patcher = patch.object(srv.subprocess, "Popen", side_effect=_capture)
    patcher.start()
    yield captured_list
    patcher.stop()


# ═══════════════════════════════════════════════════════════════════════
# Attack 2 — Multi-fire on same draft (rapid double POST /fire)
# ═══════════════════════════════════════════════════════════════════════


def test_attack02_multi_fire_same_draft(temp_gallery, fake_popen_factory):
    """Rapid double-fire on the same draft. Second call should ERROR (draft
    is no longer in status=draft after first fire). Verifies idempotency
    guard: status transitions to 'firing' so second call rejects with
    'asset is not a draft'."""
    asset_id = _stage_draft(filename="rt_multifire.png")

    r1 = srv._fire_draft(asset_id)
    assert r1.get("ok"), f"First fire failed: {r1}"

    r2 = srv._fire_draft(asset_id)
    assert not r2.get("ok"), f"Second fire should reject; got: {r2}"
    assert "not a draft" in (r2.get("error") or ""), (
        f"Expected 'not a draft' error; got: {r2}"
    )

    # Only one wrapper should have been spawned
    assert len(fake_popen_factory) == 1, (
        f"Multi-fire spawned {len(fake_popen_factory)} wrappers — should be 1"
    )


# ═══════════════════════════════════════════════════════════════════════
# Attack 3 — Re-fire a draft that has fired once and completed
# ═══════════════════════════════════════════════════════════════════════


def test_attack03_refire_completed_draft(temp_gallery, fake_popen_factory):
    """After a draft fires + transitions to review, calling _fire_draft on
    that asset_id again should reject (no longer a draft)."""
    asset_id = _stage_draft(filename="rt_refire.png")
    srv._fire_draft(asset_id)

    # Simulate wrapper success: flip status to review
    conn = srv.STATE.conn()
    conn.execute("UPDATE assets SET status='review' WHERE id=?", (asset_id,))
    conn.commit()

    r2 = srv._fire_draft(asset_id)
    assert not r2.get("ok"), f"Re-fire of completed asset should reject: {r2}"
    assert "not a draft" in (r2.get("error") or "")


# ═══════════════════════════════════════════════════════════════════════
# Attack 6 — Wrapper invoked with stale --asset-id pointing at nonexistent row
# ═══════════════════════════════════════════════════════════════════════


def test_attack06_wrapper_stale_asset_id_falls_through(temp_gallery):
    """Direct wrapper call with --asset-id pointing at a nonexistent row.
    upsert_asset_direct's docstring promises: 'if no row exists with that id
    we fall through to the path-keyed code path and a fresh INSERT'.
    Verifies that fallback works."""
    sys.path.insert(0, str(_REPO))
    import hf_gen_with_sidecar as wrapper  # noqa: E402

    # Make a real file the wrapper can stat
    target = temp_gallery / "rt_stale.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    payload = {
        "filename": "rt_stale.png",
        "client": "rt_client",
        "project": "rt_project",
        "shot_id": "SH900",
        "model": "nano_banana_2",
        "workflow": "rt.stale",
        "gallery": str(temp_gallery),
        "prompt": "rt stale prompt",
        "skip_sidecar": True,
        "status": "review",
    }

    # Use a definitely-stale id
    stale_id = 9999999
    ok = wrapper._write_db_row(
        payload, target, "rt stale prompt", [],
        job_id="job-stale", asset_id=stale_id,
    )
    assert ok, "Wrapper failed on stale asset_id fallback"

    # A new row should exist at the real path; original stale id should NOT
    conn = srv.STATE.conn()
    rows = conn.execute("SELECT id, filename FROM assets").fetchall()
    assert len(rows) == 1, f"Expected 1 row (fallback insert); got: {[dict(r) for r in rows]}"
    assert rows[0]["id"] != stale_id


# ═══════════════════════════════════════════════════════════════════════
# Attack 7 — Wrapper invoked with --asset-id pointing at a DIFFERENT draft
# ═══════════════════════════════════════════════════════════════════════


def test_attack07_wrapper_wrong_asset_id_corrupts_other_draft(temp_gallery):
    """Red-team F2 regression: Two drafts staged (A and B). Wrapper run with
    payload for A but --asset-id pointing at B's id. Wrapper must detect the
    mismatch and refuse — not silently corrupt B's row.

    FIXED PR #53 (post-redteam): _write_db_row now compares payload.filename
    against the row at asset_id and returns False (failing loudly to stderr)
    if they don't match. B's row stays untouched.
    """
    sys.path.insert(0, str(_REPO))
    import hf_gen_with_sidecar as wrapper  # noqa: E402

    # Stage two drafts
    a_id = _stage_draft(filename="rt_draft_a.png", prompt="a prompt")
    b_id = _stage_draft(filename="rt_draft_b.png", prompt="b prompt")

    # Pretend wrapper ran for A but was passed B's asset_id
    target = temp_gallery / "rt_draft_a.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    payload = {
        "filename": "rt_draft_a.png",  # payload says A
        "client": "rt_client",
        "project": "rt_project",
        "shot_id": "SH_A",
        "model": "nano_banana_2",
        "workflow": "rt.mismatch",
        "gallery": str(temp_gallery),
        "prompt": "a prompt",
        "skip_sidecar": True,
        "status": "review",
    }

    ok = wrapper._write_db_row(
        payload, target, "a prompt", [],
        job_id="job-mismatch", asset_id=b_id,  # MISMATCH
    )
    # Wrapper must REFUSE the mismatched write (return False).
    assert not ok, (
        "wrapper accepted a wrong --asset-id without complaint — F2 "
        "regression. Expected return False on filename mismatch."
    )

    # Verify B's row is untouched after the refused write.
    conn = srv.STATE.conn()
    rows = conn.execute(
        "SELECT id, filename, status, shot_id FROM assets ORDER BY id"
    ).fetchall()
    dicts = [dict(r) for r in rows]

    b_row = [r for r in dicts if r["id"] == b_id]
    assert b_row, f"B's row vanished — only have: {dicts}"
    assert b_row[0]["filename"] == "rt_draft_b.png", (
        f"HIGH-SEVERITY: B's row corrupted despite refusal. "
        f"b_row={b_row[0]}, all rows={dicts}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Attack 8 — Payload still contains asset_id (future drift)
# ═══════════════════════════════════════════════════════════════════════


def test_attack08_payload_with_asset_id_field_rejected_by_schema(temp_gallery):
    """If someone hand-crafts a payload JSON that includes asset_id (e.g.
    backward-compat artifact, agent mistake, partial refactor), the wrapper's
    strict schema should reject it.

    Note: the SERVER no longer injects asset_id into the payload (that's the
    PR's whole point), but the wrapper's defense-in-depth schema must still
    reject the field for hand-written payloads.
    """
    # Make a real payload file with asset_id leaking in
    payload = {
        "filename": "rt_leak.png",
        "client": "rt",
        "project": "rt",
        "workflow": "rt.leak",
        "gallery": str(temp_gallery),
        "prompt": "rt leak",
        "model": "nano_banana_2",
        "asset_id": 12345,  # the forbidden field
    }
    payload_path = temp_gallery / "leak_payload.json"
    payload_path.write_text(json.dumps(payload))

    # Invoke the wrapper via subprocess with --dry-run so it only validates
    wrapper_path = _REPO / "hf_gen_with_sidecar.py"
    result = subprocess.run(
        [sys.executable, str(wrapper_path), "--payload-file", str(payload_path), "--dry-run", "--quiet"],
        capture_output=True, text=True, timeout=15,
    )

    # Schema rejection should produce exit code 4 (EXIT_SCHEMA)
    assert result.returncode == 4, (
        f"Schema should reject asset_id field. exit={result.returncode}, "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Attack 11 — /tmp/draft_*.json cleanup edge cases
# ═══════════════════════════════════════════════════════════════════════


def test_attack11a_cleanup_skips_non_draft_prefix():
    """Cleanup must NOT touch payload files that don't match /tmp/draft_
    (e.g. hand-written payloads in /tmp/myfile.json, /var/tmp/draft_foo).

    Per the PR: `if pf and "/tmp/draft_" in pf:` — the substring check is
    intended to defensively scope to server-spawned temps only.

    Attack: pass a payload at /var/tmp/draft_x.json — does it survive?
    """
    # Create a payload at /var/tmp (NOT /tmp/) — should NOT be auto-deleted
    var_tmp_payload = Path("/var/tmp") / "draft_redteam_check.json"
    var_tmp_payload.write_text(json.dumps({
        "filename": "rt.png",
        "client": "rt",
        "project": "rt",
        "workflow": "rt.test",
        "gallery": "/tmp",
        "prompt": "rt",
    }))

    try:
        wrapper_path = _REPO / "hf_gen_with_sidecar.py"
        subprocess.run(
            [sys.executable, str(wrapper_path),
             "--payload-file", str(var_tmp_payload), "--dry-run", "--quiet"],
            capture_output=True, text=True, timeout=10,
        )
        # The file at /var/tmp/draft_*.json should NOT have been deleted
        # because the cleanup substring is '/tmp/draft_' (with the leading /).
        # /var/tmp/draft_* does NOT contain '/tmp/draft_' as a substring
        # because the slash before 'tmp' differs (it's /var/tmp/draft_ not /tmp/draft_).
        # Actually — /var/tmp/draft_X contains "tmp/draft_" as substring but NOT
        # "/tmp/draft_" with the leading slash. So this should survive.
        assert var_tmp_payload.exists(), (
            "/var/tmp/draft_*.json was unexpectedly deleted — cleanup matched too broadly"
        )
    finally:
        if var_tmp_payload.exists():
            var_tmp_payload.unlink()


def test_attack11b_cleanup_handles_missing_file():
    """Cleanup runs after wrapper exits. If something already removed the
    file (race / external cleanup / disk full unlink), os.unlink raises
    OSError which is caught. Verifies the bare except OSError works.

    Test by manipulating a payload path that gets pre-removed mid-wrapper.
    """
    # Build a payload at /tmp/draft_test_race.json
    pf = Path("/tmp/draft_redteam_race.json")
    pf.write_text(json.dumps({
        "filename": "rt.png",
        "client": "rt",
        "project": "rt",
        "workflow": "rt",
        "gallery": "/tmp",
        "prompt": "rt",
    }))

    # Run wrapper in dry-run, but delete the file mid-run by also unlinking
    # before subprocess.wait. Subprocess will read it first, validate, then
    # try to unlink at the end. We pre-empt by unlinking after launch.
    wrapper_path = _REPO / "hf_gen_with_sidecar.py"
    proc = subprocess.Popen(
        [sys.executable, str(wrapper_path), "--payload-file", str(pf), "--dry-run", "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Race: hopefully wrapper has already read the file. Delete it.
    time.sleep(0.5)
    if pf.exists():
        pf.unlink()
    rc = proc.wait(timeout=10)

    # Wrapper should still exit cleanly (the unlink-failed branch is caught)
    assert rc in (0, 4), f"Cleanup should not crash wrapper. rc={rc}"


def test_attack11c_cleanup_substring_bug_demo(temp_gallery):
    """SHARP EDGE: The cleanup check is `'/tmp/draft_' in pf`. This is a
    SUBSTRING match, not a prefix check. Pathological filenames like
    `/home/user/safe_file_/tmp/draft_x.json` (containing /tmp/draft_ in
    middle) would be deleted by the wrapper.

    Test: make such a path and run the wrapper on it. Does it survive?
    """
    # Make a directory in tmp_path with an embedded /tmp/draft_ substring
    weird_dir = temp_gallery / "subdir_with_slash"
    weird_dir.mkdir()
    # We can't actually put /tmp/draft_ in a filename, but we can create a
    # path like /tmp/draft_user/payload.json. That path passes the substring.
    deceptive_dir = Path(tempfile.mkdtemp(prefix="draft_redteam_dir_"))
    pf = deceptive_dir / "payload.json"
    pf.write_text(json.dumps({
        "filename": "rt.png",
        "client": "rt",
        "project": "rt",
        "workflow": "rt",
        "gallery": "/tmp",
        "prompt": "rt",
    }))

    try:
        wrapper_path = _REPO / "hf_gen_with_sidecar.py"
        rc = subprocess.run(
            [sys.executable, str(wrapper_path),
             "--payload-file", str(pf), "--dry-run", "--quiet"],
            capture_output=True, text=True, timeout=10,
        ).returncode

        # The cleanup check is '/tmp/draft_' in pf. The path
        # /tmp/draft_redteam_dir_XXX/payload.json CONTAINS '/tmp/draft_' as
        # substring → wrapper WILL delete it.
        # This is a real bug: any hand-written payload in a directory
        # matching /tmp/draft_*/ will be silently deleted.
        if not pf.exists():
            pytest.fail(
                f"BUG: substring match on '/tmp/draft_' deletes hand-written "
                f"payload at {pf}. The cleanup check should use a tighter "
                f"prefix match (e.g. os.path.dirname(pf) == '/tmp' AND "
                f"basename starts with 'draft_'). rc={rc}"
            )
    finally:
        # Best-effort cleanup
        if pf.exists():
            pf.unlink()
        try:
            deceptive_dir.rmdir()
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Attack 12 — Concurrent draft creates (race in id allocation)
# ═══════════════════════════════════════════════════════════════════════


def test_attack12_concurrent_draft_creates(temp_gallery):
    """Two POST /api/draft calls in rapid succession. SQLite serializes
    inserts so we should get two distinct ids; both rows should be clean."""
    import threading
    ids = []
    errors = []

    def _stage_one(name):
        try:
            ids.append(_stage_draft(filename=name))
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=_stage_one, args=("rt_concur_a.png",))
    t2 = threading.Thread(target=_stage_one, args=("rt_concur_b.png",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"Concurrent draft creates raised: {errors}"
    assert len(set(ids)) == 2, f"Expected 2 distinct ids; got {ids}"


# ═══════════════════════════════════════════════════════════════════════
# Attack 13 — Concurrent fires of two different drafts
# ═══════════════════════════════════════════════════════════════════════


def test_attack13_concurrent_fires_two_drafts(temp_gallery, fake_popen_factory):
    """Fire two different drafts concurrently. Both should register in the
    fires registry. Both should get distinct pids (mocked)."""
    a_id = _stage_draft(filename="rt_conc_fire_a.png")
    b_id = _stage_draft(filename="rt_conc_fire_b.png")

    ra = srv._fire_draft(a_id)
    rb = srv._fire_draft(b_id)

    assert ra.get("ok"), f"Fire A failed: {ra}"
    assert rb.get("ok"), f"Fire B failed: {rb}"

    fires = srv.STATE.list_fires(include_finished=True)
    asset_ids = {f.get("asset_id") for f in fires}
    assert a_id in asset_ids, f"A not in fires registry: {asset_ids}"
    assert b_id in asset_ids, f"B not in fires registry: {asset_ids}"

    # Both should be in 'firing' status now
    conn = srv.STATE.conn()
    rows = conn.execute(
        "SELECT id, status FROM assets WHERE id IN (?, ?)", (a_id, b_id)
    ).fetchall()
    statuses = {r["id"]: r["status"] for r in rows}
    assert statuses[a_id] == "firing"
    assert statuses[b_id] == "firing"


# ═══════════════════════════════════════════════════════════════════════
# Attack 14 — Race: wrapper writes row before _transition_fire_status runs
# ═══════════════════════════════════════════════════════════════════════


def test_attack14_wrapper_writes_before_transition(temp_gallery, fake_popen_factory):
    """Pre-mortem H1 risk. Sequence:
    1. Fire kicked off → status='firing'
    2. Wrapper runs upsert_asset_direct(asset_id=X) → row mutated to status='review'
    3. _transition_fire_status sees old 'firing' status check fails → no-op

    The current _transition_fire_status guards: if row.status != 'firing', return.
    So if wrapper already wrote 'review', the transition is a clean no-op.
    But what if wrapper wrote 'review' AND THEN _transition_fire_status fires
    after a delay and finds status='review' (not 'firing') — does it overwrite?

    Read the code: it only updates when status == 'firing'. So a wrapper-wins
    race is safe. We verify that.
    """
    asset_id = _stage_draft(filename="rt_race.png")
    srv._fire_draft(asset_id)  # status → firing

    # Wrapper "wins": directly flip to review via upsert_asset_direct
    sys.path.insert(0, str(_REPO))
    import hf_gen_with_sidecar as wrapper  # noqa: E402
    target = temp_gallery / "rt_race.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    payload = {
        "filename": "rt_race.png",
        "client": "rt_client",
        "project": "rt_project",
        "shot_id": "SH_RACE",
        "model": "nano_banana_2",
        "workflow": "rt.race",
        "gallery": str(temp_gallery),
        "prompt": "rt race",
        "skip_sidecar": True,
        "status": "review",
    }
    wrapper._write_db_row(payload, target, "rt race", [], job_id="j-race", asset_id=asset_id)

    # Now simulate _transition_fire_status firing AFTER wrapper success
    # with a stale exit_code (say 0 for success — but row already at review)
    srv._transition_fire_status(asset_id, 0)

    conn = srv.STATE.conn()
    row = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
    assert row["status"] == "review", (
        f"Race-after wrapper-win: status should stay 'review' (transition is no-op); "
        f"got {row['status']}"
    )

    # Now simulate _transition_fire_status with exit_code != 0 firing AFTER
    # wrapper success — this is the dangerous case: wrapper succeeded but
    # the proc-poll fires with bad exit code (e.g. SIGPIPE on log close).
    # The guard `if row.status != 'firing' return` should prevent corruption.
    srv._transition_fire_status(asset_id, 1)
    row = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
    assert row["status"] == "review", (
        f"DANGER: transition with bad exit_code overwrote wrapper's 'review' row! "
        f"Status flipped to: {row['status']}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Attack 16 — Card pulse verification (UI-side — skipped for now)
# ═══════════════════════════════════════════════════════════════════════
# This requires a running browser; the e2e test file covers some of this.
# Documenting as untested in the report.


# ═══════════════════════════════════════════════════════════════════════
# Attack 17 — Payload with empty prompt
# ═══════════════════════════════════════════════════════════════════════


def test_attack17_empty_prompt_rejected(temp_gallery, fake_popen_factory):
    """_fire_draft with a draft that has an empty prompt. _fire_draft
    explicitly checks `if not payload.get('prompt')` → returns error."""
    payload = {
        "filename": "rt_empty.png",
        "client": "rt",
        "project": "rt",
        "shot_id": "SH",
        "model": "nano_banana_2",
        "workflow": "rt",
        "payload": {"model": "nano_banana_2", "prompt": "", "image": []},
    }
    result = srv._create_draft(payload)
    # Draft create might succeed even with empty prompt depending on validation
    if not result.get("ok"):
        return  # Draft create rejected; fine
    asset_id = result["asset"]["id"]

    fr = srv._fire_draft(asset_id)
    assert not fr.get("ok"), f"_fire_draft with empty prompt should reject: {fr}"
    assert "no prompt" in (fr.get("error") or "").lower()


# ═══════════════════════════════════════════════════════════════════════
# Attack 18 — Filename with path traversal / special chars
# ═══════════════════════════════════════════════════════════════════════


def test_attack18_filename_path_traversal_rejected(temp_gallery, fake_popen_factory):
    """Schema regex on filename: ^[A-Za-z0-9_][A-Za-z0-9._-]*\\.<ext>$.
    Tests path traversal attempts get rejected at the wrapper schema layer."""
    # Try staging a draft with a path-traversal filename
    bad_filename = "../../etc/passwd"
    payload = {
        "filename": bad_filename,
        "client": "rt",
        "project": "rt",
        "shot_id": "SH",
        "model": "nano_banana_2",
        "workflow": "rt",
        "payload": {"model": "nano_banana_2", "prompt": "rt", "image": []},
    }
    result = srv._create_draft(payload)
    if result.get("ok"):
        # Some servers might accept it; check that fire rejects via schema
        asset_id = result["asset"]["id"]
        # Inspect what was stored
        conn = srv.STATE.conn()
        row = conn.execute("SELECT filename FROM assets WHERE id=?", (asset_id,)).fetchone()
        stored = row["filename"]
        # Acceptable: either the create rejected it OR the filename was sanitized
        # away from a traversal pattern
        assert "../" not in stored, (
            f"Path traversal stored as-is: filename={stored!r}. "
            f"Wrapper schema would catch this but only if fire is attempted."
        )


# ═══════════════════════════════════════════════════════════════════════
# Attack 19 (partial) — Wrapper hangs / no completion
# ═══════════════════════════════════════════════════════════════════════


def test_attack19_zombie_reap_demotes_stuck_firing(temp_gallery):
    """Stuck 'firing' rows older than stuck_seconds with no live wrapper
    should be reaped back to 'draft' by reap_zombie_firings."""
    asset_id = _stage_draft(filename="rt_zombie.png")

    # Manually flip to firing with an old last_updated_at
    conn = srv.STATE.conn()
    old_time = int(time.time()) - 3600  # 1 hour ago
    conn.execute(
        "UPDATE assets SET status='firing', last_updated_at=? WHERE id=?",
        (old_time, asset_id),
    )
    conn.commit()

    # Reap with a 30-min threshold
    reaped = srv.STATE.reap_zombie_firings(stuck_seconds=1800)
    assert reaped >= 1, f"Expected to reap 1 zombie; got {reaped}"

    row = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
    assert row["status"] == "draft", f"Zombie should be back to draft; got {row['status']}"


# ═══════════════════════════════════════════════════════════════════════
# Attack 20 — fire_watch.py: check_fail_rate is broken
# ═══════════════════════════════════════════════════════════════════════


def test_attack20_fire_watch_fail_rate_query_broken():
    """fire_watch.py:check_fail_rate queries status IN ('review','failed'),
    but _transition_fire_status sets failed fires back to status='draft',
    not 'failed'. So the fail-rate calculation will ALWAYS be 0% even
    during a 100% failure rate. This is a documented bug, not a PR
    regression — but worth surfacing."""
    fire_watch_path = Path("/Users/ayo/Library/CloudStorage/GoogleDrive-ayothomas@trajectoryvisual.com/Shared drives/Trajectory/Projects/AI visual chef/arsenal/00-utilities/fire_watch.py")
    if not fire_watch_path.exists():
        pytest.skip("fire_watch.py not located at canonical path")
    content = fire_watch_path.read_text()
    # Check that 'failed' status is referenced in the SQL query
    assert "'failed'" in content, "fire_watch SQL doesn't reference 'failed' anymore?"
    # And confirm _transition_fire_status does NOT write 'failed' anywhere
    srv_text = (Path(_REPO) / "vc_gallery_serve.py").read_text()
    transition_block = srv_text[srv_text.find("def _transition_fire_status"):]
    transition_block = transition_block[:transition_block.find("def _fire_draft")]
    if "'failed'" in transition_block or '"failed"' in transition_block:
        return  # Now writes 'failed' — query works
    pytest.fail(
        "fire_watch.py:check_fail_rate queries for status='failed' but "
        "_transition_fire_status never writes that status (writes 'draft' on "
        "failure). The fail-rate alert will always report 0%, masking real "
        "failure storms. Fix: either fire_watch should also count failed "
        "→ draft transitions (audit log), or _transition_fire_status should "
        "write a distinct 'failed' status."
    )
