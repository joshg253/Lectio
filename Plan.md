# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Roughly ordered: quick/concrete fixes first, then cheap UX wins, then items
that need a decision or go-ahead from Josh before they can move, then
measurement/investigation jobs, then scheduled or genuinely low-urgency
work, then the two standing watch-lists, then the one big multi-session
project last.

### Ino import resurrects deliberately-unsubscribed feeds

Bit twice in one day (2026-08-23/24). The `subscriptions` phase of
`_inoreader_drip_step` (`main.py` ~26117) does `reader.add_feed(furl,
exist_ok=True)` for every feed Ino still lists as subscribed that's missing
locally — no check for *why* it's missing. First hit: the initial full Ino
import re-added 396 feeds Josh had already unsubscribed from in Lectio over
time, all dumped into Uncategorized with no folder (import never touches
folders at all, a separate gap). Fixed live both times by hand: diff feeds
added at the sync's exact timestamp against Uncategorized, bulk-unsubscribe
via `purge_orphaned_feed`. Second hit: starting the recovery re-sync (to pull
back the stars an accidental mass-F-press wiped, see the entry below) ran the
same `subscriptions` phase again and resurrected the exact same 394 feeds a
second time, since nothing recorded that they'd been deliberately removed.

**Fix direction**: `purge_orphaned_feed` has no audit trail today — a feed
that's gone because it was merged/deduped and a feed that's gone because the
user unsubscribed it look identical afterward. Add a small table (e.g.
`declined_feeds(feed_url, declined_at)`) written only on a genuine
user-initiated unsubscribe (not dedup/merge/format-upgrade paths, which
already pass `archive_pending=False`/`migrate_curation_to`), and have the
`subscriptions` phase skip anything in it instead of blindly re-adding. Same
shape as `dedup_dismissed` for entries — this is the feed-level equivalent.
Any future "Start" on the Ino import (not just this recovery run) will
resurrect the same 394 again until this exists.

### Undo unstar (matching the existing undo-mark-read/unread)

