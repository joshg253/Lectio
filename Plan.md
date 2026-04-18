# Lectio Plan

## Purpose
Living backlog and staging area for future work. Use for feature ideas, deferred items, prioritization, and "add this later" requests.

## Current Status
**Strong foundation already built:**
- 3-pane desktop UI (Folders/Tags → Posts → Post View)  
- Working 1-pane mobile drill-in (select folder → posts → post)
- Folder/feed CRUD + orphan handling
- Read/unread + bulk actions (folder/feed/range)
- Saved/starred + filtering
- Manual tagging w/ suggestions + tag counts
- Sorting/filtering (read-state, published/received, direction)
- Manual + scheduled refresh
- Source/readability views
- OPML import/export
- UX hardening (context menus, async races, viewport clamping)

## Priority Buckets
- **Now**: Active/polished work
- **Soon**: Next after current
- **Later**: Desirable, not urgent  
- **Maybe**: Speculative/revisit

## Now
- Stronger archive/saved views
- View state persistence hardening

## Completed Recently
- Keyboard shortcut baseline finalized for now (navigation/actions, source view toggles, refresh, add feed/tags, Escape close behavior)
- 1-pane/tablet polish: state consistency + feedback visibility
- Unread/all persistence consistency across folder/scope navigation
- Active state styling fix for Saved Items vs All Feeds navigation (CSS color + JavaScript star_only logic)

## Soon
- Per-feed preferences (refresh interval, readability default, sort)
- Clarify/complete "Stronger archive/saved views" scope
- View state persistence hardening (durable preferences across restarts)

## Later
- Rules engine (keyword/author auto-tag/mark-read/highlight)
- Keyword highlighters + smart folders
- Web scraping/non-RSS monitoring
- Read-later and sharing integrations (for example Instapaper save)
- VPS deployment hardening (auth, reverse proxy, sessions)
- Docker packaging

## Maybe
- YunoHost packaging
- Multi-user support
- Richer plugin system
- Mobile web PWA features
