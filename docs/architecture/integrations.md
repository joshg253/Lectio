# Integrations

Per-user OAuth destinations, quotas and the automation that drives them.

> Split out of `tenancy.md`'s security list on 2026-08-13 — these are
> integration concerns, not security posture.

## Per-user integrations

- **YouTube quota meter (per-user)** — the Data API exposes no remaining-quota read,
  so Lectio estimates spend itself: each billed call reports its documented unit cost
  through a sink (`playlists.list`/`videos.list`/sub-sync = 1, `playlistItems.insert`/
  `playlists.insert` = 50). `services/youtube_oauth.py` and `services/youtube_sync.py`
  expose `set_quota_sink`; `YouTubeDurationService` takes a `quota_sink`; all three are
  wired to `record_yt_quota_spend`, which tallies units into the per-user `yt_quota_spend`
  table keyed by the **Pacific calendar date** (`_pacific_today()`, Google's reset). The
  Integrations panel shows spent/cap/remaining via `get_yt_quota_status()` (cap =
  `yt_quota_cap` setting, default 10k) with low (<500 left) and exhausted states; an
  actual `quotaExceeded` response snaps the tally to the cap (`mark_yt_quota_exhausted`).
  Tests null the sink (conftest autouse) so a billed call can't pollute another test.
- **Quire destination (per-user)** — another per-user OAuth outbound destination
  (`services/quire.py`, same shape as DeviantArt: `/quire/connect` → `/quire/callback`
  store tokens; `get_quire_user_token` refreshes on expiry). A user picks one default
  project (`quire_project_oid`); the `Add to Quire` entry button (`/entries/quire`),
  On-Star, and the `quire` automation rule (`_run_quire_rules_after_refresh`) all create
  a task in it. Unlike YouTube's **daily** quota, Quire rate-limits **per organization by
  minute and hour** with no remaining-quota read, so the meter is a **sliding window**:
  each billed call logs a row into `quire_call_log` (pruned to the last hour) via the
  `set_usage_sink` → `record_quire_call` sink; `get_quire_usage_status()` reports
  minute/hour usage vs the `quire_rate_cap_min`/`_hour` caps with low (≥80%) and blocked
  (≥cap) states. The caps are **auto-detected from the destination project's organization
  plan** (`detect_quire_plan_and_caps` → `GET /project/{oid}` returns `subscription.plan`;
  `PLAN_RATE_CAPS` maps Free 50/200, Professional 300/1250, Premium 1000/5000; Enterprise
  scales with members so it keeps the default), run on a background thread whenever the
  chosen project changes; the detected plan name is shown in the meter. Automation runs check the meter before each add,
  honor a per-run cap (`_QUIRE_AUTO_PER_RUN_CAP`), and back off on a 429 (`Retry-After`).
  The far-right entry-header **share-dropdown consolidation** of all destinations is a
  deferred follow-up (see Plan.md); for now Quire is a standalone glyph beside the others.
- **Hide Shorts (global, per-user)** — Shorts are auto-marked read at refresh by the
  hide-shorts pass in `_run_automation_after_refresh`. Per-feed it reads the
  `feed_display_prefs.hide_shorts` pref; the `yt_hide_shorts_global` setting
  (`youtube_hide_shorts_global()`, Integrations toggle, off by default) additionally
  targets **every** refreshed YouTube feed (URL contains `youtube.com/feeds/videos.xml`)
  regardless of the per-feed pref — one source of truth, no drift as feeds come/go via
  sync. A Short is detected by `/shorts/` in the entry link (`_is_youtube_short`).
- **YouTube embed host (per-user)** — both `youtube.com` and `youtube-nocookie.com`
  are allowlisted; which one a YouTube *embed* uses is the viewer's choice, applied
  at **render** (not ingest, since sanitization bakes content into each user's
  reader DB). `youtube_embed_host()` reads the per-user `yt_embed_account_features`
  setting (default off → privacy-enhanced `youtube-nocookie.com`). The recovered/
  injected player (`_youtube_embed_html`) builds with that host directly;
  feed-native embeds are rewritten by `_apply_youtube_embed_host` in the entry-detail
  pipeline (iframe `/embed/` URLs only — plain watch links are untouched). Opting in
  (Integrations → YouTube) switches to the standard host so the player exposes Share /
  Watch Later, which need the viewer's signed-in YouTube cookies that `-nocookie`
  blocks. Render-time application makes the toggle instant and retroactive.
- **YouTube Add-to-Playlist (per-user OAuth)** — the embed player only exposes
  Watch Later, so a real playlist picker needs the **YouTube Data API v3** with a
  write scope (`auth/youtube`), separate from the read-only `YOUTUBE_API_KEY`
  (durations, sub-sync). `services/youtube_oauth.py` speaks HTTP to Google
  (authorize / token exchange / refresh, `playlists.list`, `playlistItems.insert`,
  `playlists.insert`); main.py owns the flow (`/integrations/youtube/oauth/{connect,
  callback,disconnect}`) and stores the **refresh token per-user** in app-settings
  (`get_youtube_oauth_token()` refreshes on demand, returns "" → reconnect prompt on
  failure). The OAuth *client* creds are app-level (one registered Google app, read
  from env in both single and multi mode) — only the resulting tokens are per-user,
  so accounts are never shared. The redirect URI is fixed at
  `/integrations/youtube/oauth/callback` to match the Google client registration.
  Client-side, `enhanceYoutubeEmbeds()` injects an "Add to playlist" control beneath
  each YT iframe (video id parsed from the `/embed/` src); it lazily fetches
  `/api/youtube/playlists` (cached per page session). The playlist menu is positioned
  `fixed` at open so it escapes the article pane's `overflow:auto` clipping, flipping
  upward near the viewport bottom. `quotaExceeded` surfaces as a
  distinct 429 so the UI can tell the user to fall back to manual add on youtube.com
  (default 10k units/day ≈ 200 inserts; resets midnight Pacific). The OAuth app stays
  in **Testing** mode (refresh tokens expire ~7 days → occasional reconnect).
- **Auto add-to-playlist automation** (`youtube_playlist` rule type) — builds on the
  OAuth integration above to add new entries' videos to a playlist at refresh time.
### Rule keywords: one matcher, comma-separated OR terms

`build_keyword_matcher(keyword, is_regex)` is the single text→bool builder behind
the dry-run, Run Now and after-refresh paths, which each carried their own copy
and could drift. In regex mode the keyword compiles as written. In plain mode it
splits on commas into OR'd terms, each still a case-insensitive **substring**
test: multiple terms previously meant hand-writing a regex, which is where the
boundary traps live (`Apple|AirPods|iPhone|MacBook` matched *Grapplers* and *Dole
Pineapple Tidbits* in a deals folder — `\b(Apple|…)` with a **leading** boundary
only is the fix, since a trailing one also drops `iPhones` and `AppleTV`).

Plain terms also fold **curly punctuation** (`’ ‘ “ ” → ' "`, plus the
non-breaking spaces) and lowercase, on both sides of the compare: publishers
spell the same apostrophe two ways — 2,773 stored titles use the typographic one,
6,761 the ASCII one — and the reader cannot see which a given title used. Regex
mode is never folded; a pattern says what it says.

The split deliberately does **not** imply a word boundary: measured across the
live library, 4 plain rules match only *inside* words and would stop matching
entirely. A term containing a literal comma needs regex mode.

**Rules that could be one rule** (`find_mergeable_rule_groups`,
`merge_highlight_rule_group`, `GET /highlights/suggestions`,
`POST /highlights/merge-group`). A keyword was one term until comma lists
landed above, so multiple single-keyword rules sharing everything else
accumulated from before that existed. The identity that makes rules
mergeable is `(type, scope, scope_id, search_in, is_regex)` — two rules
differing only in `keyword` collapse into one, joined as a comma list (plain)
or `(a)|(b)` alternation (regex). Scoped to exactly `highlight` and
`mark_as_read` (`_MERGEABLE_RULE_TYPES`): `deduplicate`'s keyword is a
match-method enum, `tag_filter`'s is a +/-tag spec (already merged by its own
`_merge_tag_filter_specs`, folded on add rather than offered as a suggestion),
and the optional-keyword action types (`youtube_playlist`, `instapaper`,
`quire`, `save_article`) aren't confirmed to share the same OR-of-terms
semantics when non-blank.

