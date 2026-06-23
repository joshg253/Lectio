# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now

Build order (promoted from Later — top first):
1. ~~**Global skip-Shorts toggle** (YouTube area)~~ — ✅ SHIPPED (`yt_hide_shorts_global`).
2. **Auto add to Instapaper** rule — establishes the send-to-destination engine pattern (cheap, reused by later destinations).
3. **YouTube quota meter** — high value after finding durations were eating the quota; per-user spend vs the 10k/day cap with low alerts.
4. **Robust YT-folder identity + YT-area settings panel + connection gating** — the remaining YT-area UI.
5. **On-star → send to destination(s)** — reuses the destination senders from #2.
6. **Bare media links → embedded players** — self-contained content win.

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

  1. **Robust YT-folder identity.** Stop relying on the folder name. Mark the synced
     folder (the one `sync_youtube_folder` populates) as the canonical YouTube area,
     or derive "YT area" = any folder whose feeds are all `is_youtube_feed`. Drives
     where the special settings/automation panel shows up.

  2. **Global "skip Shorts" toggle** — ✅ **SHIPPED.** `yt_hide_shorts_global` setting
     (Integrations toggle, off by default); the hide-shorts pass targets every
     refreshed YouTube feed when on, regardless of the per-feed pref. Area-level
     setting → one source of truth, no drift.

  3. **"Auto add to playlist" automation** — ✅ **SHIPPED.** General `youtube_playlist`
     rule (any feed/folder), extracts all video ids per entry (incl. embedded + Shorts),
     include-Shorts + mark-read options, per-run quota cap, `youtube_playlist_added`
     dedup guard, rule-type gated on a connected account. (Remaining YT-area work is
     items 1, 2, and the quota meter below.)

  4. **Connection gating** — partially done: the `youtube_playlist` rule-type option
     and the per-embed control are gated on `yt_oauth_connected`. Still to do once the
     **YT special-area panel** (items 1–2) exists: gate that panel too and surface a
     single "Connect YouTube account" prompt when not connected. Gate server-side.

  5. **Quota meter — "tokens left" with low alerts.** Show estimated remaining daily
     quota somewhere visible (YT-area header, near the Add-to-playlist controls, and
     in the `youtube_playlist` rule editor), with a warning state when low and a hard
     "exhausted" state.
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

  - **Auto add to Instapaper** (next concrete one): new `instapaper` rule type that,
    on match, calls the existing Instapaper save with the entry URL/title. Gate the
    rule-type option on `is_instapaper_configured()` (same gating pattern as the
    YouTube rule on `yt_oauth_connected`). Cheap (no quota concerns like YouTube).
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

- **"On star, send to destination(s)" — star as a trigger.** Instead of (or
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

- **Convert bare media links into embedded players** — some feeds ship only a
  Bandcamp/Spotify/etc. *link* (`<a href>`), not the embed iframe (e.g. theobelisk.net,
  invisibleoranges.com: Bandcamp album links, 0 iframes in the feed). Detect known
  player links in entry content and convert them to the host's embed iframe (already
  allowlisted), so the player renders. Bandcamp needs the numeric album/track id,
  which isn't in the album URL — scrape the album page's embed `<meta>`/oEmbed once
  and cache it. Helps both the normal article view and Reader view. Larger than the
  Reader-view re-inject (Now) since it needs per-host link→id resolution + caching.
## Later

- **Webhook follow-ups** (shipped: `webhook` rule type + Send-test button): batch/digest
  delivery, a Webhooks README badge.

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
  - **Performance investigation** — systematic baseline before enabling multi-user.
    Per-request breakdown (DB time, enrich time, refresh contention) under realistic
    load. Known hotspot: first-open of an og_scrape feed (e.g. mynorthwest) can take
    several seconds on the **synchronous source-scrape caption fetch**
    (`fetch_entry_image_caption` when source HTML isn't cached) — move it fully off
    the request thread / cache-first like the lead-image fetch.
  - **Shared-content tenancy mode** — one global feed/entry store + per-user overlays
    (read/star/folders/subs). Only worth building at real scale; biggest caching/
    refresh win (single refresh per feed, deduped storage). Umbrella for "a global
    mechanism for all non-private feeds to reduce strain/storage." Pushes unread
    counts to an incrementally-maintained per-user table instead of live scans.
  - **Global WebSub subscriptions** — the callback URL is already global, but
    subscription rows + secrets live per-user, so subscribe/renew POSTs and verify/
    push fanout are duplicated across users. Move to one shared subscription store
    keyed by topic (single secret, single subscribe/renew per feed) + a topic→
    subscribers map for push fan-out. Standalone first step toward shared-content
    mode; needs a migration of the existing per-user rows.
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

- **Integrations to investigate** (ideas; feasibility unconfirmed):
  - **Inoreader import (complete)** — Inoreader's own OPML/"takeout" omits *disabled*
    feeds, tags, and other state. The user maintains
    [InoreaderExportTool](https://github.com/joshg253/InoreaderExportTool): OAuth 2.0
    against the Inoreader API, backing up **tagged items per label** to JSON
    (`backup/<label>.json` cumulative + dated batches; lowercase = tags, Title Case =
    folders by convention). Two ways to leverage it: (a) ingest its JSON output —
    map each label → a Lectio tag and import the items (URL/title/tags) so tags
    survive the move; (b) go further and talk to the Inoreader API directly to also
    recover subscriptions incl. **disabled feeds** + folder structure (OPML misses
    the disabled ones). Likely an importer in `services/` that reads the tool's JSON
    first (lowest effort, already have it), with a direct-API path as a follow-up.
    Decide scope: tags-only vs full (feeds+folders+tags+read/star state).
  - **Supernote integration** (e-ink; user has a Manta) — Supernote devices sync via
    their **Supernote Cloud** + a local Wi-Fi "Browse & Access" HTTP file interface;
    there's no official public API, so options are limited. Plausible: export
    saved/starred articles as documents (PDF or `.note`-friendly format) to a folder
    the device picks up (Cloud folder or the device's WebDAV-ish local server). A
    "send to Supernote" destination (like the send-to-destination family) that drops
    a readable PDF of an article. Investigate the local Browse&Access API and whether
    Supernote Cloud has any usable upload endpoint before committing.
  - **Quire.io integration** — task manager with a documented **OAuth2 REST API**
    (create tasks/projects). Fits the "send to destination" family: a `quire` rule /
    on-star action that creates a Quire task from an entry (title + link + excerpt) in
    a chosen project. Same shape as the planned Instapaper rule — promote a manual
    "send to Quire" into a rule type, gated on a connected Quire account (OAuth).


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
- **Miniflux API compatibility** — Fever and GReader are done. Miniflux is the
  remaining candidate for broader client support (Fluent Reader, ReadKit). Assess
  multi-user requirement and cost first. When adding this (or any new API), revisit
  the README API badge cluster (WebSub / GReader / Fever) to keep it accurate.
- **Fever pre-sync startup race** (cosmetic) — `FeverService` starts its pre-sync
  thread in `__init__` at import, before `lifespan` runs `ensure_meta_schema()`, so
  a brand-new data dir logs one `no such table: fever_entry_map` on first boot
  (harmless). Defer the thread until after schema init, or tolerate the missing table.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy +
  display settings without saving.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
