# Views

The post list, folders, sorting, layout and page weight.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## View state model

Three kinds of state, kept apart:

| kind | examples |
|---|---|
| remembered | sort mode, default filters, pane sizing |
| temporary | tag-click "show all", search result scope |
| transient | current entry, scroll position, focus |

A temporary override must never overwrite a remembered preference; leaving the
override restores the base.

### Filtering a view is not searching it

**Search** (`q`) is a server query that changes *what is fetched* — title, feed
name, link, authors, summary. **Filter this view** narrows *what is already in
front of you*, instantly, matching only title/link/feed name, and is transient:
a pane swap comes back empty by design.

**The filter owns its own class.** `post-item-filtered`, never
`post-item-hidden` — the latter belongs to the scroll-chunking reveal, and "move
all shown" keys off it. One shared class turns a filtered bulk move into a
whole-list bulk move: filter to one domain, click move, and the entire unfiltered
list is silently re-filed. Keyboard nav excludes both. While a filter is active
the chunk window steps aside and reveals every match — chunking exists to keep a
2,000-row list cheap to paint, and the filter has already done that.

**Whole-set actions resolve server-side, by predicate.** The route serves 250
posts and chunks to a 2,000 cap, so the browser holds a *page* of the view, not
the view; an action posting the ids it can see covers a fraction of a large
filter and says nothing about it — which `Move visible to feed…` did.
`POST /entries/move-visible-to-feed` takes the *predicate* (scope, tag, search,
read/star filters, filter term) and re-resolves it at an effectively unbounded
limit. Correct at any size, and with no id payload to bound `_MOVE_BATCH_CAP`
does not apply. `dry_run=1` returns the count so the dialog can state the real
number rather than the rows in the DOM, naming both when they differ.

"Shown" means everything matching the active filters regardless of scroll —
chunking is a rendering optimization, not user intent. Orphan archive rows are
excluded on both sides: there is no reader entry to move.
`/entries/mark-range-read` solved the same page-vs-view problem earlier with
`_RANGE_READ_LIMIT`; this generalizes it from an anchor lookup to a whole-set
action.

### Back on a phone walks the view stack

In single-pane mode the article pane *is* the page, so Back steps down the stack
rather than leaving: article → feed list → folder list → then it stops, toggling
the folder drawer.

Most of that needs no code — opening a feed and opening an article each push a
history entry, so a plain Back walks them. The last step is the exception.
Backing out of the entry the app was *loaded* on is a cross-document navigation:
the page unloads and `popstate` never fires. So `armDrawerBack()` pushes a
**spare history entry** — a duplicate of the current URL, silent to push and pop —
and popping it toggles the drawer.

**What is protected is the bottom of this document's history, not a URL shape.**
The first version armed only on folder-scoped lists, since an article "always has
a real parent underneath" — true only if you *navigated* there this session.
Reopen the app, restore a tab, or follow a bookmark to an article and there is no
parent, so Back went cross-document and closed the tab: the exact bug the guard
existed to prevent, live on three of the four ways in. Position is now tracked
directly — `pushState`/`replaceState` are wrapped to stamp a `lectioIdx`, the
loaded entry is 0, and `popstate` traps on 0. An *index* rather than a decremented
counter is what keeps Forward working: `popstate` fires in both directions and
only the state says which way.

**⚠ The guard cannot be made airtight.** Chrome's history-manipulation
intervention marks entries pushed *without user activation* as skippable, and a
spare pushed from inside a `popstate` handler has no gesture behind it. Observed
on a Galaxy S21+ as two working toggles then the tab closing, while headless
Chromium toggled indefinitely — **headless does not apply the intervention, so no
browser test here can prove the guard holds.** Treat green automation as "not
obviously broken".

The mitigation is re-arming from real gestures (`pointerdown`/`touchstart`/
`keydown`), since an entry pushed while touching carries activation. Reading means
tapping constantly, so in practice the spare is usually gesture-made — but press
Back repeatedly without touching anything and there is nothing to arm from.

