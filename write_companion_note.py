#!/usr/bin/env python3
"""
write_companion_note.py — canonical companion-note writer for EP6 generations.

Writes a `<image_stem>.md` sidecar next to an image with full frontmatter
metadata conforming to the Phase 2 schema (see plans/resilient-snuggling-wren.md).

Call signatures:
    Library:
        from write_companion_note import write_companion_note
        write_companion_note("/path/to/img.png", status="review", model="nb2-edit", ...)

    CLI:
        python3 write_companion_note.py /path/to/img.png --status review --model nb2-edit ...

Design notes:
- YAML is constructed manually to guarantee 4-space indent + single-quoted
  wikilinks even when filenames contain parentheses/spaces/unicode. PyYAML
  does not reliably round-trip `'[[Group 12 (3).png]]'`.
- Idempotent: raises FileExistsError if <stem>.md exists unless overwrite=True.
- Safe for filenames with parens, spaces, unicode, curly quotes.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional


VALID_STATUSES = {"", "review", "accepted", "approved", "rejected", "hero", "legacy"}

# Amendment H (2026-04-24 image-chef restructure): the `workflow:` YAML field
# in every image sidecar must be one of these values. Expanded enum — legacy
# values retained for backward compatibility with ~thousands of existing
# sidecars and with the shot-map handoff that video-chef reads.
#
# Mapping table: see .claude/agent-memory/image-chef/playbooks/README.md
VALID_WORKFLOWS = {
    # Playbook-backed values
    "createframe",             # playbooks/createframe.md (default)
    "decgi",                   # playbooks/createframe.md (DECGI_V7 variant)
    "faceswap",                # playbooks/faceswap.md
    "style-transfer",          # playbooks/style-transfer.md
    "multi-angle",             # playbooks/multi-angle-panel.md
    "multi-angle-retexture",   # playbooks/multi-angle-panel.md (retexture variant)
    "retexture",               # playbooks/retexture-pipeline.md
    "character-reference",     # playbooks/character-reference.md
    # Sidecar-only tags (no playbook; reference material)
    "2-pass",                  # 2-pass refinement work
    "backfill",                # retro-applied fixes
    "frame-capture",           # stills pulled from picture lock
    # Transitional escape hatch — emits a warning, does NOT reject
    "unknown",
}


def _yq(v: str) -> str:
    """Single-quote a YAML scalar. Doubles any embedded single quotes."""
    return "'" + v.replace("'", "''") + "'"


def _wikilink(stem: str) -> str:
    """Wrap a filename/stem in [[...]] form.

    Obsidian wikilinks resolve by basename via the vault index — never embed
    absolute filesystem paths inside [[...]]. If the input looks like a path
    (contains `/` and isn't a URL), strip to basename first.
    """
    s = stem
    if s.startswith("[[") and s.endswith("]]"):
        return s
    if s.startswith("http://") or s.startswith("https://"):
        return f"[[{s}]]"
    if "/" in s:
        s = os.path.basename(s)
    return f"[[{s}]]"


def _norm_tag(t: str) -> str:
    return (t or "").strip().lower()


def _compose_tags(
    extras: Optional[list[str]],
    model: str,
    workflow: str,
    client: str,
    episode: str,
) -> list[str]:
    base = [model, workflow, client, episode]
    combined = list(extras or []) + base
    seen = set()
    out: list[str] = []
    for t in combined:
        nt = _norm_tag(t)
        if not nt or nt == "unknown":
            # still allow real tags like 'backfill', 'btw', 'ep6'
            # but skip empty/unknown placeholders
            if nt == "unknown":
                continue
            if not nt:
                continue
        if nt in seen:
            continue
        seen.add(nt)
        out.append(nt)
    return out


def write_companion_note(
    image_path: str,
    *,
    status: str = "",
    model: str = "unknown",
    workflow: str = "unknown",
    pass_: int = 1,
    variant: str = "",
    client: str = "",
    episode: str = "",
    shot_id: str = "",
    hf_job_url: str = "",
    parent: str = "",
    session: str = "",
    session_date: str = "",
    score=None,
    notes: str = "",
    prompt: str = "",
    refs: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    overwrite: bool = False,
) -> str:
    """Write a canonical companion .md next to `image_path`.

    Returns the absolute path of the written note.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"status={status!r} not in {sorted(VALID_STATUSES)}"
        )

    # Amendment H: validate workflow against the expanded enum. Strict reject
    # for anything unknown EXCEPT the literal 'unknown' escape hatch (which
    # warns but passes). Mapping table: playbooks/README.md.
    wf = (workflow or "unknown").strip()
    if wf not in VALID_WORKFLOWS:
        raise ValueError(
            f"workflow={workflow!r} not in VALID_WORKFLOWS. "
            f"Accepted: {sorted(VALID_WORKFLOWS)}. "
            f"See .claude/agent-memory/image-chef/playbooks/README.md for the mapping table."
        )
    if wf == "unknown":
        print(
            f"WARN: write_companion_note called with workflow='unknown' for {image_path}. "
            f"Sidecar will still be written, but callers should pass a real value from "
            f"VALID_WORKFLOWS.",
            file=sys.stderr,
        )

    p = Path(image_path)
    media_name = p.name  # used for [[wikilink]] and embed
    md_path = p.with_suffix(".md")

    if md_path.exists() and not overwrite:
        raise FileExistsError(f"companion already exists: {md_path}")

    # Detect media type from extension. Bare-bones video-sidecar support: same
    # frontmatter shape as image, but use `video:` field name and `media_type:
    # video` tag so Bases can filter cleanly. Body embed `![[file.mp4]]`
    # renders inline in Obsidian.
    _VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".bmp"}
    _ext = p.suffix.lower()
    if _ext in _VIDEO_EXTS:
        media_type = "video"
        media_field = "video"
    elif _ext in _IMAGE_EXTS:
        media_type = "image"
        media_field = "image"
    else:
        # Unknown extension — default to image-shaped sidecar (preserves backward compat)
        media_type = "image"
        media_field = "image"

    final_tags = _compose_tags(tags, model, workflow, client, episode)
    refs_list = list(refs or [])

    lines: list[str] = ["---"]

    # media field (wikilink, single-quoted) — `image:` for image, `video:` for video.
    # Plus a unified `media:` field that mirrors whichever specific field exists,
    # so Obsidian Bases card views can use ONE thumbnail field name (`image: media`)
    # and cleanly thumbnail both image and video sidecars in a combined view.
    _wl = _yq(_wikilink(media_name))
    lines.append(f"{media_field}: {_wl}")
    lines.append(f"media: {_wl}")
    lines.append(f"media_type: {media_type}")

    # tags
    if final_tags:
        lines.append("tags:")
        for t in final_tags:
            lines.append(f"    - {t}")
    else:
        lines.append("tags: []")

    # simple scalars — quote only if value contains special chars; safe default: quote strings
    lines.append(f"status: {status}")
    lines.append(f"model: {model or 'unknown'}")
    lines.append(f"workflow: {workflow or 'unknown'}")
    lines.append(f"pass: {int(pass_)}")
    lines.append(f"variant: {variant or ''}")
    lines.append(f"client: {client or ''}")
    lines.append(f"episode: {episode or ''}")
    lines.append(f"shot_id: {shot_id or ''}")

    # hf_job_url — optional click-through to Higgsfield UI for the job that
    # produced this media. Only written when provided; absence is fine.
    # See `references/roadmap.md` for the wider HF↔sidecar synergy plan.
    if hf_job_url:
        lines.append(f"hf_job_url: {_yq(hf_job_url)}")

    # parent — always single-quoted wikilink form, or empty string
    if parent:
        if not (parent.startswith("[[") and parent.endswith("]]")):
            parent = _wikilink(parent)
        lines.append(f"parent: {_yq(parent)}")
    else:
        lines.append("parent: ''")

    lines.append(f"session: {session or ''}")
    lines.append(f"session_date: {session_date or ''}")

    # score: null or number
    if score is None or score == "":
        lines.append("score: null")
    else:
        lines.append(f"score: {score}")

    # notes — short string, quote to be safe
    lines.append(f"notes: {_yq(notes or '')}")

    # prompt — YAML literal block
    lines.append("prompt: |")
    prompt_body = prompt if prompt is not None else ""
    if prompt_body == "":
        lines.append("    ")
    else:
        for pl in prompt_body.splitlines() or [""]:
            lines.append(f"    {pl}")

    # refs list — each item single-quoted wikilink
    if refs_list:
        lines.append("refs:")
        for r in refs_list:
            rw = r
            if not (rw.startswith("[[") and rw.endswith("]]")) and not rw.startswith("http"):
                rw = _wikilink(rw)
            lines.append(f"    - {_yq(rw)}")
    else:
        lines.append("refs: []")

    lines.append("---")
    lines.append("")
    lines.append(f"![[{media_name}]]")
    lines.append("")
    lines.append("## Director notes")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return str(md_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Write a canonical companion .md next to an image."
    )
    p.add_argument("image_path", help="absolute path to the image")
    p.add_argument("--status", default="", choices=sorted(VALID_STATUSES))
    p.add_argument("--model", default="unknown")
    p.add_argument("--workflow", default="unknown")
    p.add_argument("--pass", dest="pass_", type=int, default=1)
    p.add_argument("--variant", default="")
    p.add_argument("--client", default="")
    p.add_argument("--episode", default="")
    p.add_argument("--shot-id", default="")
    p.add_argument("--parent", default="")
    p.add_argument("--session", default="")
    p.add_argument("--session-date", default="")
    p.add_argument("--score", default=None)
    p.add_argument("--notes", default="")
    p.add_argument("--prompt", default="")
    p.add_argument("--ref", action="append", default=[], help="repeatable")
    p.add_argument("--tag", action="append", default=[], help="repeatable")
    p.add_argument("--overwrite", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    score = args.score
    if score is not None:
        try:
            score = float(score)
            if score.is_integer():
                score = int(score)
        except Exception:
            pass
    try:
        out = write_companion_note(
            args.image_path,
            status=args.status,
            model=args.model,
            workflow=args.workflow,
            pass_=args.pass_,
            variant=args.variant,
            client=args.client,
            episode=args.episode,
            shot_id=args.shot_id,
            parent=args.parent,
            session=args.session,
            session_date=args.session_date,
            score=score,
            notes=args.notes,
            prompt=args.prompt,
            refs=args.ref,
            tags=args.tag,
            overwrite=args.overwrite,
        )
    except FileExistsError as e:
        print(f"SKIP: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