Raised 2026-08-23: Josh hit F (star toggle) repeatedly by accident while
sitting in the Inbox, unstarring ~16 articles with no way to identify which
ones afterward — unlike mark-read/unread, there's no undo token for a star
toggle. `apply_star_state`'s unstar path is a hard `DELETE FROM
saved_entries`, so once it lands there's no trace of which entry it was,
only that *something* changed (couldn't reconstruct after the fact even from
server logs — the access log has no request body, and nothing else records
per-entry star history).

Made worse by two compounding factors this time (worth remembering, not
necessarily fixing): the Inbox's "unstar removes the row immediately" fix
(shipped earlier the same day) means each repeat keypress hits a *different*
entry, not the same one toggling back and forth — `getActivePostItem()`
falls through to whatever's newest at the top once the active one's gone.
And a concurrent Ino trickle-import was inserting new stars the entire time,
so even "what's at the top of the Inbox now" can't stand in for "what was
there right before the incident."

**Fix direction**: give `/entries/saved` unstar the same short-lived undo
token pattern `/entries/mark-range-read` already uses (see
`_run_scheduled_refresh_for_all_users` era mark-read undo, or the
`/entries/undo-mark-read` route) — keep the just-removed
`(feed_url, entry_id, saved_at)` around briefly (a toast with an Undo action,
or a short server-side buffer) rather than committing to a hard delete
immediately.

**2026-08-23, root cause of the "wrong entry" half fixed**: it happened a
second time (single stray `F` with nothing open) and `getActivePostItem()`'s
`|| visiblePosts[0]` fallback was confirmed as the actual culprit — `m`/`f`,`s`/`o`,`b`
were all silently acting on the first visible post whenever nothing was
`.active`. Fallback removed; those shortcuts now no-op with no selection.
Undo-token above is still worth having as defense-in-depth for the case where
the *wrong selected* item gets hit (fat-fingered key, repeat-press on a list
that's reordering under you) — this fix only closes the *no* selection case.

### basslessons.be (FakeFeedz scrape): real bodies, and video/tabs on keep

**BUILT 2026-08-13** as `services/site_content_plugins.py` — a per-site capture
adapter with two hooks (`prefers_full_page`, `extra_embed_html`), documented in
`docs/architecture/saved.md`. Both fire per entry, on Re-fetch and on the
star/tag auto-fetch.

⚠ **One premise below was wrong.** "Ordinary readability/full-page extraction
reaches them" holds only for **full-page**: on the real page readability keeps
**1 of the 6** sheet scans and scores the cookie banner above the rest. Since
readability is the default capture mode, the site has to opt into full-page —
which is what `prefers_full_page` is for. Guarded by a real-page fixture
(`tests/fixtures/basslessons_transcription.html`), because synthetic markup does
not reproduce readability's scoring.

**DONE 2026-08-13**, confirmed by Josh. All 12 unread entries re-fetched
(`scripts/refetch_scope.py --feed … --unread --apply`); each holds the credits,
every sheet scan and the video. Reading the real captures found two things the
spec missed: full-page keeps nodes the page never shows (the consent banner is
`display: none` and led every article), and this site builds all its chrome from
plain divs, so the `<nav>`/`<header>` removal never saw the login strip, pager,
donation pitch or comment form. Hence `strip_selectors`. The ~24 already-read
entries were left alone.

The original investigation, kept because it documents the site:

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

### Re-fetch: let the user choose which date it lands on

Raised 2026-08-23. `refresh_captured_article` bumps a capture's Received date
(and `saved_entries.saved_at`, which the Inbox's star-order reads) to now by
default — right for a deliberate single re-fetch ("something changed, look at
this"), wrong for a bulk Refetch-All across dozens of old articles, which used
to dump the whole Inbox's order onto whatever finished last in the batch.
**Quick fix shipped 2026-08-23**: the batch worker now passes
`bump_received=False` and leaves every entry's date alone; the single-article
button is unchanged.

**Not yet built**: a real choice on the re-fetch action(s) — Now / Original
(saved) date / Pub date — instead of the current hardcoded bump-or-not. The
`bump_received` parameter now threads cleanly through
`services/saved_articles.py::refresh_captured_article` →
`main._refresh_captured_article_for_current_user` → both call sites (single
button, batch worker), so a picker UI has a real parameter to plug into rather
than a special case to unwind.

Related gap surfaced in the same conversation: there is no dedicated
"last fetched/re-fetched at" column — only `entries.published` (the article's
own date), `entries.first_updated` (Lectio's original ingest time, immutable),
and `entry_content_edits.edited_at` (frozen at the *first* re-fetch, for the
Revert button — not updated on later ones). If Refetch-All should ever skip
entries already re-fetched recently (raised in the same thread — no point
re-spending a site's bandwidth on articles just fixed minutes ago), that needs
a new column; nothing today records it.

### Proxy article-body images through /api/img (main app)

Asked for 2026-08-12. **The retry half shipped 2026-08-12**
(`add_img_proxy_fallback`): a body image that fails to load now swaps its `src`
for `/api/img?u=…` and only gives up if that fails too — the same `onerror` the
hero has always carried. That closed the SonarSource case below.

**Preemptive proxying shipped 2026-08-20**, behind a default-OFF per-user
toggle (`proxy_body_images`, Settings → Account → Appearance). When on,
`get_entry_detail` routes every remote `<img src>` in the article pane through
`/api/img` and drops `srcset`, using the same rewrite Read Mode always ran
(shared now as `proxy_all_body_images`, renamed from `proxy_reader_images`);
only the named-host hotlink rewrite is skipped as redundant — the onerror
fallback still runs even when proxying is on, since it's what hides an image
that's dead at the source rather than leaving a broken-image icon (a real bug
in the first cut of this, caught and fixed the same day — see
`docs/architecture/images.md`, "A body image that fails has to be able to try
again"). **Confirmed working by Josh 2026-08-20** against the live library, no
image regressions. Article loads can feel a touch slower on a not-yet-cached
image (one extra server-side fetch to a possibly-distant host before the
browser gets anything), expected and not measured further since it reads as
normal VPS-distance latency, not a regression.

Three things that buys, in order of how much they matter:

- **Content blockers stop breaking articles.** sonarsource.com images are served
  from `assets-eu-01.kc-usercontent.com`; the HTML is correct and the image loads
  fine in a clean browser (verified, 1473x331), but a blocker that filters that
  CDN host leaves the article looking empty with nothing in the logs. Same-origin
  URLs are immune.
- **The image cache starts covering article bodies**, which today it does not.
- **No silent `http://`-on-`https://` upgrade dependency**, which only works
  because browsers quietly fix it and does not work offline.

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

