# VC Gallery — Review Board (Kanban) — Build Plan & Pre-Mortem

> Locked plan for the new **Board** view. Built HTML-only, fully reversible.
> Source: deep-map + pre-mortem workflow (6 agents) + director brief, 2026-06-05.

---

## What it is (plain English)

A **Kanban "review board"** — a new tab in VC Gallery next to Grid / Segments / Shots.
Every shot is a card. Columns are your review states. **Changing a status slides the card
to another column — it never disappears.** That one behaviour kills the four pains:

| Pain today | On the board |
|---|---|
| Change status → card vanishes, lose the overview | Card moves columns, stays on screen · live per-column counts |
| 3 siblings → pick hero, reject rest, clumsy | Siblings sit adjacent with a shot tag · **▶ Play all** opens the existing side-by-side player · hero one, reject others — all stay visible |
| Mark revise → card switches out → comment elsewhere | Drop into **Revise** → comment box **autofocuses on the card** → type, it stays put |
| No single overview | All shots visible at once, sorted into columns |

Plus **multi-select**: drag a marquee box across empty space to grab 3–4 cards (or Shift/Cmd-click),
then bulk-move or compare them — reusing the selection engine the app already has.

---

## Decisions made (you were away — all reversible)

| Decision | Choice | Why | Revert |
|---|---|---|---|
| Which tab hosts it | **Revive the dead `board` tab** (not `shots`) | `board` is an empty stub — zero risk to a working view; correct name for a Kanban | delete 1 button + 1 div + 1 render fn |
| Columns | **Review → Revise → Accepted → Hero → Rejected** (+ collapsed More; Draft/Firing excluded) | the active triage loop; draft/firing have no media to review | one array constant |
| Write path | **PATCH `/api/assets/{id}`** (never `/reviews`) | verified: PATCH already logs the review row + runs hero-demotion; `/reviews` would double-write & desync | one call site |
| In-column layout | **Group-by-shot + shot tag** (not full swimlanes) | dozens of shots would make swimlanes dozens of empty rows | sort flat |
| **Marquee multi-select** | **IN — ~92% confidence** | app drags cards via native HTML5 DnD; a pointer-marquee that starts only on empty background can't collide. Feeds existing `state.multiSelection` | remove one mousedown handler + overlay div |

---

## Hard guarantees

- **HTML-only** — one file (`visual_chef_gallery.html`). Isolated diff, no backend touch, can't tangle with the uncommitted fal work in `serve.py`.
- **No restart, no interruption** — server reads the HTML per request; changes go live on a browser refresh.
- **Reuse, not rebuild** — same data, filters, stacking, compare, status/notes APIs, multi-select set, hotkeys.
- **Manual = agent parity** — drag / click / hotkey and the agent all funnel through the same PATCH endpoint.
- **Reversible** — additive surface: 1 button + 1 root + `renderKanban` + 1 guarded `patchAsset` branch + 1 factored helper. Delete them → today's app, untouched.

---

## Pre-mortem — risk register (condensed; full detail in build brief)

**Critical (build-breakers, mitigations locked):**
- `R-WRITE-PATH` — must use PATCH, never `/reviews` (double-write/desync). → grep diff for `/reviews` = 0.
- `R-NO-RELOCATE` — board branch must *move* the card to the new column, not replace-in-place. → repro step 4.
- `R-BREAK-GRID-PATCH` — the non-board `patchAsset` branch must stay byte-identical. → repro step 10.

**Silent failures (look done, aren't):**
- `R-OPTIMISTIC-NO-REVERT` — server fail leaves card in wrong column → capture prev position, revert + toast on catch.
- `R-PERF-2000-CAP` — folders >2000 assets silently truncate → fetch limit=2000 + "showing first 2000" banner.
- `R-HERO-CASCADE-DESYNC` — crowning a hero demotes another server-side → re-partition the shot on-board after a hero move.
- `R-SKILL-DRIFT` — repo SKILL.md (10KB, stale) vs live (15KB) → reconcile toward the repo on ship.

**High / Low:** drag-listener loss (bind per-column, not per-card) · missing `preventDefault` on dragover · status-filter leak into board fetch (blank status) · scroll-reset · iPad touch (drawer-pill fallback) · self-drop no-op. All mitigated in the build brief.

---

## Repro test (acceptance — must all pass before ship)

1. Open Board → columns appear with counts; all shots visible, siblings adjacent.
2. Set a model/scene filter → board honours it, all columns still populate.
3. Drag Review→Accepted → card **moves** (doesn't vanish), counts update, PATCH fires; repeat ×3 (drag still works).
4. Hotkey `h` (hero) → card to Hero; prior hero sibling leaves Hero (cascade reflected).
5. Drop into Revise → comment box autofocuses; type + Cmd-Enter → saved, card stays.
6. ▶ Play all on a 3-sibling shot → existing compare overlay, solo-audio works.
7. Marquee-drag across 3–4 cards → they select (multi-select bar shows count); bulk-move works.
8. Copy revise queue → clipboard = `shot_id — comment` lines = `GET /api/assets?status=revise`.
9. Agent PATCH a card's status → board reflects it after refresh (parity).
10. **Regression:** grid view + status filter + change status → card still disappears in grid (grid unchanged).
11. `git diff` → only `visual_chef_gallery.html` changed.

---

## Cost & touchpoints

- **Cost:** ~$0 — code only, no generation spend.
- **Last director touchpoint before autonomous finish:** none required. Verified live + adversarially reviewed before ship.
- **Done signal:** DM to director (`D083022RMCM`) with PR link, how-to, repro, one-line revert.

---

*Build brief (full spec + line anchors) handed to the builder. Workflow output archived at `/tmp/wf1_result.json` for this session.*
