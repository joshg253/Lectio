# Lectio Plan

Backlog and staging area for future work. Completed work lives in git history —
this file only tracks what's still open.

## Now (priority order)

**Current focus: Saved Articles — finish the read-later app, then get the backlog
under control.** Items **#1–#7** are that epic, in dependency order: fix what's
broken (#1–#2), organize the pile (#3–#6), finish the Instapaper-clone surface
(#7). **#8** is the daily-polish bucket to slot in whenever. **#9** is a single
command with a decay clock — run it any time, it doesn't queue behind anything.
**#10–#12** are unrelated and genuinely deferrable.

**The cleanup order inside #3–#6 matters** and isn't arbitrary: auto-file (#4)
merges curation between duplicate copies, which changes which entries carry stars
and tags — so unstarring (#5) and dupe-scanning (#6) must come after it or they
operate on a set that shifts underneath them. Re-measure between steps.

**Inoreader renews $69.99 on 2027-03-16** — confirmed 2026-07-21, ~8 months of
runway at ~$5.83/month, so the Ino chain (#10) is **scheduled work, not urgent**:
start ~Dec 2026, leaving ~3 months to validate before renewal. The motivation is
consolidation and ownership, not cost.

**Measured 2026-07-21 (read-only against live data) — the numbers driving the
epic's shape:**

| finding | number | item |
|---|---|---|
| saved articles with no feed, but a host matching one you subscribe to | **3,974 of 4,334 (91.7%)** | #4 |
| starred items that also carry a tag (star now redundant) | **1,643 (14.9% of starred)** | #5 |
| duplicate groups the *current* scan can see (`lectio:saved` only) | **5** | #6 |
| duplicate groups actually in the Saved **view** (all starred items) | **~490, ~520 extra copies** | #6 |

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
replaced. That same cost is why a **Feeds-view** search still takes ~10s; it sits
on `_search_entries_fts` and is now the slowest search surface left. Open item.

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

### 2. Saved capture quality — a raw / full-page save mode

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

Why none of the existing escape hatches help — all three call the same
`extract_readability_article`, so they are deterministic re-runs of the same
failure:

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

### 3. "Filter this view" — client-side list filter, then act on what's shown

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
3. **The server sends the whole list, not a page** — the client reveals it 10 at a
   time on scroll. So a client-side filter really does span the entire view rather
   than one page's worth. Good news for "filter to a domain, move all of it."

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
- **"Not a feed" per host.** Josh: some of these "need to be converted to just
  single saved items" — dummies.com, python.plainenglish.io and the like never
  came from a feed, so the filer could only keep re-proposing them. Marking a
  host drops it from the worklist for good; the saved articles are untouched
  (verified: entries and stars unchanged), since they already *are* standalone
  saves. Marked hosts stay reviewable in a collapsed section with undo. New meta
  table `autofile_non_feed_hosts`, created in `ensure_meta_schema` so the
  startup per-user migration covers existing tenants.
  **This is what empties the list:** after Josh's filing pass the remaining
  backlog was 1,076 articles across 72 hosts, of which **735 across 49 hosts had
  no feed at all** — 614 of those in just guitarmasterclass.net (463) and
  guitarchalk.com (151), and 35 more being one-off single-article hosts.
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

**Still open in this area:**
- **Match at import time.** `services/instapaper_import.py` should run the same
  matcher so a future import lands filed instead of piling into Uncategorized.
- **The 735 with no subscribed feed** (guitarmasterclass.net 463, guitarchalk.com
  151, …) are saves from sites Josh doesn't subscribe to. Filing can't help;
  they either stay in Saved or become new subscriptions.
- **Soft-404s are invisible to the dead-link checker.** Probing 8 guitarplayer
  articles: all returned **200**, but 4 had been redirected to the bare
  `/lessons` index — the article is gone and the site answers 200 for it.
  `_check_saved_url` only counts 404/410 as dead, so this whole class reads as
  alive. Detecting it needs a "redirected to a URL much shorter than the
  original / to a known index path" heuristic. Relevant to #1's dead-link
  arming and to any retention pass.

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

### 5. Unstar items that carry tags (DB one-off, then a Utilities button)

After the tag-as-keep flip a tag *is* a keep signal, so a star on an already-tagged
item is redundant — it only clutters Saved, which should be the read-later queue.
Josh's idea: do it at DB level now, add a Utilities button for later upkeep.

**Measured 2026-07-21 — this is safe, and I checked the thing that would have made
it unsafe:**

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

**Josh's hunch ("there's gotta be more dupes in there") is correct, and the reason
the scan disagrees is that it's looking at the wrong set.** Measured 2026-07-21:

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

- **Archived-aware node counts** — tree counts are currently total-saved, so the
  Archive split isn't reflected in the numbers. Most visible wrongness; do first.
- **Mark-read only after the last page** — today a peek marks the whole article
  read.
- **Prefetch the next article** — cuts an e-ink refresh flash on every advance.
- **Excise the dormant in-app star-mode tree/JS** that the Read Mode hijack
  bypasses — dead weight now that the sidebar row opens `/read`. Pairs naturally
  with the dead-code sweep in Later's "Code health".
- **Optional per-image `grayscale(1)`** — e-ink nicety, lowest value.

Reassess the "pinned saved-tag shortcuts" and "badge counts total instead of
unread" ideas *after* #4 lands — auto-filing changes what the tree looks like, so
judging those now would be premature.

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

### 9. Tag-as-keep — Part C: run pass 1 now, defer pass 2

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

- **Autocomplete while typing** — auto-list matching existing tags during tag
  entry. Broader than the deferred rule-form autocomplete noted under "Tag
  filtering for firehose feeds"; if built, do it once as a shared control and
  cover both the rule form and normal per-entry tagging.

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
