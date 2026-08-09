# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now
### ⏰ Check on or after 2026-08-13: did the husk feeds come back?

29 article URLs had been subscribed as feeds and were rehomed 2026-08-06. The
Add-Feed door they *could* have come through is now shut (a page that reads fine
but has no feed can no longer be force-subscribed), **but that was never proven
to be the door they used** — `create_feed` already refused that shape, so they
arrived by some other route: an older version, an OPML import, or the extension.

One command settles it:

    uv run python scripts/rehome_article_feeds.py     # dry run, writes nothing

**Zero** means the door is shut and this reminder can be deleted. **Anything
above zero means there is a second door**, and the URLs it reports are the
evidence for finding it — check what they have in common (host, import batch,
whether they carry captures from the extension).

### The "database is locked" CI flake — source two still unexplained

Source one was found and fixed on 2026-08-08: `_queue_media_audio_scan` spawned a
daemon writing the per-user meta DB, and `LECTIO_DISABLE_STARTUP_BACKFILL` only
covered daemons started at *startup*. Now gated — the suite ends `2629 passed`
with no `no such table: feed_media_scan` warning.

**Source two has no explanation.** `test_saved_inbox_chunking` failed at fixture
setup with no thread exception in the log, and it does not reproduce here: that
file passes on repeat and the full suite passes every run. The leaked-handle
theory was measured and **refuted** — 18 leaked SQLite fds with an autouse
teardown, 18 without it, 5 across a full 2,629-test run, nowhere near any limit.
The teardown was reverted rather than shipped, because a no-op carrying an
authoritative comment about a refuted cause is worse than nothing. The same tree
later passed with only a commit *message* changed, which confirms a flake and is
not evidence of a fix.

**Nothing more to do until it fires again**, and when it does it now collects its
own evidence: `pytest_exception_interact` in `tests/conftest.py` dumps live
thread stacks, every open SQLite file, and a `BEGIN IMMEDIATE` probe per DB
saying whether the lock is still held. Costs nothing on a green run.

Ruled out, so don't retry it: `-p no:randomly` is moot — **pytest-randomly is not
installed** and there is no pytest config, so collection order is already
deterministic. Whatever this is, it is timing, not ordering.
### Parked, deliberately

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
### CodeQL board triage

**Alert 184 (`py/reflective-xss`, `/read/offline`) — dismissed 2026-08-04 as a
false positive.** Same class and same rationale as the `build_reader_page`
dismissal on PR #144: everything the route embeds is allowlist-sanitized —
`article_html` on every branch of `resolve_reader_article_html` (archive at
capture, live via `sanitize_readability_html`, stored via `reader_sanitize` at
ingest), `<title>` via `html.escape`, `<h1>` via `sanitize_inline_title`
(escape-then-restore, so no attribute or unknown tag survives a round trip), and
dateline/source/lead-image URLs via `html.escape`. CodeQL flags it because our
BeautifulSoup sanitizer is not a recognized sanitizer, and the taint source is
the `feed_url`/`entry_id` query params that select the row.

**Still open on the board (5), not yet triaged:**

