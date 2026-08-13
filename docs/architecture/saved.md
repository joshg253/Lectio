# Saved

Read-it-later capture, keeping, and editing a post in place.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## Saved articles (read-it-later capture)

`services/saved_articles.py` lets users save arbitrary page URLs that come from
no feed. Rather than inventing a parallel store, a saved article is an ordinary
reader entry in a per-user synthetic feed `lectio:saved` ("Saved Articles"),
created lazily on first save with `add_feed(allow_invalid_url=True)` and
`updates_enabled=False` — the scheduler and `update_feeds()` never touch it, and
entries are user-added (`added_by='user'`), which reader guarantees never to
delete during updates. Because it's a real feed in the per-user reader DB,
tenancy, read state, tags, keyboard flows, unread counts, and feed-management
surfaces all apply with zero special-casing (the same reason FakeFeedz uses
`file://` feeds).

Saving (entry id = link = the fragment-stripped URL, published = save time):
the page is fetched and readability-extracted server-side via
`fetch_readability_article` — the same core the reader-view route uses — then
the entry is auto-starred (`saved_entries` row) and enqueued to the starred
archive, whose worker independently captures the source page + images for
offline reading. Extraction failure is deliberately non-fatal: the starred
bookmark is still created (title falls back to the URL) and the archive worker
retries the page later. A duplicate save re-stars the existing entry without
re-fetching. The on-star destination fan-out is deliberately **not** fired —
saving *into* Lectio shouldn't re-send the article to external read-later
services.

**Re-fetch follows the entry, not the feed.** `POST /articles/refresh-content`
re-fetches and re-extracts a capture, replacing its stored content in place. It
has two paths because a capture does not stay in `lectio:saved`: auto-filing
(Settings → Feeds → File saved articles) moves it onto the feed that actually
publishes the article, where it remains a capture (`added_by='user'`, entry id =
source URL) on someone else's feed. For a still-unfiled article the route reuses
`save_article(refresh_content=True)`; for a filed one it calls
`refresh_filed_article`, which updates the entry where it now lives. Routing the
filed case through the save path instead would write into `lectio:saved` and
re-create the duplicate that filing removed — hence the split, and hence
`_replace_entry_content` taking the feed as a parameter rather than assuming the
saved feed. Feed-provided entries are refused: their content belongs to the
publisher and the next refresh would overwrite it anyway.

**Batch re-fetch shares its pacing with the CLI, because a politeness guarantee
that holds in one entry point is not a guarantee.** `services/refetch_batch.py`
owns the delays (`GLOBAL_DELAY`, `PER_HOST_DELAY`, `HOST_FAILURE_LIMIT`), the
host interleave, the runtime estimate and — since a second CLI caller appeared —
`run_paced`, the serial loop that applies all of them and returns a fixed
outcome vocabulary (`ok / archive / mismatch / dead / failed / skipped_host`).
`POST /saved/refetch-scope`, `scripts/refetch_scope.py` and
`scripts/refetch_boilerplate_damage.py` all go through it rather than defining
their own. Bulk re-fetch spends someone else's bandwidth, so pacing is the design
and not a setting; fixing the vocabulary in one place is the same idea applied to
reporting, so two callers cannot count the same result under different names.
`run_paced` takes its clock and jitter as arguments purely so a test can assert
on the delays instead of stubbing them out — pacing that is only tested by not
being tested is not pacing.

Three details are load-bearing:

- **The estimate counts the per-host delay, not the global one.** A single-feed
  scope is a single host, so 89 articles is 89 × 10s, not 89 × 2s. An early
  version reported a 15-minute run as under 4 minutes — for a deliberately slow
  job, the runtime is the one number that must not be understated.
- **One job at a time, per user — but queued, not refused.** Two overlapping runs
  would each honor the pacing and together double the rate every site sees, so
  they are serialized; a second start appends to `job["queue"]` instead of
  returning 409. Serializing is a scheduling constraint, not a reason to make the
  user wait at the keyboard. A queued scope is resolved to entries when it
  *starts*, not when it is queued, because an hour in a queue is long enough for
  what is kept in the scope to change. The worker runs on a background thread
  through `_run_in_user_context`, since a raw thread loses the tenancy user.
- **The worker owns `running`, not the batch loop.** `_run_refetch_batch` handles
  one scope and deliberately does not clear the flag; `_refetch_worker` drains the
  queue and clears it once. Clearing it per batch made the status pill blink out
  between queued scopes, i.e. exactly the invisibility the pill exists to fix.
- **A refusal is not a failure.** The slug guard declining to overwrite a stored
  copy is the guard working, and it is counted apart from a fetch that broke;
  only a real failure counts toward dropping a host.

Progress is visible in a fixed status pill (`#refetch-pill`) driven by
`GET /saved/refetch-scope/status`, which reports the run in flight, the queue
(labels and counts only — not the internals a client might try to set) and the
last few completed runs. The pill is the surface for a job measured in
quarter-hours; a toast fades and takes the job's only visible trace with it. Time
remaining is computed from the job's own measured pace rather than the up-front
estimate, which is the honest number once a few articles are in.

**The mismatch guard has a second reference for opaque URLs.** `article.aspx?p=2438407&WT.rss_a=Classes in C#`
carries one usable subject word once digits and structural vocabulary are dropped
— below the guard's three-word floor — so it stood down entirely and informit's
"Articles | InformIT" section index overwrote two stored articles during a batch
run. When the slug gives nothing to judge by, the *stored* title becomes the
reference, but only in conjunction with `looks_like_a_link_index(new_html)`.
The conjunction is what makes it safe: the slug branch deliberately avoids
old-vs-new titles (re-fetch exists partly to fix a bad title), and a genuine link
roundup — Techdirt's weekly history post is 95% anchor text — still echoes its own
stored title. Thresholds are calibrated against 1,192 captured articles: p95
anchor-text ratio 0.32, p98 0.46, so the detector fires at 0.40 with a 20-anchor
floor. Note the guard protects the *first* destruction only; once an entry holds
the wrong page, its stored title is the wrong page's title.

