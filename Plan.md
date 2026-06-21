# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now

- (Outbound webhooks / IFTTT / Zapier shipped — `webhook` rule type, generic JSON
  or IFTTT Maker, SSRF-guarded, immediate delivery. Possible follow-ups: batch
  delivery, a Run-Now/test-send button, a Webhooks README badge.)


## Later

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
