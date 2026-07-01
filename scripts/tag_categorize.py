"""Categorize uncategorized feeds using YOUR OWN tags as the primary signal.

Inoreader labels were split by case on import: Mixed Case -> Folder, lowercase ->
tag. Feeds that only arrived via lowercase tag files got tags but no folder. So
the best folder signal is the manual tags already on their entries.

Strategy:
  1. Derive a tag -> folder map from the ALREADY-FOLDERED feeds: for each manual
     tag, which folder do the feeds bearing it live in? Keep only confident tags
     (dominant folder >= THRESH share, backed by >= MIN_FEEDS feeds).
  2. For each uncategorized feed, vote its confident tags -> plurality folder.
  3. Fall back to title/sample keyword heuristics (categorize_uncategorized.RULES)
     for feeds with no confident tag signal. Leave the rest blank.

Writes the same CSV shape as categorize_uncategorized.py so `--apply` there can
consume it.

Usage:
    uv run python -m scripts.tag_categorize --data-dir data --user u_x --out proposal.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from scripts.categorize_uncategorized import RULES  # heuristic fallback

PFX = "lectio.manual_tag."
THRESH = 0.60      # top folder must own >= 60% of a tag's foldered feeds
MIN_FEEDS = 2      # ...backed by at least this many feeds
EXCLUDE_FOLDERS = {"All Feeds", "_Lectio"}


def _db_paths(data_dir: Path, user: str | None) -> tuple[Path, Path]:
    base = data_dir / "users" / user if user else data_dir
    return base / "lectio_reader.sqlite", base / "lectio_meta.sqlite3"


def _heuristic(text: str) -> str:
    low = text.lower()
    for folder, kws in RULES:
        for kw in kws:
            if kw in low:
                return folder
    return ""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--user", default=None)
    p.add_argument("--out", default="proposal.csv")
    p.add_argument("--show-map", action="store_true", help="print the derived tag->folder map")
    args = p.parse_args()

    reader, meta = _db_paths(Path(args.data_dir), args.user)
    rc = sqlite3.connect(reader)
    mc = sqlite3.connect(meta)

    fid2name = {r[0]: r[1] for r in mc.execute("SELECT id, name FROM folders")}
    feed2folders: dict[str, set[str]] = defaultdict(set)
    for fid, furl in mc.execute("SELECT folder_id, feed_url FROM folder_feeds"):
        name = fid2name.get(fid)
        if name and name not in EXCLUDE_FOLDERS:
            feed2folders[furl].add(name)
    foldered = set(feed2folders)
    all_feeds = {r[0] for r in rc.execute("SELECT url FROM feeds")}
    uncat = all_feeds - foldered

    feed_tags: dict[str, set[str]] = defaultdict(set)
    for feed, key in rc.execute(f"SELECT DISTINCT feed, key FROM entry_tags WHERE key LIKE '{PFX}%'"):
        feed_tags[feed].add(key[len(PFX):])

    # 1. tag -> folder distribution from foldered feeds
    tag_folder: dict[str, Counter] = defaultdict(Counter)
    for f in foldered:
        for t in feed_tags.get(f, ()):
            for fold in feed2folders[f]:
                tag_folder[t][fold] += 1
    tag_map: dict[str, str] = {}
    for t, c in tag_folder.items():
        top, n = c.most_common(1)[0]
        total = sum(c.values())
        if n >= MIN_FEEDS and n / total >= THRESH:
            tag_map[t] = top

    if args.show_map:
        print("tag -> folder (confident):")
        for t in sorted(tag_map):
            c = tag_folder[t]
            top, n = c.most_common(1)[0]
            other = ", ".join(f"{k}:{v}" for k, v in c.most_common()[1:4])
            print(f"  {t:22} -> {tag_map[t]:24} ({n}/{sum(c.values())})" + (f"   [{other}]" if other else ""))
        print()

    titles = {r[0]: (r[1] or r[0]) for r in rc.execute("SELECT url, title FROM feeds")}
    samples: dict[str, list[str]] = defaultdict(list)
    for feed, title in rc.execute(
        "SELECT feed, title FROM entries WHERE title IS NOT NULL AND title!='' ORDER BY COALESCE(published,updated) DESC"
    ):
        if feed in uncat and len(samples[feed]) < 5:
            samples[feed].append(title)

    rows = []
    m = Counter()
    for f in sorted(uncat):
        votes = Counter()
        for t in feed_tags.get(f, ()):
            if t in tag_map:
                votes[tag_map[t]] += 1
        folder, method = "", ""
        if votes:
            folder, method = votes.most_common(1)[0][0], "tag"
        else:
            folder = _heuristic(f + " || " + titles.get(f, "") + " || " + " || ".join(samples.get(f, [])))
            method = "heuristic" if folder else ""
        m[method or "blank"] += 1
        rows.append({
            "feed_url": f, "title": titles.get(f, f), "suggested_folder": folder,
            "method": method,
            "tags": ",".join(sorted(feed_tags.get(f, ()))),
            "samples": " | ".join(samples.get(f, [])),
        })

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["feed_url", "title", "suggested_folder", "method", "tags", "samples"])
        w.writeheader()
        w.writerows(rows)

    by_folder = Counter(r["suggested_folder"] for r in rows if r["suggested_folder"])
    print(f"uncategorized={len(uncat)}  by-tag={m['tag']}  by-heuristic={m['heuristic']}  blank={m['blank']}")
    for fold, n in by_folder.most_common():
        print(f"  {n:4} {fold}")
    print(f"\nProposal -> {args.out}. Review, then apply via categorize_uncategorized.py --apply.")


if __name__ == "__main__":
    main()
