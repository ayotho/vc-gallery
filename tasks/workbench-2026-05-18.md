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

## GitHub bugs fixed tonight

- [x] #48 — Stacks sort by newest member (fe52baf) — stacks sort by max(first_seen_at) of members
- [x] #46 — Hero promote cascade (fe52baf) — auto-demotes prior heroes for same shot_id to accepted
- [x] #44 — Variant draft refs (fe52baf) — uses asset.refs (original sources) not file_path (output)
- [x] #47 — Select All Visible (527b857) — Cmd+A, stack expansion at dispatch, partial-load disclosure
- [x] #7 — Compare mode (closed, shipped earlier)
- [x] #24 — Skill split (closed, /select + /vc-gallery)
- [x] Stacking append bug (581fda0) — renderGrid always re-groups when stacking on
- [x] Counter accuracy — shows visible stacks count with "(N cards)" suffix

## Bugs found + fixed

- Stacking append: `renderGrid(append=true)` skipped grouping. Fix: always re-group full items array, force `append=false` when stacking.
- Counter mismatch: showed total items not visible stacks. Fix: use `_renderItems.length` with suffix.
- Hero pile-up: no cascade on promote. Fix: demote prior heroes in same transaction.
- Variant i2i loop: dupe-draft used output as ref. Fix: use `asset.refs` (original sources).
- Stack sort: new variants orphaned at top. Fix: sort stacks by newest member timestamp.
- Select All partial load: no disclosure. Fix: show "(of M loaded)" when not all fetched.

## Next up (cron picks from here)

- [ ] Code review pass: read 200 lines of HTML, look for null refs, race conditions, missing error handling
- [ ] Code review pass: read 200 lines of serve.py, same checks
- [ ] Test visual stacking with real data: manually set shot_ids on a few assets via PATCH, verify grouping renders
- [ ] Test Select All + bulk reject: verify stack expansion sends all member IDs
- [ ] Test drawer version strip: open a stacked card, verify siblings show, Compare All works
- [ ] Test drag-to-inherit: drag card onto another, verify shot_id copies, grid re-groups
- [ ] Test hero cascade: set hero on one card, verify prior hero demotes
- [ ] Verify Cmd+A doesn't fire when typing in search box
- [ ] Set up Playwright e2e test suite (playwright 1.57.0 already installed)
- [ ] Playwright: test grid renders cards, stacking toggle groups them
- [ ] Playwright: test drag-to-inherit (drag card A onto B, verify toast + re-group)
- [ ] Playwright: test Cmd+A select all, verify multi-select bar count
- [ ] Playwright: test drawer version strip renders siblings on stacked card click
- [ ] Playwright: screenshot each state for visual regression baseline

## Parked (not building yet)

- Auto-rename on drag-to-stack — parked until stacking used in real sessions
- HF metadata pull (issue #43) — manual trigger, build after current batch settles
- Crown hero auto-classify (Phase 2) — pick hero, siblings auto-alternate
- Prompt diff view (Phase 3) — show prompt evolution across versions

## Cron log

| Time | Action | Result |
|------|--------|--------|
