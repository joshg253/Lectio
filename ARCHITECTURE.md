# Lectio Architecture

Lectio is a self-hosted feed reader built around the `reader` Python library. The goal is a fast triage workflow with a clean multi-user architecture and VPS-friendly deployment.

## Contents

Design rationale, split by area. Each file is the *why* behind its
part of the system — what was tried, what broke, and what the current
shape is defending against.

- **[tenancy.md](docs/architecture/tenancy.md)** — Per-user isolation, the resolver seam, and what multi-user mode changes.
- **[feeds.md](docs/architecture/feeds.md)** — Subscribing, fetching, deduplicating, combining and removing feeds.
- **[views.md](docs/architecture/views.md)** — The post list, folders, sorting, layout and page weight.
- **[images.md](docs/architecture/images.md)** — Choosing, rejecting, sizing and serving the image for a post.
- **[reading.md](docs/architecture/reading.md)** — Read Mode, offline reading, and offline actions.
- **[saved.md](docs/architecture/saved.md)** — Read-it-later capture, keeping, and editing a post in place.
- **[apis.md](docs/architecture/apis.md)** — The sync APIs Lectio speaks to third-party clients.

## Layering

- UI/API layer: web routes, handlers, presentation state.
- Services layer: feed operations, tagging, filtering, refresh, readability.
- Storage layer: reader DB, app-data, settings.

The layers run in one process today, but the boundaries should stay clean.

## Reader-first philosophy

`reader` is the primary storage/ops primitive. It already covers:
- feed retrieval and storage,
- read state,
- arbitrary tags and metadata,
- filtering and search,
- statistics,
- plugin support.

Prefer reader API and plugin behavior first. Add custom logic only when the existing reader model cannot express the behavior cleanly.

## Deployment path

Lectio is designed for VPS deployment behind a reverse proxy. Auth is always active; access requires a user account. See `.env.example` for deployment configuration.

## Extension strategy

Use plugin/adapter style for non-native behavior instead of hardwired branching. Prefer replaceable pieces and avoid duplicating `reader` capabilities in app code.

## Security direction

Keep the local-first path simple. Add auth only when exposing the app beyond trusted local use. The multi-user phase makes per-user identity, per-user API tokens, route-level authorization, and SSRF hardening mandatory — see "Multi-user tenancy → Security posture".
