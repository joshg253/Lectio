"""Unit tests for the email service: HTML/text rendering and send logic."""

from __future__ import annotations

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


def test_build_html_renders_multiple_paragraphs_for_full_text():
    html = _build_html("Title", "Feed", "https://example.com/", "First para.\n\nSecond para.")
    assert html.count('<p class="excerpt">') == 2
    assert '<p class="excerpt">First para.</p>' in html
    assert '<p class="excerpt">Second para.</p>' in html


def test_build_html_uses_excerpt_html_unescaped_when_given():
    """excerpt_html is pre-sanitized article HTML — embedded as-is, not
    escaped-and-wrapped like the plain excerpt path."""
    out = _build_html(
        "Title",
        "Feed",
        "https://example.com/",
        "fallback plain text, should not appear",
        excerpt_html='<p>Real <b>formatted</b> body with a <a href="https://x.com">link</a>.</p>',
    )
    assert '<div class="excerpt"><p>Real <b>formatted</b> body' in out
    assert 'href="https://x.com"' in out
    assert "fallback plain text" not in out


def test_build_html_falls_back_to_plain_excerpt_when_no_html_given():
    out = _build_html("Title", "Feed", "https://example.com/", "Plain snippet.", excerpt_html=None)
    assert '<p class="excerpt">Plain snippet.</p>' in out
    assert '<div class="excerpt">' not in out


def test_build_html_shrinks_oversized_images_instead_of_clipping():
    """.wrapper clips overflow rather than scrolling it, so a wide article
    image needs its own max-width or its right edge gets cut off."""
    out = _build_html(
        "Title",
        "Feed",
        "https://example.com/",
        "",
        excerpt_html='<p><img src="https://example.com/wide.jpg" width="1200" height="800"></p>',
    )
    assert ".excerpt img" in out and "max-width: 100%" in out


def test_build_html_wrapper_is_wider_than_the_old_600px():
    out = _build_html("Title", "Feed", "https://example.com/", "Text.")
    assert "max-width: 600px" not in out


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


def test_send_article_email_html_part_uses_excerpt_html(monkeypatch):
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
        excerpt="Plain text fallback.",
        excerpt_html="<p>Rich <em>body</em>.</p>",
    )

    assert ok is True
    assert err is None
    assert "<p>Rich <em>body</em>.</p>" in calls[0]["html"]
    # The text part is unaffected — always the plain excerpt, never the HTML.
    assert "Plain text fallback." in calls[0]["text"]
    assert "<em>" not in calls[0]["text"]


def test_send_article_email_returns_error_on_exception(monkeypatch):
    class BrokenEmails:
        @staticmethod
        def send(_payload):
            raise RuntimeError("API down")

    import resend as _resend

    monkeypatch.setattr(_resend, "Emails", BrokenEmails)

    ok, err = send_article_email("key", "from@x.com", "to@x.com", "T", "F", "https://x.com", "")
    assert ok is False
    assert err and "API down" in err


# --- the app look (2026-09-25) ----------------------------------------------------------------------------------------------------------

LEAD = "https://cdn.example.com/hero.jpg"


def test_lead_image_is_a_hero_unless_the_body_already_has_it():
    with_hero = _build_html("T", "Feed", "https://example.com/a", "snippet", lead_image_url=LEAD)
    assert f'<img class="hero" src="{LEAD}"' in with_hero
    in_body = _build_html("T", "Feed", "https://example.com/a", "", excerpt_html=f'<p><img src="{LEAD}"></p>', lead_image_url=LEAD)
    assert 'class="hero"' not in in_body


def test_non_http_lead_image_is_ignored():
    assert 'class="hero"' not in _build_html("T", "Feed", "https://example.com/a", "x", lead_image_url="javascript:alert(1)")


def test_meta_line_carries_feed_date_and_author_like_the_entry_pane():
    out = _build_html("T", "PC Gamer", "https://www.pcgamer.com/a", "x", author="Morgan & Park", published="Sep 24, 2026")
    assert '<span class="feed">PC Gamer</span> · Sep 24, 2026 · by Morgan &amp; Park' in out


def test_button_names_the_site():
    assert "Read on pcgamer.com →" in _build_html("T", "F", "https://www.pcgamer.com/games/x/", "x")


def test_inline_svg_is_dropped_from_full_text():
    body = '<p>Text</p><svg width="100%" viewBox="0 0 10 10"><path d="M0 0"/></svg><p>More</p>'
    out = _build_html("T", "F", "https://example.com/", "", excerpt_html=body)
    assert "<svg" not in out and "<p>Text</p><p>More</p>" in out


def test_dark_theme_follows_the_reader_app():
    out = _build_html("T", "F", "https://example.com/", "x")
    assert '<meta name="color-scheme" content="light dark">' in out
    assert "@media (prefers-color-scheme: dark)" in out and "#1d242a" in out  # the app's dark surface


def test_digest_uses_the_same_look():
    from services.email import _build_digest_html

    out = _build_digest_html([{"title": "One", "feed_title": "Feed A", "link": "https://a.test/1", "excerpt": "e"}, {"title": "Two"}])
    assert out.count('class="item"') == 2
    assert "digest · 2 articles" in out and "#fcfbf7" in out


def test_hero_is_skipped_when_the_body_carries_the_lead_image_html_escaped():
    lead = "https://cdn.example.com/img.jpg?w=1200&h=800"
    body = '<p><img src="https://cdn.example.com/img.jpg?w=1200&amp;h=800"></p>'
    assert 'class="hero"' not in _build_html("T", "F", "https://example.com/a", "", excerpt_html=body, lead_image_url=lead)
