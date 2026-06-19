# Lectio Architecture

Lectio is a local-first, single-user RSS reader built around the `reader` Python library. The goal is a fast triage workflow that can later grow into VPS deployment without a rewrite.

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

## Adaptive layout model

Lectio uses responsive layouts rather than a fixed three-pane assumption:
- wide desktop: 3-pane side-by-side,
- medium tablet landscape: 2-pane refinement,
- narrow phone portrait: 1-pane drill-in navigation.

The priority is fast triage, not always showing three panes.

## Deployment path

Current target is local-first single-user, with optional login auth behind a reverse proxy for VPS deployment. The next phase is multi-user (see "Multi-user tenancy"), introduced as a storage-layer strategy so the single-user path stays the zero-config default and the route layer does not require a rewrite.

## Multi-user tenancy

Lectio is single-user today. Multi-user is introduced as a **storage-layer
strategy behind a resolver**, so the UI/API and service layers never learn which
tenancy mode is active. Two modes, one interface:

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

Implementation status: the resolver and per-user connection pools exist
(`services/tenancy.py`; `get_reader()` / `get_meta_connection()` /
`get_starred_archive_connection()` in main.py resolve through it). The current
user is a `contextvars.ContextVar` that defaults to `DEFAULT_USER_ID`.

`LECTIO_SECURITY_MODE` selects the posture:

- **single** (default) — legacy single-user; the `LECTIO_USERNAME`/`PASSWORD`
  env credential gates the login. The tenancy context never leaves
  `DEFAULT_USER_ID`, so behavior is identical to before multi-user existed.
- **multi** — accounts live in a global users table (`lectio_auth.sqlite`,
  `services/users.py`, NOT routed through tenancy). Each account has a stable,
  immutable **`user_id`** (an opaque slug generated at creation) and a mutable
  **`username`**. The `user_id` is the identity everything keys on — the tenancy
  key, the on-disk directory (`users/<user_id>/`), the session value, and the
  foreign key for API tokens — so a username can be renamed
  (`UserStore.rename_user`, admin UI) without moving any data. Auth lookups take a
  typed username and return a `user_id`; the rest of the system passes `user_id`.
  Passwords are hashed by
  `services/passwords.py` (scheme via `LECTIO_PASSWORD_HASH_SCHEME`: `scrypt`
  default, `pbkdf2_sha256`, or `argon2` if `argon2-cffi` is installed; hashes are
  self-describing and transparently re-hashed to the configured scheme on login).
  On first startup with an empty table, an admin is seeded from
  `LECTIO_ADMIN_USERNAME`/`LECTIO_ADMIN_PASSWORD` (default `admin`/`ChangeA$ap`,
  with a loud warning if the default password is used). Login binds
  `session["user_id"]`; `_TenancyMiddleware` (pure-ASGI, innermost) binds that
  user into the tenancy context around the endpoint, so every storage access
  routes to the user's own DBs. A username doubles as the tenancy `user_id` and a
  path segment, so it must match the resolver's slug charset.

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
"not stick" for the real user across refreshes.

Account UI: `/account` (multi mode only; 404 in single) lets a user change their
password and view/regenerate their API token; admins additionally create/disable
users and reset passwords. New users are provisioned (`provision_user_storage`)
on creation.

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
and must fan out to its subscribers) still runs as the default user; linking
`/account` from the main settings UI; and the data migration of the existing
single-user DBs into a user. (SSRF hardening of `/api/img` and `/thumb` has
landed — see "Security posture". The WebSub discover-on-subscribe spawned when a
feed is added now re-binds the requesting user via `_run_in_user_context`.)

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

### Integrations in multi mode

The Resend **API key** is instance-shared (`get_resend_api_key` keeps its env
fallback) — one verified domain owned at the instance level. Everything else is
per-user: the email **From** identity (`get_resend_from`, no env fallback), the
default recipient, contacts, profile, and Instapaper credentials. The env values
(`LECTIO_EMAIL_FROM`, `LECTIO_EMAIL_TO`) seed only the bootstrap admin's settings
(`_seed_admin_integrations_from_env`) and are then ignored for per-user reads, so
one user's sender/account never becomes another's default.

