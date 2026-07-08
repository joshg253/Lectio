# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now

### CodeQL — triage DONE 2026-07-07 (verify alerts auto-close after merge)

Full code-scanning triage completed (fix/codeql-triage branch):
- **4 `py/lectio/full-ssrf` — fixed in code.** Webhooks + probe_frameability now
  route through the modeled `ensure_safe_outbound_url` barrier (they were guarded,
  but via the boolean `is_safe_outbound_url` form the query can't see); the
  feed-properties title refetch and YouTube channelId page fetch were genuinely
  unguarded and now use `url_guard.safe_get` (per-hop redirect revalidation).
- **1 real open redirect fixed** — `GET /login?next=` already-logged-in shortcut
  redirected to raw `next`; now sanitized via `_safe_next` like the POST path.
- **Host substring checks hardened** — `is_reddit_feed_url` and the YouTube
  URL helpers (`_is_youtube_host`) now do exact host suffix matching.
- **`actions/missing-workflow-permissions`** — ci.yml given `contents: read`.
- **Dismissed as false positive with per-alert comments:** all
  `py/stack-trace-exposure` (str(exc) messages in JSON errors — no traceback,
  deliberate operator UX), all remaining `py/url-redirection` (fixed local
  `/?...` paths with int-typed ids + quote_plus params, or `_safe_next`-guarded),
  test-only + strategy-dispatch `py/incomplete-url-substring-sanitization`.

**Remaining:** after the branch merges to main, confirm the next CodeQL analysis
auto-closes the code-fixed alerts (141-144, 120, 54/55/57, 8, 1).


## Later

### Dev.to feed migration (manual, after adapter deploy)

The dev.to filtered-feed adapter is SHIPPED (`services/devto.py` — see
ARCHITECTURE.md "dev.to filtered feeds"). Remaining user step: replace the four
existing raw dev.to subscriptions (front page + C++/C#/Python tag feeds) with
filtered adapter feeds via the Add Feed dialog, carrying any starred posts over
(all-or-nothing via the unsubscribe-migration modal today; the per-entry
"Move to feed…" item below would allow cherry-picking).

### Per-entry "Move to feed…" action

Moving starred/tagged entries between feeds is all-or-nothing today (the
unsubscribe-migration modal). Add a per-entry "Move to feed…" action (entry
context menu) reusing that modal's star-archive/tag-carry machinery so individual
saved posts can be cherry-picked to another feed — e.g. when swapping a firehose
feed for a filtered variant.

### Global audio player — deferred v2 ideas

Shipped in PR #111 (see git history). Still deferred: queue/playlist of audio
across a folder, remember position per episode, Media Session API (lock-screen /
hardware-key controls), speed presets.

### Uncategorized orphan-feed cleanup (script built — needs a live run)

The virtual "Uncategorized" folder (negative sentinel id, derived per-render as
`all reader feeds − foldered feeds`) is shipped and pins migration orphans to the
bottom of the sidebar. The cleanup tool `scripts/categorize_uncategorized.py` now
has all three stages: `--propose` (high-precision keyword heuristics → dry-run
CSV), `--review` (sends still-blank rows to Claude via structured outputs — folder
constrained to an enum of real folders, or blank when ambiguous/dead), and
`--apply` (writes approved assignments to `folder_feeds`). Genuinely-ambiguous and
dead feeds are left in Uncategorized for manual sorting. Remaining: run it against
the live data (needs `ANTHROPIC_API_KEY`/`ant` creds and `uv run --with anthropic`
for the review pass), eyeball the reviewed CSV, `--apply`, and restart the
container so the sidebar reflects the new folders.


### Send-to-destination — remaining candidates

The rule engine + on-star fan-out + shared destination senders are shipped
(Instapaper auto-rule, YouTube playlist, email, Quire, Pinterest). Only build more
destinations if actually wanted: save-to-tag / starred-archive as a rule action,
future read-later services (Pocket is shutting down; Readwise/Reader, Wallabag if
someone runs one). Each is "manual action → rule type" reusing the existing engine
(own per-run cap, "configured?" gate, run-log entry, not-idempotent guard). Small
per destination.

### Code health (deferred — low value, no user impact)
- **Consolidate the dedup routes** — PARTIAL. Shared feed-URL prologue extracted
  (`_resolve_dedup_feed_urls`). The match-method bodies (slug/title/both/fuzzy/
  safe) still diverge by preview-vs-apply output; a full shared-core-with-
  `apply:bool` merge is deferred — behavior-sensitive (dedup correctness),
  under-tested, needs broader characterization tests first.
- **`ensure_meta_schema` (~585L)** — long but linear (CREATE + idempotent ALTERs),
  runs once at startup, low churn. A by-area split is cosmetic; low priority.
- **Backfill Sphinx-math height on already-stored entries** — the math
  height/baseline fix (`_promote_math_height`) applies at ingest, so entries stored
  before it keep their flattened math until re-ingested. A one-off that re-fetches
  each Sphinx-math feed and re-sanitizes affected entries would retroactively fix
  them; low value (math articles are few), do on demand. NB: `entries.content` is
  stored as reader JSON (`json.dumps([Content._asdict()])`, i.e.
  `[{"value":html,"type":...,"language":...}]`), **not** raw HTML — a backfill must
  rewrite that structure (or go through reader's API), not overwrite the column with
  a bare HTML string.

### Multiuser
- **Performance investigation** — systematic baseline. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load.
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

## Known limitations (not bugs)

- **Pre-existing date-less entries sort by received time, not true age** — new
  imports backfill a real `published` (Inoreader crawl-time fallback), and the
  Pub-Old/Pub-New window now falls back to `first_updated` so old posts surface
  correctly. But the handful of already-imported entries with a NULL `published`
  (~343 in the live DB) still lack a true publication date; rather than overwrite
  reader's `published` column with import time (worse than the runtime
  URL/title-inferred fallback), they sort by when the reader first saw them. A
  one-time backfill that persists the inferred effective date could be added later
  if the ordering of those specific entries ever matters.

- **Reddit OAuth app registration blocked** — Reddit killed free OAuth2 app
  registration as part of the 2023 API crackdown. The Integrations → Reddit panel
  and all supporting code (`services/reddit.py`, routes, scheduler hook, submit
  button) are fully implemented and will work once credentials are available, but
  Reddit now requires either Devvit (their proprietary in-Reddit app platform, not
  applicable) or a formal API access request. The old.reddit.com feed switch is the
  practical mitigation for 429s in the meantime. Revisit if Reddit reopens app
  registration or the access request is approved.

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

- **Deployment genericization** (after multi-user phases) — make base
  `docker-compose.yml` proxy-agnostic (publish `:8000`, no Traefik labels), move
  Traefik labels to an opt-in overlay; move security headers (HSTS/nosniff/
  frameDeny/referrer) from Traefik into app middleware; make trusted-proxy IPs
  configurable instead of `--forwarded-allow-ips=*`. Document Traefik + one
  alternative now; expand later.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy +
  display settings without saving.
- **Supernote integration** — no confirmed public API. Revisit if the Browse&Access
  HTTP interface proves usable.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
