# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **Article-open DB-write contention fixed** — every `/entries/pane` open ran a
  synchronous `store_entry_lead_image` (and conditionally `store_entry_image_alt`)
  write to `entry_lead_images` on the request thread. SQLite allows one writer at
  a time, so when the background lead-image backfill held the meta-DB writer the
  open waited up to the 10s `busy_timeout` — producing intermittent 10–20s article
  opens and `database is locked` warnings at large feed counts. Article opens now
  call `persist_lead_image_async` / `persist_image_alt_async`, which update the
  in-memory cache synchronously and push the DB write to a daemon thread (tenancy
  re-bound) **only when the value changed** — so re-opening an already-resolved
  entry does no write at all. Tests in `tests/services/test_lead_images_service.py`.
- **Inline-SVG thumbnails / lead images** — a post with no raster image but a raw
  inline `<svg>` in its content (e.g. analogue.co firmware notes) now uses that
  SVG as the list thumbnail and article lead image. `services/svg_sanitize.py`
  strips scripts / `on*` handlers / external & `href` refs (only `url(#fragment)`
  kept) via a BeautifulSoup allowlist and emits a `data:image/svg+xml` URI — kept
  vector, no rasterization, no outbound fetch. `currentColor` icons get a neutral
  fallback color so they're visible standalone. Extracted as a last-resort source
  in `extract_inline_thumb_url` / `extract_inline_svg_thumb_url` and
  `_derive_article_lead_image`; the template renders `data:` thumbnails directly
  (bypassing `/thumb`, which only rasterizes http(s)). Tests:
  `tests/services/test_svg_sanitize.py`, inline-SVG cases in
  `tests/services/test_lead_images_service.py`. Note: rare in practice — only
  analogue.co is known to use it, and surfacing it generally still needs some
  hands-on per-feed plugin work, so this is not a broadly-applicable strategy.
- **Tag removal / deletion** — manual tagging was add-only. The article-pane tag
  chips now carry an `×` that removes that one tag from the post (submits the
  reduced set in replace mode, `append_mode=0`). Right-clicking any tag (sidebar
  list or article-pane chip) opens a context menu with **Delete tag everywhere**,
  which (after a confirm) strips the tag from every entry via `/tags/delete` →
  `delete_manual_tag_everywhere`; the sidebar entry disappears once its count hits
  zero (tag-counts cache is now invalidated on tag mutations). Test:
  `tests/integration/test_tag_removal.py`.
