from __future__ import annotations

import sys
import pathlib
import time

# Ensure repo root is on sys.path when running as a script
repo_root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from main import get_reader, lead_image_service


def backfill_feed(feed_url: str) -> None:
    print(f"Backfilling all entries for {feed_url}")
    try:
        with get_reader() as reader:
            entries = list(reader.get_entries(feed=feed_url))
    except Exception as e:
        print("Failed to read feed entries:", e)
        return

    count = 0
    for entry in entries:
        entry_link = getattr(entry, "link", None) or ""
        entry_id = getattr(entry, "id", None) or ""
        if not entry_id:
            continue
        try:
            if not entry_link:
                # Try extracting inline thumbnail first
                thumb = lead_image_service.extract_entry_thumbnail_url(entry, include_source_lookup=False)
                lead_image_service.store_entry_lead_image(str(feed_url), str(entry_id), thumb)
            else:
                # Try fetching source lead image
                src = lead_image_service._fetch_source_lead_image(entry_link)
                lead_image_service.store_entry_lead_image(str(feed_url), str(entry_id), src)
            count += 1
            # be polite
            time.sleep(0.12)
        except Exception as e:
            print("error for", entry_id, e)
    print(f"Backfilled {count} entries.")
    # Warm cache from DB
    lead_image_service.warm_cache_from_db()


if __name__ == "__main__":
    # Find PC Gamer feeds
    reader = get_reader()
    matched = []
    for feed in reader.get_feeds():
        try:
            url = getattr(feed, "url", None) or getattr(feed, "link", None)
        except Exception:
            url = None
        if not url:
            continue
        if "pcgamer" in url.lower():
            matched.append(str(url))

    if not matched:
        print("No PC Gamer feeds found.")
        sys.exit(0)

    for f in matched:
        backfill_feed(f)

    print("Done.")
