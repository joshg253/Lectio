"""_derived_entry_link gives link-less podcast feeds (Buzzsprout) a clickable
page URL derived from the audio enclosure, so the post title isn't inert."""
from __future__ import annotations

from types import SimpleNamespace

import main


def _enc(href):
    return SimpleNamespace(href=href, url=None)


def test_buzzsprout_link_derived_from_enclosure():
    e = SimpleNamespace(
        link=None,
        enclosures=[_enc("https://www.buzzsprout.com/2315966/episodes/19369337-close-your-apps-and-think-of-england.mp3")],
    )
    assert (
        main._derived_entry_link(e)
        == "https://www.buzzsprout.com/2315966/episodes/19369337-close-your-apps-and-think-of-england"
    )


def test_existing_link_not_overridden():
    e = SimpleNamespace(link="https://x.test/post", enclosures=[_enc("https://www.buzzsprout.com/1/episodes/2-a.mp3")])
    assert main._derived_entry_link(e) is None


def test_non_buzzsprout_enclosure_ignored():
    e = SimpleNamespace(link=None, enclosures=[_enc("https://cdn.other.com/ep1.mp3")])
    assert main._derived_entry_link(e) is None


def test_no_enclosures_returns_none():
    assert main._derived_entry_link(SimpleNamespace(link=None, enclosures=[])) is None
