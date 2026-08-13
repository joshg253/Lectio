# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Roughly ordered: quick/concrete fixes first, then cheap UX wins, then items
that need a decision or go-ahead from Josh before they can move, then
measurement/investigation jobs, then scheduled or genuinely low-urgency
work, then the two standing watch-lists, then the one big multi-session
project last.

### basslessons.be (FakeFeedz scrape): real bodies, and video/tabs on keep

Asked for 2026-08-12, investigated but **not built**. Everything below is
verified against the live site, so this is a build job, not a research job.

**Today:** the scraped feed (`file:///data/scraped-feeds/c9d2ca59-….xml`, 36
entries) stores title + link only — `summary` and `content` are both empty. What
renders as the "body" is the lead image (the first music page) with alt/title
text under it.

**Wanted:** (a) the article body should be the linked page's real content, and
(b) on tag/star, capture the embedded YouTube video and all the tab images.

**(a) The tab images are easy.** They sit in the raw HTML in
`div.transImgBorders` — for `transcriptions.php?i=1211` that is
`/partituren/1211-1.png` … `-6.png`, six sheet-music pages, plus the surrounding
text. Ordinary readability/full-page extraction reaches them.

**(b) The video needs one extra call.** It is NOT in the HTML — the page ships an
empty `div.videoMask` ("Searching far and wide for the video") and fills it with
JS. The resolver is reachable server-side, no auth, no JS:

    POST https://basslessons.be/ajax/a_transcriptionVideo.php
    trans_id=<the ?i= value from the entry link>
    → {"status":"success","message":"<iframe … youtube-nocookie.com/embed/fxoeU3vzdEw …>"}

So the adapter derives `trans_id` from the link, makes one POST, and injects the
returned iframe. `youtube-nocookie.com` must be on the embed host allowlist for
the sanitizer to keep it.

⚠ **Do this as a per-entry re-fetch, not a bulk rewrite of the feed.** Replacing
36 stored bodies in one pass is the same irreversible content change that lost a
Standard Ebooks body the same day (the refetch had already overwritten reader's
own entry content, and only a backup got it back). Per-entry keeps it reversible
and lets one be checked before the rest.

### Proxy article-body images through /api/img (main app)

Asked for 2026-08-12. **The retry half shipped 2026-08-12**
(`add_img_proxy_fallback`): a body image that fails to load now swaps its `src`
for `/api/img?u=…` and only gives up if that fails too — the same `onerror` the
hero has always carried. That closed the sonarsource case below. **Preemptive
proxying is still not built, and Josh wants to test that one thoroughly.**

Read Mode already does it (`_proxy_reader_body_images`): every `<img src>` in the
body is rewritten to `/api/img?u=…`, and `srcset`/`data-src` are dropped so the
browser cannot pick a direct URL instead. The main app's article pane does not —
it renders third-party image URLs raw.

Three things that buys, in order of how much they matter:

- **Content blockers stop breaking articles.** sonarsource.com images are served
  from `assets-eu-01.kc-usercontent.com`; the HTML is correct and the image loads
  fine in a clean browser (verified, 1473x331), but a blocker that filters that
  CDN host leaves the article looking empty with nothing in the logs. Same-origin
  URLs are immune.
- **The image cache starts covering article bodies**, which today it does not.
- **No silent `http://`-on-`https://` upgrade dependency**, which only works
  because browsers quietly fix it and does not work offline.

⚠ Not a drop-in: proxying every body image raises `/api/img` traffic and cache
size sharply (bodies carry many more images than heroes do), so the cache budget
and eviction want checking against the live library first — and `srcset` removal
changes what high-DPI screens fetch. Ship behind a per-feed or global toggle so a
regression is one setting away from being undone, and test with a big article
before it goes near everything.

### joanwestenberg: an avatar became the lead image, with a URL that cannot load

Found 2026-08-12, **not fixed.** Two faults in one entry
(`/p/nobody-wants-your-newsletter-you`):

- **The chosen image is the author's avatar** — its stored alt is literally
  "JA Westenberg's avatar". The avatar heuristics never saw it, so whichever path
  resolved this one is not consulting alt text the way the inline scan does. This
  is the same shape as the `Site Icon` miss fixed the same day: the signal was
  right there in the alt attribute and nothing looked at it.
- **The stored URL is mangled and 404s**:
  `https://www.joanwestenberg.com/p/fl_progressive:steep/https%3A%2F%2Fsubstack-post-media…`
  — a Substack CDN URL that was relative-joined onto the post path instead of
  being used absolute. **That unloadable URL is the reported thumbnail flicker**:
  the list renders it, the browser fails it, and the fallback swaps in.