**What removes the risk is not being a tab.** The web app manifest
(`display: standalone`) installs to the home screen, where Back at the root
backgrounds the app. The in-page guard is the fallback for browser-tab use.
**Installability needs a service worker, not just a manifest**, and the main app
registered none — `offline-probe.js` is loaded only by Read Mode and early-returns
without its `rm-*` elements. The browser then declines to install and "Add to Home
screen" degrades to a shortcut that opens in a tab, which dies with Back: the
thing the manifest existed to prevent. `index.html` now registers `/sw.js` itself.
That adds no caching of the main app — `_worthCaching` covers only `/read`,
`/static/*` and `/api/img`. (A true standalone install is an Android WebAPK minted
by Chrome via Play Services; a Chromium derivative may still only offer a
bookmark.)

**This applies to every layout whose tree is a drawer — single *and* medium
(721–1100px).** Gating on single-pane alone meant a tablet never armed it. Wide is
excluded: the tree is a permanent column, so there is nothing to toggle and a Back
that visibly does nothing is a trap. Re-arms from `updateSingleMode()`, since
rotating a tablet moves it between wide and medium after load.

**Back therefore never exits the app in single-pane mode, deliberately** — asked
for after a stray press closed the tab mid-read. Leaving is still an ordinary
browser action; only Back no longer does it. Desktop is untouched.

The spare is re-armed the instant it is consumed, so the stack stays the same size
indefinitely (verified at ten presses, `history.length` flat). An article and a
feed-scoped list have real parents, so neither arms it, and a scope param is
required so the bare landing is excluded. Forward navigation clears the flag.

The handler lives in `index.html`, not `app.js`, because it registers first — so
`stopImmediatePropagation()` also suppresses app.js's `popstate` handler, which
would otherwise refetch the list already on screen.

The toolbar's top-left control mirrors the hierarchy: scoped to a feed it is a
back arrow labelled with the folder name; at the folder list it is the hamburger.
Two controls rather than one that changes meaning — a button reading "Folders"
that does not open Folders is worse than either.

### Off-site links never open in the reading tab

Following a link in place loses your position, and on a phone Back no longer
leaves, so there is no cheap way back.

Every off-site `http(s)` link opens in a new tab via the anchor's **own `target`**,
never `window.open`: a real tab is what normal activation produces, whereas a
scripted open is what popup blockers stop and what a phone renders as a floating
window. `rel="noopener noreferrer"` goes with it — without `noopener` the opened
page can rewrite the reading tab out from under you.

Deliberately narrow: same-origin links are app navigation, in-page fragments are
how footnotes work, and `mailto:`/`tel:` would hand a blank tab to a handler that
cannot close it.

Enforced in three places because none covers everything: **`html_sanitize.py`**
marks external links at ingest (correct at the source, but only for content stored
from then on); **`index.html`** carries a capture-phase click listener for older
bodies and anything injected later; **`reader.js`** carries the same for Read
Mode. The sanitizer allows `target`/`rel` but never trusts them — it overwrites
both on every external link and deletes them elsewhere, so a feed can neither
choose its target nor drop the `noopener`.

### Resume where you left off

Every attempt to stop Back leaving failed, each *on purpose* at a different level:
Chrome marks script-pushed entries skippable to defeat back-traps, and Android
exits any app at its root — a WebAPK install does not change that.

So the app stops trying to keep you in it and makes leaving cost nothing. The
current position (URL, pane level, both scroll offsets) is written to
`localStorage` — not `sessionStorage`, which dies with the tab, the exact event
being defended against — on `pagehide`, on `visibilitychange` to hidden, and on
every navigation. The restore runs inline in `<head>` before anything renders, as
a `location.replace` — `replace`, not `assign`, so resuming leaves no history
entry to bounce off.

