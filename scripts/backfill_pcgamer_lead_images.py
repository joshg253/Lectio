"""Backfill lead-image fetches specifically for the PC Gamer feed."""
from __future__ import annotations

import sys

import pathlib

# Ensure repo root is on sys.path when running as a script
repo_root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from main import get_reader, lead_image_service


def main():
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
        return 0

    print(f"Found {len(matched)} PC Gamer feed(s). Backfilling lead images...")
    for feed_url in matched:
        try:
            print(f"Backfilling: {feed_url}")
            lead_image_service.fetch_and_store_lead_images_for_feed(feed_url, force_retry_negative=True)
        except Exception as e:
            print(f"Failed for {feed_url}: {e}")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
