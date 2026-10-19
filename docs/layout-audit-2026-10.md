# Lectio layout audit (read-only, 2026-10-18)

Method: AST scans of `main.py` (top-level def/assignment dependency graph, scratch script `scratchpad/deps.py`), `grep` over `routes/`, `services/`, `scripts/`, `tests/`, and reading `ARCHITECTURE.md`, `docs/architecture/tenancy.md` and the `routes/__init__.py` docstring. Nothing was executed or imported; no import-order claim below was run.

Measurements used throughout:
- `main.py` is 26,231 lines, 699 top-level defs/classes, 436 module-level assignments, 558 `.execute(` calls (`grep -c`).
- `services/` is 30k lines and mostly clean: services take dependencies by constructor injection (`main.py:8365`, `8388`, `11917`).
- Only 3 services import `main` at module level (`services/automation_rules.py:37`, `services/inoreader_import.py:24`, `services/migration_common.py:19`) and 1 imports it lazily (`services/scraper_service.py:313`, `335`).
- 22 route modules import `main`. 52 of the scripts in `scripts/` also `import main` (the AST scan of `scripts/*.py` lists them).
- Tests: 401 `setattr(main, ...)` / `setattr(main.<svc>, ...)` call sites, 4,741 `main.<attr>` references covering 552 distinct attributes. 26 test files read `static/js/app.js` as text, and 9 read `main.py` or route source as text.

## Ranked findings

### 1. There is no storage layer. `main.py` is storage, domain and wiring at once
ARCHITECTURE.md ("Layering") promises UI/API, services and storage. The tree has only `routes/`, `services/`, `state.py` (394 lines) and `main.py`. Storage lives in `main.py`:
- Per-user connection pools and per-DB schema bootstraps: `3042-3611` (`get_meta_connection` 3167, websub 3223, thumb 3237, img-cache 3330, yt-duration 3359, `archive_conn` 3411).
- `ensure_meta_schema`, 1,358 lines of DDL and migrations: `3612-4969`.
- App settings store: `4970-5030` (`get_setting`, `set_setting`, `get_runtime_setting`).
- The reader handle and pooling: `get_reader` 8237, `_PersistentReaderProxy` 7916.

Routes reach this layer by `from main import get_meta_connection`, then run SQL inline: `routes/feeds.py` has 63 `.execute(` calls, `routes/entries.py` 35, `routes/system.py` 19.

Tenancy itself is clean. `services/tenancy.py` is the resolver, and `get_meta_connection` resolves through `tenancy.current_user_id()` and `tenancy.meta_db_path` (`main.py:3167-3195`). The problem is where this code lives, not how it is bound.

Verdict: this is the root of every cycle below. Fixing it first makes the rest mechanical.

### 2. 22 route modules depend on `main`, so module load order is load-bearing
- Imported name counts from `main`: `routes/settings.py` 172, `routes/entries.py` 121, `routes/feeds.py` 115, `routes/system.py` 92, `routes/saved.py` 41, `routes/home.py` 33, `routes/integrations_deviantart.py` 29. The AST scan of `routes/*.py` produced these counts.
- The route imports sit in a hand-ordered block at `main.py:26105-26202`. Each position is justified by a comment: `routes.feeds` and `routes.system` must come after `services.automation_rules` (`26172-26193`); `routes.integrations_deviantart` after `routes.feeds` (`26181-26188`).
- The 516-line docstring in `routes/__init__.py:1-516` is mostly an explanation of this ordering and of the retarget-your-tests gotchas.
- A test that imports a `routes.*` module before `main` hits a circular-import failure (`routes/__init__.py:9-18`).
- `from main import X` copies the binding. Patching `main.X` then silently stops affecting the route (`routes/entries.py` module docstring, last paragraph of Stage 9A, describes tests that had to double-patch).

This is the hazard that governs every proposed move (see "Patch targets" below).

