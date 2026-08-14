# Saved

Read-it-later capture, keeping, and editing a post in place.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## Saved articles (read-it-later capture)

A saved article is an ordinary reader entry in a per-user synthetic feed
`lectio:saved` (`allow_invalid_url=True`, `updates_enabled=False`,
`added_by='user'` so reader never deletes it). Because it is a real feed,
tenancy, read state, tags, keyboard flows and counts apply with no
special-casing — the same reason FakeFeedz uses `file://` feeds.

Saving (`services/saved_articles.py`): entry id = link = fragment-stripped URL;
the page is fetched and readability-extracted server-side, then auto-starred and
enqueued to the archive worker for offline capture. Extraction failure is
non-fatal (title falls back to the URL, the worker retries). A duplicate save
re-stars without re-fetching. The on-star destination fan-out is **not** fired —
saving *into* Lectio should not re-send the article elsewhere.

**Re-fetch follows the entry, not the feed.** A capture does not stay in
`lectio:saved` — auto-filing moves it onto the feed that really publishes it. So
`POST /articles/refresh-content` has two paths: unfiled reuses
`save_article(refresh_content=True)`, filed calls `refresh_filed_article`.
Routing the filed case through the save path would write into `lectio:saved` and
re-create the duplicate filing removed. Feed-provided entries are refused — the
next refresh would overwrite them anyway.

### Batch re-fetch

`services/refetch_batch.py` owns the pacing (`GLOBAL_DELAY`, `PER_HOST_DELAY`,
`HOST_FAILURE_LIMIT`), the host interleave, the estimate, and `run_paced` — the
serial loop returning a fixed vocabulary (`ok / archive / mismatch / dead /
failed / skipped_host`). `POST /saved/refetch-scope` and the two CLI callers all
go through it: bulk re-fetch spends someone else's bandwidth, so pacing is the
design rather than a setting, and one vocabulary stops two callers counting the
same result under different names. `run_paced` takes its clock and jitter as
arguments so tests can assert on delays instead of stubbing them out.

- **The estimate counts the per-host delay**, not the global one — a single-feed
  scope is one host, so 89 articles is 89 x 10s. An early version reported a
  15-minute run as under 4.
- **One job per user, queued not refused.** Two runs would double the rate every
  site sees. A queued scope resolves to entries when it *starts* — an hour in a
  queue is long enough for the scope to change. Runs via `_run_in_user_context`;
  a raw thread loses the tenancy user.
- **`_refetch_worker` owns the `running` flag**, not `_run_refetch_batch`.
  Clearing it per batch made the status pill blink out between queued scopes.
- **A refusal is not a failure** — only real failures count toward dropping a
  host.

Progress lives in a fixed pill (`#refetch-pill`), not a toast: a job measured in
quarter-hours needs a surface that does not fade. Time remaining comes from the
job's measured pace, not the up-front estimate.

### The three re-fetch guards

Each catches what the previous one structurally cannot.

1. **Slug mismatch** — refuses a page plainly different from the stored one.
2. **Opaque URLs fall back to the stored title.** `article.aspx?p=2438407` yields
   one usable word, below the three-word floor, so the guard stood down and
   informit's section index overwrote two articles. When the slug gives nothing,
   the stored title becomes the reference — but only together with
   `looks_like_a_link_index(new_html)`, because a genuine link roundup echoes its
   own title. Calibrated on 1,192 captures (p95 anchor ratio 0.32, p98 0.46 →
   fires at 0.40 with a 20-anchor floor). Protects the *first* destruction only:
   once an entry holds the wrong page, its stored title is the wrong page's.
3. **Right article, wrong node.** Chrome extracts *identically for every post on
   a feed*, so `extraction_matches_sibling` refuses an extraction byte-identical
   to one stored against a **different entry of the same feed**. Fingerprint is
   over visible text (markup varies between runs, words do not), scoped to one
   feed (the same text under two feeds is syndication), and exempt under 120
   characters.

