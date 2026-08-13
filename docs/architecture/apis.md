# Apis

The sync APIs Lectio speaks to third-party clients.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

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
