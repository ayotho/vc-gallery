#!/usr/bin/env python3
"""HTTP routes for the read-only Library overlay (see vc_gallery_library.py).

Kept out of vc_gallery_serve.py so the review server only gains a dispatch
hook. Every route is read-only with respect to the library tree. The only
writes are (a) the list of remembered library roots in the server config and
(b) image thumbnails in a cache outside the tree.

GET  /library                         → Library page
GET  /api/library/roots               → {roots: [...]}
POST /api/library/roots {path}        → remember a root (validated folder)
GET  /api/library?root=               → overview (units, stages, latest cut)
GET  /api/library/folder?root=&rel=   → every file under one folder
GET  /library/file?root=&rel=         → stream a file (Range) — may download it
GET  /library/thumb?root=&rel=        → image thumbnail (images only)
POST /api/library/reveal {root, rel}  → reveal the file in Finder
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vc_gallery_lib as lib  # noqa: E402
import vc_gallery_library as library  # noqa: E402
import vc_gallery_thumb as thumb_mod  # noqa: E402

PAGE = _HERE / "visual_chef_library.html"
THUMB_CACHE = lib.SERVER_CONFIG_DIR / "library_thumbs"
MAX_ROOTS = 12


def _roots() -> list[str]:
    cfg = lib.load_server_config()
    return [r for r in cfg.get("library_roots", []) if Path(r).is_dir()]


def _remember_root(path: str) -> list[str]:
    root = str(library.resolve_root(path))
    cfg = lib.load_server_config()
    roots = [r for r in cfg.get("library_roots", []) if r != root]
    roots.insert(0, root)
    cfg["library_roots"] = roots[:MAX_ROOTS]
    lib.save_server_config(cfg)
    return cfg["library_roots"]


def _one(params: dict, key: str) -> str:
    v = params.get(key) or [""]
    return v[0]


def _errors(handler, fn) -> None:
    try:
        fn()
    except PermissionError as exc:
        handler._send_error_json(403, str(exc))
    except FileNotFoundError as exc:
        handler._send_error_json(404, str(exc) or "not found")
    except ValueError as exc:
        handler._send_error_json(400, str(exc))


def handle_get(handler, path: str, params: dict) -> bool:
    """Return True if the request was a Library route (handled)."""
    if path == "/library":
        handler._send_file(PAGE, no_cache=True)
        return True
    if path == "/api/library/roots":
        handler._send_json(200, {"roots": _roots()})
        return True
    if path == "/api/library":
        def run() -> None:
            if _one(params, "fresh"):
                library.clear_cache()
            handler._send_json(200, library.library_overview(_one(params, "root")))
        _errors(handler, run)
        return True
    if path == "/api/library/folder":
        _errors(handler, lambda: handler._send_json(
            200, library.library_folder(_one(params, "root"), _one(params, "rel"))))
        return True
    if path == "/library/file":
        _errors(handler, lambda: handler._serve_with_range(
            library.file_for_serving(_one(params, "root"), _one(params, "rel"))))
        return True
    if path == "/library/thumb":
        def run() -> None:
            src = library.file_for_serving(_one(params, "root"), _one(params, "rel"))
            if library.KIND_BY_EXT.get(src.suffix.lower()) != "image":
                handler._send_error_json(415, "thumbnails are images-only; open videos explicitly")
                return
            name = thumb_mod.ensure_thumb(src, THUMB_CACHE)
            if not name:
                handler._send_error_json(404, "thumbnail unavailable")
                return
            handler._send_file(THUMB_CACHE / name)
        _errors(handler, run)
        return True
    return False


def handle_post(handler, path: str) -> bool:
    if path == "/api/library/roots":
        def run() -> None:
            body = handler._read_json_body()
            handler._send_json(200, {"roots": _remember_root(body.get("path", ""))})
        _errors(handler, run)
        return True
    if path == "/api/library/reveal":
        def run() -> None:
            body = handler._read_json_body()
            p = library.file_for_serving(body.get("root", ""), body.get("rel", ""))
            subprocess.run(["open", "-R", str(p)], check=False, timeout=10)
            handler._send_json(200, {"ok": True})
        _errors(handler, run)
        return True
    return False


def _unused(_: Any) -> None:  # keep linters quiet about Any import in py<3.11
    pass
