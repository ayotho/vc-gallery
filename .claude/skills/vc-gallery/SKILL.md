---
name: vc-gallery
description: "Visual Chef Gallery — the central asset dashboard for all AI-generated images and videos. Use when: reviewing/searching assets, staging drafts for generation, firing wrapper jobs, checking fire status, reading the director's selection, or pushing new assets. Runs at localhost:8770. Replaces the old 8766 viewer entirely."
---

# /vc-gallery — Visual Chef Canvas (Gallery)

The gallery server at `http://127.0.0.1:8770/` is the single source of truth for all generated assets (images + videos) across every client project. Every agent (image-chef, video-chef, acquisition-chef) talks to it.

## Start the server

```bash
# Windows (this PC)
python3 "C:/Users/aytho/vc-gallery/vc_gallery_serve.py"

# Mac
python3 "/Users/ayo/Coding projects/vc-gallery/vc_gallery_serve.py"

# With explicit folder
python3 vc_gallery_serve.py --folder "/path/to/working/folder"
```

Port 8770. Localhost only. One process serves one director.

## When to invoke

| Trigger | Action |
|---------|--------|
| `/select` or "look at this", "the selected one" | Read selection (see Selection Bridge below) |
| `/select 2095` or `#2095` | Fetch specific asset |
| "stage a draft", "queue this gen" | POST /api/draft |
| "fire it", "run the draft" | POST /api/draft/{id}/fire |
| "what's firing", "check status" | GET /api/fires |
| "search for SH450" | GET /api/assets?q=SH450 |
| "mark as hero/accepted/rejected" | POST /api/assets/{id}/reviews |

---

## API Reference

### Health + Folder

```
GET  /healthz              -> {ok, version, current_folder, asset_count}
GET  /api/folder            -> {current, recent[], asset_count}
POST /api/folder            -> body: {path: "/abs/path", scan: true}
POST /api/rescan            -> re-scan current folder
GET  /api/health            -> {ok, db_writable, zero_byte_files, db_asset_count}
```

### Assets (browse + search)

```
GET  /api/assets            -> {items[], total, limit, offset}
  ?status=review            filter by status
  ?media_type=video         filter image/video
  ?model=kling3_0           filter by model
  ?shot_id=SH450            filter by shot
  ?q=nathan                 full-text search (filename, shot_id, prompt)
  ?sort=recent|oldest|name|name-desc|id|id-desc|status|shot|model
  ?limit=50&offset=0

GET  /api/assets/{id}       -> full asset detail + prompt + refs_resolved + review_history
GET  /api/facets            -> {status: {review: 45, hero: 12, ...}, source_type: {...}, ...}
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

Updatable fields: `status`, `shot_id`, `scene`, `notes`, `score`, `tags` (array)

Use this to tag shot IDs after generating, add notes, or update scene labels. The dashboard has inline editing for shot_id and scene in the drawer.

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

### Selection Bridge (director picks)

```
GET  /api/selection         -> {asset_ids[], assets[], count, set_at, set_by, folder}
POST /api/selection         -> body: {asset_ids: [1,2,3]}   (agents: rarely needed)
DELETE /api/selection       -> clear
```

When the director clicks a card in the dashboard, the selection updates. Read it to know what they're looking at.

### Open in Finder/Explorer

```
POST /api/assets/{id}/open  -> reveals the file in Finder (macOS) or Explorer (Windows)
```

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

## Debug endpoints

```
GET /api/debug/orphans          -> {zero_byte_rows[], missing_file_rows[]}
GET /api/debug/recent-events    -> [...last N audit events...]
  ?n=20
```

---

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

---

## Replaces the old viewer

The old `viewer.py` on port 8766 is deprecated. All references to port 8766, `image_outputs.json`, or `POST /api/add` are legacy. This gallery (port 8770) is the canonical system.

## Known gaps

- **No auto-start.** Server must be started manually each session. No launchd/systemd service yet.
- **Cross-platform ref paths.** If image-chef on Mac writes refs with `/Users/ayo/...` paths, they won't resolve when viewed on Windows (and vice versa). The gallery still shows the asset but ref previews break. Workaround: set `VC_REF_ALLOW_ROOTS` env var to include the local equivalent path.
- **Scanner backfill for shot_id** (issue #2) is not yet implemented. Shot IDs are only populated when: (a) the wrapper/draft sets one explicitly, or (b) you edit it manually in the drawer. A regex-based auto-tagger from filenames (e.g. `SH450_kling_v1.mp4` -> `SH450`) would cover legacy assets.

## Server not running?

If `curl http://127.0.0.1:8770/healthz` fails with connection refused, start the server (see "Start the server" above).
