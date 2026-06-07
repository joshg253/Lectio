# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **Tuning tab restructure + strat compare improvements** — strat compare grid moved to top of Tuning tab; **Webcomic** and **Artwork** strategies added to the comparison; per-strategy title/alt text rows now appear below the grid (not inside each card); strategy cards use friendly labels ("Feed content", "Source page", etc.); strategy dropdown options include descriptions; ↺ Auto button styled to match the Refresh button.
- **Alt/title in strat cards + caption source picker** — strat cards now show raw `title` and `alt` attribute text below each image. Two checkboxes (Title / Alt) + "↺ Auto" in Tuning let the user pre-select which attribute(s) to show as the entry caption. Text is pre-loaded during refresh (no pop-in). `entry_lead_images` and `feed_strategy_cache` gained `image_title` column; `feed_display_prefs` gained `caption_source`.

## Up next

- **Better tuning / live preview** — full entry preview pane with swappable strategy and display settings side by side. Goal: see exactly what an entry looks like under different combinations (strategy × show-in-article × caption mode) without saving. Probably a modal or split-pane triggered from Feed Properties Tuning tab.
- **Performance investigation** — systematic baseline before enabling multi-user. Capture per-request breakdown (DB time, enrich time, refresh contention) under realistic load (concurrent page loads + active refresh cycle). Identify whether bottleneck is SQLite write contention, thumbnail enrichment, or network. Also covers: user leaving webapp open on main PC and accessing from phone/tablet browser — ensure the long-poll/SSE connection degrades gracefully and the session stays usable across network switches.
- **FRB remaining gaps**:
  - *Persistent failure alerting* — feeds with ~30+ consecutive failures (or `feed.last_exception` set in the reader DB) should surface a prominent ⚠ warning badge in the feed list and full error details in Feed Properties. Add a "Pause updates" toggle (stop fetching without unsubscribing — don't lose the feed). Add a "Change URL" field in Properties for www→non-www or other redirect fixes. Auto-disable is too blunt; alert first, let the user act.
  - *Adaptive polling / feed TTL hints* — honor `ttl`, `skipHours`, `skipDays`, `sy:updatePeriod` from feed XML as scheduling hints. Feeds that rarely update should be polled less frequently.
  - *Per-folder refresh cadence* — let the user assign a custom refresh interval to a folder (e.g. "Status/News" every 5–10 min, "Comics" every 2 h). Feeds that don't support WebSub still benefit from targeted fast polling without hammering servers that only publish once a day. The global interval remains the default floor.
- **Resurface / GUID-churn suppression** — ~~moved to backburner~~ done: URL-slug matching implemented. Title+date matching (for feeds that change both GUID and URL) remains as a possible follow-up if needed.

## Backburner

- **Feed type strategy profiles** — tiered override system: (1) generic engine (safe defaults, no assumptions); (2) type-profile layer for known feed categories (webcomic, art/illustration, photography — each with preferred strategy order, caption behavior, thumb sizing); (3) per-feed override layer on top. "Plugins" in Lectio = reader plugins (`lead_image_plugins.py`); the type profiles would be implemented as configurable plugin presets rather than hard-coded special cases. GitHub app-release feeds are a specific case: default to no thumbnail/favicon, use the generated changelog image already present at the top of the entry.
- **WebSub (PubSubHubbub) push support** — feeds that publish a `<link rel="hub">` header can push new entries in real-time without polling. Implementation: (1) on feed add/refresh, discover hub URL and POST a subscription with callback `{lectio_url}/websub/callback`; (2) `GET /websub/callback` handles hub challenge-response; (3) `POST /websub/callback` processes pushed content immediately like a single-feed refresh; (4) `websub_subscriptions` table tracks hub, lease expiry, HMAC secret; background job renews before expiry. Lectio is already publicly accessible via Traefik so the callback URL requirement is met. Estimated coverage: ~30–50% of feeds (WordPress.com, Medium, YouTube, Mastodon, major CMSes). Feeds without WebSub fall back to scheduled polling. Reader library has no built-in WebSub plugin — needs custom implementation.
- **GReader-compatible API** — implement the Google Reader / Fever API subset so existing Android RSS reader apps (FeedMe, Reeder, etc.) can use Lectio as their backend without a custom client. Scope: mark-read sync, unread counts, feed list, entry fetch. Multi-user/auth is a prerequisite.
- **Python file header comments** — several `.py` files lack a top-of-file description docstring; standardize across the codebase (one-paragraph summary of what the module does and its role in the architecture).
- **Resurface / GUID-churn suppression** — publishers sometimes change entry GUIDs (CMS migrations, permalink changes, plugin rebuilds), causing batches of already-read articles to reappear as new. Mitigation: when a new entry arrives whose title + approximate date matches a known read entry in the same feed, auto-mark it read. Overlaps with cross-feed dedup (slug/title matching). Distinct from `updated`-timestamp changes, which don't affect read state because the GUID is unchanged.
- Per-user vs. shared thumb cache (only relevant if multi-user is added).
- Archive caps for starred entries.
- Multi-user support / auth refactor — performance investigation first.
- YunoHost or other packaging.
- PWA / offline-first features.
