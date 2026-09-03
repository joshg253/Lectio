# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Grouped and re-prioritized 2026-08-30 (was a flat rough ordering before). Five tiers: things
actively impeding unread-clearing right now, small independent wins, ready-to-run maintenance,
real features that aren't blocking anything today, and deliberately-deferred big investments.
Within a tier, related items are clustered under a bold sub-heading; unrelated items stand alone.
Two standing watch-lists (CodeQL, Parked) moved to their own section at the end — nothing in them
is scheduled, they're just what to check if a related symptom recurs.

## Tier 1 — actively impeding unread-clearing

**Refresh-contention latency** — home-route stalls, post-refresh read-range slowness, the GIL-contention tally, and the post-restart startup flood are all the same investigation now; merged into one item below.

### Refresh-contention latency (home route, Read Above/Below, and the GIL tally — merged 2026-08-30)

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
refresh's own DB work blocking readers.

**Caught live 2026-08-30**: Josh reported `GET /?folder_id=1&read_filter=unread` taking ~10s; the
log confirms `11124ms` total. The `[perf]` ticks pin the gap down further than before:
`meta_block=263ms` + `tag_block=126ms` finished at 18:18:56.0, then nothing logged until
`list_entries` fetch starts at ~18:19:03.1 (`posts_block=2541ms` total, of which `fetch_ms=1454` +
`filter/enrich/process≈360ms` are accounted for) — **roughly 6.2s of the 11.1s sits between
tag_block finishing and posts_block's own work starting**, still with no timing of its own, plus
another ~2s after posts_block before the response is logged as sent. A refresh pass was confirmed
running throughout — dozens of concurrent `httpx` feed fetches logged in the same window, dev.to
and DeviantArt integration calls included. So the region to instrument is now narrower: not
"between tag_block and posts_block" generally, but specifically **whatever runs immediately after
tag_block and before `list_entries`'s own fetch begins** — add a tick there before theorizing
further.

**Instrumented and measured live, 2026-09-01, right after a restart with refresh/FlareSolverr churn
running.** New `[perf] home: gap_block=%dms (badges=%dms title_map=%dms rest=%dms)` log line
(main.py, right before `posts_start`) confirms the theory cleanly: across five captured stalls
(2.1s-9.7s gap_block), `badges` accounted for 98-100% of the gap every time; `title_map` and the
pure-Python `rest` (folder-tree loop, inactive-feed sort) stayed at 0-6ms even on the worst request.
Rules out `get_feed_title_map` and the folder loop entirely — the "already fixed to lazy" comment
there holds up.

`badges` wraps `get_saved_unread_count()` / `get_saved_counts_by_folder()` / `get_starred_inbox_total()`.
Only the first touches the reader DB (a raw `db.execute(...)` on `reader._storage.get_db()`,
main.py:15789) — the other two are meta-DB-only. That raw reader query is the prime suspect for
the actual lock wait.

**But it is not confined to that one call.** In the same capture window, `meta_block` (app's own
meta DB, normally 7-80ms) spiked to 1730ms and 2391ms, and `posts_block` (which calls
`list_entries_for_feeds`, a different reader-DB path) spiked to 7325ms — both independent of
`get_saved_unread_count`. So the mechanism is not "one slow query to optimize" — it's *whichever*
query happens to be mid-flight when the refresh thread commits a write that gets stuck behind a
lock. This matches the busy_timeout-stacking theory already floated for the Read Above/Below WAL
item above; the two are very likely the same root cause.

