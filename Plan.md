# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Five tiers: actively impeding unread-clearing, small independent wins, sized work with no open
decision, real features not blocking anything today, and deliberately-deferred big investments.
Within a tier, related items are clustered under a bold sub-heading. Two watch-lists (CodeQL,
Parked) sit at the end — nothing there is scheduled, just what to check if a symptom recurs.

Tiers 1 and 3 are empty (2026-10-17). Tier 2 is small, decided work and watch items; Tier 4 is real features, highest value first;
Tier 5 is deliberately unstarted projects (shared rendering core, OIDC, shared-content tenancy).

## Tier 1 — actively impeding unread-clearing

Empty.

## Tier 2 — small, fast, independent wins

**Do next** — small, concrete, decided.

### Email batch mode can re-queue an entry after a flush (2026-10-18)

`email_batch_queue` dedupes on `UNIQUE(rule_scope, rule_scope_id, rule_keyword, entry_id)`, but a flush deletes the rows, so a second pass
inside the 15-minute window re-queues an already-digested entry. The key also lacks `feed_url` and `rule_uid`. Fix with the same
`automation_dispatched` claim, taken at queue time.

### Feed-tag capture gaps that predate the YouTube hashtags (2026-10-14)
Found by PR review; all three apply to every tag source, so they want their own change: (1) `_collect_entry_tags` skips an entry whose
extraction is empty, so a feed that drops all of an entry's tags never clears the stored `source='feed'` rows; (2) `entry_feed_tags` is
keyed `(feed_url, entry_id, tag)`, so a feed tag equal to a page tag is ignored on insert and vanishes if the page row is later removed;
(3) a failed meta-DB write during parse is logged and not retried, and a 304 never re-parses.

### Move-all-shown-to-feed may share the Select All gap (2026-10-16; plausible, not reproduced)

Select All now also takes on-screen rows because the server resolves by read filter while rows marked read stay visible. "Move all shown
to feed" resolves server-side the same way; check it before assuming it matches what is on screen.

### Archive fetch-status follow-ups (decided 2026-10-17)

`archived_entry.source_fetch_status` exists (2026-09-23); the pre-column empty archives were backfilled by `scripts/probe_empty_archives.py --write`
(2026-09-25), with reachable-now rows left NULL as recapture candidates. Decided:
1. A "Capture failed" filter chip in Saved (no per-row badge), the way Disabled works in Automations.
2. Transient kinds (`timeout`/`connect`/`http_5xx`) retry in a weekly pass capped at about 20 rows (good web citizen); after 3 tries a row
   stays on the filter for a manual call.
3. Recapture the 20 reachable-now rows once with `scripts/recapture_archived_entries.py`, dry-run first and the list shown, since it rewrites the capture.

### Tag-filter chip click under a shared rule: keep forking, say so (decided 2026-10-17)

Clicking a post-header chip lit by a folder or global rule keeps creating a feed-level override (editing the shared rule from one feed could
change dozens of feeds unnoticed). Add a tooltip making the fork explicit ("Overrides the folder rule for this feed").

### Refresh the screenshots (last 2026-08-09)

