# Lectio

Lectio is a local-first browser feed reader with a three-pane layout:

1. Folder tree
2. Recursive post list for the selected folder
3. Post detail view

The app is built in this folder only and currently targets local development first.

## Stack

- [FastAPI](https://fastapi.tiangolo.com/) (web app)
- [reader](https://reader.readthedocs.io/en/stable/) (feed retrieval/storage engine)
- [SQLite](https://www.sqlite.org/) (reader database + small metadata database for folder mapping)
- [uv](https://docs.astral.sh/uv/) (environment and dependency management)

## How reader is linked into Lectio

Lectio uses `reader` as the primary feed backend:

- Feed ingestion and persistence: `reader.add_feed()`, `reader.update_feed()`
- Entry retrieval: `reader.get_entries()` and `reader.get_entry()`
- Read state: `reader.mark_entry_as_read()` and `reader.mark_entry_as_unread()`
- Saved/starred state (currently app-managed, planned migration to reader important): `/entries/saved`
- Resource tags for manual entry tags: `reader.get_tags()`, `reader.set_tag()`, `reader.delete_tag()`

`feedparser` is used in a limited helper role for feed tag suggestion parsing.

Lectio keeps folder taxonomy separately in `lectio_meta.sqlite3` and maps feeds to folders there.
This separation lets us keep reader's feed/entry lifecycle intact while supporting custom folder UX and OPML import/export behavior.

## Run locally

```powershell
$env:LECTIO_REFRESH_DEBUG = '1'
uv run uvicorn main:app --reload
```

Open http://127.0.0.1:8000

## Current features

- Three-pane browser UI
- Responsive pane modes:
	- Wide: 3-pane layout with adjustable pane widths and a folders collapse/strip control.
	- Medium: 2-pane posts+detail layout with folders as a flyout strip; when resizing from wide to medium, the posts (middle) width is preserved and the detail pane fills remaining space.
	- Narrow: single-pane drill-in navigation (folders -> posts -> entry).
- Folder creation
- Folder deletion
- Add feed URL to folder
- Move feed between folders
- Unsubscribe feed from folder/app
- Recursive post listing for selected folder
- Entry detail display
- Mark read/unread
- In 1-pane mobile mode, swipe post tiles left-to-right to toggle read/unread and right-to-left to toggle save/star, with the tile sliding over the action lane
- Post tiles use a compact card layout with right-side unread/saved quick controls and denser feed/time metadata (Inoreader-inspired)
- Mark all read for folder subtree
- Mark all read for one feed
- Mark read above/below an anchor post
- Save/unsave (star) posts
- Post filters: unread toggle (all <-> unread) plus a star-only override
- Star filter behavior: turning star on shows saved items regardless of read state; turning it off restores the previous all/unread view
- Filter state consistency: all/unread + star state is preserved across folder/feed/tag/search navigation
- Post search (top bar): case-insensitive term matching across title/feed/source text within the current folder/feed/tag scope, ordered by the active sort controls
- Global History view (hamburger menu): ignores folder/feed/star constraints and forces read-most-recent-first ordering with read timestamp display
- Sort by published vs received + ascending/descending toggle
- Oldest-first sorting is stabilized for unread/all views by evaluating complete per-feed slices before global ordering, avoiding surprising jumps to older items after read actions
- Global Note (hamburger menu): a shared plain-text notepad saved in app settings
- Post list chunking in batches of 10 with auto-fill-to-viewport and scroll-to-load
- Manual entry tags with suggestions
- Left pane tags card with counts and click-to-filter
- Left pane quick-action utility strip (Saved toggle, Tags toggle, Global Note, Problem Feeds, Pin/Unpin)
- Post list cards show a left-side thumbnail when an entry exposes an image (inline or linked image asset), with a fallback placeholder when it does not
- Problematic feeds view (hazard icon) with failure count and retry timing
- Problematic feeds warning indicator only signals new failures since the last time you opened the list; existing unresolved items remain listed until they recover
- Feed properties/status endpoint and panel data
- Source/readability/frame-check entry endpoints for source loading modes
- Entry header quick actions for save + read/unread, with Reader/Web/Open controls moved into the lower tag/action row
- Entry header read/unread toggles update in place (no full page reload) and keep list/header state synchronized
- Entry content media guardrails: oversized inline images are constrained to fit the viewport
- For short blurb-style posts, Lectio attempts to pull a lead image from the source page (for example via og:image/twitter:image) when the feed payload has no inline image
- Standard Ebooks entries prefer canonical `/downloads/cover.jpg` as lead image via site plugin fallback
- Entry body images are left intact; lead-image selection does not remove in-article image placements
- Background auto-refresh of all feeds (default every 60 minutes)
- Per-feed manual refresh endpoint
- OPML import/export

## YouTube Video Embeds

- For YouTube feeds (channel video feeds), Lectio now automatically embeds the YouTube video player at the top of the post body in the entry detail view.
- The embed is only injected for trusted YouTube feeds and is not present if the post body already contains a YouTube embed.

### Video duration

- Lectio will attempt to include `duration_seconds` (integer) and `duration_display` (string like `3:21` or `1:02:05`) in the entry detail JSON for YouTube videos.
- Initial post-list rendering does not wait on uncached YouTube duration lookups. Cached durations can still appear in the list, but cold page loads prioritize instant rendering.
- By default Lectio tries the YouTube Data API (recommended). To enable the API, set the environment variable `YOUTUBE_API_KEY` before starting the app.

PowerShell example (temporarily for the current shell):

```powershell
$env:YOUTUBE_API_KEY = 'YOUR_API_KEY_HERE'
$env:LECTIO_REFRESH_DEBUG = '1'
uv run uvicorn main:app --reload
```

If `YOUTUBE_API_KEY` is not set, Lectio falls back to scraping the YouTube video page for a duration value (less reliable).

<!-- Embed customization removed — embeds use fixed player params and no add-to-playlist link -->

## Refresh behavior

- Manual refresh: use the `Refresh Selected` button to update feeds in the current folder subtree.
- Manual refresh keeps your current scope/filter context (and selected entry when available) instead of resetting view state.
- In 1-pane mobile mode, pull-to-refresh on Folders or Posts updates counts/posts in place without a full page reload.
- Repeatedly failing feeds are automatically retried with exponential backoff (up to 24h), then resume normal cadence once healthy.
- Scheduled refresh: Lectio refreshes all subscribed feeds in the background every 60 minutes by default.
- To change the interval, set `LECTIO_AUTO_REFRESH_MINUTES` before starting the app; values lower than 15 are clamped to 15.
- To disable scheduled refresh, set `LECTIO_AUTO_REFRESH_MINUTES=0`.

## Keyboard shortcuts

These shortcuts are active when focus is not inside an input/textarea/select field.

- `/`: Focus search
- `j` / `k`: Open next / previous visible post
- `n` / `p`: Move selected post highlight down / up
- `m`: Toggle read/unread for active post
- `f` or `s`: Toggle save/star for active post
- `b` or `o`: Open active post in a new tab
- `w`: Toggle Reader view for the open post
- `v`: Toggle Web view (embedded source) for the open post
- `a`: Open Add Feed modal
- `d`: Pin or unpin the left pane
- `r`: Refresh current feed (or current folder when no feed is active)
- `t`: Toggle entry tags panel
- `Escape`: Close open flyouts/modals/menus and dismiss tags/search focus

## OPML and test data

- Sample OPML file: `devdata/sample_test_set.opml`
- Generator script: `devdata/generate_sample_opml.py`
- Includes a bootstrap JSON Feed sample URL for reader JSON Feed validation

Regenerate sample set:

```powershell
uv run devdata/generate_sample_opml.py
```

## Attribution and references

The project workflow and tooling choices in this repo were informed by:

1. UV skills article and shared skill files:
	- https://mathspp.com/blog/uv-skills
	- https://mathspp.com/blog/uv-skills/SKILL-python-via-uv.txt
	- https://mathspp.com/blog/uv-skills/SKILL-uv-script-workflow.txt
2. reader project/docs and release notes:
	- https://death.andgravity.com/reader-3-22
	- https://reader.readthedocs.io/en/stable/
