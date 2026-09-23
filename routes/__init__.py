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
"""