`uv run python scripts/refresh_screenshots.py` is hermetic (synthetic demo data). Since August the UI gained tag-chip filtering in the filter bar,
the Disabled chip in Automations, disabled-feed reasons, the grouped Utilities tab and regrouped right-click menus. Add one filter-bar shot and
a right-click menu shot. Needs `uv run playwright install chromium` (the venv's Playwright wants a newer Chromium than the cached one).

**Dated** — act on or after the date.

### Reddit RSS ends 2026-11-13 (2026-10-04)

Reddit is retiring `.rss` feeds with no replacement for non-mod use, and API access was refused, so Reddit stops being a source. On/after the cutoff, delete the six Reddit feeds in the admin account (r/VinylDeals, r/buildapcsales, r/GameDeals, r/Boardgamedeals, r/LaptopDeals, one user-submitted) through the app's feed-delete path (none are starred or archived), restart, and drop any Reddit mention from README.

Replacements: BGG `https://boardgamegeek.com/rss/uforum/10` (Boardgamedeals; the forum HTML 403s, only the `/rss/` URL works) and a filtered Slickdeals feed (PC and laptop deals). No replacement for r/GameDeals or r/VinylDeals; vinyl.deals has no feed and its rows are `onclick` handlers, so the page scraper can't read it without a new row mode (skipped).

**Build when it bites** — only if the symptom recurs or the cost is felt.

### HTTP/2-only feed hosts: 426 Upgrade Required (2026-10-12)

Counted 2026-10-14: one feed across all users fails with `426` (an Apache host that serves only HTTP/2; `requests` is HTTP/1.1-only), so an `httpx[h2]` path isn't worth building yet. Revisit if a second host shows up: treat a 426 as a page-ladder escalation trigger and fetch it with `httpx[h2]`.

### GitHub OpenGraph card 429s — watch for recurrence (2026-10-12)

Hugo's feed has an empty description for every release, so the `opengraph.githubassets.com` card is the whole post. The v0.167.0 card
returned 429 through `/api/img` (and from curl) while v0.166.0's returned 200 from the same container, so it looks like a per-URL
GitHub-side hiccup, not our upgrade. If it recurs across releases: check whether a failed card fetch is negative-cached (it would stay blank
after GitHub recovers) and whether the pane should retry.

### Folder-switch speed — what's left (2026-10-02)

Shipped: the saved-unread badge cache (unread generation + `saved_entries` count/max-rowid signature, 15s TTL, cleared by tag writes and `apply_star_state`), a partial index on `entry_lead_images(feed_url) WHERE locked_until IS NOT NULL`, no underfill retry on a short raw fetch or a first page within 10% of full, Saved unread as one set intersection, a 15s
tagged-key cache, a 1ms GIL switch interval, and no pinning of thumbnails over 1 MB. Left:
- GIL starvation was confirmed with `py-spy` (78% of GIL time in `purge_tombstoned_entries`, fixed). The 1ms switch interval is a
  belt-and-braces measure; revert it to the 5ms default if it ever shows a throughput cost. Re-profile before considering a
  separate refresh process.
- `lectio:saved` has ~4.7k tombstones and no publisher window, so the nightly sweep never removes them. Harmless now that the purge
  is one query per feed.
- `_has_manual_tags_cache` is keyed per user but not per DB; the tagged-key entry is path-checked, the `"any"` entry is not.
- `img_cache` is still 2.6 GB on disk after deleting 1.25 GB of oversized pinned rows; a `VACUUM` reclaims it (exclusive lock on the live DB: run it deliberately, after a WAL checkpoint).

### Re-fetch follow-ups (2026-10-02)

- The page ladder's 6h host cooldown trips on a single URL exhausting every tier, which silences autofetch for the whole host. Requiring
  two consecutive exhausted fetches would soften that but sends one more request at a host that is blocking us; weigh against the
  good-web-citizen rule before changing (existing tests assume one).
- Lazy-loaded embeds recovered from the raw page are appended at the end of the article, not placed where they were. A per-site
  plugin could re-insert each into its empty `.tve_responsive_video_container` (guitarchalk has ~30 per lesson).
- Already-stored posts that lost embeds to the old `data-src` drop need a re-fetch to get them back.

## Tier 3 — sized, no open decision, ready to build

Empty.

## Tier 4 — real features, not blocking anything today

No decision pending on any of these; each is "build when wanted". Roughly highest value first: FakeFeedz, Combine-when-failing, then the rest.

### FakeFeedz Enhancements (2026-10-16)

Setting up three page feeds took many round trips because the Add Feed dialog only exposes the listing-page selector; `content_selector`
is a hidden field. In rough value order:

1. Visible "Post body" selector (accepts `START >> END`) plus a "Preview first post" button showing the extracted body and images.
2. Richer item preview: per-item date (or "no date found"), the thumbnail that would be used, and a flag on URL-as-title.
3. Click-to-pick the body region in the existing picker (click the first element, then where it ends) and write the `START >> END`.
4. Defaults when a post body is set: lead-image strategy "inline" (a page scan takes the site banner).
5. A "Check feed" step after creating: titles that are URLs, empty bodies, identical thumbnails across entries.
6. No edit UI for `content_selector` on an existing feed (a direct DB edit today); a "Page Feed" tab in Feed Properties.

### Similar-feeds Combine refuses when every candidate is failing

The similar-feeds utility's Compare → Combine (app.js, "None of these returned valid feed data — nothing to combine") blocks the merge
when no candidate fetches. Raised 2026-09-28 on a pair differing only by a trailing slash, both 403: Josh had to combine them by
hand from Feeds. When the URLs differ only trivially (trailing slash, scheme, `www.`), still offer Combine with a survivor picker.

### Inline SVG in feed content is mangled at ingest

feedparser mishandles self-closing tags, so any feed shipping inline SVG art renders as a flat color block. Fix: re-parse SVG subtrees as XML at
ingest. Real bug with a known fix; build when wanted.

### Code layout refactor (audit 2026-10-18, not started)

Audit: `docs/layout-audit-2026-10.md` (read-only, line numbers as of f8be4d6; re-grep before cutting). Root problem: no storage layer.
`main.py` (26k lines) holds the connection pools, ~1,350 lines of schema DDL, the settings store and the reader pool; 22 route
modules and 4 services `from main import` them, so import order is load-bearing. Rules for every step: one extraction per PR, no
behavior change, `main` keeps a re-export, and grep `setattr` for each moved name first (a moved function stops seeing
`monkeypatch.setattr(main, ...)`, so the test passes without testing the patch).

Sequence (details and line ranges in the audit): (1) `run_in_user_context` to `services/tenancy.py`; (2) `mine_publish_date` and
`_append_site_embeds` out of `main` (kills the lazy imports in `scraper_service`); (3) shared leaf constants module; (4)
`_view_filter_predicate` to `services/view_filter.py`; (5) `bulk_feed_action` to a service; (6) `LeadImageService` into mixins in a
package; (7) `routes/feeds.py` into sub-routers; (8) `routes/entries.py` likewise; (9) split `app.js` into ordered classic scripts,
Highlights/Automation block first; (10) schema DDL and connections to `storage/`; (11) settings store and `get_reader` pool to
`storage/`; (12) HTML transforms and article extraction into `services/`; (13) per-integration credential getters and the DeviantArt
sync into their services; (14) rules and tag helpers out, then drop `from main import` in the three services; (15) redesign work
(scheduler, shared read/star mutation service, starred-archive split, `get_entry_detail` and listing), each behind a characterization
test. Open before step 9: check whether `app.js` functions are called from template inline handlers.

Folded in (2026-10-18): (a) the dormant in-app star-mode tree and JS that the Read Mode hijack bypasses is deleted before step 9
(check nothing in `tests/` reads those strings first); (b) the `/api/*` cluster moves during steps 7-8: `/api/entry-thumb`,
`/api/feed-thumb`, `/api/img`, `/api/favicon` to a small `routes/media.py`; `/api/save` and `/api/bookmarklet/save` to
`routes/saved.py`; `/api/unread-counts` has no owner yet, decide then; (c) the shared rendering core (`_home_inner`,
`list_entries_for_feeds`, `get_entry_detail`, `build_reader_page`, reused by `/`, `/read`, pane-swap and the greader/fever/v1 APIs) is
step 15's last item, not a mechanical split. Sequencing: land "Inline SVG in feed content is mangled at ingest" before step 12, and
do "Unify the escalation ladder" before or after step 12 and the `routes/media.py` move, never between (it touches `/api/img`).

### Page-fetch escalation ladder — follow-ups

- Broaden `extract_page_tags`'s recognized markup patterns as new gaps turn up (last full survey
  covered ~622 feeds; ~210 genuinely have no taxonomy, ~107 still blocked, mostly ArtStation's
  JS-heavy tag widget). No more broad surveys needed unless the untagged count grows a lot.
