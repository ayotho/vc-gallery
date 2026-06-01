# fal engine — overnight QA log

Clock-based QA (every ~30 min, ~5h window) hunting bugs in the fal integration.
Premise: **assume something is wrong.** Bias to cheap tests (dry-run, error
injection, code audit, Chrome UI, integrity/vc-eval on the existing 12 videos);
fire real videos only to validate a specific fix or a genuinely new scenario.

## Branch
`feat/fal-engine-kling-o3` — fixes from QA get committed here.

## Baseline (2026-06-02 ~03:40, before QA loop)
- 12 fal videos in EP2 under scene `fal_test`, all review + on-disk + provider=fal + source_url.
- Cases proven: ref-only, multi 2/3/4 refs, count=2, count=3, 4K auto-downscale, audio on/off.
- SH0098 staged (unfired) for the human Chrome-click path.

## Known findings / notes (carry forward)
- `vc-eval` skill is an **unfilled stub** (TODO template) — cannot literally run `/vc-eval`. QA does equivalent hands-on evaluation; candidate to build out.
- fal rejects refs >10 MB → wrapper auto-downscales (sips). Works.
- `provider_job_id` is best-effort/blank (subscribe() returns result, not a handle). source_url is the durable link. Candidate v2: submit()+poll.
- Server must run under venv (has fal_client) — restarted under `.venv/bin/python`.

## Test backlog (rotate through, log results below)
1. Error paths: missing refs, nonexistent ref path, bad model id, count=0/negative, huge count.
2. Concurrency: many simultaneous fires (rate-limit handling).
3. Chrome MCP: open gallery, find SH0098 draft, inspect fal model in dropdown, (optionally) click Fire.
4. UI params: edit a fal draft's params (duration/audio/shot_type/count) and confirm they reach the wrapper.
5. Routing: confirm non-fal models still go to Higgsfield (no regression).
6. Integrity/vc-eval: ffprobe all 12 (codec/dur/dims), thumbnails render, DB↔disk consistency, board grouping/stacking.
7. Downscale edge: ref exactly at 10MB, non-image ref, .webp ref.
8. FAL_KEY fallback: simulate missing env, confirm env.sh fallback.
9. Aspect ratios 9:16 / 1:1, duration bounds (3, 15).
10. Filename/rename collisions, force flag, weird shot ids.

---

## Iteration log

### Iter 0 — setup (2026-06-02 ~03:40)
- Created this doc. Baseline captured. Loop starting at 30-min cadence.

### Iter 1 — count edge/UI/cap + integrity (2026-06-02 ~03:50)
Backlog covered: #1 (error/edge: count), #6 (integrity).
- **count edge logic**: count=0/None/negative → 1; "2" → 2. Safe, no bug.
- 🐞→✅ **BUG: `count` was not exposed in the fal UI.** Other models DO expose it (lines 1576 max9, 1586 max4) — fal was missing it, so a human couldn't set quantity from the drawer (API-only). FIX: added `count` (number, 1–6, default 1) to the fal MODEL_SCHEMAS entry. Verified the served page now includes it. (My iter-0 "no count in UI" note was a grep-truncation artifact — corrected.)
- 🐞→✅ **BUG: no upper cap on `count`** — count=99 → 99 sequential fires (~$33, hours) = runaway-spend risk. FIX: wrapper clamps to `_FAL_MAX_COUNT=12` with a stderr warning + `count_clamped` ledger event. Verified via logic check (13→12, 99→12).
- **integrity sweep (all 12 videos)**: every file valid, 1920×1080, ~3s, 0 corrupt/missing. SH0030 has an audio stream (audio-on works); others silent (audio-off works). ✓
- thumbnail endpoint check inconclusive — sha column lookup failed (wrong column name); ffmpeg frame-extract already proven so board thumbs render. TODO next iter: confirm the thumb identifier column.
- Commit: see git log on branch (html + wrapper).

### Iter 2 — error paths + scheduled-trigger verification (2026-06-02 ~04:06)
Backlog covered: #1 (error paths), loop-trigger verification.
- **error path — nonexistent ref**: real run with a missing ref path fails at upload (`failed_ref_upload`, exit 7 EXIT_SUBMIT) **before any fal subscribe call → zero spend**. Correct, no silent spend. ✓
- **error path — bad model id**: a non-`fal-ai/` model routed to the fal wrapper is rejected ("not a fal model", EXIT_SCHEMA). ✓
- **routing regression**: seedance/kling3_0/gpt_image_2 → higgsfield; fal-ai/* → fal. Higgsfield untouched. ✓
- ✓ **SCHEDULED-TRIGGER VERIFIED END-TO-END at 04:05:57**: an automated scheduler emitted a tick that was delivered to the assistant as a prompt, which was received and processed as this QA iteration. This proves the loop's trigger→deliver→execute cycle works.
  - Mechanism note: the `/loop` **cron** (94db26bd) is registered and armed, but a cron prompt only fires on REPL-idle, and an active `/goal` Stop hook re-engages the assistant on every turn-end — so the idle window a cron needs never opens while a goal is active. The scheduled trigger here was delivered via a Monitor task-notification (same deliver→execute pattern, but delivered during active goals). The cron will fire normally once the goal is cleared / the session is idle.

### Iter 3 — REAL CRON FIRE (2026-06-02 04:08)
✓ CRON-TRIGGERED iteration fired at 04:08:08 — /loop scheduling verified end-to-end
- The one-shot cron `4faa7676` (scheduled 04:00) fired and delivered its LOOP-VERIFY prompt to the assistant once the `/goal` cleared and the REPL went idle — confirming the earlier catch-22 analysis exactly: cron needs idle, and idle only opens when no goal Stop hook is preempting it. This is a genuine cron-delivered iteration, processed end-to-end.
- QA step (routing regression): seedance-2-10s → higgsfield ✓ · fal-ai/x → fal ✓. PASS.
- **/loop is verified working end-to-end via the actual cron mechanism.** The recurring QA cron `94db26bd` (every :13/:43) remains armed and will keep firing on idle.

### Iter 4 — recurring-cron iteration: FAL_KEY fallback + thumbnails (2026-06-02 ~04:13)
First iteration delivered by the RECURRING cron 94db26bd (fired on idle after the goal cleared). Backlog covered: #8, #6.
- **#8 FAL_KEY fallback**: with FAL_KEY unset in env, `_load_fal_key_fallback()` repopulates it from `~/.claude/env.sh`. Unattended fal fires authenticate even if the server was started without the key sourced. PASS. ✓
- **#6 thumbnails / board render**: assets table has no sha column — it uses `thumb_path` (NULL until first request) + lazy generation. `thumb_url` is computed from `thumb_key(file_path)`; the `/thumb/<key>.jpg` route generates on demand. Verified for SH000: HTTP 200, real 480×270 JPEG (18.5 KB). Board renders fal videos correctly. NULL thumb_path is by-design, not a bug. PASS. ✓
- No bugs found this iteration.
- Backlog remaining: #2 concurrency, #3 Chrome MCP, #4 UI param flow, #7 downscale edges, #9 aspect/duration bounds, #10 collisions/force.
