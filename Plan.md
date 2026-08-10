# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Roughly ordered: quick/concrete fixes first, then cheap UX wins, then items
that need a decision or go-ahead from Josh before they can move, then
measurement/investigation jobs, then scheduled or genuinely low-urgency
work, then the two standing watch-lists, then the one big multi-session
project last.

### CodeQL board — watch-note

Board is at zero open alerts as of 2026-08-09 (PR #190 closed out the last 5:
4× `py/polynomial-redos`, 1× `py/stack-trace-exposure` — detail in that PR's
history). **If the reflective-XSS class keeps recurring**, the repo already has the pattern
for it: `.github/codeql/queries/` holds guard-aware copies of the SSRF and
path-injection queries that model our audited guards as sanitizer barriers, with
the stock versions excluded in `codeql-config.yml`. A `LectioReflectiveXss.ql`
modeling `html_sanitize.sanitize_html` / `sanitize_inline_title` as barriers would
end the hand-dismissals. Not built yet — two dismissals is not yet a pattern, and
excluding stock `py/reflective-xss` repo-wide is a heavier trade than excluding
`py/full-ssrf` was.

### Small daily-friction items (cheap; slot between the bigger pieces)

- **No way to reach a feed in Feeds from a post in Saved** (2026-08-04). Saved
  is where you notice a feed misbehaving; Feeds is where you fix it, and there
  is no route between them. Both feed-name links (post list and entry pane)
  deliberately carry `star_only` through, so they navigate *within* Saved —
  verified: clicking a post's feed name twice yields the identical URL both
  times. With ~2,900 feeds, "switch tabs and find it" is not an answer. Josh
  remembered a second click jumping to Feeds; that behaviour is not in the code
  — most likely what he saw was the SPA expanding the feed's containing folder
  in the sidebar tree ([index.html:1133](templates/index.html#L1133)), which
  looks like a scope switch. Suggested: an **"Open in Feeds"** entry in the post
  right-click menu, which already holds feed-scoped actions (*Mark Feed as
  Read*, *Move to feed…*) but nothing that navigates. Cheap, and discoverable
  where the other feed actions already are.
- **Set up the four verified firehose tag_filter rules** — config, not code; the
  engine already ships. Vocabularies verified 2026-07-21, see "Tag filtering for
  firehose feeds" in Later for the per-feed data and suggested rule shapes.
  **The rule form now autocompletes the tag list from each feed's own captured
  vocabulary with post counts (above), so this is now typing four short specs
  against a visible list rather than against a guess.** Still Josh's call: which
  tags to drop is a taste judgement, not a derivable one.
- **Dead code sweep** (promoted from "Code health" in Later, 2026-07-21 finds)
  — delete-the-unused-thing, no design work: `server_posts_total` /
  `server_posts_sent` (read in `templates/index.html` with `is defined`
  guards but never set anywhere in Python); the orphaned
  `templates/js/_layout_shell.js` / `_pull_to_refresh.js` (unreferenced
  leftovers from an earlier extraction attempt — confirm nothing external
  uses them first); the dead `LECTIO_SECURITY_MODE` line in
  `scripts/refresh_screenshots.py` (nothing in the app reads it since auth
  became unconditional). Do all three together in one pass.

### Batch-align Uncategorized saved items into Feeds (promoted from Later)

Bulk assignment with auto-match by domain, instead of one-at-a-time.
Distinct from `scripts/categorize_uncategorized.py` (that's orphan
*feeds*; this is saved *articles*, and should be in-app).

### Saved dedup workflow — repeat-session polish

The correctness and safety work is done (2026-07-21) and **the scan currently
returns nothing: 0 confirmed, 0 possible.** What is left is cosmetic except for
one item:

- **"Not duplicates"** — persistent per-pair suppression so a rejected group
  stops reappearing. Needs a new meta-DB table, so it also needs the startup
  per-user migration or existing tenants 500. **Demoted:** inline title editing
  dissolves *title*-matched false positives outright (correcting the title
  removes the only signal binding the group), so build this when a **body**-
  matched false positive actually shows up — that is the case a title edit
  cannot fix.
- **Red 404 status**, **collapsible Confirmed/Possible sections**, **resizable
  dialog** — cheap, all in the same dialog, do them in one pass.

### Find duplicate feeds by title — 32 groups, 72 feeds

The scheme-insensitive grouping shipped 2026-08-08 catches URL variants of one
address. It cannot catch **the same publication subscribed under two different
addresses**, which is the case that actually recurs: two Webtoons `title_no`
values for one comic, a Tumblr and a Tapas copy of Cryptid Club. The entry-level
scans cannot reach it either: the two Sarah's Scribbles Webtoons feeds shared
**zero** titles and **zero** links, because the second only ever had one episode
and it was not in the other's window. Measured across 2,886 feeds, feed **title**
is the only signal that finds them:

| signal | groups | feeds | verdict |
|---|---:|---:|---|
| same host + path, differing query | 10 | 740 | useless — nearly all YouTube `videos.xml?channel_id=…` |
| **same feed title** | **32** | **72** | a real, reviewable list |

Mostly genuine: `sarah's scribbles ×3`, `cryptid club ×2`, `fantasyanime ×3`,
`nine inch nails ×3`, plus 15 same-host pairs. Needs a generic-title floor —
`news ×7` is seven unrelated sites — and it stays advisory, because a same-title
pair can legitimately be a site's blog and its podcast. Nothing pre-checked, per
the usual rule. A third tier in the Dupes tab.

### "Filter this view" — ready to build (decision confirmed 2026-08-09)

**Decision confirmed:** **(c)** server-side move + **(a)** local instant-feedback
filter (see below) — no longer blocked, ready to build. Josh's framing:
**"actual search" vs "filter search"** are different tools. Search is a
server-side query that changes *what is fetched*; a filter narrows *what is
already in front of you*, instantly, so you can then act on the result as a
set. Settings → Feeds already has this pattern
([templates/index.html:1938](templates/index.html#L1938), logic at
[static/js/app.js:10594](static/js/app.js#L10594)); port it to the posts list.
"Move all visible to feed" already exists
([static/js/app.js:7045](static/js/app.js#L7045)) and post rows already carry
`data-post-link`/`data-post-title` ([templates/index.html:629](templates/index.html#L629)),
so the filter itself needs no server change — but the *move* action does.

**Why it's blocked:** the server sends 250 posts on first load
([main.py:16591](main.py#L16591)), then further chunks capped at 2,000. A
client-side filter only spans what's currently in the DOM, so "Move N shown"
on a 1,321-match filter would silently move a fraction of it — the exact
footgun this item exists to avoid. **The existing `Move visible to feed…` has
this bug today** too ([static/js/app.js:7332](static/js/app.js#L7332)):
its claim to cover "whatever survives the active filters" is only true under
250 results. Worth fixing regardless of whether the new filter gets built.

**The three options that were on the table** (kept for context — (c)+(a) won):
- (a) Honest partial: filter what's loaded, label the button with both numbers.
  Cheap but not a whole-set guarantee.
- (b) Load-all then filter: pull all chunks to the 2,000 cap first. Correct
  guarantee, but re-inflates the page weight the last optimization pass cut,
  and still truncates above 2,000.
- **(c) Server-side filter — chosen for the move action.** Pass the filter
  term as a query param, move by *predicate* not by id list. Correct at any
  size, overlaps existing search (acceptable).

**(c)** for the move action, **(a)** for the instant-feedback filter itself —
filter locally for feel, resolve the move server-side against the same
predicate.

**⚠ The footgun — read before implementing.** `post-item-hidden` is already
taken by the scroll-chunking reveal
([static/js/app.js:11491](static/js/app.js#L11491)), and "move all visible"
selects `.posts .post-item` without excluding it. The moment a filter reuses
that same hidden mechanism, filtering to one domain and clicking "move all
visible" would move the *entire unfiltered list* — a silent, bulk mis-file.
So: give the filter its own class (`post-item-filtered`, not
`post-item-hidden`), update the move-visible selector to respect it, and
restate the button copy as "move the N shown" from the same predicate.

**What "visible" means, settled:** move everything matching the active
filters (server-side tag/search/unread/star *plus* the new client-side
filter), regardless of scroll position — not just the rows currently
painted (scroll-chunking is a rendering optimization, not user intent).

**Sequencing vs "Auto-file saved articles":** build this first — roughly a
day, general-purpose, and the manual escape hatch for what auto-filing can't
resolve. Don't hand-grind the bulk of it by hand; that's what auto-filing is for.

(The dead `server_posts_total` / `server_posts_sent` plumbing noticed while
checking this is filed under "Code health" in Later.)

### Auto-file saved articles — the tail

Built and run 2026-07-21: `lectio:saved` went **4,334 → 424**, and the four big
no-feed hosts are gone from the list. Rationale is in ARCHITECTURE.md ("Saved
articles"). What remains:

- **guitarplayer.com's 303 articles** — the site's own subscription is a
  scraped one-article stub (barred as a target), and probing showed many
  article URLs soft-404. **Decision confirmed 2026-08-09: look for/build a
  real guitarplayer feed** rather than leaving them as one-off saves or
  deleting — worth the investigation despite the soft-404s.
- **The orphaned-star sweep — GO-AHEAD CONFIRMED 2026-08-09.** Delete
  `saved_entries` rows whose entry is gone (4,508 total, 4,264 on
  `lectio:saved`). The cause was found and fixed
  (`backfill_saved_entries_from_archive` re-created them at every startup, and a
  second bug in the same function was starring *tagged* entries), so a sweep now
  stays swept. Cleared to run — bulk delete against live data, but confirmed.
- **166 already-converted stars** — tagged entries starred by that backfill
  before it was fixed. Indistinguishable from a genuine star-and-tag, so they
  cannot be surgically reverted; the unstar-tagged pass is what removes them.

### Characterize the 259 failing feeds

9% of the library errors on every refresh cycle and nobody has looked at the
shape of it. The useful split is dead (host gone, 404/410 forever) vs bot-walled
(403 that a browser identity or a different route might get past) vs moved (a
redirect or a discoverable replacement), because each wants a different action —
unsubscribe, force-subscribe, or Change URL. Two of them turned out to be dead
`tapastic.com` husks duplicating live feeds, which suggests the pile has other
easy wins in it. Nothing here is urgent; it is a measurement job first, and the
measurement is what decides whether any of it is worth automating.

### Cross-feed duplicate scan — the dupes you can actually feel

**RE-MEASURED 2026-07-22 — auto-filing collapsed almost all of this.** Before
auto-filing, all-starred items held ~490 duplicate groups (~520 extra
copies), 447 of them cross-feed (`lectio:saved` ↔ the same article starred
in its real feed). After auto-filing: **65 groups, 87 extra copies** — only
3 remain saved↔real (auto-filing's `_move_entry_to_feed`, matching by GUID
then normalized link, merges those for free), leaving 44 groups that are
genuinely two legitimate subscriptions carrying the same article (a site
plus an aggregator) and 18 same-feed.

**Decision confirmed 2026-08-09: fold it into the existing `/saved/duplicates`
scan, as its own section/tier — not merged into the Confirmed/Possible
groups.** Same shape as the "Find duplicate feeds by title" third tier in
the Dupes tab: a distinct, separately-labeled section so the 44
judgment-call cross-sub groups (which subscription should own the post?)
don't get conflated with the mechanical-dedup Confirmed/Possible tiers. Not
a whole new dedicated surface — 87 copies across 65 groups doesn't justify
that.

**⚠ Guard against homepage-links, if this is ever built.** The raw
measurement found one bogus 244-copy group — `romhacking.net`'s feed uses
the site homepage as every entry's `link` — so any cross-feed scan needs to
ignore bare-domain/homepage links and flag oversized groups for review
rather than presenting them as confident matches (same hazard class as the
pre-armed-delete lesson elsewhere in this doc).

**Also found: 354 orphan star rows** — `saved_entries` holds 4,669 rows for
`lectio:saved` but reader has only 4,334 matching entries. Harmless but
inflates counts; worth a sweep if the orphan-star cleanup above ever runs.

### Finish the Instapaper clone (Read Mode follow-ups)

The read-later app (Save any article, Saved sidebar view, Read Mode at
`GET /read`) and its deferred finishing touches (archived-aware counts,
mark-read-after-last-page, image prefetch, dates/sort/Archive-button
readability, Delete/Archive working on tag-kept items, and the Archive vs.
Delete model — Archive keeps tags/offline capture, Delete releases both) all
shipped 2026-07-28/29. Full rationale in ARCHITECTURE.md. One piece from
that work is not yet safe to use:

**⚠ Settings → Feeds → Utilities → Archive old stars — DO NOT RUN YET.**
The cutoff (7d/30d/90d/6mo/1yr) sorts on `saved_at`, but `saved_at` is not a
real star date for most rows: 6,091 of 10,002 stars carry a `saved_at` in
2026-06, which is when multi-user went live and the migration stamped its
own run date instead of preserving the original — mostly years-old
Inoreader stars wearing a seven-week-old timestamp. A 30-day cutoff would
sweep those 6,091 in and a 90-day cutoff would protect them, neither for
any real reason. **Fix before use: offer the date basis, default to
publish date** (asks the better question anyway — "articles from 2019 I
have still never opened"). Only 419 of 10,002 stars have a genuine
Lectio-made `saved_at`; the rest are either real pre-migration dates
(3,492) or the migration timestamp (6,091).

### Page tag extraction grabs the sentence, not the anchors (2026-07-29)

gottadeal posts carry a real category line on the page:

    Posted on 7/29/26 in Woot!, Pet Supplies

`Woot!` and `Pet Supplies` are genuine categories, but the harvested tag came out
as **"in XXX, YYY"** — the extractor took surrounding sentence text instead of the
two anchor texts. First reported as junk chrome and dismissed as such; Josh
corrected it ("these actually do have tags of sort").

Distinct from the coverage rule shipped the same day: that hides tags a feed puts
on *everything*, whereas this is a per-post tag being read wrongly. Look at
`extract_page_tags`' anchor tiers in `services/feed_tags.py` — the `rel="tag"` /
tag-classed-anchor branches, and whichever path let containing text in.

Example: `https://gottadeal.com/deals/woot-up-to-80-off-petopia-deals-…-475022`

**⚠ Automatic suppression of feed-tag suggestions was tried twice and REVERTED
(2026-07-29). Do not attempt a third heuristic without reading this.**

- *Coverage* (tag on ~90% of a feed's entries) caught `Popular Deals`, `Forum`,
  `VinylDeals`, `LaptopDeals`, talkpython's 8-tag block — 661 pairs. Then Josh:
  "Lessons should be the category". A guitarplayer tag feed puts `Lessons` on every
  post and it is the right filing tag. **Suggestions are for filing, not for
  discriminating within a feed, so uniformity is not disqualifying.**
- *Feed-name echo* (uniform AND tag tokens ⊆ feed-title tokens, camelCase split)
  looked right: it suppressed `Popular Deals`/`VinylDeals` and kept `Lessons`
  against the title "Guitar Player" — **which was an assumed title.** The live one
  is "Latest from Guitar Player in Lessons", so it suppressed `Lessons` too. Feed
  URLs fail the same way: `/r/VinylDeals/` vs `/feeds/tag/lessons`.

`VinylDeals` is a *place*; `Lessons` is a *kind of content*. That is semantic and
no feed metadata expresses it. **A useless chip is ignored; a hidden wanted one is
invisible** — so everything is shown and the user dismisses per (feed, tag).

**Resolution shipped 2026-07-29:** manual per-(feed, tag) dismissal
(`suppressed_feed_tags`, × on each chip, undo at Feed Properties → *Hidden
tags*) instead of a third heuristic.

**More page-tag examples Josh flagged, not yet handled** (2026-07-29). All are
"there IS a usable tag here and we are not taking it", i.e. the same tier work:

- `guitarplayer.com` — a `DEALS` tag on the post is not picked up
  (`?feed_url=…/feeds/tag/lessons`, entry `wu6rVpzS4PyZRihCreDbEF`).
- **Sub-categories from the URL path** — `guitarplayer.com/lessons/advice-tips`
  carries "Lessons" *and* "Advice & Tips" as path segments. A post URL's own path
  is a taxonomy source no current tier reads; it would also give the parent
  category for free on sites that do not link it.

Also still open from the same pass: Real Python's page tag block mixes taxonomies
(`ai` is a topic, `intermediate` is a **skill level**). A four-word stop-list
(`beginner`/`intermediate`/`advanced`/`basics`) would express that where coverage
cannot — it is fixed vocabulary, not per-feed frequency.

### Article cleanup — Phase 2: promote a removal into a per-feed rule

Phase 1 shipped 2026-07-24: the pane's **Clean up article** (🧹) removes elements
by hand and `entry_content_edits` records both the pristine body and the ops that
were replayed over it. The ops are the raw material for Phase 2 — the whole point
of recording them rather than just storing cleaned HTML.

What's left:

- **A `feed_content_rules` table + matcher**, applied inside
  `_apply_feed_content_cleanups` at *render* time. Not a bulk rewrite of stored
  bodies: feed-wide that would touch hundreds of entries irreversibly, while the
  render-time form covers old and new posts alike and un-promoting restores them.
  This is also where the six hand-coded per-site strips should eventually migrate
  to — they are the same thing, hardcoded.
- **A Cleanups section in Feed Properties** listing the selectors recorded from
  that feed's edits, each with a live match count across the feed's entries
  ("matches 47 of 312") so a promotion's blast radius is visible before it
  happens. Nothing pre-checked (see the bulk-action rule).
- **Selector derivation from the recorded fingerprints.** An op stores a
  structural path plus tag/id/classes/text; a *rule* needs the part that
  generalizes — usually `tag.class` — and must refuse to promote an op whose only
  distinguishing signal is its text (that matches one post, not a feed).
- Open question worth measuring before building: how many removals a real feed
  actually repeats. If share widgets and footers dominate, promotion is high
  value; if most cleanups are one-offs, this stays deferred.

**MEASURED 2026-07-28 — stays deferred, and the design needs one change first.**
Corpus is too small (4 edited entries / 67 ops) to measure the repeat rate,
but two findings hold regardless:

- **⚠ `feed_url` is the wrong rule key.** Three of the four edits are on
  `lectio:saved`, a pseudo-feed holding saved articles from everywhere, so a
  `feed_content_rules` row keyed on it would apply one site's strip to
  unrelated sites. **Key on the entry-link host instead**, with the feed as a
  secondary scope for real feeds — also fits the six hand-coded per-site
  strips better, since those are per-*site* already.
- **30% of ops can't be promoted at all** (no id, no class, so `tag.class`
  degenerates to a bare tag matching every `<p>`/`<svg>` on the site). The
  matcher must skip these; a promotion UI has to show "12 of 49 removals are
  promotable" or it will look broken.

Re-measure once there are edits on **≥3 entries of the same real feed** —
that's the shape that would justify building this. Until then Phase 1's
hand-cleanup is doing the job.

### Offline actions — two pieces left

Shipped 2026-08-01 and confirmed on the Supernote 2026-08-02; design rationale is
in ARCHITECTURE.md ("Offline reading and offline acting"). What was left undone:

- **The stale-action guard.** The conflict rule as shipped is plain
  last-writer-wins: a queued action replays over whatever the server now holds.
  "If the server state already moved, accept the server's version" needs a
  per-entry modification time the schema does not carry (`archived_entries` has
  `archived_at`, `saved_entries` has `saved_at`, tags and read state have
  nothing), so it is a schema question, not a client tweak. Low urgency — the
  only conflicting writer is Josh on another device, within minutes. Worth doing
  only if a surprising revert is actually observed.
- **An offline star/unstar.** Scoped in but not built: the reader has no star
  control, only Archive (which unstars) and Delete. Adding one is a UI question
  first, and Read Mode deliberately has few controls.

Deliberately *not* built: a `synced_actions` idempotency table. The four routes
the outbox drives are already idempotent set-state operations, so replaying one
is a no-op; an action-id table would cost a meta-DB schema change plus the
startup per-user migration for no behavioral change.

### Page-weight reduction — follow-ups (main work landed 2026-07-15)

The 12.95MB landing render (2.9k feeds) was cut by lazy-loading the
Settings → Feeds table (5.6MB), the Stale list (3.8MB), and the sidebar
feed rows (2.7MB), and by moving the ~580KB inline script to
`static/js/app.js`. Remaining:

- **Entry-pane loading state/timeout** — slow pane loads still look like dead
  clicks (pending nicety carried over from the 2026-07-15 session).
- *(The orphaned `templates/js/_layout_shell.js` / `_pull_to_refresh.js` deletion
  moved to the dead-code sweep under "Code health" — it's cleanup, not perf.)*
- **Optional**: the pane-swap path still renders the full page server-side per
  fetch (posts + tree + shells, ~200KB now); a render-splitting/fragment
  endpoint for `.pane-posts`/`.pane-entry` would cut server time further.

### Parked, deliberately

Genuinely nothing to do here until one of these recurs or a lead turns up —
not scheduled, just watched.

- **makeuseof re-fetch returns white images.** Seen once during testing
  2026-08-06 and never investigated. Waiting on a second sighting rather than
  hunting it cold — Josh will flag it if it recurs.
- **440 stored feed URLs are non-canonical.** Surfaced by the OPML round-trip
  duplication fix (which canonicalizes both sides of the comparison now, so
  re-importing your own export is a no-op). Why they are non-canonical was never
  chased: they predate the canonicalization or arrive by another route. A
  one-off normalization pass over `folder_feeds` would converge the spellings,
  but nothing is broken by leaving them.
- **A re-fetch that returns a *different unique* article.** The sibling-text
  guard cannot see this shape — entry 26031 came back as a piece about
  sandbox-game rendering, unique text and all. The slug/title mismatch guard is
  what should have caught it and did not. Worth investigating if it recurs.
- **The scheduler's trickle case.** The stall watchdog bounds it but does not
  prevent it: a host feeding one byte per 29s keeps a pass "advancing" per-read
  but not per-feed. If that shows up, the fix is a per-feed wall-clock budget in
  `update_feeds`, not a shorter read deadline.
- **The Wayback timestamp as a date source.** The availability API returns the
  *closest* snapshot, so its timestamp is whatever crawl happened to be nearest.
  A real first-capture date needs the CDX API sorted ascending; probed
  2026-08-04, it worked (what-if/105 → 2014-07-19) but timed out on 2 of 3 tries.
  Worth revisiting only if a cluster shows up with no other date source.
- **Inline SVG in feed content is mangled at ingest.** feedparser parses an
  HTML-escaped `<description>` as HTML, where a trailing slash is meaningless, so
  `<rect/><circle/><path/>` becomes `<rect><circle><path>` — every shape nested
  inside the rect, which cannot contain them. The browser paints the rect and
  drops the rest, so **any feed shipping inline SVG art renders as a flat colour
  block**. Lectio's own sanitizers are innocent; the damage is done before either
  sees it. A real fix means re-parsing SVG subtrees as XML at ingest. The
  screenshot tooling emits explicit end tags to dodge it, which is a workaround
  for the demo, not a fix.

### main.py / index.html breakup — extraction map (2026-08-09, investigation only)

`main.py` is 32,112 lines (266 route handlers) and `index.html` is 3,130
lines; CLAUDE.md calls for a routes/services/storage split main.py has only
partly grown into. Mapped 2026-08-09, not yet started — this is the plan to
execute from, not a same-session change (a file this size needs incremental
extraction with tests between steps, not a rewrite).

**What's already there to build on:** a `services/` package (40+ modules)
already exists and is one-directional by convention
(`services/saved_articles.py:79` documents "services must not import
main"). `index.html` already has two `{% include %}`s and a proven
`data-lazy-src` panel-fetch pattern (`_settings_feeds_folders.html`,
`_settings_feeds_stale.html`, served from `/settings/feeds/panel/{name}`,
main.py:30245) for large optional content. `static/js/app.js` (15,736
lines) was already extracted from an inline `<script>` in the page-weight
work — but a *prior* JS-splitting attempt left dead files
(`templates/js/_layout_shell.js`/`_pull_to_refresh.js`, filed under
"Code health" for deletion): verify any new split is actually wired into a
`<script src>`, not just written and abandoned.

**Landmines to respect:** ~7 module-level `PerUserDict` caches
(`_meta_structure_cache`, `unread_counts_cache`, etc.) plus ~10 more locks
and 19 `global` statements must stay singletons — route modules can import
them but must not redefine them, and every `invalidate_*` call site has to
stay wired to the same instance. `get_reader()`/thread-local pooling and
`lifespan` (main.py:1928) are startup-order-sensitive. The shared rendering
core — `_home_inner`, `list_entries_for_feeds`, `get_entry_detail`,
`build_reader_page` — is reused by `/`, `/read`, pane-swap, and the
greader/fever/v1 compat APIs; it is the entangled core, not a clean
per-feature boundary, and should be its own project, not part of a
mechanical file split.

**Proposed order, safest → riskiest:**
1. Integration OAuth/credential blocks (DeviantArt, YouTube, Pinterest,
   Reddit, Inoreader, Quire) → thin `routes/integrations_*.py` modules —
   self-contained, minimal shared-state coupling.
2. Post-refresh automation pipeline (`_run_*_rules_after_refresh`,
   main.py:7024–7908) → `services/automation_rules.py` — called only from
   refresh routes.
3. `index.html` modals (settings tabs, feed/folder properties, add-feed,
   save-article, context menus — ~20 blocks, main.py:1528–3120) →
   `templates/_*.html`, following the existing lazy-panel pattern for
   anything large.
4. Dedup engine (main.py:5401–6324) → `services/dedup.py` — Plan.md already
   flags this as wanted but deferred until characterization tests exist
   ("Consolidate the dedup routes" under Code health); do this step
   alongside that work, not before it.
5. Route modules by URL prefix (`routes/feeds.py`, `entries.py`, `saved.py`,
   `settings.py`, `admin.py`, `greader.py`, `v1_api.py`, …) — mechanical
   once the caches/locks above are confirmed importable as shared
   singletons (worth a `state.py` module for them first).
6. `ensure_meta_schema` (main.py:3279, ~1040 lines, linear DDL) — already
   flagged low-priority/cosmetic; do last if at all.
7. The shared rendering core — do not attempt as part of a mechanical
   split; treat as its own carefully-tested project.

## Later

### Inoreader replacement — the migration (start ~Dec 2026)

**Scheduled, not urgent**: renewal is 2027-03-16, so starting around Dec 2026 leaves
~3 months to validate before the date. Pulling it earlier buys nothing; the plan is
already paid and won't prorate.

The blocker is **bot-blocking**: feeds Inoreader can fetch but Lectio can't.
Publishers allowlist known aggregators (Inoreader/Feedly) by UA/IP; Lectio fetches
from the VPS IP with an honest UA and gets 403'd (the 🟢 "blocked" bucket in the
Failing Feeds filter — isocpp 752, libhunt newsletters, etc.). Good-citizen policy
forbids spoofing Ino's UA or evading IP blocks; Lectio already auto-escalates to
browser-UA on refusal (`browser_ua_feeds`), which recovers some 403s but not
IP/aggregator-only blocks.

Both steps reuse the **existing** `services/inoreader.py` (OAuth +
`get_subscriptions` + `get_stream_contents`).

**9a — Comparison report** (read-only; start here). Cross-reference Inoreader
subscriptions vs Lectio feeds and flag three sets:

- **(a) in-Ino-with-recent-items but failing-in-Lectio** = the "Ino can, we can't"
  risk set. This is also the **triage list that gates Part C pass 2**, produced
  mechanically instead of by hand, and it names the feeds that need 9b.
- **(b) in Ino, not in Lectio** — subscriptions never migrated.
- **(c) in Lectio, not in Ino** — Lectio-only, safe to ignore for the cutover.

Turns "safe to drop Ino?" into a concrete checklist.

**9b — Inoreader as fetch-proxy.** The step that actually lets Ino lapse, and
legitimate rather than evasion — Ino *is* the subscriber. A per-feed "fetch via
Inoreader" toggle pulling items from `stream/contents` instead of the origin, for
the stubborn bot-walled feeds in set (a). Keep Ino connected as a quiet backend, not
the reader. **Scope depends on how big set (a) turns out to be — run 9a first and let
the count decide whether this is worth building at all.**

Sequence: connect Ino → comparison report (9a) → triage/replace dead feeds → Tag-as-keep
Part C pass 2 (Now) → proxy the only-Ino feeds (9b) → let the plan lapse 2027-03-16
(annual SaaS rarely prorates; worth asking, but plan to ride it out).

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Overlaps
with Inoreader replacement above: some "we can't fetch" feeds get fixed here
instead of via the Ino proxy, so it's worth revisiting once the comparison
report sizes set (a).

### Instapaper-alternative: reader-only view for saved/starred items

The read-it-later app (Save any article, Saved Articles sidebar view, Read
Mode at `GET /read`) shipped 2026-07-09/12 — rationale in ARCHITECTURE.md
("Saved articles", "Read Mode"). Its remaining follow-ups now live under
"Finish the Instapaper clone" in Now.

### Single-post pages as first-class entries (the "feed" that is one document)

Josh has several "feeds" that are really **a single standing document** — e.g.
`https://schacon.github.io/git/everyday.html` (Everyday Git). There's no RSS to
subscribe to, and the content is a reference doc he wants to keep and re-read, not
a stream.

Current workaround (his): save as a Saved Article → create a feed → move the entry
into it. Two things make that unsatisfying, and they're separate problems:

1. **The capture is bad** — that is what the full-page capture mode is for (readability returns 6.7% of this
   particular page, and the wrong node). Fixing raw/full-page save makes the
   workaround *work*, and is the cheap immediate win.
2. **The workflow is a hack** — "save, then manufacture a feed, then move it" is
   three steps to express "track this one page." A first-class **single-page
   subscription** would be: add a URL, get a one-entry feed, optionally re-check
   periodically and bump/re-capture when the page changes (the classic
   page-monitoring feature other readers ship for RSS-less sites). Natural home is
   the existing add-feed/discovery path — when discovery finds no feed, offer
   "track this page" instead of failing.

**Josh's stated preference (2026-07-21) is not a synthetic single-page feed — it's
to file such pages into an existing, at-least-related real feed.** That is what auto-filing does,
which does exactly this in bulk. So the first-class single-page subscription is
mostly *superseded*: build #2's raw-capture fix (makes the content good) and #4's
auto-file (puts it somewhere sensible), then reassess. Only revisit page-monitoring
if the "re-check the page for changes" half turns out to be the actual want — that
part #4 does not cover.

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
tagged-RSS firehoses.

**The four candidate firehoses are VERIFIED (2026-07-21) — all carry
`<category>`, so all four are set-up-able today (config not code):**

| feed | items | cats/item | distinct | vocabulary |
|---|---|---|---|---|
| HackerNoon | 20 | 7.8 | 140 | lowercase-hyphenated slugs |
| GamingOnLinux | 50 | 5.8 | 81 | Title Case, controlled |
| Rock Paper Shotgun | 100 | **13.2** | 298 | Title Case platform/genre |
| PlayStation Blog | 10 | 2.9 | 20 | mostly game/studio names |

Per-feed notes, worth reading before writing rules:

- **Rock Paper Shotgun** is the standout — 13.2 tags/item of genuinely structured
  platform/genre metadata (`PC` 92/100, `Single Player`, `PS5`, `RPG`,
  `Third person`, `Shooter`). Precise include/exclude is easy here.
- **GamingOnLinux** has the cleanest controlled vocabulary (`Steam`, `Proton`,
  `Steam Deck`, `Native Linux`, `Open Source`, `Indie Game`) — small, stable, high
  signal.
- **HackerNoon** has a 140-tag long tail in only 20 items, so *exclude* lists will
  be endless whack-a-mole — use **include** (`++`) mode. Note the editorial marker
  `hackernoon-top-story`, which is a ready-made quality filter.
- **PlayStation Blog** is the weak one: its tags are mostly game titles and studio
  names rather than topics (`PS5` at 9/10 is the only real topical tag). Tag
  filtering buys little; deprioritize or use it only to keep `PS5`.

Multi-word tags are **not** a problem, despite the hyphenation note below:
`parse_tag_filter_spec` ([main.py:5779](main.py#L5779)) splits on **commas, not
spaces**, and `normalize_tag_value` hyphenates to the stored form — so
`+Steam Deck, -Xbox Series X/S` can be typed naturally.

Remaining follow-ups:

- **dev.to adapter** stays API-based (its value is language/reaction
  filtering, not just tags): extend to multiple include tags — one API call
  per include tag, merged + deduped by article id, exclusion applied
  client-side on `tag_list`.
- freeCodeCamp per-tag Ghost RSS (`/news/tag/<slug>/rss/`) remains a fallback
  if include-list recall from the main feed's window is insufficient.

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

**Readit (wereadit.com)** — send-to-Readit is blocked: their
`/api/bookmarklet/save` is unreachable outside their own extension
(Cloudflare challenges both server traffic and the browser CORS preflight).
Revisit only if Readit CORS-enables the endpoint (issue draft handed to
Josh for github.com/mahmoudalwadia/readit-extension). **Import from Readit**
likewise blocked until they expose an export/RSS/API of saves. The reverse
direction works today — Lectio speaks the Readit extension's save protocol
(see ARCHITECTURE "Extension save protocol"), so pointing the extension's
Backend at Lectio is a one-click capture path already.

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

> **Deprioritized 2026-07-21 by the cross-feed measurement (see "Cross-feed duplicate scan").** Fuzzy
> matching was the theory for "there must be more dupes"; the measurement says the
> missing dupes aren't fuzzy, they're **out of scope** — the scan only reads
> `lectio:saved` while the Saved view shows all starred items, and 447 of ~490
> real duplicate groups are cross-feed. Within `lectio:saved` the exact tiers find
> just 5 groups in 4,334 items, so there is little left for fuzzy to catch. Fix the
> scope first (#6, and #4 which collapses most of them), re-measure, and only then
> ask whether fuzzy is worth its false-positive risk.

- **Fuzzy title matching in the Saved scan** — `/saved/duplicates` matches on
  canonical URL/slug (confirmed) and exact normalized title / extracted-body
  prefix (possible). A typo-fixed re-save where the title, URL, *and* body all
  changed slips through; the safe-dedup fuzzy tier (`title_word_similarity`
  ≥ 0.80) would catch it but needs blocking (e.g. rarest-title-word buckets) to
  stay sane at 10k+ saved items. Add only if the exact tiers leave real dupes
  behind after the Instapaper-import cleanup.

### Code health (deferred — low value, no user impact)

**Flaky test seen 2026-07-21:**
`tests/integration/test_youtube_playlist_rules.py::test_add_route_accepts_blank_keyword`
failed once in a full run, then passed in isolation and in two further full
runs, on a commit that touched only `templates/index.html`. Same family as the
earlier flaky-CI work (reader `busy_timeout` + startup-backfill gate) and the
`PytestUnhandledThreadExceptionWarning` noise the suite still emits — a
background thread racing the test's DB. Not chased; note the run if it recurs.

**Dead code sweep, remaining piece** — the three cheapest finds (`server_posts_total`/
`server_posts_sent`, the orphaned `templates/js/_layout_shell.js`/`_pull_to_refresh.js`,
the dead `LECTIO_SECURITY_MODE` line) were promoted to Now ("Small daily-friction
items") since they're zero-risk deletes. One left here, more involved:

- **The dormant in-app star-mode tree/JS** that the Read Mode hijack bypasses —
  see "Finish the Instapaper clone" in Now, which lists it as a Read Mode follow-up.

Other:
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

- **Entries with no date anywhere sort by received time** — an entry with a
  NULL `published`, no dated permalink and no date in its title has no
  publication date to find, so it sorts by when the reader first saw it.
  (Two bugs that used to inflate this bucket — truthy sentinel dates beating
  real fallbacks, and dead URL/title date-inference code — were fixed
  2026-08-04; `entry_publication_date`/`entry_effective_date`/
  `real_published_date` in main.py are the relevant functions.) A one-time
  backfill persisting the inferred date could still be added if the
  ordering of these specific entries ever matters.

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

- **Dead feed-redirector links — automation is exhausted** (investigated
  2026-07-22; recorded so nobody re-investigates). 37 starred redirector links,
  and every automatic recovery path returns zero: no archive rows, no live
  redirect chain (feedproxy.google.com 404s), no Archive.org snapshots.
  `scripts/backfill_canonical_links.py` reports `0/37 recoverable`. A host+slug
  reconstruction tier was considered and rejected — ~13 entries, a verification
  fetch each, on mostly dead 2013-era blogs. Resolved instead by **Edit URL**,
  which covers the 22 opaque ids no heuristic could reach. The loss is only the
  *link*: 36 of 37 still hold their content and read fine.
- **Some pages have no server-side tags to pull** (2026-07-29; do not spend time
  on these). behance.net ships zero `<category>` elements and renders its gallery
  tags in JavaScript; Real Python's Atom feed carries no categories and its page
  is bot-walled. Real Python's page tag block also mixes taxonomies (`ai` is a
  topic, `intermediate` is a skill level) — a four-word stop-list would express
  that where the coverage rule cannot.
- **EEA geo-blocks are not a migration loss** (2026-07-30). Some US local-news
  sites answer **451 Unavailable For Legal Reasons** to any EEA IP — a GDPR
  position by the publisher, not a bot wall. Nothing to work around.
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