### 3. `services -> main` imports: 4 files, and what they actually need
- `services/automation_rules.py:37` imports 57 names. By kind:
  - Foundation: `LOGGER`, `get_reader`, `get_meta_connection`, `get_setting`, `set_setting`, `get_runtime_setting`.
  - Settings keys: `SETTING_*`.
  - Credentials and integration quotas: `get_quire_user_token`, `get_youtube_oauth_token`, `get_resend_api_key`, `youtube_quota_cap`, `mark_yt_quota_exhausted`, `get_yt_quota_spent_today`.
  - Tag and rule helpers: `build_keyword_matcher` (`main.py:7100`), `parse_tag_filter_spec` (7124), `author_filter_token` (7158), `normalize_tag_value` (8419), `rule_scope_*` (6286-6330), `_clamp_min_title_words` (5201), `_dedup_fuzzy_threshold` (5219).
  - View-state appliers that are really domain logic: `_apply_auto_read_age` (7511), `_apply_hide_paywalled` (7584), `_apply_hide_shorts` (7401), `_apply_hide_members_only` (7629), `_cleanup_intra_feed_slug_dupes` (7392), `_is_youtube_short` (7271).
  - UI-flavoured "current user" actions: `_star_entry_for_current_user` (25110), `_quire_add_entry` (25585), `_instapaper_save_url` (25556), `_flush_email_batch_for_rule` (7724).
- `services/inoreader_import.py:24` imports 11 names and `services/migration_common.py:19` imports 6: `get_reader`, `get_meta_connection`, `get_setting`, `set_setting`, `canonical_feed_url` (18702), `_get_or_create_folder_by_name` (6043), `invalidate_meta_structure_cache` (2085), `MANUAL_TAG_KEY_PREFIX`. All foundation or feed-identity helpers.
- `services/scraper_service.py:313` and `:335` import lazily: `mine_publish_date` (`main.py:12804`) and `_append_site_embeds` (`11614`). Both are pure HTML/date functions with no `main`-resident state. These are the two cheapest `services -> main` removals.
- Late binding: `_run_automation_after_refresh` is bound into `main`'s namespace by an import at `main.py:26147-26170`, then re-imported by `routes/feeds.py` and `routes/system.py`. That is the cause of the ordering constraint in finding 2.

### 4. The HTML transformation and article-extraction cluster is about 5,000 lines of nearly pure functions inside `main.py`
- `10699-11500`: comic and embed cleaners, attachments and media rendering.
- `11504-12810`: sanitize, proxy images, lazy media, bbcode, markdown (1,307 lines).
- `12745-13340`: wayback, readability and full-page fetch (596 lines).
- `15371-17650`: lead-image display, embed injection, gallery, webcomic and webtoons panels, caption, ad stripping (about 2,300 lines).

Dependency scan on `11504-12810`: 24 outbound `main` names, of which most are constants and `get_reader`/`get_meta_connection`/`lead_image_service`; the only real coupling is 4 feed-flag callbacks (`_flag_*_on_still_blocked`, 7962-8109) and `_youtube_embed_html` (15584). Inbound uses from the rest of `main` are 30 names. For `12745-13340` (readability): 29 outbound, mostly the same block's own helpers plus `page_fetcher` (8360). Existing homes: `services/html_sanitize.py` (1,143 lines), `services/entry_content.py` (57), `services/publish_date.py` (271), `services/site_content_plugins.py` (483), `services/youtube_embeds.py` (81).

Verdict: highest-yield mechanical move after the storage foundation (finding 1). It also removes the scraper_service lazy imports.

### 5. Entry detail, entry listing and reader pages are the genuine redesign work
- `get_entry_detail` is 715 lines (`17650-18364`) with 61 outbound `main` dependencies. It composes content cleanup, lead-image resolution, tags, archive fallback and orphan handling.
- `_list_entries_for_feeds_fetch` is 826 lines (`13897-14722`), with 43 outbound dependencies for the whole `13343-14720` block (tags, display prefs, pinned thumbs, dedupe, archive).
- `_home_inner` is 864 lines (`22067-22930`) with 78 outbound dependencies, and `routes/home.py` patches it (`setattr(routes.home, "_home_inner", ...)`).
- `build_reader_page` and the `/read` context builders span `21224-21957`.

These are cross-cutting orchestrators. They cannot move until the leaf clusters (findings 4, 7, 8) and storage (finding 1) are out. Moving them is a redesign (split into "gather" and "present" steps), not a cut-and-paste. Tests use them as patch seams: `get_entry_detail` has 3 `setattr`s and 29 references, `_list_entries_for_feeds_fetch` 4 `setattr`s.

