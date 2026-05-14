#!/usr/bin/env python3
"""write_media_sidecar — thin alias over `write_companion_note`.

Exists for two reasons:
  1. The muapi wrapper scripts (generate-image.sh, generate-video.sh,
     image-to-video.sh) were written with `--project` semantics. They expect
     this CLI path and accept `--project X` instead of `--episode X`.
  2. "Sidecar" reads more naturally for non-BTW work (commercials, brand
     films) where `episode` doesn't apply.

Output is IDENTICAL to `write_companion_note`. This module delegates to it.
Do not maintain parallel frontmatter logic here — the canonical writer owns
the schema.

CLI:
    write_media_sidecar.py <file> --client btw --project ep6 \\
        --model nb2-edit --workflow createframe --prompt "..."

Python:
    from write_media_sidecar import write_sidecar
    write_sidecar(file_path="...", client="btw", project="ep6", ...)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the canonical writer importable regardless of cwd
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from write_companion_note import write_companion_note, VALID_STATUSES  # noqa: E402


def write_sidecar(
    file_path: str,
    client: str,
    project: str,
    model: str = "unknown",
    workflow: str = "unknown",
    prompt: str = "",
    refs: list[str] | None = None,
    status: str = "",
    session: str = "",
    session_date: str = "",
    variant: str = "",
    pass_num: int = 1,
    shot_id: str = "",
    parent: str = "",
    extra_tags: list[str] | None = None,
    notes: str = "",
    hf_job_url: str = "",
    force: bool = False,
) -> bool:
    """Write a sidecar by delegating to the canonical companion-note writer.

    `project` maps to `episode` in the canonical schema. Returns True if
    written, False if skipped (exists and not forced)."""
    p = Path(file_path)
    if not p.exists():
        print(f"[sidecar] skip — media file missing: {p}", file=sys.stderr)
        return False

    try:
        out = write_companion_note(
            str(p),
            status=status,
            model=model,
            workflow=workflow,
            pass_=pass_num,
            variant=variant,
            client=client,
            episode=project,
            shot_id=shot_id,
            hf_job_url=hf_job_url,
            parent=parent,
            session=session,
            session_date=session_date,
            prompt=prompt,
            refs=refs or [],
            tags=extra_tags or None,
            notes=notes,
            overwrite=force,
        )
        print(f"[sidecar] wrote {Path(out).name}", file=sys.stderr)
        return True
    except FileExistsError as e:
        print(f"[sidecar] skip — exists: {p.with_suffix('.md').name}", file=sys.stderr)
        return False
    except ValueError as e:
        # Amendment H: workflow or status enum rejected by canonical writer.
        # Show the clean error message, not a Python traceback.
        print(f"[sidecar] REJECT — {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file_path")
    ap.add_argument("--client", required=True)
    ap.add_argument("--project", required=True, help="maps to `episode` in frontmatter")
    ap.add_argument("--model", default="unknown")
    ap.add_argument("--workflow", default="unknown")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--refs", nargs="*", default=[])
    ap.add_argument("--status", default="", choices=sorted(VALID_STATUSES))
    ap.add_argument("--session", default="")
    ap.add_argument("--session-date", default="")
    ap.add_argument("--variant", default="")
    ap.add_argument("--pass", dest="pass_num", type=int, default=1)
    ap.add_argument("--shot-id", default="")
    ap.add_argument("--parent", default="")
    ap.add_argument("--tag", dest="extra_tags", action="append", default=[])
    ap.add_argument("--notes", default="", help="short why-this-exists note for sidecar")
    ap.add_argument("--hf-job-url", dest="hf_job_url", default="", help="click-through URL to the Higgsfield job UI")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ok = write_sidecar(
        file_path=args.file_path,
        client=args.client,
        project=args.project,
        model=args.model,
        workflow=args.workflow,
        prompt=args.prompt,
        refs=args.refs,
        status=args.status,
        session=args.session,
        session_date=args.session_date,
        variant=args.variant,
        pass_num=args.pass_num,
        shot_id=args.shot_id,
        parent=args.parent,
        extra_tags=args.extra_tags,
        notes=args.notes,
        hf_job_url=args.hf_job_url,
        force=args.force,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
