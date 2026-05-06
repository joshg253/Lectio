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

## Running locally

Use `uv` to run the app and scripts.

## Deployment

Lectio can later be deployed behind a reverse proxy with basic auth if needed.

## Backups

Use the provided backup script to snapshot the SQLite databases.

## Notes

- Saved/starred content may be archived for durability.
- Some debug features are intended for development only.
