# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now

### Offline actions: apply locally, sync on reconnect — NEXT (2026-08-01)

Offline *reading* works on the Supernote (verified: 21 articles, 26 images, 9 MB,
read with WiFi off). Offline *acting* does not. Archive and Delete are ordinary
POSTs; with no connection they simply fail. Josh's expectation, and the right
design: "do what I said locally, then sync that info back to server upon online
again."

**Scope.** The three actions that matter while reading are Archive, Delete and
star/unstar; tagging is a stretch goal (it needs the tag list, which is a bigger
offline payload). Read/unread is already implicit in Archive.

**Design.**

1. **An outbox in IndexedDB**, not in the Cache API — these are mutations, and
   they must survive a browser kill. One record per action:
   `{id, ts, verb, feed_url, entry_id, payload}`. `id` is a client-generated
   UUID so a replayed action is idempotent server-side.
2. **Apply optimistically in the DOM** the moment it is tapped: the row leaves
   the list, the glyph fills. The reader must not be able to tell whether it was
   online, which is the whole point.
3. **Replay on reconnect.** Background Sync where it exists, plus a flush on the
   next page load, because Chrome 96 on the Supernote may not have Background
   Sync and a feature that only works on modern browsers is no use on the device
   this is for. Flush oldest-first, drop a record only on 2xx or a definitive
   4xx (409/410 = the server already agrees).
4. **Conflict rule: last-writer-wins, with the device losing ties it cannot
   see.** If the server state already moved (archived elsewhere, entry deleted),
   accept the server's version silently — the alternative is a merge dialogue on
   an e-ink screen, which nobody wants. Log discarded actions so a surprising
   loss is at least explicable.
5. **A visible queue depth** — "3 changes waiting to sync" in the Read Mode
   footer, cleared when it drains. Silent queues that never drain are how people
   lose work without noticing.

**Server side.** `POST /entries/discard` and the archive routes already exist and
are the right granularity. What they need is idempotency: accept a client action
id and no-op a repeat, so a flush that half-succeeded before the connection died
can be retried whole. A small `synced_actions(action_id, applied_at)` table in
the meta DB is enough; it needs the startup per-user migration (see
`project_per_user_schema_migration` — adding a meta-DB table without it 500s
existing tenants).

**Adjacent, smaller, do it at the same time: make "Save 20 more" cache-aware.**
The cursor currently skips by POSITION (`localStorage` offset per node), not by
what is already stored. Press once for items 1–20, again for 21–40. If new
articles arrive at the top between presses the list has shifted underneath the
cursor, so a few get re-saved and a few get missed. Rare in a backlog folder,
likely in the Inbox — where new stars land at the top, which is the entire point
of the Inbox. The service worker can query its own cache, so the honest version
is "save the next 20 I do not already have" rather than "the next 20 by index".

**Existing pieces to build on:** `static/sw.js` (fetch handler, `_worthCaching`,
precache message), `static/offline-probe.js` (button, cursor, status line),
`GET /read/offline/manifest` (now takes `offset`), `archived_entries`,
`POST /entries/discard`.

**Device constraints, learned the hard way:** the Supernote browser has no
download handler at all, so a service worker serving the NAVIGATION is the only
in-browser route; it is Android 11 WebView / Chrome 96; and it caches `/static`,
so anything shipped there needs a moving `?v=` (fixed 2026-08-01 — the version
hashed a hand-kept list that omitted `offline-probe.js`).

### Phone layout revived (2026-07-31)

Single-pane mode is back as a third mode in the main app's layout shell (≤720px).
Deliberately *not* revived alongside it: swipe gestures and pull-to-refresh, which
`9dab5a8` removed as part of the same commit but are a separate decision from "can I
read on my phone". Their `bindSwipeGestures` / `bindSinglePanePullToRefresh` call
sites remain as no-ops, so either can come back on its own.

Untested on real hardware: verified with Playwright at 390x844 (levels step 0→1→2,
back controls, 44px touch targets), but not on Josh's actual phone. (priority order)

