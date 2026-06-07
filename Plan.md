# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **Feed type presets in Tuning tab** — Webcomic/Artwork preset buttons (pill toggles) sit between the strat compare grid and the image controls. Presets store directly as the strategy value; mode dropdown now contains only raw modes (Feed content / Source page / Media RSS / None). Caption simplified: checkboxes (Alt/Title/↺ Auto) replace the old auto/always/never dropdown — unchecked = none, checked = always show. Explicit caption source bypasses the auto-suppress heuristic.
- **⚠ Failure badge in feed sidebar** — feeds with unacknowledged fetch failures now show a small ⚠ badge next to their name in the sidebar. Sourced from existing `feed_failure_state` / `problematic_feeds` data; acknowledged failures are suppressed.
- **Tuning tab restructure + strat compare improvements** — strat compare grid moved to top of Tuning tab; **Webcomic** and **Artwork** strategies added to the comparison; per-strategy title/alt text rows now appear below the grid (not inside each card); strategy cards use friendly labels ("Feed content", "Source page", etc.); strategy dropdown options include descriptions; ↺ Auto button styled to match the Refresh button.
- **Alt/title in strat cards + caption source picker** — strat cards now show raw `title` and `alt` attribute text below each image. Two checkboxes (Title / Alt) + "↺ Auto" in Tuning let the user pre-select which attribute(s) to show as the entry caption. Text is pre-loaded during refresh (no pop-in). `entry_lead_images` and `feed_strategy_cache` gained `image_title` column; `feed_display_prefs` gained `caption_source`.

## Up next

- ~~**⚠ Failure badge**~~ *(done)*
- ~~**Feed type presets**~~ *(done — phase 1: Webcomic/Artwork preset buttons, mode-only strategy dropdown, caption simplified to checkboxes)*
- **Full persistent failure alerting UI** — Pause updates toggle, Change URL field, full error detail in Feed Properties. GitHub app-release preset: no thumbnail, use the changelog image already in the entry.
- **Adaptive polling / TTL hints + Per-folder refresh cadence** *(do together — same scheduling code)* — honor `ttl`, `skipHours`, `skipDays`, `sy:updatePeriod` from feed XML as per-feed floor values. Let the user assign a custom refresh interval per folder as a ceiling (e.g. "Status/News" every 5–10 min, "Comics" every 2 h). Global interval remains the fallback.
- **WebSub (PubSubHubbub) push support** *(after failure alerting — shares the failure-state infrastructure)* — feeds that publish a `<link rel="hub">` header can push new entries in real-time. Implementation: (1) discover hub on feed add/refresh, POST subscription with callback `{lectio_url}/websub/callback`; (2) `GET /websub/callback` handles hub challenge-response; (3) `POST /websub/callback` processes pushed content immediately; (4) `websub_subscriptions` table tracks hub, lease expiry, HMAC secret; background job renews before expiry. Estimated coverage: ~30–50% of feeds. Reader library has no built-in WebSub plugin — needs custom implementation.
- **Resurface / GUID-churn suppression** *(standalone, slot in anywhere)* — URL-slug matching is done. Remaining: title+date matching for feeds that change both GUID and URL. When a new entry's title + approximate date matches a known read entry in the same feed, auto-mark it read.

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
