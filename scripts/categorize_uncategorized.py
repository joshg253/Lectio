"""One-time helper to categorize feeds that live in no folder (the virtual
"Uncategorized" folder) into Lectio's existing folders.

Hybrid workflow:
  1. `--propose`  applies high-precision keyword heuristics to feed titles +
     recent entry titles and writes a dry-run proposal CSV. Feeds no rule is
     confident about are written with an empty folder for manual/LLM fill-in.
  2. A human (or Claude) reviews the CSV and fills the blanks.
  3. `--apply <csv>` inserts the approved feed->folder assignments into
     folder_feeds. Rows with an empty folder are skipped.

The app caches folder structure in-process, so after --apply restart the
container (`docker compose restart`) for the sidebar to reflect the changes.

Usage:
    uv run scripts/categorize_uncategorized.py --propose \
        --data-dir data --user u_xxx --out proposal.csv
    uv run scripts/categorize_uncategorized.py --apply proposal.csv \
        --data-dir data --user u_xxx
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

# High-precision keyword rules, most specific first. A rule matches if any of its
# keywords appears (case-insensitively) in the feed title or a recent entry
# title. Kept conservative on purpose: better to leave a feed blank for review
# than to file it wrong. Folder names must match rows in the meta `folders` table.
RULES: list[tuple[str, list[str]]] = [
    ("Command Shells", ["powershell", "commandlinefu", "command line", "bash script",
                         "shell script", "learn bash", "wildcards in bash", " bash ",
                         "zsh", "cmd.exe", "sysadmin", "tmux", "terminal emulator",
                         "console fonts", "shell history"]),
    ("Linux", ["linux", "ubuntu", "debian", "arch linux", "fedora", "systemd",
               "gnome", " kde ", "kernel", "raspberry pi", "tilix", "lxterminal",
               "archbang", "puppy linux", "jwm menu", "open as root"]),
    ("Guitar, Bass, Drums, Piano", ["guitar", "bass guitar", "bass tab", "double bass",
                                    "drums", "drumeo", "drum groove", "piano",
                                    "music theory", "fretboard", "relative keys for bass"]),
    ("Music", ["beatmaking", "propellerheads", "ableton", "reason 7", "synthesizer",
               "music production"]),
    ("Photo", ["photography", "photographer", "lightroom", "mirrorless", "dslr",
               "camera review", "photoshop", "gimp", "digital painting",
               "illustrator brushes", "duotone", "depth of field effect"]),
    ("Tabletop", ["tabletop", "board game", "boardgame", "dungeons", "d&d", "paizo",
                  "warhammer", "miniatures", " rpg "]),
    ("Video Games", ["video game", "gaming", "playstation", "xbox", "nintendo",
                     "steam deck", "speedrun", "godot", "unity3d", "unity ",
                     "game jam", "indie game", "gamedev", "game dev", "minecraft",
                     "morrowind", "pc gamer", "pcgamer", "rock paper shotgun",
                     "abandonware", "dos games", "keeperrl", "gameskinny",
                     "game environment", "baldursgate", "larian"]),
    ("Comics & Art", ["webcomic", "comic", "manga", "illustration", "art tip",
                      "coloring page", "how you draw"]),
    ("Web Browsers", ["firefox", "chromium", "web browser", "vivaldi", "safari browser"]),
    ("Android", ["android", "google pixel"]),
    ("VPS", ["homelab", "proxmox", "self-host", "self host", "virtual private server", "vps"]),
    ("Booze", ["whisky", "whiskey", "cocktail", "craft beer", "brewing", "sour beer",
               "fermentation", "brewery", "drinkers guide", "wine tasting"]),
    ("Health + Fitness", ["workout", "nutrition", "marathon", "strength training",
                          "fitbit", "healthy habits", "good posture"]),
    ("Supernote", ["supernote"]),
    ("Fediverse", ["mastodon", "fediverse", "activitypub"]),
    ("Tech News", ["arstechnica", "betanews", "makeuseof", "lifehacker", "ghacks",
                   "howtogeek", "how-to geek", "techdirt", "engadget", "gizmodo",
                   "the verge", "tecmint"]),
    ("Deals", ["coupon", "discount code", "deal of the day", "slickdeals"]),
    ("Books & Education", ["betterexplained", "datagenetics", "mindyourdecisions",
                           "learn trig", "trigonometry", "learn math",
                           "grammar cheat sheet", "coursera", "edx", "freecodecamp",
                           "khan academy", "learn anything", "unit circle",
                           "regression method"]),
    ("Dev", ["python", "javascript", "typescript", "c++", "c#", " c programming",
             "learn to program", "golang", "rust lang", "programming", "leetcode",
             "algorithm", "devops", "docker", "kubernetes", "git ", "github",
             "gitignore", "commits", "compiler", "framework", "webdev",
             "azure", "msdn", "technet", "django", "matplotlib", "pandas",
             "postgresql", "mysql", "hibernate", "spring boot", "regular expression",
             "regex", "cmake", "visual studio", "clean code", "design pattern",
             "concurrency", "unit test", "pvs-studio", "webgl", "hackerrank",
             "stack overflow", "data structure", "encapsulation", "inheritance"]),
    ("Deals", ["coupon", "discount code", "deal of the day"]),
    ("Productivity", ["obsidian", "note-taking", "getting things done", "gtd",
                      "trello", "to do vo do", "1:1 meeting", "focus at work",
                      "morning routine", "increase focus", "productivity"]),
]


def _db_paths(data_dir: Path, user: str | None) -> tuple[Path, Path]:
    base = data_dir / "users" / user if user else data_dir
    return base / "lectio_reader.sqlite", base / "lectio_meta.sqlite3"


def _classify(text: str) -> tuple[str, str]:
    low = text.lower()
    for folder, keywords in RULES:
        for kw in keywords:
            if kw in low:
                return folder, kw
    return "", ""


def _load_uncategorized(reader: Path, meta: Path) -> list[dict]:
    rc = sqlite3.connect(reader)
    rc.row_factory = sqlite3.Row
    mc = sqlite3.connect(meta)
    foldered = {r[0] for r in mc.execute("SELECT DISTINCT feed_url FROM folder_feeds")}
    feeds = []
    for r in rc.execute("SELECT url, COALESCE(NULLIF(title,''),url) AS title FROM feeds"):
        if r["url"] in foldered:
            continue
        samples = [
            x[0] for x in rc.execute(
                "SELECT title FROM entries WHERE feed=? AND title IS NOT NULL AND title!=''"
                " ORDER BY COALESCE(published,updated) DESC LIMIT 5",
                (r["url"],),
            )
        ]
        feeds.append({"url": r["url"], "title": r["title"], "samples": samples})
    rc.close()
    mc.close()
    return feeds


def cmd_propose(args) -> None:
    reader, meta = _db_paths(Path(args.data_dir), args.user)
    feeds = _load_uncategorized(reader, meta)
    matched = 0
    per_folder: dict[str, int] = {}
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["feed_url", "title", "suggested_folder", "method", "matched_keyword", "samples"])
        for f in feeds:
            folder, kw = _classify(f["url"] + " || " + f["title"] + " || " + " || ".join(f["samples"]))
            if folder:
                matched += 1
                per_folder[folder] = per_folder.get(folder, 0) + 1
            w.writerow([f["url"], f["title"], folder, "heuristic" if folder else "",
                        kw, " | ".join(f["samples"])])
    print(f"uncategorized feeds: {len(feeds)}")
    print(f"heuristic matches:  {matched}  (blank / needs review: {len(feeds) - matched})")
    for folder in sorted(per_folder, key=lambda k: -per_folder[k]):
        print(f"  {per_folder[folder]:4}  {folder}")
    print(f"\nProposal written to {args.out} — review, fill blanks, then --apply it.")


def cmd_apply(args) -> None:
    reader, meta = _db_paths(Path(args.data_dir), args.user)
    mc = sqlite3.connect(meta)
    name_to_id = {r[1]: r[0] for r in mc.execute("SELECT id, name FROM folders")}
    applied = skipped = unknown = 0
    with open(args.csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            folder = (row.get("suggested_folder") or "").strip()
            url = (row.get("feed_url") or "").strip()
            if not folder or not url:
                skipped += 1
                continue
            fid = name_to_id.get(folder)
            if fid is None:
                print(f"  ! unknown folder '{folder}' for {url}")
                unknown += 1
                continue
            mc.execute(
                "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                (fid, url),
            )
            applied += 1
    mc.commit()
    mc.close()
    print(f"applied: {applied}  skipped(blank): {skipped}  unknown-folder: {unknown}")
    print("Restart the app for the sidebar to refresh: docker compose restart")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--user", default=None, help="user id (multi-user); omit for single-user")
    sub = p.add_subparsers(dest="cmd", required=False)
    p.add_argument("--propose", action="store_true")
    p.add_argument("--apply", dest="apply_csv", default=None)
    p.add_argument("--out", default="proposal.csv")
    args = p.parse_args()
    if args.apply_csv:
        args.csv = args.apply_csv
        cmd_apply(args)
    else:
        cmd_propose(args)


if __name__ == "__main__":
    main()
