"""``tag:`` / ``tagged:`` terms in "Filter this view" (services/view_filter.py and its server-side predicate)."""

from __future__ import annotations

import main  # noqa: F401 -- must import before routes.entries (see routes/__init__.py's circular-import note)
from routes.entries import _view_filter_predicate
from services.view_filter import parse_filter_term


def _match(term: str, **post) -> bool:
    return _view_filter_predicate(term)({"title": "", "link": "", "feed_title": "", **post})


def test_plain_words_become_separate_terms():
    parsed = parse_filter_term("Zelda Tears")
    assert (parsed.clauses, parsed.include, parsed.exclude) == ([], ["zelda", "tears"], [])


def test_bare_prefixes_while_typing_are_skipped():
    for typing in ("tag:", "tag:!", "tagged:", "!", '""'):
        parsed = parse_filter_term(typing)
        assert not (parsed.clauses or parsed.include or parsed.exclude), typing
    assert _match("tag:", title="anything")


def test_chip_exact_negated_and_wildcard():
    assert _match("tag:games", chip_tags=["games", "pc"])
    assert not _match("tag:game", chip_tags=["games"])
    assert not _match("tag:games", chip_tags=[])
    assert _match("tag:!games", chip_tags=["pc"])
    assert _match("tag:!games", chip_tags=[])  # no chips at all still lacks the chip
    assert not _match("tag:!games", chip_tags=["games"])
    assert _match("tag:game*", chip_tags=["games"])
    assert not _match("tag:game*", chip_tags=["gaming"])
    assert _match("tag:gam*", chip_tags=["gaming"])
    assert _match("tag:*game*", chip_tags=["retro-games-news"])
    assert not _match("tag:!game*", chip_tags=["game", "x"])


def test_tagged_reads_applied_tags_not_chips():
    assert _match("tagged:inbox", applied_tags=["inbox"], chip_tags=[])
    assert not _match("tagged:inbox", applied_tags=[], chip_tags=["inbox"])
    assert not _match("tag:inbox", applied_tags=["inbox"], chip_tags=[])


def test_tokens_and_together_with_text():
    post = {"chip_tags": ["games"], "applied_tags": [], "title": "Zelda review"}
    assert _match("tag:games tagged:!inbox zelda", **post)
    assert not _match("tag:games zelda tagged:inbox", **post)
    assert not _match("tag:games mario", **post)


def test_case_and_spaces_normalize_like_chips():
    assert _match("TAG:Popular Deals".replace(" Deals", "-Deals"), chip_tags=["popular-deals"])
    assert _match("tag:#games", chip_tags=["games"])


def test_regex_metacharacters_are_literal():
    assert _match("tag:c++", chip_tags=["c++"])
    assert not _match("tag:c++", chip_tags=["cc"])
    assert _match("tag:a.b", chip_tags=["a.b"])
    assert not _match("tag:a.b", chip_tags=["axb"])


def test_quoted_phrase_must_be_contiguous():
    assert _match('"zelda tears"', title="Zelda Tears of the Kingdom")
    assert not _match('"zelda tears"', title="Tears for Zelda")
    assert _match("zelda tears", title="Tears for Zelda")  # unquoted words are ANDed, any order


def test_negated_words_and_phrases():
    assert _match("!sponsored", title="Plain post")
    assert not _match("!sponsored", title="Sponsored: a deal")
    assert not _match('!"press release"', title="Our press release")
    assert _match('!"press release"', title="Release the press")
    assert _match('zelda !"tears of"', title="Zelda Breath of the Wild")
    assert not _match('zelda !"tears of"', title="Zelda Tears of the Kingdom")


def test_negation_searches_link_and_feed_too():
    assert not _match("!ign.com", link="https://ign.com/x")
    assert not _match("!pcgamer", feed_title="PCGamer")


def test_quoted_tag_value_can_hold_spaces():
    assert _match('tag:"games to play"', chip_tags=["games-to-play"])
    assert _match('tag:!"games to play"', chip_tags=["other"])
    assert not _match('tag:!"games to play"', chip_tags=["games-to-play"])


def test_quoted_text_that_looks_like_a_tag_term_is_text():
    assert _match('"tag:games"', title="about tag:games syntax")
    assert not _match('"tag:games"', title="plain", chip_tags=["games"])


def test_unclosed_quote_runs_to_the_end():
    assert _match('"zelda tea', title="Zelda Tears")


def test_many_stars_cannot_backtrack():
    """A `.*`-joined regex took seconds on a dozen stars against a 32-character tag of one letter."""
    import time

    started = time.perf_counter()
    assert not _match("tag:" + "*a" * 14 + "*b", chip_tags=["a" * 32])
    assert _match("tag:" + "*a" * 14 + "*", chip_tags=["a" * 32])
    assert time.perf_counter() - started < 0.5


def test_glob_edges():
    assert _match("tag:*", chip_tags=["anything"])
    assert not _match("tag:*", chip_tags=[])
    assert _match("tag:a**b", chip_tags=["ab"])
    assert _match("tag:a*b*c", chip_tags=["a-b-c"])
    assert not _match("tag:a*b*c", chip_tags=["a-c-b"])
    assert not _match("tag:ab*bc", chip_tags=["abc"])  # prefix and suffix may not overlap
    assert _match("tag:*-games", chip_tags=["retro-games"])


def test_duration_equals_forms_in_the_youtube_folder(monkeypatch):
    import routes.entries as entries

    monkeypatch.setattr(entries, "_folder_is_yt_folder", lambda folder_id: True)
    pred = lambda term, dur: entries._view_filter_predicate(term, folder_id=1)({"duration_seconds": dur})  # noqa: E731
    assert pred("=0", None) and not pred("=0", 90)
    assert pred("=1-2", 90) and not pred("=1-2", 30) and not pred("=1-2", None)
    assert pred("=1:30-2:00", 100)
