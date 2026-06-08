"""Print lead image data for a specific PC Gamer entry from the meta DB."""
import sqlite3

conn = sqlite3.connect("lectio_meta.sqlite3")
conn.row_factory = sqlite3.Row
entry = "GnkKMMZh5RxEuHixJczmhc"
row = conn.execute(
    "SELECT feed_url,entry_id,image_url,fetched_at FROM entry_lead_images WHERE feed_url=? AND entry_id=?",
    ("https://www.pcgamer.com/rss/", entry),
).fetchone()
if row:
    print("entry:", row["entry_id"])
    print("image_url:", row["image_url"])
    print("fetched_at:", row["fetched_at"])
else:
    print("no row for", entry)
