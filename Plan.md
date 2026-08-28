# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

### Phone unsub dialog behind the folder drawer / email full-text / footer link — DONE 2026-08-28

Three small fixes shipped together:

- **Unsubscribe confirmation painted behind the phone folder drawer.**
  `#unsub-migrate-modal` was `z-index: 90` (`static/style.css`) but the phone
  folder drawer (`.pane-folders` in single-pane layout) is `z-index: 300` —
  same bug class the `.context-menu` comment already documented (that one was
  bumped to 340 when a long-press context menu had the identical problem;
  this modal never got the same treatment). Bumped to `310`.
- **Email Article can now send the full article instead of a snippet.** New
  unchecked-by-default "Include full article text instead of a snippet"
  checkbox on the Email Article dialog (`templates/_action_modals.html`,
  `#email-article-full-text`, `name="full_text"`), reset on every modal open
  (it's a reused DOM node, same reason `cc_me` was already reset — an earlier
  version of this fix skipped that and the box stayed checked across opens).
  When checked, the `/entries/email` route (`main.py`) prefers `entry.content`
  over `entry.summary`. First cut sent a stripped-plain-text version of the
  whole article and just looked like a wall of unformatted text — fixed by
  reusing `_sanitize_html_allowlist` (the same allowlist chokepoint the entry
  pane itself renders with) to build a real HTML body (`excerpt_html` in
  `services/email.send_article_email`/`_build_html`), so the email actually
  gets paragraphs, bold/italic, links and lists. `html_sanitize.plain_text_full`
  (no truncation, real block-level boundaries become paragraph breaks via a
  sentinel so pretty-printed feed HTML's incidental newlines don't also
  become breaks) is kept as the plain-text part's fallback, for mail clients
  that don't render HTML.
- **"Shared via Lectio" footer linked to the wrong repo.** `services/email.py`
  hardcoded `https://github.com/lectio/lectio`; fixed to
  `https://github.com/joshg253/Lectio` ([[github-repo]]), matching the URL
  used everywhere else (e.g. the honest User-Agent strings). The digest email
  footer has no link at all, so it was unaffected.
- **Email body was too narrow and clipped wide images instead of shrinking
  them.** `.wrapper` clips overflow rather than scrolling it, so a full-text
  article image (feed HTML usually ships explicit `width`/`height`
  attributes) had its right edge cut off instead of wrapping. Bumped
  `.wrapper` from 600px to 680px and added `.excerpt img, .excerpt iframe {
  max-width: 100%; height: auto; }` in both `_build_html` and
  `_build_digest_html`.

Tests added: `tests/unit/test_html_sanitize.py` (`plain_text_full`),
`tests/services/test_email_service.py` (multi-paragraph `_build_html`, the
image-shrink rule, wrapper width), `tests/integration/test_email_route.py`
(`full_text` on/off, and the no-content fallback to summary).

**Follow-up idea, not built:** "Include full article text" only pulls what's
already stored (`entry.content`/`entry.summary`) — for a feed that ships a
thin stub body, that's still a thin email even with the checkbox on, while
Readability Mode (the existing extraction used for Save/re-fetch) can pull
the real article from the same feeds. Worth wiring the checkbox to run that
extraction live when the stored body is thin, rather than only reformatting
whatever's already there. Not scoped — needs a real example of a thin-stub
feed to test against first.

### Bulk "Add tag…" had no typeahead — DONE 2026-08-28

`openBulkTagModal`'s `#bulk-tag-input` never called `attachTagAutocomplete`,
the shared widget the per-entry tag field already used — so multi-select →
right-click → Add tag offered no suggestions while typing. One-line fix:
wired the same widget in, matching the per-entry tagging grammar (space-
separated, no auto-apply-on-choose since several tags can be typed before
clicking Add tag). Confirmed working live 2026-08-28.

### Feed Properties "Suggested tags" was a raw space-joined text field — DONE 2026-08-28

