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
    joined = "".join(e for _, e in out)
    assert f"/embed/{VID}" in joined
    assert "bandcamp.com/EmbeddedPlayer" in joined
    assert "soundcloud.com" in joined
    assert "evil.example.com" not in joined


def test_extract_skips_already_present_src():
    existing = f'<iframe src="https://www.youtube.com/embed/{VID}"></iframe>'
    out = main._extract_source_embed_iframes(SOURCE_HTML, existing_html=existing)
    joined = "".join(e for _, e in out)
    assert f"/embed/{VID}" not in joined  # already present → skipped
    assert "bandcamp.com/EmbeddedPlayer" in joined


def test_extract_youtube_uses_inline_player_and_canonical():
    out = main._extract_source_embed_iframes(
        f'<iframe src="https://www.youtube.com/embed/{VID}"></iframe>'
    )
    canonical, embed = out[0]
    assert canonical == f"yt:{VID}"
    assert "youtube-embed-container" in embed


def test_extract_bandcamp_canonical_from_inner_link():
    html = (
        f'<iframe src="{BC}">'
        '<a href="https://artist.bandcamp.com/album/foo">Foo by Artist</a>'
        "</iframe>"
    )
    out = main._extract_source_embed_iframes(html)
    assert out[0][0] == "https://artist.bandcamp.com/album/foo"


def test_extract_no_iframe_is_empty():
    assert main._extract_source_embed_iframes("<p>no embeds here</p>") == []


def test_extract_youtube_facade_thumbnail():
    # guitarworld.com pattern: click-to-load facade, no iframe in raw HTML.
    html = (
        '<div class="youtube-video youtube-facade" id="youtube-hIsHUwJZa_o">'
        '<img src="https://img.youtube.com/vi/hIsHUwJZa_o/maxresdefault.jpg">'
        "</div>"
    )
    out = main._extract_source_embed_iframes(html)
    assert out and out[0][0] == "yt:hIsHUwJZa_o"
    assert "/embed/hIsHUwJZa_o" in out[0][1]


def test_extract_facade_thumbnail_needs_video_ancestor_hint():
    # A bare /vi/ thumbnail with no video-ish container (e.g. a random article
    # image that happens to be a YouTube thumb) is not treated as an embed.
    html = (
        '<div class="article-body"><figure>'
        f'<img src="https://i.ytimg.com/vi/{VID}/hqdefault.jpg">'
        "</figure></div>"
    )
    assert main._extract_source_embed_iframes(html) == []


def test_extract_facade_thumbnail_host_case_insensitive():
    html = (
        '<div class="youtube-facade">'
        f'<img src="https://I.YTIMG.COM/vi/{VID}/maxresdefault.jpg">'
        "</div>"
    )
    out = main._extract_source_embed_iframes(html)
    assert out and out[0][0] == f"yt:{VID}"  # ID case preserved


def test_extract_lite_youtube_element():
    out = main._extract_source_embed_iframes(f'<lite-youtube videoid="{VID}"></lite-youtube>')
    assert out and out[0][0] == f"yt:{VID}"


def test_extract_facade_dedupes_against_iframe_of_same_video():
    html = (
        f'<iframe src="https://www.youtube.com/embed/{VID}"></iframe>'
        f'<div class="youtube-facade"><img src="https://i.ytimg.com/vi/{VID}/maxresdefault.jpg"></div>'
    )
    out = main._extract_source_embed_iframes(html)
    assert len([c for c, _ in out if c == f"yt:{VID}"]) == 1


def test_place_fills_empty_p_after_heading():
    body = "<h3>Burial official video</h3><p></p><h3>Live on KEXP</h3><p></p>"
    items = [
        (f"yt:{VID}", main._youtube_embed_html(VID)),
        ("yt:abc12345678", main._youtube_embed_html("abc12345678")),
    ]
    out = main._place_recovered_embeds(body, items)
    assert "<p></p>" not in out  # both placeholders filled
    assert f"/embed/{VID}" in out
    # filled in document order: first heading's slot gets the first embed
    assert out.index("Burial") < out.index(f"/embed/{VID}") < out.index("Live on KEXP")


def test_place_replaces_matching_bare_link():
    body = f'<p>Watch:</p><p><a href="https://youtu.be/{VID}">https://youtu.be/{VID}</a></p>'
    items = [(f"yt:{VID}", main._youtube_embed_html(VID))]
    out = main._place_recovered_embeds(body, items)
    assert f"youtu.be/{VID}" not in out  # bare link replaced by the player
    assert "youtube-embed-container" in out


def test_place_fills_empty_video_husk_div():
    # guitarworld feed bodies keep the stripped facade's empty container div;
    # the recovered player should land there (top of the article), not at the
    # bottom.
    body = (
        '<article><div class="youtube-video"><div class="video-aspect-box"></div></div>'
        "<p>First real paragraph.</p></article>"
    )
    out = main._place_recovered_embeds(body, [(f"yt:{VID}", f'<iframe src="https://www.youtube.com/embed/{VID}"></iframe>')])
    assert out.index(f"/embed/{VID}") < out.index("First real paragraph")
    assert 'class="youtube-video"' not in out  # husk consumed, not duplicated


def test_place_husk_ignores_divs_with_content():
    body = '<div class="video-wrap"><img src="poster.jpg"></div><p>text</p>'
    out = main._place_recovered_embeds(body, [(f"yt:{VID}", "<iframe></iframe>")])
    # Div has content → not a husk; embed appended at bottom instead.
    assert '<img src="poster.jpg"' in out
    assert 'class="lectio-embed"' in out  # fell through to the append pass


def test_place_appends_leftovers_at_bottom():
    body = "<p>Article body, no slots or links.</p>"
    items = [(f"yt:{VID}", main._youtube_embed_html(VID))]
    out = main._place_recovered_embeds(body, items)
    assert "Article body" in out
    assert 'class="lectio-embed"' in out
    assert out.index("Article body") < out.index("lectio-embed")


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


def test_strip_bandcamp_track_signature():
    src = ("https://bandcamp.com/EmbeddedPlayer/album=2004014866/size=large/"
           "bgcol=ffffff/tracklist=true/tracks=159,260/esig=d83a3a2cbedcb6/")
    html_in = f'<iframe loading="lazy" src="{src}"></iframe>'
    out = main._strip_bandcamp_track_signature(html_in)
    assert "tracks=" not in out
    assert "esig=" not in out
    assert "album=2004014866" in out
    assert "tracklist=true" in out  # display params kept


def test_strip_bandcamp_leaves_plain_album_untouched():
    src = "https://bandcamp.com/EmbeddedPlayer/album=1116738620/size=large/tracklist=false/"
    html_in = f'<iframe src="{src}"></iframe>'
    assert main._strip_bandcamp_track_signature(html_in) == html_in


def test_strip_bandcamp_noop_without_embed():
    assert main._strip_bandcamp_track_signature("<p>no embeds</p>") == "<p>no embeds</p>"
