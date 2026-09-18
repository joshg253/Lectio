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
chunking is a rendering optimization, not user intent. Orphan archive rows
**used to be** excluded on both sides ("no reader entry to move") but now merge
in under the same root/whole-backlog-star-view condition the home route itself
uses — `_move_entry_to_feed` grew an orphan-archive fallback (see
`docs/architecture/saved.md`), and until `_resolve_view_posts` merged them too,
Select All and this route silently dropped exactly the rows a user was trying
to bulk-move: reported live 2026-09-14 (the same MakeUseOf stars — see
`saved.md`) as "Select All only selects the non-MakeUseOf posts" after
searching Saved for the feed's own domain, since the home route already
displays its orphaned stars but this predicate-resolve never saw them.
`/entries/mark-range-read` solved the same page-vs-view problem earlier with
`_RANGE_READ_LIMIT`; this generalizes it from an anchor lookup to a whole-set
action.

### The id-list bulk actions (tag/read/star/move) are chunked client-side past `_MOVE_BATCH_CAP`

`_MOVE_BATCH_CAP` (500, `main.py`) is a payload/processing-size safety limit on
the id-list bulk routes (`/entries/tags-batch`, `/read-batch`, `/star-batch`,
`/move-to-feed-batch`) — never a design intent that a selection can't exceed
it. Once Select All could select an entire large view (2026-09-11), hitting it
became routine rather than exceptional: reported live 2026-09-12 as an outright
"max 500 per action" error when bulk-tagging a big selection.

`postEntriesBatched` (app.js) splits `entries` into ≤500 groups and POSTs each
to the same route sequentially (never concurrently — these are real DB
writes), returning every chunk's parsed response for the caller to merge per
its own route's result shape (counts summed, message rebuilt, `still_tagged`/
`now_untagged` concatenated for tags-batch). All four call sites route through
it now. `/move-to-feed-batch`'s sibling `/move-visible-to-feed` (predicate-
resolved, see above) was never affected — it has no id payload to cap in the
first place.

**Star-batch's undo is the one place chunking has a real, accepted limitation.**
`entry_unstar_batch`'s shared undo token is per-request, so a selection split
across multiple chunks gets multiple tokens — the single "undo this batch"
toast only applies cleanly when everything fit in one chunk (`results.length
=== 1`). A selection over 500 still unstars correctly either way; it just
doesn't get one coherent undo affordance across the whole thing. Not solved
further — the common case is well under 500, and building cross-chunk undo
wasn't worth it for the rare one.