**A third guard catches the case the other two structurally cannot: the page is
the right article, and readability returned the site's furniture.** Neither
title nor length separates those — the title stays correct and the boilerplate
is often *longer* than the post it replaced (commandlinefu.com's "is the place
to record those command-line gems…"). What does separate them is that chrome
extracts *identically for every post on a feed*, so `extraction_matches_sibling`
refuses an extraction byte-identical to one already stored against a **different
entry of the same feed**. Three properties are deliberate: the fingerprint is
over visible text, not markup, because attribute order and whitespace vary
between runs while the words do not; the comparison is scoped to one feed,
because the same text under two feeds is a syndicated post rather than
furniture; and extractions under 120 characters are exempt, because a two-line
stub can legitimately coincide and refusing those would block real re-fetches.

**The guard remembers what it has allowed this run, because the archive cannot.** `enqueue_archive` is asynchronous, so during a bulk repair `extraction_matches_sibling` could not see the batch's own writes: entry 1 is allowed, entry 2 gets the same wrong text seconds later and is allowed too because entry 1's extraction has not been archived yet. A 368-entry run on 2026-08-07 wrote identical comment-section text to five supernote entries exactly this way. The service now keeps a bounded per-feed map of fingerprints it has allowed (300 per feed, 6h TTL, cleared by `forget_recent_extractions`) and refuses a match against that as well as against the archive. An archive-sourced refusal is deliberately *not* recorded — a maybe should not become a fact.

**Corollary, and the more expensive half: the archive is not evidence of current state.** Anything measuring damage from `archived_entry.readability_html_zlib` reads a store that lags every repair. After that run, 129 of its 131 rewritten entries still had their pre-run extraction stored and still looked damaged; worse, the same lag had inflated the original damage count from the start — of 594 flagged, 327 held perfectly good bodies. `scripts/refetch_boilerplate_damage.py` therefore filters its scope, and verifies its own output, against the READER, which is what a person actually reads. Use the archive to find candidates; use the reader to decide.

`sibling_extraction_entries` is the same test in bulk — "which stored
extractions already are boilerplate?" rather than "would writing this one be?" —
and lives next to the guard rather than in the repair scripts, because two
implementations of one judgement is how a repair script starts disagreeing with
the thing that prevents the damage. Both `scripts/revert_boilerplate_refetches.py`
(restores from a local snapshot, no network) and
`scripts/refetch_boilerplate_damage.py` (re-acquires from the network when there
is no snapshot) call it. In the second, the live guard also becomes the repair's
safety net: a page that still extracts to the same boilerplate is refused, so an
unrecoverable entry keeps what it has rather than being re-damaged.

Scope is kept articles (starred or tagged) with an `http(s)` link — the same rule
the single-article button uses, because an unkept feed entry is rewritten by the
next refresh anyway. Everything the interactive re-fetch does still happens per
entry: the previous body is snapshotted (so any one result is revertible), a
plainly-different page is refused, a refusal falls back to the Wayback Machine,
and a missing publish date is learned on the way.

**Whole-page capture is an opt-in `mode`, on both the save and re-fetch paths.**
`mode="full"` swaps `fetch_readability_article` for `fetch_full_page_article` —
same sanitizer and post-processing tail (`_finalize_article_html`), but the
body-selection step keeps everything instead of scoring it. It exists because
readability doesn't merely under-extract on document-shaped pages, it picks the
*wrong node*: on a DocBook-style export (84 `<p>` across 68 `<div>`, no
`<article>`, 13 `<pre>`) it returns a single shell-session `<pre>` and drops the
prose, because it scores containers by paragraph density.

Reachable from `POST /articles/refresh-content` (UI: **Re-fetch full page**) and
from `POST /articles/save` (UI: the **Capture the whole page** checkbox). Having
it at *save* time matters because the re-fetch form only helps once an entry
exists — a page shape known to extract badly would otherwise have to be captured
wrong first. **Never the default, and matched exactly against `"full"`** so a
stray value can't silently widen a capture: on a blog-shaped page this keeps the
nav and sidebar chrome readability strips, so it is the escape hatch rather than
the better option. The modal's checkbox resets on every open for the same reason
— it describes one page's shape, not a standing preference.

The UI gates the control the same way, on a per-entry `captured` flag
(`data-post-captured`) rather than on feed identity. Gating on the feed is what
silently stripped the escape hatch from every article the filer moved.

**One layout owner, three modes.** The inline shell in `index.html` resolves
`wide` / `medium` / `single` from a single `updateSingleMode()`, at 1100px and
720px. Single-pane mode was removed in `9dab5a8` and revived rather than replaced
with a phone-specific renderer, and that is the whole design argument: a second
renderer means every feed-appearance feature — lead images, per-feed thumbnail
crop and zoom, embeds, the full-image webcomic view — has to be ported to it, and
every future one silently misses it. The phone runs the same markup, so it
inherits all of them and everything added later.

The revival was wiring, not a rewrite: `9dab5a8` stubbed `isSingleMode` /
`setSinglePaneLevel` as no-ops but left ~10 call sites in `app.js` intact, and
left `templates/js/_layout_shell.js` on disk holding a complete second
implementation that was included nowhere (now deleted — dead code that looks live
is worse than none). Porting into the existing shell rather than re-including that
file is what stops two shells disagreeing about the current mode.

Three details worth keeping: the pane level is clamped to 0–2 and persisted in
`sessionStorage`, because a pane swap re-runs the shell and would otherwise drop
the reader back to the folder list; an `entry_id` in the URL overrides the
remembered level, so a shared or reloaded article opens the article; and hidden
panes are `display: none` rather than translated off-canvas, since laying out and
fetching a hidden pane's images is the expensive half of a page on a phone.

**The tree is not re-rendered by pane-swap navigation, so anything the server
stamped into it goes stale.** `updateScopeActiveState` already re-derives active
rows and mode blocks for that reason; sidebar tag links now get the same
treatment, because their server-rendered `folder_id` otherwise survives every SPA
navigation for the life of the page — open a folder, click Feeds, click a tag,
and you are back in the folder you left. The stamp reads the URL's *own*
`folder_id`/`list_feed_url`, captured before the fallbacks in that function
reassign them from whichever row is still lit: those fallbacks exist to stop
active-state flicker on bare URLs, and reusing their result here would reproduce
the staleness rather than fix it. `resume_read_filter` is refreshed alongside,
since a tag view forces `read_filter=all` and carries the filter to come back to.

**A URL can carry a month without carrying a day.** `url_inferred_pubdate` reads
the `/YYYY/MM/DD/` permalink; `url_inferred_pubmonth` reads `/YYYY/MM/` and
resolves it to the first of the month. The day is a placeholder, the month is not
— WordPress generates the permalink from the publish date. It is the last tier in
`recover_publish_dates.py` for that reason, but on blog.guitar-pro.com (67 of the
68 entries it recovered) it is also the *only honest* signal: those pages publish
`dateModified` and nothing else, so mining the page would have dated a 2021 post
to October 2024. A real month beats a precise-looking lie.

**A re-fetch moves Received, never Pub.** Surfacing a re-pulled capture at the top of the backlog used to be done by writing `entries.published = now` — which corrupted the data it sorted by. Pub means the date the article was published, and re-fetching does not republish it; worse, under a **Pub oldest** sort the bump did the opposite of surfacing, sending the article to the far end of the list. Measured on the live library 2026-07-25: **101 entries** had lost their real publish dates this way, some by 16 years. `replace_entry_content(bump_received=...)` now moves the Received date instead — which is honest ("this content arrived just now"), sorts correctly in both directions, and needs no new per-entry field. Both received columns move together: `first_updated` backs `Entry.added`, which the UI displays and the render-path sort reads, and `recent_sort` backs the list's SQL fast path.

`scripts/restore_bumped_publish_dates.py` repairs entries already damaged. The original date is recovered from the starred archive (`archived_entry.published_at`), which snapshots each entry's dates at capture and is untouched by a content re-fetch, and is cross-checked against reader's own `recent_sort` (the entry's original sort position, likewise untouched). Only forward drift qualifies — a bump can only move a date later — and by default only rows where the two independent records agree are restored; on the live library that was all 101, with zero disagreements.

Worth knowing when touching the received sort: the two list paths read **different columns** for it. The SQL fast path orders by `recent_sort`; the render path sorts on `received_timestamp`, i.e. `Entry.added`/`first_updated`. They agree for ordinary ingested entries and can disagree for entries written outside the normal path, which is why the bump above writes both.

Entry points: the **+ Save Article** modal (session `POST /articles/save`), a
bookmarklet (`GET /articles/save?url=…` — a top-level navigation, so the
SameSite=Lax session cookie rides along and an unauthenticated hit round-trips
through `/login?next=`), and `GET|POST /api/save` for share sheets/shortcuts.
`/api/save` follows the Fever model: session/CSRF-exempt (both prefix lists),
authenticated by `username` + the per-user API token, and bound to the user
with an explicit `tenancy.user_context` before the threadpooled save runs (the
blocking fetch must not stall the event loop).

**Extension save protocol** (`POST /api/bookmarklet/save`): Lectio implements
the wire format of the Readit browser extension / bookmarklet —
`{token, url, html, title}` — so that extension, pointed at a Lectio Backend,
becomes a Lectio save-extension. The value of the shape: `html` is the
**rendered DOM captured from the user's authenticated browser**, so
paywalled/bot-walled pages arrive with full text and the server performs *no
fetch* (`extract_readability_article` runs the same readability pipeline on
the provided HTML; absent `html` it falls back to the normal server-side
fetch). Auth is token-only (`UserStore.user_for_api_token` — bare-token
resolution, constant-ish comparison count across users) since the payload
carries no username; the route is session/CSRF-exempt and answers CORS
preflights with a wildcard origin (safe: auth lives in the JSON body, no
cookies), which is required because the extension's `host_permissions` don't
cover third-party backends, putting its fetch under normal CORS. Captured
HTML is capped at 6.5M chars, mirroring the extension's own truncation.
A captured-DOM save of an **already-saved URL** is treated as a deliberate
re-capture (the user often cleaned the page in-browser first): extraction
re-runs on the new DOM, the stored content is replaced (direct column write
in reader's JSON shape — EntryData has no public setter), and the entry bumps
to the top of the backlog (published/saved_at = now). A pinned title (Edit
title…) is never clobbered; URL-only re-saves (bookmarklet, /api/save) stay
light re-star no-ops. One special case: the extension captures whatever tab
it's on, so a capture made *from inside Lectio* would bookmark Lectio's own
UI page —
`_unwrap_lectio_reading_url` detects a submitted URL on this instance
(request host or `LECTIO_PUBLIC_URL`), extracts the wrapped
`feed_url`/`entry_id`, and **stars that entry** instead (the native
save-for-later; no on-star fan-out, matching direct saves). If the wrapped
entry has aged out but its id is itself an http(s) URL (common — many feeds
use the article URL as the guid), that URL is saved as a normal article via
server fetch, since the captured DOM is Lectio chrome rather than the
article.

**Saved Articles sidebar view.** The tree's top row (`.saved-items-row`,
restored from the pre-2026-04-20 sidebar with its surviving CSS/JS) opens the
all-starred view (`star_only=1` at the root folder) with an unread-starred
count badge (`get_saved_unread_count`; kept live client-side by
`adjustSavedUnreadBadge` on single-post read toggles and star toggles — bulk
operations let it drift until the next render). Saved mode and Feeds mode are
**mutually exclusive tree blocks**: entering Saved expands its own folder
sublist (`.saved-tree-children` — folders holding saved items, badges =
*total* saved per folder via `get_saved_counts_by_folder`, since the view
defaults to the whole backlog) and collapses the feeds tree
(`.feeds-tree-children`), and vice versa; the pane-swap path toggles the two
blocks in `updateScopeActiveState` since it never re-renders the tree.
Entering Saved always opens on **All** and stashes the current read filter in
`resume_read_filter`; the **All Feeds** row (and the *active* Starred menu
item) exits star mode restoring that filter. Feeds-tree links never carry
star mode (each block is only visible in its own mode). Within star mode the
read filter **composes** instead of being ignored:
`read_filter=unread&star_only=1` narrows to unread starred
(`list_entries_for_feeds` skips read entries regardless of star mode; only
`history` stays exclusive with starred since it sorts by read time).
Archive-only orphans are excluded from the unread narrowing — they are read
by definition (no live entry). Clicking the Saved Articles header is an
**expand-only landing** (`saved_home=1`: no posts load — the whole backlog is
expensive); the sublist starts with an **All** row (the full-backlog view at
the root folder) and ends with **Uncategorized** (saves in unfoldered feeds,
including everything in `lectio:saved`). In saved mode the sublist is the
scrollable region and the All Feeds row pins to the bottom above Tags
(`.tree.saved-mode` flex layout). The `lectio:saved` feed itself is excluded
from the Uncategorized *display* set (feed list, unread badge, and the row is
hidden in Feeds mode when it's the only unfoldered feed) but stays in the
Uncategorized *view* set, so the Saved sublist's Uncategorized folder reaches
its entries.

### Tag-as-keep — the unified Kept view

Tag and Star are the two **keep** axes: a post is kept (offline-archived and
never auto-pruned) whenever it's starred **or** manually tagged. Tag is the
"keep forever" axis, Star the lightweight "to-do". The archive
(`archived_entry`, keyed on `(feed_url, entry_id)`) is independent of the
`saved_entries` star table, so tagging can enqueue a capture without a star row.

- **Enqueue on tag.** `set_manual_tags_for_entry` enqueues an archive when a tag
  is added and enqueues a removal when the **last** tag is removed *and* the
  entry isn't starred. `delete_manual_tag_everywhere` applies the same
  last-tag-and-unstarred release per entry. The star toggle's off-branch is
  likewise guarded — it only releases the archive when the entry carries no
  manual tag. The shared guard is `_entry_should_keep_archive` (= starred OR
  has ≥1 manual tag); dropping one axis never wipes an archive the other needs.
- **Kept view.** The Saved-mode entry list (`list_entries_for_feeds` with
  `star_only=1`) filters on **star OR tag**: alongside `saved_entries_set` it
  loads a `tagged_entries_set` (one `entry_tags LIKE` scan over the view's
  feeds), unions them into `kept_entries_set`, and both the point-lookup fast
  path and the membership filter use that union. `saved_entries_set` still drives
  the per-row `saved` flag. `get_saved_counts_by_folder` /
  `get_saved_unread_count` count the union too.
- **Kept-but-unsubscribed feeds.** `reader` requires a feed to exist for its
  entries, so unsubscribing a feed that carries curation defaults to a **keep**
  mode (`keep_entries=1` on `/feeds/unsubscribe`, the default radio in the
  curation dialog): it deletes the `folder_feeds` rows, `disable_feed`s the feed,
  records it in the new meta table **`kept_feeds`**, and force-flushes pending
  captures — but does **not** `purge_orphaned_feed`, so the reader feed, its
  entries, tags, and stars survive. Kept feeds are hidden from the tree by
  excluding them at the source: `get_all_reader_feed_urls()` subtracts
  `get_kept_feed_urls()` by default (so All Feeds, Uncategorized, and counts drop
  them), while the Saved/Kept view passes `include_kept=True`/unions the kept set
  back so their curated items stay browsable **grouped under their original feed
  name**. Re-subscribing (`add_feed_to_folder`) clears the `kept_feeds` row and
  re-enables updates; a later full delete (`purge_orphaned_feed`) drops it.
  `kept_feeds` is created in `ensure_meta_schema` (covered by the startup
  per-user migration, so existing tenants don't 500).

**Tagging orphan archives.** `_build_orphan_entry_detail` renders an entry whose
feed is gone from `reader` entirely from its `archived_entry` row (see "Removing
a feed" above). Star already worked on these — `apply_star_state` writes
`saved_entries` directly by `(feed_url, entry_id)`, with no `reader` dependency
— but manual tags piggyback on `reader`'s own `entry_tags`, keyed to a
`resource_id` that only exists if `reader.get_entry()` finds something. An
orphan has nothing there, so tagging one silently no-op'd: `set_manual_tags_for_entry`
returned `[]`, the route reported `{"ok": true}`, and the UI cleared the input
with no chip and no error (reported 2026-08-09, `#pshell` on an orphaned
`packtpub.com/rss.xml` item — Packt migrated to `hub.packtpub.com` years earlier
and the old feed was long gone, but the archive capture and its "kept" status
survived).

`orphan_entry_tags` (meta DB, `(feed_url, entry_id, tag)`) gives tags the same
independence Star already had, as a fallback *only* — every manual-tag entry
point (`get_manual_tags_for_entry`, `set_manual_tags_for_entry`,
`delete_manual_tag_everywhere`, `rename_manual_tag_everywhere`,
`has_any_manual_tags`, `get_all_manual_tag_names`) tries `reader` first and
falls back to this table only when `reader.get_entry()` misses. Normal
(non-orphan) entries are untouched — this never becomes the primary path for
anything `reader` can already answer.

**A surviving capture is not itself a keep signal.** `_build_orphan_entry_detail`
used to hardcode `"kept": True` for every orphan, purely because a complete
`archived_entry` row existed — a weaker definition of kept than everywhere else
in the app, where it strictly means star OR tag. Measured on the live library
2026-08-09: of 1,279 orphans, 1,089 (85%) were already genuinely starred, but
190 carried neither signal — mostly historical (the packtpub batch above, 89
of them) rather than anything current-day tagging/starring produces. `kept` and
`saved` on the orphan detail dict now reflect the real signals (star via
`_entry_is_starred`, tag via `orphan_entry_tags`), and
`get_orphan_saved_entries` filters to the same rule before the Saved/Kept view
ever sees a row — an uncurated leftover capture no longer appears as Saved. It
still opens fine via a direct link (`get_entry_detail`'s fallback doesn't
gate on curation), so nothing is deleted or hidden from direct navigation —
only the "is this Saved" list membership and flag changed. The user-visible
effect: navigate to one of the 190 and it now looks correctly *un*starred and
untagged, with the tools to fix that right there instead of looking identical
to something you'd actually curated.

**Orphans and search.** `merge_orphan_saved_entries` used to be skipped
outright whenever a search query was active (`not search_query` at both call
sites) — reported 2026-08-09: searching "packtpub" turned up nothing, because
orphans were never in the candidate set search ran over at all. Orphans have
no reader row, so the SQL-narrowed search paths (`_filter_star_keys_by_search`
etc.) can't reach them regardless. Rather than excluding orphans from a
search, `get_orphan_saved_entries` takes an optional `search_terms` list and
matches it in Python against title/link/feed_title/author — same
AND-across-terms rule as the rest of search, same tokenization
(`search_terms_from_query`), just not routed through SQL. Deliberately
metadata-only, not the archived body text: decompressing every orphan's
`content_html_zlib` on every search would cost more than the orphan set's size
(low thousands at most) justifies today; revisit if that ever feels thin.

**The discoverability dead-end this created, and why the 190 got deleted.**
Once "kept" meant star-OR-tag, the 190 uncurated orphans stopped appearing
*anywhere* — not the Kept view, not search — with no path back to one except
an exact bookmarked URL (which is how the packtpub orphan was found in the
first place). An item you cannot find is one you cannot curate, so leaving
them stranded-but-present served no purpose. `scripts/purge_uncurated_orphan_archives.py`
deletes exactly this set (orphan AND no star AND no tag) via the existing
`delete_archive` cascade (asset rows and now-unreferenced asset blobs go with
it). Run 2026-08-09: 190 deleted, 89 of them the packtpub batch. A curated
orphan (star or tag) is untouched regardless of how old or how dead its feed
is — this only ever removes captures with zero keep signal.

**Searching the Kept view.** The kept branch in `list_entries_for_feeds` runs
*ahead* of the generic `elif search_terms` fast path, so for a long time this was
the one view where a search took no fast path at all: it hydrated every kept key
via `reader.get_entry` and filtered in Python — ~11k lookups, measured at ~19s
per search on a real library, which reads as a search box that does nothing.
`_filter_star_keys_by_search` now narrows the keys in SQL first (the same
keys-joined-against-`entries` technique as `_sorted_star_key_window`), so only
the survivors are hydrated: ~1.2s for the same queries.

reader's own FTS index is deliberately **not** used for this. `search_entries`
builds a highlighted snippet per result, measured at ~7.8ms/row — 76s for one
common term across 133k entries — so routing the kept view through it was *worse*
than the scan it replaced (97s end to end). The SQL predicate
covers title, resolved feed title, feed URL, link, author, summary **and the
stored content**, so a Saved search reaches the article's text — the point of a
read-later archive. Content is matched as stored (raw HTML), so a markup-ish term
matches nearly everything; stripping tags would need a plain-text column
maintained at ingest, which isn't worth a schema change yet. On any SQL error the
helper returns `None` and the caller keeps the full key set and post-filters in
Python, so a failure degrades to the old behavior instead of showing no posts.

**Searching the Feeds view** (`_search_entry_keys_in_sql`) now works the same
way, for the same reason. It previously used the FTS index, and the snippet cost
above turned out to be ~95% of the time — measured on the live library (134k
entries, 2,888 feeds): `search_entries` took 19.7s for `python` against 1.3s to
hydrate the results. Narrowing to matching keys in SQL and hydrating only the
survivors took the same search to **1.45s**, and `guitar` from 9.3s to 1.3s.

Two consequences worth knowing. First, the two search surfaces now share a
predicate, so they finally agree: a Feeds search reaches article text rather than
only metadata (`coffee`: 833 → 1,237 hits), and inherits the same raw-HTML
caveat. Second, when the selected feed set fits under SQLite's 999-variable limit
the scope goes into the query, so `LIMIT` applies to rows the user can actually
see; above that it matches unscoped and the caller drops out-of-scope feeds — the
same shape (and the same under-fill caveat) the FTS path had.

**reader's FTS index is retired.** Both surfaces resolve in SQL, so nothing
called `search_entries` — and maintaining the index was not free: 1.3ms per new
entry on every refresh (`update_search()` ran at the end of each refresh batch,
on every save, and after imports), plus a file roughly the size of the reader DB
itself — **564MB against 743MB** on the live library. It is no longer built,
enabled, or updated, and the startup index-build thread is gone with it, so a
fresh install no longer spends its first minutes walking every entry.

`scripts/drop_search_index.py` reclaims the space on an existing install.
`disable_search()` alone does *not* reclaim it: the DROPs land in the WAL and
SQLite never shrinks a file on its own, so a naive drop briefly **doubles** disk
use (measured: 564MB index + 567MB WAL). The script checkpoints and VACUUMs,
taking the index to 4KB.

The index is derived, not user data — `enable_search()` + `update_search()`
rebuilds it from the entries table should a future ranked search want it. That
rebuild walks every entry and takes minutes on a large library, which is why
dropping it is a deliberate script rather than a startup side effect.

**Auto-filing saved articles** (`services/saved_autofile.py`, `GET /saved/autofile/preview`, `POST /saved/autofile`, driven from the top-level Settings → **Utilities** tab, which also holds the two duplicate scanners and the one-shot maintenance actions; it was promoted out of a Feeds sub-tab so the scanners — long-running, long reviewable lists, worked in repeated passes — sit alongside the rest rather than behind an extra click). A read-later library imported from a feed reader is mostly articles from feeds already subscribed to, so they can be filed onto their real feed — which also collapses cross-feed duplicates for free, because `_move_entry_to_feed` matches into the target by GUID else normalized link.

Matching is by **article host**, from two independent signals. The evidential one is which subscribed feed already carries entries whose links are on that host — a feed's own URL often lives elsewhere than the articles it publishes (`rss.beehiiv.com` serving `joanwestenberg.com`). The declarative one is the hosts a feed *advertises*: its own URL host and its `link` (site) host. Entry links alone are not enough, because two common cases produce no usable evidence at all — a feed **subscribed but not yet fetched** has no entries, so a feed added specifically to receive a backlog would never be offered for it; and a **link-proxying feed** (FeedBurner rewrites every entry link to `feeds.feedburner.com`) points its evidence at the wrong host entirely. Measured here, 696 of 2,881 feeds advertise a site on a different host than their feed URL. Adding the declarative signal took unmatched articles from 698 to 66.

A declared host makes a feed a candidate; it makes it *confident* only when the feed is also stocked (`feed_sizes` ≥ `MIN_SUPPORT`). Without that size check a scraped one-article URL sitting in the subscription list — right host, plausible title — would read as the site's real feed and collect the site's whole backlog. Two guards decide what may be *pre-approved*, and the distinction matters — measured against real data, `guitarworld.com`'s target was backed by 77 of the feed's own entries while `guitarplayer.com`'s only candidate was a scraped single-article URL with **one** supporting entry, and filing 303 articles into it would have been wrong:

- `ambiguous` — more than one *on-host* subscribed feed carries entries on the host, so picking one would be a guess.
- `support` — how many of the target feed's own entries are on that host; below `MIN_SUPPORT` the cluster is shown but not pre-checked.

The service is pure (it takes extracted rows, not a reader) so the guards are testable without a database. The preview is read-only and strips `entry_ids` before sending; apply takes the target from the request rather than recomputing it, so what was approved is exactly what runs.

**Filing is batched, and has to be.** `_move_entry_to_feed` runs at roughly 17 articles/second, so one uncapped call over a large host (1,300+ articles) takes well over a minute and gets cut off in flight — observed live as `POST /saved/autofile → status 0, 16180ms`, where 278 articles really were filed but the reply never arrived, so the UI looked untouched and the articles appeared unmoved. The endpoint therefore caps each call at `_AUTOFILE_BATCH` articles and reports `remaining`; the client loops, showing progress, until nothing is left. A batch that reports work outstanding while moving nothing breaks the loop rather than spinning.

**Unstarring tagged articles** (`services/unstar_tagged.py`, `GET /saved/unstar-tagged/preview`, `POST /saved/unstar-tagged`, UI: Settings → Utilities → *Unstar tagged articles*). After the tag-as-keep flip a tag keeps an article on its own — tagged entries are archived and pruning-protected independently of the star — so a star on an already-tagged article is redundant, and it only clutters Saved, which is meant to be the read-later queue rather than a second copy of the filing system. Nothing is lost by dropping it: pruning protects starred and manually-tagged entries *independently*, and the unstar route only enqueues archive removal for an entry with no manual tags, so the offline capture survives either way. Only the star row is deleted. The service is the pure decision layer (it takes the current curation and returns what would change); all DB access and the cache invalidation a behind-the-back write needs stay with the route, which recomputes the plan server-side under the submitted opt-outs rather than trusting a client-supplied id list.

**The UI inverts the API's opt-out, on purpose.** The endpoint takes `keep_tags` — the tags to *protect* — but a panel that rendered that directly would show all ~58 affected tags pre-checked, making "unstar everything" the default and *unchecking* the destructive act. That breaks the rule that a bulk-action list arrives with nothing selected. The panel therefore selects tags to **clear** and derives `keep_tags = every affected tag − selected` before each call, so an empty selection is an empty action. The inversion looks redundant and is pinned by tests for that reason.

**Per-tag counts cannot be summed**, which is why the action button's number comes from the server. An entry is protected by *any* kept tag, so an article tagged both `python` and `books` survives a selection of `python` alone even though it is counted in the `python` row. Adding the checked rows client-side would therefore over-promise. Every selection change re-requests the preview under the derived `keep_tags` and shows `to_unstar`, with an out-of-order guard so a slower earlier reply can't overwrite a newer count. Tag names suggesting a reading queue (`read`, `todo`, `later`, `queue`, …) are flagged via `queue_like_tags` and excluded from "select all topical tags" — for those the star *is* the queue, not a redundant copy. Measured zero matches on live data twice, but tag vocabularies drift and this cleanup is meant to be re-run.

**Two different "this isn't a feed" decisions**, deliberately labelled apart in the UI because they can appear on the same row:

- **`non_feed_subscriptions`** (`POST /saved/autofile/non-feed-subscription`, UI: *not a feed*) bars a **subscription** from ever being a filing destination. Some subscriptions are a single article URL that got added as a feed; they sit on exactly the right host with a plausible title, so they are the target you would pick by mistake, and filing a site's backlog into a one-article stub is the worst available outcome. The subscription itself is left alone — its entries are real reading — and it is fed into `_autofile_excluded_targets`, so it is barred on the apply path too.
- **`autofile_non_feed_hosts`** (`POST /saved/autofile/non-feed`, UI: *one-off saves*) settles a **host** whose saves never came from a feed at all.

**Hosts settled as one-off saves** drop out of the worklist entirely. Some saves never came from a feed at all — a cheat sheet, a one-off tutorial — and the filer can only observe that no subscribed feed matches, so it re-proposed them on every pass and they never resolved. Marking is purely a worklist decision: the saved articles are untouched and stay exactly what they already are, standalone read-later captures. Marked hosts are reported back in a collapsed section with an undo, so the decision is reviewable rather than a black hole, and the mark keys on `article_host()` — the same normalized form the plan groups by, or a host would return under its `www.`/cased spelling. The table is created in `ensure_meta_schema`, which the startup per-user migration runs for every tenant; a meta table added anywhere else 500s for users provisioned before it existed.

**Nothing in the plan is ever pre-checked.** It files thousands of rows at a time and is meant to be worked in passes — file a batch, re-scan, continue — so the `confident` flag drives a *label* ("strong match — N posts from this host"), never a selection. Same rule as the Saved duplicate dialog: a scan result is a claim, not an instruction.

**Barred targets** (`_autofile_excluded_targets`, applied on *both* preview and apply so a stale plan can't route around it): Saved Articles itself, and every YouTube feed. A saved page is never really a video-channel post, and channels routinely share a name with the blog they accompany — with only feed titles on screen, a YouTube feed is precisely the target a reviewer would pick by mistake. For the same reason the picker shows each candidate's **feed URL**, inline and as a hover title: feed titles are frequently and deliberately unlike their URLs (`rss.beehiiv.com/feeds/XYZ.xml` titled "The Woodshed"), so the title alone is not enough to identify what you are filing into. When two candidates for one host share a title (a feed and its format variant, or a blog and its companion channel) the URL is folded into the option label itself — otherwise the dropdown offers choices that read identically and the pick cannot be made at all.

**Reviewing an ambiguous host by hand.** A host with more than one on-host feed (Medium, Ars Technica) is `ambiguous` — the filer can't pick a destination, so those saves stall in the worklist. Each row carries a magnifying-glass link that opens exactly those saves: `list_feed_url=lectio:saved&read_filter=all&q=site:<host>`. Two pieces make it precise. The **Saved Articles feed** (`lectio:saved`) holds only articles *not yet attached to a feed* — filing moves them onto the real feed and out of this synthetic one — so scoping the view to it drops the host's already-filed posts; it is synthetic and absent from `get_all_feed_urls`, so `filter_feed_urls` allows it through explicitly. The **`site:<host>`** search operator (`_split_site_terms`) matches an entry's *link host* (apex or subdomain, boundary-checked) rather than a bare substring, so a mention of the host in some article's body doesn't pull that article in. `site:` never reaches the text-matching SQL/haystack paths — it is a link-host filter applied during hydration, and forces the full-scan path (`need_all`) so nothing is clipped before it runs. From there each is filed by hand with the per-post **Move to feed** action. Frontend-only wiring; the `site:` operator and the synthetic-feed allowance are the only server changes.

**Moving a saved article deletes its source.** `_move_entry_to_feed` normally leaves the source entry in place — reader can't delete feed-provided entries, so it settles for marking them read and stripping star/tags. That rationale does not apply to `lectio:saved`, whose entries are `added_by='user'` and therefore properly deletable, so the saved source is hard-deleted (tombstoned, via the shared `_hard_delete_entry`) once the move succeeds. Without this the Saved Articles feed kept a read, unstarred husk per filed article: the backlog never shrank as it was filed, and every later duplicate scan re-read rows that were no longer real saves.

**Toolbar listeners must be delegated.** `loadScopePanesWithoutFullRefresh`
(every sidebar/folder/scope click, and the search form itself) re-renders the
toolbar, replacing its DOM nodes. Any listener attached directly to a
`#toolbar-*` node at init dies with the node it was bound to — silently, with no
console error. That is exactly how the search button came to do nothing at all
after the first in-page navigation, while still working on a direct URL load
(which is why it survived testing). The search button, its clear control, the
query input, and the search form's `submit` handler are therefore all delegated
from `document`. Wire anything new on this toolbar the same way.

## Saving an article you already subscribe to

An extension save used to create a `lectio:saved` entry unconditionally, so an article you already follow ended up as two posts — and they were never equivalent. The feed entry carries the publisher's tags (`entry_feed_tags`) and keeps updating; the capture carries a body the server often cannot fetch at all (Medium and treblezine refuse this host outright). Split apart you get an article with tags and no text beside one with text and no tags, which is exactly what happened to a Medium post on 2026-07-26 — before a "move to feed" onto the empty twin dropped its 44KB body entirely.

`save_article` now takes an injected `find_existing_entry`. `main._find_subscribed_entry_for_url` resolves an article URL to an entry in another feed by **canonical link**, not id: the two rarely agree (Medium's guid is `/p/<hash>` while the URL is the long slug) but both carry the same `link`, and `get_dedupe_host_aliases` folds declared domain migrations in. A feed-provided entry wins the tie — it keeps updating and holds the tags a capture cannot supply.

The merge keeps whichever body is longer, pinned through `entry_content_overrides` so the feed's thinner copy can't overwrite it on the next refresh, then applies the resurface a save already implies: star, un-archive, mark unread. A save that finds nothing behaves exactly as before. Without the hook — any caller that doesn't pass it — behavior is unchanged, which is what keeps the service testable in isolation.

This is the primitive the cross-feed duplicate work (Plan #6) needs as well: a save that merges is a duplicate that never happens.

## Re-fetch on keep

Both Star and Tag already `enqueue_archive`, so an offline capture (page +
readability) is taken on either. That capture is what Read Mode and
`/entries/readability` read. It does **not** touch the entry pane, which shows
stored feed content — so a truncated feed still showed its teaser there.

`_maybe_autofetch_on_keep` closes that, narrowly:

- **Only when the stored copy is thin**, judged by `_archived_copy_is_plausible`,
  the same test the reader uses to reject a failed extraction. Re-fetching a good
  copy can only make it worse — the live page may now be a paywall, a cookie wall,
  a 404, or a readability miss that locks onto a sidebar (the illogicalcontraption
  case). Overwriting a good article at the exact moment the
  reader marked it worth keeping is the failure being avoided.
- **Only from the star and tag ROUTES**, never from `set_manual_tags_for_entry`.
  That service is also driven by the feed auto-taggers, at ingest, across
  everything a refresh just delivered — hooking it would turn one refresh into a
  burst of outbound requests at a single host.
- **Never for a `lectio:saved` capture**, which was fetched from the page already.

It runs off-request through `_run_in_user_context`, since a bare thread would lose
the tenancy user and fetch as the default one.

**A refusing host is remembered.** Because this is a side effect of tagging rather
than a request, a site that declines us must not be re-asked on every tag —
DeviantArt answers this server with 403 every time, and tagging across a watchlist
would be dozens of requests it has already refused. After a failed automatic
re-fetch the host is paused for six hours (in memory; it is a politeness memo, not
a record, and it is bounded). Manual Re-fetch ignores the pause entirely: that is
a person asking on purpose.

**The mismatch guard was too blunt, and it broke this feature on arrival.**
`_page_is_a_different_article` compared the URL slug against the fetched *title*
only, and refused on zero overlap. But a descriptive slug and a specific title
disagree routinely: whiskyadvocate.com/peated-whisky-cocktail-for-summer is headed
"Charred Garden Smash", the drink's name, sharing not one word with its own slug.
That refused the manual re-fetch and the automatic one alike — one bug reported as
two. The guard now also consults the fetched **body** before refusing, which does
not weaken what it was built for: a parked "Empowering Relationships" page does not
mention ornaments or dingbats either, and a section index does not discuss the
article it replaced. Zero overlap across title *and* body is a far stronger signal
than zero overlap with a title, which is normal.

**Re-fetch is available on any entry with a link** (2026-08-02). It used to
require a capture, star or tag, on the reasoning that the next feed refresh would
undo the replacement — but the **pin** is what prevents that, and
`refresh_captured_article` applies it to every non-capture entry
(`pin_content=not is_capture`) regardless of whether anything keeps it. The gate
was guarding a hazard already handled, and its practical effect was that repairing
a truncated post meant tagging it first, filing something you may not want filed
just to read it properly. The real protections are unconditional and stay: the
mismatch guard, and the pre-replacement snapshot in `entry_content_edits` that
makes any re-fetch one click to Revert. Kept-ness still decides one thing — the
offline **archive** enqueue, since a capture with no keep signal holding it is
precisely the husk the unstar path has to clean up.

Separately, the right-click **Re-fetch** items gate on `data-post-kept`, and only
starring kept that attribute current: tagging re-rendered the entry *pane* and
left the list row — the thing actually right-clicked — stale until a reload. The
tag handlers now sync the row from the server's reply (`data.tags`, the normalized
and capped set), OR-ing the star back in so clearing the last tag off a starred
post does not un-keep it.

## Node bulk actions, and what a re-fetch may replace

**Node bulk actions are scoped to the drilled-into view, and Read Mode gets buttons
rather than a menu.** `_scope_starred_keys(folder_id, list_feed_url, tag)` resolves
the stars in the *current* view — feed **and** tag together, since the case is
"drilled down to a single feed with stars I don't need". Stars only: a tagged-but-
unstarred entry has no star to remove, and unstarring is not how a tag is dropped
(that is *Delete tag everywhere*, which already existed in the sidebar context menu).

`POST /saved/unstar-scope` recomputes the set server-side and goes through
`apply_star_state` **per entry** rather than issuing one bulk `DELETE`. That is not
fastidiousness: the unstar path releases the offline capture and hard-deletes a
`lectio:saved` husk once no keep signal remains, and a bulk delete skips both,
leaving orphaned captures and invisible husks.

Read Mode has no right-click, and long-press there offers only text selection — so
the actions render as visible buttons in the browse header, plain forms in the same
navigation model as the Sort switcher (no JS, one e-ink repaint). **Deleting a tag
takes two taps**, the first arming a row that spells out what goes, because a
browser `confirm()` is awkward to hit on that WebView. The row never appears on the
Archive node: that is a review surface, not a place to bulk-destroy curation.

**A re-fetch snapshots the body before replacing it, and falls back to the archive
when the live page is refused.** Two entries were destroyed before either existed —
the-digital-reader served a parked page returning 200, and informit's
`/articles/article.aspx?p=…` hid its subject in the query string while its path
("articles") matched the site index title. Each needed a backup dive, and one was
unrecoverable.

The snapshot reuses `entry_content_edits.original_content`, the same row the cleanup
feature reverts from, with `INSERT OR IGNORE` so the FIRST original wins — reverting
means "as the feed served it", not "as the last re-fetch left it". Sharing the row
also lights up the existing Revert control with no further wiring.

The archive fallback is one JSON call to `archive.org/wayback/available` — no
crawling, and nothing further asked of a site that already refused. It runs only
when the guard rejected the page or the source is gone, and the refused result is
kept unless the archived copy actually succeeds. The guard still applies to the
archived fetch, comparing the ORIGINAL URL against the archived page's title, so a
snapshot of the same parked page is refused just as the live one was.

**Re-fetch is gated on KEPT, not on the star.** Both re-fetch items (readability
and whole-page) appear when the post is one Lectio is keeping — a capture, starred,
**or manually tagged** — because only then is there a stored copy worth replacing.

The gate originally checked the star alone, on both sides (`postCanRefetch` in the
client, `refresh_captured_article` on the server). Tag-as-keep made a tag a keep
signal everywhere else and neither followed, so a tagged-but-unstarred post showed
in the Saved view with no way to re-fetch its content — **14,695 items**, the
majority of the library.

An *unkept* feed entry is still refused, and the reason is what makes the kept case
safe: `replace_entry_content(pin_content=not is_capture)` writes an
`entry_content_overrides` row for a feed entry so the next refresh cannot clobber
the fuller copy. Without that pin the re-fetch would be silently undone, which is
exactly the failure the original refusal existed to prevent.

## Keeping the files a post links to

Some posts are a wrapper around a download: guitar-pro's tab posts link `.gp`
files and PDF lyric sheets that disappear with the article. A per-feed extension
list (`feed_display_prefs.attachment_exts`) drives a scan of the captured HTML on
star/tag, storing matches through the existing `_archive_asset` — which already
stores non-image bytes untouched and dedupes per `(entry, source_url)`, so
attachments inherit retention and the orphan sweep for free and differ from images
only in how they are **found**. 25 MB per file (`ATTACHMENT_MAX_BYTES`).

What counts as a file is the whole design:

- **No bare wildcard.** `*` on an ordinary post also matches every link to a
  homepage, a category or a social profile. A **prefix** pattern is allowed
  (`gp*` → gp/gp3/gp4/gp5/gpx) because it still names a family of file types and
  cannot reach a page, and it must be at least two characters — `p*` (pdf, png,
  ppt, psd…) is a wildcard wearing a hat.
- **Page extensions are refused everywhere** (`html`, `php`, `aspx`, `jsp`, …),
  dropped on save *and* re-checked in the finder, so a stored value cannot smuggle
  one through. Dropped rather than rejected: typing "pdf html" means the pdf, and
  the UI says which were ignored.
- **Any host, matched on the URL path.** A same-host rule was rejected —
  guitar-pro serves tabs from `assets-wp.guitar-pro.eu` while the post is on
  `blog.guitar-pro.com`, so it would miss exactly the case this exists for.
  Path-only matching also keeps `/post.php?download=song.gp` a page.
- **Anchors are judged on the path, not the whole URL.** Requiring an href to
  *end* in an image extension let a Pinterest `/pin/create/button/?…&media=….jpg`
  share button be stored as an asset. Capture refuses HTML outright as well,
  whatever led it there.
- **The Attachments list is decided by stored content type**, not guessed from
  the URL, so a Gravatar (`/avatar/<hash>?s=48`) and an extension-less CDN path
  stop being offered as files without needing a re-capture.
- **Named data attributes are decoded, never a blind base64 sweep.** guitar-pro
  ships `<span class="obflink" data-o="<base64>">`, reachable by a browser and
  invisible to an href scan; `data-o`/`data-url`/`data-href`/`data-link`/
  `data-file` are decoded and the result still has to satisfy the feed's
  extension list. This widens where links are *found*, not what counts as a file.

**Enclosures are captured unconditionally**, without the extension list: an
`<enclosure>` is the publisher *declaring* that a file belongs to the post
(Standard Ebooks attaches the epub), which is a stronger claim than a body link.
Audio is skipped (podcast enclosures are large and stream fine) and images are
already captured as images.

An archived asset is addressed by content hash, so a bare `download` attribute
made the browser save `cfc24ad676…` with no extension — unopenable and
unidentifiable. The name derived from the source URL's basename is carried in
three places: the Attachments list, in-body links rewritten to the archive (unless
the publisher set their own `download` name), and the `/starred-asset/` route via
`Content-Disposition`, so "Save link as" and a pasted URL are named too. Skipped
for images/audio/video, which render inline and would only be made un-viewable by
an attachment disposition.

## Editing a post's published date (overrides)

**Edit date…** (`POST /entries/set-date`) fixes garbage publish dates (epoch-0 entries sink to the bottom of every date sort). reader's `EntryData` is ingest-owned with no public setter, and the entry list sorts in SQL on reader's `entries.published` column — so the corrected date is written directly into that column (via `reader._storage.get_db()`), in reader's naive-UTC `YYYY-MM-DD HH:MM:SS` format. A meta-DB override row (`entry_date_overrides`) records the correction, and the refresh service re-pins it after every update batch (`reapply_entry_date_overrides`) in case a refresh re-ingested the feed's original value. Clearing the date deletes the override row only — the stored value stays until the feed next updates the entry.

**Edit title…** (`POST /entries/set-title`) is the same mechanism aimed at `entries.title` (`entry_title_overrides`, re-pinned by `reapply_entry_title_overrides`): it fixes "(untitled)" posts and garbage feed titles, and renames saved articles whose readability-extracted title is off (for `lectio:saved` entries the feed never refreshes, so the direct column write alone would already stick; the override row is kept anyway for uniformity).

**Canonical entry links** (`entry_link_overrides`, re-pinned by `reapply_entry_link_overrides`) rewrite feed-redirector links — FeedBurner's feedproxy.google.com / feeds.feedburner.com and CNAMEd burner domains (the `/~r/` path signature), FeedsPortal — to the URL the redirect resolves to, so the title's href outlives the redirector service (feedproxy is already dead). Detection lives in `services/link_canonical.py`. Three write paths: (1) the **starred-archive capture** already fetches the source page on every star, so its `on_canonical_link` hook canonicalizes at zero extra requests (and the archive row + relative-URL resolution follow the final URL); (2) **Save Article** pre-resolves redirector URLs before storing; (3) the **Inoreader importer** picks whichever of an item's `canonical`/`alternate` hrefs isn't a redirector. For stars whose redirector died before any of this existed, `scripts/backfill_canonical_links.py` recovers the real URL from the starred archive's captured page HTML (`rel=canonical` / `og:url`) — dry-run by default, `--live-resolve` for still-alive redirectors. Ordinary redirects (http→https, trailing slash) are never rewritten: only known-redirector sources qualify.

**Edit URL…** (`POST /entries/set-link`) is the manual write path into that same `entry_link_overrides` table, and exists because every automatic path can fail at once. Measured on the live library (2026-07-22): of 37 starred redirector links, **zero** were recoverable — no captured archive HTML to mine, feedproxy.google.com answers 404 with no redirect chain, and Archive.org holds no snapshot of the redirector URLs. 22 of the 37 are opaque ids (`~3/vGL5XCHkyww/`) with not even a slug to reconstruct from. When the machine can't resolve it, the user can: find the article's new home by hand, pin it here, then **Re-fetch content** to pull the body from that address.

**Only `link` changes — never the entry id.** For a Lectio capture the id *is* the original URL, and it keys the `saved_entries` star row, manual tags, and archive rows; re-keying would scatter all three. Changing the link alone suffices because both the "open original" href and `refresh_filed_article` read `link` first, falling back to the id. The route accepts http(s) only — `safe_link_url` also passes `mailto:`/`tel:`, which are legitimate hrefs but not source URLs a re-fetch could follow.

**Other domains** (`POST /feeds/url-rewrites`, `…/delete`; listed in the `/feeds/properties` payload) is the direct way to manage `feed_url_rewrites`, added 2026-07-25. Edit Website below can only seed a rule for a host it can *infer* — the channel `<link>`, or the host most posts link to — so an author's *older* dead domain, one with no surviving entries to infer from, had no way in at all. It also had no way out: nothing rendered the rules, and no route deleted one, so a wrong alias could only be undone in SQL. Adding one migrates matching entries inline through the same `migrate_feed_host_rewrite` Edit Website calls; a domain with nothing left on it reports 0 migrated and the rule still stands, because it governs ingest and the global dedupe alias map from then on. Removing one stops future rewrites only — entries already migrated keep their new ids, since the old id is gone and re-deriving it would scatter the star, tags and archive rows that followed it. Hosts are accepted as bare domains or pasted URLs, with `www.` dropped to match how `get_dedupe_host_aliases` stores its keys.

**Edit Website…** (`POST /feeds/set-website`) is the *feed*-level counterpart, for an author who moved domains without updating their feed's `<guid>`/`<link>`. Unlike Edit URL, the id here *must* change: the feed keeps re-serving the old-domain guid, so a link-only override is undone every refresh. Editing the Website seeds a `feed_url_rewrites` rule (old channel-link host → new Website host) — which rewrites the host at *ingest*, before reader derives ids — and migrates the existing posts inline via `migrate_feed_host_rewrite`/`migrate_entry_to_new_host` (recreate under the rewritten id, carry star+archived_at, manual tags, read state and the offline archive, delete the old). The batch `scripts/apply_feed_url_rewrites.py` now imports that same per-entry logic from `main`, so the one-off and the UI share one implementation. A subtlety this surfaced: the list/pane link **rebase** (`_rebase_proxy_entry_link`, built to move feedburner-proxied entry links onto the publisher host named in the feed's channel `<link>`) would take a feed whose channel link still names the *dead* host and rewrite already-correct entry links back onto it. The caller now folds the channel link through the declared migrations (`get_dedupe_host_aliases` → `_rewrite_url_host`) before rebasing, so a declared migration wins; the same fold corrects the Feed Properties Website field and the favicon lookup.

## Editing a post's body — Aardvark-style cleanup

**Clean up article** (🧹 in the pane; `POST /entries/content/clean`) arms `.entry-content` into an element-picker: hover outlines the node under the cursor, click or `R` removes it, `I` isolates it, `W`/`N` widen and narrow the selection, `Ctrl+Z` undoes. It is the manual counterpart to `_apply_feed_content_cleanups` — the hand-coded per-site strips (NASA nav, mynorthwest's related block, JWPlayer control DOM) exist because there was no way for the user to do it themselves; this is that way.

**The browser sends what it removed, not the edited HTML.** Each op is a structural path (element-child indexes from the content root) plus a fingerprint of the node — tag, id, classes, normalized text prefix, element-child count, and the last path segment of `src` with any `/api/img?u=` wrapper unwrapped. `services/content_edits.py` replays that list server-side. Posting the DOM back would be simpler and wrong: the rendered body is not the stored body (hotlink images are routed through `/api/img`, `referrerpolicy` is injected, starred assets are rewritten to local copies, and app.js rewrites more `src`s on error), so the edited DOM would bake render-time artifacts into stored content. The op list is also the durable record of *what* was removed, which is what a per-feed rule would be promoted from.

Matching is two-tier because the rendered tree and the stored tree are not guaranteed identical: walk the path and accept where it lands only if the fingerprint agrees; otherwise search the whole tree for the best fingerprint match and accept it only if it is unambiguous. An op matching neither is returned as `unmatched` rather than guessed at — a rendered-only node (an injected embed, something a render-time cleanup already removed) genuinely has nothing to delete, and silently deleting the wrong node is the one outcome worth failing over. Ops apply in order against a tree that mutates as it goes, mirroring the client, whose paths are derived from the DOM as it stands at each click.

**Persistence reuses the re-fetch path.** The result is sanitized through the normal allowlist (a cleanup must not be a way to widen what a body may contain) and written into reader's `entries.content` via `saved_articles.replace_entry_content` with `pin_content=True`, so `reapply_entry_content_overrides` re-pins it after every refresh and the feed can't re-serve the junk. `entry_content_edits` snapshots the pristine body **on the first edit only** (repeated cleanups must still revert to the feed's version, not to the previous cleanup) alongside the accumulated ops; `POST /entries/content/revert` restores it and drops both the pin and the edit row. While an edit exists, `_inject_recovered_source_embeds` is skipped for that entry — re-adding an embed the user just deleted is the one way a cleanup could look undone.

Saving or reverting re-renders **only the article pane**, via `window.lectioReloadEntryPane` (app.js's `loadEntryPaneWithoutFullRefresh`, exposed for this). The sibling edit routes (title/date/URL) full-reload because what they change is *in the list*; a body edit is not, so a reload would rebuild the list and move the reader's place in it for no reason. The loader's post-swap `centerActivePostInView` keeps the open post where it was; measured on a 30-post list, the list's scroll position is unchanged across a cleanup save. A pane fetch that fails still falls back to a full reload — `/entries/pane` requires `folder_id`, so a URL lacking it (a hand-typed link) degrades rather than breaking.

Deferred: promoting a recorded removal into a per-feed rule. That rule belongs at render time inside `_apply_feed_content_cleanups`, *not* as a bulk rewrite of stored bodies — feed-wide it would touch hundreds of entries irreversibly, and the render-time form covers old and new posts alike and can be switched off.
