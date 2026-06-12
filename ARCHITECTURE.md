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

Current target is local-first single-user. Later phases may add basic auth behind a reverse proxy for VPS deployment. Keep auth non-invasive so that path does not require a rewrite.

## Extension strategy

Use plugin/adapter style for non-native behavior instead of hardwired branching. Prefer replaceable pieces and avoid duplicating `reader` capabilities in app code.

## Feed URL normalization

`normalize_feed_url` (main.py) is applied at add-feed time and in the Duplicate scan (`GET /feeds/duplicates`). It handles:

- Trailing-slash stripping from paths longer than `/`.
- Format-selector query params (`alt=rss`, `alt=atom`, etc.) that select serialization without changing content — lets the Blogger Atom and RSS URLs of the same feed collapse to one.
- ArtStation subdomain rewrites (`username.artstation.com/rss` → `www.artstation.com/username.rss`) to avoid TLS hostname issues with underscore usernames.
- `_DOMAIN_ALIASES` map — known domain pairs that serve identical content (currently `old.reddit.com` → `www.reddit.com`). Add new pairs there; the normalization and duplicate-scan logic picks them up automatically.

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
3. **Source-page scraping** — fetches the article URL, checks `og:image` / `twitter:image` meta tags (both `property=` and `name=` attribute order), preload hints, CSS background-image, then scored in-page `<img>` tags. Body scanner decision order: (a) first valid image in document gets a +10 position bonus; (b) when an `<img>` sits inside a `<picture>` with a `<source type="image/webp">`, the WebP srcset URL is substituted as the candidate. Logo/site-chrome rejection uses `_LOGO_URL_PATTERNS` (word-boundary-aware — compound words like "imdblogo" are not rejected) and `_SITE_CHROME_PATH/DOMAIN_PATTERNS` (`www.blogger.com` is chrome-only — Blogger content images live on `bp.blogspot.com`/`googleusercontent.com`); SVG candidates are always skipped. `_SITE_CHROME_CONTEXT_RE` skips images whose preceding markup carries nav/dropdown/widget class names (menu icons and sidebar/footer widgets are never lead images). The alt-text logo check is suppressed for images with explicit `width`/`height` attrs ≥ minimum dimensions, since publishers who size article images explicitly signal intentional placement.
4. **Inline feed content** — images embedded in `<content>` or `<summary>` elements.

Results are stored in `entry_lead_images (feed_url, entry_id, image_url, image_alt, image_title, fetched_at)`. `image_alt` and `image_title` hold the raw `alt` and `title` HTML attributes from the matching `<img>` tag on the source page, stored separately so the user can choose which to display via the `caption_source` feed preference (`feed_display_prefs.caption_source`: `auto` / `alt` / `title` / `both` / `none`). NULL image_url means "no image found." Negative results are retried after **4 hours** (`_NEGATIVE_RETRY_SECONDS`); positive results are revalidated after 12 hours (`_POSITIVE_REVALIDATE_SECONDS`). An existing non-NULL URL is never overwritten with NULL during revalidation.

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
4. **Push callback** (`POST /websub/callback`) — hub delivers content; `verify_push_signature` checks HMAC-SHA256 (or SHA1 fallback) against the stored secret. On success, `feed_refresh_service.update_feeds([feed_url])` runs in a daemon thread so the full pipeline (dedup, automation, lead images) runs on the fresh content.
5. **Lease renewal** — `renew_expiring_subscriptions()` re-subscribes any verified row whose `expires_at` is within 24 hours. Called each refresh cycle.

The service is initialized only when `LECTIO_PUBLIC_URL` is set; all integration points in `main.py` guard on `if websub_service`. Unsubscription on feed removal is best-effort — hubs expire leases anyway.

Storage: `websub_subscriptions (feed_url TEXT PK, hub_url TEXT, secret TEXT, lease_seconds INTEGER, subscribed_at REAL, expires_at REAL, verified INTEGER, hub_tried_at REAL)` in the meta DB.

## Security direction

Keep the local-first path simple. Add auth only when exposing the app beyond trusted local use.
