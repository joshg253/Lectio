# Lectio

Lectio is a local-first browser feed reader with a three-pane layout:

1. Folder tree
2. Recursive post list for the selected folder
3. Post detail view

## Stack

- [FastAPI](https://fastapi.tiangolo.com/) (web app)
- Dedupe Log (hamburger menu): lists duplicate unread post links collapsed by list deduplication, including copy counts and example feeds/titles
- Entry detail display
- Mark read/unread
- In 1-pane mobile mode, swipe post tiles left-to-right to toggle read/unread and right-to-left to toggle save/star, with the tile sliding over the action lane
- Post tiles use a compact card layout with right-side unread/saved quick controls and denser feed/time metadata (Inoreader-inspired)
- Mark all read for folder subtree
- Mark all read for one feed
- Mark read above/below an anchor post
- Posts toolbar includes a centered `Mark Read` menu (between filters and sort controls) with quick actions for current scope and older/newer-than-open ranges
- Save/unsave (star) posts
- Post filters: unread toggle (all <-> unread) plus a star-only override
- Star filter behavior: turning star on shows saved items regardless of read state; turning it off restores the previous all/unread view
- Filter state consistency: all/unread + star state is preserved across folder/feed/tag/search navigation
- Post search (top bar): case-insensitive term matching across title/feed/source text within the current folder/feed/tag scope, ordered by the active sort controls
- Global History view (hamburger menu): ignores folder/feed/star constraints and forces read-most-recent-first ordering with read timestamp display
- Sort by published vs received + ascending/descending toggle
- Oldest-first sorting is stabilized for unread/all views by evaluating complete per-feed slices before global ordering, avoiding surprising jumps to older items after read actions
- Global Note (hamburger menu): a shared plain-text notepad saved in app settings
- Post list chunking in batches of 10 with auto-fill-to-viewport, scroll-to-load, and `j`-key load-next-chunk at the bottom of the visible window
- When opening an entry (click, `j`/`k`, etc.), the active post tile auto-scrolls into view in the list if it isn't fully visible
- Sidebar feed/folder/global unread counters update in real time as posts are marked read (including auto-mark-on-open)
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
- In 1-pane mobile entry view, swipe left/right in the post content area to open next/previous posts in the current list scope
- In 1-pane mobile entry view, pinch/spread zoom uses the browser's native viewport zoom (smooth, GPU-accelerated)
- Entry content media guardrails: oversized inline images are constrained to fit the viewport
- For short blurb-style posts, Lectio attempts to pull a lead image from the source page (for example via og:image/twitter:image) when the feed payload has no inline image
- Standard Ebooks entries prefer canonical `/downloads/cover.jpg` as lead image via site plugin fallback
- Per-site lead-image plugins handle webcomics that need special handling: Gunnerkrigg (URL derived from page number), SMBC (extracted from feed content since the source page is JS-rendered), Penny Arcade (full comic via og:image instead of single panel), Monster Soup / Bad Machinery (skip mature-content gate images and re-fetch the real comic)
- Cached source-derived thumbnails are periodically revalidated so updated hero images on source sites can replace stale thumbs over time
- Entry body images are left intact; lead-image selection does not remove in-article image placements
- Background auto-refresh of all feeds (default every 60 minutes)
- Per-feed manual refresh endpoint
- OPML import/export

## Running Lectio

PowerShell (local machine only):

```powershell
$env:LECTIO_REFRESH_DEBUG = '1'
uv run uvicorn main:app --reload
```

PowerShell (LAN access from phone/tablet):

```powershell
$env:LECTIO_REFRESH_DEBUG = '1'
uv run uvicorn main:app --reload --reload-exclude .venv --host 0.0.0.0 --port 8000
```

Then open `http://<YOUR_LAN_IP>:8000` from another device on the same network.

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
- Mobile action modals are keyboard-aware and shift upward when the virtual keyboard is open.
- Repeatedly failing feeds are automatically retried with exponential backoff (up to 24h), then resume normal cadence once healthy.
- Scheduled refresh: Lectio refreshes all subscribed feeds in the background every 60 minutes by default.
- To change the interval, set `LECTIO_AUTO_REFRESH_MINUTES` before starting the app; values lower than 15 are clamped to 15.
- To disable scheduled refresh, set `LECTIO_AUTO_REFRESH_MINUTES=0`.

## Health check

`GET /healthz` returns `{"status": "ok"}` (HTTP 200) when the meta DB is reachable, or `{"status": "error", "error": "..."}` (HTTP 503) otherwise. Auth-exempt so reverse-proxy probes (Traefik, etc.) don't need credentials.

## Debug mode

`LECTIO_DEBUG=1` enables the `/debug/*` endpoints (lead-image cache clear, feed bypass toggle, etc.) and the topbar debug controls. Defaults to **off** so VPS deploys are safe by default. Local dev workflows (Makefile `run` target, VS Code launch config) already pass it explicitly.

`LECTIO_REFRESH_DEBUG=1` enables verbose refresh logging. Same default-off behavior.

## Authentication

Authentication is **disabled by default** (safe for local use). To enable it, set these environment variables (in a `.env` file or your shell):

```
LECTIO_USERNAME=your-username
LECTIO_PASSWORD=your-password
LECTIO_SECRET_KEY=<random hex string>   # generate: python -c "import secrets; print(secrets.token_hex(32))"
```

A login page is shown on first visit. The session cookie lasts 1 year — you only log in once per browser.

**When running behind a reverse proxy (Traefik, nginx, Caddy) with TLS termination**, also set:

```
LECTIO_HTTPS_ONLY=1
```

This marks the session cookie `Secure` (HTTPS-only) and enables proxy header trust (`X-Forwarded-For`, `X-Forwarded-Proto`).

To sign out, navigate to `/logout`.

### Login brute-force protection

The `POST /login` endpoint is rate-limited per client IP: by default 5 failed attempts within a 5-minute window will return HTTP 429 until the window elapses. A successful login clears the counter for that IP. Tunable via `LECTIO_LOGIN_MAX_FAILURES` and `LECTIO_LOGIN_WINDOW_SECONDS`. Disabled when `LECTIO_DEBUG=1` so dev iteration isn't blocked.

## CSRF protection

All state-changing HTTP methods (`POST`, `PUT`, `PATCH`, `DELETE`) require a per-session CSRF token. The token is generated on first request, stored in the signed session cookie, and rendered into the page via `<meta name="csrf-token">`. A small JS shim handles delivery automatically:

- **SPA async fetches**: a wrapped `window.fetch` adds an `X-CSRF-Token` header to same-origin unsafe-method requests.
- **Native form submits**: a capture-phase `submit` listener injects a hidden `_csrf` input into POST forms before the browser sends them.

`POST /login` is exempt (the rate limit + auth check are the protection there). Browser-driven dev (including LAN access from the phone) and the test suite both work transparently. To submit a POST manually via `curl` / `Invoke-WebRequest`, first GET any page to establish the session, extract the token from the `<meta>` tag, and include it as either the `X-CSRF-Token` header or a `_csrf` form field.

## Outbound URL safety (SSRF guard)

Lead-image source scraping and webcomic plugin fetches resolve their target URL via DNS and refuse to connect if any resolved address is in private / loopback / link-local / multicast IP space. This prevents a malicious feed entry from probing internal services on the host's network. Bypassed when `LECTIO_DEBUG=1` so LAN test feeds remain reachable in dev.

## Persistent logging

Set `LECTIO_LOG_DIR=/var/log/lectio` (or any writable directory) to attach a `RotatingFileHandler` that writes everything from the root logger to `<dir>/lectio.log`. Defaults: 5 MB per file, 5 backups. Tunable via `LECTIO_LOG_MAX_BYTES` and `LECTIO_LOG_BACKUPS`. Stdout logging is unchanged. When `LECTIO_LOG_DIR` is unset, no file is written.

## SQLite backups

Use `scripts/backup_databases.py` to produce a consistent online backup of both databases via `VACUUM INTO` (works while the app is running — no need to copy WAL/SHM sidecar files):

```powershell
uv run scripts/backup_databases.py --dest C:\Backups\Lectio --keep 14
```

`--keep N` retains the N most recent backup pairs (default 7). Schedule with cron / Task Scheduler / a systemd timer for periodic backups.

To restore, stop the app and replace `lectio_reader.sqlite` and `lectio_meta.sqlite3` in the project root with the desired backup files (rename them back to those filenames).

## Graceful shutdown

On shutdown the lifespan handler signals the scheduled-refresh worker to stop and waits up to `LECTIO_SHUTDOWN_TIMEOUT_SECONDS` (default 30) for any in-flight refresh to finish before exiting. If the timeout elapses a warning is logged and the daemon thread is abandoned.

## Deploying behind Traefik

Minimum required env on the deploy host:

```
LECTIO_USERNAME=...
LECTIO_PASSWORD=...
LECTIO_SECRET_KEY=...        # 64+ hex chars
LECTIO_HTTPS_ONLY=1          # secure cookies + trust forwarded headers
LECTIO_AUTO_REFRESH_MINUTES=60
LECTIO_LOG_DIR=/var/log/lectio
```

Health check endpoint for Traefik: `GET /healthz` (auth-exempt; returns 200 when meta DB is reachable, 503 otherwise).

Example Traefik labels (Docker compose snippet):

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.lectio.rule=Host(`lectio.example.com`)
  - traefik.http.routers.lectio.entrypoints=websecure
  - traefik.http.routers.lectio.tls.certresolver=letsencrypt
  - traefik.http.services.lectio.loadbalancer.server.port=8000
  - traefik.http.services.lectio.loadbalancer.healthcheck.path=/healthz
  - traefik.http.services.lectio.loadbalancer.healthcheck.interval=30s
```

First-run bootstrap:

1. Set the env vars above and start the app.
2. Browse to `https://lectio.example.com` → login with `LECTIO_USERNAME` / `LECTIO_PASSWORD`.
3. Import your OPML via the hamburger menu.

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

	## Icons / Glyphs

	- **Source:** Icons used in Lectio come from Google Fonts Icons (Material Symbols) — https://fonts.google.com/icons
	- **Variant used:** Material Symbols Rounded. The app includes the stylesheet in the main template so glyphs render correctly.

	- **Where it's loaded:** the Material Symbols stylesheet is included in the app shell at [templates/index.html](templates/index.html#L15).

	- **How to use in templates:** add a span with the `material-symbols-rounded` class and the symbol name as the text content. Example:

	```html
	<span class="material-symbols-rounded" aria-hidden="true">menu</span>
	```

	- **Notes:**
		- CSS in `static/style.css` already contains rules targeting `.material-symbols-rounded` and various icon helper classes.
		- Where an icon is appropriate, prefer the Material Symbols glyph over custom inline SVGs to keep visual consistency (see `.github/copilot-instructions.md` guidance).

