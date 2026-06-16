# VC Gallery — project guide

The VC Gallery is the **organization & review layer** for AI-generated assets (images + videos) across every client project. Runs at `http://127.0.0.1:8770/`, supervised by launchd (`com.visualchef.vc-gallery`).

## Current role (since 2026-06-16) — ORGANIZE FIRST, don't fire

Director's directive: the gallery is for **viewing, organizing, and triaging** — quickly seeing which assets are accepted vs not, grouping by shot/scene, tagging. **Generation now happens via the Higgsfield MCP**, not the gallery's CLI fire path.

- ✅ USE the gallery for: browse/search, accept/reject triage (Review Board), versions, scenes, drafts-as-organization, element/ref display.
- ⛔ DON'T route new generations through the gallery's CLI wrapper. The fire path (`/api/draft/{id}/fire` → `hf_gen_with_sidecar.py`) is **shelved** — too many schema/PATH failures ate the director's flow. Generate via the **Higgsfield MCP** instead.
- The CLI firing code stays in the repo but is **back-burner**. Don't invest in fixing CLI-fire bugs (PATH spawn, gp2 `batch_size`, `t2v` coercion) unless the director revives that path.

**Execution = Higgsfield MCP.** "The MCP" means the connected Higgsfield MCP server (`mcp__737cd1ae-…`: `generate_video`, `generate_image`, `show_reference_elements`, `list_workspaces`, etc.). All generation triggers go there.

**Reversibility (the director may change his mind — keep this in mind):** firing is *shelved, not deleted*. Keep the fire code path intact and keep re-enabling it a small, clean toggle. Do NOT rip out the fire endpoints/wrappers or make the shelving hard to undo. This is a deliberate "risk reversal" — the gallery may resume firing later.

## Dependency-update rule (REQUIRED)

When you change the gallery (server, HTML, API, schema, behavior), in the **same change** update every dependent surface so nothing drifts:

- `.claude/skills/vc-gallery/SKILL.md` — the API + workflow reference agents load. Keep it in lockstep with the server's real routes/params.
- Any docs/plans describing the changed behavior (`ROADMAP.md`, `REVIEW_BOARD_PLAN.md`, etc.).
- This `CLAUDE.md` if the role/architecture changed.

A gallery change that leaves the skill stale is **incomplete**. One source of truth per fact; everything else points to it.

## How it runs (operational safety)

- launchd service `com.visualchef.vc-gallery` (KeepAlive). Restart with `launchctl kickstart -k gui/$(id -u)/com.visualchef.vc-gallery` — **never** kill+nohup.
- Boots `vc_gallery_serve.py` + `visual_chef_gallery.html` **from this working tree** → whatever branch is checked out + a restart = what goes live. **Confirm you're not dropping features before restarting** (check served HTML size, feature files on disk).
- Working folder persists in `~/.config/visual_chef/server.json`; read `/healthz` for the live folder before any restart.

## "Make canon" rule

"Make canon" = promote the **FULL working branch as-is** (it already contains the new feature on top of everything else). **NEVER** rebuild `main` from a bare base keeping only one feature's commits — that strips the director's accumulated work from the live gallery. (Near-miss on 2026-06-16.)

## Key files

- `vc_gallery_serve.py` — server + API
- `visual_chef_gallery.html` — single-page UI
- `vc_gallery_scan.py` / `vc_gallery_lib.py` — scanner + helpers
- `.claude/skills/vc-gallery/SKILL.md` — agent-facing API + workflow reference
