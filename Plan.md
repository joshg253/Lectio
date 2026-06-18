# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **feed_strategy_cache / feed_display_prefs fresh-DB migration fix** — the
  `image_alt`/`image_title` and `caption_source` columns were added by ALTERs
  that ran before their table's `CREATE`, so a brand-new meta DB never got them
  and `get_feed_properties` raised `no such column: image_alt`. Folded the
  columns into the base `CREATE` (idempotent ALTERs kept for existing DBs).
  Test: `tests/integration/test_meta_schema_migrations.py`.
- **rule_run_log nightly prune fixed** — the 90-day prune in
  `_daily_maintenance_for_user` queried a misnamed `ran_at` column and compared
  the ISO-text `run_at` against an int epoch, so it always raised, was swallowed,
  and the log grew unbounded. Now compares against an ISO cutoff on `run_at`.
  Test: `tests/integration/test_maintenance_prune.py`.
- **Inline YouTube player fixed** — the embed set `enablejsapi=1` without an
  `origin=` parameter, which YouTube now refuses to play. Nothing in the app
  drives the IFrame JS API, so the embed now uses YouTube's canonical markup
  (`youtube-nocookie.com` host, `referrerpolicy`, no `enablejsapi`) via a single
  `_youtube_embed_html` helper shared by both injection sites.
- **Tag filter fixed** — clicking a manual tag now surfaces every tagged entry,
  not just those inside the newest-N fetch window. The tag is pushed into
  reader's native `tags=` argument (SQL-side match across the whole library)
  instead of being post-filtered over the truncated page window, which had
  hidden tagged entries older than that window.
- **`/api/img` server-side cache** — the image proxy now caches fetched bytes in
  a global content-addressed store (`lectio_img_cache.sqlite`), downscaling to
  `LECTIO_IMG_CACHE_MAX_DIM` and evicting on a last-accessed TTL
  (`LECTIO_IMG_CACHE_DAYS`, 0 = unlimited) in daily maintenance. Tunable via env
  + Administration page; size surfaced in `/stats`.

## Up next

### Multi-user — tenancy seam + isolated mode

Design decision (see ARCHITECTURE.md "Multi-user tenancy"): tenancy is a
storage-layer strategy behind a resolver, so routes/services never learn which
mode is active. Ship the **isolated** mode now (DB-per-user); keep
content-addressed caches (thumbnails, image proxy, lead-image/strategy results)
**global** because they hold no per-user data. Defer the **shared-content** mode
until/unless real scale arrives — it becomes a storage swap behind the same
resolver, not a route rewrite.

Target scale now: 1–3 trusted users behind Cloudflare. Build the software to be
secure regardless; defer the SaaS-scale defenses (quotas, abuse) behind hooks.

Phasing:

1. ~~**Tenancy resolver + per-user connection pool**~~ — DONE (see Recently
   Completed). `services/tenancy.py` + per-(thread, user) pools in main.py.
2. ~~**Users table + per-user auth**~~ — DONE (core + account/admin UI at
   `/account`, linked from the main menu in multi mode). Remaining: optional
   user deletion (today: disable).
