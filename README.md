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
- **Feed Properties / Image Troubleshooter** — right-click a feed → Properties to inspect feed metadata, health, and post counts. The Image Display section controls three per-feed flags:
  - *Show as thumbnail* — whether the lead image appears in the post list.
  - *Show in article* — whether the lead image is prepended to the article body.
  - *Image caption* — auto (suppress junk captions heuristically), always show, or never show.
  - *Thumbnails strategy* — lock the feed to a specific image source: Auto-detect, Feed content only, Source scraping, or None.
  - *Strategy comparison* — when Properties is opened while reading an article, it automatically runs all four extraction strategies (og_scrape, inline, media_rss, youtube) against that specific entry. Click an **og_scrape** or **inline** card to select that strategy. Click **Refresh** to re-test at any time.

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
