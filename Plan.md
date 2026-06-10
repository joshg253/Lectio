# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

## Up next

## Later

- **Per-entry thumbnail strategy** — currently, Post thumbnail > "Per-entry image (auto)" and the Image Source (article) share the same strategy. To use e.g. `og_scrape` for the article top image but `inline` for list thumbnails, a separate `thumb_strategy` column in `feed_display_prefs` is needed, plus a second extraction pass or multi-result storage in `entry_lead_images`. Add "Per-entry: inline / og:image / media RSS" options to the Post thumbnail dropdown that apply the named strategy independently of the article image source.
- **Miniflux API compatibility** — Fever and GReader are done. Miniflux API is the remaining candidate for broader client support (e.g. Fluent Reader, ReadKit). Assess multi-user requirement and implementation cost before committing.
- **Performance investigation** — systematic baseline before enabling multi-user. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load.

## Backburner

- **Multi-user support / auth refactor** — performance investigation first.
- **Per-user vs. shared thumb cache** — only relevant after multi-user.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy + display settings without saving.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
