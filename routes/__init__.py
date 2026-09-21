"""Route modules extracted out of main.py (Plan.md's main.py/index.html breakup).

Each module here does `from main import ...` for shared connection/setting/
credential helpers and defines `router = APIRouter()`; main.py imports these
modules and calls `app.include_router(...)` near the bottom of the file,
after every name a route module needs is already defined.

Gotcha: this only resolves in the direction main.py -> routes.*. Anything
that imports a routes.integrations_* module directly (tests do this to reach
handler functions or monkeypatch their main-derived bindings) must `import
main` first — otherwise the routes module's own `from main import ...` kicks
off main.py's execution while the routes module is still mid-import, and
main.py's bottom-of-file `from routes.integrations_x import router` fails
with a circular-import error because that module hasn't defined `router` yet.

Same gotcha, one level deeper, for `services/migration_common.py`: it also
does `from main import ...` at module level (for `canonical_feed_url`,
`get_reader`, etc. — general primitives with no other home yet) and is also
imported late from main.py's bottom section, so a test that imports it before
`main` has finished loading hits the identical failure.

Same again for `services/automation_rules.py` (`from main import
build_keyword_matcher`), imported late for the same reason.
"""
