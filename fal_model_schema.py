#!/usr/bin/env python3
"""fal_model_schema — discover fal models + auto-derive MODEL_SCHEMAS entries.

Removes the hand-maintenance of the gallery's fal dropdown. Two subcommands,
both backed by fal's own public APIs (no CLI, no key needed for these reads):

  search <keywords>   list matching fal models (id · category · title)
                      → GET https://fal.ai/api/models?keywords=...
  gen <slug>          fetch the model's OpenAPI input schema and print a
                      ready-to-paste MODEL_SCHEMAS entry for visual_chef_gallery.html
                      → GET https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=...

Because the fal engine wrapper is generic (any fal-ai/* model routes to it and
forwards declared params), adding a model = `gen <slug>` then paste. No code.

Usage:
  python3 fal_model_schema.py search "kling o3 4k"
  python3 fal_model_schema.py gen fal-ai/kling-video/o3/4k/reference-to-video
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

API = "https://fal.ai/api/models"
OPENAPI = "https://fal.ai/api/openapi/queue/openapi.json"
# fal input fields the gallery handles itself (refs via the draft) or that the
# wrapper maps — never surface these as editable dropdown params.
SKIP = {"prompt", "multi_prompt", "elements", "image_urls",
        "start_image_url", "end_image_url", "image_url", "video_url"}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "vc-gallery"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def search(keywords: str) -> int:
    data = _get(f"{API}?keywords={urllib.parse.quote(keywords)}")
    items = data.get("items", [])
    print(f"{data.get('total', len(items))} results for '{keywords}':\n")
    for x in items:
        dep = " [DEPRECATED]" if x.get("deprecated") else ""
        print(f"  {x.get('id',''):52} {str(x.get('category','')):16} {str(x.get('title',''))[:34]}{dep}")
    print("\nNext: python3 fal_model_schema.py gen <id>")
    return 0


def _unwrap(spec: dict) -> dict:
    """Collapse fal's anyOf[type,null] optional wrapper to the real type spec."""
    if "anyOf" in spec:
        for s in spec["anyOf"]:
            if s.get("type") != "null":
                return s
    return spec


def _is_image(spec: dict) -> bool:
    s = _unwrap(spec)
    if s.get("_fal_ui_field") == "image" or (s.get("ui") or {}).get("field") == "image":
        return True
    items = s.get("items") or {}
    return items.get("_fal_ui_field") == "image" or (items.get("ui") or {}).get("field") == "image"


def _field(name: str, spec: dict) -> str | None:
    s = _unwrap(spec)
    enum = s.get("enum")
    typ = s.get("type")
    # numeric-string enum (fal's duration) → a number field with min/max
    if enum and all(str(e).isdigit() for e in enum):
        lo, hi = min(int(e) for e in enum), max(int(e) for e in enum)
        return f"{{ type: 'number', min: {lo}, max: {hi}, default: {lo}, group: 'core' }}"
    if enum:
        opts = ", ".join(f"'{e}'" for e in enum)
        return f"{{ type: 'select', options: [{opts}], default: '{enum[0]}', group: 'core' }}"
    if typ == "boolean":
        return "{ type: 'select', options: ['true','false'], default: 'false', group: 'core' }"
    if typ in ("integer", "number"):
        lo = s.get("minimum"); hi = s.get("maximum")
        bits = ["type: 'number'"]
        if lo is not None: bits.append(f"min: {lo}")
        if hi is not None: bits.append(f"max: {hi}")
        bits.append(f"default: {lo if lo is not None else 1}"); bits.append("group: 'core'")
        return "{ " + ", ".join(bits) + " }"
    if typ == "string":
        return "{ type: 'text', default: '', group: 'core' }"
    return None  # unknown/complex → skip (don't guess)


def gen(slug: str) -> int:
    doc = _get(f"{OPENAPI}?endpoint_id={urllib.parse.quote(slug)}")
    schemas = doc.get("components", {}).get("schemas", {})
    key = next((k for k in schemas if k.lower().endswith("input")
                or "request" in k.lower()), None)
    if not key:
        print(f"✗ no input schema found for {slug}", file=sys.stderr); return 1
    props = schemas[key].get("properties", {})
    has_image = any(_is_image(s) or n in SKIP and "image" in n for n, s in props.items())
    kind = "video" if "video" in slug else "image"
    lines = [f"  '{slug}': {{",
             f"    label: 'fal · {slug.split('/')[-1].replace('-', ' ')}',",
             f"    kind: '{kind}',"]
    if has_image:
        lines.append("    // refs → image_urls (handled by the fal wrapper); refs >10MB auto-downscaled")
    lines.append("    params: {")
    for name, spec in props.items():
        if name in SKIP or _is_image(spec):
            continue
        f = _field(name, spec)
        if f:
            lines.append(f"      {name}: {f},")
    lines.append("      count: { type: 'number', min: 1, max: 6, default: 1, group: 'core', help: 'Variants (each = 1 generation)' },")
    lines.append("    },\n  },")
    print("\n".join(lines))
    print(f"\n# ^ paste into MODEL_SCHEMAS in visual_chef_gallery.html. "
          f"No wrapper/server change needed (engine routes any fal-ai/* slug).", file=sys.stderr)
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__); return 2
    cmd, arg = sys.argv[1], " ".join(sys.argv[2:])
    return {"search": search, "gen": gen}.get(cmd, lambda a: (print(__doc__) or 2))(arg)


if __name__ == "__main__":
    raise SystemExit(main())
