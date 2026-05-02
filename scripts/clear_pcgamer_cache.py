"""One-shot: clear PCGamer lead image cache entries from the last 2 days."""

import sqlite3
import time

DB = "lectio_meta.sqlite3"
FEED_URL = "https://www.pcgamer.com/rss/"
CUTOFF = time.time() - 2 * 24 * 3600

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT COUNT(*) FROM entry_lead_images WHERE feed_url = ? AND fetched_at > ?",
    (FEED_URL, CUTOFF),
).fetchone()
print(f"Deleting {rows[0]} cached entries for PCGamer (last 2 days)...")
conn.execute(
    "DELETE FROM entry_lead_images WHERE feed_url = ? AND fetched_at > ?",
    (FEED_URL, CUTOFF),
)
conn.commit()
conn.close()
print("Done. Restart the app (or it will re-cache on next page load).")
