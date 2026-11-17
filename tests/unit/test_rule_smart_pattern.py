"""A rule's keyword box is "smart": /slashes/ mean regex, and a leading =, <, > (as in "Filter this view") means YouTube duration."""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy

import main  # must sort before routes.automation — see routes/__init__.py
from routes import automation as automation_routes
from services import automation_rules, rule_duration, tenancy


@pytest.fixture
def isolated_reader(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


@pytest.mark.parametrize(
    "term,expected",
    [
        ("=2-10", {"op": "range", "min": 120, "max": 600}),
        ("=1:30-2:00", {"op": "range", "min": 90, "max": 120}),
        ("=90s-3m", {"op": "range", "min": 90, "max": 180}),
        ("<2:00", {"op": "<", "seconds": 120}),
        (">=1h", {"op": ">=", "seconds": 3600}),
        ("  >5  ", {"op": ">", "seconds": 300}),
        ("=2", None),  # bare exact-match isn't a shape
        ("=0", None),  # "no duration" would match every non-video post; not supported in rules
        ("=a-b", None),
        ("apple", None),
        ("", None),
        ("<br> tag", None),
    ],
)
def test_parse_duration_term(term, expected):
    assert rule_duration.parse_duration_term(term) == expected


def test_duration_in_range_is_inclusive():
    spec = rule_duration.parse_duration_term("=2-10")
    assert spec is not None
    assert rule_duration.duration_matches(spec, 120) is True
    assert rule_duration.duration_matches(spec, 600) is True
    assert rule_duration.duration_matches(spec, 119) is False
    assert rule_duration.duration_matches(spec, 601) is False


def test_slashes_mean_regex_in_the_matcher():
    match = main.build_keyword_matcher(r"/^live (at|from)\b/", False)
    assert match("Live at the Paradiso") is True
    assert match("Not live at all") is False
    # Plain text with slashes elsewhere stays literal, and a lone "/x/" pair needs real content between.
    assert main.build_keyword_matcher("a/b", False)("xa/by") is True
    assert main.build_keyword_matcher("//", False)("a // b") is True


_YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=abc"


class _Entry:
    def __init__(self, link, title="t", feed_url=_YT_FEED):
        self.feed_url = feed_url
        self.link = link
        self.title = title
        self.content = ()
        self.summary = ""


@pytest.fixture
def durations(monkeypatch):
    table = {"aaaaaaaaaaa": 300, "bbbbbbbbbbb": 30, "ccccccccccc": 3000, "ddddddddddd": None}
    monkeypatch.setattr(automation_rules.youtube_duration_service, "get_cached_duration", lambda vid: (table.get(vid), None))
    return table


def _yt(vid):
    return _Entry(f"https://www.youtube.com/watch?v={vid}")


def test_entry_matches_rule_by_duration(durations):
    assert main._entry_matches_rule(_yt("aaaaaaaaaaa"), "=2-10", False, "title") is True
    assert main._entry_matches_rule(_yt("bbbbbbbbbbb"), "=2-10", False, "title") is False
    assert main._entry_matches_rule(_yt("ccccccccccc"), "=2-10", False, "title") is False
    assert main._entry_matches_rule(_yt("ccccccccccc"), ">20:00", False, "title") is True


def test_unknown_duration_and_non_video_never_match(durations):
    assert main._entry_matches_rule(_yt("ddddddddddd"), "=2-10", False, "title") is False
    assert main._entry_matches_rule(_Entry("https://example.test/post"), "=2-10", False, "title") is False


def test_run_now_and_dry_run_use_duration(isolated_reader, durations):
    import datetime as dt

    feed = _YT_FEED
    reader = main.get_reader()
    reader.add_feed(feed, allow_invalid_url=True)
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"):
        reader.add_entry(
            {
                "feed_url": feed,
                "id": vid,
                "title": f"video {vid}",
                "link": f"https://www.youtube.com/watch?v={vid}",
                "published": dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
            }
        )
    with main.get_meta_connection() as conn:
        preview = automation_routes._dry_run_pattern(conn, "feed", feed, "=2-10", False, "title", unread_only=True)
        assert [m["title"] for m in preview["matches"]] == ["video aaaaaaaaaaa"]
        result = main._run_now_pattern(conn, "feed", feed, "=2-10", False, "title")
    assert result["count"] == 1
    assert reader.get_entry((feed, "aaaaaaaaaaa")).read is True
    assert reader.get_entry((feed, "bbbbbbbbbbb")).read is False


def test_a_youtube_link_in_a_non_youtube_feed_never_matches_a_duration(durations):
    entry = _Entry("https://www.youtube.com/watch?v=aaaaaaaaaaa", feed_url="https://example.test/feed")
    assert main._entry_matches_rule(entry, "=2-10", False, "title") is False


@pytest.mark.parametrize(
    "rule_type,keyword,fragment",
    [
        ("highlight", "=2-10", "Mark as Read rules only"),
        ("youtube_playlist", "<2:00", "Mark as Read rules only"),
        ("mark_as_read", "apple, =2-10", "can't be one item"),
        ("mark_as_read", "apple, /i(pad|phone)/", "can't be one item"),
    ],
)
def test_keyword_syntax_errors(rule_type, keyword, fragment):
    assert fragment in (rule_duration.keyword_syntax_error(rule_type, keyword) or "")


@pytest.mark.parametrize(
    "rule_type,keyword",
    [("mark_as_read", "=2-10"), ("mark_as_read", "/a,b/"), ("mark_as_read", "apple, ipad"), ("tag_filter", "-a, +b"), ("highlight", "x")],
)
def test_keyword_syntax_ok(rule_type, keyword):
    assert rule_duration.keyword_syntax_error(rule_type, keyword) is None


def test_failed_duration_prefetch_does_not_raise(monkeypatch):
    def boom(_feed):
        raise RuntimeError("quota")

    monkeypatch.setattr(automation_rules.youtube_duration_service, "fetch_and_store_durations_for_feed", boom)
    automation_rules._prefetch_durations_for_rule(_YT_FEED, "=2-10")  # must not raise
