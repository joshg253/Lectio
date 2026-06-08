# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **Feed type presets in Tuning tab** — Webcomic/Artwork preset buttons (pill toggles) sit between the strat compare grid and the image controls. Presets store directly as the strategy value; mode dropdown now contains only raw modes (Feed content / Source page / Media RSS / None). Caption simplified: checkboxes (Alt/Title/↺ Auto) replace the old auto/always/never dropdown — unchecked = none, checked = always show. Explicit caption source bypasses the auto-suppress heuristic.
- **⚠ Failure badge in feed sidebar** — feeds with unacknowledged fetch failures now show a small ⚠ badge next to their name in the sidebar. Sourced from existing `feed_failure_state` / `problematic_feeds` data; acknowledged failures are suppressed.
- **Tuning tab restructure + strat compare improvements** — strat compare grid moved to top of Tuning tab; **Webcomic** and **Artwork** strategies added to the comparison; per-strategy title/alt text rows now appear below the grid (not inside each card); strategy cards use friendly labels ("Feed content", "Source page", etc.); strategy dropdown options include descriptions; ↺ Auto button styled to match the Refresh button.
- **Alt/title in strat cards + caption source picker** — strat cards now show raw `title` and `alt` attribute text below each image. Two checkboxes (Title / Alt) + "↺ Auto" in Tuning let the user pre-select which attribute(s) to show as the entry caption. Text is pre-loaded during refresh (no pop-in). `entry_lead_images` and `feed_strategy_cache` gained `image_title` column; `feed_display_prefs` gained `caption_source`.
- **GUID-churn suppression: title+date matching** — extended `_suppress_guid_churn` to also match on normalized title + published date within 7 days. Requires ≥4 title words to guard against generic titles. Covers publishers that change both GUID and URL on CMS migrations.
- **Full persistent failure alerting UI** — **Pause/Resume updates** toggle in Feed Properties Info tab lets the user suspend a misbehaving feed without losing it. **Change URL** inline edit (pencil → input → Save) in the XML address row: migrates reader DB via `reader.change_feed_url` and updates all meta DB tables atomically. Both actions reflected immediately in the Properties dialog without a full reload.
- **WebSub (PubSubHubbub) push** — `websub_subscriptions` table, hub discovery on feed add and refresh cycles, HMAC-verified push callback, automatic lease renewal. Enabled when `LECTIO_PUBLIC_URL` is set.
- **Per-folder refresh cadence** — `folders.cadence_minutes` column. Folder Properties dialog has a cadence select (5 min → once a day; default = use global setting). `scheduled_refresh_loop` now tracks per-folder last-refresh time in `app_settings` and only includes a folder's feeds when its cadence has elapsed. Per-folder HTTP Cache-Control / TTL-floor honoring is already handled by the reader library via `update_after` (no separate RSS TTL parsing needed).

## Up next

- ~~**⚠ Failure badge**~~ *(done)*
- ~~**Feed type presets**~~ *(done — phase 1: Webcomic/Artwork preset buttons, mode-only strategy dropdown, caption simplified to checkboxes)*
- ~~**Full persistent failure alerting UI**~~ *(done — Pause/Resume toggle + Change URL in Feed Properties)*
- ~~**Adaptive polling / TTL hints + Per-folder refresh cadence**~~ *(done — per-folder cadence in Folder Properties; reader's update_after already handles HTTP-level TTL floors)*
- ~~**Resurface / GUID-churn suppression: title+date**~~ *(done)*
- ~~**WebSub**~~ *(done)*

## Later

- **API compatibility research** — investigate which APIs would let existing RSS clients (Capy Reader, Fluent Reader, ReadKit, etc.) use Lectio as a backend. Known candidates: Fever (simpler, possibly single-user viable), Miniflux API, GReader/Google Reader subset. Note multi-user requirement per API. Also look at `reader[app]` (the built-in Flask web UI from the reader library) as a lightweight mobile fallback. Output: a short list of what each API requires and what it unlocks, to inform implementation priority.
- **Performance investigation** — systematic baseline before enabling multi-user. Per-request breakdown (DB time, enrich time, refresh contention) under realistic load. Also covers graceful degradation of long-poll/SSE across network switches (phone/tablet use case).
- **Python file header comments** *(quick win, do alongside any other task)* — several `.py` files lack a top-of-file description docstring; standardize across the codebase.

## Backburner

- **Multi-user support / auth refactor** — performance investigation first.
- **Per-user vs. shared thumb cache** — only relevant after multi-user.
- **Archive caps for starred entries** — only relevant after multi-user.
- **Better tuning / live preview** — full entry preview pane, swappable strategy + display settings without saving. Feed type presets may make this unnecessary.
- **YunoHost or other packaging.**
- **PWA / offline-first features.**
