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

`routes/feeds.py` (Stage 8 of the route-by-URL-prefix split -- the biggest single cluster at 60 routes,
scoped into sub-stages A-E) is NOT done in one shot: this file currently holds only sub-stage A's 10
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
Sub-stage E (tags/attachments/curation/bulk ops) is the last one, still to come.
"""