### 6. The scheduler, maintenance and lifespan block needs injected callbacks, not a plain move
- `19851-20760` (scheduler, refresh tick, daily maintenance, escalation reprobe, DeviantArt re-sign): 59 outbound dependencies, including `app` and `feed_refresh_service`.
- `lifespan` (`2133-2490`) starts threads for names defined 17,000 lines later (`scheduled_refresh_loop` 19958, `_daily_maintenance_loop` 20700), which works only because it runs after import.
- `_for_each_background_user`, `_background_user_ids` (19880-19887) are patched by tests (4 `setattr`s) and are imported by services as injected callables (`main.py:8388` area).

Direction: `services/scheduler.py` taking a small dependency object (settings getter, reader factory, refresh service) built in `main.py`. Not mechanical.

### 7. Credentials and config constants form one 1,349-line block that nothing can import without `main`
`783-2131` holds env parsing, `SETTING_*` keys, per-integration credential and token getters (YouTube 1101-1156, Pinterest 1157-1210, Reddit 1211-1266, Inoreader 1267-1314, DeviantArt 1315-1361, Quire 1362-1497), proxy/FlareSolverr/image-cache getters (1893-1990), and `AUTH_ENABLED` (2028). AST scan: 93 outbound and 79 inbound names. The getters are small and independent.

Split by concern:
- Constants go to a leaf `config.py` (no `main` imports).
- Each integration's getters go next to its service (`services/quire.py`, `services/reddit.py`, ...), which already exist.

`AUTH_ENABLED = True` is a patch seam with 21 test `setattr`s; the middleware (`_AuthMiddleware`, `main.py:2500`) reads the module global at call time. Do not move either without moving the tests' patch target (see "Patch targets").

### 8. DeviantArt sync is integration logic living in `main.py`, plus route-to-route imports
- `main.py:1498-1893` (396 lines: `sync_deviantart_watchlist`, deactivation bookkeeping, `push_galleries_to_deviantart_watchlist`) has 17 outbound dependencies and only 2 inbound. `services/deviantart.py` (988 lines) already exists.
- `routes/integrations_deviantart.py` imports `bulk_feed_action` from `routes.feeds` (the dependency noted at `main.py:26181-26188`), and `routes/feeds.py` imports `_view_filter_predicate` from `routes.entries` (`1183`). Route-to-route imports are the opposite of the layering. `services/view_filter.py` (104 lines) is the obvious home for `_view_filter_predicate` and the duration-filter parsers (`routes/entries.py:1127-1247`).
- `bulk_feed_action` (`routes/feeds.py:2901-2970`) needs to become a service function (for example `services/feed_ops.py`) called by both routes.

### 9. `routes/feeds.py` (3,018 lines, 61 routes) is a feed-management service wearing a router
Natural boundaries, by line range:
- Folders and tree reads: `384-692`.
- Discover, add feed and scraped feeds: `693-1100` (`create_feed` is about 180 lines).
- Properties, titles and display/strategy settings: `1025-1413`.
- Per-feed network settings (browser-ua, proxy, tailscale, flaresolverr): `1484-1550`.
- Lifecycle: reparse, move, disable, change-url, unsubscribe: `1550-2034` (`change_feed_url_route` is 238 lines, `1697-1935`).
- Curation counts, tag suggestions and attachments: `2034-2250`.
- URL rewrites and combine: `2249-2456`.
- Duplicates and multi-folder: `2457-2900` (`get_feed_duplicates` is about 296 lines, `2507-2803`).
- Bulk and mark-read: `2901-3018`.

The first cut (routes into `routes/feeds/` sub-routers, one `APIRouter` each, included from a package `__init__` that keeps `routes.feeds.router`) is mechanical. The handlers with inline logic (`change_feed_url_route`, `get_feed_duplicates`, `create_feed`) need to be pushed down into services first, which is a redesign.

Patch targets: 23 test `setattr(routes.feeds, ...)` sites (`get_meta_connection` 4, `unread_counts_cache` 2, `restar_curated_entries` 2, `discover_feed_urls_ex` 2, `_mark_entries_as_read_for_view` 2). A package split moves where `routes.feeds.<name>` is looked up; keep re-exports in `routes/feeds/__init__.py` and retarget each patch to the sub-module that now calls the name.

