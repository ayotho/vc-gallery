#!/usr/bin/env python3
"""Bootstrap an Obsidian Base gallery for a client/project.

Mirrors the Ep 6 pattern: creates ~/Desktop/Clients/<Client>/<Project>/
(or honours --dest), drops a `<Project> Gallery.base` filtered to that folder.

Usage:
    vc_gallery_init.py <client> <project>
    vc_gallery_init.py tallvue pilot
    vc_gallery_init.py malcolm-callux ethan-video --dest "/Users/ayo/Desktop/Clients/Malcolm-Callux/Ethan"
    vc_gallery_init.py btw ep7 --folder-name EP7   # override the file.folder filter value
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "templates" / "gallery.base"
# VC_GALLERY_ROOT env var overrides default. Default matches the Ep6 pattern.
DEFAULT_ROOT = Path(
    os.environ.get("VC_GALLERY_ROOT") or str(Path.home() / "Desktop" / "Clients")
).expanduser()


def titlecase(s: str) -> str:
    """Title-case a slug while preserving all-caps acronyms (BDA, ICP, BTW, etc.)."""
    parts = re.split(r"[-_\s]+", s)
    out = []
    for p in parts:
        if not p:
            continue
        # Preserve all-caps tokens of length >= 2 (acronyms)
        if len(p) >= 2 and p.isupper():
            out.append(p)
        else:
            out.append(p.capitalize())
    return " ".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("client", help="Client slug, e.g. 'tallvue', 'btw', 'malcolm-callux'")
    ap.add_argument("project", help="Project slug, e.g. 'pilot', 'ep6', 'ethan-video'")
    ap.add_argument("--dest", help="Override destination directory (otherwise ~/Desktop/Clients/<Client>/<Project>/)")
    ap.add_argument("--folder-name", help="Override file.folder filter value (defaults to the dest directory basename)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing .base file")
    args = ap.parse_args()

    if not TEMPLATE.exists():
        print(f"[fatal] template missing: {TEMPLATE}", file=sys.stderr)
        sys.exit(2)

    client_dir = titlecase(args.client)
    project_dir = titlecase(args.project)
    if args.dest:
        dest = Path(args.dest).expanduser().resolve()
    else:
        dest = (DEFAULT_ROOT / client_dir / project_dir).resolve()

    dest.mkdir(parents=True, exist_ok=True)
    print(f"[dir] {dest}", file=sys.stderr)

    folder_name = args.folder_name or dest.name
    base_path = dest / f"{project_dir} Gallery.base"

    if base_path.exists() and not args.force:
        print(f"[skip] gallery exists: {base_path} (use --force to overwrite)", file=sys.stderr)
        sys.exit(1)

    content = TEMPLATE.read_text().replace("{{PROJECT_FOLDER}}", folder_name)
    base_path.write_text(content)
    print(f"[wrote] {base_path}", file=sys.stderr)
    print(f"\nOpen in Obsidian. Sidecars written into {dest}/ will appear in Needs Review / Accepted / Rejected.", file=sys.stderr)


if __name__ == "__main__":
    main()
