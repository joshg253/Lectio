# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

## Up next

## Later

- **Miniflux API compatibility** — Fever and GReader are done. Miniflux API is the remaining candidate for broader client support (e.g. Fluent Reader, ReadKit). Assess multi-user requirement and implementation cost before committing.
- **Performance investigation** — systematic baseline before enabling multi-user. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load. Also covers graceful degradation of long-poll/SSE across network switches (phone/tablet use case).
- **Python file header comments** *(quick win, do alongside any other task)* — several `.py` files lack a top-of-file description docstring; standardize across the codebase.

## Backburner

- **Multi-user support / auth refactor** — performance investigation first.
- **Per-user vs. shared thumb cache** — only relevant after multi-user.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy + display settings without saving. Feed type presets may make this unnecessary.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