**Next, before any fix:** confirm this is actually SQLite lock-wait (vs. some other serialization,
e.g. a Python-level lock/GIL artifact) by instrumenting the busy-wait itself — reader's and the
meta connection's configured `busy_timeout`/`timeout=` values, and ideally a `sqlite3_trace`/
`PRAGMA busy_timeout` check plus logging actual `SQLITE_BUSY` retries if reader exposes them. If
confirmed, the fix is likely on the WAL/checkpoint or refresh-transaction-granularity side (per the
Read Above/Below item's lead), not a rewrite of `get_saved_unread_count` — needs a plan before
touching anything, this is a shared-connection-behavior change, not a local one.

**Confirmed 2026-09-03, conclusively.** Both `busy_timeout`s were already 10000ms
(`get_meta_connection`/`_LectioReaderStorage.setup_db`, main.py / services/reader_api.py) — that
alone was suggestive (every observed stall fell under that 10s ceiling) but not proof. Shipped real
instrumentation instead of inferring further: both connection classes now wall-clock-time every
statement and log the slow ones with their SQL text (`_TimedMetaConnection` in main.py,
`_TimedConnection` in services/reader_api.py, hooked in via reader's own `CONNECTION_CLS` extension
point — the same one it uses for its `READER_DEBUG_STORAGE` debug mode). First live capture, during
an ordinary refresh pass:

| elapsed | db | statement |
|---|---|---|
| 7939ms | meta | `INSERT INTO entry_lead_images (...) ON CONFLICT ... DO UPDATE ...` (one row) |
| 5837ms | meta | `INSERT INTO yt_quota_spend (day, units) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET units = units + excluded.units` (one row) |
| 5837ms | meta | the same `entry_lead_images` upsert, same instant, identical elapsed time |
| 1744ms / 725ms / 614ms | reader | the home-route `list_entries_for_feeds` `WITH ids AS (...) ORDER BY recent_sort DESC` read |

A single-row indexed upsert cannot take 5-8 real seconds of work — this is `SQLITE_BUSY` retry,
full stop, and it hits both the meta DB and the reader DB. The two `entry_lead_images`/
`yt_quota_spend` writes logging the *identical* elapsed time in the same instant is the tell for the
mechanism, not just the symptom: this is **writer-vs-writer queueing**, not one long transaction
blocking everyone. `get_meta_connection()` hands every thread its own persistent connection
(refresh, the lead-image backfill, the lead-image async write-worker, YouTube quota tracking,
foreground requests that write settings/badges) and SQLite allows exactly one writer at a time even
in WAL mode — during an active refresh pass touching hundreds of feeds, several of these threads are
committing small writes in the same window and stack up behind each other's `busy_timeout` waits.

`services/lead_images.py`'s `fetch_and_store_lead_images_for_feed` is the biggest contributor by
volume: it calls `store_entry_lead_image` (its own `with get_meta_connection() as conn:` block, one
INSERT, commits on exit) from 12 different call sites in a per-entry loop, so a feed with many
qualifying (unread/saved/tagged) entries acquires-and-releases the meta writer lock once per entry
instead of once per feed. Batching those writes — accumulate results in memory while the loop's
existing `time.sleep(0.05/0.15)` politeness delays and network fetches run, then one `executemany`
in a single transaction per feed (or per some chunk size) — would cut the number of separate lock
acquisitions from this one source by whatever the average per-feed entry count is, directly
reducing contention without touching WAL/checkpoint settings or any read path.

**Shipped 2026-09-03**: `store_entry_lead_image` takes an optional `batch` list (append instead of
commit-immediately; cache update and the thumb-pin sink stay synchronous either way -- see
`docs/architecture/images.md` "Batched meta-DB writes during the per-feed backfill" for the full
mechanism), and `fetch_and_store_lead_images_for_feed` now flushes one `executemany` every 25
entries plus once more in a `finally` around the loop, so an early return or exception mid-feed
can't drop already-buffered rows -- only the last partial chunk (<=24 entries) is at risk on a hard
crash, versus none before. Two new tests cover it: batching actually reduces meta-connection opens
(30 entries -> well under 15), and a mid-loop exception still flushes what was buffered before it.
Full suite green (3914).

**Confirmed live 2026-09-03, partial win.** Josh's own report after living with the deploy: "perf
seems better, still a few seconds delay when switching between folders." A 6-hour log capture from
the deployed container backs that up on both sides. Wins: only 4 `slow_sql db=meta` events (>250ms)
in 6 hours, versus the near-continuous stream implied by the pre-fix capture above -- the dominant
per-entry `entry_lead_images` write from the per-feed backfill loop is no longer the routine
offender. Remaining: those 4 events are still real multi-second stalls (8542ms/3735ms/3335ms/3734ms),
and `get_meta_structure_snapshot` (the folder/feed-tree cache every home render reads, `main.py:6129`)
spiked to 3010ms and 5502ms in the same window when its cache was cold -- confirming the broader
"whichever query is mid-flight when a writer holds the lock" theory rather than one fixable query.
The 4 slow writes, by source:

| elapsed | statement | source |
|---|---|---|
| 8542ms, 3735ms | `entry_lead_images` alt/title UPSERT | `store_entry_image_alt`, called from `_maybe_store_alt_from_cache` -- 2 of the just-batched loop's own branches, at the time still un-batched |
| 3734ms | `entry_lead_images` image_url/fetched_at UPSERT, single-row shape | NOT the new `executemany` flush (its own `.execute` isn't even wrapped by the slow-SQL instrumentation, a gap in itself) -- one of the still-unbatched call sites: `_do_backfill_entry_list` (render-triggered chunk backfill, `services/lead_images.py`) or `persist_lead_image_async`'s single-writer background queue |
| 3335ms | `entry_read_state` UPSERT | a completely different subsystem -- the post-refresh automation pipeline's mark-as-read writes (`main.py:7024-7908` range, per-entry commits same shape as the lead-image bug) or an ordinary user read-toggle caught mid-lock |

Three candidate next steps were sized, ranked by how directly they extend what's already proven vs.
how much new ground they cover; Josh picked #1.

1. **Shipped 2026-09-03, same session.** `store_entry_image_alt` now takes the same optional `batch`
   param and its own `_flush_pending_image_alts`, flushed at the same 25-entry/`finally` points as
   the lead-image batch but as an independent list (see `docs/architecture/images.md`'s "Batched
   meta-DB writes" for the full writeup). The boilerplate-title read/clear stays synchronous on
   purpose -- it touches *other* entries' rows, not the one being written. Two new tests: 30 entries
   with unique captions produce exactly 2 flushes per stream (not one commit per entry), and both the
   URL and alt/title land correctly. Full suite green (3915). **Confirmed live 2026-09-03** --
   Josh, informally, after living with the deploy: "it's definitely feeling more responsive." No
   fresh log capture taken yet to confirm the alt-write stalls specifically stopped recurring.
2. **Shipped 2026-09-03, same day.** `_do_backfill_entry_list` (the render-triggered chunk backfill
   -- fires on ordinary browsing whenever a rendered page has visible entries with no cached
   thumbnail, not just during refresh) batches the same way: same `pending`/`pending_alts` shape,
   same 25-entry/`finally` flush points, now wrapping its `for feed_url, entry_pairs in
   by_feed.items(): for entry_id, entry_link in entry_pairs:` double loop so a chunk spanning
   several feeds pays for a handful of `executemany` calls instead of one commit per visible
   thumbnail. Two new tests mirror the per-feed ones (30 entries across 3 feeds in one call ->
   exactly 2 flushes; an exception partway through one feed's group still persists an earlier
   feed's already-buffered writes). Full suite green (3917). **Not yet confirmed live** -- this
   path's concurrency profile (ordinary browsing, not refresh) hasn't been captured yet.
3. **Shipped 2026-09-03, same day -- root cause found, not just "investigated."** Every OTHER
   bulk mark-as-read path in main.py (`_mark_existing_shorts_read`, `_run_now_pattern`,
   `_suppress_guid_churn`, the merge/undo/read-batch routes -- 8+ call sites) already collects
   `(feed_url, entry_id)` pairs into a list and does one `conn.executemany` at the end. Exactly one
   didn't: `_apply_hide_paywalled` (`main.py:8198`) called `upsert_entry_read_state` -- a single-row
   `.execute()`, one meta-connection use per call -- inline inside its `for feed_u in targets: for
   entry in reader.get_entries(...):` loop, the identical shape to the lead-image bug, just never
   caught because its sibling `_apply_hide_shorts` was written correctly and nobody compared them.
   A feed with many paywalled stubs (first time the pref is enabled, or a feed that publishes mostly
   stubs) would commit once per stub during an active refresh pass. Fixed to match its sibling's
   shape exactly: collect `to_mark` during the scan, `reader.mark_entry_as_read` per entry after (the
   reader-DB side, unavoidably per-entry -- reader has no bulk API here), then one `executemany` for
   the meta-DB side. 3 new tests (`tests/integration/test_hide_paywalled.py`): off leaves stubs
   unread, on marks them read and persists `entry_read_state`, and 10 stub entries produce ≤2
   meta-connection uses (the targets read + the one write) instead of 10. Full suite green (3920).
   **Not yet confirmed live** -- needs a feed with a real paywalled backlog to trigger it, which is
   rarer than the lead-image/alt paths (only fires once per newly-stubbed feed, not every refresh).

**Also shipped 2026-09-03**: `_TimedMetaConnection`/`_TimedMetaCursor` (main.py) and
`_TimedConnection`/`_TimedCursor` (services/reader_api.py) now wrap `executemany` the same way they
already wrapped `execute` -- closes the visibility gap flagged above. All four batched-write flush
points from this investigation (lead image, alt/title, chunk-backfill's copies of both, and now
hide-paywalled) are visible to slow-SQL logging going forward; verified manually that a slow
`executemany` logs with elapsed time and SQL text, same as `execute` always has.

**A second capture, same day, sharpens it further.** `GET /?folder_id=11&read_filter=unread` (a
tiny folder — 16 feeds, 3 entries) logged `5498ms`, and its own follow-up chunk request logged
`7075ms`, both while the same refresh pass was still running (a `minecraft.net` parse error and the
DeviantArt/dev.to integration calls landed seconds before, in the log right above). Real work in
both requests was trivial — `meta_block` 68-74ms, `tag_block` 0-17ms, `list_entries` fetch 30-38ms,
process ~60-70ms — under 200ms combined against 5.5-7s totals. The gap is the same
tag_block-to-list_entries-fetch region as the folder_id=1 capture, this time ~5s and ~6.5s on
requests doing almost no real work. **That the gap doesn't scale with folder size (16 feeds/3
entries vs. 2,181 feeds/250 entries in the first capture, same few-second gap either way) rules out
per-folder query cost and points at something shared** — a lock or connection contended with the
concurrently-running refresh thread is now the leading suspect over pure GIL/CPU contention.
Whatever runs in that gap is almost certainly a blocking DB call; reader's own `busy_timeout` has
already needed tuning once for a concurrent-writer flakiness issue (see the flaky-CI fix under
Saved articles in git history) — a busy_timeout retry stacking up while refresh holds a write lock
would explain a several-second stall with no timing of its own exactly like this.

**Josh's ask (2026-08-30):** the VPS has 4 vCores — can background refresh just not eat all of them
while someone's actively using the app? Cheapest lever: lower the refresh thread's OS niceness
(`os.setpriority`, Linux-only) once at thread start. That fits "only when there's obvious user
activity" for free, with no activity-detection code needed — niceness only matters once a core is
actually contended, i.e. exactly when a foreground request and refresh are both runnable at once;
idle, refresh still gets full throughput. Caveat: it only addresses CPU-scheduling contention. The
two captures just above look more like a DB lock/busy_timeout stall than CPU starvation, so
niceness is a good complementary fix, not a proven fix for the multi-second numbers actually
measured here — the DB-lock lead above should still be chased first.

**Same shape, worse, right after a restart (2026-08-30).** Josh reported the app taking "a minute
or more" to feel usable after a container restart. `/healthz` itself responds in ~15-20s (Docker's
health check passes fast), but the log shows a startup flood immediately after: a one-time per-user
scraped-feed backfill, a YouTube-video recheck, a starred-archive orphan sweep (skipped ~8,500
rows), then the first scheduled refresh pass hitting a large batch of feeds essentially all at once
— everything is simultaneously "due" right after a restart instead of trickling in on its normal
cadence. Not independently measured (this observation landed during an unrelated rebuild mishap, so
the timeline is muddied — see git history same day), but it is consistent with, and probably an
amplified case of, the same contention this whole item is chasing. Worth re-measuring cleanly once
the DB-lock lead above is understood, rather than treating it as a separate bug.

Also corrected while chasing this: refresh is **not** a thread pool. It calls
`reader.update_feed()` sequentially in one background thread, so the contention
is one CPU-hungry thread, not many.


**The GIL-contention tally that used to be its own item** — same phenomenon, folded in here rather than tracked separately. Add a line each time a stall is noticed enough to record; the pattern (time of day, request type, cadence) is more visible in one place.

| Date | Request | Wall time | Notes |
|---|---|---|---|
| 2026-08-23 | `GET /?folder_id=23&sort_dir=desc&star_only=1` (5 items) | 6919ms | 6.3s gap between two already-fast, already-logged steps — nothing itself slow |
| 2026-08-23 | `GET /?folder_id=1&star_only=1&kept=starred&sort_by=starred&sort_dir=desc` (F5 on Saved) | 18664ms | Landed mid-scheduled-refresh — dozens of concurrent `httpx` feed fetches logged in the same window |
| 2026-08-23 | 4 back-to-back `GET /?folder_id=1&star_only=1&kept=starred` (clicked Saved) | 2114/7882/8684/18192/9303ms | Cluster, not a one-off — same gap signature (list_entries logs fast, posts_block/meta_block absorb the delay) ~5-7 min after a container restart; may correlate with post-restart cold caches/backfill rather than being independent of it |
| 2026-08-30 | `GET /?folder_id=1&read_filter=unread` | 9786ms | ~7 min after a restart — **confirms** the 2026-08-23 5-7-min-post-restart correlation rather than just suggesting it. Same tag_block→list_entries gap (~5.8s, `meta_block=75ms`+`tag_block=0ms` at :37.4, `list_entries` fetch not starting until ~:43.2) |
| 2026-08-30 | `GET /?folder_id=1&read_filter=unread&list_feed_url=...jsnover.com...` | 9216ms | ~9 min after the same restart — still elevated a bit past the 5-7-min band, so the window isn't a sharp cutoff |
| 2026-09-02 | [entry pane, play.nobleknight.com](https://lectio.catfork.win/?folder_id=23&sort_dir=desc&read_filter=unread&feed_url=https%3A%2F%2Fplay.nobleknight.com%2Ffeed&entry_id=https%3A%2F%2Fplay.nobleknight.com%2F%3Fp%3D19266) | "really long time to open" | Not measured server-side yet — flagged by Josh, not chased in-session. Same folder (23) as the 2026-08-23 cluster above |

**Follow-up on the "prime suspect" above, from git history (2026-09-01 commits e1eb580/7e792ab —
shipped but never written up here, so re-recording it now).** The `get_saved_unread_count` batching
fix landed (533 round-trips → 12) but did **not** resolve the stall — badges stayed 5-7s under
refresh contention with the new code confirmed running, so raw round-trip *count* wasn't the
mechanism. The timing was split into three sub-calls (`unread_count`/`counts_by_folder`/
`inbox_total`) to localize further, and suspicion moved to `get_tagged_entry_keys` (main.py:10860,
called from both `get_saved_unread_count` and `get_saved_counts_by_folder`): it opened its own
`sqlite3.connect(reader_db_path, timeout=5.0)` per call instead of going through `get_reader()`'s
pooled connection — the exact anti-pattern `get_meta_connection()`'s own docstring already
identified as expensive under concurrency (file-open/schema-load/mmap-setup paid per call instead of
once per thread), and a plain connect-timeout rather than the pooled connection's
`PRAGMA busy_timeout=10000`.

**Fixed 2026-09-02:** `get_tagged_entry_keys` now reads through `get_reader()._storage.get_db()`
like the rest of this module's reader-DB reads, with its own `[perf] get_tagged_entry_keys=%dms`
log (fires only when >200ms) so the next live stall either pins the blame here directly or clears
it. Not yet confirmed live — needs a `make rebuild` deploy and a real capture during refresh
contention before this item can be marked resolved. If it's still slow after this, the lead moves
back to shared WAL/lock contention (the Read Above/Below item's checkpoint-timing theory below)
rather than any one call site.

**Live capture 2026-09-03, after the meta-DB write-batching work above.** Josh reported "serious
delay" on `GET /?folder_id=9&read_filter=unread` — access log confirms `12961ms`. Two findings:

1. **`get_tagged_entry_keys` is NOT cleared** by the 2026-09-02 pooled-connection fix — it logged
   `1078ms` (unscoped, 15,742 rows) and, in the same request, `2038ms` (scoped, 15,579 rows) for
   `get_saved_unread_count` and `get_saved_counts_by_folder` respectively. The pooled connection
   removed the *per-call connection-open* cost, but this is `db=reader`, not `db=meta` — refresh
   writes to the reader DB constantly (that's its whole job), so a `SELECT ... WHERE key LIKE ?`
   landing mid-write is exactly the same `SQLITE_BUSY`-retry mechanism already proven for the meta
   DB, just never directly measured on the reader DB's read side before. This is the same shape as
   the Read Above/Below item below, not a new mechanism — one more data point for that WAL/checkpoint
   lead, not a call site to patch.
2. **A second, previously invisible gap, likely bigger than the first.** Summing every instrumented
   block (`meta_block` 111ms + `tag_block` 59ms + `gap_block` 5580ms + `posts_block` 1232ms) accounts
   for only ~6982ms of the request's 12961ms — **~5979ms with no timing of its own**, after
   `posts_block` finishes and before the response is fully sent. `_home_inner` builds its template
   context dict (a dozen+ settings/integration-status lookups: `is_email_configured`,
   `*_oauth_connected` x4, `is_instapaper_configured`, `is_quire_configured`,
   `get_all_manual_tag_names`, `get_push_active_feed_urls`, `unsubscribed_feed_urls_among`, …) and
   then hands a **lazy** Jinja `TemplateStream` to `StreamingResponse` — `.stream()` itself does no
   rendering; the actual per-post Jinja evaluation (250 posts here) happens only as the stream is
   drained, which is *after* `_home_inner` returns, so none of it was ever inside a measured block.

**Instrumented, not yet fixed, 2026-09-03**: two new ticks bracket this previously-dark region —
`[perf] home: ctx_block=%dms` (context-dict build, >100ms only) right before the template call, and
`[perf] home: template_stream=%dms` (>100ms only) wrapping the stream generator so its render+send
time is attributed correctly even though it runs after the handler function returns. Needs a
`make rebuild` deploy and a real capture to say which of the two (or both) is where the ~6s actually
went — until then this reads as a lead, not a diagnosis: it could be slow Python-side per-post
template logic, or the same lock-wait mechanism as everything else in this item showing up in
whichever settings lookup happens to run during a write.

**Josh's own hypothesis, 2026-09-03: "something to do with sorting/filtering... maybe the default is
structured for a different view to be fast."** Not quite the exact mechanism, but it pointed at the
right code and turned up a real, well-scoped bug — fixed same day.

`list_entries_for_feeds` has three shapes: a per-feed path for ≤32 feeds (goes through `get_reader()`'s
pooled connection), and two "many feeds" SQL fast paths — one for `sort_dir=asc` (Josh's daily driver:
Unread + oldest-first), one for `desc` — both gated on `len(feed_urls) > PER_FEED_QUERY_THRESHOLD`
(32). **Both** many-feed paths, plus the `feed_site_map` favicon-host lookup just above them, opened a
**fresh `sqlite3.connect(reader_db_path, timeout=5.0)` on every single request** instead of reusing
`get_reader()`'s pooled, timed connection — the identical anti-pattern `get_tagged_entry_keys` was
fixed for 2026-09-02, just never caught here. So it isn't that ASC is slower than DESC (both shared
the bug equally); it's that **small views (≤32 feeds) never hit this code at all**, while any large
folder — exactly where real browsing happens — paid full file-open/schema-load cost on every render,
with a *shorter* busy_timeout (5s) than everywhere else (10s), and were completely invisible to
slow-SQL logging since a bare `sqlite3.connect()` bypasses `_TimedConnection` entirely. A `sqlite3.connect(str(tenancy.reader_db_path())...)` grep turned up **20 occurrences** across main.py total —
these 3 were fixed because they're squarely in the hot list-rendering path and directly explain this
symptom; the other ~17 are scattered across less-hot routes and are a separate, deferred cleanup (see
below), not part of this fix.

**Fixed 2026-09-03**: all three call sites now do `reader._storage.get_db()` inside the existing
`with get_reader() as reader:` block (row access switched from `sqlite3.Row` named lookup to
positional, matching `get_tagged_entry_keys`'s established pattern — reader's pooled connection
doesn't set a row_factory). 2 new tests (`tests/integration/test_list_entries_pooled_connection.py`):
one asserts no fresh reader-DB connection is opened for either sort direction past the threshold
(would have failed against the old code), the other confirms ASC/DESC still agree on membership after
the rewrite. Full suite green (3923). **Not yet confirmed live** — needs a deploy and a real capture
on a large folder during refresh contention.

**Deferred, separate cleanup**: the other ~17 `sqlite3.connect(reader_db_path)` call sites elsewhere
in main.py share the same anti-pattern (shorter busy_timeout, invisible to slow-SQL logging) but
aren't in a request-path hot loop the way these three were — worth a systematic sweep at some point,
not sized here.

**Live capture 2026-09-03, after the pooled-connection fix above deployed.** Josh: "still many
seconds to switch to Dev." Genuinely good news mixed with the next lead: 3 consecutive requests to
the same folder in a 45s window logged `6532ms` / `1421ms` / `6692ms` — the middle one is now
authentically fast (`meta_block=11ms`, everything else <400ms each, `template_stream=328ms`,
confirming both the write-batching and the ASC/DESC pooled-connection fixes are working as intended
when the cache is warm) — but the two slow ones both show the **entire** delay concentrated in one
place: `meta_block=4999ms`/`5282ms`, and inside that, `meta.structure_snapshot=4964ms`/`5268ms` —
`get_meta_structure_snapshot`'s cached folder/feed-tree bundle (`main.py:6129`) going fully cold and
paying its 5-query rebuild cost under refresh contention, twice in under a minute. Its own docstring
says the cache should invalidate "only on explicit user mutations" (subscribe/unsubscribe, folder
changes) — Josh was just clicking between folders, not managing feeds, so something is invalidating
it far more often than that design assumes, landing this expensive rebuild right in the middle of
ordinary browsing during refresh.

**Instrumented, not yet diagnosed, 2026-09-03**: 37 call sites call `invalidate_meta_structure_cache()`
— rather than manually audit all of them, the function now logs its immediate caller's
file/line/function name on every call, so the next live occurrence names the actual code path firing
during a routine refresh pass instead of a guess. `ctx_block`/`template_stream` from the previous
capture never fired in this one (both requests' non-meta-block time was already trivial), so that
lead is provisionally cleared — needs a deploy and a real capture to name the caller before sizing
a fix (candidates worth checking first once a caller shows up: anything in the per-feed refresh loop
or `_run_automation_after_refresh` that runs unconditionally rather than only on an actual structural
change).

**Live capture 2026-09-03, after the caller-logging deploy — the caching theory was wrong, and the
real bug was a labeling mistake.** Josh: "still many seconds to switch to Dev." Confirmed the browser
side was ordinary hard-refreshes/new tabs, not Lectio's own manual Refresh button — ruling out
self-inflicted refresh-contention as a confound. A fresh capture caught `meta.structure_snapshot=2594ms`
again 45 minutes into steady-state (not a post-restart cold start) — **but zero
`invalidate_meta_structure_cache` log lines fired anywhere in the preceding 60 minutes.** That's a
clean negative result: the cache was never actually being invalidated, so "going cold under refresh
contention" was the wrong theory entirely.

Re-reading `_home_inner`'s timing code (`main.py:24107`, the `_tick()` closure) found the real bug:
the `_tick("structure_snapshot")` call was placed *after* `get_all_reader_feed_urls()` and a block of
derived-set building, not right after `get_meta_structure_snapshot(conn)` itself — so the tick's name
described the cached snapshot lookup, but its *measurement window* actually included that unrelated
call too. `get_all_reader_feed_urls` (`main.py:5854`) opened its own fresh
`sqlite3.connect(reader_db_path, timeout=5.0)` on **every single home-route render, unconditionally**
— the identical anti-pattern already fixed twice this session (`get_tagged_entry_keys`,
`list_entries_for_feeds`' sort paths), just in a third location, mislabeled by a timing tick that
made it look like a caching problem. This one is worse than the previous two in blast radius: it has
no `len(feed_urls) > 32` gate at all, so it fires on literally every home-route request regardless of
folder size, plus 11 other call sites elsewhere in main.py.

**Fixed 2026-09-03**: `get_all_reader_feed_urls` now uses `reader._storage.get_db()` from
`get_reader()`'s pooled connection. Split the mislabeled tick into two: `structure_snapshot` now wraps
only `get_meta_structure_snapshot()` itself, and a new `uncategorized_derive` tick isolates
`get_all_reader_feed_urls()` plus the Uncategorized-folder set-building that follows it — so a future
capture attributes correctly instead of repeating this exact misdiagnosis. 2 new tests
(`tests/integration/test_get_all_reader_feed_urls_pooled.py`): one guards against a fresh connection
(would have failed on the old code), the other confirms `include_kept` behavior survived the rewrite.
Full suite green (3925). **Not yet confirmed live** — needs a deploy and a real capture; if
`structure_snapshot` (now correctly isolated) is still slow after this, the cache-cold-under-contention
theory becomes worth revisiting for real, this time with a tick that actually measures what its name
says.

**Confirmed live 2026-09-03 — resolved.** Josh clicked through several folders: "all seem to be about
the same 1-2 seconds." A fresh log capture backs it up directly: every request in the window logged
`meta_block` 9-49ms (down from 5-13*seconds*), `badges_detail` ~300-500ms, `posts_block` ~100-700ms
(feed-count dependent), `template_stream` ~300-800ms — real, legitimate work, no lock-wait pathology
anywhere. One request's `meta_block` did spike to 1664ms, and the split tick correctly attributed it
to `uncategorized_derive` (not `structure_snapshot`) — the fix's own diagnostics working as intended,
not a new problem. Total per-request now consistently lands at 1-2s, matching Josh's report and, not
coincidentally, roughly what the very first entry in this item (2026-08-11, "median 700ms") described
as the *good* case before any of this started.

Two small residuals visible in the same capture, neither on the click path: a single-row
`yt_quota_spend` write hit ~3.9s (background YouTube-quota tracking, its own long-known non-batched
single-row shape, never sized as worth fixing), and one batched `entry_lead_images` `executemany`
flush hit ~4.7s under heavy load — visible only because of the executemany-wrapping fix a few commits
back, and exactly the intended trade-off: one commit occasionally waiting, not N commits compounding.
Neither is user-visible. **This closes out the Tier 1 refresh-contention chain that started
2026-08-11** — four real bugs found and fixed across two sessions (lead-image per-entry writes ×3
shapes, hide-paywalled's missed batching, and three separate raw-reader-connection call sites), each
confirmed by a live capture, several confirmed by Josh directly. Remaining open threads, none of them
blocking: `entry_read_state` batching in the post-refresh automation pipeline was investigated and
found to already be correctly batched everywhere except the one site fixed here; the ~17 other
`sqlite3.connect(reader_db_path)` call sites elsewhere in main.py (deferred cleanup, noted above); and
the Read Above/Below WAL/checkpoint-timing lead below, which was never confirmed as the same mechanism
and may simply no longer matter now that the connection-pooling bugs are fixed.

**Read Above/Below, same shape:**


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

**Re-fetch/extraction quality & staleness** — the article being read is broken or stale; directly in the way of triage.

### Entry pane doesn't refresh after a background auto-refetch-on-tag finishes

First noticed 2026-08-30 as what looked like a bad extraction (a mindyourdecisions.com entry,
"Fantabulous Numbers," showing only the embedded video after tagging) — **root-caused via a live
repro on a second entry the same day**, and it isn't an extraction bug. Tagging a stub entry (empty
`content`/`summary`) triggers `_maybe_autofetch_on_keep` (main.py:33223), which re-fetches in a
background thread *after* the tag request already returned — "off-request so the star stays
instant," per its own docstring. The already-open entry pane has no way to know that background
fetch finished, so it keeps showing the stale stub render (just an empty `lectio-embed` placeholder
slot, no visible text) until the pane is closed and reopened. Confirmed directly: right after
tagging, the DB already held the full 19,916-char article (every solution step, no missing prose),
while the open pane still showed just the video slot; navigating away and back showed the real
content immediately. This reframes the first report too — "a manual re-fetch fixed it" was most
likely just forcing a pane re-render onto content the background auto-refetch had *already* filled
in, not a second, better extraction.

Not built: some way for the pane to pick up the auto-refetch's result once it lands — e.g. the
existing tag-response payload flagging that a background re-fetch was kicked off, and the pane
polling or re-fetching itself once it's done, rather than the user having to discover the trick of
clicking away and back.

### An ArtStation entry with a body image still resolved to no lead image

Noticed 2026-08-30: a list-view thumbnail missing on an ArtStation feed entry. Checked
`entry_lead_images` for it — the row exists with `fetched_at` set (a resolution attempt did
complete) but `image_url`/`image_alt`/`image_title`/`thumb_crop` are all NULL, even though the
entry's stored body has exactly one `<img>`, a normal (non-signed-looking) `cdnb.artstation.com`
CDN URL. So resolution ran and came back empty despite an obvious single candidate sitting right
in the content. Not investigated further — worth checking whether this is systemic across
ArtStation entries or a one-off before digging into the resolver itself.

### A Bluesky entry has a thumb but no lead image

Flagged 2026-09-02, not investigated:
[entry](https://lectio.catfork.win/?folder_id=6&read_filter=unread&feed_url=https%3A%2F%2Fbsky.app%2Fprofile%2Fdid%3Aplc%3Ae2ehcohu3lrobwew5gzqd7vp%2Frss&entry_id=at%3A%2F%2Fdid%3Aplc%3Ae2ehcohu3lrobwew5gzqd7vp%2Fapp.bsky.feed.post%2F3muab5zxz4k2a)
— a thumbnail resolved and shows in the list, but the entry pane shows no lead image. Opposite
shape from the usual "no thumb" reports; worth checking whether the thumb and lead-image resolvers
disagree on this entry, or whether Bluesky's `at://` post feeds need their own handling (same
family as the DeviantArt/webcomic per-source lead-image work already in git history).

### Shared proxy/FlareSolverr escalation for page fetches — SHIPPED 2026-08-31

Closed both re-fetch and tag/lead-image gaps raised 2026-08-31 (tamriel-rebuilt.org 403s on
"Refetch content"; gottadeal.com's Cloudflare-walled article pages blocking tag capture). New
`services/page_fetch.py` (`PageFetcher`) runs a single-URL honest → browser → proxy → FlareSolverr
ladder, deliberately separate from the feed-refresh flag-and-retry loop (see
`docs/architecture/feeds.md` "Page fetches" for the full rationale — host-keyed in-memory
escalation memory instead of a new table, no Tailscale tier, `max_tier` differs between the
synchronous reader-view path and the always-backgrounded re-fetch path). Wired into
`_fetch_page_html` (tags + lead images) and `fetch_readability_article`/`fetch_full_page_article`
(re-fetch), sharing one `PageFetcher` instance so a host FlareSolverr solves for one path is known
to the other. Settings → Feeds → Fetch Tiers gained a fourth section showing this ladder's state.

**Still open, deliberately deferred (see the shipped commit's "non-goals"):**

- **Broaden `extract_page_tags`'s recognized markup patterns** for reachable sites whose tag block
  isn't recognized yet — smaller, incremental, same shape as the RebelMouse/Hugo/ArtStation cases
  it already handles. Independent of the escalation ladder.
  **Full survey done 2026-08-31** — all ~622 then-untagged feeds (by entry count, one representative
  entry each, two passes). ~272 produced *some* tags across the two passes (mostly just from being
  reachable at all now); 63 of those were YouTube's fixed UI-boilerplate `keywords` meta and are now
  deliberately excluded as junk, not counted as real wins — see below. 6 new patterns added, each
  confirmed against a real page: `og:article:tag` meta, `aria-label="...tagged with X"` anchors,
  `itemprop="keywords"` anchors, a "Filed under:" cue alongside the existing "Posted ... in", a
  space-separated (not comma-separated) `keywords` meta fallback, and raising the tag-anchor regex's
  inner-content cap 120→500 chars (icon-decorated anchors were entirely invisible below the old cap).
  Plus 2 false-positive fixes: `front`/`main` added to the URL-path stopwords (netbeans.apache.org's
  own routing segments), and excluding youtube.com/youtu.be from page-tag scraping entirely (its
  `keywords` meta is fixed, locale-translated chrome, not per-video — confirmed byte-identical across
  63 unrelated channels). ~210 of the survey genuinely have no taxonomy on the page; ~107 are still
  blocked (ArtStation is most of that — FlareSolverr solves the page but the tag widget needs more JS
  than the solve waits for). Checked and deliberately NOT added: several JSON-LD/meta "keywords" hits
  that were empty or from an unrelated single-page-app JSON blob, dozens of WordPress "Uncategorized"
  defaults (already correctly filtered as junk), and a "tagged" hit that was body prose, not markup.
  Site-by-site from here as new gaps turn up — no more broad surveys needed unless the untagged
  count grows a lot.
- **Persist `HostEscalationState`** to a `host_fetch_tiers` table if in-memory proves insufficient
  in practice — the class already hides this behind its current five methods, so it's a drop-in.
- **Consolidate `feed_discovery._get_with_escalation`** onto `PageFetcher` — a third, older copy of
  "honest then browser UA" with its own header set; left alone to keep the ladder's own PR reviewable.
- **Key `_autofetch_failed_hosts` on deepest-available-tier**, the same fix `HostEscalationState`'s
  cooldown got, if it turns out to matter in practice.

**Spot-checked live 2026-08-31/09-01** against the real proxy/FlareSolverr backends (configured via
Administration, not `.env` — `gluetun`/`flaresolverr`/`tailscale` containers all genuinely running):

- **gottadeal.com — fixed.** `queue_source_html_fetch` on a real entry: honest 403, browser 403,
  **proxy 200** — tags captured (`['deals']`) where there were none before. Never needed FlareSolverr;
  the escalation ladder correctly stopped once the proxy tier alone got through.
- **tamriel-rebuilt.org — ladder works correctly, FlareSolverr itself can't solve this particular
  challenge.** `_refresh_captured_article_for_current_user(..., ignore_cooldown=True)` on a real
  entry: honest 403, browser 403, proxy 403 (still blocked even through the VPN exit), correctly
  escalated to FlareSolverr (confirming `bot_challenge` recognized a real challenge here, unlike
  gottadeal.com's plain block) — FlareSolverr's own container log shows it detected Cloudflare's
  "Just a moment..." page and then genuinely timed out after 55s trying to solve it. Same failure
  mode hit other unrelated sites in the same log window (mxlinux.org, neowin.net timeouts;
  romhacking.net "IP is banned"), so this reads as FlareSolverr's own solve reliability on tough
  Cloudflare challenges, not a bug in the new escalation code — it built the right request
  (`{"cmd":"request.get","url":...,"proxy":{"url":"socks5://gluetun:1080"}}`, scheme correctly
  normalized) and reported the failure cleanly rather than crashing. Not pursued further here; the
  gap this item exists to close (no escalation offered at all) is closed regardless of whether
  FlareSolverr wins every individual challenge.

### A thin post whose entire content is one image ends up looking empty

Also from tamriel-rebuilt.org (2026-08-31): entry 17652's summary is exactly one `<img>` and no
other text — the lead-image hoist strips that image out of the body once it becomes the hero,
which is correct for a normal article (avoid showing the hero twice) but leaves NOTHING behind
when the image was the entire post. Reads as "no img" even though the thumb/hero resolved
correctly. Would need the hoist-and-strip step to check whether stripping would leave the body
empty and skip the strip in that case; not done here.

## Tier 2 — small, fast, independent wins

**Navigation/UX papercuts** — no design work needed, just haven't been built.

### New subscription missing from feed tree — UX idea remaining

Root-cause code bug already fixed (2026-07-08: re-adding a feed that existed
in reader as disabled now calls `enable_feed()`). Remaining idea, not asked
for: auto-disambiguate duplicate display titles (e.g. suffix from the feed
URL path) — the tree tooltip already shows the URL, but identical titles
still invite unsubscribing the wrong feed.

**Field reports, 2026-09-02 — flagged, not investigated unless noted**

### Global Note (?) — posts list scrolls way up sometimes

"open note? posts list scrolls way up sometimes" — vague as reported, not reproduced. Sounds like
opening the Global Note (or some other panel) occasionally yanks the post list's scroll position.
Needs a repro before it's actionable — ask Josh what exactly triggers it next time it happens.

### Global "hide subscriber-only" toggle for YouTube

Feature idea, not scoped: a library-wide setting to hide YouTube videos that are members/subscriber-only,
similar in spirit to the existing hide-Shorts/hide-unpremiered per-feed display prefs
(`_DISPLAY_PREF_KEYS`). Not investigated — needs checking whether the feed data even distinguishes
subscriber-only videos before sizing this.

### NEXT UP (after the refresh-contention perf work wraps): bump the size of in-header buttons/tag chips

Clarified 2026-09-03 — supersedes the vaguer "larger tags/'+^vx' for Surface" report (2026-09-02,
which read as feed-specific and was left unscoped for that reason). Not feed-specific: Josh wants
the entry pane's post-header controls (the `+`/`-` tag-filter chips, suggested-tag chips, and
whatever else lives in that row) sized up generally, across the app. Explicitly flagged as the next
thing to pick up once the current refresh-contention perf thread (Tier 1) is done. Not scoped
further yet — needs a look at the header markup/CSS to see whether this is a simple size-token bump
or touches layout (chip-row wrapping, spacing against adjacent controls).

### Global ignored suggested-tags list, editable in Settings

Distinct from the existing per-(feed, tag) dismissal (`suppressed_feed_tags`, × on a chip, undo at
Feed Properties → *Hidden tags* — see "Feed-tag suggestion suppression" below). Josh wants a
**global** list of tag values (e.g. `comments`) that should never render as a suggested-tag chip on
*any* feed — filtering the chip from the suggestion UI itself, explicitly **not** a rule that acts
on entries carrying that tag. Wants it editable somewhere in Settings (a new list, add/remove).
Not scoped: needs a new setting (JSON list or a small table), a check at chip-render time
(`feed_tag_suggestions` filtering), and a Settings UI panel.

### MAR scope: only what's currently shown, or newer-not-yet-seen too?

"mark folder only shown or potentially newer that haven't been seen yet?" — a question about what a
folder's "Mark Read" bulk action should cover: just the entries currently rendered/loaded in the
list, or also anything newer that hasn't been fetched into view yet. Not resolved — needs Josh to
say which behavior he actually wants (and whether the two already differ today) before scoping.

## Tier 3 — maintenance backlog, ready to run

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

### Uncategorized orphan-feed cleanup — 9 stragglers left (manual)

9 feeds are dead/one-shot/ambiguous (an Instagram post URL, a single Vice
article, cochaser.com (no entries), WebServicesDir, whiskypaint/nolanfa
tumblrs, norfolkwinters, crispian-jago, owenyoung myfeed) — sort or
unsubscribe manually.

## Tier 4 — real features, not blocking anything today

### Backblaze B2 support for backups

Requested by Josh 2026-09-03. `scripts/backup_databases.py` (`VACUUM INTO`, size-aware retention —
see its own docstring) only ever writes to `$LECTIO_DATA_DIR/backups` on local disk today; nothing
ships a copy off-host. B2 is S3-compatible, so this is likely a `boto3`/`s3fs`-style upload step
after the existing local backup completes (upload each newly-written file, prune remotely to match
local retention or keep its own schedule), plus new env vars for the bucket/key/application key —
mirror `.env`/`.env.example` per the usual convention. Not sized yet — needs a look at whether to
reuse the existing local-retention pruning logic for the remote side or keep them independent (remote
retention probably wants to be longer-lived than local, since off-host is the actual disaster-recovery
copy).

### Soundslice tab-player embeds are permanently blocked by the content owner's own domain allowlist

Raised 2026-08-31, premierguitar.com lessons: `soundslice.com` is now on the iframe embed
allowlist, but the player itself refuses to load off-domain — confirmed live, even a bare fetch of
the embed URL returns "Failed embed allowlist check." — because Soundslice lets the *creator*
(premierguitar.com's own account) restrict which domains may embed a given slice, and Lectio isn't
one of them (nor could it ever ask to be, since PG doesn't know Lectio exists). No public
static/print/image export endpoint either (403/404 on the obvious guesses) — same gate. A static
image would need actually rendering the *original* premierguitar.com page (where the embed IS
authorized) in a real headless browser and screenshotting just that region — FlareSolverr gives us
real Chrome already, but per-slice screenshot-and-crop at capture time is a genuine new feature,
not a tweak. Skipped for now — narrow (guitar tab specifically), not worth the effort unless it
comes up more.

**Dedup subsystem** — one coherent area, biggest single feature idea on the list.

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

### Auto-file saved articles — the tail

- **guitarplayer.com's 303 articles** — the site's own subscription is a
  scraped one-article stub (barred as a target), and probing showed many
  article URLs soft-404. **Decision confirmed 2026-08-09: look for/build a
  real guitarplayer feed** rather than leaving them as one-off saves or
  deleting — worth the investigation despite the soft-404s.
- **166 already-converted stars** — tagged entries starred by a since-fixed
  backfill bug. Indistinguishable from a genuine star-and-tag, so they cannot
  be surgically reverted; the unstar-tagged pass is what removes them.

**Rules engine follow-ups**

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

### Post-header tag-filter chips don't reflect a folder/global-scoped rule

Found 2026-09-03: Josh promoted a batch of per-feed `tag_filter` rules to folder scope (hand-editing
each rule's scope in the rule builder — there's no dedicated "promote" action). The automation
itself keeps working fine — folder scope is fully supported by the after-refresh pipeline
(`main.py:8317-8326` resolves each refreshed feed against the folder's feed set and applies the
rule's spec per feed). What breaks is the entry pane's `+`/`-` tag chips: `get_feed_tag_filter_rule`
(`main.py:7698`) explicitly filters `scope = 'feed'` and ignores folder/global rules by design (its
own docstring says so, and always has — this isn't a regression from the promotion, just a gap the
promotion exposed). So for a feed now covered only by a folder rule: the chips render **unlit** even
though the folder rule is actively filtering that feed, and clicking one **creates a brand-new,
separate, disabled per-feed rule** instead of editing the folder rule — silently forking the config
rather than merging into it.

Not fixed. The real shape of a fix is probably a small **scope hierarchy** the chip lookup walks —
feed rule wins if present, else the covering folder rule, else a global rule — and the chip UI would
need to show *which* level is currently governing a tag (a feed-level override sitting on top of a
folder-level filter is a different situation than the folder rule alone) rather than just lit/unlit.
`toggle_feed_tag_filter` would also need a decision for what "click a chip when only a folder rule
covers this feed" should do — edit the folder rule (affects every other feed in it), or explicitly
create a feed-level override/exception on top (closer to today's behavior, but currently happens
silently with no indication that's what's occurring). Needs its own design pass, not sized further
here.

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

### Read Mode: no Back guard

Read Mode (`GET /read`) still has no equivalent of the main app's Back-button
guard (resume was already fixed 2026-08-25 — separate `lectio-read-last-position`
localStorage key so the main app and Read Mode don't bounce into each other).
Not cheap: `/read` has no drawer for Back to land on, and a Back that visibly
does nothing is worse than one that exits the app. Give Read Mode a
collapsible folder tree first, then add the guard.

## Tier 5 — deliberately deferred / big investments

**Architecture**

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

### Single-user mode does not exist anymore — retire DEFAULT_USER

Multi-user is simply how Lectio works now; making one account is the "single user" case. But `DEFAULT_USER_ID = "default"` survives as the default value of the `lectio_current_user` ContextVar (`services/tenancy.py:53`), so any code path that never binds a user silently resolves to the legacy top-level DBs at `/data/lectio_meta.sqlite3` and `/data/lectio_reader.sqlite` instead of failing. Those files are stale — the legacy reader DB was last written 2026-07-24 and is 73 KB against a 685 MB per-user one — so the failure mode is not an error, it is quietly correct-looking answers computed from the wrong database. It has already produced nonsense failing-feed counts during debugging, and it is the same trap as a background thread losing its tenancy binding.

The change: default the ContextVar to `None` and make resolution raise when unbound, so every background thread, CLI script and push handler must bind a user explicitly and a missed binding fails loudly at the first read. Then delete the legacy path branches in `tenancy.py` and the stale DB files, and drop `DEFAULT_USER_ID` from `_RESERVED_USERNAMES`.

Not small: 54 references outside `tenancy.py` and `tests/`. Wants its own PR, and wants the per-user startup migration checked, since anything still reading the legacy paths will surface the moment they stop resolving. Related: the bg-thread tenancy rule already in place (`_run_in_user_context`).

### Add OIDC login

Not scoped. Current auth (`services/users.py`, `/login` at `main.py:21999`)
is username/password only — no SSO/OIDC exists today. Architecture-level
addition (new login flow, session handling alongside the existing one,
tenancy binding from an OIDC subject to a Lectio `user_id`, first-login
provisioning) — wants a real plan before code, not attempted yet.

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

**Grab bag** — low-urgency, independent of each other and of everything above.

### `make rebuild` cycle is 100-130s, getting annoying under heavy iteration

Noted 2026-09-01: real numbers from a session doing several rebuild-test cycles in a
row — `docker compose build` (dominated by the layer-export substep) runs ~60-70s,
then container start-to-healthy adds another 40-55s, consistently. Not new, just
not measured until now; been "well over a minute" for days per Josh. Not painful
for a normal single deploy, but a session iterating on a live-diagnosis loop
(instrument → rebuild → observe → repeat, e.g. the refresh-contention gap_block
work above) eats minutes per cycle on this alone.

Not investigated. Two separate things to look at if it's worth the time:

- **Build/export time** — likely the BuildKit layer-export step; see
  [[docker-disk-pressure]] (cache grows ~1GB/session already).
- **Start-to-healthy time** — may not be independent of the post-restart startup
  flood already tracked above (backfill + YouTube recheck + orphan sweep + a full
  refresh batch all firing at once right after boot) — worth checking whether
  they're the same root cause before treating this as a second problem.

Unclear how much longer this session will be in heavy-iteration mode, so not
scoped further — revisit if it keeps coming up.

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

### Page-weight reduction — follow-ups

- **Entry-pane loading state/timeout** — slow pane loads still look like dead
  clicks.
- **Optional**: the pane-swap path still renders the full page server-side per
  fetch (posts + tree + shells, ~200KB now); a render-splitting/fragment
  endpoint for `.pane-posts`/`.pane-entry` would cut server time further.

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

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only in 2026-07 (CMS change: no
description/content element at all; older stored entries have bodies, so this
is upstream). A per-feed "fetch full content from the source page at ingest"
option (readability pipeline already exists) would fix such feeds generally —
per-feed opt-in in Feed Properties, capped/throttled like enhancement. Doesn't
help the bot-walled feeds above (they're blocked at fetch, before content
matters) but could recover feeds elsewhere that are body-less rather than
blocked.

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

### Global audio player — deferred v2 ideas

Shipped in PR #111 (see git history). Still deferred: queue/playlist of audio
across a folder, remember position per episode, Media Session API (lock-screen /
hardware-key controls), speed presets.

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

## Watch-lists

Nothing here is scheduled — just what to check if a related symptom recurs.

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

### Parked, deliberately

Genuinely nothing to do here until one of these recurs or a lead turns up —
not scheduled, just watched.

- **makeuseof re-fetch returns white images.** Seen once during testing
  2026-08-06 and never investigated. Waiting on a second sighting rather than
  hunting it cold — Josh will flag it if it recurs.
- **guitarworld.com lessons: an interactive practice widget (MatchMySound,
  `app.matchmysound.com/embed.html?ass_id=...`) totally missing from Lectio's
  capture.** Found 2026-09-02 on one entry. Not the same shape as the
  Soundslice/PremierGuitar item above — that one captures a real iframe that
  then fails to *load* (creator domain-allowlist). Here nothing embed-shaped
  reaches Lectio's stored content at all (checked 40 recent entries in the
  feed, zero matches), and a plain fetch of the live page comes back as a
  content-free JS shell (zero occurrences of the article's own title text) —
  same failure Lectio's own fetcher likely hits. Looked paywalled at first
  (VS Code's unauthenticated browser showed "exclusively for Guitar World
  Backstage Pass members"), but Josh isn't logged in either way and sees the
  full article + widget in Vivaldi — probably a client-side/script-driven
  paywall gate his adblocker kills, not a real server-side one. Since none of
  the fetch-escalation tiers run an adblocker, they'd likely all hit the same
  gate regardless. One article, not chased further — revisit only if this
  turns out to be a pattern across more than this one lesson.
- **A second raw-Markdown-instead-of-HTML feed, unconfirmed.** The
  blog.gitea.com case is fixed (see git history 2026-08-30); Josh recalls a
  similar wall-of-text symptom on another feed recently but couldn't place
  which one. The fix already covers it generically if it recurs — just
  watching for a confirmed second instance to be sure.
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

