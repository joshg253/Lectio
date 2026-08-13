# Views

The post list, folders, sorting, layout and page weight.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## View state model

Keep three kinds of state separate:
- remembered base preferences,
- contextual temporary overrides,
- transient navigation state.

Examples:
- remembered: sort mode, default filters, pane sizing.
- temporary: tag-click “show all,” search result scope.
- transient: current entry, scroll position, focus.

Temporary overrides must not silently overwrite remembered preferences. Leaving the override context should restore the base preference.

### Filtering a view is not searching it

Search and filter are different tools and are kept apart deliberately. **Search**
(`q`) is a server-side query that changes *what is fetched*; it spans title, feed
name, link, authors and summary. **Filter this view** (`#posts-filter-input`)
narrows *what is already in front of you*, instantly, so the result can be acted
on as a set — it matches only title, link and feed name, and is transient
navigation state: a pane swap re-renders the toolbar and the filter comes back
empty by design.

Two mechanics make this safe.

**The filter owns its own class.** `post-item-filtered`, never
`post-item-hidden` — the latter belongs to the scroll-chunking reveal, and
"move all shown" keys off it. One shared class would have turned a filtered bulk
move into a whole-list bulk move: filter to one domain, click move, and the
entire unfiltered list is silently re-filed. Keyboard navigation excludes both
classes; while a filter is active the chunk window steps aside and reveals every
match, because chunking exists to keep a 2,000-row list cheap to paint and the
filter has already done that job.

**Whole-set actions resolve server-side, by predicate.** The list route serves
250 posts on first load and chunks to a 2,000 cap thereafter, so the browser
holds a *page* of the view, not the view. An action that posts the ids it can
see therefore covers a fraction of a large filter and says nothing about it —
which `Move visible to feed…` did. `POST /entries/move-visible-to-feed` takes the
*predicate* instead (folder scope, tag, search, read/star filters, and the filter
term) and re-resolves it here at an effectively unbounded limit, mirroring the
home route's scope derivation. That is correct at any size, and since there is no
id payload to bound, `_MOVE_BATCH_CAP` does not apply to it. A `dry_run=1` call
returns just the count so the confirm dialog can state the real number rather
than the number of rows in the DOM — the dialog names both when they differ
("Move the 60 shown posts… 46 are loaded here; all 60 are moved").

"Shown" means everything matching the active filters regardless of scroll
position: scroll-chunking is a rendering optimization, not user intent. Orphan
archive rows (saves whose feed is gone) are excluded on both sides — there is no
reader entry to move.

`/entries/mark-range-read` solved the same page-vs-view problem earlier with
`_RANGE_READ_LIMIT`; this is that lesson generalized from an anchor lookup to a
whole-set action.

### Back on a phone walks the view stack

In single-pane mode the article pane *is* the page, so Back has to step down the
stack rather than leave: article → the feed list it was opened from → that feed's
folder list → and there it stops, toggling the folder drawer open and closed.

Most of that chain needs no code. Opening a feed from the drawer and opening an
article each push a history entry, so the stack already holds the feed list under
the article and the folder list under the feed list, and a plain Back walks them.

The last step is the exception, and the reason for the machinery in
`templates/index.html`. Backing out of the entry the app was *loaded* on is a
cross-document navigation: the page unloads and `popstate` never fires, so
nothing script can do would intercept it. Instead a **spare history entry** — a
duplicate of the current URL, so pushing and popping it are both visually
silent — is pushed ahead of time by `armDrawerBack()`, and popping it is what
toggles the drawer.

**What gets protected is the bottom of this document's history, not a particular
kind of URL.** The first version decided from the URL shape — arm only on a
folder-scoped list, since an article or feed list "always has a real parent
underneath". That holds only if you *navigated* there in this session. Reopen the
app, restore a tab, or follow a bookmark straight to an article and there is no
parent, so Back went cross-document and closed the tab — the exact bug the guard
existed to prevent, still live on three of the four ways in.

Position is therefore tracked directly: `history.pushState`/`replaceState` are
wrapped to stamp a `lectioIdx` into the state, the loaded entry is stamped 0, and
`popstate` traps when it lands back on 0. An *index* rather than a counter
decremented on each `popstate` is what keeps Forward working — `popstate` fires
in both directions, and only the state can say which way, or how far. (Once you
Back all the way to the floor the re-pushed spare truncates forward history, so
Forward stops there; mid-stack it is unaffected.)

### ⚠ The guard cannot be made airtight, and the manifest is why

Chrome ships a **history manipulation intervention**: history entries a page
pushes *without user activation* are marked skippable, and Back traverses
straight past them. It exists to stop pages trapping the back button — which is
exactly what the spare does. A spare pushed from inside a `popstate` handler has
no gesture behind it and can therefore be skipped, letting Back walk out.

