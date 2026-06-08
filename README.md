# Lectio

> **Work in progress.** This README covers features and design intent. Setup documentation is forthcoming.

Lectio is a self-hosted, local-first RSS reader with a focus on fast reading triage. It runs as a single-user server behind a TLS proxy and is designed to be deployed on a personal VPS.

---

## What it is

A three-pane desktop RSS reader (folder tree → post list → article pane) with a mobile drill-in mode. Built on Python + FastAPI + the [`reader`](https://github.com/lemon24/reader) library, with a plain-HTML/JS frontend — no build step, no bundler, no framework.

The design priority is **speed of triage**: quickly marking things read, surfacing what matters, and staying out of the way.

---

## Feature highlights

### Reading experience
- Folder tree with recursive post list; read/unread, saved/starred, tags, sort, and filter
- Keyboard navigation throughout; mobile swipe gestures
- **Context menus** — right-click (or long-press) a feed, folder, or entry for contextual actions (mark feed/folder as read, etc.) without leaving the current view
- **Bulk mark-as-read** — toolbar dropdown or context menu; updates the visible list in-place with no page reload
- **Read History** — reverse-chronological list of individually-opened articles, capped at 2,000 entries (main menu or folder-pane footer)
- **Readability view** — extracts clean article text from the source page
- **Web view proxy** — fetches source pages server-side when sites block embedding; detects Cloudflare/paywall pages
- **Search** within the current scope
- **YouTube duration prefix** — `[H:MM:SS]` shown in post list and title for YouTube feeds

### Lead images
- Per-feed **image extraction strategy**: Auto-detect, Webcomic (source-page scrape), Artwork (for art-portfolio feeds like ArtStation), Feed content only, Source scraping, Media RSS, or None
- **Strategy comparison** in Feed Properties — runs all strategies against the current article, shows results side-by-side with actual image dimensions
- Pin any strategy result as the post thumbnail; set a custom URL or feed favicon as a fixed thumbnail
- **Caption source** — Alt / Title checkboxes select which HTML attribute to show as the image caption; **↺ Auto** applies title-preferred logic with junk suppression; text is pre-loaded at refresh (no pop-in)
- Art-portfolio feeds (ArtStation) auto-assigned **Artwork** strategy; feeds in "comic"-named folders auto-assigned **Webcomic**
- ArtStation feed URLs normalized to `www.artstation.com/username.rss` at add time (avoids TLS hostname issues with underscore usernames)

### Automation
- **Highlight** — keyword/regex rules color-highlight matching titles and article body text
- **Mark as Read** — auto-marks matching entries at fetch time; scoped per feed, folder, or globally
- **Deduplicate** — marks newer duplicates read across feeds; URL slug, title, slug+title, fuzzy, or safe match modes; results logged with per-article detail
- **Email Article rules** — server-side rules that send matching articles via email (Resend); immediate or daily digest mode with Cc option
- All rules fire automatically at refresh time; manual "Run Now" available

### Feed management
- **OPML import/export**
- **RSS auto-discovery** — paste a website URL; probes for `<link rel="alternate">` and common feed path suffixes
- **Page Feed (FakeFeedz)** — subscribe to any webpage as a feed: new links mode or content-change mode, with optional CSS selector
- **YouTube folder sync** — sync a folder to a YouTube channel's video feed via YouTube Data API
- **Hide Shorts** — per-feed toggle (YouTube feeds only) to automatically mark YouTube Shorts as read at fetch time
- **Per-folder refresh cadence** — right-click a folder → Properties to set a custom polling interval (5 min to once a day); overrides the global interval for feeds in that folder
- **Feed Properties** — health status, post counts, backoff state, per-feed image and thumbnail tuning
  - **Pause / Resume updates** — suspend automatic fetching for a feed without unsubscribing
  - **Change URL** — update a feed's URL in-place; history, images, rules, and display prefs migrate automatically

### Reliability
- Conditional HTTP requests (ETag / If-Modified-Since via `reader` library)
- Exponential backoff per feed and per domain on errors; 410 Gone permanently disables a feed
- HTML-response detection — surfaces "returned an HTML page instead of a feed" as a health error
- **GUID-churn suppression** — entries that reappear with a new GUID but the same URL slug, or the same title + date (within 7 days), are automatically marked read after refresh
- Feed User-Agent: `Lectio/0.1 (+https://github.com/joshb253/Lectio)`
- WAL-mode SQLite for all databases

### Real-time updates
- **WebSub (PubSubHubbub)** — feeds that advertise a hub receive real-time push updates instead of waiting for the next poll; HMAC-verified, subscriptions renewed automatically. Requires `LECTIO_PUBLIC_URL` in `.env`.

### API compatibility
- **GReader API** — Google Reader-compatible API at `/greader`; works with Capy, Readrops, Aggregator, Read You, and other Android/desktop clients. Authenticate with your Lectio username and `LECTIO_FEVER_PASSWORD`.
- **Fever API** — Fever-compatible API at `/fever`; works with Reeder, FeedMe, NetNewsWire, etc. Set `LECTIO_FEVER_PASSWORD` in `.env` to enable. Uses a dedicated password (not your main login) because Fever transmits credentials as MD5.

### Data portability
- **Takeout / Export & Import** — ZIP archive containing feeds (OPML), rules, contacts, tags, starred entries, read history, and settings; imports non-destructively
- **Backup script** — online-safe SQLite `VACUUM INTO` snapshots via `scripts/backup_databases.py`

### Integrations
- **Instapaper** — "Save to Instapaper" button in the entry toolbar
- **Email** — Resend API for Email Article and Email Article rules
- **Settings UI** — all API keys and options configurable in-app (env vars still accepted as fallback)

---

## Technical overview

| Layer | What it does |
|---|---|
| `main.py` | FastAPI routes, Jinja2 templates, all request handling |
| `services/` | Feed refresh, lead images, email, starred archive, YouTube, reader API wrapper |
| `reader` library | Feed fetching, parsing, storage, ETag/conditional requests |
| `lectio.db` | reader's SQLite feed+entry store |
| `lectio_meta.sqlite3` | App state: prefs, automation rules, lead images, read history, failure tracking |
| `lectio_meta.sqlite` | Starred/saved entry archive |

---

## Stack

- **Backend**: Python 3.14, FastAPI, uvicorn
- **Feed library**: [reader](https://github.com/lemon24/reader) (handles HTTP, parsing, ETags, scheduling)
- **Frontend**: Vanilla JS, Jinja2 templates, no build step
- **Database**: SQLite (WAL mode) × 3
- **Deployment**: Docker + docker-compose, Traefik reverse proxy

---

## Status

Active personal use. Not yet documented for general deployment. The codebase moves fast — APIs, DB schema, and config format may change without notice.

Issues and PRs welcome, but this is primarily a personal project.
