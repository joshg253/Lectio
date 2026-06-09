# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **Desktop-first web GUI cleanup** — Removed 1-pane mobile mode entirely. Eliminated top bar: logo + hamburger moved to folders pane header; search moved to posts toolbar; theme toggle, note, and collapse sidebar moved into hamburger menu. Tags list gets a collapsible "Tags" header row (like "All Feeds"). Burger menu uses `position: fixed` to escape `overflow:hidden` clipping.

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
