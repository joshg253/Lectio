# Lectio Architecture

Lectio is a local-first, single-user RSS reader built around the `reader` Python library. The goal is a fast triage workflow that can later grow into VPS deployment without a rewrite.

## Layering

- UI/API layer: web routes, handlers, presentation state.
- Services layer: feed operations, tagging, filtering, refresh, readability.
- Storage layer: reader DB, app-data, settings.

The layers run in one process today, but the boundaries should stay clean.

## Reader-first philosophy

`reader` is the primary storage/ops primitive. It already covers:
- feed retrieval and storage,
- read state,
- arbitrary tags and metadata,
- filtering and search,
- statistics,
- plugin support.

Prefer reader API and plugin behavior first. Add custom logic only when the existing reader model cannot express the behavior cleanly.

## View state model

Keep three kinds of state separate:
- remembered base preferences,
- contextual temporary overrides,
- transient navigation state.

Examples:
- remembered: sort mode, default filters, pane sizing.
- temporary: tag-click “show all,” search result scope.
- transient: current entry, scroll position, focus.

Temporary overrides must not silently overwrite remembered preferences. Leaving the override context should restore the base preference.

## Adaptive layout model

Lectio uses responsive layouts rather than a fixed three-pane assumption:
- wide desktop: 3-pane side-by-side,
- medium tablet landscape: 2-pane refinement,
- narrow phone portrait: 1-pane drill-in navigation.

The priority is fast triage, not always showing three panes.

## Deployment path

Current target is local-first single-user. Later phases may add basic auth behind a reverse proxy for VPS deployment. Keep auth non-invasive so that path does not require a rewrite.

## Extension strategy

Use plugin/adapter style for non-native behavior instead of hardwired branching. Prefer replaceable pieces and avoid duplicating `reader` capabilities in app code.

## Lead image pipeline

`LeadImageService` (services/lead_images.py) resolves a hero image for each entry using a layered strategy:

1. **Feed-level strategy** (`feed_lead_image_strategy` table) — detected automatically and cached weekly. Values: `og_scrape`, `inline`, `media_rss`, `youtube`, `artwork`, `webcomic`, `none`, `unknown`. Two auto-taggers run at startup: `_auto_tag_artwork_feeds` (matches `artstation.com` URLs → `artwork`) and `_auto_tag_webcomic_feeds` (folder name contains "comic" → `webcomic`). Artwork wins over webcomic when both conditions apply. Manual overrides (`manual=1`) are never overwritten by either tagger.
2. **Plugin fallbacks** — site-specific handlers (e.g. YouTube thumbnail from video ID).
3. **Source-page scraping** — fetches the article URL, checks `og:image` / `twitter:image` meta tags (both `property=` and `name=` attribute order), preload hints, then scored in-page images.
4. **Inline feed content** — images embedded in `<content>` or `<summary>` elements.

Results are stored in `entry_lead_images (feed_url, entry_id, image_url, fetched_at)`. NULL means "no image found." Negative results are retried after **4 hours** (`_NEGATIVE_RETRY_SECONDS`); positive results are revalidated after 12 hours (`_POSITIVE_REVALIDATE_SECONDS`). An existing non-NULL URL is never overwritten with NULL during revalidation.

## Async bulk mark-read

`/feeds/mark-read`, `/folders/mark-read`, and `/entries/mark-older-than-read` serve two response modes controlled by the `X-Requested-With` request header:

- **`lectio-mark-read`** (sent by the JS fetch path): returns `{"ok": true, "marked": N, ...}` with HTTP 200. The client applies an optimistic in-place read-state update via `applyBulkReadState()` before the fetch completes.
- **Anything else** (native form submit fallback): returns an HTTP 303 redirect to the main page with a `message=` query param.

The JS layer reads the CSRF token explicitly from `<meta name="csrf-token">` and adds it as `X-CSRF-Token` on every async POST.

## Security direction

Keep the local-first path simple. Add auth only when exposing the app beyond trusted local use.