**The third guard remembers this run, because the archive cannot.**
`enqueue_archive` is async, so during a bulk repair entry 2 could not see entry
1's write. A 368-entry run wrote identical comment-section text to five entries
that way. The service keeps a bounded per-feed map of allowed fingerprints (300,
6h TTL, `forget_recent_extractions`). An archive-sourced refusal is deliberately
not recorded — a maybe should not become a fact.

**The archive is not evidence of current state.** It lags every repair: after
that run, 129 of 131 repaired entries still looked damaged in the archive, and
the same lag had inflated the original count (327 of 594 flagged were fine).
Find candidates in the archive; decide against the **reader**, which is what a
person reads. `sibling_extraction_entries` is the same judgement in bulk and
lives beside the guard, so a repair script cannot drift from what prevents the
damage.

Scope is kept articles (starred or tagged) with an `http(s)` link — an unkept
feed entry is rewritten by the next refresh anyway.

**A re-fetch moves Received, never Pub.** Surfacing a capture by writing
`published = now` corrupted the data it sorted by, and under a Pub-oldest sort
did the opposite of surfacing. 101 entries had lost their real dates this way,
some by 16 years. `replace_entry_content(bump_received=…)` moves both received
columns — `first_updated` (backs `Entry.added`, the render-path sort) and
`recent_sort` (backs the SQL fast path); they can disagree for entries written
outside the normal path. `scripts/restore_bumped_publish_dates.py` repairs the
damage from `archived_entry.published_at`, cross-checked against `recent_sort`,
forward drift only — all 101 agreed.

### Whole-page capture (`mode="full"`)

Swaps `fetch_readability_article` for `fetch_full_page_article`: same sanitizer
and tail, but the body-selection step keeps everything. Readability does not
merely under-extract on document-shaped pages, it picks the *wrong node* — on a
DocBook export (84 `<p>` across 68 `<div>`, 13 `<pre>`) it returns one
shell-session `<pre>` and drops the prose, because it scores by paragraph
density.

On both save and re-fetch, because the re-fetch form only helps once an entry
exists. **Never the default, matched exactly against `"full"`** so a stray value
cannot widen a capture — on a blog-shaped page it keeps the chrome readability
strips. The modal checkbox resets on every open: it describes one page's shape,
not a preference. The UI gates on a per-entry `captured` flag, not feed identity;
gating on the feed stripped the escape hatch from every filed article.

### Per-site capture adapters (`services/site_content_plugins.py`)

Five things a site can override, all defaulting to "no opinion" so an unlisted
site behaves exactly as before:

- **`prefers_full_page`** forces `mode="full"` for pages whose content IS their
  images. Readability scores by paragraph density, so on a basslessons.be
  transcription (six sheet-music scans in `div.transImgBorders`) it keeps the one
  scan it ranks highest, drops the other five, and promotes the cookie banner
  above them. An explicit `mode="archive"` still wins — the user asked for the
  snapshot.
