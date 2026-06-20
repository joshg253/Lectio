"""Seed a demo Lectio library for the screenshot tooling.

Run as a subprocess (so it picks up ``LECTIO_DATA_DIR`` before importing the app):

    LECTIO_DATA_DIR=<demo> LECTIO_DEBUG=1 DEMO_BASE_URL=http://127.0.0.1:PORT \\
        uv run python -m scripts.screenshots.seed

``LECTIO_DEBUG=1`` is required so the SSRF guard allows the localhost demo feed
server. The demo feeds must already be served at ``DEMO_BASE_URL`` (the
orchestrator starts that static server). This script subscribes them, refreshes
once, and sets a realistic read/saved/tag state.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

from scripts.screenshots import demo


def main() -> int:
    base_url = os.environ.get("DEMO_BASE_URL")
    if not base_url:
        print("DEMO_BASE_URL is required", file=sys.stderr)
        return 2

    import main as app  # imported after LECTIO_DATA_DIR is set

    app.ensure_meta_schema()
    app.ensure_starred_archive_schema()

    feed_urls: list[str] = []
    with app.get_meta_connection() as conn:
        for folder, _title, slug, _rss in demo.feeds():
            url = f"{base_url}/{slug}.xml"
            folder_id = app._get_or_create_folder_by_name(conn, folder)
            with app.get_reader() as reader:
                reader.add_feed(url, exist_ok=True)
            conn.execute(
                "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                (folder_id, url),
            )
            feed_urls.append(url)
        conn.commit()

    # Fetch the demo content (localhost; fast and deterministic).
    with app.get_reader() as reader:
        reader.update_feeds()

    # Give the library a lived-in look: mark most entries read, save a couple,
    # and apply a few manual tags. Deterministic so screenshots are stable.
    tags_by_position = {
        1: "favorite", 4: "favorite", 8: "favorite", 11: "favorite",
        3: "to-read", 9: "to-read", 6: "reference",
    }
    saved_positions = {2, 5}
    read_history_rows: list[tuple] = []
    with app.get_reader() as reader:
        feed_titles = {f.url: (f.title or "") for f in reader.get_feeds()}
        entries = sorted(
            reader.get_entries(),
            key=lambda e: (str(e.feed_url), str(e.id)),
        )
        with app.get_meta_connection() as conn:
            for pos, entry in enumerate(entries):
                rid = (str(entry.feed_url), str(entry.id))
                # Leave the newest few unread (every 4th entry stays unread).
                if pos % 4 != 0:
                    reader.mark_entry_as_read(rid)
                    # Mirror the open into the Read History log (newest first).
                    read_at = (datetime.now() - timedelta(hours=pos * 5)).isoformat()
                    read_history_rows.append((
                        rid[0], rid[1], str(entry.title or ""), str(entry.link or ""),
                        feed_titles.get(rid[0], ""), read_at,
                    ))
                tag = tags_by_position.get(pos)
                if tag:
                    reader.set_tag(rid, f"{app.MANUAL_TAG_KEY_PREFIX}{tag}")
                if pos in saved_positions:
                    conn.execute(
                        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                        rid,
                    )
            conn.executemany(
                "INSERT OR IGNORE INTO read_history"
                " (feed_url, entry_id, title, link, feed_title, read_at) VALUES (?, ?, ?, ?, ?, ?)",
                read_history_rows,
            )
            conn.commit()

    # Synthetic per-feed fetch history so the Feed Properties → History tab has
    # content (mostly successful refreshes, with one error to populate the
    # "Errors" filter).
    now = time.time()
    with app.get_meta_connection() as conn:
        for fi, url in enumerate(feed_urls):
            for h in range(8):
                fetched_at = now - (h * 6 + fi) * 3600
                if fi == 0 and h == 2:
                    conn.execute(
                        "INSERT INTO feed_fetch_history (feed_url, fetched_at, status,"
                        " http_status, new_entries, duration_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (url, fetched_at, "error", 503, None, 4200,
                         "Server error (HTTP 503)"),
                    )
                else:
                    conn.execute(
                        "INSERT INTO feed_fetch_history (feed_url, fetched_at, status,"
                        " http_status, new_entries, duration_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (url, fetched_at, "ok", 200, (2 if h == 0 else 0),
                         140 + (h * 37 + fi * 11) % 480, None),
                    )
        conn.commit()

    # A couple of automation rules so the Automation tab has real content.
    with app.get_meta_connection() as conn:
        app.add_highlight_keyword(
            conn, "global", "", "local-first", "green",
            rule_type="highlight", search_in="title", enabled=1,
        )
        app.add_highlight_keyword(
            conn, "global", "", "sponsored", "orange",
            rule_type="mark_as_read", search_in="title", enabled=1,
        )
        conn.commit()

    print(f"Seeded {len(feed_urls)} demo feeds, {len(entries)} entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
