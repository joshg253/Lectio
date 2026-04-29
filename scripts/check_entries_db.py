import sqlite3


def check(entries, feed="https://www.pcgamer.com/rss/"):
    conn = sqlite3.connect("lectio_meta.sqlite3")
    conn.row_factory = sqlite3.Row
    for entry in entries:
        row = conn.execute("SELECT image_url,fetched_at FROM entry_lead_images WHERE feed_url=? AND entry_id=?", (feed, entry)).fetchone()
        print(entry, "->", row["image_url"] if row else "NO ROW")


if __name__ == "__main__":
    check(["QdRmYjXZri5eE3zX5QL3eD", "mYe7aVXmmVpE54iQ6TsYSb", "GnkKMMZh5RxEuHixJczmhc"])