### What stays global in every mode

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

Multi-user makes these structural changes mandatory (not optional hardening):

- **Per-user identity** — a users table with argon2/bcrypt hashing replaces the
  single `LECTIO_USERNAME`/`PASSWORD` env credential, which is demoted to a
  first-admin bootstrap seed. `session["authenticated"]` becomes
  `session["user_id"]`.
- **Per-user API tokens** — Fever/GReader cannot share one `LECTIO_FEVER_PASSWORD`
  once there is more than one user; the protocols derive everything from it.
- **Authorization** — every per-user route scopes by `user_id`. This is the
  largest code surface, but the resolver localizes it to the storage seam.
- **SSRF hardening** — `url_guard.safe_get` / `safe_get_async` follow redirects
  manually and re-validate every hop against private/loopback/link-local space.
  Now applied to all reachable user/feed-controlled fetches: `/api/img`, `/thumb`,
  feed discovery (`_guarded_get` / `_guarded_head`, which also pre-validate HEAD
  probes), the source-proxy / readability / feed-tag fetches in main.py, and the
  service-layer background fetches (lead-image plugins, lead-image source-page
  fetch, the page scraper, and the starred-archive text/byte fetches) — all with
  `follow_redirects=False`, closing the redirect-to-internal bypass. HEAD probes
  (image-fetchability / comic-URL checks) go through `url_guard.safe_head`, which
  validates the target and fetches `follow_redirects=False` (HEAD has no per-hop
  counterpart to `safe_get`).
  Still open: WebSub hub fetches and the `reader` library's own feed refresh (a
  subscribed `http://10.x` host is still fetched); and full DNS-rebind closure
  needs connection IP-pinning (the validate→connect window is small but nonzero).