### 10. `routes/entries.py` (2,808 lines) mixes routing with read/star state transitions
Boundaries:
- Reading utilities (lead-image status, audio, readability, source, feed tags, content clean/revert): `233-695`.
- Metadata edits (date, title, link) and attachments: `696-1030`.
- Move to feed and view resolution (`_resolve_view_posts` 1247-1335, `move_visible_entries_to_feed_route` 1380-1469): `1028-1470`.
- Tags: `1508-1690`.
- Read/star/archive/undo and bulk read: `1689-2574`. `mark_entries_range_read` 2119-2265, `mark_entries_older_than_read` 2265-2327, `mark_entries_newer_than_unread` 2473-2574.
- Send-to (email, Instapaper, Quire): `2574-2732`.
- `/entries/pane`: `2732-2808`.

The read/star/undo group duplicates mutation logic that `main.py` already has (`mark_entry_read_everywhere` 14999, `apply_star_state` 14784, `mark_feeds_as_read` 18364, `_mark_entries_as_read_for_view` 18397). It should call one service. Patch targets: 41 `setattr(routes.entries, ...)` sites (`get_reader` 4, `get_meta_connection` 4, `send_article_email` 4).

### 11. `services/lead_images.py` is one 4,800-line class
- `LeadImageService` runs `69-4866`; 94 of its methods take `self`, and they account for about 3,600 of the lines. Only 18 methods (about 290 lines) are static or class-bound.
- The collaborators are already injected (`__init__` at `799`: `get_meta_connection`, `get_reader`, `page_fetcher`, plugins). The service has no `main` imports.
- Natural groups:
  - Pattern tables and pure classifiers (`is_ad_dimension` 410, `is_ad_url` 420, avatar/social-icon/related-post stripping 523-1100): about 600 lines, mostly classmethods.
  - Strategy and persistence: DB cache, write worker, thumb crop, alt text (`1103-1600`).
  - Thumbnail extraction from feed content (`1595-2036`).
  - Resolve and backfill (`2036-2600`).
  - HTML candidate scoring and parsing (`2593-3480`).
  - Image acceptability (`_is_image_url_acceptable` 3480-3782, 302 lines).
  - Source-page fetch and queueing (`3835-4480`).
  - Strategy test harness (`test_entry_strategies` 4485-4730; a diagnostic method whose name starts with `test_`).
- Tests patch the instance (`main.lead_image_service.<attr>`: 62 `setattr(main.lead_image_service, ...)` plus 99 `main.lead_image_service` references). Mixins keep those working because `self.` lookups still find the patched attribute on the same object. Converting methods into free functions in new modules would break every one of these patches.

Direction: split into mixin classes in a `services/lead_images/` package (`LeadImageService(PersistenceMixin, ExtractMixin, SourceFetchMixin, ...)`), re-exporting `LeadImageService` from `services/lead_images/__init__.py`. Mechanical, and low-risk because `self.*` attribute access is unchanged.

### 12. `services/starred_archive.py` (2,006 lines, one class) contains the saved-articles domain and a capture pipeline
- Capture pipeline: `_archive_entry` (267 lines, `1493-1759`), fetch helpers `1765-1843`, asset/image processing `1844-2006` (can be a plain module of functions that take explicit parameters).
- Worker lifecycle: `1361-1492`.
- Asset and attachment queries: `165-336`, `529-760`.
- Sibling-extraction guard: `337-528`. This overlaps the concept already imported from `services/saved_articles.py` (`_page_is_a_different_article`, line 37).
- Orphan saved entries and backfills (`get_orphan_saved_entries` 921-1038, `backfill_saved_entries_from_archive` 1090-1200, `get_orphan_feed_title` 900): saved-articles domain. They belong with `services/saved_articles.py` (964 lines).
- Constructor injection is clean (`98-164`), so there is no `main` import to remove. 44 tests `setattr(main.starred_archive_service, ...)`; keep it as the same object with the same method names (mixins or delegating methods), as with finding 11.

### 13. `static/js/app.js`: 20,807 lines in one global scope
- The only top-level statements are six consts and one function (`app.js:10-29`); everything else sits at a uniform 4-space indent (a leftover from extraction out of `index.html`). It is one classic script (`templates/index.html:2145`) followed by `cleanup.js:2146` and `media-player.js:2166`. 266 named functions, 403 consts, 924 `getElementById` calls.
- Largest chunks that are self-contained feature areas:
  - Highlights/Automation modal: one bare block, `12153-15007` (2,854 lines) with `hl*` functions (`12159-14990`). It is the largest single unit.
  - Feed properties modal: `5389-6406`, plus thumb crop and strategy UI `6406-6857`.
  - Dedup inline results: `780-1190`.
  - Post list and selection: `9499-9568`, `9568-10072` (`bindPostListInteractions`, 454 lines).
  - Read-state, counters and undo toasts: `10072-10936`.
  - YouTube playlist picker and batch jobs: `4272-4575`.
  - Tags (autocomplete, entry tags): `19087-19570`.
  - Keyboard shortcuts and post navigation: `19676-20810`.
  - Entry-pane loading and context menus: `4805-5389`.
