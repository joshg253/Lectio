"""Auto-file matching: which subscribed feed does an unfiled saved article
belong to, and when is that proposal safe to pre-approve?

The guards exist because of real data: guitarworld.com's target was backed by 77
of the feed's own entries (trustworthy), while guitarplayer.com's only candidate
was a scraped one-article URL with a single supporting entry — filing 303
articles into that would have been wrong.
"""
from __future__ import annotations

import pytest

from services.saved_autofile import (
    MIN_SUPPORT,
    article_host,
    build_autofile_plan,
    plan_totals,
)


@pytest.mark.parametrize("url,expected", [
    ("https://www.Example.COM/a", "example.com"),
    ("http://example.com:8080/a", "example.com"),
    ("https://user@example.com/a", "example.com"),
    ("https://sub.example.com/a", "sub.example.com"),  # subdomains are distinct
    ("", ""),
    (None, ""),
    ("not a url", ""),
])
def test_article_host(url, expected):
    assert article_host(url) == expected


def _links(feed, host, n):
    return [(feed, f"https://{host}/post-{i}") for i in range(n)]


def test_matches_on_article_host_not_feed_url_host():
    """A feed's URL often lives on a different host than its articles
    (rss.beehiiv.com serving joanwestenberg.com), so the evidence has to be the
    feed's entry links."""
    plan = build_autofile_plan(
        [("s1", "https://joanwestenberg.com/p/one")],
        _links("https://rss.beehiiv.com/feeds/ABC.xml", "joanwestenberg.com", 10),
    )
    assert len(plan) == 1
    assert plan[0]["target_feed_url"] == "https://rss.beehiiv.com/feeds/ABC.xml"
    assert plan[0]["confident"] is True


def test_low_support_target_is_proposed_but_not_confident():
    """The guitarplayer.com case: one candidate, one supporting entry — almost
    certainly a scraped single-article URL, not the site's feed."""
    plan = build_autofile_plan(
        [(f"s{i}", "https://guitarplayer.com/a") for i in range(303)],
        _links("https://guitarplayer.com/lessons/string-skipping-licks", "guitarplayer.com", 1),
    )
    c = plan[0]
    assert c["count"] == 303
    assert c["target_feed_url"] is not None      # still shown, so it can be picked
    assert c["support"] == 1
    assert c["confident"] is False


def test_enough_support_makes_it_confident():
    plan = build_autofile_plan(
        [("s1", "https://guitarworld.com/a")],
        _links("https://guitarworld.com/feeds/tag/lessons", "guitarworld.com", MIN_SUPPORT),
    )
    assert plan[0]["confident"] is True


def test_two_candidate_feeds_are_ambiguous():
    plan = build_autofile_plan(
        [("s1", "https://example.com/a")],
        _links("https://example.com/feed-a", "example.com", 20)
        + _links("https://example.com/feed-b", "example.com", 15),
    )
    c = plan[0]
    assert c["ambiguous"] is True
    assert c["confident"] is False
    # Ranked best-first so the reviewer sees the strongest option first.
    assert [x["support"] for x in c["candidates"]] == [20, 15]


def test_host_with_no_subscribed_feed_has_no_target():
    plan = build_autofile_plan([("s1", "https://guitarchalk.com/a")], [])
    c = plan[0]
    assert c["target_feed_url"] is None
    assert c["confident"] is False
    assert c["candidates"] == []


def test_clusters_are_largest_first():
    saved = ([(f"a{i}", "https://big.com/x") for i in range(5)]
             + [(f"b{i}", "https://small.com/x") for i in range(2)])
    plan = build_autofile_plan(saved, [])
    assert [c["host"] for c in plan] == ["big.com", "small.com"]


def test_excluded_feeds_are_never_targets():
    """lectio:saved itself must never be proposed as a destination."""
    plan = build_autofile_plan(
        [("s1", "https://example.com/a")],
        _links("lectio:saved", "example.com", 50),
        exclude_feeds=frozenset({"lectio:saved"}),
    )
    assert plan[0]["target_feed_url"] is None


def test_entries_without_a_usable_host_are_dropped():
    plan = build_autofile_plan([("s1", ""), ("s2", "mailto:x@y.z")], [])
    assert plan == []


def test_plan_totals_split_the_backlog_by_disposition():
    plan = build_autofile_plan(
        [("c1", "https://good.com/a"), ("c2", "https://good.com/b"),
         ("l1", "https://weak.com/a"),
         ("m1", "https://both.com/a"),
         ("u1", "https://nowhere.com/a")],
        _links("https://good.com/feed", "good.com", 10)
        + _links("https://weak.com/one-article", "weak.com", 1)
        + _links("https://both.com/feed-a", "both.com", 5)
        + _links("https://both.com/feed-b", "both.com", 4),
    )
    t = plan_totals(plan)
    assert t["hosts"] == 4
    assert t["articles"] == 5
    assert t["confident_articles"] == 2
    assert t["low_support_articles"] == 1
    assert t["ambiguous_articles"] == 1
    assert t["unmatched_articles"] == 1
