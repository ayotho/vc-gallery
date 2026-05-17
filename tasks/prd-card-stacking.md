# PRD: Card Stacking / Version Grouping

## Introduction

Frame.io-style version stacking for the Visual Chef Gallery. Multiple cards (versions of the same shot) get grouped into a visual stack. Clicking a stack fans out all versions. Agents auto-stack when iterating on a shot. Fired versions inherit properties (shot_id, scene, model, etc.) from the parent card.

This replaces the approach in issue #41 (single-card version history with an `asset_versions` table). Instead of hiding versions inside one card, we stack multiple real cards together. Each version stays a full asset with its own status, reviews, and metadata. The stack is a lightweight grouping layer on top.

## Goals

- Let the director visually group related cards (versions of the same shot) into a single stack
- Let agents auto-stack iterations when firing new versions of an existing shot
- Show stacks collapsed by default (one cover card with a version count badge)
- Fan out all versions inline when a stack is clicked
- Inherit shot_id, scene, client, project, and other properties from parent when firing a new version
- Keep every version as a full independent asset (own status, reviews, score, notes)

## User Stories

### US-001: Add stacks table to database
**Description:** As a developer, I need a `stacks` table and a `stack_id` column on `assets` so cards can be grouped.

**Acceptance Criteria:**
- [ ] New `stacks` table: `id INTEGER PRIMARY KEY`, `name TEXT`, `cover_asset_id INTEGER` (nullable, FK to assets), `created_at REAL`, `created_by TEXT` (director/agent)
- [ ] New nullable column `stack_id INTEGER` on `assets` table (FK to stacks)
- [ ] New nullable column `stack_position INTEGER` on `assets` (ordering within stack, 1 = oldest)
- [ ] Index on `assets(stack_id)`
- [ ] Migration is idempotent (runs cleanly on fresh DB and existing DB)
- [ ] Existing assets unaffected (stack_id defaults NULL = not stacked)

### US-002: Stack creation via drag-and-drop
**Description:** As the director, I want to drag one card onto another to create a version stack, the same way I'd group layers or clips in an editor.

**Acceptance Criteria:**
- [ ] Cards are draggable in gallery grid view (HTML5 drag, long-press on mobile)
- [ ] Dragging card A onto card B creates a new stack containing both
- [ ] If card B is already in a stack, card A joins that existing stack
- [ ] Visual feedback during drag: target card highlights with a "drop to stack" glow/border
- [ ] Stack position auto-assigned by first_seen_at order (oldest = 1)
- [ ] Cover card defaults to: hero/accepted version if one exists, otherwise the latest card
- [ ] POST /api/stacks endpoint accepts `{ asset_ids: [int, ...] }`
- [ ] Returns the new stack object with all member assets
- [ ] Verify in browser

### US-003: Stack creation via multi-select
**Description:** As the director, I want to select multiple cards and stack them in one action, for when drag-and-drop is cumbersome (e.g. cards far apart in the grid).

**Acceptance Criteria:**
- [ ] When 2+ cards are multi-selected (existing Ctrl/Shift+click), a "Stack" button appears in the bulk action bar
- [ ] Clicking "Stack" groups all selected cards into a new stack
- [ ] Reuses existing multi-select infrastructure (no new selection UI)
- [ ] Cover card auto-selected: hero > accepted > latest
- [ ] Verify in browser

### US-004: Stack creation (agent auto-stack on fire)
**Description:** As an agent, when I fire a new version of an existing shot, I want it auto-stacked with the parent so version history stays linked.

**Acceptance Criteria:**
- [ ] POST /api/draft accepts optional `parent_asset_id` field (this is where the asset row is created, so inheritance happens here)
- [ ] When `parent_asset_id` is provided and the parent has a stack, the new asset joins that stack
- [ ] When `parent_asset_id` is provided and the parent has NO stack, a new stack is created containing parent + new asset
- [ ] New asset inherits `shot_id`, `scene`, `client`, `project` from parent
- [ ] New asset gets `stack_position` = max position in stack + 1
- [ ] Stack cover auto-updates to hero/accepted if one exists, otherwise latest
- [ ] Without `parent_asset_id`, fire proceeds normally (no stacking)

### US-005: Collapsed stack display in gallery
**Description:** As a user, I want stacked cards to appear as a single card with a version badge so the gallery isn't cluttered with iterations.

