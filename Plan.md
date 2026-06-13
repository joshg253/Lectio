# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **Multi-user Phase 1 — tenancy seam + per-user connection pools** (`services/tenancy.py`).
  Resolver carries the current user in a contextvar (defaults to `DEFAULT_USER_ID`
  → legacy top-level paths, so single-user is byte-for-byte unchanged and needs no
  migration). `get_reader()` and `get_meta_connection()` are now per-(thread, user)
  pools keyed via the resolver; `get_reader()` is LRU-bounded.
  `get_starred_archive_connection()` resolves per-user; the thumb cache stays
  global. 30 new tests; full suite green (332).

- **Multi-user Phase 2 (core) — users table + per-user auth + request routing**.
  `LECTIO_SECURITY_MODE` (single default | multi). `services/passwords.py`
  (scrypt default / pbkdf2_sha256 / optional argon2; self-describing hashes,
  rehash-on-login). `services/users.py` `UserStore` on global `lectio_auth.sqlite`.
  Bootstrap admin seeded from `LECTIO_ADMIN_USERNAME`/`LECTIO_ADMIN_PASSWORD`
  (defaults admin/ChangeA$ap). `_TenancyMiddleware` binds `session["user_id"]`
  into the tenancy context per request. Single mode is byte-for-byte unchanged.
  33 new tests (incl. subprocess E2E for both modes); full suite green (365).

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
2. ~~**Users table + per-user auth**~~ — DONE (core; see Recently Completed).
   Remaining Phase-2 follow-ups: an account-management UI (create/disable users,
   change password — currently only the bootstrap admin exists and provisioning
   is programmatic), and a logged-in password-change flow.
3. **Per-user API tokens** — Fever/GReader still share one env password and, in
   multi mode, their background pre-sync runs as the default user (reads the
   legacy DBs). Each user needs their own token + per-user routing for these
   protocols. Until then, the Fever/GReader APIs are effectively single-user.
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
5. **Data migration** — move existing single-user DBs into the per-user layout
   (`DATA_DIR/users/{uid}/…`); keep global caches where they are. Always back up
   first (`scripts/backup_databases.py`). Note: in multi mode the default/legacy
   DBs are still schema-initialized at startup but not served to any logged-in
   user until migrated.

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