Converted to chips, matching the entry pane's tag display everywhere else:
current tags render as removable chips (`.entry-tag-chip`/`.entry-tag-remove`,
same look as a post's manual tags), and the text input is now scratch space
for typing new ones to add rather than an editable copy of the whole
space-joined list. Clicking a chip's × immediately saves the reduced set
(`saveFeedPropSuggestedTags`); typing + Add/Enter merges new tokens into the
existing set and saves. Backend (`POST /feeds/suggested-tags`, full-replace
semantics) is unchanged — this was purely the editing UI. × is always visible
here (not hover-revealed like the entry pane) since Settings is a
deliberate-click surface, not the dense per-post row. Picking a typeahead
suggestion saves immediately (`applyOnChoose: submitFeedPropSuggestedTags`,
same "picking IS the decision" reasoning as the entry-pane tag field) — Add
is now only needed for a hand-typed tag that didn't come from a suggestion.

**Also found and fixed, pre-existing (not caused by the chip change):** the
`attachTagAutocomplete` call here read `window.lectioTagNames`, but
`lectioTagNames` is a top-level `let` — unlike `var`, that does *not* become a
`window` property in a classic script, so the lookup was always `undefined`
and every suggestion list was silently empty. Chips rendered fine (unrelated
code path) but no typeahead ever showed. Fixed to reference the binding
directly, matching the other two `attachTagAutocomplete` callers
(entry-pane tags, bulk "Add tag…").

Roughly ordered: recurring/active pain first, then concrete bugs, then
items already scoped and decided so they're ready whenever picked up, then
measurement/investigation jobs, then low-urgency work and the two standing
watch-lists last. Re-prioritized 2026-08-24: items shipped since the last
pass moved to git history (rationale stays in ARCHITECTURE.md where it's
not already there); items with no trigger condition met yet moved to Later.

### YouTube's RSS feed endpoint is currently 404ing this server's IP — 689 of 705 feeds broken

Found 2026-08-24 while characterizing the "73 unparseable" failing-feeds
bucket below — this is a bigger, more urgent finding than what that job was
looking for, so it goes first. **Not a code bug and not fixed** — this needs
Josh's read on whether/how to act, not a live change made while he's asleep.

**What's happening, confirmed live:** `feed_failure_state` shows 834 failing
feeds against the reader's 2,279 subscriptions (`consecutive_failures > 0`,
checked against currently-subscribed feed_urls to exclude stale rows — only
6 of 834 are stale). 693 of those 834 are youtube.com feeds, and 689 of
those 693 currently show `last_error` = "HTTP 404" — **97.7% of all 705
subscribed YouTube feeds.** All 689 share the *exact same* `last_failure_at`
in one of two clusters (23:19:08 and 23:50:08 on 2026-08-24), meaning one
refresh pass, not independent per-channel decay. `last_success_at` on a
sample was 18:23–19:17 the same day — these were fetching fine a few hours
earlier.

**Confirmed it is not real channel deletions.** Curled
`youtube.com/feeds/videos.xml?channel_id=UC-lHJZR3Gqxm24_Vd_AJ5Yw` (PewDiePie
— about as alive as a channel gets) directly from the container: **404**,
Google's own error page. `youtube.com/` itself and `google.com` both load
fine (200) from the same container at the same time, and a non-YouTube feed
(hnrss.org) fetches normally — so this is not a general network problem, and
not a real "resource not found." It reads as YouTube's feed endpoint
specifically blocking or rate-limiting this server's outbound IP and
answering with 404 instead of 429, which is unusual but not unheard of for
anti-scraping.

**⚠ Correction made while writing this up: the obvious "no fix ever shipped"
theory is wrong.** `services/feed_refresh.py` already has exactly the fix
this symptom calls for — high-fanout exemption (hosts with ≥8 feeds in a
batch, i.e. youtube.com, are exempt from domain-level backoff) plus pacing
(`_HIGH_FANOUT_PACE_SECONDS = 0.7`, ~8 minutes for a full ~700-feed YouTube
pass), with a comment that already names this *exact* symptom: "YouTube RSS
returns spurious 404s to a ~700-feed burst even though each feed is fine
one-at-a-time." This shipped 2026-07-12/13. So the question isn't "why was
this never fixed" — it's "why did a real fix apparently fail to prevent a
real recurrence."