Observed on a Galaxy S21+ as two working drawer toggles followed by the tab
closing, while an automated Chromium run of the identical steps toggled
indefinitely — **headless does not apply the intervention, so no browser test
here can prove the guard holds.** Treat green automation on this feature as
"not obviously broken", never as proof.

The mitigation is to re-arm from real gestures (`pointerdown`/`touchstart`/
`keydown`), since an entry pushed while the user is touching carries activation.
Reading means tapping constantly, so in practice the spare is usually
gesture-made. It is not a guarantee: press Back repeatedly without touching
anything in between and there is no gesture to arm from.

**What actually removes the risk is not being a tab at all.** Lectio ships a web
app manifest (`static/manifest.webmanifest`, `display: standalone`) so it installs
to the home screen; installed, Back at the root backgrounds the app instead of
closing a tab, and the session survives. The in-page guard is the fallback for
browser-tab use, not the primary answer.

**Installability needs a service worker, not just a manifest** — and the main app
did not register one. `offline-probe.js` registers `/sw.js`, but it is loaded only
by Read Mode *and* early-returns without its own `rm-*` elements, so `/` had a
manifest and no worker. The browser then declines to install and "Add to Home
screen" degrades to a plain shortcut that opens in the browser — a tab, which
dies with Back, which is the thing the manifest existed to prevent. `index.html`
therefore registers `/sw.js` itself on load. That adds no caching of the main
app: `_worthCaching` covers only `/read`, `/static/*` and `/api/img`, so its
requests stay network-first and pass straight through.

Note a true standalone install (an Android WebAPK) is minted by Chrome via Play
Services. A Chromium derivative may still only offer a bookmark-style shortcut,
in which case installing once from Chrome is what produces a real app icon.

**This applies to every layout whose tree is a drawer — single *and* medium
(721–1100px).** Gating it on single-pane mode alone was a bug: a tablet sits in
medium, so the guard never armed and Back closed the tab there exactly as it had
on the phone. Wide is deliberately excluded — the tree is a permanent column, so
there is nothing to toggle, and a Back that visibly does nothing is a trap rather
than a step. The guard also re-arms from `updateSingleMode()`, because rotating a
tablet moves it between wide and medium after load.

**Back therefore never exits the app in single-pane mode. That is deliberate**,
and was asked for after a stray press closed the tab mid-read: this is a
self-hosted reader you live in, and losing the session costs more than being able
to reverse out of it. Leaving is still an ordinary browser action — close the
tab, switch apps — it is only Back that no longer does it. Desktop is untouched;
the whole mechanism is gated on `isSingleMode()`.

The spare is re-armed the instant it is consumed, so consuming one entry and
pushing one back leaves the stack the same size: this can run indefinitely
without growing history and without running out of presses (verified at ten
consecutive presses, `history.length` flat). An article and a feed-scoped list
each already have a real parent underneath, so neither arms it, and a scope
param is required so the bare landing screen is excluded — there is no folder
list to toggle there, and Back on the app's front door should do the ordinary
thing. Forward navigation clears the flag, since whatever was pushed now sits
above the spare.

The handler lives in `index.html` rather than `app.js` because it is registered
before `app.js` loads, so `stopImmediatePropagation()` also suppresses that
file's `popstate` handler. That is deliberate: when the spare is consumed nothing
actually navigated, and app.js's handler would refetch the list already on screen.

The list toolbar's top-left control mirrors the same hierarchy. Scoped to one
feed it is a back arrow labelled with the folder name (`selected_folder_name`,
server-rendered) that goes up to the folder's list; at the folder list it is the
hamburger that opens the drawer. Two controls rather than one that changes
meaning, because a button reading "Folders" that does not open Folders is worse
than either.

### Off-site links never open in the reading tab

Following a link in place replaces Lectio and loses your position in the list —
and on a phone, where Back now toggles the folder drawer rather than leaving,
there is no cheap way back to it.

Every off-site `http(s)` link therefore opens in a new tab. This is done by
setting the anchor's **own `target`**, never `window.open`: a real tab is what
the browser's normal activation produces, whereas a scripted open is what popup
blockers stop and what a phone renders as an awkward floating window. `rel` is
set to `noopener noreferrer` alongside it — without `noopener` the opened page
gets a handle on `window.opener` and can rewrite the reading tab out from under
you (reverse tabnabbing).

Deliberately narrow: same-origin links are app navigation, in-page fragments are
how footnotes and tables of contents work and must stay put, and `mailto:`/`tel:`
would hand a blank tab to a handler that cannot close it.

