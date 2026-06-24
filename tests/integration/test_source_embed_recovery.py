"""Source-page embed recovery (#6 Part A).

Older entries whose <iframe> was stripped at ingest (no placeholder figure) get
their YouTube/Bandcamp/SoundCloud players recovered from the source page."""
from __future__ import annotations

import types

import main

VID = "HnYwtLQp2ko"
BC = "https://bandcamp.com/EmbeddedPlayer/album=2900162577/size=large/"
SC = "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/123"

SOURCE_HTML = (
    "<html><body><article>"
    f'<iframe src="https://www.youtube.com/embed/{VID}"></iframe>'
    f'<iframe src="{BC}"></iframe>'
    f'<iframe src="{SC}"></iframe>'
    '<iframe src="https://evil.example.com/tracker"></iframe>'
    "</article></body></html>"
)


def test_extract_pulls_only_allowlisted_embeds():
    out = main._extract_source_embed_iframes(SOURCE_HTML)
    joined = "".join(out)
    assert f"/embed/{VID}" in joined
    assert "bandcamp.com/EmbeddedPlayer" in joined
    assert "soundcloud.com" in joined
    assert "evil.example.com" not in joined


def test_extract_skips_already_present_src():
    existing = f'<iframe src="https://www.youtube.com/embed/{VID}"></iframe>'
    out = main._extract_source_embed_iframes(SOURCE_HTML, existing_html=existing)
    joined = "".join(out)
    assert f"/embed/{VID}" not in joined  # already present → skipped
    assert "bandcamp.com/EmbeddedPlayer" in joined


def test_extract_youtube_uses_inline_player():
    out = main._extract_source_embed_iframes(
        f'<iframe src="https://www.youtube.com/embed/{VID}"></iframe>'
    )
    assert "youtube-embed-container" in out[0]


def test_extract_no_iframe_is_empty():
    assert main._extract_source_embed_iframes("<p>no embeds here</p>") == []


def _entry(link="https://example.com/post"):
    return types.SimpleNamespace(link=link)


def test_inject_appends_recovered_embeds(monkeypatch):
    monkeypatch.setattr(
        main.lead_image_service, "get_cached_source_html",
        lambda link: ("https://example.com/post", SOURCE_HTML),
    )
    body = "<p>Article body with no embed.</p>"
    out = main._inject_recovered_source_embeds(body, _entry())
    assert "Article body with no embed." in out
    assert f"/embed/{VID}" in out
    assert "bandcamp.com/EmbeddedPlayer" in out
    assert 'class="lectio-embed"' in out


def test_inject_skips_when_body_already_has_iframe(monkeypatch):
    called = {"n": 0}

    def _boom(link):
        called["n"] += 1
        return None

    monkeypatch.setattr(main.lead_image_service, "get_cached_source_html", _boom)
    body = f'<p>Body</p><iframe src="https://www.youtube.com/embed/{VID}"></iframe>'
    assert main._inject_recovered_source_embeds(body, _entry()) == body
    assert called["n"] == 0  # never touched the cache


def test_inject_noop_without_link(monkeypatch):
    body = "<p>no link</p>"
    assert main._inject_recovered_source_embeds(body, _entry(link="")) == body


def test_inject_queues_without_blocking_on_miss(monkeypatch):
    events = {"queued": [], "waited": []}
    monkeypatch.setattr(
        main.lead_image_service, "get_cached_source_html", lambda link: None
    )
    monkeypatch.setattr(
        main.lead_image_service, "queue_source_html_fetch",
        lambda link, *a, **k: events["queued"].append(link),
    )
    monkeypatch.setattr(
        main.lead_image_service, "wait_for_source_html_fetch",
        lambda *a, **k: events["waited"].append(1) or False,
    )
    body = "<p>body</p>"
    out = main._inject_recovered_source_embeds(body, _entry())
    assert out == body  # cache miss → unchanged, fills on a later open
    assert events["queued"] == ["https://example.com/post"]
    assert events["waited"] == []  # must NOT block the render on the fetch
