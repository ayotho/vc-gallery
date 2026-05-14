# vc-canvas

> The local triage cockpit that turns hundreds of AI-generated media files into a reviewable narrative. One SQLite per gallery, one server, one director, many agents.

Runs at `http://127.0.0.1:8770/`. No cloud, no auth, no vendor lock-in.

---

## Why this exists

AI video production now produces 100+ images and videos per session per shot. Sidecar markdown files (one `.md` next to every media file) were tried and abandoned — they desync from the real file, double-write under parallel agents, and break Obsidian's wikilink graph.

Cloud SaaS triage tools (Frame.io, Aspera, etc.) assume a team of twenty and a vendor relationship. We needed something a solo creative director and his AI agents can both drive, that lives on `localhost`, owes nothing to anyone, and treats the AI agent as a **first-class consumer of every endpoint the UI uses**.

This is that.

## Who it's for

| Consumer | How they use it |
|---|---|
| **Solo creative director** | Triages ~2,000 assets per gallery via the dashboard at `localhost:8770`. Marks Accept / Hero / Revise / Reject. Stages drafts, fires wrappers, watches them complete live. |
| **AI agents** (image-chef, video-chef, future) | Read + write the same data via the JSON API. Stage drafts programmatically, embed outputs into Obsidian shot cards, query the canvas as a source of truth for what's already shot. |

Both are equal. **Every feature ships as an API endpoint the agent can call before it ships as a button the director clicks.**

## Core principles

The decisions we will not reverse without ceremony:

1. **SQLite is the truth.** Filenames, paths, sidecars — all subordinate. Sidecars officially deprecated 2026-05-13.
2. **API-first.** Drafts MVP set the pattern: same `POST /api/draft` serves image-chef on autopilot AND a director clicking "Stage draft" in the drawer. New features inherit this pattern.
3. **Localhost only.** `127.0.0.1:8770`. No external network. No auth surface. No compliance theatre.
4. **One source of truth per fact.** `pulled_from` lives in `jobs.source_url`, never duplicated into `notes`. The `notes` column is for what the director typed — nothing else.
5. **Backlog discipline.** Every bug captured with full repro context — session JSONL, exact files, exact commands, recovery commands. The "fresh agent cold-start" test: can a brand-new agent reproduce + fix this 6 months from now using only the issue body?
6. **The director clicks. The agent fires. Same API.**

## The north-star loop (60 seconds, two consumers)

```
1. Image-chef stages a draft via POST /api/draft
2. Director glances at the drawer, hits Fire
3. Wrapper subprocess runs; fires panel shows live progress
4. Output lands; scanner backfills shot_id from the filename
5. Server appends ![[output.mp4]] to SH450.md under ## Variants
6. Director hits H for Hero — server moves the embed into ## Hero
7. Next time video-chef picks up SH450, it reads the brief from the shot card
   and starts from the hero frame
```

Six steps, zero context switches, both consumers agree on truth. Steps 4–7 land in the **v0.3 Obsidian Bridge** milestone — see [ROADMAP.md](ROADMAP.md).

## Non-goals

What this will never become:

- **Not a SaaS.** No login screens, no multi-tenancy, no billing.
- **Not a Premiere / DaVinci replacement.** This is the triage layer ABOVE editorial tools. Editorial relinks via Premiere XML/JSX (see the `vc-pipeline` skill).
- **Not a generation engine.** Generation lives in wrappers (`hf_gen_with_sidecar.py` and siblings). Canvas only reviews + organizes the outputs.
- **Not multi-user.** One director, many of their agents. If you need collaborative editing, use Frame.io.
- **Not a Notion competitor.** Notion is the client-facing share layer; canvas is the operator-facing truth layer.

---

## Quick start

```bash
cd "/Users/ayo/Coding projects/vc-canvas"
python3 vc_gallery_serve.py
# open http://localhost:8770/
```

First load: click the **EP pill** at the top-left to point the server at a working folder. The folder gets its own SQLite DB at `<folder>/.visual_chef/visual_chef.db`.

## What it does today

- Watches a working folder full of media (images + video)
- Grid view with status filters (review / accepted / hero / revise / rejected / alternate / draft / legacy)
- Side drawer for per-asset detail: preview, metadata, notes, references, action buttons
- Tracks wrapper fires in real time — flame icon in topbar, click for live log tail
- Drafts panel — stage a prompt + refs via API or UI, fire when ready, watch it land
- Lightbox with zoom + annotate
- Hover-to-play on video cards · spacebar play/pause in drawer · auto-loop on video preview
- Inline folder switcher (no browser `prompt()` quirks)
- Live cache headers — fixes ship the instant the server is restarted (no hard-reload required after first bust)

## File map

