---
name: select
description: "Read the director's current selection from the VC Gallery. Use when: the user says 'look at this', 'the selected one', '/select', '/select 2095', or references what they're looking at in the gallery dashboard."
---

# /select — Selection Bridge

Read what the director is currently looking at in the VC Gallery dashboard (localhost:8770).

## When to invoke

| Trigger | Action |
|---------|--------|
| `/select` or "look at this", "the selected one" | Read selection |
| `/select 2095` or `#2095` | Fetch specific asset by ID |

## API

```
GET  /api/selection         -> {asset_ids[], assets[], count, set_at, set_by, folder}
POST /api/selection         -> body: {asset_ids: [1,2,3]}   (agents: rarely needed)
DELETE /api/selection       -> clear
```

When the director clicks a card in the dashboard, the selection updates automatically. Read it to know what they're looking at. Multi-select (Ctrl+click) sends multiple asset_ids.

## Fetch a specific asset

```
GET /api/assets/{id}        -> full asset detail + prompt + refs_resolved + review_history
```

## Reading assets visually

- **Images:** use the Read tool on `file_path` to see the image (Claude has vision)
- **Videos:** can't visually inspect. Report `width x height`, `duration_sec`, `filename`. Extract a frame: `ffmpeg -ss 2 -i <path> -frames:v 1 /tmp/frame.png` then Read that

---

For the full VC Gallery API (browsing, drafts, fires, stacking, scenes, compare), see `/vc-gallery`.