- No-build-step split: several classic `<script src>` tags, in order. Top-level `const` and `function` declarations are shared across classic scripts, so a split by file does not require adding exports. `static/js/cleanup.js` (`window.LectioCleanup`) is the existing precedent.
- Constraints:
  - Order matters for top-level `const`s read during script start (temporal dead zone), so leaf utilities load first.
  - `main.py:747-760` hashes every `.js`/`.css` under `static/`, so new files are cache-busted automatically.
  - `static/sw.js` caches `/static/*` (lines 86, 117), so a new file is cached with no config change; the `SHELL` list at `sw.js:30` is for Read Mode only.
  - 26 tests regex over `app.js` text (for example `tests/unit/test_safe_link_url.py:72`, `tests/unit/test_single_pane_layout.py:22`, `tests/unit/test_refetch_guard_and_thumb_plugin.py:241-294`). Each split needs those tests pointed at the new file, or a shared test helper that concatenates `static/js/*.js`.

### 14. `main.py` re-exports and hand-ordered imports have become the compatibility API
- The `from services.automation_rules import (...)` block at `main.py:26147-26170` keeps names importable "only for tests". 52 scripts and 552 distinct `main.<attr>` test references depend on `main` being a facade.
- Consequence: any extraction must leave a re-export in `main.py`, and any test or script that patches `main.<x>` for moved code must be retargeted.
- A cheap hardening step: a lint test that fails when a `main.<name>` referenced by tests or scripts disappears from `main`'s namespace.

### 15. Raw-thread and singleton-wiring rules are enforced by convention, scattered across the file
- Thread starts in `main.py`: 2256-2424 (lifespan), 6864, 11440, 14819, 15441, 18809, 19869, 20401, 20814, 22677, 24240, 24747, 24768, 26098. All carried a tenancy user or a worker that needs none when I read them: `_run_in_user_context` at 6864, 14819, 22677, 26098; `tenancy.user_context` inline at 11440; `_check_corp` (15441) does no tenancy work; the websub thread at 18809 passes `_uid` explicitly. I did not confirm the lifespan threads (2256-2424) or 19869-20814 individually.
- `_run_in_user_context` is defined at `main.py:26062` and re-imported by every route and by the DeviantArt block. It is a 3-line wrapper over `tenancy.user_context` and belongs in `services/tenancy.py` (whose docstring at line 19 already refers to it).
- Service construction and sink wiring are spread over `8310`, `8360-8395`, `11917`, `23444` (`lead_image_service.set_thumb_pin_sink`, 15,000 lines after the service is built). A single `wiring.py` (or one block in `main`) would show the object graph in one place.

## Import cycles and blockers, in one view
- `main -> routes.* -> main` (all 22 route modules): resolved only by the ordered import block `main.py:26105-26202`.
- `main -> services.automation_rules / inoreader_import / migration_common -> main`: same mechanism (`main.py:26147-26172`).
- `main -> services.scraper_service -> main` (lazy, `scraper_service.py:313,335`).
- `routes.feeds -> routes.entries` (`_view_filter_predicate`) and `routes.integrations_deviantart -> routes.feeds` (`bulk_feed_action`).
- `lead_images.py`, `starred_archive.py`, `feed_refresh.py` do not import `main`; they have comments claiming "not imported to avoid a main.py <-> services dependency" (`lead_images.py:25-26`, `saved_articles.py:82`) and duplicate `MANUAL_TAG_KEY_PREFIX` as a result. Move the constant to a leaf module and import it in all three.

