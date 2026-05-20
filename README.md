# Lectio

Lectio is a local-first browser RSS reader with a three-pane desktop layout and a one-pane mobile drill-in mode.

## Features

- Folder tree, recursive post list, and post detail view.
- Read/unread, saved/starred, tagging, filtering, and sorting.
- Manual and scheduled refresh.
- Search within the current scope.
- Keyboard navigation.
- Mobile swipe gestures.
- OPML import/export.
- Readability and source views.
- Backup and restore support.
- Debug tooling for development.
- Lead images extracted from og:image, preload hints, and page content.
- Context menus on sidebar items and post entries (right-click or long-press).
  - Right-click a feed or entry to **Mark Feed as Read** without leaving the current view.
  - Right-click a folder to mark all feeds in it as read.
- Bulk mark-as-read (toolbar dropdown or context menu) updates the visible list in-place — no page reload.
- **Email Article** — share button in the entry toolbar sends the title, excerpt, and link as a styled email via Resend (requires `RESEND_API_KEY`, `LECTIO_EMAIL_FROM`, and `LECTIO_EMAIL_TO` in `.env`).
- **RSS Auto-Discovery** — paste a plain website URL when adding a feed; Lectio probes the page for `<link rel="alternate">` RSS/Atom tags and falls back to common feed path suffixes (`/feed`, `/rss.xml`, etc.) before reporting failure.
- **Feed Properties / Image Troubleshooter** — right-click a feed → Properties to inspect feed metadata, health, and post counts. The Image Display section controls three per-feed flags:
  - *Show as thumbnail* — whether the lead image appears in the post list.
  - *Show in article* — whether the lead image is prepended to the article body.
  - *Image caption* — auto (suppress junk captions heuristically), always show, or never show.
  - *Thumbnails strategy* — lock the feed to a specific image source: Auto-detect, Feed content only, Source scraping, or None.
  - *Strategy comparison* — when Properties is opened while reading an article, it automatically runs all four extraction strategies (og_scrape, inline, media_rss, youtube) against that specific entry. Click an **og_scrape** or **inline** card to select that strategy. Click **Refresh** to re-test at any time.
- **Automation** — keyword/regex rules that fire automatically at fetch time. Managed via the main menu → Automation or right-click a folder/feed. Rule types:
  - *Highlight* — marks matching post titles with a color in the post list and entry pane. Client-side only.
  - *Mark as Read* — auto-marks matching entries read when a feed is fetched. Scoped to a specific feed, folder, or globally. Supports title, body, or both search.
  - *Deduplicate* — marks newer duplicates as read across feeds in a folder or globally. Match methods: URL slug, title, slug+title, or fuzzy. Results logged to Automation History.
- **Read History** — the History view (main menu or folder-pane footer) shows individually-opened articles in reverse-read order, capped at 2,000 entries.
- **Settings dialog** — unified settings panel (user avatar button in topbar) with tabs: *Profile* (name, email), *Settings* (timezone display pref, maintenance hour), *Contacts* (email recipients), *Email* (Resend API key + from address), and *Integrations* (YouTube config + Instapaper credentials). All previously env-only keys are now editable in the UI (env still works as fallback).
- **Instapaper integration** — "Save to Instapaper" button in the entry toolbar. Configure credentials in Settings → Integrations. Entries are saved with their tags plus a `viaLectio` tag. Requires an Instapaper account (free).
- **YouTube duration prefix** — entries from YouTube feeds show a `[H:MM:SS]` duration prefix in the post list and entry pane title.
- **Web View proxy** — the Source view (reader icon in entry toolbar) first attempts to load the article in an inline frame. If the site blocks framing (via `X-Frame-Options` or `Content-Security-Policy: frame-ancestors`), Lectio automatically falls back to a server-side proxy that fetches the page, injects a `<base>` tag so relative assets resolve correctly, and adds a persistent "Proxied view · Open original ↗" bar. Cloudflare challenge pages and paywalls are detected and shown as informational notices instead of blank frames.

## Running locally

Use `uv` to run the app and scripts.

## Deployment

A `Dockerfile` and `docker-compose.yml` are included for deployment behind a TLS-terminating reverse proxy (e.g. Traefik on an existing `proxy` network).

1. `cp .env.example .env` and fill in `BASE_URL`, `TZ`, `LECTIO_USERNAME`, `LECTIO_PASSWORD`, `LECTIO_SECRET_KEY`.
2. `mkdir -p data && sudo chown -R 1000:1000 data` (the container runs as uid 1000).
3. `docker compose up -d --build`.

The compose file sets `LECTIO_HTTPS_ONLY=1` and routes `lectio.${BASE_URL}` through Traefik with HSTS/frameDeny/compress middleware.

## Backups

`scripts/backup_databases.py` uses SQLite `VACUUM INTO` for online-safe snapshots and honors `LECTIO_DATA_DIR`, so the same script works locally and in the container.

Schedule it on the VPS host by dropping the following into `/etc/cron.d/lectio-backup`:

```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 3 * * * root docker exec lectio uv run scripts/backup_databases.py --keep 14 >> /opt/lectio/data/logs/backup.log 2>&1
```

Daily at 03:17, keeps 14 days, lands in `/opt/lectio/data/backups/` via the bind mount. Restoring: stop the app, replace the three `lectio_*.sqlite*` files in the data dir with the backup copies (renamed back to their original filenames), restart.

## Notes

- Saved/starred content may be archived for durability.
- Some debug features are intended for development only.
