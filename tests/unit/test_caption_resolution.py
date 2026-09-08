"""Unit tests for the caption-resolution helpers extracted from get_entry_detail:
_initial_image_caption, _suppress_junk_caption, _apply_caption_source_pref."""
from __future__ import annotations

from types import SimpleNamespace

import main


def _entry(title="Title", feed_url="https://f.test/feed", entry_id="e1", summary=None):
    return SimpleNamespace(title=title, feed_url=feed_url, id=entry_id, summary=summary)


# --- _initial_image_caption -------------------------------------------------

def test_initial_caption_prefers_lead_img_title(monkeypatch):
    monkeypatch.setattr(main.lead_image_service, "get_entry_image_alt", lambda *a: None)
    monkeypatch.setattr(main.lead_image_service, "get_entry_image_title", lambda *a: None)
    content = '<img src="https://x.test/lead.jpg" title="the punchline">'
    cap, is_lead, palt, ptitle = main._initial_image_caption(content, _entry(), "https://x.test/lead.jpg")
    assert cap == "the punchline"
    assert is_lead is True


def test_initial_caption_persisted_overrides(monkeypatch):
    monkeypatch.setattr(main.lead_image_service, "get_entry_image_alt", lambda *a: "alt text")
    monkeypatch.setattr(main.lead_image_service, "get_entry_image_title", lambda *a: "title text")
    content = '<img src="https://x.test/lead.jpg" title="in-feed">'
    cap, is_lead, palt, ptitle = main._initial_image_caption(content, _entry(), "https://x.test/lead.jpg")
    assert cap == "title text"  # persisted title preferred over in-feed


# --- _suppress_junk_caption -------------------------------------------------

def test_suppress_trivial_alt():
    assert main._suppress_junk_caption("Share", _entry()) is None
    assert main._suppress_junk_caption("image", _entry()) is None


def test_suppress_date_only():
    assert main._suppress_junk_caption("June 12, 2026", _entry()) is None


def test_suppress_title_restatement():
    assert main._suppress_junk_caption("My Great Post", _entry(title="My Great Post")) is None


def test_suppress_banner_restatement():
    # "Progress Update Banner 2026" restates title "Progress Update" + decorative+date.
    assert main._suppress_junk_caption("Progress Update Banner 2026", _entry(title="Progress Update")) is None


def test_keeps_real_caption():
    assert main._suppress_junk_caption("A clever hovertext joke", _entry(title="Comic 42")) == "A clever hovertext joke"


# --- _apply_caption_source_pref ---------------------------------------------

def test_caption_pref_none():
    assert main._apply_caption_source_pref("x", {"caption_source": "none"}, _entry(), "<p>c</p>") is None


def test_caption_pref_alt(monkeypatch):
    monkeypatch.setattr(main.lead_image_service, "get_entry_image_alt", lambda *a: "ALT")
    assert main._apply_caption_source_pref("x", {"caption_source": "alt"}, _entry(), "<p>c</p>") == "ALT"


def test_caption_pref_both(monkeypatch):
    monkeypatch.setattr(main.lead_image_service, "get_entry_image_title", lambda *a: "T")
    monkeypatch.setattr(main.lead_image_service, "get_entry_image_alt", lambda *a: "A")
    assert main._apply_caption_source_pref("x", {"caption_source": "both"}, _entry(), "<p>c</p>") == "T — A"


def test_caption_pref_auto_runs_suppression(monkeypatch):
    # auto keeps the value only if should_show_caption approves.
    monkeypatch.setattr(main, "should_show_caption", lambda *a, **k: False)
    assert main._apply_caption_source_pref("x", {"caption_source": "auto"}, _entry(), "<p>c</p>") is None
    monkeypatch.setattr(main, "should_show_caption", lambda *a, **k: True)
    assert main._apply_caption_source_pref("x", {"caption_source": "auto"}, _entry(), "<p>c</p>") == "x"


# --- should_show_caption -----------------------------------------------------

def test_show_caption_suppresses_a_genuinely_visible_figcaption():
    html = '<img src="https://x.test/a.jpg"><figcaption>a clever joke</figcaption>'
    assert main.should_show_caption("a clever joke", entry_title="T", content_html=html, pref=-1) is False


def test_show_caption_keeps_hovertext_that_only_lives_in_an_attribute():
    # xkcd-style body: the whole entry is one <img> whose own alt/title carry the
    # hovertext. That text is a raw substring of content_html but is not visible
    # without a hover — unlike a rendered <figcaption>, it must not be suppressed.
    html = '<img src="https://xkcd.test/c.png" alt="a clever joke" title="a clever joke">'
    assert main.should_show_caption("a clever joke", entry_title="T", content_html=html, pref=-1) is True
