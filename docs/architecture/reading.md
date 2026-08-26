# Reading

Read Mode, offline reading, and offline actions.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## Read Mode — e-ink reading app (`GET /read`)

A standalone reading app for the starred backlog, built e-ink-first (a Supernote
Manta browser, where Instapaper renders badly). Own `reader.css`/`reader.js`,
black-on-white, large adjustable type, and `transition:none/animation:none` —
transitions ghost on e-ink. Standalone also sidesteps the app's localStorage-only
theming, which a server-rendered page cannot read.

**Two states, one route.** No `entry_id` → the 2-pane browse (saved tree +
item list); with one → the paginated reader. Read Mode ignores read/unread
(`star_only=True, read_filter='all'`); node scope is `folder_id` / `tag` /
`archived` / `q`. Tree counts are non-archived; tag buckets are restricted to
tags on inbox items. Archiving drops an item from those counts but never strips
its tags.

**Paginated, not scrolled.** `reader.js` lays the article out in `100vw` CSS
columns inside an `overflow:hidden` viewport and translates horizontally. Tap
zones (left third back, right two-thirds forward), swipe and keys turn pages;
past either end it loads the prev/next article from the *selected node's* list.
`A−`/`A+` adjusts `--reader-fs` and re-paginates, persisted.

### Archive and Delete are the "done" axis

A feed post needs only read/unread, but **a star is a TODO** — you can read
something and still not be done with it. So done is its own axis, and all four
combinations are reachable: *read but not archived* is the TODO that survives
reading; *archived but unread* is deciding you never need to read it.

- **Delete** = leave Kept entirely: clears tags **then** unstars. The unstar
  route releases the offline capture only when no tags remain, so the reverse
  order strands the capture with nothing keeping it. The confirm names the tags —
  tag loss is unrecoverable; a plain unstar stays unconfirmed.
- **Archive** = out of the inbox, filing intact. It **removes the star** (the
  TODO is discharged) while tags, the capture and pruning-exemption survive.
- **Both mark the entry read**, at both levels, and append to `read_history` —
  acting on something *is* dealing with it, and leaving it unread puts it back in
  the queue it was just cleared from.
- **Archive is a third keep signal** (`entry_has_keep_signal`, and `_prune_entries`
  honors the same three). Archiving is *implemented* as an unstar, and the unstar
  path releases the capture and hard-deletes a `lectio:saved` husk once nothing
  keeps the entry — miss this and the gesture that promises to keep the contents
  destroys them.
- **Ordering is server-side.** `/entries/archive` writes the archived row before
  unstarring; `/entries/discard` clears tags *and* the archived row before
  unstarring. Delete forgetting the archived row failed quietly twice: the entry
  stayed in Archive forever, and the surviving keep signal skipped the capture
  release, so Delete kept what it exists to drop. It is one route now because the
  constraint belongs to the storage layer, not the caller.

**`archived_entries` is its own table, not a column on `saved_entries`.** That row
*is* the star, and Archive removes it — state stored there could not outlive the
act that sets it — and a table also lets a tagged-but-unstarred entry be
archived. Migrated by lifting the legacy column **once** and clearing it in the
same transaction. **Idempotent was not enough:** an `INSERT OR IGNORE` that left
the column populated re-lifted it every boot, so un-archiving worked until the
next restart ("I've unarchived them 3 times now"). Clearing the source is what
removes the second source of truth.

This Archive is *not* the offline starred archive (`archived_entry`) — same word,
different concept.

**Content resolution** (`resolve_reader_article_html`): archived readability copy
first (offline, `/starred-asset/` URLs, survives dead sources), then a live
extraction, then stored feed content. All already sanitized.

### Mark-read is earned by reaching the last page

Serving the page marks nothing. In a browse loop, opening an item is how you
decide whether you want it, so marking on render turned every peek into a read.
`reader.js` posts to `/entries/read` once the last page is reached, reusing the
route's existing async branch rather than adding an endpoint.

The mark waits for pagination to **settle**, and settling is a readiness check,
not a delay: on a cold load a 12-page article measures as **one page** until its
stylesheet and images arrive, which satisfies "last page reached" immediately. A
fixed timeout cannot fix a race against arbitrary resource timing, so `trySettle`
re-measures until `document.readyState === 'complete'` *and* every `<img>` reports
`.complete` (errored images are settled — they just add no height), capped at
`SETTLE_MAX_TRIES` ≈ 5s. Only the one-page case needs the guard; a multi-page
article cannot be marked without genuinely paging to the end. Both scopes follow
the same rule, since the scope is invisible from inside the reader.

### Tagging is a tap-list, not a text field

E-ink has no hover, a slow repaint, and an expensive keyboard. The whole tag
vocabulary ships inline as `__READER_TAGS__` and renders as large toggles —
applied first and inverted. A keyboard is needed only for "+ New".

Each tap applies immediately and sends the *whole desired set* with
`append_mode=0`, so add and remove are one request shape and closing half way
still saves what was tapped. The server's reply is the truth and the panel
re-renders from it, so "Brand New Tag" coming back as three tags is visible
rather than assumed. The panel overlays the article — re-paginating to make room
would be a full-screen e-ink flash. The header count and Delete's `data-tags`
re-sync after every change, so Delete's confirm keeps naming the right tags.

