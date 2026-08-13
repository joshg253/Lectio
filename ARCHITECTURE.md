# Lectio Architecture

Lectio is a self-hosted feed reader built around the `reader` Python library. The goal is a fast triage workflow with a clean multi-user architecture and VPS-friendly deployment.

## Contents

Sections are grouped by theme and ordered to match, so reading top to
bottom follows the request path rather than the order things were written.

**Foundations**  
[Layering](#layering) · [Reader-first philosophy](#reader-first-philosophy) · [Deployment path](#deployment-path) · [Extension strategy](#extension-strategy) · [Security direction](#security-direction)

**Storage, tenancy and identity**  
[Multi-user tenancy](#multi-user-tenancy) · [Feed URL normalization](#feed-url-normalization) · [Duplicate entry suppression](#duplicate-entry-suppression) · [Duplicate feeds: the scheme (and www) is folded in the comparison, not the URL](#duplicate-feeds-the-scheme-and-www-is-folded-in-the-comparison-not-the-url) · [Removing a feed: what goes, and what a surviving capture protects](#removing-a-feed-what-goes-and-what-a-surviving-capture-protects) · [Hard-deleting a single entry (tombstones)](#hard-deleting-a-single-entry-tombstones)

**Interface and view state**  
[View state model](#view-state-model) · [Page weight: lazy HTML fragments](#page-weight-lazy-html-fragments) · [Adaptive layout model](#adaptive-layout-model) · [Folder tree & the Uncategorized folder](#folder-tree--the-uncategorized-folder) · [Folder Properties counts in SQL, not by hydrating entries](#folder-properties-counts-in-sql-not-by-hydrating-entries) · [Entry sort window (Pub Old / Pub New)](#entry-sort-window-pub-old--pub-new) · [Remembered sort: Feeds and Saved keep their own](#remembered-sort-feeds-and-saved-keep-their-own) · [Async bulk mark-read](#async-bulk-mark-read)

**Feeds: discovery, ingest, automation**  
[Feed discovery: which feed a page actually means](#feed-discovery-which-feed-a-page-actually-means) · [Feed auto-taggers](#feed-auto-taggers) · [Feed-provided tag suggestions (`entry_feed_tags`)](#feed-provided-tag-suggestions-entry_feed_tags) · [dev.to filtered feeds](#devto-filtered-feeds) · [DeviantArt integration](#deviantart-integration) · [Combining feeds moves the entries, not just the curation](#combining-feeds-moves-the-entries-not-just-the-curation) · [Combining feeds carries the offline captures](#combining-feeds-carries-the-offline-captures) · [WebSub (PubSubHubbub)](#websub-pubsubhubbub)

**Reading surfaces**  
[Read Mode — e-ink reading app (`GET /read`)](#read-mode--e-ink-reading-app-get-read) · [Offline reading and offline acting (`static/sw.js`, `static/outbox.js`)](#offline-reading-and-offline-acting-staticswjs-staticoutboxjs) · [Floated images, and why margins are not kept](#floated-images-and-why-margins-are-not-kept) · [Titles render a tiny inline allowlist](#titles-render-a-tiny-inline-allowlist)

**Images**  
[Lead image pipeline](#lead-image-pipeline) · [Image bytes: the dimension cap is not a size cap](#image-bytes-the-dimension-cap-is-not-a-size-cap) · [Transparency: `convert("RGB")` paints line art black](#transparency-convertrgb-paints-line-art-black) · [Thumbnails must reuse the image proxy's bytes](#thumbnails-must-reuse-the-image-proxys-bytes) · [DeviantArt mature images: signed for minutes, cached for good](#deviantart-mature-images-signed-for-minutes-cached-for-good)

**Saved articles, capture and editing**  
[Saved articles (read-it-later capture)](#saved-articles-read-it-later-capture) · [Saving an article you already subscribe to](#saving-an-article-you-already-subscribe-to) · [Re-fetch on keep](#re-fetch-on-keep) · [Node bulk actions, and what a re-fetch may replace](#node-bulk-actions-and-what-a-re-fetch-may-replace) · [Keeping the files a post links to](#keeping-the-files-a-post-links-to) · [Editing a post's published date (overrides)](#editing-a-posts-published-date-overrides) · [Editing a post's body — Aardvark-style cleanup](#editing-a-posts-body--aardvark-style-cleanup)

**Client APIs**  
[GReader API](#greader-api) · [Fever API](#fever-api)

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

## Deployment path

Lectio is designed for VPS deployment behind a reverse proxy. Auth is always active; access requires a user account. See `.env.example` for deployment configuration.

## Extension strategy

Use plugin/adapter style for non-native behavior instead of hardwired branching. Prefer replaceable pieces and avoid duplicating `reader` capabilities in app code.

## Security direction

Keep the local-first path simple. Add auth only when exposing the app beyond trusted local use. The multi-user phase makes per-user identity, per-user API tokens, route-level authorization, and SSRF hardening mandatory — see "Multi-user tenancy → Security posture".

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

**The archive connection is the odd one out: it is not pooled.**
`get_starred_archive_connection()` returns a *fresh* connection per call, so
the caller owns closing it. `with conn:` does not — that is sqlite3's
*transaction* manager, which commits or rolls back and leaves the handle open,
so the plain form leaked a connection per call site. Go through
`main.archive_conn()` (or `StarredArchiveService._archive_conn()`) instead:
same transaction semantics, plus the close. Reach for the raw factory only
where a connect failure has to be caught before the body runs.

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

### Dating an entry

Two functions, deliberately not one:

- **`entry_publication_date`** — when the entry was actually published, or
  `None` when nothing says. Ordered by trust: the feed's own `published`, then
  `updated`, then a date the permalink states outright (`/2019/07/06/` or
  `/2025-11-22/`), then a month-precision permalink (`/2021/04/` → the 1st),
  then a date parsed out of the title.
- **`entry_effective_date`** — the same, falling back to the received date so it
  *always* returns something. The list sort, the unread counts and the bulk age
  actions all key off this one; they disagreed once, and mark-older skipped
  entries the UI had already greyed.

Being able to return `None` is the point of the split: it is what lets the UI
show "no date" honestly instead of quietly presenting "when Lectio first saw it"
as a publication date.

**Missing dates do not always arrive as NULL.** Importers and parsers write
sentinels — the Unix epoch, or year 0001 — and a sentinel is *truthy*. Since
every step above is an `or` chain, a sentinel beat every fallback below it, and
an entry carrying its own date in its URL still displayed nothing.
`real_published_date` normalizes anything before 1990 back to the `None` it
stood for. Any new date source added here must go through it; a raw
`entry.published or …` reintroduces the bug silently.

### Learning a date on re-fetch

Re-fetch is a free chance to learn a date for an entry that has none, and it
consults three sources, worst last (`services/publish_date`):

1. **the page's own metadata** — `article:published_time`, JSON-LD
   `datePublished`, `<time datetime=…>`;
2. **a date the page prints but never marks up** — hanselman.com ships
   `<span class="blogMetaDate">February 03, 2026</span>` and nothing
   machine-readable, so mining metadata correctly found nothing on a page
   visibly showing its date. Only elements the publisher *labelled*
   (class/id naming date/publish/posted/byline) count, and the first match
   wins: a page is full of date-shaped text — comment timestamps, related-post
   rails, a copyright footer — and matching any of it would reliably pick the
   wrong one;
3. **the site's own index**, for sites that publish dates nowhere near the
   article. what-if.xkcd.com carries no date in any form on an article, while
   its archive index lists all 162 with theirs. This is a *site adapter*, not a
   branch in the caller: adding a site means registering a resolver, and the
   caller keeps asking one question.

Two guards make this safe to run automatically. It **never overwrites a date the
entry already has** — re-fetch once moved `published` and destroyed 105 real
dates — and it never touches an entry whose date the user pinned by hand. "Has a
date" means `real_published_date` says so, which is what stops a sentinel from
counting as one.

### The Internet Archive as a re-fetch source

`wayback_snapshot_url` asks archive.org's availability API — one small JSON call,
no crawling of a site that already refused us. Re-fetch reaches for it two ways:

- **Automatically**, when the live fetch is *refused*: a parked page, a section
  index, a 404. The guard is right to refuse those, but on its own it leaves the
  user with nothing.
- **On request** (`mode=archive`, "Re-fetch from Internet Archive"), which exists
  for the case the automatic path cannot see — a publisher serving a page that
  passes every guard while no longer being the article: rewritten, truncated, or
  paywalled. That page is indistinguishable from a good one to a guard, so only
  the reader can say it is wrong.

An archived page is also usually a *better* date source than the live one, since
the archived copy still carries the byline the publisher has since dropped —
which is why the date mining runs over whatever the re-fetch actually fetched.

### Refresh scheduler: why it has a watchdog

The scheduler is one thread running one pass at a time, and every feed in a pass
is fetched sequentially. That is a deliberate simplicity trade (it keeps Lectio a
polite client and makes backoff reasoning local), but it has one consequence
worth stating plainly: **any single outbound call that never returns stops every
feed behind it.** That is not hypothetical — it happened, and nothing refreshed
for 34 hours while `/healthz` answered 200 the whole time.

Three layers, each covering what the one above it cannot:

1. **A read deadline on every feed fetch.** `ReaderApi` states `session_timeout`
   explicitly (`LECTIO_FEED_CONNECT_TIMEOUT` / `LECTIO_FEED_READ_TIMEOUT`) rather
   than inheriting reader's default. A host that accepts a connection and then
   says nothing costs one timeout, not the whole cycle.
2. **Nothing may escape the loop.** `scheduled_refresh_loop` catches everything,
   and the WebSub renewal — which sits *after* the guarded per-user loop — has its
   own guard. An uncaught exception there does not crash the app; it silently ends
   the only thread that refreshes feeds, which looks exactly like a hang.
3. **A watchdog on progress, not elapsed time.** A read deadline is per-socket-
   read, so a host trickling one byte at a time never trips it. `FeedRefreshService`
   reports a stage each time it advances; `scheduler_watchdog_loop` trips only when
   a pass is in flight and has not advanced for `LECTIO_SCHEDULER_STALL_SECONDS`.
   Elapsed time alone is useless here — a full-library pass legitimately runs for
   an hour, so "slow" and "stuck" are only distinguishable by whether it moves.

A wedged pass cannot be cancelled: Python cannot interrupt a thread blocked in a
socket read. So the escalation past `LECTIO_SCHEDULER_STALL_RESTART_SECONDS` is
`os._exit(1)`, letting the container's `restart: unless-stopped` policy do what
the manual recovery did. Set it to `0` to log only.

A stall is reported in the `/healthz` body but **never fails the probe**. That
endpoint is both the Docker HEALTHCHECK and Traefik's: a reader whose background
refresh is stuck is still entirely usable for reading, and failing the probe would
withdraw the backend, converting a background-work failure into an outage.

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

### Instance-level settings

Instance config (Administration → Instance Config: maintenance hour,
image-cache tunables, fetch-history retention, login lockout, default
auto-refresh, shared OAuth creds) is *stored* in the saving admin's own
`app_settings`, but *consumed* from arbitrary contexts — the daily-maintenance
loop is a bare thread bound to the default user, login lockout checks run
pre-auth, image-cache eviction runs in maintenance. These values must
therefore be read through `get_instance_setting()` (current context's setting
→ first enabled admin's setting → env fallback), never `get_runtime_setting()`
directly — the latter silently reads the wrong user's (empty) settings and
the feature dies without an error, which is exactly how nightly maintenance
sat disabled for three weeks in 2026-07. The admin tier reads through
`get_setting` (DB-backed, not cache-only) so it works before the admin's
cache ever loads, is TTL-cached (60s; `list_users()` is a DB query and
`get_img_cache_max_dim` sits on the `/api/img` hot path), and is invalidated
by the settings-save route so edits apply immediately.

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
- **Refresh backoff & high-fanout pacing** (`FeedRefreshService.update_feeds`) —
  failing feeds get exponential *per-feed* backoff. A coarse *per-domain* backoff
  also exists for hosts that go down, but it is **exempt for high-fanout hosts**
  (>= `_HIGH_FANOUT_DOMAIN_FEEDS` feeds in the batch, e.g. ~700 youtube.com subs):
  reader doesn't reliably expose an HTTP status on its exceptions (real YouTube
  404s arrive with status `None`), so a few dead channels would otherwise look
  like transport failures and lock the whole domain — which starved every
  subscription for days. Per-feed backoff handles the dead ones; the domain guard
  is kept only for small hosts, and there it activates only after several
  consecutive failures, caps at 1h, and clamps stale locks at read so they
  self-heal. Requests to a high-fanout host are also **paced**
  (`_HIGH_FANOUT_PACE_SECONDS`) so a big serial burst isn't throttled into
  spurious 404s (YouTube 404s a ~700-request burst though each feed is fine
  singly) — a polite-client measure, feeds to other hosts interleave at full speed.
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
- **One feedparser per process, and it is the installed one** — `reader` ships a
  vendored feedparser (`reader._vendor.feedparser`) and uses it by default, so
  for a long time Lectio ran two copies at different versions: the vendored one
  parsed every feed at ingest, while the `import feedparser` in `main.py` only
  served Lectio's own direct `feedparser.parse` calls (feed comparison, podcast
  enclosure sniffing, YouTube embed recovery). Two consequences, both silent:
  the date handler `main.py` registers for day-of-week-less RFC 2822 dates
  (`_parse_month_first_pubdate`) never reached ingest, and dependency bumps to
  feedparser never touched the code that parses the library. `services/__init__.py`
  now sets `READER_NO_VENDORED_FEEDPARSER=1` so `reader` uses the installed
  feedparser. It is a `setdefault`, so an operator can still force the vendored
  copy — but the vendored copy in `reader` 3.26 is 6.0.11, whose `sgml.py` does a
  bare `import sgmllib`; that module came in transitively from feedparser's
  `sgmllib3k` dependency, which feedparser 6.0.13 dropped in favor of a
  self-contained `feedparser.sgml`. So on feedparser ≥ 6.0.13 the vendored copy
  does not import at all. The variable must be set before anything imports
  `reader`, which is why `main.py` and `tests/conftest.py` — both of which reach
  `reader` ahead of the services package — establish it themselves.
  `tests/services/test_feedparser_wiring.py` pins the invariant.
- **Link fields are feed input too** — that allowlist only covers URLs *inside*
  content HTML. An entry's own `link` is equally attacker-controlled, and Jinja
  escaping protects the attribute *context*, not the scheme: `href="{{ link }}"`
  with a `javascript:` link renders a clickable XSS in our origin (confirmed —
  it reached four live hrefs plus `data-source-url`/`data-post-link` before
  2026-07-16). So every link that reaches a template or a `data-*` attribute
  passes `html_sanitize.safe_link_url` (http/https/mailto/tel, same rule as
  content hrefs) at the presentation choke points — `get_entry_detail`,
  `list_entries_for_feeds`, `_build_orphan_entry_detail` — which empties unsafe
  links so the `{% if entry.link %}` guards hide them. Sanitize at those
  hydration points, not per template line: new render sites inherit the guard.
  `safeHttpUrl()` in `static/js/app.js` mirrors it for values read back out of
  the DOM (defense in depth; also what CodeQL's `js/xss-through-dom` flagged).
- **Embed allowlist** — `<iframe>` is kept only when its `src` host is on
  `_EMBED_HOST_ALLOWLIST` (YouTube/Vimeo/Dailymotion/Twitch/SoundCloud/Bandcamp/
  Spotify + Twitter/CodePen/Reddit/Archive.org), https-only, matched by exact or
  dot-suffix host (so `youtube.com.evil.com` doesn't slip through). Kept iframes
  are forced into a `sandbox` (`allow-scripts allow-same-origin …` — same-origin
  refers to the *embed's* origin, not Lectio's) with a conservative
  `referrerpolicy` and lazy loading. Inline SVG is cleaned via
  `services/svg_sanitize.py`; MathML is kept with a curated element/attribute
  allowlist.
- **Presentational formatting is preserved, by enumerated allowlist.** Bold and
  italic always survived (`b`/`strong`/`i`/`em` are allowed tags), but *centering*
  did not: `style` was stripped wholesale, `align` was granted only to `img`/`td`/
  `th`/`tr`, and `<center>` fell through the unknown-tag unwrap. Since feed CSS is
  never loaded, nothing could restore the author's intent afterwards. Now
  `<center>` is allowed, `align` extends to block elements (`p`/`div`/`figure`/
  `figcaption`/`h1`–`h6`/`table` — the same "value-constrained, no scripting
  surface" reasoning already applied to table cells, which had simply never been
  extended), and `_sanitize_style_attr` keeps a **fixed table of property →
  literal values** (`text-align`, `font-style`, `font-weight`,
  `text-decoration`, `text-transform`, `font-variant`).
  **Nothing free-form is ever kept**, so there is no place for `url(…)`,
  `expression(…)`, `-moz-binding`, or an escaped payload to survive — an unlisted
  property *or* an unlisted value is dropped rather than cleaned-and-kept, and the
  declarations are re-emitted from the table so the output string is ours.
  Layout and positioning (`position`, `z-index`, `width`, `display`, `opacity`)
  are deliberately excluded: without any scripting they still let feed content
  escape the pane or overlay the app's own UI. The normalized output spacing
  (`text-align: center`) is load-bearing — `style.css` keys its centering rules
  off that exact string.
- **JS-dependent chrome is stripped at render** (`_strip_js_dependent_chrome`).
  Share widgets and lazy "related posts" carousels only become anything once the
  source page's own JavaScript runs; we don't run it, so they arrive as rows of
  dead icons and empty bullets (paizo.com ends every post with a
  `div.sharing_widget` of href-less anchors plus four
  `<li class="blog-item loading">` holding a dice spinner). The safety rule is
  narrow and load-bearing: **only elements with no text *and* no `<img>` are
  removed**, so a real related-posts block (which has headlines) and a real
  gallery (which has images) can never match on class name alone.
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
  **Caveat (found 2026-07-21): most inline SVG never reaches this code intact.**
  feedparser parses an HTML-escaped `<description>` as HTML, where a trailing
  slash is meaningless, so `<rect/><circle/><path/>` arrives as
  `<rect><circle><path>` — every shape nested inside a rect, which cannot
  contain shapes. The sanitizer faithfully preserves that nesting and the
  browser paints only the rect, so the feature degrades to a flat colour block.
  Both sanitizers are innocent; the damage predates them. See Plan.md for the
  repro and the fix options (re-parse SVG subtrees as XML at ingest).
  **Icon SVGs are excluded from the lead-image slot *and* sized down in the
  pane** — two separate fixes for one cause. An inline icon's `width`/`height`
  attributes are **viewBox units**, not pixels: a Font Awesome chevron ships
  `width="320" height="512"` and depends on the source site's own CSS to shrink
  it to ~1em. Neither of `_is_decorative_inline_svg`'s original tests caught
  that — the class carries no "icon" word (`fa-lg svg-inline--fa
  fa-chevron-left`) and 320×512 is far above the small-glyph pixel floor — so
  on a paizo.com post the chevron both won the thumbnail and rendered as a
  pane-filling graphic. `_SVG_FA_CLASS_RE` now matches `svg-inline--fa`, Font
  Awesome's own marker, and `.entry-content svg.svg-inline--fa` (plus the
  generic `*-icon` conventions) constrains them to text height. Both match that
  exact marker rather than any `fa-` substring, which would also catch an
  illustration classed `alfa-romeo-art`. Non-icon inline SVG keeps only a
  `max-width` cap — forcing a height would squash legitimate SVG figures.
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

## Feed URL normalization

`normalize_feed_url` (main.py) is applied at add-feed time and in the Duplicate scan (`GET /feeds/duplicates`). It handles:

- Trailing-slash stripping from paths longer than `/`.
- Format-selector query params (`alt=rss`, `type=atom`, `feed=rss2`, etc. — `_FORMAT_SELECTOR_PARAMS`) whose *value* is also on the allowlist (`_FORMAT_SELECTOR_VALUES = {rss, rss2, atom}`, plus anything prefix-matching `json*` since JSON Feed versioning is a moving target) that select serialization without changing content — lets the Blogger Atom and RSS URLs of the same feed collapse to one. Param name **and** value both have to match — a hypothetical `?type=news` category selector is left alone.
- ArtStation subdomain rewrites (`username.artstation.com/rss` → `www.artstation.com/username.rss`) to avoid TLS hostname issues with underscore usernames.
- `_DOMAIN_ALIASES` map — known domain pairs that serve identical content, or renamed domains (`old.reddit.com` → `www.reddit.com`; `tapastic.com` → `tapas.io`). Add new pairs there; the normalization and duplicate-scan logic picks them up automatically.

**Curation migration on consolidation.** Every duplicate-scan tier and the format-Upgrade tier resolve through `POST /feeds/combine` (a user-picked survivor + one or more sources), which calls `purge_orphaned_feed` with `migrate_curation_to` set. That first calls `_migrate_curation` to move the removed feed's manual tags and stars onto the surviving feed — matching each curated source entry to a survivor entry by GUID, else normalized link, else synthesizing it into the survivor (`reader.add_entry`) so nothing is lost. This is unconditional (independent of the opt-in "rescue unread" toggle, which only re-flags read/unread state) and mirrors the offline `scripts/reconcile_duplicate_feeds.py --merge` path. The old bulk `POST /feeds/deduplicate` (auto-apply same-folder pairs, checkbox-picked cross-folder/upgrade choices) was retired 2026-08-10 once every tier moved to `/feeds/combine`'s per-group Compare-then-Combine flow — see below.

**Import-time canonicalization.** `canonical_feed_url` (main.py) composes `normalize_youtube_feed_url` + `normalize_feed_url` and is the single choke point every bulk importer runs each incoming feed URL through *before* it subscribes or keys per-entry tag/star state. This makes a variant URL (old.reddit, `?alt=rss`, trailing slash) attach to an existing subscription instead of spawning a duplicate. It is wired into OPML import, the Inoreader local-file migrator, the shared migration applier `_apply_migration_items` (Miniflux/FreshRSS/Tiny Tiny RSS), the Inoreader JSON upload, and the Inoreader OAuth drip (subscriptions, label, and starred phases). Importers that key both subscription and tagging off `item["feed_url"]` call `_canonicalize_item_feed_urls(items)` once up front so both phases stay in sync. Google Takeout import is exempt: it only applies tags/stars to entries already present in the reader DB (never `add_feed`s), and those URLs are already canonical from the original subscription.

**Canonicalizing the incoming URL is only half of it — the set you compare against has to be canonical too.** `import_opml` canonicalized each `xmlUrl` and then tested it against the *raw* `folder_feeds` URLs. Any subscription whose stored URL was not already canonical therefore never matched, looked new, and was subscribed a second time under the canonical spelling. A trailing slash was enough: re-importing Lectio's own OPML export duplicated **440 of 2,909** foldered feeds — the restore-from-backup path, which is exactly when a user can least afford it. The dedupe set is now built through `canonical_feed_url` as well. Worth noting for any future importer: these duplicates are invisible to a `GROUP BY feed_url` check, because the two rows hold different strings (`…/feed/` and `…/feed`), so verify idempotency by comparing subscription *counts* across a round trip.

## Duplicate entry suppression

Two mechanisms prevent duplicate articles from accumulating in the reader DB:

**GUID-churn suppression** (`_suppress_guid_churn`, runs after each refresh): detects entries that reappear with a new GUID but the same URL slug, or the same title + publication date (within 7 days). Checks both read history AND existing unread entries so that multiple copies arriving before any are opened are also caught.

**Intra-feed and cross-feed cleanup** (`_cleanup_intra_feed_slug_dupes`, runs at startup and after each refresh cycle): two-pass retroactive cleanup for duplicates that slipped through before suppression was in place or before Deduplicate rules ran.
- Pass 1: within each feed, keep the oldest entry per slug and per title+date; mark newer copies read.
- Pass 2: across all feeds, group entries by `normalize_entry_link_for_dedupe`; keep the oldest copy globally and mark the rest read. This handles syndicated posts that appear in multiple subscribed feeds (e.g. a blog post cross-posted to two feeds from the same author).

`normalize_entry_link_for_dedupe` is the single canonical link key, shared by this pass, the render-time list collapse (`build_entry_dedupe_key`), the curation migration on feed removal, and the Saved duplicate scan's "confirmed" tier. It drops the fragment and trailing slash, then **folds the scheme and a leading `www.`**, lowercasing only the host — paths stay case-sensitive. The fold matters because the Saved scan's other confirmed-tier key, the URL slug, is deliberately discarded when it is generic (`/index.html`, blocklisted, or hyphen-free and short); before the fold, an http/https or www/non-www twin with such a URL had no confirmed-tier key at all and fell through to the weaker "possible" tier, where it needed a hand judgment. The result is a comparison key, not a URL — it has no scheme and is never fetched or displayed.

**The saved scan's slug key is host-scoped** (`_saved_dup_host_slug`), unlike the shared `_safe_dedup_entry_slug` it wraps. That helper returns the last path segment and nothing more, which is right for its other callers — per-feed GUID-churn history, and the multi-signal dedup where a lone `slug` is never one of `_SAFE_DEDUP_COMBOS` — but in `/saved/duplicates` a bare slug match *confirms* a duplicate on its own. Host-blind, that made unrelated publishers collide: guitarworld.com and guitarmasterclass.net each have a `pinch-harmonics` article, and the scan offered to delete one of them. A descriptive slug clears every length/hyphen guard precisely *because* it names a topic. Scoping the key to the folded host keeps the tier's real job (one article under a changed path on one site, including across a scheme/www change) and leaves genuine cross-host syndication to the title/body tier, which is far stronger evidence.

These run server-side and affect the underlying DB state, so third-party clients (Capy, etc.) see the clean state after the next sync.

## Duplicate feeds: the scheme (and www) is folded in the comparison, not the URL

`get_feed_duplicates` groups subscriptions by `normalize_feed_url` **with the
scheme and a leading `www.` stripped** (`_dupe_group_key`). Both have to be
folded somewhere other than `normalize_feed_url` itself, because some hosts
really are http-only or www-only and forcing either there would break them —
the comparison has no such obligation, unlike the stored subscription URL.
`_DOMAIN_ALIASES` rewrites a host without touching the scheme, which is what
originally forced the scheme fold: a legacy `http://tapastic.com/…`
subscription normalized to `http://tapas.io/…` while its live twin was
`https://…`, so the two never grouped and a dead feed sat beside a working copy
failing every refresh, unflagged. The www fold followed the same shape:
`deathbulge.com/rss.xml` and `www.deathbulge.com/rss.xml` are the same feed.

The survivor (`keep`) is chosen from the variants that are actually subscribed —
preferring `https`, then the already-canonical spelling, then the shortest.
`keep` used to be the canonical *string*, which is not necessarily one of the
variants; when it was not, `url_folders.get(keep)` came back empty, so every
variant looked cross-folder and was offered for removal against a URL nobody was
subscribed to.

### Five tiers, one Compare-then-Combine UX

`GET /feeds/duplicates` returns five tiers, each rendered in Settings → Feeds
→ Utilities as its own `<details>` section but sharing one frontend format
(originally built for the title tier alone, generalized to the rest
2026-08-10 — `_renderDedupGroups` in `app.js`): a group of candidate feed
URLs, an inline **Compare** (`GET /feeds/compare`, live-fetches each URL and
shows format/entry-count/full-text/dates/GUID-type/sample-title), then an
inline **Combine** (survivor radio + optional "carry over unread state",
`POST /feeds/combine`). Nothing auto-applies anywhere — every merge is an
explicit, post-Compare user decision. The bulk `POST /feeds/deduplicate`
(same-folder auto-apply, checkbox-picked cross-folder/upgrade choices) that
this replaced is gone.

- **`same_folder` / `cross_folder`** — a `_dupe_group_key` match: same feed,
  different scheme/www/format-selector. Two feeds, no checkboxes needed
  (there's only one possible comparison) — Compare is just always available.
- **`query_pairs`** — same host+path, *different* query, deliberately never
  auto-folded: a query param can be a real duplicate (a format selector the
  allowlist doesn't recognize) or genuinely different content (a WordPress
  category/tag feed). YouTube (`channel_id` differs by design — the Watch
  folder sync is bidirectional, so two channel ids are never a duplicate) and
  DeviantArt's `backend.deviantart.com/rss.xml?q=gallery:<user>` (one shared
  endpoint for the whole site) are excluded entirely as noise sources.
- **`title_groups`** — same feed *title* across two genuinely different
  addresses (a Tumblr and a Tapas copy of the same webcomic) — the only tier
  the URL-scheme grouping can't reach at all, since it needs two different
  addresses, not variants of one. `GENERIC_TITLE_GROUP_MAX = 5` floors out
  generic titles ("News" shared by unrelated sites is noise; every genuine
  match measured at 2–3 feeds). The only tier where a group can legitimately
  hold a non-match (an unrelated site sharing a generic title alongside a
  real pair), so it's also the only one that keeps a pick-a-subset checkbox
  gate before Compare — see `_renderDedupGroups`'s `selectable` flag.
- **`upgradable`** — a subscribed URL still carrying a format-selector query
  param. `upgrade_to` is the stripped default (skipped entirely when
  stripping it would leave nothing but a bare domain — WordPress's
  root-level `?feed=rss2` is the failure case: the query *is* the whole
  address there, not decoration on a working default). `alternates` are
  same-family format-selector swaps (`_format_alternate_urls`: a WordPress
  `?feed=rss2` subscription also serves `?feed=atom` and `?feed=rss`
  natively — a real, near-guaranteed-to-exist option, not a guess). Neither
  candidate is ever assumed better: RSS isn't reliably worse than Atom on
  every site (some sites' RSS is the richer feed), so nothing here gets a
  "suggested keep" bias and Compare-then-Combine is mandatory before
  switching.

**`content_identical` gates the "suggested keep" hint.** `same_folder` and
`cross_folder` pairs carry a `content_identical` flag (`_pair_is_content_identical`)
— true only when the two URLs differ by scheme/www alone (provably the same
bytes). A format-selector swap does *not* get the hint: reported 2026-08-10,
freac.org's `?type=rss` carries summaries only while `?type=atom` carries full
text, so the length-based tie-break (`keep`) happened to "suggest" the worse
one. `query_pairs` and `upgradable` never get the hint at all, since content
identity isn't provable from the URL shape for either.

**A broken Compare result can't be picked as the Combine survivor.** If
`/feeds/compare` returns an `error` for a candidate (dead link, unparseable —
an Upgrade-tier guess turning out to be the site's HTML homepage, say), the
frontend drops it from the survivor radio list before rendering the Combine
panel, so a wrong candidate can't repoint a subscription at something that
isn't a feed. It can still be a Combine *source* (a silent no-op — nothing to
migrate from a URL nobody's subscribed to).

**Dismissals.** `POST /feeds/duplicates/dismiss` (and the "Not dupes" button
on every group) records the group's exact feed-URL set in `dedup_dismissed`
(`_dedup_dismiss_key` — order-independent, sorted-and-joined); `get_feed_duplicates`
filters every tier against it before returning. `POST /feeds/combine` also
records a dismissal automatically on every completed call, survivor + all
sources, regardless of whether anything was actually deleted — load-bearing
for the Upgrade tier specifically, where picking the already-subscribed
"current" URL as survivor makes every "source" a candidate nobody was ever
subscribed to, so the purge loop is a structural no-op and, without the
auto-dismiss, the next scan re-detects the identical group (reported
2026-08-10: "I just combined them, then checked again and they are back").
A dismissal naturally stops matching if the underlying feed set changes later
(unsubscribed, URL changed) rather than silently hiding some other, unrelated
group.

## Removing a feed: what goes, and what a surviving capture protects

Feed removal used to clean two marker tables and leave everything keyed on
`(feed_url, entry_id)` behind. Measured on the live library before the fix:
**51,672 dead rows across 551 removed feeds**, almost all of it two derived
caches (24,443 `entry_lead_images`, 27,049 `entry_read_state`).

**It is a per-entry question, not `DELETE ... WHERE feed_url = ?`, because a
capture can outlive its feed.** The Saved view renders those archive orphans and
reads their thumbnail and hand-made corrections from these very tables — 2,547
rows were still being displayed that way. `_purge_dead_entry_meta` therefore
stages the feed's surviving `archived_entry` ids in a temp table and deletes
only what is not among them. A temp table rather than a bound `NOT IN` list: a
feed can hold thousands of captures, past SQLite's variable limit, and chunking
a `NOT IN` is actively wrong — each chunk would delete the ids named in every
other chunk. If the archive cannot be read the function deletes **nothing**,
because leaking rows is recoverable and blanking an orphan's thumbnail is not.

Two tables are deliberately excluded. `read_history` is a log of what you read,
not state owned by a subscription, so unsubscribing must not rewrite it.
`saved_entries` belongs to the star/keep paths.

## Hard-deleting a single entry (tombstones)

The entry context menu's **Delete post…** (`POST /entries/delete`) hard-removes one garbage entry (spam, corrupted post). reader's public `delete_entry` only covers user-added entries, so feed-provided ones go through the storage-level delete — the same API reader's own `entry_dedupe` plugin uses. A tombstone row in the meta DB (`deleted_entries`, keyed feed_url + entry_id) records the deletion, and the refresh service purges any tombstoned entry a refresh re-ingested (`purge_tombstoned_entries`, runs after every update batch, before enhancement) — otherwise the entry would resurrect on every fetch while still inside the publisher's feed window. Tombstones are kept forever (tiny rows; the guid could reappear any time the publisher republishes).

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

### Filtering a view is not searching it

Search and filter are different tools and are kept apart deliberately. **Search**
(`q`) is a server-side query that changes *what is fetched*; it spans title, feed
name, link, authors and summary. **Filter this view** (`#posts-filter-input`)
narrows *what is already in front of you*, instantly, so the result can be acted
on as a set — it matches only title, link and feed name, and is transient
navigation state: a pane swap re-renders the toolbar and the filter comes back
empty by design.

Two mechanics make this safe.

**The filter owns its own class.** `post-item-filtered`, never
`post-item-hidden` — the latter belongs to the scroll-chunking reveal, and
"move all shown" keys off it. One shared class would have turned a filtered bulk
move into a whole-list bulk move: filter to one domain, click move, and the
entire unfiltered list is silently re-filed. Keyboard navigation excludes both
classes; while a filter is active the chunk window steps aside and reveals every
match, because chunking exists to keep a 2,000-row list cheap to paint and the
filter has already done that job.

**Whole-set actions resolve server-side, by predicate.** The list route serves
250 posts on first load and chunks to a 2,000 cap thereafter, so the browser
holds a *page* of the view, not the view. An action that posts the ids it can
see therefore covers a fraction of a large filter and says nothing about it —
which `Move visible to feed…` did. `POST /entries/move-visible-to-feed` takes the
*predicate* instead (folder scope, tag, search, read/star filters, and the filter
term) and re-resolves it here at an effectively unbounded limit, mirroring the
home route's scope derivation. That is correct at any size, and since there is no
id payload to bound, `_MOVE_BATCH_CAP` does not apply to it. A `dry_run=1` call
returns just the count so the confirm dialog can state the real number rather
than the number of rows in the DOM — the dialog names both when they differ
("Move the 60 shown posts… 46 are loaded here; all 60 are moved").

"Shown" means everything matching the active filters regardless of scroll
position: scroll-chunking is a rendering optimization, not user intent. Orphan
archive rows (saves whose feed is gone) are excluded on both sides — there is no
reader entry to move.

`/entries/mark-range-read` solved the same page-vs-view problem earlier with
`_RANGE_READ_LIMIT`; this is that lesson generalized from an anchor lookup to a
whole-set action.

### Back on a phone walks the view stack

In single-pane mode the article pane *is* the page, so Back has to step down the
stack rather than leave: article → the feed list it was opened from → that feed's
folder list → and there it stops, toggling the folder drawer open and closed.

Most of that chain needs no code. Opening a feed from the drawer and opening an
article each push a history entry, so the stack already holds the feed list under
the article and the folder list under the feed list, and a plain Back walks them.

The last step is the exception, and the reason for the machinery in
`templates/index.html`. Backing out of the entry the app was *loaded* on is a
cross-document navigation: the page unloads and `popstate` never fires, so
nothing script can do would intercept it. Instead a **spare history entry** — a
duplicate of the current URL, so pushing and popping it are both visually
silent — is pushed ahead of time by `armDrawerBack()`, and popping it is what
toggles the drawer.

**What gets protected is the bottom of this document's history, not a particular
kind of URL.** The first version decided from the URL shape — arm only on a
folder-scoped list, since an article or feed list "always has a real parent
underneath". That holds only if you *navigated* there in this session. Reopen the
app, restore a tab, or follow a bookmark straight to an article and there is no
parent, so Back went cross-document and closed the tab — the exact bug the guard
existed to prevent, still live on three of the four ways in.

Position is therefore tracked directly: `history.pushState`/`replaceState` are
wrapped to stamp a `lectioIdx` into the state, the loaded entry is stamped 0, and
`popstate` traps when it lands back on 0. An *index* rather than a counter
decremented on each `popstate` is what keeps Forward working — `popstate` fires
in both directions, and only the state can say which way, or how far. (Once you
Back all the way to the floor the re-pushed spare truncates forward history, so
Forward stops there; mid-stack it is unaffected.)

### ⚠ The guard cannot be made airtight, and the manifest is why

Chrome ships a **history manipulation intervention**: history entries a page
pushes *without user activation* are marked skippable, and Back traverses
straight past them. It exists to stop pages trapping the back button — which is
exactly what the spare does. A spare pushed from inside a `popstate` handler has
no gesture behind it and can therefore be skipped, letting Back walk out.

Observed on a Galaxy S21+ as two working drawer toggles followed by the tab
closing, while an automated Chromium run of the identical steps toggled
indefinitely — **headless does not apply the intervention, so no browser test
here can prove the guard holds.** Treat green automation on this feature as
"not obviously broken", never as proof.

The mitigation is to re-arm from real gestures (`pointerdown`/`touchstart`/
`keydown`), since an entry pushed while the user is touching carries activation.
Reading means tapping constantly, so in practice the spare is usually
gesture-made. It is not a guarantee: press Back repeatedly without touching
anything in between and there is no gesture to arm from.

**What actually removes the risk is not being a tab at all.** Lectio ships a web
app manifest (`static/manifest.webmanifest`, `display: standalone`) so it installs
to the home screen; installed, Back at the root backgrounds the app instead of
closing a tab, and the session survives. The in-page guard is the fallback for
browser-tab use, not the primary answer.

**Installability needs a service worker, not just a manifest** — and the main app
did not register one. `offline-probe.js` registers `/sw.js`, but it is loaded only
by Read Mode *and* early-returns without its own `rm-*` elements, so `/` had a
manifest and no worker. The browser then declines to install and "Add to Home
screen" degrades to a plain shortcut that opens in the browser — a tab, which
dies with Back, which is the thing the manifest existed to prevent. `index.html`
therefore registers `/sw.js` itself on load. That adds no caching of the main
app: `_worthCaching` covers only `/read`, `/static/*` and `/api/img`, so its
requests stay network-first and pass straight through.

Note a true standalone install (an Android WebAPK) is minted by Chrome via Play
Services. A Chromium derivative may still only offer a bookmark-style shortcut,
in which case installing once from Chrome is what produces a real app icon.

**This applies to every layout whose tree is a drawer — single *and* medium
(721–1100px).** Gating it on single-pane mode alone was a bug: a tablet sits in
medium, so the guard never armed and Back closed the tab there exactly as it had
on the phone. Wide is deliberately excluded — the tree is a permanent column, so
there is nothing to toggle, and a Back that visibly does nothing is a trap rather
than a step. The guard also re-arms from `updateSingleMode()`, because rotating a
tablet moves it between wide and medium after load.

**Back therefore never exits the app in single-pane mode. That is deliberate**,
and was asked for after a stray press closed the tab mid-read: this is a
self-hosted reader you live in, and losing the session costs more than being able
to reverse out of it. Leaving is still an ordinary browser action — close the
tab, switch apps — it is only Back that no longer does it. Desktop is untouched;
the whole mechanism is gated on `isSingleMode()`.

The spare is re-armed the instant it is consumed, so consuming one entry and
pushing one back leaves the stack the same size: this can run indefinitely
without growing history and without running out of presses (verified at ten
consecutive presses, `history.length` flat). An article and a feed-scoped list
each already have a real parent underneath, so neither arms it, and a scope
param is required so the bare landing screen is excluded — there is no folder
list to toggle there, and Back on the app's front door should do the ordinary
thing. Forward navigation clears the flag, since whatever was pushed now sits
above the spare.

The handler lives in `index.html` rather than `app.js` because it is registered
before `app.js` loads, so `stopImmediatePropagation()` also suppresses that
file's `popstate` handler. That is deliberate: when the spare is consumed nothing
actually navigated, and app.js's handler would refetch the list already on screen.

The list toolbar's top-left control mirrors the same hierarchy. Scoped to one
feed it is a back arrow labelled with the folder name (`selected_folder_name`,
server-rendered) that goes up to the folder's list; at the folder list it is the
hamburger that opens the drawer. Two controls rather than one that changes
meaning, because a button reading "Folders" that does not open Folders is worse
than either.

### Off-site links never open in the reading tab

Following a link in place replaces Lectio and loses your position in the list —
and on a phone, where Back now toggles the folder drawer rather than leaving,
there is no cheap way back to it.

Every off-site `http(s)` link therefore opens in a new tab. This is done by
setting the anchor's **own `target`**, never `window.open`: a real tab is what
the browser's normal activation produces, whereas a scripted open is what popup
blockers stop and what a phone renders as an awkward floating window. `rel` is
set to `noopener noreferrer` alongside it — without `noopener` the opened page
gets a handle on `window.opener` and can rewrite the reading tab out from under
you (reverse tabnabbing).

Deliberately narrow: same-origin links are app navigation, in-page fragments are
how footnotes and tables of contents work and must stay put, and `mailto:`/`tel:`
would hand a blank tab to a handler that cannot close it.

It is enforced in three places because there is no single one that covers
everything:

- **`services/html_sanitize.py`** marks external links as it sanitizes. This is
  the correct fix at the source, but sanitization runs at *ingest*, so it only
  applies to content stored from that point on.
- **`templates/index.html`** carries a capture-phase click listener for the main
  app. This is what covers article bodies stored before the sanitizer change, and
  anything injected into the shell later.
- **`static/reader.js`** carries the same listener for Read Mode, which loads
  none of `app.js` and serves HTML sanitized when it was stored.

The sanitizer allows `target`/`rel` on `<a>` but never trusts them: it overwrites
both on every external link and deletes them everywhere else, so a feed can
neither choose its own target nor drop the `noopener`.

### Resume where you left off

Every attempt to stop Back leaving the app failed, and each failed *on purpose*
at a different level:

- **Chrome** marks script-pushed history entries skippable to defeat back-traps,
  so the spare entry can be walked straight past.
- **Android** exits any app at its root. Installing Lectio as a WebAPK — which
  does work, the manifest and worker meet every installability check — does not
  change that. A standalone install still exits on Back.

So the app stops trying to keep you in it and makes leaving cost nothing
instead. The current position (URL, pane level, post-list and article scroll) is
written to `localStorage` — not `sessionStorage`, which dies with the tab, which
is the exact event being defended against — on `pagehide`, on
`visibilitychange` to hidden, and on every navigation. Opening the app then
lands you back where you were.

The restore runs in an inline script in `<head>`, before anything renders, so it
is a `location.replace` rather than a visible jump — and `replace`, not `assign`,
so resuming leaves no history entry for Back to bounce off.

Three rules keep it from becoming its own trap:

- **A URL with a query is an explicit destination** and is never overridden. Only
  a bare `/` counts as "just opened the app".
- **The wordmark links to `/?home=1`**, so "take me to the top" still exists.
  Without the parameter it would be a bare `/` and would resume, leaving no way
  to reach the landing.
- **Positions older than 7 days are ignored** — long enough to cover a weekend
  away, past which dropping you into a half-read article is a surprise rather
  than a convenience.

Scroll is re-applied after two animation frames plus a short delay, because the
article pane's height depends on images and the chunked post list grows as it
reveals; a `scrollTop` set too early lands against a shorter document and clamps.

Read Mode does not participate — it has its own navigation model and no Back
guard either (see Plan.md).

### Pull down in an article for Reader view

On a phone, pulling down from the top of the article pane toggles Reader view,
and pulling again comes back — the Reader-view button sits in a toolbar that is
easy to miss and awkward to reach one-handed.

This is **not** a revival of pull-to-refresh, which was removed deliberately and
stays removed (`window.bindSinglePanePullToRefresh` is still a no-op stub).
Different gesture, different surface, different action.

The gesture delegates to `#entry-readability-button` rather than reimplementing
the toggle, so both directions come for free from the control that already owns
them. Listeners are on `document` (a pane swap replaces `.pane-entry` wholesale)
and stay **passive**: the browser's own pull-to-refresh is suppressed with
`overscroll-behavior-y: contain` in CSS rather than by calling `preventDefault`
on every touchmove. Three guards keep it from eating ordinary input — the pull
must start at `scrollTop === 0`, travel at least 90px, and be clearly vertical.

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

## Page weight: lazy HTML fragments

At thousands of feeds, any template section that renders a row per feed is
megabytes of HTML — far heavier than the posts themselves. The rule: per-feed
row sections must not render inline in `index.html`. They live in `_*.html`
fragment templates served by dedicated GET endpoints, and the page ships a
small container `<div data-lazy-src="…">` that client JS fills on first open.

Current fragments:
- `/settings/feeds/panel/{folders,stale}` (`_settings_feeds_folders.html`,
  `_settings_feeds_stale.html`): the Settings → Feeds folders table (a hidden
  row per feed, including disabled) and the Stale view (every active feed
  ranked by last-post age), fetched on first open of the Feeds tab / Stale
  view.
- `/tree/folder-feeds/{folder_id}` (`_tree_folder_feeds.html`): one sidebar
  folder's feed `<li>` rows, fetched on first expand. Only the selected
  folder inlines its rows (the active-feed highlight and auto-expand must
  work on full page load); the same template is `{% include %}`d there so the
  markup can't drift. `updateScopeActiveState` fetches a folder's rows when
  SPA navigation selects a feed whose folder hasn't loaded, and the loader
  re-applies the unread-only tree filter to injected rows.

This works without rebinding because row interactions are event-delegated on
stable ancestors (`#settings-tab-feeds`, `nav.tree`, `document`) — new tree
row handlers must follow that pattern, never per-element binding at load.
Only small shells with direct `getElementById` bindings (search input,
comparison toolbar) stay server-rendered in the page. Tree link hrefs carry
initial sort/filter state (rebuilt server-side from remembered preferences in
the fragment path) but the SPA re-stamps them from live state at click time.
Fragment responses are `Cache-Control: no-store`, like the page itself.

The app's main script lives in `static/js/app.js` (long-lived cache, busted by
`?v={STATIC_ASSET_VERSION}` — new static files must be added to
`_static_asset_version()`'s hash list or their changes won't bust caches). It
must stay Jinja-free: template-derived values reach it via the `window.*`
config object rendered in the document `<head>`. Only small inline scripts
remain in `index.html` (head config, CSRF shim, theme bootstrap, layout
shell) — the CSRF shim and theme bootstrap must run before anything else, and
the theme bootstrap uses `document.write`, which Chrome may block for
parser-inserted external scripts on slow connections.

## Adaptive layout model

Lectio uses responsive layouts rather than a fixed three-pane assumption:
- wide desktop: 3-pane side-by-side,
- medium tablet landscape: 2-pane refinement,
- narrow phone portrait: 1-pane drill-in navigation.

The priority is fast triage, not always showing three panes.

**Stacking bands.** Once a pane becomes an overlay the z-index ordering stops
being decorative, so the values are banded and the band is the contract:
**250–320 = overlays** (`.medium-pane-backdrop` 250, the medium/wide folder
drawer 260, the single-mode backdrop 290, the phone folder drawer 300,
`.topbar-menu` 320); **340+ = things opened on top of an overlay**
(`.context-menu` 340, `.context-submenu` 341); **1000+ = popup pickers**
(`.lectio-pin-menu`, `.lectio-quire-menu`, …) and **1190/1200 = toasts**. The
rule that matters: *a control opened from inside an overlay must outrank that
overlay*. `.context-menu` sat at 50 and `.context-submenu` had no z-index at
all, which is invisible on desktop — the folder pane is in normal flow there —
and broke the moment the same markup became a fixed drawer: a long-press on a
folder opened a menu painted behind the list it came from. Pick the band, not a
number, and never reach for 9999 — that is how the *next* overlay ends up
underneath something it should cover.

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
repaired by **Settings → Utilities → Fix multi-folder feeds**
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

## Folder Properties counts in SQL, not by hydrating entries

`get_folder_properties` looped `reader.get_entries(feed=url)` over every feed in
the folder and counted in Python. On the Deals folder — 17 feeds, 31,843 entries
— the dialog took **74 seconds**; the root folder, which resolves to every feed
in the library, was far worse. Nothing the dialog shows needs an `Entry` object:
a count, an unread count and the oldest date per feed are all aggregates. (A
`newest` date was being computed the same expensive way and never read.)

It now issues one `GROUP BY feed` per 900-URL chunk — chunked because the root
folder is past SQLite's bound-variable limit — using
`COALESCE(published, first_updated)`, the same expression the entry sort window
uses so an undated entry falls back to when reader first saw it instead of
sorting as NULL. Deals answers in 0.25s and the root folder in 1.18s, with
identical totals.

The trade is deliberate: `oldest` no longer honours per-entry date overrides,
and it feeds only the articles-per-week estimate on this one dialog.

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

## Remembered sort: Feeds and Saved keep their own

**Feeds and Saved remember their sort separately**, and a remembered sort is only
written by an *explicit* choice. Both halves were bugs:

- One shared `sort_by`/`sort_dir` pair meant picking an order in Saved silently
  re-sorted Feeds. They are different jobs — a publish-date backlog versus a
  to-do pile — so `sort_setting_keys(star_only)` splits them. The unprefixed keys
  stay Feeds' so existing installs keep the value they had.
- The index used to re-save the remembered sort on **every** plain load, passing
  it through `normalize_sort_by` first. So any stored value the normalizer did not
  recognize was silently replaced by the default — the preference destroying
  itself with nothing to show it had happened. Persisting only when the request
  carries an explicit `sort_by` also gives node-specific defaults (Read Mode's
  Inbox opens star-date-ordered) somewhere to live: applied without a URL param,
  they cannot overwrite the scope's remembered order, so leaving the node
  restores it.

**`normalize_sort_by` keeps `starred` behind `allow_starred=True`.** It exists for
Read Mode's Inbox, and blessing it globally let it reach the index, which persists
what it is handed; the regular sort menu has no entry for `starred`, so nothing
rendered as active and the toolbar showed "Published newest" while the list was
ordered by star date. Reported as the Feed view reverting to "Pub new" after
switching in and out of e-ink mode.

**A sort is a pair, and any path that can put half of one into a URL can rewrite
the preference.** This bug has now happened twice in different code. Refreshing a
feed rewrote the remembered sort because `refreshCurrentFeedOrFolder` substituted
its own `'desc'` for an absent `sort_dir` — absent because the templates emit the
parameter only when it differs from `DEFAULT_SORT_DIR` ("asc"), so the JS default
had simply never agreed with the server's. `build_sort_query` then put
`&sort_dir=desc` in the redirect, the index persisted it as an *explicit* choice,
and the preference was gone. It could only bite someone whose preference was
oldest-first. The Read Mode Inbox had the same shape from the other side: its sort
*key* was guarded against persisting but its *direction* was not, so visiting the
Inbox flipped Saved from oldest-first to newest-first and it stayed. The fix in
both cases is to pass the parameter through rather than invent one — absent means
"not in the URL", the redirect carries nothing, and the remembered preference
stands. Suspect this first the next time an order "won't stick".

## Async bulk mark-read

`/feeds/mark-read`, `/folders/mark-read`, and `/entries/mark-older-than-read` serve two response modes controlled by the `X-Requested-With` request header:

- **`lectio-mark-read`** (sent by the JS fetch path): returns `{"ok": true, "marked": N, ...}` with HTTP 200. The client applies an optimistic in-place read-state update via `applyBulkReadState()` before the fetch completes.
- **Anything else** (native form submit fallback): returns an HTTP 303 redirect to the main page with a `message=` query param.

The JS layer reads the CSRF token explicitly from `<meta name="csrf-token">` and adds it as `X-CSRF-Token` on every async POST.

## Feed discovery: which feed a page actually means

Two entry points share one set of rules, and must: `probe_url` previews what the Add dialog shows, while the Add route itself re-discovers through `discover_feed_urls_ex`. Any divergence means the dialog promises one feed and the button subscribes to another — which is exactly what happened when the page-path fix below landed in only one of them.

**Page path before site root.** Multisite WordPress puts a whole blog under a path (`devblogs.microsoft.com/oldnewthing/`) while the domain root serves a firehose of every blog on it. Probing the root first meant subscribing to "The Old New Thing" silently handed back "Microsoft for Developers". The more specific feed is the one the user asked for; a path with no feed of its own still falls through to the root.

**Gone vs refused.** A stale `<link rel="alternate">` is discarded only when positively confirmed dead — 4xx/5xx under the current identity *and* a browser-identity retry, with 405/501 and network errors left alone. Redirects are now followed one guarded hop at a time (re-running the SSRF check per hop, so no probe is ever bounced blind to an internal address): a stale tag is often an `http://` URL whose 301 hid the 404 behind it.

When every advertised link is dead and nothing else answers, what happens next depends on *why*:

- **Gone (404/410)** — report "no feed found", naming the dead address and pointing at Page Feed. Handing the link back produced the worst outcome available: the dialog says it found a feed, the add route then refuses it, and nothing appears in the feed list. The failure toast already offers a "Create page feed" button, so this lands the user where they need to be.
- **Refused (403, 429, 5xx)** — still offered. The server declined to answer a HEAD; that is not proof the feed is absent, and reader's real GET may get through. This is the bot-walled case the last resort exists for.

**Two kinds of known-site rule.** `_SITE_FEED_REWRITES` are pure URL functions (Pinboard, ArtStation, Behance, freeCodeCamp, and the numeric Tapas form) — no network, applied before the fetch. `_SITE_BODY_FEED_EXTRACTORS` are the other shape: the feed address exists only in the page body, so they run *after* the fetch, against HTML discovery already has. Tapas is the case that needed it — it advertises no `<link rel="alternate">` at all (its only alternate is the mobile page) and its canonical link points at the latest *episode*, not the series, so `tapas.io/series/<slug>` is invisible to generic discovery. The series id lives in the markup as `seriesId:` / `data-series-id`, which is what the community userscripts scrape by hand. Extractors run only when nothing was advertised, and their result flows into the same liveness check as any advertised link, so a stale id is caught rather than offered. Both entry points call the same helper on the same slice of the same HTML — see the divergence warning above.

## Fetching a feed: three ways it fails before it ever parses

All three were found on the live library on 2026-08-12, and all three presented
as "this feed is broken" while the site worked fine in a browser.

**A challenge page is a block, not a malformed feed** (`services/bot_challenge.py`).
An anti-bot interstitial served in place of a feed arrives as a 2xx whose body is
HTML, then fails to parse — indistinguishable from a genuinely broken feed unless
you look. poorlydrawnlines.com was logged as *"could not be parsed as a valid
RSS/Atom document"* for months while returning a SiteGround captcha as **HTTP
202**, which is also why no count of blocked feeds could ever be right: anything
keyed on 403 never saw it. The fetch hook now sniffs for vendor markers
(SiteGround, Cloudflare, Imperva, Sucuri, DDoS-Guard, AWS WAF) and raises a
stable `bot challenge: blocked by <vendor>`. Status is deliberately not part of
the test. The detector is narrow on purpose — a site serving its ordinary
homepage at a dead feed URL is *moved/dropped*, a different failure with a
different fix.

⚠ These blocks are keyed on the **client IP**, not the user-agent: the same URL
fetched with the honest UA and with a full browser identity returns a
byte-identical challenge naming our own IP. All 29 of the library's genuine
403s were already flagged for browser identity and still 403. Escalating the UA
does nothing, so detection here exists to *label* the failure, not to get past
it.

**One illegal byte costs a whole feed.** reader parses with a strict SAX parser,
so a single character XML 1.0 forbids makes the entire document not well-formed.
inventwithpython.com shipped a raw `0x0B` mid-sentence and its 2.7MB, 100-entry
feed failed outright at line 19918. The fetch hook strips exactly the C0 controls
XML forbids and keeps the three it allows (tab, LF, CR), so a feed that was
already valid comes through byte-identical.

⚠ **feedparser reads that same feed happily.** Verifying a replacement URL with
feedparser reported "200, 100 entries, looks great" and the feed then refused to
ingest. When checking whether a feed will work, check with *reader*.

**Announcing yourself as a crawler invites a fake 404.** Auto-discovery used to
send `Lectio/1.0 (RSS auto-discovery; +…)`, and filters match on that phrase:
chickensoft.games returns a fabricated 404 to any UA containing it while serving
200 to `Lectio/1.0`. Discovery was the only part of Lectio sending it, so it was
the only part that could not read the site. The damage went past a failed lookup
— `probe_url` reported "server denied the request", `refusal_is_forceable()` read
that as the site refusing us, and Add Feed offered **Subscribe anyway**, the
husk-feed path the add-feed code explicitly warns against, while suppressing the
page-feed offer that was correct. Discovery now sends the same honest identity as
every other fetch. Still names the app and links the repo; only the description
of the activity is gone.

## Feed auto-taggers

Three functions run at startup to apply strategy and display defaults without user action:

- `_auto_tag_artwork_feeds()` — matches `artstation.com` and `deviantart.com` feed URLs → `strategy=artwork`.
- `_auto_tag_webcomic_feeds()` — matches feeds in folders whose name contains "comic" → `strategy=webcomic`. Artwork wins if both conditions apply.
- `_auto_tag_github_release_feeds()` — matches `github.com/*/releases.atom` URLs → `strategy=og_scrape` + `show_lead_image_as_thumb=0`. GitHub generates a unique social-preview card per release; thumbnails are suppressed because the card is contextual rather than a post image.

All three skip feeds where `feed_lead_image_strategy.manual=1` (user has explicitly chosen a strategy in Feed Properties). To add a new tagger, follow the same pattern and register it in `lifespan()`.

## Feed-provided tag suggestions (`entry_feed_tags`)

**Numbers-only tags are dropped at capture, from feed categories and page scraping
alike.** They are comment counts, post ids, pagination and bare years — Josh's test
was "trying to think where a numbers-only tag would be useful … definitely mixed are
useful", so anything carrying a non-digit survives: `80s`, `3d`, `2.5 Admins`,
`2020 election`, `Windows 11`. A stray `84` reached lemire.me's suggestions this way
and 580 stored rows were bare numbers.

Separately, **an archive year-list is dropped as a run**: nwcpp.org carries
2000–2026 down the side of every page and all sixteen landed on one post. Five or
more distinct 4-digit years on a single page is a sidebar, not a tag set, so the
whole run goes rather than any single year being judged on its own.

**Subscriber-only posts are detectable without any marker.** Substack publishes
none — no category, no audience field — but ships a body containing only a "Read
more" link back to the post. `is_paywall_stub` requires both a body under ~120
characters of text *and* that its only link points at the entry's own URL, which is
what keeps it off a genuinely short post (a link roundup points elsewhere). Measured
on abortretry.fail: 17 of 20 items were 9-character stubs against three real posts of
19k–38k. The per-feed `hide_paywalled` pref marks them read at fetch time, mirroring
`hide_shorts` — non-destructive, still findable under All, and opt-in because a
*partial* feed is all stubs by design and enabling it there would empty the feed.

**Feed-tag suggestions are NOT filtered automatically, and that is a considered
position.** Two heuristics were tried on live data and both hid tags the user
wanted:

1. **Coverage** — suppress a tag carried by ~every entry of a feed, on the theory
   that it says nothing about any one entry. It correctly caught `Popular Deals`
   (2,525 slickdeals posts) and `VinylDeals` (576). It was then killed by a
   guitarplayer.com tag feed: `Lessons` is on every post *and* is exactly the tag
   you want when filing a guitar lesson. These chips are for **filing**, not for
   telling entries apart, so uniformity is not disqualifying at all.
2. **Feed-name echo** — suppress a uniform tag that restates the feed's title
   (splitting camelCase, so `VinylDeals` matches "Deals on Vinyl Records"). Killed
   by the *actual* title, "Latest from Guitar Player in Lessons": a tag feed puts
   its tag in its own name. Matching the feed URL fails identically —
   `/r/VinylDeals/` and `/feeds/tag/lessons` have the same shape.

The difference between `VinylDeals` (a place, useless) and `Lessons` (a kind of
content, wanted) is **semantic**, and nothing in the feed metadata expresses it.

⚠ The asymmetry is what decides the default: **a useless chip is cheap, because it
is ignored. A hidden wanted one is invisible.** So everything is shown and the
suppression belongs to the user. Resist a third heuristic; the first two each
looked convincing against the data that motivated them.

The × on a chip records that decision in `suppressed_feed_tags (feed_url, tag)`,
compared through `normalize_tag_value` on **both** sides. That matters: the chips are
rendered normalized (lowercased, spaces to hyphens), so the × sends `popular-deals`
while the stored feed tag is `Popular Deals`. A plain lowercase compare yields
`popular deals` and misses — so every **multi-word** tag reappeared after being
dismissed while single-word ones like `python` stuck, because those normalize to
themselves. The asymmetry ("other tags I've removed elsewhere seem to stay gone") is
what identified it. **Per feed, not global** — `Forum` is noise on Slickdeals and may
be a real topic elsewhere. It hides a chip; it does not forget a fact, so the
`entry_feed_tags` rows stay and keep feeding the tag-filtered feed adapters. Undo
lives in Feed Properties → **Hidden tags**, because a mis-clicked × must have a way
back and that list is the only place the decision is visible.

**Boilerplate feed tags are hidden by the user, per (feed, tag) — nothing is
filtered automatically, and that is a decision, not an omission.** Two automatic
heuristics were built and both reverted on 2026-07-29 (`1381cbc`).

*Coverage* — suppress a tag carried by ~every entry of a feed — caught the
motivating cases exactly: `Popular Deals` on slickdeals, `VinylDeals`,
talkpython's eight-tag block. It also hid `Lessons` on a guitarplayer tag feed,
where `Lessons` is precisely the right tag. These chips exist for **filing**, not
for telling entries apart, so uniformity is not disqualifying. *Feed-name echo*
— uniform, and the tag's tokens are a subset of the feed's title — failed the
same way, because a tag feed puts its tag in its own name ("Latest from Guitar
Player in Lessons").

`VinylDeals` is a place, `Lessons` is a kind of content, and nothing in feed
metadata carries that distinction. The asymmetry picks the default: an unwanted
chip is cheap because it is ignored, a wanted chip that is hidden is invisible.
So the suppression list is the user's — `suppressed_tags` /
`set_tag_suppressed`, edited from the chip's × and reviewable under Feed
Properties → Hidden tags.

Suppression hides a **chip**; it never forgets a fact. The stored rows are
untouched, since the table is also the data foundation for tag-filtered feed
adapters, where "every post in this feed is tagged VinylDeals" is worth keeping.

`reader` discards entry categories (RSS/Atom `<category>`) at ingest — its `Entry` type has no tags attribute — so Lectio captures them itself at the only point the raw feedparser result exists: `SanitizingFeedparserParser.__call__` (`services/reader_sanitize.py`). After `_process_feed`, the parser hands `(entry_id, tags)` pairs to an **injected sink** (`set_entry_tag_sink`, wired in `main` to `FeedTagService.record_entry_tags`), keeping services free of main/DB imports. Design notes:

- **Tenancy for free.** Parsing runs synchronously inside `reader.update_feed(s)`, always in a user context (request thread or `_run_in_user_context` background threads), so the sink's `get_meta_connection()` resolves the correct per-user meta DB at call time — the same guarantee `get_reader()` relies on. The service itself is tenancy-unaware (LeadImageService pattern).
- **Id mapping re-derives, never zips.** `_process_feed` skips unparsable entries, so positions don't line up; the capture re-derives each raw entry's reader id (`id`, falling back to `link` for RSS-family feeds) and keeps only ids present in the processed set. A sink failure is logged and swallowed — tag capture must never fail a feed parse.
- **Storage** (`services/feed_tags.py`): per-user table `entry_feed_tags(feed_url, entry_id, tag, first_seen_at, PK(feed_url, entry_id, tag))`. Tags are stored **raw** (case-preserving) and normalized to Lectio tag format (`normalize_tag_value`) only at display — the raw text is the data foundation for future tag-filtered feed adapters. Replace-per-entry semantics: re-seeing an entry replaces its rows (publisher edits propagate); entries absent from the current fetch window keep theirs. Rows are pruned on feed removal and follow feed-URL migrations (`_feed_url_tables`); no other retention.
- **UI.** The entry pane shows the captured tags as **[ + tag ▲ ▼ ]** chips in the tags row. The leading **+** applies the tag as a manual tag through the existing `/entries/tags` append pipeline (hidden when already applied). **▲/▼** POST `/rules/tag-filter/toggle` (`toggle_feed_tag_filter`), which edits the **feed-scoped** `tag_filter` rule in place: same sign → remove, opposite → flip; the rule is created **disabled** on first use — chips are a tuning surface, and the user arms the rule in Automation — and deleted when the spec empties; folder/global rules are never touched; chip edits never change the enabled flag. Only when the rule is already enabled does a chip edit apply the new spec to unread entries immediately (logged to automation history as a manual trigger). Active signs render lit via `feed_tag_filter_signs`. This replaced an earlier ephemeral implementation that re-fetched the live feed in a background thread and fuzzy-matched entries against an in-memory cache — the DB lookup is exact-key and instant.
- **Synthetic feeds** (dev.to, DeviantArt) don't write the table directly: they emit `<category>` elements in their generated RSS, which flows through the same parser capture — one code path. DeviantArt's browse/gallery API omits deviation tags (they need `/deviation/metadata` calls), so DA categories appear only when the tags field is present; the scraper has no tag source.
- **Tag-filter rule.** The `tag_filter` rule type in the rules engine (`highlight_keywords`) consumes this table to tame firehose feeds. The whole spec lives in `keyword` as one comma-separated field with three strengths: `-tag` **drops**; `+tag` (or bare) is a **good** tag — it rescues an entry from drops but its absence never cuts anything; `++tag` **requires** — tagged entries lacking every required tag are cut (opt-in whitelist). Commas — not spaces — separate, so multi-word tags are typed as-is (`+windows 11, -rust`) and `normalize_tag_value` hyphenates both sides before comparing. `_run_tag_filter` runs in `_run_automation_after_refresh` per refreshed feed in scope (and via dry-run/run-now) and auto-marks matching unread entries read (same suppression as `mark_as_read`, logged to automation history). The entry's author rides along as a pseudo-tag (`author_filter_token`: 'Steven Parker' → `by-steven-parker`), so author tokens work in every position and ▲/▼ controls render next to the author name in the entry header; an authored-but-untagged entry is filterable. Evaluation: requires first (tagged entry lacking all required tags → cut), then drops (a good or required tag rescues; `+android, -iphone` keeps a post tagged with both, and Samsung posts still flow since good tags don't whitelist); **untagged entries are always kept** — a feed that stops tagging must not have its whole firehose suppressed. It runs *after* `update_feed` (the tag sink fires during parse, so the table is populated by then).
- **Writing the spec: one autocomplete control, two token grammars.** A `tag_filter` spec can only ever match what ingest captured, stored lowercase-hyphenated — so typing one against an unseen vocabulary (HackerNoon: 140 distinct tags in 20 items) is guesswork, and the failure is silent: a rule that matches nothing looks exactly like a rule that is working. `GET /rules/tag-vocabulary?scope=&scope_id=` resolves the draft's scope through the same `resolve_rule_feed_urls` the rule itself will use and returns `FeedTagService.tag_vocabulary` **normalized through `normalize_tag_value`**, so completing a suggestion produces a token that matches by construction, plus the entry count per tag — which is the actual decision (a tag on 9 of 10 posts is a filter; a tag on one is noise). Counts are merged across casing variants, since a publisher switching `AI` to `ai` must not halve the number the user is deciding on. Loaded lazily on focus and keyed by scope, because global scope is the whole table and most rules are not tag_filter. The **same** `attachTagAutocomplete` powers the per-entry tag input; the rule form differs only in the two things that are genuinely different about the grammar — comma separation (so multi-word tags are typed naturally, matching `parse_tag_filter_spec`) and the `-`/`+`/`++` sign, which is part of the *spec* and so survives completion untouched, unlike the per-entry `#`, which is decoration on the tag and is overwritten. It consumes its keys with `stopImmediatePropagation`, not just `preventDefault`: the rule form binds Enter-to-save on the same element, and a listener can only stop one registered after it — so the autocomplete must be attached first, and is.
- **Every captured tag reaches the chip row; only the first eight are on screen.** `MAX_FEED_TAG_SUGGESTIONS` was 8, which was a *fetch* cap, so anything past the eighth tag simply did not exist client-side. Rock Paper Shotgun ships **28 tags per post** and puts `PC` tenth — so the row offered every platform to drop (`Apple`, `iOS`, `Mac`) and no way to name the one to keep, which is precisely the `+pc` rescue the drop needs. The hard cap is now 40 and `FEED_TAG_CHIPS_COLLAPSED` (8) governs *display*: the overflow chips render with `hidden` + `is-extra-feed-tag` and a `+N more` button reveals them. Hidden rather than omitted, for the same reason the suggestion list has no auto-suppression heuristic — a chip you ignore costs nothing, a chip that is not there cannot be filtered on. The late-injection path in `maybeInjectFeedTagChips` applies the same collapse, or backlog entries (whose chips arrive from `/entries/feed-tags` after render) would dump all 28.
- **The dry run explains an empty result.** `-mac, +pc` reads as "drop Apple, keep PC" and is the natural first spec to write — but Rock Paper Shotgun tags *platform availability*, so all 41 of its Mac-tagged posts are also tagged `PC` and the rescue cancels the entire rule. Zero matches then looks exactly like a rule that is working, which is the failure mode this whole feature has. `_run_tag_filter` counts entries a drop tag caught that a good/required tag let through, and returns `rescued` + the top `rescued_by` tags; the Test panel prints them under the count and, when nothing matched, names the rescue as the reason. This is a diagnostic, not a policy change — the rescue semantics are correct and deliberate (`+android, -iphone` must keep a post tagged both). The same applies to a **good-only** spec (`+wallpapers`), which cuts nothing by construction — good tags rescue from drops and whitelist nothing, so with no drops there is nothing for them to do — yet reads perfectly naturally as "keep these". `good_only` is returned whenever the spec has good tags and neither drops nor requires, and the panel names the two specs that do have teeth (`++tag` to keep only these, `-tag` to drop). Both notes exist because the three strengths are the one genuinely non-obvious thing about this rule type, and a bare zero teaches nothing.
- **Source-page fallback.** Entries whose feed never delivered `<category>` data (aged out of the publisher's feed window before capture, or a tag-stripping publisher) are tagged from the article page itself on open: `extract_page_tags` harvests `article:tag` / `keywords` / `parsely-tags` metas from the lead-image service's source-HTML cache (zero extra requests when primed); on a cache miss the entry-detail handler queues `queue_source_html_fetch` and the tags appear on the next open — the same deferral pattern as image captions. Harvested tags are persisted to `entry_feed_tags` like feed tags; the fallback only runs when the entry has no rows, so feed-provided tags stay authoritative.
- **Synthetic-feed gotcha (fixed):** dev.to/DeviantArt XML is regenerated from their per-user entry tables (`devto_entries`/`deviantart_entries`), not from the live API objects — tags must persist in those rows (comma-joined `tags` column) to come out as `<category>`. Re-seen articles backfill/refresh the stored tags while still in the API window.

## FakeFeedz entries get the article's own date

A listing page is a wall of links: titles and hrefs are there, dates usually are
not (chickensoft.games shows none at all on `/blog`, only on each post). Stamping
every scraped entry with the scrape time made a fresh feed look like its whole
backlog was published the second it was added, and made sorting by date
meaningless — ten entries all reading `2026-08-12 23:18:17`.

`_article_published_at` fetches each **new** entry's page and asks it for a date,
trying the publisher's own metadata first (`mine_publish_date`: JSON-LD,
`article:published_time`, `<time datetime=…>`) and the date the page merely
*prints* second — the same order the re-fetch path uses. Cost is one fetch per
new entry: an entry already in `scraped_entries` is never re-fetched, so a steady
feed pays nothing per refresh and only the first scrape pays for its backlog. Any
failure falls back to "now", because a missing date must never cost the entry.

`publish_date.from_visible_text` needed a second tier for this. Its matcher wants
an element whose `class`/`id` says `date`/`posted`/`byline`, which utility-CSS
frameworks never provide — the byline lives in
`<p class="text-[var(--color-muted-foreground)] text-sm font-serif">April 26, 2026</p>`.
The fallback accepts an element whose **entire text is a date and nothing else**,
which is a comparably strong signal without matching the dates scattered through
comment timestamps, related-post rails and copyright footers. `Updated April 26,
2026 by Chris` does not qualify. It runs only after the labelled pass, so a
publisher that marks its date up properly still wins.

## dev.to filtered feeds

Dev.to's RSS (front page and per-tag) is an unfiltered firehose that mixes languages, while its public unauthenticated JSON API (`GET https://dev.to/api/articles`) exposes a per-article `language` label, reaction counts, and a `top=N` ranking window. `services/devto.py` follows the DeviantArt/FakeFeedz synthetic-feed pattern: one polite API request per refresh, client-side filtering (the API ignores `?language=`; we filter on dev.to's *own* `language` field, deliberately not our own detection), then render to `file://` RSS under `DATA_DIR/devto-feeds/` for `reader` to ingest. Per-feed config (tag, top-window days, English-only, min reactions, tags_exclude) lives in the per-user meta table `devto_feeds`; the Add Feed dialog detects dev.to front-page/tag URLs client-side (mirroring `parse_devto_url` — user/org pages are left to their normal small RSS) and reveals the filter fields, and the config is editable later via feed Properties → Tuning (`POST /devto-feeds/{id}/config`). Cover images seed the lead-image cache via the same sink mechanism as DeviantArt; deletion is dispatched in `purge_orphaned_feed` alongside the other rendered-feed types. Filter changes shape what arrives from then on — already-ingested entries are kept.

## DeviantArt integration

DeviantArt's legacy `backend.deviantart.com/rss.xml` is behind a CloudFront WAF that 403s datacenter traffic, so Lectio uses the DeviantArt API and renders results to `file://` RSS files like FakeFeedz (services/deviantart.py). Per-user creds live in app-settings.

**Bluesky image recovery** (`services/bluesky.py`): per-profile bsky.app RSS (`/profile/<did>/rss`) is text-only, and content-labeled posts (e.g. adult) also expose no og:image on the web page. The images live in the post record and are served from the public `cdn.bsky.app` CDN, so Lectio fetches them from the public AT Protocol API (`app.bsky.feed.getPosts`) keyed by the post's `at://` URI — which the RSS feed stores as the entry id. `extract_entry_thumbnail_url` uses the first image for the list thumbnail; `get_entry_detail` appends all images to the article body. No auth and no label check at this layer — subscribing to the account is the user's opt-in. Cached in-memory (1h TTL) so list rendering doesn't re-hit the API.

- **Auth** — OAuth2. Public galleries use the *client-credentials* grant; the *authorization_code* grant (PKCE — DeviantArt requires `code_challenge`) connects the user's account for watch-list access. Tokens are stored per-user and auto-refreshed; the token request tries with-secret then without, tolerating both confidential and public clients.
- **Watch feed** (preferred) — one combined feed from `/browse/deviantsyouwatch` (everyone you Watch), instead of one feed per artist. A few paginated calls per refresh keep it under DeviantArt's strict per-user rate limit (`DeviantArtRateLimited` aborts bulk work cleanly; the scheduled refresh is round-robin capped).
- **Add = Watch** — while connected, adding a `deviantart.com/<user>` URL Watches that artist on DeviantArt (it then appears in the Watch feed) rather than creating a per-artist feed.
- **Tags** — browse/gallery responses don't include deviation tags; those need `/deviation/metadata` (up to 50 ids per call). Each feed refresh makes at most one metadata call for never-checked entries (`fetch_and_store_missing_tags`; `tags_fetched_at` is set even on zero-tag results so untagged deviations aren't re-queried forever), stores them on `deviantart_entries.tags`, and the regenerated RSS emits them as `<category>` — from there the standard `entry_feed_tags` capture renders the suggestion chips. The bulk watchlist sync's create path deliberately skips the lookup (adding N artists stays N calls); tags fill in on the first scheduled refresh. A rate-limited lookup is skipped, not fatal — entries land untagged and are retried next cycle.
- **Watch-list sync auto-resume** — `sync_deviantart_watchlist` is add-only and stops cleanly at the rate cap; instead of waiting for a re-click it schedules a background continuation (a daemon `threading.Timer` routed through `_run_in_user_context`) honoring the 429's `Retry-After` (conservative 15-min fallback), capped at 12 rounds per triggering run. A per-user in-process guard keeps the Settings button, the daily maintenance run, and a pending auto-resume from syncing concurrently. Timers don't survive a restart — the daily maintenance sync is the catch-up. The sync also reconciles: subscribed artists no longer on the watch list are reported, never auto-unsubscribed (a curated feed may deliberately outlive a Watch). The reconcile excludes the synthetic combined Watch feed (`source='watch'`, username `deviantsyouwatch`) so it isn't perpetually flagged as unwatched. Failed adds and unwatched artists are persisted structurally in `deviantart_sync_detail` (JSON alongside the `deviantart_sync_status` string) so the Settings → DeviantArt subtab lists them as links to `deviantart.com/<user>` — the user never has to read server logs.
- **Deactivated accounts** — a watched artist who deactivated their DeviantArt account returns `HTTP 400 "Account is inactive."` on gallery fetch (distinct from 404 = deleted/renamed). Such artists can never be added, so the sync would otherwise re-probe and re-fail them on every run. They're parked in the `deviantart_deactivated` table (`_is_da_deactivated_error` detects them), excluded from the sync's `to_add`, and shown as their own "Watched but deactivated" list in the subtab. Daily maintenance (`_deviantart_recheck_deactivated`, capped per run, oldest-checked first) re-probes them: a successful fetch means reactivation → subscribe and un-park; still inactive → bump `last_checked_at`; rate-limited → stop and resume next run.
- **Images** — deviations carry stable (non-expiring) signed `wixmp.com` image URLs. DA feeds are pinned to the `inline` strategy so the article lead image and list thumbnail derive statelessly from the embedded content image (no source-page scrape, nothing to clobber). `wixmp.com` is trusted in `_is_image_url_acceptable` (its long auto-generated filenames/UUIDs otherwise trip the avatar/ad heuristics) and routed through `/api/img`.
- The lead-image cache reads through to its DB table on a miss, so stored images survive restarts (the in-memory cache is seeded once under the default tenancy and otherwise warms lazily).
- The interactive on-open `queue_source_fetch` persists **only a positive result**. A `None` is ambiguous — a transient page-fetch failure is indistinguishable from a genuine "no image" — and this path runs once per opened entry with no retry, so storing `None` would cement a momentary miss as a permanent negative and blank a thumbnail the feed actually has (e.g. a Standard Ebooks cover, which lives in `media:thumbnail` and resolves via the page's `og:image`). Negative-recording is left to the background backfill, which retries on its own schedule.

## Combining feeds moves the entries, not just the curation

`_migrate_curation` used to walk `set(src_tags) | set(src_stars)` — curation,
not entries. A post with no tag, no star and no capture was never visited, so it
was neither matched onto a survivor twin nor synthesized, and it vanished when
`reader.delete_feed` ran. Combining two subscriptions to the same Webtoons comic
silently lost an unread post that way. Dropping uncurated entries is correct for
an *unsubscribe*; for a combine — the user asserting these two feeds are the same
feed — it is not, so the loop now walks every source entry.

Two things make that safe at scale. **Read state is carried, not reset**, or
combining an old feed would dump its entire history into the survivor's unread
count; a survivor twin that is already read is never resurrected as unread by an
unread source copy. And **per-entry meta follows the entry**
(`_rekey_entry_meta`: lead image, captured feed tags, content/title/date/link
overrides, media, read state, history), because a post that arrives with no
thumbnail and its hand-made title correction reverted is a worse outcome than
one that did not move. The re-key omits INTEGER PRIMARY KEY columns — copying
`read_history.id` verbatim collides with the row being copied from, so
`INSERT OR IGNORE` drops the copy and the follow-up `DELETE` loses the row.

## Combining feeds carries the offline captures

`_migrate_curation` moves a removed feed's manual tags and stars onto the survivor, and now its **starred-archive rows** too (`rekey_archive`, which carries the asset links and refuses to clobber a capture the survivor already has). Without that the captures stayed keyed to a feed that was about to be deleted: the articles were fine, but their offline copies became unreachable and the Saved view rendered them as archive-only *orphans* — from the archive row's own stale `link`, which is how it surfaced, as a combined feed's articles still showing their old dead URLs. Measured on the live library 2026-07-25, past combines had stranded **85** of them; `scripts/repair_orphaned_archives.py` re-attached them (64 re-keyed, 3 where the stranded row was the *better* capture and replaced a thinner twin, 18 redundant drops, 14 links refreshed).

Two subtleties the fix has to respect. The migration loop walks *curation*, not entries, so a capture on an entry carrying neither star nor tag is never visited — a sweep after the loop re-keys those, but only when the article exists on the survivor; synthesizing an entry purely to host a capture would put an uncurated row in the survivor, and the orphan view is already that capture's home. And the archived id set is read **once** up front, because the per-entry alternative opens an archive connection for every entry just to learn that most have nothing to move.

## WebSub (PubSubHubbub)

`WebSubService` (`services/websub.py`) implements the WebSub subscriber protocol:

1. **Hub discovery** — on feed add and periodically during refresh, `_discover_hub_url` fetches the feed URL and looks for `rel="hub"` in the HTTP `Link` header or in `<atom:link>` / `<link>` XML elements. A "no hub found" attempt is recorded in `websub_subscriptions.hub_tried_at` so the check is not repeated for 7 days.
2. **Subscription** — `subscribe(feed_url, hub_url)` posts `hub.mode=subscribe` with a random HMAC secret and a 7-day lease request. The row is written as `verified=0` until the hub confirms.
3. **Verification callback** (`GET /websub/callback`) — hub sends `hub.challenge`; `handle_verification` confirms the topic matches, marks the row `verified=1`, and echoes the challenge. FastAPI query-alias params (`hub.mode`, `hub.topic`, `hub.challenge`, `hub.lease_seconds`) map the dot-notation params cleanly.
4. **Push callback** (`POST /websub/callback`) — hub delivers content; `verify_push_signature` checks HMAC-SHA256 (or SHA1 fallback) against the stored secret. On success, for each subscriber `feed_refresh_service.update_feeds([feed_url])` fetches the new entries, then `_run_automation_after_refresh({feed_url})` runs the rules (mark-read, tag-filter, dedup, guid-churn suppression) on them — the same post-refresh step every scheduled/manual refresh performs. Omitting it (the state before this was fixed) meant WebSub-delivered entries bypassed all automation, so rules on prolific push publishers (e.g. realpython.com, which delivers almost entirely via push) effectively never fired.
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

## Read Mode — e-ink reading app (`GET /read`)

A standalone, distraction-free reading *app* for the saved/starred backlog, built
e-ink-first (the driver is reading the backlog on a Supernote Manta browser,
where Instapaper renders badly). The **Saved Articles** sidebar row is hijacked to
open it — a full navigation, since `_layout_shell.js` now bails on any link whose
path isn't `/`. It is deliberately **not** the app shell: its own light-themed
`static/reader.css` + `static/reader.js`, pure black-on-white, large adjustable
type, no shadows/gradients, and (critically) `transition:none/animation:none`,
which ghost on e-ink. Being standalone also sidesteps the app's localStorage-only
theming (a server-rendered page can't read it) and satisfies "auto light theme".

**Two states, one route.** With no `entry_id`, `reader_view` renders the **2-pane
browse** (`templates/read_mode.html`): a simplified saved tree (folders + manual-tag
buckets + an **Archive** node pinned at the bottom, each a plain full-navigation
`<a>`) on the left, and the item list for the selected node on the right. With an
`entry_id`, it renders the **paginated reader** (`build_reader_page`). Read Mode
ignores read/unread — it always lists starred items (`star_only=True,
read_filter='all'`); the browse node scope is `folder_id` / `tag` / `archived` / `q`.
A bare bookmarked `/read` lands on the browse **All (inbox)**. All tree counts are
**non-archived** totals (`_read_mode_saved_index`); tag buckets are restricted to
tags on inbox items with inbox counts (`_inbox_tag_counts`) and collapsed in a
`<details>` (heavy taggers have dozens). Archiving an item drops it from these
counts/lists but never strips its tags. Saved-article captures show their source
domain as the list subtitle (`_read_mode_subtitle`); feed-starred items show the
feed title.

**Paginated, not scrolled.** `reader.js` lays the article out in screen-width CSS
columns (`column-width:100vw`, stride = column-width + column-gap = 100vw) inside
an `overflow:hidden` viewport and turns "pages" by translating the column
container horizontally. Tap zones (left third = back, right two-thirds = forward),
**horizontal swipe**, and arrow/page/space keys turn pages; past the first/last
page it navigates to the prev/next article (`data-prev`/`data-next`, full loads).
`A−`/`A+` adjusts `--reader-fs` and re-paginates, persisting size in `localStorage`.
Prev/next follow the **selected node's** list.

**Archive vs Delete (the "done" axis).** Saved items need a *second* layer of
read/unread. Josh's framing (2026-07-29): a feed post only needs read/unread, but
**a star is a TODO** — "I still have to decide what to do with this" — and you can
read something and still not be done with it. So done is its own axis.

The two axes are deliberately independent, and all four combinations are
reachable: *read but not archived* is the TODO that survives reading; *archived
but unread* is deciding you don't need to read it at all. The model is triaging
an email inbox — you can tell from the list whether you want to read something,
keep it, or drop it, without opening it.

**`archived_entries` is its own table, not a column on `saved_entries`.** A
`saved_entries` row *is* the star, and Archive **removes** the star — so state
stored on that row could not outlive the act that sets it. The table also lets a
tagged-but-unstarred entry be archived, which the column could never represent.
Migrated by lifting the legacy `saved_entries.archived_at` column **once**, then
clearing the column in the same transaction. **Idempotent is not enough here.**
The first version used `INSERT OR IGNORE` and left the column populated, so every
boot re-lifted it: un-archiving deleted the `archived_entries` row, the old column
survived, and the next restart resurrected the item. It surfaced as "I've
unarchived them 3 times now and they keep coming back", and it is invisible until
something restarts the app — the same shape as the starred-archive backfill that
re-created orphan star rows at every boot. Clearing the source is also what
removes the second source of truth; a marker flag would leave a populated column
that still reads as authoritative. This also deleted the unstar-tagged panel's
`archived_at_lost` warning outright — unstarring can no longer discard archived
state, so there is nothing to warn about.

Archiving drops the item from the inbox (`resolve_reader_backlog(archived=…)` filters against
`get_archived_saved_keys()`; `None` = no filter, used for Search which spans
everything). The reader header's **Archive** button POSTs `/entries/archive`
(`set_entry_archived`); **Delete** simply un-saves via the existing
`POST /entries/saved` (`saved=0`); both are `fetch` calls carrying the page's
CSRF token, then advance to `data-next`. This **Archive** is *not* the offline
**starred archive** (`archived_entry` capture DB) — same word, different concept.

**Content resolution** (`resolve_reader_article_html`): archived readability copy
first (`_resolve_archived_readability_html`, shared with `/entries/readability` —
offline HTML with `/starred-asset/` URLs, survives dead sources), then a live
readability extraction, then the stored feed content — all already sanitized, so
no new sanitization surface.

**Mark-read is earned by reaching the last page, not by opening the article.**
Serving the reader page marks nothing. In an e-ink browse loop opening an item
is how you decide whether you want it, so marking on render turned every peek
into a read — the whole saved backlog could be cleared by scrolling through it.
`static/reader.js` posts to `/entries/read` once the last page is reached, which
is the first moment the whole article has been on screen; a one-page article
qualifies immediately, because there it is true. The post reuses the route's
existing async branch (`X-Requested-With: lectio-entry-read-toggle`) rather than
adding an endpoint — that branch already writes read state, `entry_read_state`
and read history from a tenancy-rebinding daemon thread, which is exactly what
the old on-render `_mark_entry_read_background` call did.

The client holds the mark until pagination has **settled**, and settling is a
readiness check rather than a delay. On a cold load a 12-page article measures as
**one page** until its stylesheet and images arrive — which satisfies "last page
reached" and marks an article read the instant it is opened. That was observed in
testing, not theorised, and a fixed timeout cannot fix it because the wait is a
race against however long those resources take. So `trySettle` re-measures every
`SETTLE_POLL_MS` and only trusts the count once `document.readyState` is
`complete` *and* every `<img>` reports `.complete` (which covers errored images —
they are settled, they just contribute no height). `SETTLE_MAX_TRIES` caps the
wait at ~5s so an image that never resolves can't block mark-read forever.

Only the **one-page** case actually needs this guard: a multi-page article can't
be marked without the reader genuinely paging to the end, whenever that happens.
The guard exists because a wrong measurement makes every article look one-page.

Both scopes follow this rule: the peek problem is identical in each, and the
scope isn't visible from inside the reader, so splitting the rule would make
"did that count as read?" unpredictable.

**Tagging from Read Mode is a tap-list, not a text field.** Filing on e-ink has
no hover, a slow repaint, and a keyboard that costs several. So the whole tag
vocabulary (`get_all_manual_tag_names`) ships with the page as inline JSON
(`__READER_TAGS__`, same no-reading-from-the-DOM style as `__READER_NAV__`) and
renders as large toggle buttons — applied tags first and inverted, the rest
alphabetical. A keyboard is needed only for the "+ New" field, which stays hidden
until asked for.

Each tap **applies immediately** and sends the *whole desired set* with
`append_mode=0`, so adding and removing are one request shape and closing the
panel half way still saved what was tapped. The server normalizes and caps
(`MAX_MANUAL_TAGS`), and its reply is treated as the truth — the panel re-renders
from it, so a tag typed as "Brand New Tag" coming back as three tags is visible
rather than silently assumed. The panel overlays the article instead of reflowing
it: re-paginating the body to make room would be a full-screen e-ink flash. The
header's `#n` count and Delete's `data-tags` are both re-synced after every
change, the latter so Delete's confirm keeps naming the right tags.

**Delete and Archive mean different things, and both had to change for the kept
model.** Josh's rule: *Delete removes star **and** tags; Archive just takes it out
of the inbox.*

- **Delete** = leave Kept entirely. Unstarring alone was a no-op on anything
  tagged — a tag keeps an entry on its own, so the row stayed in the list while
  the reader advanced as though it had gone. The client clears tags **first**,
  then unstars: the unstar route only enqueues removal of the offline archive
  when no manual tags remain, so the reverse order would strand the captured
  copy with nothing keeping it. Tag loss is unrecoverable, so the confirm names
  the tags; a plain unstar (no tags) stays unconfirmed because it is cheap to undo.
- **Archive** = out of the inbox, filing intact. The 2026-07-29 refinement (the
  `archived_entries` table above) is that Archive **removes the star** — the TODO
  is discharged — while tags, the offline capture, and pruning-exemption all
  survive, because "keep its contents" is the whole point. It works on tag-kept
  items too; the old "hide the button when only a tag keeps it" branch is gone.
- **Both Archive and Delete mark the entry read, at both levels.** Acting on
  something from the list *is* dealing with it, so leaving it unread would put it
  straight back in the queue it was just cleared from. Both also append to
  `read_history`, which is what makes a triaged item findable again.
- **Archive is a third keep signal**, next to starred and tagged —
  `entry_has_keep_signal`. This is not bookkeeping: archiving is *implemented* as
  an unstar, and the unstar path releases the offline capture and hard-deletes a
  `lectio:saved` husk once nothing keeps the entry. Miss the archived signal and
  the gesture that promises to keep the contents is the gesture that destroys
  them. `_prune_entries` honors the same three signals, or retention would delete
  archived posts nightly — they are read and unstarred, exactly its target shape.
- **Ordering is server-side.** `POST /entries/archive` writes the archived row
  *before* unstarring, and `POST /entries/discard` clears tags **and the archived
  row** *before* unstarring, both for the same reason: the capture is released
  only when the last keep signal goes. Delete forgetting the archived row failed
  quietly twice — the entry stayed listed in Archive forever, and the unstar saw
  a surviving keep signal and skipped releasing the capture, so Delete kept the
  contents it exists to drop. Deleting an archived item is not a contradiction:
  Archive means "done, keep it", Delete means "done, don't", so Delete wins. Delete used to be a client-side chain of two POSTs with
  that ordering encoded in a comment in `reader.js`; it is one route now, because
  the constraint is a property of the storage layer, not of the caller.

The **superseded** rule, kept because the reasoning still explains the code:
Archive used to keep the star and set `archived_at` on the star row, and was
hidden entirely for tag-kept items — a control that silently did nothing.

**The Inbox is STARRED minus Archived — not everything saved.** Read Mode's root
saved node was briefly labelled "All" and counted *kept* (starred OR tagged) minus
archived, which made it 24,672 items: the whole library wearing an inbox label.

The narrowing follows from what the two signals mean. A **star is a TODO** ("I
still have to decide what to do with this"); a **tag is filing** — already sorted,
and filing something is not a to-do. So tagged-but-unstarred entries live in the
tag tree, which is where you would look for them, instead of padding a queue.
`list_entries_for_feeds(kept_scope=…)` carries this: `"kept"` (star OR tag, the
main app's Saved view) or `"starred"` (the Inbox). `_read_mode_saved_index` returns
*filed* (tagged minus archived) alongside the inbox, because the tag counts must
still be counted over filed items — counting them over a starred-only inbox would
empty most of the tree.

**The Archive filter is applied before every clip, and there are three.**
`_sorted_star_key_window` sorts and clips *in SQL over the raw kept keys*,
`list_entries_for_feeds` clips its light records, and `merge_orphan_saved_entries`
re-sorts and re-clips after appending archive-only orphans. The done-axis filter
originally sat downstream of all three, in `resolve_reader_backlog`, so it chose
archived rows out of a window computed against the *unfiltered* backlog.

Live symptom (2026-07-29): one archived post among 24,672 kept. Newest-first
found it, because a recent post sorts high; oldest-first and Recently-starred
returned nothing, because it sorted to the far end and fell outside the first
150. The Archive node rendered "Nothing here" while its own count — read straight
from `archived_entries` — said 1. **A count and a list computed at different
layers is the recurring bug in this area**; it is the same shape as the
9,979-vs-24,695 tree mismatch.

`kept_entries_set` also folds in the archived keys, because archiving removes the
star: an archived, untagged entry is kept by no other axis and would be
unreachable in the one view built to show it.

**"All Saved" is a separate node**, not a mode of the Inbox: everything kept
minus archived, i.e. what the main app's Saved view shows. The Inbox has to stay
narrow to be a queue, but the two modes disagreeing about what *exists* is the
mismatch Read Mode is meant not to have — the tagged-but-unstarred items are all
reachable under Tags, and this is the flat view of them. It carries `kept=all`
through every hop, because it is otherwise indistinguishable from the Inbox
(same root folder, no tag, no archive, no search) and would silently inherit the
Inbox's starred-only scope and star-date default.

Two consequences worth stating, both from live reports:

- **The Inbox opens most-recently-starred** (`sort=starred` → `saved_entries.
  saved_at`, a fourth sort key beside post/received/history). A to-do pile is
  ordered by when you added to it; an old article starred today belongs at the
  top, which is exactly what publish-date order gets wrong. `saved_at` is stored
  in two shapes (SQLite `CURRENT_TIMESTAMP` and ISO-8601 from imports), so it is
  parsed rather than string-sorted — `' '` sorts before `'T'`, which would
  scramble a single day's stars.
- **That order must not follow you out.** `resume_sort` stows the order you were
  using when you entered the Inbox and hands it back on the way out, so leaving
  neither drags most-recently-starred into a folder where nothing is starred nor
  resets a folder you had set to Oldest. Same shape as the main app's
  `resume_read_filter`, which restores your filter when you close History.

The Read Mode **Tags** section now renders open. It was a collapsed `<details>`,
which was fine while tags were a side-bucket of a kept-everything inbox; now that
filed items are reachable *only* there, collapsing them made them look absent.

**Prefetching the next article warms its images, not its page.** The e-ink flash
on advance is mostly image decode, and the reader page itself is `no-store`, so a
`<link rel="prefetch">` would fetch the next article and immediately discard it —
cost with no benefit. Instead `prefetchNextImages` fetches the next article's
HTML, parses it in a **detached `DOMParser` document** (runs no scripts, loads no
resources — it only reads `src` attributes), and warms up to
`PREFETCH_MAX_IMAGES` images via `new Image()`.

Warming is restricted by `isWarmableImagePath` to the two endpoints article
images are actually rewritten to: `/api/img` (`public, max-age=86400`) and
`/starred-asset/` (a year, immutable). Same-origin alone is too loose — a feed's
broken *relative* `src` resolves against our own origin and would be prefetched
into a 404, and other same-origin assets (a `/static/` placeholder) are already
cached or not worth a request. These two are also the only paths with a real
`max-age`, which is the entire reason prefetching works here when prefetching the
page does not. The cap exists because a lesson-length article can carry 50+
images. The prefetch is hung off
the **settle** (plus `PREFETCH_DELAY_MS`) rather than a fixed delay from load, so
warming the next article never competes with rendering the one being read — on a
slow load settling can itself take seconds, and a fixed timer would fire straight
into it. Failures are swallowed: a prefetch must never disturb reading.

**This is only safe because rendering no longer marks read.** Fetching the next
article's HTML to discover its images would otherwise have marked it read without
it ever being seen — the two changes are ordered, not independent.

**Two scopes** (`?scope=`). `saved` (default, above) reads the starred backlog
with the Archive axis. `feeds` is ordinary **unread feed reading**:
`_build_feeds_mode_context` renders a simplified feeds tree (All Feeds + folders
with unread counts) → `list_entries_for_feeds(star_only=False, read_filter=
'unread')` → the same paginated reader, minus the Archive/Delete controls (marked
read on open). Entry points are additive (the feeds three-pane app is unchanged):
an app-menu **Read Mode (e-ink)** link, and a **Supernote auto-detect** — a
tablet whose UA contains `supernote` hitting `/` is redirected to
`/read?scope=feeds`; the Read Mode exit link (`/?full=1`) opts back into the full
app and sets a `lectio_full_app` cookie so in-app navigation isn't re-redirected.

## Offline reading and offline acting (`static/sw.js`, `static/outbox.js`)

The Supernote's browser has **no download handler at all** — no `<a download>`,
no long-press "save link" — so every file-based route to offline reading is
closed. A service worker is the only remaining in-browser option, and the reason
it works where a plain cache cannot is that it intercepts the **navigation**: a
saved hyperlink to `/read?…` still resolves with WiFi off. The worker is served
from `/sw.js` (not `/static/`) because a worker's default scope is its own
directory, and Read Mode lives at `/read`.

Fetch handling is deliberately **network-first**. Lectio is a live app; serving a
stale Inbox to a device that has WiFi would be a worse bug than not working
offline. The cache is a fallback, never the primary. On a miss, `/read` is matched
**exactly** — its query string *is* the article's identity, and an `ignoreSearch`
match there returned the cached browse page for every article, a cache miss that
looked like a successful navigation going nowhere. `ignoreSearch` is for
`/static` assets whose `?v=` moved, and nothing else.

**Precaching is driven by the page, but images are derived by the worker.** The
page sends articles only — the hrefs the list actually renders, never
server-proposed URLs, since the server built those without the active sort and
every cached article sat under a URL nothing navigates to. The worker then
harvests each article's images *out of the bytes it just stored*. This replaced a
server manifest that sliced the node by position and returned images for items
`[offset, offset+n)`: because the article set was chosen from the DOM and the
image set by index, the two could disagree, and articles went offline with no
pictures. Deriving images from the stored article makes that mismatch
unrepresentable, and it deleted `GET /read/offline/manifest`, which had been
rendering every candidate article through BeautifulSoup to guess. The harvest
regex must undo `&amp;` — src attributes are HTML-escaped and `/api/img` URLs
carry several parameters, so the escaped form caches a response nothing ever
requests again: a counted success with the image still missing.

**"Save 20 more" asks the cache, not a counter.** The cursor was a per-node
`localStorage` offset — press once for items 1–20, again for 21–40. When new
articles arrive at the top between presses the list shifts underneath it, so some
are re-saved and some are skipped and never offered again: rare in a backlog
folder, routine in the Inbox, where new stars landing at the top is the entire
point of the Inbox. The page now asks the worker which hrefs it already holds
(`{type:"cached"}`) and takes the first 20 that are missing. If the worker does
not answer it falls back to "nothing is cached" — which degrades to re-saving
rather than to skipping, because re-saving costs bandwidth and skipping costs an
article you thought you had.

**Acting offline: an outbox, not a retry.** Archive, Delete, tagging and
mark-read were ordinary POSTs, so with no connection they failed and the tap was
gone. `static/outbox.js` persists each one to **IndexedDB** — not the Cache API,
because these are mutations and must survive the browser being killed, which on
that device is how reading sessions normally end.

Three decisions carry the design:

- **Enqueue first, then send** — even when online. The failure that actually
  loses work is not "offline", which the page can see, but the connection dying
  *mid-POST*; and every Read Mode action is immediately followed by a navigation,
  which cancels the request in flight. Post-first-then-queue-on-failure never
  learns about those. This is affordable only because the four target routes are
  **idempotent set-state operations** (`archived=0/1`, discard, the full tag set
  with `append_mode=0`, `read=1`), so a record retried after a half-finished
  flush is a no-op. That is also why there is no `synced_actions` dedupe table:
  it would buy a schema change plus a per-user migration for no behavior change.
- **The caller awaits the durable write, not the send.** An IndexedDB transaction
  still open at unload is aborted, so navigating without waiting would lose the
  very record that exists to stop the action being lost. The flush is left
  dangling on purpose; if the navigation kills it, the next page load picks it up.
- **403 is never dropped.** Replay drops a record on 2xx or a definitive 4xx
  (400/404/409/410/422 — the entry is gone, or the request can never apply, and
  retrying forever would wedge every later action behind it). A 403 here is an
  expired session or a CSRF token from a page cached hours ago, neither of which
  is a verdict on the action.

Replay is serial and oldest-first, stopping at the first record that neither
succeeded nor died definitively — pushing past it would reorder the rest, and an
archive-then-unarchive replayed backwards brings the item back. Triggers are page
load and the `online` event, **plus** Background Sync where it exists; Sync cannot
be the only path, because the device this is for runs Chrome 96 in an Android
WebView, where it is absent. The same file runs in both places (every DOM touch
is guarded, and `sw.js` pulls it in with `importScripts`), so there is one
implementation rather than two that drift. The CSRF token is captured **at enqueue
time** and stored on the record: a worker replaying under Background Sync has no
document, and therefore no `<meta name=csrf-token>` to read.

Tagging is the deliberate exception — it posts directly and queues only on
failure, because it is the one action whose **reply** matters (the server
normalizes the name and enforces the cap, and the panel re-renders from what came
back), and nothing navigates away, so there is no cancellation race to protect
against.

Pending depth renders into any `[data-outbox-depth]` element — the Read Mode
footer and the reader's control bar. A queue nobody can see is how work gets lost
without anyone noticing, and on a device that is offline by default that is not a
rare case.

**Conflict rule: last-writer-wins, and the device loses ties it cannot see.** A
replayed action overwrites whatever the server now holds. Detecting "the server
already moved on" would need per-entry modification times the schema does not
carry, and the alternative — a merge dialogue on an e-ink screen — is worse than
the loss it prevents. Discarded actions are logged so a surprising result is at
least explicable.

## Floated images, and why margins are not kept

The style allowlist (`_ALLOWED_STYLE_VALUES`) keeps `float` and `clear` alongside
the typographic properties. Blogger emits `clear: right; float: right` on every
side-set image, and dropping it turned a post written *around* a right-set cover
into a centred block with all the text pushed below — a visibly different
article, and the way it was reported (2026-08-02).

Floats are admitted where `position`/`z-index`/`width` are not, and the
distinction is not arbitrary: a float stays inside its container, so it cannot
overlay the app's own UI or escape the pane. It is prose layout.

The author's accompanying `margin-*` is **deliberately dropped**. Margins are
free-form lengths, so keeping them would mean matching a value *pattern* rather
than a literal — the one thing this table promises never to do, and the property
that makes it auditable. The gutter comes from `static/style.css` and
`static/reader.css` instead, keyed off the sanitizer's **normalized** output.
That is why the spacing inside `"float: right"` is load-bearing: the stylesheets
select on `[style*="float: right"]`, and an emitter that ever wrote `float:right`
would silently stop matching every rule with nothing failing.

**The narrow-screen override needs `!important`, and this is not stylistic.** The
float survives as an *inline* style — that is how the sanitizer preserves it —
and an inline declaration outranks any stylesheet rule. Without `!important` the
`max-width: 620px` block is inert and a 45%-wide image stays floated on a phone.
It shipped that way and was caught in a browser at 390px, not by a test; the test
now asserts the `!important` specifically, because a plain `float: none` passes a
naive check while doing nothing.

Read Mode already floated WordPress `alignleft`/`alignright` for the same reason,
so the inline-style selectors were folded into those existing rules rather than
added beside them.

**Preserving the float in the sanitizer was only half of it.** The FIRST image in
a body is also what `_strip_lead_image_opener` hoists into a full-width hero,
removing it from the flow — so the one image a reader is most likely to be
pointing at was the one still losing its wrap. That function already had the
right rule for images further down (*"an occurrence further down is the author
placing it in the flow, which is content rather than a header"*); a float is that
same placement stated explicitly, and it happens to be at the top. A floated
opener is now left where it is and the separate lead is dropped, or the picture
would appear twice. Only the *article* lead is dropped: the list thumbnail is
resolved on its own path, so the post keeps it. "Don't show the lead image in the
article" still outranks the author's layout — that is an explicit instruction.

**⚠ `lead_image_url` after the dedup is a RENDERING decision, not a fact about
the entry.** Every branch that sets it to `None` means "don't draw this twice" —
the lead is already visible in the body, or the author floated it and it stays in
the flow. `get_entry_detail` used to persist that `None` into `entry_lead_images`,
which is where the **list thumbnail** reads from, so an article opened after the
floated-opener change recorded itself as imageless and lost its thumb. 130 live
entries before it was caught. The resolved value is now captured *before* the
dedup (`_resolved_lead_for_cache`) and persisted instead. Anything added to that
function has the same trap.

## Titles render a tiny inline allowlist

**Titles render a tiny inline allowlist, by escape-then-restore.** Feeds put `<em>`
in titles — and they also put `std::vector<T>` and `#include <chrono>` in them.
Measured on the live library: of 21 titles containing angle brackets, only 6 are
markup; 11 are C++ generics and the rest are header names. **Treating titles as
HTML would silently delete those**, mangling a C++ post title, which is a worse
failure than showing `<em>` literally.

`sanitize_inline_title` escapes the whole string and then restores only bare tags
from a short list (`em`, `i`, `strong`, `b`, `code`, `sub`, `sup`, …). That ordering
is what makes it provably safe: an attribute or an unknown tag cannot survive a
round trip through escaping, so `<em onmouseover=…>` and `<img onerror=…>` stay
escaped text with no allowlist to argue about.

Records carry both forms. `title_html` is used where the title is visible text (post
rows, the entry-pane headline, Read Mode rows and the reader headline);
`title_plain` — the same string with those tags stripped — is used everywhere that
cannot render markup: `title=` attributes, `<title>`, exports, email.

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

**The panel is usually marked on its CONTAINER, not on the `<img>`** — a lesson that cost three feeds. Matching only the img's own id/class meant `<div id="comic">` was invisible, so pbfcomics.com (image carries just `class="lazyload"`) resolved to the 79×30 "Home" nav button on every entry, and mahonoir.com to a 1200×630 OG social card. `_WEBCOMIC_CONTAINER_OPEN_RE` now selects a wrapper first, and **inside such a container the first acceptable image wins with no id/class test on the img at all**: the container is the evidence. Its tokens are an explicit list rather than a fuzzy `comic` substring, so `comic-nav` (previous/next buttons) and PBF's `comic_categories-comic` post class do not match.

The counterpart is `_WEBCOMIC_CHROME_OPEN_RE`, which drops nav/menu/widget/sidebar/gallery containers before either scan. This exists because **`wp-post-image` is a weak signal**: it is WordPress's featured-image class, it was added to `_WEBCOMIC_IMG_CLASS_RE` *for claycomix*, and it also marks PBF's nav items and claycomix's own `pf-summary-widget` sidebar — which was serving *another post's* comic. The class alone is not evidence; where it sits is. The same strip runs in `_extract_webcomic_alt_text`, because a caption has to come from wherever the panel came from — without it, every PBF strip was captioned "Home". Both scans share the balanced-element walker (`_strip_balanced_containers` / `_iter_balanced_containers`), which takes its tag name from the matched text rather than a capture group: these patterns are alternations, and a wrong group index fails silently by stripping nothing.

One site needed more than a pattern. mahonoir.com publishes each page **twice inside one outer `<div id="comic">`** — `#spliced-comic` cut into single panels for phones, first in the document, and `#unspliced-comic` whole. Excluding the spliced container from the match list does not help, because the outer container matches and its first image is the spliced one; the spliced container is removed by the chrome strip instead, so what remains inside `#comic` is the whole page. Finally, an `og:description` equal to `og:site_name` is refused as hover text: PBF ships `og:description="The Perry Bible Fellowship"` on every strip, and using it would caption the whole feed with the site name.

Results are stored in `entry_lead_images (feed_url, entry_id, image_url, image_alt, image_title, fetched_at)`. `image_alt` and `image_title` hold the raw `alt` and `title` HTML attributes from the matching `<img>` tag on the source page, stored separately so the user can choose which to display via the `caption_source` feed preference (`feed_display_prefs.caption_source`: `auto` / `alt` / `title` / `both` / `none`). NULL image_url means "no image found." Negative results are retried after **4 hours** (`_NEGATIVE_RETRY_SECONDS`); positive results are revalidated after 12 hours (`_POSITIVE_REVALIDATE_SECONDS`). An existing non-NULL URL is never overwritten with NULL during revalidation. Likewise on first resolution: an `og_scrape`-**manual** feed stores the inline feed image and then falls through to the authoritative source-page fetch, but a transient source miss must not clobber that inline image with NULL — otherwise a brand-new post (whose `og:image` isn't generated yet at first fetch) loses its thumbnail until the 4-hour negative retry. The NULL negative is only recorded when there was no inline image either.

First-open availability: when `queue_source_fetch` (the lead-**image** fetch) is called for a new entry, it posts a `threading.Event` keyed by `(feed_url, entry_id)`. The entry render path calls `wait_for_source_fetch(..., timeout=0.8)` immediately after queuing so the lead image — which the user sees right away — fills on the very first open for fast sites, capped low enough that slow hosts (Squarespace, WordPress.com) fall through and fill on the next open instead.

The **caption** source-HTML fetch (`queue_source_html_fetch` → `fetch_entry_image_caption`) is, by contrast, fully asynchronous: when the source HTML isn't already cached, the render queues the background fetch (which both primes the HTML cache and persists the alt/title to `entry_lead_images`) and returns immediately — it does **not** call `wait_for_source_html_fetch`. The caption appears on the next open from the persisted value. This was previously a `wait_for_source_html_fetch(..., timeout=3.0)` blocking call, which stalled first-open by up to 3s on og_scrape feeds (e.g. mynorthwest) purely to maybe show a caption that gets persisted for next time anyway; removing the wait is the cache-first/defer fix (the lead image still uses the brief 0.8s wait above since it's the user-visible payload). The narrower `inject_source_images` gallery path keeps a 0.8s `wait_for_source_html_fetch` since it's gated on an opt-in per-feed pref.

**The gallery ranks nothing, so it needs its own filters.** `extract_source_gallery_urls` collects *every* acceptable image in document order rather than picking a winner, which means the lead-image scorer's defences don't apply to it. Two consequences were live on tinyview, whose comic post injected 14 images — 5 real panels, 5 chrome, 4 broken:

- **Plugin verdicts now apply here too.** `LeadImagePlugin.source_score_adjustment` only ever fed the scorer, so `TinyviewPlugin`'s −200 for `assets.tinyview.com` (skeleton animation, wordmark, icons8 buttons) demoted those URLs for the lead image while leaving them first-class gallery entries. Anything scored at or below `_PLUGIN_CHROME_SCORE` is now skipped — a plugin scoring that low is calling a URL chrome, not merely ranking it lower.
- **Duplicate filenames collapse** (`_drop_duplicate_basenames`). A server-rendered app often emits an image twice, at its real location and at a fallback path that 404s; tinyview ships each panel as both `/<comic>/<yyyy>/<mm>/<dd>/<slug>/IMG_*.jpeg` (200) and `/<comic>/IMG_*.jpeg` (404). Which is real can't be known without fetching, but it can be inferred — prefer the URL whose path carries the entry's own slug, since that is the copy filed under this post. Falls back to first-seen order, so sites without the pattern are untouched.

**A plugin verdict is only as good as the paths that honor it.**
`should_skip_source_lookup` was consulted on 3 of the 12 `_fetch_source_lead_image`
call sites, and the storing paths in `fetch_and_store_lead_images_for_feed` were
not among them — so a plugin-owned host could resolve correctly on the render
path and then be overwritten by a background revalidation that scraped the page
anyway. Webtoons episodes went back to the series thumbnail hours after the
plugin was fixed, which is how this surfaced. Storing paths now go through
`_plugin_or_source_lead_image`: the plugin's own answer wins, a plugin that
forbids scraping gets no scrape, and a forbidding plugin with no answer yields
NULL rather than a scraped one. The old code stored NULL outright for any
skip-source host, which blanked panels the plugin could have named.

**Name heuristics must not run against machine-generated filenames.**
`_AD_URL_PATTERNS`'s `[-_]ad[0-9]` exists for `Cert-ad1.png` and cannot tell it
from `-ad27-` inside `ff52deff-c6a8-448d-ad27-a3c3d14c719c.jpg` — two Tapas
panels were rejected as ad slots that way. The wixmp host-trust a few lines
above was added for exactly this class ("ad87 in a UUID") one host at a time;
`_UUID_BASENAME_RE` generalizes it. Only the *filename* half is exempted: the
pattern does two jobs, and `/ads/` still names a directory, so a UUID sitting in
an ads directory is still an ad. Path-, host- and dimension-based checks are
untouched.

**A plugin that suppresses everything is a claim about the feed, not just the page.** `WebtoonsPlugin` skipped source scraping *and* returned no fallback, on the stated belief that "the feed and og:image return the series thumbnail for every episode". Only half was true. An episode page's og:image really is the series thumbnail — byte-identical across episodes — but every Webtoons `<description>` carries that episode's own panel on the same CDN, distinct per episode on all nine subscribed feeds. The result was the series thumbnail on every strip, served from cache rows written before the plugin existed, while the real panel sat unused in the entry body. The plugin now reads the body like `BloggerPlugin` does, keeps `should_skip_source_lookup`, and narrows `should_bypass_cached_url` to URLs whose basename is `thumbnail.*` — so stale thumbnails are re-resolved and a good cached panel is left alone instead of being re-derived on every render.

**Webtoons is the same trade, and the hotlink gate is dodged rather than
forged.** An episode is a vertical strip cut into slices — 50 on a Backchannel
chapter, 8 on MercWorks, 5 on False Knees — and the feed carries exactly one
image. The slices are on the page as `<img class="_images" data-url=…>`; the
class is what bounds them, because the page also embeds a recommendation strip
of other series and a looser scan swept 62 URLs into an episode that has 50.

The page serves them from `webtoon-phinf`, which answers **403 to any request
without a `webtoons.com` Referer** — including a browser loading the image off a
Lectio page. The sibling `swebtoon-phinf` host serves the same paths with no
Referer at all, and is the host the RSS feed itself uses, so
`_webtoons_public_slice_url` rewrites to it and drops the `?type=` resize.
Verified 200 on every slice of three series. That is the difference between
routing around a gate and forging a header we do not have; it also means no
image-proxy change.

**The feed's image and the article's image are different questions, and on
Tapas they have different answers.** `/sa/` is *series art* — one picture per
episode, thumbnail-grade, and all the RSS feed carries. `/c/` is the episode's
actual content, one URL per panel, so a four-panel episode arrives in the feed
as a single image. `_inject_tapas_episode_panels` fetches the episode page and
puts the `/c/` panels in the body, strips the `/sa/` picture out of it, and
drops the separate hero — otherwise the thumbnail renders above the comic it is
a thumbnail of. The **list** thumbnail is untouched, because the caller captured
the resolved lead in `_resolved_lead_for_cache` before the body rewrite; that
split is what lets one entry have a thumbnail-grade image in the list and the
real comic in the article.

The `/c/` URLs are signed and short-lived (`?__token__=exp=…~acl=…`), which
drives two decisions. The page is **re-read** rather than the URLs stored, since
a stored URL is dead within the hour; and `__token__` joins
`_IMG_CACHE_VOLATILE_PARAMS`, so `/api/img` caches the bytes under a
token-stripped key and keeps answering after every token expires. The page fetch
is synchronous on the render path — narrow enough to afford (Tapas links only,
and only when the body has no `/c/` image already) and *not* deferrable the way
a caption is, because deferring would show the wrong picture now.

**Tapas is the same shape on a different host, and needed its own plugin.** An
episode page's og:image is a social *card* (`.png` on `us-a.tapas.io`) while the
panel is a `.jpg` on the same CDN inside `<content:encoded>`. The card is
distinct per episode, so unlike Webtoons nothing in the URL says which is which
— `TapasPlugin` therefore takes the body image and treats a non-`.jpg` cached
value as the card worth re-resolving.

The strategy comparison cache (`feed_strategy_cache`) also stores `image_alt` and `image_title` per strategy so the Tuning tab can display them below each card without a live fetch.

SmartCrop's `min_scale` is a per-feed preference (`feed_display_prefs.smart_min_scale`, NULL = default 0.9), set in Feed Properties next to the thumb fit mode and passed to the `/thumb` proxy as the `ms` query param; it was previously a global app setting. The min_scale is part of the thumb cache key, so changing it regenerates that feed's Smart thumbnails.

Fill mode's `fill_zoom` multiplier (`feed_display_prefs.fill_zoom`, NULL = default 1.0, range 0.5–2.0) scales the cover-crop resize step before the anchor-crop. Values below 1.0 produce a letterbox (image pasted on a black canvas); values above 1.0 crop more aggressively than the default tight fill. Passed to `/thumb` as the `fz` query param and included in the cache key for cover-family modes.

**Direct-load fallback:** `/thumb` fetches the source image *from the server*, so a host that IP-blocks datacenter traffic (e.g. Cloudflare 403, washingtonstatestandard.com) makes `/thumb` 502 and the list thumbnail break — even though the browser's own (residential) IP can fetch the image fine. The list `<img>` carries the raw image URL in `data-direct`; on a `/thumb` error its `onerror` (`window.thumbImgFallback`, defined pre-body so it exists before any load fails) retries once with that direct URL, letting the browser load the image itself. CSS `object-fit:cover` sizes the un-resized image to the tile. This recovers the thumbnail without evading the block server-side (it's the user's own client fetching, exactly as the article view already does). Only `http(s)` direct URLs are retried, and only once (a `data-triedDirect` guard prevents an error loop); if the direct load also fails, the tile collapses to `is-empty` as before. The same helper backs the JS-derived list thumbnail (it sets `data-direct` to the lead-image URL).

## A kept post has to say its feed is gone

Once curation outlives its feed, the feed name beside a saved post becomes a
half-truth: it names something you can no longer read. Two different states get
there and both look identical on screen — a **kept feed** still exists in reader
but is hidden from the tree, and an **orphan archive**'s feed is gone from reader
entirely. What they share is the only thing that matters at the point of use:
there is no subscription behind the name, so clicking through will not show you
more of it. `unsubscribed_feed_urls_among` treats them the same and the name
renders `Feed Name (unsubscribed)`.

The test is *not* "has no folder row". A feed in no folder is still subscribed —
it lives under the virtual Uncategorized folder — and marking those would be
wrong. The test is membership of `get_all_reader_feed_urls(include_kept=False)`,
which excludes both states for exactly this reason.

Scoped to the feeds actually on screen rather than computed library-wide, so the
template context carries a handful of URLs instead of a set of thousands on every
render.

Rendered as a separate muted `<span>`, never concatenated into the title string:
the feed name is data that appears in search, exports, feed properties and the
tree, and baking a status word into it would leak everywhere the name goes.

## Unsubscribing has to be able to actually remove things

The keep model has a corollary that only shows up at removal time: a star or a
tag **preserves the offline capture**, so once the previous section made tags
survive an unsubscribe, *every* exit from the dialog left the posts behind in
Saved as orphan archives. That is right when the feed was worth reading and
wrong when the feed itself was the mistake — and the only way out was to untag
and unstar every entry by hand first.

`drop_all_curation` is the fourth choice. Order is load-bearing:

1. **Tags come off first.** `apply_star_state(False)` consults
   `entry_has_keep_signal`, so unstarring while a tag remains would (correctly)
   refuse to release the capture.
2. **Then the star.**
3. **Then the archive, synchronously.** `enqueue_removal` only marks the row
   `pending_removal` for a background worker, and the feed is about to
   disappear — so `delete_archive` runs the cascade immediately instead, keeping
   assets another entry still references.

`archived_entries` (the Archive *state*, not the capture) needs no handling:
with every capture gone, `_purge_dead_entry_meta` treats the whole feed as
uncaptured and drops all of its per-entry meta in one pass.

Two guards in the UI, because this is the only irreversible choice in the dialog
— it deletes the offline copies, so there is nothing left to undo it from. It
carries a `confirm()` naming the number of posts, and the re-star checkbox is
disabled and cleared while it is selected, since "bring these to the top of the
Inbox" and "delete these" contradict each other.

## A tag has to survive an unsubscribe, because a star does

Stars and tags are the same promise — "keep this" — but they are stored in
completely different places. A star is a `saved_entries` row in the meta DB with
no reader dependency, so it survives the feed's deletion untouched. A tag lives
in **reader's own** `entry_tags`, attached to the entry resource, and is deleted
*with the feed*.

So a tagged-but-unstarred entry used to come out of a plain unsubscribe with its
offline capture intact and its tags gone. That is not untidy, it is destructive
one step later: an orphan archive counts as kept only if it is starred **or**
manually tagged (see "A surviving capture is not itself a keep signal"), so
losing the tag makes the entry read as carrying no keep signal at all —
unreachable in Saved, and eligible for deletion by
`scripts/purge_uncurated_orphan_archives.py`. The dialog said so out loud
("tags are lost"), which is how it was noticed.

`_carry_tags_to_orphan_archive` copies the tags into `orphan_entry_tags` before
the delete. That is not a new concept: it is the table the UI already writes when
you tag an entry whose feed is gone, precisely because there is no reader
resource to attach to. The tags simply move to where an orphan's tags are meant
to live.

Two gates:

- **Only for entries whose capture survives** — the same test
  `_purge_dead_entry_meta` uses, so both agree about what still exists
  afterwards. With no archive row the entry is gone entirely and a tag row would
  point at nothing.
- **Not when `migrate_curation_to` is set** — there the tags follow the entries
  onto the surviving feed instead, which is `_migrate_curation`'s job.

`orphan_entry_tags` is deliberately absent from `_DEAD_ENTRY_META_TABLES`, so the
meta sweep that runs immediately afterwards does not undo this.

## "Re-star" is two operations, not one

Unsubscribing a feed can bring its curated posts back to the top of the Inbox.
The Inbox orders by `saved_entries.saved_at`, so an item curated months ago sinks
to wherever it was — which is the last place you look right after deciding to
drop its feed.

The obvious implementation is "set saved_at to now", and it is wrong for half the
items. `restar_curated_entries` does two different things:

- **Tagged but unstarred** — there is no `saved_entries` row to update, so a date
  bump touches nothing and the item still never appears. It has to be *genuinely
  starred*, which is also what enqueues its offline capture. This is the common
  case, and it follows from the keep model: a tag is a keep signal, but only a
  star is archived.
- **Already starred** — `apply_star_state` is `INSERT OR IGNORE`, so calling it
  again silently leaves the old timestamp and nothing moves. That row is
  re-stamped directly.

Deliberately **not** unstar-then-star for the second case. Unstarring an entry
with no other keep signal enqueues removal of its offline capture
(`entry_has_keep_signal`), so a failure between the two calls would destroy
exactly what the option exists to preserve. There is no "re-star" primitive —
only star and unstar, and star is idempotent — which is why this is a named
operation rather than two calls at the call site.

It runs **before** the feed is removed: the entries must still be readable, and
the capture it enqueues is flushed by the force-archive that both the keep and
purge branches already perform. Off by default, and the checkbox is reset every
time the dialog opens — the modal is reused, so a box ticked for the last feed
would silently reorder the Inbox for the next one.

## Removing a feed has to clear the record that it was failing

`purge_orphaned_feed` cleaned `kept_feeds`, `feeds_needing_replacement` and
`folder_feeds`, but never `feed_failure_state`. So a feed unsubscribed *because*
it was dead stayed on the failure record permanently, and Failing Feeds plus the
"dead — needs replacement" triage went on counting subscriptions that no longer
existed — with nothing left to fix or remove. The 404 sweep on 2026-08-11/12
made it obvious by creating 560 of them at once.

Safe to drop unconditionally: a failure record is derived state rebuilt on the
next fetch, and a feed with no reader row has no next fetch. A feed that is later
re-added starts clean, which is the right reading of a deliberate re-subscribe.
`scripts/clear_ghost_failure_state.py` clears the pre-existing backlog (562 rows
on the live library).

**Ghost is defined against reader, not against folders.** Unsubscribe-with-keep
deliberately leaves a feed with no `folder_feeds` row while it still exists and
stays reachable through the Kept view, so keying the sweep on folder membership
would delete the failure record of feeds that are still real.

Other feed-keyed tables still outlive their feeds — `feed_lead_image_strategy`
(1,024 ghost feeds), `feed_fetch_history` (754), `feed_seen_window` (553),
`feed_media_scan` (173), `fever_feed_map` (154), `browser_ua_feeds` (95),
`websub_subscriptions` (74). They are deliberately left alone for now: they are
untidy rather than wrong, and some are not obviously safe to drop (the API id
maps are handed out to sync clients, and fetch history is history). Failure state
was the one with a visible, misleading consequence.

## An embed URL wearing an anchor is still an embed

Feeds that lose their oEmbed iframe usually ship the video as a `watch?v=`,
`youtu.be/` or `/shorts/` link, and `_embed_standalone_youtube_links` rebuilds a
player from any of those when the link is a paragraph's sole content.
sonarsource.com ships a fourth shape: the **`/embed/` URL itself**, as a plain
`<a href>` with descriptive text —

```html
<p><a href="https://www.youtube.com/embed/<id>?si=…">Escape from AppleScript</a></p>
```

That is an embed source that happens to be wearing an anchor. It read as "the
video is on the web post but not in the article", which is precisely what this
function exists to undo, and it missed in **two** places at once: the URL matcher
(`_YT_WATCH_URL_RE`) listed only the three link shapes, and `extract_video_id`
could not name a video from an `/embed/` URL either — so even code that reached
past the matcher had nothing to build a player from. Both now accept `/embed/`,
and `youtube-nocookie.com` alongside it, since it is the same URL shape from the
privacy host.

There was a vestigial `if "/embed/" in content_html: pass` at the top of the
function, anticipating this case and doing nothing about it. Removed rather than
left to imply a check that was not happening.

The paragraph-sole rule is unchanged and still the whole scope guard: a link
inside a sentence stays a link. The anchor's *text* was never required to be a
bare URL — the test is whether the anchor is its container's only content — so a
worded link like this one already qualified once the URL shape was recognized.

## A body image that fails has to be able to try again

The article's hero image has always carried an `onerror` that swaps its `src`
for `/api/img?u=…` and only gives up if the proxy fails too. Body images carried
nothing: a failed load left blank space, no second attempt, and no way to tell a
blocked image from one the publisher never shipped.

That asymmetry stays invisible for as long as a post has both a hero and body
images, because the hero is the one people look at. It surfaces when a post's
`og:image` **is** its body image. `_strip_lead_image_opener` then correctly drops
the separate hero — showing the same picture twice above and inside the article
is worse than showing it once — and the only copy left is the body copy, the one
with no fallback. That is why sonarsource.com's blog read as "the posts before
and after this one show their image and this one doesn't": the neighbours were
rendering a hero, this post was rendering a body image.

`add_img_proxy_fallback` closes it, running alongside `add_no_referrer_to_images`
on the entry-pane path. Three properties matter:

- **The direct URL stays the first attempt.** This is a retry, not a rewrite.
  Preemptively routing every body image through the proxy is a different (and
  much larger) change — `proxy_hotlink_images` already does that deliberately,
  for the named hosts in `_HOTLINK_IMG_HOSTS`, where a direct load is *known* to
  fail.
- **It costs nothing on the happy path.** `onerror` never fires for an image
  that loads.
- **It only adds a handler where there is none.** The sanitizer strips
  author-supplied event handlers, so in practice that is every image, but the
  guard means running the pass twice is a no-op rather than a nest of handlers.

Because the retry is same-origin and the server-side fetch carries no `Referer`,
it recovers the same three failures the hero's copy always did: a host that
refuses cross-origin image loads, a URL a client-side blocker drops, and a signed
URL that expired between storage and reading.

## Choosing a lead image: what gets thrown away, and what sneaks through

The selector is a pile of heuristics, and its failures come in two opposite
shapes. Both were live on 2026-08-12 and each one silently produced a *plausible
wrong answer*, which is why they went unnoticed.

**Site chrome that was being kept.** blogs.windows.com made its site icon the
article image. Both available signals missed it: the alt text said `Site Icon`
but `_LOGO_URL_PATTERNS` only allowed `-`/`_` between the words — and that
pattern is matched against alt/title *text* as well as URLs, where words are
separated by spaces. The file was `Windows11Icon.png`, CamelCase, so the
`[-_]icon.png` rule needed a separator that never existed. Separators are now
`[-_\s]`, and the icon-filename rule is separator-optional behind a lookbehind so
`emoticon.png` and `lexicon.png` stay safe.

**Real art that was being thrown away.** Two of these:

- *A title that contains a hint word.* `round` is a shape hint for a cropped
  avatar (`avatar-round.png`) and was matched against the whole path, so Standard
  Ebooks' **"The Third Round"** lost its cover — a perfectly good 1400×2100 JPEG
  — because the word appeared in a *directory segment that is a book title*. The
  hint now applies to the filename only. Same class of false positive the
  `profile` guard in that pattern already documents for DeviantArt.
- *A file honestly named "fallback".* Full Circle Magazine's genuine podcast
  cover art is `covers/podcasts/fallback.webp` — the art used when an episode has
  none of its own — and the placeholder rule reads the name. Declared dimensions
  now override it, since a page sizing an image at 640×360 is asserting intent.
  ⚠ The bar is **hero scale (400×200), not the ordinary minimums (200×100)**:
  WordPress's `blank.jpg` is a 200×200 white box sitting exactly on the floor, so
  an at-or-above rule readmits the canonical placeholder. An existing regression
  test caught that immediately.

**A negative is cached, so a wrong rejection is sticky.** Each of these stored
`image_url = NULL` and stopped re-resolving, so fixing the rule is only half the
job — the poisoned rows have to be cleared for the entries to recover.

## A comic's prev/next arrows are not its lead image

`main.py` has known these as body chrome for a long time (`_COMIC_NAV_ALT_RE`,
`_COMIC_NAV_SRC_RE`) and strips them from the article. Nothing stopped one being
chosen as the **lead**: dresdencodak's feed opens with

```html
<img alt="Previous" height="30" src=".../prev_002.png">
```

so the 30px arrow won the first-image bonus and became both the hero and the
thumbnail.

The match is anchored to a basename that is *only* the nav word plus an optional
number, because these are ordinary English words: `first-contact.jpg` and
`next-door.png` are comics, `prev_002.png` and `next.gif` are buttons. `main`'s
looser `src` pattern can afford the ambiguity because it only fires alongside
other nav signals; this one stands alone, so it cannot.

## An `<img>` inside a `<script>` is source code, not an image

monstersoupcomic's bookmark widget does

```js
document.write('<a …><img src="'+imgTag+'" …>')
```

and the page scan dutifully produced the lead image
`https://monstersoupcomic.com/'+imgTag+'` — a URL that cannot resolve to
anything. `<script>` blocks are now stripped once in `_fetch_page_html`, rather
than at each of the ten `_IMG_TAG_RE` scan sites, so no future scan can forget.
Safe there because this class reads no JSON-LD (which also lives in `<script>`),
and `og:`/`meta` tags are in `<head>` and untouched.

## An age gate is the one image that is definitely not the post

An adult webcomic serves a content-warning interstitial *instead of* the strip,
so a page scrape picks it up exactly where the comic should be — and on a
webcomic feed whose body ships no image, `_inject_webcomic_panel_into_bodyless_entry`
then puts it in the article. monstersoupcomic.com illustrated both halves at once:
a post about paintbrushes rendered `maturecontentwarning.png`.

Unlike most site chrome this is not a logo or a widget, so none of the existing
rules saw it. It is now matched in `_SITE_CHROME_PATH_PATTERNS`, separator-optional
because these files are named every way going (`maturecontentwarning`,
`mature-content-warning`, `age_gate`, `nsfw-warning`).

The words themselves appear in real comic titles, so the patterns match the
*gate* shapes only: `the-warning-sign-chapter-4.jpg` and
`mature-audiences-episode.jpg` still pass, and a test pins that.

Removing the gate exposed what it had been masking: the same feed's text posts
then resolved to `/images/blog_on.png`, a 99x44 nav button. `<name>_on.png` /
`<name>_off.png` is the rollover convention for a button's two states, and a
site that still writes its menu that way carries no other markup saying so. The
size floor cannot catch it either — the dimensions are neither in the URL nor
declared on the tag, so nothing measures them without fetching the bytes. That
shape is rejected too, anchored to the whole basename so `lights-on.jpg` and
`switched_on_and_off_again.png` are untouched.

## A caption that never changes is the site's, not the post's

Webcomic caption extraction falls back to `og:description`, because that is
where a lot of comics put the hover-text punchline. Plenty of sites put a fixed
blurb there instead. `_extract_webcomic_alt_text` already rejects one that merely
repeats `og:site_name` (pbfcomics ships `og:description="The Perry Bible
Fellowship"` on every strip), but Penny Arcade defeats that twice over: it ships
no `og:site_name` at all, and its description is a real sentence —

> Videogaming-related online strip by Mike Krahulik and Jerry Holkins. Includes
> news and commentary.

Nothing *within* one page marks that as boilerplate. What marks it is that it
does not vary: a punchline belongs to its strip, a tagline belongs to the site.
So the test is across the feed rather than within the page — if another entry
already carries this exact caption, neither of them is a caption.

Self-healing rather than perfect. The first entry cannot know, so it stores the
text; the second recognises the repeat, and `_clear_feed_boilerplate_title` drops
it from every row of that feed at once (and from the in-memory title cache, which
would otherwise keep serving it until a restart). Worst case is a missing caption
on one post instead of a wrong one on every post.

Scoped to a single feed on purpose: two different sites may legitimately share a
sentence, and `image_alt` is never touched — only the title, which is the field
that fell back to `og:description` in the first place.

## A webcomic wants a different image in the list than in the article

Penny Arcade strips are ~1050×438 — three panels side by side, which is three
unreadable smudges at thumbnail size, while panel 1 is legible. The two are
derivable from each other (`…/comics/<hash>.jpg` ↔
`…/comics/panels/<hash>-p1.jpg`), so the `thumbnail_from_lead_image` plugin hook
returns a thumbnail crop from the already-resolved lead image with no extra
fetch, safe on the render path.

Getting that right needed **three** places to agree, and fixing the first two was
not enough:

1. `should_skip_source_lookup` — without it the page scan takes the first `<img>`
   (panel 1) and stores it as the article image, beating the plugin's own
   og:image fallback. The plugin's docstring described behaviour it never reached.
2. `get_cached_entry_thumbnail` — the panel-bypass returned `None` rather than
   falling back, so a cached panel produced *no thumbnail at all*.
3. `_inject_webcomic_panel_into_bodyless_entry` — re-scanned the source page **at
   render time** and injected panel 1, discarding the strip that had just been
   resolved. It now honours the same `should_skip_source_lookup` answer the
   storing path uses, so a plugin-owned host is never re-scanned; hosts with no
   plugin opinion still scan, which is the case that injection was written for
   (mahonoir's enclosure is a share card, so the page really is the only source).

## Image bytes: the dimension cap is not a size cap

`/api/img` downscales a cached image to `LECTIO_IMG_CACHE_MAX_DIM` (3840) on the
longest side. That says nothing about how many bytes it weighs. A 3840x2160 RGBA
**PNG** is exactly at the cap, so `_maybe_downscale_image` returns it untouched —
at 11.6 MB, shipped whole on every article view. Reported as "this image loads
slowly every time", which it did, because it does.

`_maybe_shrink_oversized_image` adds a byte budget (`LECTIO_IMG_TARGET_BYTES`,
default 1.5 MB, 0 disables). Over it, the image is re-encoded to WebP:

- **Lossless** for small, few-colour images — logos, pixel art, diagrams,
  screenshots — where lossy compression visibly damages hard edges and text.
- **Lossy q85** for everything else, on the reasoning that a large full-colour
  image is photographic or painted, which is the case lossy handles best.

**Both gates were measured, and the obvious implementations are wrong.** Trying
lossless first and falling back cost **12.3s** in the request path; lossless alone
on that image is 6s and still 7.9 MB. Its colour count is 42,082 — low enough
that a naive "few colours means line art" test sends painted artwork down exactly
that slow path. So pixels are checked first (cheap, and pixel count is what makes
the encoder slow) and the colour scan only runs where the answer is both fast and
true. Result on the reported image: **11.6 MB → 0.17 MB in 0.54s**, with line art
verified pixel-identical.

Both re-encoders now run via `run_in_threadpool`. `/api/img` is an async route, so
running a multi-hundred-millisecond bitmap decode inline blocks the event loop and
every other request on the worker queues behind one large image. Both settings are
read *before* entering the pool, so the threaded call is pure CPU with no DB access
and no tenancy context to carry.

The budget is an instance setting (**Administration → Image cache**), with the env
var as its default — the same shape as `LECTIO_IMG_CACHE_MAX_DIM` beside it, and
admin-only for the same reason: it decides how every user's images are stored.

## Transparency: `convert("RGB")` paints line art black

`Image.convert("RGB")` keeps whatever RGB sits *under* the alpha channel, and for
line art that is black — so a transparent PNG becomes a solid black rectangle.
Measured on `what-if.xkcd.com/imgs/a/138`: mean luminance **33** the naive way
against **235** composited onto white. Two paths had it, in two disguises:

- **`/thumb`** called `.convert("RGB")` outright, and now composites onto white
  first. White rather than a theme colour because the output is a JPEG cached and
  shared across users and themes, so the background is chosen once — and this
  kind of image (diagrams, logos, line art) is drawn for a light page. The
  zoom<1 letterbox canvas follows the same rule: white when the source had alpha,
  black for photos.
- **The starred archive** tested `"A" in img.mode`, which is `False` for a
  *palette* PNG (mode `"P"`, transparency in `img.info`) — so precisely the
  images this breaks were the ones it flattened. WebP carries alpha, so capture
  keeps it, and normalization moved ahead of the resize since LANCZOS on a
  palette image resamples palette indices rather than colours.

**Neither fix reaches bytes already stored.** A saved entry's images are rewritten
to `/starred-asset/<hash>` at render time, so the body shows the stored copy, not
the live image — only a re-fetch restores the alpha
(`scripts/repair_flattened_archive_images.py`; 143 what-if assets restored
2026-08-04). Candidates are found from the **WebP header** rather than by
decoding: an asset declaring no alpha whose source format *can* carry it is a
suspect, a 32-byte read instead of decoding 25k images (a decode scan did not
finish in ten minutes). A candidate is only rewritten when the re-fetched source
has a *meaningful* alpha channel — xkcd's book covers have a fully opaque alpha
channel and artwork that is simply dark, and an earlier pass "repaired" those into
byte-identical output and reported a fix that never happened.

Cached thumbnails were already black too, so the fix needed a cache bust:
`_THUMB_RENDER_VERSION` joins the cache key (the same idiom as the existing `_p2`
suffix), so old entries are never looked up again and each thumbnail re-renders on
first view — no mass delete of the 59k-row, 431 MB cache and no refetch storm.

At render time the theme paints behind transparent images via `--img-backdrop`,
white in *both* themes: transparent article images are overwhelmingly black line
art drawn for a light page, and an image that changed appearance when you toggled
the theme would be worse than one that never does. Setting the variable to
`transparent` restores the untouched look with no re-fetch — which is the point of
doing it in CSS rather than in the stored bytes. Deliberately not applied in Read
Mode, where `#reader-columns *` forces `background: transparent !important` for
e-ink contrast and transparent art already lands on white.

## Thumbnails must reuse the image proxy's bytes

`/thumb` fetched its source URL directly and never consulted `img_cache`. For an
image behind a short-lived signed URL that is fatal: the article renders fine
(`/api/img` holds the bytes under a token-stripped key — see
[DeviantArt mature images](#deviantart-mature-images-signed-for-minutes-cached-for-good)) while the thumbnail re-requests the dead URL, gets a 401, and is
recorded in the recently-failed set. The result is a post with a working image and
no thumbnail, permanently, plus a failing fetch on every list render. Found on a
deviation whose token expired two days earlier.

The proxy cache is now consulted first, and — importantly — **before** the
recently-failed short-circuit. Ordering it the other way preserves the bug: the
host *is* failing, which is precisely when the cached bytes are the only way to
get a thumbnail.

**A related way to lose a thumbnail: comparing two references to the same file.**
`GunnerkriggPlugin` derives the panel URL from the entry's `?p=` number and
bypasses any *cached* URL that differs, so a stale site banner cannot win. It
compared strings exactly, and lost twice over — the site serves the panel with a
`?v=<timestamp>` cache-buster, and the derived URL inherits the **entry link's**
scheme, which that feed still publishes as `http://` for an image served over
`https://`. So the plugin declared the very image it derives "not preferred" and
suppressed it. The article still rendered the picture, because that path does not
consult the bypass, which is exactly how it presented: a comic post with an image
and no thumbnail. Comparison is now on host+path (`_same_file_key`); the cached
URL is still *served* untouched, cache-buster and all, since rewriting it is the
ComicControl mistake `_promote_known_thumbnail` documents.

## DeviantArt mature images: signed for minutes, cached for good

DeviantArt serves images from wixmp with a signed JWT in the query string. Ordinary deviations are signed permanently; **mature** ones are signed for about **15 minutes**, and every variant (`content.src` and every thumb) shares the expiry — so there is no long-lived variant to prefer, and a stored URL is normally dead by the time the post is read, showing neither image nor thumbnail.

Nothing scheduled can fix that: a nightly re-sign yields images dead a quarter of an hour later. The re-sign therefore happens **on open** — `_resign_expired_deviantart_images`, run in `get_entry_detail` just before the hotlink-proxy rewrite.

What keeps it cheap is the proxy's byte cache, which was already most of the answer: `wixmp.com` is in `_HOTLINK_IMG_HOSTS`, so these images render through `/api/img`, and `_img_cache_key_url` strips `token`/`sig`/`exp` (`_IMG_CACHE_VOLATILE_PARAMS`) from the cache key. Once the bytes are cached under *any* valid token they answer for every later one. So the re-sign fires only when a token has already expired **and** the cache has no copy — one API call per image over its lifetime, not one per view — and a permanently-signed image (21,564 of 21,568 on the live library) never reaches the API at all. The fresh URL is persisted back onto the entry so the list thumbnail starts from it too.

`scripts/refresh_expired_deviantart_images.py` remains as a manual catch-up over the same routine. Note it must use `get_deviantart_user_token()` rather than reading `deviantart_access_token` directly: DA access tokens last an hour, so any batch reading the stored value 401s on almost every run.

## Saved articles (read-it-later capture)

`services/saved_articles.py` lets users save arbitrary page URLs that come from
no feed. Rather than inventing a parallel store, a saved article is an ordinary
reader entry in a per-user synthetic feed `lectio:saved` ("Saved Articles"),
created lazily on first save with `add_feed(allow_invalid_url=True)` and
`updates_enabled=False` — the scheduler and `update_feeds()` never touch it, and
entries are user-added (`added_by='user'`), which reader guarantees never to
delete during updates. Because it's a real feed in the per-user reader DB,
tenancy, read state, tags, keyboard flows, unread counts, and feed-management
surfaces all apply with zero special-casing (the same reason FakeFeedz uses
`file://` feeds).

Saving (entry id = link = the fragment-stripped URL, published = save time):
the page is fetched and readability-extracted server-side via
`fetch_readability_article` — the same core the reader-view route uses — then
the entry is auto-starred (`saved_entries` row) and enqueued to the starred
archive, whose worker independently captures the source page + images for
offline reading. Extraction failure is deliberately non-fatal: the starred
bookmark is still created (title falls back to the URL) and the archive worker
retries the page later. A duplicate save re-stars the existing entry without
re-fetching. The on-star destination fan-out is deliberately **not** fired —
saving *into* Lectio shouldn't re-send the article to external read-later
services.

**Re-fetch follows the entry, not the feed.** `POST /articles/refresh-content`
re-fetches and re-extracts a capture, replacing its stored content in place. It
has two paths because a capture does not stay in `lectio:saved`: auto-filing
(Settings → Feeds → File saved articles) moves it onto the feed that actually
publishes the article, where it remains a capture (`added_by='user'`, entry id =
source URL) on someone else's feed. For a still-unfiled article the route reuses
`save_article(refresh_content=True)`; for a filed one it calls
`refresh_filed_article`, which updates the entry where it now lives. Routing the
filed case through the save path instead would write into `lectio:saved` and
re-create the duplicate that filing removed — hence the split, and hence
`_replace_entry_content` taking the feed as a parameter rather than assuming the
saved feed. Feed-provided entries are refused: their content belongs to the
publisher and the next refresh would overwrite it anyway.

**Batch re-fetch shares its pacing with the CLI, because a politeness guarantee
that holds in one entry point is not a guarantee.** `services/refetch_batch.py`
owns the delays (`GLOBAL_DELAY`, `PER_HOST_DELAY`, `HOST_FAILURE_LIMIT`), the
host interleave, the runtime estimate and — since a second CLI caller appeared —
`run_paced`, the serial loop that applies all of them and returns a fixed
outcome vocabulary (`ok / archive / mismatch / dead / failed / skipped_host`).
`POST /saved/refetch-scope`, `scripts/refetch_scope.py` and
`scripts/refetch_boilerplate_damage.py` all go through it rather than defining
their own. Bulk re-fetch spends someone else's bandwidth, so pacing is the design
and not a setting; fixing the vocabulary in one place is the same idea applied to
reporting, so two callers cannot count the same result under different names.
`run_paced` takes its clock and jitter as arguments purely so a test can assert
on the delays instead of stubbing them out — pacing that is only tested by not
being tested is not pacing.

Three details are load-bearing:

- **The estimate counts the per-host delay, not the global one.** A single-feed
  scope is a single host, so 89 articles is 89 × 10s, not 89 × 2s. An early
  version reported a 15-minute run as under 4 minutes — for a deliberately slow
  job, the runtime is the one number that must not be understated.
- **One job at a time, per user — but queued, not refused.** Two overlapping runs
  would each honor the pacing and together double the rate every site sees, so
  they are serialized; a second start appends to `job["queue"]` instead of
  returning 409. Serializing is a scheduling constraint, not a reason to make the
  user wait at the keyboard. A queued scope is resolved to entries when it
  *starts*, not when it is queued, because an hour in a queue is long enough for
  what is kept in the scope to change. The worker runs on a background thread
  through `_run_in_user_context`, since a raw thread loses the tenancy user.
- **The worker owns `running`, not the batch loop.** `_run_refetch_batch` handles
  one scope and deliberately does not clear the flag; `_refetch_worker` drains the
  queue and clears it once. Clearing it per batch made the status pill blink out
  between queued scopes, i.e. exactly the invisibility the pill exists to fix.
- **A refusal is not a failure.** The slug guard declining to overwrite a stored
  copy is the guard working, and it is counted apart from a fetch that broke;
  only a real failure counts toward dropping a host.

Progress is visible in a fixed status pill (`#refetch-pill`) driven by
`GET /saved/refetch-scope/status`, which reports the run in flight, the queue
(labels and counts only — not the internals a client might try to set) and the
last few completed runs. The pill is the surface for a job measured in
quarter-hours; a toast fades and takes the job's only visible trace with it. Time
remaining is computed from the job's own measured pace rather than the up-front
estimate, which is the honest number once a few articles are in.

**The mismatch guard has a second reference for opaque URLs.** `article.aspx?p=2438407&WT.rss_a=Classes in C#`
carries one usable subject word once digits and structural vocabulary are dropped
— below the guard's three-word floor — so it stood down entirely and informit's
"Articles | InformIT" section index overwrote two stored articles during a batch
run. When the slug gives nothing to judge by, the *stored* title becomes the
reference, but only in conjunction with `looks_like_a_link_index(new_html)`.
The conjunction is what makes it safe: the slug branch deliberately avoids
old-vs-new titles (re-fetch exists partly to fix a bad title), and a genuine link
roundup — Techdirt's weekly history post is 95% anchor text — still echoes its own
stored title. Thresholds are calibrated against 1,192 captured articles: p95
anchor-text ratio 0.32, p98 0.46, so the detector fires at 0.40 with a 20-anchor
floor. Note the guard protects the *first* destruction only; once an entry holds
the wrong page, its stored title is the wrong page's title.

**A third guard catches the case the other two structurally cannot: the page is
the right article, and readability returned the site's furniture.** Neither
title nor length separates those — the title stays correct and the boilerplate
is often *longer* than the post it replaced (commandlinefu.com's "is the place
to record those command-line gems…"). What does separate them is that chrome
extracts *identically for every post on a feed*, so `extraction_matches_sibling`
refuses an extraction byte-identical to one already stored against a **different
entry of the same feed**. Three properties are deliberate: the fingerprint is
over visible text, not markup, because attribute order and whitespace vary
between runs while the words do not; the comparison is scoped to one feed,
because the same text under two feeds is a syndicated post rather than
furniture; and extractions under 120 characters are exempt, because a two-line
stub can legitimately coincide and refusing those would block real re-fetches.

**The guard remembers what it has allowed this run, because the archive cannot.** `enqueue_archive` is asynchronous, so during a bulk repair `extraction_matches_sibling` could not see the batch's own writes: entry 1 is allowed, entry 2 gets the same wrong text seconds later and is allowed too because entry 1's extraction has not been archived yet. A 368-entry run on 2026-08-07 wrote identical comment-section text to five supernote entries exactly this way. The service now keeps a bounded per-feed map of fingerprints it has allowed (300 per feed, 6h TTL, cleared by `forget_recent_extractions`) and refuses a match against that as well as against the archive. An archive-sourced refusal is deliberately *not* recorded — a maybe should not become a fact.

**Corollary, and the more expensive half: the archive is not evidence of current state.** Anything measuring damage from `archived_entry.readability_html_zlib` reads a store that lags every repair. After that run, 129 of its 131 rewritten entries still had their pre-run extraction stored and still looked damaged; worse, the same lag had inflated the original damage count from the start — of 594 flagged, 327 held perfectly good bodies. `scripts/refetch_boilerplate_damage.py` therefore filters its scope, and verifies its own output, against the READER, which is what a person actually reads. Use the archive to find candidates; use the reader to decide.

`sibling_extraction_entries` is the same test in bulk — "which stored
extractions already are boilerplate?" rather than "would writing this one be?" —
and lives next to the guard rather than in the repair scripts, because two
implementations of one judgement is how a repair script starts disagreeing with
the thing that prevents the damage. Both `scripts/revert_boilerplate_refetches.py`
(restores from a local snapshot, no network) and
`scripts/refetch_boilerplate_damage.py` (re-acquires from the network when there
is no snapshot) call it. In the second, the live guard also becomes the repair's
safety net: a page that still extracts to the same boilerplate is refused, so an
unrecoverable entry keeps what it has rather than being re-damaged.

Scope is kept articles (starred or tagged) with an `http(s)` link — the same rule
the single-article button uses, because an unkept feed entry is rewritten by the
next refresh anyway. Everything the interactive re-fetch does still happens per
entry: the previous body is snapshotted (so any one result is revertible), a
plainly-different page is refused, a refusal falls back to the Wayback Machine,
and a missing publish date is learned on the way.

**Whole-page capture is an opt-in `mode`, on both the save and re-fetch paths.**
`mode="full"` swaps `fetch_readability_article` for `fetch_full_page_article` —
same sanitizer and post-processing tail (`_finalize_article_html`), but the
body-selection step keeps everything instead of scoring it. It exists because
readability doesn't merely under-extract on document-shaped pages, it picks the
*wrong node*: on a DocBook-style export (84 `<p>` across 68 `<div>`, no
`<article>`, 13 `<pre>`) it returns a single shell-session `<pre>` and drops the
prose, because it scores containers by paragraph density.

Reachable from `POST /articles/refresh-content` (UI: **Re-fetch full page**) and
from `POST /articles/save` (UI: the **Capture the whole page** checkbox). Having
it at *save* time matters because the re-fetch form only helps once an entry
exists — a page shape known to extract badly would otherwise have to be captured
wrong first. **Never the default, and matched exactly against `"full"`** so a
stray value can't silently widen a capture: on a blog-shaped page this keeps the
nav and sidebar chrome readability strips, so it is the escape hatch rather than
the better option. The modal's checkbox resets on every open for the same reason
— it describes one page's shape, not a standing preference.

The UI gates the control the same way, on a per-entry `captured` flag
(`data-post-captured`) rather than on feed identity. Gating on the feed is what
silently stripped the escape hatch from every article the filer moved.

**One layout owner, three modes.** The inline shell in `index.html` resolves
`wide` / `medium` / `single` from a single `updateSingleMode()`, at 1100px and
720px. Single-pane mode was removed in `9dab5a8` and revived rather than replaced
with a phone-specific renderer, and that is the whole design argument: a second
renderer means every feed-appearance feature — lead images, per-feed thumbnail
crop and zoom, embeds, the full-image webcomic view — has to be ported to it, and
every future one silently misses it. The phone runs the same markup, so it
inherits all of them and everything added later.

The revival was wiring, not a rewrite: `9dab5a8` stubbed `isSingleMode` /
`setSinglePaneLevel` as no-ops but left ~10 call sites in `app.js` intact, and
left `templates/js/_layout_shell.js` on disk holding a complete second
implementation that was included nowhere (now deleted — dead code that looks live
is worse than none). Porting into the existing shell rather than re-including that
file is what stops two shells disagreeing about the current mode.

Three details worth keeping: the pane level is clamped to 0–2 and persisted in
`sessionStorage`, because a pane swap re-runs the shell and would otherwise drop
the reader back to the folder list; an `entry_id` in the URL overrides the
remembered level, so a shared or reloaded article opens the article; and hidden
panes are `display: none` rather than translated off-canvas, since laying out and
fetching a hidden pane's images is the expensive half of a page on a phone.

**The tree is not re-rendered by pane-swap navigation, so anything the server
stamped into it goes stale.** `updateScopeActiveState` already re-derives active
rows and mode blocks for that reason; sidebar tag links now get the same
treatment, because their server-rendered `folder_id` otherwise survives every SPA
navigation for the life of the page — open a folder, click Feeds, click a tag,
and you are back in the folder you left. The stamp reads the URL's *own*
`folder_id`/`list_feed_url`, captured before the fallbacks in that function
reassign them from whichever row is still lit: those fallbacks exist to stop
active-state flicker on bare URLs, and reusing their result here would reproduce
the staleness rather than fix it. `resume_read_filter` is refreshed alongside,
since a tag view forces `read_filter=all` and carries the filter to come back to.

**A URL can carry a month without carrying a day.** `url_inferred_pubdate` reads
the `/YYYY/MM/DD/` permalink; `url_inferred_pubmonth` reads `/YYYY/MM/` and
resolves it to the first of the month. The day is a placeholder, the month is not
— WordPress generates the permalink from the publish date. It is the last tier in
`recover_publish_dates.py` for that reason, but on blog.guitar-pro.com (67 of the
68 entries it recovered) it is also the *only honest* signal: those pages publish
`dateModified` and nothing else, so mining the page would have dated a 2021 post
to October 2024. A real month beats a precise-looking lie.

**A re-fetch moves Received, never Pub.** Surfacing a re-pulled capture at the top of the backlog used to be done by writing `entries.published = now` — which corrupted the data it sorted by. Pub means the date the article was published, and re-fetching does not republish it; worse, under a **Pub oldest** sort the bump did the opposite of surfacing, sending the article to the far end of the list. Measured on the live library 2026-07-25: **101 entries** had lost their real publish dates this way, some by 16 years. `replace_entry_content(bump_received=...)` now moves the Received date instead — which is honest ("this content arrived just now"), sorts correctly in both directions, and needs no new per-entry field. Both received columns move together: `first_updated` backs `Entry.added`, which the UI displays and the render-path sort reads, and `recent_sort` backs the list's SQL fast path.

`scripts/restore_bumped_publish_dates.py` repairs entries already damaged. The original date is recovered from the starred archive (`archived_entry.published_at`), which snapshots each entry's dates at capture and is untouched by a content re-fetch, and is cross-checked against reader's own `recent_sort` (the entry's original sort position, likewise untouched). Only forward drift qualifies — a bump can only move a date later — and by default only rows where the two independent records agree are restored; on the live library that was all 101, with zero disagreements.

Worth knowing when touching the received sort: the two list paths read **different columns** for it. The SQL fast path orders by `recent_sort`; the render path sorts on `received_timestamp`, i.e. `Entry.added`/`first_updated`. They agree for ordinary ingested entries and can disagree for entries written outside the normal path, which is why the bump above writes both.

Entry points: the **+ Save Article** modal (session `POST /articles/save`), a
bookmarklet (`GET /articles/save?url=…` — a top-level navigation, so the
SameSite=Lax session cookie rides along and an unauthenticated hit round-trips
through `/login?next=`), and `GET|POST /api/save` for share sheets/shortcuts.
`/api/save` follows the Fever model: session/CSRF-exempt (both prefix lists),
authenticated by `username` + the per-user API token, and bound to the user
with an explicit `tenancy.user_context` before the threadpooled save runs (the
blocking fetch must not stall the event loop).

**Extension save protocol** (`POST /api/bookmarklet/save`): Lectio implements
the wire format of the Readit browser extension / bookmarklet —
`{token, url, html, title}` — so that extension, pointed at a Lectio Backend,
becomes a Lectio save-extension. The value of the shape: `html` is the
**rendered DOM captured from the user's authenticated browser**, so
paywalled/bot-walled pages arrive with full text and the server performs *no
fetch* (`extract_readability_article` runs the same readability pipeline on
the provided HTML; absent `html` it falls back to the normal server-side
fetch). Auth is token-only (`UserStore.user_for_api_token` — bare-token
resolution, constant-ish comparison count across users) since the payload
carries no username; the route is session/CSRF-exempt and answers CORS
preflights with a wildcard origin (safe: auth lives in the JSON body, no
cookies), which is required because the extension's `host_permissions` don't
cover third-party backends, putting its fetch under normal CORS. Captured
HTML is capped at 6.5M chars, mirroring the extension's own truncation.
A captured-DOM save of an **already-saved URL** is treated as a deliberate
re-capture (the user often cleaned the page in-browser first): extraction
re-runs on the new DOM, the stored content is replaced (direct column write
in reader's JSON shape — EntryData has no public setter), and the entry bumps
to the top of the backlog (published/saved_at = now). A pinned title (Edit
title…) is never clobbered; URL-only re-saves (bookmarklet, /api/save) stay
light re-star no-ops. One special case: the extension captures whatever tab
it's on, so a capture made *from inside Lectio* would bookmark Lectio's own
UI page —
`_unwrap_lectio_reading_url` detects a submitted URL on this instance
(request host or `LECTIO_PUBLIC_URL`), extracts the wrapped
`feed_url`/`entry_id`, and **stars that entry** instead (the native
save-for-later; no on-star fan-out, matching direct saves). If the wrapped
entry has aged out but its id is itself an http(s) URL (common — many feeds
use the article URL as the guid), that URL is saved as a normal article via
server fetch, since the captured DOM is Lectio chrome rather than the
article.

**Saved Articles sidebar view.** The tree's top row (`.saved-items-row`,
restored from the pre-2026-04-20 sidebar with its surviving CSS/JS) opens the
all-starred view (`star_only=1` at the root folder) with an unread-starred
count badge (`get_saved_unread_count`; kept live client-side by
`adjustSavedUnreadBadge` on single-post read toggles and star toggles — bulk
operations let it drift until the next render). Saved mode and Feeds mode are
**mutually exclusive tree blocks**: entering Saved expands its own folder
sublist (`.saved-tree-children` — folders holding saved items, badges =
*total* saved per folder via `get_saved_counts_by_folder`, since the view
defaults to the whole backlog) and collapses the feeds tree
(`.feeds-tree-children`), and vice versa; the pane-swap path toggles the two
blocks in `updateScopeActiveState` since it never re-renders the tree.
Entering Saved always opens on **All** and stashes the current read filter in
`resume_read_filter`; the **All Feeds** row (and the *active* Starred menu
item) exits star mode restoring that filter. Feeds-tree links never carry
star mode (each block is only visible in its own mode). Within star mode the
read filter **composes** instead of being ignored:
`read_filter=unread&star_only=1` narrows to unread starred
(`list_entries_for_feeds` skips read entries regardless of star mode; only
`history` stays exclusive with starred since it sorts by read time).
Archive-only orphans are excluded from the unread narrowing — they are read
by definition (no live entry). Clicking the Saved Articles header is an
**expand-only landing** (`saved_home=1`: no posts load — the whole backlog is
expensive); the sublist starts with an **All** row (the full-backlog view at
the root folder) and ends with **Uncategorized** (saves in unfoldered feeds,
including everything in `lectio:saved`). In saved mode the sublist is the
scrollable region and the All Feeds row pins to the bottom above Tags
(`.tree.saved-mode` flex layout). The `lectio:saved` feed itself is excluded
from the Uncategorized *display* set (feed list, unread badge, and the row is
hidden in Feeds mode when it's the only unfoldered feed) but stays in the
Uncategorized *view* set, so the Saved sublist's Uncategorized folder reaches
its entries.

### Tag-as-keep — the unified Kept view

Tag and Star are the two **keep** axes: a post is kept (offline-archived and
never auto-pruned) whenever it's starred **or** manually tagged. Tag is the
"keep forever" axis, Star the lightweight "to-do". The archive
(`archived_entry`, keyed on `(feed_url, entry_id)`) is independent of the
`saved_entries` star table, so tagging can enqueue a capture without a star row.

- **Enqueue on tag.** `set_manual_tags_for_entry` enqueues an archive when a tag
  is added and enqueues a removal when the **last** tag is removed *and* the
  entry isn't starred. `delete_manual_tag_everywhere` applies the same
  last-tag-and-unstarred release per entry. The star toggle's off-branch is
  likewise guarded — it only releases the archive when the entry carries no
  manual tag. The shared guard is `_entry_should_keep_archive` (= starred OR
  has ≥1 manual tag); dropping one axis never wipes an archive the other needs.
- **Kept view.** The Saved-mode entry list (`list_entries_for_feeds` with
  `star_only=1`) filters on **star OR tag**: alongside `saved_entries_set` it
  loads a `tagged_entries_set` (one `entry_tags LIKE` scan over the view's
  feeds), unions them into `kept_entries_set`, and both the point-lookup fast
  path and the membership filter use that union. `saved_entries_set` still drives
  the per-row `saved` flag. `get_saved_counts_by_folder` /
  `get_saved_unread_count` count the union too.
- **Kept-but-unsubscribed feeds.** `reader` requires a feed to exist for its
  entries, so unsubscribing a feed that carries curation defaults to a **keep**
  mode (`keep_entries=1` on `/feeds/unsubscribe`, the default radio in the
  curation dialog): it deletes the `folder_feeds` rows, `disable_feed`s the feed,
  records it in the new meta table **`kept_feeds`**, and force-flushes pending
  captures — but does **not** `purge_orphaned_feed`, so the reader feed, its
  entries, tags, and stars survive. Kept feeds are hidden from the tree by
  excluding them at the source: `get_all_reader_feed_urls()` subtracts
  `get_kept_feed_urls()` by default (so All Feeds, Uncategorized, and counts drop
  them), while the Saved/Kept view passes `include_kept=True`/unions the kept set
  back so their curated items stay browsable **grouped under their original feed
  name**. Re-subscribing (`add_feed_to_folder`) clears the `kept_feeds` row and
  re-enables updates; a later full delete (`purge_orphaned_feed`) drops it.
  `kept_feeds` is created in `ensure_meta_schema` (covered by the startup
  per-user migration, so existing tenants don't 500).

**Tagging orphan archives.** `_build_orphan_entry_detail` renders an entry whose
feed is gone from `reader` entirely from its `archived_entry` row (see "Removing
a feed" above). Star already worked on these — `apply_star_state` writes
`saved_entries` directly by `(feed_url, entry_id)`, with no `reader` dependency
— but manual tags piggyback on `reader`'s own `entry_tags`, keyed to a
`resource_id` that only exists if `reader.get_entry()` finds something. An
orphan has nothing there, so tagging one silently no-op'd: `set_manual_tags_for_entry`
returned `[]`, the route reported `{"ok": true}`, and the UI cleared the input
with no chip and no error (reported 2026-08-09, `#pshell` on an orphaned
`packtpub.com/rss.xml` item — Packt migrated to `hub.packtpub.com` years earlier
and the old feed was long gone, but the archive capture and its "kept" status
survived).

`orphan_entry_tags` (meta DB, `(feed_url, entry_id, tag)`) gives tags the same
independence Star already had, as a fallback *only* — every manual-tag entry
point (`get_manual_tags_for_entry`, `set_manual_tags_for_entry`,
`delete_manual_tag_everywhere`, `rename_manual_tag_everywhere`,
`has_any_manual_tags`, `get_all_manual_tag_names`) tries `reader` first and
falls back to this table only when `reader.get_entry()` misses. Normal
(non-orphan) entries are untouched — this never becomes the primary path for
anything `reader` can already answer.

**A surviving capture is not itself a keep signal.** `_build_orphan_entry_detail`
used to hardcode `"kept": True` for every orphan, purely because a complete
`archived_entry` row existed — a weaker definition of kept than everywhere else
in the app, where it strictly means star OR tag. Measured on the live library
2026-08-09: of 1,279 orphans, 1,089 (85%) were already genuinely starred, but
190 carried neither signal — mostly historical (the packtpub batch above, 89
of them) rather than anything current-day tagging/starring produces. `kept` and
`saved` on the orphan detail dict now reflect the real signals (star via
`_entry_is_starred`, tag via `orphan_entry_tags`), and
`get_orphan_saved_entries` filters to the same rule before the Saved/Kept view
ever sees a row — an uncurated leftover capture no longer appears as Saved. It
still opens fine via a direct link (`get_entry_detail`'s fallback doesn't
gate on curation), so nothing is deleted or hidden from direct navigation —
only the "is this Saved" list membership and flag changed. The user-visible
effect: navigate to one of the 190 and it now looks correctly *un*starred and
untagged, with the tools to fix that right there instead of looking identical
to something you'd actually curated.

**Orphans and search.** `merge_orphan_saved_entries` used to be skipped
outright whenever a search query was active (`not search_query` at both call
sites) — reported 2026-08-09: searching "packtpub" turned up nothing, because
orphans were never in the candidate set search ran over at all. Orphans have
no reader row, so the SQL-narrowed search paths (`_filter_star_keys_by_search`
etc.) can't reach them regardless. Rather than excluding orphans from a
search, `get_orphan_saved_entries` takes an optional `search_terms` list and
matches it in Python against title/link/feed_title/author — same
AND-across-terms rule as the rest of search, same tokenization
(`search_terms_from_query`), just not routed through SQL. Deliberately
metadata-only, not the archived body text: decompressing every orphan's
`content_html_zlib` on every search would cost more than the orphan set's size
(low thousands at most) justifies today; revisit if that ever feels thin.

**The discoverability dead-end this created, and why the 190 got deleted.**
Once "kept" meant star-OR-tag, the 190 uncurated orphans stopped appearing
*anywhere* — not the Kept view, not search — with no path back to one except
an exact bookmarked URL (which is how the packtpub orphan was found in the
first place). An item you cannot find is one you cannot curate, so leaving
them stranded-but-present served no purpose. `scripts/purge_uncurated_orphan_archives.py`
deletes exactly this set (orphan AND no star AND no tag) via the existing
`delete_archive` cascade (asset rows and now-unreferenced asset blobs go with
it). Run 2026-08-09: 190 deleted, 89 of them the packtpub batch. A curated
orphan (star or tag) is untouched regardless of how old or how dead its feed
is — this only ever removes captures with zero keep signal.

**Searching the Kept view.** The kept branch in `list_entries_for_feeds` runs
*ahead* of the generic `elif search_terms` fast path, so for a long time this was
the one view where a search took no fast path at all: it hydrated every kept key
via `reader.get_entry` and filtered in Python — ~11k lookups, measured at ~19s
per search on a real library, which reads as a search box that does nothing.
`_filter_star_keys_by_search` now narrows the keys in SQL first (the same
keys-joined-against-`entries` technique as `_sorted_star_key_window`), so only
the survivors are hydrated: ~1.2s for the same queries.

reader's own FTS index is deliberately **not** used for this. `search_entries`
builds a highlighted snippet per result, measured at ~7.8ms/row — 76s for one
common term across 133k entries — so routing the kept view through it was *worse*
than the scan it replaced (97s end to end). The SQL predicate
covers title, resolved feed title, feed URL, link, author, summary **and the
stored content**, so a Saved search reaches the article's text — the point of a
read-later archive. Content is matched as stored (raw HTML), so a markup-ish term
matches nearly everything; stripping tags would need a plain-text column
maintained at ingest, which isn't worth a schema change yet. On any SQL error the
helper returns `None` and the caller keeps the full key set and post-filters in
Python, so a failure degrades to the old behavior instead of showing no posts.

**Searching the Feeds view** (`_search_entry_keys_in_sql`) now works the same
way, for the same reason. It previously used the FTS index, and the snippet cost
above turned out to be ~95% of the time — measured on the live library (134k
entries, 2,888 feeds): `search_entries` took 19.7s for `python` against 1.3s to
hydrate the results. Narrowing to matching keys in SQL and hydrating only the
survivors took the same search to **1.45s**, and `guitar` from 9.3s to 1.3s.

Two consequences worth knowing. First, the two search surfaces now share a
predicate, so they finally agree: a Feeds search reaches article text rather than
only metadata (`coffee`: 833 → 1,237 hits), and inherits the same raw-HTML
caveat. Second, when the selected feed set fits under SQLite's 999-variable limit
the scope goes into the query, so `LIMIT` applies to rows the user can actually
see; above that it matches unscoped and the caller drops out-of-scope feeds — the
same shape (and the same under-fill caveat) the FTS path had.

**reader's FTS index is retired.** Both surfaces resolve in SQL, so nothing
called `search_entries` — and maintaining the index was not free: 1.3ms per new
entry on every refresh (`update_search()` ran at the end of each refresh batch,
on every save, and after imports), plus a file roughly the size of the reader DB
itself — **564MB against 743MB** on the live library. It is no longer built,
enabled, or updated, and the startup index-build thread is gone with it, so a
fresh install no longer spends its first minutes walking every entry.

`scripts/drop_search_index.py` reclaims the space on an existing install.
`disable_search()` alone does *not* reclaim it: the DROPs land in the WAL and
SQLite never shrinks a file on its own, so a naive drop briefly **doubles** disk
use (measured: 564MB index + 567MB WAL). The script checkpoints and VACUUMs,
taking the index to 4KB.

The index is derived, not user data — `enable_search()` + `update_search()`
rebuilds it from the entries table should a future ranked search want it. That
rebuild walks every entry and takes minutes on a large library, which is why
dropping it is a deliberate script rather than a startup side effect.

**Auto-filing saved articles** (`services/saved_autofile.py`, `GET /saved/autofile/preview`, `POST /saved/autofile`, driven from the top-level Settings → **Utilities** tab, which also holds the two duplicate scanners and the one-shot maintenance actions; it was promoted out of a Feeds sub-tab so the scanners — long-running, long reviewable lists, worked in repeated passes — sit alongside the rest rather than behind an extra click). A read-later library imported from a feed reader is mostly articles from feeds already subscribed to, so they can be filed onto their real feed — which also collapses cross-feed duplicates for free, because `_move_entry_to_feed` matches into the target by GUID else normalized link.

Matching is by **article host**, from two independent signals. The evidential one is which subscribed feed already carries entries whose links are on that host — a feed's own URL often lives elsewhere than the articles it publishes (`rss.beehiiv.com` serving `joanwestenberg.com`). The declarative one is the hosts a feed *advertises*: its own URL host and its `link` (site) host. Entry links alone are not enough, because two common cases produce no usable evidence at all — a feed **subscribed but not yet fetched** has no entries, so a feed added specifically to receive a backlog would never be offered for it; and a **link-proxying feed** (FeedBurner rewrites every entry link to `feeds.feedburner.com`) points its evidence at the wrong host entirely. Measured here, 696 of 2,881 feeds advertise a site on a different host than their feed URL. Adding the declarative signal took unmatched articles from 698 to 66.

A declared host makes a feed a candidate; it makes it *confident* only when the feed is also stocked (`feed_sizes` ≥ `MIN_SUPPORT`). Without that size check a scraped one-article URL sitting in the subscription list — right host, plausible title — would read as the site's real feed and collect the site's whole backlog. Two guards decide what may be *pre-approved*, and the distinction matters — measured against real data, `guitarworld.com`'s target was backed by 77 of the feed's own entries while `guitarplayer.com`'s only candidate was a scraped single-article URL with **one** supporting entry, and filing 303 articles into it would have been wrong:

- `ambiguous` — more than one *on-host* subscribed feed carries entries on the host, so picking one would be a guess.
- `support` — how many of the target feed's own entries are on that host; below `MIN_SUPPORT` the cluster is shown but not pre-checked.

The service is pure (it takes extracted rows, not a reader) so the guards are testable without a database. The preview is read-only and strips `entry_ids` before sending; apply takes the target from the request rather than recomputing it, so what was approved is exactly what runs.

**Filing is batched, and has to be.** `_move_entry_to_feed` runs at roughly 17 articles/second, so one uncapped call over a large host (1,300+ articles) takes well over a minute and gets cut off in flight — observed live as `POST /saved/autofile → status 0, 16180ms`, where 278 articles really were filed but the reply never arrived, so the UI looked untouched and the articles appeared unmoved. The endpoint therefore caps each call at `_AUTOFILE_BATCH` articles and reports `remaining`; the client loops, showing progress, until nothing is left. A batch that reports work outstanding while moving nothing breaks the loop rather than spinning.

**Unstarring tagged articles** (`services/unstar_tagged.py`, `GET /saved/unstar-tagged/preview`, `POST /saved/unstar-tagged`, UI: Settings → Utilities → *Unstar tagged articles*). After the tag-as-keep flip a tag keeps an article on its own — tagged entries are archived and pruning-protected independently of the star — so a star on an already-tagged article is redundant, and it only clutters Saved, which is meant to be the read-later queue rather than a second copy of the filing system. Nothing is lost by dropping it: pruning protects starred and manually-tagged entries *independently*, and the unstar route only enqueues archive removal for an entry with no manual tags, so the offline capture survives either way. Only the star row is deleted. The service is the pure decision layer (it takes the current curation and returns what would change); all DB access and the cache invalidation a behind-the-back write needs stay with the route, which recomputes the plan server-side under the submitted opt-outs rather than trusting a client-supplied id list.

**The UI inverts the API's opt-out, on purpose.** The endpoint takes `keep_tags` — the tags to *protect* — but a panel that rendered that directly would show all ~58 affected tags pre-checked, making "unstar everything" the default and *unchecking* the destructive act. That breaks the rule that a bulk-action list arrives with nothing selected. The panel therefore selects tags to **clear** and derives `keep_tags = every affected tag − selected` before each call, so an empty selection is an empty action. The inversion looks redundant and is pinned by tests for that reason.

**Per-tag counts cannot be summed**, which is why the action button's number comes from the server. An entry is protected by *any* kept tag, so an article tagged both `python` and `books` survives a selection of `python` alone even though it is counted in the `python` row. Adding the checked rows client-side would therefore over-promise. Every selection change re-requests the preview under the derived `keep_tags` and shows `to_unstar`, with an out-of-order guard so a slower earlier reply can't overwrite a newer count. Tag names suggesting a reading queue (`read`, `todo`, `later`, `queue`, …) are flagged via `queue_like_tags` and excluded from "select all topical tags" — for those the star *is* the queue, not a redundant copy. Measured zero matches on live data twice, but tag vocabularies drift and this cleanup is meant to be re-run.

**Two different "this isn't a feed" decisions**, deliberately labelled apart in the UI because they can appear on the same row:

- **`non_feed_subscriptions`** (`POST /saved/autofile/non-feed-subscription`, UI: *not a feed*) bars a **subscription** from ever being a filing destination. Some subscriptions are a single article URL that got added as a feed; they sit on exactly the right host with a plausible title, so they are the target you would pick by mistake, and filing a site's backlog into a one-article stub is the worst available outcome. The subscription itself is left alone — its entries are real reading — and it is fed into `_autofile_excluded_targets`, so it is barred on the apply path too.
- **`autofile_non_feed_hosts`** (`POST /saved/autofile/non-feed`, UI: *one-off saves*) settles a **host** whose saves never came from a feed at all.

**Hosts settled as one-off saves** drop out of the worklist entirely. Some saves never came from a feed at all — a cheat sheet, a one-off tutorial — and the filer can only observe that no subscribed feed matches, so it re-proposed them on every pass and they never resolved. Marking is purely a worklist decision: the saved articles are untouched and stay exactly what they already are, standalone read-later captures. Marked hosts are reported back in a collapsed section with an undo, so the decision is reviewable rather than a black hole, and the mark keys on `article_host()` — the same normalized form the plan groups by, or a host would return under its `www.`/cased spelling. The table is created in `ensure_meta_schema`, which the startup per-user migration runs for every tenant; a meta table added anywhere else 500s for users provisioned before it existed.

**Nothing in the plan is ever pre-checked.** It files thousands of rows at a time and is meant to be worked in passes — file a batch, re-scan, continue — so the `confident` flag drives a *label* ("strong match — N posts from this host"), never a selection. Same rule as the Saved duplicate dialog: a scan result is a claim, not an instruction.

**Barred targets** (`_autofile_excluded_targets`, applied on *both* preview and apply so a stale plan can't route around it): Saved Articles itself, and every YouTube feed. A saved page is never really a video-channel post, and channels routinely share a name with the blog they accompany — with only feed titles on screen, a YouTube feed is precisely the target a reviewer would pick by mistake. For the same reason the picker shows each candidate's **feed URL**, inline and as a hover title: feed titles are frequently and deliberately unlike their URLs (`rss.beehiiv.com/feeds/XYZ.xml` titled "The Woodshed"), so the title alone is not enough to identify what you are filing into. When two candidates for one host share a title (a feed and its format variant, or a blog and its companion channel) the URL is folded into the option label itself — otherwise the dropdown offers choices that read identically and the pick cannot be made at all.

**Reviewing an ambiguous host by hand.** A host with more than one on-host feed (Medium, Ars Technica) is `ambiguous` — the filer can't pick a destination, so those saves stall in the worklist. Each row carries a magnifying-glass link that opens exactly those saves: `list_feed_url=lectio:saved&read_filter=all&q=site:<host>`. Two pieces make it precise. The **Saved Articles feed** (`lectio:saved`) holds only articles *not yet attached to a feed* — filing moves them onto the real feed and out of this synthetic one — so scoping the view to it drops the host's already-filed posts; it is synthetic and absent from `get_all_feed_urls`, so `filter_feed_urls` allows it through explicitly. The **`site:<host>`** search operator (`_split_site_terms`) matches an entry's *link host* (apex or subdomain, boundary-checked) rather than a bare substring, so a mention of the host in some article's body doesn't pull that article in. `site:` never reaches the text-matching SQL/haystack paths — it is a link-host filter applied during hydration, and forces the full-scan path (`need_all`) so nothing is clipped before it runs. From there each is filed by hand with the per-post **Move to feed** action. Frontend-only wiring; the `site:` operator and the synthetic-feed allowance are the only server changes.

**Moving a saved article deletes its source.** `_move_entry_to_feed` normally leaves the source entry in place — reader can't delete feed-provided entries, so it settles for marking them read and stripping star/tags. That rationale does not apply to `lectio:saved`, whose entries are `added_by='user'` and therefore properly deletable, so the saved source is hard-deleted (tombstoned, via the shared `_hard_delete_entry`) once the move succeeds. Without this the Saved Articles feed kept a read, unstarred husk per filed article: the backlog never shrank as it was filed, and every later duplicate scan re-read rows that were no longer real saves.

**Toolbar listeners must be delegated.** `loadScopePanesWithoutFullRefresh`
(every sidebar/folder/scope click, and the search form itself) re-renders the
toolbar, replacing its DOM nodes. Any listener attached directly to a
`#toolbar-*` node at init dies with the node it was bound to — silently, with no
console error. That is exactly how the search button came to do nothing at all
after the first in-page navigation, while still working on a direct URL load
(which is why it survived testing). The search button, its clear control, the
query input, and the search form's `submit` handler are therefore all delegated
from `document`. Wire anything new on this toolbar the same way.

## Saving an article you already subscribe to

An extension save used to create a `lectio:saved` entry unconditionally, so an article you already follow ended up as two posts — and they were never equivalent. The feed entry carries the publisher's tags (`entry_feed_tags`) and keeps updating; the capture carries a body the server often cannot fetch at all (Medium and treblezine refuse this host outright). Split apart you get an article with tags and no text beside one with text and no tags, which is exactly what happened to a Medium post on 2026-07-26 — before a "move to feed" onto the empty twin dropped its 44KB body entirely.

`save_article` now takes an injected `find_existing_entry`. `main._find_subscribed_entry_for_url` resolves an article URL to an entry in another feed by **canonical link**, not id: the two rarely agree (Medium's guid is `/p/<hash>` while the URL is the long slug) but both carry the same `link`, and `get_dedupe_host_aliases` folds declared domain migrations in. A feed-provided entry wins the tie — it keeps updating and holds the tags a capture cannot supply.

The merge keeps whichever body is longer, pinned through `entry_content_overrides` so the feed's thinner copy can't overwrite it on the next refresh, then applies the resurface a save already implies: star, un-archive, mark unread. A save that finds nothing behaves exactly as before. Without the hook — any caller that doesn't pass it — behavior is unchanged, which is what keeps the service testable in isolation.

This is the primitive the cross-feed duplicate work (Plan #6) needs as well: a save that merges is a duplicate that never happens.

## Re-fetch on keep

Both Star and Tag already `enqueue_archive`, so an offline capture (page +
readability) is taken on either. That capture is what Read Mode and
`/entries/readability` read. It does **not** touch the entry pane, which shows
stored feed content — so a truncated feed still showed its teaser there.

`_maybe_autofetch_on_keep` closes that, narrowly:

- **Only when the stored copy is thin**, judged by `_archived_copy_is_plausible`,
  the same test the reader uses to reject a failed extraction. Re-fetching a good
  copy can only make it worse — the live page may now be a paywall, a cookie wall,
  a 404, or a readability miss that locks onto a sidebar (the illogicalcontraption
  case). Overwriting a good article at the exact moment the
  reader marked it worth keeping is the failure being avoided.
- **Only from the star and tag ROUTES**, never from `set_manual_tags_for_entry`.
  That service is also driven by the feed auto-taggers, at ingest, across
  everything a refresh just delivered — hooking it would turn one refresh into a
  burst of outbound requests at a single host.
- **Never for a `lectio:saved` capture**, which was fetched from the page already.

It runs off-request through `_run_in_user_context`, since a bare thread would lose
the tenancy user and fetch as the default one.

**A refusing host is remembered.** Because this is a side effect of tagging rather
than a request, a site that declines us must not be re-asked on every tag —
DeviantArt answers this server with 403 every time, and tagging across a watchlist
would be dozens of requests it has already refused. After a failed automatic
re-fetch the host is paused for six hours (in memory; it is a politeness memo, not
a record, and it is bounded). Manual Re-fetch ignores the pause entirely: that is
a person asking on purpose.

**The mismatch guard was too blunt, and it broke this feature on arrival.**
`_page_is_a_different_article` compared the URL slug against the fetched *title*
only, and refused on zero overlap. But a descriptive slug and a specific title
disagree routinely: whiskyadvocate.com/peated-whisky-cocktail-for-summer is headed
"Charred Garden Smash", the drink's name, sharing not one word with its own slug.
That refused the manual re-fetch and the automatic one alike — one bug reported as
two. The guard now also consults the fetched **body** before refusing, which does
not weaken what it was built for: a parked "Empowering Relationships" page does not
mention ornaments or dingbats either, and a section index does not discuss the
article it replaced. Zero overlap across title *and* body is a far stronger signal
than zero overlap with a title, which is normal.

**Re-fetch is available on any entry with a link** (2026-08-02). It used to
require a capture, star or tag, on the reasoning that the next feed refresh would
undo the replacement — but the **pin** is what prevents that, and
`refresh_captured_article` applies it to every non-capture entry
(`pin_content=not is_capture`) regardless of whether anything keeps it. The gate
was guarding a hazard already handled, and its practical effect was that repairing
a truncated post meant tagging it first, filing something you may not want filed
just to read it properly. The real protections are unconditional and stay: the
mismatch guard, and the pre-replacement snapshot in `entry_content_edits` that
makes any re-fetch one click to Revert. Kept-ness still decides one thing — the
offline **archive** enqueue, since a capture with no keep signal holding it is
precisely the husk the unstar path has to clean up.

Separately, the right-click **Re-fetch** items gate on `data-post-kept`, and only
starring kept that attribute current: tagging re-rendered the entry *pane* and
left the list row — the thing actually right-clicked — stale until a reload. The
tag handlers now sync the row from the server's reply (`data.tags`, the normalized
and capped set), OR-ing the star back in so clearing the last tag off a starred
post does not un-keep it.

## Node bulk actions, and what a re-fetch may replace

**Node bulk actions are scoped to the drilled-into view, and Read Mode gets buttons
rather than a menu.** `_scope_starred_keys(folder_id, list_feed_url, tag)` resolves
the stars in the *current* view — feed **and** tag together, since the case is
"drilled down to a single feed with stars I don't need". Stars only: a tagged-but-
unstarred entry has no star to remove, and unstarring is not how a tag is dropped
(that is *Delete tag everywhere*, which already existed in the sidebar context menu).

`POST /saved/unstar-scope` recomputes the set server-side and goes through
`apply_star_state` **per entry** rather than issuing one bulk `DELETE`. That is not
fastidiousness: the unstar path releases the offline capture and hard-deletes a
`lectio:saved` husk once no keep signal remains, and a bulk delete skips both,
leaving orphaned captures and invisible husks.

Read Mode has no right-click, and long-press there offers only text selection — so
the actions render as visible buttons in the browse header, plain forms in the same
navigation model as the Sort switcher (no JS, one e-ink repaint). **Deleting a tag
takes two taps**, the first arming a row that spells out what goes, because a
browser `confirm()` is awkward to hit on that WebView. The row never appears on the
Archive node: that is a review surface, not a place to bulk-destroy curation.

**A re-fetch snapshots the body before replacing it, and falls back to the archive
when the live page is refused.** Two entries were destroyed before either existed —
the-digital-reader served a parked page returning 200, and informit's
`/articles/article.aspx?p=…` hid its subject in the query string while its path
("articles") matched the site index title. Each needed a backup dive, and one was
unrecoverable.

The snapshot reuses `entry_content_edits.original_content`, the same row the cleanup
feature reverts from, with `INSERT OR IGNORE` so the FIRST original wins — reverting
means "as the feed served it", not "as the last re-fetch left it". Sharing the row
also lights up the existing Revert control with no further wiring.

The archive fallback is one JSON call to `archive.org/wayback/available` — no
crawling, and nothing further asked of a site that already refused. It runs only
when the guard rejected the page or the source is gone, and the refused result is
kept unless the archived copy actually succeeds. The guard still applies to the
archived fetch, comparing the ORIGINAL URL against the archived page's title, so a
snapshot of the same parked page is refused just as the live one was.

**Re-fetch is gated on KEPT, not on the star.** Both re-fetch items (readability
and whole-page) appear when the post is one Lectio is keeping — a capture, starred,
**or manually tagged** — because only then is there a stored copy worth replacing.

The gate originally checked the star alone, on both sides (`postCanRefetch` in the
client, `refresh_captured_article` on the server). Tag-as-keep made a tag a keep
signal everywhere else and neither followed, so a tagged-but-unstarred post showed
in the Saved view with no way to re-fetch its content — **14,695 items**, the
majority of the library.

An *unkept* feed entry is still refused, and the reason is what makes the kept case
safe: `replace_entry_content(pin_content=not is_capture)` writes an
`entry_content_overrides` row for a feed entry so the next refresh cannot clobber
the fuller copy. Without that pin the re-fetch would be silently undone, which is
exactly the failure the original refusal existed to prevent.

## Keeping the files a post links to

Some posts are a wrapper around a download: guitar-pro's tab posts link `.gp`
files and PDF lyric sheets that disappear with the article. A per-feed extension
list (`feed_display_prefs.attachment_exts`) drives a scan of the captured HTML on
star/tag, storing matches through the existing `_archive_asset` — which already
stores non-image bytes untouched and dedupes per `(entry, source_url)`, so
attachments inherit retention and the orphan sweep for free and differ from images
only in how they are **found**. 25 MB per file (`ATTACHMENT_MAX_BYTES`).

What counts as a file is the whole design:

- **No bare wildcard.** `*` on an ordinary post also matches every link to a
  homepage, a category or a social profile. A **prefix** pattern is allowed
  (`gp*` → gp/gp3/gp4/gp5/gpx) because it still names a family of file types and
  cannot reach a page, and it must be at least two characters — `p*` (pdf, png,
  ppt, psd…) is a wildcard wearing a hat.
- **Page extensions are refused everywhere** (`html`, `php`, `aspx`, `jsp`, …),
  dropped on save *and* re-checked in the finder, so a stored value cannot smuggle
  one through. Dropped rather than rejected: typing "pdf html" means the pdf, and
  the UI says which were ignored.
- **Any host, matched on the URL path.** A same-host rule was rejected —
  guitar-pro serves tabs from `assets-wp.guitar-pro.eu` while the post is on
  `blog.guitar-pro.com`, so it would miss exactly the case this exists for.
  Path-only matching also keeps `/post.php?download=song.gp` a page.
- **Anchors are judged on the path, not the whole URL.** Requiring an href to
  *end* in an image extension let a Pinterest `/pin/create/button/?…&media=….jpg`
  share button be stored as an asset. Capture refuses HTML outright as well,
  whatever led it there.
- **The Attachments list is decided by stored content type**, not guessed from
  the URL, so a Gravatar (`/avatar/<hash>?s=48`) and an extension-less CDN path
  stop being offered as files without needing a re-capture.
- **Named data attributes are decoded, never a blind base64 sweep.** guitar-pro
  ships `<span class="obflink" data-o="<base64>">`, reachable by a browser and
  invisible to an href scan; `data-o`/`data-url`/`data-href`/`data-link`/
  `data-file` are decoded and the result still has to satisfy the feed's
  extension list. This widens where links are *found*, not what counts as a file.

**Enclosures are captured unconditionally**, without the extension list: an
`<enclosure>` is the publisher *declaring* that a file belongs to the post
(Standard Ebooks attaches the epub), which is a stronger claim than a body link.
Audio is skipped (podcast enclosures are large and stream fine) and images are
already captured as images.

An archived asset is addressed by content hash, so a bare `download` attribute
made the browser save `cfc24ad676…` with no extension — unopenable and
unidentifiable. The name derived from the source URL's basename is carried in
three places: the Attachments list, in-body links rewritten to the archive (unless
the publisher set their own `download` name), and the `/starred-asset/` route via
`Content-Disposition`, so "Save link as" and a pasted URL are named too. Skipped
for images/audio/video, which render inline and would only be made un-viewable by
an attachment disposition.

## Editing a post's published date (overrides)

**Edit date…** (`POST /entries/set-date`) fixes garbage publish dates (epoch-0 entries sink to the bottom of every date sort). reader's `EntryData` is ingest-owned with no public setter, and the entry list sorts in SQL on reader's `entries.published` column — so the corrected date is written directly into that column (via `reader._storage.get_db()`), in reader's naive-UTC `YYYY-MM-DD HH:MM:SS` format. A meta-DB override row (`entry_date_overrides`) records the correction, and the refresh service re-pins it after every update batch (`reapply_entry_date_overrides`) in case a refresh re-ingested the feed's original value. Clearing the date deletes the override row only — the stored value stays until the feed next updates the entry.

**Edit title…** (`POST /entries/set-title`) is the same mechanism aimed at `entries.title` (`entry_title_overrides`, re-pinned by `reapply_entry_title_overrides`): it fixes "(untitled)" posts and garbage feed titles, and renames saved articles whose readability-extracted title is off (for `lectio:saved` entries the feed never refreshes, so the direct column write alone would already stick; the override row is kept anyway for uniformity).

**Canonical entry links** (`entry_link_overrides`, re-pinned by `reapply_entry_link_overrides`) rewrite feed-redirector links — FeedBurner's feedproxy.google.com / feeds.feedburner.com and CNAMEd burner domains (the `/~r/` path signature), FeedsPortal — to the URL the redirect resolves to, so the title's href outlives the redirector service (feedproxy is already dead). Detection lives in `services/link_canonical.py`. Three write paths: (1) the **starred-archive capture** already fetches the source page on every star, so its `on_canonical_link` hook canonicalizes at zero extra requests (and the archive row + relative-URL resolution follow the final URL); (2) **Save Article** pre-resolves redirector URLs before storing; (3) the **Inoreader importer** picks whichever of an item's `canonical`/`alternate` hrefs isn't a redirector. For stars whose redirector died before any of this existed, `scripts/backfill_canonical_links.py` recovers the real URL from the starred archive's captured page HTML (`rel=canonical` / `og:url`) — dry-run by default, `--live-resolve` for still-alive redirectors. Ordinary redirects (http→https, trailing slash) are never rewritten: only known-redirector sources qualify.

**Edit URL…** (`POST /entries/set-link`) is the manual write path into that same `entry_link_overrides` table, and exists because every automatic path can fail at once. Measured on the live library (2026-07-22): of 37 starred redirector links, **zero** were recoverable — no captured archive HTML to mine, feedproxy.google.com answers 404 with no redirect chain, and Archive.org holds no snapshot of the redirector URLs. 22 of the 37 are opaque ids (`~3/vGL5XCHkyww/`) with not even a slug to reconstruct from. When the machine can't resolve it, the user can: find the article's new home by hand, pin it here, then **Re-fetch content** to pull the body from that address.

**Only `link` changes — never the entry id.** For a Lectio capture the id *is* the original URL, and it keys the `saved_entries` star row, manual tags, and archive rows; re-keying would scatter all three. Changing the link alone suffices because both the "open original" href and `refresh_filed_article` read `link` first, falling back to the id. The route accepts http(s) only — `safe_link_url` also passes `mailto:`/`tel:`, which are legitimate hrefs but not source URLs a re-fetch could follow.

**Other domains** (`POST /feeds/url-rewrites`, `…/delete`; listed in the `/feeds/properties` payload) is the direct way to manage `feed_url_rewrites`, added 2026-07-25. Edit Website below can only seed a rule for a host it can *infer* — the channel `<link>`, or the host most posts link to — so an author's *older* dead domain, one with no surviving entries to infer from, had no way in at all. It also had no way out: nothing rendered the rules, and no route deleted one, so a wrong alias could only be undone in SQL. Adding one migrates matching entries inline through the same `migrate_feed_host_rewrite` Edit Website calls; a domain with nothing left on it reports 0 migrated and the rule still stands, because it governs ingest and the global dedupe alias map from then on. Removing one stops future rewrites only — entries already migrated keep their new ids, since the old id is gone and re-deriving it would scatter the star, tags and archive rows that followed it. Hosts are accepted as bare domains or pasted URLs, with `www.` dropped to match how `get_dedupe_host_aliases` stores its keys.

**Edit Website…** (`POST /feeds/set-website`) is the *feed*-level counterpart, for an author who moved domains without updating their feed's `<guid>`/`<link>`. Unlike Edit URL, the id here *must* change: the feed keeps re-serving the old-domain guid, so a link-only override is undone every refresh. Editing the Website seeds a `feed_url_rewrites` rule (old channel-link host → new Website host) — which rewrites the host at *ingest*, before reader derives ids — and migrates the existing posts inline via `migrate_feed_host_rewrite`/`migrate_entry_to_new_host` (recreate under the rewritten id, carry star+archived_at, manual tags, read state and the offline archive, delete the old). The batch `scripts/apply_feed_url_rewrites.py` now imports that same per-entry logic from `main`, so the one-off and the UI share one implementation. A subtlety this surfaced: the list/pane link **rebase** (`_rebase_proxy_entry_link`, built to move feedburner-proxied entry links onto the publisher host named in the feed's channel `<link>`) would take a feed whose channel link still names the *dead* host and rewrite already-correct entry links back onto it. The caller now folds the channel link through the declared migrations (`get_dedupe_host_aliases` → `_rewrite_url_host`) before rebasing, so a declared migration wins; the same fold corrects the Feed Properties Website field and the favicon lookup.

## Editing a post's body — Aardvark-style cleanup

**Clean up article** (🧹 in the pane; `POST /entries/content/clean`) arms `.entry-content` into an element-picker: hover outlines the node under the cursor, click or `R` removes it, `I` isolates it, `W`/`N` widen and narrow the selection, `Ctrl+Z` undoes. It is the manual counterpart to `_apply_feed_content_cleanups` — the hand-coded per-site strips (NASA nav, mynorthwest's related block, JWPlayer control DOM) exist because there was no way for the user to do it themselves; this is that way.

**The browser sends what it removed, not the edited HTML.** Each op is a structural path (element-child indexes from the content root) plus a fingerprint of the node — tag, id, classes, normalized text prefix, element-child count, and the last path segment of `src` with any `/api/img?u=` wrapper unwrapped. `services/content_edits.py` replays that list server-side. Posting the DOM back would be simpler and wrong: the rendered body is not the stored body (hotlink images are routed through `/api/img`, `referrerpolicy` is injected, starred assets are rewritten to local copies, and app.js rewrites more `src`s on error), so the edited DOM would bake render-time artifacts into stored content. The op list is also the durable record of *what* was removed, which is what a per-feed rule would be promoted from.

Matching is two-tier because the rendered tree and the stored tree are not guaranteed identical: walk the path and accept where it lands only if the fingerprint agrees; otherwise search the whole tree for the best fingerprint match and accept it only if it is unambiguous. An op matching neither is returned as `unmatched` rather than guessed at — a rendered-only node (an injected embed, something a render-time cleanup already removed) genuinely has nothing to delete, and silently deleting the wrong node is the one outcome worth failing over. Ops apply in order against a tree that mutates as it goes, mirroring the client, whose paths are derived from the DOM as it stands at each click.

**Persistence reuses the re-fetch path.** The result is sanitized through the normal allowlist (a cleanup must not be a way to widen what a body may contain) and written into reader's `entries.content` via `saved_articles.replace_entry_content` with `pin_content=True`, so `reapply_entry_content_overrides` re-pins it after every refresh and the feed can't re-serve the junk. `entry_content_edits` snapshots the pristine body **on the first edit only** (repeated cleanups must still revert to the feed's version, not to the previous cleanup) alongside the accumulated ops; `POST /entries/content/revert` restores it and drops both the pin and the edit row. While an edit exists, `_inject_recovered_source_embeds` is skipped for that entry — re-adding an embed the user just deleted is the one way a cleanup could look undone.

Saving or reverting re-renders **only the article pane**, via `window.lectioReloadEntryPane` (app.js's `loadEntryPaneWithoutFullRefresh`, exposed for this). The sibling edit routes (title/date/URL) full-reload because what they change is *in the list*; a body edit is not, so a reload would rebuild the list and move the reader's place in it for no reason. The loader's post-swap `centerActivePostInView` keeps the open post where it was; measured on a 30-post list, the list's scroll position is unchanged across a cleanup save. A pane fetch that fails still falls back to a full reload — `/entries/pane` requires `folder_id`, so a URL lacking it (a hand-typed link) degrades rather than breaking.

Deferred: promoting a recorded removal into a per-feed rule. That rule belongs at render time inside `_apply_feed_content_cleanups`, *not* as a bulk rewrite of stored bodies — feed-wide it would touch hundreds of entries irreversibly, and the render-time form covers old and new posts alike and can be switched off.

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
- `POST /greader/reader/api/0/subscription/edit` — folder move + rename (`ac=edit`): `a=user/-/label/<name>` moves the feed into folder `<name>` (created if absent) mapped onto Lectio's single-folder model, a lone `r=user/-/label/<name>` makes it folderless, `t=<title>` sets the feed's `user_title`. `ac=subscribe`/`unsubscribe` stay no-op-OK so a client can't unexpectedly unsubscribe feeds. (Was a bare stub — Capy's moves silently reverted on the next sync.)
- `POST /greader/reader/api/0/subscription/quickadd` — stub OK response

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