**Rule-management UI shipped 2026-07-25** — Feed Properties → **Other domains**
lists a feed's declared domain aliases with add/remove (`POST
/feeds/url-rewrites`, `…/delete`). Closes the deferral below: Edit Website can
only seed a rule for a host it can *infer*, so an author's older dead domain
(Tushar's `tusharsadhwani.dev` / `tushar.bio`, neither with a surviving entry)
had no way into `feed_url_rewrites`, no way to be seen, and no way to be removed
short of SQL.

**Shipped 2026-07-23 (engines done, rule-management UI deferred to a browser
session):**
- **"Fix URLs" per-feed host rewrite** — for an author who moved domains without
  updating their feed's `<guid>`/`<link>` (e.g. `tush.ar/rss.xml` still emits
  `tushar.lol`/`sadh.life`). A `feed_url_rewrites` rule rewrites the host at
  ingest (raw feedparser result, before reader derives ids), so entries arrive
  with the current-domain id/link — which is what the post title-links carry, and
  the *only* way to fix them durably (the link alone can be overridden; the id is
  the PK and the feed re-serves the old guid every refresh).
  `scripts/apply_feed_url_rewrites.py` migrated the 31 existing tush.ar entries
  (→18 after same-slug cross-domain merges; 1 star + 15 tags preserved), and a
  live refresh confirmed the old ids don't come back.
  **UI shipped 2026-07-24:** Feed Properties → **Edit** next to Website. Editing
  the site domain seeds the `feed_url_rewrites` rule (old channel-link host → new
  Website host) and migrates the existing posts inline via
  `migrate_feed_host_rewrite` — the same per-entry logic the batch script now
  imports from `main` (`migrate_entry_to_new_host`). Also fixed the reverse bug
  it exposed: the list/pane rebase (`_rebase_proxy_entry_link`) folds the channel
  link through declared migrations first, so a feed whose channel `<link>` still
  names the dead host can't rewrite correct entry links back onto it, and the
  Website/favicon read the migrated host too.
- **Re-save resurfaces from Archive** — an explicit save of an already-archived+
  read article (e.g. one a 2019 Instapaper import archived) now un-archives it and
  marks it unread, so it lands in the Saved Inbox instead of silently staying in
  Archive. Was the reactormag "Black Cat" report.

**Current focus: Saved Articles — finish the read-later app, then get the backlog
under control.** Items **#1–#7** are that epic, in dependency order: fix what's
broken (#1–#2), organize the pile (#3–#6), finish the Instapaper-clone surface
(#7). **#8** is the daily-polish bucket to slot in whenever. **#9** is a single
command with a decay clock — run it any time, it doesn't queue behind anything.
**#10–#12** are unrelated and genuinely deferrable.

**Overnight session 2026-07-22 — what changed:**
- **#9 pass 1 ran** (`--only archive --apply`): **3,581 archives enqueued**, the
  worker is draining them. Pass 2 (Wayback) still deferred.
- **The orphaned star rows are solved** — `backfill_saved_entries_from_archive`
  was re-creating them at every startup, which is why the bug looked
  irreproducible. Fixed and tested; **the sweep still needs a go-ahead**, and
  the orphaned *archive* rows need a re-key-or-delete decision. See #4.
- **⚠ A second bug in the same function was silently starring tagged entries**,
  and pass 1 was feeding it — left alone it would have manufactured ~3,581
  redundant stars, re-creating #5's entire backlog. Caught mid-run at 166 and
  fixed. See #4 for the mechanism; **#5's count is now a moving target
  (1,603 → 1,769), so re-measure right before acting on it.**
- **#5 and #6 re-measured.** #5 is unchanged in size (1,603) and safer than
  before (only 29 rows carry `archived_at`). **#6 collapsed from ~490 groups to
  65** — #4 did what it was predicted to do, and #6 may no longer be worth
  building.
- **#3 is blocked on a decision**: its core premise (client holds the whole
  list) turned out to be false — the server sends 250 and pages after that.

**Shipped 2026-07-21: #1, #1c and #4.** The duplicate workflow is safe and the
scan returns nothing; Saved search went ~19s → ~1.2s and now matches article
text; auto-filing took `lectio:saved` from **4,334 to 424**. **#5 and #6 are
therefore unblocked** — and every number they were scoped against is now stale,
because #4 rearranged exactly the sets they operate on. **Re-measure first.**
Still open in the epic: **#2**, **#3**, **#5**, **#6**, **#7**, plus #1b's "not
duplicates" persistence (demoted — see #1).

**The cleanup order inside #3–#6 matters** and isn't arbitrary: auto-file (#4)
merges curation between duplicate copies, which changes which entries carry stars
and tags — so unstarring (#5) and dupe-scanning (#6) must come after it or they
operate on a set that shifts underneath them. Re-measure between steps.

**Inoreader renews $69.99 on 2027-03-16** — confirmed 2026-07-21, ~8 months of
runway at ~$5.83/month, so the Ino chain (#10) is **scheduled work, not urgent**:
start ~Dec 2026, leaving ~3 months to validate before renewal. The motivation is
consolidation and ownership, not cost.

**⚠ Measured 2026-07-21 BEFORE the filing run — every row below is superseded.**
Kept because it is the reasoning that shaped the epic, not because it is current.
#4 has since taken `lectio:saved` from 4,334 to **424**, so the #5 and #6 rows in
particular describe a set that no longer exists. Re-measure before acting on them.
(The starred total also moved 11,050 → 13,895, but ~3,600 of that is the orphaned
star rows logged under #4 — not real saves.)

| finding | number | item |
|---|---|---|
| saved articles with no feed, but a host matching one you subscribe to | **3,974 of 4,334 (91.7%)** | #4 — done |
| starred items that also carry a tag (star now redundant) | **1,643 (14.9% of starred)** | #5 — restale |
| duplicate groups the *current* scan can see (`lectio:saved` only) | **5** | #6 — now 0 |
| duplicate groups actually in the Saved **view** (all starred items) | **~490, ~520 extra copies** | #6 — restale |

That last gap is the headline: **the dupe scan structurally cannot see the dominant
duplicate class.** It scans `feed = 'lectio:saved'` only, but the Saved view shows
all 11,050 starred items — and 447 of the ~490 duplicate groups are *cross-feed*
(the same article both URL-saved **and** starred in its real feed). See #6.

Taken together the epic should take the Saved view from 11,050 items to something
far smaller and actually organized: ~3,974 filed onto real feeds, ~1,643 unstarred
as already-tagged, ~520 duplicate copies collapsed (with heavy overlap between
those sets — re-measure rather than adding them up).

### 1. Saved dedup workflow — correctness, safety, then UX (one project)

Treat the whole dupe cluster as **one piece of work**, not six tickets. It's a
single workflow you're actively using, the pieces reinforce each other, and
shipping them separately means re-opening the same code five times. Full per-item
detail under "Saved / Tags / dupe-scan friction" in Later.

**1a — correctness + safety. DONE 2026-07-21.** Both halves shipped together; the
scope changed on contact with the data, so the corrections are recorded here.

- **Scheme/`www` folding — done in the dedupe key only.**
  `normalize_entry_link_for_dedupe` ([main.py:4920](main.py#L4920)) now folds the
  scheme and a leading `www.` (host lowercased, paths left case-sensitive), which
  reaches all four consumers at once: the Saved scan's confirmed tier, the
  render-time list collapse, the cross-feed cleanup pass, and the curation
  migration on feed removal.
  - **`normalize_article_url` was deliberately left alone.** The Plan called for
    fixing both layers, but that one is the stored entry id *and* link. Rewriting
    it would touch up to 780 `http://` saved entries, some on genuinely http-only
    hosts, to fix a class with **zero live instances** — see below. The stored URL
    stays as saved; only the comparison key folds.
  - **The one-off merge was dropped: there was nothing to merge.** Measured
    2026-07-21 — **zero** http/https or www twins remain *inside* `lectio:saved`
    (Josh had already cleared them by hand). "New pairs accrue daily" was a
    code-derived prediction, not visible in the data. Across the whole starred
    set the fold gains only 8 groups / 10 copies out of 448.
  - **The real payoff is the tier, not the count.** The confirmed tier's other
    key, the URL slug, is discarded when generic (`/index.html`, blocklisted, or
    hyphen-free and short — [main.py:4996](main.py#L4996)). So twins split by that
    rule: 5 were rescued into *confirmed* by their slug, 4 had no usable slug and
    fell to *possible*, where nothing is preselected and each needs a hand
    judgment. That is the bug Josh hit ("I removed a bunch of http/https dupes,
    but they appear under the maybe dupes"). Folding gives every twin a
    confirmed-tier key. It also merges the 5 http `romhacking.net` rows into the
    existing 239-copy homepage-link false positive — no worse, same known footgun.
  - Side effect worth knowing: the keep-order's "prefer https" tiebreak now
    actually engages, since twins finally group.
- **The confirmed tier no longer pre-arms deletion.** Nothing renders checked in
  either tier. `savedDedupGroupHtml`'s flag is now `showKeeper` — it only labels
  the copy the keep-order would keep. Selection is armed solely by probe evidence:
  `_sdApplySelection` (replacing `_sdFlipKeeper`) checks a copy only when its URL
  came back 404/410. Alive, bot-walled, timed-out, and unchecked copies stay
  unselected, so an inconclusive probe can never queue a delete. Two groups
  deliberately select nothing: one where *every* copy is dead (link rot, not
  duplication), and the sole copy still holding stored content. The possible tier
  never auto-arms at all.
  - **Correction to the original note:** the "Check All" button beside it is
    *"Check all URLs"* — it runs the throttled liveness probe, not a select-all.
    The danger was only the pre-checked boxes, and those are gone.

**1a-bis — the slug tier was host-blind. DONE 2026-07-21.** Found immediately
after the above shipped: Josh re-ran the scan and *every* group was a
false positive. Both confirmed groups were cross-site slug collisions —
`pinch-harmonics` on guitarworld.com vs guitarmasterclass.net,
`acoustic-guitar-strumming-patterns` on guitarworld.com vs guitarchalk.com.
`_safe_dedup_entry_slug` returns the last path segment with no host, and
`/saved/duplicates` is the **only** consumer where a bare slug match confirms a
duplicate on its own (the multi-signal dedup always requires title/body
corroboration — a lone `slug` is not in `_SAFE_DEDUP_COMBOS`). So two
publishers writing about one topic became a confirmed duplicate, pre-armed for
deletion until the safety fix landed hours earlier. Fixed with
`_saved_dup_host_slug`, which scopes the key to the folded host; the shared
helper is untouched. **Confirmed groups went 2 → 0 on live data.**

**Inline title editing in the dupe dialog — DONE 2026-07-21.** Josh: some saved
titles no longer match what the post says. Each row gets a ✎ that swaps the
title for an input (Enter saves, Esc cancels, blur saves) and POSTs to the
existing `/entries/set-title`, so the correction is pinned as an override that a
later refresh can't clobber. The row is a `<label>`, so the handler
preventDefaults to keep the edit from toggling that copy's checkbox.

Covered by `tests/unit/test_entry_dedupe_key.py` (22 cases) and verified in a
browser against a seeded instance: no checkbox pre-checked, "keep" on row 0
only, title edit persists and leaves the selection alone.

**The Saved dupe scan is now clean: 0 confirmed, 0 possible** (verified against
live data 2026-07-21, after Josh used the new inline title editing).

Worth recording *why*, because it changes the priority of "Not duplicates": the
three residual possible-tier groups matched on **title only** — they were
different posts whose saved titles had drifted into collision. Correcting the
titles removed the only signal binding them, so the groups stopped existing
rather than being suppressed. Inline title editing turned out to be a partial
substitute for "not duplicates", not just a convenience.

**Partial**, though — it only dissolves *title*-matched groups. A group flagged
`same content` (body-prefix match) won't respond to a title edit. So **"Not
duplicates" persistence in #1b is demoted from blocking to worth-having**: build
it when a body-matched false positive actually shows up, not before.

Also of note: the corrections are durable. `entry_title_overrides` re-pins the
title if a refresh re-ingests the entry, and `_replace_entry_content` checks
`title_pinned`, so a later **Re-fetch content** won't undo them.

**1b — make repeat sessions bearable.** Only one item here isn't cosmetic:

- **"Not duplicates"** — persistent per-pair suppression so a rejected group stops
  reappearing on every scan. Needs new storage (a meta-DB table, so it also needs
  the startup per-user migration or existing tenants 500). **Demoted 2026-07-21:**
  the scan now returns nothing at all, and inline title editing dissolves
  title-matched false positives outright. Build this when a *body*-matched false
  positive shows up — that's the case a title edit can't fix.
- **Red 404 status**, **collapsible Confirmed/Possible sections**, **resizable
  dialog** — cheap, all in the same dialog, do them in one pass while you're there.

**1c — Saved search. DONE 2026-07-21.** The button was never the problem, and
neither was Read Mode: **the search took ~19 seconds**, which is
indistinguishable from doing nothing. Reproduced end-to-end against a copy of
the live library (133,765 entries, FTS index rebuilt so the numbers are honest).

Root cause: the kept branch in `list_entries_for_feeds` runs *ahead* of the
generic `elif search_terms` fast path, so the Saved view was the one place a
search took no fast path at all — it hydrated all ~11k kept keys via
`reader.get_entry` and filtered in Python. `_filter_star_keys_by_search` now
narrows the keys in SQL first (same technique as `_sorted_star_key_window`) and
only the survivors are hydrated.

| query | before | after |
|---|---|---|
| Saved, no query | 1.09s | 1.05s |
| Saved `q=python` | 18.94s | 1.51s |
| Saved `q=coffee` | 18.25s (28 posts) | 1.52s (**406 posts**) |

**A dead end worth not repeating: do not route this through reader's FTS index.**
`search_entries` builds a highlighted snippet per result — ~7.8ms/row, 76s for
one common term — so the FTS version measured *worse* (97s) than the scan it
replaced. That same cost is why a **Feeds-view** search still took ~10s.
**DONE 2026-07-22** — the Feeds view now uses the same SQL narrowing
(`_search_entry_keys_in_sql`), measured on the live library (134k entries,
2,888 feeds):

| query | before | after |
|---|---|---|
| `python` | 21.0s | **1.45s** |
| `guitar` | 9.3s | **1.26s** |
| `coffee` | 4.6s | **1.35s** |

Snippet-building was ~95% of it (19.7s of `python`'s 21.0s); hydration was never
the problem. Both search surfaces now share a predicate, so a Feeds search
reaches article text like Saved does (`coffee` 833 → 1,237 hits) and inherits
the same raw-HTML caveat. `_search_entries_fts` and `_fts_query` were deleted.

**FTS index retired — DONE 2026-07-22.** Nothing read it, and maintaining it
cost 1.3ms per new entry on every refresh plus **564MB** on disk (against a
743MB reader DB). No longer built, enabled or updated; the startup index-build
thread is gone too, so a fresh install stops spending its first minutes walking
every entry. `scripts/drop_search_index.py` reclaims the space.

Worth remembering, because it is a trap: **`disable_search()` does not reclaim
anything.** The DROPs go to the WAL and SQLite never shrinks a file on its own,
so the first run *grew* usage to 564MB index + 567MB WAL before the script
learned to checkpoint and VACUUM (index → 4KB). Any future "drop a big derived
table" work needs the same follow-through.

The `coffee` jump (28 → 406 posts) is the second half: Saved search previously
matched only metadata, never the article text, so a phrase from inside a saved
article returned nothing after a 19s wait. The SQL haystack now includes the
stored content (~60ms extra). Content is matched as raw HTML, so a markup-ish
term ("span", "http") matches nearly everything — stripping tags needs a
plain-text column maintained at ingest, deferred until a real search is hurt.

Covered by `tests/integration/test_star_key_search_filter.py` (10 cases:
field coverage, body matching, AND-ing, LIKE-wildcard escaping, >999-variable
chunking, and the fall-back-to-Python path).

**1c-bis — the actual reason Search "did nothing". DONE 2026-07-21.** The perf
fix above was real but wasn't what Josh was hitting: the log showed his page
served in 852ms and **not one request carrying `q=`, ever**. Reproduced in a
browser:

**In-page navigation replaces the toolbar DOM node, killing every listener bound
to it.** `loadScopePanesWithoutFullRefresh` — the sidebar, folder, scope and
search-form path — swaps the toolbar, and `#toolbar-search-btn`'s click handler
was attached to the old node at init. So after the *first* in-page nav, clicking
Search did literally nothing: no row, no request, no console error. A direct URL
load worked fine, which is why it never showed up in testing.

Fixed by delegating from `document` instead: the search button, the new clear
button, the input listener, and the form's `submit` handler (which had the same
flaw — Enter would silently degrade to a full page reload once the form node was
replaced). **Anything wired to this toolbar must be delegated**; binding to
`#toolbar-*` nodes at init is a live trap for the next feature added here.

Shipped alongside, since the surface was already open:
- **A real submit button** on both the toolbar search and Read Mode's form.
  Neither had one — Enter was the only trigger and nothing said so. Read Mode's
  matters most: it's the e-ink/stylus surface, where there may be no comfortable
  Enter key at all.
- **A clear (✕) control** on both, appearing once there's a query.
- **Read Mode search no longer drops the selected node** (the Plan's long-standing
  note): the form posted only `scope`, so searching from a folder, feed, tag, or
  Archive silently widened to everything. `_read_mode_search_fields` now carries
  the node as hidden inputs, and `_read_clear_search_href` returns you to that
  same node minus the query.

Verified in a browser end to end: search and clear both work *after* an in-page
nav, Enter still routes in-page rather than reloading, and a search started
inside Archive stays in Archive.

### 2. Saved capture quality — DONE 2026-07-28

`extract_full_page_article` / `fetch_full_page_article` capture the whole page
body instead of readability-extracting it: same sanitizer and post-processing
tail (factored into `_finalize_article_html`), but the body-selection step keeps
everything rather than scoring it.

**UI shipped 2026-07-28**, both halves, verified in a browser:

- **Re-fetch full page** in the post context menu — same handler as *Re-fetch
  content*, with `mode` selecting the extractor, so the dead-source recovery
  (offer to delete a 404) can't drift between the two.
- **Capture the whole page** checkbox on the Save Article modal
  (`POST /articles/save` gained `mode`). Having it at *save* time is the point:
  the re-fetch form only helps once an entry exists, so a page shape known to
  extract badly had to be captured wrong first.

**Off by default, and matched exactly against `"full"`** so a stray value can't
silently widen a capture — on a blog-shaped page full capture keeps the nav and
sidebar chrome readability strips. The checkbox also resets on every modal open:
it describes one page's shape, not a standing preference.

Verified on the two shapes below plus the live Blood Meridian article: full
capture keeps the cover image and pull-quote readability drops (4 imgs / 7,238
chars vs 2 / 5,774). Subsumes the lead-image-drop finding — that third failure
mode is now fixable, not just diagnosed.

The tradeoff is deliberate: on a blog-shaped page this keeps nav/sidebar chrome
readability would strip, so it is the escape hatch for document-shaped pages,
not the default. Only script/style/nav/header/footer are removed as never-content.

**Image-drop also fixed at the extraction level (2026-07-23).** guitarplayer
lessons store ~54 tab figures in bare divs no content selector matches;
readability kept ~1, so a normal refetch lost the figures that *are* the lesson.
`extract_readability_article` now falls back to the whole body as a last resort
when it *and* the selector fallback both keep ≤1 image on a >10-image page — so a
normal refetch recovers them, not only `mode=full`. Gated hard so a reasonable
extraction is never widened into dragging in chrome. 51 captured guitarplayer
lessons batch-refetched with `mode=full` (images 41 → 1,118, ~22 each); the 50
feed-provided GP entries are correctly untouched (the feed owns their content).

The original analysis follows.



**Every save path funnels through readability, so there is currently no way to get
a fuller copy of a page it handles badly.** Verified 2026-07-21 against Josh's
example, `https://schacon.github.io/git/everyday.html`:

| | chars |
|---|---|
| page body text | 11,658 |
| readability extracted | **786** (~6.7%) |

It doesn't just under-extract — it picks the *wrong node*, returning a single
`<pre>` shell-session block instead of the prose. The page is a DocBook-style
export: 84 `<p>` scattered across 68 `<div>`, no `<article>`/`<section>`, and 13
`<pre>`. Readability scores containers by paragraph density, so one big `<pre>`
wins while the actual prose stays split across sibling divs that each score low.

**⚠ CORRECTED 2026-07-22 — "deterministic re-run" is only half true, and the
half that's false was hiding a working fix.** Two distinct failure modes were
filed here as one:

- **Deterministic** (the schacon example below): a static document that scores
  the same way every time. Re-fetch genuinely can't help. The analysis below
  stands for this case.
- **Transient** — a JS-heavy page where extraction depends on what the fetch
  returned that day. **Re-fetch is exactly the right escape hatch here**, and it
  was being wrongly dismissed.

Demonstrated on Josh's report of a Dropbox blog post stored as 638KB / 522
images whose only text was bylines — readability had taken the *article-listing
grid* off a 2.6MB, 1,620-image AEM page. Re-running the identical extractor on
the live URL returned the correct article (11,591 chars, 5,006 chars of text).
Better evidence still: the same URL was captured twice into two different feeds,
and **one copy was already correct** — same code, same page, different outcome.

Scope check across all 273 large user-saved captures: only 3 are
image-dominated, and 2 of those are legitimately so ("All 182 screensavers on
your Amazon Fire TV", "50 Time-Saving and Free Photoshop Actions"). So this is a
rare failure, not a systemic one — which argues for keeping #2 as a manual
escape hatch rather than a pipeline change.

**Shipped alongside: Re-fetch works on filed articles.** It was gated on feed
identity in both the route and the UI, so auto-filing (#4) silently stripped the
hatch from every one of the ~3,900 articles it moved — the surface most likely
to need it. Now gated on the entry being a Lectio capture, with in-place update
via `refresh_filed_article`. See ARCHITECTURE "Saved articles".

**A third failure mode, found 2026-07-22: readability silently drops the lead
image.** Distinct from both above — extraction succeeds, the prose is fine, but
the article loses its opening art.

Reproduced across three sibling posts on one site (Blood Meridian pt.1/2/3 on
mattiaspettersson.com). All three wrap the cover identically:
`<div class="separator"><a style="float:left"><img></a></div>` as the first
child of `.entry-content`. Readability's *conditional cleaning* drops child divs
that are text-free and link-heavy — the cover is 100% link, 0% text — but the
thresholds scale with the page's overall text volume, so:

| | extracted chars | lead image |
|---|---|---|
| pt-1 | 6,948 | **dropped** |
| pt-2 | 4,155 | kept |
| pt-3 | 8,870 | kept |

Same code, same site, same markup, different side of the line. pt-1 also lost
the styled pull-quote that followed the cover, so the loss isn't only images.

**Two things this rules out, so don't re-test them:**
- **Not the save method.** Josh's read was that the Readit extension captured
  the image and the server-side save didn't. Running the *server-side* extractor
  over all three URLs reproduces the split exactly, so the extension has no
  advantage here and re-saving pt-1 by any route produces the same result.
- **Not recoverable from metadata.** The page has no `og:image` and no
  `twitter:image`, so the lead-image service has nothing to fall back on — the
  image exists only inside the content readability just stripped.

**Options, undecided:** (a) accept it; (b) a *lead-image rescue* — after
extraction, if the raw page's first in-content image is absent from the result,
prepend it. Fixes the class, but changes the pipeline for every save and needs
care not to start dragging in logos and header art; (c) fold it into the
raw/full-page save mode below, which sidesteps extraction entirely. **(c) is
the natural home** — it's the same "readability made a bad call, let me keep
the whole thing" need, and designing (b) separately risks two half-solutions.

Why none of the existing escape hatches help *for the deterministic case* — all
three call the same `extract_readability_article`, so they are deterministic
re-runs of the same failure:

- **Re-fetch content** (`/articles/refresh-content`, [main.py:22731](main.py#L22731))
  → re-fetch + re-extract, same pipeline.
- **Extension / captured-DOM save** — `_extract_from_capture`
  ([main.py:23022](main.py#L23022)) runs readability *on the captured DOM*. It
  helps for JS-rendered or paywalled pages, but for a static document the
  captured DOM ≈ the fetched HTML, so the result is identical.
- **Delete and re-save** — saved entries are keyed by normalized URL, so a re-save
  refreshes the same entry with the same extraction.

So this is a genuine gap, not user error: **add a "save full page (don't extract)"
option** that sanitizes the whole `<body>` via the existing
`html_sanitize.sanitize_html` instead of readability-extracting. Wants to be
reachable both at save time and as a per-entry "re-save without extraction" so
already-bad captures can be fixed in place. Related to #10 (same pipeline, opposite
direction: that one *adds* extraction to feeds with no body).

### 2a. Backups: retention is count-based and size-blind

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

### 2b. Dead feed-redirector links — investigated 2026-07-22, automation exhausted

Not a new item; recording the measurement so nobody re-investigates. Every
automatic recovery path fails on the live library:

| starred redirector links | 37 |
|---|---|
| recoverable from captured archive HTML | **0** — no archive rows for these |
| recoverable by live redirect resolution | **0** — feedproxy.google.com 404s, no redirect chain |
| recoverable from Archive.org | **0** — no snapshots of the redirector URLs |
| have an article slug in the path (reconstructable in principle) | 15 |
| opaque id only (`~3/vGL5XCHkyww/`) | 22 |
| whose feed knows the publisher host | 30 |
| **still hold their content** (article readable in Lectio) | **36 of 37** |

`scripts/backfill_canonical_links.py` was built for this and returns
`0/37 recoverable`. Hosts: 35 feedproxy.google.com, 1 danielmiessler, 1 betanews.

**A host+slug reconstruction tier was considered and rejected**: it would cover
~13 entries, needs a per-entry verification fetch, and guesses the publisher
host from the feed's site link or the `/~r/<token>/` path. Poor value for 13
links on mostly dead 2013-era blogs.

**Resolved instead by Edit URL** (`POST /entries/set-link`) — the user finds the
new location by hand and pins it, then Re-fetch pulls the body from there. That
covers the 22 opaque ones no heuristic could ever reach, and generalizes past
redirectors to any moved or reorganized site. See ARCHITECTURE "Canonical entry
links".

Note the loss here is only the *link*: 36 of 37 still have their stored content,
so the articles read fine in Lectio today.

### 3. "Filter this view" — ⚠ BLOCKED on a decision (see finding 3 below)

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

### 4. Auto-file Uncategorized saved items into their real feeds — BUILT 2026-07-21

**Shipped:** `services/saved_autofile.py` + `GET /saved/autofile/preview` +
`POST /saved/autofile`, driven from Settings → Feeds → Utilities → **File saved
articles** (the two duplicate scanners moved to their own **Dupes** tab). Nothing moves without per-host approval. Re-measured on live data at
build time (the Plan's original numbers predate a lot of manual filing):

| | |
|---|---|
| live unfiled saved articles | 4,261 across 176 hosts |
| **confident match, pre-checked** | **2,880 across 87 hosts** |
| weak match (low support) — shown, unchecked | 465 |
| ambiguous (2+ candidate feeds) | 181 |
| no subscribed feed for the host | 735 |

**Match on the article host, not the feed-URL host** — a feed often lives on a
different host than the articles it publishes (`rss.beehiiv.com` serving
`joanwestenberg.com`), so the signal is which subscribed feed already carries
entries linking to that host.

**"Exactly one candidate" is not the same as "confident", and the difference was
load-bearing.** `guitarworld.com`'s target is backed by 77 of the feed's own
entries; `guitarplayer.com`'s only candidate was a scraped single-article URL
with **one** supporting entry — auto-filing 303 articles into it would have been
wrong. Hence `MIN_SUPPORT`. Josh independently confirmed the guitarplayer case
is messy ("gp got sucked into guitarworld at some point").

**Also fixed here: `_move_entry_to_feed` left a husk behind.** It marked the
source read and stripped star/tags but never removed it, on the reasoning that
reader can't delete feed-provided entries — which isn't true for `lectio:saved`,
whose entries are `added_by='user'`. So filing never shrank the backlog (Josh
moved a batch and `lectio:saved` stayed at exactly 4,334) and every later dupe
scan re-read husks. The saved source is now hard-deleted via the shared
`_hard_delete_entry`. Verified on a copy of live data: filing 11 articles took
`lectio:saved` 4,334 → 4,323 and moved 11 stars onto the target feed.

**Refinements from Josh working the list:**
- **Newly added feeds weren't being suggested — three separate causes.** Josh
  added feeds for the four biggest no-feed hosts and saw no change. Entry-link
  evidence alone can't see them: `guitarchalk.com/blog/feed` and
  `quickreads.net/feed.xml` had **0 entries** (never fetched, so no evidence can
  exist), and the guitarmasterclass subscription is a **FeedBurner** feed whose
  27 entries all link to `feeds.feedburner.com`, pointing its evidence at the
  wrong host. Fixed by also using the hosts a feed *declares* — its own URL host
  and its advertised `link` host (696 of 2,881 feeds differ between the two).
  **Unmatched articles 698 → 66; confident 45 → 473**, with guitarmasterclass's
  463-article cluster going from "no feed" to confident. A declared host makes a
  feed a candidate but only makes it confident when the feed is also stocked, or
  a one-article stub on the right host would swallow the site's backlog.
  Third cause, not fixed: `tutsplus.com/posts.atom` publishes to
  `photography.`/`design.`/`code.tutsplus.com`, never `music.tutsplus.com` —
  subdomains are distinct hosts and an eTLD+1 fallback was already rejected.
- **Barring a subscription as a destination** (`non_feed_subscriptions`, UI
  "not a feed"). This was Josh's actual ask, which I first misread as being
  about the saved articles: *"the 'not a feed' I'm talking about are some of the
  actual feeds added"* — guitarplayer.com's subscription is a single scraped
  article URL, so it is on exactly the right host and is precisely the wrong
  place to file 303 articles. Marking bars it as a target on both preview and
  apply; the subscription and its entry are untouched. Distinct from the
  host-level decision below and labelled apart, since both can appear on one row.
- **"One-off saves" per host.** Josh: some of these "need to be converted to just
  single saved items" — dummies.com, python.plainenglish.io and the like never
  came from a feed, so the filer could only keep re-proposing them. Marking a
  host drops it from the worklist for good; the saved articles are untouched
  (verified: entries and stars unchanged), since they already *are* standalone
  saves. Marked hosts stay reviewable in a collapsed section with undo. New meta
  table `autofile_non_feed_hosts`, created in `ensure_meta_schema` so the
  startup per-user migration covers existing tenants.
  (Sized mid-session at 735 no-feed articles across 49 hosts; Josh then
  subscribed to feeds for the four biggest, so see the end-of-session numbers
  below for where it landed.)
- **Filing is batched.** One uncapped call over a big host runs past a minute
  and is cut off in flight: observed live as `POST /saved/autofile → status 0,
  16180ms`, where 278 articles *were* filed but the reply never arrived, so the
  list looked untouched ("I just allegedly filed a bunch, still see them").
  Each call now caps at `_AUTOFILE_BATCH` and reports `remaining`; the client
  loops with progress on the button. Verified against a copy of the live
  library: 1,279 guitarworld articles filed across 9 batched POSTs,
  `lectio:saved` 4,334 → 2,777, 1,322 stars landing on the target feed.
- **The action is pinned to the bottom** of the ~180-row list and carries the
  running total ("File 1279 article(s) from 1 host(s)"), since the selection
  isn't on screen from down there. Disabled when nothing is selected.
- **The site's own feed outranks aggregators.** Feeds that link outward (Hacker
  News, link blogs) became candidates for every host they ever linked to — HN
  appeared for 16 hosts, and one link blog outranked a site's own feed 23 posts
  to 11. On-host candidates now rank first, and off-host ones no longer make a
  host "ambiguous" when a real feed exists. **Ambiguous articles 181 → 28,
  confident hosts 87 → 103.**
- **Nothing is pre-checked.** The intended workflow is passes — file a chunk,
  re-scan, continue — so `confident` now drives a *label* ("strong match — N
  posts from this host"), not a selection. Same rule as the dupe dialog.
- **Same-titled candidates are disambiguated in the option label.** Josh hit
  dropdowns whose entries "looked identical"; when two candidate feeds for one
  host share a title, the URL is folded into the label rather than left to the
  hover title, which is unreachable on touch/e-ink anyway. Hosts whose candidate
  titles are unique keep the clean label.
- **YouTube feeds are never valid targets** (`_autofile_excluded_targets`,
  enforced on preview *and* apply). A saved page is never really a video-channel
  post, and channels often share a name with the blog they accompany — with only
  titles visible, a YouTube feed is exactly what you'd pick by mistake. Currently
  a no-op on live data (no saved youtube.com articles, and a YouTube feed can
  only ever be a candidate for the youtube.com host), but 693 of 2,879 feeds are
  YouTube, so the first saved YT link would have hit it.
- **The target's feed URL is shown**, inline and as a hover title on both the
  select and each option. Feed titles are often deliberately unlike their URLs
  ("The Woodshed" living at `rss.beehiiv.com/feeds/XYZ.xml`), so a title alone
  doesn't identify what you're filing into. Inline rather than hover-only
  because hover doesn't exist on touch or e-ink.

Covered by `tests/services/test_saved_autofile.py` (17 cases) and
`tests/unit/test_autofile_excluded_targets.py` (4), and verified in a browser
against a copy of the real library: 175 rows, 86 pre-checked, 49 disabled for
having no feed, guitarplayer.com correctly not pre-checked. The YouTube bar was
verified with two feeds sharing the title "The Woodshed" (one blog, one channel):
the channel is absent from the picker, and posting it directly to apply is
rejected.

**Where it actually ended up (re-measured against live data at end of session
2026-07-21 — every number above predates the filing and is kept only as the
reasoning trail):**

| | |
|---|---|
| `lectio:saved` entries | **4,334 → 424** |
| real (kept) saves left | **403 across 65 hosts** |
| still unmatched | 370 — and **303 of those are guitarplayer.com** |
| strong match remaining | 6 |

The four big no-feed hosts are **gone from the list entirely**: Josh subscribed
to real feeds for guitarmasterclass.net, guitarchalk.com, music.tutsplus.com and
quickreads.net and filed them. What remains is a genuine long tail — 46 hosts
with no feed, most holding one or two articles.

**Still open in this area:**
- **guitarplayer.com's 303** are the single biggest remaining item and have no
  good home: the site's own subscription is a scraped one-article stub (now
  barred as a target), and probing showed many of its article URLs soft-404.
  Options are a real guitarplayer feed, "one-off saves", or deletion — Josh's
  call, not automatable.
- **Match at import time — DONE 2026-07-22, but as a *report*, not a file.**
  The import now runs the autofile matcher over just the rows it created and
  says so: "N of these match feeds you already follow (M sites) — review under
  Settings → Feeds → Utilities → File saved articles."

  **Deliberately does not auto-file**, which is a change from how this item was
  originally written. Filing exists behind a per-host review precisely because
  "exactly one candidate feed" is not the same as a trustworthy one — the
  guitarplayer.com stub would have swallowed 303 articles — and Josh's own
  refinement was that confidence drives a *label*, never a selection. Filing
  silently at import would bypass both. The value was never the automation; it
  was that an import used to land in Uncategorized with nothing said, which is
  how a 4,000-article backlog accumulates unnoticed.

  `_current_autofile_plan(restrict_to=...)` was extracted from the preview route
  so both share one assembly; the `restrict_to` filter keeps the count about
  *this* import rather than the whole backlog. The matcher is wrapped so a
  failure can never fail an import that has already committed.
- **✅ SOLVED 2026-07-22: the orphaned star rows were the archive backfill.**
  Cause found and fixed; the sweep is still outstanding and needs the go-ahead.

  **`backfill_saved_entries_from_archive` was re-creating them at every
  startup.** The function exists to recover from a meta-DB reset where the
  starred-archive DB survived: it inserts a `saved_entries` row for every
  `complete` `archived_entry`. But **an archive row outlives its entry** —
  filing a saved article hard-deletes the `lectio:saved` source (and correctly
  deletes its star row) while leaving the archive row untouched. So the next
  boot dutifully "restored" a star pointing at a tombstone.

  Every prediction the old note got wrong is explained by this:
  - *"Not reproducible"* — correct, and it never would be: the orphan is not
    created by the move at all. It needs **a move followed by a restart**.
  - *"Stamped today, so written during the session"* — right, and it is an
    `INSERT` (defaulting `saved_at` to `CURRENT_TIMESTAMP`), not a survival.
  - *"The only writer that stamps CURRENT_TIMESTAMP is `save_article`"* —
    **wrong**, and this is what sent the investigation the wrong way. Roughly
    ten call sites insert `(feed_url, entry_id)` without `saved_at` and so
    default it to now; the archive backfill is one of them.

  **Verified against live data, 100% match**: all 4,264 orphaned `lectio:saved`
  star rows have a `complete` archive row, and 0 have none. The star-row count
  (4,667) equals the complete-archive count (4,667) exactly.

  **It was also self-perpetuating**, which vindicates the old note's one correct
  instinct ("don't ship the sweep without reproducing, or it will just run again
  next session"): a sweep would have deleted all 4,264 and the very next restart
  would have re-created every one. The count grew 3,593 → 4,264 between sessions
  for exactly this reason, and 671 fresh rows landed at 21:00 local on 2026-07-21
  on a restart alone.

  **Fixed** in `backfill_saved_entries_from_archive`
  ([services/starred_archive.py:324](services/starred_archive.py#L324)): it now
  restores a star only when reader still holds the entry, and logs how many
  archive rows it skipped as stale. Non-destructive by design — no archived
  content is deleted — and it stops new orphans permanently. Covered by
  `tests/services/test_starred_archive_backfill.py` (7 cases), including that a
  failing reader lookup is treated as missing rather than resurrecting a star.

  **A second bug in the same function, found while verifying the first — this
  one was actively firing.** After the fix above shipped and the container
  restarted, star rows still climbed 14,566 → 14,732. The 166 new rows were
  **not** orphans: every one was a **manually tagged entry on a live feed**,
  163 of them on the dead `heyscriptingguy` feed.

  **Tag-as-keep broke this function's core inference.** It reasons "has a
  complete archive ⇒ was starred", which was true when written. Since the flip a
  *tag* archives too, so `archived_entry` is now a **superset** of the starred
  set — and the backfill was converting tagged entries into starred ones.

  **Tonight's Part C pass 1 was the trigger**, which makes this a self-inflicted
  wound worth understanding: retro-archiving 3,581 tagged entries meant that as
  each archive completed, the next startup would star it. Measured mid-run:
  1,769 tagged entries already starred, 29 more queued, **3,385 archives still
  pending** — i.e. left alone it would have manufactured ~3,581 redundant stars
  and inflated Saved by that much. That is precisely what #5 exists to undo, so
  pass 1 would have quietly re-created #5's entire backlog.

  **Fixed** in the same function: an entry carrying a manual tag is never
  star-restored (its archive is explained by the tag), and if the manual-tag
  lookup fails the backfill restores *nothing* rather than guessing — inventing
  thousands of stars is far worse than skipping a recovery path. Entries both
  starred and tagged are skipped too; this is disaster recovery, and losing one
  real star beats inventing thousands. Bulk `_manually_tagged_entry_keys()` in
  main.py backs it so the startup pass stays one query, not thousands.

  **Consequence for #5: its number is now a moving target.** The 1,603 measured
  earlier tonight became 1,769 during the run. Re-measure immediately before
  acting, and note ~166 of the affected set are stars *this session created*.

  **Still open:**
  - **The 166 already-converted stars** — tagged entries starred by the buggy
    backfill before it was fixed. They are indistinguishable from a genuine
    star-and-tag, so they cannot be surgically reverted; #5's cleanup is what
    removes them, which is an argument for running #5 sooner.
  - **The sweep** — delete `saved_entries` rows whose entry is gone (4,508
    total, 4,264 on `lectio:saved`). Bulk delete, needs Josh's go-ahead. Now
    worth doing, because the fix means it stays swept.
  - **Orphaned archive rows — RESOLVED 2026-07-23.** These surfaced as
    user-visible **phantom duplicates**: the Read/Saved view renders starred
    entries from archive rows (`get_archived_entry_detail`), so an orphaned
    `lectio:saved` capture showed as a second copy of the moved article, with
    its own worse content (a comments thread, an empty husk). Both decisions
    taken, not either/or: `_move_entry_to_feed` now re-keys the capture onto the
    target (or deletes it if the target already has one), and
    `scripts/dedupe_orphan_archives.py` cleaned the backlog — **4,076 redundant
    deleted, 502 true orphans left** (fully-gone articles, invisible, not a dup).
    `lectio:saved` archive rows 4,650 → 574. New service primitives:
    `has_complete_archive` / `delete_archive` / `rekey_archive`. A 597MB export
    of the deleted rows is parked at `/data/deleted_saved_archives_*.sqlite` as a
    safety net — delete once confident. Archive not VACUUMed (needs ~8GB scratch
    the disk lacks; freed pages get reused).
- **⚠ Inline SVG in feed content is mangled at ingest.** Found 2026-07-21 while
  redoing the docs screenshots. feedparser parses an HTML-escaped
  `<description>` as HTML, where a trailing slash is meaningless — so
  `<rect/><circle/><path/>` becomes `<rect><circle><path>`, every shape nested
  inside the rect, which cannot contain shapes. The browser paints the rect and
  drops the rest, i.e. **any feed shipping inline SVG art renders as a flat
  colour block**, and the inline-SVG thumbnail feature (PR #29) degrades to a
  solid rectangle. Lectio's own sanitizers are innocent: `svg_sanitize` and
  `sanitize_html` both round-trip the markup correctly — the damage is done
  before either sees it.
  - Reproduce: feed a `<description>` containing `<svg><rect/><circle/></svg>`
    through `feedparser.parse` and look for `</rect>` in the result.
  - The screenshot tooling now emits explicit end tags, which survive the HTML
    parse; that is a workaround for the demo, **not** a fix for real feeds. A
    real fix means re-parsing SVG subtrees as XML at ingest (or repairing the
    nesting in `sanitize_html`, which already special-cases `<svg>`).
  - A `data:` image dodges the parser entirely but Lectio's sanitizer strips
    data URIs from `src`, so that is not an escape hatch either.
- **Soft-404 detection — DONE 2026-07-22.** `_check_saved_url` only counted
  404/410, so a site that answers 200 for an article it no longer has read as
  alive. `_looks_like_soft_404` adds a `soft_dead` flag, surfaced in the dupe
  dialog as an amber **"probably gone"** badge.

  **It is advisory and never arms a delete.** `_sdApplySelection` still keys on
  `dead` alone, so a URL-shape guess can't pre-check a destructive box — same
  rule as the rest of that dialog.

  **The first implementation was wrong and live data caught it.** An
  ancestor-only rule (`/lessons/x` → `/lessons`) matched the Plan's original
  description but flagged **0 of 14** sampled guitarplayer URLs, because the
  site redirects *across* sections: `/technique/<article>` → `/lessons`, which
  shares no path prefix and isn't named like an index. The real signal is that a
  2+ segment article path collapsed onto a **single-segment section page**.
  After the fix, the same sample: **9 soft-404, 1 hard dead, 2 alive, 2
  redirects correctly left alone** — one of those being
  `/technique/exploring-ty-tabors-guitar-magic` →
  `/lessons/exploring-ty-tabors-guitar-magic`, a genuine section move where the
  article still exists. Same depth in, same depth out, so it isn't flagged.

  Worth reusing that shape: **depth lost = content gone; depth preserved = URL
  reshaped.** Relevant to any retention pass, and to the guitarplayer.com 303.

Original analysis, kept for the reasoning:

| | |
|---|---|
| saved articles in `lectio:saved` (the unfiled pile) | **4,334** |
| exact host match to a subscribed feed | **3,974 (91.7%)** |
| of those, host maps to >1 feed (ambiguous) | **29 (0.7%)** |
| no match | 339 (7.8%) |

**Use exact-host matching only.** A registrable-domain (eTLD+1) fallback tier was
tested and adds just **0.5%** (21 items) while introducing the `.co.uk`-style
public-suffix problem — not worth it. Strip `www.`, match saved-entry host against
both the feed URL host and the feed's site `link` host; that's the whole algorithm.

Ambiguity is a non-issue at 0.7%, so a straightforward "review the proposed
mapping, then apply" flow works — no clever disambiguation UI needed.

**Most of the machinery already exists.** `/entries/move-to-feed-batch`
([main.py:21440](main.py#L21440)) does batched moves today, `_MOVE_BATCH_CAP` = 500.
What's missing is only the *auto-match proposal* and a review screen. Two
behaviors of `_move_entry_to_feed` ([main.py:13327](main.py#L13327)) to know:

- If the target feed no longer holds the article (usual case — it aged out of the
  feed window, which is *why* it was URL-saved), the entry is **synthesized** into
  the target. So this works even for long-gone originals.
- The source entry can't be deleted (reader owns feed entries), so it stays in
  `lectio:saved` marked read and stripped of star/tags. Functionally invisible,
  but ~3,974 husks will accumulate — worth a thought re: retention/purge.
- Per move it may scan the target feed's entries to link-match. At ~4k moves that
  is not free; batch in chunks and expect it to run for a while.

**Falls out for free: a subscription-discovery signal.** The 339 unmatched are
concentrated in a handful of hosts Josh clearly reads but doesn't subscribe to —
`guitarchalk.com` (151), `texasbluesalley.com` (62), `joanwestenberg.com` (46) =
259 of 339 from three sites. "You've saved 151 articles from this domain and don't
subscribe" is a strong add-feed prompt, and reuses the existing discovery path.

Note this also supersedes most of the single-post-page workaround (see "Single-post
pages" in Later): Josh's instinct is to file such pages into *a related real feed*,
which is exactly what this does.

### 5. Unstar items that carry tags — DONE 2026-07-28

Built as `services/unstar_tagged.py` (pure decision layer) +
`GET /saved/unstar-tagged/preview` + `POST /saved/unstar-tagged`. Read-only
preview returns per-tag counts, the archived_at-loss count, and suggested
queue-like opt-outs; apply recomputes server-side under the given `keep_tags`
and deletes only the star row.

**UI shipped 2026-07-28** — Settings → Utilities → **Unstar tagged articles**,
verified in a browser against a seeded instance. Two decisions worth keeping:

- **The panel inverts the API's opt-out.** Rendering `keep_tags` directly would
  arrive with all 58 tags checked, making "unstar everything" the default and
  unchecking the destructive act — against the no-preselected-bulk-actions rule.
  The panel selects tags to *clear* and derives `keep_tags = all − selected`.
- **The button count comes from the server, not from summing rows.** An entry is
  protected by *any* kept tag, so an article tagged `python`+`books` survives a
  `python`-only selection despite being counted in the `python` row. Each
  selection change re-previews for the honest total.

Queue-like tags are flagged and excluded from "select all topical tags"; the
archived_at warning fires before a one-way loss of Read Mode progress.

**Not yet run against live data** — the live set was ~1,603 stars across 58 tags
at last measure, and those numbers are now days old. Re-run the scan and review
the tag list before applying anything.

Dry-run on live data at build time: **1,767 affected across 60 tags, 24 carrying
archived_at, zero queue-like names.** The count includes the ~166 tag-created
stars from the backfill bug (now indistinguishable from genuine star+tag), which
is fine — this is exactly the cleanup that removes them.

The rest of this entry is the original analysis, still valid as the reasoning.



After the tag-as-keep flip a tag *is* a keep signal, so a star on an already-tagged
item is redundant — it only clutters Saved, which should be the read-later queue.
Josh's idea: do it at DB level now, add a Utilities button for later upkeep.

**RE-MEASURED 2026-07-22 (post-filing — these are the current numbers):**

| | before #4 | now |
|---|---|---|
| star rows total | 13,895 | 14,566 |
| **starred AND tagged (affected set)** | 1,643 | **1,603** |
| ↳ in `lectio:saved` | 554 | **31** |
| ↳ in real feeds | 1,089 | **1,572** |
| share of all star rows | 14.9% | 11.0% |
| ↳ carrying `archived_at` (Read Mode state) | 371 (all rows) | **29** |
| distinct tags on the set | 57 | 58 |

**The conclusion holds and is now stronger.** #4 moved the affected items out of
`lectio:saved` and onto real feeds (554 → 31) rather than changing the size of
the set, so this is still ~1,600 redundant stars. The tag distribution is
essentially unchanged — `misc` 319, `linux-stuff` 211, `c++` 201,
`science-+-math` 143, `games-to-play` 97, `python` 85 — and a fresh search for
read/todo/later/queue/inbox/pending/unread-ish tag names again returns
**nothing**. Topical filing tags, safe to unstar.

Only **29** rows carry `archived_at`, so the Read Mode state at risk is now
trivial (was 371 across the whole table). Still worth an opt-out rather than a
blind delete, but it is no longer a reason to hesitate.

Caveat carried forward: the star-row total includes the 4,264 orphans, so
"share of all star rows" is understated. Run the orphan sweep (#4) first and the
real share is nearer 15%.

**Original measurement 2026-07-21 — this is safe, and I checked the thing that
would have made it unsafe:**

| | |
|---|---|
| manually tagged entries | 16,686 |
| starred entries | 11,050 |
| **starred AND tagged (the affected set)** | **1,643 (14.9% of starred)** |
| ↳ in `lectio:saved` / in real feeds | 554 / 1,089 |

**The risk I went looking for was read-later tags, and there are none.** Earlier
notes describe a `#toread` vs `#todo` pattern — buckets *under* a star — and
blanket-unstarring those would have gutted the read-later queue. All **57** distinct
tags on the affected set are topical filing tags: `misc` (319), `linux-stuff` (208),
`c++` (200), `science-+-math` (184), `games-to-play` (97), `python` (85), `guitar`
(59)… A targeted search for read/todo/later/queue/inbox-ish names returned
**nothing**. So the affected items are "filed by topic," not "queued to read", and
unstarring them is exactly the decluttering intended.

**Nothing is lost by unstarring a tagged entry — verified, don't re-derive:**

- **Pruning**: `_prune_entries` ([main.py:21192](main.py#L21192)) protects starred
  and manually-tagged entries **independently** (tagged excluded in SQL, starred
  skipped in Python), so the tag alone keeps them.
- **Archive**: the unstar route ([main.py:22689](main.py#L22689)) only enqueues
  removal `if not get_manual_tags_for_entry(...)` — a tagged entry keeps its
  capture. A **raw DB delete bypasses that path entirely**, which here is *safer*,
  not riskier: no removal is ever enqueued, so archives simply stay.

**Two things a DB-level run must handle** (they're why this shouldn't be a bare
`DELETE`):

1. **Cache/generation invalidation.** Deleting `saved_entries` rows behind the
   app's back leaves unread/tag counts stale until restart — and the unread-count
   cache is generation-guarded, so it won't self-heal. Either run it through the
   app's own invalidation helpers or restart the container after.
2. **`archived_at` is on this row.** 371 rows carry Read Mode's archived state;
   deleting a row discards it. Moot for items leaving Saved, but check the overlap
   with the affected 1,643 before running rather than after.

**Build it tag-selectable, not blanket.** The distribution is clean today, but
`games-to-play` (97) and `books` (43) are plausibly aspirational queues Josh may
want to keep starred. A preview listing counts per tag with opt-out beats an
all-or-nothing button, and it's what makes the Utilities version safe to re-run
later when the tag vocabulary has drifted.

**Sequencing:** run this *after* #4 (auto-file), since filing merges curation
between duplicate copies and changes which entries carry stars and tags. Doing it
first means operating on a set that #4 will rearrange underneath you.

### 6. Cross-feed duplicate scan — the dupes you can actually feel

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

### 7. Finish the Instapaper clone (Read Mode follow-ups)

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

### 7c. Importers fabricated publish dates — FIXED + REPAIRED 2026-07-29

Two capture paths stored "when this arrived in Lectio" as "when this was
published", and every date-based decision then trusted it:

- **Instapaper import** used the CSV's *save* timestamp as `published`, so a 2015
  article bookmarked in 2019 read as published 2019. **3,308 entries.**
- **Save-article (URL capture)** used `now()` at capture time. 5 entries.

Both now write `services.saved_articles.UNKNOWN_PUBLISHED` (the Unix epoch).
**1970 rather than NULL is the load-bearing choice**: `entry_effective_date`
falls back published → updated → added, so clearing the field would silently
substitute the *import* date — the same wrong answer by a longer route. The epoch
is visibly not a publish date, sorts to the end of every order, and is
searchable, which makes these findable rather than merely untrusted.

`scripts/repair_fabricated_publish_dates.py` reset the 3,310 already stored (JSON
log alongside the other repair logs, so it is reversible). It matches only two
*exact* signatures — `published == saved_at` and `published == first_updated` —
and **preserves `entry_date_overrides`** (4 rows; an explicit correction outranks
any inference). A looser rule would have destroyed the **22,543** user-added
entries that carry genuine publish dates from the Inoreader migration.

⚠ The lesson generalizes: **an importer must never invent a field it does not
have.** A missing date is recoverable; a plausible wrong one is not, because
nothing downstream can tell it apart from real data.

  **This retires #5.** Those 1,786 starred+tagged items get archived like the
  rest, keeping tag *and* capture, instead of being unstarred and losing the TODO
  axis. **⚠ The bulk path deliberately does not write `read_history`** — capped at
  2,000 rows, it is the only reverse-chronological record of what has been dealt
  with, and 9,000 bulk archives would evict all of it.

  **PR E (after): pinned tags.** Promote any tag to a top-level node in both
  modes. Store the pin **separately from the tag name** — Josh floated a `##tag`
  sigil, but that is the same magic-value-in-user-data problem as the `#archive`
  auto-tag: typing `#inbox` instead of `##inbox` would silently change behavior,
  and both `parse_manual_hashtags` and `normalize_tag_value` would have to learn
  the sigil.

- **⚠ #5's premise may no longer hold.** It unstars starred+tagged entries because
  "after tag-as-keep a tag is a keep signal, so the star is redundant." Under the
  workflow above star = TODO and tag = filing, so starred+tagged means "filed, and
  I still have to deal with it" — an ordinary state, not redundancy. Unstarring
  the 1,786 affected items would erase the TODO axis from everything already
  filed, with no undo. Re-decide before running it.

- **Read Mode has no per-row actions.** Archive/Delete exist only inside the
  reader, so the email-triage flow the model describes (deal with it from the
  list, without opening) isn't actually possible yet. Deferred 2026-07-29.

- **⚠ `read_history` is capped at 2,000 rows** (`READ_HISTORY_CAP`,
  [main.py:9098](main.py#L9098)) and is currently full — oldest entry 2026-07-08,
  about three weeks. Dropping the regular-app Archive view was made conditional
  on History being browsable in reverse order, which it is (`ORDER BY read_at
  DESC`). But Archive and Delete now both write history, so triage volume flows
  through that cap and the window will shrink. Read Mode's Archive node is the
  durable record; History is a convenience. Raising the cap is a one-line change
  and awaiting a number.
- ~~**Tag from inside Read Mode**~~ — **DONE 2026-07-28.** A `#n` button opens a
  panel of every tag in the library as large toggles (applied first, inverted),
  tap to add or remove, with "+ New" revealing a text field only when needed.
  Each tap applies immediately and sends the whole desired set
  (`append_mode=0`), so closing half way still saved. The server's reply is the
  truth and the panel re-renders from it. Note the new-tag field is
  space-separated like the main app's, so "brand new tag" is three tags — the
  placeholder says so, since there is no autocomplete here to make it obvious.
- ~~**Saved counts disagree between the two modes**~~ — **FIXED 2026-07-28.**
  The regular sidebar showed **24,695**, Read Mode **9,979**: the sidebar counts
  **kept** (starred OR tagged), `_read_mode_saved_index` counted **starred,
  non-archived**. Archiving was never the gap (23 rows) — it was
  tagged-but-not-starred (16,479 tagged vs 10,002 starred).
  **The list was already right**, which is what settled it:
  `resolve_reader_backlog(star_only=True)` resolves against
  `kept_entries_set = saved_entries_set | tagged_entries_set`, so the tree
  numbers never matched the list they opened. The index now counts kept-minus-
  archived. Only starred rows can be Archived (`archived_at` lives on
  `saved_entries`), so a tagged-but-unstarred entry is always in the inbox.

  **Josh's rule, which decided it:** *"eInk should really just be a slightly
  simpler version of regular, mostly just the look, and eink specific things like
  page instead of scroll."* Read Mode may differ in **presentation** (paging, high
  contrast, big tap targets, inverted state blocks) and in genuinely
  paging-specific behavior (mark-read on last page, the Archive done-axis). It
  must **not** carry a different data model — same kept definition, same effective
  date, same sort options, same tag vocabulary. A number that differs between the
  modes is a bug to reconcile, not a feature.
- **Excise the dormant in-app star-mode tree/JS** that the Read Mode hijack
  bypasses — dead weight now that the sidebar row opens `/read`. Pairs naturally
  with the dead-code sweep in Later's "Code health".
- **Optional per-image `grayscale(1)`** — e-ink nicety, lowest value.

Reassess the "pinned saved-tag shortcuts" and "badge counts total instead of
unread" ideas *after* #4 lands — auto-filing changes what the tree looks like, so
judging those now would be premature.

### 7a. Feed tags — Real Python has none to pull (2026-07-29)

Reported as "can pull some tags from page". Checked, and there is nothing to
pull without evading a bot wall:

- **The Atom feed carries zero `<category>` elements** — 0 across 50 entries.
- **The page fallback cannot run.** `extract_page_tags` already fires whenever a
  feed supplies no tags (main.py, via the lead-image source-HTML cache or its
  deferred fetch), but `realpython.com` answers a non-browser client with a
  Cloudflare interstitial ("Just a moment…"), so there is no article HTML to
  scan. Fixing this means spoofing a browser UA, which we don't do.

Same class as the bot-walled feeds blocking #10 (see `inoreader-replacement`).

### 7b. Offline reading on the Supernote — WORKS (2026-07-30)

**Confirmed on the device: WiFi off, article opens.** A service worker serves the
navigation itself, which is what makes it work at all — the browser has no launcher,
so Read Mode is reached from a saved hyperlink, and a cache the browser cannot
navigate to is useless.

Route taken, after the alternatives were eliminated by measurement:

- **Downloads are impossible.** The Supernote's WebView has no download handler:
  `<a download>` is a silent no-op and long-press offers only text selection. That
  killed self-contained HTML, EPUB-by-download, and bundles in one go.
- **Service workers DO register** there (Android 11 WebView, Chrome 96), precache,
  and intercept navigation.

Two bugs found on the way, both mine, both instructive:

- `ignoreSearch` in the offline fallback stripped the query string, and for `/read`
  the query string IS the article's identity — so every article matched the cached
  BROWSE page and re-rendered the list. A cache miss that looked like a navigation
  going nowhere.
- **Article and image coverage diverged.** The page cached every `.rm-item-link` in
  the DOM (up to 150 rows) while asking the manifest for images from only the first
  20, so most cached articles had no pictures. Reported as "seemed like images
  weren't included". Both halves now derive from one `OFFLINE_ARTICLE_COUNT`.

Residual limitation: images at **cross-origin** URLs still cannot be cached — a
no-cors response is opaque and indistinguishable from a failure. Measured small: 3
of 45 images across 25 Inbox articles, because most reader images are already
same-origin (`/starred-asset/…` from the archive, or `/api/img`). If it ever
matters, routing reader images through `/api/img` would close it.

### 7c-2. Epoch-dated articles — 2,030 of 3,308 recovered offline (2026-07-29)

The Instapaper importer had stored save timestamps as publish dates; repairing that
left 3,308 entries at the Unix epoch. `scripts/recover_publish_dates.py` recovered
**2,030 (61%) with no network requests at all** — every date came from page HTML
already captured in the starred archive, which also means it works for articles
whose sites are long dead:

| source | n |
|---|---|
| `article:published_time` (Open Graph) | 1,811 |
| URL path (`/2019/07/06/`) | 90 |
| JSON-LD `datePublished` | 89 |
| `<time datetime=…>` | 40 |

**The save date as an upper bound is what makes the `<time>` tier safe.** The HTML
was captured *recently*, not when the article was saved, so a stray `<time>` can be
years newer than the article (a comment, a "latest posts" rail). You cannot save
something before it is published, so a candidate later than the Instapaper save
timestamp + 1 day is rejected. That guard threw out ~189 dates a plain regex sweep
would have accepted.

**Re-fetch now learns a date too (2026-07-30).** `mine_publish_date` runs on the
page a re-fetch already fetched — no extra request, since `fetch_readability_article`
and `fetch_full_page_article` hand the raw body back through a `capture` dict
(readability strips head metadata, which is where the date lives). It writes **only
when the entry has no date or sits at the epoch**, and never over an
`entry_date_overrides` row: re-fetch used to *move* `published` and destroyed 105
real dates before that was caught.

**Batch-fetching the stragglers is NOT worth it — measured 2026-07-30.**
`scripts/fetch_missing_publish_dates.py` exists (date-only probe, paced, guarded)
but a 25-entry sample returned **zero** dates and a per-host probe across 8 hosts
also returned zero. The remaining entries cluster on sites that publish no date
metadata: blog.guitar-pro.com (96), joanwestenberg.com (45), what-if.xkcd.com (45),
datagenetics.com (27). One host answered 404 carrying the 404 page's own date,
which is what the slug and save-date guards are for. Sample with `--limit` before
any future full pass.

**Only 253 remain at 1970** (down from 1,278 — feed refreshes re-ingested most of
the rest with real dates), deliberately: 324 have no captured HTML, the rest have HTML
with no date in it. Guessing there would re-create the original bug. `<time>` is
tried last for the same reason — a page has many, and the first is often a
comment's.

Manual `entry_date_overrides` are never touched; an explicit correction outranks
anything inferred.

### 7d. Re-fetch: undo + Wayback fallback — DONE 2026-07-30

Two entries were wrecked by re-fetch in two days, each defeating the guard in a way
the previous case did not predict:

- **the-digital-reader** — a parked "Empowering Relationships" page returning **200**
  for a 2019 post. Unrecoverable: the archive copy had been rewritten too.
- **informit** — `/articles/article.aspx?p=…&WT.rss_a=<title>`. The subject lived
  only in the query string, and the path word "articles" matched the site index
  title "Articles | InformIT", so the slug guard passed it. Recovered from the
  07-28 backup, then upgraded from a Wayback snapshot Josh found by hand — the feed
  had only ever carried a 472-byte teaser, so the entry is now better than it was.

**The lesson: stop sharpening the heuristic.** It failed twice on cases that could
not have been foreseen. A re-fetch now snapshots the previous body into
`entry_content_edits.original_content` (INSERT OR IGNORE, so the first original
wins), which lights up the existing Revert control for free — a bad result is one
click to undo rather than a backup dive.

And the guard alone left the user stuck, so a refusal now falls back to
`archive.org/wayback/available`. The guard still applies to the archived fetch, so a
snapshot of the same parked page is refused too.

### 7c-4. accu.org: an image-less post with nine chrome images (2026-07-29)

Reported three times as "img-less post, getting a social icon". Each fix revealed
the next piece of chrome, which is the useful finding:

1. `/img/bsky.png` → rejected by filename → **`/img/mastadon.png`** (the site
   misspells Mastodon, so a name list is always one spelling behind);
2. social row stripped structurally → **`ads.accu.org/www/delivery/avw.php`**;
3. ad servers rejected → **`/img/accu/join.png`**, a "Join ACCU" banner.

**Two general fixes shipped** and both are worth having everywhere:
`_strip_social_link_images` drops `<a href="<social host>">…<img></a>` before
scoring (spelling-independent — every icon shares an anchor to the network), and
`is_ad_url` rejects ad-server hosts plus the Revive/OpenX `/www/delivery/*.php`
shape.

**But the page is not solvable by heuristics, and further attempts are wasted.**
It has no `<article>`, no `<main>`, and no content-ish container class (Bootstrap
`panel-body`), so container-scoping cannot separate the remaining nine chrome
images from an article image that does not exist. Any scorer will keep finding
*something*.

**The right answer for this feed is the existing per-feed escape hatch**: Feed
Properties → lead-image strategy → **None**, which `_is_feed_none_strategy` already
honors. Reach for that rather than a fourth rejection rule.

### 7c-3. Bot-walled pages: no server-side tags available (2026-07-29)

Two feeds where the tags Josh can see are rendered by the site's JavaScript and are
unreachable from the server. **Not code bugs — do not spend time on them.**

- **behance.net** — the feed carries **zero** `<category>` elements, and the gallery
  page answers a non-browser client with a JS challenge (`js_challenge_value`
  cookie + `window.location.reload()`). Page extraction *did* work 2026-07-13 →
  07-20 (793 tag rows captured, e.g. "Adobe Photoshop" ×39), then stopped dead, so
  the wall went up around 07-20. Any Behance entry ingested after that has no tags
  and cannot get them.
- **realpython.com** — feed has no `<category>` either; the page returns a
  Cloudflare interstitial. See 7a.

Both would need browser-shaped requests, which Lectio deliberately does not send
(see the good-web-citizen rule). Worth re-testing occasionally: Behance's wall
appeared mid-July, so it may lift the same way. If it does, the existing background
source-fetch fills the tags in with no work from us.

⚠ Knock-on to check if Behance thumbnails ever look wrong: lead-image extraction
uses the same source-page fetch, so it went blind on the same date.

### 7e. Batch re-fetch over a folder or feed — script + UI, 2026-07-30

`scripts/refetch_scope.py`. Josh: "Needs to be gentle!" — so pacing is the design:
2s global gap, **10s per host**, hosts dropped after 4 consecutive failures, nothing
parallel, and the runtime estimate accounts for the per-host delay (a single-feed
scope is one host, so 89 articles is 89x10s, not 89x2s — understating the runtime of
a deliberately slow job is the one number that must not be wrong).

Safe to run in bulk only because every single-re-fetch protection applies per entry:
the slug guard refuses a wrong page instead of overwriting, the previous body is
snapshotted so any result is revertible, a refusal falls back to the archive, and a
missing publish date is learned.

**Proven on informit** (4-article slice): all four live pages now serve the section
index, so the guard refused every one and **the Wayback fallback recovered every
one** — 13k-28k characters each where the feed carried teasers. 89 kept articles on
that feed, ~15 min for the full pass.

**Shipped as a UI action too (2026-07-30).** Right-click a feed or folder in the
Saved tree → *Re-fetch all articles…*: `GET /saved/refetch-scope/preview` shows the
count, host count and runtime before you commit, `POST /saved/refetch-scope` runs it
on a background thread (via `_run_in_user_context`, so the thread keeps the tenancy
user), and status polling reports progress and the outcome breakdown. The same menu
item cancels a run. One job at a time per user — two overlapping runs would each
honor the pacing and together double the rate every host sees.

Pacing now lives in `services/refetch_batch.py` and is imported by both the route and
the script, so the two cannot drift.

A second scope started while one runs is **queued**, not refused (Josh, same day:
"I cannot queue/start a batch for another Feed/Folder while one is running"), and a
fixed status pill shows the running scope, progress, measured time remaining and the
queue — the batch had no visible surface at all once its toast faded.

Deferred from the UI version: resumability across a restart, and a per-run log file
like the script writes.

⚠ **The restart gap bit immediately (2026-07-31).** A batch on the informit feed was
killed 4 minutes in by a container rebuild — 29 of 89 articles done, no trace in the
logs because only the completion line logs. Re-running is safe, but a job with no
persistence and no start/interrupt logging is invisible after the fact. Worth either
persisting the queue+cursor to the meta DB, or at minimum logging a start line and
marking the job interrupted on the next boot.

### 7c-1. Page tag extraction grabs the sentence, not the anchors (2026-07-29)

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

### 8. Small daily-friction items (cheap; slot between the bigger pieces)

- **Tag autocomplete while typing** — auto-list matching existing tags during tag
  entry. Build **one shared control** and use it for both normal per-entry tagging
  and the rule form (see "Tag filtering for firehose feeds" in Later, which wants
  the same thing fed from `entry_feed_tags`). Don't build two.
- **Batch-align Uncategorized saved items into Feeds** — *promoted out of this
  list; see Now #4.* Measured 2026-07-21 and it turned out far higher-yield than
  a "small item."
- **Set up the four verified firehose tag_filter rules** — config, not code; the
  engine already ships. Vocabularies verified 2026-07-21, see "Tag filtering for
  firehose feeds" in Later for the per-feed data and suggested rule shapes.

### 8b. Publish dates a re-fetch overwrote — FIXED + REPAIRED 2026-07-25

Cause fixed: `replace_entry_content` bumped `entries.published` to now to surface
a re-pulled capture, which is wrong twice over (Pub is a publication date, not a
last-touched date; and under the Pub-oldest sort in use the bump *buried* the
article instead of surfacing it). It now bumps **Received** instead.

Damage repaired the same day with `scripts/restore_bumped_publish_dates.py
--apply`: **101 restored, 101 corroborated, 0 uncorroborated** (second user: 0).
Drift ran up to 16 years — LWN "What every programmer should know about memory,
Part 1" read 2026-07-25 instead of 2012-05-03. Undo snapshot at
`data/users/u_40208f374ac18038598b39/restored_publish_dates_20260725-054213.json`;
a follow-up dry-run reports 0 candidates.

The script stays in the tree because the recovery source (starred archive
`published_at`, cross-checked against reader's `recent_sort`) is generic — if any
other path is ever found bumping published, re-run the dry-run first.

### 8d. Tushar feed consolidation — DONE 2026-07-25

Two subscriptions to one blog (`sadh.life/rss`, dead; `tush.ar/rss.xml`, live).
Josh ran Edit Website twice on the old feed (seeding `sadh.life → tush.ar` and
`tushar.lol → tush.ar`, migrating 14 then 1) and then Combine — which matched by
GUID and synthesized nothing. Survivor: 18 entries, 16 stars, 15 manual tags, no
orphaned star rows, no entry left on a dead host.

Fallout, since fixed: the combine stranded the offline captures (see
ARCHITECTURE "Combining feeds carries the offline captures"). 85 orphaned
archive rows library-wide — not just this feed — repaired with
`scripts/repair_orphaned_archives.py --apply`; undo snapshot (with blobs) at
`data/users/u_40208f374ac18038598b39/repaired_orphan_archives_20260725-071313.json`.
A follow-up dry-run reports 0.

Still open: `tusharsadhwani.dev` and `tushar.bio` are two more dead domains of
his with **no** entries in the library. Nothing to migrate; declaring them via
Feed Properties → Other domains only future-proofs the dedupe alias map.

### 8e. Comic thumbnails — FIXED 2026-07-25; tinyview + DA mature still open

Reported as "the comic image loads but there's no thumb". Cause: the cached
**lead image** (which is what the list thumbnail derives from) was site chrome,
while the article's own `<img>` came from the feed body and rendered fine.

- **gunnerkrigg** was a real code bug: the panel is `class="comic_image"` and the
  webcomic class pattern listed hyphen spellings only, so the Archives banner won
  the scan for 49 strips. Fixed; the id pattern already accepted both spellings.
- **smbc / misfile** extracted correctly already — their rows were just stale,
  from before the webcomic strategy was set. Confirmed by both fixing themselves
  once re-derived.
- `scripts/reset_webcomic_chrome_lead_images.py --apply` cleared **202 rows
  across 19 images** on webcomic feeds (gunnerkrigg 49, qwantz 39, webtoons 20,
  harkavagrant, tethered, badmachinery…). Undo snapshot at
  `data/users/u_40208f374ac18038598b39/reset_webcomic_lead_images_20260726-000945.json`.
  The detector is generic: a panel is unique per strip, so an image cached as the
  lead for many entries of one webcomic feed is chrome by definition.

**Closed 2026-07-26** — all four reported comics resolve to their panel
(atomic-robo 1.2MB `…494`, gunnerkrigg, smbc, whomp). Three further causes were
found behind the first: the blind `/comicsthumbs/`→`/comics/` rewrite existed in
*two* places (thumbnail path as well as body), five `_fetch_source_lead_image`
call sites on the render/revalidate paths never passed `is_webcomic` (so a page
scanned there returned site chrome), and `_derive_article_lead_image` bypassed
the cache for webcomic feeds, showing the inline thumbnail instead of the cached
panel. **tinyview** now has a plugin (`TinyviewPlugin`) — its panels were in the
served HTML all along as `cdn.tinyview.com` URLs; the scan was picking
`assets.tinyview.com` chrome. Its feed also has `inject_source_images` on so all
panels render, not just the first.

**tinyview follow-up FIXED 2026-07-28.** With `inject_source_images` on, the post
rendered **14 images: 5 real panels, 5 chrome, 4 broken**. Two causes, both fixed
generically rather than per-site:

- **Plugins only ever fed the lead-image *scorer*.**
  `extract_source_gallery_urls` doesn't rank — it takes everything acceptable —
  so `TinyviewPlugin`'s −200 for `assets.tinyview.com` never applied, and the
  skeleton GIF, wordmark and three icons8 buttons walked straight in. The gallery
  now drops anything a plugin scores at or below `_PLUGIN_CHROME_SCORE`.
- **The site emits every panel twice** — once under the entry's dated path
  (`/<comic>/<yyyy>/<mm>/<dd>/<slug>/IMG_*.jpeg`, **200**) and once bare
  (`/<comic>/IMG_*.jpeg`, **404**). `_drop_duplicate_basenames` collapses a
  repeated filename, preferring the copy whose path carries the entry's slug.

Verified against the live page: 14 → the 5 panels, each confirmed 200.

**qwantz FIXED 2026-07-28 — the same miss, one step further.** Reported as "no
longer shows the comics, just a pterodactyl", right after the feed's strategy was
set to `webcomic` (which invalidates the cached leads and re-scans). The scan then
picked `qwantz.com/pteranodon.png` — a decorative image sitting first in document
order — for **all 44 strips**.

Cause: qwantz marks its panel `class="comic"`, a **bare** token. The *id* pattern
has always accepted a bare `comic`; `_WEBCOMIC_IMG_CLASS_RE` only took the
hyphen/underscore spellings, so `_extract_webcomic_panel_image` returned `None`
and the generic fallback let chrome win. Proven against the live page: the old
pattern returns `None`, the new one returns `comics/comic2-5197.png`.

The bare alternative uses **lookarounds, not `\b`** — `\b` would also fire inside
`comic-nav` and hand the panel bonus to navigation buttons. Pinned by a test.

`scripts/reset_webcomic_chrome_lead_images.py --apply` then cleared the poisoned
rows (qwantz 44, birdandmoon 4); they re-derive on next view. **Run order
matters** — the script's own docstring says to fix the picker first, or
re-derivation just reproduces the bad pick.

### 8f. DeviantArt mature images expire in ~15 minutes — needs render-time re-signing

Measured 2026-07-26, correcting two earlier wrong readings. DA signs *mature*
deviations' wixmp URLs with a very short life: a freshly-signed URL expires in
**~15 minutes** (0.01 days), and every variant shares the expiry, so there is no
permanent thumbnail to prefer. Ordinary deviations are signed permanently
(21,564 of 21,568 entries).

**Nightly maintenance cannot fix this** — a 3am re-sign yields images dead by
3:15 — so the hook was written, measured, and deliberately removed rather than
shipped. What exists now:

- `main.refresh_expiring_deviantart_images(within_seconds=…, apply=…)` — finds
  entries whose stored image token has expired and re-signs them via the API,
  using `get_deviantart_user_token()` (DA access tokens last an hour, so reading
  `app_settings` directly 401s on any scheduled run).
- `services.deviantart.image_token_expiry` / `fetch_fresh_image_url`.
- `scripts/refresh_expired_deviantart_images.py` — manual catch-up; makes the
  image work *right now*, which is all a batch pass can promise.

**Shipped 2026-07-26:** `_resign_expired_deviantart_images` runs on entry-detail
render, just before the hotlink-proxy rewrite. It re-signs only an image whose
token has *already* expired **and** whose bytes the `/api/img` cache does not
already hold — the cache key drops the signing token
(`_img_cache_key_url`/`_IMG_CACHE_VOLATILE_PARAMS`, and `wixmp.com` was already
in `_HOTLINK_IMG_HOSTS`), so once an image has been fetched under any valid
token it answers forever. That makes it one API call per *image*, not per view,
and the 21,564 permanently-signed images never reach the API at all. The fresh
URL is persisted so the list thumbnail starts from it too.

**Superseded note (kept for the reasoning):** Not an age gate:
DA signs mature deviations' image URLs with a ~7-day JWT and **every** variant
shares the expiry (checked live: `content.src` and both thumbs all
`exp=1785040938`), so there is no permanent thumbnail to prefer — the fix first
proposed would not have worked. `scripts/refresh_expired_deviantart_images.py`
re-fetches expired ones from the API and rewrites the stored HTML; 4 refreshed,
0 stale after. **Worth scheduling**: each refresh buys about another week, so
mature deviations rot again without a periodic pass.

**Superseded — the old open list:**

1. **tinyview** — a different animal. It is a JS app, and what got scraped is
   `Tinyview_skeleton-animation.gif`, the pre-hydration loading skeleton, which
   renders as "a mockup of the whole webpage sans images". Its rows were cleared
   with the rest, but re-derivation will find the same skeleton until the site
   gets a plugin/adapter (`services/lead_image_plugins.py`) that knows where the
   panel lives — or the feed is treated as needing a rendered fetch.
2. **DeviantArt mature deviations** — a separate bug, not a lead-image one: the
   entry (`CC48A953-…`, "Tifa and Aerith - Hot Spring") has **no stored content
   at all**, so neither image nor thumb can exist. Suspect the DA sync silently
   drops mature items (scope/param on the API call). Needs its own pass.

### 8c. Flaky test `test_tampered_hash_fails` — FIXED 2026-07-26

The test flipped the **last** base64 character of a scrypt digest, which is not
always a real change: when the digest length leaves slack bits in that
character, several values decode to identical bytes, so verification correctly
succeeded and the test failed on working code. It now flips a bit in the
*decoded* digest, which always changes the bytes.

Rate was never pinned down — seen failing in a full run, reproduced at 4/300
hashes once, then 0/400 in a later sample. The fix removes the class rather than
narrowing the odds, which matters more now that CI is the only reviewer.

### 8g. Extension save should MERGE into the existing post — DONE 2026-07-26

**Shipped in `d5c171b`** (`services/saved_articles.py`:
`_find_subscribed_entry_for_url` + `_merge_save_into_entry`, plus
`tests/services/test_save_merges_into_existing.py`). Verified still present
2026-07-28 — this entry had been left in future tense, which is how #7's
"archived-aware node counts" nearly got rebuilt after it had already shipped.
**Check the code before starting anything in this file.**

The design sketch below is what was built, kept as the rationale. **Still open
from this item: the time-to-read estimate** at the very bottom — that was a
pairing suggestion, not part of the merge, and it never shipped.

**The goal, in Josh's words (2026-07-26):** "open any article webpage, save it via
extension, and that would get merged into the post, with no loss of data."

Before this, an extension save always created a *new* `lectio:saved` entry, even when
the article is already subscribed. One Medium article ended up in three places —
the feed's own `/p/<hash>` copy (15KB **plus the five feed tags**), an empty
auto-filed capture, and the 44KB extension capture — with the body on one, the
tags on another, and nothing joining them. Moving copies around then lost the
body outright (fixed 2026-07-26, "carry the body when the target copy is
emptier"), but the copies themselves are the real problem.

**Matching is already solved, which is the good news.** Both Medium copies carry
the *same* `link` (the long article URL) even though their ids differ, so
`normalize_entry_link_for_dedupe` — with `get_dedupe_host_aliases` for declared
domain migrations — pairs them exactly. No per-site logic needed.

Design sketch:

- On save, look for an existing entry whose normalized link matches, across all
  feeds, before creating anything. Prefer the **feed-provided** entry as the
  survivor: it keeps updating, and it is the one carrying `entry_feed_tags`.
- Merge rather than replace: longest body wins (the extension capture usually,
  pinned via `entry_content_overrides` so refresh can't thin it), union of feed
  tags and manual tags, star set, earliest real **Pub** date kept (not the save
  time — see the Received/Pub fix), existing title kept unless pinned.
- Only when nothing matches does a new `lectio:saved` entry get created, exactly
  as now.

This subsumes several open items: the empty-capture case, tags-stranded-on-a-twin,
and much of the cross-feed duplicate work in #6 — a save that merges is a
duplicate that never happens. It is also the natural home for the entry-level
merge tool #6 wants ("keep the better body, union the tags"), since both need the
same primitive.

Worth pairing with a **time-to-read estimate** (Josh, same day): word count over
~220 wpm. Cheap to compute, but decide first whether it is derived at render or
stamped at ingest into a column (sortable/filterable later), and whether it shows
in the post list, the article header, or both.

### 8a. Article cleanup — Phase 2: promote a removal into a per-feed rule

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

### 9. Tag-as-keep — Part C: pass 1 DONE 2026-07-22, pass 2 still deferred

**Pass 1 ran with `--apply` on 2026-07-22: 3,581 archives enqueued** (dry-run and
apply agreed; the Plan's earlier ~3,596 estimate was accurate). Live DBs were
backed up first. The archive worker drains the queue in the background — expect a
long tail of 404s, since most of these are dead/unsubscribed feeds. Check
progress with `SELECT status, COUNT(*) FROM archived_entry GROUP BY status` in
the user's `lectio_starred_archive.sqlite`; at kickoff it read
`complete 14567 / pending 3576 / failed 3 / in_progress 1`.

Pass 2 (Wayback) was **not** run and stays gated on the #10 triage list.

The original write-up follows.



The semantics flip shipped (PR #150): tagging keeps + full-archives, archive kept
while starred OR tagged, unified **Kept** view, keep-on-unsubscribe (`kept_feeds`).
The backfill script (`scripts/migrate_tag_as_keep.py`) is **written and committed**,
and its dry-run has run against live data. Dry-run is the *default*; writes are
gated behind `--apply`.

**The two passes have different gates — decouple them** (this is the change from the
earlier "wait for triage" framing):

- **Pass 1, retro-archive: run it now.** It needs **no** triage. Finding replacements
  for dead feeds is about *resubscribing*, which has nothing to do with capturing
  content already collected. And it has a decay clock — it archives content from
  dead/unsubscribed feeds, which keeps getting less recoverable. The script already
  supports running it alone: `--only archive --apply`. The Plan's own stated order
  ("retro-archive first, then Wayback only the DNS-dead residual") always implied
  this; the triage gate was inherited from pass 2 and applied to both by accident.
- **Pass 2, Wayback: keep deferred.** This one genuinely benefits from triage — you
  want to know which feeds are truly dead before spending Archive.org lookups, since
  a live-but-403 site is better served by the archive worker's own page fetch. Gate
  it on the triage list from #9.

Caveat when running pass 1: it enqueues ~3,596 archive jobs, each a page fetch
against mostly-dead hosts, so expect a long slow tail of 404s and watch worker load.
Note `--limit` caps **Wayback lookups only** — it does not throttle pass 1.

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

### 10a. EEA geo-blocks are NOT a migration loss (2026-07-30)

Some US local-news sites answer **451 Unavailable For Legal Reasons** to any EEA
IP — a GDPR position by the publisher, not a bot wall:

> "We recognise you are attempting to access this website from a country belonging
> to the European Economic Area (EEA) … and therefore cannot grant you access."

The VPS is in Germany, so these refuse Lectio, and the *feed* URL 451s identically
— server-side fetching cannot work, and routing around a legal geo-restriction is
not something to build. thecentersquare.com is the example.

**But Inoreader is Sofia-based (Innologica), so it is EEA too and these sites never
worked there either.** Josh confirmed he does not recall that site working in Ino.
So this costs the migration nothing — do not log it as a regression when comparing
coverage.

**Resolution: the host migration, not a proxy.** Josh is leaving OVH when the
prepaid year ends; a non-EEA host makes these feeds work with no code at all. A
per-feed `proxy_url` was designed and deliberately NOT built — one feed does not
justify the machinery, and the obvious free option is doubtful: a Cloudflare Worker
egresses from the colo nearest its caller, so a call from Germany likely leaves
Frankfurt and gets 451'd exactly as before. Untested, and cheap to test if it ever
matters (a ~12-line Worker plus `curl "…/?u=https://ifconfig.co/country"`).

⚠ **Re-check EEA-blocked feeds after the host move** — thecentersquare.com is the
known one, and any US local-news feed that failed to subscribe is a candidate.

Distinguish it from the other walls when triaging: 451 = geo (legal, unfixable);
403 + "Just a moment…" = Cloudflare (washingtonstatestandard, realpython, behance);
plain 403 = ordinary bot rules. Only the middle kind might ever lift on its own.

**The way through for an individual article is the browser extension** — the user's
own browser has a non-EEA IP and posts the page Lectio cannot reach. 451 is already
in `_BLOCKED_STATUSES`, so a re-fetch says "blocked, use the extension" rather than
offering to delete the article as gone.

### 10. Inoreader replacement — the migration (start ~Dec 2026)

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

### 11. Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Overlaps
with #9: some "we can't fetch" feeds get fixed here instead of via the Ino proxy,
so it's worth revisiting once the comparison report sizes set (a).

### 12. Page-weight reduction — follow-ups (main work landed 2026-07-15)

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

> **Most of this section was promoted into Now #1**, which treats the dupe cluster
> as one project (correctness+safety → repeat-session UX). Tag autocomplete and the
> tag autocomplete went to Now #8 and the Uncategorized batch-align to Now #4
> (it measured far bigger than expected). Everything stays documented here in
> full; the Now entries are summaries. Nothing in this section is still deferred
> except where noted inline.

**Bugs** — *promoted to Now #1a; both dupe-scan bugs SHIPPED 2026-07-21, see there
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

**Saved dupe-scan UX** (all in the dupe dialog) — *promoted to Now #1b*

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
  **reassess only after Now #4** since auto-filing changes what the tree looks
  like: pinned saved-tag shortcuts under the Saved Articles row, and a badge
  counting total saved instead of unread (if unread proves the wrong default).
- **The Read Mode follow-ups listed above are now Now #7** ("finish the Instapaper
  clone") — they stay documented here for context, but the actionable list and its
  ordering live in Now.

### Single-post pages as first-class entries (the "feed" that is one document)

Josh has several "feeds" that are really **a single standing document** — e.g.
`https://schacon.github.io/git/everyday.html` (Everyday Git). There's no RSS to
subscribe to, and the content is a reference doc he wants to keep and re-read, not
a stream.

Current workaround (his): save as a Saved Article → create a feed → move the entry
into it. Two things make that unsatisfying, and they're separate problems:

1. **The capture is bad** — that's Now #2 (readability returns 6.7% of this
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
to file such pages into an existing, at-least-related real feed.** That's Now #4,
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
`<category>`, so all four are set-up-able today (Now #8, config not code):**

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
  earlier "must hyphenate" reading was wrong. Still worth a tag autocomplete in
  the rule form fed from `entry_feed_tags`; see the broader "autocomplete while
  typing" request now at Now #8 — build one shared control, not two.

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

> **Deprioritized 2026-07-21 by the cross-feed measurement (Now #6).** Fuzzy
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

### CI flake: "database is locked" from leaked background threads

Seen 2026-07-28 on PR #155: `test_feed_link_xss.py::test_entry_detail_empties_unsafe_link`
errored with `StorageError: while opening database: database is locked`, while
the other **1914 passed**. Passes locally, twice, on the full suite — CI-only.

Not a missing pragma: `_LectioReaderStorage` already sets
`busy_timeout=10000` and opens with `timeout=30.0`.

The mechanism is almost certainly the one behind the
`PytestUnhandledThreadExceptionWarning` the suite already emits (e.g. "no such
table: feed_media_scan"): background threads started by one test outlive it,
and because tenancy is a **global**, a leaked thread resolves paths against
whatever the *next* test just configured — so it opens and locks a DB it was
never meant to touch. Same family as the earlier flaky-CI work (reader
busy_timeout + the startup-backfill gate), and the same family as the
`test_youtube_playlist_rules` flake logged 2026-07-21.

A real fix gates background threads in tests (a fixture that refuses to start
them, or joins them on teardown) rather than widening timeouts. Worth doing:
with CI as the only reviewer, a suite that reddens at random teaches you to
ignore the one signal you have.

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
  empty. Found 2026-07-21 while checking the posts list for Now #3.
- **`templates/js/_layout_shell.js` and `templates/js/_pull_to_refresh.js`** —
  unreferenced leftovers from an earlier extraction attempt (was filed under
  page-weight follow-ups; it's a dead-code item, not a perf one). Confirm nothing
  external uses them, then drop.
- **The dormant in-app star-mode tree/JS** that the Read Mode hijack bypasses —
  see Now #7, which lists it as a Read Mode follow-up. Same sweep.
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