Three rules stop it becoming its own trap:

- **A URL with a query is an explicit destination** and is never overridden. Only
  a bare `/` counts as "just opened the app".
- **The wordmark links to `/?home=1`**, so "take me to the top" still exists.
- **Positions older than 7 days are ignored** — long enough for a weekend, past
  which it is a surprise rather than a convenience.

Scroll is re-applied after two animation frames plus a short delay: the article
pane's height depends on images and the chunked list grows as it reveals, so a
`scrollTop` set too early clamps against a shorter document.

Read Mode has its own resume, shipped 2026-08-25 (`templates/read_mode.html`)
— a separate `lectio-read-last-position` key, deliberately not shared with
this one: reusing this key would have let the two surfaces redirect into each
other. No scroll offset (plain page navigation, not this pane model), and the
"Saved" scope tab carries a harmless `?home=1` as its own escape hatch, same
trick as the wordmark above. Still no Back guard (see Plan.md).

### Pull down in an article for Reader view

Pulling down from the top of the article pane toggles Reader view, and pulling
again returns — the Reader-view button is easy to miss and awkward one-handed.

**Not** a revival of pull-to-refresh, which stays removed
(`bindSinglePanePullToRefresh` is still a no-op stub). The gesture delegates to
`#entry-readability-button` rather than reimplementing the toggle, so both
directions come free. Listeners are on `document` (a pane swap replaces
`.pane-entry` wholesale) and stay **passive** — the browser's own pull-to-refresh
is suppressed with `overscroll-behavior-y: contain` rather than by
`preventDefault` on every touchmove. Three guards keep it off ordinary input: the
pull must start at `scrollTop === 0`, travel 90px, and be clearly vertical.

### Global audio player

A deliberate exception to the pane-swap lifecycle: the entry view is swapped via
`/entries/pane`, so any `<audio>` inside it is destroyed on navigation. A single
`<audio>` + control bar lives outside the swap target, owned by
`static/media-player.js`; podcast posts inject a `.podcast-player` trigger that
hands the track to it. Player state is transient client-side only, with playback
speed persisted to `localStorage`.

## Page weight: lazy HTML fragments

At thousands of feeds, any template section that renders a row per feed is
megabytes of HTML — far heavier than the posts themselves. The rule: per-feed
row sections must not render inline in `index.html`. They live in `_*.html`
fragment templates served by dedicated GET endpoints, and the page ships a
small container `<div data-lazy-src="…">` that client JS fills on first open.

Current fragments:
- `/settings/feeds/panel/{folders,stale,failing}` (`_settings_feeds_folders.html`,
  `_settings_feeds_stale.html`, `_settings_feeds_failing.html`): the
  Settings → Feeds folders table (a hidden row per feed, including disabled),
  the Stale view (every active feed ranked by last-post age), and the Failing
  view (up to `LECTIO_FAILING_FEEDS_LIMIT`, default 500, failing/acked/
  needs-replacement feeds), fetched on first open of the Feeds tab / that
  view. Found 2026-08-27: `failing`/`acked`/`needs_replacement` were still
  inlined after folders/stale had already been moved to this pattern — ~500
  rows of buttons/icons/title-attrs, ~1.4MB of the settings modal alone on
  the live library. The tab-bar's failing-count badge stays server-rendered
  from `problematic_feeds` (already in the page context, cheap to filter —
  the cost is markup, not the list); only the row markup itself is lazy.
- `/tree/folder-feeds/{folder_id}` (`_tree_folder_feeds.html`): one sidebar
  folder's feed `<li>` rows, fetched on first expand. Only the selected
  folder inlines its rows (the active-feed highlight and auto-expand must
  work on full page load); the same template is `{% include %}`d there so the
  markup can't drift. `updateScopeActiveState` fetches a folder's rows when
  SPA navigation selects a feed whose folder hasn't loaded, and the loader
  re-applies the unread-only tree filter to injected rows.