## Patch targets: what breaks on each move
- `setattr(main, "<name>", ...)` affects only code that resolves `<name>` through `main`'s globals. After a function and its callers move to another module, `main.<name>` still exists via the re-export, but the patch no longer reaches the callers. Tests keep passing only if the patched code path is no longer exercised, which is worse than a failure.
- Highest-risk names, from `grep` of `setattr`/`main.` counts: `main.get_meta_connection` (21 setattrs, 585 references), `AUTH_ENABLED` (21), `WEBSUB_DB_PATH` (12), `get_reader` (10 setattrs, 430 references), `_refresh_captured_article_for_current_user` (15), `fetch_readability_article` (14), `get_folder_feed_urls` (9), `get_deviantart_user_token` (9), `check_and_mark_manual_refresh` (8), `get_runtime_setting` (7), `unread_counts_cache` (7).
- Mitigation per PR: grep `setattr` for each moved name before the PR, and retarget to the module that calls it. For names patched in many tests (`get_meta_connection`, `get_reader`, `AUTH_ENABLED`), either leave the definition's patch seam in `main` for now or add a test helper that patches every module-level copy.
- Source-text tests: 26 read `app.js`, 9 read `main.py`/route source (for example `tests/unit/test_attachment_ext_suppression.py:14-19` reads `main.py`, `routes/feeds.py` and `app.js`). Moving text breaks them even when behavior is unchanged.

## Ordered sequence of small, behavior-neutral extractions
Each item is one PR. Risk is the chance of a silent behavior change or a hard-to-find test break.

1. **`run_in_user_context` to `services/tenancy.py`**, with `main._run_in_user_context = run_in_user_context` kept. Moves `main.py:26062-26070`. Mechanical. Risk low; 2 tests `setattr` it (12 references), retarget them.
2. **Move `mine_publish_date` (12804) and `_append_site_embeds` (11614) out of `main`** into `services/publish_date.py` / `services/site_content_plugins.py`; delete the lazy imports at `scraper_service.py:313,335`. Mechanical. Risk low (0 `setattr`s, 4 and 7 references); check `_append_site_embeds` imports its own helpers (`_insert_after_first_heading` 11648).
3. **Shared leaf constants module** (`MANUAL_TAG_KEY_PREFIX` and the other duplicated constants; `SETTING_*` keys; limits). Re-export from `main`. Mechanical. Risk low, but touch `lead_images.py:27` and `saved_articles.py:82` only if their duplicates match exactly.
4. **`_view_filter_predicate` and duration parsers (`routes/entries.py:1127-1247`) to `services/view_filter.py`**; fix `routes/feeds.py`'s import. Mechanical. Risk low (5 references, 0 patches).
5. **`bulk_feed_action` (`routes/feeds.py:2901-2970`) to a service function**; `routes/feeds.py` and `routes/integrations_deviantart.py` call it. Light redesign (route wrapper stays). Risk low-medium. Removes the DeviantArt import-order constraint (`main.py:26181-26188`).
6. **Mixin-split `LeadImageService` into a package** (pattern tables first, then persistence, thumbnail extraction, source fetch). Mechanical. Risk low-medium: keep method names and `self.` access, re-export `LeadImageService`; run the 62 instance-patching tests after each slice. Do it as 2-3 PRs (patterns and classifiers; persistence; source fetch and acceptability).
7. **`routes/feeds.py` into sub-routers** (`routes/feeds/` package), cutting at the line ranges in finding 9, one PR per 2-3 groups. Mechanical for the cut; do not refactor handler bodies in the same PR. Risk medium: 23 patch targets, the include order in `main.py:26204-26225`, and the "tests that register a handler on a bare FastAPI()" gotcha noted in `routes/entries.py`'s docstring.
8. **`routes/entries.py` the same way**, after item 4. Risk medium (41 patch targets). Leave the read/star/undo group (`1689-2574`) for item 14.
9. **Split `app.js` along feature boundaries**, starting with the Highlights/Automation block (`12153-15007`) into its own classic script loaded after `app.js`. Mechanical if the block's free variables are only globals from `app.js` (verify by loading in headless Chromium; there is no node in the sandbox). Risk medium: load order (TDZ), 26 text-reading tests, service-worker caching. Follow with the feed-properties modal (`5389-6857`), YouTube picker (`4272-4575`), and shortcuts (`19676-20810`).
10. **`ensure_meta_schema` (3612-4969) and the per-DB `ensure_*_schema` / connection helpers (3042-3611) to `storage/`**, with re-exports in `main`. Mechanical, but patch targets matter: `get_meta_connection` (21 setattrs) and `WEBSUB_DB_PATH` (12) must keep working. Two PRs: schema DDL first (only 13 outbound deps, mostly constants; `ensure_meta_schema` has 163 test references, no patches), connections second. Risk medium on the second PR. Preserve the startup order in `lifespan` (the per-user schema migration, per memory notes, is required for existing tenants).
11. **Settings store (4970-5030) and `get_reader` pool (7891-8312) to `storage/`.** The settings store has 0 outbound dependencies, so it is mechanical; `get_reader` pulls in 25 dependencies (feed flag getters, proxy getters, quota recorders) and needs those passed in or imported from the new foundation modules. Risk medium-high (`get_reader`: 430 test references, 10 setattrs; the thread-local pool is startup-order sensitive, per ARCHITECTURE.md).
12. **Move HTML transforms and article extraction (finding 4) into `services/`**: first the `10699-11500` comic and attachment cleaners, then `11504-12810`, then `12745-13340`, then `15371-17650`. After steps 10-11, the dependencies on `get_reader` and settings can be imported from `storage/`. Mechanical per slice; the four feed-flag callbacks (`7962-8109`) become constructor/callback parameters. Risk medium: 14 `setattr`s on `fetch_readability_article`, `sanitize_readability_html` (13 references), `wayback_snapshot_url` (5); inbound-use scan found 30 names in the 11504-12810 block.
13. **Per-integration credential getters (finding 7) into their services**, then `sync_deviantart_watchlist` block (1498-1893) into `services/deviantart.py`. Mechanical per integration. Risk medium: `get_deviantart_user_token` (9 setattrs), `sync_deviantart_watchlist` (1 setattr).
14. **Rules and tag helpers (7059-7260, 8419-9750) to `services/`; then drop `from main import` in `services/automation_rules.py`, `inoreader_import.py`, `migration_common.py`** by importing from the new homes. After this the ordered block at `main.py:26105-26202` can shrink. Medium: `build_keyword_matcher` has 3 setattrs, 25 references. The tag block has 26 inbound names. Do it in two PRs (tags, then rules).
15. **Redesign steps (not mechanical, one at a time):** scheduler/maintenance to `services/scheduler.py` (finding 6); read/star mutation service shared by routes and rules (finding 10); starred-archive split with saved-articles domain moved to `saved_articles.py` (finding 12); `get_entry_detail` / listing / reader-page orchestration (finding 5). Risk high; each needs a characterization test first (`tests/integration/test_entry_detail_characterization.py` already exists for `get_entry_detail`).