**Acceptance Criteria:**
- [ ] Stacked cards show as one card (the cover card) with a badge showing version count (e.g. "v3" or "3 versions")
- [ ] Slight visual offset/shadow to indicate depth (cards behind the cover)
- [ ] Cover card shows its own status color as normal
- [ ] Filtering still works: if a filter matches ANY card in the stack, the stack appears
- [ ] Sort uses the cover card's properties
- [ ] Verify in browser

### US-006: Expand stack for version comparison
**Description:** As the director, I want to click a stack and see all versions laid out for easy comparison so I can pick the best one and promote it.

**Acceptance Criteria:**
- [ ] Clicking the version badge (or a dedicated expand button) opens a comparison view
- [ ] All versions shown as a horizontal strip, ordered oldest → newest (left → right)
- [ ] Each version card shows its thumbnail, status badge, model name, and relative timestamp
- [ ] Versions are large enough to visually compare (not tiny thumbnails)
- [ ] Director can change status of any individual version from the expanded view
- [ ] Director can set any version as the stack cover (pin)
- [ ] Keyboard nav: arrow keys to move between versions, Escape to close
- [ ] Click collapse button or press Escape to return to normal gallery
- [ ] Verify in browser

### US-007: Manage stack membership
**Description:** As the director, I want to add cards to an existing stack, remove cards from a stack, or unstack entirely.

**Acceptance Criteria:**
- [ ] Drag a card onto a stack to add it (or use context menu "Add to stack")
- [ ] Right-click a card in an expanded stack to "Remove from stack"
- [ ] Removing the last 2nd card auto-dissolves the stack (1 card = no stack)
- [ ] "Unstack" action on the stack badge dissolves the entire stack
- [ ] PATCH /api/stacks/{id} for adding/removing members
- [ ] DELETE /api/stacks/{id} to dissolve
- [ ] Verify in browser

### US-008: Pin cover card
**Description:** As the director, I want to pin a specific version as the stack's cover card, overriding the auto-selection.

**Acceptance Criteria:**
- [ ] Right-click a card in expanded stack -> "Set as cover"
- [ ] Updates `stacks.cover_asset_id` to that card
- [ ] Pinned cover persists even when new versions are added
- [ ] If pinned card is removed from stack, cover falls back to auto-select (hero > accepted > latest)
- [ ] Verify in browser

### US-009: Stack-aware API responses
**Description:** As an agent, I need the asset listing API to return stack information so I can understand version relationships.

**Acceptance Criteria:**
- [ ] GET /api/assets includes `stack_id` and `stack_position` in each asset object
- [ ] New query param `?group_stacks=true` collapses stacked assets, returning only cover cards with a `stack_count` field
- [ ] GET /api/stacks/{id} returns full stack detail: all member assets ordered by position
- [ ] GET /api/stacks lists all stacks (with cover card info and member count)
- [ ] Agents can query "all versions of shot X" via `?shot_id=X` which returns all stack members ungrouped

### US-010: Stack display in Scene Overview
**Description:** As a user, I want stacks to appear correctly in the Scene Overview grouped view.

**Acceptance Criteria:**
- [ ] Scene overview shows stacked cards as collapsed stacks (same badge + depth visual)
- [ ] Expanding a stack works the same as in gallery grid view
- [ ] Stack count contributes to scene card counts correctly (stack of 3 = 1 visual card, not 3)
- [ ] Verify in browser

### US-011: Stack-aware filtering
**Description:** As a user, I want filters to work sensibly with stacks so searching still finds what I need.

**Acceptance Criteria:**
- [ ] Status filter: a stack appears if ANY member matches the filter status
- [ ] Scene/shot filter: stacks filter by the cover card's scene/shot_id
- [ ] Search: matches against any member's properties (filename, notes, model)
- [ ] Filter results show collapsed stacks, expandable inline
- [ ] Verify in browser

## Functional Requirements

