"""Dump all entry_lead_images rows from the meta DB for inspection."""
import sqlite3

conn = sqlite3.connect("lectio_meta.sqlite3")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT feed_url,entry_id,image_url,fetched_at FROM entry_lead_images").fetchall()
print(f"Total lead image rows: {len(rows)}")
for r in rows[:20]:
    print(r["feed_url"], r["entry_id"], r["image_url"])
