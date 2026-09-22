"""Route modules extracted out of main.py (Plan.md's main.py/index.html breakup).

Each module here does `from main import ...` for shared connection/setting/
credential helpers and defines `router = APIRouter()`; main.py imports these
modules and calls `app.include_router(...)` near the bottom of the file,
after every name a route module needs is already defined.

Gotcha: this only resolves in the direction main.py -> routes.*. Anything
that imports a routes.integrations_* module (or routes.system) directly
(tests do this to reach handler functions or monkeypatch their main-derived
bindings) must `import main` first — otherwise the routes module's own `from
main import ...` kicks off main.py's execution while the routes module is
still mid-import, and main.py's bottom-of-file `from routes.integrations_x
import router` fails with a circular-import error because that module hasn't
defined `router` yet.

`routes/system.py` (Stage 1 of the follow-on route-by-URL-prefix split:
healthz/stats/login/logout/administration/dev-feeds/thumb/starred-asset/
OPML/takeout/instapaper/websub/warm-lead-image-cache) is imported even later
than the integrations modules — after `services.automation_rules` — because
it does `from main import _run_automation_after_refresh`, which only exists
in main.py's namespace once that import has already run.

`routes/compat_fever.py`, `routes/compat_greader.py`, and `routes/compat_v1.py`
(Stage 2: the Fever, GReader, and Miniflux-v1 external API compat surfaces)
have no such ordering constraint — none of their handlers call anything from
the late `services.automation_rules`/`services.inoreader_import` imports — so
they're imported alongside the plain `routes.integrations_*` modules.

Same gotcha, one level deeper, for `services/migration_common.py`: it also
does `from main import ...` at module level (for `canonical_feed_url`,
`get_reader`, etc. — general primitives with no other home yet) and is also
imported late from main.py's bottom section, so a test that imports it before
`main` has finished loading hits the identical failure.

Same again for `services/automation_rules.py` (`from main import
build_keyword_matcher`), imported late for the same reason.

`routes/tags.py` (Stage 3: tag alias/inventory/global-suppression CRUD, plus
`/feed-tags/dismiss` which is a tags concern despite its URL) and
`routes/highlights.py` (Stage 3: the highlight/automation-rule CRUD surface —
add/edit/remove/toggle/reorder plus the merge-suggestion routes) have no
ordering constraint either, and are imported alongside the plain
`routes.integrations_*`/`routes.compat_*` modules. Both leave their
widely-shared helpers in main.py rather than moving them: `normalize_tag_value`
(51 call sites) and the rest of the tag-alias/rename/delete helpers for
`routes.tags`, and `get_highlight_keywords`/`add_highlight_keyword`/the
rule-group finder-and-merge functions for `routes.highlights` — all of these
are also called from `services/automation_rules.py`, other main.py-resident
routes, scripts, or are exercised directly as `main.<name>` by dedicated test
files.

`routes/automation.py` (Stage 4: `/rules/*`, `/automation/history*`,
`/dedup/*`) is the first module in this split to import from
`services/automation_rules.py` directly (`_run_tag_filter`, `_run_now_dedup`,
`_run_now_pattern`) rather than round-tripping those names through main.py —
combining both gotchas above in one module. Empirically neither import order
actually breaks (verified by testing both), since none of
`services/automation_rules.py`'s own `from main import (...)` dependencies are
late-bound names, unlike `_run_automation_after_refresh`; it's imported after
the `services.automation_rules` block anyway, for the same reason `routes.system`
is — so that module is already fully loaded before anything reaches into it,
rather than routes.automation's own import being what first triggers
`services/automation_rules.py`'s module load mid-way through main.py's
execution. `_dry_run_dedup`/`_dry_run_pattern` (the `/rules/dry-run` preview
engine, each with exactly one caller) moved here too, pulling their own
shared helpers (`_resolve_dedup_feed_urls`, `dedup_order_key`,
`build_keyword_matcher`, etc.) back from main.py — full consolidation of the
preview-vs-apply dedup engine into `services/dedup.py` is a separate,
deliberately-deferred Plan.md project. `resolve_rule_feed_urls`,
`toggle_feed_tag_filter`, and `feed_tag_service` stay in main.py: shared with
`services/automation_rules.py` and/or other still-in-main.py routes.

`routes/admin.py` (Stage 5: `/account/*` self-service, admin-only
`/admin/users/*` and `/admin/logs`, and the `/debug/*` maintenance toggles)
has no ordering constraint either — none of its handlers touch
`services.automation_rules` — so it's imported alongside the plain
`routes.compat_*`/`routes.tags`-style modules. `_username_error`,
`_password_error`, `_account_redirect` (and the USERNAME_MIN_LEN/
PASSWORD_MIN_LEN constants) and `_purge_thumb_cache_for_urls` moved with
their routes, having no caller left in main.py. `_dir_bytes` stayed and gets
imported back even though `admin_vacuum_user` is its only caller in this
module, because `routes/system.py` (Stage 1) also calls it for the Admin ->
storage-usage stats. `_is_web_admin`/`_current_web_user` stay too, called
from several still-in-main.py routes. `_read_log_tail`/`_log_line_dt`/
`_parse_local_ts` (and the `_LOG_LEVEL_RANK`/`_LOG_LINE_RE` constants) stay
in main.py rather than moving with `admin_logs`, for the same "exercised
directly as `main.<name>` by a dedicated test file" reason Stage 3 kept
`get_highlight_keywords` and friends: `tests/unit/test_admin_log_tail.py`
calls them directly. `provision_user_storage` stays, also called from
`bootstrap_admin`; `delete_user_storage`, its lifecycle-pair sibling defined
right next to it far from this route cluster, stays alongside it rather than
being split out for its single remaining caller.

`routes/saved.py` (Stage 6: `/saved/*` bulk maintenance plus the
`/articles/refresh-content`/`/articles/save` capture routes, 22 routes) has
no ordering constraint either — nothing here touches `services.automation_rules`
— so it's imported alongside the plain `routes.compat_*`/`routes.tags`-style
modules. This stage moved far fewer helpers than its route count suggests:
most of the plan/engine functions each route leans on are exercised directly
by a dedicated test as `main.<name>` (the same precedent Stage 3 and Stage 5
already established) and stayed in main.py rather than moving with their one
route caller — `_autofile_excluded_targets`, `_current_unstar_tagged_plan`,
`_saved_dup_host_slug`, `_check_saved_url` (with its own
`_looks_like_soft_404`/`_normalize_probe_path` neighborhood), and the whole
scoped-refetch engine (`_refetch_job_state`, `_scope_refetchable`,
`_refetch_begin`, `_refetch_worker`, `_run_refetch_batch`, plus their two
untested siblings `_refetch_scope_label`/`_refetch_status_payload`, kept
alongside rather than splitting one state machine across two files).
`_scope_starred_keys` stays for the more ordinary reason: it's also called
from a still-in-main.py Read Mode route. Two more stay for a third reason
found only by grepping `routes/*.py` and `scripts/*.py`, not just main.py and
tests: `_current_autofile_plan` (also called by `routes/system.py`'s
Instapaper import) and `_saved_dup_groups` with its
`_SAVED_DUP_BODY_HEAD_CHARS`/`_SAVED_DUP_BODY_SQL_CHARS` constants (also
called by two `scripts/*.py` maintenance tools as `main.<name>`) — first
moved on the assumption they were single-route-only, then moved back after
`import main` failed. The Save Article capture helpers are the extreme
version of the same story — `_save_article_for_current_user`,
`_refresh_captured_article_for_current_user`, and the whole auto-refetch-on-
keep machinery all stay, because each is also called from `/api/save`,
`/api/bookmarklet/save`, `/entries/saved`, `/entries/tags`, or
`/entries/autofetch-status`, none of which moved here — only the three route
handlers this module actually owns moved, importing almost everything they
call back from main.py. Plan.md's own "backed by services/saved_articles.py"
note only half held up: that service genuinely backs the capture path, but
the dupe-scan, autofile, unstar-tagged, and archive-old *planners* are
main.py-resident logic with no service-layer home, and moved or stayed on
their own test-coverage and sharing merits, not a service boundary.

`routes/settings.py` (Stage 7: profile/email-bcc, the `/settings/all` bulk get/save, the manual maintenance trigger,
auto-refresh, the Global Note, the lazy Settings -> Feeds panels, and the problematic-feeds triage actions — 14 routes)
has no ordering constraint either — nothing here touches `services.automation_rules` — so it's imported alongside the
plain `routes.compat_*`/`routes.tags`-style modules. The two clusters this stage's routes came from were not contiguous
in main.py (`/settings/email-bcc` through `/settings/maintenance/run-now` sat together; `/settings/auto-refresh` through
the `/settings/problematic-feeds/*` group sat ~5,600 lines away), and `/tree/folder-feeds/{folder_id}` — a sidebar
fragment route, not a settings concern despite living inside that second block — stays in main.py. No third outlier
turned up. Only one helper sat immediately next to a moved route: `_keep_existing_sensitive` (the masked-secret-field
guard `save_all_settings` uses so a routine re-save never blanks a stored secret just because the masked "••••"
placeholder came back) — it stays in main.py and is imported back, for the same "exercised directly as `main.<name>` by
a dedicated test file" reason Stage 3/5/6 kept their own adjacent helpers (`tests/unit/test_settings_sensitive_save.py`).
Everything else these routes call is pre-existing main.py-resident, widely-shared infrastructure (the whole family of
settings getters, `FeedInFolder`, `_disambiguate_feed_titles`, `_run_youtube_sync`, and so on) with callers well outside
this cluster — none of it moved. Three tests needed retargeting for the copied-reference gotcha, none via
`monkeypatch.setattr(main, ...)` but via a subtler variant of the same shape: registering a moved handler function
object directly onto a test-local `FastAPI()` app as `main.<handler_name>` no longer works once the handler lives in
`routes.settings` — `tests/integration/test_needs_replacement.py` and `tests/integration/test_note_title_img_reextract.py`
now import `routes.settings` and register `settings_routes.mark_feed_needs_replacement`/`unmark_feed_needs_replacement`/
`get_global_note_setting` instead of `main.<name>`. `tests/unit/test_refetch_guard_and_thumb_plugin.py` hit the same
shape via `inspect.getsource`: it fell back to grepping `main.__file__`'s raw text for `_ADMIN_ONLY = {` since
`main.save_settings` never existed (the real name is `save_all_settings`), and that text is no longer in main.py now
that the function moved — retargeted to `inspect.getsource(routes.settings.save_all_settings)` directly.

`routes/feeds.py` (Stage 8 of the route-by-URL-prefix split -- the biggest single cluster at 61 routes,
scoped into sub-stages A-E) was NOT done in one shot: this file started with only sub-stage A's 10
routes (folder CRUD + tree reads -- `/api/folders`, `POST /folders`, `/folders/rename`, `/folders/delete`,
`/folders/properties`, `/folders/cadence`, `/folders/retention`, `/folders/mark-read`,
`/tree/folder-feeds/{folder_id}`, `/api/folder-feeds`); sub-stages B (feed discovery/add flow), C (display/
thumbnail strategy config), D (network/fetch settings + lifecycle), and E (tags/attachments/curation/bulk
ops) each add more routes to this same module in later tasks -- don't assume this is the final state. No
ordering constraint for sub-stage A: none of its handlers touch `_run_automation_after_refresh` or anything
else from the late `services.automation_rules` import, so it's imported alongside the plain
`routes.compat_*`/`routes.tags`-style modules. No helper moved with its route -- `get_folder_properties` and
`delete_folder` stay in main.py, tested directly as `main.<name>` by `tests/integration/test_folder_properties_counts.py`,
`tests/integration/test_retention_purge.py`, and `tests/integration/test_feed_removal_consolidation.py`;
`_FOLDER_CADENCE_LAST_REFRESH_PREFIX` stays, also read by the still-in-main.py cadence-refresh scheduler;
`_mark_entries_as_read_for_view` stays, shared with the still-in-main.py `/feeds/mark-read` and
`/entries/mark-older-than-read` routes. Two test files needed retargeting for the usual "handler registered
directly as `main.<name>` on a bare test `FastAPI()` app" gotcha (`tests/integration/test_mark_read_routes.py`,
`tests/integration/test_mark_read_view_scope.py`, both for `/folders/mark-read` -> `routes.feeds.mark_folder_as_read`);
the former also hit the copied-reference monkeypatch gotcha (`get_meta_connection`, `get_folder_feed_urls`,
`_mark_entries_as_read_for_view`, and `unread_counts_cache` all needed patching on both `main` and `routes.feeds`).

Stage 8B added the feed discovery/add flow (13 more routes: `/feeds/discover`, `/feeds/compare`, `POST /feeds`,
the `/scraped-feeds*` cluster, `/feeds/properties`, `/feeds/suggest-migration`, `/feeds/set-user-title`,
`/feeds/fix-url-titles`, `/feeds/lazy-titles`) to the same `routes/feeds.py` module -- see that file's own
docstring for the full rationale. Sub-stages C-E (display/thumbnail strategy config, network/fetch settings +
lifecycle, tags/attachments/curation/bulk ops) still come later.

Stage 8C added the feed display/thumbnail strategy config cluster (10 more routes, all `POST`: `/feeds/strategy`,
`/feeds/display-prefs`, `/feeds/backfill-hide-shorts`, `/feeds/thumbnail-url`, `/feeds/thumb-crop`,
`/feeds/smart-min-scale`, `/feeds/fill-zoom`, `/feeds/thumb-strategy`, `/feeds/caption-source`,
`/feeds/strategy-refresh`) to the same `routes/feeds.py` module -- see that file's own docstring for the full
rationale, including which helpers moved (`_VALID_MANUAL_STRATEGIES`, `upsert_feed_thumb_crop`) versus stayed in
main.py and got imported back. Sub-stage E (tags/attachments/curation/bulk ops) still comes later.

Stage 8D added the feed network/fetch settings + lifecycle cluster (12 more routes: `/feeds/browser-ua`,
`/feeds/proxy`, `/feeds/tailscale`, `/feeds/flaresolverr`, `/feeds/reparse`, `/feeds/move`, `/feeds/disable`,
`/feeds/enable`, `/feeds/toggle-updates`, `/feeds/change-url`, `/feeds/unsubscribe`, `/feeds/curation-count`) to
the same `routes/feeds.py` module -- see that file's own docstring for the full rationale. Real feed-lifecycle
logic, not thin wrappers; only `feed_curation_counts` moved as a genuinely single-route-only helper, while the
`flag_*`/`unflag_*`/`_invalidate_*_feeds_cache` fetch-escalation families, `disable_feed`/`enable_feed`,
`purge_orphaned_feed`, and several others stayed in main.py despite looking route-adjacent, each confirmed shared
with still-in-main.py code, other `routes/*.py` modules, or tested directly. Two scripts
(`scripts/fix_reddit_rss_host.py`, `scripts/find_redirecting_feeds.py`) called `change_feed_url_route` directly as
a plain function and were retargeted from `main.change_feed_url_route` to `routes.feeds.change_feed_url_route`.

Stage 8E added the final cluster (16 more routes: tags/attachments/curation/bulk ops --
`/feeds/suggested-tags`, `/feeds/attachment-candidates`, `/feeds/attachment-candidate-suppress`,
`/feeds/attachment-exts`, `/feeds/set-website`, `/feeds/url-rewrites`, `/feeds/url-rewrites/delete`,
`/feeds/curation-items`, `/feeds/combine`, `/feeds/duplicates`, `/feeds/duplicates/undismiss`,
`/feeds/duplicates/dismiss`, `/feeds/multi-folder`, `/feeds/multi-folder/resolve`, `/feeds/bulk`,
`/feeds/mark-read`) to `routes/feeds.py` -- see that file's own docstring for the full rationale. **This
closes out Stage 8: `routes/feeds.py` is complete at 61 routes across sub-stages A-E, and no further
sub-stages are planned.** The one new wrinkle this sub-stage hit: `bulk_feed_action`'s "refresh" action
calls `_run_automation_after_refresh` directly, so `routes/feeds.py` itself now needs the same late-import
treatment `routes/system.py` and `routes/automation.py` already needed -- its import moved from the early
alphabetical block in main.py's bottom-of-file import section to after the `services.automation_rules`
import. `routes/integrations_deviantart.py` had to move later still, since its watchlist auto-pause path
calls `bulk_feed_action` directly and switched from `from main import bulk_feed_action` to `from
routes.feeds import bulk_feed_action` -- which only resolves once `routes.feeds` has already fully loaded.
One script (`scripts/combine_deviantart_galleries.py`) called `combine_feeds_route` directly as a plain
function, the same shape Stage 8D found twice, and was retargeted the same way. Nine test files needed
retargeting for the usual gotchas (handler-on-a-bare-`FastAPI()`-app and copied-reference monkeypatches);
`tests/integration/test_feed_removal_consolidation.py` needed the largest sweep since it calls several of
this sub-stage's routes as plain functions throughout the file.

`routes/entries.py` (Stage 9 of the route-by-URL-prefix split -- 45 `/entries/*` routes, scoped into its
own A-E sub-stages, same reasoning as Stage 8) **is only partially done: this file started with only
sub-stage A's 12 routes** -- sub-stages B (entry metadata edits + attachments), C (move/organize + tags), D
(read/unread/star state + integration sends), and E (`/entries/pane` alone, last) each add more routes to
this same module in later tasks -- don't assume this is the final state. A/B/C/D are now in; E (`/entries/pane`
alone) is the only sub-stage still remaining.

Stage 9A -- content/reading utility: `GET /entries/lead-image`, `GET /entries/media/audio`,
`GET /entries/media/download`, `POST /entries/thumb-crop`, `GET /entries/readability`, `GET /entries/source`,
`GET /entries/frame-check`, `GET /entries/feed-tags`, `GET /entries/content/has-original`,
`POST /entries/content/clean`, `POST /entries/content/revert`, `GET /entries/autofetch-status` (12 routes,
exactly as scoped). main.py: 28,695 -> 28,254 lines; `routes/entries.py` created at 560 lines (sub-stages
B-E extend the same file). No ordering constraint: none of these handlers touch
`_run_automation_after_refresh` or anything else from the late `services.automation_rules` import, so this
module is imported alongside the plain `routes.compat_*`/`routes.tags`-style modules.

Only one helper moved with its route, having no caller anywhere else: `_wrap_readability_html` (with
`entry_readability`). Its immediate neighbor `_resolve_archived_readability_html` looked like the same
shape but stayed in main.py and got imported back instead -- it's also called by
`resolve_reader_article_html`, the still-in-main.py e-ink `/read` view's article resolver (Stage 10), so
moving it would have broken that caller; confirmed via the `routes/*.py`/`scripts/*.py`/`tests/` three-way
grep, not assumed from physical adjacency. Everything else these 12 routes touch stayed in main.py and got
imported back: widely-shared services/singletons (`lead_image_service`, `starred_archive_service`,
`saved_articles_service`, `feed_tag_service`, `feed_tags_service_mod`, `content_edits`, `html_sanitize`,
`url_guard`, `feed_refresh_service`, `get_reader`, `get_meta_connection`), helpers with a second
still-in-main.py caller (`_resolve_entry_audio_url`/`_find_entry_audio_url`, shared between the two media
routes and a third caller besides; `_lead_image_display_url`; `_resolve_entry_content_html`), a
`state.py`-sourced `_PerUserDict` singleton also written by the still-in-main.py star/tag routes
(`_autofetch_jobs`, Stage 9D), and helpers tested directly as `main.<name>` by a dedicated test file
(`build_readability_response`, `_CLEANUP_ERROR_MESSAGES`/`_CLEANUP_ERROR_FALLBACK`). `_VALID_THUMB_CROPS` is
the same constant `routes/feeds.py` has re-imported since Stage 8C.

Four test files needed retargeting for the "handler registered directly as `main.<name>`" gotcha, in both
its shapes: `tests/integration/test_autofetch_pane_refresh.py` and
`tests/integration/test_entry_content_cleanup.py` register a moved handler directly onto a bare test
`FastAPI()` app (-> `routes.entries.entry_autofetch_status` and
`routes.entries.clean_entry_content_route`/`routes.entries.revert_entry_content_route`);
`tests/integration/test_feed_tag_dismiss_survives_reharvest.py` and
`tests/integration/test_orphan_entry_tags.py` (three call sites) call a moved handler directly as a plain
function (-> `routes.entries.entry_feed_tags_route`). One test hit the copied-reference monkeypatch gotcha
over real HTTP: `tests/integration/test_reader_view_stored_content_fallback.py` monkeypatches
`main.build_readability_response` and then hits `GET /entries/readability` via `TestClient(main.app)` --
since `entry_readability` now does its own `from main import build_readability_response`, the patch needed
doubling onto `routes.entries.build_readability_response` too.

Stage 9B -- entry metadata edits + attachments, 9 routes: `POST /entries/delete`, `POST /entries/set-date`,
`POST /entries/set-title`, `POST /entries/set-link`, `GET /entries/attachments`,
`POST /entries/attachments/delete`, `POST /entries/attachments/delete-all`, `POST /entries/attachments/save`,
`POST /entries/attachments/save-all`. Sub-stages C (move/organize + tags), D (read/unread/star state +
integration sends), and E (`/entries/pane` alone, last) still come later. `_parse_local_date_to_utc` and
`_set_orphan_entry_date` moved with `/entries/set-date` (the former has no caller besides the latter, and the
latter's own only caller is `set_entry_date_route`); `_ENTRY_TITLE_MAX_LEN` moved with `/entries/set-title`;
`_entry_content_html_and_base` moved with `/entries/attachments`. `_hard_delete_entry` stayed and is imported
back despite sitting immediately above `/entries/delete` -- its own docstring says it's "shared by
/entries/delete and /saved/deduplicate", and `routes/saved.py` already imports it directly, confirmed via the
`routes/*.py`/`scripts/*.py`/`tests/` three-way grep rather than assumed from the docstring alone (it's also
tested directly as `main._hard_delete_entry` by two dedicated test files). `_ENTRY_LINK_MAX_LEN` stayed too,
for the "tested directly as `main.<name>`" reason Stage 9A's `_CLEANUP_ERROR_MESSAGES` and several earlier
stages' constants already established (`tests/integration/test_entry_link_override.py`);
`STARRED_ASSET_URL_PREFIX` (8 other call sites across main.py), `candidate_attachment_links_in_html` (tested
directly as `main.<name>`), `_filtered_file_enclosures` (a second still-in-main.py caller besides
`_entry_content_html_and_base`), and `get_starred_archive_connection`/`invalidate_unread_counts_cache`
(widely-shared low-level primitives) all stayed too and got imported back. Four test files needed
retargeting for the "handler registered directly as `main.<name>` on a bare test `FastAPI()` app" gotcha:
`tests/integration/test_delete_entry_tombstone.py` (-> `routes.entries.delete_entry_route`),
`tests/integration/test_entry_title_override.py` (-> `routes.entries.set_entry_title_route`),
`tests/integration/test_entry_link_override.py` (-> `routes.entries.set_entry_link_route`), and both
`tests/integration/test_entry_date_override.py` and
`tests/integration/test_backfill_url_inferred_dates_refresh_safety.py` (-> `routes.entries.set_entry_date_route`,
the same handler registered in two separate test files). One test hit the plain-function-call variant:
`tests/integration/test_entry_attachments_route.py` calls `main.entry_attachments_route(...)` directly five
times, retargeted to `routes.entries.entry_attachments_route`. No script-only callers turned up for any of
this sub-stage's 9 routes or their moved helpers.

Stage 9C -- move/organize + tags, 9 routes: `POST /entries/move-to-feed`, `POST /entries/move-to-feed-batch`,
`POST /entries/select-all-visible`, `POST /entries/move-visible-to-feed`, `POST /entries/purge`,
`GET /entries/manual-tags-batch`, `POST /entries/tags`, `POST /entries/tags-batch`, `POST /entries/discard`.
Sub-stages D (read/unread/star state + integration sends) and E (`/entries/pane` alone, last) still come
later. These 9 routes were NOT contiguous in main.py -- scattered lines 24117-26596, interleaved with several
Stage 9D routes (`/entries/read*`, `/entries/saved`, `/entries/archive`, `/entries/star-batch`,
`/entries/mark-*`, `/entries/undo-*`, `/entries/email`, `/entries/instapaper`, `/entries/quire`) that stayed
untouched despite sitting right next to a moved route.

The two tag routes (`/entries/tags`, `/entries/tags-batch`) confirmed Plan.md's Landmines prediction: they
lean on the same widely-shared tag machinery Stage 3's `routes/tags.py` already left behind --
`get_manual_tags_for_entry`, `set_manual_tags_for_entry`, `parse_manual_hashtags`, `normalize_tag_value`, and
`MAX_MANUAL_TAGS` all stayed in main.py and got imported back, each with call sites well outside this
sub-stage (`set_manual_tags_for_entry` alone is also driven by the feed auto-taggers at ingest).
`parse_manual_tag_edit_tokens`/`apply_manual_tag_edits` (the bulk route's own `+/-tag` parser) also stayed --
both are tested directly as `main.<name>` by `tests/unit/test_manual_tag_edit_tokens.py`. `_merge_manual_tags`
(the single-entry append-mode helper) *did* move with `/entries/tags`: despite its own docstring claiming it
was "shared by the single-entry append path and the bulk tag-add route," the three-way grep found only one
live caller (`set_entry_manual_tags`) -- the bulk route now goes through `apply_manual_tag_edits` instead, so
the docstring was stale.

`_move_entry_to_feed` (the per-entry move engine both `/entries/move-to-feed` and the batch/visible variants
call) stayed in main.py despite sitting in the middle of this cluster: `routes/saved.py` already imports it
directly for the saved-duplicate merge flow, and three scripts (`dedupe_orphan_archives.py`,
`merge_saved_vs_real_duplicates.py`, `rehome_article_feeds.py`) call it as `main._move_entry_to_feed`.
`_MOVE_BATCH_CAP` stayed too -- shared with the still-in-main.py `/entries/read-batch` and `/entries/star-batch`
routes (Stage 9D) and re-imported by `routes/integrations_youtube.py`. `_prune_entries` (the purge/retention
engine `/entries/purge` calls) stayed, also called by main.py's own nightly per-folder retention sweep and
tested directly as `main._prune_entries` by several files. `set_entry_archived`/`apply_star_state`/
`mark_entry_read_everywhere` (the three primitives `/entries/discard` chains) all stayed -- each has other
still-in-main.py callers among the Stage 9D star/archive routes, and `apply_star_state` is also called from
`routes/saved.py` and a script.

`_resolve_view_posts`, `_view_filter_predicate`, `_MOVE_VISIBLE_LIMIT`, `_folder_is_yt_folder`, and the
duration-filter trio (`_DURATION_FILTER_RE`/`_parse_duration_filter`/`_parse_duration_filter_seconds`) all
moved together: confirmed via the three-way grep to have no caller anywhere outside
`select_all_visible_entries_route` and `move_visible_entries_to_feed_route`, both of which moved in this same
sub-stage, so the whole cluster came across as one block.

Six test files needed retargeting for the "handler registered directly as `main.<name>` on a bare test
`FastAPI()` app" gotcha: `tests/integration/test_select_all_visible.py` (->
`routes.entries.select_all_visible_entries_route`), `tests/integration/test_move_visible_to_feed.py` (->
`routes.entries.move_visible_entries_to_feed_route`), `tests/integration/test_retention_purge.py` (->
`routes.entries.purge_old_entries`), and `tests/integration/test_autofetch_pane_refresh.py` (one more handler
added to its existing `routes.entries` registrations, -> `routes.entries.set_entry_manual_tags`). Three more
hit the plain-function-call variant: `tests/integration/test_move_entry_to_feed.py` (batch route, two call
sites), `tests/integration/test_tags_batch.py` (both batch routes, four call sites), and
`tests/integration/test_archive_discard_semantics.py` (`discard_entry`, six call sites). One unit test file,
`tests/unit/test_keep_signal_followups.py`, used `inspect.getsource(main.set_entry_manual_tags)` and needed
retargeting to `entries_routes.set_entry_manual_tags` (a fresh `from routes import entries as entries_routes`
import, matching that file's existing `system_routes` alias style) since the function no longer lives in
main.py's namespace. No script-only callers turned up for any of this sub-stage's 9 routes.

Stage 9D -- read/unread/star state + integration sends, the biggest and most state-coupled sub-stage of
`routes/entries.py`, deliberately scoped second-to-last: `POST /entries/read`, `POST /entries/saved`,
`POST /entries/archive`, `POST /entries/read-batch`, `POST /entries/star-batch`, `POST /entries/mark-range-read`,
`POST /entries/mark-older-than-read`, `POST /entries/undo-mark-unread`, `POST /entries/undo-mark-read`,
`POST /entries/undo-unstar`, `POST /entries/mark-newer-than-unread`, `POST /entries/email`,
`POST /entries/instapaper`, `POST /entries/quire` (14 routes). Only sub-stage E (`/entries/pane` alone) remains
after this. No ordering constraint: none of these handlers touch `_run_automation_after_refresh` or anything
else from the late `services.automation_rules` import, so this module keeps its existing import position.

Every one of these routes touches the `unread_counts_cache`/`unread_counts_cache_lock`/
`_bump_unread_counts_generation` trio from `state.py` (re-exported through `main`, same as every earlier
stage's cache-touching routes) -- moved verbatim, same lock-then-bump-then-clear shape main.py already used,
never a raw `global` re-derivation. `get_unread_counts_generation()` is exercised (not by these routes directly,
but by the pre-existing regression tests that watch it) via `tests/integration/test_read_batch.py`'s
`test_batch_read_invalidates_unread_count_cache`/`test_batch_unread_invalidates_unread_count_cache`, both
retargeted to call `routes.entries.mark_entries_read_batch_route` directly and re-verified green after the move.

None of the private helpers these 14 routes lean on moved with them -- every one has a caller elsewhere that
would have broken, confirmed via the `routes/*.py`/`scripts/*.py`/`tests/` three-way grep, not assumed from
adjacency: `_run_on_star_destinations` (called from `apply_star_state`, itself unmoved, plus tested directly as
`main.<name>`), `_instapaper_save_url` and `_quire_add_entry` (each also called from `_run_on_star_destinations`,
and each tested directly as `main.<name>`), `_undo_token_problem` (also called from `apply_star_state`),
`_mark_entries_as_read_for_view` (shared with `routes/feeds.py`'s `/folders/mark-read` and `/feeds/mark-read`,
already imported there since Stage 8A), `_youtube_unpremiered_video_id`, `get_tagged_entry_keys`, and
`entry_effective_date` (each with several other still-in-main.py or cross-module callers), and `_sanitize_html_allowlist`
(tested directly as `main.<name>` by `tests/unit/test_security_fixes.py`). All of these stayed in main.py and
were imported back into `routes/entries.py` instead. `_RANGE_READ_LIMIT` and `_UNDO_MARK_READ_WINDOW` likewise
stayed: the former has a second caller elsewhere in main.py, the latter is private to `_undo_token_problem`,
which itself stayed.

Thirteen test files needed retargeting for the "handler registered directly as `main.<name>` on a bare test
`FastAPI()` app" gotcha: `tests/integration/test_mark_read_routes.py` (four handlers: `mark_entry_read`,
`mark_entries_older_than_read` twice, `mark_entries_newer_than_unread`), `tests/integration/test_undo_unstar.py`
(`toggle_entry_saved` and `undo_unstar`), `tests/integration/test_autofetch_pane_refresh.py` (one more handler
added to its existing `routes.entries` registrations, `toggle_entry_saved`), `tests/integration/test_unstar_husk_cleanup.py`,
`tests/integration/test_tag_as_keep.py` (both `toggle_entry_saved`), `tests/integration/test_youtube_unpremiered.py`
(`mark_entries_older_than_read` and `mark_entries_range_read`), `tests/integration/test_mark_range_read.py`
(`mark_entries_range_read`), `tests/integration/test_mark_read_view_scope.py` (`mark_entries_older_than_read`),
`tests/integration/test_undo_mark_read.py` (`undo_mark_read`), `tests/integration/test_email_route.py`
(`email_entry`), `tests/integration/test_instapaper_route.py` (`save_to_instapaper`), and
`tests/integration/test_quire_add_route.py` (`add_to_quire`). Four more hit the plain-function-call variant:
`tests/integration/test_archive_discard_semantics.py` (`toggle_entry_archived`), `tests/integration/test_read_batch.py`
(`mark_entries_read_batch_route`, two call sites), and `tests/integration/test_star_batch.py`
(`star_entries_batch_route` and `undo_unstar`, five call sites combined). One unit test file,
`tests/unit/test_keep_signal_followups.py`, used `inspect.getsource(main.toggle_entry_saved)` (twice) and needed
retargeting to `entries_routes.toggle_entry_saved`, the same alias `set_entry_manual_tags` already used in Stage
9C. The email/instapaper/quire test files also hit the copied-reference monkeypatch gotcha over real HTTP --
each stubs several main.py-resident getters (`is_email_configured`, `get_resend_api_key`, `get_resend_from`,
`send_article_email`, `get_setting`, `get_runtime_setting`, `is_quire_connected`, `get_quire_user_token`,
`quire_project_oid`, `get_quire_usage_status`, `_quire_add_entry`, `get_reader`) that now also need patching on
`routes.entries`, doubled the same way Stage 8A's `_build_feed_mark_read_app` established. No script-only
callers turned up for any of this sub-stage's 14 routes or their private helpers.

Stage 9E -- `GET /entries/pane` alone (1 route), the last sub-stage of `routes/entries.py`. Landmines-flagged as
the riskiest sub-stage going in, but it turned out low-risk: `entry_pane` itself is pure orchestration -- param
normalization, one `get_entry_detail` call for the single selected entry, a small feed_url->folder_id map built
from `get_meta_structure_snapshot`, a handful of integration-configured checks, and a `templates.TemplateResponse`
render. It does NOT call `_home_inner`, `list_entries_for_feeds`, or `build_reader_page` -- those three plus
`get_entry_detail` are the shared rendering-core functions Plan.md's Landmines note and the "Shared rendering
core" section both flag as reused by `/`, `/read`, pane-swap, and the compat APIs; `get_entry_detail` is the only
one this route touches, and it (along with `_home_inner`/`build_reader_page`) stays in main.py untouched, imported
back like every other main.py-resident helper. `_get_email_to_default` moved with the route -- confirmed via the
three-way grep to have no caller besides `entry_pane` and no dedicated test. `_mark_entry_read_background` looked
single-route-only by the same grep (only caller is `entry_pane`) but stayed in main.py and got imported back
instead: `tests/integration/test_reader_view.py` monkeypatches `main._mark_entry_read_background` as a defensive
stub in its `_patch_read` helper for the unrelated still-in-main.py `reader_view` (`/read`) route, which doesn't
actually call it -- moving the function out of main.py entirely would still have broken that `monkeypatch.setattr`
(it requires the attribute to exist on the target), so it was left as the safer "tested/patched directly as
`main.<name>`" case Stage 3/5/6/8/9A already established, rather than touching an unrelated test file for a
route this stage didn't move. `normalize_resume_read_filter` and `unsubscribed_feed_urls_among` both stayed too,
confirmed shared with `_home_inner` (main.py, the `/`+`/read` home-view core, Stage 10 territory) via the same
grep. `get_meta_structure_snapshot`, `is_instapaper_configured`, `is_quire_configured`, `pinterest_oauth_connected`,
`reddit_connected`, and `templates` are all pre-existing widely-shared main.py infrastructure (several already
re-imported by `routes/feeds.py`/`routes/settings.py`) -- none of it moved. No `services.automation_rules`
ordering constraint. One test file needed retargeting for the "handler registered directly as `main.<name>` on a
bare test `FastAPI()` app" gotcha: `tests/integration/test_hide_locked_comics.py` (-> `routes.entries.entry_pane`).
No `scripts/*.py` callers turned up. Full `make test`/`lint`/`types`/`ruff format --check` pass.

**This closes out Stage 9: `routes/entries.py` is complete at 45 routes across sub-stages A-E, no further
sub-stages planned.** Only Stage 10 (`routes/home.py` -- `/`, `/read`, `/read/offline`, and the shared
rendering core itself) remains of the route-by-URL-prefix split.

`routes/home.py` (Stage 10, the last module of the split) **is only partially done: this file currently
holds only sub-stage A's 1 route, `GET /read/offline`** -- sub-stages B (`/read`) and C (`/`) add more
routes to this same module in later tasks, don't assume this is the final state. See that file's own
docstring for the full rationale. No ordering constraint: this route touches nothing from the late
`services.automation_rules` import, so it's imported alongside the plain `routes.compat_*`/`routes.tags`-style
modules. Three helpers moved with the route, confirmed with no caller anywhere else: `_fetch_image_for_offline`,
`_inline_images_as_data_uris`, and the `_OFFLINE_IMG_MAX_BYTES`/`_OFFLINE_IMG_TOTAL_BYTES`/
`_OFFLINE_IMG_MAX_FETCHES` constants. The shared rendering-core functions this route touches
(`get_entry_detail`, `resolve_reader_article_html`) stayed in main.py untouched and were imported back, same
as `_read_mode_date` (a second caller, the still-in-main.py `reader_view`/`/read`, Stage 10B) and the
widely-shared image-cache primitives `api_img_proxy`/`_img_cache_get`/`_img_cache_key_url`. Hit the same
`Path(__file__).parent` relocation bug Stage 1 found in `offline_service_worker` -- fixed the same way, with
`BASE_DIR`. No test exercised `/read/offline` before the move, so no test file needed retargeting for either
gotcha, and no `scripts/*.py` callers turned up.
"""
