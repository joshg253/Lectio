"""Deterministic, fully-synthetic demo content for the screenshot tooling.

No live feeds are ever fetched: every feed is generated here as a local RSS file
and the post art is a little inline ``<svg>`` doodle (rendered offline by
Lectio's inline-SVG thumbnail support), so the screenshots never depend on the network and can never
leak a private feed. Everything is seeded from fixed values, so re-running the
tool produces the same library.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

# A fixed base date so generated pubDates (and therefore the post-list order) are
# stable across runs. Far enough back that "x days ago" labels read naturally.
_BASE = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)

# Background wash for the generated SVG tiles (light, dark accent).
_PALETTE = [
    ("#e8eaf6", "#3949ab"), ("#e0f2f1", "#00897b"), ("#fff3e0", "#fb8c00"),
    ("#fce4ec", "#d81b60"), ("#ede7f6", "#5e35b1"), ("#e8f5e9", "#43a047"),
    ("#e3f2fd", "#1e88e5"), ("#fbe9e7", "#f4511e"),
]

# Post art is a little doodle rather than a coloured block: the lead image is the
# largest element in the article pane, and a flat rectangle there made every
# screenshot look like a rendering failure. Drawn from plain shapes so each tile
# stays a self-contained inline <svg> — no network, no binary assets, no
# licensing question — and stays legible at both thumbnail and lead-image size.
_INK = "#263238"


def _duck() -> str:
    return (
        '<ellipse cx="82" cy="66" rx="34" ry="22" fill="#ffca28"/>'
        '<circle cx="112" cy="42" r="17" fill="#ffca28"/>'
        '<path d="M126 40 l18 5 -18 7z" fill="#fb8c00"/>'
        '<circle cx="117" cy="38" r="3.4" fill="' + _INK + '"/>'
        '<path d="M52 60 q14 12 30 6" stroke="#f0b400" stroke-width="3" fill="none"/>'
        '<path d="M40 84 q40 10 84 0" stroke="#90a4ae" stroke-width="3" fill="none"/>'
    )


def _dino() -> str:
    return (
        '<path d="M30 82 q6 -34 34 -36 q26 -2 32 14 l14 2 -10 10 q4 16 -14 18z"'
        ' fill="#66bb6a"/>'
        '<path d="M30 82 q-14 -6 -18 -22 q12 6 20 6z" fill="#66bb6a"/>'
        '<rect x="52" y="86" width="9" height="12" rx="3" fill="#4caf50"/>'
        '<rect x="76" y="86" width="9" height="12" rx="3" fill="#4caf50"/>'
        '<circle cx="98" cy="52" r="9" fill="#fff" stroke="' + _INK + '" stroke-width="3"/>'
        '<circle cx="78" cy="52" r="9" fill="#fff" stroke="' + _INK + '" stroke-width="3"/>'
        '<path d="M87 52 h2" stroke="' + _INK + '" stroke-width="3"/>'
        '<circle cx="98" cy="52" r="3" fill="' + _INK + '"/>'
        '<circle cx="78" cy="52" r="3" fill="' + _INK + '"/>'
    )


def _cat_on_keyboard() -> str:
    return (
        '<rect x="20" y="66" width="120" height="26" rx="5" fill="#cfd8dc"/>'
        + "".join(
            f'<rect x="{26 + i * 14}" y="72" width="10" height="7" rx="2" fill="#90a4ae"/>'
            for i in range(8)
        )
        + '<path d="M54 66 q0 -26 26 -26 q26 0 26 26z" fill="#455a64"/>'
        '<path d="M58 44 l-3 -14 14 8z" fill="#455a64"/>'
        '<path d="M102 44 l3 -14 -14 8z" fill="#455a64"/>'
        '<circle cx="70" cy="54" r="3.2" fill="#ffd54f"/>'
        '<circle cx="90" cy="54" r="3.2" fill="#ffd54f"/>'
        '<path d="M106 62 q20 -4 22 -20" stroke="#455a64" stroke-width="6"'
        ' fill="none" stroke-linecap="round"/>'
    )


def _robot() -> str:
    return (
        '<line x1="80" y1="20" x2="80" y2="34" stroke="' + _INK + '" stroke-width="3"/>'
        '<circle cx="80" cy="17" r="5" fill="#ef5350"/>'
        '<rect x="46" y="34" width="68" height="48" rx="10" fill="#b0bec5"'
        ' stroke="' + _INK + '" stroke-width="3"/>'
        '<circle cx="64" cy="54" r="7" fill="#fff"/><circle cx="66" cy="55" r="3.2" fill="' + _INK + '"/>'
        '<circle cx="96" cy="54" r="7" fill="#fff"/><circle cx="98" cy="55" r="3.2" fill="' + _INK + '"/>'
        '<rect x="62" y="68" width="36" height="7" rx="3" fill="#78909c"/>'
        '<rect x="30" y="46" width="10" height="20" rx="4" fill="#90a4ae"/>'
        '<rect x="120" y="46" width="10" height="20" rx="4" fill="#90a4ae"/>'
    )


def _octopus() -> str:
    tentacles = "".join(
        f'<path d="M{46 + i * 17} 66 q{-6 if i % 2 else 6} 16 {2 if i % 2 else -2} 26"'
        f' stroke="#ab47bc" stroke-width="7" fill="none" stroke-linecap="round"/>'
        for i in range(5)
    )
    return (
        tentacles
        + '<path d="M44 66 q0 -34 36 -34 q36 0 36 34z" fill="#ba68c8"/>'
        '<circle cx="68" cy="50" r="8" fill="#fff"/><circle cx="70" cy="51" r="3.6" fill="' + _INK + '"/>'
        '<circle cx="92" cy="50" r="8" fill="#fff"/><circle cx="94" cy="51" r="3.6" fill="' + _INK + '"/>'
        '<path d="M74 62 q6 6 12 0" stroke="' + _INK + '" stroke-width="2.6" fill="none"/>'
    )


def _satellite() -> str:
    panel = (
        '<rect x="{x}" y="38" width="34" height="24" rx="2" fill="#42a5f5"'
        ' stroke="' + _INK + '" stroke-width="2.5"/>'
        '<path d="M{x1} 38 v24 M{x2} 38 v24" stroke="' + _INK + '" stroke-width="1.6"/>'
    )
    return (
        '<path d="M56 50 h12 M92 50 h12" stroke="' + _INK + '" stroke-width="3"/>'
        + panel.format(x=22, x1=33, x2=45)
        + panel.format(x=104, x1=115, x2=127)
        + '<rect x="68" y="34" width="24" height="34" rx="4" fill="#eceff1"'
        ' stroke="' + _INK + '" stroke-width="3"/>'
        '<path d="M80 34 v-8" stroke="' + _INK + '" stroke-width="3"/>'
        '<path d="M68 22 q12 -8 24 0 q-12 8 -24 0z" fill="#cfd8dc"'
        ' stroke="' + _INK + '" stroke-width="2.5"/>'
        '<circle cx="75" cy="46" r="4.6" fill="#fff" stroke="' + _INK + '" stroke-width="1.8"/>'
        '<circle cx="76" cy="47" r="2.2" fill="' + _INK + '"/>'
        '<circle cx="86" cy="46" r="4.6" fill="#fff" stroke="' + _INK + '" stroke-width="1.8"/>'
        '<circle cx="87" cy="47" r="2.2" fill="' + _INK + '"/>'
        '<path d="M74 57 q6 5 12 0" stroke="' + _INK + '" stroke-width="2.4" fill="none"/>'
    )


def _coffee() -> str:
    return (
        '<path d="M46 44 h56 l-6 42 q-2 8 -10 8 h-24 q-8 0 -10 -8z" fill="#fff"'
        ' stroke="' + _INK + '" stroke-width="3"/>'
        '<path d="M102 52 q16 0 16 12 t-16 12" stroke="' + _INK + '" stroke-width="3" fill="none"/>'
        '<circle cx="64" cy="62" r="3.4" fill="' + _INK + '"/>'
        '<circle cx="84" cy="62" r="3.4" fill="' + _INK + '"/>'
        '<path d="M66 72 q8 7 16 0" stroke="' + _INK + '" stroke-width="2.6" fill="none"/>'
        '<path d="M62 36 q6 -8 0 -16" stroke="#b0bec5" stroke-width="3" fill="none" stroke-linecap="round"/>'
        '<path d="M80 34 q6 -9 0 -18" stroke="#b0bec5" stroke-width="3" fill="none" stroke-linecap="round"/>'
    )


def _floppy() -> str:
    return (
        '<rect x="46" y="24" width="68" height="66" rx="5" fill="#37474f"/>'
        '<rect x="60" y="24" width="40" height="26" rx="2" fill="#eceff1"/>'
        '<rect x="82" y="28" width="10" height="18" rx="2" fill="#90a4ae"/>'
        '<rect x="56" y="58" width="48" height="32" rx="3" fill="#f5f5f5"/>'
        '<circle cx="70" cy="70" r="3.2" fill="' + _INK + '"/>'
        '<circle cx="90" cy="70" r="3.2" fill="' + _INK + '"/>'
        '<path d="M72 79 q8 6 16 0" stroke="' + _INK + '" stroke-width="2.6" fill="none"/>'
    )


def _rocket() -> str:
    return (
        '<path d="M80 18 q18 22 18 44 h-36 q0 -22 18 -44z" fill="#eceff1"'
        ' stroke="' + _INK + '" stroke-width="3"/>'
        '<path d="M62 56 l-14 16 h14z" fill="#ef5350"/>'
        '<path d="M98 56 l14 16 h-14z" fill="#ef5350"/>'
        '<circle cx="80" cy="44" r="8" fill="#4fc3f7" stroke="' + _INK + '" stroke-width="2.5"/>'
        '<path d="M68 62 h24 l-4 10 h-16z" fill="#b0bec5"/>'
        '<path d="M74 74 q6 16 6 20 q0 -4 6 -20z" fill="#ffa726"/>'
        '<path d="M78 74 q2 12 2 15 q0 -3 2 -15z" fill="#ffee58"/>'
    )


def _telescope() -> str:
    return (
        # Tripod first, so the tube sits over the legs.
        '<path d="M62 58 l-14 36 M62 58 l14 36 M62 58 v34" stroke="#546e7a"'
        ' stroke-width="4" stroke-linecap="round"/>'
        '<g transform="rotate(-28 62 58)">'
        '<rect x="34" y="49" width="72" height="18" rx="6" fill="#90a4ae"'
        ' stroke="' + _INK + '" stroke-width="3"/>'
        '<rect x="100" y="44" width="14" height="28" rx="3" fill="#607d8b"'
        ' stroke="' + _INK + '" stroke-width="2.5"/>'
        '<rect x="26" y="52" width="10" height="12" rx="3" fill="#455a64"/>'
        '<path d="M52 58 h34" stroke="' + _INK + '" stroke-width="1.6" opacity="0.5"/>'
        "</g>"
        '<g fill="#ffd54f">'
        '<path d="M128 20 l3.5 8 8 3.5 -8 3.5 -3.5 8 -3.5 -8 -8 -3.5 8 -3.5z"/>'
        '<path d="M108 12 l2 4.5 4.5 2 -4.5 2 -2 4.5 -2 -4.5 -4.5 -2 4.5 -2z"/>'
        '<path d="M142 44 l2 4.5 4.5 2 -4.5 2 -2 4.5 -2 -4.5 -4.5 -2 4.5 -2z"/>'
        "</g>"
    )


def _ghost() -> str:
    return (
        '<path d="M50 88 v-34 q0 -24 30 -24 q30 0 30 24 v34 l-10 -8 -10 8 -10 -8'
        ' -10 8 -10 -8z" fill="#fafafa" stroke="' + _INK + '" stroke-width="3"/>'
        '<circle cx="69" cy="52" r="9" fill="none" stroke="' + _INK + '" stroke-width="3"/>'
        '<circle cx="91" cy="52" r="9" fill="none" stroke="' + _INK + '" stroke-width="3"/>'
        '<path d="M78 52 h4" stroke="' + _INK + '" stroke-width="3"/>'
        '<circle cx="69" cy="52" r="3.2" fill="' + _INK + '"/>'
        '<circle cx="91" cy="52" r="3.2" fill="' + _INK + '"/>'
        '<ellipse cx="80" cy="68" rx="5" ry="6" fill="' + _INK + '"/>'
    )


def _crt() -> str:
    return (
        '<rect x="36" y="26" width="88" height="58" rx="7" fill="#cfd8dc"'
        ' stroke="' + _INK + '" stroke-width="3"/>'
        '<rect x="46" y="34" width="68" height="38" rx="4" fill="#1b5e20"/>'
        '<circle cx="68" cy="50" r="4" fill="#69f0ae"/>'
        '<circle cx="92" cy="50" r="4" fill="#69f0ae"/>'
        '<path d="M68 60 q12 9 24 0" stroke="#69f0ae" stroke-width="3" fill="none"/>'
        '<rect x="70" y="84" width="20" height="8" fill="#90a4ae"/>'
        '<rect x="56" y="92" width="48" height="5" rx="2" fill="#78909c"/>'
    )


# Ordered so the two most on-the-nose pairings land on the posts that earn them
# (the octopus piece, the telescope piece); the rest just cycle.
_DOODLES = [
    _duck, _crt, _dino, _satellite, _cat_on_keyboard, _coffee, _octopus,
    _telescope, _robot, _floppy, _rocket, _ghost,
]


# Self-closing SVG tags do not survive ingest. Inside an HTML-escaped
# <description>, feedparser parses the markup as HTML, where the trailing slash
# is meaningless — so `<rect/><circle/><path/>` becomes `<rect><circle><path>`,
# every shape nested inside the rect, which cannot contain shapes. The browser
# paints the rect and ignores the rest, which is exactly why these screenshots
# used to be flat colour blocks. Emitting explicit end tags survives both the
# HTML parse and the SVG sanitizer. (A `data:` image would dodge the parser, but
# Lectio's sanitizer strips data URIs from src. The ingest bug itself is noted
# in Plan.md — it degrades the inline-SVG thumbnail feature for real feeds too.)
_SELF_CLOSING_RE = re.compile(r"<([a-zA-Z]+)([^>]*?)\s*/>")


def _with_end_tags(svg: str) -> str:
    return _SELF_CLOSING_RE.sub(r"<\1\2></\1>", svg)


def _svg_tile(label: str, idx: int) -> str:
    """A small, self-contained SVG used as the post's lead image / thumbnail."""
    wash, accent = _PALETTE[idx % len(_PALETTE)]
    doodle = _DOODLES[idx % len(_DOODLES)]
    return _with_end_tags(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 100">'
        f'<rect width="160" height="100" fill="{wash}"/>'
        f'<circle cx="26" cy="20" r="30" fill="{accent}" opacity="0.14"/>'
        f'<circle cx="140" cy="92" r="26" fill="{accent}" opacity="0.12"/>'
        f"{doodle()}"
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