- **Article lead image honors inline/media_rss strategy** — feeds pinned to the
  `inline` strategy (e.g. DeviantArt galleries) could show a list thumbnail but
  no article image: the list thumb bypasses the lead-image cache, but the article
  view always used the cache-consulting `extract_entry_thumbnail_url`, which
  returns a stale negative ("no image") entry without scanning content.
  `_derive_article_lead_image` now routes by feed strategy (inline → inline
  extractor, media_rss → media extractor) so the article matches the list. Test:
  `tests/unit/test_article_lead_image_strategy.py`.
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
   `/account`, linked from the main menu in multi mode). ~~Optional user
   deletion~~ — DONE. The Administration → Users table gained a **Delete**
   action (confirm-gated): `/admin/users/delete` drops the account row +
   GReader tokens (`UserStore.delete_user`) and removes the user's isolated
   data dir (`delete_user_storage`), leaving the global image/thumbnail caches
   intact. Refuses self-deletion and the last admin (`count_admins`). Tests:
   `tests/services/test_users.py`, `account_ui` scenario in
   `tests/integration/_multiuser_harness.py`.
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
   Background work that used to run as the default user only — now resolved:
   - ~~**WebSub push callback**~~ — DONE. The shared callback carries only the
     topic, so both the verification GET and the content push now fan out across
     `_background_user_ids()`: verification confirms whichever user has a pending
     subscription, and a push refreshes every subscriber (after confirming
     authenticity against any one user's secret) under that user's context. Previously
     both ran as the empty default tenant, so no real user's WebSub worked.
     Tests: `tests/integration/test_websub_fanout.py`.
   - ~~**Update scheduling policy**~~ — addressed for the current scale. Users
     are still refreshed sequentially within a tick (every user every tick, which
     is fine at 1–3 users), but `_rotate_for_fairness` now rotates the per-tick
     start user round-robin so there's no fixed first-mover bias and a slow/hung
     user delays a different set of downstream users each pass. Deeper fairness at
     real scale (per-user concurrency, fetch budgets) stays deferred behind this
     seam. Test: `tests/integration/test_scheduled_refresh_fairness.py`.
4. ~~**SSRF hardening**~~ — DONE for the two directly-reachable proxies.
   `url_guard.safe_get` / `safe_get_async` follow redirects manually and
   re-validate every hop with `is_safe_outbound_url`; `/api/img` (auth-exempt!)
   and `/thumb` now use them with `follow_redirects=False`, closing the
   redirect-to-internal bypass. 18 new tests. Service-layer follow-up now also
   DONE: the lead-image plugins, lead-image source-page fetch, page scraper, and
   starred-archive text/byte fetches route through `safe_get` (`follow_redirects=
   False`); their HEAD probes pre-validate with `is_safe_outbound_url`. Test:
   `tests/services/test_service_fetch_ssrf.py`. WebSub hub fetches are now guarded
   too (see Code health). Remaining hardening: full DNS-rebind closure needs
   connection IP-pinning (the validate→connect TOCTOU window is now small but
   nonzero) — deferred as lower-priority for the trusted-user threat model.
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
- ~~**media:content audio**~~ — DONE. The `reader` library drops
  `<media:content>` / `<media:group>`, so audio that lives only there never
  reached the entry. `services/podcast_audio.py` re-parses the raw feed with
  feedparser (which surfaces media:content) and extracts a per-entry audio URL.
  `_resolve_entry_audio_url` consults a cache (`entry_media_audio`); on a miss for
  a feed whose scan is due (`feed_media_scan` tracks last-scan time + whether
  audio was found, with a short TTL for podcast feeds and a long one for feeds
  with none) it enqueues a background SSRF-guarded re-parse, so the article open
  never blocks and the player fills in on a later open. Used by the article view
  and both `/entries/media/*` routes. Tests:
  `tests/services/test_podcast_audio.py`,
  `tests/integration/test_media_audio_fallback.py`.
- **Website-feed vs podcast-host-feed** — common gotcha: a site's blog/WordPress
  feed (rich notes) has no audio while the MP3s live in a separate podcast-host
  feed (Libsyn/Buzzsprout/Transistor/Megaphone/Simplecast/…) referenced on the
  episode page. Stage 1 (DONE): `services/podcast_feed_discovery.py` detects that
  host feed from the episode page during the media-audio background scan and
  stores it (`feed_media_scan.suggested_audio_feed`); the article view shows a
  one-click "Subscribe to the audio feed" banner when an entry has no audio and
  the host feed isn't already subscribed. Tests:
  `tests/services/test_podcast_feed_discovery.py`, suggestion cases in
  `tests/integration/test_media_audio_fallback.py`. Stage 2 (DONE): **audio
  borrowing** — when an audio-less feed has a discovered host feed, the
  background scan fetches it and matches each website entry to the host
  episode's MP3 (`match_episode_audio`, by normalized title then episode
  number), caching the borrowed URL in `entry_media_audio`. So the website feed
  plays audio inline with no duplicate subscription; the suggestion banner only
  appears for entries that couldn't be matched. Matcher tests in
  `tests/services/test_podcast_feed_discovery.py`.

### Feed rendering — plain-text & paywalled feeds (low priority)

- ~~**Bare-text feeds with literal URLs**~~ (e.g. orpheus.network news) — DONE.
  `_promote_plaintext_summary` upgrades a summary that carries bare `https://…`
  URLs or `<br>` / double-escaped `&lt;br&gt;` breaks into content_html
  (breaks normalized to real `<br>`, URLs linkified, then the existing
  `<br>`→paragraph pipeline runs on it). Genuinely plain prose returns None and
  keeps the `<pre>` fallback so its whitespace layout is preserved. Test:
  `tests/unit/test_plaintext_summary.py`.
- **Paywalled teaser feeds** (e.g. selfh.st): the feed ships only a "subscribe to
  read the full-text RSS" stub; the full article is gated behind membership.
  Readability/source-scrape can't bypass the paywall, so there's no clean fix
  without the user's subscription. Documented as a known limitation.

### Embeds & feed-content cleanup (follow-ups from the sanitization migration)

Lectio now owns HTML sanitization (feedparser sanitize OFF at ingest, embeds
kept — see ARCHITECTURE "HTML sanitization"). Surfaced during testing 2026-06-20;
these are pre-existing feed-quality quirks, not regressions:

