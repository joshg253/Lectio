# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **Posts toolbar + folders pane header polish** — filter/sort dropdowns use `position:fixed`+JS to escape `overflow:hidden` clip; toolbar restructured to two rows (search+mark-read on top, filter+sort on bottom); logo centered in folders pane header; pin/collapse button moved to header right; footer removed; single-mode CSS dead code removed.

## Up next

## Later

- **Miniflux API compatibility** — Fever and GReader are done. Miniflux API is the remaining candidate for broader client support (e.g. Fluent Reader, ReadKit). Assess multi-user requirement and implementation cost before committing.
- **Performance investigation** — systematic baseline before enabling multi-user. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load.

## Backburner

- **Multi-user support / auth refactor** — performance investigation first.
- **Per-user vs. shared thumb cache** — only relevant after multi-user.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy + display settings without saving.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
