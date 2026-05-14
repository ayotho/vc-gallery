# vc-canvas

Visual Chef Gallery — a local review dashboard for AI-generated images and videos. Built for fast triage during video production (Trajectory studio / BTW Documentary).

Runs at `http://127.0.0.1:8770/`.

## What it does

- Watches a working folder full of media (images + video)
- Shows them in a grid with status filters (review / accepted / hero / revise / rejected / alternate / draft / legacy)
- Right drawer for per-asset detail: preview, metadata, notes, references, action buttons
- Tracks Higgsfield wrapper fires in real time (active PIDs + tail logs)
- Drafts panel: stage a prompt + refs, fire when ready, watch it land
- Lightbox with zoom + annotate
- Hover-to-play on video cards, spacebar play/pause, auto-loop in drawer

## Quick start

```bash
cd "/Users/ayo/Coding projects/vc-canvas"
python3 vc_gallery_serve.py
# open http://localhost:8770/
```

First load: click the **EP pill** at the top-left to point the server at a working folder. The folder gets its own SQLite DB at `<folder>/.visual_chef/visual_chef.db`.

## File map

| File | Role |
|---|---|
| `vc_gallery_serve.py` | HTTP server (BaseHTTPRequestHandler, no Flask) |
| `vc_gallery_lib.py` | DB schema + asset/job row helpers |
| `vc_gallery_scan.py` | Walks the working folder, upserts new media into DB |
| `vc_gallery_obs.py` | Observability — structured event logging, health snapshots, orphan checks |
| `vc_gallery_cleanup.py` | Wipes 0-byte placeholders + their DB rows |
| `vc_gallery_init.py` | Bootstraps a fresh gallery folder |
| `vc_gallery_thumb.py` | Generates / serves thumbnails |
| `vc_gallery_test.py` | End-to-end test harness (no HF spend) |
| `vc_gallery_backfill_hf_links.py` | One-time backfill of Higgsfield job URLs into notes |
| `visual_chef_gallery.html` | The dashboard (single-page vanilla JS) |
| `hf_gen_with_sidecar.py` | Higgsfield wrapper — fires one generation end-to-end |
| `jsonl_append.py` | Atomic JSONL writer with flock |
| `write_media_sidecar.py` / `write_companion_note.py` | Legacy sidecar emitters (deprecated 2026-05-13 — DB is the truth) |

## Tests

```bash
python3 vc_gallery_test.py
```

48/48 should pass. No HF spend — uses local drafts + endpoint contracts only.

## Health + introspection

```bash
# Live snapshot (DB writable, asset count, 0-byte pollution, wrapper present)
python3 vc_gallery_obs.py health --gallery /path/to/working/folder

# Orphan inventory (zero-byte rows, missing-on-disk rows, untracked files)
python3 vc_gallery_obs.py orphans --gallery /path/to/working/folder

# Tail recent events
python3 vc_gallery_obs.py tail --gallery /path/to/working/folder
```

Or hit the live endpoints:

- `GET /api/health`
- `GET /api/debug/orphans`
- `GET /api/debug/recent-events?n=50`
- `GET /api/fires` — active wrapper fires
- `GET /api/fires/<pid>/log` — tail a specific fire's log

## Cache headers

The dashboard HTML is served with `Cache-Control: no-store` so fixes ship instantly. Media (`/media/...`) and thumbs cache normally for performance.

## Background

Originally lived inside Drive at
`Projects/AI visual chef/arsenal/00-utilities/`.
Moved to local + Git in 2026-05 to avoid Drive sync corruption mid-edit and to get a proper change history.