- **Subscription scheme allowlist** — user-supplied feed URLs (Add Feed, OPML
  import, discovered `<link>` candidates) are restricted to http/https via
  `_is_subscribable_feed_url`. `reader` natively fetches `file://`, so without
  this an `xmlUrl="file:///…"` could read local files (other tenants' DBs, `.env`)
  on refresh. Internal scraped feeds still register their `file://` URLs through
  `reader.add_feed` directly, bypassing the user-facing guard.
- **HTML sanitization** — proxied source-page and Readability HTML (rendered with
  `| safe`) is sanitized by `_sanitize_html_allowlist`, a BeautifulSoup
  tag/attribute allowlist that drops scriptable tags, all `on*` handlers, `style`,
  and `javascript:`/`vbscript:`/`data:` URLs (incl. control-char-obfuscated). It
  replaced regex sanitizers that let unquoted handlers and `href="javascript:"`
  through. Feed-entry content relies on feedparser's upstream sanitization.
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

## DeviantArt integration

DeviantArt's legacy `backend.deviantart.com/rss.xml` is behind a CloudFront WAF that 403s datacenter traffic, so Lectio uses the DeviantArt API and renders results to `file://` RSS files like FakeFeedz (services/deviantart.py). Per-user creds live in app-settings.

- **Auth** — OAuth2. Public galleries use the *client-credentials* grant; the *authorization_code* grant (PKCE — DeviantArt requires `code_challenge`) connects the user's account for watch-list access. Tokens are stored per-user and auto-refreshed; the token request tries with-secret then without, tolerating both confidential and public clients.
- **Watch feed** (preferred) — one combined feed from `/browse/deviantsyouwatch` (everyone you Watch), instead of one feed per artist. A few paginated calls per refresh keep it under DeviantArt's strict per-user rate limit (`DeviantArtRateLimited` aborts bulk work cleanly; the scheduled refresh is round-robin capped).
- **Add = Watch** — while connected, adding a `deviantart.com/<user>` URL Watches that artist on DeviantArt (it then appears in the Watch feed) rather than creating a per-artist feed.
- **Images** — deviations carry stable (non-expiring) signed `wixmp.com` image URLs. DA feeds are pinned to the `inline` strategy so the article lead image and list thumbnail derive statelessly from the embedded content image (no source-page scrape, nothing to clobber). `wixmp.com` is trusted in `_is_image_url_acceptable` (its long auto-generated filenames/UUIDs otherwise trip the avatar/ad heuristics) and routed through `/api/img`.
- The lead-image cache reads through to its DB table on a miss, so stored images survive restarts (the in-memory cache is seeded once under the default tenancy and otherwise warms lazily).

## Duplicate entry suppression

Two mechanisms prevent duplicate articles from accumulating in the reader DB:

**GUID-churn suppression** (`_suppress_guid_churn`, runs after each refresh): detects entries that reappear with a new GUID but the same URL slug, or the same title + publication date (within 7 days). Checks both read history AND existing unread entries so that multiple copies arriving before any are opened are also caught.

**Intra-feed and cross-feed cleanup** (`_cleanup_intra_feed_slug_dupes`, runs at startup and after each refresh cycle): two-pass retroactive cleanup for duplicates that slipped through before suppression was in place or before Deduplicate rules ran.
- Pass 1: within each feed, keep the oldest entry per slug and per title+date; mark newer copies read.
- Pass 2: across all feeds, group entries by `normalize_entry_link_for_dedupe` (canonical URL after stripping tracking params); keep the oldest copy globally and mark the rest read. This handles syndicated posts that appear in multiple subscribed feeds (e.g. a blog post cross-posted to two feeds from the same author).

These run server-side and affect the underlying DB state, so third-party clients (Capy, etc.) see the clean state after the next sync.

## Feed auto-taggers

Three functions run at startup to apply strategy and display defaults without user action:

- `_auto_tag_artwork_feeds()` — matches `artstation.com` and `deviantart.com` feed URLs → `strategy=artwork`.
- `_auto_tag_webcomic_feeds()` — matches feeds in folders whose name contains "comic" → `strategy=webcomic`. Artwork wins if both conditions apply.
- `_auto_tag_github_release_feeds()` — matches `github.com/*/releases.atom` URLs → `strategy=og_scrape` + `show_lead_image_as_thumb=0`. GitHub generates a unique social-preview card per release; thumbnails are suppressed because the card is contextual rather than a post image.

All three skip feeds where `feed_lead_image_strategy.manual=1` (user has explicitly chosen a strategy in Feed Properties). To add a new tagger, follow the same pattern and register it in `lifespan()`.

## Lead image pipeline

`LeadImageService` (services/lead_images.py) resolves a hero image for each entry using a layered strategy:

1. **Feed-level strategy** (`feed_lead_image_strategy` table) — detected automatically and cached weekly. Values: `og_scrape`, `inline`, `media_rss`, `youtube`, `artwork`, `webcomic`, `none`, `unknown`. Two auto-taggers run at startup: `_auto_tag_artwork_feeds` (matches `artstation.com` URLs → `artwork`) and `_auto_tag_webcomic_feeds` (folder name contains "comic" → `webcomic`). Artwork wins over webcomic when both conditions apply. Manual overrides (`manual=1`) are never overwritten by either tagger.
2. **Plugin fallbacks** — site-specific handlers (e.g. YouTube thumbnail from video ID).
3. **Source-page scraping** — fetches the article URL, checks `og:image` / `twitter:image` meta tags (both `property=` and `name=` attribute order), preload hints, CSS background-image, then scored in-page `<img>` tags. Body scanner decision order: (a) first valid image in document gets a +10 position bonus; (b) when an `<img>` sits inside a `<picture>` with a `<source type="image/webp">`, the WebP srcset URL is substituted as the candidate. Logo/site-chrome rejection uses `_LOGO_URL_PATTERNS` (word-boundary-aware — compound words like "imdblogo" are not rejected) and `_SITE_CHROME_PATH/DOMAIN_PATTERNS` (`www.blogger.com` is chrome-only — Blogger content images live on `bp.blogspot.com`/`googleusercontent.com`); SVG candidates are always skipped. `_SITE_CHROME_CONTEXT_RE` skips images whose preceding markup carries nav/dropdown/widget class names (menu icons and sidebar/footer widgets are never lead images). Before scoring, `_strip_related_post_blocks` removes whole balanced `div`/`section`/`aside`/`nav`/`ul` containers whose class names a related/recent/more-posts list (e.g. Hugo blogs' `related-content` widget) — the per-image context check only looks ~500 chars back, so a sibling post's thumbnail deep in such a list would otherwise win on pages that lack their own `og:image`/hero. The alt-text logo check is suppressed for images with explicit `width`/`height` attrs ≥ minimum dimensions, since publishers who size article images explicitly signal intentional placement. Additional URL/attribute rejections in `_is_image_url_acceptable`: `_SITE_CHROME_PATH_PATTERNS` includes a `/navigation/` asset-directory segment (header/menu icons, e.g. Paizo's `Personal-Account.png`); `_AD_URL_PATTERNS` + `_AD_ALT_PATTERNS` drop advertisement banners (filename `-ad1`/`/ads/`, or alt text "banner ad"/"advertisement"); the placeholder list covers `blank.{jpg,png,webp}` (WordPress.com's `s0.wp.com/i/blank.jpg` 200×200 white box shipped as og:image on image-less posts); and the logo safety-valve that lets a logo-named URL through on large embedded dimensions now also requires a content-like aspect ratio (0.25–4.0), so banner-shaped wordmarks like `logo-color-600x100` are still rejected. Two further refinements: (a) a `logo`-named image hosted **under the post's own URL directory** (passed as `source_url`) is treated as the post's own asset and skips the logo filter — site logos live at the site root or on a CDN, not under a specific post path, so a content hero like andreagrandi's `…/announcing-mcp-wire-0-3-0/mcp-wire-logo.png` is no longer dropped; (b) code-forge avatar URLs (`github.com/<user>.png`, `gitea.com/<user>.png`, gitlab/codeberg) — a single user segment + `.png` on the forge host — are rejected as profile pictures, so an election/announcement post that embeds candidate avatars doesn't pick one as its lead image (repo/asset paths have more segments and are unaffected). `_TRACKER_URL_PATTERNS` also rejects analytics pixels and social share-button sprites — `statcounter` (the `c.statcounter.com` `alt="Web Analytics"` 1×1 GIF that scales to a grey thumbnail on image-less posts) and `addtoany`/`addthis`/`sharethis` (e.g. `static.addtoany.com/buttons/share_save_171_16.png`, `alt="Share"`); because the tracker check runs even under `skip_logo_patterns=True`, the render cache-gate in `extract_entry_thumbnail_url` drops a *stale cached* statcounter/share URL on display without a DB rewrite. `_EMOJI_URL_PATTERNS` rejects emoji image sprites (`s.w.org/images/core/emoji/`, twemoji CDN) as lead images — they're decorative glyphs, not article content (but they survive inline; see below). When a lead image is rejected here, the alt/title that came with it is also suppressed at render via `_TRIVIAL_ALT_TEXTS` ("share", "web analytics", "analytics"), so an entry whose only "image" was a share button or tracking pixel shows neither a thumbnail nor a junk caption.

**Inline body-content rendering** (separate from lead-image selection): images that are rejected as lead images may still be legitimate *inline* content. Emoji sprites are kept in the body but constrained to ~1.2em via CSS (`.entry-content img.wp-smiley/.emoji/.ipsEmoji`) so they read as text-sized glyphs rather than the full-size 72×72 block the general `.entry-content img` rule would otherwise produce (e.g. IP.Board's `ipsEmoji` 🙃, which carries no inline size style of its own). All inline body images are also given `referrerpolicy="no-referrer"` (`add_no_referrer_to_images`, applied late in the entry-content pipeline; skipped for locally-served starred assets) so hotlink-protected hosts that serve a placeholder image on a foreign `Referer` return the real asset. `referrerpolicy` only fixes *fresh* loads, though — a browser that already cached a host's "image was hotlinked" placeholder under the unchanged image URL (these hosts send no `Vary`) keeps serving it. So for a small set of *known* hotlink hosts (`_HOTLINK_IMG_HOSTS`, e.g. nanolx.org), body-image `src` and the lead image are rewritten to the same-origin `/api/img?u=…` proxy (`proxy_hotlink_images`, `_lead_image_display_url`): the new URL isn't in the browser's cache, and the server-side proxy fetch carries no `Referer`, so the real image loads and stays correct. `srcset` is dropped on those imgs so the proxied `src` is the one used. Add a registrable domain to `_HOTLINK_IMG_HOSTS` to cover a new host (matches it and any subdomain).
4. **Inline feed content** — images embedded in `<content>` or `<summary>` elements. The render-triggered chunk backfill (`_do_backfill_entry_list`) does source-page fetches for `og_scrape`/`webcomic`/`unknown` feeds; when that fetch yields nothing it falls back via `_inline_from_reader` to the entry's own inline image rather than caching a blank. This rescues feeds whose pages are JS-only SPAs with no `og:image` (e.g. ArtStation) but which embed the artwork directly in the feed.

At render time, a feed pinned to `inline`/`media_rss` thumb strategy that extracts nothing also falls back to the cached lead image (`list_entries` in main.py) instead of showing a blank — important for feeds whose `thumb_strategy` was auto-detected as `media_rss` but whose reader `Entry` objects carry no `media:*` fields.

The in-memory cache is warmed at startup **per enabled user** (`_for_each_background_user("lead-image cache warm", ...)`): lead images live in each tenant's own `entry_lead_images` table, and the render path consults only the shared in-memory cache (no per-user DB read), so warming bare against the default tenant would leave every other user's thumbnails blank until the rate-limited background backfill caught up after each restart.

For webcomics, the main comic panel is the lead image and takes priority over both the publisher's `og:image` and any RSS enclosure thumbnail. `_fetch_source_lead_image` calls `_extract_webcomic_panel_image` first when `is_webcomic` is set: it returns the `<img>` matched by `_WEBCOMIC_IMG_ID_RE`/`_CLASS_RE` (e.g. ComicControl's `id="cc-comic"`) before the `og:image` early-return — many webcomic CMSes set a single generic site banner as `og:image` on every page with a sane aspect ratio, which would otherwise win. For the same reason, backfill (`fetch_and_store_lead_images_for_feed`) treats `webcomic` like `og_scrape`-manual: it falls through the inline/enclosure image (typically a small `/comicsthumbs/` variant with no hover text) to the source-page fetch so the full-resolution panel and its alt/title win, and skips the feed-XML media-thumbnail lookup entirely (the enclosure is the same small thumbnail). `_extract_webcomic_alt_text` then surfaces the hover-text punchline: it checks the WordPress `comic-alt-text` balloon, then the `title`/`alt` attribute of the main comic `<img>` (matched by `_WEBCOMIC_IMG_ID_RE`/`_CLASS_RE`, e.g. SMBC's `id="cc-comic"`), and only then falls back to `og:description` (which on many comic sites is just the post title). At render time, captions that merely restate the entry title are dropped — including auto-generated banner captions that pad the title with a decorative word and/or date (e.g. "Progress Update Banner 2026-06-06" for a post titled "Progress Update 6/06/2026").

Results are stored in `entry_lead_images (feed_url, entry_id, image_url, image_alt, image_title, fetched_at)`. `image_alt` and `image_title` hold the raw `alt` and `title` HTML attributes from the matching `<img>` tag on the source page, stored separately so the user can choose which to display via the `caption_source` feed preference (`feed_display_prefs.caption_source`: `auto` / `alt` / `title` / `both` / `none`). NULL image_url means "no image found." Negative results are retried after **4 hours** (`_NEGATIVE_RETRY_SECONDS`); positive results are revalidated after 12 hours (`_POSITIVE_REVALIDATE_SECONDS`). An existing non-NULL URL is never overwritten with NULL during revalidation. Likewise on first resolution: an `og_scrape`-**manual** feed stores the inline feed image and then falls through to the authoritative source-page fetch, but a transient source miss must not clobber that inline image with NULL — otherwise a brand-new post (whose `og:image` isn't generated yet at first fetch) loses its thumbnail until the 4-hour negative retry. The NULL negative is only recorded when there was no inline image either.

First-open caption availability: when `queue_source_fetch` is called for a new entry, it posts a `threading.Event` keyed by `(feed_url, entry_id)`. The entry render path calls `wait_for_source_fetch(..., timeout=3.0)` immediately after queuing so that alt/title text is present on the very first open for sites that respond within the timeout, without blocking indefinitely on slow sites.

The strategy comparison cache (`feed_strategy_cache`) also stores `image_alt` and `image_title` per strategy so the Tuning tab can display them below each card without a live fetch.

SmartCrop's `min_scale` is a per-feed preference (`feed_display_prefs.smart_min_scale`, NULL = default 0.9), set in Feed Properties next to the thumb fit mode and passed to the `/thumb` proxy as the `ms` query param; it was previously a global app setting. The min_scale is part of the thumb cache key, so changing it regenerates that feed's Smart thumbnails.

Fill mode's `fill_zoom` multiplier (`feed_display_prefs.fill_zoom`, NULL = default 1.0, range 0.5–2.0) scales the cover-crop resize step before the anchor-crop. Values below 1.0 produce a letterbox (image pasted on a black canvas); values above 1.0 crop more aggressively than the default tight fill. Passed to `/thumb` as the `fz` query param and included in the cache key for cover-family modes.

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

**Multi-user fan-out:** the callback URL carries only the topic (no user), and `websub_subscriptions` is a per-user meta table, so both callbacks fan out across `_background_user_ids()` rather than acting on a single tenant. `_websub_verify_fanout` confirms the handshake for whichever user(s) have a matching pending subscription; `_process_websub_push` collects every user with a verified subscription for the topic, confirms the push is authentic against *any* of their secrets (a forged push matches none), then refreshes each subscriber under its own tenancy context in a daemon thread. Because the shared callback means the hub only retains the most recent subscriber's secret, validating against any one secret and fanning the refresh out to all subscribers is what lets several users share a single hub subscription.

The service is initialized only when `LECTIO_PUBLIC_URL` is set; all integration points in `main.py` guard on `if websub_service`. On feed removal, `purge_orphaned_feed` sends an active unsubscription request to the hub (best-effort HTTP POST; hubs expire leases anyway if the request is lost).

**Feed removal lifecycle:** `purge_orphaned_feed(reader, conn, feed_url, *, archive_pending, rescue_to)` is the single canonical sequence run whenever a feed leaves the system (confirmed orphaned — no remaining `folder_feeds` rows). Steps in order: (1) force-archive pending saved/starred entries; (2) rescue unread entries into a kept/canonical feed; (3) dispatch the delete via the appropriate path (DeviantArt rendered feed → `deviantart_service.delete_deviantart_feed`; scraped/FakeFeedz feed → `scraper_service.delete_scraped_feed`; plain feed → `reader.delete_feed`); (4) WebSub unsubscribe. Callers set `archive_pending=False` when entries survive under a kept URL (dedup, format-upgrade), and pass `rescue_to` to migrate unread state. The helper takes an already-open `reader` and `conn` so callers control the `with` scope and context-manager nesting is never doubled.

**Push indicator:** `get_push_active_feed_urls()` queries `websub_subscriptions` for `verified=1 AND hub_url IS NOT NULL` in one pass and returns a `set[str]`; the index route threads this into the template context so both the sidebar feed tree and Settings → Feeds can render the ⚡ glyph without per-feed queries.

Storage: `websub_subscriptions (feed_url TEXT PK, hub_url TEXT, secret TEXT, lease_seconds INTEGER, subscribed_at REAL, expires_at REAL, verified INTEGER, hub_tried_at REAL)` in the per-user meta DB.

## Security direction

Keep the local-first path simple. Add auth only when exposing the app beyond trusted local use. The multi-user phase makes per-user identity, per-user API tokens, route-level authorization, and SSRF hardening mandatory — see "Multi-user tenancy → Security posture".
