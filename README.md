# Lectio

[![CI](https://github.com/joshg253/Lectio/actions/workflows/ci.yml/badge.svg)](https://github.com/joshg253/Lectio/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![WebSub](https://img.shields.io/badge/realtime-WebSub-FF5700)
![Webhooks](https://img.shields.io/badge/automation-Webhooks-FF5700)
![GReader API](https://img.shields.io/badge/API-Google%20Reader-FF5700)
![Fever API](https://img.shields.io/badge/API-Fever-FF5700)
![Miniflux API](https://img.shields.io/badge/API-Miniflux%20v1-FF5700)
![Last commit](https://img.shields.io/github/last-commit/joshg253/Lectio)

> **Work in progress.** This README covers features and design intent. Setup documentation is forthcoming.

Lectio is a self-hosted feed reader focused on fast reading triage, rich content handling, and automation. It runs well on a personal VPS with full multi-user support, and is built to keep feed reading fast, keyboard-friendly, and workflow-oriented.

---

## What it is

A self-hosted RSS reader with a triage-first interface that adapts from a three-pane desktop layout to narrower tablet and phone workflows. Built on Python + FastAPI + the [`reader`](https://github.com/lemon24/reader) library, with a plain-HTML/JS frontend — no build step, no bundler, no framework.

The design priority is **speed of triage**: quickly marking things read, surfacing what matters, and staying out of the way.

---

## Screenshots

| Dark mode | Light mode |
|---|---|
| ![Dark mode](docs/screenshots/1dark.png) | ![Light mode](docs/screenshots/2light.png) |

More shots (settings, automation, feed properties, tags, history, admin) are in
the **[Screenshots wiki page](https://github.com/joshg253/Lectio/wiki/Screenshots)**.

---

## Feature highlights

Full detail lives in the wiki — **[Features](https://github.com/joshg253/Lectio/wiki/Features)**
and **[Multi-user & APIs](https://github.com/joshg253/Lectio/wiki/Multi-user-and-APIs)**.
The short version:

- **Fast triage** — three-pane reader, keyboard nav, context menus, bulk
  mark-as-read, manual tags, read history, search, and a Readability/web-view proxy.
- **Rich content** — embeds that actually render (curated trusted-host allowlist),
  inline podcast players (incl. audio borrowed from a separate host feed), file
  attachments, recovered YouTube embeds, and bare-text feed cleanup. When an older
  article lost its player (the feed stripped the `<iframe>` before Lectio kept
  them), the missing YouTube/Bandcamp/SoundCloud embed is recovered from the
  source page and re-attached. Bandcamp single-track players (domain-locked to
  the original publisher, so they'd otherwise show "not available") fall back to
  the album player so they actually stream. Titles that arrive HTML-encoded (or
  double-encoded, as Tumblr does — `Magus&rsquo; Castle`) are decoded so they read
  correctly instead of showing the raw entity. A bare
  YouTube or Bandcamp album/track link sitting alone in its own paragraph (common
  when a feed strips the oEmbed iframe) is turned into an inline player. (Bandcamp
  resolves the numeric embed ID from the album page on first open and caches it; the
  embed appears on the next open when the page isn't yet cached.) Reader view re-injects
  allowlisted players (YouTube/Spotify/Bandcamp) that the readability extractor
  would otherwise strip — audio players (Bandcamp/SoundCloud/Spotify) keep their
  proper fixed height instead of a 16:9 video box — and de-duplicates a repeated
  lead image.
  YouTube embeds default to the privacy-enhanced host; a per-user Integrations
  setting switches them to the standard host so Share / Watch Later work, and
  connecting a YouTube account (per-user OAuth) adds an **Add to playlist** control
  beneath each video embed (lists your playlists, creates new ones). A global
  Integrations toggle auto-hides **Shorts** across all YouTube feeds, and a
  **quota meter** estimates your daily YouTube API usage against the cap.
- **Lead images** — per-feed extraction strategies with side-by-side comparison,
  smart crop/fit tuning, caption sourcing, junk-image rejection, inline-SVG art,
  and full-resolution webcomic panels (ComicControl thumb→full promotion). List
  thumbnails fall back to a direct browser load when the server-side image proxy
  is refused (some hosts IP-block the server but serve your own IP fine). The
  proxy also handles hotlink protection: if a host refuses an image fetched with
  no referrer, it retries once with the image's own site as the `Referer`, and
  reader/web view routes hotlink-protected images through the proxy too.
- **Automation** — highlight, mark-as-read, deduplicate, email-article,
  outbound-webhook (with an optional **batch mode** that groups all matches from
  one refresh run into a single `{entries:[...]}` request instead of one call per
  entry), **save-to-Instapaper**, **add-to-YouTube-playlist**, and
  **add-to-Quire** rules (the YouTube rule auto-adds new
  videos — including those embedded in any feed's article — to a chosen playlist,
  with include-Shorts, mark-read, and **min/max-duration** options; quota-capped,
  no double-adds); scope a
  rule to all feeds, a folder, a single feed, or **a multi-selected set of feeds**
  (deduplicate can run across a selected set of feeds, not just a whole folder),
  with a Duplicate button to clone one quickly; all fire at refresh time with a
  manual "Run Now". **Starring** an article can also auto-send it to Instapaper, a
  YouTube playlist, email, Quire, and/or Reddit (Integrations → On Star).
- **Submit to Reddit** — connect a Reddit account (per-user OAuth) via Integrations → Reddit; a **Reddit** button appears in each article's share menu to post a link to any subreddit you choose. Once connected, Reddit feeds are also fetched via the authenticated API (60 req/min vs. anonymous limits), which helps with subreddits that 429 on anonymous RSS polling. Register a **web app** at reddit.com/prefs/apps; the shared-instance credential pattern is supported (admin sets instance-wide creds, users can override per-account). On Star can auto-submit starred articles to a configured subreddit.
- **Save to Pinterest** — connect a Pinterest account (per-user OAuth) and a
  **Pin** button appears on each article, saving its lead image (linked back to
  the source) to a board you pick. Needs `PINTEREST_OAUTH_CLIENT_ID/SECRET`;
  entries without an image can't be pinned.
- **Add to Quire** — connect a [Quire](https://quire.io) account (per-user OAuth)
  and pick a destination project; an **Add to Quire** button then appears on each
  article and creates a task (titled from the entry, with the link in the
  description). A plain click adds straight to your default project; right-click the
  button to open a picker and send it to a different project instead. Also available
  via On Star and Automation rules. Quire's
  per-organization minute/hour rate limits are tracked with a usage meter in
  Settings, and automation runs are capped and back off on a 429. Register an app
  at [quire.io/apps/dev](https://quire.io/apps/dev) with redirect URI
  `https://<your-host>/quire/callback`; creds are per-user (or
  `QUIRE_CLIENT_ID/SECRET` as instance-wide fallback credentials).
- **Feed management** — OPML, RSS/Atom auto-discovery, Page Feeds, YouTube &
  DeviantArt sync, per-folder cadence, feed compare, fetch-history & automations
  tabs, and duplicate-feed scanning. Feeds that aren't in any folder (e.g. after
  a reader migration) are gathered into an **Uncategorized** folder pinned to the
  bottom of the sidebar, so they stay visible and easy to file — right-click a
  feed there and pick **Add to folder** to categorize it. "All Feeds" always
  includes them. In **Settings → Feeds** you can multi-select feeds (or tick a
  folder's header checkbox to select all its feeds) and **bulk move, disable/
  enable, mark-read, refresh, or unsubscribe** them in one action.
- **Reliability** — conditional GET, per-feed/domain backoff, GUID-churn
  suppression, WebSub real-time push, WAL-mode SQLite, and browser-identity
  fetch fallback for feeds whose servers refuse the default client.
- **Multi-user** — isolated per-user databases with shared content caches;
  **GReader**, **Fever**, and **Miniflux v1** API compatibility; Instapaper & email integrations.
- **Data portability** — Takeout-style ZIP export/import, online-safe backups, and platform migration. The Import/Export tab has dedicated migrator subtabs for four readers:
  - **Inoreader** — file upload (ExportTool JSON / native export ZIP / JSON Feed, applying tags and starred state) or OAuth API drip (subscriptions, labels, starred, optional delete-from-source, 250 calls/day rate-limited).
  - **Miniflux** — single-pass REST API import: subscriptions + category folders + starred articles + entry tags.
  - **FreshRSS** — single-pass Google Reader API import: subscriptions + folder assignments + labels-as-tags + starred articles.
  - **tt-rss** — single-pass JSON-RPC API import: subscriptions + category folders + starred articles + labels-as-tags.

  Every migrator canonicalizes incoming feed URLs (old.reddit → www.reddit, `?alt=rss`, trailing slashes, YouTube channel forms) before subscribing, so variant URLs merge into an existing subscription instead of creating duplicate feeds.

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

## Development

- **Tests** — pytest suite (unit, services, integration, scripts) under `tests/`. Run with `uv run pytest`.
- **CI** — GitHub Actions runs the suite on Python 3.14 for every pull request and push to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Dependencies install from the locked `uv.lock` (`uv sync --frozen`), and the run treats any `DeprecationWarning` as an error so they surface immediately rather than accumulating.
- **Dependency audit** — `uv audit` (OSV-backed) scans the locked dependencies for known vulnerabilities and deprecated packages. Run it locally with `make audit`; CI runs the same scan. It's a uv preview feature, so it's kept separate from `make test` locally and the CI step is informational (non-blocking) for now.

---

## Status

Active personal use. Not yet documented for general deployment. The codebase moves fast — APIs, DB schema, and config format may change without notice.

Issues and PRs welcome, but this is primarily a personal project.