This works without rebinding because row interactions are event-delegated on
stable ancestors (`#settings-tab-feeds`, `nav.tree`, `document`) — new tree
row handlers must follow that pattern, never per-element binding at load.
Only small shells with direct `getElementById` bindings (search input,
comparison toolbar) stay server-rendered in the page. Tree link hrefs carry
initial sort/filter state (rebuilt server-side from remembered preferences in
the fragment path) but the SPA re-stamps them from live state at click time.
Fragment responses are `Cache-Control: no-store`, like the page itself.

The app's main script lives in `static/js/app.js` (long-lived cache, busted by
`?v={STATIC_ASSET_VERSION}` — new static files must be added to
`_static_asset_version()`'s hash list or their changes won't bust caches). It
must stay Jinja-free: template-derived values reach it via the `window.*`
config object rendered in the document `<head>`. Only small inline scripts
remain in `index.html` (head config, CSRF shim, theme bootstrap, layout
shell) — the CSRF shim and theme bootstrap must run before anything else, and
the theme bootstrap uses `document.write`, which Chrome may block for
parser-inserted external scripts on slow connections.

## Adaptive layout model

Lectio uses responsive layouts rather than a fixed three-pane assumption:
- wide desktop: 3-pane side-by-side,
- medium tablet landscape: 2-pane refinement,
- narrow phone portrait: 1-pane drill-in navigation.

The priority is fast triage, not always showing three panes.

**Stacking bands.** Once a pane becomes an overlay the z-index ordering stops
being decorative, so the values are banded and the band is the contract:
**250–320 = overlays** (`.medium-pane-backdrop` 250, the medium/wide folder
drawer 260, the single-mode backdrop 290, the phone folder drawer 300,
`.topbar-menu` 320); **340+ = things opened on top of an overlay**
(`.context-menu` 340, `.context-submenu` 341); **1000+ = popup pickers**
(`.lectio-pin-menu`, `.lectio-quire-menu`, …) and **1190/1200 = toasts**. The
rule that matters: *a control opened from inside an overlay must outrank that
overlay*. `.context-menu` sat at 50 and `.context-submenu` had no z-index at
all, which is invisible on desktop — the folder pane is in normal flow there —
and broke the moment the same markup became a fixed drawer: a long-press on a
folder opened a menu painted behind the list it came from. Pick the band, not a
number, and never reach for 9999 — that is how the *next* overlay ends up
underneath something it should cover.

## Folder tree & the Uncategorized folder

Folders live in the meta DB (`folders` + `folder_feeds`); the reader owns the
feeds themselves. These two can diverge: a feed can exist in the reader with no
`folder_feeds` row (common after an OPML/reader migration). Such feeds are
**orphans**.

**Single-folder invariant:** a feed belongs to exactly one folder. `folder_feeds`
has no DB-level uniqueness (it once allowed multi-folder membership), so the
invariant is enforced in the write paths: `add_feed_to_folder` clears a feed's
other memberships before inserting, and the dedup/format-upgrade paths delete the
survivor's stale rows before re-inserting the chosen folders (earlier they added
without removing, which let feeds drift across folders). Pre-existing drift is
repaired by **Settings → Utilities → Fix multi-folder feeds**
(`GET /feeds/multi-folder` reports feeds with >1 row; `POST
/feeds/multi-folder/resolve` keeps only the user-chosen folder per feed).

