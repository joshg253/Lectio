"""Unit tests for the email service: HTML/text rendering and send logic."""
from __future__ import annotations

import pytest
from services.email import _build_html, _build_text, send_article_email


def test_build_html_contains_key_fields():
    html = _build_html("My Title", "My Feed", "https://example.com/article", "A short excerpt.")
    assert "My Title" in html
    assert "My Feed" in html
    assert "https://example.com/article" in html
    assert "A short excerpt." in html
    assert "Lectio" in html


def test_build_html_escapes_special_chars():
    html = _build_html("<b>Title</b>", "Feed & Co.", "https://example.com/?a=1&b=2", "Excerpt <script>alert(1)</script>")
    assert "<b>Title</b>" not in html
    assert "&lt;b&gt;Title&lt;/b&gt;" in html
    assert "Feed &amp; Co." in html
    assert "<script>" not in html


def test_build_html_no_excerpt_omits_block():
    html = _build_html("Title", "Feed", "https://example.com/", "")
    assert 'class="excerpt"' not in html


def test_build_html_no_feed_title_omits_meta():
    html = _build_html("Title", "", "https://example.com/", "")
    assert 'class="meta"' not in html


def test_build_text_contains_all_fields():
    text = _build_text("My Title", "My Feed", "https://example.com/", "Excerpt here.")
    assert "My Title" in text
    assert "My Feed" in text
    assert "https://example.com/" in text
    assert "Excerpt here." in text
    assert "Lectio" in text


def test_build_text_no_feed_title():
    text = _build_text("Title", "", "https://example.com/", "")
    assert "from:" not in text.lower()


def test_send_article_email_calls_resend(monkeypatch):
    calls = []

    class FakeEmails:
        @staticmethod
        def send(payload):
            calls.append(payload)

    import resend as _resend
    monkeypatch.setattr(_resend, "Emails", FakeEmails)

    ok, err = send_article_email(
        api_key="re_test",
        from_addr="from@example.com",
        to_addr="to@example.com",
        title="Hello",
        feed_title="My Feed",
        link="https://example.com/hello",
        excerpt="Short excerpt.",
    )

    assert ok is True
    assert err is None
    assert len(calls) == 1
    assert calls[0]["to"] == ["to@example.com"]
    assert calls[0]["from"] == "from@example.com"
    assert calls[0]["subject"] == "Hello"
    assert "Hello" in calls[0]["html"]
    assert "Hello" in calls[0]["text"]


def test_send_article_email_returns_error_on_exception(monkeypatch):
    class BrokenEmails:
        @staticmethod
        def send(_payload):
            raise RuntimeError("API down")

    import resend as _resend
    monkeypatch.setattr(_resend, "Emails", BrokenEmails)

    ok, err = send_article_email("key", "from@x.com", "to@x.com", "T", "F", "https://x.com", "")
    assert ok is False
    assert "API down" in err