**"Star and re-fetch do not pull content" — RESOLVED 2026-08-23, not a bug.**
Re-fetch on this exact entry now runs clean (`ok`, `extracted`) and correctly
pulls 11.7KB of real page content — it's just that the page *is* a paywall
promo: this specific post is a paid Substack post ("∙ Paid", "Continue
reading this post for free... or purchase a paid subscription"), confirmed
independently by curling the raw page directly. Checked several of her other
recent posts on the same feed — free, full text, no paywall — so this is one
occasional paid post, not a feed-wide change. Nothing to fix; there is no
fuller version of this specific post publicly available to extract. The
avatar/mangled-URL lead-image bug above is unrelated and still open.

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

### Redirecting feeds — no way to find them in bulk

Idea 2026-08-14, **not built.** Josh keeps subscriptions on the URL the
publisher actually serves and checks feeds on sites by hand to spot moves.
Nothing helps him: there is no report of redirecting feeds, and the refresh path
does not record that a fetch *was* redirected — it follows the hop and moves on.
2,281 http(s) feeds to check by hand.

Why it is worth more than tidiness: a feed reached through a 301 costs two
requests per poll forever, and it dies silently the day the publisher retires
the redirect (which they do once a migration finishes). The stored URL also
feeds the Change-URL field, the dupe scan and discovery, so a forwarder makes
all three describe somewhere the posts do not come from.

Shape: mirror `scripts/probe_dead_feeds.py` — HEAD each feed, follow redirects,
report where the final URL differs, `--apply` through `POST /feeds/change-url`,
which already migrates reader, every meta table, the `feed_url_rewrites` host
alias and entry ids on the old host. Read-only and paced by default, like the
dead-feed probe.

Two distinctions the report has to make, or applying it does damage:

- **301 vs 302.** A temporary redirect must not be applied.
- **Moved vs replaced.** A hop that lands on a *different* feed (a site-wide
  firehose, a FeedBurner default) is not the same feed at a new address — the
  same "a discovered feed is not a replacement" trap the 404 sweep hit, where 8
  of 23 candidates were the site firehose standing in for a section feed.

Worked example (lerner.co.il, 2026-08-14): `lerner.co.il/blog/feed/` 301s to
`lernerpython.com/blog/feed/`. Applying it migrated 55 entries and re-homed the
19 whose ids still used the old host. Note it fixed nothing visible — the
symptom that prompted it (old posts arriving daily) was the publisher
re-importing its archive under the new domain, and continued afterwards.

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

**Re-measured 2026-08-13 — the 404 work is done.** 176 failing (was 238): 73
unparseable, 28 conn/DNS, 21 bot challenge, 12 timeout, 11 TLS, 9× 403, 5× 5xx,
**3× 404**. The 4 duplicate dead rows (tartanllama, xubuntu, krshrimali,
markjames) are unsubscribed — one healthy row each now, all fetching. The 46
"no feed found" were removed too.

⚠ Measure against `data/users/<uid>/lectio_meta.sqlite3`, not the root
`data/lectio_meta.sqlite3` — the root one is the DEFAULT user's and reports
nonsense (87 rows, mostly `no such feed`). And `feed_failure_state` holds a row
per feed, so filter `consecutive_failures > 0` or you count all 2596.

**Still open:**

- **73 unparseable** — the biggest bucket and never characterized. Next job here.
- **10 risky replacements** above — each is a judgement call about scope, not a
  mechanical fix.
- **3 remaining 404s** — ocw.mit.edu newcourses-6, blog.hipmunk.com (43 and 23
  consecutive failures), and a bsky.app profile RSS.

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

### Phone polish — shipped 2026-08-11

Pull-down toggles Reader view, Back walks article → feed → folder → drawer, and
the Global Note no longer opens under the drawer. Rationale in
`docs/architecture/views.md`.

Standing decisions, so they are not re-litigated:

- **Back leaving the app is accepted, not fought.** A WebAPK install passes every
  installability check and *still* exits, because Android exits any app at its
  root. Resume-on-open is the answer. **Do not spend more time preventing the
  exit** — if resume misses a case, extend what is saved.
- **The Back guard is best-effort by browser design.** Chrome's history
  intervention skips entries pushed without user activation, and headless does
  not apply it, so no browser test can prove the guard. If it is hit while
  *installed*, the next lead is whether standalone mode changes the intervention
  — do not just add more spares.
- **Read Mode has no resume and no Back guard.** Resume is cheap (same
  localStorage key, different restore target). The guard is not: `/read` has no
  drawer for Back to land on, and a Back that visibly does nothing is worse than
  one that exits — give Read Mode a collapsible tree first.

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

### Signed image URLs rot, and the cache is not catching them

**Measured 2026-08-18.** Of 120,302 stored lead-image URLs, 22,903 are DeviantArt wixmp links signed with a `?token=` JWT, and **only 583 (2.5%) have bytes in the image cache**. Everything else signed is 75 rows. A spot-checked entry: wixmp host, no cached bytes, live fetch HTTP 400. Unrecoverable from the stored URL. This decays continuously — every new DeviantArt entry starts a timer.

`_IMG_CACHE_VOLATILE_PARAMS` strips the token from the *cache key*, which is sound but does far less than its comment claimed: the bytes still have to have been fetched once while the token was valid, and to have survived `last_accessed` eviction since. For a feed not opened within the token's lifetime that race is lost every time. (Comment corrected 2026-08-18.)

**Stop the bleeding: pin thumbnail-sized bytes during the enhance pass**, while the token is fresh. About 25 KB each against the ~121 KB average currently in the cache, and host-agnostic, so it covers any future signing CDN rather than DeviantArt alone. Same pinning mechanism as the per-feed thumbnail (`_feed_thumb_cache_key`, exempt from eviction) — that one is keyed per feed; this wants keying per entry. Does nothing for what has already expired.

**Re-signing already exists on the article path** — `_resign_expired_deviantart_url` re-signs a dead wixmp token when an entry is opened, and skips the API call when the image proxy already holds the bytes. So the gap is narrower than the raw 2.5% suggests: opening a DeviantArt post repairs it. What is NOT covered is the LIST thumbnail, which reads the stored URL out of `entry_lead_images` and never goes through that path, so a feed's thumbnails stay broken until each entry is opened one at a time. Either re-sign on the list path too, or make pinning moot by storing the bytes. Every one of the 22,884 DA entries stores the deviation UUID as its entry id, so a paced backfill is possible if it turns out to be wanted — but fix the list path before building one.

⚠ **The article-path re-sign had its own bug, found and fixed 2026-08-20.** It only re-signed a token whose JWT carried a readable `exp` claim in the past; a token with no `exp` claim at all was treated as permanent and never checked. That assumption is false — a spot-checked live entry (a GIF) had no `exp` and was already dead (wixmp: `400 image is invalid`), and a feed-wide check found **22,597 of 22,884 stored wixmp tokens carry no `exp` claim**, all previously trusted blind. Fixed by adding `_wixmp_url_is_live`: when `exp` is unreadable, one direct HEAD at the image host (not the DeviantArt API — no rate-limit cost) decides whether to trust the URL or fall through to a real re-sign. Still article-path only — the list-thumbnail gap above is unchanged.

Build the pinning first: self-contained, no API budget, no pacing to get right, and it turns the backfill into a one-time cleanup instead of a permanent crutch.

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

### Utilities: find rules that could be one rule

Marking things read in Deals now takes several rules (Apple products, one set of
stores, …) because a keyword was one term until comma lists landed. Nothing
surfaces that they are mergeable, so they accumulate.

Measured 2026-08-19 on the live library: **5 rules could collapse** — 3 global
`highlight` + 3 `highlight` on folder 9 + 2 `mark_as_read` on folder 8, each
group already sharing type, scope, `search_in` and the regex flag. That last
pair is regex, so merging means `(a)|(b)`, not a comma join.

Rules for merging: same type + same scope + same `search_in` + same regex flag →
offer to join the keywords (comma list when plain, alternation when regex). A
folder rule and a feed rule are **not** merged — different scopes are the point.
But a feed rule whose scope sits inside a folder that already has a same-type
rule whose keyword covers it is **redundant** and should be flagged for removal.
(Zero of those today, so it is the secondary case.)

Josh: "maybe they should?" — wants to discuss whether redundant-feed-rule
removal is automatic or a suggestion. Suggestion-with-preview is the safer
default, and matches how the dupe scans already behave (nothing pre-checked).

### Rule editing has no atomic endpoint

Editing a rule is a client-side remove-then-add against `/highlights/remove` and
`/highlights/add`, because there is no update route. Sending both at once
destroyed the rule whenever the identity `(scope, scope_id, keyword)` had not
changed — the add landed first and the remove deleted it, 20 times out of 20 in
a local reproduction, with both responses OK so the UI reported success. Josh
lost a Deals dedup rule to it on 2026-08-20 (a dedup rule hits this on every
edit, since its match method IS the keyword).

Fixed on the client 2026-08-20: skip the remove when the identity is unchanged
(`INSERT OR REPLACE` overwrites in place), and await it before the add when it
did change. That closes the hole, but the real shape is a single `POST
/highlights/edit` doing both in one transaction — worth building the next time
this area is open, since any future caller can re-introduce the same race.

### Saved: see and sort by item size, to clear the big ones first

Saved articles carry captured content (and, for archived ones, offline copies in
the starred-archive DB), so a handful of heavyweight captures can dominate the
store while hundreds of small ones are irrelevant. Nothing surfaces per-item
size, so pruning is guesswork.

Wanted: a size column in the Saved view, sortable descending, so the worst
offenders are the first thing on screen. Size means the stored body plus its
archived assets, not the source page's weight — the two diverge sharply for a
full-page capture with images.

Open questions before building: whether the number is computed live (a LENGTH()
over content plus a join to the archive's asset rows, fine for a few thousand
items, less so as a sort key on every render) or maintained as a column at
capture/re-fetch time; and whether the same sort belongs in the Kept view, where
an unsubscribed feed's retained posts accumulate unseen.

### GIL-contention request stalls — tally

Not fixed, not investigated further yet — just tracking how often it's bad
enough to notice before deciding whether it's worth the architectural work
(background refresh and request handling currently share the same
process/threads, so a request can sit for seconds with nothing itself wrong,
starved of CPU by a concurrent background refresh doing CPU-bound work —
parsing, sanitizing). Add a line each time Josh notices one; look for a
pattern (time of day, request type, cadence) once there are enough to see one.

| Date | Request | Wall time | Notes |
|---|---|---|---|
| 2026-08-23 | `GET /?folder_id=23&sort_dir=desc&star_only=1` (5 items) | 6919ms | 6.3s gap between two already-fast, already-logged steps — nothing itself slow |
| 2026-08-23 | `GET /?folder_id=1&star_only=1&kept=starred&sort_by=starred&sort_dir=desc` (F5 on Saved) | 18664ms | Landed mid-scheduled-refresh — dozens of concurrent `httpx` feed fetches logged in the same window |
| 2026-08-23 | 4 back-to-back `GET /?folder_id=1&star_only=1&kept=starred` (clicked Saved) | 2114/7882/8684/18192/9303ms | Cluster, not a one-off — same gap signature (list_entries logs fast, posts_block/meta_block absorb the delay) ~5-7 min after a container restart; may correlate with post-restart cold caches/backfill rather than being independent of it |

### CodeQL board — watch-note

Board is at zero open alerts as of 2026-08-13 (PR #200 cleared a `py/redos` in
the lead-image opener and a substring assertion in a test; alert 191,
`py/url-redirection`, was dismissed as the same false positive as 145/148/177-179.
Before that, PR #190 closed 4× `py/polynomial-redos` + 1× `py/stack-trace-exposure`).

⚠ **A negative lookahead will not clear a redos alert.** CodeQL's regex model
ignores lookaheads, so `(?:-(?!->)[^-]*)*` — measurably linear — was re-flagged
as ambiguous on the first push of #200. Either write the loop lookahead-free or,
as that PR did, move the scan out of the regex into Python.

**Committed page fixtures are excluded** (`paths-ignore: tests/fixtures`, added
2026-08-13 for alert 198). A captured page is byte-for-byte what a site served,
so analyzing it reports the remote site's choices as ours — jQuery from a CDN
with no SRI, in that case.

**If the reflective-XSS class keeps recurring**, the repo already has the pattern
for it: `.github/codeql/queries/` holds guard-aware copies of the SSRF and
path-injection queries that model our audited guards as sanitizer barriers, with
the stock versions excluded in `codeql-config.yml`. A `LectioReflectiveXss.ql`
modeling `html_sanitize.sanitize_html` / `sanitize_inline_title` as barriers would
end the hand-dismissals. Not built yet — two dismissals is not yet a pattern, and
excluding stock `py/reflective-xss` repo-wide is a heavier trade than excluding
`py/full-ssrf` was.

## Later

*Moved down from Now on 2026-08-13: real, but not what is next.*

### Single-user mode does not exist anymore — retire DEFAULT_USER

Multi-user is simply how Lectio works now; making one account is the "single user" case. But `DEFAULT_USER_ID = "default"` survives as the default value of the `lectio_current_user` ContextVar (`services/tenancy.py:53`), so any code path that never binds a user silently resolves to the legacy top-level DBs at `/data/lectio_meta.sqlite3` and `/data/lectio_reader.sqlite` instead of failing. Those files are stale — the legacy reader DB was last written 2026-07-24 and is 73 KB against a 685 MB per-user one — so the failure mode is not an error, it is quietly correct-looking answers computed from the wrong database. It has already produced nonsense failing-feed counts during debugging, and it is the same trap as a background thread losing its tenancy binding.

The change: default the ContextVar to `None` and make resolution raise when unbound, so every background thread, CLI script and push handler must bind a user explicitly and a missed binding fails loudly at the first read. Then delete the legacy path branches in `tenancy.py` and the stale DB files, and drop `DEFAULT_USER_ID` from `_RESERVED_USERNAMES`.

Not small: 54 references outside `tenancy.py` and `tests/`. Wants its own PR, and wants the per-user startup migration checked, since anything still reading the legacy paths will surface the moment they stop resolving. Related: the bg-thread tenancy rule already in place (`_run_in_user_context`).

### Backfill older posts from a URL pattern

Idea 2026-08-13, **not scoped.** A feed shows the publisher's recent window; the
back catalogue is usually still on the site behind predictable URLs (paginated
archives, or per-item ids the feed already exposes). Where the pattern is
derivable, Lectio could walk backwards and import what the feed no longer lists,
instead of the library starting the day you subscribed.

The fetching is the easy half. These are the decisions to make first:

- **Dates.** A synthesized entry with no real published date lands at "now" and
  floods the top of the Inbox — the exact corruption `restore_bumped_publish_dates`
  had to repair. Mine the date from the page, and if there is none, the entry is
  not importable rather than importable-with-today.
- **Identity, before the first fetch.** Backfill must dedupe against what is
  already there *and* against what was deliberately deleted, or an import
  resurrects everything the user threw away. `dedup_dismissed` and the
  retention sweep both have a claim here.
- **Where it stops.** Walking until 404 is how one subscription becomes 4,000
  entries. Needs a bound the user sets (N pages, or back to a date) and a dry
  run that reports the count before writing.
- **Rate.** This is the largest burst of outbound requests the app could make.
  It must go through `refetch_batch.run_paced`, not a new loop
  ([[good-web-citizen]] applies at import too).

Fits the existing adapter shape: a per-feed pattern (stored, not hardcoded —
see `image_size_rule` for the precedent) plus a paced walker. Worth a real plan
before any code.

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

### Combine cross-feed duplicates instead of marking one read

Dedup's only action is "mark the newer copy read". That is destructive, which is why the Safe tier insists on body corroboration, which is why it finds nothing in the folders where duplicates actually pile up. Combining removes the reason for the strictness: a false positive costs an extra link on an entry and a click to split, instead of silently hiding something you wanted.

**Measured 2026-08-18** with `tmp/dedup_experiment.py` (repointed at the per-user DB — it had been surveying the stale legacy one). Library-wide: 101 safe, 13 needs-review. Tech News: **0 safe, 5 review**, every candidate at `body_j = 0.00`, because the folder pairs aggregators against sources and an HN body is `article url: … comments url: … points: 23`. Deals: **zero candidate pairs even across 60k entries** — Reddit deals posts have distinct slugs and human-written titles, and fuzzy cannot rescue it because `cand_pairs` is seeded only from feeds that already share an exact slug or title (`main.py` `_safe_dedup_find_pairs`), so in a folder with no exact match the fuzzy tier never runs at all.

**The behavior.** A duplicate group renders as one entry. The primary is the member with the richest body — not the oldest, which is today's rule and which would keep HN's stub over the real article. The other members appear **in the entry body only, not in the list**: the list shows one ordinary item. Body gets an "Also at" line — `Also at: OSnews` / `Discussion: Hacker News (23 points, 10 comments)`. One unread item; marking it read marks the group; splitting restores the members.

HN's stub body stops being the problem and becomes the feature: `points:` / `# comments:` and the comments URL parse into a real discussion affordance. Josh subscribes to HN for the comments, so an HN link must never be the copy that disappears — combining satisfies that without a per-feed "discussion feed" flag, which was the alternative design and is not needed if nothing is destroyed.

**Matching.** Two tiers, split by what the action costs. Combining accepts the current safe combos plus `{slug,title}` and exact cross-feed title; anything that marks read keeps today's strict rule. Slug alone stays out of both — there is a real false positive in the survey (two different Microsoft stories sharing a slug, `title_j = 0.09`, four days apart).

**Storage.** New meta table for the groups (group id, feed_url, entry_id, role primary/alt). `dedup_false_matches` already records "these two are not the same" and should feed the splitter. Needs the per-user startup migration or existing tenants 500.

**Open.** Whether combining runs as an automation rule, a scan you invoke, or at ingest. Unread counts and the offline outbox both need to agree that a group is one item.

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
- ~~**Sub-categories from the URL path**~~ — SHIPPED 2026-08-14 as
  `tags_from_url_path`. Drops the last segment (the slug), numeric segments
  (so a dated permalink is not filed under "2026") and structure words. It
  needs no page fetch, which turned out to matter more than expected: gottadeal
  and realpython 403 even a browser identity, so this is the only tier that
  works there at all.

~~Real Python's skill-level tags~~ — **dropped 2026-08-14 at Josh's call**:
"don't care about the skill levels, they can stay as tags". More tags are
cheaper than missing ones, since an unwanted chip is one dismissal and a tag
never captured is invisible.

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


### Inoreader replacement — the migration

Started early (2026-08-21/22), ahead of the original "start ~Dec 2026"
schedule. Folder-by-folder audit done for all 27 folders (method: health
check + silent-stall check + live Ino comparison via `services/inoreader.py`,
reusing `get_subscriptions`/`get_stream_contents` — this superseded the
originally-planned automated comparison report, same result by hand). 26/27
folders are safe to cut over; YouTube is the one open blocker (silent
multi-week stalls, root cause not yet found).

**No fetch-proxy.** Considered pulling feeds Inoreader can reach but Lectio
can't (bot-walled: isocpp, libhunt newsletters, Project Euler, etc. — about a
dozen feeds) via Ino's API instead of the origin. Rejected 2026-08-22: it only
works on a paid Ino account, which defeats the point of dropping Ino. These
feeds are accepted as permanent losses — same call for the 2 Cloudflare-walled
Deals feeds (camelcamelcamel, homebrewfinds). Nothing further planned here;
let the Ino plan lapse 2027-03-16 (annual SaaS rarely prorates; worth asking,
but plan to ride it out).

Remaining before Ino can fully lapse: Comics & Art and !NSFW dead-feed
pruning (mechanical), and the YouTube root-cause dig.

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Doesn't
help the bot-walled feeds above (they're blocked at fetch, before content
matters) but could recover feeds elsewhere that are body-less rather than
blocked.

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

22 legacy `backend.deviantart.com` subscriptions unsubscribed and 521 per-artist
gallery feeds combined into the Watch feed, 0 failures. Library 2,868 → 2,325
feeds; survivor holds 21,857 entries across 493 artists.

⚠ **Recorded because the reasoning that nearly blocked it was wrong.** "401
entries covering 34 of 543 artists" reads like a coverage cap. It is not — only
~23-34 of the watched artists post at all. What settled it: zero artists who
posted since the Watch feed was created were missing from it, and its intake rate
matches the observed posting rate. **Check whether a number is a limit or just
the size of the active set before concluding anything from it.**

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
2. **Tags from bot-walled pages.** ArtStation and one art site both show
   per-post tags on the page and ship **none** in the feed (0 `<category>` in
   either). The server cannot reach the pages: ArtStation 403s behind
   Cloudflare, the other 401s behind a JS proof-of-work ("Making sure you're
   not a bot!"). Both survive a browser-identity retry, because both are JS
   challenges — see `services/bot_challenge.py`, which already says detecting
   one is not a prelude to working around it. **The extractor is done**:
   `extract_page_tags` handles ArtStation (link-text tier, added 2026-08-14)
   and the other (`rel="tag"`, already worked). Only delivery is missing — POST the
   open page's DOM to a route and store the result in `entry_feed_tags`. 86
   ArtStation feeds have never stored a tag. Trigger on KEEP, not per entry: a
   page fetch per post is the traffic the good-citizen rule guards.
   A bookmarklet does the same job for ~30 lines; the extension only wins if
   you want it to happen without clicking.

   ⚠ **Cookie harvesting was considered and rejected 2026-08-14.** Cloudflare's
   `cf_clearance` is bound to IP **and** UA, so a cookie from Josh's browser is
   rejected at the VPS outright — ArtStation, the bigger prize, cannot work this
   way at all. The other site's token is `HttpOnly` (a bookmarklet cannot read it) and
   expires in hours, making it "re-paste most days, per site". And anything able
   to harvest the cookie can already send the tags directly: the cookie route is
   a harder version of the same mechanism that succeeds in fewer cases.
3. **Dual-extension use**: the stock extension has a single Backend setting —
   a fork lets one browser run save-to-Readit and save-to-Lectio side by
   side.
4. Nice-to-haves once forked: badge feedback distinguishing saved vs
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

- **Performance investigation** — a systematic per-request baseline (DB time,
  enrich time, refresh contention) under realistic load.
- **Shared-content tenancy mode** — one global feed/entry store plus per-user
  overlays (read/star/folders/subs). Only worth building at real scale, but it is
  the biggest caching/refresh win: one refresh per feed regardless of subscriber
  count, deduped storage, and unread counts moved to an incrementally-maintained
  per-user table instead of live scans. reader 3.24 documents the canonical
  layout — `shared.sqlite` for content, per-user DBs for personal state, a routing
  layer merging at query time, with `update_feeds_iter()` fanning out per-feed
  results. Lectio currently fetches each feed once per user; that is fine for 1–3
  trusted users and is the natural limit before this becomes worth building.
- **Per-user resource fairness** — rate limits on refresh, scraping and thumb
  generation. Not needed for trusted users; hooks are in the seam.
- **Write-abuse protection (read-state spam).** Every read toggle writes the
  reader DB and `entry_read_state` and bumps `_unread_counts_generation`, which
  invalidates the counts cache and forces a recompute — so flip-flopping hammers
  the shared SQLite. Defenses cheapest → strongest: coalesce rapid toggles on one
  entry (last-write-wins); throttle the counts recompute per user; and the actual
  blocker, a per-user token bucket on the state-changing endpoints returning 429
  with a cooldown. ⚠ **Tune so legitimate heavy use never trips it** — fast
  keyboard triage marking dozens of items is normal; only sustained pathological
  flip-flopping should hit the limit.
