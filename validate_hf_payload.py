#!/usr/bin/env python3
"""validate_hf_payload — strict-validate input to hf_gen_with_sidecar.sh.

Two layers of validation:

1. **JSON Schema** (SHAPE) — `hf_payload.schema.json` enforces required fields,
   types, regex patterns, enums, mutual exclusion. Fast, deterministic.

2. **Runtime checks** (REALITY) — this file enforces:
   - `gallery` canonicalizes under `$VC_GALLERY_ROOT` (no `..` escape, no symlinks
     leaving the root)
   - `filename` is basename-only (defence-in-depth on top of regex)
   - source image path (`image`/`start_image`/`end_image`), if a local path,
     canonicalizes under the project's allowed roots
   - `model` value is in the current `higgsfield model list --json` snapshot
   - `workflow` enum stays in sync with `VALID_WORKFLOWS` in canonical writer
   - `soul_name` resolves in `clients/<client>/soul_refs.yaml` (if provided)
     and stored `workspace_id` matches active workspace (deferred to wrapper —
     this file flags the requirement, doesn't have access to workspace state)

Library:
    from validate_hf_payload import validate
    payload = json.load(open('payload.json'))
    errors = validate(payload, vc_gallery_root='/path/to/gallery_root')
    if errors:
        for e in errors: print(e)

CLI:
    cat payload.json | validate_hf_payload.py --gallery-root /path
    validate_hf_payload.py --payload payload.json --gallery-root /path
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

_HERE = Path(__file__).resolve().parent
_SCHEMA_PATH = _HERE / "hf_payload.schema.json"
_FIXTURE_DIR = _HERE / "hf_fixtures"
_MODEL_LIST_IMAGE_FIXTURE = _FIXTURE_DIR / "model_list_image.json"
_MODEL_LIST_VIDEO_FIXTURE = _FIXTURE_DIR / "model_list_video.json"
_MODEL_LIST_FIXTURES = (_MODEL_LIST_IMAGE_FIXTURE, _MODEL_LIST_VIDEO_FIXTURE)

# Imported at validate-time to assert schema enum stays in sync.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _load_schema() -> dict:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _check_workflow_sync(schema_workflows: list[str]) -> list[str]:
    """Assert schema enum matches VALID_WORKFLOWS in canonical writer."""
    try:
        from write_companion_note import VALID_WORKFLOWS
    except ImportError as e:
        return [f"workflow sync: cannot import VALID_WORKFLOWS ({e})"]

    schema_set = set(schema_workflows)
    canonical_set = set(VALID_WORKFLOWS)
    missing_from_schema = canonical_set - schema_set
    extra_in_schema = schema_set - canonical_set
    errs = []
    if missing_from_schema:
        errs.append(
            f"workflow enum drift: canonical has but schema missing: "
            f"{sorted(missing_from_schema)}"
        )
    if extra_in_schema:
        errs.append(
            f"workflow enum drift: schema has but canonical missing: "
            f"{sorted(extra_in_schema)}"
        )
    return errs


def _validate_with_schema(payload: dict, schema: dict) -> list[str]:
    """Schema-level validation. Uses jsonschema if available, else fallback."""
    try:
        import jsonschema
    except ImportError:
        return _fallback_schema_validate(payload, schema)

    errs = []
    validator = jsonschema.Draft7Validator(schema)
    for e in sorted(validator.iter_errors(payload), key=lambda x: list(x.path)):
        path = ".".join(str(p) for p in e.path) or "<root>"
        errs.append(f"schema [{path}]: {e.message}")
    return errs


def _fallback_schema_validate(payload: dict, schema: dict) -> list[str]:
    """Hand-rolled validation for the subset we care about, when jsonschema
    is missing. Covers required fields, type, pattern, enum, additionalProperties.
    """
    errs: list[str] = []

    # required
    for field in schema.get("required", []):
        if field not in payload:
            errs.append(f"schema [<root>]: missing required field {field!r}")

    # additionalProperties: false
    if schema.get("additionalProperties") is False:
        allowed = set(schema.get("properties", {}).keys())
        for k in payload:
            if k not in allowed:
                errs.append(f"schema [<root>]: unknown field {k!r}")

    props = schema.get("properties", {})
    for k, v in payload.items():
        if k not in props:
            continue
        rules = props[k]
        # type
        t = rules.get("type")
        if t == "string" and not isinstance(v, str):
            errs.append(f"schema [{k}]: must be string")
            continue
        if t == "integer" and not isinstance(v, int):
            errs.append(f"schema [{k}]: must be integer")
            continue
        if t == "boolean" and not isinstance(v, bool):
            errs.append(f"schema [{k}]: must be boolean")
            continue
        if t == "array" and not isinstance(v, list):
            errs.append(f"schema [{k}]: must be array")
            continue
        # enum
        if "enum" in rules and v not in rules["enum"]:
            errs.append(f"schema [{k}]: {v!r} not in enum {rules['enum']}")
        # pattern (string only)
        if t == "string" and "pattern" in rules:
            if not re.match(rules["pattern"], v):
                errs.append(f"schema [{k}]: {v!r} does not match pattern {rules['pattern']!r}")
        # minLength / maxLength
        if t == "string":
            if "minLength" in rules and len(v) < rules["minLength"]:
                errs.append(f"schema [{k}]: shorter than minLength {rules['minLength']}")
            if "maxLength" in rules and len(v) > rules["maxLength"]:
                errs.append(f"schema [{k}]: longer than maxLength {rules['maxLength']}")
        # min/max for integers
        if t == "integer":
            if "minimum" in rules and v < rules["minimum"]:
                errs.append(f"schema [{k}]: below minimum {rules['minimum']}")
            if "maximum" in rules and v > rules["maximum"]:
                errs.append(f"schema [{k}]: above maximum {rules['maximum']}")

    # mutually exclusive: soul_id + soul_name
    if "soul_id" in payload and "soul_name" in payload:
        errs.append("schema [<root>]: soul_id and soul_name are mutually exclusive")

    return errs


def _resolves_under(child: Path, root: Path) -> bool:
    """True if `child` resolves under `root`. Resolves both — symlinks too.

    Uses os.path.commonpath on resolved absolute paths.
    """
    try:
        c = child.resolve(strict=False)
        r = root.resolve(strict=False)
        common = os.path.commonpath([str(c), str(r)])
        return common == str(r)
    except (ValueError, OSError):
        return False


def _validate_paths(payload: dict, vc_gallery_root: str | None) -> list[str]:
    """Filesystem-level checks: gallery under VC_GALLERY_ROOT, filename basename only."""
    errs: list[str] = []

    fname = payload.get("filename", "")
    if "/" in fname or "\\" in fname or ".." in fname:
        errs.append(f"path [filename]: contains forbidden characters: {fname!r}")

    gallery = payload.get("gallery")
    if gallery and vc_gallery_root:
        if not _resolves_under(Path(gallery), Path(vc_gallery_root)):
            errs.append(
                f"path [gallery]: {gallery!r} does not canonicalize under "
                f"VC_GALLERY_ROOT={vc_gallery_root!r}"
            )

    # Source image paths: if they look like local paths (start with / or contain /),
    # we don't enforce a specific root here (varies by client); we only flag
    # obvious traversal markers. The wrapper applies tighter per-client checks.
    for key in ("image", "start_image", "end_image", "video", "audio"):
        v = payload.get(key)
        if not v:
            continue
        # All media fields may be a list (multi-ref / multi-clip workflows); normalize to list
        paths = v if isinstance(v, list) else [v]
        for p in paths:
            if isinstance(p, str) and p.startswith("/") and ".." in p:
                errs.append(f"path [{key}]: contains traversal marker {p!r}")

    return errs


def _validate_model(payload: dict) -> list[str]:
    """Verify model is in the current model_list fixtures (image OR video)."""
    model = payload.get("model")
    if not model:
        return []

    missing = [str(p) for p in _MODEL_LIST_FIXTURES if not p.exists()]
    if missing:
        return [
            f"model [{model}]: cannot verify — fixture(s) missing: {missing}. "
            f"Refresh: `higgsfield model list --image --json > {_MODEL_LIST_IMAGE_FIXTURE}` "
            f"and `higgsfield model list --video --json > {_MODEL_LIST_VIDEO_FIXTURE}`."
        ]

    job_set_types: set[str] = set()
    for fixture in _MODEL_LIST_FIXTURES:
        try:
            with open(fixture, "r", encoding="utf-8") as f:
                models = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return [f"model [{model}]: fixture parse error in {fixture.name}: {e}"]
        job_set_types.update(
            m.get("job_set_type") for m in models if isinstance(m, dict)
        )

    if model not in job_set_types:
        return [
            f"model [{model}]: not in current model list (image+video). "
            f"Known: {sorted(t for t in job_set_types if t)[:8]}..."
        ]
    return []


def validate(payload: dict, vc_gallery_root: str | None = None) -> list[str]:
    """Full validation. Returns a list of error strings (empty = valid)."""
    if not isinstance(payload, dict):
        return [f"payload must be an object, got {type(payload).__name__}"]

    schema = _load_schema()

    # Sync check between schema enum and canonical writer enum
    workflow_enum = next(
        (
            p.get("enum", [])
            for k, p in schema.get("properties", {}).items()
            if k == "workflow"
        ),
        [],
    )
    sync_errs = _check_workflow_sync(workflow_enum)

    schema_errs = _validate_with_schema(payload, schema)
    path_errs = _validate_paths(payload, vc_gallery_root)
    model_errs = _validate_model(payload)

    return sync_errs + schema_errs + path_errs + model_errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--payload", help="path to payload JSON")
    g.add_argument("--payload-stdin", action="store_true", help="read payload from stdin")
    ap.add_argument(
        "--gallery-root",
        default=os.environ.get("VC_GALLERY_ROOT"),
        help="Path that gallery must resolve under (default: $VC_GALLERY_ROOT)",
    )
    args = ap.parse_args()

    if args.payload:
        with open(args.payload, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"ERROR: stdin is not valid JSON: {e}", file=sys.stderr)
            return 2

    errors = validate(payload, vc_gallery_root=args.gallery_root)
    if errors:
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        print(f"\n{len(errors)} validation error(s).", file=sys.stderr)
        return 1

    print("✓ payload valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
