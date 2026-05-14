#!/usr/bin/env python3
"""vc_gallery_test — end-to-end tests for the gallery server.

Runs every API endpoint with realistic payloads and a known-good gallery.
NO HF FIRES — drafts are staged and immediately deleted; never fired.

Exit codes:
    0 — all tests pass
    1 — one or more tests failed (see stdout for which)

Usage:
    # against the default localhost:8770
    python3 vc_gallery_test.py

    # against a different server
    python3 vc_gallery_test.py --host http://localhost:8771
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class Tester:
    def __init__(self, host: str) -> None:
        self.host = host.rstrip("/")
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def req(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        url = f"{self.host}{path}"
        data: bytes | None = None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return resp.status, payload
            return resp.status, payload  # binary or unknown — leave as bytes
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()
                return e.code, json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                return e.code, b""
        except urllib.error.URLError as e:
            return 0, {"error": str(e)}

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed.append(name)
            print(f"  ✓ {name}")
        else:
            self.failed.append((name, detail))
            print(f"  ✗ {name} — {detail}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'─' * 60}")
        print(f"Total: {total}  ·  Passed: {len(self.passed)}  ·  Failed: {len(self.failed)}")
        if self.failed:
            print("\nFAILURES:")
            for name, detail in self.failed:
                print(f"  ✗ {name}: {detail}")
            return 1
        return 0


def test_basic(t: Tester) -> None:
    print("\n[1/6] Basic endpoints")
    status, data = t.req("GET", "/healthz")
    t.check("GET /healthz returns 200", status == 200)
    t.check("/healthz reports ok:true", isinstance(data, dict) and data.get("ok") is True)
    t.check("/healthz has current_folder", isinstance(data, dict) and data.get("current_folder"))

    status, data = t.req("GET", "/api/folder")
    t.check("GET /api/folder returns 200", status == 200)
    t.check("/api/folder has asset_count", isinstance(data, dict) and "asset_count" in data)


def test_assets(t: Tester) -> None:
    print("\n[2/6] Assets")
    status, data = t.req("GET", "/api/assets?limit=5")
    t.check("GET /api/assets returns 200", status == 200)
    items = data.get("items", []) if isinstance(data, dict) else []
    t.check("/api/assets returns items", len(items) > 0, f"got {len(items)}")
    if items:
        a = items[0]
        t.check("asset row has hf_url field", "hf_url" in a)
        first_id = a["id"]
        status, single = t.req("GET", f"/api/assets/{first_id}")
        t.check("GET /api/assets/<id> returns 200", status == 200)
        t.check("single asset has refs_resolved", "refs_resolved" in single)
        t.check("single asset has prompt", "prompt" in single)
        t.check("single asset has review_history", "review_history" in single)


def test_health_and_debug(t: Tester) -> None:
    print("\n[3/6] Health + debug endpoints")
    status, data = t.req("GET", "/api/health")
    t.check("GET /api/health returns 200", status == 200)
    t.check("/api/health has ok", isinstance(data, dict) and "ok" in data)
    t.check("/api/health has db_writable", isinstance(data, dict) and "db_writable" in data)
    t.check("/api/health has zero_byte_files", isinstance(data, dict) and "zero_byte_files" in data)
    t.check("0-byte count is 0 (post-cleanup)", isinstance(data, dict) and data.get("zero_byte_files") == 0,
            f"got {data.get('zero_byte_files') if isinstance(data, dict) else '?'}")

    status, data = t.req("GET", "/api/debug/orphans")
    t.check("GET /api/debug/orphans returns 200", status == 200)
    t.check("orphans has zero_byte_rows", isinstance(data, dict) and "zero_byte_rows" in data)
    t.check("orphans zero_byte_rows is empty after cleanup",
            isinstance(data, dict) and len(data.get("zero_byte_rows", [])) == 0,
            f"got {len(data.get('zero_byte_rows', [])) if isinstance(data, dict) else '?'}")

    status, data = t.req("GET", "/api/debug/recent-events?n=5")
    t.check("GET /api/debug/recent-events returns 200", status == 200)
    t.check("events is a list", isinstance(data, list))


def test_drafts(t: Tester) -> None:
    print("\n[4/6] Drafts (MVP)")
    payload = {
        "filename": f"VC_TEST_DRAFT_{int(time.time())}.mp4",
        "client": "test_client",
        "project": "test_project",
        "shot_id": "SHTEST",
        "model": "kling3_0",
        "workflow": "test.draft",
        "estimated_cost": 24,
        "payload": {
            "model": "kling3_0",
            "mode": "pro",
            "duration": 5,
            "aspect_ratio": "16:9",
            "prompt": "TEST DRAFT — do not fire.",
            "image": ["/tmp/test_ref_1.png", "/tmp/test_ref_2.png"]
        }
    }
    status, data = t.req("POST", "/api/draft", payload)
    t.check("POST /api/draft returns 200", status == 200,
            f"got {status} {data}")
    asset = (data.get("asset") if isinstance(data, dict) else {}) or {}
    draft_id = asset.get("id")
    t.check("draft has id", isinstance(draft_id, int))
    t.check("draft.status == 'draft'", asset.get("status") == "draft")
    t.check("draft.media_type == 'video'", asset.get("media_type") == "video")
    t.check("draft.prompt is set", bool(asset.get("prompt")))
    t.check("draft.refs has 2 entries", len(asset.get("refs", [])) == 2)
    t.check("draft.draft.payload preserved", isinstance(asset.get("draft", {}).get("payload"), dict))

    # List drafts — should include ours
    status, data = t.req("GET", "/api/drafts")
    t.check("GET /api/drafts returns 200", status == 200)
    ids = [a["id"] for a in (data.get("items") if isinstance(data, dict) else [])]
    t.check("drafts list includes our draft", draft_id in ids)

    # Edit draft — change prompt
    if draft_id:
        status, data = t.req("PUT", f"/api/draft/{draft_id}", {
            "payload": {**payload["payload"], "prompt": "EDITED TEST DRAFT"},
            "image_refs": ["/tmp/test_ref_1.png"],  # narrow refs
        })
        t.check("PUT /api/draft/<id> returns 200", status == 200,
                f"got {status} {data}")
        new_asset = (data.get("asset") if isinstance(data, dict) else {}) or {}
        t.check("draft prompt updated", new_asset.get("prompt") == "EDITED TEST DRAFT")
        t.check("draft refs narrowed to 1", len(new_asset.get("refs", [])) == 1)

    # Delete draft
    if draft_id:
        status, data = t.req("DELETE", f"/api/draft/{draft_id}")
        t.check("DELETE /api/draft/<id> returns 200", status == 200)
        # Confirm it's gone
        status, data = t.req("GET", f"/api/assets/{draft_id}")
        t.check("deleted draft returns 404", status == 404, f"got {status}")


def test_rename(t: Tester) -> None:
    print("\n[5/6] Rename roundtrip")
    # Find a real asset with hf_url
    status, data = t.req("GET", "/api/assets?limit=20")
    items = data.get("items", []) if isinstance(data, dict) else []
    target = next((a for a in items if a.get("hf_url")), None)
    if not target:
        target = items[0] if items else None
    if not target:
        t.check("rename roundtrip", False, "no asset to test with")
        return
    aid = target["id"]
    orig_name = target["filename"]
    new_name = f"ZZZ_TEST_RENAME_{int(time.time())}.{orig_name.rsplit('.', 1)[-1]}"

    # Rename without moving file (DB-only test — safer)
    status, data = t.req("POST", "/api/rename", {
        "asset_id": aid, "new_filename": new_name, "move_file": False,
    })
    t.check("POST /api/rename returns 200", status == 200, f"got {status}: {data}")
    if isinstance(data, dict) and data.get("ok"):
        renamed = data["asset"]
        t.check("filename updated", renamed["filename"] == new_name)
        t.check("file_path updated", new_name in renamed["file_path"])
        t.check("status preserved", renamed["status"] == target["status"])

    # Revert
    status, data = t.req("POST", "/api/rename", {
        "asset_id": aid, "new_filename": orig_name, "move_file": False,
    })
    t.check("revert rename succeeds", status == 200 and data.get("ok") is True if isinstance(data, dict) else False)


def test_ref_serving(t: Tester) -> None:
    print("\n[6/6] Ref file serving")
    # Pick an asset whose file_path actually exists on disk (DB can lag rename/delete)
    status, data = t.req("GET", "/api/assets?limit=50")
    items = data.get("items", []) if isinstance(data, dict) else []
    from pathlib import Path
    real = next((a for a in items if a.get("file_path") and Path(a["file_path"]).exists()), None)
    if not real:
        t.check("ref serving", False, "no on-disk asset found in first 50")
        return
    file_path = real["file_path"]
    from urllib.parse import quote
    status, _ = t.req("GET", f"/ref?path={quote(file_path)}")
    t.check("GET /ref?path=<existing> returns 200", status == 200, f"got {status} for {file_path}")

    # /ref with a forbidden path → 403
    status, _ = t.req("GET", "/ref?path=/etc/passwd")
    t.check("GET /ref?path=/etc/passwd is blocked", status in (403, 404), f"got {status}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="http://localhost:8770", help="Server base URL")
    args = ap.parse_args(argv)

    t = Tester(args.host)
    print(f"vc-gallery test harness · against {args.host}")
    print("=" * 60)

    test_basic(t)
    test_assets(t)
    test_health_and_debug(t)
    test_drafts(t)
    test_rename(t)
    test_ref_serving(t)
    test_fires(t)
    test_open_in_finder_feedback(t)

    return t.summary()


def test_fires(t: Tester) -> None:
    print("\n[7/8] Fires endpoint")
    status, data = t.req("GET", "/api/fires")
    t.check("GET /api/fires returns 200", status == 200)
    t.check("/api/fires has fires list", isinstance(data, dict) and isinstance(data.get("fires"), list))
    # No real fires expected unless one was just triggered — just ensure shape is sane
    fires = data.get("fires", []) if isinstance(data, dict) else []
    for f in fires:
        t.check(f"fire row has pid+state", "pid" in f and "state" in f)
    # /api/fires/<bogus>/log should 404
    status, _ = t.req("GET", "/api/fires/99999999/log")
    t.check("GET /api/fires/<bogus>/log returns 404", status == 404, f"got {status}")


def test_open_in_finder_feedback(t: Tester) -> None:
    print("\n[8/8] Open-in-Finder feedback")
    # Pick an asset that does NOT exist on disk — must get clear error, not silent 200
    status, data = t.req("GET", "/api/assets?limit=200")
    items = data.get("items", []) if isinstance(data, dict) else []
    from pathlib import Path
    stale = next((a for a in items if a.get("file_path") and not Path(a["file_path"]).exists()), None)
    if stale is None:
        print("  (skipped — no stale-path asset found to test feedback)")
        return
    status, body = t.req("POST", f"/api/assets/{stale['id']}/open")
    t.check("stale-path open returns non-200", status != 200, f"got {status}")
    t.check("response carries human error", isinstance(body, dict) and bool(body.get("error")),
            f"body={body}")


if __name__ == "__main__":
    sys.exit(main())