3. ~~**Per-user API tokens**~~ + ~~**per-user scheduled refresh**~~ — DONE.
   ~~**Startup backfills + starred-archive worker as default user**~~ — DONE.
   The startup tasks (scraped-feed sync, auto-taggers, guid-churn dedup, and the
   YouTube / lead-image / starred-archive / read-history backfills) now run once
   per enabled user via `_for_each_background_user`; the long-lived
   starred-archive worker scans every user's archive DB under its own context
   (injected `background_user_ids`); and the discover-on-subscribe thread spawned
   when a feed is added re-binds the requesting user. `LeadImageService`'s
   render-path source-image / alt-text fetch threads (`queue_source_fetch`,
   `queue_source_html_fetch`) likewise capture and re-bind the requesting user
   instead of persisting to the default tenant. Previously these ran as
   `DEFAULT_USER_ID` and wrote the legacy top-level DBs. New tests in
   `tests/services/test_starred_archive_tenancy.py` and
   `tests/services/test_lead_images_tenancy.py`.
   Remaining background work still running as the default user only (lower
   priority; scheduled refresh covers the feeds within the cadence window):
   - **WebSub push callback** — a push carries only a topic (feed URL); needs to
     find which users subscribe to it (across per-user `websub_subscriptions`)
     and refresh each. Until then a push refreshes only the default user; other
     users still get the content on their next scheduled pass.
   - ~~**Update scheduling policy**~~ — addressed for the current scale. Users
     are still refreshed sequentially within a tick (every user every tick, which
     is fine at 1–3 users), but `_rotate_for_fairness` now rotates the per-tick
     start user round-robin so there's no fixed first-mover bias and a slow/hung
     user delays a different set of downstream users each pass. Deeper fairness at
     real scale (per-user concurrency, fetch budgets) stays deferred behind this
     seam. Test: `tests/integration/test_scheduled_refresh_fairness.py`.
   - ~~**WebSub push callback**~~ — DONE. The shared callback carries only the
     topic, so both the verification GET and the content push now fan out across
     `_background_user_ids()`: verification confirms whichever user has a pending
     subscription, and a push refreshes every subscriber (after confirming
     authenticity against any one user's secret) under that user's context. Previously
     both ran as the empty default tenant, so no real user's WebSub worked.
     Tests: `tests/integration/test_websub_fanout.py`.
   - **Update scheduling policy** — revisit cadence/fairness across many users
     (currently each user is processed sequentially every poll tick).
4. ~~**SSRF hardening**~~ — DONE for the two directly-reachable proxies.
   `url_guard.safe_get` / `safe_get_async` follow redirects manually and
   re-validate every hop with `is_safe_outbound_url`; `/api/img` (auth-exempt!)
   and `/thumb` now use them with `follow_redirects=False`, closing the
   redirect-to-internal bypass. 18 new tests. Remaining hardening: (a) the
   service-layer fetches that still pass `follow_redirects=True` (lead-image /
   scraper / source-proxy in main.py + services) should adopt the same helpers;
   (b) full DNS-rebind closure needs connection IP-pinning (the validate→connect
   TOCTOU window is now small but nonzero) — deferred as lower-priority for the
   trusted-user threat model.
5. ~~**Data migration**~~ — DONE. `scripts/migrate_to_multiuser.py` copies the
   legacy DBs into `DATA_DIR/users/<user_id>/` (user_id resolved from the auth
   DB), dry-run default, reversible, integrity-checked. `--apply` run on the real
   data; **multi-user live since 2026-06-14** (see `docs/multiuser-migration.md`).

### Feed Properties — fetch history & automations

- ~~**Fetch-history tab**~~ — DONE. New `feed_fetch_history` table logs one row
  per non-skipped refresh attempt (status, HTTP status, new-entry count,
  duration, error) from `FeedRefreshService`; `get_feed_fetch_history` feeds the
  Feed Properties → **History** tab (All / New entries / Errors filters).
  Retention bounded in daily maintenance (`LECTIO_FETCH_HISTORY_KEEP` per feed,
  `LECTIO_FETCH_HISTORY_MAX_AGE_DAYS` age cap). Tests:
  `tests/integration/test_fetch_history.py`.
- **Automations-applied view** — show which automations (auto-taggers, dedup,
  strip rules, lead-image strategy overrides) have been applied to the feed, so
  the user can see what Lectio is doing to a feed's content without reading code.
- **Fetch-history tab** — add a tab (or tabs) in Feed Properties showing the
  feed's recent refresh/fetch history (timestamps, HTTP status, entries added,
  errors/backoff). Surfaces why a feed is stale or flagged problematic.
- ~~**Automations-applied view**~~ — DONE. Feed Properties → **Automations** tab
  shows the rules in effect for a feed (global / its folder / feed-scoped, from
  `highlight_keywords`) plus recent runs that touched its entries (from
  `rule_run_log_entries`). `collect_feed_automations` in main.py; tests in
  `tests/integration/test_feed_automations.py`.

### DeviantArt — remaining follow-ups

Phase 1 (public galleries via OAuth2 **client-credentials**, rendered to `file://`
feeds) and Phase 2 (the **authorization_code** flow: per-user connect/disconnect,
token refresh, watch-list → feeds sync (add-only), and push-galleries → DA watch
list) are both **done**. Follow-ups all resolved:
- ~~**Auto watch-list sync**~~ — DONE. `sync_deviantart_watchlist` now runs in
  `_daily_maintenance_for_user` for connected users (gated on a user token),
  alongside the YouTube sync; still available on demand from the Settings button.
  Test: `tests/integration/test_deviantart_maintenance_sync.py`.
- ~~**Mature deviations**~~ — already correct. Every connected refresh path
  (watch-list sync, watch feed, scheduled + manual `refresh_all_deviantart_feeds`)
  passes `get_deviantart_user_token()` into `fetch_gallery`/`fetch_watch_feed`,
  and both always send `mature_content=true`. The only token-less caller is the
  not-connected standalone-gallery fallback, which has no user token by design.
- ~~**wixmp hotlink images**~~ — already handled. `wixmp.com` is in
  `_HOTLINK_IMG_HOSTS`, so its `<img>` URLs route through the `/api/img` proxy.

### Podcast feeds — missing embedded audio

- ~~Broaden inline-audio detection~~ — DONE. `_find_entry_audio_url` now matches
  enclosures by audio extension on the URL *path* (so `?token=` query strings and
  untyped/oddly-typed enclosures still match), covers more extensions
  (`.m4b/.aac/.oga/.flac`), and falls back to the entry link when it points
  straight at an audio file. Test: `tests/unit/test_audio_detection.py`.
