# Lectio

[![CI](https://github.com/joshg253/Lectio/actions/workflows/ci.yml/badge.svg)](https://github.com/joshg253/Lectio/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![WebSub](https://img.shields.io/badge/realtime-WebSub-FF5700)
![GReader API](https://img.shields.io/badge/API-Google%20Reader-FF5700)
![Fever API](https://img.shields.io/badge/API-Fever-FF5700)
![Last commit](https://img.shields.io/github/last-commit/joshg253/Lectio)

> **Work in progress.** This README covers features and design intent. Setup documentation is forthcoming.

Lectio is a self-hosted, local-first RSS reader with a focus on fast reading triage. It runs as a single-user server behind a TLS proxy and is designed to be deployed on a personal VPS.

---

## What it is

A three-pane desktop RSS reader (folder tree → post list → article pane). Built on Python + FastAPI + the [`reader`](https://github.com/lemon24/reader) library, with a plain-HTML/JS frontend — no build step, no bundler, no framework.

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
  attachments, recovered YouTube embeds, and bare-text feed cleanup. Reader view
  re-injects allowlisted players (YouTube/Spotify/Bandcamp) that the readability
  extractor would otherwise strip, and de-duplicates a repeated lead image.
  YouTube embeds default to the privacy-enhanced host; a per-user Integrations
  setting switches them to the standard host so Share / Watch Later work, and
  connecting a YouTube account (per-user OAuth) adds an **Add to playlist** control
  beneath each video embed (lists your playlists, creates new ones).
- **Lead images** — per-feed extraction strategies with side-by-side comparison,
  smart crop/fit tuning, caption sourcing, junk-image rejection, inline-SVG art,
  and full-resolution webcomic panels (ComicControl thumb→full promotion). List
  thumbnails fall back to a direct browser load when the server-side image proxy
  is refused (some hosts IP-block the server but serve your own IP fine).
- **Automation** — highlight, mark-as-read, deduplicate, email-article,
  outbound-webhook, and **add-to-YouTube-playlist** rules (the last auto-adds new
  videos — including those embedded in any feed's article — to a chosen playlist,
  with include-Shorts and mark-read options; quota-capped, no double-adds); scope a
  rule to all feeds, a folder, a single feed, or **a multi-selected set of feeds**,
  with a Duplicate button to clone one quickly; all fire at refresh time with a
  manual "Run Now".
- **Feed management** — OPML, RSS/Atom auto-discovery, Page Feeds, YouTube &
  DeviantArt sync, per-folder cadence, feed compare, fetch-history & automations
  tabs, and duplicate-feed scanning.
- **Reliability** — conditional GET, per-feed/domain backoff, GUID-churn
  suppression, WebSub real-time push, WAL-mode SQLite, and browser-identity
  fetch fallback for feeds whose servers refuse the default client.
- **Optional multi-user** — isolated per-user databases with shared content caches;
  **GReader** and **Fever** API compatibility; Instapaper & email integrations.
- **Data portability** — Takeout-style ZIP export/import and online-safe backups.

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