A lead image that cannot be fetched should not be storable — validating a
candidate resolves (or at least refusing one whose host is the *site's own* page
path with an embedded absolute URL) would catch the whole class, not just
Substack.

**Also unexplained on the same feed:** star and re-fetch do not pull content.
Not yet diagnosed.

### Feed known-migrations into discovery, so a 404 is not the end

Idea from the 2026-08-12 404 sweep, **not built.** Working through ~40 dead feeds
by hand, the same handful of *host-level* migrations kept recurring — each one
mechanical, and each one Lectio could have resolved itself instead of reporting
"no feed found":

| pattern | hits that day |
|---|---|
| `blogs.technet.com` / `blogs.technet.microsoft.com` → `devblogs.microsoft.com` | 3 |
| `feeds.feedburner.com/<name>` → the origin site's own feed | 3 |
| `powershell.com/cs/blogs/*` → `powershell.org` | 2 |

Six of that day's twelve replacements were one of these. The rest (Blogger →
custom domain, → Substack, → GitHub Pages) are per-site facts that cannot be
derived and are only worth storing once discovered.

**The machinery already exists — two pieces, doing different jobs:**

- `_SITE_FEED_REWRITES` / `rewrite_known_site_url` in `services/feed_discovery.py`
  — code-level rewriters applied at *Add Feed* time.
- The `feed_url_rewrites` table (`feed_url`, `from_host`, `to_host`), which
  already holds 16 rows including a feedburner→origin rule and a
  beehiiv→custom-domain move.

**What is missing is the connection**: when a subscribed feed starts 404ing,
nothing consults either of them. The proposal is that the failure path check
host-migration rules *before* the feed is declared dead — and, for FeedBurner
specifically, follow the redirect and autodiscover on the destination, which is
general rather than per-site.

Worth doing because it compounds: the technet rule alone fixes every remaining
technet feed at once, and FeedBurner is a graveyard that will keep producing
these. ⚠ Whatever resolves a candidate must still **verify it parses with
reader** before switching (see the fetch-failures notes in ARCHITECTURE.md —
feedparser will happily bless a feed reader then refuses), and must not widen
scope silently: a category feed replaced by the site firehose is a wrong answer
that looks like a right one.

### Failing feeds — re-measured 2026-08-12, with work applied

⚠ **The 950 figure from 2026-08-11 was inflated by stale rows** — since fixed
(see the DeviantArt section): `feed_failure_state` kept a row after a feed was
unsubscribed, so long-gone feeds still counted. Live truth: **238 failing
feeds**, of which:

| failure | count | state |
|---|---|---|
| unparseable / other | 123 | not yet characterized — **next job** |
| HTTP 404 | 69 → **60** | 9 fixed by Change URL 2026-08-12 |
| HTTP 403 | 29 | genuine IP-level walls; email is the only lever |
| 5xx / conn / TLS | ~14 | mostly transient |
| bot challenge | 3 | the new detector working |

**Done 2026-08-12.** All 69 live 404s probed (`scripts/probe_dead_feeds.py`,
read-only by default, paced and honest-UA). 23 had a discoverable replacement;
**only 13 were same-scope, and 9 of those applied cleanly** — all 9 verified
fetching real entries afterwards.

**The probe's own lesson: a discovered feed is not a replacement.** A site that
dropped its feed still serves a homepage, and autodiscovery there returns
*something*. Swapping a broken feed for a wrong one is worse than leaving it
broken, because it looks fixed. Two classes had to be rejected by hand:

- **Widening** (8) — a section feed replaced by the site firehose.
  `blog.google/products/docs/rss` → `blog.google/rss/` is Docs-only → all of
  Google's blog. Same for `towardsdatascience.com/feed/tagged/python`.
- **Collision** (2) — `sourcery.ai/blog` and `/changelog` both resolving to the
  same site-wide feed, which would merge two distinct subscriptions.

**Waiting on a decision (nothing applied):**

- **4 dead feeds whose replacement you already subscribe to** — Change URL
  refused them as duplicates, so these are just redundant dead rows to
  unsubscribe: tartanllama, xubuntu, krshrimali, markjames.
- **10 risky replacements** above — each is a judgement call about scope, not a
  mechanical fix.
- **46 with no feed found at all** — host gone or RSS dropped. Unsubscribe
  candidates, but that needs the go-ahead bulk removal always needs.