- **`extra_embed_html`** supplies an embed the page never ships. This is the case
  `_reinject_readability_embeds` cannot reach: that recovers iframes present in
  the fetched HTML, but basslessons ships an empty `div.videoMask` and fills it
  from `POST /ajax/a_transcriptionVideo.php` (`trans_id` = the link's `?i=`).
  One guarded POST, JSON out, and only the `<iframe>` is taken from the payload.

- **`strip_selectors`** names chrome to remove before extraction. Full-page
  keeps every node by design, including nodes the page never shows: basslessons
  ships its consent banner as `display: none` first in the DOM, so the first
  capture opened with ~700 characters of cookie policy ahead of the music. Scoped
  to the site, so full-page's "keeps everything" contract is unchanged elsewhere.

- **`embed_at_top`** places that embed after the article's first heading rather
  than at the end, and **`strips_first_image_alt`** drops alt/title from the
  first image. Both exist because of what the render path does next: the first
  body image is hoisted to a hero and its alt becomes the caption under it, so a
  scan whose alt restates the headline captioned the article with its own title.

The append runs *after* extraction, so `_append_site_embeds` sanitizes its own
block — otherwise the iframe would join already-sanitized output and never meet
`_sanitize_iframe`. A rejected embed is dropped silently rather than failing the
capture. `youtube-nocookie.com` was already on the embed host allowlist.

Both hooks fire per entry — on Re-fetch and on the star/tag auto-fetch — never as
a bulk pass. Rewriting a whole feed's stored bodies in one go is irreversible;
that is how a Standard Ebooks body was lost on 2026-08-12, recovered only from a
backup.

### Entry points

The **+ Save Article** modal, a bookmarklet (`GET /articles/save?url=…` — a
top-level navigation, so the SameSite=Lax cookie rides along), and
`GET|POST /api/save` for share sheets. `/api/save` follows the Fever model:
session/CSRF-exempt, authenticated by `username` + per-user API token, bound with
an explicit `tenancy.user_context` before the threadpooled save.

**Extension save protocol** (`POST /api/bookmarklet/save`) implements the Readit
extension's wire format `{token, url, html, title}`. The point is `html`: the
**rendered DOM from the user's authenticated browser**, so paywalled and
bot-walled pages arrive with full text and the server performs no fetch. Auth is
token-only (the payload carries no username); CSRF-exempt with a wildcard CORS
preflight — safe because auth is in the body, not cookies — which is required
since the extension's `host_permissions` do not cover third-party backends.
Captured HTML is capped at 6.5M chars.

A captured-DOM save of an already-saved URL is a deliberate re-capture (the user
often cleaned the page first): extraction re-runs, content is replaced by direct
column write (EntryData has no setter), and it bumps to the top. A pinned title
is never clobbered; URL-only re-saves stay light no-ops. One special case: a
capture made *from inside Lectio* would bookmark Lectio's own UI, so
`_unwrap_lectio_reading_url` detects an on-instance URL and stars the wrapped
entry instead. If that entry has aged out but its id is itself an http(s) URL
(common — many feeds use the article URL as the guid), that URL is saved
normally.

### Saved Articles sidebar view

The tree's top row opens the all-starred view (`star_only=1` at root) with an
unread-starred badge, kept live client-side on single-post toggles (bulk
operations let it drift until the next render).

Saved mode and Feeds mode are **mutually exclusive tree blocks**; the pane-swap
path toggles them in `updateScopeActiveState`, since it never re-renders the
tree. Entering Saved opens on **All** and stashes the current read filter in
`resume_read_filter`; **All Feeds** exits star mode and restores it. Within star
mode the read filter **composes** rather than being ignored — only `history`
stays exclusive, since it sorts by read time. Archive-only orphans are excluded
from unread narrowing: they are read by definition.

Clicking the header is an expand-only landing (`saved_home=1`) — loading the
whole backlog is expensive. The sublist runs **All** … **Uncategorized**. The
`lectio:saved` feed is excluded from the Uncategorized *display* set but stays in
its *view* set, so the sublist reaches its entries.

### Tag-as-keep — the unified Kept view

A post is kept — offline-archived, never auto-pruned — when it is starred **or**
manually tagged. Tag is "keep forever", Star the lightweight to-do. The archive
(`archived_entry`) is independent of `saved_entries`, so tagging can enqueue a
capture with no star row.

- **Enqueue on tag.** `set_manual_tags_for_entry` archives on the first tag and
  releases on the last one removed *and* unstarred; the star toggle is guarded
  the same way. Shared guard: `_entry_should_keep_archive` (starred OR ≥1 manual
  tag), so dropping one axis never wipes an archive the other needs.
- **Kept view.** `list_entries_for_feeds` with `star_only=1` filters on star OR
  tag: a `tagged_entries_set` is unioned with `saved_entries_set` into
  `kept_entries_set`. `saved_entries_set` still drives the per-row `saved` flag.
  Counts use the union too.
- **Kept-but-unsubscribed feeds.** reader requires a feed to exist for its
  entries, so unsubscribing a feed carrying curation defaults to keep
  (`keep_entries=1`): folder rows go, the feed is disabled and recorded in
  `kept_feeds`, pending captures are flushed, and `purge_orphaned_feed` is *not*
  called. Hidden at the source — `get_all_reader_feed_urls()` subtracts
  `get_kept_feed_urls()` — while the Kept view unions them back, grouped under
  their original feed name. Re-subscribing clears the row.

**Tagging orphan archives.** An orphan (feed gone from reader, capture survives)
had working stars — `apply_star_state` writes `saved_entries` by
`(feed_url, entry_id)` with no reader dependency — but tags piggyback on reader's
`entry_tags`, keyed to a `resource_id` that no longer exists, so tagging silently
no-op'd while reporting success. `orphan_entry_tags` gives tags the same
independence, as a **fallback only**: every manual-tag entry point tries reader
first and falls back only on a miss.

**A surviving capture is not itself a keep signal.** The orphan detail used to
hardcode `"kept": True` because a capture existed — weaker than everywhere else,
where kept means star OR tag. Of 1,279 orphans, 1,089 were genuinely starred and
190 carried neither. Both flags now reflect the real signals, and
`get_orphan_saved_entries` filters before the Kept view sees a row. Direct links
still open (the fallback does not gate on curation).

**Orphans and search.** Orphans have no reader row, so SQL-narrowed search cannot
reach them; they used to be dropped from search entirely.
`get_orphan_saved_entries` now takes `search_terms` and matches in Python against
title/link/feed_title/author — same AND rule, same tokenization. Metadata only:
decompressing every archived body per search costs more than the orphan set
justifies.

**Why the 190 were deleted.** Once kept meant star-OR-tag they appeared nowhere
— not the Kept view, not search — with no path back except a bookmarked URL. An
item you cannot find is one you cannot curate.
`scripts/purge_uncurated_orphan_archives.py` deletes exactly that set via the
`delete_archive` cascade. A curated orphan is untouched however dead its feed.

### Search: why reader's FTS index is retired

The kept branch runs *ahead* of the generic search fast path, so for a long time
it took no fast path at all — ~11k `get_entry` lookups, ~19s per search.
`_filter_star_keys_by_search` narrows keys in SQL first: ~1.2s.

reader's FTS is deliberately unused. `search_entries` builds a highlighted
snippet per result at ~7.8ms/row — 76s for one common term across 133k entries,
*worse* than the scan it replaced. `_search_entry_keys_in_sql` gave the Feeds
view the same treatment: `python` 19.7s → 1.45s, `guitar` 9.3s → 1.3s.

Two consequences. The two surfaces now share a predicate, so they agree — a Feeds
search reaches article text, not just metadata (`coffee`: 833 → 1,237 hits) — and
both inherit the raw-HTML caveat, since content is matched as stored. And when
the feed set fits under SQLite's 999-variable limit the scope goes into the
query, so `LIMIT` applies to visible rows; above that it matches unscoped and the
caller drops out-of-scope feeds. On any SQL error the helper returns `None` and
the caller post-filters in Python.

Nothing called `search_entries`, and the index was not free: 1.3ms per new entry
on every refresh, plus **564MB against a 743MB reader DB**. It is no longer
built, enabled or updated. `scripts/drop_search_index.py` reclaims the space —
`disable_search()` alone does not, because the DROPs land in the WAL and SQLite
never shrinks a file, briefly *doubling* disk use. The index is derived, so
`enable_search()` + `update_search()` rebuilds it; that walk takes minutes, which
is why dropping it is a script rather than a startup side effect.

### Auto-filing saved articles

`services/saved_autofile.py`, driven from Settings → **Utilities**. An imported
read-later library is mostly articles from feeds already subscribed, so they can
be filed onto their real feed — which collapses cross-feed duplicates for free,
since `_move_entry_to_feed` matches by GUID else normalized link.

Matching is by **article host**, from two independent signals:

- **evidential** — which subscribed feed already carries entries on that host;
- **declarative** — the hosts a feed advertises (its own URL host and its `link`
  host).

Entry links alone fail twice: a feed subscribed but not yet fetched has no
entries, and a link-proxying feed (FeedBurner) points its evidence at the wrong
host. 696 of 2,881 feeds advertise a site on a different host than their feed
URL; adding the declarative signal took unmatched articles from 698 to 66.

A declared host makes a feed a *candidate*; it makes it *confident* only when the
feed is also stocked (`feed_sizes` ≥ `MIN_SUPPORT`). Without that, a scraped
one-article URL on the right host would collect the site's whole backlog —
measured, `guitarworld.com`'s target had 77 supporting entries while
`guitarplayer.com`'s only candidate had **one**, and filing 303 articles into it
would have been wrong. Two guards decide what may be pre-approved: `ambiguous`
(more than one on-host feed) and `support` (below `MIN_SUPPORT` → shown, not
pre-checked). The service is pure — it takes extracted rows, not a reader — so
the guards are testable without a database. Preview is read-only and strips
`entry_ids`; apply takes the target from the request rather than recomputing it.

**Filing is batched, and has to be.** `_move_entry_to_feed` runs at ~17
articles/second, so an uncapped call over 1,300 articles is cut off in flight —
observed as `status 0, 16180ms` with 278 articles really filed and the UI looking
untouched. Each call caps at `_AUTOFILE_BATCH` and reports `remaining`; the
client loops. A batch reporting work outstanding while moving nothing breaks the
loop rather than spinning.

**Moving a saved article deletes its source.** `_move_entry_to_feed` normally
leaves the source (reader cannot delete feed-provided entries), but
`lectio:saved` entries are `added_by='user'` and properly deletable, so the
source is hard-deleted once the move succeeds. Without this the backlog never
shrank as it was filed, and duplicate scans re-read husks.

**Barred targets** (`_autofile_excluded_targets`, on preview *and* apply): Saved
Articles itself, and every YouTube feed — a saved page is never a channel post,
and channels routinely share a name with the blog they accompany. For the same
reason the picker shows each candidate's **feed URL** inline, and folds it into
the option label when two candidates share a title: feed titles are frequently
unlike their URLs (`rss.beehiiv.com/feeds/XYZ.xml` titled "The Woodshed").

**Two different "this isn't a feed" decisions**, labelled apart because they can
appear on the same row:

- **`non_feed_subscriptions`** (*not a feed*) bars a **subscription** from being
  a destination — some subscriptions are a single article URL on exactly the
  right host, the target you would pick by mistake. The subscription is left
  alone; it is also barred on the apply path.
- **`autofile_non_feed_hosts`** (*one-off saves*) settles a **host** whose saves
  never came from a feed. The filer can only observe that nothing matches, so it
  re-proposed them forever. Purely a worklist decision — the articles are
  untouched — reported back collapsed with an undo, keyed on `article_host()`.

**Reviewing an ambiguous host by hand.** Each row links to
`list_feed_url=lectio:saved&read_filter=all&q=site:<host>`. `lectio:saved` holds
only articles not yet attached to a feed, so this drops already-filed posts; it
is synthetic, so `filter_feed_urls` allows it explicitly. `site:<host>`
(`_split_site_terms`) matches the entry's *link host*, boundary-checked, never
the body text, and forces the full-scan path so nothing is clipped.

**Nothing in the plan is ever pre-checked.** `confident` drives a *label*, never
a selection — a scan result is a claim, not an instruction.

### Unstarring tagged articles

`services/unstar_tagged.py`, Settings → Utilities. After the tag-as-keep flip a
tag keeps an article on its own, so a star on an already-tagged article is
redundant and only clutters the read-later queue. Nothing is lost: pruning
protects both axes independently, and the route only enqueues archive removal for
an entry with no manual tags. Only the star row is deleted. The service is the
pure decision layer; DB access and cache invalidation stay with the route, which
recomputes the plan server-side rather than trusting a client id list.

**The UI inverts the API's opt-out on purpose.** The endpoint takes `keep_tags` —
tags to *protect* — but rendering that directly would show ~58 tags pre-checked,
making "unstar everything" the default and *unchecking* the destructive act. The
panel selects tags to **clear** and derives `keep_tags = affected − selected`, so
an empty selection is an empty action. Pinned by tests because it looks
redundant.

**Per-tag counts cannot be summed.** An entry is protected by *any* kept tag, so
an article tagged both `python` and `books` survives a `python`-only selection
while being counted in that row. Every change re-requests the preview and shows
`to_unstar`, with an out-of-order guard. Queue-like tag names (`read`, `todo`,
`later`, …) are excluded from "select all topical tags" — there the star *is* the
queue.

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