**Most likely explanation, not confirmed:** tonight's session did seven
other rebuild-and-redeploy cycles over roughly 90 minutes, each one
restarting the container (and with it, the scheduled-refresh background
thread) outright. The "this folder is due" timestamp is written to the
per-user meta DB *before* the paced fetch loop runs, not after — so a
restart mid-pass doesn't cause the same folder to be immediately re-selected
as due, but it does mean a courteously-paced fetch loop can be cut off
mid-flight, mid-connection, with no graceful close. If the YouTube folder's
own 30-minute cadence happened to come due more than once inside that
90-minute window (plausible — that's three cadence cycles), each attempt
could have been interrupted the same way, several times, in a short span.
Whether *that* pattern (repeated aborted bursts, not one clean burst) is
what actually trips YouTube's rate limiting is a real unknown — flagged as
the leading theory, not a conclusion. Restarting this often for routine
deploys is itself unusual; a normal work session doesn't rebuild seven times
in 90 minutes.

**Deliberately not touched tonight:** no change to `feed_refresh.py`'s
backoff/pacing logic (the existing fix is doing what it was designed to do;
guessing at a further change without being able to reproduce the failure
risks solving the wrong problem), and no bulk action on the 689 individual
feeds (disabling or pausing several hundred subscriptions is exactly the
kind of bulk, live-data action that needs a go-ahead first, and might not
even be the right move if this clears on its own).

**Open questions for next time this is picked up:**
- Does it self-clear, and on what timescale — check `consecutive_failures`/
  `last_error` on a sample of these feeds again later; if `last_success_at`
  has moved forward, it cleared on its own.
- If it recurs on a session with zero restarts, the "repeated interrupted
  bursts" theory above is wrong and something else is going on — worth
  checking `domain_failure_state` and a fresh `last_failure_at` cluster
  the same way this was found.
- If the restart theory holds up, the actionable lesson is process
  discipline (batch deploys, don't rebuild mid-session repeatedly) rather
  than a code change — but confirm before treating that as the fix.

**Josh's read (2026-08-25 morning): not pursuing further.** Matches what he
already sees day-to-day — individual YouTube channels 404 intermittently even
opened directly in a browser, temporarily. His guess is the diagnostic
probing itself (repeated live curls against the RSS endpoint during this
investigation) contributed to the block, on top of that normal flakiness. His
overall take: YouTube-in-Lectio was already working well enough before this,
and he's mid-migration off Inoreader (unrelated feeds — currently working
through a backlog of saved-photos wallpapers, not a YouTube-specific step) so
this isn't blocking anything. No code change requested; closing the
investigation here rather than probing YouTube further.

### Refetch-All has no "already re-fetched recently" skip

Surfaced 2026-08-23 alongside the re-fetch date picker (built 2026-08-24 —
see `docs/architecture/saved.md` "The re-fetch date picker"). There is no
dedicated "last fetched/re-fetched at" column — only `entries.published`
(the article's own date), `entries.first_updated` (Lectio's original ingest
time, now *not* always immutable: the date picker's "Now"/"Pub date" choices
deliberately move it), and `entry_content_edits.edited_at` (frozen at the
*first* re-fetch, for the Revert button — not updated on later ones). If
Refetch-All should ever skip entries already re-fetched recently — no point
re-spending a site's bandwidth on articles just fixed minutes ago — that
needs a new column; nothing today records it. Not scoped further; raised but
not asked for yet.

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

**Re-measured 2026-08-24 — the "unparseable" job, done, but overtaken by a
bigger finding.** 834 failing feeds now (was 176 on 2026-08-13), almost
entirely explained by one thing: **689 of those are the YouTube mass-404**
documented in its own item at the top of this file ("YouTube's RSS feed
endpoint is currently 404ing…") — not a data-quality problem, a live
incident. Excluding that cluster, the remaining 145 are stable and close to
the 2026-08-13 numbers, now with a clean mutually-exclusive breakdown
(verified: sums exactly to 145):

| failure (YouTube-404 excluded) | count |
|---|---|
| genuinely unparseable ("could not be parsed as a valid RSS/Atom document") | 50 |
| DNS lookup failed | 21 |
| bot challenge | 17 |
| conn/other (non-DNS, non-timeout) | 16 |
| 403 | 9 |
| timeout | 9 |
| 5xx | 7 |
| unknown feed type | 4 |
| non-YouTube HTTP 404 | 4 |
| 401 | 2 |
| 429 | 2 |
| redirect loop | 1 |
| feedparser crash | 1 |
| other (below) | 2 |

**The "50 genuinely unparseable" bucket is per-site judgement calls** (a
publisher changed CMS and broke their own feed XML, moved to a platform that
serves HTML at the old feed URL, etc.) — the same shape as the 404 work
already done above, not mechanical. Not triaged individual-by-individual
this pass; worth the same `scripts/probe_dead_feeds.py`-style treatment if
picked up, watching for the same "a discovered feed is not a replacement"
trap. The 401/429 pairs and the 403/5xx buckets are likewise untriaged —
some may be soft-blocked bot detection rather than genuinely dead, same
caveat as the bot-challenge bucket.

**One-offs worth a look, all small and mechanical:**
- **`grcnews@ino.to`** is a malformed `feed_url` — not a URL at all, just an
  Inoreader-internal-looking identifier — subscribed 2026-08-24 21:02,
  during that day's Ino recovery re-sync. One bad row from
  `_inoreader_drip_step`'s subscriptions phase; `sub.get("feed_url", "")`
  apparently returned something non-URL-shaped for this one subscription
  and nothing rejected it. Fix direction: validate the shape (has a
  scheme) before `reader.add_feed`, or at minimum before writing to
  `declined_feeds`/subscribing. Single row, low urgency, but a real gap —
  worth revisiting with the Ino subscriptions-phase code already open.
- **4× `unknown feed type`** at codeproject.com (`WebServicesRSS.aspx?cat=2`
  and `cat=3`) and one each at retropie.org.uk and blog.lastpass.com —
  feedparser can't identify the format at all, distinct from "malformed
  XML." Worth a raw curl each to see what's actually being served before
  assuming they're dead.
- **1 real feedparser crash**, not a remote issue: `feeds.feedburner.com/
  LinuxMintGuide` throws `AttributeError: object has no attribute 'version'`
  inside feedparser itself while parsing. Reproducible, worth a minimal
  repro + upstream/local workaround if this feed matters.
- **1 redirect loop** (themadfermentationist.com, >30 redirects), and the
  2nd "other" row: **`steviesnacks.com`**, a feed with an entry missing both
  id and a usable link fallback.

**Still open from 2026-08-13, unchanged:**
- **10 risky replacements** — each a judgement call about scope, not a
  mechanical fix (see "widening"/"collision" above).
- **3 remaining pre-existing 404s** — ocw.mit.edu newcourses-6,
  blog.hipmunk.com, a bsky.app profile RSS. Now a rounding error next to
  the YouTube cluster, but still real and still open.

### Feed known-migrations into discovery, so a 404 is not the end

**FeedBurner piece shipped 2026-08-25** — see `docs/architecture/feeds.md`
"Suggesting a replacement for a feed on a known dead-end host". Live-checked
first: FeedBurner turned out not to redirect at all (the original 2026-08-12
assumption) — it serves the origin site's own homepage HTML back at the dead
feed URL, wrong content, no redirect, and the page's own `rel="alternate"`
just points back at itself. `suggest_feed_migration` reads `rel="canonical"`
off that page instead to recover the real origin, then runs the existing
`probe_url` discovery there. Suggestion-only, same "never automatic" call as
the mergeable-rules feature: a **"Suggest fix" button on FeedBurner rows** in
the Failing Feeds panel pre-fills the existing (already-verified) Change URL
field — a human still clicks Save.

**technet/powershell.com were investigated and deliberately not built.** Of
the three host-level migrations flagged in the 2026-08-12 sweep, only
FeedBurner still has live failures (12 feeds, 2026-08-25) — zero subscribed
feeds are currently failing on `blogs.technet.com` or
`powershell.com/cs/blogs/*`. Guessing at a path-mapping with no live example to
verify it against risks exactly the "a discovered feed is not a replacement"
trap this feature exists to avoid; add a resolver for them if/when a real
404'd example reappears. Even within FeedBurner, roughly a quarter of the
current failures have no `rel="canonical"` at all (parked domain, JS-rendered
SPA) and still need the manual "risky replacement" judgment call.

The older `feed_url_rewrites` table / `_SITE_FEED_REWRITES` machinery this
item originally pointed at turned out to be a different mechanism entirely
(entry link/id host rewriting for an author's *existing, still-alive* feed,
not resubscribing a dead feed to a new one) — not reused here.

### Redirecting feeds — no way to find them in bulk

**Scanner built and run 2026-08-25** — `scripts/find_redirecting_feeds.py`,
mirroring `scripts/probe_dead_feeds.py`'s shape (read-only by default, paced,
honest UA). Swept all 2,264 currently-subscribed http(s) feeds:

| verdict | count |
|---|---|
| direct (no redirect) | 1,938 |
| **candidate** (301 all the way, verified same feed) | **128** |
| temporary (a 302+ hop somewhere in the chain) | 26 |
| redirects elsewhere (rejected — see below) | 17 |
| redirects to dead/non-feed | 5 |
| errors (unreachable, mostly Tumblr) | 150 |

**Not applied.** 128 verified candidates is a real batch, worth Josh's own
`--apply` run rather than something to fire unilaterally — `--apply` reuses
`change_feed_url_route` (force=0) so its own independent verification and full
meta-table migration still run per feed; this script only picks *which* URLs
are worth offering it. Raw results: `/data/redirecting_feeds_20260825.json`
(inside the container).

**The "moved vs replaced" guard worked in practice**, not just in theory: of
the 17 "redirects elsewhere" rejections, at least one (`rowenathebarbarian.com`)
would have been a bad swap if applied blindly — the redirect target parses as
a feed but has zero entries, caught by `_looks_like_same_feed`'s host-match
check before ever reaching "candidate."

Why it is worth more than tidiness: a feed reached through a 301 costs two
requests per poll forever, and it dies silently the day the publisher retires
the redirect (which they do once a migration finishes). The stored URL also
feeds the Change-URL field, the dupe scan and discovery, so a forwarder makes
all three describe somewhere the posts do not come from.

Worked example (lerner.co.il, 2026-08-14): `lerner.co.il/blog/feed/` 301s to
`lernerpython.com/blog/feed/`. Applying it migrated 55 entries and re-homed the
19 whose ids still used the old host. Note it fixed nothing visible — the
symptom that prompted it (old posts arriving daily) was the publisher
re-importing its archive under the new domain, and continued afterwards.

### Finish the Instapaper clone (Read Mode follow-ups) — DONE 2026-08-25

The read-later app (Save any article, Saved sidebar view, Read Mode at
`GET /read`) and its deferred finishing touches (archived-aware counts,
mark-read-after-last-page, image prefetch, dates/sort/Archive-button
readability, Delete/Archive working on tag-kept items, and the Archive vs.
Delete model — Archive keeps tags/offline capture, Delete releases both) all
shipped 2026-07-28/29. Full rationale in ARCHITECTURE.md. The one piece from
that work that wasn't yet safe to use:

**Settings → Feeds → Utilities → Archive old stars — fixed and safe to use
2026-08-25.** Was blocked: the cutoff sorted on `saved_at`, but `saved_at` is
not a real star date for most rows — 6,091 of 10,002 stars carry a `saved_at`
in 2026-06, when the multi-user migration stamped its own run date instead of
preserving the original. Fix shipped as a **date basis** choice in the
Utilities panel: "Publish date" (now the default — asks the better question
anyway, "articles from 2019 I have still never opened") or "Star date" (kept
as an option, its unreliability caveat only shown when picked). See
`docs/architecture/saved.md` "Archive old stars ("Inbox bankruptcy") — the
saved_at trap" for the full mechanism.

### Read Mode: resume + Back guard

Carried over from the 2026-08-11 phone-polish work (full rationale for that
work is in `docs/architecture/views.md`, which notes this gap and points
back here). Read Mode (`GET /read`) never got either of the phone-nav fixes
the main app has:

- **Resume — shipped 2026-08-25.** Turned out "same localStorage key" would
  have made the two surfaces redirect into each other (leaving the main app
  at `/` could bounce you into Read Mode and back) — deliberately used a
  *separate* key instead (`lectio-read-last-position`) so each surface only
  remembers its own last spot. No scroll offset to restore, unlike the main
  app: Read Mode is plain page navigation, so the URL alone is the position.
  The "Saved" scope tab now carries a harmless `?home=1` (same trick as the
  main app's wordmark) so there's still a reliable way to reach the true
  landing instead of bouncing back into whatever was last read.
- **No Back guard**, and this one is not cheap — `/read` has no drawer for
  Back to land on, and a Back that visibly does nothing is worse than one
  that exits the app. Give Read Mode a collapsible tree first, then add the
  guard.

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

Board is at zero open alerts as of 2026-08-26 (PR #241 fixed a
`py/weak-sensitive-data-hashing` on `_entry_thumb_cache_key`'s sha1 — CodeQL
flags a hash whenever the input matches an "id" pattern, even here where
`entry_id` is a public feed-entry id, not a credential, and the hash is a
cache key, not a security control. Fixed rather than dismissed: sha256 costs
nothing and ends the noise, cheaper than arguing false-positive each time.
Before that, board was at zero as of 2026-08-13: PR #200 cleared a `py/redos`
in the lead-image opener and a substring assertion in a test; alert 191,
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

*Moved down from Now on 2026-08-24: deliberately deferred, no trigger condition met yet.*

### Add OIDC login

Not scoped. Current auth (`services/users.py`, `/login` at `main.py:21999`)
is username/password only — no SSO/OIDC exists today. Architecture-level
addition (new login flow, session handling alongside the existing one,
tenancy binding from an OIDC subject to a Lectio `user_id`, first-login
provisioning) — wants a real plan before code, not attempted yet.

### Two suspect SQL clauses found while building the light-entry fetch path (2026-08-28)

Not fixed — found by inspection while adding `_light_entries_from_sql`
(docs/architecture/views.md), not reproduced as a live bug yet.

- The `>32-feed` ASC/DESC branches' `read_sql` uses `read IS NOT NULL` for
  the "read-only" case. reader stores `read` as always 0/1 (`entry_factory`:
  `read == 1`, no None-handling like `important` gets), so that clause likely
  matches unread rows too — a `history` view over many feeds could pull a
  polluted window. `_light_entries_from_sql` uses the correct `entries.read`
  / `NOT entries.read` instead; the existing branches weren't touched.
- Same DESC branch sorts `sort_by="received"` by `recent_sort`, but the
  light-record loop's actual Python sort key for "received" is
  `entry.added` (`first_updated`) — a different column. Instance of the
  `_ENTRY_SORT_SQL` disagreement class documented above it in the same file.
  `_light_entries_from_sql` uses `first_updated` to match.

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

### Rule editing has no atomic endpoint — DONE 2026-08-25

Editing a rule was a client-side remove-then-add against `/highlights/remove`
and `/highlights/add`. Sending both at once destroyed the rule whenever the
identity `(scope, scope_id, keyword)` had not changed — the add landed first
and the remove deleted it, 20 times out of 20 in a local reproduction, with
both responses OK so the UI reported success. Josh lost a Deals dedup rule to
it on 2026-08-20 (a dedup rule hits this on every edit, since its match
method IS the keyword). Client-side sequencing closed the hole the same day.

**Built the real fix**: a single `POST /highlights/edit` doing the delete
(only if the identity changed) and the write in one transaction server-side,
so no future caller can reintroduce the race regardless of sequencing
discipline. Validation and response-shape logic factored out of
`add_highlight_route` into shared helpers so `/add` and `/edit` can't drift.

### Post list multi-select → bulk actions — SHIPPED 2026-08-26

Checkbox-based multi-select on the post list (`.post-select-check` per row).
Only the checkbox itself adds to the selection — an ordinary click that opens
a post is normal browsing, not selection-building, and replaces the
selection with just that post (`selectOnlyPost`; an earlier version had it
add to the selection instead, which read as "useful, but not what I
expect"). Selection persists across bulk actions, which chain (add to a
playlist, then Mark as read, on the same selection) rather than clearing it.
Escape (in the global
Escape-key priority chain — modal, tags panel, search row, THEN context
menu/selection) closes an open context menu first and only clears the
selection on a second, separate press with no menu open — the first version
cleared both in the same keypress via its own standalone listener, wiping a
22-item selection just to dismiss the menu. Navigating to a different view
also clears it. Right-clicking a selected post collapses the context menu to
bulk-safe items only:

- **Add to YouTube Playlist…** — shown when every selected post is a YouTube
  video (`data-post-video-id`, extracted server-side same as the `[duration]`
  prefix). One request to new `/api/youtube/playlists/add-batch`, capped at
  25/batch (50 quota units/insert) — checks the playlist's existing contents
  first (~1 unit/50 items) and skips anything already there, since the API
  happily inserts the same video twice and removing one copy removes both.
- **Add tag…** — works for any post, single or bulk, via new
  `POST /entries/tags-batch` (mirrors `/entries/move-to-feed-batch`'s
  JSON-array-of-pairs shape). Always appends, never replaces.
- **Mark as read** — bulk-only sibling of the per-post toggle; always marks
  read (no unread direction for a mixed selection). New `/entries/read-batch`,
  same shape, skips already-read entries and unpremiered YouTube videos
  (mirrors "Read above/below"'s guard), bumps the unread-count generation.

Both single-right-click and bulk right-click populate the same
`contextSelectedPosts` array, so the menu items work uniformly for 1 or N.

Fixed same day: `selectedPosts` is keyed by (feedUrl, entryId), not DOM nodes,
so it survived a whole-pane navigation swap and kept accumulating across
folder/feed switches — 1 real selection plus 14 stale ones from earlier
browsing showed as "Add tag to 15 posts". Now cleared on every scope-pane
replace (not on chunk-delta paging, which is the same view). This was also
the likely cause of "Add to Playlist" not showing for an all-YouTube
selection — a stale non-YouTube post silently failed the `every post has a
video_id` check.

### Article list date separators (Today, Yesterday, ...) — SHIPPED 2026-08-25

Client-side `applyPostDateDividers()` in `static/js/app.js`, hooked into the
one `applyVisibleWindow()` call site that all four re-render triggers already
funnel through. `post`/`received` sorts only; suppressed for `starred`/`size`.
A group whose posts are all chunk-hidden or filtered-out hides its own
divider.

First deploy showed zero dividers, ever. Two bugs, found in this order:

- The real cause: code read `data-post-iso`/`data-received-iso` off
  `.post-item` itself, but those live on a nested `<time>` child — the exact
  trap an existing comment on `applyBulkReadState` already flagged, missed on
  the first pass. Every item's timestamp came back empty, so no group ever
  formed. Fixed by querying `item.querySelector('time[data-post-iso]')` first.
- A second, latent bug found and fixed while chasing the first: `container`
  (`.posts`) is also what the chunk-reveal `MutationObserver` watches with
  `childList: true`, and rebuilding dividers unconditionally on every call is
  itself a childList mutation — the observer's own re-render would have
  retriggered the rebuild forever the moment the first bug stopped masking it.
  Fixed by diffing against what's already in the DOM (`data-divider-key` +
  `nextElementSibling` per group) and only writing when it actually differs.

Confirmed working live by Josh 2026-08-26.

Scoping notes below kept for context on why this was one hook, not four:

- Every `.post-item` is in the DOM from first paint; `post-timestamp`/
  `received-timestamp` already ride as `data-post-iso`/`data-received-iso` on
  each row's `<time>` (`applyRelativeTimestamps` already reads these), so no
  new server data is needed for the two universally-available sorts (`post`,
  `received`).
- **Chunking hides rows via a CSS class (`post-item-hidden`), it does not
  remove them** — `setupPostChunks`' `applyVisibleWindow`/`revealNextChunk`
  reveal more of what is already in the DOM, then fetch a server-side
  "chunk delta" and *append* more rows once the loaded set is exhausted. A
  separator's own visibility has to track whether anything under it (until
  the next separator) is currently visible, and has to be recomputed after
  every chunk reveal and every server-appended chunk delta, not just once.
- **The live filter (`postsFilterActive`) bypasses the chunk window
  entirely** and hides/shows rows independently — same recompute problem
  again, on a third trigger.
- **A sort/scope change replaces the whole `.posts` container** via the
  pane-swap fragment fetch, so separator injection has to re-run after that
  too, not just at initial page load.
- `sort_by=starred`/`size` (Saved/Kept-scoped) either aren't uniformly
  chronological (`size`) or don't currently expose a per-row date attribute
  (`starred`) — the honest scope for a first version is `post`/`received`
  sorts only, separators suppressed for the other two.

Four re-render triggers to hook (initial paint, chunk reveal, filter
apply/clear, pane-swap reload) is real integration work, not a one-file
change — worth a short plan before touching code, per the usual bar for
multi-file behavior changes.

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

### Read Above/Below still slow right after a refresh touches the same feeds

`_light_entries_from_sql` (2026-08-28, docs/architecture/views.md) fixed the
entry-hydration cost — reading the "Deals" folder's full history (17 feeds,
~10.7k entries) dropped from 8.4s to 0.4-1.0s in a settled DB. But a real
mark-range-read minutes later on the same folder measured `fetch_ms=4301`
(~5s total) instead. Diagnosed, not fixed: a refresh cycle had just written to
that folder's feeds (slickdeals: `modified=16`) ~3 minutes earlier, and
reading through a WAL file still holding recent writes costs more than
reading a checkpointed one. Same shape as the home-route item above —
concurrent-with-refresh reads pay a tax the isolated benchmark doesn't show.
Ruled out as a cause: the mark-as-read write loop itself (117 individual
`reader.mark_entry_as_read` calls) — measured at 38ms total, not the
bottleneck.

The lead, if picked up: whether reader's WAL checkpoint behavior can be made
more proactive after a refresh pass, so reads shortly after don't inherit an
un-checkpointed WAL. Bigger and riskier than the fetch-path fix — it's a
pragma/timing change affecting every read/write in the app, not just this one
path, so it needs its own measurement pass before touching anything.

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

**Separate gap, noticed alongside the resurrection bug (2026-08-23/24, not
built): the import never assigns folders.** Any feed the `subscriptions`
phase adds — first import or later resync — lands in Uncategorized
regardless of what folder/label it had on Ino's side. `get_subscriptions`
returns each sub's `categories` (Ino's label list); `get_tags`/labels_items
already models the label→folder relationship for the *tagging* phase, so
the same mapping could place a newly-subscribed feed into a matching folder
(creating it if needed, same as `_get_or_create_folder_by_name` does for the
generic migration applier) instead of dumping it folderless. Low urgency —
mechanical once picked up, and `declined_feeds` (above) already stops the
bigger problem of the same feeds reappearing on every resync.

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

