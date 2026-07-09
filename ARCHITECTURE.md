# Lectio Architecture

Lectio is a self-hosted feed reader built around the `reader` Python library. The goal is a fast triage workflow with a clean multi-user architecture and VPS-friendly deployment.

## Layering

- UI/API layer: web routes, handlers, presentation state.
- Services layer: feed operations, tagging, filtering, refresh, readability.
- Storage layer: reader DB, app-data, settings.

The layers run in one process today, but the boundaries should stay clean.

## Reader-first philosophy

`reader` is the primary storage/ops primitive. It already covers:
- feed retrieval and storage,
- read state,
- arbitrary tags and metadata,
- filtering and search,
- statistics,
- plugin support.

Prefer reader API and plugin behavior first. Add custom logic only when the existing reader model cannot express the behavior cleanly.

## View state model

Keep three kinds of state separate:
- remembered base preferences,
- contextual temporary overrides,
- transient navigation state.

Examples:
- remembered: sort mode, default filters, pane sizing.
- temporary: tag-click “show all,” search result scope.
- transient: current entry, scroll position, focus.

Temporary overrides must not silently overwrite remembered preferences. Leaving the override context should restore the base preference.

### Global audio player

The persistent audio player is a deliberate exception to the pane-swap lifecycle.
The entry view is loaded via `/entries/pane` swaps, so any `<audio>` inside it is
destroyed on navigation. Instead a single `<audio>` + control bar lives in
`templates/index.html` outside the swap target and is owned by
`static/media-player.js`; podcast posts inject a `.podcast-player` Play trigger
(`_apply_entry_media`) that hands the track URL/title to the global bar. Player
state (current track, position, playback speed) is transient client-side state
only — no server or DB involvement — with playback speed persisted to
`localStorage`.

## Adaptive layout model

Lectio uses responsive layouts rather than a fixed three-pane assumption:
- wide desktop: 3-pane side-by-side,
- medium tablet landscape: 2-pane refinement,
- narrow phone portrait: 1-pane drill-in navigation.

The priority is fast triage, not always showing three panes.

## Deployment path

Lectio is designed for VPS deployment behind a reverse proxy. Auth is always active; access requires a user account. See `.env.example` for deployment configuration.

## Folder tree & the Uncategorized folder

Folders live in the meta DB (`folders` + `folder_feeds`); the reader owns the
feeds themselves. These two can diverge: a feed can exist in the reader with no
`folder_feeds` row (common after an OPML/reader migration). Such feeds are
**orphans**.

**Single-folder invariant:** a feed belongs to exactly one folder. `folder_feeds`
has no DB-level uniqueness (it once allowed multi-folder membership), so the
invariant is enforced in the write paths: `add_feed_to_folder` clears a feed's
other memberships before inserting, and the dedup/format-upgrade paths delete the
survivor's stale rows before re-inserting the chosen folders (earlier they added
without removing, which let feeds drift across folders). Pre-existing drift is
repaired by **Settings → Feeds → Utilities → Fix multi-folder feeds**
(`GET /feeds/multi-folder` reports feeds with >1 row; `POST
/feeds/multi-folder/resolve` keeps only the user-chosen folder per feed).

The sidebar surfaces orphans through a **virtual "Uncategorized" folder**,
derived at render time — it has no `folders` row. Its id is a negative sentinel
(`UNCATEGORIZED_FOLDER_ID`) so it never collides with real (positive) folder
ids, and its membership is computed as `all reader feeds − foldered feeds`. It's
pinned last in the tree, hidden when empty, and self-updates as feeds get filed.
Because it isn't a real folder, its context menu exposes only whole-folder
actions (mark-read / refresh) and it's excluded from move-target lists; the
`get_folder_feed_urls` resolver special-cases the sentinel so those actions still
work. The root "All Feeds" folder resolves to *every* reader feed (not just
foldered ones), so orphans and their unread counts are always reachable from the
top of the tree.

The root is treated as equivalent to Uncategorized for feed placement: both
`add_feed_to_folder` and `move_feed_to_folder` store a feed folderless (no
`folder_feeds` row) when the target is the root id or `UNCATEGORIZED_FOLDER_ID`,
rather than writing a root membership row. This keeps the invariant that a
`folder_feeds` row always means "filed in a real sub-folder," so a feed added to
the root consistently surfaces under Uncategorized. `delete_folder`'s move path
already applies the same rule.

## Multi-user tenancy

Lectio uses a **storage-layer resolver** so the UI/API and service layers are
user-agnostic. One interface:

```
get_current_user(request) -> user_id        # auth layer, resolved once per request
tenancy.reader_db_for(user_id)              # storage layer
tenancy.meta_db_for(user_id)
```

