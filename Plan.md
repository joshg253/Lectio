# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now

- Webhook follow-ups (shipped: `webhook` rule type + Send-test button): batch/digest
  delivery, a Webhooks README badge.


## Later

- **YouTube as a first-class "special" area** (manual Add-to-playlist already
  shipped in PR #61: per-user OAuth in Settings → Integrations + an "Add to
  playlist" control beneath each embed, via `services/youtube_oauth.py`). Next:
  treat the YouTube folder as a real integration surface with its own settings and
  automations, instead of a folder that merely *starts with* "YouTube".

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

  2. **Global "skip Shorts" toggle** for the YT area — one switch that applies the
     existing `hide_shorts` behavior across all YT feeds (and to feeds added later by
     sync), instead of toggling each feed. **Default: off.** Implement as an
     area-level setting the hide-shorts pass consults, OR as a bulk-apply that sets
     `hide_shorts=1` on every YT feed + on sync of new ones. Prefer the area-level
     setting (one source of truth; no drift when feeds come and go).

  3. **"Auto add to playlist" automation** (the headline ask). A general automation
     rule — **available for any feed/folder, not just the YT area** — because a
     YouTube video can be embedded in any feed's article, and an article can contain
     **multiple** videos.
     - **Choose a playlist** (dropdown from `/api/youtube/playlists`; allow "create
       new").
     - **Scope** like any other rule: a specific feed, a folder, or all feeds (reuse
       the existing rule-scope picker — no YT-only feed selector).
     - Options: **include Shorts** (default off — pairs with the global skip), and
       **mark post read after add** (default on).
     - Engine: new `youtube_playlist` rule type + `_run_youtube_playlist_rules_after
       _refresh`, modeled on the webhook runner. For each new matching entry:
       **extract *all* YouTube video ids from the entry** — not just the entry link.
       For YT-feed entries the link is the watch URL; for general feeds, scan the
       article content for embedded YT iframes / `youtu.be` / `watch?v=` links (reuse
       `services/youtube_embeds.py`, which already pulls video ids from entry HTML).
       Dedupe within the entry, then `playlistItems.insert` each (counts against the
       per-run cap and quota individually) → if "mark read", mark the post once after
       all its videos are added.
     - **UI placement:** appears in the **general Automations rule-builder** as a
       normal rule type (so the earlier "YT-area only" placement is dropped for this
       rule). The YT special area still owns the YT-only settings — sync + global
       skip-Shorts (items 1–2) — but the auto-add rule is general.

  4. **Connection gating.** YT-dependent surfaces only render when the user has
     connected YouTube (`yt_oauth_connected`): the YT special-area panel, the
     per-embed "Add to playlist" control (already shipped), and — in the general
     rule-builder — the **`youtube_playlist` rule type option** (hidden/disabled when
     not connected, so it can't be created without a token). Not connected → these
     don't show; the YT area surfaces a single "Connect YouTube account" prompt. Gate
     server-side, not just CSS.

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
  - **Authenticated/private feeds** — none supported today, so all feed/image content
    is safe to global-cache. If added, exclude those feeds from the global caches.


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
  (good-citizen policy). Article lead images still render: they're served *direct* to
  the browser, which loads them from the user's own IP. Only the server-generated
  list thumbnails are missing. A future graceful fallback could let the list `<img>`
  load the direct image when `/thumb` fails.
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