**A same-identity group is not automatically the same rule.** Two rules can
share type/scope/search_in/is_regex and still mean different things —
measured live 2026-08-19: a folder-9 `highlight` group mixed `blue` and
`green`, a global group mixed `blue` and `orange`. Merging would have to pick
a side silently. `_MERGE_IDENTITY_FIELDS` (`color`, `delivery`, `email_to`,
`batch_time`, `batch_count`, `cc_me`) extends the grouping key: within an
identity group, `find_mergeable_rule_groups` further splits rows by this
settings tuple and offers each settings-consistent subgroup of 2+ as its own
independent `mergeable` entry. Raised 2026-08-31 by Josh against a real
5-rule group (orange/blue/blue/green/orange) that used to come back as one
big `mismatched` blob: it now offers the orange pair and the blue pair as two
separate merges, leaving the true singleton (green, no partner) unreported —
same as a genuinely solo identity group always has been. `mismatched` now
means only the narrower thing left over: 2+ settings-distinct singletons
under one identity that still disagree once every already-agreeing pair has
been pulled out. Suggestion-with-preview, never automatic (decided
2026-08-24): nothing merges without a click, matching how the duplicate-feed
scans already behave.

`merge_highlight_rule_group` re-derives the current matching rows from the
identity tuple **and** the specific settings values passed in (not just
identity+is_regex) rather than trusting a client-supplied row list, so a
stale preview (a rule removed or edited since the page loaded) fails closed —
returns `None` (409 at the route) instead of merging the wrong things. The
settings values are part of the match now, not a same-group precondition
checked after the fact: an identity key can hold several settings-distinct
subgroups, so a merge request has to say which one it means (the client reads
it off the first rule in the subgroup card it's acting on) rather than
letting the query silently gather every row sharing identity regardless of
settings.

**The identity's `is_regex` term means a plain rule sitting next to a regex rule on
the same scope was invisible to all of the above (raised 2026-08-31).**
`find_regex_convertible_rule_groups` / `merge_regex_convertible_rule_group` /
`POST /highlights/merge-group-regex-convert` cover exactly that boundary: group by
`(type, scope, scope_id, search_in)` **without** `is_regex`, keep only groups that
actually span both a regex and a plain rule (a same-`is_regex` group is the
function above's job, not this one's), apply the same `_MERGE_IDENTITY_FIELDS`
mismatch guard, and — on merge — `re.escape()` each plain keyword before joining as
`(a)|(b)` alternation, same as the regex path. Confirmed live 2026-08-31 on
`("Lowe's", plain)` next to `("AirPods|iPhone|MacBook|AppleTV", regex)`: `re.escape`
leaves the apostrophe alone (not a regex metacharacter, and not escaped by Python's
`re.escape` since 3.7 dropped over-escaping), so the merged pattern
(`(Lowe's)|(AirPods|iPhone|MacBook|AppleTV)`) matches exactly what the plain rule
already matched — Josh's own hesitation about hand-converting it to regex (unsure
how to escape the apostrophe) turned out to be a non-issue, but the one-click
conversion means nobody has to work that out by hand anyway. Same run turned up a
second real case on a feedburner feed rule mixing plain `iPhone` with regex
`Apple|iOS|iPadOS`, confirming this isn't a one-off shape.

