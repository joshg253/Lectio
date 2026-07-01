"""Find and merge duplicate feeds that collapse onto the same *canonical* URL.

Lectio "upgrades" some feed URLs (old.reddit->www.reddit, username.artstation
->www.artstation, tapastic->tapas, ?alt=rss, trailing slash). Feeds imported
from other readers came in with the *raw* URL, so an imported feed and an
existing "good" (upgraded, usually foldered) feed can be the same source under
two URLs. Entry-overlap misses these (reddit/artstation feeds are time-windowed,
so the snapshots share no current entries), but `canonical_feed_url` collapses
them.

`--find`  (READ-ONLY): group every reader feed by canonical URL; any group with
          >1 member is a duplicate set. Picks the canonical/foldered member as
          survivor and writes a dry-run pair list with curation + folder info.
`--merge` (WRITES, run in the app container with --user): for each pair, migrate
          the dupe's tags + stars onto the matching entry in the survivor
          (matching by GUID, else canonical link, else synthesizing the entry so
          nothing is lost), copy the dupe's folder memberships to the survivor,
          then unsubscribe the dupe.

Usage:
    uv run scripts/reconcile_duplicate_feeds.py --find  --data-dir data --user u_x --out dupes.csv
    # in container, after review:
    uv run scripts/reconcile_duplicate_feeds.py --merge dupes.csv --data-dir data --user u_x
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

import main  # for canonical_feed_url (and, for --merge, the reader/tenancy helpers)


def _db_paths(data_dir: Path, user: str | None) -> tuple[Path, Path]:
    base = data_dir / "users" / user if user else data_dir
    return base / "lectio_reader.sqlite", base / "lectio_meta.sqlite3"


def _norm_link(l):
    return l.split("#")[0].rstrip("/") if l else None


def _duplicate_pairs(reader_db: Path, meta_db: Path) -> list[dict]:
    """Return [{dupe, survivor, dupe_foldered, survivor_foldered, tags, stars}]."""
    rc = sqlite3.connect(reader_db)
    mc = sqlite3.connect(meta_db)
    foldered = {r[0] for r in mc.execute("SELECT DISTINCT feed_url FROM folder_feeds")}
    feeds = [r[0] for r in rc.execute("SELECT url FROM feeds")]

    tag_by_feed: dict[str, int] = defaultdict(int)
    for (f,) in rc.execute(
        "SELECT feed FROM entry_tags WHERE key LIKE 'lectio.manual_tag.%'"
    ):
        tag_by_feed[f] += 1
    star_by_feed: dict[str, int] = defaultdict(int)
    for (f,) in mc.execute("SELECT feed_url FROM saved_entries"):
        star_by_feed[f] += 1

    entry_count: dict[str, int] = {
        r[0]: r[1] for r in rc.execute("SELECT feed, COUNT(*) FROM entries GROUP BY feed")
    }

    groups: dict[str, list[str]] = defaultdict(list)
    for u in feeds:
        groups[main.canonical_feed_url(u)].append(u)

    pairs = []
    for canon, members in groups.items():
        if len(members) < 2:
            continue
        # Survivor = the feed that actually holds the content (most entries), so
        # curation migrates onto existing items instead of synthesizing them.
        # Tie-break toward a foldered member, then the canonical ("upgraded") URL.
        def rank(m: str) -> tuple:
            return (entry_count.get(m, 0), m in foldered, m == canon)
        survivor = max(members, key=rank)
        for m in members:
            if m == survivor:
                continue
            pairs.append({
                "dupe": m,
                "survivor": survivor,
                "dupe_foldered": int(m in foldered),
                "survivor_foldered": int(survivor in foldered),
                "tags": tag_by_feed.get(m, 0),
                "stars": star_by_feed.get(m, 0),
            })
    rc.close()
    mc.close()
    # Curation-bearing dupes first, then by name.
    pairs.sort(key=lambda p: (-(p["tags"] + p["stars"]), p["dupe"]))
    return pairs


def cmd_find(args) -> None:
    reader, meta = _db_paths(Path(args.data_dir), args.user)
    pairs = _duplicate_pairs(reader, meta)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["dupe", "survivor", "dupe_foldered",
                                           "survivor_foldered", "tags", "stars"])
        w.writeheader()
        w.writerows(pairs)
    with_cur = [p for p in pairs if p["tags"] or p["stars"]]
    print(f"duplicate feeds (canonical collision): {len(pairs)}")
    print(f"  with curation to migrate: {len(with_cur)}  "
          f"({sum(p['tags'] for p in pairs)} tags, {sum(p['stars'] for p in pairs)} stars)")
    print(f"  pure drops (no curation):  {len(pairs) - len(with_cur)}")
    print(f"\ncuration-bearing pairs [dupe(fold?) -> survivor(fold?)  tags/stars]:")
    for p in with_cur:
        print(f"  {'F' if p['dupe_foldered'] else 'u'} {p['dupe'][:44]:44} -> "
              f"{'F' if p['survivor_foldered'] else 'u'} {p['survivor'][:44]:44}  "
              f"{p['tags']}t/{p['stars']}s")
    print(f"\nPair list written to {args.out}. Review, then --merge (in the container).")


def _resolve(dupe_id, s_guids, s_link2id, dupe_link2id):
    """Return (target_survivor_entry_id, kind) where kind is guid|link|synth.

    For a synth, the survivor doesn't have the entry yet, so the target id is the
    dupe's id (the entry will be added under the survivor feed with that id).
    """
    if dupe_id in s_guids:
        return dupe_id, "guid"
    n = dupe_link2id.get(dupe_id)
    if n and n in s_link2id:
        return s_link2id[n], "link"
    return dupe_id, "synth"


def cmd_merge(args) -> None:
    from services import tenancy
    if args.user:
        tenancy.set_current_user(args.user)
    reader_db = str(tenancy.reader_db_path())
    dry = args.dry_run

    with open(args.merge_csv, newline="", encoding="utf-8") as fh:
        pairs = [r for r in csv.DictReader(fh)
                 if r["dupe"].strip() and r["survivor"].strip()
                 and r["dupe"].strip() != r["survivor"].strip()]

    c = defaultdict(int)  # counters
    for row in pairs:
        dupe = row["dupe"].strip()
        surv = row["survivor"].strip()

        # ---- Resolve (read-only) --------------------------------------------
        with sqlite3.connect(reader_db, timeout=30.0) as rconn:
            rconn.row_factory = sqlite3.Row
            s_guids = {r["id"] for r in rconn.execute("SELECT id FROM entries WHERE feed=?", (surv,))}
            s_link2id: dict[str, str] = {}
            for r in rconn.execute("SELECT id, link FROM entries WHERE feed=?", (surv,)):
                n = _norm_link(r["link"])
                if n:
                    s_link2id.setdefault(n, r["id"])
            dupe_link2id = {r["id"]: _norm_link(r["link"])
                            for r in rconn.execute("SELECT id, link FROM entries WHERE feed=?", (dupe,))}
            tag_rows = rconn.execute(
                "SELECT id, key FROM entry_tags WHERE feed=? AND key LIKE 'lectio.manual_tag.%'",
                (dupe,)).fetchall()
        with main.get_meta_connection() as mc:
            star_rows = mc.execute(
                "SELECT entry_id, saved_at FROM saved_entries WHERE feed_url=?", (dupe,)).fetchall()

        plan_tags = []   # (target_id, key)
        plan_stars = []  # (target_id, saved_at)
        synth_ids: set[str] = set()
        for r in tag_rows:
            tid, kind = _resolve(r["id"], s_guids, s_link2id, dupe_link2id)
            plan_tags.append((tid, r["key"], kind))
            c[f"tags_{kind}"] += 1
            if kind == "synth":
                synth_ids.add(r["id"])
        for r in star_rows:
            tid, kind = _resolve(r["entry_id"], s_guids, s_link2id, dupe_link2id)
            plan_stars.append((tid, r["saved_at"], kind))
            c[f"stars_{kind}"] += 1
            if kind == "synth":
                synth_ids.add(r["entry_id"])
        c["feeds"] += 1
        c["synth_entries"] += len(synth_ids)

        if dry:
            continue

        # ---- Apply ----------------------------------------------------------
        # 1. Synthesize missing entries into the survivor (reader lib), tracking
        #    which succeeded so we never write a tag/star for a missing entry.
        synth_ok: set[str] = set()
        if synth_ids:
            with main.get_reader() as reader:
                for sid in synth_ids:
                    e = reader.get_entry((dupe, sid), None)
                    ed: dict = {"feed_url": surv, "id": sid,
                                "title": (e.title if e else "") or "",
                                "link": (e.link if e and e.link else sid)}
                    if e and e.published:
                        ed["published"] = e.published
                    if e and e.content:
                        ed["content"] = [{"value": e.content[0].value}]
                    elif e and e.summary:
                        ed["summary"] = e.summary
                    try:
                        reader.add_entry(ed)
                        synth_ok.add(sid)
                    except Exception:
                        pass
        # 2. Tags (reader DB). 3. Stars + folders (meta DB). Fresh connections,
        #    after the reader context is closed, to avoid writer contention.
        with sqlite3.connect(reader_db, timeout=30.0) as wconn:
            for tid, key, kind in plan_tags:
                if kind == "synth" and tid not in synth_ok:
                    continue
                wconn.execute("INSERT OR IGNORE INTO entry_tags (feed,id,key,value) VALUES (?,?,?, 'null')",
                              (surv, tid, key))
                c["tags_applied"] += 1
        with main.get_meta_connection() as mc:
            for tid, saved_at, kind in plan_stars:
                if kind == "synth" and tid not in synth_ok:
                    continue
                mc.execute("INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?,?,?)",
                           (surv, tid, saved_at))
                c["stars_applied"] += 1
            for (fid,) in mc.execute("SELECT folder_id FROM folder_feeds WHERE feed_url=?", (dupe,)):
                mc.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?,?)", (fid, surv))
            mc.execute("DELETE FROM saved_entries WHERE feed_url=?", (dupe,))
            mc.commit()
        # 4. Unsubscribe the dupe: strip ALL its folder memberships (the survivor
        #    already inherited them above), then purge it from the reader. Removing
        #    only the root folder wouldn't drop a dupe that lives in a real folder.
        with main.get_meta_connection() as mc:
            mc.execute("DELETE FROM folder_feeds WHERE feed_url=?", (dupe,))
            mc.commit()
        with main.get_reader() as reader:
            if reader.get_feed(dupe, None) is not None:
                with main.get_meta_connection() as mc:
                    main.purge_orphaned_feed(reader, mc, dupe, archive_pending=True)
        main.invalidate_meta_structure_cache()
        c["dropped"] += 1

    print(("DRY-RUN " if dry else "") + f"pairs={c['feeds']} synth_entries={c['synth_entries']}")
    print(f"  tags:  guid={c['tags_guid']} link={c['tags_link']} synth={c['tags_synth']}"
          + ("" if dry else f"  applied={c['tags_applied']}"))
    print(f"  stars: guid={c['stars_guid']} link={c['stars_link']} synth={c['stars_synth']}"
          + ("" if dry else f"  applied={c['stars_applied']}"))
    if not dry:
        print(f"  feeds dropped={c['dropped']}")
        print("Restart the app for the sidebar to refresh: docker compose restart")


def main_cli() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--user", default=None)
    p.add_argument("--find", action="store_true")
    p.add_argument("--merge", dest="merge_csv", default=None)
    p.add_argument("--dry-run", action="store_true", help="with --merge: resolve + count, write nothing")
    p.add_argument("--out", default="dupes.csv")
    args = p.parse_args()
    if args.merge_csv:
        cmd_merge(args)
    else:
        cmd_find(args)


if __name__ == "__main__":
    main_cli()
