"""Deterministic, fully-synthetic demo content for the screenshot tooling.

No live feeds are ever fetched: every feed is generated here as a local RSS file
and the post art is an inline ``<svg>`` (rendered offline by Lectio's inline-SVG
thumbnail support), so the screenshots never depend on the network and can never
leak a private feed. Everything is seeded from fixed values, so re-running the
tool produces the same library.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

# A fixed base date so generated pubDates (and therefore the post-list order) are
# stable across runs. Far enough back that "x days ago" labels read naturally.
_BASE = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)

# Palette for the generated SVG tiles (foreground, background).
_PALETTE = [
    ("#e8eaf6", "#3949ab"), ("#e0f2f1", "#00897b"), ("#fff3e0", "#fb8c00"),
    ("#fce4ec", "#d81b60"), ("#ede7f6", "#5e35b1"), ("#e8f5e9", "#43a047"),
    ("#e3f2fd", "#1e88e5"), ("#fbe9e7", "#f4511e"),
]


def _svg_tile(label: str, idx: int) -> str:
    """A small, self-contained SVG used as the post's lead image / thumbnail."""
    fg, bg = _PALETTE[idx % len(_PALETTE)]
    initials = "".join(w[0] for w in label.split()[:2]).upper() or "L"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 100">'
        f'<rect width="160" height="100" fill="{bg}"/>'
        f'<circle cx="128" cy="22" r="44" fill="{fg}" opacity="0.18"/>'
        f'<text x="16" y="64" font-family="Georgia, serif" font-size="40" '
        f'font-weight="700" fill="{fg}">{escape(initials)}</text>'
        "</svg>"
    )


# Folder -> list of (feed title, [entry title, ...]). Entry bodies are generated.
_LIBRARY: list[tuple[str, list[tuple[str, list[str]]]]] = [
    ("Technology", [
        ("The Quiet Terminal", [
            "Why local-first software keeps winning",
            "A field guide to SQLite write contention",
            "Notes on building calm, single-user tools",
            "The case against the infinite feed",
        ]),
        ("Protocol Digest", [
            "WebSub in practice: push without the polling",
            "Designing an RSS reader that respects your attention",
            "Atom, RSS, and the long tail of the open web",
        ]),
    ]),
    ("Science", [
        ("Orbital Notes", [
            "How small telescopes still find new comets",
            "The unreasonable usefulness of tide tables",
            "A weekend with a backyard radio antenna",
        ]),
        ("The Tidepool", [
            "Why octopuses taste with their arms",
            "Mapping the slow rivers beneath the seafloor",
        ]),
    ]),
    ("Design", [
        ("Margins & Gutters", [
            "Typography for screens that are read, not scanned",
            "The forgotten craft of the table of contents",
            "Dark mode is a reading decision, not a trend",
        ]),
    ]),
    ("Podcasts", [
        ("Longform Audio", [
            "Episode 142 — Building software for one user",
            "Episode 141 — The archivists of the open web",
        ]),
    ]),
]

_PARAS = [
    "Lectio keeps everything local and single-user, so the reader stays fast and "
    "your reading history never leaves the machine.",
    "This is sample content generated for the documentation screenshots. It is "
    "deliberately synthetic — no real feed is fetched while the shots are taken.",
    "The article pane derives a lead image, threads tags through the sidebar, and "
    "keeps the read/unread state in step as you move through the list.",
]


def _entry_html(title: str, idx: int) -> str:
    body = "".join(f"<p>{html.escape(p)}</p>" for p in _PARAS)
    return _svg_tile(title, idx) + f"<h2>{html.escape(title)}</h2>" + body


def feeds():
    """Yield ``(folder, feed_title, slug, rss_xml)`` for every demo feed.

    ``slug`` is a filesystem-safe name for the generated ``.xml`` file; the caller
    turns it into the actual ``http://`` URL it serves the file from.
    """
    seq = 0
    for folder, feed_list in _LIBRARY:
        for feed_title, entry_titles in feed_list:
            slug = feed_title.lower().replace(" ", "-").replace("&", "and")
            slug = "".join(c for c in slug if c.isalnum() or c == "-")
            items = []
            for i, title in enumerate(entry_titles):
                seq += 1
                pub = _BASE - timedelta(hours=seq * 7)
                guid = f"demo-{slug}-{i}"
                content = _entry_html(title, seq)
                items.append(
                    "<item>"
                    f"<title>{escape(title)}</title>"
                    f"<link>http://demo.local/{slug}/{i}</link>"
                    f"<guid isPermaLink=\"false\">{escape(guid)}</guid>"
                    f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
                    f"<description>{escape(content)}</description>"
                    "</item>"
                )
            rss = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<rss version="2.0"><channel>'
                f"<title>{escape(feed_title)}</title>"
                "<link>http://demo.local/</link>"
                f"<description>{escape(feed_title)} — Lectio demo feed</description>"
                + "".join(items)
                + "</channel></rss>"
            )
            yield folder, feed_title, slug, rss