Fixed in the 2026-06-20 browser-testing pass:
- ~~**Sanitizer stripped `class` (regression)**~~ — the own-sanitizer allowlist
  dropped `class`, silently breaking every class-based content cleanup (mynorthwest
  "Related Stories" strip, NASA blocks, Ghost audio cards, embed-container, webcomic/
  YouTube-figure detection). Now keeps `class` globally (`id` still dropped to avoid
  colliding with the app's element IDs). Heals on re-parse. Test: `test_html_sanitize.py`.
- ~~**Enclosure-image feeds showed no article image + backfill wiped the thumb**~~
  (gottadeal) — the deal photo is an `<enclosure>`; it was listed as an Attachment,
  which hid it as a download link AND made the lead-image dedup null it, and the open
  then persisted a negative that wiped the thumbnail on backfill. Image enclosures are
  now excluded from Attachments (`_url_has_image_ext`) and the inline strategy falls
  back to the enclosure-aware extractor. Tests: `test_attachments.py`, repro verified.
- ~~**Webcomic full strip in article, preview as thumb**~~ (claycomix) — the feed
  content ships the FULL multi-panel strip while the source-scrape only finds a
  single-pane preview. The article was showing the preview and stripping the full
  image. `_derive_article_lead_image` now prefers the inline full image for
  `webcomic` feeds (falling back to the scraped panel when the feed has no inline
  image), and the article open no longer persists its lead for webcomic feeds (so
  the scraped preview stays the list thumbnail). Tests:
  `test_article_lead_image_strategy.py`.
- ~~**Junk lead images from widget badges**~~ — openmw grabbed a `shields.io` follow
  badge, claycomix the `ko-fi.com` tip button; both now rejected as site chrome.
  Test: `test_lead_images_service.py::test_badge_and_kofi_widgets_rejected`.
- **qwantz "alt twice" (4485)** — could not reproduce in current code (clean render);
  was a pre-rebuild cached view.

Still open from that pass (deferred — need live-render confirmation or lower priority):
- **Music-blog embeds render tiny** — backfilled SoundCloud/Bandcamp iframes (e.g.
  540×540) look small; needs a look at the live reading-column layout to size them
  (width:100%/responsive) without distorting YouTube video embeds.
- **mynorthwest source image not at top** — og_scrape feed; recheck whether the OG
  lead image shows in-article after the class/cache fixes + a re-derive.
- **Buzzsprout title has no page link** — the feed ships no `<link>` (only guid +
  enclosure); derive the episode page URL from the enclosure (strip `.mp3`) so the
  title is clickable. Needs a `_display_link` fallback used by both list and pane.
- **selfh.st / waynocartoons load via Reader mode** — promising for the paywall spike:
  if Readability already extracts the full article from the page, the "paywalled
  teaser" limitation may be a non-issue for these. Confirm and wire up.

- ~~**WordPress "The post … appeared first on …" footer boilerplate**~~ — DONE.
  `_strip_wp_post_footer` removes the trailing self-link footer (incl. plugin
  duplicates and the double-encoded literal-`<p>` variant). Test:
  `tests/unit/test_wp_footer.py`.
- ~~**Backfill embeds on old entries**~~ — DONE (decision: self-heal + manual
  button, not a mass re-parse pass — stays a good web citizen). Entries stored
  before the sanitization migration kept their embeds stripped; reader only
  re-stores on a content change, which conditional GET skips for unchanged feeds.
  Feed Properties → Info now has a **Backfill embeds** button (`/feeds/reparse`)
  that marks the feed stale (reader's `set_feed_stale`, its own ignore-HTTP-cache
  flag) then updates it, so the now-unsanitized re-parse re-stores those entries
  with embeds intact; read/star state is preserved (reader keys on entry id).
  Test: `tests/integration/test_reparse_route.py`.
- ~~**qwantz (Dinosaur Comics)**~~ — DONE. `_clean_qwantz_content` rebuilds the
  body as just the comic `<img>` (its `title` = secret hover text, kept for the
  caption) plus the dated commentary cell, dropping the top archive/contact/merch
  nav table and the bottom prev/date/next nav row. Test: `tests/unit/test_qwantz.py`.
- **Webcomic single-image feeds (e.g. claycomix)** — investigated 2026-06-20: not
  actually a multi-panel case. claycomix posts a single `wp-post-image` per entry;
  the source page's extra `<img>`s are a DRM-protected early-access preview and
  lazy-loaded (`data-src` + SVG placeholder) support badges, not comic panels. The
  webcomic lead-image strategy already surfaces the single panel. Parked: a generic
  "scrape all panels" feature needs a real multi-panel exemplar to design against;
  revisit if one turns up.
- ~~**Text-only feeds — show derived lead image in article**~~ — already handled.
  Superseded by the background lead-image pipeline: `_derive_article_lead_image`
  consults the cache (filled by the source-page OG scrape) and the article
  template renders `lead_image_url` whenever `show_lead_image_in_article` is on
  (no requirement that the image appear in content), with first-open misses covered
  by the pending/poll path. So zero-`<img>` feeds already get the derived OG image
  in-article. (Spot-check mynorthwest/gottadeal if it ever regresses.)
- ~~**Inject source-page image gallery**~~ — DONE. Some feeds (e.g. paizo blog)
  ship full text but no inline `<img>` — only a single `<media:content>` teaser —
  while the article page has several images. New opt-in per-feed toggle
  **Inject source-page images** (Feed Properties → Tuning) scrapes the article page
  (background `queue_source_html_fetch`, brief wait, then fill on a later open),
  extracts all acceptable images via `extract_source_gallery_urls` (same author/
  site-chrome/related/junk filters as the lead scraper), dedupes against the lead +
  existing body images, and appends them as a `.source-gallery`. Off by default;
  pref `inject_source_images`. Tests: `tests/services/test_source_gallery.py`.
  (Note: paizo WAF-blocks non-production IPs, so verify on the live server.)
- ~~**Blogger "(untitled)" posts** (e.g. treecardgames)~~ — DONE. These Blogger
  entries genuinely ship an empty feed `<title>`; the real title lives only in the
  first body heading and the URL slug. `_display_title` recovers a humanized title
  from the Blogger slug (scoped to Blogger so genuinely-untitled posts elsewhere —
  e.g. Tumblr reblogs — keep "(untitled)"). Test: `tests/unit/test_blogger_title.py`.

### Ideas

- **Outbound webhooks / IFTTT / Zapier etc.** — let automation rules (or a global
  hook) POST matching entries to an external service: a generic webhook URL plus
  presets for IFTTT (Maker/Webhooks applet), Zapier, and similar. Lets users wire
  Lectio into downstream automations (push notifications, Notion/Sheets, home
  automation, reposting). Reuse the existing rule match/scope machinery; send a
  JSON payload (title, link, feed, tags, summary). Must route through the SSRF
  guard and be per-user in multi mode.
- ~~**Compare existing subscriptions**~~ — addressed: Settings → Feeds → Folders now has per-feed checkboxes and a "Compare selected" button (2–6 feeds) that calls `/feeds/compare` and renders the same chips as the Add-Feed picker.
- ~~**Favicon fallback for feeds Google's service can't resolve**~~ — DONE. `/api/favicon` resolves icons via Google → `/favicon.ico` → SVG placeholder, with img-cache caching.
- ~~**Email Article → contacts picker**~~ — DONE. The Email Article dialog gained a
  "choose a saved contact" `<select>` (the default address + Settings → Contacts)
  that fills the free-text "To" field, which still accepts any typed address. A
  `<datalist>` was tried first but browsers filter its suggestions by the input's
  pre-filled value, so it showed nothing until cleared — a real select matches the
  rule-editor pattern and the user's expectation. Also added a "Cc me" checkbox
  that copies the sender's profile email (`cc_me` → route resolves `cc_addr`,
  skipping a self-Cc) AND sets `Reply-To` to that address so a recipient's reply
  reaches the sender instead of bouncing off the Resend sender domain (the From is
  a no-reply sending domain). Template + `/entries/email` route +
  `send_article_email` `reply_to` param. Test:
  `tests/integration/test_email_route.py`.