- `py/polynomial-redos` ×4 — [main.py:13882](main.py#L13882),
  [13888](main.py#L13888) (`_LEAD_IMG_OPENER_RE` against stored content),
  [17153](main.py#L17153) (`<[^>]+>` tag-strip in the visible-text measure),
  [17244](main.py#L17244) (the `srcset` strip in `proxy_reader_images`). All four
  run over **feed-supplied HTML**, which is attacker-influenced in the way that
  matters for a ReDoS, so these are worth actually fixing rather than dismissing.
- `py/stack-trace-exposure` — [main.py:21412](main.py#L21412), the webhook test
  route returning `err` to the caller. Admin-only, but the fix (log the detail,
  return a generic message) is smaller than the argument for keeping it.

**If the reflective-XSS class keeps recurring**, the repo already has the pattern
for it: `.github/codeql/queries/` holds guard-aware copies of the SSRF and
path-injection queries that model our audited guards as sanitizer barriers, with
the stock versions excluded in `codeql-config.yml`. A `LectioReflectiveXss.ql`
modeling `html_sanitize.sanitize_html` / `sanitize_inline_title` as barriers would
end the hand-dismissals. Not built yet — two dismissals is not yet a pattern, and
excluding stock `py/reflective-xss` repo-wide is a heavier trade than excluding
`py/full-ssrf` was.

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
### Backups: retention is count-based and size-blind

`scripts/backup_databases.py --keep 5` means five generations of an **8.4GB**
starred archive — ~40GB on a 72GB disk. Two safety backups taken during the
2026-07-22 session took the disk to **98% (1.9GB free)** on their own; deleting
the older set brought it back to 86%. Nothing schedules backups (no cron), so
this only bites when someone runs it repeatedly — which is exactly what a busy
session does.

Wants a size budget rather than a count: `--max-bytes`, or a `--keep` default
that drops to 2 once a source is over a GB. Also worth noting the archive grew
7.2 → 7.9GB overnight capturing 3,581 retro-archive pages, so the number this
is sized against keeps moving.

### "Filter this view" — ⚠ BLOCKED on a decision (see finding 3 below)

**Do not start this as written.** The premise that the client holds the whole
list is false as of 2026-07-22; pick between options (a)/(b)/(c) in finding 3
first. The `post-item-filtered` footgun below is still correct and still applies
whichever option wins.

Josh's framing (2026-07-21) is the right one: **"actual search" vs "filter search"**
are different tools. Search is a server-side query that changes *what is fetched*;
a filter narrows *what is already in front of you*, instantly, so you can then act
on the result as a set. Settings → Feeds already has the filter flavor
([templates/index.html:1938](templates/index.html#L1938), logic at
[static/js/app.js:10594](static/js/app.js#L10594) — debounced 200ms, matches
folder name OR feed name OR feed URL, toggles `hidden`, shows an empty state).
Port that pattern to the posts list.

**Most of this already works — three findings from checking (2026-07-21):**

1. **"Move all visible to feed" already exists**
   ([static/js/app.js:7045](static/js/app.js#L7045)), so the "attach everything
   shown to a feed" half is built. Only the filter is missing.
2. **The data is already in the DOM.** Every row carries `data-post-link` and
   `data-post-title` ([templates/index.html:629](templates/index.html#L629)), so a
   URL/title filter needs **no** server change.
3. ~~**The server sends the whole list, not a page**~~ — **WRONG, corrected
   2026-07-22. Read this before building anything here.** The server sends **250
   posts** on first load ([main.py:16591](main.py#L16591)); the client then pulls
   further chunks of `CHUNK_SIZE` (10) via `chunk`/`chunk_delta`, **capped at
   2,000** (`limit = min(requested_chunk * CHUNK_SIZE, 2000)`). Almost certainly
   introduced by the page-weight work (#12, PR #146) after this finding was
   written.

   **This invalidates the design below, so don't build it as specified.** A
   client-side filter spans only the ~250 rows currently in the DOM, so
   "Move N shown" would silently move a fraction of a filtered set — the exact
   footgun this item set out to avoid, relocated from the scroll window to the
   fetch window. A 1,321-match filter is precisely the case that breaks.

   **It also re-explains Josh's original report.** "Only the literally visible
   stuff moved" was read as scroll-chunking; it is really server-side
   pagination. **The existing `Move visible to feed…` has this bug today** — it
   collects `document.querySelectorAll('.posts .post-item')`
   ([static/js/app.js:7332](static/js/app.js#L7332)) and its comment claims that
   is "whatever survives the active filters (tag, search, unread, star)", which
   is only true when the result set is under 250. Worth fixing regardless of
   whether the filter gets built.

   **Three ways forward — needs a decision before any code:**
   - **(a) Honest partial.** Filter what is loaded; label the button with both
     numbers ("Move 84 shown of 250 loaded"). Cheap, keeps the promise small
     and true, but is not the "act on the whole set" capability wanted.
   - **(b) Load-all, then filter.** On engaging the filter, keep pulling chunks
     to the 2,000 cap before filtering. Delivers the intended guarantee, but
     re-inflates the page weight that #12 spent a release cutting, and still
     silently truncates above 2,000.
   - **(c) Server-side filter.** Pass the filter term as a query param and let
     the server narrow, then move by *predicate* rather than by a list of ids.
     The only option that is correct at any size, and the only one where "move
     everything matching" is truthful — but it is a server change and overlaps
     the existing search.

   Leaning **(c)** for the move action and **(a)** for the typing-feels-instant
   filter, i.e. filter locally for feedback but resolve the *move* server-side
   against the same predicate. That keeps "everything I filtered to, nothing I
   didn't" honest without re-loading thousands of rows into the DOM.

**⚠ The footgun — read before implementing.** `post-item-hidden` is *already taken*
by the scroll-chunking reveal ([static/js/app.js:11491](static/js/app.js#L11491)),
and "move all visible" selects `.posts .post-item` **without** excluding it. Today
that's merely mis-labeled ("visible" actually means "everything the server
returned"). But the moment a filter reuses the same hidden mechanism, *filter to
one domain and "move all visible" would move the entire unfiltered list to that
feed* — a silent, bulk, hard-to-undo mis-file. So:

- give the filter its **own** class (e.g. `post-item-filtered`), not
  `post-item-hidden`; and
- update the move-visible selector to respect it, and restate the button copy as
  "move the N shown" with the count coming from the same predicate.

**Josh confirmed 2026-07-21: "only the literally visible stuff moved."** One
ambiguity to settle, because both readings are defensible and one is useless:

- ✅ **What to build** — move everything matching the **active filters** (server-side
  tag/search/unread/star *plus* the new client-side filter), regardless of how far
  the list has been scrolled.
- ❌ **Not** the strictly-literal reading — "only the rows currently painted." The
  scroll-chunk reveals 10 at a time, so that would silently move ~10 of a filtered
  1,321 and look like it worked.

The distinction: **scroll-chunking is a rendering optimization, not a user
intent.** Filters are something the user *chose*; the scroll window isn't. So the
guarantee to implement is "everything I filtered to, nothing I didn't." Put the
resolved count in the button (**"Move 1,321 shown"**) so the set is stated before
the click and can never be guessed at.

**Sequencing note vs #4:** build this first — it's roughly a day (filter is a copy
of an existing one, move already exists), it's a *general* capability worth having
forever, and it's the manual escape hatch for the cases #4 can't resolve (the 7.8%
with no match and the 0.7% ambiguous). But **don't hand-grind the 92% with it** —
that's what #4 automates; use this for the tail and for spot work.

(The dead `server_posts_total` / `server_posts_sent` plumbing noticed while checking
this is filed with the other dead-code items under "Code health" in Later.)

### Auto-file saved articles — the tail

Built and run 2026-07-21: `lectio:saved` went **4,334 → 424**, and the four big
no-feed hosts are gone from the list. Rationale is in ARCHITECTURE.md ("Saved
articles"). What remains:

- **guitarplayer.com's 303 articles** are the single biggest item and have no
  good home: the site's own subscription is a scraped one-article stub (barred
  as a target), and probing showed many of its article URLs soft-404. A real
  guitarplayer feed, "one-off saves", or deletion — Josh's call, not automatable.
- **The orphaned-star sweep** — delete `saved_entries` rows whose entry is gone
  (4,508 total, 4,264 on `lectio:saved`). The cause was found and fixed
  (`backfill_saved_entries_from_archive` re-created them at every startup, and a
  second bug in the same function was starring *tagged* entries), so a sweep now
  stays swept. Bulk delete against live data — **needs a go-ahead**.
- **166 already-converted stars** — tagged entries starred by that backfill
  before it was fixed. Indistinguishable from a genuine star-and-tag, so they
  cannot be surgically reverted; the unstar-tagged pass is what removes them.
### Removing a feed leaks its per-feed meta rows — measured 2026-08-08

Noticed after combining the two Sarah's Scribbles Webtoons feeds. The combine
itself was correct — the removed feed carried no stars, tags or archive rows, so
its single uncurated 2021 episode was rightly dropped — but it left a row behind
in `entry_lead_images`, and that turns out to be the normal outcome.

**The rename path and the removal path disagree.** `change_feed_url` carries a
15-table `_feed_url_tables` list (archive, folders, saved, read state, history,
lead images, feed tags, prefs, …). `purge_orphaned_feed` deletes the reader feed
and cleans exactly two tables: `kept_feeds` and `feeds_needing_replacement`.
Everything else keyed on `feed_url` stays.

Accumulated on the live library, `entry_lead_images` alone:

| | rows | feeds |
|---|---:|---:|
| on feeds reader no longer has | **25,504** | 371 |
| …of those, feeds that still have **archive** rows | **1,923** | 38 |
| …dev/localhost test feeds | 11,250 | 6 |
| …genuinely dead | 12,331 | 327 |

**The middle row is the trap, and it is why this is not a one-line DELETE.** The
Saved view renders archive orphans — entries whose feed is gone but whose capture
survives — so 1,923 of those rows are still displayed. `kept_feeds` is a second
exclusion: it is empty today, which makes "not in reader" *look* safe, but an
unsubscribe-with-keep would put rows there and the naive sweep would then blank
thumbnails in the Kept view.

So a safe sweep excludes three sets, not one: feeds reader still has, feeds with
archive rows, and `kept_feeds`. Two things to do, and the first matters more:

1. **Fix the leak** — have feed removal reuse the same table list the rename path
   already maintains, minus the tables an archive orphan still needs. Combine no
   longer contributes: it re-keys each entry's meta rows onto the survivor
   (`_rekey_entry_meta`). Plain unsubscribe still leaks.
2. **Sweep the backlog** once, behind the exclusions above. Bulk delete on live
   data, so it wants a go-ahead. The dev/localhost 11,250 are free.

### Clearing the lead-image cache is not a repair — found 2026-08-08

`clear_lead_image_cache` + `fetch_and_store_lead_images_for_feed` looks like the
obvious way to re-derive a feed's thumbnails. It is not: the backfill loop
deliberately visits only entries that are **unread, saved or manually tagged**,
so clearing first leaves every *read* entry with no row at all and nothing ever
comes back for it. Doing exactly that across the 15 Tapas feeds took them from
33 correct / 54 wrong to 20 correct / **124 unresolved** — worse than before.

The repair that works resolves per entry and stores the result directly
(`_plugin_or_source_lead_image` + `store_entry_lead_image`), which reached all
144. Worth remembering before the next "just clear it and let it rebuild".

Two things that could follow from it, neither started: give the backfill an
explicit "include read entries" mode for repair runs, or have
`clear_lead_image_cache` refuse to clear rows the backfill will not revisit.

### A domain alias rewrites the host but not the scheme — found 2026-08-08

Two dead `http://tapastic.com/rss/series/N` feeds sat next to their live
`https://tapas.io/rss/series/N` twins, failing every refresh, and the duplicate
scanner never flagged them. `_DOMAIN_ALIASES` rewrites `tapastic.com` →
`tapas.io` but preserves the scheme, so the husk normalized to
`http://tapas.io/...` while the working feed is `https://...` — different
strings, so `get_feed_duplicates` (which groups on `normalize_feed_url`) put
them in different groups.

The husks are gone, but the gap is general. Measured across 2,883 feeds, exactly
**two** other pairs differ only by scheme, and both are real duplicates that are
live and fetching:

| | http | https |
|---|---:|---:|
| Azius Blog | 7 entries | 6 entries |
| Featured Projects (Behance/FeedBurner) | 106 entries | 153 entries |

**The fix is in the comparison, not the normalizer.** Rewriting `http` → `https`
in `normalize_feed_url` would be wrong — some hosts really are http-only, and
that function decides the stored subscription URL. Grouping
scheme-insensitively inside `get_feed_duplicates`, keeping `https` as the
survivor, is narrow and safe. Small, but it closes a class where the two copies
diverge silently: the Behance pair is 47 entries apart.

### Duplicate *feeds* are invisible to every scanner — measured 2026-08-08

Two subscriptions to Sarah's Scribbles on Webtoons (`title_no=50260`, 20 entries;
`title_no=677113`, 1 entry from 2021) are plainly the same comic, and nothing
finds them. `get_feed_duplicates` groups by `normalize_feed_url`, so it only ever
sees slash and format variants of **one address**; two different `title_no`
values never group. The entry-level scans cannot help either — measured, the two
feeds share **zero** titles and **zero** links, because 677113 only ever had one
episode and it is not in the other's window.

What does identify them is the feed **title**. Measured across the live library
(2,886 feeds):

| signal | groups | feeds covered | verdict |
|---|---|---|---|
| same host + path, differing query only | 10 | **740** | useless — almost all YouTube `videos.xml?channel_id=…` |
| **same feed title** | **32** | **72** | a real, reviewable list |

The title groups are mostly genuine: `sarah's scribbles ×3`, `cryptid club ×2`,
`fantasyanime ×3`, `nine inch nails ×3`, and 15 same-host pairs. The obvious
guard is a generic-title floor — `news ×7` is 7 unrelated sites, not a duplicate
— plus keeping it advisory: a same-title pair can legitimately be a site's blog
and its podcast. Nothing should be pre-checked, per the usual rule.

Worth building as a third tier in the Dupes tab. Not started.

### Cross-feed duplicate scan — the dupes you can actually feel

**RE-MEASURED 2026-07-22 — #4 collapsed almost all of this, exactly as predicted.
This item is now small enough to question whether it is worth building at all.**

| set scanned | groups | extra copies |
|---|---|---|
| all starred, measured 2026-07-21 (pre-filing) | ~490 | ~520 |
| **all starred, measured 2026-07-22 (post-filing)** | **65** | **87** |

Breakdown of the 65 (10,058 starred entries carry a usable link; 323 skipped as
homepage-like):

| | groups |
|---|---|
| cross-feed, saved ↔ real feed | **3** (was 447) |
| cross-feed, between two real feeds | **44** (was 46) |
| same-feed | 18 |
| oversized (≥10 copies) | **0** |

**The dominant class is gone.** Auto-filing merged the saved copy onto the feed
entry where it was already starred, taking saved↔real from 447 groups to 3 — the
mechanism the Plan predicted (`_move_entry_to_feed` matches by GUID, else
normalized link). What is left is the 44 "subscribed to a site *and* an
aggregator that carries it" groups, which #4 was never going to touch.

**The romhacking.net 244-copy false positive did not survive either**, and not by
luck: the homepage guard sketched below (skip bare-domain/index links) removes it
along with 322 other homepage-linked entries, and with it every oversized group.
So the guard is validated — but it is now guarding a scan that finds 87 deletable
copies.

**Recommendation: don't build the cross-feed scanner UI.** 87 copies across 65
groups is a smaller pile than the ~490 that justified a dedicated surface, and
44 of the groups are a judgment call (which of two legitimate subscriptions
should own the post?) rather than a mechanical dedup. Either fold the homepage
guard + cross-feed grouping into the *existing* `/saved/duplicates` scan so it
stops being blind to the class, or leave it. Josh's call.

**Original analysis 2026-07-21 — Josh's hunch ("there's gotta be more dupes in
there") was correct, and the reason the scan disagreed is that it was looking at
the wrong set:**

| set scanned | duplicate groups | extra copies |
|---|---|---|
| `lectio:saved` only — what `/saved/duplicates` does today | 5 | 5 |
| all starred items (what the Saved **view** actually shows) | **~490** | **~520** |

Breakdown of the ~490: **447 groups are cross-feed** — the same article URL-saved
into `lectio:saved` *and* starred in its real feed; 46 are between two real feeds
(subscribed to a site plus an aggregator that carries it, e.g. martinfowler.com
articles appearing 3–5×).

**#4 collapses most of this for free.** `_move_entry_to_feed` matches into the
target feed by GUID, else normalized link — so auto-filing a saved copy onto the
feed where it's *already* starred merges the curation onto the existing entry and
the duplicate disappears. **Run #4 first, then re-measure**; the residual is what
actually needs a scan.

**⚠ Guard against homepage-links before building any cross-feed scan.** The raw
measurement found a single bogus group of **244 copies** — `romhacking.net`, whose
feed uses the site homepage as *every* entry's `link`. Grouping on normalized link
alone would offer to delete 243 unrelated articles in one click. Any cross-feed
scan needs to ignore bare-domain/homepage links, and should cap + flag oversized
groups for review rather than presenting them as confident matches. (This is the
same class of hazard as the pre-armed delete in #1a — be conservative by default.)

**Also found: 354 orphan star rows.** `saved_entries` holds 4,669 rows for
`lectio:saved` but reader has only 4,334 matching entries, so 354 stars point at
entries that no longer exist. Harmless but they inflate counts; worth a sweep
while in here.

### Finish the Instapaper clone (Read Mode follow-ups)

The read-later app shipped across PRs #137–#144 (Save any article, Saved sidebar
view, Read Mode at `GET /read`). These are the deferred finishing touches, moved up
from Later now that "finish it" is the stated goal. All were explicitly parked as
"build on demand" — this is that demand. Full context under
"Instapaper-alternative" in Later.

- ~~**Archived-aware node counts**~~ — **already done, the item was stale.**
  Shipped 2026-07-12 in `e9faf10` ("inbox-scoped Read Mode counts/tags"), the
  same day Read Mode landed: `_read_mode_saved_index` splits archived from
  inbox, and the folder/tag nodes count the inbox only. Verified 2026-07-28.
  (The main app's Saved sidebar `get_saved_counts_by_folder` *is* total-saved,
  but deliberately — the Kept view defaults to All, so the whole backlog is the
  meaningful number there. Not the same surface, not a bug.)
- ~~**Mark-read only after the last page**~~ — **DONE 2026-07-28.** The server
  no longer marks on render; `static/reader.js` posts to `/entries/read` once
  pagination settles *and* the last page is reached, reusing that route's async
  branch rather than adding an endpoint. **Settling is a readiness check, not a
  delay** — a cold load measures a 12-page article as one page until its CSS
  lands, which trips "last page reached" on open. Caught in review re-testing;
  the fix polls until `readyState === 'complete'` and every image has resolved,
  capped at ~5s.
  Verified in a browser: peek at 1/12 stays unread, 12/12 marks read, one-page
  article marks on open. **Applies to both scopes** — the peek problem is the
  same in the feeds scope, and the scope isn't visible from inside the reader.
- ~~**Prefetch the next article**~~ — **DONE 2026-07-28, via images only.**
  The obvious form was blocked: the reader page is `Cache-Control: no-store`
  ([main.py:16307](main.py#L16307)), so `rel="prefetch"` would fetch the next
  article and discard it. Of the three options — (a) images only, (b) relax the
  page to `private, max-age=…`, (c) client-side HTML swap — **(a) shipped**:
  it's where the flash actually comes from, and it leaves both the page's
  no-store posture and the reader's navigation model untouched.
  `prefetchNextImages` parses the next article in a detached `DOMParser`
  document and warms up to 12 same-origin images, hung off the settle rather
  than a fixed delay from load.
  **Depends on the mark-read change above** — fetching the next article's HTML
  would previously have marked it read unseen.
- ~~**Dates, sort order, and a readable Archive button**~~ — **DONE 2026-07-28.**
  Reported while reading on the Supernote: the Archive control "just looks like
  an empty checkbox" (it was ▢/▣ — the shape said nothing about archiving and the
  filled/empty difference was invisible at button size; now ▤, the glyph the tree
  already uses, with state shown by inverting the button), no date on posts (now
  the publish date, falling back to received, under the reader headline and on
  each browse row), and **no way to sort** (Newest / Oldest / Received; the
  backlog always took sort_by/sort_dir, Read Mode just pinned them). The chosen
  order rides the URL through `_read_scope_params`, so Next/Prev walk the list
  the same way instead of reverting at the first hop.
- ~~**Delete/Archive were no-ops on tagged items**~~ — **FIXED 2026-07-28**, and
  it was made urgent by the counts fix above: once Read Mode showed all kept
  items, the majority of the backlog (16,479 tagged vs 10,002 starred) had no
  working way to be filed or dismissed from the device. Both buttons still
  advanced to the next article, so they looked like they worked.
  **Josh's rule:** *Delete removes star and tags; Archive just removes it from
  the inbox.* Delete now clears tags **then** unstars — that order matters,
  because the unstar route only removes the offline archive when no tags remain.
  Archive is hidden for a tag-kept item rather than silently doing nothing.
- **Archive for tag-kept items** — **schema landed 2026-07-29 (PR A).** The
  separate-table option won: `saved_entries` row existence means "starred" in 115
  places across 13 files, so a `kept_only` column would have needed every one of
  them audited, and any miss turns an archived item back into a starred one.
  `archived_entries` leaves all 115 untouched. Legacy `saved_entries.archived_at`
  is lifted on every boot (`INSERT OR IGNORE`) and no longer read.

  **Josh's workflow definition (2026-07-29), which drove the whole design:**
  *Archive = mark this To Read item as read, keep its contents. Delete = I'm done
  with this, don't necessarily delete it now but don't protect it anymore.* A star
  is a **TODO**, not a keep — saved items needed a second read/unread layer
  because you can read something and still not have decided what to do with it.
  The model is triaging an email inbox from the list without opening things.

  | | Archive | Delete |
  |---|---|---|
  | star (TODO) | removed | removed |
  | tags | kept | cleared |
  | read state | marked read | marked read |
  | inbox | out | out |
  | offline capture | kept | released |
  | pruning | exempt | not protected |

  **PR B landed 2026-07-29**: Archive unstars + marks read (both levels, plus
  `read_history`), works on tag-kept items, and `archived_entries` membership is
  now a third keep signal in `entry_has_keep_signal` and `_prune_entries`. Delete
  became `POST /entries/discard` — one route instead of a client-side chain,
  because the tags-before-unstar ordering is a storage-layer property.

  **PR C landed 2026-07-29**: Inbox = starred − archived (was kept − archived =
  24,672, the whole library); tag tree counts *filed* items so it doesn't empty;
  new `starred` sort (`saved_at`) as the Inbox default; `resume_sort` so that
  order doesn't follow you out; Read Mode Tags section renders open.

  **All Saved node added** the same day, after the Inbox narrowing landed: the
  ~15k tagged-but-unstarred items were still reachable under Tags, but Read Mode
  had no flat "everything kept" view while the main app did.

  **Not built, by decision:** row-level Archive/Delete in the Read Mode list
  ("ignore for now"), and an Archive view in the regular app ("don't think we
  need it at all", conditional on History being browsable in reverse order).

  **PR D landed 2026-07-29**: Settings → Feeds → Utilities → **Archive old
  stars**. Cutoff chips (7d/30d/90d/6mo/1yr) with an age-spread table, because a
  lone total cannot be sanity-checked before thousands of items move. Manual,
  never automatic. Measured live:

  | cutoff | archives | Inbox left |
  |---|---|---|
  | 7 days | 9,827 | 175 |
  | **30 days** | **9,581** | **421** |
  | 90 days | 3,461 | 6,541 |
  | 1 year | 3,130 | 6,872 |

  **⚠ DO NOT RUN IT YET — `saved_at` is not a real star date for most rows.**
  Measured 2026-07-29: **6,091 of 10,002 stars carry a `saved_at` in 2026-06**,
  which is when multi-user went live and the data was migrated — the migration
  stamped its own run date instead of preserving the original. Those are largely
  years-old Inoreader stars wearing a seven-week-old timestamp. (This is the
  "1–3 month bucket holds 6,120" figure, which was first misread as the filing
  run.) So a 30-day cutoff sweeps 6,091 articles in and a 90-day cutoff protects
  them, neither for any real reason.

  **Fix before this is usable: offer the date basis, and default to publish
  date.** Publish date asks the better question anyway ("articles from 2019 I
  have still never opened"). Josh also said he still wants to go through most of
  this material, so the whole premise of a bulk cutoff is his call rather than a
  given.

  **Star provenance, measured 2026-07-29** — only 419 of 10,002 stars were made
  by hand in Lectio:

  | `saved_at` | stars | meaning |
  |---|---|---|
  | before 2026-06 | 3,492 | real dates, read out of the Instapaper CSV |
  | 2026-06 | 6,091 | **the migration's own run date** |
  | 2026-07+ | 419 | actually starred in Lectio |

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

**BUILT 2026-07-29:** `suppressed_feed_tags(feed_url, tag)`, an × on each chip
(`POST /feed-tags/dismiss`), and an undo list at Feed Properties → *Hidden tags*.
Case-insensitive, per feed, and it never deletes the stored `entry_feed_tags` rows.

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

### Small daily-friction items (cheap; slot between the bigger pieces)

  list; see "Auto-file saved articles".* Measured 2026-07-21 and it turned out far higher-yield than
  a "small item."
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
Corpus is only 4 edited entries / 67 ops (Phase 1 shipped 07-24), so the repeat
rate itself is *unmeasurable*, not proven low — but two findings don't depend on
sample size:

| finding | number |
|---|---|
| edited entries | 4, across 4 **distinct hosts** |
| ...of those, filed under `lectio:saved` | **3** |
| ops carrying a promotable `tag.class` | 47 / 67 (70%) |
| ops that are **tag-only** (no id, no class) | **20 / 67 (30%)** |
| selectors recurring across entries | **0** |

- **⚠ `feed_url` is the wrong rule key.** Three of the four edits are on
  `lectio:saved`, which is a pseudo-feed holding saved articles from everywhere —
  currentaffairs.org, dummies.com and reactormag.com all share that one
  `feed_url`. A `feed_content_rules` row keyed on it would apply one site's strip
  to unrelated sites. **Key on the entry-link host instead**, with the feed as a
  secondary scope for real feeds. This also fits the six hand-coded per-site
  strips better — those are per-*site* already.
- **30% of ops can't be promoted at all**, confirming the derivation caveat
  above: they have no id and no class, so `tag.class` degenerates to a bare tag
  that would match every `<p>`/`<svg>` on the site. The matcher must skip these,
  which means a promotion UI has to show "12 of 49 removals are promotable" or it
  will look broken.
- Re-measure once there are edits on **≥3 entries of the same real feed** —
  that's the shape that would actually justify building this. Until then the
  hand-cleanup from Phase 1 is doing the job.

### Tag-as-keep — Part C, pass 2

Pass 1 ran with `--apply` 2026-07-22: **3,581 archives enqueued** and drained by
the worker. Pass 2 — the Wayback tier for entries whose live page is gone — is
still deferred, and is a single command with a decay clock: run it any time, it
queues behind nothing.
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
  risk set. This is also the **triage list that gates Part C pass 2 (#8)**, produced
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

Sequence: connect Ino → comparison report (9a) → triage/replace dead feeds → Part C
pass 2 (#8) → proxy the only-Ino feeds (9b) → let the plan lapse 2027-03-16 (annual
SaaS rarely prorates; worth asking, but plan to ride it out).

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Overlaps
with #9: some "we can't fetch" feeds get fixed here instead of via the Ino proxy,
so it's worth revisiting once the comparison report sizes set (a).

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

## Later

### Saved / Tags / dupe-scan friction (reported 2026-07-21)

User-reported friction on already-shipped surfaces. Code pointers verified
2026-07-21.

> **Most of this section was promoted into "Saved dedup workflow"**, which treats the dupe cluster
> as one project (correctness+safety → repeat-session UX). Tag autocomplete and the
> tag autocomplete went to "Small daily-friction items" and the Uncategorized batch-align to "Auto-file saved articles"
> (it measured far bigger than expected). Everything stays documented here in
> full; the Now entries are summaries. Nothing in this section is still deferred
> except where noted inline.

**Bugs** — *promoted to the Saved dedup workflow; both dupe-scan bugs shipped 2026-07-21, see there
for what actually changed and why the `normalize_article_url` half was dropped.*

- ~~**`http://` and `https://` count as different URLs in the Saved dupe scan.**~~
  **DONE.** The analysis below was right about the mechanism — the slug tier
  rescues only twins whose slug clears the guards — and that is exactly the split
  the data showed (5 rescued, 4 fell to "possible"). The "deeper cause" half was
  **not** built: zero twins remained inside `lectio:saved` to merge.
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

**Saved dupe-scan UX** (all in the dupe dialog) — *promoted to the Saved dedup workflow*

- **"Not duplicates" action** — needs persistent per-pair suppression so a
  rejected group stops reappearing on every scan. New storage; the only item
  here that isn't cosmetic.
- **Collapse the two Confirmed/Possible sections** — collapsible, so a long
  confirmed list doesn't bury the possible tier.
- **Resizable / larger dialog.**
- **More obvious per-item status** — e.g. a 404 rendered in red rather than
  neutral text (URL status already comes from `/saved/duplicates/check-urls`,
  [main.py:22031](main.py#L22031)).
- ~~**Change the auto-select rule**~~ — **DONE 2026-07-21** (shipped with the
  http/https fix, not with the rest of this UX batch). One correction to the
  note below: "Check All" is *"Check all URLs"*, the liveness probe — not a
  select-all. Auto-select *only* 404
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

- **Autocomplete while typing** — per-entry tagging SHIPPED 2026-07-24. A shared
  token-aware control (`attachTagAutocomplete`, exposed on `window`) suggests from
  `get_all_manual_tag_names()` as you type each whitespace-separated tag; the
  names are a page-load JSON snapshot (`#lectio-tag-names`). **Still to do:** wire
  the same control into the automation rule form, fed from `entry_feed_tags`
  (feed-provided tags) rather than manual tags — a different source, same widget.

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
- Save Article follow-up ideas. **The "archive (unstar-on-read) flow to mimic
  Instapaper's read/archive split" is DONE** — Read Mode shipped it 2026-07-12 as
  the `saved_entries.archived_at` state (keeps the star; "done" as a separate axis
  from read/unread). Struck to stop it reading as outstanding. Still open, but
  **reassess only after auto-filing** since auto-filing changes what the tree looks
  like: pinned saved-tag shortcuts under the Saved Articles row, and a badge
  counting total saved instead of unread (if unread proves the wrong default).
- **The Read Mode follow-ups listed above are now under "Finish the Instapaper clone"** ("finish the Instapaper
  clone") — they stay documented here for context, but the actionable list and its
  ordering live in Now.

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
- Multi-word tags are *stored* hyphenated (`windows-11`) but can be **typed
  naturally** in rule lists (comma-separated; see the parser note above) — the
  earlier "must hyphenate" reading was wrong. ~~Still worth a tag autocomplete
  in the rule form fed from `entry_feed_tags`~~ — **DONE 2026-08-02**,
  one shared control as required, and it fills in the hyphenated stored form
  from whatever you type.

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

**Dead code sweep** — do these together in one pass, they're all "delete the thing
nobody references":

- **`server_posts_total` / `server_posts_sent`** — read in `templates/index.html`
  with `is defined` guards but **never set anywhere in Python**, so they're always
  empty. Found 2026-07-21 while checking the posts list for "Filter this view".
- **`templates/js/_layout_shell.js` and `templates/js/_pull_to_refresh.js`** —
  unreferenced leftovers from an earlier extraction attempt (was filed under
  page-weight follow-ups; it's a dead-code item, not a perf one). Confirm nothing
  external uses them, then drop.
- **The dormant in-app star-mode tree/JS** that the Read Mode hijack bypasses —
  see "Finish the Instapaper clone", which lists it as a Read Mode follow-up. Same sweep.
- **`LECTIO_SECURITY_MODE`** — set to `"multi"` by
  `scripts/refresh_screenshots.py` for the Administration capture, but **nothing
  in the app reads it**; `grep -rn SECURITY_MODE --include=*.py` matches only
  that one line. A leftover from before auth became unconditional (`AUTH_ENABLED
  = True`), so the admin instance is multi-user whether or not it is set.
  Harmless but misleading — it reads as a supported switch. Found 2026-07-21
  while repairing the screenshot tooling; drop the line.

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

- **Entries nothing can date sort by received time** — an entry with a NULL
  `published`, no dated permalink and no date in its title has no publication
  date to find, so it sorts by when the reader first saw it. That is now the
  *only* remaining case: the two bugs that used to dominate this bucket were
  fixed 2026-08-04 (see below). A one-time backfill persisting the inferred date
  could still be added if the ordering of those specific entries ever matters.

  **Fixed 2026-08-04 — sentinel dates, and unreachable inference.** Two defects
  that hid each other. (1) A missing date stored as a *sentinel* — the Unix epoch
  from an importer, or year 0001 from a parser — is **truthy**, and every date
  fallback here is an `or` chain, so it beat every fallback instead of falling
  through like the NULL it stood for. 312 entries across 31 feeds. (2) The
  URL/title inference was **dead code**: it sat behind `entry_effective_date`,
  which already fell back to the received date and so was never falsy, meaning
  `url_inferred_pubdate` had never once run. The visible symptom was an entry
  with `2025-11-22` in its own URL showing no usable date at all. Split into
  `entry_publication_date` (may return None — what lets the UI say "no date"
  honestly) and `entry_effective_date` (always returns something, for the sort
  and the bulk age actions), with `real_published_date` normalizing sentinels to
  None. `_URL_PUBDATE_RE` also gained the `/YYYY-MM-DD/` permalink shape.

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