- **isolated** (shipping first): each user gets their own reader + meta +
  starred-archive DB under `DATA_DIR/users/{user_id}/`. Reader-native (no fight
  with the single-tenant `reader` storage model), strongest isolation, trivial
  cost at small scale. The global `get_reader()` / `get_meta_connection()`
  singletons become per-user resolutions backed by an LRU connection pool keyed
  by `user_id` (one user reproduces today's behavior exactly).
- **shared-content** (deferred): one global feed/entry store plus per-user
  overlays for read/star/folders/subscriptions. Biggest caching/refresh win
  (single fetch per feed serves all subscribers) but only worth building at real
  scale. Because routes/services go through the resolver, switching modes is a
  storage swap, not a route rewrite.

The resolver and per-user connection pools live in `services/tenancy.py`;
`get_reader()` / `get_meta_connection()` / `get_starred_archive_connection()` in
`main.py` resolve through it. The current user is a `contextvars.ContextVar` that
defaults to `DEFAULT_USER_ID`.

Accounts live in a global users table (`lectio_auth.sqlite`, `services/users.py`,
NOT routed through tenancy). Each account has a stable, immutable **`user_id`**
(an opaque slug generated at creation) and a mutable **`username`**. The
`user_id` is the identity everything keys on — the tenancy key, the on-disk
directory (`users/<user_id>/`), the session value, and the foreign key for API
tokens — so a username can be renamed (`UserStore.rename_user`, admin UI) without
moving any data. Auth lookups take a typed username and return a `user_id`; the
rest of the system passes `user_id`. Passwords are hashed by `services/passwords.py`
(scheme via `LECTIO_PASSWORD_HASH_SCHEME`: `scrypt` default, `pbkdf2_sha256`, or
`argon2` if `argon2-cffi` is installed; hashes are self-describing and
transparently re-hashed to the configured scheme on login). On first startup with
an empty table, an admin is seeded from `LECTIO_ADMIN_USERNAME`/`LECTIO_ADMIN_PASSWORD`
(default `admin`/`ChangeA$ap`, with a loud warning if the default password is
used). Login binds `session["user_id"]`; `_TenancyMiddleware` (pure-ASGI,
innermost) binds that user into the tenancy context around the endpoint, so every
storage access routes to the user's own DBs. A username doubles as the tenancy
`user_id` and a path segment, so it must match the resolver's slug charset.

Per-user API tokens (Fever + GReader): each user has an `api_token` in the auth
DB, serving both protocols (as the single `LECTIO_FEVER_PASSWORD` did before).
Fever resolves `md5(username:api_token)` to a user; GReader ClientLogin verifies
username + token and mints a global bearer token (`greader_api_tokens`, a global
table because a request carries only the token and must resolve to a user before
the context is bound). GReader binds context in `_TenancyMiddleware` from the
header/query token (no body read); Fever binds in its handler (api_key is in the
body). The protocol services' data methods are user-independent and reused
as-is; Fever's entry-map sync is tracked per user. Background work spawned by a
request (GReader mark-all-as-read; the per-entry mark-read writes fired off the
entry pane and the async read toggle) must re-bind the captured user via
`_run_in_user_context`, since threads don't inherit contextvars — otherwise the
write lands in `DEFAULT_USER_ID`'s DB and the entry keeps showing as unread for
the actual user. The same applies inside the service layer: `LeadImageService`'s
`queue_source_fetch` / `queue_source_html_fetch` resolve a lead image (or its
alt/caption) in a daemon thread off the render path and persist it via the
context-bound meta connection, so they capture `tenancy.current_user_id()` and
re-wrap the worker in `tenancy.user_context` — otherwise a user browsing their
feed silently writes lead images into the default tenant's `entry_lead_images`.
The chunk-level visible-entry backfill (`backfill_entry_list`, spawned from the
home route for entries missing a cached thumbnail) is a bare daemon thread for
the same reason and must likewise be wrapped in `_run_in_user_context` at the
call site — otherwise its thumbnails persist to the default tenant and appear to
"not stick" for the real user across refreshes. Manual refresh (`/refresh`,
`/refresh/feed`) follows the same pattern: it ingests entries with
`update_feeds(enhance=False)` and hands the network-heavy lead-image / YouTube-
duration enhancement to `_spawn_feed_enhancement` (a daemon thread wrapped in
`_run_in_user_context`, with a per-feed in-flight guard so concurrent manual /
scheduled runs don't duplicate fetches), so the request returns promptly while
images fill in shortly after. The scheduled tick (`_scheduled_refresh_tick`)
follows the same ordering: ingest with `enhance=False`, run automation
(hide-shorts, mark-read, dedup) immediately so entries are triaged as soon as
they land, then run enhancement in the scheduler thread, followed by a second
hide-shorts pass to catch Shorts identifiable only by their freshly-fetched
duration (≤60s, no `#shorts` hashtag). Each refresh path (manual, single-feed, scheduled)
calls `invalidate_unread_counts_cache()` after ingest so newly-arrived entries
update the folder "new" badges immediately instead of waiting out the
stale-while-revalidate TTL. Both the async refresh and the *cold* synchronous
compute in `get_unread_counts_by_feed` are guarded by the cache generation
counter: a scan takes ~2s, and if a mark-read/refresh bumps the generation
mid-scan, the result predates that change and is discarded rather than written
back — otherwise a slow render's stale counts would repopulate the just-cleared
cache and make a mark-read appear to revert seconds later.

The bulk age actions (`/entries/mark-older-than-read`, `/entries/mark-newer-than-unread`)
must key off the same entry date the list renders and the client optimistically
greys on — `published or updated or added`. The list falls back to `added`
(received) when a feed omits publish dates, so an endpoint that only considered
`published or updated` would skip entries the UI already marked, making them
flash read and then revert.

Account UI: `/account` lets a user change their password and view/regenerate
their API token; admins additionally create/disable users and reset passwords.
New users are provisioned (`provision_user_storage`) on creation.

Per-user background work: the scheduled refresh loop and the daily-maintenance
loop both iterate every enabled user (`_background_user_ids`) and run each pass
under that user's context — feeds refresh on each user's cadence, and per-user
maintenance (rule-log prune, orphan cleanup, meta/starred VACUUM, email-batch
flush) runs against each user's DBs. Users are processed sequentially within a
tick (every user every tick); `_rotate_for_fairness` rotates the per-tick start
user round-robin so there's no fixed first-mover bias — adequate at the 1–3 user
target, with per-user concurrency and fetch budgets deferred behind that seam. The startup tasks follow the same rule:
the scraped-feed sync, auto-taggers, guid-churn dedup, and the YouTube /
lead-image / starred-archive / read-history backfills all run once per enabled
user via `_for_each_background_user` — a bare daemon thread inherits no
contextvar, so running them unwrapped would resolve to `DEFAULT_USER_ID` and
write the legacy top-level DBs instead of each user's. The starred-archive
worker (`StarredArchiveService`) is one long-lived global thread; each poll cycle
it scans every background user's archive DB under that user's context (injected
`background_user_ids`), so a single worker drains all users' queues without
binding itself to the default tenant. Work that is genuinely global runs once in
`_run_global_maintenance` (thumb-cache VACUUM, YouTube sync — a single config).

Remaining (see Plan.md): the WebSub push callback (a push carries only a feed URL
and must fan out to its subscribers) still runs as the default user. (SSRF
hardening of `/api/img` and `/thumb` has landed — see "Security posture". The
WebSub discover-on-subscribe spawned when a feed is added now re-binds the
requesting user via `_run_in_user_context`.)

### Per-user in-memory caches

The module-level caches that hold per-user data (folder/feed structure, unread
counts, tag counts, feed-title map, problematic feeds, has-manual-tags, and the
`app_settings` cache) are partitioned by the current tenancy user via
`_PerUserDict` (and a `user_id`-keyed dict for `_app_settings_cache`). A global
cache here leaks one user's data into another's view (the tree/avatar render from
cache even though per-request DB reads are correct). Likewise, any code path that
opens a DB by the raw `READER_DB_PATH`/`META_DB_PATH` constant instead of
`tenancy.*_db_path()` reads the default user's data — per-request paths (unread
counts, tag scans, takeout, `/stats` sizes) must use the resolver. Caches keyed
purely by content (e.g. domain classification, source-HTML by URL) may stay
global.

### Integrations

The Resend **API key** is instance-shared (`get_resend_api_key` keeps its env
fallback) — one verified domain owned at the instance level. Everything else is
per-user: the email **From** identity (`get_resend_from`, no env fallback), the
default recipient, contacts, profile, and Instapaper credentials. The env values
(`LECTIO_EMAIL_FROM`, `LECTIO_EMAIL_TO`) seed only the bootstrap admin's settings
(`_seed_admin_integrations_from_env`) and are then ignored for per-user reads, so
one user's sender/account never becomes another's default.

### What stays global

Content-addressed caches hold no per-user data and are shared across all users:

- **`thumb_cache`** — keyed by `sha256(url|W|H|crop)`.
- **`img_cache`** (`lectio_img_cache.sqlite`) — shared by `/api/img` (keyed by
  `sha256(source_url)`) and `/api/favicon` (keyed by `favicon:<host>`). The
  `/api/img` proxy stores the (optionally downscaled) original bytes + content-type +
  `created_at`/`last_accessed`/`size`. On a miss, the proxy does the SSRF-guarded
  fetch, downscales to `LECTIO_IMG_CACHE_MAX_DIM` (longest side, never upscaling;
  animated/SVG/unknown formats are stored byte-for-byte), then stores and serves.
  `/api/favicon` resolves icons via a three-hop chain (Google faviconV2 →
  `/favicon.ico` → bundled SVG placeholder), caching the winning result under its
  `favicon:<host>` key. Eviction is a **last-accessed TTL** run in daily global
  maintenance (`_evict_img_cache`): entries not served within `LECTIO_IMG_CACHE_DAYS`
  are dropped (0 = keep forever). Both tunables fall back to env but admins can
  override them in the Administration page. Caching the bytes server-side also lets
  images behind short-lived signed URLs (e.g. `wixmp.com`) survive token expiry.
- **`entry_lead_images` / `feed_strategy_cache`** — derived from public pages,
  keyed by feed + entry.

This is safe today because **no authenticated/private feeds exist** — all feed
and image content is publicly fetchable. If private feeds are added later, those
feeds must be excluded from the global caches.

### Security posture

- **Per-user identity** — accounts live in a users table with scrypt/argon2
  hashing. `session["user_id"]` identifies the authenticated user.
- **Per-user API tokens** — each user has their own Fever/GReader API token.
- **Authorization** — every per-user route scopes by `user_id`. This is the
  largest code surface, but the resolver localizes it to the storage seam.
- **SSRF hardening** — `url_guard.safe_get` / `safe_get_async` follow redirects
  manually and re-validate every hop against private/loopback/link-local space.
  Now applied to all reachable user/feed-controlled fetches: `/api/img`, `/thumb`,
  feed discovery (`_guarded_get` / `_guarded_head`, which also pre-validate HEAD
  probes), the source-proxy / readability / feed-tag fetches in main.py, the
  service-layer background fetches (lead-image plugins, lead-image source-page
  fetch, the page scraper, and the starred-archive text/byte fetches), and the
  WebSub hub fetches (`_discover_hub_url` via `safe_get`; the subscribe /
  unsubscribe POSTs pre-validate `hub_url` with `is_safe_outbound_url` since
  `safe_get` is GET-only) — all with `follow_redirects=False`, closing the
  redirect-to-internal bypass. HEAD probes (image-fetchability / comic-URL checks)
  go through `url_guard.safe_head`, which validates the target and fetches
  `follow_redirects=False` (HEAD has no per-hop counterpart to `safe_get`).
  Outbound **webhook** automation rules (`services/webhooks.py`) POST to a
  user-supplied URL, so they validate with `is_safe_outbound_url` and POST with
  `follow_redirects=False` (no GET helper for POST) — same outbound policy as the
  image proxy and WebSub. The migration source clients (`services/freshrss.py`,
  `services/miniflux_import.py`, `services/ttrss.py`) fetch a user-supplied server
  URL over both GET and POST, so each validates at its URL-builder choke point
  (`_api_base` / `_api_url`) via `url_guard.ensure_safe_outbound_url` — one guard
  covers every request (test + import worker) since they all share that host, and
  the httpx clients don't follow redirects. Their `/import/test` endpoints return
  generic error messages (and log detail server-side) rather than echoing the
  exception, so an internal-probe attempt can't exfiltrate response detail.
  Still open: the `reader` library's own feed refresh (a subscribed `http://10.x`
  host is still fetched); and full DNS-rebind closure needs connection IP-pinning
  (the validate→connect window is small but nonzero).
