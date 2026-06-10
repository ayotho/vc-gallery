# vc-gallery roadmap

The plan, phased. Each item below maps to a GitHub issue + milestone — `gh issue list --milestone "v0.3 — Obsidian Bridge MVP"` shows the live state.

**Guiding principle:** the AI agents (image-chef, video-chef) do most of the volume work. Features serve the agent first, the director second. Same plumbing, two consumers.

**Positioning (tie-breaker for new ideas):** see [POSITIONING.md](POSITIONING.md) — vc-gallery is engine-agnostic mission control (PLAN / REVIEW / DELIVER). Generation is a thin, swappable plug, not the product.

---

## v0.3 — Obsidian Bridge MVP

The bridge between vc-gallery (asset truth) and the Obsidian vault (narrative truth). After this lands, every wrapper landing auto-embeds into the right shot card with zero director touch.

1. **Vault path resolver + `VC_OBSIDIAN_VAULT_ROOT` env var** — server learns where the vault is + how to resolve per-client/project paths.
2. **Scanner: backfill `shot_id` from filename regex** — unlocks every other bridge feature for the existing 2089-asset corpus.
3. **`POST /api/obsidian/embed`** — idempotent append of `![[filename]]` to a shot card under a section; flock-safe.
4. **`GET /api/obsidian/shot-card?shot_id=…`** — parsed read so agents can check brief/refs before generating.
5. **`GET /api/obsidian/shot-cards?project=…`** — episode rollup; lets agents answer "what's still unshot."
6. **Hero-promote cascade** — status change in canvas auto-moves the embed line from `## Variants` → `## Hero` in the shot card.

## v0.4 — Daily Review UX

Compounding flow wins for the director's review loop. Bounded value each, but each one ships in <100 lines.

7. **Compare mode (split-view drawer)** — Cmd+click second card → A/B side-by-side; status buttons mark the focused side.
8. **Duplicate-to-draft** — new draft seeded with source asset's prompt + refs. Turns 5-minute variant flow into one click.
9. **Keyboard shortcut overlay** — `?` shows A/H/V/R, Esc, /, Space, J/K. Surfaces features that already exist but are invisible.
10. **Active filter breadcrumb + clear-all** — orientation chip at top of canvas; one-click filter reset.
11. **"What's new since" chip** — `+12 since 14:30 · view` filters to recently-landed assets.
12. **Bulk "mark visible as X"** — confirm-prompt button in canvas head; applies status to all filtered.

## v0.5 — Cross-app bridges

Connect canvas to the other tools the director uses daily: Figma, Claude Desktop, GitHub, Obsidian (text-side).

13. **Drag-and-drop multi-format** — single `dragstart` sets URL + path + wikilink + DownloadURL. Modifier keys pick the format: plain=file (Figma/Claude), Cmd=wikilink (Obsidian), Shift=path (terminal).
14. **`/log-issue` skill** — chat → GitHub issue. Auto-grabs session ID, recent file paths, last error from logs; drafts body per backlog discipline; posts via `gh issue create`.
15. **Trace ID per fire (causation chain)** — every mutating request stamps a `trace_id` on its events. `GET /api/trace/<id>` returns the full chain. Debug "what happened during that fire" in one query.

## v0.6 — Tech debt

Audit findings from the Plan agent (post-stress-test 2026-05-14). Hardening, performance, data correctness.

16. **H3 sort=shot index optimization + H4 fire registry persistence** — index-friendly ORDER BY; `fires.jsonl` replay on server boot reaps dead PIDs and unlinks orphan tmp payload files.
17. **M2 eta model matching, M4 scanner→obs_mod, M5 thumb table-scan** — explicit-prefix eta dict; route scan logs through `obs_mod.record_event`; denormalize `thumb_path`.
18. **M6: status flip only after wrapper succeeds** — drafts stay `'draft'` until `exit_code == 0`; failed fires don't pollute the review queue.
19. **Ref propagation for new Kling/Seedance fires** — wrapper writes `refs_json` to the prompts table on success (currently skipped for some workflows; ~70% of new gens have no refs visible in the drawer).

---

## Logging an issue from chat

```
"log this as an issue: <symptom>"
```
or use the planned `/log-issue` skill (v0.5 #14). Either path produces an issue that follows the backlog-discipline template at `.github/ISSUE_TEMPLATE/bug.md` — fresh-agent-cold-start ready.

## Conventions

- **Labels:** `feature`, `bug`, `tech-debt`, `security`, `obsidian-bridge`, `agent-api`, `ux`, `p0`/`p1`/`p2`
- **Milestones** = phases. Issues assigned to exactly one milestone.
- **Issue bodies** follow the mandatory blocks in `.github/ISSUE_TEMPLATE/{bug,feature}.md`. Sparse one-line entries get bounced and rewritten.
- **Commits referencing issues** use `Fixes #N` / `Closes #N` / `Refs #N` so GitHub auto-links + auto-closes on merge.
