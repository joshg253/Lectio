# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now

- **List-thumbnail direct fallback for server-blocked images** — feeds whose images
  are IP-blocked server-side (e.g. washingtonstatestandard.com, Cloudflare 403 on
  `/thumb`) show no list thumbnails, though the article lead image loads direct in
  the browser. Let the list `<img>` fall back to the direct image URL when `/thumb`
  fails (the user's own IP can fetch it). Recovers thumbnails without evading the
  block server-side. (`/thumb` itself already hardened: capped timeout + negative
  cache, PR #54.)
- Webhook follow-ups (shipped: `webhook` rule type + Send-test button): batch/digest
  delivery, a Webhooks README badge.


## Later

- **YouTube Add-to-Playlist follow-ups** (shipped: per-user OAuth connect in
  Settings → Integrations + "Add to playlist ▾" beneath each embed, via YouTube
  Data API v3 `playlists.list`/`playlistItems.insert`/`playlists.insert`). Possible
  refinements: cache playlists across pane loads with a TTL/refresh affordance;
  success/error toasts instead of inline status text; batch "add all on page";
  surface remaining daily quota. Note: the Google OAuth app is in **Testing** mode,
  so refresh tokens expire ~7 days → occasional reconnect. Publishing to Production
  (no formal verification needed under the user cap, just clicks through an
  "unverified app" screen) would drop the 7-day churn if it becomes annoying.

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
