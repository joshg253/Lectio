# Lectio — Claude Code Instructions

## Project orientation

Local-first single-user RSS reader. Deployment target: VPS later, no rewrites needed.

Key docs — read before suggesting architectural changes:
- `README.md` — user-facing features and setup
- `Plan.md` — backlog and prioritization ("add this later" items go here)
- `ARCHITECTURE.md` — design rationale and constraints

## Architecture: 3-layer split

```
UI/API Layer   → web routes, handlers, presentation state
     ↓
Services Layer → feed ops, tagging, filtering, refresh, readability
     ↓
Storage Layer  → reader DB + app-data/settings
```

Preserve layer boundaries even though everything runs in one process now.

## Implementation order (Reader-first)

1. `reader` API — primary path
2. `reader` plugin — for reader-adjacent behavior
3. Custom app logic — only when the first two can't do it; justify the choice

**Never duplicate capabilities the `reader` library already provides.**

## Plugin-first extensions

Non-native behavior goes through plugin/adapter patterns rather than hardwired branching. Keeps features replaceable.

## Tooling defaults

- `uv` for package management and script running
- `ty` for type checking
- Justify any alternative before introducing it

### uv enforcement rules

Never invoke `python` or `python3` directly. Always use `uv` as the entrypoint:

| Task | Command |
|---|---|
| Run a script | `uv run path/to/script.py` |
| Run a module | `uv run -m package.module` |
| Run a one-liner | `uv run python -c "..."` |
| Run a tool | `uv run tool-name` (e.g. `uv run pytest`) |
| Add a dep to a standalone script | `uv add --script path/to/script.py <package>` |
| Remove a dep from a standalone script | `uv remove --script path/to/script.py <package>` |

Using `python` inside `uv run ...` is fine — `uv` is still the entrypoint.

For new standalone scripts (not part of the main project): use `uv init --script script.py` to create it, then `uv add --script` / `uv remove --script` to manage its inline dependencies. Never hand-edit PEP 723 metadata blocks.

If docs or examples use `python`/`python3` directly, translate to the nearest `uv` form before running.

## View state model

Three distinct categories — keep them separate:

| Type | Examples | Persistence |
|---|---|---|
| Remembered base preferences | sort mode/dir, default filters | Durable across restarts |
| Contextual temporary overrides | tag-click, search results | Session/context only |
| Transient navigation state | current entry, scroll position | Ephemeral |

Rules:
- Temporary overrides **never** silently overwrite remembered preferences
- Leaving an override context → restore the remembered base preference
- Refresh/redirect/async must not promote temporary state to remembered

## UX targets

- **Primary**: 3-pane desktop and 1-pane mobile, developed in near tandem
- **Later**: 2-pane medium-width (tablet landscape)
- Do not regress mobile into stacked/squished multi-pane layouts
- Test devices: Win11 (Vivaldi/VSCode), Surface Pro 6 (Firefox touch), S21+
- **Icons**: prefer Material Symbols Rounded; avoid mixing disparate icon styles

## Commit gates

- User-visible feature or behavior change → update `README.md` in the same commit
- "Document design decision" → `ARCHITECTURE.md`
- "Add this later" → `Plan.md`

## Data / config

- Runtime settings: config/env-driven only
- Mutable app state: app-data path abstraction
- Dev/test data: disposable

## Deployment path

1. Local-first single-user (current)
2. VPS: basic auth + reverse proxy (auth is now partially in place)
3. Docker (optional)
4. YunoHost (optional)

Keep auth non-invasive so the VPS step doesn't require a rewrite.