The sidebar surfaces orphans through a **virtual "Uncategorized" folder**,
derived at render time — it has no `folders` row. Its id is a negative sentinel
(`UNCATEGORIZED_FOLDER_ID`) so it never collides with real (positive) folder
ids, and its membership is computed as `all reader feeds − foldered feeds`. It's
pinned last in the tree, hidden when empty, and self-updates as feeds get filed.
Because it isn't a real folder, its context menu exposes only whole-folder
actions (mark-read / refresh) and it's excluded from move-target lists; the
`get_folder_feed_urls` resolver special-cases the sentinel so those actions still
work. The root "All Feeds" folder resolves to *every* reader feed (not just
foldered ones), so orphans and their unread counts are always reachable from the
top of the tree — with one deliberate exception: the Saved Articles virtual
feed (`lectio:saved`). It's a real, orphaned reader feed (backs the Saved/Kept
view), so both root's and Uncategorized's naive "every reader feed" widening
pick it up — and both exclude it outright, in `get_folder_feed_urls`, in
`_home_inner`'s shared `folder_feed_urls_by_id` snapshot (which backs both the
tree's display AND the entry-fetch scope when you actually browse into either
folder), and from tree display specifically. It must never show — or be
browsable, or actionable via mark-read/refresh — as a subscription in Feeds
mode, root or Uncategorized alike.

Saved mode's own "Uncategorized" grouping still needs to reach `lectio:saved`'s
entries — it's an orphan feed same as any other, and belongs in the unfoldered
subset of the Saved view same as it belongs at Saved's root. That reachability
is served by a `star_only`-gated re-inclusion in `_home_inner`
(`selected_folder_id in (root_id, UNCATEGORIZED_FOLDER_ID) and not selected_feed_url`),
not by leaving the shared feed-set inclusive — leaving it inclusive was the
original 2026-08-24 root fix's approach for Uncategorized (reasoning: "the
Saved sidebar's own Uncategorized grouping needs to reach its entries through
it"), but that meant a plain Feeds-mode browse of Uncategorized showed the
whole saved-articles backlog mixed into the real orphan feeds' posts, while
the tree badge (built from a separately-scrubbed display set) read far lower
than what the list actually showed — reported 2026-08-27 as "badge says 5,
but there are tons of unread/starred items in there."

The root is treated as equivalent to Uncategorized for feed placement: both
`add_feed_to_folder` and `move_feed_to_folder` store a feed folderless (no
`folder_feeds` row) when the target is the root id or `UNCATEGORIZED_FOLDER_ID`,
rather than writing a root membership row. This keeps the invariant that a
`folder_feeds` row always means "filed in a real sub-folder," so a feed added to
the root consistently surfaces under Uncategorized. `delete_folder`'s move path
already applies the same rule.

## Folder Properties counts in SQL, not by hydrating entries

`get_folder_properties` looped `reader.get_entries(feed=url)` over every feed in
the folder and counted in Python. On the Deals folder — 17 feeds, 31,843 entries
— the dialog took **74 seconds**; the root folder, which resolves to every feed
in the library, was far worse. Nothing the dialog shows needs an `Entry` object:
a count, an unread count and the oldest date per feed are all aggregates. (A
`newest` date was being computed the same expensive way and never read.)

It now issues one `GROUP BY feed` per 900-URL chunk — chunked because the root
folder is past SQLite's bound-variable limit — using
`COALESCE(published, first_updated)`, the same expression the entry sort window
uses so an undated entry falls back to when reader first saw it instead of
sorting as NULL. Deals answers in 0.25s and the root folder in 1.18s, with
identical totals.

The trade is deliberate: `oldest` no longer honours per-entry date overrides,
and it feeds only the articles-per-week estimate on this one dialog.

## Entry sort window (Pub Old / Pub New)

`reader` only sorts newest-first, so for large folders (`> PER_FEED_QUERY_THRESHOLD`
feeds) `list_entries` fetches the sort window with a direct SQL query and then
enriches only the surviving rows. Both directions order by
`coalesce(published, first_updated)` so an entry that carries no `published`
falls back to when the reader first saw it instead of sorting as NULL. Previously
the ascending path ordered by raw `published`, and since SQLite sorts NULLs first
under `ASC`, date-less imported entries filled the `LIMIT` window and were then
re-dated to their (recent) import time — pushing genuinely old posts out of view.
Imports set a real `published` at ingest where possible: the Inoreader parser
(`_coerce_published`) falls back from the item's `published` to `crawlTimeMsec` /
`timestampUsec`, so newly imported entries carry their true age.

