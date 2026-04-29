import sqlite3
import time

DB = "lectio_meta.sqlite3"
FEED = "https://www.pcgamer.com/rss/"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
try:
    total = conn.execute("SELECT COUNT(*) as c FROM entry_lead_images WHERE feed_url=?", (FEED,)).fetchone()["c"]
except Exception as e:
    print("ERROR querying DB:", e)
    raise
print("pcgamer backfilled rows:", total)
print("\nMost recent 10 backfilled entries:")
rows = conn.execute(
    "SELECT entry_id,image_url,fetched_at FROM entry_lead_images WHERE feed_url=? ORDER BY fetched_at DESC LIMIT 10", (FEED,)
).fetchall()
for r in rows:
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["fetched_at"])) if r["fetched_at"] else "0"
    print(r["entry_id"], "->", r["image_url"], "fetched_at", t)

# show count of total feed entries from reader DB if possible
try:
    import sqlite3 as sql2

    rconn = sql2.connect("lectio_reader.sqlite")
    rconn.row_factory = sql2.Row
    feed_count = rconn.execute("SELECT COUNT(*) as c FROM entries WHERE feed_url=?", (FEED,)).fetchone()["c"]
    print("\nTotal entries in reader DB for feed:", feed_count)
except Exception:
    pass
