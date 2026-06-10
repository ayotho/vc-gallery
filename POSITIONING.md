# vc-gallery positioning — mission control, not a gen tool

*Locked 2026-06-10, director session. Context: Higgsfield Supercomputer launch (May 2026) — a cloud agent that plans, prompts, and generates better than any wrapper we maintain.*

---

## The one-liner

**vc-gallery is engine-agnostic mission control for AI video production. Generation is a plug, not the product.**

## Why

Generation is the commodity layer. Higgsfield Supercomputer, fal, Kling — funded platforms racing each other on model quality and prompting. Any "gen feature" inside vc-gallery is a thin wrapper around someone else's model, and it depreciates every month as the platforms' own agents out-prompt ours.

What nobody else has:

- **Whole-episode awareness.** Cloud agents do one brief per chat. vc-gallery knows SH010–SH450, which shots are accepted, which need refires, the cref lineage.
- **Review at volume.** Triage of 200 b-roll candidates is a director problem no chat UI solves. Local gallery + compare mode + keyboard triage is the right interface; a chat thread is the worst one.
- **The local delivery pipeline.** Files live on the machine and flow into Premiere, editor handoff, client folders. Cloud agents end at the download button.

## The value chain, and who owns each layer

```
   PLAN      shots · scenes · coverage tracking · "what's still missing"   ← OURS
     ▼
   FIRE      batch gens across whichever engine is best right now          ← COMMODITY (keep thin + swappable)
     ▼
   REVIEW    see 200 outputs fast · compare · versions · keepers           ← OURS
     ▼
   DELIVER   rename · organize · Premiere/editor handoff                   ← OURS
```

## What this means for the backlog

1. **Weight features toward PLAN / REVIEW / DELIVER.** Review speed (triage, compare, keeper workflows), planning (shot/scene state, coverage rollups), delivery (handoff automation).
2. **Keep FIRE thin and swappable.** Engines are plugins: Higgsfield wrapper, fal wrapper, and — if Supercomputer exposes an API — a third plug. Their improvements become our free upgrades, not our competition.
3. **Do not build prompt-improvement features into the gallery.** Prompting craft lives in the agents (image-chef / video-chef) and increasingly in the platforms. Outsource it.
4. **When triaging an issue, ask: which layer is this?** FIRE-layer polish loses to any PLAN/REVIEW/DELIVER item of similar size.

## Relationship to ROADMAP.md

The roadmap already leans this way (v0.3 Obsidian bridge = PLAN/DELIVER, v0.4 review UX = REVIEW, v0.5 cross-app bridges = DELIVER). This doc is the tie-breaker when new ideas land: gen-layer ideas need an unusually strong case; orchestration-layer ideas are on-thesis by default.
