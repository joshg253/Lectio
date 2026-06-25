# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now

Build order (promoted from Later — top first):
1. ~~**Global skip-Shorts toggle** (YouTube area)~~ — ✅ SHIPPED (`yt_hide_shorts_global`).
2. ~~**Auto add to Instapaper** rule~~ — ✅ SHIPPED (`instapaper` rule type; `_run_instapaper_rules_after_refresh`; gated on configured creds).
3. ~~**YouTube quota meter**~~ — ✅ SHIPPED (per-user spend vs cap, Pacific reset, low/exhausted states).
4. **Robust YT-folder identity + Integrations subtabs** — ✅ SHIPPED. Sync menu detects the YT folder by content (rename-safe); Integrations split into YouTube/DeviantArt/Instapaper subtabs (Settings→Feeds style) so the crowded YT section has its own tab. (Server-side connection gating of the YT subtab still possible later.)
5. ~~**On-star → send to destination(s)**~~ — ✅ SHIPPED (Integrations → On Star: Instapaper / YouTube playlist / email; fires once on a genuine new star, async, one-way).
6. **Bare media links → embedded players** — ✅ Part B SHIPPED: a bare YouTube or Bandcamp album/track link that is the sole content of its paragraph becomes an inline player. YouTube (`_embed_standalone_youtube_links`): vid ID is in the URL. Bandcamp (`_embed_standalone_bandcamp_links`): numeric album/track ID scraped from the album page; cache-first (resolves immediately when cached, queues a background fetch otherwise so the embed appears next open). ✅ Part A (source-page embed recovery) SHIPPED: when an entry's stored body has no `<iframe>` and it has a source link, missing YouTube/Bandcamp/SoundCloud players are scraped from the cached source HTML and re-attached in context (`_inject_recovered_source_embeds` / `_extract_source_embed_iframes`).
7. ~~**Save to Pinterest**~~ — ✅ SHIPPED (per-entry **Pin** button; per-user Pinterest API v5 OAuth; board picker; pins the entry's lead image linked to source. `services/pinterest_oauth.py`, `PINTEREST_OAUTH_CLIENT_ID/SECRET`).
8. ~~**Add to Quire**~~ — ✅ SHIPPED (per-entry **Add to Quire** button + On-Star + `quire` automation rule; per-user OAuth `services/quire.py`, `QUIRE_CLIENT_ID/SECRET`; one default destination project; sliding-window per-minute/hour usage meter `quire_call_log` + `get_quire_usage_status`, per-run cap `_QUIRE_AUTO_PER_RUN_CAP`, 429 back-off).

9. ~~**Miniflux v1 API compatibility**~~ — ✅ SHIPPED. `services/miniflux.py` + `/v1/*` routes in `main.py`. Covers: `/v1/version`, `/v1/me`, `/v1/categories`, `/v1/feeds`, `/v1/entries` (with status/feed/category/starred/limit/cursor params), `/v1/feeds/{id}/entries`, `/v1/categories/{id}/entries`, `/v1/entries/{id}`, `PUT /v1/entries` (bulk read/unread), `PUT /v1/entries/{id}/bookmark` (star toggle). Auth via `X-Auth-Token` header (user's raw `api_token`) or HTTP Basic password. Fever pre-sync race was already fixed (`presync=False`). README badge added.

### Deferred follow-ups (Quire / destinations)
- ~~**Share-dropdown consolidation**~~ — ✅ SHIPPED. Single `ios_share` button; all four destinations in the dropdown; unconfigured ones are disabled with a "connect in Settings" tooltip.
- ~~**Per-click Quire project picker**~~ — ✅ SHIPPED. Quire button now opens a project-picker menu (mirrors Pinterest board picker); POST `/entries/quire` accepts optional `project_oid` form param that overrides the settings default. On-Star and automation rules still use the settings default project; adding a per-rule project field is a future follow-up if needed.

Detailed specs follow.

- **YouTube as a first-class "special" area.** Shipped so far: manual Add-to-playlist
  (PR #61: per-user OAuth + the per-embed control, `services/youtube_oauth.py`) and the
  **`youtube_playlist` automation rule** (auto-adds new entries' videos — incl. those
  embedded in any feed's article — to a chosen playlist; include-Shorts + mark-read
  options; per-run quota cap + non-idempotent dedup guard via `youtube_playlist_added`;
  gated on a connected account). **Still to do** (the remaining YT-area pieces below):

  Background — what exists today:
  - The "YouTube folder" is detected loosely by name (`contextFolderName
    .startsWith('YouTube')`, index.html) — that's how right-click "Sync
    Subscriptions" appears. Not robust (rename breaks it).
  - YT feeds are flagged per-feed by URL (`is_youtube_feed` = URL contains
    `youtube.com/feeds/videos.xml`).
  - **Shorts** are handled per-feed: a `hide_shorts` display pref auto-marks Shorts
    read on refresh (`_run_automation_after_refresh`, main.py ~4508); a Short is
    detected by `/shorts/` in the entry link (`_is_youtube_short`). No global toggle.
  - Automation rules live in one table (`highlight_keywords`, `type` column;
    `_HIGHLIGHT_VALID_TYPES`), scoped per feed/folder, fired after refresh. The
    **webhook** runner (`_run_webhook_rules_after_refresh`, per-run cap of 50) is the
    closest template for a new YT action.

  Planned pieces:

  1. ~~**Robust YT-folder identity.**~~ ✅ **SHIPPED.** `_hasYtFeed` (content-based) is now the primary check; the name-based fallback is an exact match against `_ytFolderName` (loaded from settings) instead of the fragile `startsWith('YouTube')`. Rename-safe, and avoids false-positives on unrelated folders named "YouTube-adjacent".

  2. **Global "skip Shorts" toggle** — ✅ **SHIPPED.** `yt_hide_shorts_global` setting
     (Integrations toggle, off by default); the hide-shorts pass targets every
     refreshed YouTube feed when on, regardless of the per-feed pref. Area-level
     setting → one source of truth, no drift.

  3. **"Auto add to playlist" automation** — ✅ **SHIPPED.** General `youtube_playlist`
     rule (any feed/folder), extracts all video ids per entry (incl. embedded + Shorts),
     include-Shorts + mark-read options, per-run quota cap, `youtube_playlist_added`
     dedup guard, rule-type gated on a connected account. (Remaining YT-area work is
     items 1, 2, and the quota meter below.)

  4. ~~**Connection gating**~~ — ✅ **SHIPPED.** `youtube_playlist` rule-type and per-embed "Add to playlist" button are gated on `yt_embed_account_features` setting (user must explicitly enable embed account features) and the button is only injected when that setting is on; the per-rule-type option remains gated on `yt_oauth_connected`. The Settings OAuth row is hidden until both Client ID + Secret are configured. A full server-side gate for a "YT special-area panel" can be added if a dedicated panel is built later.

  5. **Quota meter — "tokens left" with low alerts.** — ✅ **SHIPPED.** Per-user
     `yt_quota_spend` table keyed by Pacific date; each billed call reports its unit
     cost via a sink → `record_yt_quota_spend`; Integrations panel shows spent/cap/
     remaining (`get_yt_quota_status`) with low/exhausted states; `quotaExceeded`
     snaps to the cap. (Could still surface it in the rule editor / run-log later.)
     - **Key constraint:** the YouTube Data API exposes **no endpoint to read your
       remaining quota** — Google only enforces it server-side and returns
       `quotaExceeded`. So we must **track spend ourselves**: a per-user daily counter
       that increments by each call's documented cost (`playlists.list`/`getRating`
       = 1, `playlistItems.insert`/`playlists.insert`/`videos.rate` = 50, sub-sync
       `videos.list` = 1, etc.), displayed against the **default 10,000-unit/day** cap
       (make the cap a setting in case Google grants more).
     - **Reset:** counter resets at **midnight Pacific** (Google's reset), not local
       midnight — store the spend keyed by the Pacific calendar date.
     - Treat it as an **estimate** (other tools sharing the same Google project, or
       quota changes, can skew it). On an actual `quotaExceeded` response, snap the
       displayed remaining to 0 regardless of the counter.
     - **Alerts:** visible low-quota warning (e.g. < 500 units ≈ <10 adds left) and
       an exhausted banner; optionally an automation-run-log note when an auto-add
       run is throttled/skipped for quota.

  Caveats to design around:
  - **Quota**: each add is **50 units**; 10k/day default ≈ **~200 auto-adds/day**,
    shared with duration lookups + sub-sync. Add a conservative per-run cap (like
    webhooks' 50). On `quotaExceeded`: skip-and-log, retry leftovers next refresh.
  - **No double-adds**: `playlistItems.insert` is **not idempotent** (re-adding
    dupes). "Mark read after add" mostly prevents re-fire (rule matches unread only),
    but also record added entry IDs per rule as a guard.
  - **Token expiry**: testing-mode refresh tokens die ~7 days → auto-add silently
    stops. Surface failures in the automation run log (and as a nudge to publish the
    OAuth app to Production, which removes the 7-day churn — no formal verification
    needed at single-user scale, just clicking through an "unverified app" screen).
  - **Schema**: a new rule type / columns needs the startup per-user meta migration
    (existing tenants 500 otherwise).

  Size: medium. Schema migration + the rule-engine runner + the YT-area settings/
  rule-builder UI. Reuses the shipped `youtube_oauth` service and playlists endpoint.

- **"Send to destination" automation family — reduce the need for IFTTT.** The
  `youtube_playlist` rule above is one instance of a broader pattern: on a matching
  new entry at refresh, push it somewhere. Lectio already has the bones —
  `email_article` (send each match by email) and `webhook` (generic JSON / IFTTT
  Maker) are exactly this shape, and **Instapaper** already has a manual save path
  (`/entries/instapaper` → `instapaper.com/api/add`, configured via the Instapaper
  username/password settings). So the work is mostly *promoting existing manual
  integrations into automation rule types*, sharing one engine.

  - **Auto add to Instapaper** — ✅ **SHIPPED.** `instapaper` rule type;
    `_run_instapaper_rules_after_refresh` saves matches via `_instapaper_save_url`
    (shared with the manual save route); gated on `is_instapaper_configured()`;
    Instapaper dedupes by URL so re-saves are harmless (15-min cutoff + per-run cap).
  - **Other candidates** (each is "manual action → rule type"): save to the starred
    archive / a tag, DeviantArt-style pushes, future read-later services (Pocket is
    shutting down; Readwise/Reader, Wallabag if someone runs one). Only build the
    ones actually wanted.
  - **Shared design:** all send-to-destination rule types run from the same
    after-refresh pass (today: `mark_as_read`/`deduplicate`/`email_article`, then
    `webhook`), each with its own per-run cap, its own "configured?" gate hiding the
    rule-type option, run-log entries, and a not-idempotent guard where the
    destination can dupe (record sent entry ids per rule, like the YT add). Keeping
    them as sibling rule types (one table, one engine, one rule-builder) is what
    makes Lectio a viable IFTTT replacement for feed→destination flows.

  Size: small per destination once the YouTube rule establishes the engine pattern
  (Instapaper especially — reuses the existing save call).

- **"On star, send to destination(s)"** — ✅ **SHIPPED.** Integrations → On Star
  subtab: toggle Instapaper, pick a YouTube playlist, and/or set an email address.
  `/entries/saved` fires `_run_on_star_destinations` in a background thread on a
  genuine new star (INSERT rowcount), one-way, reusing the existing senders.
  (Original spec below.)

  Instead of (or
  alongside) the keyword-matched after-refresh rules above, a much simpler trigger:
  when the user **stars/saves** a post, automatically push it to one or more chosen
  destinations. Star is a deliberate, single-item, user-initiated action — so this
  is the lowest-friction "send" of all and sidesteps most of the rule machinery.
  - **Hook point:** the existing `/entries/saved` endpoint (main.py ~14121). On a
    star (not unstar), fan out to the enabled destinations.
  - **Config:** a single global setting, not per-feed rules — "When I star a post,
    also send it to: ☐ Instapaper ☐ YouTube playlist [pick] ☐ Email [to] ☐ …",
    each row gated on that destination being configured/connected. Far simpler UI
    than the rule-builder.
  - **Reuses the same destination senders** as the send-to-destination family (one
    sender per destination, two triggers: keyword-rule and on-star).
  - **Design notes:** fire once per star (guard so re-starring doesn't re-send);
    **one-way** — un-starring does NOT remove from the destination; do the push
    async so the star action stays snappy; YouTube still respects the quota meter.
    For YouTube specifically, "star → add to playlist" is a natural manual companion
    to the per-embed control (star a watched-later candidate, it lands in the
    playlist automatically).

## Later

- ~~**Security-mode removal / admin tuning panel**~~ — ✅ **SHIPPED** (prior session).
  `MULTI_USER = True` hardcoded; fetch-history / login-rate-limit / auto-refresh tuning
  moved to DB-backed settings in the Administration panel; dynamic OAuth callback URLs
  in Settings (populated from `public_url`).

- **Webhook follow-ups** (shipped: `webhook` rule type + Send-test button + README badge): batch/digest
  delivery.

- **Social embeds (Instagram, X/Twitter, etc.)** — harder subcase of the above.
  These ship in feeds as `<blockquote class="instagram-media" / "twitter-tweet">` +
  a platform `<script>`, not an iframe. We strip scripts (privacy/security + we don't
  load third-party trackers), so they currently render as a plain quote. X/Twitter
  iframes via `platform.twitter.com` ARE allowlisted, but the blockquote/widgets.js
  form isn't an iframe; Instagram isn't allowlisted at all. To render these we'd
  convert the blockquote/permalink to the platform's oEmbed/iframe — but Twitter and
  Instagram oEmbed now require API auth, and IG embeds are increasingly login-walled,
  so this may not be reliably doable without third-party scripts we don't want to
  load. Assess feasibility before committing; may end up "won't fix" for privacy.
- Code health (deferred — low value, no user impact):
  - **Consolidate the dedup routes** — PARTIAL. Shared feed-URL prologue extracted
    (`_resolve_dedup_feed_urls`). The match-method bodies (slug/title/both/fuzzy/
    safe) still diverge by preview-vs-apply output; a full shared-core-with-
    `apply:bool` merge is deferred — behavior-sensitive (dedup correctness),
    under-tested, needs broader characterization tests first.
  - **`ensure_meta_schema` (~585L)** — long but linear (CREATE + idempotent ALTERs),
    runs once at startup, low churn. A by-area split is cosmetic; low priority.
- Multiuser stuff:
  - **Performance investigation** — systematic baseline. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load. ~~Sync source-scrape caption hotspot~~ ✅ already fixed: cache-first / background queue, no longer blocks `/entries/pane`.
  - **Shared-content tenancy mode** — one global feed/entry store + per-user overlays
    (read/star/folders/subs). Only worth building at real scale; biggest caching/
    refresh win (single refresh per feed, deduped storage). Umbrella for "a global
    mechanism for all non-private feeds to reduce strain/storage." Pushes unread
    counts to an incrementally-maintained per-user table instead of live scans.
    reader 3.24 documented the canonical layout: `shared.sqlite` holds all feed/entry
    content (updated once per feed regardless of N subscribers), per-user DBs hold
    only personal state, a routing layer merges at query time. `update_feeds_iter()`
    yields per-feed results which could fan out into user-specific tables.
    Current Lectio layout fetches each feed once per user (N users = N fetches) — fine
    for 1–3 trusted users, but the natural limit before shared-content mode becomes
    worth building.
  - ~~**Global WebSub subscriptions**~~ — ✅ **SHIPPED.** Subscription rows + secrets
    moved to a shared `lectio_websub.sqlite` with a `websub_subscribers` join table.
    One subscribe/renew HTTP call per feed regardless of subscriber count; push fan-out
    verifies the signature once then refreshes each subscriber's per-user reader DB.
    Startup migration copies legacy per-user rows idempotently.
  - **Per-user resource fairness** — rate-limits/quotas on refresh, scraping, thumb
    generation. Not needed for trusted users; hooks left in the seam.
  - **Write-abuse protection (read-state spam)** — an untrusted user flip-flopping
    read/unread (or bulk mark) hammers the shared SQLite/process: every toggle writes
    the reader DB + `entry_read_state` and bumps `_unread_counts_generation`, which
    invalidates the unread-counts cache and forces a recompute. Defenses, cheapest →
    strongest: (1) **coalesce/debounce** rapid toggles on the same entry (the toggle
    is already async) so A→B→A→B collapses to last-write-wins; (2) **throttle the
    unread-count recompute** (min interval per user) so spam can't trigger back-to-back
    full scans; (3) the actual blocker — a **per-user token-bucket rate limit** on the
    state-changing endpoints (mark-read/unread, mark-range, saved/star), returning
    **429 + a short cooldown** when exceeded. **Tune thresholds so legitimate heavy use
    never trips it** — fast keyboard triage marking dozens of items is normal; only
    sustained pathological flip-flopping should hit the limit. **Role-based: admins
    are exempt (do whatever); regular users are subject to the limits.** Single-user
    mode is exempt entirely. Make the exemption a reusable role check so it also
    governs the other quotas (refresh cadence, scraping, thumb generation).
  - **Authenticated/private feeds** — none supported today, so all feed/image content
    is safe to global-cache. If added, exclude those feeds from the global caches.

- ~~**Tag management — remove / delete tags**~~ — ✅ ALREADY SHIPPED. `×` on each article-pane tag chip removes it (append_mode=0); right-click any tag (sidebar or chip) → "Delete tag everywhere" via `/tags/delete`. Both fully wired.

- **Integrations to investigate** (ideas; feasibility unconfirmed):
  - **Inoreader import (in-app, all sources, with rate-limit tracking)**

    Goal: import everything recoverable from an Inoreader account — feeds + folders
    (incl. disabled), tagged/starred items, and whatever the export tool captured —
    into the current Lectio user. Three complementary input sources, each optional:

    **Source A — Inoreader OPML export**
    Standard OPML; feeds + folder structure for *active* subscriptions only. Reuse
    the existing `import_opml()` / `/opml/import` machinery. User uploads the file.

    **Source B — InoreaderExportTool JSON files**
    [joshg253/InoreaderExportTool](https://github.com/joshg253/InoreaderExportTool)
    produces `backup/<label>.json` — one file per Inoreader label (lowercase label =
    tag, Title Case = folder by convention). Each file contains a list of items with
    at minimum: entry URL, title, and the label. User uploads one or more files.
    → Map each label: lowercase → Lectio tag applied to those entries; Title Case →
      also add the entry's feed to that folder.
    → Mark starred items as starred in Lectio (if an `isStarred` / starred flag
      exists in the JSON — **schema TBD: user to provide sample files**).
    → For each entry URL, find the matching Lectio entry (by URL) and apply tags/star;
      entries not yet in Lectio are skipped (API source below fills that gap).

    **Source C — Inoreader API**
    Fills in what OPML and JSON miss: disabled/inactive subscriptions, complete
    label/folder list, starred items not captured in JSON batches, and items for
    labels that weren't exported.

    Auth: Inoreader API uses **AppId + AppKey** headers plus a **ClientLogin token**
    (POST `/accounts/ClientLogin` with email + password → token). No OAuth redirect
    needed. User enters App ID, App Key, Inoreader email, and password in Settings;
    Lectio exchanges them for a token at import time. Credentials stored per-user
    (`inoreader_app_id`, `inoreader_app_key`, `inoreader_email`, `inoreader_password`
    — password treated as sensitive/masked).

    **Rate limit and checkpoint:**
    - Inoreader free tier: **250 API calls/day** (developer accounts: higher, but
      treat 250 as the safe floor).
    - Store per-user import state in a `inoreader_import` meta row:
      `{calls_today, date, continuation_token, phase, label_cursor}`.
    - On each API call: increment `calls_today`; if ≥ limit, pause and surface
      "quota reached — resume tomorrow" in the UI. Show a quota meter in the import
      panel (calls used / 250 today).
    - Each resume picks up from the stored `continuation_token` and `label_cursor`
      so nothing restarts from scratch.

    **Import phases (for Source C):**
    1. `GET /reader/api/0/subscription/list` → all subscriptions (incl. disabled);
       add missing feeds + their folder assignments. (1 call)
    2. For each label (from `/reader/api/0/tag/list`): page through
       `/reader/api/0/stream/contents/<label_id>` with continuation token; for each
       item, find the Lectio entry by URL and apply the tag. (N calls — pauses when
       quota runs out; resumes at the stored continuation.)
    3. Starred items stream (`user/-/state/com.google/starred`) — same paging
       approach; mark matching entries as starred in Lectio.

    **Settings keys (per-user, all in `_ALLOWED`):**
    ```
    SETTING_INOREADER_APP_ID       = "inoreader_app_id"
    SETTING_INOREADER_APP_KEY      = "inoreader_app_key"      # sensitive
    SETTING_INOREADER_EMAIL        = "inoreader_email"
    SETTING_INOREADER_PASSWORD     = "inoreader_password"     # sensitive
    ```
    (No refresh token needed — the import is one-shot; the ClientLogin token is
    short-lived and re-exchanged each session.)

    **UI — per-user Settings → new "Inoreader Import" section** (separate from
    Integrations; this is a one-time migration, not an ongoing connection):
    - Credentials fields (App ID, App Key, email, password) + Save.
    - Upload OPML file → immediate import, show count added.
    - Upload JSON label files (multi-select) → immediate import, show tags/stars applied.
    - "Sync via API" button → starts Phase 1–3 with progress bar + quota meter.
    - Quota meter: "X / 250 API calls used today. Resets midnight UTC."
    - "Resume" button appears if prior run hit the quota mid-import.

    **New files:** `services/inoreader.py` (API client + import logic).
    **`main.py` changes:** 4 new SETTING constants, new `/inoreader/import/*`
    routes, settings added to `_ALLOWED`/`_SENSITIVE`, checkpoint state helpers.

    **TODO before implementation:** user to provide a sample
    `backup/<label>.json` entry so the JSON field names are confirmed (starred flag,
    URL field name, etc.).
  - **Supernote integration** (e-ink; user has a Manta) — Supernote devices sync via
    their **Supernote Cloud** + a local Wi-Fi "Browse & Access" HTTP file interface;
    there's no official public API, so options are limited. Plausible: export
    saved/starred articles as documents (PDF or `.note`-friendly format) to a folder
    the device picks up (Cloud folder or the device's WebDAV-ish local server). A
    "send to Supernote" destination (like the send-to-destination family) that drops
    a readable PDF of an article. Investigate the local Browse&Access API and whether
    Supernote Cloud has any usable upload endpoint before committing.


## Known limitations (not bugs)

- **Hard JS bot-walls** (e.g. seattletimes — HTTP 202 + empty body) — some feeds sit
  behind a challenge that returns success-with-no-body to *any* non-headless client,
  so even the browser-identity escalation can't fetch them. Lectio escalates on
  refusal (403/415/429/503/timeout) but won't run a headless browser; these feeds
  stay unsubscribable. Surfaced as a "site is blocking automated access" message.
- **Network/IP-level image blocks** (e.g. washingtonstatestandard.com — Cloudflare
  403 on every server request, honest *and* browser UA, persistent over hours) — the
  feed itself fetches, but server-side image ops (the `/thumb` list thumbnails and
  source-page scrape) are blocked at the IP/ASN level. We don't evade IP blocks
  (good-citizen policy). Article lead images render direct to the browser (user's own
  IP), and **list thumbnails now fall back to a direct browser load when `/thumb`
  fails** (`thumbImgFallback`), so they render too. Only the server-side source-page
  *scrape* (e.g. caption sourcing) remains blocked for such hosts.
- **Webcomic single-image feeds** (e.g. claycomix) — investigated: not multi-panel.
  A single `wp-post-image` per entry; the source page's extra `<img>`s are DRM'd
  early-access previews + support badges. The webcomic strategy already surfaces the
  panel. A generic "scrape all panels" feature needs a real multi-panel exemplar to
  design against; revisit if one turns up.

## Backburner

- **selfh.st / paywalled-teaser reader-mode spike** — selfh.st & waynocartoons load
  in Reader view; if Readability already extracts the full article from the page,
  the "paywalled teaser" limitation may be moot. Confirm, then optionally a per-feed
  "open in Reader by default" toggle.
- **Deployment genericization** (after multi-user phases) — make base
  `docker-compose.yml` proxy-agnostic (publish `:8000`, no Traefik labels), move
  Traefik labels to an opt-in overlay; move security headers (HSTS/nosniff/
  frameDeny/referrer) from Traefik into app middleware; make trusted-proxy IPs
  configurable instead of `--forwarded-allow-ips=*`. Document Traefik + one
  alternative now; expand later.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy +
  display settings without saving.
- **`ty` type-error backlog** — after upgrading to `ty` 0.0.53 (2026-06-24), 235
  diagnostics surfaced. All appear pre-existing (Pillow `LANCZOS` stub gap,
  `FeverService._synced` dynamic attr, test fake classes, `re.Match` narrowing,
  `base64.binascii` access, etc.). Not blocking; address incrementally.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
