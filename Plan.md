# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Five tiers: actively impeding unread-clearing, small independent wins, maintenance backlog ready
to run, real features not blocking anything today, and deliberately-deferred big investments.
Within a tier, related items are clustered under a bold sub-heading. Two watch-lists (CodeQL,
Parked) sit at the end — nothing there is scheduled, just what to check if a symptom recurs.

Tiers 1 through 3 are empty. The main.py/index.html breakup's Step 1 (top of Tier 4, the
Integration routes cluster) is done — Stages A-E all shipped 2026-09-19/20. Step 2 (post-refresh
automation pipeline) is now scoped into its own A-E sub-stages and Stage A shipped 2026-09-20.
Steps 3-7 of the breakup are unscoped follow-on work, not started.

## Tier 1 — actively impeding unread-clearing

Empty.

## Tier 2 — small, fast, independent wins

Empty.

## Tier 3 — maintenance backlog, ready to run

Empty.

## Tier 4 — real features, not blocking anything today

### main.py / index.html breakup — extraction map

`main.py` was 40,474 lines when this started (2026-09-19), now 38,218 after Step 1 (Stages A-E,
below) moved the whole Integration routes cluster — ~44 routes plus their workers — into
`routes/integrations_*.py` and two new services modules. `static/js/app.js` is 20,042 lines;
`index.html` is 2,305 lines — untouched by this round. CLAUDE.md calls for a routes/services/
storage split main.py has only partly grown into. Not a same-session change — needs incremental
extraction with tests between steps; Steps 2-7 below are unscoped.

**Already done, organically, without anyone treating it as "the breakup project":** `index.html`'s
modal extraction (7 `{% include %}`s now — `_tree_folder_feeds.html`, `_entry_pane.html`,
`_action_modals.html`, `_add_feed_modal.html`, `_settings_modal.html`,
`_feed_properties_modal.html`, `_folder_properties_modal.html`) and the lazy `data-lazy-src`
panel-fetch pattern (4 panels now: `folders`/`stale`/`fetch-tiers`/`failing`). Only the 4 context
menus (`folder-context-menu`, `root-context-menu`, `post-context-menu`, `tag-context-menu`, ~150
lines) remain inline.

**Landmines:** 10 module-level `PerUserDict` caches, 32 module-level `threading.Lock()` instances,
20 `global` statements must stay singletons — route modules can import them but must not redefine
them, and every `invalidate_*` call site has to stay wired to the same instance. `get_reader()`
thread-local pooling and `lifespan` are startup-order-sensitive. The shared rendering core —
`_home_inner` (main.py:25598), `list_entries_for_feeds` (15994), `get_entry_detail` (19755),
`build_reader_page` (23797) — is reused by `/`, `/read`, pane-swap, and the greader/fever/v1
compat APIs; treat as its own carefully-tested project, not part of a mechanical split.

**Proposed order, safest → riskiest:**

