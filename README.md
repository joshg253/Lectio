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

| Read Mode (e-ink) | Saved articles |
|---|---|
| ![Read Mode](docs/screenshots/13readmode.png) | ![Saved](docs/screenshots/12saved.png) |

More shots — the phone layout, Save Article, settings, automation, feed
properties, tags, history and admin — are on the
**[Screenshots wiki page](https://github.com/joshg253/Lectio/wiki/Screenshots)**.
All of them are generated from synthetic demo data, never a real library.

---

## What it does

The full tour — every feature, organized by area — is on the
**[Features wiki page](https://github.com/joshg253/Lectio/wiki/Features)**. The
short version:

- **Triage first.** Three-pane reader, keyboard nav everywhere, context menus,
  bulk mark-as-read/unread that updates in place, per-view remembered sort, and
  a layout that collapses to two panes on a tablet and one on a phone. Checkbox
  multi-select on the post list drives bulk tag/star/playlist/move actions —
  see the [Features wiki page](https://github.com/joshg253/Lectio/wiki/Features)
  for the detail.
- **Picks up where you left off.** Close the tab, press Back once too often, or
  swipe the app away — reopening Lectio returns you to the same article, scrolled
  to the same place. It also installs to your home screen as a standalone app.
- **Built for a phone, not just shrunk onto one.** One pane at a time, with Back
  walking the view stack — article → feed → folder — and then toggling the folder
  drawer instead of closing the tab out from under you.
- **Tagging a post keeps it forever.** A tag triggers a full offline capture —
  page, readability text, every image, and any files the post links to — so a
  kept article survives the site going down. Stars are the to-do pile; tags are
  the keep pile.
- **Read-it-later built in.** Save any page (menu, bookmarklet, `/api/save`, or
  the browser extension) with no feed needed, then read it in **Read Mode**, an
  e-ink-friendly reading app at `/read` that works offline — including
  Archive, Delete and mark-read, which queue and drain when you reconnect.
- **Content that actually renders.** Feed sanitization is Lectio's own, so
  embeds survive: sandboxed players from a curated host allowlist, inline SVG
  and MathML, recovered YouTube embeds, podcast audio (even when it lives in a
  separate host feed), inline LaTeX math, and images kept at the author's
  layout and a sane size.
- **Automation.** Highlight, mark-as-read, tag-filter, deduplicate, and
  send-to-destination rules (Instapaper, Pinterest, Reddit, Quire, email,
  YouTube playlists, webhooks) at any scope, with dry-run and run history.
- **Feed management that expects the real web.** Resilient auto-discovery,
  Page Feeds for feedless sites, dev.to and DeviantArt adapters, conditional
  GET, per-feed and per-domain backoff, GUID-churn suppression, feed compare,
  duplicate scanning, unsubscribe that keeps your curation, and an optional
  outbound proxy with FlareSolverr/last-resort-backend escalation for feeds
  behind an anti-bot challenge.
- **Fix a post in place.** Edit its date, title or URL; clean up its body with
  an Aardvark-style element remover; re-fetch its content — undoably, with an
  Internet Archive fallback — one post, or a whole feed or folder at a time.
  Teaser-only feeds can fetch the full article automatically as new posts
  arrive, switched on per folder or per feed.
- **Yours to run.** Isolated per-user databases with shared content caches,
  Google Reader / Fever / Miniflux v1 APIs, WebSub push, Takeout-style
  export/import, and no build step: plain HTML and JS, no bundler, no framework.

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

Dates, saved-article recovery, per-feed file capture, suggested tags, comic
episode expansion, transparent-image handling, the refresh watchdog and the
page-weight work are all described on the
**[Features wiki page](https://github.com/joshg253/Lectio/wiki/Features)** —
they are behaviour, not architecture, and this file is meant to stay short.
Design rationale for any of it lives in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## Stack

- **Backend**: Python 3.14, FastAPI, uvicorn
- **Feed library**: [reader](https://github.com/lemon24/reader) (handles HTTP, parsing, ETags, scheduling)
- **Frontend**: Vanilla JS, Jinja2 templates, no build step
- **Database**: SQLite (WAL mode) × 3
- **Deployment**: Docker + docker-compose, Traefik reverse proxy

---

## Development

Enable the lint hook once per clone:

```bash
git config core.hooksPath .githooks
```

It runs `scripts/lint_changed.py` over the lines you staged (ruff, ~40ms) so a
lint problem is fixed in the commit rather than after a CI round-trip. CI runs
the same script across the whole PR. Both are scoped to *changed lines* rather
than whole files, because the repo carries a backlog of existing findings — new
code is held to the rules while the backlog shrinks file by file. `git commit
--no-verify` bypasses it.


- **Tests** — pytest suite (unit, services, integration, scripts) under `tests/`. Run with `make test` (or `uv run pytest`).
- **Rebuilding the container** — `make rebuild` prunes the BuildKit cache to a ceiling (`BUILD_CACHE_MAX`, 2GB), builds, and restarts. The image bakes the source in, so a rebuild is what makes a commit live. Rebuilding after every commit had grown the cache to 7.9GB across 57 entries; capping it evicts only the old tail, so recent layers still make builds fast. The target refuses to run while a `docker compose exec` is in flight — recreating the container kills a session mid-write.
- **Scratch cleanup** — `make test` runs `make clear-scratch` first, and the local-verification workflow does the same before launching a dev server. On a host where `/tmp` is a small RAM-backed tmpfs, each full suite run leaves its per-test SQLite scratch behind; once `/tmp` fills, pytest reports *mass failures that look like real regressions* rather than an out-of-space error. `scripts/clear_dev_scratch.py` clears only known-disposable paths — never a bare `/tmp/*` sweep — and skips both the running session and anything under two days old, so a concurrent session is safe. `--dry-run` shows what would go.
- **CI** — GitHub Actions runs the suite on Python 3.14 for every pull request and push to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Dependencies install from the locked `uv.lock` (`uv sync --frozen`), and the run treats any `DeprecationWarning` as an error so they surface immediately rather than accumulating.
- **Dependency audit** — `uv audit` (OSV-backed) scans the locked dependencies for known vulnerabilities and deprecated packages. Run it locally with `make audit`; CI runs the same scan. It's a uv preview feature, so it's kept separate from `make test` locally and the CI step is informational (non-blocking) for now.
- **Backups** — `scripts/backup_databases.py` takes a consistent `VACUUM INTO` snapshot of every database; `scripts/ship_backups_to_b2.py` ships that snapshot to a Backblaze B2 bucket via `rclone move` and clears it locally on success. `deploy/systemd/` has the daily timer and install steps; see its README.

---

## Status

Active personal use. Not yet documented for general deployment. The codebase moves fast — APIs, DB schema, and config format may change without notice.

Issues and PRs welcome, but this is primarily a personal project.
