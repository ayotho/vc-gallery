# fal engine — morning report

**Date:** 2026-06-02 · **Branch:** `feat/fal-engine-kling-o3` (committed locally, **not pushed**)

## What you asked for vs what's done

fal.ai is now a **second video engine** in the gallery, sitting next to Higgsfield. You switch between them just by **picking the model in the dropdown** — nothing about Higgsfield changed.

The model wired up: **fal · Kling O3 Pro — reference to video**. It takes **reference images only** (1–4 of them) plus a prompt — no "start frame". The references are used as references (you point at them in the prompt with `@Image1`, `@Image2`, …), they are *not* secretly turned into a first/last frame.

## The 12 test videos — all on the board now

Open the gallery → they're grouped under scene **`fal_test`**, organized by shot number:

```
SH000   ✓  one reference, no audio          (the basics)
SH0030  ✓  one reference, audio ON          (native sound works)
SH3000  ✓  TWO references (@Image1/@Image2) (multiple images)
SH3030  ✓  THREE references                 (multiple images)
SH3033  ✓  FOUR references (the max)        (multiple images)
SH0033  ✓  a 4K still as the reference      (auto-shrunk to fit, see note)
SH0300  ✓✓✓ quantity = 3  → 3 videos        (count works)
SH0303  ✓✓  quantity = 2, two refs → 2 videos
SH0099  ✓  the 12th, fired by the agent
SH0098  ▢  STAGED, not fired — for YOU to click Fire in Chrome
```

Every finished video: lands in the gallery, gets the **correct name** (e.g. `SH0300_klingo3_v1_1.mp4`, `_2`, `_3`), flips from **draft → review**, is tagged as **fal**, and keeps the **source link** to the file. Quantity videos stack together under one shot.

## The two ways to fire it — both proven

- **Agent / API:** all 12 above were fired through the gallery's own fire button endpoint — that's the same path an AI agent uses. ✓
- **Human / click:** the Fire button in the drawer calls that exact same endpoint. To see it with your own eyes, open the gallery, find **SH0098** (it's staged and waiting), and click **Fire**. It'll produce a 13th video the same way.

## Cost

~**$4.50** for the 12 test videos (short 3s clips, mostly audio-off) + ~$0.34 for the earlier single smoke test. Normal use ≈ $1.12 per 8-second clip with audio.

## Notes worth knowing

- **10 MB reference limit:** fal rejects reference images over 10 MB, and your 4K stills are ~30 MB. The wrapper now **auto-shrinks** any oversized reference before sending — you don't have to do anything. (This was caught by the smoke test before any money was spent.)
- **Login:** uses your saved `FAL_KEY`. If the server ever starts without it, the wrapper now reads it from your env file automatically.
- The skill **`/vc-gallery`** is updated with a new "Engines" section explaining all of this.

## Housekeeping

- Code is committed on a branch but **not pushed / no PR** — that's waiting for your go.
- The 12 test videos live in your real EP2 board under scene `fal_test` so you can see them. They're clearly test shots (SH000 / SH3000 / etc.) — easy to bulk-delete when you're done reviewing.
- An overnight QA loop is running to keep stress-testing and hunting bugs (separate doc: `FAL_QA_LOG.md`).
