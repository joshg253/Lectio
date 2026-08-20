"""A rule's plain keyword may list several terms, comma-separated.

Multiple terms used to require regex, and a hand-written regex is where the
word-boundary traps live: `Apple|AirPods|iPhone|MacBook` matched "Grapplers"
and "Dole Pineapple Tidbits" in a deals folder. Terms keep SUBSTRING semantics
— on the live library 4 plain rules match only *inside* words and would stop
matching entirely if a split implied a word boundary."""
from __future__ import annotations

import re

import pytest

import main


@pytest.mark.parametrize("keyword,expected", [
    ("apple", ["apple"]),
    ("apple, iphone", ["apple", "iphone"]),
    ("  apple ,  iphone  ", ["apple", "iphone"]),
    ("apple,,iphone", ["apple", "iphone"]),
    (" , ", []),
    ("", []),
])
def test_split_keyword_terms(keyword, expected):
    assert main.split_keyword_terms(keyword) == expected


def test_single_keyword_is_still_a_substring_match():
    match = main.build_keyword_matcher("apple", False)
    assert match("Grapplers: Relic Rivals") is True      # unchanged, deliberately
    assert match("APPLE PIE") is True
    assert match("nothing here") is False


def test_comma_list_matches_any_term():
    match = main.build_keyword_matcher("spoiler, leak, rumor", False)
    assert match("Big spoiler inside") is True
    assert match("a LEAK today") is True
    assert match("rumors abound") is True
    assert match("just the news") is False


def test_empty_text_never_matches():
    for keyword in ("apple", "apple, iphone"):
        match = main.build_keyword_matcher(keyword, False)
        assert match("") is False
        assert match(None) is False


def test_regex_mode_is_case_insensitive_and_safe_on_empty_text():
    match = main.build_keyword_matcher(r"a{1,2}b", True)
    assert match("AAB") is True
    assert match("") is False
    assert match(None) is False


def test_a_plain_term_cannot_require_a_literal_comma():
    """The limitation stated plainly: in plain mode the comma is the separator,
    so "hello, world" matches text with neither comma present."""
    match = main.build_keyword_matcher("hello, world", False)
    assert match("hello world") is True      # matched by the term "hello"
    assert match("world domination") is True  # and by "world" alone
    assert main.build_keyword_matcher(r"hello, world", True)("say hello, world!") is True


def test_regex_mode_ignores_commas():
    """A comma is a legal regex character; splitting one would silently rewrite
    the pattern."""
    match = main.build_keyword_matcher(r"a{1,2}b", True)
    assert match("aab") is True
    assert match("ab") is True


def test_regex_mode_still_reaches_word_boundaries():
    match = main.build_keyword_matcher(r"\b(Apple|iPhone)", True)
    assert match("new iPhone deal") is True
    assert match("Grapplers: Relic Rivals") is False
    assert match("Dole Pineapple Tidbits") is False


def test_bad_regex_raises_for_the_caller_to_report():
    with pytest.raises(re.error):
        main.build_keyword_matcher("(unclosed", True)


def test_a_literal_comma_needs_regex_mode():
    """Documented limitation: in plain mode the comma is the separator."""
    assert main.build_keyword_matcher("hello, world", False)("hello, world") is True
    assert main.build_keyword_matcher("x, y", False)("only x") is True
    assert main.build_keyword_matcher(r"hello, world", True)("say hello, world!") is True


# --- the three call sites that used to carry their own copy of the matcher ----


def test_dry_run_run_now_and_live_matching_share_one_matcher(monkeypatch):
    """They each built their own lambda before, which is how they drift. Assert
    all three route through build_keyword_matcher rather than re-implementing."""
    calls = []
    real = main.build_keyword_matcher

    def spy(keyword, is_regex):
        calls.append((keyword, is_regex))
        return real(keyword, is_regex)

    monkeypatch.setattr(main, "build_keyword_matcher", spy)

    class _Entry:
        title = "A spoiler appears"
        content = ()
        summary = ""

    assert main._entry_matches_rule(_Entry(), "spoiler, leak", False, "title") is True
    assert main._entry_matches_rule(_Entry(), "leak, rumor", False, "title") is False

    # The other two call sites, which are the ones that actually drift: a preview
    # that matches differently from the run is the bug this consolidation fixes.
    with main.get_meta_connection() as conn:
        main._dry_run_pattern(conn, "global", "", "spoiler, leak", False, "title")
        main._run_now_pattern(conn, "global", "", "leak, rumor", False, "title")

    assert calls == [("spoiler, leak", False), ("leak, rumor", False),
                     ("spoiler, leak", False), ("leak, rumor", False)]
