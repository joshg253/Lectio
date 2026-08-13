# Tenancy

Per-user isolation, the resolver seam, and what multi-user mode changes.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

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
- **Open-redirect guard** — the login `next` param is filtered by `_safe_next`
  (same-origin paths only) before redirecting.

Deferred behind hooks: per-user rate-limits/quotas on refresh, scraping, and
thumb generation (not needed for a handful of trusted users).
