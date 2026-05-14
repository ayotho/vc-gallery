---
name: Bug
about: Something is broken — write it so a fresh agent can reproduce and fix it cold
title: '[BUG] '
labels: bug
assignees: ''
---

## Symptom (what happened)
_2–4 sentences in plain English. What did you see go wrong? Quote the director if it was a correction._

## Session context
- **Session JSONL:** `~/.claude/projects/<slug>/*.jsonl`
- **Date:**
- **Agent that was running:** (image-chef / video-chef / direct-chat / wrapper)
- **Sub-agent IDs (if any):**

## Exact files & paths involved
_Absolute paths only — no "the gallery folder."_

## Reproducible commands
```bash
# the exact bash/curl/sql that triggered the bug
```

## State snapshot
_Actual DB rows, filesystem listings, JSON outputs at the moment of the bug. Trim noise, keep signal._

## Manual recovery (if you did any)
_The exact commands that worked. Often doubles as the spec for the proper fix._

## Schema / API / environment constraints
_Relevant column types, UNIQUE keys, endpoint signatures, Python version, shell, paths. Anything the future agent can't infer from filenames alone._

## Likely culprit code
_Best guess at which file / function / endpoint contains the bug. Don't fix from the issue — just point at the zip code._

## Repro test
_Concrete steps to verify the fix. "Do X, Y, Z. Expected: 1 row. Buggy: 4 rows."_

## Sample IDs / fixtures
_If the bug used specific data (asset UUIDs, shot IDs, session IDs), list them — gives the fixer a known-good test case._