- Persist `HostEscalationState` to a `host_fetch_tiers` table if in-memory proves insufficient.
- Key `_autofetch_failed_hosts` on deepest-available-tier, same fix `HostEscalationState`'s
  cooldown got, if it matters in practice.

### Unify the escalation ladder as one fetch layer in front of every outbound content fetch

Idea from Josh, 2026-09-23. Today the per-host escalation logic (`HostEscalationState`, honest UA →
browser UA → FlareSolverr) is split across at least three places that don't share state cleanly:
`services/page_fetch.py`'s `PageFetcher` (used by `main.py` and `services/lead_images.py`),
`services/feed_discovery.py`'s own separate, older `_get_with_escalation`/`HostEscalationState`
(feed refresh's fetch path), and `main.py`'s `/api/img` proxy, which doesn't escalate at all — it
only *borrows* FlareSolverr-solved cookies from a prior `PageFetcher` solve for the same host
(`page_fetcher.cookies_for_host`), one-way and only when a page fetch happened to solve that host
first. That gap is the likely root cause of the Parked "play.nobleknight.com images 403 despite
FlareSolverr cookie reuse" item — no page fetch ever solved that host, so there's no cookie to
borrow, and the image path has no fallback of its own. `/api/favicon` and thumbnail generation are
unaudited but likely the same shape.

The idea: one shared fetch layer (`PageFetcher`, extended, or a new front for it) that every
content-fetching call site goes through — feed refresh, page/readability fetch, `/api/img`,
`/api/favicon` — keyed by host, sharing one `HostEscalationState`/cooldown table instead of three
copies of the concept. A host escalated for its feed or a page fetch would already be at the right
tier for its images, no separate solve needed. Out of scope: the OAuth/API integrations
(DeviantArt, Reddit, YouTube, Bluesky, etc. in `services/*.py`) — those hit trusted APIs, not
scraped pages, so they don't need anti-block escalation, just their own auth.

Not small and not safe to rush: this sits in front of the two highest-traffic, most
correctness-sensitive fetch paths in the app (feed refresh — see the refresh-scheduler-stall
history — and the image proxy, which is also the one place `url_guard`'s SSRF guarding has to keep
working exactly as it does today). The already-scoped "consolidate `feed_discovery._get_with_escalation`
onto `PageFetcher`" follow-up above is the first slice of this same idea, not a separate item —
do that first as the smaller, lower-risk step, and let it validate the shared-state shape before
extending to images/favicons. Worth a real plan before any of it, same bar as the dedup-combining
idea below.

**Dedup subsystem** — biggest single feature idea on the list.

### Combine cross-feed duplicates instead of marking one read

Dedup's only action today is "mark the newer copy read" — destructive, so the Safe tier requires
body corroboration, which is why it finds nothing in the folders where duplicates actually pile up
(aggregator vs. source, e.g. Hacker News). Combining removes the reason for that strictness: a
duplicate group renders as one entry (primary = richest body), other members appear as an
"Also at:"/"Discussion:" line in the body rather than in the list; marking read marks the group;
splitting restores members.

Matching: combining accepts the current Safe combos plus `{slug,title}` and exact cross-feed title
(slug alone stays excluded — a real false positive exists: two different stories sharing a slug).
Storage: a new meta table for groups (group id, feed_url, entry_id, role), needing the per-user
startup migration; `dedup_false_matches` should feed the splitter.

Open: whether combining runs as an automation rule, an invoked scan, or at ingest; unread counts
and the offline outbox both need to treat a group as one item. Worth a real plan before starting.

### Saved-articles dupe scan follow-ups (deferred)

Fuzzy title matching for `/saved/duplicates` — deprioritized: the actual missing dupes are
cross-feed (out of the scan's scope entirely), not fuzzy-matchable within `lectio:saved`. Add only
if the exact tiers still leave real dupes behind after the cross-feed item above.

### Auto-file saved articles — the tail

- guitarplayer.com's 303 articles: decision made to look for/build a real feed rather than leave
  them as one-off saves.
- 166 already-converted stars (from a since-fixed backfill bug) can't be surgically reverted; the
  unstar-tagged pass removes them.

**Rules engine follow-ups**

### Article cleanup — Phase 2: promote a removal into a per-feed rule

Phase 1 (manual per-article cleanup via the pane's 🧹, with `entry_content_edits` recording both
the pristine body and the replayed ops) is shipped. Phase 2 would add a `feed_content_rules` table
+ render-time matcher, a Cleanups section in Feed Properties showing match counts before promoting,
and selector derivation from the recorded ops.

Measured 2026-09-22: grown to 15 edited entries / 111 ops (from 4/67), spread across 9 feeds —
7 on `lectio:saved`, one each on 8 real feeds (lwn.net, Scott Hanselman, commandlinefu, guitar-pro,
OSNews, Lambgoat, kingcountyweeds, PBS NewsHour). Still not ready: the trigger is ≥3 edited entries
on the *same* real feed, and every real feed still has exactly 1. Any future rule should key on
entry-link host, not `feed_url` (most edits are on the `lectio:saved` pseudo-feed, which spans every
site). Re-check the per-feed distribution periodically rather than assuming the old snapshot holds.

### Refetch-All has no "already re-fetched recently" skip

No column records last-refetch time today (`published`/`first_updated`/`edited_at` all mean
something else). Sizing if ever wanted: small — one new column (needs the per-user startup
migration) + a skip check in the Refetch-All loop. Not requested yet — not scheduled ahead of
demand.

### Read Mode: no Back guard

`/read` has no equivalent of the main app's Back-button guard. Not cheap: `/read` has no drawer
for Back to land on, and a Back that visibly does nothing is worse than one that exits the app.
Give Read Mode a collapsible folder tree first, then add the guard.

### Page-weight reduction — optional follow-up

Render-splitting/fragment endpoint for `.pane-posts`/`.pane-entry` — pane-swap currently
re-renders the full page server-side per fetch (~200KB). Bigger and optional.

### Offline actions — stale-action guard

Today's conflict rule is last-writer-wins; "accept the server's version if it already moved" needs
a per-entry modification timestamp the schema doesn't carry. Low urgency (the only conflicting
writer is Josh on another device, within minutes) — do only if a surprising revert is actually
observed. Offline star/unstar, the other piece this project used to carry, is decided closed
(2026-09-22): star stays a desktop-only action, Read Mode's control set stays Archive/Delete.

Deliberately *not* built: a `synced_actions` idempotency table — the four outbox routes are
already idempotent set-state operations, so replaying one is a no-op.

### One stored image per entry, but three feeds want two

Three comic feeds want a different image in the list than in the article; Lectio stores one URL
per entry and derives the list crop from it. Two of three needed a plugin/derivation (Penny
Arcade, dresdencodak); the third needed nothing (`media_rss` already picks up the right
publisher-supplied thumbnail) — check what a feed already provides before writing a plugin. General
fix — a second stored URL + a per-feed "thumbnail source" setting — needs the startup migration;
worth doing when a fourth feed wants it, not before.

### Send-to-destination — remaining candidates

Rule engine + on-star fan-out + shared destination senders are shipped (Instapaper, YouTube
playlist, email, Quire, Pinterest). Build more only if actually wanted: save-to-tag/starred-archive
as a rule action, Readwise/Reader, Wallabag. Each is small, reusing the existing engine.

Instapaper API v2 (announced 2026-09-29): we only *save* via the Simple API (`/api/add`, username + password), which the post doesn't
mention, so nothing is forced. xAuth shuts off 2027-09-30. If the Simple API is ever deprecated, or we want tags sent with a save,
switch the sender to v2 with a personal access token (no password stored). Revisit if Instapaper announces anything about the Simple API.

Readit (wereadit.com): send-to-Readit is blocked — their save endpoint is unreachable outside
their own extension (Cloudflare). Import is blocked until they expose an export/API. The reverse
direction (Lectio receiving from the Readit extension's save protocol) already works today.

### Global audio player — deferred v2 ideas

Queue/playlist across a folder, remember position per episode, Media Session API (lock-screen
controls), speed presets.

## Tier 5 — deliberately deferred / big investments

**Waiting on an example**

### Single-post pages: raw/full-page capture quality

Some "feeds" are one standing document (a single tutorial page) saved via a manufactured feed; readability can return a small fraction of the
page, or the wrong node. Needs a concrete example page before it can be scoped (deferred 2026-09-24). Workflow half superseded: such pages get
filed into a related feed by auto-filing.

**Architecture**

### Add OIDC login

No SSO/OIDC exists today (username/password only). Architecture-level addition — new login flow,
session handling alongside the existing one, tenancy binding from an OIDC subject to a Lectio
`user_id`, first-login provisioning. Wants a real plan before code.

### Multiuser

- **Performance investigation** — a systematic per-request baseline (DB time, enrich time, refresh
  contention) under realistic load.
- **Shared-content tenancy mode** — one global feed/entry store plus per-user overlays
  (read/star/folders/subs): one refresh per feed regardless of subscriber count, deduped storage,
  incrementally-maintained unread counts. Biggest caching/refresh win, but only worth building at
  real scale — the current per-user-fetch model is fine for 1–3 trusted users.
- **Per-user resource fairness** — rate limits on refresh, scraping, thumb generation. Not needed
  for trusted users; hooks are in the seam.
- **Write-abuse protection (read-state spam).** Every read toggle invalidates the counts cache and
  forces a recompute, so flip-flopping hammers the shared SQLite. Defenses cheapest → strongest:
  coalesce rapid toggles on one entry; throttle the counts recompute per user; a per-user token
  bucket on state-changing endpoints (429). ⚠ Tune so legitimate fast keyboard triage never trips
  it.

### Lectio browser extension (fork of readit-extension)

Deliberately deprioritized below the Now chain — a fork is a new codebase and a real commitment,
pick up only when ready to invest. Value order:

1. **Visibility-aware capture.** The stock extension serializes the raw DOM, including everything
   the live page merely *hides* (uBlock cosmetic filters, dismissed cookie walls). Walking the DOM
   and dropping hidden/zero-size nodes before POSTing would make "what you see is exactly what
   saves" true, ending a whole class of server-side widget whack-a-mole.
2. **Deliver tags already extracted from bot-walled pages** (ArtStation and one other site show
   per-post tags on the page but ship none in the feed, and the server can't reach either page).
   The extractor (`extract_page_tags`) is done — only delivery is missing: POST the open page's DOM
   to a route. Trigger on Keep, not per entry. (Cookie harvesting was considered and rejected:
   Cloudflare's `cf_clearance` is IP+UA-bound, so a harvested cookie doesn't work from the VPS
   either way.)
3. **Dual-extension use** — one browser running save-to-Readit and save-to-Lectio side by side.
4. Nice-to-haves once forked: badge feedback distinguishing saved/duplicate/refreshed, prefilled
   Backend, username+API-token auth.

Keep the wire protocol unchanged (`/api/bookmarklet/save`) so the stock extension keeps working.

### Backfill older posts from a URL pattern

A feed shows only the publisher's recent window; the back catalogue is often still reachable via
predictable URLs. Open decisions before any code:

- **Dates** — don't synthesize "now" for an undated entry (floods the Inbox top); mine the date
  from the page or skip the entry.
- **Identity** — dedupe against both what's already there and what was deliberately deleted, or an
  import resurrects discarded entries.
- **Where it stops** — a user-set bound (N pages / back to a date) with a dry run reporting the
  count first.
- **Rate** — must go through `refetch_batch.run_paced`, not a new loop ([[good-web-citizen]]
  applies at import too).

Fits the existing adapter shape: a per-feed pattern (stored, not hardcoded — see `image_size_rule`
for the precedent) plus a paced walker. Worth a real plan before any code.

### Code health (deferred — low value, no user impact)

- **Stack updates** — `python:3.15-slim` and `uv:latest` are floating tags, so a cached base layer can go stale between rebuilds (`--pull` in
  `make rebuild`, or a digest pin bumped in the weekly sweep; both current 2026-09-30). The sibling containers (searxng, traefik, tailscale,
  browserless, redis, gluetun) run `:latest` from separate compose projects and were behind on 2026-09-30; pull and recreate by hand, which
  restarts the VPN, tailscale and traefik.

## Watch-lists

Nothing here is scheduled — just what to check if a related symptom recurs.

### CodeQL board

The 48 route-split alerts (28 `py/url-redirection`, 20 `py/stack-trace-exposure`) were fixed 2026-09-30, not dismissed: redirect targets go
through `services.redirects.local_url` (modeled as a barrier in `.github/codeql/queries/`, with the stock query excluded the same way as
SSRF and path-injection), and catch-all handlers answer with `services.errors.public_error` and log the traceback. Reproduce with
`make codeql-db && make codeql-fast` (see README); local results matched CI's before the fix. If alerts return, check a new
`RedirectResponse(url=...)` or `str(exc)` response first. Notes for next time:

- A negative lookahead will not clear a ReDoS alert — CodeQL's regex model ignores lookaheads.
  Write the loop lookahead-free or move the scan into Python.
- Committed page fixtures are excluded from analysis (`paths-ignore: tests/fixtures`) — a captured
  page is byte-for-byte what a site served, so analyzing it reports the remote site's choices as
  ours.
- Stock `py/url-redirection` clears `"/x?y=" + tainted` (right side of a `+`) but not f-strings, which is why every f-string redirect was
  flagged; `local_url` is the fix rather than rewriting them as concatenation.
- If the reflective-XSS class keeps recurring, `.github/codeql/queries/` already has the pattern
  for a guard-aware custom query (see the SSRF/path-injection/URL-redirection ones modeling our sanitizers as
  barriers).

### Feed-tag suggestion suppression — do not attempt a third heuristic

Tried twice, reverted both times: coverage-based suppression wrongly caught legitimate filing tags
(a feed can put one tag on every post and still have that be the right tag); feed-name-echo broke
on the real, non-assumed feed title. The distinction (a tag naming a *place* vs. a *kind of
content*) is semantic and no feed metadata expresses it. Resolved with manual per-(feed, tag)
dismissal instead (`suppressed_feed_tags`).

### og_scrape feeds with no og:image at all

162 of 585 auto-detected `og_scrape` feeds have no `og:image` on their source page and correctly
fall back to a body image. Not broken — just where a future "odd body image" report will come from.

### Free-threaded Python is blocked by lxml

lxml hasn't declared free-threading support, forcing the GIL back on at import. Recheck when it
does; nothing else blocks it. Rechecked 2026-10-15 on 3.15.0t: lxml and Pillow now ship `cp315t`
wheels, but `import lxml.etree` still re-enables the GIL, so the blocker is unchanged.

### Methodology: diagnosing refresh-contention/latency stalls

Elapsed-time SQL timing can't distinguish SQLite lock-wait from GIL starvation — live `py-spy`
sampling during a real stall settles it faster. Reach for this first, not last. Run it as
`sudo env "PATH=$PATH" py-spy record --pid <python pid from docker top> --gil --nonblocking ...`: without `--nonblocking` it
pauses the process for every sample and freezes the live app.

### Parked, deliberately

Nothing to do here until one of these recurs or a lead turns up.

- Sort briefly reverts to default after a backgrounded tab is restored, self-corrects within ~90s
  (Surface/Edge) — traced to a real top-level nav from a stale history entry, not a
  stored-preference bug; no reliable repro yet.
- On-screen-keyboard popup scrolls the post list to top — not reproduced in Playwright with
  viewport-resize emulation; needs a real-device screen recording.
- `make rebuild` cycle is 100-130s — not investigated; revisit if it keeps coming up.
- 5 tests fail locally with `outbound network blocked in tests` (reproduces on a clean commit too,
  smells like sandbox-specific networking) — check real CI is clean before spending time on it.
- play.nobleknight.com images 403 despite FlareSolverr cookie reuse — the solve never returns the
  actual `cf_clearance` cookie for this host; would need per-image browser routing. Likely explained
  by the fetch-unification idea in Tier 4 ("Unify the escalation ladder...") — no page fetch solved
  this host first, so there's no cookie to borrow and the image path has no escalation of its own.
- A re-fetch once returned a different, unique article and the sibling-text guard didn't catch it
  (entry 26031) — the slug/title mismatch guard should have and didn't. Investigate if it recurs.
- The scheduler's trickle case: a host feeding one byte per 29s could keep a refresh pass
  "advancing" without real progress. If it recurs, the fix is a per-feed wall-clock budget, not a
  shorter read deadline.
- Article-nav post-swap binder exception — mitigated (no more hard-reload on failure), but the
  underlying binder exception still exists somewhere. Grab the console error if it recurs.

### Page-topics auto-enable: mixed feeds

`page_topics.enable_if_page_sourced` skips any feed that has even one RSS-sourced tag, so a feed with RSS section categories plus page-only
topics (PC Gamer) is never auto-enabled when you filter on a topic; turn it on in Feed Properties. Fix when it bites: judge the tags named in
the rule (seen only from the page → enable) instead of the whole feed.

### FlareSolverr slot fairness

The single shared solve slot is a plain semaphore: ingest drains (page topics, full content) and an entry-open fetch compete for it with no priority, and a waiter gives up after 55s. gg.deals needs a ~16s solve per page and ignores the saved cookies, so flipping through several posts right after a refresh can starve an open-time fetch (it now just fails without cooling the host; reopen retries). Fix when it bites: give interactive fetches priority over background ones, and log a failed open-time page-tag fetch.

Related, same area, both predating the gg.deals work: a FlareSolverr *outage* (endpoint timeout or HTTP error, as opposed to a busy slot) still records the 6-hour host block, and an ingest fetch that fails on every tier can land its `record_block` after a concurrent open's `record_success`, re-blocking a host that just worked. Fix with the same "the site was never actually tested" rule, and per-host attempt generations for the race.

### Valnet gallery duplicates on sibling sites
`HowToGeekPlugin` strips the duplicate carousel lists on howtogeek.com only. XDA, MakeUseOf, Screen Rant and other Valnet sites probably
ship the same `.valnet-gallery` markup; verify one article per site before adding hosts.

### "Filter this view" follow-ups
- Filtered pages still resolve and enrich the whole 2,000-row window per request (~2s); filtering the light records before enrichment would make each page cheap.
- `tag:~game`: a fuzzy operator that connects related words (game, games, gaming). Maybe never.
- Regex in the filter text (`/pattern/`): needs a timeout-capable engine on the server (user-supplied patterns run in the request); see CodeQL #199 note.

### Mergeable Mark as Read rules: plain text to regex
Two Mark as Read rules on Deals could be one if the plain-text rule became regex, but combining never behaves quite the same without word boundaries:
`\b(?:Kanye\s+West|Micro\s?Center|Lowe['’]s|Chromebooks?|Post\s+Malone)\b` plus a plain list (`Taylor Swift, My Chemical Romance`). Offer the merge as a suggested regex with boundaries added, not a blind join.