| File | Role |
|---|---|
| `vc_gallery_serve.py` | HTTP server (stdlib `BaseHTTPRequestHandler`, no Flask), fires registry, draft CRUD, ref serving, facets, sort/filter |
| `vc_gallery_lib.py` | DB schema, asset/job/review row helpers |
| `vc_gallery_scan.py` | Disk → DB scanner. Rename reconciliation. Skips 0-byte + partial-download files. |
| `vc_gallery_obs.py` | Observability — structured event logging, health snapshots, orphan checks, test-event filter |
| `vc_gallery_cleanup.py` | Wipes 0-byte placeholders + their DB rows |
| `vc_gallery_init.py` | Bootstraps a fresh gallery folder |
| `vc_gallery_thumb.py` | Generates + serves thumbnails (ffmpeg-backed for video frames) |
| `vc_gallery_test.py` | End-to-end test harness — 48 tests, zero HF spend |
| `vc_gallery_backfill_hf_links.py` | One-time backfill of Higgsfield job URLs into notes |
| `visual_chef_gallery.html` | The dashboard (single-page vanilla JS, no framework) |
| `hf_gen_with_sidecar.py` | Higgsfield wrapper — fires one generation end-to-end, writes the asset row directly |
| `jsonl_append.py` | Atomic flock-safe JSONL writer |
| `write_media_sidecar.py` / `write_companion_note.py` | Legacy sidecar emitters — deprecated 2026-05-13, DB is the truth |

## API surface (the agent contract)

Every endpoint usable from `curl` or any agent. UI is a thin wrapper.

```
GET  /                              dashboard HTML (no-store cached)
GET  /api/folder                    current working folder + asset count + watermark
POST /api/folder                    switch working folder (triggers scan)
GET  /api/assets                    list with status/source/media_type/workflow/shot_id/scene/q filters
GET  /api/assets/<id>               full asset detail — prompt, refs_resolved, hf_url, draft block
PATCH /api/assets/<id>              update status (writes a review history row)
POST /api/assets/<id>/open          reveal in Finder (returns clear error if file missing)
POST /api/rename                    atomic filename + file_path swap (preserves status, history)

POST /api/draft                     stage a new draft (agent or director)
GET  /api/drafts                    list all drafts
PUT  /api/draft/<id>                edit draft payload / refs / cost
DELETE /api/draft/<id>              drop the draft
POST /api/draft/<id>/fire           spawn wrapper, return pid

GET  /api/fires                     active + recent fires (state, model, eta_s, refs_count)
GET  /api/fires/<pid>/log           tail wrapper log, parses failure sidecar if present

GET  /api/facets                    sidebar buckets — status, source_type, workflow, scene, shot_id
GET  /api/health                    DB writability, journal mode, asset count, 0-byte pollution
GET  /api/debug/orphans             zero-byte rows, missing-on-disk rows, untracked files
GET  /api/debug/recent-events       JSONL event tail (severity/source/event_filter/include_test)

GET  /media/<id>                    HTTP Range-capable media serve
GET  /thumb/<sha>.jpg               cached thumb (ffmpeg-extracted for videos)
GET  /ref?path=<absolute>           serve a reference image — strict allowlist (STATE.folder, ~/Desktop, VC_REF_ALLOW_ROOTS)
```

Coming in v0.3: `/api/obsidian/embed`, `/api/obsidian/shot-card`, `/api/obsidian/shot-cards` — see [ROADMAP.md](ROADMAP.md).

## Tests

```bash
python3 vc_gallery_test.py
```

48/48 should pass. No HF spend — uses local drafts + endpoint contracts only.

## Health + introspection

```bash
# Live snapshot (DB writable, asset count, 0-byte pollution, wrapper present)
python3 vc_gallery_obs.py health --gallery /path/to/working/folder

# Orphan inventory
python3 vc_gallery_obs.py orphans --gallery /path/to/working/folder

# Tail recent events (severity / source / event filtering supported)
python3 vc_gallery_obs.py tail --gallery /path/to/working/folder
```

## How decisions get made

- New features → opened as a GitHub Issue with the **mandatory backlog block** (session JSONL, exact files, repro commands, state snapshot, recovery commands, schema constraints, likely culprit, repro test, sample IDs). One-line "X is broken" issues are explicitly disallowed.
- Architectural changes → drafted as a plan, reviewed by `/claudex:plan` or stress-tested by spawning subagents against the proposed surface BEFORE merge.
- Reversing a Core Principle (above) → requires a separate issue explaining why the principle no longer serves the project.

## Cache headers

The dashboard HTML is served with `Cache-Control: no-store` so fixes ship instantly. Media (`/media/...`) and thumbs cache normally for performance.

## Background

Originally lived inside Drive at `Projects/AI visual chef/arsenal/00-utilities/`. Moved to local + Git on 2026-05-14 to avoid Drive sync corruption mid-edit and to get proper version history. Drive sync was actively corrupting `.py` files mid-write and leaving partial-download `.tmp.*` fragments in working folders. Local Git fixes both; GitHub Issues replaces the prior `BACKLOG.md` (kept as archive).

## License

MIT. See [LICENSE](LICENSE).

---

**See also:**
- [ROADMAP.md](ROADMAP.md) — phased plan + open milestones
- [`.github/ISSUE_TEMPLATE/bug.md`](.github/ISSUE_TEMPLATE/bug.md) — the mandatory backlog format
- GitHub Issues at https://github.com/ayotho/vc-canvas/issues — current backlog