### Backfill already-expired signed lead-image thumbnails

**BUILT 2026-08-24: the go-forward half.** Per-entry lead-image pinning
(`_pin_entry_thumbnail_bytes`, sink wired into
`lead_image_service.store_entry_lead_image`) now stores a small stable-URL
copy of any *signed* lead image (host-agnostic via `_IMG_CACHE_VOLATILE_PARAMS`,
not just DeviantArt) the moment it's discovered, while the token is still
fresh — closing the list-thumbnail gap for every entry ingested from now on.
Full design in `docs/architecture/images.md` ("Pinning a list thumbnail
before its signed URL dies").

**Not built: the backfill.** Pinning only fires on write, so it does nothing
for the ~22,300 already-expired wixmp URLs sitting in `entry_lead_images`
today — their list thumbnails stay broken until a re-fetch/re-enhance
happens to touch them. Every DA entry stores the deviation UUID as its entry
id, so a paced script (`refetch_batch.run_paced`, [[good-web-citizen]]) that
re-signs each dead URL via the DeviantArt API (same call
`_resign_expired_deviantart_url` makes on article-open) and feeds the fresh
URL through `store_entry_lead_image` — which pins it as a side effect, no
separate pinning call needed — would turn this into a one-time cleanup.
Low urgency: the number stops growing either way, and opening a post already
repairs it via the article-view re-sign.

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

### Saved-dedup checkbox direction inverted from feed-combine — fixed 2026-08-25

Josh reported almost deleting good saved articles: the feed-dedup Combine flow
picks which one to **keep** (a radio, "survivor"), but the saved-articles dupe
scan's checkboxes marked copies for **deletion** — checking a box meant the
opposite of what the muscle memory from the other flow suggested. The tooltip
even said so ("dead links are flagged and selected for deletion") but that's
not where anyone's eyes are when clicking through a list fast.

**Fix: every copy now defaults to Keep**, with an explicit ✓ Keep / ✗ Delete
toggle per row instead of a bare checkbox — no direction to misremember, since
the two buttons are labeled. Check URLs still auto-switches confirmed-dead
copies to Delete (never the sole copy with real stored content), everything
else stays manual. New safety property that didn't exist before either
direction: **a group where every copy got switched to Delete is skipped
entirely** rather than deleting the whole group — the group-level equivalent
of the per-row protections that already existed. `saved_dedup_workflow`
(memory) has the wider dupe-scan history; this only touched the selection UI,
not the scan/matching logic.

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

**Whole-repo lint backlog — CLEARED 2026-08-24.** `make lint` went 220 → 0.
Four dead one-off debug scripts deleted (unreferenced, from a resolved
investigation), `ruff --fix` handled the mechanical rest, and the remaining
72 were hand-fixed: renamed ambiguous/unused loop vars, lambda→def,
`raise ... from`, split long lines, and a per-file `pyproject.toml` ignore
for main.py's FastAPI `Query`/`File`/`Form` route defaults (B008 false
positive — that's the framework's required idiom). Two real bugs surfaced
along the way, not lint-only: `lead_images.py` used
`.lstrip("www.")` on a cookie-challenge domain (strips the character set,
not the literal prefix — `www.wired.com` became `ired.com`; fixed with
`removeprefix`), and a dead `tok = ... if False else None` block in the
Miniflux token route. Full history in the "Weekly stack sweep" commits
around 2026-08-24. `make lint` is now part of what's worth keeping green —
watch for regression, no further backlog to burn down.

