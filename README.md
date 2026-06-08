# Lectio

Lectio is a local-first browser RSS reader with a three-pane desktop layout and a one-pane mobile drill-in mode.

## Features

- **Rachel by the Bay** support.
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
- Lead images extracted from og:image, preload hints, and page content. When the article uses a `<picture>` element, the WebP source is preferred over the fallback PNG/JPEG.
- Context menus on sidebar items and post entries (right-click or long-press).
  - Right-click a feed or entry to **Mark Feed as Read** without leaving the current view.
  - Right-click a folder to mark all feeds in it as read.
- Bulk mark-as-read (toolbar dropdown or context menu) updates the visible list in-place — no page reload.
- **Email Article** — share button in the entry toolbar sends the title, excerpt, and link as a styled email via Resend (requires `RESEND_API_KEY`, `LECTIO_EMAIL_FROM`, and `LECTIO_EMAIL_TO` in `.env`).
- **RSS Auto-Discovery** — paste a plain website URL when adding a feed; Lectio probes the page for `<link rel="alternate">` RSS/Atom tags and falls back to common feed path suffixes (`/feed`, `/rss.xml`, etc.) before reporting failure.
- **Feed Properties / Image Troubleshooter** — right-click a feed → Properties to inspect feed metadata, health, and post counts. The Image Display section controls three per-feed flags:
  - *Show as thumbnail* — whether the lead image appears in the post list.
  - *Show in article* — whether the lead image is prepended to the article body.
  - *Feed type preset* — **Webcomic** (source-page scrape with comic-strip image scoring) or **Artwork** (first image from feed content, suited for art-portfolio feeds like ArtStation). ArtStation feeds are auto-assigned **Artwork**; feeds in folders whose name contains "comic" are auto-assigned **Webcomic**.
  - *Image source* — override the raw extraction mode: **Auto-detect**, **Feed content**, **Source page**, **Media RSS**, or **None**.
  - *Caption* — **Alt** / **Title** checkboxes select which HTML attribute to show as the image caption. Both unchecked = no caption; **↺ Auto** = title-preferred with automatic junk suppression. The selected text is available on first open (source fetch waits up to 3 s) and pre-loaded at subsequent refresh — no pop-in.
  - *Post thumbnail* — per-entry auto image, a pinned custom URL, or one of the strategy-detected images.
  - *Strategy comparison* — when Properties is opened while reading an article, it automatically runs all extraction strategies against that specific entry and displays the resulting images side-by-side with their actual source dimensions. Each card also shows the **title** and **alt** attribute text extracted for that image. Click a card to select that strategy; click **📌** to pin that image as the post thumbnail. Click **Refresh** to re-test at any time.
  - *Pause / Resume updates* — the **Updates** row in the Info tab has a toggle to suspend automatic fetching for a specific feed without unsubscribing.
  - *Change URL* — click **Edit** next to the XML address to update the feed's URL in-place. All history, images, automation rules, and display preferences are migrated to the new URL automatically.
- **Automation** — keyword/regex rules that fire automatically at fetch time. Managed via the main menu → Automation or right-click a folder/feed. Rule types:
  - *Highlight* — marks matching post titles and article body text with a color in the post list and entry pane. Client-side only.
  - *Mark as Read* — auto-marks matching entries read when a feed is fetched. Scoped to a specific feed, folder, or globally. Supports title, body, or both search.
  - *Deduplicate* — marks newer duplicates as read across feeds in a folder or globally. Match methods: URL slug, title, slug+title, fuzzy, or safe. Results logged to Automation History with a per-article detail list.
- **Read History** — the History view (main menu or folder-pane footer) shows individually-opened articles in reverse-read order, capped at 2,000 entries.
- **Settings dialog** — unified settings panel (user avatar button in topbar) with tabs: *Profile* (name, email), *Settings* (timezone display pref, maintenance hour), *Contacts* (email recipients), *Email* (Resend API key + from address), and *Integrations* (YouTube config + Instapaper credentials). All previously env-only keys are now editable in the UI (env still works as fallback).
- **Instapaper integration** — "Save to Instapaper" button in the entry toolbar. Configure credentials in Settings → Integrations. Requires an Instapaper account (free).
- **YouTube duration prefix** — entries from YouTube feeds show a `[H:MM:SS]` duration prefix in the post list and entry pane title.
- **Web View proxy** — the Source view (reader icon in entry toolbar) first attempts to load the article in an inline frame. If the site blocks framing (via `X-Frame-Options` or `Content-Security-Policy: frame-ancestors`), Lectio automatically falls back to a server-side proxy that fetches the page, injects a `<base>` tag so relative assets resolve correctly, and adds a persistent "Proxied view · Open original ↗" bar. Cloudflare challenge pages and paywalls are detected and shown as informational notices instead of blank frames.
- **Email Article rules** — server-side automation for Email Article rules fires at every feed refresh. Two delivery modes: *Immediately* sends one email per matching new article (capped at 10 per refresh cycle); *Batch* queues articles and flushes as a digest when the configured `batch_time` (HH:MM, local) arrives each day or when a count threshold is hit. The *Cc me* option adds your profile email as Cc, suppressed when it already matches the To address.
- **Hide Shorts** — per-feed toggle in Feed Properties → Tuning (YouTube feeds only). When enabled, YouTube Shorts (entries whose link contains `/shorts/`) are automatically marked as read at fetch time.
- **Takeout / Export & Import** — main menu → Takeout → *Export ZIP* downloads a `lectio-takeout-YYYYMMDD.zip` containing your feeds (OPML), automation rules, email contacts, tagged entries, starred entries, read history, and non-sensitive settings. *Import ZIP* uploads the ZIP to any Lectio instance and merges data non-destructively (rules and contacts skip duplicates, history appends, tags and stars are re-applied to matching entries).
- **GUID-churn suppression** — after each feed refresh, entries that reappear with a new GUID but the same URL slug or the same title + publication date (within 7 days) are automatically marked read. Covers CMS migrations that change both GUID and permalink.
- **Per-folder refresh cadence** — right-click a folder → Properties to set a custom polling interval (5 min to once a day). Overrides the global interval for feeds in that folder; the global interval remains the fallback.
- **Page Feed (FakeFeedz)** — subscribe to any page as an RSS feed without an external service. Two modes: *New links* surfaces each new link on the page as a feed entry; *Content changes* creates an entry whenever the page content changes. Supports a CSS selector to narrow the watched region. Add via the **+** menu → *Page Feed*, or from the toast that appears when a URL has no RSS feed.
- **Dev feeds** — when `LECTIO_DEBUG=1`, six synthetic feed endpoints are served in RSS, Atom, and JSON Feed formats (`/dev/feeds/email-match.*`, `/dev/feeds/email-skip.*`) that generate fresh entries on every request for testing email automation rules. Dev feeds bypass the 60-second manual refresh cooldown. A "Flush email batch queue" button appears in Feed Properties for dev feeds.

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
