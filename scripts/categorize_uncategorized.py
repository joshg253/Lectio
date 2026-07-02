"""One-time helper to categorize feeds that live in no folder (the virtual
"Uncategorized" folder) into Lectio's existing folders.

Hybrid workflow:
  1. `--propose`  applies high-precision keyword heuristics to feed titles +
     recent entry titles and writes a dry-run proposal CSV. Feeds no rule is
     confident about are written with an empty folder for manual/LLM fill-in.
  2. `--review <csv>` (optional) sends the still-blank rows to Claude, which
     picks one of the existing folders for each — or leaves it blank when the
     feed is genuinely ambiguous or dead. Writes an updated CSV.
  3. A human reviews the CSV and fixes any remaining blanks.
  4. `--apply <csv>` inserts the approved feed->folder assignments into
     folder_feeds. Rows with an empty folder are skipped.

The app caches folder structure in-process, so after --apply restart the
container (`docker compose restart`) for the sidebar to reflect the changes.

Usage:
    uv run scripts/categorize_uncategorized.py --propose \
        --data-dir data --user u_xxx --out proposal.csv
    uv run --with anthropic scripts/categorize_uncategorized.py \
        --review proposal.csv --data-dir data --user u_xxx --out reviewed.csv
    uv run scripts/categorize_uncategorized.py --apply reviewed.csv \
        --data-dir data --user u_xxx

The review pass needs Anthropic credentials — an `ANTHROPIC_API_KEY` env var or
an `ant auth login` profile.
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


def _valid_folder_names(meta: Path) -> list[str]:
    """Real, assignable folder names — excludes the virtual root and _Lectio."""
    mc = sqlite3.connect(meta)
    excluded = {"All Feeds", "_Lectio"}
    names = sorted(
        r[0]
        for r in mc.execute("SELECT name FROM folders WHERE name IS NOT NULL")
        if r[0] not in excluded
    )
    mc.close()
    return names


# Batch size for the Claude review pass — small enough to keep each response well
# under the token cap, large enough to amortize the shared folder-list prompt.
_REVIEW_CHUNK = 40
_REVIEW_MODEL = "claude-opus-4-8"


def _review_chunk(client, folders: list[str], feeds: list[dict]) -> dict[str, str]:
    """Ask Claude to file each feed into one of `folders` (or "" if unsure)."""
    listing = "\n".join(
        f"{i}. url={f['url']}\n   title={f['title']}\n   recent: {' | '.join(f['samples'])}"
        for i, f in enumerate(feeds)
    )
    prompt = (
        "You are sorting RSS/Atom feeds into an existing set of folders for a "
        "feed reader. For each feed, choose the single best-fitting folder from "
        "the allowed list below, based on its title and recent entry titles. If "
        "none clearly fits, or the feed looks dead/spammy/uncategorizable, leave "
        "the folder empty (\"\") — do not force a bad match.\n\n"
        f"Allowed folders:\n- " + "\n- ".join(folders) + "\n\n"
        f"Feeds:\n{listing}"
    )
    response = client.messages.create(
        model=_REVIEW_MODEL,
        max_tokens=8000,
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "assignments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string"},
                                    "folder": {"type": "string", "enum": folders + [""]},
                                },
                                "required": ["url", "folder"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["assignments"],
                    "additionalProperties": False,
                },
            }
        },
        messages=[{"role": "user", "content": prompt}],
    )
    import json

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return {a["url"]: a["folder"] for a in data["assignments"] if a.get("folder")}


def cmd_review(args) -> None:
    try:
        import anthropic
    except ImportError:
        raise SystemExit(
            "The review pass needs the anthropic SDK. Re-run with:\n"
            "  uv run --with anthropic scripts/categorize_uncategorized.py --review ..."
        )

    _, meta = _db_paths(Path(args.data_dir), args.user)
    folders = _valid_folder_names(meta)
    if not folders:
        raise SystemExit("No assignable folders found in the meta DB.")

    with open(args.review_csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fieldnames = rows[0].keys() if rows else []

    blanks = [r for r in rows if not (r.get("suggested_folder") or "").strip()]
    print(f"rows: {len(rows)}  blank (to review): {len(blanks)}")
    if not blanks:
        print("Nothing to review.")
        return

    client = anthropic.Anthropic()
    resolved: dict[str, str] = {}
    for start in range(0, len(blanks), _REVIEW_CHUNK):
        chunk = blanks[start : start + _REVIEW_CHUNK]
        feeds = [
            {
                "url": r.get("feed_url", ""),
                "title": r.get("title", ""),
                "samples": (r.get("samples", "") or "").split(" | "),
            }
            for r in chunk
        ]
        try:
            picks = _review_chunk(client, folders, feeds)
        except Exception as exc:  # keep going; unreviewed rows stay blank
            print(f"  ! chunk {start // _REVIEW_CHUNK} failed: {exc}")
            continue
        resolved.update(picks)
        print(f"  reviewed {start + len(chunk)}/{len(blanks)}  (+{len(picks)} filled)")

    filled = 0
    for r in rows:
        url = r.get("feed_url", "")
        if url in resolved and not (r.get("suggested_folder") or "").strip():
            r["suggested_folder"] = resolved[url]
            r["method"] = "claude"
            filled += 1

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fieldnames))
        w.writeheader()
        w.writerows(rows)
    print(f"filled {filled} of {len(blanks)} blanks -> {args.out}")
    print("Review it, fix any remaining blanks, then --apply it.")


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
    p.add_argument("--review", dest="review_csv", default=None,
                   help="fill blank rows in this proposal CSV via Claude")
    p.add_argument("--apply", dest="apply_csv", default=None)
    p.add_argument("--out", default="proposal.csv")
    args = p.parse_args()
    if args.apply_csv:
        args.csv = args.apply_csv
        cmd_apply(args)
    elif args.review_csv:
        cmd_review(args)
    else:
        cmd_propose(args)


if __name__ == "__main__":
    main()
