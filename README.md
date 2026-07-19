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

- **Fast triage** — three-pane reader, keyboard nav, context menus, manual and
  feed-provided tags, read history, search, and a Readability/web-view proxy.
  Bulk mark-as-read shows an **Undo** toast that restores exactly that batch.
- **Rich content** — embeds that actually render (curated trusted-host
  allowlist), inline podcast audio (including audio borrowed from a separate
  host feed), file attachments, recovered YouTube/Bandcamp/SoundCloud embeds,
  and a **persistent audio player** bar that keeps playing as you navigate.
- **Lead images** — per-feed extraction strategies with side-by-side
  comparison, smart crop/fit tuning, caption sourcing, junk-image rejection,
  and full-resolution webcomic panels.
- **Automation** — highlight, mark-as-read, tag-filter, deduplicate,
  email-article, outbound-webhook, save-to-Instapaper, **save/star-article**
  (auto-saves into a pinned Saved **Inbox**), add-to-YouTube-playlist, and
  add-to-Quire rules; scoped to all feeds, a folder, a feed, or a
  multi-selected set; run history shows exactly what each run touched.
- **Keep vs. to-do** — **tagging a post keeps it forever**: it triggers a full
  offline capture (page + images) so tagged posts survive a dead feed, while
  **starring** is the lightweight "needs dealing with" marker. A post is kept
  (never auto-pruned, archived offline) whenever it's starred **or** tagged; the
  unified **Saved** view browses everything kept, filterable per feed and per tag.
- **Read-it-later** — save any page via menu, bookmarklet, `/api/save` (share
  sheets), or a browser extension that ships the rendered page past paywalls;
  saved articles get offline capture, tags, and an e-ink **Read Mode** at
  `/read` (paginated, Supernote-friendly). A **Scan Saved for duplicates**
  utility (with side-by-side Compare and dead-link checking) cleans up
  same-article-different-URL saves; an **Instapaper CSV import** brings your
  whole library over with tags and archive state.
- **Retention** — per-folder *Delete after read* (nightly), a **Purge old
  posts** utility with preview, and tombstones that keep deleted posts from
  resurrecting (swept only after they leave the publisher's feed window).
  Starred and tagged posts are never auto-deleted.
- **Feed management** — OPML, resilient RSS/Atom auto-discovery (survives
  stale autodiscovery links and schemeless input), Page Feeds for feedless
  sites, dev.to filtered feeds, YouTube & DeviantArt sync, Bluesky image
  recovery, per-folder refresh cadence, feed compare, fetch history,
  duplicate-feed scanning, and curation-preserving unsubscribe/combine/move —
  unsubscribing a feed that has starred/tagged posts defaults to **keeping**
  them: the feed leaves the tree but its curated items stay browsable per feed
  in Saved. Per-post fixes: delete (tombstoned), edit date, edit title.
- **Integrations** — Reddit (submit + authenticated fetching), Pinterest
  (pin lead images), Quire (tasks), Instapaper, email (Resend), webhooks;
  per-user OAuth with optional shared-instance credentials. On Star can
  fan out to any of them.
- **Reliability** — conditional GET, per-feed/domain backoff, GUID-churn
  suppression, WebSub real-time push, WAL-mode SQLite, and browser-identity
  fetch fallback for feeds whose servers refuse the default client.
- **Multi-user** — isolated per-user databases with shared content caches;
  **GReader**, **Fever**, and **Miniflux v1** API compatibility.
- **Data portability** — Takeout-style ZIP export/import, online-safe
  backups, and one-shot migrators for **Inoreader, Miniflux, FreshRSS, and
  tt-rss** (feed URLs canonicalized so variants merge, not duplicate).
- **Browser-extension quick subscribe** — answers Feedbin/Nextcloud News
  `?subscribe=` URL patterns, so RSSHub-Radar's quick-subscribe drops feeds
  straight into the Add Feed dialog.

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

Pages stay light at large subscription counts: per-feed row sections (the
sidebar folder feed lists, the Settings → Feeds table, and the Stale view)
load as HTML fragments on first open instead of shipping with every page, and
the app script is a cacheable static file rather than inline JS.

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
