# VC Gallery Workbench — 2026-05-18

Overnight QA + bug fix tracker. Cron runs every 40 min.

## Features shipped today

- [x] Latest Only toggle (CTE with ROW_NUMBER)
- [x] Shots view (group by shot_id tab)
- [x] Compare mode (2-6 cards side-by-side overlay)
- [x] Drag-to-inherit (POST /api/assets/{id}/inherit)
- [x] Visual stacking (client-side grouping by shot_id in grid)
- [x] Stack version badges + shadow depth + attention dots
- [x] Drawer version strip + Compare All button
- [x] Stack versions toggle (replaces Latest Only, default ON)
- [x] Backend: limit cap 2000, updated_after filter
- [x] Skill split: /select (slim) + /vc-gallery (full)

## QA pass 1 (completed)

- [x] latest_per_shot — all edge cases pass
- [x] group_by=shot — correct empty response when no shot_ids
- [x] compare — 2-ID, 1-ID, 7-ID, bad IDs, missing param, garbage all handled
- [x] inherit — missing source, bad target, no-op, self-inherit all pass
- [x] facets + filters — cross-filter works, SQL injection blocked
- [x] Code review — no logic bugs found

## GitHub bugs to fix tonight

- [ ] #48 — Stacks sort by newest member (new variant pulls stack to top)
- [ ] #46 — Hero promote doesn't demote prior hero (multiple heroes per shot)
- [ ] #44 — Variant draft uses output as ref instead of original source refs
- [ ] Stacking append bug — infinite scroll appends bypass grouping, flat cards appear below stacks

## Bugs found

- Stacking append bug: `renderGrid(append=true)` skips grouping (line 1577: `!append` guard), appended cards render flat

## Bugs fixed

_(none yet)_

## Parked (not building yet)

- Auto-rename on drag-to-stack — parked until stacking used in real sessions
- HF metadata pull (issue #43) — manual trigger, build after current batch settles
- Crown hero auto-classify (Phase 2) — pick hero, siblings auto-alternate
- Prompt diff view (Phase 3) — show prompt evolution across versions

## Cron log

| Time | Action | Result |
|------|--------|--------|
