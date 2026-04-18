# Lectio Architecture

## Overview
Lectio follows a **local-first, single-user RSS reader** model built around the `reader` Python library with a clear 3-layer conceptual split that can deploy to VPS later without rewrite.

## Layering
UI/API Layer      → web routes, handlers, presentation state
     ↓
Services Layer    → feed ops, tagging, filtering, refresh, readability
     ↓  
Storage Layer     → `reader` DB + app-data/settings

Layers run in one process now but preserve boundaries for future deployment.

## Reader-First Philosophy
**`reader` is the storage/ops primitive.** It already handles:
- Feed retrieval/storage
- Read/important state  
- Arbitrary tags/metadata
- Filtering/search capabilities
- Statistics
- Plugin system

**Implementation order:**
1. `reader` API → primary path
2. `reader` plugin → for `reader`-adjacent behavior  
3. Custom app logic → only when necessary

**Never duplicate `reader` capabilities in app code.**

## Adaptive Layout Model
Lectio uses responsive/adaptive layouts rather than fixed 3-pane:
- **Wide** (desktop): 3-pane side-by-side
- **Medium** (tablet landscape): 2-pane (TBD: nav+detail or list+detail)
- **Narrow** (phone portrait): 1-pane drill-in navigation

**Priority**: 3-pane desktop + 1-pane mobile developed in near tandem, 2-pane as later refinement.

**Goal**: Fast triage workflow, not "always show 3 panes."

## View State Model
Three distinct categories that must stay separate:

| Type                               | Examples                                    | Persistence             | Notes                 |
| ---------------------------------- | ------------------------------------------- | ----------------------- | --------------------- |
| **Remembered base preferences**    | sort mode/dir, default filters, pane sizing | Durable across restarts | User explicitly chose |
| **Contextual temporary overrides** | tag-click shows "All", search results       | Session/context only    | Navigation semantics  |
| **Transient navigation state**     | current entry, scroll pos, focus            | Ephemeral               | Safe to lose          |

**Rules:**
- Temporary overrides **never** silently overwrite remembered preferences
- Leaving override context → restore remembered base preference  
- Refresh/redirect/async must not promote temporary state to remembered

## Data Boundaries
- **Runtime settings**: config/env driven only
- **Mutable state**: app-data path abstraction
- **Development data**: disposable now

## Deployment Path
1. Local-first single-user (current)
2. VPS deployment w/ basic auth + reverse proxy  
3. Docker (optional)
4. YunoHost (optional)

**Today**: Skip auth complexity. **Before VPS**: Add basic auth + secure sessions.

## Extension Strategy
Non-native behavior → plugin/adapter style over hardwired branching. Keeps features replaceable.

## Security Direction
No-login local-first → basic auth before VPS exposure. Avoid assumptions that make auth invasive later.

## UX Constraint
Architecture exists to enable fast triage workflow, not fight it.
