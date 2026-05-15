#!/usr/bin/env python3
"""hf_envelope_parse — versioned schema adapter for Higgsfield CLI responses.

The CLI's `--json` envelope shape varies by command:

- `generate create --wait --json` returns a TOP-LEVEL ARRAY of job objects:
    [
      {
        "id": "<uuid>",
        "status": "completed",
        "display_name": "Z Image",
        "job_set_type": "z_image",
        "result_url": "https://.../foo.png",
        "created_at": 1777973235.05,
        "params": {...}
      },
      ...
    ]

- Errors come back as a TOP-LEVEL OBJECT with `error_type`:
    {
      "billing_period": "monthly",
      "error_type": "not_enough_credits",
      "plan_type": "Team",
      ...
    }

- Other commands (`account status`, `model list`, `cost`) have command-specific
  shapes captured under `hf_fixtures/`.

This module exposes one function per command class. Each parser:
1. Tolerates both top-level array (success) and top-level object (error)
2. Extracts the normalized fields downstream code actually needs
3. Falls back gracefully (multiple field-name candidates) so minor schema
   drift in 0.x CLI doesn't immediately break everything
4. Records observed shape so callers can capture fresh fixtures on drift

Returned shape (success):
    {
      "success": True,
      "jobs": [
        {"id", "status", "result_url", "content_type", "model", "params", "created_at"},
        ...
      ],
      "command": "generate",
      "schema_version": "<cli_version>:<command>",
    }

Returned shape (failure):
    {
      "success": False,
      "error_type": "<error_type>" | "unknown",
      "error_message": "<best-guess message>",
      "error_data": {<full raw error envelope>},
    }

Library:
    from hf_envelope_parse import parse_generate_response
    result = parse_generate_response(raw_json)
    if result["success"]:
        for job in result["jobs"]:
            download(job["result_url"]) ...
    else:
        log_error(result)

CLI:
    cat raw_response.json | hf_envelope_parse.py --command generate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_FIXTURE_DIR = _HERE / "hf_fixtures"

# Filename extension lookup — used when CLI doesn't tell us content type
# but the URL has a recognizable suffix.
_EXT_TO_CONTENT_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".m4v": "video/mp4",
}


# ──────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────

def _is_error_envelope(obj: Any) -> bool:
    """Heuristic: top-level object with `error_type` field = error envelope."""
    return isinstance(obj, dict) and "error_type" in obj


def _extract_url_from_job(job: dict) -> str | None:
    """Try multiple known field names for the result URL.

    Defends against minor 0.x CLI schema drift. If they rename the field
    we'll hit the fixture-drift fallback.
    """
    candidates = [
        "result_url",
        "url",
        "output_url",
        "rawUrl",
        "raw_url",
    ]
    for k in candidates:
        v = job.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    # Sometimes URLs are nested under media[0].url
    media = job.get("media")
    if isinstance(media, list) and media and isinstance(media[0], dict):
        for k in ("url", "result_url"):
            v = media[0].get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
    return None


def _content_type_from_url(url: str) -> str | None:
    if not url:
        return None
    # Strip query string for extension detection
    bare = url.split("?", 1)[0].lower()
    for ext, ct in _EXT_TO_CONTENT_TYPE.items():
        if bare.endswith(ext):
            return ct
    return None


def _load_fixture(name: str) -> Any | None:
    """Load a fixture file by basename; returns None if missing."""
    p = _FIXTURE_DIR / name
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ──────────────────────────────────────────────────────────────────
# Public parsers — one per CLI command class
# ──────────────────────────────────────────────────────────────────

def parse_generate_response(
    raw: str | dict | list,
    *,
    cli_version: str = "0.1.28",
) -> dict:
    """Parse a `higgsfield generate ...` response. Handles 4 shapes:

    1. Submit-only (`create` without `--wait`): top-level array of job_id STRINGS
         ["uuid1", "uuid2"]
       → returns jobs[] with id only, no URL/status yet
    2. Create-and-wait (`create --wait`): top-level array of job OBJECTS
         [{id, status, result_url, ...}, ...]
    3. Wait-by-id (`wait <job_id>`): top-level SINGLE object
         {id, status, result_url, ...}
    4. Error: top-level object with `error_type`
    """
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return {
                "success": False,
                "error_type": "json_parse_error",
                "error_message": str(e),
                "error_data": {"raw": raw[:500]},
            }
    else:
        obj = raw

    # Shape 4: Error envelope (top-level object with error_type)
    if _is_error_envelope(obj):
        return {
            "success": False,
            "error_type": obj.get("error_type", "unknown"),
            "error_message": obj.get("message") or obj.get("error_type", "unknown error"),
            "error_data": obj,
        }

    # Shape 3: Wait response — single top-level object with id/status
    if isinstance(obj, dict) and ("id" in obj or "result_url" in obj or "status" in obj):
        return _shape_jobs_to_result([obj], cli_version, mode="wait")

    # Shape 1 + 2: Top-level array
    if isinstance(obj, list):
        if len(obj) == 0:
            return {
                "success": True,
                "jobs": [],
                "command": "generate",
                "schema_version": f"{cli_version}:generate",
                "drift_warnings": ["empty array response"],
                "mode": "empty",
            }

        # Shape 1: array of strings = submit-only (job_ids)
        if all(isinstance(item, str) for item in obj):
            return {
                "success": True,
                "jobs": [{"id": jid, "status": "submitted", "result_url": None,
                          "content_type": None, "model": None,
                          "display_name": None, "params": {}, "created_at": None}
                         for jid in obj],
                "command": "generate",
                "schema_version": f"{cli_version}:generate",
                "drift_warnings": [],
                "mode": "submit_only",
            }

        # Shape 2: array of objects = create+wait
        if all(isinstance(item, dict) for item in obj):
            return _shape_jobs_to_result(obj, cli_version, mode="create_wait")

        # Mixed array — drift
        return {
            "success": False,
            "error_type": "schema_drift",
            "error_message": (
                f"array contains mixed types: {set(type(x).__name__ for x in obj)}. "
                f"Capture fresh fixture: `higgsfield generate ... --json > "
                f"{_FIXTURE_DIR}/generate_<command>_<model>.json`"
            ),
            "error_data": {"raw": obj[:5]},
        }

    # Anything else — drift
    return {
        "success": False,
        "error_type": "schema_drift",
        "error_message": (
            f"expected top-level array or object, got {type(obj).__name__}. "
            f"Capture fresh fixture under {_FIXTURE_DIR}/"
        ),
        "error_data": {"raw": obj},
    }


def _shape_jobs_to_result(
    jobs_in: list[dict],
    cli_version: str,
    mode: str,
) -> dict:
    """Common shaping helper for shape 2 and shape 3."""
    jobs: list[dict] = []
    drift_warnings: list[str] = []

    for i, job in enumerate(jobs_in):
        if not isinstance(job, dict):
            drift_warnings.append(f"job[{i}] is not an object: {type(job).__name__}")
            continue

        url = _extract_url_from_job(job)
        if not url and job.get("status") == "completed":
            drift_warnings.append(
                f"job[{i}] (id={job.get('id', '?')}): completed but no URL via known fields. "
                f"Available keys: {sorted(job.keys())}"
            )

        ct = _content_type_from_url(url) if url else None

        jobs.append({
            "id": job.get("id"),
            "status": job.get("status", "unknown"),
            "result_url": url,
            "content_type": ct,
            "model": job.get("job_set_type") or job.get("model"),
            "display_name": job.get("display_name"),
            "params": job.get("params") or {},
            "created_at": job.get("created_at"),
        })

    return {
        "success": True,
        "jobs": jobs,
        "command": "generate",
        "schema_version": f"{cli_version}:generate",
        "drift_warnings": drift_warnings,
        "mode": mode,
    }


def parse_account_status(raw: str | dict) -> dict:
    """Parse `higgsfield account status --json`.

    Known fields (v0.1.28): email, credits, subscription_plan_type.
    No account_id field (use email + workspace_id for scoping).
    """
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"success": False, "error_message": str(e)}
    else:
        obj = raw

    if _is_error_envelope(obj):
        return {"success": False, "error_type": obj.get("error_type"), "error_data": obj}

    if not isinstance(obj, dict):
        return {"success": False, "error_message": f"expected object, got {type(obj).__name__}"}

    return {
        "success": True,
        "email": obj.get("email"),
        "credits": obj.get("credits"),
        "subscription_plan_type": obj.get("subscription_plan_type"),
    }


def parse_workspace_status(raw: str | dict) -> dict:
    """Parse `higgsfield workspace status --json`.

    Gotcha: when no workspace selected, CLI returns plain text "No workspace
    selected." NOT JSON, even with --json. Caller must check is_set field.
    """
    if isinstance(raw, str):
        # Detect plain-text "no workspace" response
        if raw.strip().startswith("No workspace"):
            return {"success": True, "is_set": False, "workspace": None}
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # Fall back to "no workspace" interpretation if it's clearly text
            return {"success": True, "is_set": False, "workspace": None}
    else:
        obj = raw

    if not isinstance(obj, dict):
        return {"success": True, "is_set": False, "workspace": None}

    return {
        "success": True,
        "is_set": obj.get("is_selected", True),
        "workspace": {
            "id": obj.get("id"),
            "name": obj.get("name"),
            "plan_type": obj.get("plan_type"),
            "credits": obj.get("credits"),
            "user_role": obj.get("user_role"),
        },
    }


def parse_workspace_list(raw: str | list) -> dict:
    """Parse `higgsfield workspace list --json`. Returns array of workspaces."""
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"success": False, "error_message": str(e)}
    else:
        obj = raw

    if _is_error_envelope(obj):
        return {"success": False, "error_type": obj.get("error_type"), "error_data": obj}

    if not isinstance(obj, list):
        return {"success": False, "error_message": "expected array"}

    return {
        "success": True,
        "workspaces": [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "plan_type": w.get("plan_type"),
                "credits": w.get("credits"),
                "is_selected": w.get("is_selected", False),
                "user_role": w.get("user_role"),
            }
            for w in obj if isinstance(w, dict)
        ],
    }


def parse_model_list(raw: str | list) -> dict:
    """Parse `higgsfield model list --json`. Returns {display_name → job_set_type}."""
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"success": False, "error_message": str(e)}
    else:
        obj = raw

    if not isinstance(obj, list):
        return {"success": False, "error_message": "expected array"}

    return {
        "success": True,
        "models": [
            {
                "display_name": m.get("display_name"),
                "job_set_type": m.get("job_set_type"),
                "type": m.get("type"),  # "image" | "video"
            }
            for m in obj if isinstance(m, dict)
        ],
    }


def parse_cost_response(raw: str | dict) -> dict:
    """Parse `higgsfield generate cost --json` → {credits: int}."""
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"success": False, "error_message": str(e)}
    else:
        obj = raw

    if _is_error_envelope(obj):
        return {"success": False, "error_type": obj.get("error_type"), "error_data": obj}

    if not isinstance(obj, dict) or "credits" not in obj:
        return {"success": False, "error_message": "expected {credits: int}"}

    return {"success": True, "credits": obj["credits"]}


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────

_PARSERS = {
    "generate": parse_generate_response,
    "account": parse_account_status,
    "workspace_status": parse_workspace_status,
    "workspace_list": parse_workspace_list,
    "model_list": parse_model_list,
    "cost": parse_cost_response,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--command",
        required=True,
        choices=sorted(_PARSERS.keys()),
        help="Which CLI response shape to parse",
    )
    ap.add_argument(
        "--input",
        help="Path to raw JSON file (default: read stdin)",
    )
    args = ap.parse_args()

    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()

    parser = _PARSERS[args.command]
    result = parser(raw)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
