# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now (priority order)

Two independent clocks drive this list, and they are not the same clock:

- **The Inoreader chain (#2 → #4) is important but not urgent-today.** The annual
  plan is already paid and won't prorate, so the value lands at *renewal*, not now.
  Its real deadline is "renewal date minus enough time to validate the replacement."
  **If renewal is close, #2 jumps to the front** — that date is the single input
  that reorders this list.
- **The saved-dupe bugs (#1) are compounding and cheap.** Every day they go unfixed,
  more duplicate entries accumulate at save time, and the dupe dialog keeps arriving
  with deletes pre-armed. Hours of work, not days.

Because #1 is hours and the chain is days, they don't really compete — do #1 first,
then start the chain. Everything after #4 is genuinely deferrable.

### 1. Saved-dupe correctness + safety (small; do first)

Two bugs that belong in **one** change, because fixing either alone leaves the
workflow wrong. Full detail under "Saved / Tags / dupe-scan friction" in Later.

- **http/https produce separate saved entries.** `normalize_article_url`
  ([services/saved_articles.py:40](services/saved_articles.py#L40)) preserves the
  scheme and saved entries are keyed by that value, so both variants of one article
  become two entries — new pairs accrue daily. `normalize_entry_link_for_dedupe`
  ([main.py:4920](main.py#L4920)) then also keeps the scheme, so the scan's "same
  URL" tier can't rejoin them. Fix **both** layers (fold scheme, almost certainly
  `www.`), plus a one-off merge for existing pairs — normalization alone stops the
  bleeding but won't heal what's there.
- **The confirmed tier pre-arms deletion.** It renders with `preselect = true`
  ([static/js/app.js:957](static/js/app.js#L957)): row 0 is "keep" and *every other
  copy is pre-checked*, with a one-click "Check All" beside it. Change to
  auto-select **only** 404 items, and select **none** when every item in a group is
  404. (The possible tier already correctly preselects nothing.)

These interlock: today's keeper heuristic claims to "prefer https," but with
http/https variants rarely grouped in the first place that preference seldom
engages — so the pre-armed default is picking winners on a signal that isn't
working. Ship them together.

Also small, same area: **the Saved search button does nothing** — reproduce first
to confirm the surface (Read Mode's form is the prime suspect: no submit button, no
JS, and it drops the selected node on submit).

### 2. Inoreader replacement — comparison report (the last blocker to dropping Ino)

Josh is close to fully replacing Inoreader with Lectio. The remaining concern is
**bot-blocking**: feeds Inoreader can fetch but Lectio can't. Publishers allowlist
known aggregators (Inoreader/Feedly) by UA/IP; Lectio fetches from the VPS IP with
an honest UA and gets 403'd (the 🟢 "blocked" bucket in the Failing Feeds filter —
isocpp 752, libhunt newsletters, etc.). Good-citizen policy forbids spoofing Ino's
UA or evading IP blocks; Lectio already auto-escalates to browser-UA on refusal
(`browser_ua_feeds`), which recovers some 403s but not IP/aggregator-only blocks.

**This step is the comparison report only** (the fetch-proxy is #4). Both reuse the
**existing** `services/inoreader.py` (OAuth + `get_subscriptions` +
`get_stream_contents`).

**Comparison report** (highest-leverage, read-only — START HERE) — cross-reference
the user's Inoreader subscriptions vs Lectio feeds and flag three sets:

- **(a) in-Ino-with-recent-items but failing-in-Lectio** = the "Ino can, we can't"
  risk set. **This set IS the failing-feed triage list that gates #3**, produced
  mechanically instead of by hand, and it names the feeds that need #4.
- **(b) in Ino, not in Lectio** — subscriptions never migrated.
- **(c) in Lectio, not in Ino** — Lectio-only, safe to ignore for the cutover.

Turns "safe to drop Ino?" into a concrete checklist.

Overall migration sequence: connect Ino → run comparison (#2) → triage/replace dead
feeds (#3) → proxy the only-Ino feeds (#4) → let the annual Ino plan lapse (annual
SaaS rarely prorates a refund; worth asking but plan to ride it out).

### 3. Tag-as-keep — Part C write-run (gated on the triage from #2)

The semantics flip shipped (PR #150): tagging keeps + full-archives, archive kept
while starred OR tagged, unified **Kept** view, keep-on-unsubscribe (`kept_feeds`).
The backfill script (`scripts/migrate_tag_as_keep.py`) is **written and committed**,
and its dry-run has run against live data. Dry-run is the *default*; writes are
gated behind `--apply`. The real (write) run is **deferred pending manual
failing-feed triage** (Josh wants to find replacements for dead feeds first). The
comparison report (#2) now feeds that triage: its "Ino can, we can't" set is the
"Needs replacement" worklist, alongside the PR #151 category filter.

**Scope interaction with #1** (checked 2026-07-21, don't re-derive): at the default
`--scope dead-unsub` the saved feed is **not** touched, so the dupe work and Part C
are independent. `_at_risk_feeds` is `kept_feeds ∪ feed_failure_state(failures ≥ N)`,
and `lectio:saved` has updates disabled (so never fails) and is never unsubscribed —
it lands in neither set. **At `--scope all` it does matter**: saved articles are
starred and `curated = tagged | starred`, so duplicate saves would each get
retro-archived — wasted capture you then delete. If a `--scope all` run is ever
planned, do the #1 dedup first.

Two passes (`--scope dead-unsub` default, YouTube always excluded):
1. **Retro-archive** every tagged entry with no `complete` archive row
   (`enqueue_archive`, per-user). Dry-run: **~3,596** dead/unsub candidates
   (~15k across the whole library at `--scope all`).
2. **Wayback backfill** empty curated posts (<300 chars): closest Archive.org
   snapshot → readability-extract → fill reader `entries.content` (JSON shape).
   Dry-run: **~1,101** dead/unsub candidates, concentrated in a few feeds
   (CodeProject 541, etc.). Refine before running: many are newsletters/digests
   (no full article to recover) or 403 bot-walls where the *site* is alive (the
   archive worker's live page-fetch beats Wayback). Order: retro-archive first,
   then Wayback only the DNS-dead residual.

### 4. Inoreader as fetch-proxy (needs the report from #2)

Durable follow-on, and the step that actually lets Ino lapse. Legitimate — Ino *is*
the subscriber, so this is not evasion: a per-feed "fetch via Inoreader" toggle that
pulls items from Ino's `stream/contents` API instead of the origin, for the dozen-ish
stubborn bot-walled feeds surfaced by set (a) of the report. Keep Ino connected as a
quiet backend, not the reader. Scope depends on how big set (a) actually is — run #1
first and let the count decide whether this is worth building at all.

### 5. Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Overlaps
with #2: some "we can't fetch" feeds get fixed here instead of via the Ino proxy.

### 6. Page-weight reduction — follow-ups (main work landed 2026-07-15)

The 12.95MB landing render (2.9k feeds) was cut by lazy-loading the
Settings → Feeds table (5.6MB), the Stale list (3.8MB), and the sidebar
feed rows (2.7MB), and by moving the ~580KB inline script to
`static/js/app.js`. Remaining:

- **Entry-pane loading state/timeout** — slow pane loads still look like dead
  clicks (pending nicety carried over from the 2026-07-15 session).
- **Delete orphaned `templates/js/_layout_shell.js` and
  `templates/js/_pull_to_refresh.js`** — unreferenced leftovers from an
  earlier extraction attempt; confirm nothing external uses them, then drop.
- **Optional**: the pane-swap path still renders the full page server-side per
  fetch (posts + tree + shells, ~200KB now); a render-splitting/fragment
  endpoint for `.pane-posts`/`.pane-entry` would cut server time further.


## Later

### Saved / Tags / dupe-scan friction (reported 2026-07-21)

User-reported friction on already-shipped surfaces. Code pointers verified
2026-07-21.

> **The two bugs below were promoted to Now #1** once the pre-armed-delete behavior
> was confirmed — they're compounding and cheap. They stay documented here in full;
> Now #1 is the summary. Everything else in this section remains deferred.

**Bugs** — *promoted to Now #1*

- **`http://` and `https://` count as different URLs in the Saved dupe scan.**
  Confirmed: `normalize_entry_link_for_dedupe` ([main.py:4920](main.py#L4920))
  strips only the fragment and trailing slash — the scheme survives, so the
  `_canon` ("same URL") tier never matches an http/https pair. They *may* still
  group via the `_slug` tier (`_safe_dedup_entry_slug`,
  [main.py:4996](main.py#L4996)) since that uses only the last path segment, but
  only when the slug clears the length/hyphen guards — so short or dateless
  paths slip through entirely. Fix is to fold the scheme (and almost certainly
  `www.`) into the canonical form. **Note the deeper cause**: `normalize_article_url`
  ([services/saved_articles.py:40](services/saved_articles.py#L40)) also preserves
  the scheme, and saved entries are keyed by that normalized URL — so an http and
  an https save of one article become two *entries* in the first place. Fixing
  only the scan hides the symptom; fixing normalization prevents new pairs but
  does not merge existing ones. Probably want both, plus a one-off merge.
- **Saved search button does nothing.** Needs repro detail on *which* surface.
  The main-app toolbar search (`toolbar-search-btn`) *is* wired
  ([static/js/app.js:12976](static/js/app.js#L12976)). Read Mode's search
  ([templates/read_mode.html:85](templates/read_mode.html#L85)) is a plain GET
  form with **no submit button at all** and no JS — it only submits on Enter, and
  it carries `scope` but not the selected tree node, so a search from inside a
  node also loses that context. Likeliest culprit; confirm before fixing.

**Saved dupe-scan UX** (all in the dupe dialog)

- **"Not duplicates" action** — needs persistent per-pair suppression so a
  rejected group stops reappearing on every scan. New storage; the only item
  here that isn't cosmetic.
- **Collapse the two Confirmed/Possible sections** — collapsible, so a long
  confirmed list doesn't bury the possible tier.
- **Resizable / larger dialog.**
- **More obvious per-item status** — e.g. a 404 rendered in red rather than
  neutral text (URL status already comes from `/saved/duplicates/check-urls`,
  [main.py:22031](main.py#L22031)).
- **Change the auto-select rule** — *promoted to Now #1*. Auto-select *only* 404
  items; if every item in a group is 404, select none (never auto-arm a delete
  that removes the whole group). Current behavior confirmed 2026-07-21: the
  confirmed tier renders with `preselect = true`
  ([static/js/app.js:957](static/js/app.js#L957)), so row 0 is tagged "keep" and
  every other copy arrives **already checked**, with a one-click "Check All"
  beside it. The possible tier already preselects nothing and is fine.

**Saved organization**

- **Batch-align Uncategorized saved items into Feeds** — bulk assignment with
  auto-match by domain, instead of one-at-a-time. Distinct from the existing
  `scripts/categorize_uncategorized.py` orphan-*feed* cleanup: this is about
  saved *articles*, and it should be in-app rather than a script.

**Tags**

- **Autocomplete while typing** — auto-list matching existing tags during tag
  entry. Broader than the deferred rule-form autocomplete noted under "Tag
  filtering for firehose feeds"; if built, do it once as a shared control and
  cover both the rule form and normal per-entry tagging.

### Instapaper-alternative: reader-only view for saved/starred items

Make Lectio usable as a read-it-later app.

- SHIPPED 2026-07-09: **Save any article** (no feed needed) — modal, bookmarklet,
  and token-authenticated `/api/save`; readability capture into the local
  `lectio:saved` feed, auto-star + starred-archive offline capture (see
  ARCHITECTURE "Saved articles"). Note: the starred archive already stores a
  readability-extracted copy + images for every starred entry, so the earlier
  "beef up Star to capture full content" item was largely already covered at the
  archive level; what remains is surfacing it (below).
- SHIPPED 2026-07-09: **Saved Articles sidebar view** — first-class tree row
  (unread-starred badge) opening the whole starred backlog in the familiar
  three-pane layout; the read filter now composes with starred (All / Unread
  narrowing), and the toolbar Tags submenu slices the backlog by tag within
  the view (user pattern: `#toread` vs `#todo` — "read later" vs "deal with
  later" are different buckets under one star).
- SHIPPED 2026-07-12: **Read Mode** (`GET /read`) — a standalone, light-themed
  e-ink reading app for the saved backlog, opened by hijacking the **Saved
  Articles** sidebar row (see ARCHITECTURE "Read Mode"). 2-pane browse (saved
  tree = folders + tag buckets + Archive, pinned) → open an item in the
  paginated reader (CSS columns; tap/swipe/keys, no scroll; `static/reader.{css,js}`)
  → close back to the 2-pane. New **Archive** state on `saved_entries.archived_at`
  (keeps the star, the "done" axis instead of read/unread; Archive node + Search
  reach archived items); the reader header's Archive/Delete(unstar) advance to the
  next item. Follow-ups (build on demand): excise the now-dormant in-app star-mode
  tree/JS that the hijack bypasses; archived-aware node counts (tree counts are
  currently total-saved); mark-read only after the last page; prefetch next
  article to cut e-ink flashes; optional per-image `grayscale(1)`. A possible
  env-gated higher-quality extraction backend (Instapaper's paid Instaparser API,
  evaluated + rejected as third-party/paid) belongs to the "full-content fetch at
  ingest" item below, not here. Two CodeQL alerts on the Read Mode PR (#144) were
  dismissed as false positives: `py/reflective-xss` on `build_reader_page`
  (`article_html` is allowlist-sanitized upstream via `html_sanitize.sanitize_html`
  — the same trust model as the existing reader-view responses; our BeautifulSoup
  sanitizer isn't a CodeQL-recognized sanitizer) and `js/xss-through-dom` on
  `reader.js` `go()` (nav targets are exclusively app-generated same-origin `/read`
  paths, and `go()` further validates same-origin via `new URL()`).
- Save Article follow-up ideas (build on demand): an "archive"
  (unstar-on-read) flow to mimic Instapaper's read/archive split, pinned
  saved-tag shortcuts under the Saved Articles row, badge counting total
  saved instead of unread (if unread proves the wrong default).

### DeviantArt watchlist sync — remaining follow-up

Auto-resume + reconcile SHIPPED 2026-07-08 (see ARCHITECTURE "Watch-list sync
auto-resume"): rate-capped runs schedule a Retry-After-honoring background
continuation (12-round cap, per-user concurrency guard), and artists no longer
watched are surfaced in the status line/logs. Remaining idea: an optional
"unsubscribe unwatched" action (currently report-only by design).

### Tag filtering for firehose feeds — follow-ups

The generic **tag_filter rule** is SHIPPED (rules engine `tag_filter` type;
see ARCHITECTURE "Feed-provided tag suggestions"): include/exclude feed-tag
lists per rule, any scope, auto-mark-read after refresh, dry-run/run-now/
history. Covers MakeUseOf, Lifehacker, How-To-Geek, freeCodeCamp, and other
tagged-RSS firehoses (candidates to set up: HackerNoon, GamingOnLinux, Rock
Paper Shotgun, PlayStation Blog — verify each carries `<category>` tags).
Remaining follow-ups:

- **dev.to adapter** stays API-based (its value is language/reaction
  filtering, not just tags): extend to multiple include tags — one API call
  per include tag, merged + deduped by article id, exclusion applied
  client-side on `tag_list`.
- freeCodeCamp per-tag Ghost RSS (`/news/tag/<slug>/rss/`) remains a fallback
  if include-list recall from the main feed's window is insufficient.
- Multi-word tag entry in rule lists is hyphenated (`windows-11`); consider a
  tag autocomplete in the rule form fed from entry_feed_tags. See also the
  broader "autocomplete while typing" request under "Saved / Tags / dupe-scan
  friction" — build one shared control, not two.

### New subscription missing from feed tree (but posts show)

Investigated 2026-07-08. Ruled out: snapshot-cache staleness (single uvicorn
process; `add_feed_to_folder` invalidates), zero-unread hiding (CSS only dims),
missing URL tooltip (already present on tree feed links). One concrete code
path DID reproduce the symptom and is now FIXED: re-adding a feed that existed
in reader as disabled (`reader.add_feed(exist_ok=True)` keeps its state, and
nothing cleared `disabled_feeds`) left it excluded from the sidebar while its
old entries showed in the posts list — `add_feed_to_folder` now calls
`enable_feed()`. The original Lifehacker repro data is gone (both feeds
unsubscribed), so if the symptom recurs on a genuinely brand-new feed, capture
the sidebar state before navigating away. Remaining UX idea: auto-disambiguate
duplicate display titles (e.g. suffix from the feed URL path) — the tooltip
already shows the URL, but identical titles still invite unsubscribing the
wrong feed.

### Article-nav full refresh (binder follow-up)

- Small lead image: RESOLVED 2026-07-08 — noirlab.edu was fixed by switching
  the feed's image strategy to Artwork in feed properties (no code change
  needed; the default strategy just wasn't upgrading past the feed's thumb).
- Article-nav full refresh: MITIGATED 2026-07-08 — the pane-swap catch-all
  was hard-reloading on any exception in the post-swap binder pipeline even
  though the pane had already rendered (server logs showed /entries/pane
  never fails). The fallback now only fires when the pane truly failed to
  load; post-swap errors are console.error'd instead. FOLLOW-UP: the
  underlying entry-specific binder exception still exists — when it recurs,
  grab the '[lectio] entry-pane post-swap enhancement failed' console error
  to identify and fix the actual binder.

### Global audio player — deferred v2 ideas

Shipped in PR #111 (see git history). Still deferred: queue/playlist of audio
across a folder, remember position per episode, Media Session API (lock-screen /
hardware-key controls), speed presets.

### Uncategorized orphan-feed cleanup — 9 stragglers left (manual)

Live run DONE 2026-07-08: `scripts/categorize_uncategorized.py --propose` +
in-session review + `--apply` foldered 11 of 20 orphans; container restarted.
The 9 still in Uncategorized are dead/one-shot/ambiguous (an Instagram post
URL, a single Vice article, cochaser.com (no entries), WebServicesDir,
whiskypaint/nolanfa tumblrs, norfolkwinters, crispian-jago, owenyoung
myfeed) — sort or unsubscribe manually.


### Send-to-destination — remaining candidates

The rule engine + on-star fan-out + shared destination senders are shipped
(Instapaper auto-rule, YouTube playlist, email, Quire, Pinterest). Only build more
destinations if actually wanted: save-to-tag / starred-archive as a rule action,
future read-later services (Pocket is shutting down; Readwise/Reader, Wallabag if
someone runs one). Each is "manual action → rule type" reusing the existing engine
(own per-run cap, "configured?" gate, run-log entry, not-idempotent guard). Small
per destination.

**Readit (wereadit.com)** — send-to-Readit share button was built 2026-07-09
and **REMOVED 2026-07-10**: their `/api/bookmarklet/save` is unreachable
outside their own extension (Cloudflare challenges server traffic AND the
browser CORS preflight; a no-preflight simple-request fallback verifiably
didn't deliver). No dead controls — revisit as a standard destination only if
Readit CORS-enables/exempts the endpoint (issue draft handed to Josh for
github.com/mahmoudalwadia/readit-extension). **Import from Readit** likewise
blocked until Readit exposes an export/RSS/API of saves.

**Reverse integration SHIPPED 2026-07-10**: Lectio now speaks the Readit
extension's save protocol (`/api/bookmarklet/save`, see ARCHITECTURE
"Extension save protocol") — pointing the extension's Backend at Lectio gives
one-click rendered-DOM capture into Saved Articles (paywalled pages arrive
with full text). Captured-DOM re-saves refresh the stored content and bump
the entry (the clean-the-page-then-resave workflow).

### Lectio browser extension (fork of readit-extension)

**Deliberately deprioritized below the Now chain**, despite item 1 being genuinely
high-value: a fork is a *new codebase* and a real commitment, not a next-up task.
Pick it up when you're ready to invest, not to fill a gap.

Fork github.com/mahmoudalwadia/readit-extension (MIT-style; MV3, vanilla JS,
no build step) into a Lectio-branded extension. Motivations, in value order:

1. **Visibility-aware capture — the killer feature.** The stock extension
   serializes `document.documentElement.outerHTML`, which includes every
   element the live page merely HIDES: uBlock cosmetic filters, site CSS that
   hides player chrome, cookie walls dismissed by stylesheet. Learned live
   2026-07-11: uBlock-hidden junk resurfaced in a captured Melvins article
   ("what I removed came back"), and JWPlayer control DOM needed a
   server-side strip (`_apply_feed_content_cleanups`). A capture that walks
   the DOM and drops nodes with computed `display:none` /
   `visibility:hidden` / zero-size before POSTing makes "what you see is
   exactly what saves" true — uBlock/Aardvark/anything-based cleanups all
   just work, and a whole class of server-side widget whack-a-mole
   disappears.
2. **Dual-extension use**: the stock extension has a single Backend setting —
   a fork lets one browser run save-to-Readit and save-to-Lectio side by
   side.
3. Nice-to-haves once forked: badge feedback distinguishing saved vs
   duplicate vs refreshed (the stock ✓ hides duplicates — confused real use
   2026-07-11); default Backend prefilled from the install instance;
   auth by username+API-token instead of bare token.

Keep the wire protocol unchanged (`/api/bookmarklet/save`) so the stock
extension keeps working too.

### Saved-articles dupe scan follow-ups (deferred)
- **Fuzzy title matching in the Saved scan** — `/saved/duplicates` matches on
  canonical URL/slug (confirmed) and exact normalized title / extracted-body
  prefix (possible). A typo-fixed re-save where the title, URL, *and* body all
  changed slips through; the safe-dedup fuzzy tier (`title_word_similarity`
  ≥ 0.80) would catch it but needs blocking (e.g. rarest-title-word buckets) to
  stay sane at 10k+ saved items. Add only if the exact tiers leave real dupes
  behind after the Instapaper-import cleanup.

### Code health (deferred — low value, no user impact)
- **Centralize schemeless-URL normalization** (Sourcery, PR #148): the
  assume-https logic lives in both the add-feed dialog JS and `/feeds/discover`;
  a shared helper would prevent drift.
- **Wrap saved-dedup storage access** (Sourcery, PR #148): the Saved duplicate
  scan reads reader's entries table directly (JSON content paths, substring
  limits); a thin storage-layer wrapper would localize breakage if reader's
  schema evolves.
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

- **CodeQL: `_safe_next` login redirect will re-flag** — triage completed and
  verified 2026-07-08; the code-scanning board is at **zero open alerts**. The fixes
  merged in PR #114 auto-closed their alerts; the `_safe_next`-guarded login redirect
  re-flagged once post-merge (alert 152) and was dismissed — the stock query can't
  model a validate-and-return-same-string sanitizer. Any future edit near
  `RedirectResponse(url=_safe_next(...))` may re-flag; dismiss with the same
  rationale.

- **Pre-existing date-less entries sort by received time, not true age** — new
  imports backfill a real `published` (Inoreader crawl-time fallback), and the
  Pub-Old/Pub-New window now falls back to `first_updated` so old posts surface
  correctly. But the handful of already-imported entries with a NULL `published`
  (~343 in the live DB) still lack a true publication date; rather than overwrite
  reader's `published` column with import time (worse than the runtime
  URL/title-inferred fallback), they sort by when the reader first saw them. A
  one-time backfill that persists the inferred effective date could be added later
  if the ordering of those specific entries ever matters.

- **Reddit OAuth app registration blocked (access request DENIED 2026-07-19)** —
  Reddit killed free OAuth2 app registration as part of the 2023 API crackdown. The
  Integrations → Reddit panel and all supporting code (`services/reddit.py`, routes,
  scheduler hook, submit button) are fully implemented and will work once credentials
  are available, but Reddit now requires either Devvit (their proprietary in-Reddit
  app platform, not applicable) or a formal API access request — and that request was
  **denied**. The old.reddit.com feed switch remains the practical mitigation for
  429s. Treat native OAuth as closed unless Reddit reopens app registration or reverses
  the denial; do not re-file speculatively.

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
