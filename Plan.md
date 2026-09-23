# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Five tiers: actively impeding unread-clearing, small independent wins, sized work with no open
decision, real features not blocking anything today, and deliberately-deferred big investments.
Within a tier, related items are clustered under a bold sub-heading. Two watch-lists (CodeQL,
Parked) sit at the end — nothing there is scheduled, just what to check if a symptom recurs.

Tier 1 is empty. Tier 2 is empty — nothing currently qualifies as small *and* fast *and*
independent; the ready items below all take real focused time. Tier 3 holds six items that are
sized, have no outstanding decision, and are ready to pick up (2026-09-22: Josh decided the
archive-capture-failures item, promoting it in from Tier 4's decision list; the tag-filter-chip
scope item stayed on that list pending further discussion; offline star/unstar was decided closed,
not built). The main.py/index.html breakup, the `state.py` singleton extraction, and the full
route-by-URL-prefix split (256 routes across 10 stages) are all done, shipped 2026-09-19 through
2026-09-22 (PRs #329-#342). Tier 4 opens with the remaining items blocked on a product decision,
not on code, plus the `/api/*` cluster split (deferred, undecided) and the shared rendering core
(not started, deliberately).

## Tier 1 — actively impeding unread-clearing

Empty.

## Tier 2 — small, fast, independent wins

Empty.

## Tier 3 — sized, no open decision, ready to build

No outstanding decision blocks any of these — pick up in whatever order suits, ordered here
roughly cheapest-first.

### Tag filtering for firehose feeds — follow-ups

`tag_filter` rule type is shipped (include/exclude feed-tag lists, any scope, auto-mark-read,
dry-run/run-now/history). Remaining:

- dev.to adapter: extend to multiple include tags (one API call per tag, merged/deduped by
  article id, exclusion applied client-side on `tag_list`).
- freeCodeCamp per-tag Ghost RSS (`/news/tag/<slug>/rss/`) as a fallback if include-list recall is
  insufficient.

### Entry-pane loading state/timeout

Slow pane loads still look like dead clicks. Part of the page-weight reduction work (PR #146); the
render-splitting/fragment-endpoint idea from that same follow-up list is bigger and optional, left
in Tier 4.

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only (CMS change, upstream). A per-feed "fetch full content
from the source page at ingest" opt-in (readability pipeline already exists), capped/throttled
like enhancement, would fix such feeds generally. Also unblocks the Email "full article text"
follow-up in Tier 4, which is sequenced after this so both share one "thin" threshold.

### Single-post pages: fix raw/full-page capture quality

Some "feeds" are really one standing document (e.g. a single tutorial page), saved via a
manufactured feed. Readability can return a small fraction of such a page, and the wrong node.
(The workflow-simplification half of this idea is superseded — Josh's preference is filing such
pages into an existing related feed, which auto-filing already does in bulk — so this is capture
quality only.)

### Archive capture failures are indistinguishable from real empty content

`_archive_entry`'s source fetch (`_fetch_text_with_url`) swallows every exception identically —
a 404, a 403, a TLS error, anything — and stores an empty "complete" archive with no error
recorded. Found 2026-09-19 auditing 633 such rows live: mostly dead links, a handful recoverable,
but nothing short of a manual per-entry fetch could tell which was which beforehand. Decided
2026-09-22: add a status column (queryable/reportable later) rather than just logging — needs the
per-user startup migration plus recording the failure kind at capture time; UI surfacing can follow
once the column exists.

### Dedup routes consolidation → `services/dedup.py`

Next concrete step: write characterization tests for the dedup match-method bodies (now
`_dry_run_dedup` in `routes/automation.py`, `_run_now_dedup` in `services/automation_rules.py`) —
dedup correctness is behavior-sensitive, so this needs to happen before touching the preview/apply
logic, not as an afterthought. Once tests land, pull the consolidated engine into
`services/dedup.py`, and fold in `_suppress_guid_churn` and `_cleanup_intra_feed_slug_dupes`
(main.py:7589-7798, refresh-time guid/slug dedup — line numbers drift with every main.py change,
re-grep before trusting them) at the same time — same problem space, avoids moving them twice. The
three unrelated hide-* hygiene functions next to them in main.py (`_is_youtube_short`,
`_apply_hide_shorts`, `_apply_hide_paywalled`, `_apply_hide_members_only`, main.py:7479-7974 minus
the two above) aren't dedup — decide at extraction time whether they're worth carrying along in
the same pass (adjacent code, same refresh-pipeline callers) or splitting off into a later
`services/feed_hygiene.py`.

## Tier 4 — real features, not blocking anything today

### Needs a decision from Josh before these can be built

Everything below is sized or scoped already — each is waiting on one call only Josh can make, not
on more investigation. Once answered, each drops into Tier 2 or 3.

- **Post-header tag-filter chips don't reflect a folder/global-scoped rule** — `get_feed_tag_filter_rule`
  only checks feed-scoped rules, so a feed covered only by a folder-scoped rule shows unlit chips,
  and clicking one forks a brand-new disabled per-feed rule instead of touching the folder rule.
  Partially discussed 2026-09-22: chips governed by a folder/global rule should look visually
  distinct (a different color) from feed-scoped ones, so the governing level is visible before the
  click-behavior question even comes up. The click-behavior decision itself (edit the shared rule
  vs. fork a feed-level override) is still open — talk through with Josh before sizing. Once both
  are settled: a lookup-order change to `get_feed_tag_filter_rule` plus one new branch in
  `toggle_feed_tag_filter`, plus the chip-coloring CSS/markup.
- **Email template overhaul** — Josh wants to revisit the emailed-article template's look. No
  specifics yet — needs his input on what to change before this can be scoped at all.

### main.py / index.html breakup — done

`main.py` went from 40,474 lines to 25,757 across three chained projects (2026-09-19 through
2026-09-22, PRs #329-#342): the Integration routes cluster + automation pipeline + index.html's
last inline context menus moved out first, then all module-level singleton state moved into
`state.py`, then all 256 remaining `@app.*` route handlers moved into `routes/*.py` by URL prefix
(`system`, `compat_{fever,greader,v1}`, `tags`, `highlights`, `automation`, `admin`, `saved`,
`settings`, `feeds` — the biggest at 61 routes, `entries` — 45 routes, `home` — 3 routes but the
riskiest). The shared rendering core (`_home_inner`, `list_entries_for_feeds`, `get_entry_detail`,
`build_reader_page`) deliberately stayed in `main.py` throughout — every route that calls into it
just imports the functions back. Stage-by-stage detail lives in the PR history, not here; the
durable gotchas (copied-reference monkeypatch traps, import-ordering rules, the `global`-vs-accessor
landmine) are in [ARCHITECTURE.md](ARCHITECTURE.md)'s "Route modules" section since they apply to
any future extraction, not just this one.

### `/api/*` cluster split — undecided

Deferred out of the route split rather than decided: `/api/entry-thumb`, `/api/feed-thumb`,
`/api/img`, `/api/favicon` are pure image-proxy concerns (candidate: a small `routes/media.py`);
`/api/save`, `/api/bookmarklet/save` are external save-capture endpoints, natural fit in
`routes/saved.py`; `/api/unread-counts` has no obvious owner yet — decide when picked up.

### Shared rendering core

`_home_inner`, `list_entries_for_feeds`, `get_entry_detail`, and `build_reader_page` are reused by
`/`, `/read`, pane-swap, and the greader/fever/v1 compat APIs. Not a mechanical split like the
route-by-prefix work — its own carefully-tested project if ever undertaken.

### Page-fetch escalation ladder — follow-ups

- Broaden `extract_page_tags`'s recognized markup patterns as new gaps turn up (last full survey
  covered ~622 feeds; ~210 genuinely have no taxonomy, ~107 still blocked, mostly ArtStation's
  JS-heavy tag widget). No more broad surveys needed unless the untagged count grows a lot.
- Persist `HostEscalationState` to a `host_fetch_tiers` table if in-memory proves insufficient.
- Key `_autofetch_failed_hosts` on deepest-available-tier, same fix `HostEscalationState`'s
  cooldown got, if it matters in practice.

### Unify the escalation ladder as one fetch layer in front of every outbound content fetch

Idea from Josh, 2026-09-23. Today the per-host escalation logic (`HostEscalationState`, honest UA →
browser UA → FlareSolverr) is split across at least three places that don't share state cleanly:
`services/page_fetch.py`'s `PageFetcher` (used by `main.py` and `services/lead_images.py`),
`services/feed_discovery.py`'s own separate, older `_get_with_escalation`/`HostEscalationState`
(feed refresh's fetch path), and `main.py`'s `/api/img` proxy, which doesn't escalate at all — it
only *borrows* FlareSolverr-solved cookies from a prior `PageFetcher` solve for the same host
(`page_fetcher.cookies_for_host`), one-way and only when a page fetch happened to solve that host
first. That gap is the likely root cause of the Parked "play.nobleknight.com images 403 despite
FlareSolverr cookie reuse" item — no page fetch ever solved that host, so there's no cookie to
borrow, and the image path has no fallback of its own. `/api/favicon` and thumbnail generation are
unaudited but likely the same shape.

The idea: one shared fetch layer (`PageFetcher`, extended, or a new front for it) that every
content-fetching call site goes through — feed refresh, page/readability fetch, `/api/img`,
`/api/favicon` — keyed by host, sharing one `HostEscalationState`/cooldown table instead of three
copies of the concept. A host escalated for its feed or a page fetch would already be at the right
tier for its images, no separate solve needed. Out of scope: the OAuth/API integrations
(DeviantArt, Reddit, YouTube, Bluesky, etc. in `services/*.py`) — those hit trusted APIs, not
scraped pages, so they don't need anti-block escalation, just their own auth.

Not small and not safe to rush: this sits in front of the two highest-traffic, most
correctness-sensitive fetch paths in the app (feed refresh — see the refresh-scheduler-stall
history — and the image proxy, which is also the one place `url_guard`'s SSRF guarding has to keep
working exactly as it does today). The already-scoped "consolidate `feed_discovery._get_with_escalation`
onto `PageFetcher`" follow-up above is the first slice of this same idea, not a separate item —
do that first as the smaller, lower-risk step, and let it validate the shared-state shape before
extending to images/favicons. Worth a real plan before any of it, same bar as the dedup-combining
idea below.

**Dedup subsystem** — biggest single feature idea on the list.

### Combine cross-feed duplicates instead of marking one read

Dedup's only action today is "mark the newer copy read" — destructive, so the Safe tier requires
body corroboration, which is why it finds nothing in the folders where duplicates actually pile up
(aggregator vs. source, e.g. Hacker News). Combining removes the reason for that strictness: a
duplicate group renders as one entry (primary = richest body), other members appear as an
"Also at:"/"Discussion:" line in the body rather than in the list; marking read marks the group;
splitting restores members.

Matching: combining accepts the current Safe combos plus `{slug,title}` and exact cross-feed title
(slug alone stays excluded — a real false positive exists: two different stories sharing a slug).
Storage: a new meta table for groups (group id, feed_url, entry_id, role), needing the per-user
startup migration; `dedup_false_matches` should feed the splitter.

Open: whether combining runs as an automation rule, an invoked scan, or at ingest; unread counts
and the offline outbox both need to treat a group as one item. Worth a real plan before starting.

### Cross-feed duplicate scan tier — still not worth building

Cross-feed dupes (two legit subscriptions carrying the same article) measure at 23 groups
library-wide — not enough to justify a third `/saved/duplicates` UI section.

### Saved-articles dupe scan follow-ups (deferred)

Fuzzy title matching for `/saved/duplicates` — deprioritized: the actual missing dupes are
cross-feed (out of the scan's scope entirely), not fuzzy-matchable within `lectio:saved`. Add only
if the exact tiers still leave real dupes behind after the cross-feed item above.

### Auto-file saved articles — the tail

- guitarplayer.com's 303 articles: decision made to look for/build a real feed rather than leave
  them as one-off saves.
- 166 already-converted stars (from a since-fixed backfill bug) can't be surgically reverted; the
  unstar-tagged pass removes them.

**Rules engine follow-ups**

### Article cleanup — Phase 2: promote a removal into a per-feed rule

Phase 1 (manual per-article cleanup via the pane's 🧹, with `entry_content_edits` recording both
the pristine body and the replayed ops) is shipped. Phase 2 would add a `feed_content_rules` table
+ render-time matcher, a Cleanups section in Feed Properties showing match counts before promoting,
and selector derivation from the recorded ops.

Measured 2026-09-22: grown to 15 edited entries / 111 ops (from 4/67), spread across 9 feeds —
7 on `lectio:saved`, one each on 8 real feeds (lwn.net, Scott Hanselman, commandlinefu, guitar-pro,
OSNews, Lambgoat, kingcountyweeds, PBS NewsHour). Still not ready: the trigger is ≥3 edited entries
on the *same* real feed, and every real feed still has exactly 1. Any future rule should key on
entry-link host, not `feed_url` (most edits are on the `lectio:saved` pseudo-feed, which spans every
site). Re-check the per-feed distribution periodically rather than assuming the old snapshot holds.

### Refetch-All has no "already re-fetched recently" skip

No column records last-refetch time today (`published`/`first_updated`/`edited_at` all mean
something else). Sizing if ever wanted: small — one new column (needs the per-user startup
migration) + a skip check in the Refetch-All loop. Not requested yet — not scheduled ahead of
demand.

### Read Mode: no Back guard

`/read` has no equivalent of the main app's Back-button guard. Not cheap: `/read` has no drawer
for Back to land on, and a Back that visibly does nothing is worse than one that exits the app.
Give Read Mode a collapsible folder tree first, then add the guard.

### Page-weight reduction — optional follow-up

Render-splitting/fragment endpoint for `.pane-posts`/`.pane-entry` — pane-swap currently
re-renders the full page server-side per fetch (~200KB). Bigger and optional; the other follow-up
from this same PR (#146) list, entry-pane loading state, is sized and ready — see Tier 3.

### Offline actions — stale-action guard

Today's conflict rule is last-writer-wins; "accept the server's version if it already moved" needs
a per-entry modification timestamp the schema doesn't carry. Low urgency (the only conflicting
writer is Josh on another device, within minutes) — do only if a surprising revert is actually
observed. Offline star/unstar, the other piece this project used to carry, is decided closed
(2026-09-22): star stays a desktop-only action, Read Mode's control set stays Archive/Delete.

Deliberately *not* built: a `synced_actions` idempotency table — the four outbox routes are
already idempotent set-state operations, so replaying one is a no-op.

### Email "full article text" doesn't run Readability on thin-stub feeds

The full-text Email Article option only pulls stored content — still a thin email for a
thin-stub feed. meetingcpp.com is the concrete example (see "Full-content fetch at ingest" in
Tier 3). Scope: at send time, if the stored body is thin, run the same readability fetch Save/re-fetch
already uses. Sequence after that item lands so both share one "thin" threshold rather than
inventing two.

### One stored image per entry, but three feeds want two

Three comic feeds want a different image in the list than in the article; Lectio stores one URL
per entry and derives the list crop from it. Two of three needed a plugin/derivation (Penny
Arcade, dresdencodak); the third needed nothing (`media_rss` already picks up the right
publisher-supplied thumbnail) — check what a feed already provides before writing a plugin. General
fix — a second stored URL + a per-feed "thumbnail source" setting — needs the startup migration;
worth doing when a fourth feed wants it, not before.

### FakeFeedz `content_selector` has no edit UI for existing feeds

Shipped 2026-09-19 (link_list body-fill from the entry's own page, bypassing readability/full-page
guessing) but only settable at feed creation in the Add Feed dialog — changing or adding it on an
already-created scraped feed needs a direct DB edit today. Worth a "Page Feed" tab in Feed
Properties if this comes up again; not before.

### Send-to-destination — remaining candidates

Rule engine + on-star fan-out + shared destination senders are shipped (Instapaper, YouTube
playlist, email, Quire, Pinterest). Build more only if actually wanted: save-to-tag/starred-archive
as a rule action, Readwise/Reader, Wallabag. Each is small, reusing the existing engine.

Readit (wereadit.com): send-to-Readit is blocked — their save endpoint is unreachable outside
their own extension (Cloudflare). Import is blocked until they expose an export/API. The reverse
direction (Lectio receiving from the Readit extension's save protocol) already works today.

### Global audio player — deferred v2 ideas

Queue/playlist across a folder, remember position per episode, Media Session API (lock-screen
controls), speed presets.

## Tier 5 — deliberately deferred / big investments

**Architecture**

### Single-user mode does not exist anymore — retire DEFAULT_USER

`DEFAULT_USER_ID` still silently resolves any unbound code path to stale legacy top-level DBs
instead of failing loudly — quietly-wrong answers, not an error. Fix: default the
`lectio_current_user` ContextVar to `None`, raise on unbound resolution, then delete the legacy
path branches and stale DB files. Re-counted 2026-09-22: down to 7 references outside
`tenancy.py`/`tests/` (2 are comments), across 4 files — `main.py` (3), `services/users.py` (1),
`services/starred_archive.py` (1), `scripts/screenshots/seed.py` (1 comment). Smaller than it used
to be (previously scoped at 54); worth re-sizing at pickup time rather than trusting either number.
Wants its own PR and a check of the per-user startup migration. Related: the bg-thread tenancy rule
already in place (`_run_in_user_context`).

### Add OIDC login

No SSO/OIDC exists today (username/password only). Architecture-level addition — new login flow,
session handling alongside the existing one, tenancy binding from an OIDC subject to a Lectio
`user_id`, first-login provisioning. Wants a real plan before code.

### Multiuser

- **Performance investigation** — a systematic per-request baseline (DB time, enrich time, refresh
  contention) under realistic load.
- **Shared-content tenancy mode** — one global feed/entry store plus per-user overlays
  (read/star/folders/subs): one refresh per feed regardless of subscriber count, deduped storage,
  incrementally-maintained unread counts. Biggest caching/refresh win, but only worth building at
  real scale — the current per-user-fetch model is fine for 1–3 trusted users.
- **Per-user resource fairness** — rate limits on refresh, scraping, thumb generation. Not needed
  for trusted users; hooks are in the seam.
- **Write-abuse protection (read-state spam).** Every read toggle invalidates the counts cache and
  forces a recompute, so flip-flopping hammers the shared SQLite. Defenses cheapest → strongest:
  coalesce rapid toggles on one entry; throttle the counts recompute per user; a per-user token
  bucket on state-changing endpoints (429). ⚠ Tune so legitimate fast keyboard triage never trips
  it.

### Lectio browser extension (fork of readit-extension)

Deliberately deprioritized below the Now chain — a fork is a new codebase and a real commitment,
pick up only when ready to invest. Value order:

1. **Visibility-aware capture.** The stock extension serializes the raw DOM, including everything
   the live page merely *hides* (uBlock cosmetic filters, dismissed cookie walls). Walking the DOM
   and dropping hidden/zero-size nodes before POSTing would make "what you see is exactly what
   saves" true, ending a whole class of server-side widget whack-a-mole.
2. **Deliver tags already extracted from bot-walled pages** (ArtStation and one other site show
   per-post tags on the page but ship none in the feed, and the server can't reach either page).
   The extractor (`extract_page_tags`) is done — only delivery is missing: POST the open page's DOM
   to a route. Trigger on Keep, not per entry. (Cookie harvesting was considered and rejected:
   Cloudflare's `cf_clearance` is IP+UA-bound, so a harvested cookie doesn't work from the VPS
   either way.)
3. **Dual-extension use** — one browser running save-to-Readit and save-to-Lectio side by side.
4. Nice-to-haves once forked: badge feedback distinguishing saved/duplicate/refreshed, prefilled
   Backend, username+API-token auth.

Keep the wire protocol unchanged (`/api/bookmarklet/save`) so the stock extension keeps working.

### Backfill older posts from a URL pattern

A feed shows only the publisher's recent window; the back catalogue is often still reachable via
predictable URLs. Open decisions before any code:

- **Dates** — don't synthesize "now" for an undated entry (floods the Inbox top); mine the date
  from the page or skip the entry.
- **Identity** — dedupe against both what's already there and what was deliberately deleted, or an
  import resurrects discarded entries.
- **Where it stops** — a user-set bound (N pages / back to a date) with a dry run reporting the
  count first.
- **Rate** — must go through `refetch_batch.run_paced`, not a new loop ([[good-web-citizen]]
  applies at import too).

Fits the existing adapter shape: a per-feed pattern (stored, not hardcoded — see `image_size_rule`
for the precedent) plus a paced walker. Worth a real plan before any code.

### Code health (deferred — low value, no user impact)

- **Flaky test:** `test_add_route_accepts_blank_keyword` failed once, unreproduced since — likely
  a background thread racing the test DB. Not chased.
- **Dead code:** the dormant in-app star-mode tree/JS the Read Mode hijack bypasses.
- **`ruff format`'s opt-in `docstring-code-format`** is inert until (if) `ruff format` itself gets
  adopted project-wide — that's the real decision, weighed against whatever the original reason
  was for leaving formatting unenforced (most likely: noise-free diffs). If adopted: one isolated
  reformat commit, hash added to `.git-blame-ignore-revs`.
- **Wrap saved-dedup storage access** (Sourcery) — the Saved duplicate scan reads reader's entries
  table directly; a thin storage-layer wrapper would localize breakage if reader's schema evolves.
- **Consolidate the dedup routes** — see "Dedup routes consolidation" in Tier 3; tracked there
  since it also gates a `services/dedup.py` extraction, not just this cleanup.
- **`ensure_meta_schema`** (main.py:3626, ~1,332 lines) — long but linear (CREATE + idempotent
  ALTERs), low churn. A by-area split is cosmetic.
- **Backfill Sphinx-math height on already-stored entries** — the ingest-time fix doesn't
  retroactively help entries stored before it; low value (few math articles), do on demand. Note:
  `entries.content` is reader's JSON structure, not raw HTML — a backfill must respect that shape.

## Watch-lists

Nothing here is scheduled — just what to check if a related symptom recurs.

### CodeQL board

9 open alerts from PR #329 (the main.py/index.html breakup, Step 1): "information exposure through
an exception" on `return JSONResponse({"error": str(exc)}, ...)` in the moved
`routes/integrations_*.py` files. Not new — the same pattern (`"error": str(exc)` in an exception
handler) already exists 15+ times in main.py; CodeQL flags them because the lines are new *files*,
not new *code*. Left open rather than dismissed or fixed inline (decided when triaging the PR); a
real fix means picking a message for each call site that's still useful in the UI (several surface
the caught exception directly, e.g. "Sync failed: {result['error']}"), so it's its own pass across
every instance — including the ones still in main.py, not just the 9 CodeQL happened to flag — not
scope for this refactor. Notes for next time on other alert classes:

- A negative lookahead will not clear a ReDoS alert — CodeQL's regex model ignores lookaheads.
  Write the loop lookahead-free or move the scan into Python.
- Committed page fixtures are excluded from analysis (`paths-ignore: tests/fixtures`) — a captured
  page is byte-for-byte what a site served, so analyzing it reports the remote site's choices as
  ours.
- If the reflective-XSS class keeps recurring, `.github/codeql/queries/` already has the pattern
  for a guard-aware custom query (see the SSRF/path-injection ones modeling our sanitizers as
  barriers).

25 more alerts (3 flagged "high") surfaced on PR #340 (route-split Stage 8, `routes/feeds.py`) —
same "new file, not new code" attribution as above, verified individually rather than assumed:
the polynomial-regex pair (`services/saved_articles.py:221/224`) and the reflected-XSS one
(`main.py:20916`) are both years-old code untouched by this PR (confirmed via `git log -S`/`-L`),
just re-attributed because a large file-restructuring diff shifts line numbers CodeQL's PR-diff
heuristic uses to associate an alert with "changed" code. The other 22 (`routes/feeds.py`'s own
"URL redirection from remote source"/"information exposure through an exception") are the same
`RedirectResponse(url=f"/?...")`-with-query-params and `str(exc)` patterns used everywhere else in
this app, newly visible because they're now in a new file. Not fixed as part of the route split
for the same reason as the PR #329 batch — left open.

Re-checked 2026-09-22, after all 10 route-split stages: 48 open alerts total (28 `py/url-redirection`,
20 `py/stack-trace-exposure`), now spread across every stage's output file, not just the two above —
`routes/feeds.py` (19), `routes/integrations_*.py` (8, one dismissed/fixed since the PR #329 count of
9), `routes/entries.py` (6), `routes/system.py` (6), `routes/automation.py` (4), `routes/settings.py`
(2). Same "new file, not new code" attribution expected to hold for the later stages too (not
individually re-verified per-file the way PR #329/#340 were) — no reason to expect otherwise, since
every stage used the same mechanical move. Still left open for the same reason: a real fix is one
pass picking a useful message per call site, across the whole app, not scoped to any one refactor.

### Feed-tag suggestion suppression — do not attempt a third heuristic

Tried twice, reverted both times: coverage-based suppression wrongly caught legitimate filing tags
(a feed can put one tag on every post and still have that be the right tag); feed-name-echo broke
on the real, non-assumed feed title. The distinction (a tag naming a *place* vs. a *kind of
content*) is semantic and no feed metadata expresses it. Resolved with manual per-(feed, tag)
dismissal instead (`suppressed_feed_tags`).

### `_resolve_view_posts` enrichment cost — measured, not worth building

Enrichment is ~24% of a whole-view resolve, not the dominant cost (the raw fetch is >2.5x larger);
a light-record mode isn't a clean drop-in since `select-all-visible` needs enrichment-phase fields.
Not worth building for the payoff.

### og_scrape feeds with no og:image at all

162 of 585 auto-detected `og_scrape` feeds have no `og:image` on their source page and correctly
fall back to a body image. Not broken — just where a future "odd body image" report will come from.

### Free-threaded Python is blocked by lxml

lxml hasn't declared free-threading support, forcing the GIL back on at import. Recheck when it
does; nothing else blocks it.

### Methodology: diagnosing refresh-contention/latency stalls

Elapsed-time SQL timing can't distinguish SQLite lock-wait from GIL starvation — live `py-spy`
sampling during a real stall settles it faster. Reach for this first, not last.

### Parked, deliberately

Nothing to do here until one of these recurs or a lead turns up.

- Sort briefly reverts to default after a backgrounded tab is restored, self-corrects within ~90s
  (Surface/Edge) — traced to a real top-level nav from a stale history entry, not a
  stored-preference bug; no reliable repro yet.
- neowin.net: 20 entries briefly shared one lead image, repaired live — likely a transient
  upstream glitch, no code-level cause found.
- On-screen-keyboard popup scrolls the post list to top — not reproduced in Playwright with
  viewport-resize emulation; needs a real-device screen recording.
- An entry ([play.nobleknight.com/?p=19266](https://play.nobleknight.com/?p=19266)) took a long
  time to open once — inconclusive, no repro caught; check entry-pane response time and `[perf]`
  logs if it recurs.
- Soundslice tab-player embeds are permanently blocked by the content owner's own domain
  allowlist — no fix available short of screenshotting the original page through a real browser;
  not worth it for one narrow case.
- `make rebuild` cycle is 100-130s — not investigated; revisit if it keeps coming up.
- 5 tests fail locally with `outbound network blocked in tests` (reproduces on a clean commit too,
  smells like sandbox-specific networking) — check real CI is clean before spending time on it.
- makeuseof re-fetch returns white images — seen once, waiting for a second sighting.
- play.nobleknight.com images 403 despite FlareSolverr cookie reuse — the solve never returns the
  actual `cf_clearance` cookie for this host; would need per-image browser routing. Likely explained
  by the fetch-unification idea in Tier 4 ("Unify the escalation ladder...") — no page fetch solved
  this host first, so there's no cookie to borrow and the image path has no escalation of its own.
- guitarworld.com lessons: a MatchMySound practice widget is entirely missing from capture
  (likely a client-side paywall gate defeated by Josh's own adblocker) — one article, not chased
  further.
- A second raw-Markdown-instead-of-HTML feed — unconfirmed, Josh recalls a similar symptom but
  couldn't place which feed. The existing fix (from the blog.gitea.com case) already covers it
  generically if confirmed.
- ~407 stored feed URLs differ from canonical only by a trailing slash — harmless, zero actual
  collisions.
- A re-fetch once returned a different, unique article and the sibling-text guard didn't catch it
  (entry 26031) — the slug/title mismatch guard should have and didn't. Investigate if it recurs.
- The scheduler's trickle case: a host feeding one byte per 29s could keep a refresh pass
  "advancing" without real progress. If it recurs, the fix is a per-feed wall-clock budget, not a
  shorter read deadline.
- The Wayback timestamp as a date source returns the closest snapshot, not first-capture — the CDX
  API sorted ascending would be more accurate but timed out on 2/3 tries.
- Inline SVG in feed content is mangled at ingest (feedparser mishandles self-closing tags) — any
  feed shipping inline SVG art renders as a flat color block. Real fix needs re-parsing SVG
  subtrees as XML at ingest.
- Article-nav post-swap binder exception — mitigated (no more hard-reload on failure), but the
  underlying binder exception still exists somewhere. Grab the console error if it recurs.
