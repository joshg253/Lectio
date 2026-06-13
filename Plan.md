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
2. **Users table + per-user auth** — argon2/bcrypt password hashing,
   `session["user_id"]`. Env `LECTIO_USERNAME`/`PASSWORD` demoted to a
   first-admin bootstrap seed.
3. **Per-user API tokens** — Fever/GReader can no longer share one env password
   once there is >1 user; the protocols key everything off it.
4. **SSRF hardening** — pin the validated IP for the actual connection and
   re-check each redirect hop in `/api/img` and `/thumb` (DNS-rebind / redirect
   bypass of the pre-check). Independent; can land anytime.
5. **Data migration** — move existing single-user DBs into the per-user layout
   (`DATA_DIR/users/{uid}/…`); keep global caches where they are. Always back up
   first (`scripts/backup_databases.py`).

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

- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy + display settings without saving.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