- **Browser-identity fetch escalation** — feeds are fetched with an honest
  identity (`Lectio/0.1 (+repo)`). Some hosts (WAFs returning 403/415/429/503, or
  hanging non-browser requests) refuse it. On a *refusal* — never preemptively —
  Lectio escalates to a full browser identity (UA + `Sec-Fetch-*`/`Accept-Language`
  headers, since some WAFs sniff those, not just the UA). Discovery
  (`feed_discovery._get_with_escalation`) retries inline at subscribe time and
  flags the feed; the scheduled-refresh path (`FeedRefreshService`, via an
  `on_fetch_refused` callback) flags + retries once; reader's own fetch applies
  the browser identity per-feed through a request hook
  (`reader_api.ReaderApi._make_browser_ua_request_hook`) keyed on the
  `browser_ua_feeds` set. Per-user, manually resettable in Feed Properties. This is
  escalation on refusal, not IP-block evasion — consistent with the good-citizen
  policy (honest by default; don't spoof hosts happy to serve us).
- **Outbound TLS cipher compatibility** — httpx/httpcore's default `SSLContext`
  advertises a narrower cipher list than curl/requests/browsers, and some WAF/CDN
  edges (e.g. Tumblr) drop the connection at the TLS layer before any HTTP response
  ("Server disconnected without sending a response"). All arbitrary-web-content
  clients are built via `url_guard.build_client` / `build_async_client`, which use
  a shared `WEB_SSL_CONTEXT` reset to OpenSSL's stock `DEFAULT` ciphers so those
  hosts accept us. This is a standard TLS config, not JA3/browser fingerprint
  spoofing — same good-citizen posture as the UA policy. Fixed-API integration
  clients (freshrss/ttrss/inoreader/quire/etc.) keep httpx defaults.
- **Subscription scheme allowlist** — user-supplied feed URLs (Add Feed, OPML
  import, discovered `<link>` candidates) are restricted to http/https via
  `_is_subscribable_feed_url`. `reader` natively fetches `file://`, so without
  this an `xmlUrl="file:///…"` could read local files (other tenants' DBs, `.env`)
  on refresh. Internal scraped feeds still register their `file://` URLs through
  `reader.add_feed` directly, bypassing the user-facing guard.
- **HTML sanitization (Lectio owns it)** — `reader` parses feeds with
  feedparser's `sanitize_html=True`, which *destroys* (not escapes) anything off
  its allowlist — iframes, SVG, MathML, audio/video — silently stripping embeds
  from every article. Lectio instead mounts a replacement parser
  (`services/reader_sanitize.py`) that parses with sanitization **off** and runs
  entry content/summary through its own allowlist (`services/html_sanitize.py`)
  at ingest, so embeds survive while scripts/handlers don't. The same allowlist
  also sanitizes proxied source-page and Readability HTML at render. Because
  `reader` does no sanitizing of its own, that single allowlist is the only thing
  standing between feed HTML and a `| safe` render — it drops scriptable tags,
  all `on*` handlers, `style`, `javascript:`/`vbscript:`/`data:` URLs (incl.
  control-char-obfuscated), and `object`/`embed`/`form`.
- **Embed allowlist** — `<iframe>` is kept only when its `src` host is on
  `_EMBED_HOST_ALLOWLIST` (YouTube/Vimeo/Dailymotion/Twitch/SoundCloud/Bandcamp/
  Spotify + Twitter/CodePen/Reddit/Archive.org), https-only, matched by exact or
  dot-suffix host (so `youtube.com.evil.com` doesn't slip through). Kept iframes
  are forced into a `sandbox` (`allow-scripts allow-same-origin …` — same-origin
  refers to the *embed's* origin, not Lectio's) with a conservative
  `referrerpolicy` and lazy loading. Inline SVG is cleaned via
  `services/svg_sanitize.py`; MathML is kept with a curated element/attribute
  allowlist.
- **Sphinx/dvisvgm math sizing** — blogs like eli.thegreenplace.net emit formulas
  as `<object type="image/svg+xml">` / `<img>` whose *true* rendered height rides on
  an inline `style="height: Npx"` (the SVGs' intrinsic dimensions are in `pt`, which
  renders tiny) plus a `valign-mN` baseline class. Since the allowlist strips inline
  `style`, `_promote_math_height` lifts that px height onto a real `height` attribute
  (already allowlisted) before the strip; CSS then honors the per-glyph height and
  `valign-*` baseline instead of flattening every formula to one size. `_MATH_SCALE`
  (default 1.0) is the single knob to enlarge all math (requires re-ingest to apply).
- **YouTube quota meter (per-user)** — the Data API exposes no remaining-quota read,
  so Lectio estimates spend itself: each billed call reports its documented unit cost
  through a sink (`playlists.list`/`videos.list`/sub-sync = 1, `playlistItems.insert`/
  `playlists.insert` = 50). `services/youtube_oauth.py` and `services/youtube_sync.py`
  expose `set_quota_sink`; `YouTubeDurationService` takes a `quota_sink`; all three are
  wired to `record_yt_quota_spend`, which tallies units into the per-user `yt_quota_spend`
  table keyed by the **Pacific calendar date** (`_pacific_today()`, Google's reset). The
  Integrations panel shows spent/cap/remaining via `get_yt_quota_status()` (cap =
  `yt_quota_cap` setting, default 10k) with low (<500 left) and exhausted states; an
  actual `quotaExceeded` response snaps the tally to the cap (`mark_yt_quota_exhausted`).
  Tests null the sink (conftest autouse) so a billed call can't pollute another test.
- **Quire destination (per-user)** — another per-user OAuth outbound destination
  (`services/quire.py`, same shape as DeviantArt: `/quire/connect` → `/quire/callback`
  store tokens; `get_quire_user_token` refreshes on expiry). A user picks one default
  project (`quire_project_oid`); the `Add to Quire` entry button (`/entries/quire`),
  On-Star, and the `quire` automation rule (`_run_quire_rules_after_refresh`) all create
  a task in it. Unlike YouTube's **daily** quota, Quire rate-limits **per organization by
  minute and hour** with no remaining-quota read, so the meter is a **sliding window**:
  each billed call logs a row into `quire_call_log` (pruned to the last hour) via the
  `set_usage_sink` → `record_quire_call` sink; `get_quire_usage_status()` reports
  minute/hour usage vs the `quire_rate_cap_min`/`_hour` caps with low (≥80%) and blocked
  (≥cap) states. The caps are **auto-detected from the destination project's organization
  plan** (`detect_quire_plan_and_caps` → `GET /project/{oid}` returns `subscription.plan`;
  `PLAN_RATE_CAPS` maps Free 50/200, Professional 300/1250, Premium 1000/5000; Enterprise
  scales with members so it keeps the default), run on a background thread whenever the
  chosen project changes; the detected plan name is shown in the meter. Automation runs check the meter before each add,
  honor a per-run cap (`_QUIRE_AUTO_PER_RUN_CAP`), and back off on a 429 (`Retry-After`).
  The far-right entry-header **share-dropdown consolidation** of all destinations is a
  deferred follow-up (see Plan.md); for now Quire is a standalone glyph beside the others.
- **Hide Shorts (global, per-user)** — Shorts are auto-marked read at refresh by the
  hide-shorts pass in `_run_automation_after_refresh`. Per-feed it reads the
  `feed_display_prefs.hide_shorts` pref; the `yt_hide_shorts_global` setting
  (`youtube_hide_shorts_global()`, Integrations toggle, off by default) additionally
  targets **every** refreshed YouTube feed (URL contains `youtube.com/feeds/videos.xml`)
  regardless of the per-feed pref — one source of truth, no drift as feeds come/go via
  sync. A Short is detected by `/shorts/` in the entry link (`_is_youtube_short`).
- **YouTube embed host (per-user)** — both `youtube.com` and `youtube-nocookie.com`
  are allowlisted; which one a YouTube *embed* uses is the viewer's choice, applied
  at **render** (not ingest, since sanitization bakes content into each user's
  reader DB). `youtube_embed_host()` reads the per-user `yt_embed_account_features`
  setting (default off → privacy-enhanced `youtube-nocookie.com`). The recovered/
  injected player (`_youtube_embed_html`) builds with that host directly;
  feed-native embeds are rewritten by `_apply_youtube_embed_host` in the entry-detail
  pipeline (iframe `/embed/` URLs only — plain watch links are untouched). Opting in
  (Integrations → YouTube) switches to the standard host so the player exposes Share /
  Watch Later, which need the viewer's signed-in YouTube cookies that `-nocookie`
  blocks. Render-time application makes the toggle instant and retroactive.
- **YouTube Add-to-Playlist (per-user OAuth)** — the embed player only exposes
  Watch Later, so a real playlist picker needs the **YouTube Data API v3** with a
  write scope (`auth/youtube`), separate from the read-only `YOUTUBE_API_KEY`
  (durations, sub-sync). `services/youtube_oauth.py` speaks HTTP to Google
  (authorize / token exchange / refresh, `playlists.list`, `playlistItems.insert`,
  `playlists.insert`); main.py owns the flow (`/integrations/youtube/oauth/{connect,
  callback,disconnect}`) and stores the **refresh token per-user** in app-settings
  (`get_youtube_oauth_token()` refreshes on demand, returns "" → reconnect prompt on
  failure). The OAuth *client* creds are app-level (one registered Google app, read
  from env in both single and multi mode) — only the resulting tokens are per-user,
  so accounts are never shared. The redirect URI is fixed at
  `/integrations/youtube/oauth/callback` to match the Google client registration.
  Client-side, `enhanceYoutubeEmbeds()` injects an "Add to playlist" control beneath
  each YT iframe (video id parsed from the `/embed/` src); it lazily fetches
  `/api/youtube/playlists` (cached per page session). The playlist menu is positioned
  `fixed` at open so it escapes the article pane's `overflow:auto` clipping, flipping
  upward near the viewport bottom. `quotaExceeded` surfaces as a
  distinct 429 so the UI can tell the user to fall back to manual add on youtube.com
  (default 10k units/day ≈ 200 inserts; resets midnight Pacific). The OAuth app stays
  in **Testing** mode (refresh tokens expire ~7 days → occasional reconnect).
- **Auto add-to-playlist automation** (`youtube_playlist` rule type) — builds on the
  OAuth integration above to add new entries' videos to a playlist at refresh time.
  It's a **general** automation rule (any feed/folder scope, via the shared
  `highlight_keywords` table + after-refresh pass), not YT-folder-bound, because a
  YouTube video can be embedded in any feed's article and an entry can carry several.
  `_run_youtube_playlist_rules_after_refresh` runs last in `_run_automation_after_refresh`
  (after mark_as_read so its own "mark read after add" doesn't fight an earlier rule):
  for each new (within the 15-min cutoff) matching entry it extracts **all** video ids
  from the entry link + content (`youtube_embeds.video_ids_in_text`, which also matches
  `/shorts/`), inserts each via `playlistItems.insert`, and optionally marks the post
  read. The rule's keyword is an **optional filter** — empty = every new video in
  scope. Per-rule columns on `highlight_keywords`: `yt_playlist_id`,
  `yt_playlist_title`, `yt_include_shorts` (default off — Shorts detected by the
  `/shorts/` link), `yt_mark_read` (default on), and `yt_min_minutes`/`yt_max_minutes`
  (0 = no limit). The **duration filter** reuses the cached video length (the same
  store behind the `[duration]` title prefix), so it needs the `YOUTUBE_API_KEY`; a
  video whose duration isn't cached yet is skipped that run and retried once it is.
  Durations are fetched in **batches of 50 ids per `videos.list` call** — that endpoint
  bills **1 quota unit per call, not per video**, so a large subscription set (10k+
  videos) costs ~200 units instead of ~10k. (Per-video fetching previously blew the
  10k/day quota, leaving a rotating ~13% of videos perpetually duration-less; ids the
  API returns no item for stay NULL and are retried per the negative-retry window.)
  Because `playlistItems.insert` is
  **not idempotent**, a `youtube_playlist_added (scope, scope_id, keyword, entry_id,
  video_id)` table is the dedup guard: each (rule, entry, video) row is claimed with
  `INSERT OR IGNORE` *before* the API call (rowcount 0 → already added → skip), and
  released on failure/quota so it retries next run. A per-run cap
  (`_YT_PLAYLIST_AUTO_PER_RUN_CAP = 25`, ≈1250 units) keeps a burst of new uploads
  from exhausting the daily quota; `quotaExceeded` stops the run. The rule-type option
  is gated on `yt_oauth_connected` (server-side in `/highlights/add`; hidden in the
  rule-builder until connected) so it can't be created without a token. Runs in the
  per-user background context like the other after-refresh rules.
- **Save to Pinterest (per-user OAuth)** — an outbound-only integration: a per-entry
  **Pin** button saves an article to one of the user's boards. Pinterest has no
  write-without-OAuth path, so `services/pinterest_oauth.py` speaks the **API v5**
  OAuth flow (authorize / token / refresh — the token endpoint authenticates the
  *client* with HTTP **Basic** auth, body form-encoded, unlike Google's JSON) plus
  `boards.list` (scope `boards:read`) and `pins.create` (scope `pins:write`). main.py
  owns the routes (`/integrations/pinterest/oauth/{connect,callback,disconnect}`,
  `/api/pinterest/boards`, `/api/pinterest/pin`) and stores the **refresh token
  per-user** (`get_pinterest_oauth_token()` refreshes on demand; "" → reconnect). The
  OAuth *client* creds are app-level (`PINTEREST_OAUTH_CLIENT_ID/SECRET` from env,
  both modes); only the tokens are per-user. The pin route derives the entry's lead
  image via `_derive_article_lead_image` (the **source** URL, not the `/api/img`
  proxy, since Pinterest must fetch it) and links the pin back to the entry; an entry
  with no image returns 422 (Pinterest requires an image). The Pin button is rendered
  only when connected (`pinterest_connected` context flag); the board picker is a
  lightweight client-side menu fed by `/api/pinterest/boards`.
- **Rule scope (incl. multi-feed)** — automation rules scope to `global` (all feeds),
  `folder`, `feed` (one URL), or `feeds` (an explicit set; `scope_id` is the feed URLs
  joined by newline — newline, not comma, since URLs can contain commas). Scope
  resolution is centralized so every runner agrees: `resolve_rule_feed_urls(conn,
  scope, scope_id)` returns the feed set (or `None` for global) for the bulk/dry-run
  paths, and `feed_in_rule_scope(scope, scope_id, feed_url, folder_feed_urls)` is the
  per-feed test the after-refresh runners use against each freshly-refreshed feed
  (folder scopes pass a prefetched feed set for speed; `feeds`/`feed`/`global` don't
  need it). Deduplicate accepts `global`/`folder`/`feeds` (the latter dedupes across a
  selected set, resolved via `_resolve_dedup_feed_urls`) but rejects a single `feed`
  — one feed can't cross-dedupe. The rule builder derives the scope from a
  multi-select feed listbox: 0 selected = folder (or global if no folder), 1 =
  `feed`, 2+ = `feeds`.
- **Inline-SVG sanitization** — a raw inline `<svg>` from feed content can also
  become a list thumbnail / article lead image. `services/svg_sanitize.py` parses
  and rebuilds it with a presentation/geometry tag+attribute allowlist, dropping
  the `script`/`style`/`foreignObject`/`image`/`use`/`a` subtrees, every `on*`
  handler, all `href`/`xlink:href`, and any non-`url(#fragment)` reference, then
  serves it as a `data:image/svg+xml` URI (kept vector — no rasterization, no
  outbound fetch).
- **Reader-view embed re-injection** — `python-readability`'s `.summary()` strips
  *every* `<iframe>` during extraction (and sometimes keeps the lead image twice),
  so allowlisted players would vanish from Reader view. `build_readability_response`
  pulls the allowlisted embeds out of the raw page (`_reinject_readability_embeds`,
  reusing `_embed_host_allowed`) and appends any the extracted article is missing
  *before* the sanitizer runs — so the re-injected iframes still get sandboxed by
  `_sanitize_iframe`. `_dedupe_readability_images` then drops repeated `<img>` tags
  sharing an `src`. Responsive CSS sizes the iframes (16/9, Spotify fixed-height).
  Because Reader view is served from Lectio's own origin, relative `src`/`href`
  URLs would resolve against Lectio and 404: `Document(url=source_url)` lets
  readability absolutize the summary, and `_absolutize_article_urls` then runs a
  final `make_links_absolute` pass over the article (covering the BS4 content
  fallback, which returns its element verbatim) — fixes pages that use
  page-relative image paths with no `<base>` tag (e.g. fabiensanglard.net).
- **Feed-side YouTube recovery** — the embed `<iframe>` is stripped at ingest but
  the raw feed still carries it, so the media scan (`extract_youtube_embeds`,
  re-parsing the raw feed with sanitize off) caches the video ids and
  `_inject_recovered_youtube_embeds` refills the empty placeholder it left behind:
  WordPress' `<figure class="...is-provider-youtube">` **or** ArtStation's
  `<div class="video-wrapper media-asset...">` (matched by `_YT_EMBED_PLACEHOLDER_RE`).
  The id scan recognizes both the standard and privacy host (`youtube-nocookie.com`).
- **Source-page embed recovery (feed pane)** — entries ingested *before*
  `services.reader_sanitize` stopped stripping `<iframe>` at feed-parse time lost
  their players, and (unlike the placeholders above) leave *nothing* to refill —
  no `figure`, no `video-wrapper` — and the raw feed item has often scrolled out
  of the window, so the feed-side scan can't help. `_inject_recovered_source_embeds` (called from `get_entry_detail`
  after the cleanups, skipped for native YouTube feeds) handles this: when the
  stored body has no `<iframe>` and the entry has a source link, it reads the
  lead-image **source-HTML cache** (shared with the lead-image scraper, so it's
  often already warm; on a miss it queues `queue_source_html_fetch` and leaves the
  body unchanged — never blocking the render on a network GET — so the embed fills
  in on a later open), then `_extract_source_embed_iframes` pulls the allowlisted
  players (`_embed_host_allowed`) — YouTube rebuilt via `_youtube_embed_html`
  (honors the host preference), the rest sanitized in place (Bandcamp/SoundCloud
  esig/track signatures preserved verbatim). `_place_recovered_embeds` then puts
  each one **in context** rather than dumping them at the bottom: (1) replace a
  bare body link that points at the same media (so the player takes the place of
  the link the feed showed instead — matched by video id for YouTube, by the
  embed's fallback `<a href>` for Bandcamp/SoundCloud), (2) fill empty `<p></p>`
  placeholders that follow a heading (the stripped embed slots, e.g. theobelisk's
  `<h3>title</h3><p></p>`) in document order, (3) append leftovers. Mirrors the
  Reader-view recovery but for the normal entry pane.
- **Bandcamp single-track embeds** — Bandcamp's `.../tracks=<ids>/esig=<sig>/`
  player form is domain-locked: Bandcamp validates the Referer against the
  publisher's site and serves "Sorry, this track or album is not available."
  anywhere else (confirmed by headless test — the same iframe plays from the
  publisher domain but not from Lectio). `_strip_bandcamp_track_signature` drops
  the `tracks`/`esig` path segments so the embed falls back to the plain
  `album=<id>` player, which embeds on any site and streams the same pre-order/
  premiere album. Applied to feed-native and source-recovered embeds in
  `get_entry_detail`, and to both reader-view render paths.
- **Open-redirect guard** — the login `next` param is filtered by `_safe_next`
  (same-origin paths only) before redirecting.

Deferred behind hooks: per-user rate-limits/quotas on refresh, scraping, and
thumb generation (not needed for a handful of trusted users).

## Extension strategy

Use plugin/adapter style for non-native behavior instead of hardwired branching. Prefer replaceable pieces and avoid duplicating `reader` capabilities in app code.

## Feed URL normalization

`normalize_feed_url` (main.py) is applied at add-feed time and in the Duplicate scan (`GET /feeds/duplicates`). It handles:

- Trailing-slash stripping from paths longer than `/`.
- Format-selector query params (`alt=rss`, `alt=atom`, etc.) that select serialization without changing content — lets the Blogger Atom and RSS URLs of the same feed collapse to one.
- ArtStation subdomain rewrites (`username.artstation.com/rss` → `www.artstation.com/username.rss`) to avoid TLS hostname issues with underscore usernames.
- `_DOMAIN_ALIASES` map — known domain pairs that serve identical content, or renamed domains (`old.reddit.com` → `www.reddit.com`; `tapastic.com` → `tapas.io`). Add new pairs there; the normalization and duplicate-scan logic picks them up automatically.

**Curation migration on consolidation.** When the Duplicate scanner (`POST /feeds/deduplicate`) removes a slash/format-variant feed, `purge_orphaned_feed` first calls `_migrate_curation` to move the removed feed's manual tags and stars onto the surviving feed — matching each curated source entry to a survivor entry by GUID, else normalized link, else synthesizing it into the survivor (`reader.add_entry`) so nothing is lost. This is unconditional (independent of the opt-in "rescue unread" toggle, which only re-flags read/unread state) and mirrors the offline `scripts/reconcile_duplicate_feeds.py --merge` path.

**Import-time canonicalization.** `canonical_feed_url` (main.py) composes `normalize_youtube_feed_url` + `normalize_feed_url` and is the single choke point every bulk importer runs each incoming feed URL through *before* it subscribes or keys per-entry tag/star state. This makes a variant URL (old.reddit, `?alt=rss`, trailing slash) attach to an existing subscription instead of spawning a duplicate. It is wired into OPML import, the Inoreader local-file migrator, the shared migration applier `_apply_migration_items` (Miniflux/FreshRSS/Tiny Tiny RSS), the Inoreader JSON upload, and the Inoreader OAuth drip (subscriptions, label, and starred phases). Importers that key both subscription and tagging off `item["feed_url"]` call `_canonicalize_item_feed_urls(items)` once up front so both phases stay in sync. Google Takeout import is exempt: it only applies tags/stars to entries already present in the reader DB (never `add_feed`s), and those URLs are already canonical from the original subscription.

## DeviantArt integration

DeviantArt's legacy `backend.deviantart.com/rss.xml` is behind a CloudFront WAF that 403s datacenter traffic, so Lectio uses the DeviantArt API and renders results to `file://` RSS files like FakeFeedz (services/deviantart.py). Per-user creds live in app-settings.

**Bluesky image recovery** (`services/bluesky.py`): per-profile bsky.app RSS (`/profile/<did>/rss`) is text-only, and content-labeled posts (e.g. adult) also expose no og:image on the web page. The images live in the post record and are served from the public `cdn.bsky.app` CDN, so Lectio fetches them from the public AT Protocol API (`app.bsky.feed.getPosts`) keyed by the post's `at://` URI — which the RSS feed stores as the entry id. `extract_entry_thumbnail_url` uses the first image for the list thumbnail; `get_entry_detail` appends all images to the article body. No auth and no label check at this layer — subscribing to the account is the user's opt-in. Cached in-memory (1h TTL) so list rendering doesn't re-hit the API.

- **Auth** — OAuth2. Public galleries use the *client-credentials* grant; the *authorization_code* grant (PKCE — DeviantArt requires `code_challenge`) connects the user's account for watch-list access. Tokens are stored per-user and auto-refreshed; the token request tries with-secret then without, tolerating both confidential and public clients.
- **Watch feed** (preferred) — one combined feed from `/browse/deviantsyouwatch` (everyone you Watch), instead of one feed per artist. A few paginated calls per refresh keep it under DeviantArt's strict per-user rate limit (`DeviantArtRateLimited` aborts bulk work cleanly; the scheduled refresh is round-robin capped).
- **Add = Watch** — while connected, adding a `deviantart.com/<user>` URL Watches that artist on DeviantArt (it then appears in the Watch feed) rather than creating a per-artist feed.
- **Watch-list sync auto-resume** — `sync_deviantart_watchlist` is add-only and stops cleanly at the rate cap; instead of waiting for a re-click it schedules a background continuation (a daemon `threading.Timer` routed through `_run_in_user_context`) honoring the 429's `Retry-After` (conservative 15-min fallback), capped at 12 rounds per triggering run. A per-user in-process guard keeps the Settings button, the daily maintenance run, and a pending auto-resume from syncing concurrently. Timers don't survive a restart — the daily maintenance sync is the catch-up. The sync also reconciles: subscribed artists no longer on the watch list are reported in the status line and logs, never auto-unsubscribed (a curated feed may deliberately outlive a Watch).
- **Images** — deviations carry stable (non-expiring) signed `wixmp.com` image URLs. DA feeds are pinned to the `inline` strategy so the article lead image and list thumbnail derive statelessly from the embedded content image (no source-page scrape, nothing to clobber). `wixmp.com` is trusted in `_is_image_url_acceptable` (its long auto-generated filenames/UUIDs otherwise trip the avatar/ad heuristics) and routed through `/api/img`.
- The lead-image cache reads through to its DB table on a miss, so stored images survive restarts (the in-memory cache is seeded once under the default tenancy and otherwise warms lazily).
- The interactive on-open `queue_source_fetch` persists **only a positive result**. A `None` is ambiguous — a transient page-fetch failure is indistinguishable from a genuine "no image" — and this path runs once per opened entry with no retry, so storing `None` would cement a momentary miss as a permanent negative and blank a thumbnail the feed actually has (e.g. a Standard Ebooks cover, which lives in `media:thumbnail` and resolves via the page's `og:image`). Negative-recording is left to the background backfill, which retries on its own schedule.

## dev.to filtered feeds

Dev.to's RSS (front page and per-tag) is an unfiltered firehose that mixes languages, while its public unauthenticated JSON API (`GET https://dev.to/api/articles`) exposes a per-article `language` label, reaction counts, and a `top=N` ranking window. `services/devto.py` follows the DeviantArt/FakeFeedz synthetic-feed pattern: one polite API request per refresh, client-side filtering (the API ignores `?language=`; we filter on dev.to's *own* `language` field, deliberately not our own detection), then render to `file://` RSS under `DATA_DIR/devto-feeds/` for `reader` to ingest. Per-feed config (tag, top-window days, English-only, min reactions, tags_exclude) lives in the per-user meta table `devto_feeds`; the Add Feed dialog detects dev.to front-page/tag URLs client-side (mirroring `parse_devto_url` — user/org pages are left to their normal small RSS) and reveals the filter fields, and the config is editable later via feed Properties → Tuning (`POST /devto-feeds/{id}/config`). Cover images seed the lead-image cache via the same sink mechanism as DeviantArt; deletion is dispatched in `purge_orphaned_feed` alongside the other rendered-feed types. Filter changes shape what arrives from then on — already-ingested entries are kept.

## Duplicate entry suppression

Two mechanisms prevent duplicate articles from accumulating in the reader DB:

**GUID-churn suppression** (`_suppress_guid_churn`, runs after each refresh): detects entries that reappear with a new GUID but the same URL slug, or the same title + publication date (within 7 days). Checks both read history AND existing unread entries so that multiple copies arriving before any are opened are also caught.

**Intra-feed and cross-feed cleanup** (`_cleanup_intra_feed_slug_dupes`, runs at startup and after each refresh cycle): two-pass retroactive cleanup for duplicates that slipped through before suppression was in place or before Deduplicate rules ran.
- Pass 1: within each feed, keep the oldest entry per slug and per title+date; mark newer copies read.
- Pass 2: across all feeds, group entries by `normalize_entry_link_for_dedupe` (canonical URL after stripping tracking params); keep the oldest copy globally and mark the rest read. This handles syndicated posts that appear in multiple subscribed feeds (e.g. a blog post cross-posted to two feeds from the same author).

These run server-side and affect the underlying DB state, so third-party clients (Capy, etc.) see the clean state after the next sync.

## Entry sort window (Pub Old / Pub New)

`reader` only sorts newest-first, so for large folders (`> PER_FEED_QUERY_THRESHOLD`
feeds) `list_entries` fetches the sort window with a direct SQL query and then
enriches only the surviving rows. Both directions order by
`coalesce(published, first_updated)` so an entry that carries no `published`
falls back to when the reader first saw it instead of sorting as NULL. Previously
the ascending path ordered by raw `published`, and since SQLite sorts NULLs first
under `ASC`, date-less imported entries filled the `LIMIT` window and were then
re-dated to their (recent) import time — pushing genuinely old posts out of view.
Imports set a real `published` at ingest where possible: the Inoreader parser
(`_coerce_published`) falls back from the item's `published` to `crawlTimeMsec` /
`timestampUsec`, so newly imported entries carry their true age.

## Feed auto-taggers

Three functions run at startup to apply strategy and display defaults without user action:

- `_auto_tag_artwork_feeds()` — matches `artstation.com` and `deviantart.com` feed URLs → `strategy=artwork`.
- `_auto_tag_webcomic_feeds()` — matches feeds in folders whose name contains "comic" → `strategy=webcomic`. Artwork wins if both conditions apply.
- `_auto_tag_github_release_feeds()` — matches `github.com/*/releases.atom` URLs → `strategy=og_scrape` + `show_lead_image_as_thumb=0`. GitHub generates a unique social-preview card per release; thumbnails are suppressed because the card is contextual rather than a post image.

All three skip feeds where `feed_lead_image_strategy.manual=1` (user has explicitly chosen a strategy in Feed Properties). To add a new tagger, follow the same pattern and register it in `lifespan()`.

## Feed-provided tag suggestions (`entry_feed_tags`)

`reader` discards entry categories (RSS/Atom `<category>`) at ingest — its `Entry` type has no tags attribute — so Lectio captures them itself at the only point the raw feedparser result exists: `SanitizingFeedparserParser.__call__` (`services/reader_sanitize.py`). After `_process_feed`, the parser hands `(entry_id, tags)` pairs to an **injected sink** (`set_entry_tag_sink`, wired in `main` to `FeedTagService.record_entry_tags`), keeping services free of main/DB imports. Design notes:

- **Tenancy for free.** Parsing runs synchronously inside `reader.update_feed(s)`, always in a user context (request thread or `_run_in_user_context` background threads), so the sink's `get_meta_connection()` resolves the correct per-user meta DB at call time — the same guarantee `get_reader()` relies on. The service itself is tenancy-unaware (LeadImageService pattern).
- **Id mapping re-derives, never zips.** `_process_feed` skips unparsable entries, so positions don't line up; the capture re-derives each raw entry's reader id (`id`, falling back to `link` for RSS-family feeds) and keeps only ids present in the processed set. A sink failure is logged and swallowed — tag capture must never fail a feed parse.
- **Storage** (`services/feed_tags.py`): per-user table `entry_feed_tags(feed_url, entry_id, tag, first_seen_at, PK(feed_url, entry_id, tag))`. Tags are stored **raw** (case-preserving) and normalized to Lectio tag format (`normalize_tag_value`) only at display — the raw text is the data foundation for future tag-filtered feed adapters. Replace-per-entry semantics: re-seeing an entry replaces its rows (publisher edits propagate); entries absent from the current fetch window keep theirs. Rows are pruned on feed removal and follow feed-URL migrations (`_feed_url_tables`); no other retention.
- **UI.** The entry pane shows the captured tags as **[ + tag ▲ ▼ ]** chips in the tags row. The leading **+** applies the tag as a manual tag through the existing `/entries/tags` append pipeline (hidden when already applied). **▲/▼** POST `/rules/tag-filter/toggle` (`toggle_feed_tag_filter`), which edits the **feed-scoped** `tag_filter` rule in place: same sign → remove, opposite → flip; the rule is created **disabled** on first use — chips are a tuning surface, and the user arms the rule in Automation — and deleted when the spec empties; folder/global rules are never touched; chip edits never change the enabled flag. Only when the rule is already enabled does a chip edit apply the new spec to unread entries immediately (logged to automation history as a manual trigger). Active signs render lit via `feed_tag_filter_signs`. This replaced an earlier ephemeral implementation that re-fetched the live feed in a background thread and fuzzy-matched entries against an in-memory cache — the DB lookup is exact-key and instant.
- **Synthetic feeds** (dev.to, DeviantArt) don't write the table directly: they emit `<category>` elements in their generated RSS, which flows through the same parser capture — one code path. DeviantArt's browse/gallery API omits deviation tags (they need `/deviation/metadata` calls), so DA categories appear only when the tags field is present; the scraper has no tag source.
- **Tag-filter rule.** The `tag_filter` rule type in the rules engine (`highlight_keywords`) consumes this table to tame firehose feeds. The whole spec lives in `keyword` as one comma-separated field with three strengths: `-tag` **drops**; `+tag` (or bare) is a **good** tag — it rescues an entry from drops but its absence never cuts anything; `++tag` **requires** — tagged entries lacking every required tag are cut (opt-in whitelist). Commas — not spaces — separate, so multi-word tags are typed as-is (`+windows 11, -rust`) and `normalize_tag_value` hyphenates both sides before comparing. `_run_tag_filter` runs in `_run_automation_after_refresh` per refreshed feed in scope (and via dry-run/run-now) and auto-marks matching unread entries read (same suppression as `mark_as_read`, logged to automation history). The entry's author rides along as a pseudo-tag (`author_filter_token`: 'Steven Parker' → `by-steven-parker`), so author tokens work in every position and ▲/▼ controls render next to the author name in the entry header; an authored-but-untagged entry is filterable. Evaluation: requires first (tagged entry lacking all required tags → cut), then drops (a good or required tag rescues; `+android, -iphone` keeps a post tagged with both, and Samsung posts still flow since good tags don't whitelist); **untagged entries are always kept** — a feed that stops tagging must not have its whole firehose suppressed. It runs *after* `update_feed` (the tag sink fires during parse, so the table is populated by then).
- **Source-page fallback.** Entries whose feed never delivered `<category>` data (aged out of the publisher's feed window before capture, or a tag-stripping publisher) are tagged from the article page itself on open: `extract_page_tags` harvests `article:tag` / `keywords` / `parsely-tags` metas from the lead-image service's source-HTML cache (zero extra requests when primed); on a cache miss the entry-detail handler queues `queue_source_html_fetch` and the tags appear on the next open — the same deferral pattern as image captions. Harvested tags are persisted to `entry_feed_tags` like feed tags; the fallback only runs when the entry has no rows, so feed-provided tags stay authoritative.
- **Synthetic-feed gotcha (fixed):** dev.to/DeviantArt XML is regenerated from their per-user entry tables (`devto_entries`/`deviantart_entries`), not from the live API objects — tags must persist in those rows (comma-joined `tags` column) to come out as `<category>`. Re-seen articles backfill/refresh the stored tags while still in the API window.

## Lead image pipeline

`LeadImageService` (services/lead_images.py) resolves a hero image for each entry using a layered strategy:

1. **Feed-level strategy** (`feed_lead_image_strategy` table) — detected automatically and cached weekly. Values: `og_scrape`, `inline`, `media_rss`, `youtube`, `artwork`, `webcomic`, `none`, `unknown`. Two auto-taggers run at startup: `_auto_tag_artwork_feeds` (matches `artstation.com` URLs → `artwork`) and `_auto_tag_webcomic_feeds` (folder name contains "comic" → `webcomic`). Artwork wins over webcomic when both conditions apply. Manual overrides (`manual=1`) are never overwritten by either tagger.
2. **Plugin fallbacks** — site-specific handlers (e.g. YouTube thumbnail from video ID).
3. **Source-page scraping** — fetches the article URL, checks `og:image` / `twitter:image` meta tags (both `property=` and `name=` attribute order), preload hints, CSS background-image, then scored in-page `<img>` tags. A `<link rel="preload" as="image">` hint is used **only as a fallback when there is no acceptable `og:image`** — it's a perf hint and sites often preload an above-the-fold widget/chart that isn't the lead image (e.g. usafacts.org preloads an `answer-page-card` stats chart, which must not override the curated builder.io `og:image` hero). Body scanner decision order: (a) first valid image in document gets a +10 position bonus; (b) when an `<img>` sits inside a `<picture>` with a `<source type="image/webp">`, the WebP srcset URL is substituted as the candidate. Logo/site-chrome rejection uses `_LOGO_URL_PATTERNS` (word-boundary-aware — compound words like "imdblogo" are not rejected) and `_SITE_CHROME_PATH/DOMAIN_PATTERNS` (`www.blogger.com` is chrome-only — Blogger content images live on `bp.blogspot.com`/`googleusercontent.com`); SVG candidates are always skipped. `_SITE_CHROME_CONTEXT_RE` skips images whose preceding markup carries nav/dropdown/widget class names (menu icons and sidebar/footer widgets are never lead images). Before scoring, `_strip_related_post_blocks` removes whole balanced `div`/`section`/`aside`/`nav`/`ul` containers whose class names a related/recent/more-posts list (e.g. Hugo blogs' `related-content` widget, or a WordPress block-theme `wp-block-query` Query Loop) — the per-image context check only looks ~500 chars back, so a sibling post's thumbnail deep in such a list would otherwise win on pages that lack their own `og:image`/hero. Stripping `wp-block-query` is safe because a block theme renders the post's *own* featured image via `wp-block-post-featured-image` directly under `<article>`, never inside a Query Loop (which only lists other posts) — this also stops a pinned/featured sibling post from being picked on webcomic-strategy WordPress feeds (e.g. karlkerschl.com). The alt-text logo check is suppressed for images with explicit `width`/`height` attrs ≥ minimum dimensions, since publishers who size article images explicitly signal intentional placement. Additional URL/attribute rejections in `_is_image_url_acceptable`: `_SITE_CHROME_PATH_PATTERNS` includes a `/navigation/` asset-directory segment (header/menu icons, e.g. Paizo's `Personal-Account.png`); `_AD_URL_PATTERNS` + `_AD_ALT_PATTERNS` drop advertisement banners (filename `-ad1`/`/ads/`, or alt text "banner ad"/"advertisement"); the placeholder list covers `blank.{jpg,png,webp}` (WordPress.com's `s0.wp.com/i/blank.jpg` 200×200 white box shipped as og:image on image-less posts); and the logo safety-valve that lets a logo-named URL through on large embedded dimensions now also requires a content-like aspect ratio (0.25–4.0), so banner-shaped wordmarks like `logo-color-600x100` are still rejected. Two further refinements: (a) a `logo`-named image hosted **under the post's own URL directory** (passed as `source_url`) is treated as the post's own asset and skips the logo filter — site logos live at the site root or on a CDN, not under a specific post path, so a content hero like andreagrandi's `…/announcing-mcp-wire-0-3-0/mcp-wire-logo.png` is no longer dropped; (b) code-forge avatar URLs (`github.com/<user>.png`, `gitea.com/<user>.png`, gitlab/codeberg) — a single user segment + `.png` on the forge host — are rejected as profile pictures, so an election/announcement post that embeds candidate avatars doesn't pick one as its lead image (repo/asset paths have more segments and are unaffected). `_TRACKER_URL_PATTERNS` also rejects analytics pixels and social share-button sprites — `statcounter` (the `c.statcounter.com` `alt="Web Analytics"` 1×1 GIF that scales to a grey thumbnail on image-less posts) and `addtoany`/`addthis`/`sharethis` (e.g. `static.addtoany.com/buttons/share_save_171_16.png`, `alt="Share"`); because the tracker check runs even under `skip_logo_patterns=True`, the render cache-gate in `extract_entry_thumbnail_url` drops a *stale cached* statcounter/share URL on display without a DB rewrite. `_EMOJI_URL_PATTERNS` rejects emoji image sprites (`s.w.org/images/core/emoji/`, twemoji CDN) as lead images — they're decorative glyphs, not article content (but they survive inline; see below). When a lead image is rejected here, the alt/title that came with it is also suppressed at render via `_TRIVIAL_ALT_TEXTS` ("share", "web analytics", "analytics"), so an entry whose only "image" was a share button or tracking pixel shows neither a thumbnail nor a junk caption.

**Inline body-content rendering** (separate from lead-image selection): images that are rejected as lead images may still be legitimate *inline* content. Emoji sprites are kept in the body but constrained to ~1.2em via CSS (`.entry-content img.wp-smiley/.emoji/.ipsEmoji`) so they read as text-sized glyphs rather than the full-size 72×72 block the general `.entry-content img` rule would otherwise produce (e.g. IP.Board's `ipsEmoji` 🙃, which carries no inline size style of its own). All inline body images are also given `referrerpolicy="no-referrer"` (`add_no_referrer_to_images`, applied late in the entry-content pipeline; skipped for locally-served starred assets) so hotlink-protected hosts that serve a placeholder image on a foreign `Referer` return the real asset. `referrerpolicy` only fixes *fresh* loads, though — a browser that already cached a host's "image was hotlinked" placeholder under the unchanged image URL (these hosts send no `Vary`) keeps serving it. So for a small set of *known* hotlink hosts (`_HOTLINK_IMG_HOSTS`, e.g. nanolx.org), body-image `src` and the lead image are rewritten to the same-origin `/api/img?u=…` proxy (`proxy_hotlink_images`, `_lead_image_display_url`): the new URL isn't in the browser's cache, and the server-side proxy fetch carries no `Referer`, so the real image loads and stays correct. `srcset` is dropped on those imgs so the proxied `src` is the one used. Add a registrable domain to `_HOTLINK_IMG_HOSTS` to cover a new host (matches it and any subdomain).

**Same-origin Referer escalation** (the inverse hotlink case): some hosts do the opposite of the nanolx pattern — they *refuse* an image fetched with no `Referer` (HTTP 403, often a `text/html` body) but serve it 200 once a same-origin `Referer` is present (e.g. `fabiensanglard.net`'s `.webp` files, which `/api/img` would otherwise reject at the `content-type` gate → broken image). So both server-side image proxies (`api_img_proxy` for `/api/img`, `thumbnail_proxy` for `/thumb`) are **honest-first**: the first fetch carries only the honest `User-Agent`, and *only* if it comes back `403`/`503` do they retry once with `Referer: <scheme>://<host>/` (`_same_origin_referer`, the image's own origin root). This mirrors the honest-first WAF→browser-UA escalation in `services/lead_images.py` (`_BROWSER_USER_AGENT`): never preemptive, so hosts happy to serve us still see no `Referer`. The cache key ignores the `Referer` (the bytes are identical), so a hit skips the round trip entirely. The escalation only helps images that actually reach the proxy, though — the browser can't send a foreign site's own origin as `Referer`, so such hosts must be in `_HOTLINK_IMG_HOSTS` to have their `<img src>` rewritten to `/api/img?u=…` in the first place (`fabiensanglard.net` is listed for exactly this — its `.webp` files would otherwise load directly and 403, breaking them in reader/web view while its `.jpg` loads fine). `build_readability_response` (reader/web view) runs the same `proxy_hotlink_images` + `add_no_referrer_to_images` pass as the entry pane, after `_absolutize_article_urls` so host-matching sees absolute `src`.
4. **Inline feed content** — images embedded in `<content>` or `<summary>` elements. The render-triggered chunk backfill (`_do_backfill_entry_list`) does source-page fetches for `og_scrape`/`webcomic`/`unknown` feeds; when that fetch yields nothing it falls back via `_inline_from_reader` to the entry's own inline image rather than caching a blank. This rescues feeds whose pages are JS-only SPAs with no `og:image` (e.g. ArtStation) but which embed the artwork directly in the feed.

At render time, a feed pinned to `inline`/`media_rss` thumb strategy that extracts nothing also falls back to the cached lead image (`list_entries` in main.py) instead of showing a blank — important for feeds whose `thumb_strategy` was auto-detected as `media_rss` but whose reader `Entry` objects carry no `media:*` fields.

**ComicControl thumb→full promotion**: many ComicControl-CMS webcomics (e.g. atomic-robo.com, everblue-comic.com) ship only a small `/comicsthumbs/<file>` image in the RSS enclosure while the full-resolution panel is the same filename under `/comics/<file>` (page `id="cc-comic"`). These feeds may be pinned to `webcomic` strategy (whose source-scrape already stores the `/comics/` URL in the cache) but `_derive_article_lead_image` derives the *article* lead from the inline image, not the cache — so the article showed the small enclosure thumb. `LeadImageService._promote_known_thumbnail` rewrites the `/comicsthumbs/` path segment to `/comics/` (exact-segment lookbehind/lookahead, idempotent) on every thumbnail return, the cached-only read (`get_cached_entry_thumbnail`), and the inline-image path (`extract_inline_thumb_url`); `_apply_feed_content_cleanups` applies the same rewrite to inline body images. So the list thumbnail, the article lead, and the in-body image all show the readable full panel without an extra fetch. **Timestamp-mismatch caveat**: ComicControl filenames carry a cache-bust unix-timestamp prefix (`1782426356-ARV1701_05.jpg`), and the thumb and the full panel are often generated a second apart, so their prefixes differ (`comicsthumbs/…356-…` vs `comics/…355-…`). A naive directory swap keeps the thumb's timestamp, and ComicControl answers that nonexistent timestamp with a **200 HTML page** (not the image), so `/api/img` rejects it (422) and the comic breaks. `_promote_comicsthumbs_in_content` therefore substitutes the resolved full lead image URL (the real `/comics/<ts>-<file>` read from the page, looked up via `get_cached_lead_image_url`) whenever its timestamp-stripped filename (`_comiccontrol_stable_name`) matches the body thumb's; it only falls back to the directory swap when no lead image is cached yet.

Relatedly, `_is_image_url_acceptable` rejects show-title branding graphics (`podcast-title*`, added to `_SITE_CHROME_PATH_PATTERNS`, which is checked even on cached `skip_logo_patterns` reads): og:scrape falls back to one of these on a post with no real featured image — e.g. a WordPress `?preview=true` entry that leaked into the feed — so the article shows no image rather than the site's podcast logo.

The in-memory cache is warmed at startup **per enabled user** (`_for_each_background_user("lead-image cache warm", ...)`): lead images live in each tenant's own `entry_lead_images` table, and the render path consults only the shared in-memory cache (no per-user DB read), so warming bare against the default tenant would leave every other user's thumbnails blank until the rate-limited background backfill caught up after each restart.

For webcomics, the main comic panel is the lead image and takes priority over both the publisher's `og:image` and any RSS enclosure thumbnail. `_fetch_source_lead_image` calls `_extract_webcomic_panel_image` first when `is_webcomic` is set: it strips related/recent/Query-Loop post listings (`_strip_related_post_blocks`) and then returns the `<img>` matched by `_WEBCOMIC_IMG_ID_RE`/`_CLASS_RE` (e.g. ComicControl's `id="cc-comic"`) before the `og:image` early-return — many webcomic CMSes set a single generic site banner as `og:image` on every page with a sane aspect ratio, which would otherwise win. The related-block strip matters because `_CLASS_RE` matches WordPress's `wp-post-image`, so on a block-theme WordPress feed a sibling post's featured thumbnail in a `wp-block-query` loop would otherwise be returned as the panel; when no own panel survives the strip, resolution falls through to the regular scored body scan. For the same reason, backfill (`fetch_and_store_lead_images_for_feed`) treats `webcomic` like `og_scrape`-manual: it falls through the inline/enclosure image (typically a small `/comicsthumbs/` variant with no hover text) to the source-page fetch so the full-resolution panel and its alt/title win, and skips the feed-XML media-thumbnail lookup entirely (the enclosure is the same small thumbnail). `_extract_webcomic_alt_text` then surfaces the hover-text punchline: it checks the WordPress `comic-alt-text` balloon, then the `title`/`alt` attribute of the main comic `<img>` (matched by `_WEBCOMIC_IMG_ID_RE`/`_CLASS_RE`, e.g. SMBC's `id="cc-comic"`), and only then falls back to `og:description` (which on many comic sites is just the post title). At render time, captions that merely restate the entry title are dropped — including auto-generated banner captions that pad the title with a decorative word and/or date (e.g. "Progress Update Banner 2026-06-06" for a post titled "Progress Update 6/06/2026").

Results are stored in `entry_lead_images (feed_url, entry_id, image_url, image_alt, image_title, fetched_at)`. `image_alt` and `image_title` hold the raw `alt` and `title` HTML attributes from the matching `<img>` tag on the source page, stored separately so the user can choose which to display via the `caption_source` feed preference (`feed_display_prefs.caption_source`: `auto` / `alt` / `title` / `both` / `none`). NULL image_url means "no image found." Negative results are retried after **4 hours** (`_NEGATIVE_RETRY_SECONDS`); positive results are revalidated after 12 hours (`_POSITIVE_REVALIDATE_SECONDS`). An existing non-NULL URL is never overwritten with NULL during revalidation. Likewise on first resolution: an `og_scrape`-**manual** feed stores the inline feed image and then falls through to the authoritative source-page fetch, but a transient source miss must not clobber that inline image with NULL — otherwise a brand-new post (whose `og:image` isn't generated yet at first fetch) loses its thumbnail until the 4-hour negative retry. The NULL negative is only recorded when there was no inline image either.

First-open availability: when `queue_source_fetch` (the lead-**image** fetch) is called for a new entry, it posts a `threading.Event` keyed by `(feed_url, entry_id)`. The entry render path calls `wait_for_source_fetch(..., timeout=0.8)` immediately after queuing so the lead image — which the user sees right away — fills on the very first open for fast sites, capped low enough that slow hosts (Squarespace, WordPress.com) fall through and fill on the next open instead.

The **caption** source-HTML fetch (`queue_source_html_fetch` → `fetch_entry_image_caption`) is, by contrast, fully asynchronous: when the source HTML isn't already cached, the render queues the background fetch (which both primes the HTML cache and persists the alt/title to `entry_lead_images`) and returns immediately — it does **not** call `wait_for_source_html_fetch`. The caption appears on the next open from the persisted value. This was previously a `wait_for_source_html_fetch(..., timeout=3.0)` blocking call, which stalled first-open by up to 3s on og_scrape feeds (e.g. mynorthwest) purely to maybe show a caption that gets persisted for next time anyway; removing the wait is the cache-first/defer fix (the lead image still uses the brief 0.8s wait above since it's the user-visible payload). The narrower `inject_source_images` gallery path keeps a 0.8s `wait_for_source_html_fetch` since it's gated on an opt-in per-feed pref.

The strategy comparison cache (`feed_strategy_cache`) also stores `image_alt` and `image_title` per strategy so the Tuning tab can display them below each card without a live fetch.

SmartCrop's `min_scale` is a per-feed preference (`feed_display_prefs.smart_min_scale`, NULL = default 0.9), set in Feed Properties next to the thumb fit mode and passed to the `/thumb` proxy as the `ms` query param; it was previously a global app setting. The min_scale is part of the thumb cache key, so changing it regenerates that feed's Smart thumbnails.

Fill mode's `fill_zoom` multiplier (`feed_display_prefs.fill_zoom`, NULL = default 1.0, range 0.5–2.0) scales the cover-crop resize step before the anchor-crop. Values below 1.0 produce a letterbox (image pasted on a black canvas); values above 1.0 crop more aggressively than the default tight fill. Passed to `/thumb` as the `fz` query param and included in the cache key for cover-family modes.

**Direct-load fallback:** `/thumb` fetches the source image *from the server*, so a host that IP-blocks datacenter traffic (e.g. Cloudflare 403, washingtonstatestandard.com) makes `/thumb` 502 and the list thumbnail break — even though the browser's own (residential) IP can fetch the image fine. The list `<img>` carries the raw image URL in `data-direct`; on a `/thumb` error its `onerror` (`window.thumbImgFallback`, defined pre-body so it exists before any load fails) retries once with that direct URL, letting the browser load the image itself. CSS `object-fit:cover` sizes the un-resized image to the tile. This recovers the thumbnail without evading the block server-side (it's the user's own client fetching, exactly as the article view already does). Only `http(s)` direct URLs are retried, and only once (a `data-triedDirect` guard prevents an error loop); if the direct load also fails, the tile collapses to `is-empty` as before. The same helper backs the JS-derived list thumbnail (it sets `data-direct` to the lead-image URL).

## Async bulk mark-read

`/feeds/mark-read`, `/folders/mark-read`, and `/entries/mark-older-than-read` serve two response modes controlled by the `X-Requested-With` request header:

- **`lectio-mark-read`** (sent by the JS fetch path): returns `{"ok": true, "marked": N, ...}` with HTTP 200. The client applies an optimistic in-place read-state update via `applyBulkReadState()` before the fetch completes.
- **Anything else** (native form submit fallback): returns an HTTP 303 redirect to the main page with a `message=` query param.

The JS layer reads the CSRF token explicitly from `<meta name="csrf-token">` and adds it as `X-CSRF-Token` on every async POST.

## GReader API

`GReaderService` (`services/greader.py`) implements the Google Reader-compatible protocol used by Capy, Readrops, Aggregator, Read You, and many other clients.

**Auth:** `POST /greader/accounts/ClientLogin` accepts `Email` and `Passwd` form fields. Email may be bare username or `user@domain` (the local part is matched). Returns `SID/LSID/Auth` tokens (all identical) in the Fever-style `key=value` plain-text format. Tokens are cached in memory and persisted to `greader_tokens (token TEXT PK, expires_at REAL)` in the meta DB (90-day expiry). On restart, `check_token()` falls back to the DB on an in-memory cache miss and re-warms the cache, so clients are not logged out by container restarts or deploys. Subsequent requests pass `Authorization: GoogleLogin auth=<token>`.

**Shared ID table:** Reuses `fever_entry_map` for stable integer IDs — no additional DB table. GReader item IDs are the decimal integer for `itemRefs.id` and `tag:google.com,2005:reader/item/<16-char-hex>` for item content responses. All three input formats (decimal, `0x<hex>`, full tag URI) are parsed in `_parse_item_id`.

**Stream IDs:** `user/-/state/com.google/reading-list` (all), `user/-/state/com.google/read`, `user/-/state/com.google/starred`, `feed/<url>`, `user/-/label/<folder>`. Exclusion tag `xt=user/-/state/com.google/read` filters unread-only.

**Endpoints:**
- `GET /greader/reader/api/0/user-info` — user identity
- `GET /greader/reader/api/0/tag/list` — folders as labels + built-in states
- `GET /greader/reader/api/0/subscription/list` — feeds with folder membership
- `GET /greader/reader/api/0/unread-count` — per-feed and per-folder unread counts
- `GET /greader/reader/api/0/token` — action token (returns auth token)
- `GET /greader/reader/api/0/stream/items/ids` — paginated item ID list
- `POST /greader/reader/api/0/stream/items/contents` — item content by IDs
- `GET /greader/reader/api/0/stream/contents/{stream_id:path}` — combined IDs + content
- `POST /greader/reader/api/0/edit-tag` — mark read/unread/starred/unstarred
- `POST /greader/reader/api/0/mark-all-as-read` — bulk mark read (background thread)
- `POST /greader/reader/api/0/subscription/edit` and `/quickadd` — stub OK responses

**Pagination:** `?n=<count>` (default 20, cap 10,000), `?c=<continuation>` (published-timestamp in microseconds of the last returned item). `?r=o` reverses order to oldest-first.

**Feed titles:** subscription-list and item-origin titles use the user's overridden feed name (`user_title`) when set, falling back to reader's real title — so synced clients (Capy, etc.) match the sidebar. Note the sync APIs still serve reader's **raw** entry HTML; Lectio's render-time content customizations (sanitization allowlist, lead-image injection, caption/thumbnail strategies) are applied in the web UI only and are not reflected in synced item content.

**Credential sharing:** Uses the same `LECTIO_FEVER_PASSWORD` env var as the Fever API — one API password covers both protocols.

## Fever API

`FeverService` (`services/fever.py`) implements the [Fever RSS API](https://feedafever.com/api) for third-party client compatibility (Reeder, FeedMe, NetNewsWire, etc.).

**Auth:** The Fever protocol sends `md5(username:password)` as `api_key` on every request. Lectio uses a dedicated `LECTIO_FEVER_PASSWORD` (not the main login) to limit the exposure of MD5-hashed credentials. The computed key is compared with `hmac.compare_digest` for timing safety.

**Integer ID mapping:** The `reader` library uses opaque string entry IDs and URL-keyed feeds. Fever requires stable integers. Three mapping tables in the meta DB handle this:
- `fever_feed_map (id AUTOINCREMENT, feed_url UNIQUE)` — per-feed integer IDs
- `fever_group_map (id AUTOINCREMENT, title UNIQUE)` — per-folder integer IDs
- `fever_entry_map (id AUTOINCREMENT, feed_url, entry_id, UNIQUE(feed_url, entry_id))` — per-entry integer IDs

Entries are synced into `fever_entry_map` on first service use (background pre-sync at startup) and incrementally per-feed after each refresh via `sync_feed_entries`.

**Endpoint:** `GET /fever` and `POST /fever`. Clients configure the server URL as `https://your-lectio-host/fever`. All Fever operations are dispatched from a single `_fever_handler` in `main.py` that parses params from both query string and form body.

**Supported operations:** feeds, groups, items (`since_id` / `max_id` / `with_ids`), `unread_item_ids`, `saved_item_ids`, `links` (empty), `favicons` (empty), and mark actions (item read/unread/saved/unsaved, feed-before-timestamp, group-before-timestamp).

Storage: `fever_feed_map`, `fever_group_map`, `fever_entry_map` in the meta DB. System folders (prefixed `_`) are excluded from groups.

## WebSub (PubSubHubbub)

`WebSubService` (`services/websub.py`) implements the WebSub subscriber protocol:

1. **Hub discovery** — on feed add and periodically during refresh, `_discover_hub_url` fetches the feed URL and looks for `rel="hub"` in the HTTP `Link` header or in `<atom:link>` / `<link>` XML elements. A "no hub found" attempt is recorded in `websub_subscriptions.hub_tried_at` so the check is not repeated for 7 days.
2. **Subscription** — `subscribe(feed_url, hub_url)` posts `hub.mode=subscribe` with a random HMAC secret and a 7-day lease request. The row is written as `verified=0` until the hub confirms.
3. **Verification callback** (`GET /websub/callback`) — hub sends `hub.challenge`; `handle_verification` confirms the topic matches, marks the row `verified=1`, and echoes the challenge. FastAPI query-alias params (`hub.mode`, `hub.topic`, `hub.challenge`, `hub.lease_seconds`) map the dot-notation params cleanly.
4. **Push callback** (`POST /websub/callback`) — hub delivers content; `verify_push_signature` checks HMAC-SHA256 (or SHA1 fallback) against the stored secret. On success, `feed_refresh_service.update_feeds([feed_url])` runs so the full pipeline (dedup, automation, lead images) runs on the fresh content.
5. **Lease renewal** — `renew_expiring_subscriptions()` re-subscribes any verified row whose `expires_at` is within 24 hours. Called each refresh cycle.

**Multi-user fan-out:** the callback URL carries only the topic (no user). `websub_subscribers` lists every user subscribed to a feed; both callbacks fan out across those rows rather than acting on a single tenant. `_websub_verify_fanout` confirms the handshake for whichever user(s) have a matching pending subscription; `_process_websub_push` collects every user with a verified subscription for the topic, confirms the push is authentic against *any* of their secrets (a forged push matches none), then refreshes each subscriber under its own tenancy context in a daemon thread. Because the shared callback means the hub only retains the most recent subscriber's secret, validating against any one secret and fanning the refresh out to all subscribers is what lets several users share a single hub subscription.

The service is initialized only when `LECTIO_PUBLIC_URL` is set; all integration points in `main.py` guard on `if websub_service`. On feed removal, `purge_orphaned_feed` sends an active unsubscription request to the hub (best-effort HTTP POST; hubs expire leases anyway if the request is lost).

**Feed removal lifecycle:** `purge_orphaned_feed(reader, conn, feed_url, *, archive_pending, rescue_to)` is the single canonical sequence run whenever a feed leaves the system (confirmed orphaned — no remaining `folder_feeds` rows). Steps in order: (1) force-archive pending saved/starred entries; (2) rescue unread entries into a kept/canonical feed; (3) dispatch the delete via the appropriate path (DeviantArt rendered feed → `deviantart_service.delete_deviantart_feed`; dev.to rendered feed → `devto_service.delete_devto_feed`; scraped/FakeFeedz feed → `scraper_service.delete_scraped_feed`; plain feed → `reader.delete_feed`); (4) WebSub unsubscribe. Callers set `archive_pending=False` when entries survive under a kept URL (dedup, format-upgrade), and pass `rescue_to` to migrate unread state. The helper takes an already-open `reader` and `conn` so callers control the `with` scope and context-manager nesting is never doubled.

**Folder deletion:** `delete_folder(folder_id, feed_action, move_to_folder_id)` deletes a folder and its descendants. When the folder holds feeds the UI prompts for their fate: `feed_action="unsub"` (default) purges feeds that end up orphaned via `purge_orphaned_feed`; `feed_action="move"` reassigns every affected feed to `move_to_folder_id` without unsubscribing. A target of `UNCATEGORIZED_FOLDER_ID` (or the root folder) leaves feeds folderless (Uncategorized). Returns `(deleted_folder_count, unsubscribed_count, moved_count)`. The empty-folder case skips the prompt (simple confirm).

**Push indicator:** `get_push_active_feed_urls()` queries `websub_subscriptions` for `verified=1 AND hub_url IS NOT NULL` in one pass and returns a `set[str]`; the index route threads this into the template context so both the sidebar feed tree and Settings → Feeds can render the ⚡ glyph without per-feed queries.

Storage: **shared** `lectio_websub.sqlite` (not per-user), two tables:
- `websub_subscriptions (feed_url TEXT PK, hub_url, secret, lease_seconds, subscribed_at, expires_at, verified, hub_tried_at)` — one row per feed, one active hub subscription regardless of how many users subscribe to that feed.
- `websub_subscribers (feed_url, user_id, PRIMARY KEY (feed_url, user_id))` — the N-user fan-out list; push and verification callbacks iterate this table.

Startup migration copies legacy per-user `websub_subscriptions` rows idempotently into the shared DB.

## Security direction

Keep the local-first path simple. Add auth only when exposing the app beyond trusted local use. The multi-user phase makes per-user identity, per-user API tokens, route-level authorization, and SSRF hardening mandatory — see "Multi-user tenancy → Security posture".
