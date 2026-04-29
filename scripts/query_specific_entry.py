import sqlite3

feed = "https://www.pcgamer.com/rss/"
entry = "GnkKMMZh5RxEuHixJczmhc"
conn = sqlite3.connect("lectio_meta.sqlite3")
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT image_url,fetched_at FROM entry_lead_images WHERE feed_url=? AND entry_id=?", (feed, entry)).fetchone()
if row:
    print("image_url:", row["image_url"])
    print("fetched_at:", row["fetched_at"])
else:
    print("no row")