- FR-1: Add `stacks` table (id, name, cover_asset_id, created_at, created_by) and `stack_id` + `stack_position` columns on `assets`
- FR-2: POST /api/stacks creates a new stack from a list of asset IDs
- FR-3: POST /api/stacks/{id}/add and POST /api/stacks/{id}/remove for membership changes
- FR-4: DELETE /api/stacks/{id} dissolves a stack (assets become standalone, not deleted)
- FR-5: GET /api/stacks and GET /api/stacks/{id} for listing and detail
- FR-6: Drag-and-drop stacking: HTML5 drag on cards, drop target creates or extends a stack
- FR-7: Multi-select + "Stack" button in bulk action bar as alternate creation path
- FR-8: POST /api/draft accepts optional `parent_asset_id` for auto-stacking on fire
- FR-9: New assets created via `parent_asset_id` inherit shot_id, scene, client, project from parent (NOT model/workflow)
- FR-10: Cover card auto-selection: pinned > hero > accepted > latest. Director can pin override.
- FR-11: Hero promotion auto-updates cover (unless explicit pin exists)
- FR-12: Gallery grid renders stacks as single card with "Nv" badge and depth shadow
- FR-13: Clicking a stack opens horizontal comparison strip with full-size version cards
- FR-14: Keyboard nav in expanded view (arrows, Escape to close)
- FR-15: GET /api/assets supports `?group_stacks=true` to collapse stacked assets in API
- FR-16: Status filter on collapsed stacks: stack appears if ANY member matches
- FR-17: Bulk status change on collapsed stack applies to cover only
- FR-18: Scene overview treats stacks as single visual units
- FR-19: All stack operations logged in reviews table for audit trail
- FR-20: Stacks auto-dissolve when reduced to 0 or 1 member

## Non-Goals

- No drag-to-reorder within a stack (position is chronological)
- No nested stacks (a stack cannot contain another stack)
- No automatic shot_id-based stacking of existing cards (only on new fires with parent_asset_id, or manual grouping)
- No diff/comparison view between versions (future feature)
- No version annotations or "what changed" metadata (cards already have notes for that)
- No cross-project stacking (stacks exist within one episode/folder DB)

## Design Considerations

- **Stack badge:** top-right corner of the card, small pill shape (e.g. "3v"), same style as status badges
- **Depth effect:** 2-3px offset shadow behind the cover card, slightly rotated cards peeking out (like a deck of photos)
- **Drag feedback:** dragged card goes semi-transparent, target card gets a glowing border + "Drop to stack" label
- **Expanded view:** horizontal comparison strip spanning the full grid width, cards sized for actual visual comparison (not thumbnails). Shared background color to group them. Scroll horizontally if > 5 versions.
- **Collapse button:** small chevron or "x" at the top-right of the expanded strip
- **Multi-select stacking:** reuses existing Ctrl/Shift+click selection + "Stack" button in the bulk action bar (already built for bulk status changes)
- **Mobile/touch:** long-press to initiate drag. Multi-select + Stack button as fallback.

## Technical Considerations

- `stacks` table is lightweight. A stack is just an ID + cover pointer. All the real data lives on the assets.
- `stack_id` on assets is a simple FK. NULL = not stacked. Query is just `WHERE stack_id = ?`.
- Cover card logic: `SELECT id FROM assets WHERE stack_id = ? ORDER BY CASE status WHEN 'hero' THEN 1 WHEN 'accepted' THEN 2 ELSE 3 END, first_seen_at DESC LIMIT 1`
- Gallery query with `group_stacks=true`: use a CTE or subquery to pick one representative per stack, plus all non-stacked assets.
- Property inheritance on fire: server-side copy of fields from parent asset row before insert.
- No changes to the file watcher. Stacking is purely a DB-level grouping. Files on disk are unchanged.
- The existing `_list_assets()` fast path (no prompt JOIN) should stay fast. Stack grouping adds a lightweight post-processing step, not a JOIN.

## Success Metrics

- Director can stack 2+ cards in under 3 clicks
- Agents auto-stack iterations without any extra manual grouping
- Gallery with 50+ versions of the same shot looks clean (one stack, not 50 cards)
- Expanding a stack shows all versions instantly (no extra API call or page load)
- No performance regression on galleries with 500+ assets

## Resolved Decisions

- **Bulk status on collapsed stack:** apply to cover only. Expand first if you want to act on individuals.
- **Deleting a stacked card:** no warning needed. Stacks auto-dissolve at 1 member.
- **Inherit model/workflow from parent:** NO. Agent specifies its own model and workflow. Only inherit shot_id, scene, client, project (the "what", not the "how").
- **Version badge format:** "3v" (count only). Simple, no position tracking needed on the badge.
- **Hero promotion = auto-cover:** YES. When a stacked card gets promoted to hero, it auto-becomes the cover (unless director has explicitly pinned a different card).

## Open Questions

- Max stack size? Probably unlimited, but expanded view might need horizontal scroll at 15+ versions.
- Should the stack name auto-generate from shot_id/scene, or stay unnamed by default?