**A feed rule already covered by a folder rule** (`find_redundant_feed_rules`)
is the secondary case: a feed-scoped rule whose keyword set is a full subset
of a same-type folder rule's on a folder the feed belongs to does nothing —
the folder rule already catches everything it would. Plain rules only (a
regex's language isn't decidable as a subset this way). Flagged for removal
via the existing `/highlights/remove`, not a new endpoint — removing one rule
was already a solved problem.

  It's a **general** automation rule (any feed/folder scope, via the shared
  `highlight_keywords` table + after-refresh pass), not YT-folder-bound, because a
  YouTube video can be embedded in any feed's article and an entry can carry several.
  `_run_youtube_playlist_rules_after_refresh` runs last in `_run_automation_after_refresh`
  (after mark_as_read so its own "mark read after add" doesn't fight an earlier rule):
  for each new (within the 15-min cutoff) matching entry it extracts **all** video ids
  from the entry link + content (`youtube_embeds.video_ids_in_text`, which also matches
  `/shorts/`), inserts each via `playlistItems.insert`, and optionally marks the post
  read. The rule's keyword is an **optional filter** — empty = every new video in
  scope. Per-rule columns on `highlight_keywords`: `yt_playlist_id`,
  `yt_playlist_title`, `yt_include_shorts` (default off — Shorts detected by the
  `/shorts/` link), `yt_mark_read` (default on), and `yt_min_minutes`/`yt_max_minutes`
  (0 = no limit). The **duration filter** reuses the cached video length (the same
  store behind the `[duration]` title prefix), so it needs the `YOUTUBE_API_KEY`; a
  video whose duration isn't cached yet is skipped that run and retried once it is.
  Durations are fetched in **batches of 50 ids per `videos.list` call** — that endpoint
  bills **1 quota unit per call, not per video**, so a large subscription set (10k+
  videos) costs ~200 units instead of ~10k. (Per-video fetching previously blew the
  10k/day quota, leaving a rotating ~13% of videos perpetually duration-less; ids the
  API returns no item for stay NULL and are retried per the negative-retry window.)
  Because `playlistItems.insert` is
  **not idempotent**, a `youtube_playlist_added (scope, scope_id, keyword, entry_id,
  video_id)` table is the dedup guard: each (rule, entry, video) row is claimed with
  `INSERT OR IGNORE` *before* the API call (rowcount 0 → already added → skip), and
  released on failure/quota so it retries next run. A per-run cap
  (`_YT_PLAYLIST_AUTO_PER_RUN_CAP = 25`, ≈1250 units) keeps a burst of new uploads
  from exhausting the daily quota; `quotaExceeded` stops the run. The rule-type option
  is gated on `yt_oauth_connected` (server-side in `/highlights/add`; hidden in the
  rule-builder until connected) so it can't be created without a token. Runs in the
  per-user background context like the other after-refresh rules.

  **The rule stays general-purpose; only the editor's default changed (2026-08-30).**
  The paragraph above still holds — the engine matches any feed/folder scope, because
  a YouTube video can be embedded in an article from any feed. But the rule-builder's
  feed picker used to default to showing every folder's feeds regardless, which made
  finding your actual YouTube channel feeds (the common case for this rule type) hard
  once a library has more than a handful of folders. Switching the type dropdown to
  "Add to YT Playlist" now snaps the picker to `window.YT_FOLDER_ID` (a page-wide
  global, main.py's home-route context — has to be correct on first paint rather than
  fetched lazily from Settings, same reasoning as the neighboring `is_yt_folder` flag).
  The folder dropdown itself is untouched, so the general-purpose case — scoping the
  rule to some other feed that happens to embed videos — is still one manual
  re-selection away, not removed.

  **Manual bulk add (`/api/youtube/playlists/add-batch`) is a separate action from
  the rule above** — the post list's multi-selection "Add to YouTube Playlist…" — and
  runs as a background job the client polls, not one blocking request (fixed
  2026-08-30). It used to hold one HTTP request open for the whole batch (an
  existing-contents fetch plus one YouTube API call per video), which on a 50-video
  selection blocked silently for the better part of a minute with no feedback at all.
  Same shape as the refetch-scope status pill: the route validates, seeds a per-user
  job dict (`_yt_playlist_batch_jobs`, one job at a time — a second concurrent batch
  would double the API call rate against the same daily quota and could race the
  duplicate check), and hands the actual work to `_run_yt_playlist_batch_add` in a
  background thread (`_run_in_user_context`, since a bare thread loses tenancy
  binding); `GET .../add-batch/status` reports `phase` (`checking_existing` →
  `adding` → `done`), `processed`/`total`, and running counts, polled every 900ms to
  drive a live-updating toast rather than a silent wait.

  **Auto-marks read on completion (2026-08-31)**, so a right-click -> Mark as read
  on the same selection isn't a required second step. The job also tracks
  `ok_video_ids` — videos that ended up either newly-added or already-on-the-playlist
  — and the client marks read only the posts whose video landed in that set; a video
  that failed, or was never reached because the run stopped on quota, is deliberately
  left unread. Bulk "Add tag" does the same unconditionally (tagging implies
  filing/keeping it, and that route either tags every entry or reports one shared
  error — no partial-failure case to exclude).

  **Two bugs found live (2026-08-31), on a real 50-video batch that hit the daily
  quota partway through:**
  - `services/youtube_oauth._raise_for_quota` used to substring-match `"quotaExceeded"`
    against `resp.text[:300]` — but Google's real 403 body repeats a verbose,
    HTML-linked message at both the top level and inside `errors[0]` *before* the
    `reason` field, long enough to push it past 300 chars. Missed there, the batch
    worker treated every remaining video as an ordinary per-video failure instead of
    recognizing quota exhaustion and stopping cleanly — it just kept burning through
    the rest of the selection, each insert individually 403ing. Fixed by parsing the
    full JSON body's `error.errors[].reason` instead of truncated text, and folding in
    `dailyLimitExceeded`/`userRateLimitExceeded`/`rateLimitExceeded` alongside
    `quotaExceeded` (the intermixed 200s/403s in the batch that surfaced this look more
    like a per-second rate limit than a hard daily wall, but both call for the same
    stop-and-preserve-partial-progress handling).
  - The auto-mark-read step only ever ran from the client-side poll loop
    (`_ytPollBatchAddProgress`) started the moment the batch kicked off — but changing
    folders is a real page reload in this server-rendered app (no client-side
    routing), which kills that poll loop along with everything else on the page. The
    background job itself is unaffected (it's a server thread, not tied to the
    request), so the adds kept happening, but nothing was left to mark the results
    read once nothing was polling. `_ytResumeBatchJobOnLoad()` now runs once on every
    page load (gated on the YT account-features flag): it re-fetches the tracked job,
    and if it's still running or finished-but-unconsumed, rebuilds a `posts` array
    from the current page's `.post-item[data-post-video-id]` elements and either
    resumes the same live poller or runs the shared finished-job handler
    (`_ytHandleFinishedBatchJob`, extracted out of the poll loop for this reuse)
    directly. A post that isn't in the current view (a different folder) just can't be
    matched and is left as-is — no worse than before this existed. A `localStorage`
    flag (`lectio-yt-batch-consumed`, keyed by job id) stops a finished job from
    re-showing its toast and re-running the (harmless but pointless) mark-read call on
    every subsequent page load.
- **Save to Pinterest (per-user OAuth)** — an outbound-only integration: a per-entry
  **Pin** button saves an article to one of the user's boards. Pinterest has no
  write-without-OAuth path, so `services/pinterest_oauth.py` speaks the **API v5**
  OAuth flow (authorize / token / refresh — the token endpoint authenticates the
  *client* with HTTP **Basic** auth, body form-encoded, unlike Google's JSON) plus
  `boards.list` (scope `boards:read`) and `pins.create` (scope `pins:write`). main.py
  owns the routes (`/integrations/pinterest/oauth/{connect,callback,disconnect}`,
  `/api/pinterest/boards`, `/api/pinterest/pin`) and stores the **refresh token
  per-user** (`get_pinterest_oauth_token()` refreshes on demand; "" → reconnect). The
  OAuth *client* creds are app-level (`PINTEREST_OAUTH_CLIENT_ID/SECRET` from env,
  both modes); only the tokens are per-user. The pin route derives the entry's lead
  image via `_derive_article_lead_image` (the **source** URL, not the `/api/img`
  proxy, since Pinterest must fetch it) and links the pin back to the entry; an entry
  with no image returns 422 (Pinterest requires an image). The Pin button is rendered
  only when connected (`pinterest_connected` context flag); the board picker is a
  lightweight client-side menu fed by `/api/pinterest/boards`.
- **Rule scope (incl. multi-feed and multi-folder)** — automation rules scope to
  `global` (all feeds), `folder` (one id), `folders` (an explicit set of folder ids —
  requested by Josh 2026-08-31, shipped 2026-09-02, `scope_id` is the ids joined by
  newline), `feed` (one URL), or `feeds` (an explicit set of URLs, also newline-joined
  — newline, not comma, since URLs can contain commas). Scope resolution is
  centralized so every runner agrees: `resolve_rule_feed_urls(conn, scope, scope_id)`
  returns the feed set (or `None` for global) for the bulk/dry-run paths, and
  `feed_in_rule_scope(scope, scope_id, feed_url, folder_feed_urls)` is the per-feed
  test the after-refresh runners use against each freshly-refreshed feed. `folder` and
  `folders` share one prewarm/lookup path — `rule_scope_folder_ids(scope, scope_id)`
  returns the folder id(s) either scope references (used to build the per-run
  `folder_id -> feed set` prewarm map once per refresh tick) and
  `rule_scope_folder_feed_set(scope, scope_id, folder_feed_map)` unions the prefetched
  sets for `feed_in_rule_scope`'s per-feed test; `feeds`/`feed`/`global` don't need
  either. Deduplicate accepts `global`/`folder`/`folders`/`feeds` (the latter two
  dedupe across a selected set, resolved via `_resolve_dedup_feed_urls`) but rejects a
  single `feed` — one feed can't cross-dedupe. The rule builder derives scope from two
  independent pickers: a feed multi-select listbox (0 selected = fall through to
  folder scope, 1 = `feed`, 2+ = `feeds`) and, when no feeds are picked, a primary
  folder `<select>` plus a separate "+ another folder" chips picker (0 folders =
  global, 1 = `folder`, 2+ = `folders`) — kept as two pickers rather than converting
  the folder `<select>` itself to multi-select, since that select also drives the feed
  picker's candidate pool and the YouTube-playlist auto-scope logic elsewhere in
  app.js and a multi-value rewrite of every one of those read sites was a much
  bigger, riskier change than the feature needed.
