# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

## Up next

- **Rachel by the Bay feed health (bug)** — Feed Properties shows fetch errors. Investigate: check actual error message, verify feed URL still live, determine if `_parse_month_first_pubdate` date handler is still needed/matching.
- **Desktop-first web GUI cleanup** — Remove 1-pane mobile mode entirely (single-pane JS/CSS, swipe gestures, pull-to-refresh, topbar scroll-hide, mobile safe-area padding). Eliminate top bar: logo + hamburger move into folders pane header (logo hides with collapsed pane); search moves into posts pane toolbar; theme toggle + note button move to folders pane footer or hamburger menu. Remove duplicate star from folders pane footer. Verify/add collapsible folder groups and tags section within the folders pane.

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