**Select All is the one exception, and only when a filter term is active.**
Reported live 2026-09-11: Select All used to share `Move visible to feed…`'s
predicate-resolved, whole-set design (`POST /entries/select-all-visible`,
still there and still used when no filter term is set), but a "Filter this
view" term means something narrower than a tag/star/read/search scope —
"the specific handful of posts I'm looking at right now," not "every post in
the whole view that happens to contain this word," which can be vastly larger
across a big Inbox/folder. So with a filter term active, Select All now reads
the DOM directly — `.post-item:not(.post-item-filtered)` — instead of calling
the server at all. `Move visible to feed…` itself is unchanged: its own
confirmation dialog already states the real total up front ("N are loaded
here; all M are moved") before acting, so there is no silent-surprise version
of that gap to fix there.

**Orphans are NOT excluded here, unlike `Move visible to feed…`.** The first
cut of this fix copied that route's `data-post-orphan` exclusion (orphan
archive rows have no reader entry, so there's genuinely nothing to *move*) —
wrong for a plain multi-select, which every other bulk action (archive,
delete, tag) treats an orphan row as ordinary and individually checkable.
Reported live minutes after shipping: filtering the Inbox down to an
all-orphan set (old saves from since-unsubscribed feeds — exactly what "sort
by size" cleanup surfaces) made Select All report "Nothing to select." Fixed
by dropping the orphan filter from this path; `Move visible to feed…` keeps
its own, for the reason stated there.

### The bulk "Move to feed" target picker excluded the target itself, for a mixed selection

Reported live 2026-09-12: filtering the Inbox to a mix of "already in Guitar
World Lessons" and "not yet" posts, selecting all of them, and opening Move
to feed didn't offer Guitar World Lessons as a destination at all.

`openMoveToFeedModal`'s candidate list is `GET /feeds/curation-count`, reused
from the unsubscribe-migration picker — that endpoint's whole point there is
excluding the ONE feed named by `feed_url` (you can't usefully migrate a
feed's curation to itself). The bulk-move call site passed
`entries[0].feedUrl` as that exclusion, correct for a genuine single-entry
move (don't offer "move this post to the feed it's already in") but wrong for
a multi-entry selection: there's no single "current feed" to exclude, and if
the first selected post happened to already live in the target, the whole
picker lost that feed for every other selected post too — even though the
move route already no-ops (skips, not errors) any entry already in the
target, so showing it as a candidate is always safe.

Fixed by only passing an exclusion for a genuine single-entry move
(`entries.length === 1`); a bulk selection passes an empty `feed_url`, which
matches no real feed and so excludes nothing. No server change — the route's
own `f.url != feed_url` naturally includes everything when `feed_url` is
empty.

### Moving an entry out of the Inbox made it vanish, then come back on reload

Reported live 2026-09-12, right after the fix above unblocked using Move to
feed from the Inbox on a mixed selection: entries disappeared from the list
immediately on a successful move, but reappeared — now attributed to the new
feed — the next time that view reloaded.

`openMoveToFeedModal`'s post-success cleanup drops every moved row from the
DOM on the assumption "moved = left this scope," true for a feed- or
folder-scoped view (the target feed is outside it) but **not** for the Inbox
(`kept=starred`): that view is feed-agnostic, spanning every starred entry in
the whole library regardless of which feed it's under. Moving curation to
another feed doesn't unstar it — `_move_entry_to_feed` carries the star (and
tags, read state) onto the new feed's copy of the entry and only clears it
from the old one (confirmed directly: the old (feed, id) key's `saved_entries`
row is gone, the new one's is present) — so the entry never actually left the
Inbox's server-side query at all. The client just assumed otherwise and
removed a row a reload would put right back.

Fixed by skipping the removal entirely when the current view is the Inbox
(`new URL(location.href).searchParams.get('kept') === 'starred'`) — deliberately
narrower than "any star_only view," since a folder-scoped Saved view (star_only
alone, no `kept=starred`) *does* have a bounded feed set and a move genuinely
can leave it. Verified live: after the fix, moving an entry from within the
Inbox leaves its row in place (confirmed against the real saved_entries rows
that the star correctly moved feeds); a non-Inbox view's removal behavior is
untouched.

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

**Sibling scope swaps (folder A → folder B) replace their history entry rather
than pushing one, or Back walked through every previously viewed folder before
reaching the drawer.** `loadScopePanesWithoutFullRefresh` originally pushed
unconditionally on every scope change; picking three folders in a row stacked
three entries, and Back stepped through folder B, then folder A, before ever
reaching the drawer — reported 2026-08-30. The fix mirrors
`loadEntryPaneWithoutFullRefresh`'s own `currentUrlHasEntry` replace-vs-push
split (built for repeated article swipes): `onScopeList` is true only when
already standing on a real list (single-pane, pane level 1, and — critically —
*not* currently sitting on the drawer's history spare, or replacing it would
overwrite the spare's `lectioDrawerSpare` flag and destroy the whole mechanism
above). Drilling in from the drawer or from an article still pushes.

**That exposed a second, previously unreachable bug: the spare goes stale the
moment a scope swap replaces the entry above it.** The spare's URL is only ever
accurate at the moment `armDrawerBack` pushes it — a duplicate of whatever was
current *then*. Before this fix, reaching it required backing out through every
real intermediate folder entry first (the bug above), so the spare was rarely
the *first* thing Back landed on. Collapsing sibling swaps into one entry means
Back from any list now lands on the spare directly — exposing that it still
points at whatever was on screen when it was armed, possibly several folders
ago (or the bare landing page, if the spare was armed at initial load). Landing
on it opened the drawer correctly but silently reverted the address bar,
document title, and the drawer's "active folder" highlight to that stale URL —
reads as "Back opened the drawer but also went back to Home."

Fixed by detecting the spare via its `lectioDrawerSpare` state flag (not a
magic index number — `landedAt === 0` only ever matched the *original*
bootstrap case, before any scope existed) and healing it in place with
`history.replaceState` before toggling: `window.__lectioLastScopeUrl`, stashed
on every real scope load, is what it heals *to*. No re-arm needed on the heal
path — the same spare entry is reused indefinitely, never consumed. `armDrawerBack`
itself also prefers `__lectioLastScopeUrl` over `window.location.href` when it
re-arms, for the rarer double-press-with-no-tap-between case that pops all the
way to the true floor first.

The toolbar's top-left control mirrors the hierarchy: scoped to a feed it is a
back arrow labelled with the folder name; at the folder list it is the hamburger.
Two controls rather than one that changes meaning — a button reading "Folders"
that does not open Folders is worse than either.

**A third, previously unreachable bug: the hamburger button itself can fool
`onScopeList`.** It flips `data-single-pane-level` back to 0 (peek at the folder
list) without any history operation — no push, no replace, nothing armed or
consumed. `onScopeList` used to read that same DOM attribute to decide
replace-vs-push, so folder1 → hamburger → folder2 evaluated it as `false` (level
was 0, not 1) even though folder1's real entry was still sitting on top of the
stack, unconsumed. Folder2 got pushed on top of it instead of replacing it, and
phone Back landed on folder1 rather than the folder list — reported 2026-09-08.
The pane-level attribute is a *display* signal and the hamburger button is
allowed to change it without navigating; `onScopeList` needs a *history* signal
instead. `loadScopePanesWithoutFullRefresh` already stamps `lectioScopePane: true`
into `history.state` on every real scope load, for no reason but symmetry with
`lectioDrawerSpare` — reading that instead of the DOM attribute means a
display-only peek at the folder list can no longer desync the check, since
nothing about `history.state` changes until something actually navigates.

**A fourth bug, same intervention as the drawer spare: `await fetch` between the
tap and the push/replaceState call was consuming the activation window.**
`loadScopePanesWithoutFullRefresh` and `loadEntryPaneWithoutFullRefresh` both used
to push/replace history only after their pane-swap fetch resolved. Chromium's
history-manipulation intervention marks an entry pushed without live user
activation as skippable, and activation doesn't survive an `await` — so on a real
device (confirmed on a Galaxy S21+/Vivaldi, the same device the drawer-spare
section above already flags for this intervention) phone Back from an opened
article skipped straight past the list's own history entry, landing further back
and forcing a full list re-fetch: looked like "Back pops to the top and the
loaded chunks are gone." Fixed by moving both push/replaceState calls to fire
synchronously inside the tap handler, before the `await fetch` — nothing they
decide (`onScopeList`/`pushHistory`/`url`) depends on the fetch response, only
`document.title` and `armDrawerBack()` still run after it, on the same entry.

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

### Inline LaTeX math (KaTeX) — the first vendored frontend dependency

Math-heavy blogs (quuxplusone.github.io and others) ship `\(...\)`/`\[...\]`
delimiters meant for their own site's client-side MathJax/KaTeX; Lectio stores
that text as-is (nothing to sanitize — it's just text) so the article pane used
to show raw LaTeX source. `renderMathInEntryPane` (app.js) runs KaTeX's
`auto-render` over `.entry-content`/`.entry-readability-content` on every entry
open (both the AJAX pane swap and the first full page load), guarded by a plain
substring check first since most entries have no math at all.

KaTeX (JS + CSS + woff2 fonts only, ~600KB) is vendored under
`static/vendor/katex-<version>/` rather than pulled from a CDN — matching this
project's self-hosted stance (works offline, no third-party origin sees every
article a user opens) — and version-pinned by directory name instead of the
`?v={{ static_asset_version }}` query-string scheme every other static asset
uses, since a vendored release never changes in place; bumping KaTeX means a new
directory and a template path update, not touching the hash.

Delimiters are `\(\)`/`\[\]`, plus `$$...$$` (added 2026-09-18: vitaut.net
writes display math this way — 16 occurrences on one post showed as raw
`$$...$$` source). `$$...$$` is always on — nobody writes a price doubled
like `$$50$$`, so there's no collision risk.

Bare `$...$` is different: "between $50 and $100" would render as broken math
on any feed that isn't actually LaTeX-flavored, so it's a **per-feed opt-in**
(`katex_dollar_math` in `feed_display_prefs`, Feed Properties → Content),
default off, not a global delimiter. Added the same day the `$$` fix
shipped: the same vitaut.net post that motivated `$$` also had 89 single-`$`
inline math spans (variables, `\cdot`, `\log`, `\lfloor` — confirmed live,
none price-shaped) that stayed raw text even with `$$` working. `get_entry_detail`
exposes the resolved pref as `katex_dollar_math` in its returned dict;
`_entry_pane.html` renders it onto the pane header as
`data-post-katex-dollar-math`; `renderMathInEntryPane` (app.js) reads that
attribute and only pushes a `{left: '$', right: '$'}` delimiter onto the list
when it's `1` — the `$$` delimiter itself is unconditional, listed before `$`
so KaTeX's own delimiter matching (which checks candidates in list order at
each position) finds the double-dollar case first. Read Mode's own paginated
reader (`build_reader_page` in main.py, driven by `static/reader.js` — it has
no `app.js`) now ports the same delimiter list and per-feed toggle, resolved
via `get_feed_display_prefs` and carried on `#reader-columns`'
`data-katex-dollar-math`, and runs it before the first pagination measurement
so page counts reflect the typeset size.

### Bluesky's own recovered image must not be treated as author-placed content

Bluesky RSS is text-only, so `get_entry_detail` (main.py) appends the post's
images — recovered via `fetch_post_images` (services/bluesky.py) — as plain
`<p><img></p>` tags onto `content_html` itself. That append then flows into
`_strip_lead_image_opener`, the general dedup pass that decides whether a lead
image already sitting in the body should stay separate at the top or be
folded into its in-body occurrence (see images.md "Choosing a lead image").
For every other feed, an image already present mid-body means the *author*
put it there, so the separate lead gets dropped rather than shown twice. For
Bluesky that inference is backwards: the image is mid-body only because
Lectio itself just put it there a few lines earlier in the same function — the
real feed body never had an `<img>` at all. Treating it as author-placed
nulled `lead_image_url`, so the pane showed no hero even though the list
thumbnail (computed from the same `fetch_post_images` call) resolved fine —
reported live 2026-09-02, root-caused 2026-09-11, fixed by special-casing
`bluesky.is_bsky_feed(feed_url)` in `_strip_lead_image_opener`'s mid-body
branch to leave both the hero and the body copy in place instead of dropping
the hero.

**A second, distinct Bluesky embed shape returned no images at all.**
`_images_from_embed` (services/bluesky.py) recognized `app.bsky.embed.images`
(images under `"images"`, each carrying `fullsize`/`thumb`) but not
`app.bsky.embed.gallery` — a newer, differently-shaped embed (images under
`"items"`, each carrying `fullsize`/`thumbnail`, no nested sub-object).
Reported live 2026-09-18: a real post using this shape showed no images
anywhere despite having seven. Added as a sibling branch to the `images`
check, same fallback-to-thumbnail behavior.

### Bluesky video playback (hls.js) — lazy-loaded, not always-on like KaTeX

Extends the Bluesky image recovery above (see "Content sources"): a video post
(`app.bsky.embed.video`) has a static `thumbnail` and an HLS `playlist`, no
`images` list. `get_entry_detail` (main.py) renders these as a real
`<video controls preload="none" poster="{thumbnail}"
data-bsky-hls-src="{playlist}">` instead of a static `<img>`.

`video.bsky.app` sends `access-control-allow-origin: *`, so the browser can
fetch the manifest/segments directly — no proxy needed. Safari plays HLS
natively (`video.canPlayType('application/vnd.apple.mpegurl')`); every other
browser needs `hls.js`, vendored under `static/vendor/hls.js-<version>/`
following the KaTeX precedent above (self-hosted, directory-pinned). Unlike
KaTeX, it is **not** loaded unconditionally — `initBskyVideoPlayers` (app.js)
only injects the `<script>` tag the first time a pane actually contains one of
these `<video data-bsky-hls-src>` elements, since paying ~600KB on every entry
pane for a feature that applies to one platform's video posts isn't worth it
(see "Page weight" below). `fetch_post_images` and `fetch_post_video`
(services/bluesky.py) share one cached AT Protocol API fetch per post so
resolving both the list thumbnail and the pane's video costs one request, not
two.

No autoplay — click-to-play, thumbnail as the poster frame. Read Mode has the
same gap as KaTeX above (no `app.js`), so a saved Bluesky video article still
shows only the static thumbnail there; tracked in Plan.md alongside the KaTeX
one rather than fixed separately.

**`canPlayType`'s truthy check picked Chromium too, not just Safari — and
Chromium can't actually decode a bare HLS `src`.** The MIME-type probe
returns one of `""`/`"maybe"`/`"probably"`; the original code treated any
non-empty result as "native support," but confirmed live, Chromium's own
answer for `application/vnd.apple.mpegurl` is `"maybe"` — a hint of
uncertainty, not a real decoder. That set `video.src` directly to the raw
`.m3u8` manifest on every non-Safari browser, which has no native way to play
one, so the video silently failed. Reported live 2026-09-13 as "video is
super huge and does not play." Fixed by checking `=== "probably"` specifically
(Safari's real answer) rather than truthiness — everything else falls through
to `hls.js`, same as before.

**"Super huge" was a second, independent bug: a portrait video with nothing
capping its height.** `.entry-content video { width:100%; height:auto }`
(and the tag's own inline `max-width:100%`) constrain width but let a tall
clip's height scale right along with it — a 1080×1920 phone-shot vertical
video stretched to a ~600px column renders over 1000px tall. Images get the
equivalent problem capped already (`applyPortraitImageCap`, keyed off
`img.naturalWidth/Height` once the image decodes), but a video can't supply
that signal the same way: it's `preload="none"`, so `videoWidth`/`videoHeight`
stay `0` until playback actually starts — by which point the oversized layout
has already shipped to the screen. Fixed by carrying the post's own
`aspectRatio` (already present in the AT Protocol embed) through as HTML
`width`/`height` attributes (`_video_from_embed`, services/bluesky.py) —
available synchronously, no decode or playback required — and extending
`applyPortraitImageCap` to also check `.entry-content video[width][height]`
using those attributes directly.

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

**A stray bubble-phase handler can outrun the reveal.** `.post-feed-link` (the
feed name shown on each post row) is a real `<a>` with a correct href to the
feed's own folder, navigated by the document-level capture-phase `<a>`
interceptor (`index.html`). A second, older click listener bound directly on
`.post-item` (`bindPostListInteractions`, app.js) also reacted to any click
inside `.post-feed` — a leftover from before the feed name was a working
anchor — and re-navigated using the *tree's own, already-rendered* `.feed-link`
href instead of the clicked link's, which is often stale (the currently-viewed
folder, not the feed's real one). Root-caused 2026-08-30 via a live browser
repro: two GET requests fired from one click, the second (wrong) one winning
the race and leaving `updateScopeActiveState` acting on the wrong URL, so the
tree never revealed the feed. Fixed with an `event.defaultPrevented` guard —
the same pattern `.post-main-link`'s sibling handler already used — so a real
anchor's own (already-correct) navigation always wins.

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

## Touch-sized controls are a separate axis from layout width

The entry pane's header row (read/star/tag toggles, tag chips, Reader/Web/Share/Note) has two
independent size profiles: mouse-sized (default) and touch-sized (`body[data-compact-
article="1"]`, larger throughout). Which one applies is not the same question as which layout
mode (wide/medium/single) is active — a maximized touch-primary 2-in-1 (Surface Pro and the like)
is routinely `layoutMode: 'wide'`, and a short/landscape phone in `'medium'` still needs touch
sizing.

`compactArticle` (`templates/index.html`) always applies in `'single'` (phone-width — there is no
reliable "is this a phone" signal, so single-pane doubles as one), and additionally in
`'medium'`/`'wide'` when either the viewport is short (`SHORT_VIEWPORT`, 560px — a landscape
phone or a short desktop window, where the desktop header stack out-heights the article) or
`(pointer: coarse) and (hover: none)` — touch is the *primary* input, not merely present. That
pointer/hover pair is sufficient on its own: an earlier version also required
`navigator.userAgentData.mobile`, which is always `false` on Windows (and doesn't exist in
Firefox at all) — every touch-primary Windows 2-in-1 fell through the check entirely, in every
layout mode, until this was found and dropped (2026-09-04, a Surface Pro 6 report).

One residual gap: `pointer`/`hover` reflect the OS's own laptop-vs-tablet mode determination
(tied to whether a 2-in-1's keyboard is attached/detected), not which input a person is actually
using moment-to-moment. A device with an attached-but-nonfunctional keyboard reads as
keyboardless and the detection works; a genuinely-working attached keyboard could make the device
report `pointer: fine`/`hover: hover` even while someone taps the touchscreen. No fix attempted —
no report of it happening yet.

### The header row layout for touch mode: three failed attempts before the fix held

Once compact mode applies to something wider than a phone, the header row needs a different
layout: no back button, no camera-cutout to center against, so the read/star/tag group
left-aligns instead, and Reader/Web/Open-tab/Share/Note (`.entry-pane-alt-actions`) has to stay
pinned to the row's top-right corner *regardless of how many tag chips are open* — a post can
carry anywhere from zero tags to dozens.

Three flex-based attempts didn't hold up under a real, heavily-tagged entry:
1. Plain wrapping flex, alt-actions last in DOM order with `margin-left: auto`. Works until
   enough chips wrap past one line — alt-actions is only placed once every chip already has been,
   so it lands wherever the chip flow happens to run out.
2. `float: right` on alt-actions instead, with everything else turned into ordinary inline-level
   boxes that wrap around it like text around an image. Avoids overlap, but a float can never rise
   *above* its point of insertion in the flow — with a page's worth of chips preceding it in the
   DOM, it still ends up pushed down to wherever that flow ends.
3. `order: 1` on alt-actions (between primary-actions at `order: 0` and the chips at `order: 2`),
   still with `margin-left: auto`. `order` reorders the flex algorithm's placement sequence, not
   just which line something lands on — this put alt-actions *between* the buttons and the chips
   visually, with the auto-margin only having the chips' remaining width to push against, landing
   it somewhere mid-row instead of at the edge.

No amount of flex sequencing can give one item both "always on the first line" and "always at the
true visual edge" when a variable amount of content needs to flow between them. The fix takes
alt-actions out of the flex flow entirely: `position: absolute`, pinned to the row's top-right
corner (the row itself `position: relative`), with the row's own `padding-right` widened to
reserve room for it — deliberately on every wrapped line, not only the first, trading a small
permanent empty gutter on later lines for never again depending on insertion order or a float's
own quirks.

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

## Two feeds with the same display title get a host suffix in the tree

Two subscriptions can legitimately share a title — a scraped/renamed feed, or two
publishers who both called their feed "Latest News" — and the sidebar's own row
had nothing to tell them apart except the URL in the hover tooltip, useless on
touch and easy to miss even with a mouse. `_disambiguate_feed_titles` (called
after each of the tree's three per-folder feed-list builders sorts its list:
the eager selected-folder render, the lazy `/tree/folder-feeds/{folder_id}`
fragment, and the Settings → Feeds → Folders table) groups a folder's
`FeedInFolder` rows by title and appends `" — host"` to every member of a group
with more than one entry, using `_feed_url_display_host` (bare host, leading
`www.` folded, same shape as `_entry_link_site_host` for entry links but over a
feed URL string rather than an entry object). A group that still collides on
host too (two feed variants on the same site) falls back to the full URL rather
than inventing a second disambiguator — rare enough that a longer suffix beats
new machinery for it.

Deliberately scoped to *one folder's own feed list*, not the whole library: the
actual risk ("invite unsubscribing the wrong feed") only exists between feeds
sitting in front of you in the same list, and per-folder scoping is also what
the tree's lazy-per-folder loading model already assumes — a global title index
would mean holding every feed's title in memory at once, the exact cost the
lazy fragment route exists to avoid at thousands of feeds. Applied after
sorting (not before), so the disambiguation only changes what's shown, not the
alphabetical order.

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

**Third occurrence, 2026-09-13: a shared template variable, not a JS default
or a read-side gate this time.** `index.html`'s sidebar builds Feeds-tree and
Saved-tree links from the *same* `_tree_sq` fragment — the current view's
active sort, whichever scope it belongs to. Sorting Saved by `size` sets
`_tree_sq` to `&sort_by=size&sort_dir=desc`; the Saved-tree links are
supposed to carry that (it's their own scope's current order), but the
Feeds-tree links (present in the DOM, just `hidden` while `selected_star_only`
— the sidebar can flip between them client-side) got stamped with it too.
`sort_by=size` on a plain Feeds request falls back to the default safely (the
existing `allow_starred` gate), but `sort_dir=desc` is valid in *any* scope
and sailed through — clicking one of those Feeds-tree links persisted "desc"
as the Feeds scope's own remembered direction, flipping "Pub old" to
"Pub new" with no menu click involved. Reported live as "my FEEDS sort keeps
getting reset to Pub new" (misleadingly — `sort_by` never moved, only
`sort_dir` did; the label happens to read "Pub new" for `post`+`desc` same as
"Pub old" does for `post`+`asc`). Fixed two ways: `_feeds_tree_sq` (empty
whenever the current view is Saved, `_tree_sq` otherwise) is what the
Feeds-tree links use now, so they never carry a foreign scope's sort at all;
and, as defense in depth against a stale link or address-bar URL from before
this fix existed (the user's other reported trigger, "force-refreshing"), the
home route's persistence guard now refuses to write *either* half of the sort
when the incoming `sort_by` names a value that belongs to the other scope
(`starred`/`size` outside `allow_starred`) — which also closes a second,
worse variant of the same gap: that request would have clobbered a
genuinely different remembered Feeds `sort_by` (say, `received`) back to the
hardcoded default too, not just flipped its direction.

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

### The remembered read-filter default must only come from the root scope

Same bug class as the sort one above, raised 2026-08-31: toggling Read/Unread while
looking at one single feed was silently changing what the root "Feeds" (all) view
opens to on the next visit. `lectio-read-filter`/`lectio-read-filter-saved`
(localStorage, mirrored to a same-named cookie the server reads for the default
when a request carries no explicit `read_filter`) were written on *any*
`.filter-menu` pill click, keyed only by `star_only` (Feeds vs Saved mode) — never
by scope. A folder, feed, or tag view's filter choice is for that view alone, not a
vote for every other view's default.

`_readFilterPillIsRootScope(url)` gates both write sites (the capture-phase link
interceptor and the plain filter-pill click listener — kept as two listeners
deliberately, to avoid a capture/bubble race, so both need the same gate):
a click only persists when the clicked link has no `feed_url`/`list_feed_url`/`tag`,
and its `folder_id` (if any) matches the tree's own root folder id
(`.tree[data-root-folder-id]`). Anything narrower still filters the current view via
the URL param — it just stops overwriting the shared default.

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

## `list_entries_for_feeds` retries a larger fetch window when hide filters under-fill a page

Same family of bug as the SQL-ordering one above — a fetch capped at `limit` rows, filtered
*after* the fact. `hide_locked_comics`/`hide_unpremiered` drop a matching entry in the per-entry
filter loop that runs on whatever `_list_entries_for_feeds_fetch` already fetched; when enough of
those `limit` rows are locked/unpremiered, the page renders shorter than requested — down to
completely empty if every one of the newest `limit` entries happens to be locked, even with plenty
of real, unlocked entries further back. Reported live 2026-09-06 alongside the `hide_locked_comics`
feature itself (Sourcery review), and pre-existing for `hide_unpremiered` since it shipped —
narrow enough in practice (needs enough gated entries clustered in one fetch window) that it went
unnoticed there.

**Why the fix is a retry wrapper, not a SQL predicate.** `_list_entries_for_feeds_fetch` picks from
several different fetch strategies depending on the view shape (a direct SQL scan for many feeds,
`reader.get_entries()` per feed for few, point-lookups for Saved/history) — some go through raw SQL
this codebase controls, others through reader's own query builder, which doesn't expose a hook for
an arbitrary WHERE clause. Pushing the predicate down would mean a different implementation per
fetch strategy. Worse, `hide_unpremiered` isn't even a plain column: `_is_youtube_unpremiered`
parses a video id out of the entry's own link and looks it up in a separate live-status cache — not
something a meta-DB join could express at all without hydrating every entry first, which defeats
the point of filtering before hydration.

`list_entries_for_feeds` is now a thin wrapper: call `_list_entries_for_feeds_fetch` once at the
requested `limit`; if the result already fills it, return immediately (the overwhelmingly common
case, one extra comparison and nothing else). If it's short, check whether hide_locked_comics or
hide_unpremiered could even apply to this scope — either global toggle on, or any feed in
`feed_urls` with the per-feed pref set (`get_all_feed_display_prefs`, the same table the fetch
itself already reads unconditionally on every call, so this second read is the same cost class
already accepted there). If neither could apply, the shortfall is just "this view doesn't have
`limit` entries" — the ordinary case for most small folders — and nothing further happens. Only
when a hide filter is actually in play does it retry at progressively larger fetch windows
(`_HIDE_FILTER_UNDERFILL_RETRY_LIMIT_MULTIPLIERS`, ×2/×4/×8 of the original `limit`) until the
request is satisfied.

**No early exit on a short *retry* result, on purpose.** The natural-looking shortcut — stop
retrying once a bigger fetch returns fewer rows than it was given — is wrong here: a short
*filtered* result says nothing about whether the *raw* fetch underneath was exhausted, since the
filter is exactly what's removing rows. (Tried it; a feed with 4 locked entries ahead of 3 real
ones plateaued at 2 real entries on the first retry and never found the third, because the
filtered-length check looked exhausted when the raw fetch wasn't.) Externally there's no way to
tell "upstream ran out" from "the filter is still eating rows" using only this function's own
return value, so the fixed multiplier list is the actual bound — a feed that's *entirely*
locked/unpremiered costs a handful of retried fetches before giving up empty, not one.

**A "give me everything" caller must never enter the retry at all — found the same day.**
`_resolve_view_posts` and `mark_entries_range_read` pass `limit=1_000_000` as a "no real limit"
sentinel (see below): `len(result) >= limit` can never be true for either, so without a separate
guard the retry gate's *only* remaining condition was "does a hide filter apply anywhere in
scope" — true for any "All Feeds"-scale view once a single feed anywhere had `hide_locked_comics`
on — and it fired on **every** such call, each attempt repeating the identical full-cost fetch for
nothing (the fetch was never actually window-bound; a million-row cap could not have been what
truncated it). Measured live: a 3,832-entry "All Feeds" unread resolve went from ~5s to ~20s, one
real fetch plus three wasted retries that each re-fetched and re-filtered the same 3,832 rows.
`_HIDE_FILTER_UNDERFILL_RETRY_MAX_LIMIT` (10,000 — comfortably above any real paginated `limit`,
comfortably below the "unbounded" sentinels) skips the whole retry above that ceiling, restoring
the ~5s cost for those callers while leaving genuine small-page underfill handling untouched.

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

## "Add link to Note" — append with the cursor ready, not a silent write

Raised 2026-08-30: a fast way to drop a problematic post's link into the Global Note while
browsing, from the per-post context menu (list and entry-pane title share one menu, so this
covers both automatically) and a dedicated entry-pane button (`entry-add-link-to-note-button`,
`data-entry-link` stamped at render time).

`openGlobalNoteWithLink(link)` (app.js) opens the modal with the link **appended**, not saved —
the note isn't submitted until the user does, so they type their own context right there rather
than a background write happening silently. It runs its own fetch of `/settings/global-note`
rather than reusing the `[data-toggle-panel]` handler's own load-on-open fetch (which also targets
`global-note-modal`) — two concurrent fetch-and-compare-against-the-textarea calls on the same
open would race each other. Falls back to appending onto whatever's currently shown if the fetch
fails, rather than doing nothing.

## `.posts` scrolls, `.pane-posts` never does — a stale assumption broke chunking on phones

Root-caused 2026-08-31 from "All isn't chunking on my phone." `setupPostChunks`' scroll-trigger and
the chunk-delta append's scroll-position preservation both special-cased single-pane (phone) mode
to read/bind `.pane-posts` instead of `.posts`, on the belief that `.pane-posts` is what scrolls
there. Measured live: `.pane-posts` has `overflow-y: hidden` and reports `scrollHeight ===
clientHeight` in every mode — it never scrolls, anywhere. `.posts` (the item container
`setupPostChunks` already queries for `getPostItems()`) is the actual scrolling element
universally, confirmed by a genuinely-overflowing `scrollHeight` (2803) vs `clientHeight` (761) on
a seeded 30-item list.

Binding the `'scroll'` listener to an element that never scrolls means it never fires, so
scroll-triggered server fetches for more items silently stop working — the list caps at whatever
was already in the DOM. The same wrong element fed `ensureViewportFilled`'s "how much room is
left" calculation too: reading a fixed-size wrapper as if it were the scrollable one always
computed ~0 remaining, so the initial fill loop kept growing the local reveal window past the
normal chunk size until everything already present in the DOM was shown — the on-screen symptom
("the scrollbar was tiny and it kept scrolling forever") is that over-revealed local batch, with no
further server fetch ever following it once you'd scrolled through it. Both call sites now just use
`postsContainer`/`postsInnerEl` (`.posts`) unconditionally — no single-pane branch, since the
branch's premise was never true.

## The client can't derive "which chunk is next" from what actually rendered

Follow-up to the section above, found 2026-09-05 from "FEEDS->All only loads the first chunk,
forever" — a real, general bug in the chunking mechanism itself, once the scroll-target fix above
made it possible to actually retry.

The client used to compute the next server chunk as `floor(renderedItemCount / CHUNK_SIZE) + 1`.
That's only correct if the rendered count always exactly matches what the server fetched for that
chunk — but per-entry filters (hide-unpremiered YouTube, tag narrowing, a star/kept mismatch) run
*after* the raw fetch and can drop entries the SQL-level query didn't exclude. Fetch 10, render 9
(one filtered out), and `floor(9/10)+1 = 1` — the *same* chunk as before, forever. Every retry
re-fetches the identical top slice; the client's own duplicate-detection correctly recognizes them
as already on-screen and appends nothing new; nothing ever detects zero progress to stop the retry
loop. It only takes one filtered-out entry landing in the current chunk window — far more likely
across a library of thousands of feeds ("All") than in a small folder, which is why it surfaced
there first, but the bug isn't specific to scope size.

Fixed by not deriving the next chunk from render count at all: the server already knows exactly
how many raw entries it asked `reader` for (`limit`), immune to whatever gets filtered out
downstream, so it computes `next_chunk = (limit // CHUNK_SIZE) + 1` itself and ships it as
`data-next-chunk` on `.posts`. The client reads that instead of back-computing it, with a
`lastRequestedChunk` floor as a last-resort guard against ever re-requesting a chunk already asked
for. The chunk-delta merge path (which appends new items into the live `.posts` element without
replacing it — see above) carries the fresh `data-next-chunk` value onto that live element after
every merge, so it keeps advancing across repeated incremental loads and not just the first one.

`CHUNK_SIZE` and the scroll-trigger's lead distance are both tunable in one place each
(`main.py`'s `CHUNK_SIZE`, `app.js`'s `maybeRevealOnScroll` threshold) — `data-chunk-size` on
`.posts` reads `CHUNK_SIZE` from the template context rather than carrying its own hardcoded copy,
for the same reason `data-next-chunk` exists: two numbers that must agree cannot be allowed to
drift by being written twice.

### `next_chunk` is an offset, not a page number — the 2026-09-05 fix wasn't the whole story

Found 2026-09-11 from "sorting Inbox by Biggest First doesn't seem to work" / "not sorting by the
whole potential view, it's like the first X-number of chunks." Verified against the live library
that `list_entries_for_feeds`'s sort itself is correct and stable (a limit=100 fetch and a limit=140
fetch return identical, correctly-nested prefixes) — the bug is entirely in how the route turns a
growing `limit` into chunk boundaries.

The section above fixed *deriving* `next_chunk` from the requested `limit` instead of the client's
rendered count, but `next_chunk` itself was still `(limit // CHUNK_SIZE) + 1` and the chunk_delta
slice was still `posts[(chunk-1)*CHUNK_SIZE : chunk*CHUNK_SIZE]` — both assume `len(posts) ==
limit` always. That assumption breaks whenever **server-side** filtering shrinks the result below
`limit` — concretely, `list_entries_for_feeds`'s cross-feed dedupe (`build_entry_dedupe_key`): the
same article saved once via its live feed subscription and once via the bookmarklet/extension
capture (two different `feed_url`/`entry_id` pairs, same link+title) collapses to one entry. A big
Saved/Kept backlog accumulates plenty of these. And unlike the hide-unpremiered/tag-narrowing case
above, the shrinkage isn't a fixed amount — a *bigger* `limit` can catch additional duplicate pairs
that a smaller one didn't even include yet, so the gap between "requested" and "real" grows
unevenly as the window grows. Page-number arithmetic assumes that gap is always zero: whatever an
earlier, smaller-window request's dedupe pass already removed is permanently unreachable, because
the next chunk's fixed offset starts counting from the nominal (not real) prior total and never
looks back for it.

Fixed by tracking a true offset instead of a page number. `next_chunk` is now `len(posts)` itself —
captured right after dedup/orphan-merge, before the chunk_delta slice runs — not a formula. An
incoming `chunk` value means two different things depending on whether `chunk_delta` came with it:
plain `chunk` (e.g. single-pane's small initial fetch) keeps the old page-count meaning, since that
request has no prior "already have" state to offset from; `chunk` *with* `chunk_delta` is the
offset itself, so `limit = min(offset + CHUNK_SIZE, 2000)` and the slice is a plain `posts[offset :
offset + CHUNK_SIZE]`. This works with **no client change** — the browser already just echoes
`data-next-chunk` back verbatim as the next `chunk` param; only what that number *means* changed.
Correct regardless of how much dedup happens at any point, because the windowed sort paths
(`_sorted_star_key_window` et al.) are a stable, prefix-preserving function of the underlying key
set — confirmed directly: a bigger `limit` only ever appends past what a smaller one already
returned, never reorders or drops from it.

`tests/integration/test_saved_inbox_chunking.py` gained a dedicated regression test seeding a
cross-feed duplicate inside the first chunk's window and chaining a full chunk_delta sequence
through it — confirmed to fail against the pre-fix code (reverted main.py, same test) and pass
against the fix.
