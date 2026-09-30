---
name: vc-gallery
description: "VC Gallery — the central asset dashboard for AI-generated images and videos. ORGANIZE-FIRST: browse/search, triage accepted vs rejected, Review Board, versions, scenes, shots, drafts, element/ref display, compare mode. Full API reference. Runs at localhost:8770. Use for organizing and reviewing assets. (Generation now runs via the Higgsfield MCP — gallery firing is shelved.)"
---

# /vc-gallery — VC Gallery

## Folder and editorial ownership

Folders own file locations; Gallery owns segment (`scene`), shot and review status. Folder-to-segment alignment is a deliberate one-time metadata operation, not continuous sync. Rescans initialise new rows from available metadata but preserve existing `scene`, `shot_id` and `status`, including when media changes. Raw files without supplied scene metadata remain unassigned. Assigning a segment never moves the file. Keep art-direction references separate from production segments unless deliberately assigned.

## Folder visibility

The toolbar's **Folders** button chooses relative subfolders to hide, including their descendants. Choices persist in `<gallery>/.vc_meta/folder_visibility.json`. Files, approvals and direct asset/media access remain untouched. **Show hidden** sends `show_hidden=1` to asset, facet and scene requests; otherwise these views respect the project visibility choices. Asset filtering happens before counts, pagination and grouping.

- `GET /api/folder-visibility` returns `{hidden, folders}`.
- `POST /api/folder-visibility` accepts `{folder: "relative/subfolder", hidden: true|false}`. Absolute paths, traversal, the root itself and unknown folders are rejected.
- The legacy explicit `hide_render_frames=1` asset-list parameter still excludes images under `_frames/`, `-frames/`, or `/blender/frames/`; the UI no longer guesses which folders to hide. This legacy flag does not filter facet counts.

The gallery server at `http://127.0.0.1:8770/` is the single source of truth for all generated assets (images + videos) across every client project. Every agent (image-chef, video-chef, acquisition-chef) talks to it.

> **ROLE (since 2026-06-16): ORGANIZE-FIRST.** The gallery is for viewing, organizing, and triaging assets — browse/search, accept/reject (Review Board), versions, scenes, element/ref display, quickly seeing what's accepted vs not. **Generation now runs via the Higgsfield MCP, not the gallery.** The CLI fire path (`/api/draft/{id}/fire` → `hf_gen_with_sidecar.py`) is **shelved** — don't route new generations through it. The fire/draft docs below are retained for reference and organization (drafts are still useful as staged-intent records), but firing is no longer the gallery's job. **Reversible by design:** firing is shelved, not deleted — the director may revive it, so the fire path stays intact. "The MCP" = the connected Higgsfield MCP server.

## Start the server

```bash
# Windows (this PC)
python3 "C:/Users/aytho/vc-canvas/vc_gallery_serve.py"

# Mac
python3 "/Users/ayo/Coding projects/vc-gallery/vc_gallery_serve.py"

# With explicit folder
python3 vc_gallery_serve.py --folder "/path/to/working/folder"
```

Port 8770. Localhost only. One process serves one director.

---

## API Reference

### Health + Folder

```
GET  /healthz              -> {ok, version, current_folder, asset_count}
GET  /api/folder            -> {current, recent[], asset_count}
POST /api/folder            -> body: {path: "/abs/path", scan: true}
POST /api/rescan            -> re-scan current folder (recursive; see Scan rules)
GET  /api/health            -> {ok, db_writable, zero_byte_files, db_asset_count}
```

### Scan rules (2026-07-24)

Server rescan/watch uses **recursive** scan with a directory denylist so project
scaffolding never floods the review queue:

- **Skipped dirs:** `production/`, `development/`, `distribution/`, `.visual_chef/`,
  `.git/`, `node_modules/`, venvs, caches, `.drafts/`, other dot-dirs.