1. Integration routes cluster, main.py:29323–31562 (~2,240 lines) — bigger than first scoped: it's
   the whole Integrations surface (OAuth + post-connect actions + Miniflux/FreshRSS/TT-RSS/Inoreader
   importers), not just OAuth/credential + Inoreader import, and every route in it depends on
   main.py-resident helpers (`get_meta_connection`, `get_setting`/`set_setting`/`delete_setting`,
   `get_reader`, credential/token getters, `_get_or_create_folder_by_name`, `_run_in_user_context`),
   not just one singleton. Extraction pattern that resolves this without a storage-layer rewrite:
   each `routes/integrations_*.py` defines `router = APIRouter()` and does `from main import ...` at
   module scope; main.py imports those routers and calls `app.include_router(...)` near the bottom
   of the file (after every needed name is already defined) rather than at the top — see the comment
   there. Split into sub-stages, tests run after each:
   - **A — done (2026-09-19).** Pure OAuth connect/callback/disconnect/verify for DeviantArt, Quire,
     YouTube, Pinterest, Reddit → `routes/integrations_{deviantart,quire,youtube,pinterest,reddit}.py`.
   - **B — done (2026-09-20).** Post-connect actions with no shared workers: Reddit submit, Pinterest
     boards/pin, Quire projects, YouTube playlists (list/add/add-batch/status, incl. the
     `_yt_playlist_batch_jobs` singleton) → same per-integration files. Found a real gotcha doing
     this: two tests (`test_pinterest_pin_route.py`, `test_youtube_playlist_add_batch.py`) imported
     the moved handler off `main` and monkeypatched `main.<helper>` — neither works once the handler
     lives in a routes module, since `from main import helper` copies the reference at import time
     (monkeypatching `main.helper` afterward doesn't touch the routes module's own binding), so tests
     now target the routes module directly. That surfaced a second, sharper issue: a test importing
     `routes.integrations_x` *before* anything imports `main` triggers the circular-import failure
     for real (routes' own `from main import ...` starts loading main.py, which reaches its own
     bottom-of-file `from routes.integrations_x import router` while that module is still mid-import
     and hasn't defined `router` yet) — fixed by making sure the test's `import main` line sorts
     before its `from routes import integrations_x` line; see `routes/__init__.py`'s docstring.
     Stages C-E should check any new/updated test the same way before assuming a moved route "just
     works" with its old test.
   - **C — done (2026-09-20).** DeviantArt watchlist sync/unsubscribe/push/add-watch-feed → extends
     A's deviantart file. `test_deviantart_watchlist_autoresume.py` called two of the moved routes
     directly off `main` (`deviantart_mark_unwatched_viewed_route`, `deviantart_unsubscribe_unwatched_
     route`) and needed the same routes-module retarget as Stage B; its other monkeypatches
     (`get_runtime_setting`, `disable_feed`, etc.) were untouched since those helpers stay in main.py
     and the still-in-main functions that call them (`_load_da_sync_detail`, `bulk_feed_action`,
     `sync_deviantart_watchlist`) resolve them from main's own namespace regardless of which module
     calls in.
   - **D — done (2026-09-20).** Miniflux/FreshRSS/TT-RSS import (test/status/start/reset + worker
     each) → `routes/integrations_{miniflux,freshrss,ttrss}.py`. The shared helpers
     (`_apply_migration_items`, `_canonicalize_item_feed_urls`, `_resolve_feed_url`,
     `_canonical_feed_url_lookup`) moved first into a new `services/migration_common.py` — a real
     services module (routes import it directly, not via main), but it still does `from main import
     canonical_feed_url, get_reader, ...` at module level since those primitives have no other home
     yet, so it's imported late from main.py's own bottom section too (Inoreader's still-resident
     import code, Stage E, needs 3 of the 4 helpers). This taught the same lesson Stage B did, one
     level deeper: the "import main first" rule extends to `services.migration_common` too — a test
     touching it before `main` hits the identical circular-import failure, because loading it
     triggers main's execution, which reaches its own late import of the same not-yet-finished
     module. `test_migration_import_dedup.py` and `test_canonical_feed_url.py` called the moved
     helpers directly off `main` and were retargeted the same way as prior stages.
   - **E — done (2026-09-20).** Inoreader OAuth + import (biggest, ~800 lines, its own drip-step
     state machine) → `routes/integrations_inoreader.py` + `services/inoreader_import.py`
     (`_inoreader_local_import_worker`, `_run_import_loop`, `_api_resolve_entry`,
     `_inoreader_drip_step`). This closes out the Integration routes cluster: `main.py` is
     40,405 → 38,218 lines, and it no longer defines a single `/integrations/*` or `/deviantart|
     quire|reddit/*` OAuth or import route — all of it lives under `routes/`. One more wrinkle on
     top of B/D's lessons: `_inoreader_drip_step` is also called directly (not through a route) by
     the scheduled-refresh loop still resident in main.py, so it needed the same "import back into
     main.py's bottom section" treatment `_apply_migration_items`'s siblings got in Stage D — and
     since that was main.py's *only* remaining use of the three `services/migration_common`
     helpers, that now-unused late import was deleted rather than left dangling. `File(...)` route
     defaults moved out of main.py lost its `B008` exemption in the process (`pyproject.toml`
     scoped that to `main.py` only) — extended to `routes/*.py` too, since more stages will hit it.
2. Post-refresh automation pipeline (`_run_automation_after_refresh` + the six
   `_run_*_rules_after_refresh` functions, plus the sibling "run now" triggers `_run_now_dedup`/
   `_run_now_pattern`/`_run_tag_filter`) → `services/automation_rules.py`, ~1,510 lines total.
   Scoped into its own A-E sub-stages, same reasoning as Step 1:
   - **A — done (2026-09-20).** Zero moves. Added `_bump_unread_counts_generation()` (main.py, next
     to `invalidate_unread_counts_cache`) and rewrote every in-scope bare
     `global _unread_counts_generation; … += 1` site to call it instead — the landmine here is
     landmine-shaped but silent: a moved function that keeps `global _unread_counts_generation`
     creates a *second* counter in the new module with no ImportError, just unread badges that
     stop invalidating after dedup/mark-read automation. Also added characterization tests for
     `_run_email_rules_after_refresh` and `_run_webhook_rules_after_refresh`
     (`tests/integration/test_email_rule_automation.py`, `test_webhook_rule_automation.py`) — neither
     had any fire-path coverage before this, despite doing real external I/O with no idempotency
     guard beyond the 15-minute `added` cutoff.
   - **B — done (2026-09-20).** Moved 3 leaf helpers (`_log_auto_run`, `_get_entry_excerpt`,
     `_entry_matches_rule`) into the new `services/automation_rules.py`; left `_is_local_dev_feed`
     behind (belongs to `refresh`, not automation). Wired the bottom-of-file import-back
     (`from services.automation_rules import …`, same pattern as `services/migration_common.py`) so
     `toggle_feed_tag_filter`, `_flush_email_batch_for_rule`, and `_run_on_star_destinations` (all
     staying in main) keep resolving them, and extended `routes/__init__.py`'s import-order docstring
     to name the new module. Retargeted `test_keyword_matcher.py`'s
     `test_dry_run_run_now_and_live_matching_share_one_matcher`: `_entry_matches_rule` copied
     `build_keyword_matcher` into its own module at import time, so `monkeypatch.setattr(main,
     "build_keyword_matcher", spy)` alone no longer reaches it — needed a second
     `monkeypatch.setattr(automation_rules, "build_keyword_matcher", spy)` alongside it. main.py:
     38,218 → 38,177 lines.
   - **C — done (2026-09-20).** Moved the 3 "run now" primitives (`_run_now_dedup`, `_run_now_pattern`,
     `_run_tag_filter`, ~460 lines) — no external side effects (local mark-read only), so a botched
     transition here was cheaply recoverable, done before D's external-I/O block. Left
     `parse_tag_filter_spec`, `author_filter_token`, `get_feed_tag_filter_rule`, and
     `toggle_feed_tag_filter` in main.py (the last two are route-side chip machinery, and
     `toggle_feed_tag_filter` is one of the still-in-main callers the import-back serves). Extended
     the bottom-of-file import-back and `services/automation_rules.py`'s own `from main import ...`
     block with the dedup/pattern/scope primitives all three functions need
     (`_resolve_dedup_feed_urls`, `_safe_dedup_collect`, `_safe_dedup_find_pairs`, `dedup_order_key`,
     `entry_url_slug`, `normalize_entry_title_for_dedupe`, `title_word_similarity`,
     `entry_effective_date`, `parse_folders_scope_id`, `resolve_rule_feed_urls`, `feed_display_title`,
     `normalize_tag_value`, plus `_DEDUP_MIN_TITLE_WORDS` for `_run_now_dedup`'s import-time default
     arg). Retargeted `test_dedup_entries.py`'s 4 `monkeypatch.setattr(main, "get_reader", …)` calls
     to `automation_rules.get_reader` — same copied-reference trap as Stage B. main.py: 38,177 →
     37,717 lines.
   - **D — done (2026-09-20).** Moved the six `_after_refresh` dispatchers + `_apply_youtube_playlist_rules`
     (~844 lines, the bulk) as one atomic delete+import — `email_article` (immediate) and `webhook`
     have no idempotency guard at all (only the 15-min cutoff), so a half-moved stub left "temporarily"
     would have risked duplicate sends/POSTs; `quire` likewise (rate-limited but not deduped);
     `instapaper`/`save_article`/`youtube_playlist` are all safe (URL/duplicate/INSERT-OR-IGNORE
     guarded). `_flush_email_batch_for_rule`, `_instapaper_save_url`, `_quire_add_entry`,
     `_star_entry_for_current_user`, `_is_youtube_short`, and the various credential/setting getters
     all stayed in main.py and are imported into the module the same way; `send_article_email` and
     the webhook/`youtube_embeds`/`youtube_oauth` helpers came straight from their `services.*`
     modules instead, since main.py already imported them that way. `_entry_matches_rule` and
     `_apply_youtube_playlist_rules` ended up with no caller left inside main.py itself (only
     `scripts/backfill_missed_youtube_playlist_adds.py` and tests reach them via `main.<name>`), so
     their import-back lines carry `# noqa: F401`. Retargeted 9 monkeypatches across 5 test files —
     `test_email_rule_automation.py`/`test_webhook_rule_automation.py` (`send_article_email`/
     `send_webhook`, both files' own characterization tests from Stage A), `test_instapaper_rule.py`
     (`_instapaper_save_url`, 3 sites), `test_quire_rule.py` (`get_quire_user_token`/
     `get_quire_usage_status`), `test_youtube_playlist_rules.py` (`get_youtube_oauth_token`, 2 sites
     — every other test in that file was failing on the shared fixture, not just the 2 that looked
     related), and `test_save_article_automation.py`'s `monkeypatch.setattr(main, "datetime", …)`
     time-travel patch — all the same copied-reference trap as prior stages, now also hitting
     `automation_rules`'s own `from datetime import datetime` binding. main.py: 37,717 → 36,873
     lines; `services/automation_rules.py`: 1,466 lines.
   - **E — not started.** Move `_run_automation_after_refresh` itself last (most main-resident call
     sites: scheduler tick, WebSub fan-out, bg-refresh thread, 3 routes) — do it after everything
     else is proven so only one function's wiring is unverified at a time. Retarget
     `test_dedup_fuzzy_threshold.py`'s `main._run_now_dedup` patch to target the module.
   Every stage's new/updated test must sort `import main` before `from services import
   automation_rules` (same circular-import rule as `routes/__init__.py`'s docstring documents for
   `migration_common`).
3. The 4 remaining context menus → templates.
4. Dedup engine → `services/dedup.py` — gate on "Consolidate the dedup routes" (Code health)
   getting characterization tests first.
5. Route modules by URL prefix — mechanical once the caches/locks above are confirmed importable
   as shared singletons (worth a `state.py` module first).
6. `ensure_meta_schema` (main.py:3708, ~1,328 lines) — low priority, do last.
7. Shared rendering core — its own project, not part of the mechanical split.
8. Refresh-hygiene cluster (`_is_youtube_short`, `_apply_hide_shorts`, `_apply_hide_paywalled`,
   `_apply_hide_members_only`, `_suppress_guid_churn`, `_cleanup_intra_feed_slug_dupes`, etc.,
   main.py ~8427–8924) → a future `services/feed_hygiene.py`. Deferred out of Step 2 because it has
   render-path and route callers unrelated to automation, and two of its functions overlap Step 4's
   planned dedup service.

### Page-fetch escalation ladder — follow-ups

- Broaden `extract_page_tags`'s recognized markup patterns as new gaps turn up (last full survey
  covered ~622 feeds; ~210 genuinely have no taxonomy, ~107 still blocked, mostly ArtStation's
  JS-heavy tag widget). No more broad surveys needed unless the untagged count grows a lot.
- Persist `HostEscalationState` to a `host_fetch_tiers` table if in-memory proves insufficient.
- Consolidate `feed_discovery._get_with_escalation` onto `PageFetcher` (currently a separate,
  older "honest then browser UA" implementation).
- Key `_autofetch_failed_hosts` on deepest-available-tier, same fix `HostEscalationState`'s
  cooldown got, if it matters in practice.

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

### Cross-feed duplicate scan tier — still not worth building

Cross-feed dupes (two legit subscriptions carrying the same article) measure at 23 groups
library-wide — not enough to justify a third `/saved/duplicates` UI section.

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

### Tag filtering for firehose feeds — follow-ups

`tag_filter` rule type is shipped (include/exclude feed-tag lists, any scope, auto-mark-read,
dry-run/run-now/history). Remaining:

- dev.to adapter: extend to multiple include tags (one API call per tag, merged/deduped by
  article id, exclusion applied client-side on `tag_list`).
- freeCodeCamp per-tag Ghost RSS (`/news/tag/<slug>/rss/`) as a fallback if include-list recall is
  insufficient.

### Post-header tag-filter chips don't reflect a folder/global-scoped rule

`get_feed_tag_filter_rule` only checks feed-scoped rules, so a feed covered only by a
folder-scoped rule shows unlit chips, and clicking one forks a brand-new disabled per-feed rule
instead of touching the folder rule. Fix shape: a scope hierarchy the chip lookup walks (feed →
folder → global) with a governing-level indicator in the chip UI.

Blocked on a product decision, not missing investigation: should clicking a chip when only a
folder rule covers a feed edit that folder rule (affects every feed in it), or create an explicit
feed-level override? Sizing once decided: small — a lookup-order change to
`get_feed_tag_filter_rule` plus one new branch in `toggle_feed_tag_filter`.

### Article cleanup — Phase 2: promote a removal into a per-feed rule

Phase 1 (manual per-article cleanup via the pane's 🧹, with `entry_content_edits` recording both
the pristine body and the replayed ops) is shipped. Phase 2 would add a `feed_content_rules` table
+ render-time matcher, a Cleanups section in Feed Properties showing match counts before promoting,
and selector derivation from the recorded ops.

Measured: corpus too small to build on yet (4 edited entries / 67 ops), and ~30% of ops can't
generalize past a bare tag (no id/class to key on) — a promotion UI needs to show which ops are
promotable. Any future rule should key on entry-link host, not `feed_url` (most edits are on the
`lectio:saved` pseudo-feed, which spans every site). Re-measure once there are edits on ≥3 entries
of the same real feed.

### Refetch-All has no "already re-fetched recently" skip

No column records last-refetch time today (`published`/`first_updated`/`edited_at` all mean
something else). Sizing if ever wanted: small — one new column (needs the per-user startup
migration) + a skip check in the Refetch-All loop. Not requested yet — not scheduled ahead of
demand.

### Archive capture failures are indistinguishable from real empty content

`_archive_entry`'s source fetch (`_fetch_text_with_url`) swallows every exception identically —
a 404, a 403, a TLS error, anything — and stores an empty "complete" archive with no error
recorded. Found 2026-09-19 auditing 633 such rows live: mostly dead links, a handful recoverable,
but nothing short of a manual per-entry fetch could tell which was which beforehand. Not sized —
worth recording which kind of failure happened (or at least logging above DEBUG) so a future case
doesn't need the same manual audit, but the actual design (a status column? just better logging?)
isn't decided yet.

### Read Mode: no Back guard

`/read` has no equivalent of the main app's Back-button guard. Not cheap: `/read` has no drawer
for Back to land on, and a Back that visibly does nothing is worse than one that exits the app.
Give Read Mode a collapsible folder tree first, then add the guard.

### Page-weight reduction — follow-ups

- Entry-pane loading state/timeout — slow pane loads still look like dead clicks.
- Optional: a render-splitting/fragment endpoint for `.pane-posts`/`.pane-entry` (pane-swap
  currently re-renders the full page server-side per fetch, ~200KB).

### Offline actions — two pieces left

- **Stale-action guard.** Today's conflict rule is last-writer-wins; "accept the server's version
  if it already moved" needs a per-entry modification timestamp the schema doesn't carry. Low
  urgency (the only conflicting writer is Josh on another device, within minutes) — do only if a
  surprising revert is actually observed.
- **Offline star/unstar.** Scoped but not built — the reader has no star control today, only
  Archive (unstars) and Delete. UI question first; Read Mode deliberately has few controls.

Deliberately *not* built: a `synced_actions` idempotency table — the four outbox routes are
already idempotent set-state operations, so replaying one is a no-op.

### Email "full article text" doesn't run Readability on thin-stub feeds

The full-text Email Article option only pulls stored content — still a thin email for a
thin-stub feed. meetingcpp.com (see "Full-content fetch at ingest" below) is the concrete example.
Scope: at send time, if the stored body is thin, run the same readability fetch Save/re-fetch
already uses. Sequence after the ingest item below so both share one "thin" threshold rather than
inventing two.

### Email template overhaul

Josh wants to revisit the emailed-article template's look. No specifics yet — needs his input on
what to change before this can be scoped.

### One stored image per entry, but three feeds want two

Three comic feeds want a different image in the list than in the article; Lectio stores one URL
per entry and derives the list crop from it. Two of three needed a plugin/derivation (Penny
Arcade, dresdencodak); the third needed nothing (`media_rss` already picks up the right
publisher-supplied thumbnail) — check what a feed already provides before writing a plugin. General
fix — a second stored URL + a per-feed "thumbnail source" setting — needs the startup migration;
worth doing when a fourth feed wants it, not before.

### Full-content fetch at ingest for body-less feeds

meetingcpp.com's feed went title+link-only (CMS change, upstream). A per-feed "fetch full content
from the source page at ingest" opt-in (readability pipeline already exists), capped/throttled
like enhancement, would fix such feeds generally. Also the concrete example the Email item above
needed.

### FakeFeedz `content_selector` has no edit UI for existing feeds

Shipped 2026-09-19 (link_list body-fill from the entry's own page, bypassing readability/full-page
guessing) but only settable at feed creation in the Add Feed dialog — changing or adding it on an
already-created scraped feed needs a direct DB edit today. Worth a "Page Feed" tab in Feed
Properties if this comes up again; not before.

### Send-to-destination — remaining candidates

Rule engine + on-star fan-out + shared destination senders are shipped (Instapaper, YouTube
playlist, email, Quire, Pinterest). Build more only if actually wanted: save-to-tag/starred-archive
as a rule action, Readwise/Reader, Wallabag. Each is small, reusing the existing engine.

Readit (wereadit.com): send-to-Readit is blocked — their save endpoint is unreachable outside
their own extension (Cloudflare). Import is blocked until they expose an export/API. The reverse
direction (Lectio receiving from the Readit extension's save protocol) already works today.

### Global audio player — deferred v2 ideas

Queue/playlist across a folder, remember position per episode, Media Session API (lock-screen
controls), speed presets.

### Single-post pages: fix raw/full-page capture quality

Some "feeds" are really one standing document (e.g. a single tutorial page), saved via a
manufactured feed. The capture-quality half is worth fixing (readability can return a small
fraction of such a page, and the wrong node); the workflow-simplification half is superseded —
Josh's preference is filing such pages into an existing related feed, which auto-filing already
does in bulk.

## Tier 5 — deliberately deferred / big investments

**Architecture**

### Single-user mode does not exist anymore — retire DEFAULT_USER

`DEFAULT_USER_ID` still silently resolves any unbound code path to stale legacy top-level DBs
instead of failing loudly — quietly-wrong answers, not an error. Fix: default the
`lectio_current_user` ContextVar to `None`, raise on unbound resolution, then delete the legacy
path branches and stale DB files. Not small: 54 references outside `tenancy.py`/`tests/`. Wants
its own PR and a check of the per-user startup migration. Related: the bg-thread tenancy rule
already in place (`_run_in_user_context`).

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

- **Flaky test:** `test_add_route_accepts_blank_keyword` failed once, unreproduced since — likely
  a background thread racing the test DB. Not chased.
- **Dead code:** the dormant in-app star-mode tree/JS the Read Mode hijack bypasses.
- **`ruff format`'s opt-in `docstring-code-format`** is inert until (if) `ruff format` itself gets
  adopted project-wide — that's the real decision, weighed against whatever the original reason
  was for leaving formatting unenforced (most likely: noise-free diffs). If adopted: one isolated
  reformat commit, hash added to `.git-blame-ignore-revs`.
- **Wrap saved-dedup storage access** (Sourcery) — the Saved duplicate scan reads reader's entries
  table directly; a thin storage-layer wrapper would localize breakage if reader's schema evolves.
- **Consolidate the dedup routes** — partial (shared feed-URL prologue extracted). The
  match-method bodies still diverge by preview-vs-apply output; a full merge is deferred pending
  broader characterization tests (dedup correctness is behavior-sensitive).
- **`ensure_meta_schema`** (main.py:3708, ~1,328 lines) — long but linear (CREATE + idempotent
  ALTERs), low churn. A by-area split is cosmetic. Same function the breakup's Step 6 targets —
  keep this bullet and that step in sync.
- **Backfill Sphinx-math height on already-stored entries** — the ingest-time fix doesn't
  retroactively help entries stored before it; low value (few math articles), do on demand. Note:
  `entries.content` is reader's JSON structure, not raw HTML — a backfill must respect that shape.

## Watch-lists

Nothing here is scheduled — just what to check if a related symptom recurs.

### CodeQL board

9 open alerts from PR #329 (the main.py/index.html breakup, Step 1): "information exposure through
an exception" on `return JSONResponse({"error": str(exc)}, ...)` in the moved
`routes/integrations_*.py` files. Not new — the same pattern (`"error": str(exc)` in an exception
handler) already exists 15+ times in main.py; CodeQL flags them because the lines are new *files*,
not new *code*. Left open rather than dismissed or fixed inline (decided when triaging the PR); a
real fix means picking a message for each call site that's still useful in the UI (several surface
the caught exception directly, e.g. "Sync failed: {result['error']}"), so it's its own pass across
every instance — including the ones still in main.py, not just the 9 CodeQL happened to flag — not
scope for this refactor. Notes for next time on other alert classes:

- A negative lookahead will not clear a ReDoS alert — CodeQL's regex model ignores lookaheads.
  Write the loop lookahead-free or move the scan into Python.
- Committed page fixtures are excluded from analysis (`paths-ignore: tests/fixtures`) — a captured
  page is byte-for-byte what a site served, so analyzing it reports the remote site's choices as
  ours.
- If the reflective-XSS class keeps recurring, `.github/codeql/queries/` already has the pattern
  for a guard-aware custom query (see the SSRF/path-injection ones modeling our sanitizers as
  barriers).

### Feed-tag suggestion suppression — do not attempt a third heuristic

Tried twice, reverted both times: coverage-based suppression wrongly caught legitimate filing tags
(a feed can put one tag on every post and still have that be the right tag); feed-name-echo broke
on the real, non-assumed feed title. The distinction (a tag naming a *place* vs. a *kind of
content*) is semantic and no feed metadata expresses it. Resolved with manual per-(feed, tag)
dismissal instead (`suppressed_feed_tags`).

### `_resolve_view_posts` enrichment cost — measured, not worth building

Enrichment is ~24% of a whole-view resolve, not the dominant cost (the raw fetch is >2.5x larger);
a light-record mode isn't a clean drop-in since `select-all-visible` needs enrichment-phase fields.
Not worth building for the payoff.

### og_scrape feeds with no og:image at all

162 of 585 auto-detected `og_scrape` feeds have no `og:image` on their source page and correctly
fall back to a body image. Not broken — just where a future "odd body image" report will come from.

### Free-threaded Python is blocked by lxml

lxml hasn't declared free-threading support, forcing the GIL back on at import. Recheck when it
does; nothing else blocks it.

### Methodology: diagnosing refresh-contention/latency stalls

Elapsed-time SQL timing can't distinguish SQLite lock-wait from GIL starvation — live `py-spy`
sampling during a real stall settles it faster. Reach for this first, not last.

### Parked, deliberately

Nothing to do here until one of these recurs or a lead turns up.

- Sort briefly reverts to default after a backgrounded tab is restored, self-corrects within ~90s
  (Surface/Edge) — traced to a real top-level nav from a stale history entry, not a
  stored-preference bug; no reliable repro yet.
- neowin.net: 20 entries briefly shared one lead image, repaired live — likely a transient
  upstream glitch, no code-level cause found.
- On-screen-keyboard popup scrolls the post list to top — not reproduced in Playwright with
  viewport-resize emulation; needs a real-device screen recording.
- An entry ([play.nobleknight.com/?p=19266](https://play.nobleknight.com/?p=19266)) took a long
  time to open once — inconclusive, no repro caught; check entry-pane response time and `[perf]`
  logs if it recurs.
- Soundslice tab-player embeds are permanently blocked by the content owner's own domain
  allowlist — no fix available short of screenshotting the original page through a real browser;
  not worth it for one narrow case.
- `make rebuild` cycle is 100-130s — not investigated; revisit if it keeps coming up.
- 5 tests fail locally with `outbound network blocked in tests` (reproduces on a clean commit too,
  smells like sandbox-specific networking) — check real CI is clean before spending time on it.
- makeuseof re-fetch returns white images — seen once, waiting for a second sighting.
- play.nobleknight.com images 403 despite FlareSolverr cookie reuse — the solve never returns the
  actual `cf_clearance` cookie for this host; would need per-image browser routing.
- guitarworld.com lessons: a MatchMySound practice widget is entirely missing from capture
  (likely a client-side paywall gate defeated by Josh's own adblocker) — one article, not chased
  further.
- A second raw-Markdown-instead-of-HTML feed — unconfirmed, Josh recalls a similar symptom but
  couldn't place which feed. The existing fix (from the blog.gitea.com case) already covers it
  generically if confirmed.
- ~407 stored feed URLs differ from canonical only by a trailing slash — harmless, zero actual
  collisions.
- A re-fetch once returned a different, unique article and the sibling-text guard didn't catch it
  (entry 26031) — the slug/title mismatch guard should have and didn't. Investigate if it recurs.
- The scheduler's trickle case: a host feeding one byte per 29s could keep a refresh pass
  "advancing" without real progress. If it recurs, the fix is a per-feed wall-clock budget, not a
  shorter read deadline.
- The Wayback timestamp as a date source returns the closest snapshot, not first-capture — the CDX
  API sorted ascending would be more accurate but timed out on 2/3 tries.
- Inline SVG in feed content is mangled at ingest (feedparser mishandles self-closing tags) — any
  feed shipping inline SVG art renders as a flat color block. Real fix needs re-parsing SVG
  subtrees as XML at ingest.
- Article-nav post-swap binder exception — mitigated (no more hard-reload on failure), but the
  underlying binder exception still exists somewhere. Grab the console error if it recurs.
