# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

## Up next

### Multi-user — tenancy seam + isolated mode

Design decision (see ARCHITECTURE.md "Multi-user tenancy"): tenancy is a
storage-layer strategy behind a resolver, so routes/services never learn which
mode is active. Ship the **isolated** mode now (DB-per-user); keep
content-addressed caches (thumbnails, image proxy, lead-image/strategy results)
**global** because they hold no per-user data. Defer the **shared-content** mode
until/unless real scale arrives — it becomes a storage swap behind the same
resolver, not a route rewrite.

Target scale now: 1–3 trusted users behind Cloudflare. Build the software to be
secure regardless; defer the SaaS-scale defenses (quotas, abuse) behind hooks.

Phasing:

1. ~~**Tenancy resolver + per-user connection pool**~~ — DONE (see Recently
   Completed). `services/tenancy.py` + per-(thread, user) pools in main.py.
2. ~~**Users table + per-user auth**~~ — DONE (core + account/admin UI at
   `/account`, linked from the main menu in multi mode). Remaining: optional
   user deletion (today: disable).
3. ~~**Per-user API tokens**~~ + ~~**per-user scheduled refresh**~~ — DONE.
   ~~**Startup backfills + starred-archive worker as default user**~~ — DONE.
   The startup tasks (scraped-feed sync, auto-taggers, guid-churn dedup, and the
   YouTube / lead-image / starred-archive / read-history backfills) now run once
   per enabled user via `_for_each_background_user`; the long-lived
   starred-archive worker scans every user's archive DB under its own context
   (injected `background_user_ids`); and the discover-on-subscribe thread spawned
   when a feed is added re-binds the requesting user. `LeadImageService`'s
   render-path source-image / alt-text fetch threads (`queue_source_fetch`,
   `queue_source_html_fetch`) likewise capture and re-bind the requesting user
   instead of persisting to the default tenant. Previously these ran as
   `DEFAULT_USER_ID` and wrote the legacy top-level DBs. New tests in
   `tests/services/test_starred_archive_tenancy.py` and
   `tests/services/test_lead_images_tenancy.py`.
   Remaining background work still running as the default user only (lower
   priority; scheduled refresh covers the feeds within the cadence window):
   - **WebSub push callback** — a push carries only a topic (feed URL); needs to
     find which users subscribe to it (across per-user `websub_subscriptions`)
     and refresh each. Until then a push refreshes only the default user; other
     users still get the content on their next scheduled pass.
   - **Update scheduling policy** — revisit cadence/fairness across many users
     (currently each user is processed sequentially every poll tick).
4. ~~**SSRF hardening**~~ — DONE for the two directly-reachable proxies.
   `url_guard.safe_get` / `safe_get_async` follow redirects manually and
   re-validate every hop with `is_safe_outbound_url`; `/api/img` (auth-exempt!)
   and `/thumb` now use them with `follow_redirects=False`, closing the
   redirect-to-internal bypass. 18 new tests. Remaining hardening: (a) the
   service-layer fetches that still pass `follow_redirects=True` (lead-image /
   scraper / source-proxy in main.py + services) should adopt the same helpers;
   (b) full DNS-rebind closure needs connection IP-pinning (the validate→connect
   TOCTOU window is now small but nonzero) — deferred as lower-priority for the
   trusted-user threat model.
5. ~~**Data migration**~~ — DONE. `scripts/migrate_to_multiuser.py` copies the
   legacy DBs into `DATA_DIR/users/<user_id>/` (user_id resolved from the auth
   DB), dry-run default, reversible, integrity-checked. `--apply` run on the real
   data; **multi-user live since 2026-06-14** (see `docs/multiuser-migration.md`).

### Later

- **Shared-content tenancy mode** — one global feed/entry store + per-user
  overlays (read/star/folders/subs). Only worth building at real scale; biggest
  caching/refresh win (single refresh per feed, deduped storage). Pushes unread
  counts to an incrementally-maintained per-user table instead of live scans.
- **Per-user resource fairness** — rate-limits/quotas on refresh, scraping, and
  thumb generation. Not needed for trusted users; leave hooks in the seam.
- **Authenticated/private feeds** — none supported today, so all feed/image
  content is safe to global-cache. If added, exclude those feeds from the global
  caches.
- **Miniflux API compatibility** — Fever and GReader are done. Miniflux API is the remaining candidate for broader client support (e.g. Fluent Reader, ReadKit). Assess multi-user requirement and implementation cost before committing.
- **Performance investigation** — systematic baseline before enabling multi-user. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load.

## Backburner

- **Deployment genericization (minimal, after multi-user phases)** — the app is
  already proxy-agnostic; the coupling is in packaging. Decided scope: make the
  base `docker-compose.yml` proxy-agnostic (publish `:8000`, no Traefik labels),
  move today's Traefik labels into an opt-in overlay; move the security headers
  (HSTS/nosniff/frameDeny/referrer) from Traefik into an app middleware so they
  hold regardless of proxy; make trusted-proxy IPs configurable instead of
  `--forwarded-allow-ips=*`. Document Traefik + one alternative now; expand
  (Caddy/nginx/Cloudflare Tunnel/bare) later.
- **Fever pre-sync startup race (pre-existing, cosmetic)** — `FeverService`
  starts its pre-sync thread in `__init__` at import, before `lifespan` runs
  `ensure_meta_schema()`, so a brand-new data dir logs one
  `no such table: fever_entry_map` on first boot (harmless; next sync succeeds).
  Fix by deferring the pre-sync thread until after schema init, or tolerating the
  missing table.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy + display settings without saving.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
