"""
VC Gallery — draft.fire regression tests (Issues #27, #28, #52).

In-process tests that import the server module and a temp gallery folder,
then exercise the draft → fire path with subprocess.Popen mocked. Verifies:

- #27: A draft → fire transition mutates ONE row in place (no duplicate row
  at the .drafts/ path + the gallery path).
- #28: The fire registry registers the live fire so /api/fires reports it.
- #52: asset_id travels via `--asset-id` CLI flag, NOT inside the payload
  JSON written to the temp file. The wrapper's strict schema would reject
  payload-level asset_id; this test pins the contract.

Run: python3 -m pytest tests/test_draft_fire_regression.py -v
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Make the repo root importable so `import vc_gallery_serve` works regardless
# of how pytest is invoked.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import vc_gallery_serve as srv  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def temp_gallery(tmp_path):
    """Spin up a fresh gallery folder with a clean DB, point STATE at it,
    and tear it down after the test."""
    folder = tmp_path / "gallery"
    folder.mkdir()
    # set_folder runs the rescanner — empty folder is fine
    srv.STATE.set_folder(folder, run_scan=False)
    yield folder
    # Best-effort: close the connection so other tests don't inherit state
    try:
        if srv.STATE._conn is not None:
            srv.STATE._conn.close()
            srv.STATE._conn = None
    except Exception:
        pass
    srv.STATE.folder = None


@pytest.fixture
def fake_popen():
    """Patch subprocess.Popen inside vc_gallery_serve so _fire_draft doesn't
    actually spawn the wrapper. Capture the constructed argv for inspection."""
    captured = {"argv": None, "kwargs": None}

    def _capture(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        proc = MagicMock()
        proc.pid = 99999
        # Make poll() return 0 (clean exit) so the watcher doesn't loop
        proc.poll.return_value = 0
        return proc

    with patch.object(srv.subprocess, "Popen", side_effect=_capture):
        yield captured


# ─── Tests ───────────────────────────────────────────────────────────


def _stage_draft(filename="test_fire.png", model="nano_banana_2") -> int:
    """Stage a draft via the server's _create_draft helper. Returns asset_id."""
    payload = {
        "filename": filename,
        "client": "test_client",
        "project": "test_project",
        "shot_id": "SH999",
        "model": model,
        "workflow": "draft.fire.test",
        "payload": {
            "model": model,
            "prompt": "test prompt for #52 regression",
            "image": [],
        },
    }
    result = srv._create_draft(payload)
    assert result.get("ok"), f"_create_draft failed: {result}"
    return result["asset"]["id"]


def test_fire_passes_asset_id_via_cli_flag(temp_gallery, fake_popen):
    """Issue #52: --asset-id <id> must appear in the wrapper argv."""
    asset_id = _stage_draft()
    result = srv._fire_draft(asset_id)
    assert result.get("ok"), f"_fire_draft failed: {result}"

    argv = fake_popen["argv"]
    assert argv is not None, "subprocess.Popen was never called"
    assert "--asset-id" in argv, f"--asset-id missing from argv: {argv}"
    # Verify it's followed by the right id
    idx = argv.index("--asset-id")
    assert argv[idx + 1] == str(asset_id), (
        f"--asset-id has wrong value: got {argv[idx + 1]!r}, "
        f"expected {asset_id!r}"
    )


def test_fire_does_not_inject_asset_id_into_payload(temp_gallery, fake_popen):
    """Issue #52: the JSON written to --payload-file MUST NOT contain asset_id.

    This is the schema-strictness regression: the wrapper's hf_payload.schema.json
    has additionalProperties:false. If asset_id ever leaks back into the payload,
    every fire would fail validation with EXIT_SCHEMA before spending a dollar.
    """
    asset_id = _stage_draft()
    result = srv._fire_draft(asset_id)
    assert result.get("ok"), f"_fire_draft failed: {result}"

    argv = fake_popen["argv"]
    # --payload-file <path> is in argv
    assert "--payload-file" in argv
    pf_path = argv[argv.index("--payload-file") + 1]
    assert os.path.exists(pf_path), f"payload file missing: {pf_path}"

    with open(pf_path, "r", encoding="utf-8") as f:
        on_disk_payload = json.load(f)

    assert "asset_id" not in on_disk_payload, (
        "asset_id leaked into payload JSON — schema would reject this. "
        f"keys present: {sorted(on_disk_payload.keys())}"
    )


def test_fire_registers_live_fire(temp_gallery, fake_popen):
    """Issue #28: live fire shows up in STATE._fires after _fire_draft."""
    asset_id = _stage_draft()
    srv._fire_draft(asset_id)

    # STATE.register_fire was called with pid=99999 (our MagicMock)
    fires = srv.STATE.list_fires(include_finished=True)
    pids = [f.get("pid") for f in fires]
    assert 99999 in pids, f"fire not registered, current fires: {pids}"


def test_draft_fire_single_row_after_wrapper_completes(temp_gallery, fake_popen):
    """Issue #27 + #52 combined: after the wrapper would have completed (we
    simulate by directly invoking the wrapper's _write_db_row with asset_id),
    there is exactly ONE row at the final filename — no duplicate at the
    .drafts/ path."""
    asset_id = _stage_draft(filename="test_single_row.png")

    # Pre-condition: exactly one row (the draft), status=draft
    conn = srv.STATE.conn()
    rows = conn.execute("SELECT id, status, filename FROM assets").fetchall()
    assert len(rows) == 1, f"pre-fire should have 1 draft row, got {len(rows)}"
    assert rows[0]["status"] == "draft"

    # Simulate the wrapper's post-fire DB write with the new asset_id plumbing
    # by calling _write_db_row directly with asset_id set.
    sys.path.insert(0, str(_REPO))
    import hf_gen_with_sidecar as wrapper  # noqa: E402

    target = temp_gallery / "test_single_row.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)  # minimal stub

    fake_payload = {
        "filename": "test_single_row.png",
        "client": "test_client",
        "project": "test_project",
        "shot_id": "SH999",
        "model": "nano_banana_2",
        "workflow": "draft.fire.test",
        "gallery": str(temp_gallery),
        "prompt": "test",
        "skip_sidecar": True,
        "status": "review",
    }
    ok = wrapper._write_db_row(
        fake_payload, target, "test prompt", [],
        job_id="job-test-123", asset_id=asset_id,
    )
    assert ok, "wrapper._write_db_row returned False"

    # Post-condition: still exactly one row. Status flipped to review, the
    # filename now points at the real gallery path, shot_id preserved.
    rows = conn.execute(
        "SELECT id, status, filename, shot_id FROM assets ORDER BY id"
    ).fetchall()
    assert len(rows) == 1, (
        f"Issue #27 regression: expected 1 row after fire, got {len(rows)}. "
        f"Rows: {[dict(r) for r in rows]}"
    )
    assert rows[0]["id"] == asset_id
    assert rows[0]["status"] == "review"
    assert rows[0]["filename"] == "test_single_row.png"
    assert rows[0]["shot_id"] == "SH999"
