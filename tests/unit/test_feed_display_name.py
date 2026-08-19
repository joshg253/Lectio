"""A renamed feed must show its new name everywhere, not just in the sidebar.

`reader` keeps three fields: `title` (what the feed calls itself), `user_title` (the rename), and
`resolved_title` (user_title or title). Display paths that read `title` directly ignore the rename, which
is what made renames look like they did not stick — the sidebar updated and every post kept the old name.
"""

from __future__ import annotations

from pathlib import Path

import main

SERVICES = Path(__file__).resolve().parents[2] / "services"


class _Feed:
    def __init__(self, title=None, user_title=None, url="https://example.com/feed"):
        self.title = title
        self.user_title = user_title
        self.url = url
        # reader derives this; mirror that rule rather than trusting the caller to pass it.
        self.resolved_title = user_title or title


def test_a_rename_wins_over_the_feeds_own_title():
    assert main.feed_display_title(_Feed(title="News", user_title="Visual Studio Magazine - News")) == (
        "Visual Studio Magazine - News"
    )


def test_the_feeds_own_title_is_used_when_there_is_no_rename():
    assert main.feed_display_title(_Feed(title="The Washington Beer Blog")) == "The Washington Beer Blog"


def test_a_feed_with_no_title_at_all_falls_back():
    """Real case: a feed whose title is empty string, renamed by the user, and one with neither."""
    assert main.feed_display_title(_Feed(title="", user_title="Buttsmithy.com")) == "Buttsmithy.com"
    assert main.feed_display_title(_Feed(title=""), fallback="https://e.com/f") == "https://e.com/f"


def test_an_object_missing_the_fields_entirely_does_not_explode():
    """Several call sites pass whatever reader handed back, hence getattr rather than attribute access."""
    assert main.feed_display_title(object(), fallback="fallback") == "fallback"


def test_sync_apis_agree_on_the_display_name():
    """fever and greader already honored user_title; miniflux did not, so the same feed had two names
    depending on which client asked."""
    for name in ("fever.py", "greader.py", "miniflux.py"):
        source = (SERVICES / name).read_text()
        for line in source.splitlines():
            if '"title":' in line and ("f.title" in line or "feed.title" in line):
                assert "user_title" in line or "resolved_title" in line, f"{name}: {line.strip()}"
