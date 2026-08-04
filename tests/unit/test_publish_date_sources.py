"""Date sources beyond a page's metadata: text a human reads, and a site index.

Both came from real posts whose date was plainly visible yet unreachable:
hanselman.com prints `<span class="blogMetaDate">February 03, 2026</span>` and
ships nothing machine-readable, and what-if.xkcd.com articles carry no date at
all while the site's archive index lists every one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import publish_date as pd

# ── a date printed for humans ────────────────────────────────────────────────


def test_reads_the_labelled_date_element():
    """The exact hanselman shape."""
    html = '<section><span class="blogMetaDate">February 03, 2026</span></section>'
    assert pd.from_visible_text(html) == datetime(2026, 2, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("html,expected", [
    ('<div class="post-date">3 February 2026</div>', datetime(2026, 2, 3, tzinfo=timezone.utc)),
    ('<p class="published">2026-02-03</p>', datetime(2026, 2, 3, tzinfo=timezone.utc)),
    ('<time class="timestamp">March 9, 2021</time>', datetime(2021, 3, 9, tzinfo=timezone.utc)),
    ('<span id="entry-date">October 21, 2024</span>', datetime(2024, 10, 21, tzinfo=timezone.utc)),
])
def test_common_shapes(html, expected):
    assert pd.from_visible_text(html) == expected


def test_unlabelled_dates_are_ignored():
    """A page is full of date-shaped text — a copyright line, an article body
    mentioning a date. Only an element the publisher labelled counts."""
    html = '<footer>Copyright January 1, 2001</footer><p>born on May 5, 1999</p>'
    assert pd.from_visible_text(html) is None


def test_first_labelled_date_wins_over_later_ones():
    """Publishers put the post's date above the fold; comment timestamps and
    "related posts" rails come after."""
    html = ('<span class="blogMetaDate">February 03, 2026</span>'
            '<div class="comment-date">March 15, 2026</div>')
    assert pd.from_visible_text(html) == datetime(2026, 2, 3, tzinfo=timezone.utc)


def test_out_of_range_values_are_rejected():
    assert pd.from_visible_text('<span class="date">January 1, 1900</span>') is None


def test_future_dates_are_rejected():
    """A future date is a clock problem, not a publication."""
    future = datetime.now(timezone.utc) + timedelta(days=400)
    html = f'<span class="date">{future.strftime("%B %d, %Y")}</span>'
    assert pd.from_visible_text(html) is None


def test_no_html_is_not_an_error():
    assert pd.from_visible_text(None) is None
    assert pd.from_visible_text("") is None


# ── a date the site publishes away from the article ──────────────────────────


@pytest.fixture
def stub_whatif_index(monkeypatch):
    monkeypatch.setattr(pd, "_whatif_index", {
        "157": datetime(2018, 5, 21, tzinfo=timezone.utc),
        "1": datetime(2012, 7, 10, tzinfo=timezone.utc),
    })


@pytest.mark.parametrize("url", [
    "https://what-if.xkcd.com/157/",
    "http://what-if.xkcd.com/157",
    "https://www.what-if.xkcd.com/157/",
])
def test_site_index_dates_an_article_by_number(stub_whatif_index, url):
    """http/https, www or not, trailing slash or not — the number is the identity."""
    assert pd.from_site_index(url) == datetime(2018, 5, 21, tzinfo=timezone.utc)


def test_site_index_needs_no_page_html(stub_whatif_index):
    """The whole point: these articles have no date on them to fetch."""
    assert pd.resolve(None, "https://what-if.xkcd.com/157/") == (
        datetime(2018, 5, 21, tzinfo=timezone.utc), "site-index")


def test_unknown_article_number_yields_nothing(stub_whatif_index):
    assert pd.from_site_index("https://what-if.xkcd.com/9999/") is None


def test_unregistered_host_yields_nothing():
    assert pd.from_site_index("https://example.test/post") is None


def test_a_failing_resolver_never_raises(monkeypatch):
    """A date is a bonus; a resolver blowing up must not fail the re-fetch."""
    def _boom(_url):
        raise RuntimeError("archive down")

    monkeypatch.setitem(pd._SITE_RESOLVERS, "broken.test", _boom)
    assert pd.from_site_index("https://broken.test/post") is None


def test_empty_index_is_not_cached(monkeypatch):
    """An empty parse means the archive markup changed — caching it as truth
    would poison every later lookup in the process."""
    monkeypatch.setattr(pd, "_whatif_index", None)

    class _Resp:
        text = "<html>nothing we recognize</html>"
        def raise_for_status(self): pass

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    from services import url_guard
    monkeypatch.setattr(url_guard, "build_client", lambda **kw: _Client())
    monkeypatch.setattr(url_guard, "safe_get", lambda *a, **kw: _Resp())

    assert pd.fetch_whatif_index() == {}
    assert pd._whatif_index is None, "an empty parse was cached as truth"


# ── ordering ─────────────────────────────────────────────────────────────────


def test_visible_text_is_preferred_over_the_site_index(stub_whatif_index):
    """The page itself is closer to the truth than an index listing."""
    html = '<span class="date">January 2, 2020</span>'
    found, source = pd.resolve(html, "https://what-if.xkcd.com/157/")
    assert (found, source) == (datetime(2020, 1, 2, tzinfo=timezone.utc), "visible-text")


def test_nothing_found_reports_no_source():
    assert pd.resolve("<p>no dates here</p>", "https://example.test/x") == (None, "")