## Remembered sort: Feeds and Saved keep their own

**Feeds and Saved remember their sort separately**, and a remembered sort is only
written by an *explicit* choice. Both halves were bugs:

- One shared `sort_by`/`sort_dir` pair meant picking an order in Saved silently
  re-sorted Feeds. They are different jobs — a publish-date backlog versus a
  to-do pile — so `sort_setting_keys(star_only)` splits them. The unprefixed keys
  stay Feeds' so existing installs keep the value they had.
- The index used to re-save the remembered sort on **every** plain load, passing
  it through `normalize_sort_by` first. So any stored value the normalizer did not
  recognize was silently replaced by the default — the preference destroying
  itself with nothing to show it had happened. Persisting only when the request
  carries an explicit `sort_by` also gives node-specific defaults (Read Mode's
  Inbox opens star-date-ordered) somewhere to live: applied without a URL param,
  they cannot overwrite the scope's remembered order, so leaving the node
  restores it.

**`normalize_sort_by` keeps `starred` behind `allow_starred=True`.** It exists for
Read Mode's Inbox, and blessing it globally let it reach the index, which persists
what it is handed; the regular sort menu has no entry for `starred`, so nothing
rendered as active and the toolbar showed "Published newest" while the list was
ordered by star date. Reported as the Feed view reverting to "Pub new" after
switching in and out of e-ink mode.

**A sort is a pair, and any path that can put half of one into a URL can rewrite
the preference.** This bug has now happened twice in different code. Refreshing a
feed rewrote the remembered sort because `refreshCurrentFeedOrFolder` substituted
its own `'desc'` for an absent `sort_dir` — absent because the templates emit the
parameter only when it differs from `DEFAULT_SORT_DIR` ("asc"), so the JS default
had simply never agreed with the server's. `build_sort_query` then put
`&sort_dir=desc` in the redirect, the index persisted it as an *explicit* choice,
and the preference was gone. It could only bite someone whose preference was
oldest-first. The Read Mode Inbox had the same shape from the other side: its sort
*key* was guarded against persisting but its *direction* was not, so visiting the
Inbox flipped Saved from oldest-first to newest-first and it stayed. The fix in
both cases is to pass the parameter through rather than invent one — absent means
"not in the URL", the redirect carries nothing, and the remembered preference
stands. Suspect this first the next time an order "won't stick".

**The Feeds "Starred" filter (`read_filter=starred`) deliberately never sets
`star_only`, to stay out of this whole mechanism.** It's a peer of All/Unread/
History in the Feeds filter dropdown — literal stars only, within the current
folder/feed scope, ignoring read state — not a sub-mode layered on `star_only`
the way All/Unread narrowing within Saved is. Since `sort_setting_keys` and
`normalize_sort_by`'s `allow_starred` gate both key off `star_only`, setting it
here would silently switch `_home_inner` onto Saved's `saved_sort_by`/
`saved_sort_dir` keys while the user is still in Feeds — corrupting Saved's
remembered order on an explicit resort, the exact bug class above. It's also
exempt from every other automated exclusion (hide_unpremiered, the read/unread
and history checks) once an entry passes the star check in
`list_entries_for_feeds`'s phase-1 loop: starring is already how this codebase
marks "don't touch this" (retention/purge, bulk mark-read), so the filter meant
to find what you starred has to honor that unconditionally, or starring
something hidden would defeat the point of being able to find it again.

## Async bulk mark-read

`/feeds/mark-read`, `/folders/mark-read`, and `/entries/mark-older-than-read` serve two response modes controlled by the `X-Requested-With` request header:

