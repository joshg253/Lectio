# Lectio Plan

## Purpose
Living backlog and staging area for future work. Use for feature ideas, deferred items, prioritization, and "add this later" requests.

## Current Status
**Strong foundation already built:**
- 3-pane desktop UI (Folders/Tags → Posts → Post View)  
- Working 1-pane mobile drill-in (select folder → posts → post), with state consistency + feedback visibility on tablet/mobile
- Folder/feed CRUD + orphan handling
- Read/unread + bulk actions (folder/feed/range)
- Saved/starred + filtering, with consistent active-state styling across Saved Items / All Feeds navigation
- Manual tagging w/ suggestions + tag counts
- Sorting/filtering (read-state, published/received, direction)
- Persistent filter/sort state across folder/feed/tag/search navigation (unread/all + star)
- Comprehensive keyboard navigation/actions (`j`/`k`, `n`/`p`, `m`, `f`/`s`, `b`/`o`, `w`/`v`, `a`, `d`, `r`, `t`, `/`, `Escape`)
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
_(empty — pull from Soon)_

## Completed Recently
- Pre-VPS prep batch:
  - Flipped `LECTIO_DEBUG` / `LECTIO_REFRESH_DEBUG` defaults from `1` to `0` so VPS deploys are safe-by-default; local dev (`make run`, VS Code launch config) already passes them explicitly.
  - Added `GET /healthz` endpoint (DB ping; auth-exempt) for Traefik probes.
  - Added long-lived `Cache-Control` headers on `/static/*` via subclassed `StaticFiles` (safe: `STATIC_ASSET_VERSION` query param invalidates on changes).
  - Confirmed all `/debug/*` endpoints already gate on `DEBUG_MODE` — audit done, no changes needed beyond the default flip.
  - Fixed 2 pre-existing failing tests in `test_feed_refresh_service.py` (fixture was missing the `domain_failure_state` table).
- Pinch zoom on mobile entry pane: removed custom handler in favor of native browser pinch (was blocked by `touch-action: pan-y` on `.pane-entry`)
- Star status persistence bug: FormData was captured after the optimistic flip of `savedInput.value`, so the server received the opposite value — fixed in both entry-pane and post-list save toggles
- Active post tile auto-scrolls into view in the post list when navigating between entries (j/k, click, etc.)

## Soon
- Clarify/complete "Stronger archive/saved views" scope
- View state persistence hardening (durable preferences across restarts)
- Topbar: additional action buttons (beyond current set)
- Entry header: additional metadata/actions beyond title/feed
- More feed-specific display tweaks (webcomics, etc.)

### Pre-VPS hardening (gate items for exposing Lectio on the public internet)
- **Login brute-force protection**: per-IP rate limit on `POST /login` (e.g. 5 failures / 5 min). Bypass when `LECTIO_DEBUG=1` so dev iteration isn't blocked.
- **CSRF protection** on all state-changing POST endpoints (`/entries/saved`, `/entries/read`, `/entries/tags`, folder/feed CRUD, refresh, OPML import). Thread a token through Jinja forms and the SPA async submit handlers. Largest implementation lift of this batch; transparent in normal use once wired.
- **SSRF guardrail** on user-triggered URL fetches: block private IP ranges (10/8, 172.16/12, 192.168/16, 127/8, link-local, IPv6 equivalents) in lead-image source scraping and the WordPress/Penny Arcade plugin og:image fetches. Bypass when `LECTIO_DEBUG=1` for LAN test feeds.
- **SQLite backup strategy**: documented procedure including `.sqlite-wal`/`-shm`; ideally a scheduled `VACUUM INTO` to a backup directory.
- **Persistent logging**: add a rotating file handler alongside stdout so post-mortem is possible after VPS issues.
- **Graceful shutdown**: wait for in-flight refresh jobs to finish on SIGTERM.
- **Deploy guide**: required/optional env vars, Traefik labels example, first-run bootstrap (create user, import OPML).

- **VPS deployment**: roll out to existing Traefik setup once the items above land.

## Later
- Rules engine (keyword/author auto-tag/mark-read/highlight)
- Keyword highlighters + smart folders
- Web scraping/non-RSS monitoring
- Read-later and sharing integrations (Instapaper save, Pocket, Fediverse, etc.)
- Per-feed preferences (refresh interval, readability default, sort)
- Cloudflare free-tier integrations: Workers (e.g. lightweight proxy/cache layer), R2, or Cache API where useful

## Maybe
- Docker packaging
- YunoHost packaging
- Multi-user support (starts after basic auth lands; auth refactor is the gate)
- Richer plugin system
- Mobile web PWA features
- Cloudflare Tunnel for VPS ingress (avoids open port, free tier)