### The Inbox is STARRED minus Archived

Briefly it counted *kept* (star OR tag) minus archived, which made it 24,672
items: the whole library wearing an inbox label. A star is a TODO; a tag is
filing, and filing something is not a to-do — so tagged-but-unstarred entries
live in the tag tree. `list_entries_for_feeds(kept_scope=…)` carries this:
`"kept"` for the main app's Saved view, `"starred"` for the Inbox.
`_read_mode_saved_index` still returns *filed* counts alongside, or the tag tree
would empty.

**The Archive filter must be applied before every clip, and there are three:**
`_sorted_star_key_window` (SQL over raw keys), `list_entries_for_feeds` (light
records), and `merge_orphan_saved_entries` (after appending orphans). The filter
originally sat downstream of all three, so it chose archived rows out of a window
computed against the *unfiltered* backlog. Live symptom: one archived post among
24,672 kept — newest-first found it, oldest-first and Recently-starred returned
nothing, while the node's own count said 1. **A count and a list computed at
different layers is the recurring bug in this area.**

`kept_entries_set` folds in the archived keys too: archiving removes the star, so
an archived untagged entry is kept by no other axis and would be unreachable in
the one view built to show it.

**"All Saved" is a separate node**, not a mode of the Inbox — everything kept
minus archived. It carries `kept=all` through every hop, being otherwise
indistinguishable from the Inbox and would silently inherit its starred-only
scope.

**The Inbox opens most-recently-starred** (`sort=starred` → `saved_entries.saved_at`,
a fourth sort key). A to-do pile is ordered by when you added to it. `saved_at`
exists in two shapes (SQLite `CURRENT_TIMESTAMP` and ISO-8601 from imports) so it
is parsed, not string-sorted — `' '` sorts before `'T'`. **That order must not
follow you out:** `resume_sort` stows the order you entered with and restores it,
same shape as `resume_read_filter`.

**Undo unstar** (`entry_unstar_batch`, `POST /entries/undo-unstar`) is why
`saved_at` matters again on the way back in. Raised 2026-08-23: repeat-pressing
the star-toggle key by accident unstarred ~16 articles with no way to identify
which ones afterward — `apply_star_state`'s unstar path is a hard `DELETE FROM
saved_entries`, so nothing survived to reconstruct from. `POST /entries/saved`
(`saved=0`) now stamps the row it's about to delete with a shared timestamp
token in `entry_unstar_batch`, keeping the *original* `saved_at` alongside it —
same shape as the mark-read/mark-unread undo already had (`entry_read_state`/
`entry_unread_batch`, 15-minute window, `_undo_token_problem`), but restoring
into `saved_at`'s actual sort order matters here specifically because of the
"opens most-recently-starred" rule above: undoing into "just starred, at the
top" would silently reorder the Inbox for every other undo too. Deliberately
scoped to this one route — the bulk/administrative unstar paths (Archive,
dedup/merge, unsubscribe) call `apply_star_state` directly and don't write
this table, since an undo toast doesn't make sense for a deliberate bulk
action. One more guard `undo_mark_read` doesn't need: an untagged Saved
Article husk is *hard-deleted*, not just unstarred
(`saved_articles_service.is_saved_articles_feed` branch), so the undo route
checks the entry still exists in `reader` before restoring the star — otherwise
it would recreate exactly the orphan-star class of bug the orphaned-star sweep
exists to clean up (a `saved_entries` row with no matching entry).

### Prefetch warms the next article's images, not its page

The e-ink flash on advance is mostly image decode, and the reader page is
`no-store`, so `<link rel="prefetch">` would fetch and immediately discard it.
`prefetchNextImages` fetches the next article's HTML, parses it in a **detached
`DOMParser` document** (no scripts, no resource loads — it only reads `src`), and
warms up to `PREFETCH_MAX_IMAGES` via `new Image()`.

`isWarmableImagePath` restricts warming to the two endpoints article images are
rewritten to: `/api/img` (24h) and `/starred-asset/` (a year, immutable).
Same-origin alone is too loose — a feed's broken *relative* `src` resolves against
our origin and would be prefetched into a 404 — and these are the only paths with
a real `max-age`, which is why prefetching images works where prefetching the page
does not. Hung off the **settle** plus `PREFETCH_DELAY_MS` so it never competes
with rendering the article being read. Failures are swallowed.

**Only safe because rendering no longer marks read** — otherwise fetching the next
article's HTML would mark it read unseen. The two changes are ordered, not
independent.

### Two scopes

`?scope=saved` (default) is the starred backlog with the Archive axis. `feeds` is
ordinary unread feed reading — simplified feeds tree, `star_only=False,
read_filter='unread'`, same reader, no Archive/Delete (marked read on open).
Entry points are additive: an app-menu link, and a **Supernote auto-detect** (a UA
containing `supernote` hitting `/` is redirected to `/read?scope=feeds`). The exit
link sets a `lectio_full_app` cookie so in-app navigation is not re-redirected.

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