- **`lectio-mark-read`** (sent by the JS fetch path): returns `{"ok": true, "marked": N, ...}` with HTTP 200. The client applies an optimistic in-place read-state update via `applyBulkReadState()` before the fetch completes.
- **Anything else** (native form submit fallback): returns an HTTP 303 redirect to the main page with a `message=` query param.

The JS layer reads the CSRF token explicitly from `<meta name="csrf-token">` and adds it as `X-CSRF-Token` on every async POST.

## The list's SQL ordering has to agree with the app's date, or entries vanish

Past `PER_FEED_QUERY_THRESHOLD` (32 feeds) `list_entries_for_feeds` prefetches an
ordered window straight from the reader DB. It takes the oldest (or newest) N
rows **by its own SQL key**, so a key that disagrees with
`entry_publication_date` does not misplace an entry — it **drops** it.

The key omitted `updated`, which `entry_publication_date` reads second. A feed
shipping `<updated>` and no `<published>` (2,696 entries across 84 feeds here)
was ranked by `first_updated` instead: one entry was 2026-07-21 by `<updated>`
and 2026-08-12 by arrival, so an oldest-N prefetch discarded it from a window
spanning 07-20 to 08-01. It still showed in its folder, which is under the
threshold and uses reader's own query — **"in the folder but not in All" is the
signature of those two paths disagreeing.**

`_ENTRY_SORT_SQL` is now one expression, defined beside `entry_publication_date`.
The URL/title inference tiers are not reproducible in SQL and only apply to
entries this expression already treats as undated.

## `list_entries_for_feeds(..., enrich=False)`: Read Above/Below don't need phase 2

`list_entries_for_feeds` is two phases: phase 1 builds cheap "light" records
(filter, sort, dedupe); phase 2 enriches the clipped top-N with thumbnails,
tags, per-feed display prefs, and YouTube duration/premiere-prefix lookups.
Phase 2 is the expensive part, and it scales with the *view* size, not the
range being acted on.

`mark_entries_range_read` (Read Above / Read Below) calls
`list_entries_for_feeds` twice just to resolve which entries fall on the
anchor's side of a large unread view, then only reads `feed_url`, `id`,
`link`, and `read` off the result. On an 8,472-entry "All Feeds" unread view
it was paying for phase 2 on all of them — ~3.9s of ~10.4s total — for fields
it never used.

`enrich: bool = True` on `list_entries_for_feeds` lets a caller skip phase 2
entirely and get back light records instead (still filtered, sorted,
deduped — just missing the enriched fields). Both call sites in
`mark_entries_range_read` pass `enrich=False`. Any caller that reads
`feed_title` or another enrichment-phase field (e.g. `_resolve_view_posts`,
used by "Move all shown to feed…" and Select All) must keep the default on.

## `_light_entries_from_sql`: enrich=False skips entry hydration too, not just phase 2

`enrich=False` alone didn't fix Read Above/Below's fallback: when the anchor
entry isn't in a fresh unread-only fetch (the ordinary case of opening an
entry — marking it read — then choosing Read Above on it), the fallback
re-fetches with `read_filter="all"` to see the whole folder's history. That
fetch phase, not phase 2, is what's expensive there. Even the >32-feed SQL
fast path (`_ENTRY_SORT_SQL` above) still calls `reader.get_entry()` per
matched row — a full `Entry` hydration (JSON-decoding content/enclosures/
author, building `Content`/`Enclosure`/`Author` objects, a `feeds` join) —
which costs whatever that entry's stored content costs to decode, not just a
fixed per-row fee. Measured live: a feed whose entries carried heavy embedded
content took ~1ms/entry to hydrate this way — 7s for 7,153 entries, in a
17-feed folder (under the 32-feed threshold, so no LIMIT even applied — the
per-feed branch has none) whose Read-Above fallback took 9s total.

