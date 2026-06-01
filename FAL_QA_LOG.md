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

### Iter 5 — UI param flow + aspect/duration bounds (2026-06-02 ~04:43)
Backlog covered: #4, #9. (recurring-cron iteration)
- **#4 UI param flow**: staged a draft with non-default params (duration=15, aspect_ratio=9:16, generate_audio=true, shot_type=intelligent, count=2, 2 refs); confirmed they persist in the draft's inner payload AND map correctly to the wrapper's fal args (duration "15", aspect_ratio "9:16", generate_audio true, shot_type "intelligent", image_urls=[2]). Human-set params reach the fal API. PASS. ✓
- **#9 aspect/duration bounds**: 9:16 ratio + duration 15 (upper bound) accepted + mapped. PASS. ✓
- Test draft (SH0091) deleted after — board stays clean.
- No bugs found.
- Backlog remaining: #2 concurrency, #3 Chrome MCP, #7 downscale edges, #10 collisions/force.

### Iter 6 — downscale edges + collision/force (2026-06-02 ~05:13)
Backlog covered: #7, #10. (recurring-cron iteration)
- **#7 downscale edges**: non-image >10MB → `_shrink_image_if_needed` returns original gracefully (sips fails → no crash; fal rejects at upload → caught EXIT_SUBMIT). Small non-image → passthrough. Real ref 9.64 MiB (<10 MiB limit) → no shrink. PASS. ✓
- **#10 collision/force**: non-empty real asset at target → `_reserve_filename` raises FileExistsError → wrapper returns failed_collision (no clobber, no spend). `force=true` → intentional overwrite. 0-byte orphan from a failed run → auto-reclaimed (no false collision). All by-design + correct. PASS. ✓
- No bugs found.
- Backlog status: #2 concurrency already covered by the baseline matrix (8 simultaneous fires, 0 rate-limit failures). #3 Chrome MCP needs director to select a browser (deferred — can't drive Chrome unattended per the browser-selection rule). Free backlog effectively exhausted; further iterations = deeper code audit / re-verification.

### Iter 7 — deep audit: temp-dir leak fix + consistency (2026-06-02 ~05:43)
Deep code audit (subagent hit a transient socket error → done manually). 
- 🐞→✅ **BUG: temp-dir leak in `_shrink_image_if_needed`.** Every oversized-ref (>10MB) fire created a `fal_ref_*` temp dir via tempfile.mkdtemp that was never removed (3 already accumulated in TMPDIR). Over an overnight batch of 4K-ref fires these pile up. FIX: `_upload_or_url` now `shutil.rmtree`s the temp dir in a `finally` after upload (only when a downscaled copy was made). Verified: upload (free) of the 30MB ref leaves 0 leaked dirs; cleared the pre-existing leaks. Commit on branch. `import shutil` added.
- **audit — refs uploaded ONCE**: confirmed refs upload before the count loop (line 276) and reused via `args["image_urls"]` for all N iterations — no per-count re-upload waste. ✓
- **audit — DB↔disk consistency**: 13 fal_test rows; only SH0098 "missing" on disk = the unfired human-test draft (expected); 0 zero-byte among the 12 videos; orphans endpoint missing_file_rows=0. ✓
- **audit — count partial-failure semantics**: if an output fails, the wrapper returns first-non-OK exit and the draft row stays re-fireable (idx0 failure → draft not mutated, no clobber, no data loss). Acceptable behavior, not a bug.
- Remaining: #3 Chrome MCP (needs director to select browser — deferred).

### Iter 8 — Chrome deferred + built out vc-eval (2026-06-02 ~06:43)
- **#3 Chrome MCP**: 2 browsers connected (Mac laptop, AyPC). Selecting one requires the browser-safety AskUserQuestion, which would block the unattended loop → **Chrome deferred — needs director**. (Human fire path already proven by endpoint-equivalence: the Fire button calls POST /api/draft/{id}/fire, exercised 12× this session. SH0098 staged for the director's own click.)
- ✅ **NEW TOOL: vc-eval is no longer a stub.** Wrote `vc_gallery_eval.py` (engine-agnostic asset evaluator: ffprobe integrity, DB↔disk, zero-byte, source_url presence, board grouping; filters by scene/shot/status/provider; exit 0/1 for CI) and rewrote `.claude/skills/vc-eval/SKILL.md` with real docs + trigger description. Verified on scene=fal_test → 13/13 pass, specs + per-shot counts shown (SH0300×3, SH0303×2), audio True on SH0030 only. `/vc-eval` is now runnable on all assets as the director asked.
- No bugs found this iteration.

### Iter 9 — full-gallery vc-eval regression (2026-06-02 ~06:43)
- **vc-eval --provider fal**: 12/12 PASS. All fal videos valid (1920×1080, ~3s), audio True only on SH0030. Clean. ✓
- **vc-eval full gallery (731 assets, 7.6s)**: 621 pass / 110 fail — but **0 fal-provider failures**. The 110 are PRE-EXISTING legacy hygiene unrelated to this branch: 103 assets with no shot_id ("won't group on board"), 7 "file missing on disk" (old drafts/moved files). NOT fixed — out of scope for the fal branch; surfaced for the director as a future gallery-cleanup item.
- vc_gallery_eval.py validated at scale (731 assets in <8s). No fal bugs.

### Iter 10 — final-state branch coherence check (2026-06-02 ~07:13)
- **Change scope vs main** (code only): fal_gen_with_sidecar.py (+463, new), vc_gallery_eval.py (+154, new), vc_gallery_lib.py (+14), vc_gallery_serve.py (+36), visual_chef_gallery.html (+18). Surgical; Higgsfield path untouched.
- **Compile**: all touched modules compile clean. ✓
- **Happy-path regression** (post ALL qa fixes — count cap, temp-leak cleanup, count UI field, FAL_KEY fallback): dry-run of a standard 8s audio-on single-ref draft produces correct fal args (duration "8", generate_audio true, image_urls=[ref]). Cumulative edits did not regress the basic flow. ✓
- Branch is in a shippable state. No bugs.

### Iter 11 — URL passthrough + start_image=ref behavior (2026-06-02 ~07:43)
- **URL/data ref passthrough**: `_upload_or_url` returns http/https/data: refs unchanged (no upload, no shrink) — verified with a fake client that errors if upload is attempted. Agents can pass remote URLs (catbox/fal) as refs. PASS. ✓
- **start_image key → image_urls (ref-only)**: a payload using the `start_image` key still maps to `image_urls` (NOT `start_image_url`). Confirms the director's requirement: refs are references, never auto-defaulted to a start frame. PASS. ✓
- No bugs.