- ~~**Automated screenshot refresh**~~ — DONE. `scripts/refresh_screenshots.py`
  (`make screenshots`, Playwright via the `screenshots` extra) spins up a
  throwaway single-user Lectio over a temp data dir seeded with fully-synthetic
  demo feeds — `scripts/screenshots/` generates local RSS with inline-SVG art and
  seeds a realistic read/saved/tagged state plus a couple of automation rules —
  then captures the seven README shots. Hermetic and privacy-safe: no real feed is
  ever fetched, so nothing private (e.g. torrent trackers) can land in a committed
  image. Deterministic (fixed demo-feed port, no live network).
- ~~**Push (WebSub) indicator**~~ — DONE. ⚡ glyph next to feed names in sidebar
  and Settings → Feeds for feeds with a verified active WebSub subscription
  (`verified=1 AND hub_url IS NOT NULL`). One query per page render into a `set`.
  Disabled gracefully when `LECTIO_PUBLIC_URL` is blank. Test:
  `tests/integration/test_feed_removal_consolidation.py`.

### Code health

- **Serious duplicate-code / code-smell deep dive** — IN PROGRESS. First pass
  applied the safe, mechanical consolidations (behavior-preserving, test-backed):
  - `LeadImageService._parse_img_attrs` — collapsed the `<img>` attribute-scan loop
    that was copy-pasted across 10 source/feed extractors.
  - `_derive_article_lead_image` — collapsed 4 near-identical strategy branches
    (each `extract_X or extract_entry_thumbnail_url or svg`) into one dispatch.
  - `_entry_query_suffix` — centralized the `&feed_url=…&entry_id=…` redirect suffix
    repeated across 6 mark-read/range routes.

  Prioritized backlog for follow-up PRs (each its own focused change — too risky to
  bundle):
  1. **Decompose `get_entry_detail`** — IN PROGRESS (851 → 732 lines). Added 13
     characterization tests (`tests/integration/test_entry_detail_characterization.py`)
     pinning the dict output across branches, then extracted two cohesive stages:
     `_resolve_entry_content_html` (content/BBCode/plaintext resolution) and
     `_apply_feed_content_cleanups` (per-site strips, footer, qwantz, embeds, YT
     recovery). Remaining stages to extract (each entangled, do under the tests):
     media/audio + attachments, the lead-image resolution+dedup block (the hardest —
     mutates lead_image_url and content_html together), caption/alt, source gallery.
  2. **Consolidate the dedup routes** — `_dry_run_dedup` (198L) and `_run_now_dedup`
     (188L) are near-duplicate (preview vs apply); factor a shared match/collect core
     with an `apply: bool`. Behavior-sensitive (dedup correctness) → dedicated PR.
  3. **Unify the source-image scan loops** in `lead_images.py` — `_extract_preferred_
     source_image_data`, `extract_source_gallery_urls`, and `_extract_webcomic_panel_
     image` all iterate `_IMG_TAG_RE` with the same author/chrome/related/junk filters
     but different selection (best vs all vs comic-class). Extract a shared candidate
     iterator yielding (attrs, resolved_url); callers keep their selection logic.
  4. **Other oversized functions** — `ensure_meta_schema` (585L, mostly sequential
     CREATEs/ALTERs — could split per-table), `home`/`list_entries_for_feeds`.
  - **Test-isolation smell (pre-existing)** — `test_refresh_routes::test_refresh_
    route_success_updates_folder_scope` passes only in the full suite: the `refresh`
    route calls the DeviantArt path → `get_setting` → `_load_app_settings_cache`,
    which queries `app_settings` (absent in the test's `:memory:` dummy meta) and
    only succeeds when a *different* test has populated the module-level settings
    cache first. Fix: seed `app_settings` in the fixture (or guard the DA path).
- ~~**WebSub hub fetches still follow redirects (SSRF)**~~ — DONE. The three httpx
  calls in `services/websub.py` followed redirects: `_discover_hub_url` now fetches
  the user-supplied `feed_url` via `url_guard.safe_get` (per-hop revalidation), and
  `subscribe` / `unsubscribe` pre-validate the discovered `hub_url` with
  `is_safe_outbound_url` and POST with `follow_redirects=False` (safe_get is
  GET-only). Closes the redirect-to-internal bypass on the WebSub surface. Tests:
  SSRF cases in `tests/services/test_websub_service.py`.
- ~~**Duplicate / near-duplicate code deep dive**~~ — DONE. The five copy-pasted
  feed-removal sequences (unsubscribe route, remove-from-folder, delete-folder,
  dedup same/cross, format upgrade) collapsed into `purge_orphaned_feed`. Fixed
  real bugs: the main unsubscribe button now sends a WebSub unsubscribe and routes
  DA/scraped feeds through their special delete paths; delete-folder now
  force-archives pending saves and WebSub-unsubscribes; dedup/upgrade now
  WebSub-unsubscribe the removed URL. Test:
  `tests/integration/test_feed_removal_consolidation.py`.
- ~~**`_derive_article_lead_image` runs synchronously on `/entries/pane`**~~ —
  RESOLVED. `_derive_article_lead_image` now only consults the inline/media/cache
  extractors (`include_source_lookup=False`), so it never fetches on the request
  thread. The cache-miss source-page fetch is queued in the background
  (`queue_source_fetch`) with a 0.8s best-effort cap wait, so fast-responding sites
  still fill the image on first open; when it doesn't land in time, the entry is
  marked `pending_lead_image`, the template emits `data-lead-image-pending`
  (`_entry_pane.html:358`), and the client polls `/entries/lead-image`
  (`index.html:5872`) to lazy-fill the image once the background fetch completes.
  The earlier per-open meta-write contention was also moved off-thread (see
  "Article-open DB-write contention" in Recently Completed).

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