It is enforced in three places because there is no single one that covers
everything:

- **`services/html_sanitize.py`** marks external links as it sanitizes. This is
  the correct fix at the source, but sanitization runs at *ingest*, so it only
  applies to content stored from that point on.
- **`templates/index.html`** carries a capture-phase click listener for the main
  app. This is what covers article bodies stored before the sanitizer change, and
  anything injected into the shell later.
- **`static/reader.js`** carries the same listener for Read Mode, which loads
  none of `app.js` and serves HTML sanitized when it was stored.

The sanitizer allows `target`/`rel` on `<a>` but never trusts them: it overwrites
both on every external link and deletes them everywhere else, so a feed can
neither choose its own target nor drop the `noopener`.

### Resume where you left off

Every attempt to stop Back leaving the app failed, and each failed *on purpose*
at a different level:

- **Chrome** marks script-pushed history entries skippable to defeat back-traps,
  so the spare entry can be walked straight past.
- **Android** exits any app at its root. Installing Lectio as a WebAPK — which
  does work, the manifest and worker meet every installability check — does not
  change that. A standalone install still exits on Back.

So the app stops trying to keep you in it and makes leaving cost nothing
instead. The current position (URL, pane level, post-list and article scroll) is
written to `localStorage` — not `sessionStorage`, which dies with the tab, which
is the exact event being defended against — on `pagehide`, on
`visibilitychange` to hidden, and on every navigation. Opening the app then
lands you back where you were.

The restore runs in an inline script in `<head>`, before anything renders, so it
is a `location.replace` rather than a visible jump — and `replace`, not `assign`,
so resuming leaves no history entry for Back to bounce off.

Three rules keep it from becoming its own trap:

- **A URL with a query is an explicit destination** and is never overridden. Only
  a bare `/` counts as "just opened the app".
- **The wordmark links to `/?home=1`**, so "take me to the top" still exists.
  Without the parameter it would be a bare `/` and would resume, leaving no way
  to reach the landing.
- **Positions older than 7 days are ignored** — long enough to cover a weekend
  away, past which dropping you into a half-read article is a surprise rather
  than a convenience.

Scroll is re-applied after two animation frames plus a short delay, because the
article pane's height depends on images and the chunked post list grows as it
reveals; a `scrollTop` set too early lands against a shorter document and clamps.

Read Mode does not participate — it has its own navigation model and no Back
guard either (see Plan.md).

### Pull down in an article for Reader view

On a phone, pulling down from the top of the article pane toggles Reader view,
and pulling again comes back — the Reader-view button sits in a toolbar that is
easy to miss and awkward to reach one-handed.

This is **not** a revival of pull-to-refresh, which was removed deliberately and
stays removed (`window.bindSinglePanePullToRefresh` is still a no-op stub).
Different gesture, different surface, different action.

The gesture delegates to `#entry-readability-button` rather than reimplementing
the toggle, so both directions come for free from the control that already owns
them. Listeners are on `document` (a pane swap replaces `.pane-entry` wholesale)
and stay **passive**: the browser's own pull-to-refresh is suppressed with
`overscroll-behavior-y: contain` in CSS rather than by calling `preventDefault`
on every touchmove. Three guards keep it from eating ordinary input — the pull
must start at `scrollTop === 0`, travel at least 90px, and be clearly vertical.

### Global audio player

The persistent audio player is a deliberate exception to the pane-swap lifecycle.
The entry view is loaded via `/entries/pane` swaps, so any `<audio>` inside it is
destroyed on navigation. Instead a single `<audio>` + control bar lives in
`templates/index.html` outside the swap target and is owned by
`static/media-player.js`; podcast posts inject a `.podcast-player` Play trigger
(`_apply_entry_media`) that hands the track URL/title to the global bar. Player
state (current track, position, playback speed) is transient client-side state
only — no server or DB involvement — with playback speed persisted to
`localStorage`.

## Page weight: lazy HTML fragments

At thousands of feeds, any template section that renders a row per feed is
megabytes of HTML — far heavier than the posts themselves. The rule: per-feed
row sections must not render inline in `index.html`. They live in `_*.html`
fragment templates served by dedicated GET endpoints, and the page ships a
small container `<div data-lazy-src="…">` that client JS fills on first open.

Current fragments:
- `/settings/feeds/panel/{folders,stale}` (`_settings_feeds_folders.html`,
  `_settings_feeds_stale.html`): the Settings → Feeds folders table (a hidden
  row per feed, including disabled) and the Stale view (every active feed
  ranked by last-post age), fetched on first open of the Feeds tab / Stale
  view.
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
top of the tree.

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