- **Kept:** root drops + media session folders (e.g. `midjourney_session*`).
- Rows previously ingested under skipped dirs are **pruned** on the next scan.
- Historical bulk imports heal `first_seen_at` back to file mtime when the stamp
  was inflated by scan-time (keeps old session dumps from beating today's drops).

### Assets (browse + search)

```
GET  /api/assets            -> {items[], total, limit, offset}
  ?status=review            filter by status
  ?media_type=video         filter image/video
  ?model=kling3_0           filter by model
  ?shot_id=SH450            filter by shot
  ?q=nathan                 full-text search (filename, shot_id, prompt)
  ?sort=recent|oldest|name|name-desc|id|id-desc|status|shot|model
  ?limit=50&offset=0        (max 2000)
  ?latest_per_shot=1        show only newest card per shot_id
  ?group_by=shot            returns shot_groups[] with cover + members
  ?updated_after=1716000000 filter by last_updated_at >= epoch (for polling)

# sort=recent (default "Newest first") = MAX(first_seen_at, file_modified_at) DESC
# so fresh on-disk writes surface even when bulk-index first_seen stamps are noisy.

GET  /api/assets/{id}       -> full asset detail + prompt + refs_resolved + review_history
GET  /api/facets            -> {status: {review: 45, hero: 12, ...}, source_type: {...}, ...}
GET  /api/compare?ids=1,2,3 -> {items[], count} (2-6 assets, side-by-side detail + prompts)
GET  /api/scenes            -> {scenes[], total_scenes, unassigned_count}
```

### Asset detail fields

| Field | What it is |
|-------|-----------|
| `id` | Integer ID. Director references as "#2095" |
| `file_path` | Absolute path on disk. Pass to Read tool to see images |
| `filename` | Display name |
| `status` | review / accepted / hero / revise / rejected / alternate / draft / firing / legacy |
| `shot_id` | "SH450" etc |
| `scene` | Scene label |
| `model` | kling3_0, seedance_1_0, gpt_image, etc |
| `prompt` | Generation prompt (empty for raw drops or legacy wiped rows) |
| `refs_resolved[]` | Input refs: `{filename, url, exists, kind}` |
| `hf_url` | Higgsfield job URL if applicable |
| `width`, `height` | Pixel dimensions |
| `duration_sec` | Video duration |
| `media_type` | "image" or "video" |
| `thumb_url` | `/thumb/<sha>.jpg` path for thumbnail |

### Update asset fields (PATCH)

```
PATCH /api/assets/{id}
  body: {shot_id: "SH450", scene: "corridor", notes: "v2 with better lighting", score: 8.5}
```

Updatable fields: `status`, `shot_id`, `scene`, `notes`, `score`, `project`, `client`, `model`, `workflow`. `filename` is handled by the rename path; `file_path` is derived and rejected.

### Status changes

```
POST /api/assets/{id}/reviews
  body: {status: "hero", note: "optional reason", reviewer: "image-chef"}
```

Valid statuses: `review`, `accepted`, `hero`, `revise`, `rejected`, `alternate`, `legacy`

### Rename

```
POST /api/rename
  body: {asset_id: 123, new_filename: "SH450_kling_v2.mp4", move_file: true}
```

### Inherit properties (drag-to-assign)

```
POST /api/assets/{id}/inherit
  body: {source_id: 280}
```

Copies `shot_id` and `scene` from source asset to target asset. Used by the drag-to-inherit UI (director drags card A onto card B, A picks up B's shot_id + scene). Logged in the reviews audit trail.

### Duplicate to draft — one-click "new version" (2026-06-11)

```
POST /api/assets/{id}/duplicate-draft
  body: {} or {payload: {prompt: "tweaked prompt"}, shot_id?, scene?, workflow?}
  -> {ok, asset}  // a fresh draft row, status='draft'
```

Stages a new draft seeded from the source asset: full wrapper payload from its
draft blob when present (aspect ratio, duration, start/end frames), else
prompt+refs+model. client/project/shot/scene carry over. Filename auto-bumps
to the next free `_vN` — repeat calls never collide. `overrides.payload` keys
merge on top, so a re-roll with a tweaked prompt is ONE call. The drawer's
"⊕ New version" button is the human entry point to the same endpoint.

### HF MCP job tracking — fire anywhere, land in the gallery (2026-06-14)

```
POST /api/hf/track
  body: {job_id: "<uuid or HF url>", shot_id?, scene?, filename?, client?, project?, workflow?, notes?}
  -> {ok, job_id, state: "cooking"}
GET /api/hf/tracked -> {jobs: {<job_id>: {state, asset_id?, error?, ...}}, count}
```

For generations fired OUTSIDE the wrapper (Higgsfield MCP, web UI): register
the job id and the server polls every 30s until terminal, then auto-imports
(download → /studio filename → DB row with dims/prompt/refs). States:
cooking → landed | failed | ip_blocked | import_failed | timeout (2h cap).
Agent pattern: fire via MCP → one POST → walk away. No more hand-rolled
watcher loops. Tracking is in-memory (server restart drops pending jobs —
re-POST them).

### Credits chip (2026-06-14)

`GET /api/credits` → `{credits, plan}` (proxies `higgsfield account status`,
cached 60s). The UI shows a wallet chip next to the asset counter — red
under 50 credits. Check it before staging big batches.

### Stage + fire in one call (2026-06-12)

`POST /api/draft` accepts `"fire": true` in the envelope — the draft is
staged and fired in the same request. Response gains a `fire` key
(`{ok, pid, ...}`) and `asset.status` comes back `firing`. A failed fire
does NOT roll back the draft; it stays staged for fix-and-refire. This is
the fast path for live direction — use it whenever the director has already
approved the prompt (skip the separate `/api/draft/{id}/fire` round-trip).

### Hero export — editor handoff (2026-06-12)

```
POST /api/export/heroes
  body: {scene: "remote_viewing", statuses?: ["hero"], media_type?: "image"|"video"|"all", dest?: "/abs/path"}
  -> {ok, scene, dest, copied: [{id, shot_id, exported_as}], missing, count}
```

Copies a segment's keeper assets into `<gallery>/_exports/<scene>_heroes`,
renamed to `<SHOT_ID>.<ext>` (shot-id collisions get a `__slug` suffix).
Non-destructive (copy, not move). Human entry point: "⇣ Export heroes"
button on each segment header in the Segments view (exports hero+accepted
images). Replaces the standalone export_segment_heroes.py round-trip.

### Frame capture — chain-shot primitive (2026-06-12)

```
POST /api/assets/{id}/capture-frame
  body: {"t": 3.04} | {"t": "end"} | {"t": "start"}   // default "end"
  -> {ok, asset, existing, t}   // a new IMAGE asset row
```

Mints an image asset from a frame of a video asset. The PNG lands next to
the source video, named `<video_stem>_f<t*100>.png`, with
`parent_filename` = the video, `workflow` = frame-capture, and inherited
shot/scene/client/project. Idempotent per (asset, rounded t). THE primitive
for the chain-shot workflow: end frame of clip N → start frame / edit base
for clip N+1. No more agent-side ffmpeg + rename + rescan-wait. Human entry
points: "⛶ Grab frame" (frame under the scrubber) and "⇥ End frame"
buttons in the video drawer.

### Bulk scene assignment (2026-06-11)

```
POST /api/assets/bulk-scene
  body: {asset_ids: [1,2,3], scene: "EP9 Opening"}  // empty scene clears
```

### New-since filter (2026-06-11)

`GET /api/assets?seen_after=<epoch>` — assets with `first_seen_at >= t`.
(Complements `updated_after`, which keys on `last_updated_at`.) Powers the
UI's "✨ +N since HH:MM" chip; agents can use it for "what landed since I
last checked".

### Open in Finder/Explorer

```
POST /api/assets/{id}/open  -> reveals the file in Finder (macOS) or Explorer (Windows)
```

---

## Visual Stacking (shot_id grouping)

Cards with the same `shot_id` visually stack in the gallery grid. This is automatic, no extra API calls needed.

**How it works:**
- The grid groups cards by `shot_id` client-side when "Stack versions" is toggled on (default)
- Each stack shows one cover card (hero > accepted > latest) with a version count badge ("4v")
- Shadow-card depth effect behind stacked cards
- Amber attention dot on the badge if any version is `review` or `revise`
- Click a stack to open the drawer, which shows a **version strip** of all siblings below the main preview
- Click any sibling in the strip to switch focus. "Compare all" opens the compare overlay.
- Cards without shot_id render as normal flat cards

**For agents:** Always set `shot_id` on every draft/fire. That's how your output gets organized into stacks. Different models of the same shot stack together. Image + video of the same shot stack together. To see all versions: `GET /api/assets?shot_id=SH450`. To find shots needing revision: `GET /api/assets?status=revise&updated_after=<epoch>`.

**Drag-to-inherit:** Director can drag card A onto card B in the grid. Card A inherits B's `shot_id` and `scene` via `POST /api/assets/{id}/inherit`. Logged in the audit trail.

---

## Review Board (Kanban view)

A 4th view — the **Board** tab, next to Grid / Segments / Shots — that lays assets out as a Kanban: one column per review status, in review-flow order **Review → Revise → Accepted → Hero → Rejected** (Alternate/Legacy in a collapsed "More"; Draft/Firing excluded). The point: **changing a status MOVES the card to another column — it never disappears**, so the whole review state stays on one screen with live per-column counts. It is purely a new RENDER of existing data — no new endpoints, no schema change, frontend-only (`visual_chef_gallery.html`).

**For agents (no new API — the board reuses the existing write path):**
- **Move / triage a card:** `PATCH /api/assets/{id} {status}` — the single write path (logs a review row + runs the hero-demotion cascade server-side). NEVER `POST /reviews` from board context.
- **Bulk move:** `POST /api/assets/bulk-status {asset_ids, status}`.
- **Revise comment:** `PATCH /api/assets/{id} {notes}` (merges into `user_notes`; read back as `asset.notes`).
- **Revise queue (the "revise all these" handoff):** `GET /api/assets?status=revise` → each row's `shot_id` + `notes` is the worklist. The board's "⧉ Copy queue" button emits the same `shot_id — note` lines for a manual handoff — manual and agent read the identical field.
- **Board fetch (what the view shows):** `GET /api/assets?limit=2000` with the status param omitted (so every column populates) + any other active sidebar filters passed through.

**Manual (director):**
- **Drag** a card between columns to change status. **Shift/Cmd-click** or **marquee-drag across empty background** to multi-select, then bulk Accept/Hero/Revise/Reject or ⊞ Compare.
- **Hotkeys** on the focused card: `a` accept · `h` hero · `v` revise (autofocuses the inline comment) · `r` reject · arrows move focus.
- **▶ Play all** on a multi-version shot opens the existing side-by-side compare (solo-audio). Same `shot_id` cards cluster with a shot chip; siblings can sit across different columns and all stay visible.

**Reversibility:** additive — one `data-view="board"` button + `#board-root` + `renderKanban()` + a guarded `patchAsset` board branch + a shared `groupByShot` helper. Removing them restores the prior app; no schema/API change.

---

## Draft Lifecycle (how agents fire generations)

Drafts are the staging area for new generations. The flow:

```
draft  -->  firing  -->  review (exit 0)
                    -->  rejected (exit != 0)
```

### Stage a draft

```
POST /api/draft
body: {
  filename: "SH450_kling_nathan_v3.mp4",
  client: "BTW_Documentary",
  project: "EP9",
  shot_id: "SH450",
  model: "kling3_0",
  workflow: "cref.v2",
  estimated_cost: 24,
  payload: {
    model: "kling3_0",
    mode: "pro",
    duration: 5,
    aspect_ratio: "16:9",
    prompt: "Dr Nathan walking through corridor, cinematic lighting",
    image: ["/abs/path/to/ref1.png", "/abs/path/to/ref2.png"]
  }
}
-> {ok, asset: {id, status: "draft", ...}}
```

### List drafts

```
GET /api/drafts             -> {items: [...all drafts...]}
```

### Edit a draft

```
PUT /api/draft/{id}
body: {
  payload: { ...updated payload... },
  image_refs: ["/new/ref.png"]
}
```

### Fire a draft (start the wrapper)

```
POST /api/draft/{id}/fire   -> {ok, fire: {pid, asset_id, log_path}}
```

This spawns the wrapper subprocess. Status transitions to `firing`, then `review` (on success) or `rejected` (on failure).

### Delete a draft

```
DELETE /api/draft/{id}      -> {ok: true}
```

### Check running fires

```
GET /api/fires              -> {fires: [{pid, asset_id, state, duration_s, eta_s, ...}]}
GET /api/fires/{pid}/log    -> {log: "...stderr output..."}
  ?tail=50                  last N lines
```

Fire states: `running`, `completed`, `failed`

---

## Engines — Higgsfield (default) + fal (additional option)

The fire path picks the engine **automatically from the model id**. Nothing about
the Higgsfield flow changes — fal is an extra option, not a replacement.

| Model id | Engine | Wrapper |
|---|---|---|
| `fal-ai/…` (e.g. `fal-ai/kling-video/o3/pro/reference-to-video`) | **fal** | `fal_gen_with_sidecar.py` |
| everything else (`kling3_0`, `seedance_*`, `gpt_image_2`, …) | **higgsfield** | `hf_gen_with_sidecar.py` |

Routing lives in `_engine_for_payload()`: explicit `engine: "fal"|"higgsfield"` wins,
else any `model` starting with `fal-ai/` → fal, else higgsfield. Both engines share
the SAME draft → fire → review lifecycle, the SAME renaming/stacking, and write to
the SAME provider-agnostic `jobs` table (`job_provider`, `provider_job_id`, `source_url`).

### fal Kling O3 Pro — reference-to-video

Model: `fal-ai/kling-video/o3/pro/reference-to-video`. **Reference-URL driven** — it
does NOT need a start frame.

- **Refs → `image_urls`** (1–4 images). They are *references*, referenced in the prompt
  as `@Image1`…`@Image4`. They are NOT auto-assigned to start/end frame. Put the refs in
  the draft's `image` (or `refs`/`image_urls`) array; the wrapper requires ≥1.
- **`count`** → quantity. `count: 3` fires 3 generations, named `…_1/_2/_3`; the first
  mutates the draft row, the rest become new rows under the same shot_id/scene.
- **`generate_audio`** `"true"|"false"` (Kling O3 has native audio; audio-on costs more).
- **`duration`** 3–15s · **`aspect_ratio`** 16:9/9:16/1:1 · **`shot_type`** customize/intelligent.
- Refs over **10 MB are re-encoded to high-quality JPEG, full resolution kept** (sips) — fal rejects >10 MB but a 30 MB PNG → ~2–3 MB JPEG at full res; resolution only drops as a last resort.
- Auth: `FAL_KEY` in the server's env (falls back to reading `~/.claude/env.sh`).
- Cost (Kling O3 on fal): ≈ $0.112/s audio-off, ≈ $0.14/s audio-on (≈ $1.12 per 8s audio-on clip).

**Stage + fire (identical to Higgsfield, just a fal model id):**
```
POST /api/draft
body: {
  filename: "SH450_klingo3_v1.mp4", client: "BTW_Documentary", project: "EP9",
  shot_id: "SH450", scene: "act2", model: "fal-ai/kling-video/o3/pro/reference-to-video",
  workflow: "i2v",
  payload: {
    model: "fal-ai/kling-video/o3/pro/reference-to-video",
    prompt: "@Image1 walks through the corridor, slow dolly, cinematic",
    image: ["/abs/ref1.png", "/abs/ref2.png"],   // 1–4 refs → @Image1…@Image4
    duration: 5, aspect_ratio: "16:9", generate_audio: "true", count: 1
  }
}
POST /api/draft/{id}/fire    // human clicks Fire in the drawer, or an agent POSTs this
```
The UI dropdown lists it as **"fal · Kling O3 Pro — reference to video"** (in `MODEL_SCHEMAS`).

> **Full fal engine detail → `/vc-fal`** — wrapper contract, ref-only/count/downscale behavior, the standard + 4K models, and the `search→gen→paste` workflow for adding new fal models with no code changes. Broad fal SDK + model-discovery APIs → `/fal`. (This mirrors how `/vc-higgsfield` complements `/vc-gallery`.)

---

## External fire registration (agent-spawned wrappers, #28)

When an agent fires the wrapper directly (not through the draft UI), the wrapper auto-notifies the server via `--notify-server` (defaults to `$VC_CANVAS_URL` or `http://127.0.0.1:8770`).

Agents can also register fires manually:

```
POST /api/fires
  body: {pid: 12345, asset_id: 100, filename: "SH450_kling_v1.mp4",
         shot_id: "SH450", model: "kling3_0", workflow: "cref.v2",
         started_at: 1716000000, log_path: "/path/to/log"}

POST /api/fires/<pid>/complete
  body: {exit_code: 0, finished_at: 1716000060}
```

## Debug endpoints

```
GET /api/debug/orphans          -> {zero_byte_rows[], missing_file_rows[]}
GET /api/debug/recent-events    -> [...last N audit events...]
  ?n=20
```

## File serving

```
GET /thumb/<sha>.jpg        -> thumbnail (auto-generated via ffmpeg)
GET /media/<filename>       -> original media file from gallery folder
GET /ref?path=/abs/path     -> serve a reference image (must be under allowed roots)
GET /sidecar/<filename>     -> raw sidecar markdown
```

---

## For image-chef / video-chef agents

### Typical generation flow

1. **Check what the director wants**: `GET /api/selection` or read their message
2. **Search existing assets**: `GET /api/assets?shot_id=SH450&status=hero` to see what's already done
3. **Stage a draft**: `POST /api/draft` with the payload
4. **Show the director**: "Staged draft #2100 for SH450 -- kling3_0, 5s, 16:9. Fire it?"
5. **Fire on approval**: `POST /api/draft/2100/fire`
6. **Monitor**: `GET /api/fires` to check progress
7. **Review result**: once status transitions to `review`, the asset is in the dashboard for the director

### Reading assets visually

- Images: use the Read tool on `file_path` to see the image (Claude has vision)
- Videos: can't visually inspect. Report `width x height`, `duration_sec`, `filename`. Offer to extract a frame: `ffmpeg -ss 2 -i <path> -frames:v 1 /tmp/frame.png` then Read that

### Ref images

`refs_resolved` on each asset tells you what input images were used. Each entry has:
- `url`: a `/ref?path=...` URL for the browser
- `filename`: display name
- `exists`: whether the file is on disk
- `kind`: "file" or "url"

To actually see a ref image, use the Read tool on the raw path (available in `refs_resolved[].raw`).

**Source discipline:** video draft refs should come from VC Gallery assets, not
segment-card screenshots. Segment cards are briefing/spec context. Do not pass
`clients/.../segment_cards/frames/...` paths into `payload.image` unless the
director explicitly promotes that still into VC Gallery as a real generation
reference. If a shot has no VC Gallery ref, stage it as blocked/needs-ref rather
than silently using a segment-card frame.

**Prompt ref token split by engine:**
- fal models that support reference tags, including Kling O3
  `fal-ai/kling-video/o3/pro/reference-to-video`, may use `@Image1`,
  `@Image2`, etc. in prompt text.
- Higgsfield/Seedance wrapper prompts must not contain `@Image1` /
  `@image_1` tokens. The Higgsfield CLI treats `@...` as a read-from-file sigil
  and can fail before submit. For those drafts, pass refs in `payload.image` but
  refer to them in prompt text as `Reference 1`, `Reference 2`, etc.
- Higgsfield `seedance_2_0` rejected `sound` as an unknown param on 2026-06-03.
  Until `higgsfield model get seedance_2_0` confirms otherwise, omit `sound`
  from Seedance payloads and describe diegetic audio/SFX in the prompt text.

---

## Changelog (2026-09-07)

- **Request-thread SQLite handles close deterministically** — the threaded HTTP server now releases each request thread's lazy database connection when that request finishes, including error paths. This prevents browser polling from accumulating database/WAL descriptors until the launchd soft file limit crashes the gallery. The long-lived folder watcher keeps its own thread-local connection.

## Changelog (2026-06-06)

- **Board updates in place (no more shake on new clips)** — background polls (the new-clip scan + fire-completion) used to rebuild the whole board via `innerHTML`, which on the Review Board reset a playing preview/drawer video, kicked the cursor out of a revise note mid-type, and shook the layout whenever a clip landed. A background poll now **reconciles the board in place** (`reconcileKanban()`): cards are added/relocated/removed on the live DOM (a node move preserves a playing `<video>`), the card under active edit is never touched, the open drawer is no longer closed out (the close-drawer check now counts `boardItems`), and a marquee selection survives the poll. The full re-sort/re-cluster rebuild only runs on user-driven refreshes (filter / view change). Frontend-only, fully reversible. **Agent note:** nothing changes for agents — status/notes still go through `PATCH /api/assets/{id}`; the board just no longer flickers while you fire new generations during a review.

## Changelog (2026-06-05)

- **Review Board (Kanban view)** — new 4th view (the dormant `board` tab revived). Columns = review statuses; a status change MOVES a card between columns instead of removing it (fixes "card vanishes on status change"). Drag / click / hotkey (`a/h/v/r`) + marquee multi-select all funnel through `PATCH /api/assets/{id}` (never `/reviews`). Inline Revise comment per card; "⧉ Copy queue" exports the revise worklist (matches `GET /api/assets?status=revise`). Reuses stacking, compare, filters, and the multi-select engine. Frontend-only, fully reversible. See **Review Board (Kanban view)** section above.

## Changelog (2026-06-02)

- **Second engine: fal** — `fal-ai/…` models route to `fal_gen_with_sidecar.py`; everything else stays on Higgsfield. Same draft→fire→review lifecycle, same stacking, provider-agnostic `jobs` table. First model: `fal-ai/kling-video/o3/pro/reference-to-video` (reference-URL driven, no start frame; `count` quantity; refs >10 MB re-encoded full-res JPEG; `FAL_KEY` auth). See **Engines** section above.

## Changelog (2026-05-18)

- **Visual stacking** — cards with same shot_id group in grid with version badges, shadow depth, attention dots. Drawer shows version strip with "Compare all"
- **Shots view** — new tab grouping assets by shot_id with expandable headers
- **Compare mode** — Ctrl+select 2-6 cards, full-screen side-by-side with inline status actions
- **Stack versions toggle** — replaces Latest Only, default ON
- **Drag-to-inherit** — `POST /api/assets/{id}/inherit` copies shot_id + scene
- **Scene overview** — `GET /api/scenes` groups assets by scene with status bands
- **External fire registration** — wrapper auto-notifies server (#28)
- **Speed fixes** — conditional prompt JOIN, batch updates, indexed queries (#42)
- **`updated_after` filter** — `?updated_after=<epoch>` for agent revision polling
- **Limit cap** — raised to 2000 for full client-side grouping

## Known gaps

- **No auto-start.** Server must be started manually each session.
- **Cross-platform ref paths.** Mac refs don't resolve on Windows and vice versa. Workaround: `VC_REF_ALLOW_ROOTS` env var.
- **Shot_id coverage.** Legacy assets with free-form filenames have no shot_id. They render as flat cards, not stacks.

## Server not running?

If `curl http://127.0.0.1:8770/healthz` fails with connection refused, start the server (see "Start the server" above).
