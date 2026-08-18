"""A renamed feed must show its new name everywhere, not just in the sidebar.

`reader` keeps three fields: `title` (what the feed calls itself), `user_title` (the rename), and
`resolved_title` (user_title or title). Display paths that read `title` directly ignore the rename, which
is what made renames look like they did not stick — the sidebar updated and every post kept the old name.
"""

from __future__ import annotations

import re
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "main.py"
SERVICES = Path(__file__).resolve().parents[2] / "services"


def test_entry_feed_title_maps_use_resolved_title():
    source = MAIN.read_text()

    bad = re.findall(r"\{f\.url: \(f\.title\b.*?\}", source) + re.findall(r"\{str\(f\.url\): \(f\.title\b.*?\}", source)
    assert not bad, f"feed_title_map must prefer resolved_title so renames show on posts: {bad}"

    bad_cache = re.findall(r'feed_title_cache\[fu\] = str\(getattr\(f, "title"', source)
    assert not bad_cache, "feed_title_cache must prefer resolved_title so renames show on posts"


def test_sync_apis_agree_on_the_display_name():
    """fever and greader already honored user_title; miniflux did not, so the same feed had two names."""
    for name in ("fever.py", "greader.py", "miniflux.py"):
        source = (SERVICES / name).read_text()
        for line in source.splitlines():
            if '"title":' in line and "f.title" in line or '"title":' in line and "feed.title" in line:
                assert "user_title" in line or "resolved_title" in line, f"{name}: {line.strip()}"