Stop condition for the `routes -> main` problem: when item 14 is done, `routes/*` can import from `storage/` and `services/` and the ordered block can collapse to a plain list of `include_router` calls.

## Things I could not verify
- I did not run the test suite, `make types`, or ruff, so no extraction was tried. Everything is from reading and AST scans.
- No import-order experiment: the claims about which imports are order-sensitive come from the docstrings in `main.py:26105-26202` and `routes/__init__.py`, not from importing the modules.
- I did not read the bodies of `get_entry_detail`, `_list_entries_for_feeds_fetch`, `_home_inner` or `_update_feeds` (613 lines, `services/feed_refresh.py:429-1041`) in full, so "needs redesign" is based on size and dependency counts, not on a line-by-line review.
- The dependency counts ignore names reached through attribute access on module objects (for example `lead_image_service.x`), so real coupling may be higher than the "outbound" figures. They also count only top-level names in `main.py`.
- I did not check whether `app.js` top-level functions are referenced from `templates/*.html` inline handlers (`onclick=`) or the large inline scripts in `templates/index.html` (lines 15, 81, 141, 1051, 1080). That decides whether a split can stay load-order-only.
- I did not check `static/sw.js` precache behavior for `/static/js/*` beyond the prefix checks at lines 86 and 117, nor whether the `SHELL` list should include new files for Read Mode (it loads `reader.js`, not `app.js`).
- I did not confirm all raw `threading.Thread` starts in `main.py` for tenancy binding (finding 15 lists which I read).
- Per-test patch counts come from regex over `tests/` and undercount patches made through aliases (`from main import x`, `monkeypatch.setattr("main.x", ...)` string form, fixtures in `tests/conftest.py`); `grep` was not run against `conftest.py` separately.
- `tests/` size (about 69k lines) and the number of tests that read `main.py` text (9) are from the brief and a `grep -l`, not a full count.
- I did not verify whether `docs/architecture/*.md` references to `main.py` line numbers or function names would need updating when code moves (87 mentions of `main.py` in those docs, counted but not read).