### Finish the Instapaper clone (Read Mode follow-ups)

The read-later app (Save any article, Saved sidebar view, Read Mode at
`GET /read`) and its deferred finishing touches (archived-aware counts,
mark-read-after-last-page, image prefetch, dates/sort/Archive-button
readability, Delete/Archive working on tag-kept items, and the Archive vs.
Delete model — Archive keeps tags/offline capture, Delete releases both) all
shipped 2026-07-28/29. Full rationale in ARCHITECTURE.md. One piece from
that work is not yet safe to use:

**⚠ Settings → Feeds → Utilities → Archive old stars — DO NOT RUN YET.**
The cutoff (7d/30d/90d/6mo/1yr) sorts on `saved_at`, but `saved_at` is not a
real star date for most rows: 6,091 of 10,002 stars carry a `saved_at` in
2026-06, which is when multi-user went live and the migration stamped its
own run date instead of preserving the original — mostly years-old
Inoreader stars wearing a seven-week-old timestamp. A 30-day cutoff would
sweep those 6,091 in and a 90-day cutoff would protect them, neither for
any real reason. **Fix before use: offer the date basis, default to
publish date** (asks the better question anyway — "articles from 2019 I
have still never opened"). Only 419 of 10,002 stars have a genuine
Lectio-made `saved_at`; the rest are either real pre-migration dates
(3,492) or the migration timestamp (6,091).

### Phone polish — shipped 2026-08-11, one rough edge left

From a phone testing pass: pull down in an article to toggle Reader view (and
again to come back), Back now walks article → feed → folder → folder drawer
before leaving, and the Global Note no longer opens underneath the folder
drawer. Rationale in ARCHITECTURE.md ("Back on a phone walks the view stack",
"Pull down in an article for Reader view").

- **Back leaving the app is now accepted, not fought.** Installing as a WebAPK
  works (manifest + worker pass every installability check, confirmed via CDP:
  zero errors) and it *still* exits, because Android exits any app at its root.
  There is no configuration that changes that. Resume-on-open is the answer, and
  the in-page guard is now a nicety rather than the mechanism. **Do not spend more
  time trying to prevent the exit.** If resume ever misses a case, extend what is
  saved rather than re-litigating the history stack.
- **Read Mode has no resume.** `/read` keeps its own navigation and is untouched
  by the position-saving above; the Supernote still reopens at the list. Worth
  doing if it annoys, and cheap — the same localStorage key, a different restore
  target.
- **The Back guard is best-effort, by browser design.** Chrome's history
  manipulation intervention skips entries pushed without user activation, so a
  spare re-armed inside a `popstate` handler can be walked straight past — seen
  on a Galaxy S21+ as two toggles then the tab closing, on code that toggled
  indefinitely under headless Chromium (which does not apply the intervention).
  Re-arming from real gestures narrows it; nothing closes it. **Installing to the
  home screen is the actual fix** and the manifest now exists for that. If this
  is still hit while installed, the next lead is whether standalone mode changes
  the intervention's behaviour — do not just add more spares.
- **Read Mode has no equivalent Back guard.** `/read` (the Supernote view) is a
  two-pane layout with the tree always visible, so there is no drawer for Back to
  toggle at the end of its chain — the trick used in the main app has nothing to
  land on. Back out of the Read Mode list still leaves. Left alone rather than
  invented: a Back that visibly does nothing is worse than one that exits. If it
  bites, the fix is to give Read Mode a collapsible tree first.
- **Back never exits the app on a phone or tablet, by request.** The first cut
  ended the chain by letting the next press leave; in use that closed the tab
  mid-read.
  Back now toggles the folder drawer open/closed at the end of the chain,
  indefinitely. The trade is deliberate and worth restating before anyone
  "fixes" it: on a phone you cannot reverse out of Lectio to the previous site,
  and you cannot Back your way to an earlier folder or feed — the tree is how you
  navigate. Closing the tab or switching apps still works normally, and desktop
  is untouched (`isSingleMode()` gates all of it).
- **External links are marked in three places, and one of them is the real fix.**
  The sanitizer marks them at ingest, so bodies stored *before* 2026-08-11 carry
  no `target` and rely on the two client-side capture listeners (main app,
  `reader.js`). A one-off pass re-sanitizing stored summaries would let the
  listeners go, but they are ~10 lines each and also cover anything injected at
  runtime, so there is no pressing reason to.
- **The gesture's return trip is only as good as the button.** Pull-to-Reader
  dispatches a click on `#entry-readability-button`, so if activating Reader view
  fails (a dead source URL, say) the second pull tries to activate again rather
  than coming back. That is the button's existing behavior, not the gesture's,
  but it shows up more now that the gesture makes it easy to trigger.

## Findings (recorded so they are not re-diagnosed)

Not work items. Each is something measured on the live library that
explains a class of future report; check here before investigating one.

### One stored image per entry, but three feeds want two

Found 2026-08-13, **not built.** Three comic feeds want a different image in the
list than in the article, and Lectio stores **one** URL per entry — the list crop
is *derived* from it on the render path, network-free by contract. That works
only when the crop's URL is derivable:

| feed | article | list | derivable? |
|---|---|---|---|
| Penny Arcade | `/comics/x.jpg` | `/comics/panels/x-p1.jpg` | yes — plugin |
| dresdencodak | `dc_minis_N.jpg` | `dc_minis_N_thumbnail.jpg` | yes, for DC Minis only |
| mahonoir | `03-10.jpg` | og `0310thumb.png` | **no** (`03-12.jpg` → `12thumb.png`) |

mahonoir needed no code in the end — the publisher ships a purpose-made preview
card as a media thumbnail, so `media_rss` (manually locked) picks it up. But that
was luck, and the Tuning panel shows a *better* og:image for those posts that
nothing can select while another strategy supplies the lead.

The general fix is a second stored URL plus a per-feed "thumbnail source"
setting (auto / same as article / og:image / media). That is a meta-DB column, so
it needs the startup per-user migration or existing tenants 500. Worth doing when
a fourth feed wants it; not before.

**Check what the feed already provides before writing a plugin** — two of three
needed derivation, one needed only the right strategy.

### og_scrape feeds with no og:image at all

Found in the 2026-08-13 lead-image sweep, **no action taken.** Of 585
auto-detected `og_scrape` feeds, **162 entries' source pages carry no `og:image`**
— they fall back to a body image, which is correct for them. Not broken, but
that bucket is where any future "odd body image was picked" report will come
from, so it is worth knowing it exists before re-diagnosing from scratch.

### "Not dupes" dismissal — no un-dismiss UI yet

Shipped 2026-08-10: `POST /feeds/duplicates/dismiss` records a group's exact
feed-URL set in `dedup_dismissed`, and every completed `/feeds/combine` also
auto-dismisses (survivor + sources), so a group never silently reappears
after a real decision. There is deliberately no surface to *view or undo* a
dismissal — a settings row listing dismissed groups with an un-dismiss button
would be the natural follow-up if a wrong dismissal ever needs clawing back.
Not built since it wasn't asked for yet.

### CodeQL board — watch-note

Board is at zero open alerts as of 2026-08-09 (PR #190 closed out the last 5:
4× `py/polynomial-redos`, 1× `py/stack-trace-exposure` — detail in that PR's
history). **If the reflective-XSS class keeps recurring**, the repo already has the pattern
for it: `.github/codeql/queries/` holds guard-aware copies of the SSRF and
path-injection queries that model our audited guards as sanitizer barriers, with
the stock versions excluded in `codeql-config.yml`. A `LectioReflectiveXss.ql`
modeling `html_sanitize.sanitize_html` / `sanitize_inline_title` as barriers would
end the hand-dismissals. Not built yet — two dismissals is not yet a pattern, and
excluding stock `py/reflective-xss` repo-wide is a heavier trade than excluding
`py/full-ssrf` was.

## Later

*Moved down from Now on 2026-08-13: real, but not what is next.*

### "Filter this view" — shipped 2026-08-11, two follow-ups

Built as decided: **(a)** a local instant-feedback filter over the posts list
(`#posts-filter-input`, matching title / link / feed name) plus **(c)** a
server-side *predicate* move, `POST /entries/move-visible-to-feed`, which
re-resolves the view's scope and filters unclipped instead of posting the ids the
browser holds. Rationale in ARCHITECTURE.md ("Filtering a view is not
searching it"). The pre-existing truncation bug in `Move visible to feed…` is
fixed by the same route — the menu item is now **Move all shown to feed…** and
the dialog names both numbers when they differ ("Move the 60 shown posts… 46 are
loaded here; all 60 are moved"). The `post-item-hidden` footgun was avoided as
planned: the filter owns `post-item-filtered`, and the move/keyboard-nav
selectors exclude both.

What was deliberately left:

- **`list_entries_for_feeds` enriches every record it returns**, so both
  whole-view routes (`/entries/move-visible-to-feed` and the older
  `/entries/mark-range-read`) pay full display work — thumbnails, favicons,
  formatted dates — for entries nobody will render. A `light_only=True` that
  returns the pre-enrichment records would serve both; the move endpoint needs
  only `feed_url`/`id`/`title`/`link`/`feed_title`, and mark-range-read needs
  only `feed_url`/`id`. Not done because it touches a hot, heavily-shared
  function and the existing unbounded caller has been fine in production;
  measure before building.
- **`/entries/mark-range-read` ignores the active search.** It passes scope,
  tag, sort and read/star filters to `list_entries_for_feeds` but never `q`, so
  "mark everything above this" inside a search resolves the anchor against the
  unsearched list. Noticed while modeling the move route on it; not fixed here
  because it is a separate behavior change with its own test surface.

### Auto-file saved articles — the tail

Built and run 2026-07-21: `lectio:saved` went **4,334 → 424**, and the four big
no-feed hosts are gone from the list. Rationale is in ARCHITECTURE.md ("Saved
articles"). What remains:

- **guitarplayer.com's 303 articles** — the site's own subscription is a
  scraped one-article stub (barred as a target), and probing showed many
  article URLs soft-404. **Decision confirmed 2026-08-09: look for/build a
  real guitarplayer feed** rather than leaving them as one-off saves or
  deleting — worth the investigation despite the soft-404s.
- **The orphaned-star sweep — GO-AHEAD CONFIRMED 2026-08-09.** Delete
  `saved_entries` rows whose entry is gone (4,508 total, 4,264 on
  `lectio:saved`). The cause was found and fixed
  (`backfill_saved_entries_from_archive` re-created them at every startup, and a
  second bug in the same function was starring *tagged* entries), so a sweep now
  stays swept. Cleared to run — bulk delete against live data, but confirmed.
- **166 already-converted stars** — tagged entries starred by that backfill
  before it was fixed. Indistinguishable from a genuine star-and-tag, so they
  cannot be surgically reverted; the unstar-tagged pass is what removes them.

### Cross-feed duplicate scan — the dupes you can actually feel

**RE-MEASURED 2026-07-22 — auto-filing collapsed almost all of this.** Before
auto-filing, all-starred items held ~490 duplicate groups (~520 extra
copies), 447 of them cross-feed (`lectio:saved` ↔ the same article starred
in its real feed). After auto-filing: **65 groups, 87 extra copies** — only
3 remain saved↔real (auto-filing's `_move_entry_to_feed`, matching by GUID
then normalized link, merges those for free), leaving 44 groups that are
genuinely two legitimate subscriptions carrying the same article (a site
plus an aggregator) and 18 same-feed.

**Decision confirmed 2026-08-09: fold it into the existing `/saved/duplicates`
scan, as its own section/tier — not merged into the Confirmed/Possible
groups.** Same shape as the "Find duplicate feeds by title" third tier in
the Dupes tab: a distinct, separately-labeled section so the 44
judgment-call cross-sub groups (which subscription should own the post?)
don't get conflated with the mechanical-dedup Confirmed/Possible tiers. Not
a whole new dedicated surface — 87 copies across 65 groups doesn't justify
that.

**⚠ Guard against homepage-links, if this is ever built.** The raw
measurement found one bogus 244-copy group — `romhacking.net`'s feed uses
the site homepage as every entry's `link` — so any cross-feed scan needs to
ignore bare-domain/homepage links and flag oversized groups for review
rather than presenting them as confident matches (same hazard class as the
pre-armed-delete lesson elsewhere in this doc).

**Also found: 354 orphan star rows** — `saved_entries` holds 4,669 rows for
`lectio:saved` but reader has only 4,334 matching entries. Harmless but
inflates counts; worth a sweep if the orphan-star cleanup above ever runs.

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

**Resolution shipped 2026-07-29:** manual per-(feed, tag) dismissal
(`suppressed_feed_tags`, × on each chip, undo at Feed Properties → *Hidden
tags*) instead of a third heuristic.

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
Corpus is too small (4 edited entries / 67 ops) to measure the repeat rate,
but two findings hold regardless:

- **⚠ `feed_url` is the wrong rule key.** Three of the four edits are on
  `lectio:saved`, a pseudo-feed holding saved articles from everywhere, so a
  `feed_content_rules` row keyed on it would apply one site's strip to
  unrelated sites. **Key on the entry-link host instead**, with the feed as a
  secondary scope for real feeds — also fits the six hand-coded per-site
  strips better, since those are per-*site* already.
- **30% of ops can't be promoted at all** (no id, no class, so `tag.class`
  degenerates to a bare tag matching every `<p>`/`<svg>` on the site). The
  matcher must skip these; a promotion UI has to show "12 of 49 removals are
  promotable" or it will look broken.

Re-measure once there are edits on **≥3 entries of the same real feed** —
that's the shape that would justify building this. Until then Phase 1's
hand-cleanup is doing the job.

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

### Home-route latency under refresh — measured, partly fixed

Reported 2026-08-11 as "serious delay browsing". Home requests logged a median of
700ms but **9% over 3s, peaking at 7.2s**, always while a refresh pass was
running. Shipped that day: the route no longer builds a `FeedInFolder` for all
2,867 feeds on every load (only the two folders whose rows actually render), which
removes most of the pure-Python work that was being contended over.

Two dead ends, both measured, so nobody re-runs them:

- **Free-threaded Python is blocked by lxml.** The whole dependency stack
  (pillow, lxml, uvloop, pydantic-core) installs fine on free-threaded 3.14.6,
  but importing the app flips `sys._is_gil_enabled()` back to `True`:
  *"the GIL has been enabled to load module 'lxml.etree', which has not declared
  that it can run safely without the GIL"*. lxml 6.1.1 is current, so there is
  nothing to upgrade to. `PYTHON_GIL=0` would force it, but lxml arrives via
  `readability-lxml` and runs in the **refresh thread** — forcing unprotected C
  code in the one concurrent path is the worst possible place to take that risk.
  Recheck when lxml declares free-threading support; nothing else blocks it.
- **`sys.setswitchinterval()` does not help.** Benchmarked against the app's own
  sanitizer in a background thread with home-route-shaped work in the foreground:
  default 5ms gave p50 22.2ms / p95 30.3ms; 1ms, 0.5ms and 0.1ms were all *worse*
  on latency (25-26ms p50) **and** on refresh throughput. Pure context-switch
  overhead. Do not ship it.

**The open lead.** That benchmark showed a competing CPU thread roughly *doubles*
foreground latency (11ms → 22ms). Production went 0.5s → 7s, which is ~14×. So
GIL contention alone does not explain the spikes and something else is
contributing — candidates, in order of suspicion: the `_home_request_semaphore`
serialising concurrent home renders (it 503s rather than queues, so a phone
retrying compounds it), meta-DB lock contention with refresh's writes, and
refresh's own DB work blocking readers. **Instrument the region between the
`tag_block` and `posts_block` ticks before theorising further** — the whole reason
this took a while to find is that the expensive part sat in a gap with no timing
of its own.

Also corrected while chasing this: refresh is **not** a thread pool. It calls
`reader.update_feed()` sequentially in one background thread, so the contention
is one CPU-hungry thread, not many.

### Parked, deliberately

Genuinely nothing to do here until one of these recurs or a lead turns up —
not scheduled, just watched.

- **makeuseof re-fetch returns white images.** Seen once during testing
  2026-08-06 and never investigated. Waiting on a second sighting rather than
  hunting it cold — Josh will flag it if it recurs.
- **~407 stored feed URLs differ from canonical only by a trailing slash.**
  Harmless: re-measured 2026-08-11 across 2,868 feeds and there are **zero**
  canonical collisions, so no duplicate subscriptions are hiding behind them. A
  normalization pass would tidy the spellings and nothing else. (The *harmful*
  part of this item — 12 feeds canonicalizing to a bare homepage because
  `?feed=atom` was being stripped — was fixed the same day.)
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

### main.py / index.html breakup — extraction map (2026-08-09, investigation only)

`main.py` is 32,112 lines (266 route handlers) and `index.html` is 3,130
lines; CLAUDE.md calls for a routes/services/storage split main.py has only
partly grown into. Mapped 2026-08-09, not yet started — this is the plan to
execute from, not a same-session change (a file this size needs incremental
extraction with tests between steps, not a rewrite).

**What's already there to build on:** a `services/` package (40+ modules)
already exists and is one-directional by convention
(`services/saved_articles.py:79` documents "services must not import
main"). `index.html` already has two `{% include %}`s and a proven
`data-lazy-src` panel-fetch pattern (`_settings_feeds_folders.html`,
`_settings_feeds_stale.html`, served from `/settings/feeds/panel/{name}`,
main.py:30245) for large optional content. `static/js/app.js` (15,736
lines) was already extracted from an inline `<script>` in the page-weight
work — but a *prior* JS-splitting attempt left dead files
(`templates/js/_layout_shell.js`/`_pull_to_refresh.js`, filed under
"Code health" for deletion): verify any new split is actually wired into a
`<script src>`, not just written and abandoned.

**Landmines to respect:** ~7 module-level `PerUserDict` caches
(`_meta_structure_cache`, `unread_counts_cache`, etc.) plus ~10 more locks
and 19 `global` statements must stay singletons — route modules can import
them but must not redefine them, and every `invalidate_*` call site has to
stay wired to the same instance. `get_reader()`/thread-local pooling and
`lifespan` (main.py:1928) are startup-order-sensitive. The shared rendering
core — `_home_inner`, `list_entries_for_feeds`, `get_entry_detail`,
`build_reader_page` — is reused by `/`, `/read`, pane-swap, and the
greader/fever/v1 compat APIs; it is the entangled core, not a clean
per-feature boundary, and should be its own project, not part of a
mechanical file split.

**Proposed order, safest → riskiest:**
1. Integration OAuth/credential blocks (DeviantArt, YouTube, Pinterest,
   Reddit, Inoreader, Quire) → thin `routes/integrations_*.py` modules —
   self-contained, minimal shared-state coupling.
2. Post-refresh automation pipeline (`_run_*_rules_after_refresh`,
   main.py:7024–7908) → `services/automation_rules.py` — called only from
   refresh routes.
3. `index.html` modals (settings tabs, feed/folder properties, add-feed,
   save-article, context menus — ~20 blocks, main.py:1528–3120) →
   `templates/_*.html`, following the existing lazy-panel pattern for
   anything large.
4. Dedup engine (main.py:5401–6324) → `services/dedup.py` — Plan.md already
   flags this as wanted but deferred until characterization tests exist
   ("Consolidate the dedup routes" under Code health); do this step
   alongside that work, not before it.
5. Route modules by URL prefix (`routes/feeds.py`, `entries.py`, `saved.py`,
   `settings.py`, `admin.py`, `greader.py`, `v1_api.py`, …) — mechanical
   once the caches/locks above are confirmed importable as shared
   singletons (worth a `state.py` module for them first).
6. `ensure_meta_schema` (main.py:3279, ~1040 lines, linear DDL) — already
   flagged low-priority/cosmetic; do last if at all.
7. The shared rendering core — do not attempt as part of a mechanical
   split; treat as its own carefully-tested project.


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
  risk set. This is also the **triage list that gates Part C pass 2**, produced
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

Sequence: connect Ino → comparison report (9a) → triage/replace dead feeds → Tag-as-keep
Part C pass 2 (Now) → proxy the only-Ino feeds (9b) → let the plan lapse 2027-03-16
(annual SaaS rarely prorates; worth asking, but plan to ride it out).

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Overlaps
with Inoreader replacement above: some "we can't fetch" feeds get fixed here
instead of via the Ino proxy, so it's worth revisiting once the comparison
report sizes set (a).

### Instapaper-alternative: reader-only view for saved/starred items

The read-it-later app (Save any article, Saved Articles sidebar view, Read
Mode at `GET /read`) shipped 2026-07-09/12 — rationale in ARCHITECTURE.md
("Saved articles", "Read Mode"). Its remaining follow-ups now live under
"Finish the Instapaper clone" in Now.

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

### DeviantArt: 544 feeds → 1 — DONE 2026-08-12

The 2026-08-10 question ("are the 543 individual artist feeds redundant with the
Watch feed?") is **settled and executed**. They were redundant. DeviantArt is now
a single Watch feed.

- 22 legacy `backend.deviantart.com` subscriptions unsubscribed
  (`scripts/drop_legacy_deviantart_feeds.py`) — WAF-blocked, all 22 artists
  already Watched, all 22 also had a working gallery feed, 1 entry between them.
- 521 per-artist gallery feeds **combined into the Watch feed**
  (`scripts/combine_deviantart_galleries.py`), 0 failures. Combine moves every
  entry and carries read state, so history was migrated rather than dropped.

Result: **544 DA subscriptions → 1**, library **2,868 → 2,325 feeds**, survivor
holding **21,857 entries across 493 artists**, 21,847 of them still marked read.

⚠ **The reasoning that nearly blocked this, recorded because it was wrong.** The
Watch feed holding "401 entries covering 34 of 543 artists" reads like a coverage
cap. It is not — only ~23-34 of the watched artists post at all. What settled it:
zero artists who posted since the Watch feed was created were missing from it;
volume is ~5.6 deviations/day against a 50-per-refresh window; and the feed's
intake rate matches the observed posting rate. **Check whether a number is a
limit or just the size of the active set before concluding anything from it.**

**Fixed 2026-08-13.** `purge_orphaned_feed` now deletes the feed's
`feed_failure_state` row, and `scripts/clear_ghost_failure_state.py` cleared the
562 ghosts it had already left. That is why the failing count read 950 when the
live number was 238. Sibling tables still outlive their feeds and are documented
in ARCHITECTURE rather than fixed — `feed_lead_image_strategy` (1,024 ghost
feeds), `feed_fetch_history` (754), `feed_seen_window` (553), `feed_media_scan`
(173), `fever_feed_map` (154), `browser_ua_feeds` (95), `websub_subscriptions`
(74). Untidy rather than wrong, and the API id maps and fetch history are not
obviously safe to drop.

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

**The four originally-candidate firehoses are all resolved as of 2026-08-10**
(checked against the live rule set): Rock Paper Shotgun and GamingOnLinux both
have real tag_filter rules configured. HackerNoon is moot — no longer
subscribed to the general firehose, replaced by four per-tag HackerNoon feeds
(python/c++/cpp/cplusplus), which sidesteps the need for a rule entirely.
PlayStation Blog was always the weak one (its tags are mostly game/studio
names, not topics) and was never worth the effort per the original
measurement.

Multi-word tags are **not** a problem: `parse_tag_filter_spec`
([main.py:5779](main.py#L5779)) splits on **commas, not spaces**, and
`normalize_tag_value` hyphenates to the stored form — so
`+Steam Deck, -Xbox Series X/S` can be typed naturally.

Remaining follow-ups:

- **dev.to adapter** stays API-based (its value is language/reaction
  filtering, not just tags): extend to multiple include tags — one API call
  per include tag, merged + deduped by article id, exclusion applied
  client-side on `tag_list`.
- freeCodeCamp per-tag Ghost RSS (`/news/tag/<slug>/rss/`) remains a fallback
  if include-list recall from the main feed's window is insufficient.

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

**Readit (wereadit.com)** — send-to-Readit is blocked: their
`/api/bookmarklet/save` is unreachable outside their own extension
(Cloudflare challenges both server traffic and the browser CORS preflight).
Revisit only if Readit CORS-enables the endpoint (issue draft handed to
Josh for github.com/mahmoudalwadia/readit-extension). **Import from Readit**
likewise blocked until they expose an export/RSS/API of saves. The reverse
direction works today — Lectio speaks the Readit extension's save protocol
(see ARCHITECTURE "Extension save protocol"), so pointing the extension's
Backend at Lectio is a one-click capture path already.

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

**Dead code sweep, remaining piece** — the three cheapest finds
(`server_posts_total`/`server_posts_sent`, the orphaned
`templates/js/_layout_shell.js`/`_pull_to_refresh.js`, the dead
`LECTIO_SECURITY_MODE` line) were completed 2026-08-10 (the JS files were already
gone from an earlier extraction cleanup; only the template attributes and the
env line needed removing). One left here, more involved:

- **The dormant in-app star-mode tree/JS** that the Read Mode hijack bypasses —
  see "Finish the Instapaper clone" in Now, which lists it as a Read Mode follow-up.

Other:
- **Deduplicate context-menu open handlers** (Sourcery, PR #193): the entry-pane
  title and post-list item each have their own `contextmenu` listener in
  `static/js/app.js` that populates the same dozen-plus `contextPost*` module
  vars and calls the same `setMenuItemVisible(...)` sequence — two ~40-line
  blocks that have to be kept in sync by hand (PR #193 added its two lines to
  both). A shared `_openPostContextMenu(sourceEl, event)` taking the trigger
  element would read every `data-post-*` attribute and set visibility once.
  Predates #193; not chased there to keep that PR small. `'-1'` as the
  Uncategorized-folder fallback is scattered the same way (4+ literal spots) —
  worth a named constant in the same pass, not on its own.
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

- **Entries with no date anywhere sort by received time** — an entry with a
  NULL `published`, no dated permalink and no date in its title has no
  publication date to find, so it sorts by when the reader first saw it.
  (Two bugs that used to inflate this bucket — truthy sentinel dates beating
  real fallbacks, and dead URL/title date-inference code — were fixed
  2026-08-04; `entry_publication_date`/`entry_effective_date`/
  `real_published_date` in main.py are the relevant functions.) A one-time
  backfill persisting the inferred date could still be added if the
  ordering of these specific entries ever matters.

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
