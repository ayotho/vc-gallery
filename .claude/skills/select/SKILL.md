---
name: select
description: Bridge between vc-gallery dashboard (localhost:8770) and this Claude session. Lets the director say "look at this", "the selected one", "use these N", or quote an asset ID like "#2095" — Claude reads the active selection from /api/selection and Reads the asset file(s) so it can see images/videos without the director copy-pasting paths. Use whenever the director references something they're looking at in the gallery, OR explicitly says /select, OR mentions an asset ID like #2095, OR uses plural pronouns ("these", "those", "the selected ones") that imply a current gallery pick.
---

# /select — selection bridge to the vc-gallery gallery

The director runs the vc-gallery dashboard at `http://127.0.0.1:8770/`. When they click a card (single-select) or Cmd-click multiple cards (multi-select), the dashboard mirrors that selection to the server. This skill reads it.

## When to invoke

- User types `/select` (with or without an arg) → always
- User says "look at this", "the selected one", "what I'm looking at", "the one I'm on" → fetch single
- User says "use these", "use those N", "compare these", "what about these" → fetch multi
- User quotes an asset ID like `#2095`, `asset 2095`, `id 2095` → fetch that ID specifically
- User says "show me SH450" — first try the search endpoint with `q=SH450`, then `/select N` on the best hit

If selection is empty AND no ID was quoted, tell the director: "Nothing selected in the canvas. Click an asset in the gallery (or Cmd-click multiple) and try again." Do not guess; do not use last-known.

## Server contract

```bash
# Read current selection (could be 0, 1, or many assets)
curl -s http://127.0.0.1:8770/api/selection
# → { folder, set_at, set_by, count, asset_ids: [N,...], assets: [...full detail...] }

# Read a specific asset by ID (when director quoted one)
curl -s http://127.0.0.1:8770/api/assets/2095
# → full detail dict
```

Each asset in the response carries:

| Field | Why it matters |
|---|---|
| `id` | The director's "#N" reference |
| `file_path` | Absolute path on disk — pass to the **Read tool** to actually see the image/video |
| `filename` | Display name |
| `status` | review / hero / accepted / revise / rejected / alternate / draft / legacy |
| `shot_id` | "SH450" etc. — links to narrative context in shot cards |
| `prompt` | Original gen prompt if it was wrapper-fired (may be empty for raw drops or legacy wiped rows) |
| `refs_resolved[]` | Each ref carries `{filename, url, exists}` — the input images/refs used to generate this asset |
| `hf_url` | Higgsfield job URL if applicable |
| `width`, `height`, `duration_sec` | Native dimensions |
| `media_type` | "image" or "video" — drives whether you Read it as vision or just inspect metadata |

## Standard flow

1. **Curl `/api/selection`** (or `/api/assets/<id>` if user quoted one).
2. **Branch on count**:
   - 0 → empty-selection message, stop.
   - 1 → single asset detail; Read the `file_path` so you can SEE the image (videos: Read the path to get metadata, but acknowledge you can't visually inspect frames — note duration + dimensions instead).
   - 2+ → enumerate all. For each, present the headline (`#id · shot_id · filename · status`) and a one-line summary. Then ask the director what they want to do with the set — or if their prompt already implied an action ("use these as refs", "compare these"), just do it.
3. **Always cite asset IDs** in your response so the director can pivot ("ok now /select 2050").
4. **Surface broken state** clearly:
   - If `prompt` is empty: say "no prompt recorded (raw drop or legacy)".
   - If `refs_resolved` is empty AND the file looks generated: say "no refs in DB (pre-2026-05-14 wrapper data was wiped by an old scanner bug — see issue #19)".
   - If `file_path` doesn't exist on disk when you try to Read it: surface the path mismatch.

## Args

| Form | Behavior |
|---|---|
| `/select` | Use current `/api/selection` |
| `/select 2095` | Fetch `/api/assets/2095` directly (ignores current selection) |
| `/select SH450` | Best-effort: hit `/api/assets?q=SH450&limit=10`, present top hit + offer to switch to another |
| `/select selection` | Synonym for plain `/select` |
| `/select clear` | DELETE `/api/selection` to clear server-side selection (rarely needed) |

## Examples

**Director says** "look at this":
1. `curl /api/selection` → `count: 1, assets: [{id: 2095, file_path: "/Users/ayo/Desktop/Client/Dave/BTW/EP8/SH1140_kling_apartment_v1.mp4", status: "review", ...}]`
2. Acknowledge with the headline: "Looking at **#2095** — `SH1140_kling_apartment_v1.mp4`, status: review, 1920×1080 video, ~8s, fired via kling3_0."
3. If it's an image, Read the file_path so you can describe what's in it. If video, note you can't visually process video frames here.
4. Wait for their actual question / next directive.

**Director says** "use these as refs for a variant":
1. `curl /api/selection` → `count: 3, assets: [...]`
2. List the 3 with IDs + filenames + which are stills vs videos.
3. Hand off to image-chef or video-chef as appropriate, passing the absolute paths as the refs for the new generation.

**Director says** "#2095" with no other context:
1. Treat as `/select 2095`.
2. Fetch + summarize. Wait for their next prompt.

## Hard rules

- **Never invent a path.** The server's `/api/assets/<id>` returns the canonical `file_path`. Use it verbatim with the Read tool.
- **Never assume the selection** when the user is silent. If `count == 0`, ask them to select something first.
- **Never write back** to the selection slot via `POST /api/selection` unless the user explicitly says "select assets X, Y, Z for me". This skill is read-only by default; the dashboard is the source of selection truth.
- **The Read tool sees images natively** (Claude's vision); use it to actually look at PNG/JPG content. For MP4/MOV, Read returns binary you can't visually inspect — instead, list the dimensions + duration + frame-count + filename and propose extracting a frame if the user needs visual inspection (`ffmpeg -ss <t> -i <path> -frames:v 1 /tmp/frame.png`).

## Caveats

- The vc-gallery server must be running at `127.0.0.1:8770`. If `curl` returns a connection-refused, ask the director to start the server (`cd "/Users/ayo/Coding projects/vc-gallery" && python3 vc_gallery_serve.py`).
- Selection is **per-folder**. If the director switched gallery folders mid-conversation, the selection auto-clears.
- Selection is **ephemeral**. If the server restarted, the selection is empty. Director re-clicks to restore.
- For the ~600 legacy wrapper-fired assets whose prompts were wiped by the 2026-05-13 scanner bug, `prompt` and `refs_resolved` come back empty. Note this explicitly when surfacing them; don't pretend the data is there.
