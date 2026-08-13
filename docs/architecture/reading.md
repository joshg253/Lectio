# Reading

Read Mode, offline reading, and offline actions.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## Read Mode — e-ink reading app (`GET /read`)

A standalone, distraction-free reading *app* for the saved/starred backlog, built
e-ink-first (the driver is reading the backlog on a Supernote Manta browser,
where Instapaper renders badly). The **Saved Articles** sidebar row is hijacked to
open it — a full navigation, since `_layout_shell.js` now bails on any link whose
path isn't `/`. It is deliberately **not** the app shell: its own light-themed
`static/reader.css` + `static/reader.js`, pure black-on-white, large adjustable
type, no shadows/gradients, and (critically) `transition:none/animation:none`,
which ghost on e-ink. Being standalone also sidesteps the app's localStorage-only
theming (a server-rendered page can't read it) and satisfies "auto light theme".

**Two states, one route.** With no `entry_id`, `reader_view` renders the **2-pane
browse** (`templates/read_mode.html`): a simplified saved tree (folders + manual-tag
buckets + an **Archive** node pinned at the bottom, each a plain full-navigation
`<a>`) on the left, and the item list for the selected node on the right. With an
`entry_id`, it renders the **paginated reader** (`build_reader_page`). Read Mode
ignores read/unread — it always lists starred items (`star_only=True,
read_filter='all'`); the browse node scope is `folder_id` / `tag` / `archived` / `q`.
A bare bookmarked `/read` lands on the browse **All (inbox)**. All tree counts are
**non-archived** totals (`_read_mode_saved_index`); tag buckets are restricted to
tags on inbox items with inbox counts (`_inbox_tag_counts`) and collapsed in a
`<details>` (heavy taggers have dozens). Archiving an item drops it from these
counts/lists but never strips its tags. Saved-article captures show their source
domain as the list subtitle (`_read_mode_subtitle`); feed-starred items show the
feed title.

**Paginated, not scrolled.** `reader.js` lays the article out in screen-width CSS
columns (`column-width:100vw`, stride = column-width + column-gap = 100vw) inside
an `overflow:hidden` viewport and turns "pages" by translating the column
container horizontally. Tap zones (left third = back, right two-thirds = forward),
**horizontal swipe**, and arrow/page/space keys turn pages; past the first/last
page it navigates to the prev/next article (`data-prev`/`data-next`, full loads).
`A−`/`A+` adjusts `--reader-fs` and re-paginates, persisting size in `localStorage`.
Prev/next follow the **selected node's** list.

**Archive vs Delete (the "done" axis).** Saved items need a *second* layer of
read/unread. Josh's framing (2026-07-29): a feed post only needs read/unread, but
**a star is a TODO** — "I still have to decide what to do with this" — and you can
read something and still not be done with it. So done is its own axis.

The two axes are deliberately independent, and all four combinations are
reachable: *read but not archived* is the TODO that survives reading; *archived
but unread* is deciding you don't need to read it at all. The model is triaging
an email inbox — you can tell from the list whether you want to read something,
keep it, or drop it, without opening it.

**`archived_entries` is its own table, not a column on `saved_entries`.** A
`saved_entries` row *is* the star, and Archive **removes** the star — so state
stored on that row could not outlive the act that sets it. The table also lets a
tagged-but-unstarred entry be archived, which the column could never represent.
Migrated by lifting the legacy `saved_entries.archived_at` column **once**, then
clearing the column in the same transaction. **Idempotent is not enough here.**
The first version used `INSERT OR IGNORE` and left the column populated, so every
boot re-lifted it: un-archiving deleted the `archived_entries` row, the old column
survived, and the next restart resurrected the item. It surfaced as "I've
unarchived them 3 times now and they keep coming back", and it is invisible until
something restarts the app — the same shape as the starred-archive backfill that
re-created orphan star rows at every boot. Clearing the source is also what
removes the second source of truth; a marker flag would leave a populated column
that still reads as authoritative. This also deleted the unstar-tagged panel's
`archived_at_lost` warning outright — unstarring can no longer discard archived
state, so there is nothing to warn about.

Archiving drops the item from the inbox (`resolve_reader_backlog(archived=…)` filters against
`get_archived_saved_keys()`; `None` = no filter, used for Search which spans
everything). The reader header's **Archive** button POSTs `/entries/archive`
(`set_entry_archived`); **Delete** simply un-saves via the existing
`POST /entries/saved` (`saved=0`); both are `fetch` calls carrying the page's
CSRF token, then advance to `data-next`. This **Archive** is *not* the offline
**starred archive** (`archived_entry` capture DB) — same word, different concept.

**Content resolution** (`resolve_reader_article_html`): archived readability copy
first (`_resolve_archived_readability_html`, shared with `/entries/readability` —
offline HTML with `/starred-asset/` URLs, survives dead sources), then a live
readability extraction, then the stored feed content — all already sanitized, so
no new sanitization surface.

**Mark-read is earned by reaching the last page, not by opening the article.**
Serving the reader page marks nothing. In an e-ink browse loop opening an item
is how you decide whether you want it, so marking on render turned every peek
into a read — the whole saved backlog could be cleared by scrolling through it.
`static/reader.js` posts to `/entries/read` once the last page is reached, which
is the first moment the whole article has been on screen; a one-page article
qualifies immediately, because there it is true. The post reuses the route's
existing async branch (`X-Requested-With: lectio-entry-read-toggle`) rather than
adding an endpoint — that branch already writes read state, `entry_read_state`
and read history from a tenancy-rebinding daemon thread, which is exactly what
the old on-render `_mark_entry_read_background` call did.

The client holds the mark until pagination has **settled**, and settling is a
readiness check rather than a delay. On a cold load a 12-page article measures as
**one page** until its stylesheet and images arrive — which satisfies "last page
reached" and marks an article read the instant it is opened. That was observed in
testing, not theorised, and a fixed timeout cannot fix it because the wait is a
race against however long those resources take. So `trySettle` re-measures every
`SETTLE_POLL_MS` and only trusts the count once `document.readyState` is
`complete` *and* every `<img>` reports `.complete` (which covers errored images —
they are settled, they just contribute no height). `SETTLE_MAX_TRIES` caps the
wait at ~5s so an image that never resolves can't block mark-read forever.

Only the **one-page** case actually needs this guard: a multi-page article can't
be marked without the reader genuinely paging to the end, whenever that happens.
The guard exists because a wrong measurement makes every article look one-page.

Both scopes follow this rule: the peek problem is identical in each, and the
scope isn't visible from inside the reader, so splitting the rule would make
"did that count as read?" unpredictable.

**Tagging from Read Mode is a tap-list, not a text field.** Filing on e-ink has
no hover, a slow repaint, and a keyboard that costs several. So the whole tag
vocabulary (`get_all_manual_tag_names`) ships with the page as inline JSON
(`__READER_TAGS__`, same no-reading-from-the-DOM style as `__READER_NAV__`) and
renders as large toggle buttons — applied tags first and inverted, the rest
alphabetical. A keyboard is needed only for the "+ New" field, which stays hidden
until asked for.

Each tap **applies immediately** and sends the *whole desired set* with
`append_mode=0`, so adding and removing are one request shape and closing the
panel half way still saved what was tapped. The server normalizes and caps
(`MAX_MANUAL_TAGS`), and its reply is treated as the truth — the panel re-renders
from it, so a tag typed as "Brand New Tag" coming back as three tags is visible
rather than silently assumed. The panel overlays the article instead of reflowing
it: re-paginating the body to make room would be a full-screen e-ink flash. The
header's `#n` count and Delete's `data-tags` are both re-synced after every
change, the latter so Delete's confirm keeps naming the right tags.

**Delete and Archive mean different things, and both had to change for the kept
model.** Josh's rule: *Delete removes star **and** tags; Archive just takes it out
of the inbox.*

- **Delete** = leave Kept entirely. Unstarring alone was a no-op on anything
  tagged — a tag keeps an entry on its own, so the row stayed in the list while
  the reader advanced as though it had gone. The client clears tags **first**,
  then unstars: the unstar route only enqueues removal of the offline archive
  when no manual tags remain, so the reverse order would strand the captured
  copy with nothing keeping it. Tag loss is unrecoverable, so the confirm names
  the tags; a plain unstar (no tags) stays unconfirmed because it is cheap to undo.
- **Archive** = out of the inbox, filing intact. The 2026-07-29 refinement (the
  `archived_entries` table above) is that Archive **removes the star** — the TODO
  is discharged — while tags, the offline capture, and pruning-exemption all
  survive, because "keep its contents" is the whole point. It works on tag-kept
  items too; the old "hide the button when only a tag keeps it" branch is gone.
- **Both Archive and Delete mark the entry read, at both levels.** Acting on
  something from the list *is* dealing with it, so leaving it unread would put it
  straight back in the queue it was just cleared from. Both also append to
  `read_history`, which is what makes a triaged item findable again.
- **Archive is a third keep signal**, next to starred and tagged —
  `entry_has_keep_signal`. This is not bookkeeping: archiving is *implemented* as
  an unstar, and the unstar path releases the offline capture and hard-deletes a
  `lectio:saved` husk once nothing keeps the entry. Miss the archived signal and
  the gesture that promises to keep the contents is the gesture that destroys
  them. `_prune_entries` honors the same three signals, or retention would delete
  archived posts nightly — they are read and unstarred, exactly its target shape.
- **Ordering is server-side.** `POST /entries/archive` writes the archived row
  *before* unstarring, and `POST /entries/discard` clears tags **and the archived
  row** *before* unstarring, both for the same reason: the capture is released
  only when the last keep signal goes. Delete forgetting the archived row failed
  quietly twice — the entry stayed listed in Archive forever, and the unstar saw
  a surviving keep signal and skipped releasing the capture, so Delete kept the
  contents it exists to drop. Deleting an archived item is not a contradiction:
  Archive means "done, keep it", Delete means "done, don't", so Delete wins. Delete used to be a client-side chain of two POSTs with
  that ordering encoded in a comment in `reader.js`; it is one route now, because
  the constraint is a property of the storage layer, not of the caller.

The **superseded** rule, kept because the reasoning still explains the code:
Archive used to keep the star and set `archived_at` on the star row, and was
hidden entirely for tag-kept items — a control that silently did nothing.

**The Inbox is STARRED minus Archived — not everything saved.** Read Mode's root
saved node was briefly labelled "All" and counted *kept* (starred OR tagged) minus
archived, which made it 24,672 items: the whole library wearing an inbox label.

The narrowing follows from what the two signals mean. A **star is a TODO** ("I
still have to decide what to do with this"); a **tag is filing** — already sorted,
and filing something is not a to-do. So tagged-but-unstarred entries live in the
tag tree, which is where you would look for them, instead of padding a queue.
`list_entries_for_feeds(kept_scope=…)` carries this: `"kept"` (star OR tag, the
main app's Saved view) or `"starred"` (the Inbox). `_read_mode_saved_index` returns
*filed* (tagged minus archived) alongside the inbox, because the tag counts must
still be counted over filed items — counting them over a starred-only inbox would
empty most of the tree.

**The Archive filter is applied before every clip, and there are three.**
`_sorted_star_key_window` sorts and clips *in SQL over the raw kept keys*,
`list_entries_for_feeds` clips its light records, and `merge_orphan_saved_entries`
re-sorts and re-clips after appending archive-only orphans. The done-axis filter
originally sat downstream of all three, in `resolve_reader_backlog`, so it chose
archived rows out of a window computed against the *unfiltered* backlog.

Live symptom (2026-07-29): one archived post among 24,672 kept. Newest-first
found it, because a recent post sorts high; oldest-first and Recently-starred
returned nothing, because it sorted to the far end and fell outside the first
150. The Archive node rendered "Nothing here" while its own count — read straight
from `archived_entries` — said 1. **A count and a list computed at different
layers is the recurring bug in this area**; it is the same shape as the
9,979-vs-24,695 tree mismatch.

`kept_entries_set` also folds in the archived keys, because archiving removes the
star: an archived, untagged entry is kept by no other axis and would be
unreachable in the one view built to show it.

**"All Saved" is a separate node**, not a mode of the Inbox: everything kept
minus archived, i.e. what the main app's Saved view shows. The Inbox has to stay
narrow to be a queue, but the two modes disagreeing about what *exists* is the
mismatch Read Mode is meant not to have — the tagged-but-unstarred items are all
reachable under Tags, and this is the flat view of them. It carries `kept=all`
through every hop, because it is otherwise indistinguishable from the Inbox
(same root folder, no tag, no archive, no search) and would silently inherit the
Inbox's starred-only scope and star-date default.

Two consequences worth stating, both from live reports:

- **The Inbox opens most-recently-starred** (`sort=starred` → `saved_entries.
  saved_at`, a fourth sort key beside post/received/history). A to-do pile is
  ordered by when you added to it; an old article starred today belongs at the
  top, which is exactly what publish-date order gets wrong. `saved_at` is stored
  in two shapes (SQLite `CURRENT_TIMESTAMP` and ISO-8601 from imports), so it is
  parsed rather than string-sorted — `' '` sorts before `'T'`, which would
  scramble a single day's stars.
- **That order must not follow you out.** `resume_sort` stows the order you were
  using when you entered the Inbox and hands it back on the way out, so leaving
  neither drags most-recently-starred into a folder where nothing is starred nor
  resets a folder you had set to Oldest. Same shape as the main app's
  `resume_read_filter`, which restores your filter when you close History.

The Read Mode **Tags** section now renders open. It was a collapsed `<details>`,
which was fine while tags were a side-bucket of a kept-everything inbox; now that
filed items are reachable *only* there, collapsing them made them look absent.

**Prefetching the next article warms its images, not its page.** The e-ink flash
on advance is mostly image decode, and the reader page itself is `no-store`, so a
`<link rel="prefetch">` would fetch the next article and immediately discard it —
cost with no benefit. Instead `prefetchNextImages` fetches the next article's
HTML, parses it in a **detached `DOMParser` document** (runs no scripts, loads no
resources — it only reads `src` attributes), and warms up to
`PREFETCH_MAX_IMAGES` images via `new Image()`.

Warming is restricted by `isWarmableImagePath` to the two endpoints article
images are actually rewritten to: `/api/img` (`public, max-age=86400`) and
`/starred-asset/` (a year, immutable). Same-origin alone is too loose — a feed's
broken *relative* `src` resolves against our own origin and would be prefetched
into a 404, and other same-origin assets (a `/static/` placeholder) are already
cached or not worth a request. These two are also the only paths with a real
`max-age`, which is the entire reason prefetching works here when prefetching the
page does not. The cap exists because a lesson-length article can carry 50+
images. The prefetch is hung off
the **settle** (plus `PREFETCH_DELAY_MS`) rather than a fixed delay from load, so
warming the next article never competes with rendering the one being read — on a
slow load settling can itself take seconds, and a fixed timer would fire straight
into it. Failures are swallowed: a prefetch must never disturb reading.

**This is only safe because rendering no longer marks read.** Fetching the next
article's HTML to discover its images would otherwise have marked it read without
it ever being seen — the two changes are ordered, not independent.

**Two scopes** (`?scope=`). `saved` (default, above) reads the starred backlog
with the Archive axis. `feeds` is ordinary **unread feed reading**:
`_build_feeds_mode_context` renders a simplified feeds tree (All Feeds + folders
with unread counts) → `list_entries_for_feeds(star_only=False, read_filter=
'unread')` → the same paginated reader, minus the Archive/Delete controls (marked
read on open). Entry points are additive (the feeds three-pane app is unchanged):
an app-menu **Read Mode (e-ink)** link, and a **Supernote auto-detect** — a
tablet whose UA contains `supernote` hitting `/` is redirected to
`/read?scope=feeds`; the Read Mode exit link (`/?full=1`) opts back into the full
app and sets a `lectio_full_app` cookie so in-app navigation isn't re-redirected.

## Offline reading and offline acting (`static/sw.js`, `static/outbox.js`)

The Supernote's browser has **no download handler at all** — no `<a download>`,
no long-press "save link" — so every file-based route to offline reading is
closed. A service worker is the only remaining in-browser option, and the reason
it works where a plain cache cannot is that it intercepts the **navigation**: a
saved hyperlink to `/read?…` still resolves with WiFi off. The worker is served
from `/sw.js` (not `/static/`) because a worker's default scope is its own
directory, and Read Mode lives at `/read`.

Fetch handling is deliberately **network-first**. Lectio is a live app; serving a
stale Inbox to a device that has WiFi would be a worse bug than not working
offline. The cache is a fallback, never the primary. On a miss, `/read` is matched
**exactly** — its query string *is* the article's identity, and an `ignoreSearch`
match there returned the cached browse page for every article, a cache miss that
looked like a successful navigation going nowhere. `ignoreSearch` is for
`/static` assets whose `?v=` moved, and nothing else.

**Precaching is driven by the page, but images are derived by the worker.** The
page sends articles only — the hrefs the list actually renders, never
server-proposed URLs, since the server built those without the active sort and
every cached article sat under a URL nothing navigates to. The worker then
harvests each article's images *out of the bytes it just stored*. This replaced a
server manifest that sliced the node by position and returned images for items
`[offset, offset+n)`: because the article set was chosen from the DOM and the
image set by index, the two could disagree, and articles went offline with no
pictures. Deriving images from the stored article makes that mismatch
unrepresentable, and it deleted `GET /read/offline/manifest`, which had been
rendering every candidate article through BeautifulSoup to guess. The harvest
regex must undo `&amp;` — src attributes are HTML-escaped and `/api/img` URLs
carry several parameters, so the escaped form caches a response nothing ever
requests again: a counted success with the image still missing.

**"Save 20 more" asks the cache, not a counter.** The cursor was a per-node
`localStorage` offset — press once for items 1–20, again for 21–40. When new
articles arrive at the top between presses the list shifts underneath it, so some
are re-saved and some are skipped and never offered again: rare in a backlog
folder, routine in the Inbox, where new stars landing at the top is the entire
point of the Inbox. The page now asks the worker which hrefs it already holds
(`{type:"cached"}`) and takes the first 20 that are missing. If the worker does
not answer it falls back to "nothing is cached" — which degrades to re-saving
rather than to skipping, because re-saving costs bandwidth and skipping costs an
article you thought you had.

**Acting offline: an outbox, not a retry.** Archive, Delete, tagging and
mark-read were ordinary POSTs, so with no connection they failed and the tap was
gone. `static/outbox.js` persists each one to **IndexedDB** — not the Cache API,
because these are mutations and must survive the browser being killed, which on
that device is how reading sessions normally end.

Three decisions carry the design:

- **Enqueue first, then send** — even when online. The failure that actually
  loses work is not "offline", which the page can see, but the connection dying
  *mid-POST*; and every Read Mode action is immediately followed by a navigation,
  which cancels the request in flight. Post-first-then-queue-on-failure never
  learns about those. This is affordable only because the four target routes are
  **idempotent set-state operations** (`archived=0/1`, discard, the full tag set
  with `append_mode=0`, `read=1`), so a record retried after a half-finished
  flush is a no-op. That is also why there is no `synced_actions` dedupe table:
  it would buy a schema change plus a per-user migration for no behavior change.
- **The caller awaits the durable write, not the send.** An IndexedDB transaction
  still open at unload is aborted, so navigating without waiting would lose the
  very record that exists to stop the action being lost. The flush is left
  dangling on purpose; if the navigation kills it, the next page load picks it up.
- **403 is never dropped.** Replay drops a record on 2xx or a definitive 4xx
  (400/404/409/410/422 — the entry is gone, or the request can never apply, and
  retrying forever would wedge every later action behind it). A 403 here is an
  expired session or a CSRF token from a page cached hours ago, neither of which
  is a verdict on the action.

Replay is serial and oldest-first, stopping at the first record that neither
succeeded nor died definitively — pushing past it would reorder the rest, and an
archive-then-unarchive replayed backwards brings the item back. Triggers are page
load and the `online` event, **plus** Background Sync where it exists; Sync cannot
be the only path, because the device this is for runs Chrome 96 in an Android
WebView, where it is absent. The same file runs in both places (every DOM touch
is guarded, and `sw.js` pulls it in with `importScripts`), so there is one
implementation rather than two that drift. The CSRF token is captured **at enqueue
time** and stored on the record: a worker replaying under Background Sync has no
document, and therefore no `<meta name=csrf-token>` to read.

Tagging is the deliberate exception — it posts directly and queues only on
failure, because it is the one action whose **reply** matters (the server
normalizes the name and enforces the cap, and the panel re-renders from what came
back), and nothing navigates away, so there is no cancellation race to protect
against.

Pending depth renders into any `[data-outbox-depth]` element — the Read Mode
footer and the reader's control bar. A queue nobody can see is how work gets lost
without anyone noticing, and on a device that is offline by default that is not a
rare case.

**Conflict rule: last-writer-wins, and the device loses ties it cannot see.** A
replayed action overwrites whatever the server now holds. Detecting "the server
already moved on" would need per-entry modification times the schema does not
carry, and the alternative — a merge dialogue on an e-ink screen — is worse than
the loss it prevents. Discarded actions are logged so a surprising result is at
least explicable.

## Titles render a tiny inline allowlist

**Titles render a tiny inline allowlist, by escape-then-restore.** Feeds put `<em>`
in titles — and they also put `std::vector<T>` and `#include <chrono>` in them.
Measured on the live library: of 21 titles containing angle brackets, only 6 are
markup; 11 are C++ generics and the rest are header names. **Treating titles as
HTML would silently delete those**, mangling a C++ post title, which is a worse
failure than showing `<em>` literally.

`sanitize_inline_title` escapes the whole string and then restores only bare tags
from a short list (`em`, `i`, `strong`, `b`, `code`, `sub`, `sup`, …). That ordering
is what makes it provably safe: an attribute or an unknown tag cannot survive a
round trip through escaping, so `<em onmouseover=…>` and `<img onerror=…>` stay
escaped text with no allowlist to argue about.

Records carry both forms. `title_html` is used where the title is visible text (post
rows, the entry-pane headline, Read Mode rows and the reader headline);
`title_plain` — the same string with those tags stripped — is used everywhere that
cannot render markup: `title=` attributes, `<title>`, exports, email.
