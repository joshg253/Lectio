# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Roughly ordered: recurring/active pain first, then concrete bugs, then
items already scoped and decided so they're ready whenever picked up, then
measurement/investigation jobs, then low-urgency work and the two standing
watch-lists last. Re-prioritized 2026-08-28: shipped/closed items moved to
git history (rationale kept in ARCHITECTURE.md where relevant); items
that were done-but-with-a-real-remainder were condensed to just that
remainder.

### SOCKS5 proxy support for outbound fetches (gluetun VPN)

Design settled 2026-08-29. gluetun runs on the `proxy` Docker network at `socks5h://gluetun:1080` (shared Windscribe Pro NAT IP — helps
ordinary geoblocks/rate limits, not real bot detection; region via `.env`'s `WINDSCRIBE_SERVER_REGIONS`, not app code). `good-web-citizen`
behavior still holds — this is for legitimate geoblocks/rate limits, not evading deliberate blocks.

Settings: `SETTING_PROXY_URL` (admin-only) + `SETTING_PROXY_MODE` (off/as_needed/always, per-user-overridable via `get_instance_setting`'s
existing tier chain — own row → admin's row → env). Off everywhere by default. **Shipped** (settings scaffold).

`always` mode wiring **shipped**: `get_reader()` passes a `proxy_resolver` into `ReaderApi`, which routes feed fetches through the
configured proxy via a request hook mutating `session.proxies` (requests has no per-request proxies param reachable from a hook). Needed
adding `pysocks` as a dependency for `socks5h://` support. Verified end-to-end against a real local HTTP server + an unreachable SOCKS5
target (raised `SOCKSHTTPConnectionPool`, proving it actually routed through PySocks). Scoped to feed fetches only — the separate
httpx-based image/readability fetches (`/api/img`, save-article rendering) are not proxied; a follow-up if that's ever wanted.

`as_needed` escalation **shipped** (2026-08-30): a sibling `proxy_feeds` table + `_flag_proxy_feed_on_still_blocked` mirror the
`browser_ua_feeds` mechanism exactly, in `ensure_meta_schema` (backfills existing tenants via the startup per-user migration). The
trigger in `services/feed_refresh.py` (`_is_refusal_or_challenge`) was deliberately widened beyond the existing `_is_fetch_refusal`
(403/415/429/503/timeout) to also recognize `bot_challenge.FeedBlockedError` — a small, intentional change to existing browser-UA
escalation too, needed so a Cloudflare-challenge feed gets a browser-UA attempt at all before proxy is even considered. Escalates to
proxy only once browser-UA has already been in play for that feed (either flagged this cycle and its retry also failed, or was already
flagged from an earlier cycle) and it's still failing — flag, retry once same-cycle. `"proxied"` surfaced in Feed Properties alongside
`"browser_ua"`, with a manual Force/Stop-proxying toggle mirroring the existing browser_ua Force/Reset UI exactly (`/feeds/proxy` route
+ `feed-prop-proxy-*` elements). Mode-gated: `_flag_proxy_feed_on_still_blocked` is a no-op outside as_needed (off never wants it,
always doesn't need per-feed tracking).

**Proxy-unreachable auto-fallback shipped** (2026-08-30): a dead proxy backend (gluetun restarting, etc.) must never be worse than not
having one. Detects `pysocks`' `socks.ProxyError` — distinct from the site refusing us, which looks identical at the
`requests.exceptions.ConnectionError` level — via `_exception_chain`, a proper walk of both `__cause__` (explicit `raise ... from e`)
and `__context__` (implicit, set when a new exception is raised inside an except block): verified empirically that the real
requests/urllib3/pysocks chain mixes both styles for a dead-proxy failure, so a `__cause__`-only walk (what `_is_refusal_or_challenge`
originally did too — fixed in the same pass) silently misses it. On detection: `_mark_proxy_unreachable` skips the proxy for that user
for a 5-minute cooldown, across every mode (not just as_needed — `always` needs this even more, since one blip would otherwise fail
every fetch until someone notices), and the current fetch retries once immediately, direct. Verified end-to-end through the real
`feed_refresh_service.update_feeds()` entry point against a dead SOCKS5 port: cooldown marked, direct retry succeeded, entry ingested.

**Multi-backend escalation chain, later.** Final infra handoff 2026-08-30 — all three backends are live now, and Josh has explicitly
left the chain order/wiring to whoever builds it ("Lectio's dev decides when/how to chain them"), so treat the order below as a working
default, not a spec:

| tier | how | notes |
|---|---|---|
| Direct | Lectio's normal fetch | fastest, zero exposure |
| Windscribe (gluetun) | `socks5h://gluetun:1080` or `http://gluetun:8888` | disposable third-party IP, no personal exposure |
| Browserless (headless) | `POST http://browserless:3000/content?token=<shared secret>`, body `{"url": "..."}` | real headless Chrome, clears JS challenges, slower, still no personal exposure (uses the direct VPS IP unless later stacked with a proxy) |
| Tailscale | `socks5h://tailscale:1080` or `http://tailscale:8888` | genuine home residential IP, real exposure cost — last resort |

The Browserless token is a live secret — it is NOT recorded here (this file is git-tracked); it belongs in `.env` as something like
`LECTIO_BROWSERLESS_TOKEN` once actually wired up, same as every other credential in this codebase.

Working-default order (Josh's last stated reasoning, 2026-08-30, before the "dev decides" handoff): direct → browser-UA (existing) →
Browserless (headless) → Windscribe/gluetun → Tailscale (final fallback). Rationale for headless before gluetun: a JS-execution
challenge is a different problem from IP reputation and doesn't need a proxy hop to solve, so it's cheap to try before spending an IP
escalation.

**Stacking Browserless with a proxy is app-level, no infra changes** (confirmed 2026-08-30): pass Chrome's `--proxy-server` as a launch
flag via Browserless's `launch` query param — `?launch={"args":["--proxy-server=socks5://gluetun:1080"]}` (URL- or base64-encoded JSON
per their docs), same mechanism on the REST `/content` endpoint or a raw WebSocket/CDP connection. Swap `gluetun:1080` for
`tailscale:1080` to stack headless rendering with the residential IP instead. Chrome doesn't support SOCKS5 username/password auth, but
that's moot here since neither proxy has auth configured (internal-only on the `proxy` network). So the full combinatorial toolkit —
direct, either proxy alone, Browserless alone, or Browserless stacked with either proxy — is available purely through request
parameters; no docker-compose/container config needed for any combination.

Trust-tier note (Tailscale): a different trust tier from gluetun, not just another endpoint — gluetun/Windscribe exits on a
disposable-ish shared VPS/datacenter NAT IP; Tailscale exits on Josh's actual home Comcast IP, traceable to his house — fine for basic
geoblocking/rate-limiting, wrong for anything sketchy/untrusted or a site aggressive enough to escalate (abuse reports, IP blocklisting)
since that lands on his real connection, which he still needs to use day to day. Being the *last* resort in the chain is itself most of
the answer to "which feeds should this apply to." Also less reliable than gluetun (his home mediaserver blips every couple months,
occasionally days-long) — any code using it must fall through to direct/an earlier tier on proxy-unreachable, never hard-fail the fetch.

Current schema (one URL/one mode) supports none of this yet — `_resolve_proxy_for_fetch`/the request hook need real design work
(multiple backends, no fixed "which backend for which feed" policy needed since order + failure does that job) before it's pluggable.
Not scoped.

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

### Redirecting feeds — 128 candidates ready, awaiting Josh's own `--apply` run

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
are worth offering it. Run inside the container:
`docker compose exec lectio uv run scripts/find_redirecting_feeds.py --user u_40208f374ac18038598b39 --apply`
(re-probes fresh; ~40 min at the default 1s/feed pace). Prior raw results:
`/data/redirecting_feeds_20260825.json` (inside the container).

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

### Read Mode: no Back guard

Read Mode (`GET /read`) still has no equivalent of the main app's Back-button
guard (resume was already fixed 2026-08-25 — separate `lectio-read-last-position`
localStorage key so the main app and Read Mode don't bounce into each other).
Not cheap: `/read` has no drawer for Back to land on, and a Back that visibly
does nothing is worse than one that exits the app. Give Read Mode a
collapsible folder tree first, then add the guard.

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
nothing and ends the noise, cheaper than arguing false-positive each time).

⚠ **A negative lookahead will not clear a redos alert.** CodeQL's regex model
ignores lookaheads, so `(?:-(?!->)[^-]*)*` — measurably linear — was re-flagged
as ambiguous on the first push of #200. Either write the loop lookahead-free or
move the scan out of the regex into Python.

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

### email_batch_queue has the same scope-text-identity fragility rule_uid just fixed elsewhere

Found 2026-08-29 alongside the `youtube_playlist_added` fix (see git history —
`highlight_keywords.rule_uid` now survives a scope-changing edit).
`email_batch_queue` is `UNIQUE(rule_scope, rule_scope_id, rule_keyword,
entry_id)`, same pattern: editing a batch email rule's scope/keyword while
entries are queued orphans them (they never flush). Lower stakes than the
YouTube case — the queue drains on its own schedule rather than accumulating
history, and the failure mode is a dropped/duplicate email, not a
non-idempotent external write. Not fixed — no report of it actually biting
anyone yet; wire it to `rule_uid` if one comes in.

### Add OIDC login

Not scoped. Current auth (`services/users.py`, `/login` at `main.py:21999`)
is username/password only — no SSO/OIDC exists today. Architecture-level
addition (new login flow, session handling alongside the existing one,
tenancy binding from an OIDC subject to a Lectio `user_id`, first-login
provisioning) — wants a real plan before code, not attempted yet.

### Email "full article text" doesn't run Readability on thin-stub feeds

Noticed 2026-08-28 while building the full-text Email Article option. It
only pulls what's already stored (`entry.content`/`entry.summary`) — for a
feed that ships a thin stub body, that's still a thin email even with the
checkbox on, while Readability Mode (the existing extraction used for
Save/re-fetch) can pull the real article from the same feeds. Worth wiring
the checkbox to run that extraction live when the stored body is thin.
Not scoped — needs a real example of a thin-stub feed to test against first.

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

### "Filter this view" — two follow-ups left

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

- **guitarplayer.com's 303 articles** — the site's own subscription is a
  scraped one-article stub (barred as a target), and probing showed many
  article URLs soft-404. **Decision confirmed 2026-08-09: look for/build a
  real guitarplayer feed** rather than leaving them as one-off saves or
  deleting — worth the investigation despite the soft-404s.
- **166 already-converted stars** — tagged entries starred by a since-fixed
  backfill bug. Indistinguishable from a genuine star-and-tag, so they cannot
  be surgically reverted; the unstar-tagged pass is what removes them.

### Combine cross-feed duplicates instead of marking one read

Dedup's only action is "mark the newer copy read". That is destructive, which is why the Safe tier insists on body corroboration, which is why it finds nothing in the folders where duplicates actually pile up. Combining removes the reason for the strictness: a false positive costs an extra link on an entry and a click to split, instead of silently hiding something you wanted.

**Measured 2026-08-18** with `tmp/dedup_experiment.py` (repointed at the per-user DB — it had been surveying the stale legacy one). Library-wide: 101 safe, 13 needs-review. Tech News: **0 safe, 5 review**, every candidate at `body_j = 0.00`, because the folder pairs aggregators against sources and an HN body is `article url: … comments url: … points: 23`. Deals: **zero candidate pairs even across 60k entries** — Reddit deals posts have distinct slugs and human-written titles, and fuzzy cannot rescue it because `cand_pairs` is seeded only from feeds that already share an exact slug or title (`main.py` `_safe_dedup_find_pairs`), so in a folder with no exact match the fuzzy tier never runs at all.

**The behavior.** A duplicate group renders as one entry. The primary is the member with the richest body — not the oldest, which is today's rule and which would keep HN's stub over the real article. The other members appear **in the entry body only, not in the list**: the list shows one ordinary item. Body gets an "Also at" line — `Also at: OSnews` / `Discussion: Hacker News (23 points, 10 comments)`. One unread item; marking it read marks the group; splitting restores the members.

HN's stub body stops being the problem and becomes the feature: `points:` / `# comments:` and the comments URL parse into a real discussion affordance. Josh subscribes to HN for the comments, so an HN link must never be the copy that disappears — combining satisfies that without a per-feed "discussion feed" flag, which was the alternative design and is not needed if nothing is destroyed.

**Matching.** Two tiers, split by what the action costs. Combining accepts the current safe combos plus `{slug,title}` and exact cross-feed title; anything that marks read keeps today's strict rule. Slug alone stays out of both — there is a real false positive in the survey (two different Microsoft stories sharing a slug, `title_j = 0.09`, four days apart).

**Storage.** New meta table for the groups (group id, feed_url, entry_id, role primary/alt). `dedup_false_matches` already records "these two are not the same" and should feed the splitter. Needs the per-user startup migration or existing tenants 500.

**Open.** Whether combining runs as an automation rule, a scan you invoke, or at ingest. Unread counts and the offline outbox both need to agree that a group is one item.

### Cross-feed duplicate scan tier — still not worth building

**RE-MEASURED 2026-08-28.** cross_feed (two legit subscriptions carrying the
same article) shrank 44 → 23 groups, confirming the original "fold into
`/saved/duplicates` as a third tier" plan is even less urgent than before —
23 groups doesn't justify a new UI section any more than 44 did. Not built.

**The bigger finding from that same measurement, saved_vs_real ballooning to
1,028 pairs, was root-caused and cleaned up 2026-08-28 — see git history.**
Not a regression: bulk re-imports (Inoreader resync, Instapaper) insert
saved/starred rows directly via `_apply_migration_items`, bypassing the live
merge event `_move_entry_to_feed` normally rides on. 984 exact-link-match
pairs were merged via `scripts/merge_saved_vs_real_duplicates.py --apply`
(reuses `_move_entry_to_feed` itself, no new merge logic); **23 ambiguous
groups remain, un-merged on purpose** (a same-canon collision spanning more
than one real feed, or similar — the script reports but never guesses at
these). Worth a manual look via `/saved/duplicates` if they're worth
clearing by hand; low urgency otherwise.

### Feed-tag suggestion suppression — do not attempt a third heuristic

**Tried twice and REVERTED (2026-07-29).** Read this before trying again.

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
Resolution shipped 2026-07-29: manual per-(feed, tag) dismissal
(`suppressed_feed_tags`, × on each chip, undo at Feed Properties → *Hidden
tags*) instead of a third heuristic.

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

### Page-weight reduction — follow-ups

- **Entry-pane loading state/timeout** — slow pane loads still look like dead
  clicks.
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
  normalization pass would tidy the spellings and nothing else.
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
- **Article-nav post-swap binder exception.** Mitigated 2026-07-08 (the
  pane-swap catch-all no longer hard-reloads on a post-swap error once the pane
  has already rendered), but the underlying entry-specific binder exception
  still exists somewhere. If it recurs, grab the
  `'[lectio] entry-pane post-swap enhancement failed'` console error to
  identify and fix the actual binder.

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
work.

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

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Doesn't
help the bot-walled feeds above (they're blocked at fetch, before content
matters) but could recover feeds elsewhere that are body-less rather than
blocked.

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
mostly *superseded*: build #1's raw-capture fix (makes the content good) and
auto-file (puts it somewhere sensible), then reassess. Only revisit
page-monitoring if the "re-check the page for changes" half turns out to be
the actual want.

### Backfill already-expired signed lead-image thumbnails — script built, not yet run

**Built 2026-08-28**: `scripts/backfill_expired_deviantart_thumbnails.py`
(dry-run by default, `--apply` to write, `--limit`/`--delay`/`--user`).
Walks every un-pinned wixmp `entry_lead_images` row, calls the same
`_resign_expired_deviantart_url` the article-view path already uses (cheap
checks first, so most rows cost no DeviantArt API call at all), and feeds
the result through `store_entry_lead_image` — which pins it as a side
effect via the existing 2026-08-24 sink. Not run against the live ~22,300-row
backlog yet — needs a dry-run count first to gauge how much of the batch is
actually still resignable (a dead deviation with no fresh URL to fetch just
stays unpinned).

### Tag filtering for firehose feeds — follow-ups

The generic **tag_filter rule** is shipped (rules engine `tag_filter` type;
see ARCHITECTURE "Feed-provided tag suggestions"): include/exclude feed-tag
lists per rule, any scope, auto-mark-read after refresh, dry-run/run-now/
history.

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

### New subscription missing from feed tree — UX idea remaining

Root-cause code bug already fixed (2026-07-08: re-adding a feed that existed
in reader as disabled now calls `enable_feed()`). Remaining idea, not asked
for: auto-disambiguate duplicate display titles (e.g. suffix from the feed
URL path) — the tree tooltip already shows the URL, but identical titles
still invite unsubscribing the wrong feed.

### Global audio player — deferred v2 ideas

Shipped in PR #111 (see git history). Still deferred: queue/playlist of audio
across a folder, remember position per episode, Media Session API (lock-screen /
hardware-key controls), speed presets.

### Uncategorized orphan-feed cleanup — 9 stragglers left (manual)

9 feeds are dead/one-shot/ambiguous (an Instagram post URL, a single Vice
article, cochaser.com (no entries), WebServicesDir, whiskypaint/nolanfa
tumblrs, norfolkwinters, crispian-jago, owenyoung myfeed) — sort or
unsubscribe manually.

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
> scope first (cross-feed scan, and auto-filing which collapses most of them),
> re-measure, and only then ask whether fuzzy is worth its false-positive risk.

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

**Dead code sweep, remaining piece** — the dormant in-app star-mode tree/JS
that the Read Mode hijack bypasses; see "Finish the Instapaper clone" work
(git history) for context.

Other unbuilt cleanups:
- **Deduplicate context-menu open handlers** (Sourcery, PR #193): the entry-pane
  title and post-list item each have their own `contextmenu` listener in
  `static/js/app.js` that populates the same dozen-plus `contextPost*` module
  vars and calls the same `setMenuItemVisible(...)` sequence — two ~40-line
  blocks that have to be kept in sync by hand (PR #193 added its two lines to
  both). A shared `_openPostContextMenu(sourceEl, event)` taking the trigger
  element would read every `data-post-*` attribute and set visibility once.
  `'-1'` as the Uncategorized-folder fallback is scattered the same way (4+
  literal spots) — worth a named constant in the same pass, not on its own.

  **Looked at 2026-08-28, deliberately not attempted.** The two handlers have
  already drifted beyond simple duplication, not just grown apart in shape:
  the post-list handler resets `postClearImgCacheButton` (and sets
  above/below-read, move-visible, playlist/add-tag, the tag-scoped
  remove-tag-shown) while the entry-pane handler never touches several of
  those at all — meaning a naive merge would silently change real behavior
  (e.g. a stale "Clear image cache" item currently can leak into the
  entry-pane menu after a prior list right-click; unifying would fix that as
  a side effect, not as an intended change). This is the single most-used
  interactive path in the app, has zero characterization tests, and this
  session had no working browser (Playwright's Chromium download is
  network-blocked in this sandbox) to verify a JS behavior change against.
  Worth doing with either real browser access or characterization tests
  written first — not as a blind text refactor.

- ~~**Centralize schemeless-URL normalization**~~ — DONE 2026-08-28. New
  `assume_https_if_schemeless()` in main.py replaces the duplicated one-liner
  in `/feeds/discover` and Change URL. The add-feed dialog's own JS keeps its
  separate (stricter, hostname-shape-checked) client-side version — different
  runtime, not worth an API round-trip to share.
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