`_light_entries_from_sql` fetches only the ~9 raw columns the light-record
loop actually reads (`feed_url`/`id`/`title`/`link`/`published`/`updated`/
`first_updated`/`read`/`read_modified`/`added_by`) and wraps them in a
`_LightEntry` shim — no `Entry` object, no JSON decode, no `feeds` join. The
shim feeds the *same* light-record loop unchanged, so correctness is "does
the shim's dozen attributes match the real Entry's," not a second
record-building path to keep in sync. It reuses `_ENTRY_SORT_SQL` for
`sort_by="post"` — same reasoning as the section above, this is exactly the
sort-key-disagreement class of bug if it didn't.

Gated to exactly what the light-record loop's simple path needs: `enrich=False`,
no tag filter, no search, not star_only, no archived filter, sort_by in
`{post, received}`, read_filter in `{all, unread, starred}`. Tag/search/star/
history/archived views keep using the existing (slower but field-complete)
paths — `_LightEntry` deliberately omits `resource_id`/`feed_resolved_title`/
`summary`/`authors_str`/a `feed` attribute, so if the gate is ever loosened by
mistake, reaching one of those fields raises `AttributeError` immediately
instead of silently rendering wrong data. Falls back to the hydrated path on
any SQL error, same "degrade, never break" contract as `_sorted_star_key_window`.

Link-less entries (Buzzsprout podcast feeds ship no `<link>`, only an audio
enclosure `_derived_entry_link` recovers a page URL from) are rare enough that
widening every row's `SELECT` with a variable-length `enclosures` JSON column
would cost more than it saves — instead, rows with a falsy `link` get one
narrow follow-up query for just their enclosures.

**Known tradeoff, not a new one:** like the existing >32-feed SQL branches,
the SQL `ORDER BY` uses the simple `_ENTRY_SORT_SQL` expression while the
light-record loop's actual sort key is the richer `entry_effective_date`
(URL/title-inferred fallbacks included) — the SQL order only decides which
rows a *bounded* `LIMIT` keeps, and the loop's own stable sort afterward
fixes up the final order regardless. `mark_entries_range_read`'s calls use
`_RANGE_READ_LIMIT` (effectively unbounded), so this never drops a row in
practice today; a future `enrich=False` caller with a small `limit` should be
aware the same approximation applies here as it already does above threshold.

**Fixed 2026-08-28:** the >32-feed ASC/DESC branches' own `read_sql` used
`read IS NOT NULL` for the "read-only" case — always true, since reader's
`read` column is 0/1, never NULL — so a `history` view's SQL `LIMIT` window
could fill with unread rows before the Python `is_read` filter dropped them,
losing real entries rather than just misordering them (unlike the
`_ENTRY_SORT_SQL` approximation above, this one *does* drop rows in
practice). The DESC branch's `received` sort also used `recent_sort` instead
of `first_updated`, disagreeing with the light-record loop's own sort key.
Both now match `_light_entries_from_sql`.

## Moved here from saved.md

**One layout owner, three modes.** The inline shell in `index.html` resolves
`wide` / `medium` / `single` from a single `updateSingleMode()`, at 1100px and
720px. Single-pane mode was removed in `9dab5a8` and revived rather than replaced
with a phone-specific renderer, and that is the whole design argument: a second
renderer means every feed-appearance feature — lead images, per-feed thumbnail
crop and zoom, embeds, the full-image webcomic view — has to be ported to it, and
every future one silently misses it. The phone runs the same markup, so it
inherits all of them and everything added later.

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

**Toolbar listeners must be delegated.** `loadScopePanesWithoutFullRefresh`
(every sidebar/folder/scope click, and the search form itself) re-renders the
toolbar, replacing its DOM nodes. Any listener attached directly to a
`#toolbar-*` node at init dies with the node it was bound to — silently, with no
console error. That is exactly how the search button came to do nothing at all
after the first in-page navigation, while still working on a direct URL load
(which is why it survived testing). The search button, its clear control, the
query input, and the search form's `submit` handler are therefore all delegated
from `document`. Wire anything new on this toolbar the same way.
