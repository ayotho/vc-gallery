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