**Whole-repo type backlog — CLEARED 2026-08-24.** `make types` went 165 → 0,
same day as the lint sweep above, across ~15 commits. `ty` is now wired into
CI and the pre-commit hook — `scripts/lint_changed.py` runs both ruff and ty,
blocking only on lines a change touches, same shape as ruff always had; a
new informational whole-repo `ty check .` CI step sits alongside the
existing informational whole-repo `ruff check .` one. Deliberately not
blocking whole-repo: ty is a preview tool, and its diagnostic count already
shifted once from a version bump alone with no code change (the dependency
sweep that led into this whole cleanup).

**The `.lstrip("www.")` bug (see the bullet below) turned out to be a
recurring class, not a one-off** — a repo-wide grep for multi-char
`lstrip`/`rstrip` literals found 4 more real instances the same day:
`services/reddit.py`'s `submit_link` and two `main.py` call sites all did
`subreddit.lstrip("r/")`, mangling any subreddit actually starting with `r`
("running" → "unning") — live bug in both the `/api/reddit/submit` route and
the star-to-Reddit background sender. `scripts/lint_changed.py`'s own path
fallback did `name.lstrip("./")`, mangling any touched file in a
dot-prefixed directory (`.github/workflows/x.py` → `github/workflows/x.py`)
— the exact script that gates CI lint on touched lines. All fixed with
`removeprefix`. Worth grepping for `\.lstrip\(["'][^"']{2,}["']\)` /
`\.rstrip\(...)` again if this area is ever touched — nothing guarantees
these were the last ones, just the last ones as of 2026-08-24.

Two real bugs surfaced along the way, beyond the lint pass's two:

- `services/inoreader.py`'s `edit_tag_remove` built httpx `data=` as a list
  of `(key, value)` tuples — a `requests`-era idiom httpx's `data=` doesn't
  support (it wants a `Mapping`; a non-Mapping routes through httpx's raw-
  content fallback, which would fail trying to write tuples as HTTP body
  bytes). Never actually called from anywhere, so never crashed anyone —
  fixed to `{"r": tag, "i": item_ids}`, which httpx's own encoder expands
  into repeated `i=` params, and verified the wire output.
- `/api/save` merged POST form data into `params` with a plain
  `dict.update()`; a multipart file field named `url`/`username`/`token`
  would have overwritten the query param with an `UploadFile` object that
  downstream code treats as a plain string (auth token comparison, URL
  parsing) — now only `str`-valued form fields are merged in.

Plus two real annotation bugs (not just missing ones): `add_feed_to_folder`
was typed `-> None` but a real caller captures its return value (the
slash-normalized/reused feed URL), and `services/lead_images.py`'s
`_page_tag_sink` field was typed as a 3-arg callable when the setter, the
one real call site, and the one real sink implementation all agree on 4.

Full history in the "Type cleanup" commits following the lint sweep.

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