- **Remaining (media:content):** audio that lives only in `<media:content>` still
  isn't detected — the `reader` library keeps standard `<enclosure>` elements but
  drops media:content, so it never reaches the entry object. Supporting it would
  need re-parsing the raw feed (or a reader-layer change); deferred as a separate,
  larger effort.

### Feed rendering — plain-text & paywalled feeds (low priority)

- **Bare-text feeds with literal URLs** (e.g. orpheus.network news): content is
  empty and the summary is plain text with bare `https://…` URLs and
  double-escaped `&lt;br&gt;`. The summary renders in a `<pre>` (template
  `_entry_pane.html`), so URLs stay unclickable and the escaped breaks show as
  literal text. The content_html pipeline already linkifies bare URLs and
  normalizes `&lt;br&gt;`, but only runs when content_html exists. Consider
  routing an HTML-ish/URL-bearing summary through the same pipeline instead of
  the raw `<pre>` — scope carefully so genuinely plain text feeds are unaffected.
- **Paywalled teaser feeds** (e.g. selfh.st): the feed ships only a "subscribe to
  read the full-text RSS" stub; the full article is gated behind membership.
  Readability/source-scrape can't bypass the paywall, so there's no clean fix
  without the user's subscription. Documented as a known limitation.

### Bugs to investigate

- **Tags don't show their articles** — clicking a tag isn't surfacing the tagged
  articles in the list. Check the tag-filter query path (selected_tag →
  list_entries_for_feeds) and the tag link wiring in the sidebar.
- **YouTube video playback fails in the entry pane** — the inline player no longer
  plays. Check the embed/iframe injection for YouTube entries and any CSP/referrer
  or nocookie-domain changes.

### Ideas

- **Inline SVG as thumbnail/lead image** — some feeds ship an inline `<svg>` (or a
  `data:image/svg+xml` / `.svg` URL) as the post art. Support rendering inline SVG
  code as the thumbnail image (analogue to raster thumbs) — sanitize the SVG,
  size/crop it like other thumbs, and decide caching (SVG is text, not a wixmp-style
  binary). Scope safely (no scripts in SVG).

### Later

- **Shared-content tenancy mode** — one global feed/entry store + per-user
  overlays (read/star/folders/subs). Only worth building at real scale; biggest
  caching/refresh win (single refresh per feed, deduped storage). Pushes unread
  counts to an incrementally-maintained per-user table instead of live scans.
- **Per-user resource fairness** — rate-limits/quotas on refresh, scraping, and
  thumb generation. Not needed for trusted users; leave hooks in the seam.
- **Authenticated/private feeds** — none supported today, so all feed/image
  content is safe to global-cache. If added, exclude those feeds from the global
  caches.
- **Miniflux API compatibility** — Fever and GReader are done. Miniflux API is the remaining candidate for broader client support (e.g. Fluent Reader, ReadKit). Assess multi-user requirement and implementation cost before committing.
- **Performance investigation** — systematic baseline before enabling multi-user. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load.

## Backburner

- **feed_strategy_cache migration ordering (latent)** — in `ensure_meta_schema`,
  the `ALTER TABLE feed_strategy_cache ADD COLUMN image_alt/image_title`
  statements run *before* the table's `CREATE TABLE IF NOT EXISTS`, so on a
  brand-new meta DB the ALTERs hit "no such table" (swallowed) and the base
  CREATE then makes the table without those columns — `get_feed_properties`
  would `OperationalError: no such column: image_alt`. Existing DBs were
  migrated before the reorder so they're fine; fix by moving the CREATE ahead of
  the ALTERs (or folding the columns into the base CREATE).
- **Deployment genericization (minimal, after multi-user phases)** — the app is
  already proxy-agnostic; the coupling is in packaging. Decided scope: make the
  base `docker-compose.yml` proxy-agnostic (publish `:8000`, no Traefik labels),
  move today's Traefik labels into an opt-in overlay; move the security headers
  (HSTS/nosniff/frameDeny/referrer) from Traefik into an app middleware so they
  hold regardless of proxy; make trusted-proxy IPs configurable instead of
  `--forwarded-allow-ips=*`. Document Traefik + one alternative now; expand
  (Caddy/nginx/Cloudflare Tunnel/bare) later.
- **Fever pre-sync startup race (pre-existing, cosmetic)** — `FeverService`
  starts its pre-sync thread in `__init__` at import, before `lifespan` runs
  `ensure_meta_schema()`, so a brand-new data dir logs one
  `no such table: fever_entry_map` on first boot (harmless; next sync succeeds).
  Fix by deferring the pre-sync thread until after schema init, or tolerating the
  missing table.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy + display settings without saving.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
