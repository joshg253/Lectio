# Lectio Plan

Open work only. Anything shipped lives in git history and, where it still
explains why the code looks the way it does, in ARCHITECTURE.md.

## Now

Five tiers: actively impeding unread-clearing, small independent wins, maintenance backlog ready
to run, real features not blocking anything today, and deliberately-deferred big investments.
Within a tier, related items are clustered under a bold sub-heading. Two watch-lists (CodeQL,
Parked) sit at the end — nothing there is scheduled, just what to check if a symptom recurs.

Tiers 1 through 3 are empty. The main.py/index.html breakup (Integration routes cluster,
post-refresh automation pipeline, index.html's context menus) is done, shipped 2026-09-19/20. Its
former "Steps 4-8, unscoped follow-on work" is now split into independent Tier 4 projects —
dedup-routes consolidation, a `state.py` module + route split, and the shared rendering core.

## Tier 1 — actively impeding unread-clearing

Empty.

## Tier 2 — small, fast, independent wins

Empty.

## Tier 3 — maintenance backlog, ready to run

Empty.

## Tier 4 — real features, not blocking anything today

### main.py / index.html breakup — done

Moved the Integration routes cluster (~44 routes + workers) into `routes/integrations_*.py` plus
`services/migration_common.py`/`services/inoreader_import.py`, moved the post-refresh automation
pipeline into `services/automation_rules.py`, and moved index.html's last 4 inline context menus
into `_context_menus.html` — `index.html` is now all `{% include %}`s plus page-level structure.
main.py: 40,474 → 36,737 lines. Stage-by-stage detail and the gotchas hit along the way (the
copied-reference monkeypatch trap, the `import main`-must-sort-first circular-import rule, etc.)
are in the commit history (PRs #329-#331) and `routes/__init__.py`'s docstring, not repeated here.

**Landmines that still apply to any further main.py extraction:** a module that imports a singleton
cache/lock must not redefine it, and every `invalidate_*` call site has to stay wired to the same
instance. `get_reader()` thread-local pooling and `lifespan` are startup-order-sensitive. A scalar
rebound via `global` (a counter, an in-flight flag) has a *read*-side version of the same landmine,
not just a write-side one: `from state import _some_counter` freezes a snapshot at import time, so
any bare *read* of it elsewhere goes stale the same way an unconverted `global` write would — both
need an accessor function, not a bare imported name (found while building `state.py`, below).

### Dedup routes consolidation → `services/dedup.py`

Gate: the dedup routes' shared feed-URL prologue is already extracted, but the match-method bodies
still diverge by preview-vs-apply output — full consolidation is deferred until there are broader
characterization tests (dedup correctness is behavior-sensitive). Once that lands, pull the
consolidated engine into `services/dedup.py`, and fold in `_suppress_guid_churn` and
`_cleanup_intra_feed_slug_dupes` (main.py:8062-8270, refresh-time guid/slug dedup) at the same
time — same problem space, avoids moving them twice. The three unrelated hide-* hygiene functions
next to them in main.py (`_is_youtube_short`, `_apply_hide_shorts`, `_apply_hide_paywalled`,
`_apply_hide_members_only`, main.py:7952-8447 minus the two above) aren't dedup — decide at
extraction time whether they're worth carrying along in the same pass (adjacent code, same
refresh-pipeline callers) or splitting off into a later `services/feed_hygiene.py`.

### `state.py` module — done; route split by URL prefix still open

Moved all of main.py's module-level singleton state into a new `state.py` (9 `_PerUserDict`
caches + the class, ~20 plain dict/set/list caches with their locks, and 4 scalars previously
rebound via `global`) with a single top-of-file `from state import (...)` back in main.py — no
circular-import risk, since `state.py` depends on nothing in main.py. main.py: 36,737 → 36,499
lines; `state.py`: 394 lines. The actual counts were 9/31 `_PerUserDict`/`Lock` instances, not the
original 10/32 estimate (stale from before Step 2's edits); the `global`-rebound scalars needed
real accessor functions, not just relocation — see the Landmines note above, and
`get_unread_counts_generation()`/`try_start_unread_refresh()`/`clear_unread_refresh_inflight()`/
`next_refresh_rotation_offset()`/`check_manual_refresh_cooldown()` in `state.py` for the shape.
`_PerUserDict` itself needed an explicit `# noqa: F401` re-export — nothing in main.py's own code
references the class by name anymore (only specific instances), so `ruff --fix` tried to prune it
as unused, breaking `routes/integrations_youtube.py` and `tests/integration/test_cache_isolation.py`,
which still do `main._PerUserDict()`. Full `make test`/`lint`/`types` pass.

Next: split the rest of main.py's route handlers by URL prefix into their own modules — same
`router = APIRouter()` + bottom-of-file `include_router` pattern the integration routes used, now
that the shared state they'll need is importable from `state.py` without redefining it. **Scoped
2026-09-20:** 256 `@app.*` route decorators remain, and unlike the integrations cluster (one
contiguous 2,240-line block) these are scattered across the whole file — same URL prefix shows up
in disjoint chunks hundreds/thousands of lines apart (e.g. `/feeds/*` routes span main.py:25294 to
31671). So each stage below is "gather every route matching these paths, wherever it lives" rather
than "cut one block." Grouped by conceptual area (not always the literal first path segment) and
ordered safest → riskiest:

1. **Done (2026-09-20).** `routes/system.py` — `/healthz`, `/sw.js`, `/stats`, `/login`,
   `/logout`, `/websub/callback`, `/opml/{export,import}`, `/takeout/{export,import}`, `/thumb`,
   `/starred-asset/{asset_hash}`, `/internal/warm-lead-image-cache`, `/email-contacts*`,
   `/dev/feeds/*` + `/dev/flush-email-batch`, `/instapaper/import`, `/youtube/sync`,
   `/devto-feeds/{feed_id}/config`, `/administration` — 30 routes moved cleanly, none deferred.
   main.py: 36,499 → 35,128 lines; `routes/system.py`: 1,554 lines. A real bug the mechanical move
   would have introduced silently: `offline_service_worker` (serves `/sw.js`) used
   `Path(__file__).parent`, which would have resolved to `routes/` instead of the app root once
   moved — rewritten to use the existing `BASE_DIR` constant instead. Bottom-of-file import needed
   `routes.system` placed *after* `services.automation_rules` (it needs
   `_run_automation_after_refresh` bound into main's namespace first) plus `# noqa: I001` on the
   block to stop ruff's isort from re-sorting it earlier. 13 test files retargeted for the two
   known gotchas — `test_websub_fanout.py` was the sharpest case, needing `websub_service` and
   `_run_automation_after_refresh` monkeypatched on *both* `main` and `routes.system` since
   `_process_websub_push` reads both through its own copied-reference import. Full
   `make test`/`lint`/`types` pass (4,192 tests).
2. **Done (2026-09-20).** `routes/compat_{fever,greader,v1}.py` — 2/14/11 = 27 routes, all
   confirmed thin wrappers over `fever_service`/`greader_service`/`miniflux_service` (the v1 compat
   surface is Miniflux-protocol, not a distinct thing — no `services/v1.py` needed). main.py:
   35,128 → 34,576 lines; new files 115/281/223 lines. `_run_in_user_context`,
   `_spawn_feed_enhancement`, and `_enhance_feeds_background` sat inside the same file region but
   are shared with other still-in-main.py call sites (6 other `routes/integrations_*.py` modules,
   4 scripts, 3 other main.py call sites) — stayed in main.py, imported back like any other
   main-resident helper, the region's stale "GReader API" comment retitled to reflect that. No
   `services.automation_rules` ordering constraint needed (unlike Stage 1) — none of these handlers
   touch a late-bound name. 2 test files retargeted, one hitting each known gotcha:
   `test_greader_subscription_edit.py` (pure relocation, `main._greader_edit_subscriptions` →
   `compat_greader._greader_edit_subscriptions`) and `test_miniflux_api.py` (copied-reference
   monkeypatch, same shape as Stage 1's `test_websub_fanout.py` — `user_store` patched on both
   `main` and `compat_v1`). Full `make test`/`lint`/`types` pass (4,192 tests). Aside: main.py (and
   now these 3 files) use bare `except TypeErrorType, ValueErrorType:` (no parens) throughout —
   looked like leftover Python 2 syntax, but it's valid on Python 3.14 (confirmed: parses,
   compiles to the same tuple form as `except (A, B):`, doesn't rebind the second name) — a
   pre-existing repo-wide pattern, left alone.
3. **Done (2026-09-20).** `routes/tags.py` (`/tags/*` + `/feed-tags/dismiss`, 11 routes) and
   `routes/highlights.py` (`/highlights*`, 9 routes). main.py: 34,576 → 33,881 lines; new files
   275/521 lines. `normalize_tag_value` (the 51-call-site helper the Landmines note already flags)
   confirmed left in main.py and imported back, not moved, despite living right next to the tags
   routes — same for its whole neighborhood of alias/rename/delete helpers, all tested directly as
   `main.<name>` elsewhere. `_validate_highlight_rule`/`_highlight_rule_response` (shared only
   between add/edit) moved with the highlights routes. No `services.automation_rules` ordering
   constraint needed for either module. 3 test files retargeted — all pure relocation (a test
   registering a moved handler directly on a bare test `FastAPI()` app via `main.<handler>`, now
   `routes.tags.<handler>`/`routes.highlights.<handler>`), no copied-reference-monkeypatch case
   surfaced this time. Full `make test`/`lint`/`types` pass (4,192 tests).
4. **Done (2026-09-21).** `routes/automation.py` — `/automation/history*`, `/rules/*`,
   `/dedup/false-match*` (9 routes). main.py: 33,881 → 33,285 lines; new file 677 lines. Imports
   `_run_tag_filter`/`_run_now_dedup`/`_run_now_pattern` straight from `services.automation_rules`
   rather than round-tripping through main.py. `_dry_run_dedup`/`_dry_run_pattern` (the `/rules/dry-run`
   preview engine, main.py ~7307-7704) moved too — each had exactly one caller (the dry-run route)
   so qualified as single-route-only despite being conceptually "dedup engine"; full preview/apply
   consolidation stays the separate, still-gated "Dedup routes consolidation" project above.
   `/entries/feed-tags`, sitting inside this same file region, correctly stayed put — entries
   concern, not automation, despite physical proximity. Ordering: empirically verified (temporarily
   moved the import, ran `python -c "import main"` both ways) that `services.automation_rules`'s
   own dependencies aren't late-bound the way `_run_automation_after_refresh` was for Stage 1, so
   strictly the ordering constraint doesn't bite here — kept `routes.automation`'s import positioned
   after `services.automation_rules` anyway, defensively, same reasoning as Stage 1. 6 test files
   retargeted, one hitting a three-way copied reference on `build_keyword_matcher` (bound
   separately into `main`, `services.automation_rules`, and now `routes.automation`, each via its
   own `from main import build_keyword_matcher` — `test_keyword_matcher.py` needed all three
   patched). Full `make test`/`lint`/`types` pass (4,192 tests).
5. **Done (2026-09-21).** `routes/admin.py` — `/account/*` (3), `/admin/users/*` + `/admin/logs`
   (7), `/debug/*` (5) — 15 routes. main.py: 33,285 → 32,913 lines; new file 447 lines. Security
   check (this cluster does real auth/account mutations, so worth confirming rather than assuming):
   `_CSRFMiddleware` and the auth session gate are both `app.add_middleware`-level, keyed off the
   request path string (`_CSRF_EXEMPT_PREFIXES`, main.py:2651), not which router module registered
   a handler — moving a route between files can't change its CSRF/auth exposure as long as the URL
   path is unchanged, verified by reading the middleware directly rather than trusting the stage's
   own claim. Password hashing/`UserStore` singleton untouched, just imported back.
   `_read_log_tail`/`_log_line_dt`/`_parse_local_ts` deliberately NOT moved despite `admin_logs`
   being their only route-caller — `tests/unit/test_admin_log_tail.py` calls them directly as
   `main.<name>`, same "exercised directly by a dedicated test file" precedent Stage 3 set for
   `get_highlight_keywords`. `delete_user_storage` also stayed (flagged as a judgment call: only
   one remaining caller, but it's the lifecycle-pair sibling of `provision_user_storage` ~19,000
   lines away in a shared "user storage lifecycle" section — kept the pair together rather than
   split one out). No `services.automation_rules` ordering constraint needed. No test files
   required retargeting for either gotcha — nothing imports `routes.admin` directly, and the one
   suite exercising these routes (`tests/integration/_multiuser_harness.py`) goes over real HTTP via
   `TestClient(main.app)`, so the module split is transparent to it. Full `make test`/`lint`/`types`
   pass (4,192 tests).
6. **Done (2026-09-21).** `routes/saved.py` — `/saved/*` + `/articles/*`, 22 routes (Plan.md's
   "~21" was approximate) including the far-flung `POST /saved/folder/clear-curation` outlier that
   sat ~19,000 lines from the rest of this cluster. main.py: 32,913 → 31,965 lines; new file 1,099
   lines. **Not a thin wrapper**, unlike Stage 2's greader/fever — only the capture path
   (`save_article`/`refresh_captured_article`) is backed by `services/saved_articles.py`; the
   cross-feed dupe scan, autofile planner, unstar-tagged/archive-old planners, and scoped
   batch-refetch job are all substantial main.py-resident logic with no service-layer home, and
   mostly stayed in main.py (tested directly by dedicated test files, or shared with `/api/save`,
   `/entries/saved`, and other still-in-main.py routes that didn't move this stage) rather than
   moving with their single route. **Process gap found and worth carrying into every remaining
   stage**: two helpers (`_current_autofile_plan`, `_saved_dup_groups`) were nearly left broken
   because they're also called from *other* already-moved route modules (`routes/system.py`) and
   from `scripts/*.py` — grepping `main.py` and test files for a helper's callers isn't enough,
   `routes/*.py` and `scripts/*.py` need checking too before deciding something is single-route-only
   (verified both are still correctly main.py-resident and re-imported everywhere they're used).
   No `services.automation_rules` ordering constraint needed. 10 test files retargeted — heavy
   gotcha traffic as expected given this area's test-coverage history (Plan.md's
   `saved-articles-epic`/`saved-dedup-workflow` history), including one case where a helper itself
   moved (`_check_saved_url`) so only `routes.saved` needed patching, not `main`. Full
   `make test`/`lint`/`types` pass (4,192 tests).
7. **Done (2026-09-21).** `routes/settings.py` — `/settings/*`, 14 routes (Plan.md's "~11" was
   low). main.py: 31,965 → 31,247 lines; new file 941 lines. Two
   clusters, exactly where scoped, no third outlier this time (grepped literal path strings across
   all of main.py to confirm). `/tree/folder-feeds/{folder_id}` sits sandwiched inside the second
   cluster but is a sidebar-fragment route, not settings — confirmed left alone. Only one
   route-adjacent helper existed (`_keep_existing_sensitive`), stayed in main.py per the
   tested-directly precedent; everything else these 14 routes touch (the whole settings-getter
   family, `SETTING_*` constants, `FeedInFolder`, etc.) is pre-existing shared infrastructure,
   confirmed via the three-way `routes/*.py`/`scripts/*.py`/`tests/` grep Stage 6 established as
   the real bar — none of it moved. No `services.automation_rules` ordering constraint. 3 test
   files retargeted, none of them via the usual `monkeypatch.setattr(main,` grep — this stage's
   variant was tests registering a moved handler by name (`main.<handler>`) onto a bare test
   `FastAPI()` app, caught by `make types`/`make test` failures rather than a grep pattern; one test
   was reading main.py's raw source text as a live fallback and got rewritten to
   `inspect.getsource(routes.settings.save_all_settings)`, arguably more correct than before.
   **Process incident, not a code issue**: the agent ran `rm -rf /tmp/*` by hand while chasing a
   test issue instead of using `make test`'s own `clear-scratch` step — no project files were hit,
   but `/tmp` is a shared host-wide tmpfs, so this was flagged and a standing feedback note added
   (`feedback-subagent-no-manual-tmp-clear` in project memory) to brief every future stage against
   it explicitly. Full `make test`/`lint`/`types` pass (4,192 tests).
8. `routes/feeds.py` — biggest single cluster, 60 routes confirmed 2026-09-21 (spanning
   main.py:23641-28244, plus `/tree/folder-feeds/{folder_id}` and `/api/folder-feeds` as outliers
   around 30367/30623 — always re-grep, these numbers drift every stage). Scoped into its own A-E
   sub-stages, same reasoning as the original integrations cluster (safest → riskiest):
   - **A — done (2026-09-21).** Folder CRUD + tree reads: `/api/folders`, `POST /folders`,
     `/folders/rename`, `/folders/delete`, `/folders/properties`, `/folders/cadence`,
     `/folders/retention`, `/folders/mark-read`, `/tree/folder-feeds/{folder_id}`,
     `/api/folder-feeds` (10 routes, exactly as scoped, no discrepancy). main.py: 31,247 → 30,991
     lines; `routes/feeds.py` created at 346 lines (sub-stages B-E extend the same file). No
     genuinely single-route-only helper existed next to any of the 10 — everything touched was
     either general infra or already independently tested, confirmed clean via the
     `routes/*.py`/`scripts/*.py`/`tests/` three-way check. No `services.automation_rules` ordering
     constraint needed. 2 test files retargeted for `POST /folders/mark-read` →
     `routes.feeds.mark_folder_as_read`, one hitting both known gotchas (4 helpers needing a second
     monkeypatch on `routes.feeds` alongside `main`). Full `make test`/`lint`/`types` pass.
   - **B — done (2026-09-22).** Feed discovery/add flow: `/feeds/discover`, `/feeds/compare`,
     `POST /feeds`, `/scraped-feeds*` (5), `/feeds/properties`, `/feeds/suggest-migration`,
     `/feeds/set-user-title`, `/feeds/fix-url-titles`, `/feeds/lazy-titles` (13 routes, exactly as
     scoped). main.py: 30,991 → 30,520 lines; `routes/feeds.py`: 346 → 898 lines (23 routes total
     across 8A+8B). **Not a thin wrapper**, closer to Stage 6 — `create_feed` has real branching
     (dev.to, DeviantArt watch-vs-gallery, discovery-refusal classification, browser-UA escalation)
     and the `/scraped-feeds` cluster does meaningful validation/orchestration around
     `services/scraper_service.py`, not pure pass-through. Only 2 genuinely single-route helpers
     moved (`_is_youtube_url`, `_site_name_from_feed_url` + its constants); everything else stayed
     in main.py — several confirmed shared with other already-moved route modules
     (`_devto_config_from_form` with `routes/system.py`; `get_deviantart_user_token`/
     `get_deviantart_credentials` with `routes/integrations_deviantart.py`/`routes/settings.py`),
     one confirmed via a `scripts/*.py` caller (`_is_youtube_host`). Verification went beyond the
     usual three checks: FastAPI 0.141 wraps included routers in a lazy object so `main.app.routes`
     no longer flattens sub-router routes (a dead end chased and ruled out), so correctness was
     confirmed instead with a live `TestClient(main.app)` hitting all 13 moved paths for real
     200s. 4 test files retargeted for the usual two gotchas. Full `make test`/`lint`/`types` pass.
   - **C.** Feed display/thumbnail strategy config: `/feeds/strategy`, `/feeds/display-prefs`,
     `/feeds/backfill-hide-shorts`, `/feeds/thumbnail-url`, `/feeds/thumb-crop`,
     `/feeds/smart-min-scale`, `/feeds/fill-zoom`, `/feeds/thumb-strategy`, `/feeds/caption-source`,
     `/feeds/strategy-refresh` (10 routes) — likely touches `services/lead_image_plugins.py`.
   - **D.** Feed network/fetch settings + lifecycle: `/feeds/browser-ua`, `/feeds/proxy`,
     `/feeds/tailscale`, `/feeds/flaresolverr`, `/feeds/reparse`, `/feeds/move`, `/feeds/disable`,
     `/feeds/enable`, `/feeds/toggle-updates`, `/feeds/change-url`, `/feeds/unsubscribe`,
     `/feeds/curation-count` (12 routes).
   - **E.** Feed tags/attachments/website/curation/bulk ops — riskiest, saved for last: 
     `/feeds/suggested-tags`, `/feeds/attachment-candidates`, `/feeds/attachment-candidate-suppress`,
     `/feeds/attachment-exts`, `/feeds/set-website`, `/feeds/url-rewrites`,
     `/feeds/url-rewrites/delete`, `/feeds/curation-items`, `/feeds/combine`, `/feeds/duplicates`,
     `/feeds/duplicates/undismiss`, `/feeds/duplicates/dismiss`, `/feeds/multi-folder`,
     `/feeds/multi-folder/resolve`, `/feeds/bulk`, `/feeds/mark-read` (16 routes) — `/feeds/combine`
     and `/feeds/duplicates*` are **feed-level** duplicate detection, a third distinct dedup surface
     from Stage 6's saved-article dupe scan and the still-gated main entry-dedup engine — don't
     conflate the three when scoping this sub-stage.
9. `routes/entries.py` — `/entries/*` (~46) plus the `/api/*` thumb/img/bookmarklet-save cluster
   (`/api/entry-thumb`, `/api/favicon`, `/api/feed-thumb`, `/api/img`, `/api/bookmarklet/save`,
   `/api/save`, `/api/unread-counts`) if that doesn't want to be its own `routes/media.py` —
   decide at extraction time. Also biggest; needs its own sub-staging.
10. `routes/home.py` — `/`, `/read`, `/read/offline` last: these are the routes the Landmines note
    already flags as reused-by-everything (`_home_inner`, `build_reader_page`, pane-swap); moving
    the handler is still just importing the core functions back from main.py like everything else,
    but do it only after the pattern is proven on lower-traffic routes first.

Each stage: `grep` the current route paths (line numbers drift as earlier stages move code, so
don't trust line numbers from a previous stage's scoping), move handler + any single-route-only
helper, leave shared helpers in main.py and import them back, watch for the two known gotchas
(copied-reference monkeypatches in tests that reach a handler via `main.<name>`, and the
import-main-before-routes circular-import rule `routes/__init__.py` documents), run
`make test`/`lint`/`types` after each stage.

### Shared rendering core

`_home_inner` (main.py:25598), `list_entries_for_feeds` (15994), `get_entry_detail` (19755), and
`build_reader_page` (23797) are reused by `/`, `/read`, pane-swap, and the greader/fever/v1 compat
APIs. Not a mechanical split — its own carefully-tested project.

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
- **Consolidate the dedup routes** — see the "Dedup routes consolidation" project in Tier 4
  (main.py/index.html breakup follow-ons); tracked there now since it also gates a
  `services/dedup.py` extraction, not just this cleanup.
- **`ensure_meta_schema`** (main.py:3708, ~1,328 lines) — long but linear (CREATE + idempotent
  ALTERs), low churn. A by-area split is cosmetic.
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
