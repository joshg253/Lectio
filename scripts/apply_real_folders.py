"""Categorize using the REAL Inoreader folders + titles from an InoreaderExportTool
backup.zip (Google Reader format), replacing the earlier capitalized-tag-folder
scaffolding.

Each item in backup.zip carries origin.streamId (feed URL), origin.title (real
feed name), and categories[] with user/<id>/label/NAME — Mixed Case = folder,
lowercase = tag. So we recover the true folder and title for feeds imported via
tag streams.

Phases (all guarded by --dry-run):
  A. Undo the tag-folders created by tag_to_folders.py (folders present now but
     absent in the pre-tagfolders meta backup) + their memberships.
  B. Assign each feed to its real folder(s) from backup.zip (case-insensitive
     match to an existing Lectio folder; created if genuinely absent).
  C. Fallback: feeds still uncategorized that DO have manual tags get the
     capitalized-tag-folder treatment (first letter upper), reusing an existing
     folder case-insensitively.
  D. Titles: for feeds with no effective title, set user_title to backup.zip's
     origin.title if known, else the URL with its scheme stripped.

Usage:
    uv run python -m scripts.apply_real_folders --data-dir data --user u_x \
        --backup tmp/InoreaderTools/backup.zip \
        --pre-meta data/backups/pre-tagfolders/lectio_meta.sqlite3 [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import main

PFX = "lectio.manual_tag."
ROOT_NAME = "All Feeds"
LABEL = re.compile(r"user/\d+/label/(.+)")


def _db_paths(data_dir: Path, user: str | None):
    base = data_dir / "users" / user if user else data_dir
    return base / "lectio_reader.sqlite", base / "lectio_meta.sqlite3"


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:]


def _strip_scheme(url: str) -> str:
    return re.sub(r"^https?://", "", url).rstrip("/")


def _load_backup(path: Path):
    z = zipfile.ZipFile(path)
    names = [n for n in z.namelist() if n.endswith(".json") and not re.search(r"_20\d\d-", n)]
    feed_folders: dict[str, set[str]] = defaultdict(set)
    feed_title: dict[str, str] = {}
    for n in names:
        try:
            items = json.loads(z.read(n))
        except Exception:
            continue
        if isinstance(items, dict):
            items = items.get("items", [])
        for it in items:
            o = it.get("origin") or {}
            sid = o.get("streamId", "")
            if not sid.startswith("feed/"):
                continue
            furl = sid[5:]
            if o.get("title"):
                feed_title.setdefault(furl, o["title"])
            for c in it.get("categories", []):
                m = LABEL.match(c)
                if m and m.group(1) != m.group(1).lower():
                    feed_folders[furl].add(m.group(1))
    # collapse to canonical URL
    canon_folders: dict[str, set[str]] = defaultdict(set)
    canon_title: dict[str, str] = {}
    for furl, fols in feed_folders.items():
        canon_folders[main.canonical_feed_url(furl)] |= fols
    for furl, t in feed_title.items():
        canon_title.setdefault(main.canonical_feed_url(furl), t)
    return canon_folders, canon_title


def main_cli() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--user", default=None)
    p.add_argument("--backup", required=True)
    p.add_argument("--pre-meta", required=True, help="meta DB snapshot from before tag-folders")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    dry = args.dry_run

    reader_db, meta_db = _db_paths(Path(args.data_dir), args.user)
    canon_folders, canon_title = _load_backup(Path(args.backup))

    rc = sqlite3.connect(reader_db)
    mc = sqlite3.connect(meta_db)
    root_id = mc.execute("SELECT id FROM folders WHERE name=? AND parent_id IS NULL", (ROOT_NAME,)).fetchone()[0]

    # ---- Phase A: undo tag-folders (present now, absent in pre-meta) ----
    pre = sqlite3.connect(args.pre_meta)
    pre_names = {r[0] for r in pre.execute("SELECT name FROM folders WHERE parent_id IS NOT NULL")}
    pre.close()
    cur_children = {r[1]: r[0] for r in mc.execute("SELECT id, name FROM folders WHERE parent_id IS NOT NULL")}
    created_names = {n for n in cur_children if n not in pre_names}
    undo_ids = [cur_children[n] for n in created_names]
    undo_memberships = 0
    for fid in undo_ids:
        undo_memberships += mc.execute("SELECT COUNT(*) FROM folder_feeds WHERE folder_id=?", (fid,)).fetchone()[0]
        if not dry:
            mc.execute("DELETE FROM folder_feeds WHERE folder_id=?", (fid,))
            mc.execute("DELETE FROM folders WHERE id=?", (fid,))
    if not dry:
        mc.commit()

    # folder name (ci) -> id, after undo
    name_ci = {r[1].lower(): r[0] for r in mc.execute("SELECT id, name FROM folders")}

    def folder_id(name: str):
        fid = name_ci.get(name.lower())
        if fid:
            return fid, False
        if dry:
            return None, True
        cur = mc.execute("INSERT INTO folders (name, parent_id) VALUES (?, ?)", (name, root_id))
        name_ci[name.lower()] = cur.lastrowid
        return cur.lastrowid, True

    all_feeds = [r[0] for r in rc.execute("SELECT url FROM feeds")]

    # ---- Phase B: real folders from backup.zip ----
    b_members = 0
    b_created = set()
    for f in all_feeds:
        fols = canon_folders.get(main.canonical_feed_url(f))
        if not fols:
            continue
        for name in fols:
            fid, isnew = folder_id(name)
            if isnew:
                b_created.add(name)
            if fid and not dry:
                mc.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?,?)", (fid, f))
            b_members += 1
    if not dry:
        mc.commit()

    # ---- Phase C: capitalized-tag fallback for still-uncategorized tagged feeds ----
    foldered = {r[0] for r in mc.execute("SELECT DISTINCT feed_url FROM folder_feeds")}
    uncat = set(all_feeds) - foldered
    feed_tags: dict[str, set[str]] = defaultdict(set)
    for feed, key in rc.execute(f"SELECT DISTINCT feed, key FROM entry_tags WHERE key LIKE '{PFX}%'"):
        if feed in uncat:
            feed_tags[feed].add(key[len(PFX):])
    c_members = 0
    c_folders = set()
    for f, ts in feed_tags.items():
        for t in ts:
            name = _cap(t)
            fid, isnew = folder_id(name)
            c_folders.add(name)
            if fid and not dry:
                mc.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?,?)", (fid, f))
            c_members += 1
    if not dry:
        mc.commit()

    # ---- Phase D: titles ----
    empty = [r[0] for r in rc.execute(
        "SELECT url FROM feeds WHERE COALESCE(NULLIF(user_title,''),NULLIF(title,'')) IS NULL")]
    t_real = t_strip = 0
    for f in empty:
        title = canon_title.get(main.canonical_feed_url(f))
        if title:
            t_real += 1
        else:
            title = _strip_scheme(f)
            t_strip += 1
        if not dry:
            # Set the feed's own title (not user_title) so a later successful
            # refresh can still overwrite a stripped placeholder with the real
            # title for any live feed; dead feeds keep this value.
            rc.execute("UPDATE feeds SET title=? WHERE url=?", (title, f))
    if not dry:
        rc.commit()

    tag = "DRY-RUN " if dry else ""
    print(f"{tag}A. undo tag-folders: {len(undo_ids)} folders, {undo_memberships} memberships removed")
    print(f"{tag}B. real folders: {b_members} memberships"
          + (f"  (folders to create: {sorted(b_created)})" if b_created else "  (all matched existing)"))
    print(f"{tag}C. tag fallback: {c_members} memberships across {len(c_folders)} capitalized folders")
    print(f"{tag}D. titles: {t_real} from backup.zip + {t_strip} stripped = {len(empty)} total")
    foldered2 = {r[0] for r in mc.execute("SELECT DISTINCT feed_url FROM folder_feeds")} if not dry else foldered
    if not dry:
        print(f"\nuncategorized remaining: {len(set(all_feeds) - foldered2)}")
        print("Restart the app: docker compose restart")


if __name__ == "__main__":
    main_cli()
