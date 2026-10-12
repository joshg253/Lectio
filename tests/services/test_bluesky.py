"""Unit tests for services.bluesky embed-image extraction (no network)."""

from __future__ import annotations

from services import bluesky


def test_is_bsky_feed():
    assert bluesky.is_bsky_feed("https://bsky.app/profile/did:plc:abc/rss")
    assert bluesky.is_bsky_feed("https://bsky.app/profile/handle.bsky.social/rss/")
    assert not bluesky.is_bsky_feed("https://bsky.app/profile/did:plc:abc")
    assert not bluesky.is_bsky_feed("https://example.com/rss")
    assert not bluesky.is_bsky_feed(None)


def test_images_from_images_embed():
    post = {
        "embed": {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {"fullsize": "https://cdn.bsky.app/a", "thumb": "https://cdn.bsky.app/a_t"},
                {"thumb": "https://cdn.bsky.app/b_t"},  # falls back to thumb
            ],
        }
    }
    assert bluesky._images_from_post(post) == ["https://cdn.bsky.app/a", "https://cdn.bsky.app/b_t"]


def test_images_from_gallery_embed():
    """A newer, distinct embed shape from the classic "images" one -- items
    carry fullsize/thumbnail directly (not nested "thumb"), under "items"
    (not "images"). Found live 2026-09-18: a real post using this shape
    returned zero images through the images-only branch."""
    post = {
        "embed": {
            "$type": "app.bsky.embed.gallery#view",
            "items": [
                {
                    "$type": "app.bsky.embed.gallery#viewImage",
                    "fullsize": "https://cdn.bsky.app/a",
                    "thumbnail": "https://cdn.bsky.app/a_t",
                },
                {"$type": "app.bsky.embed.gallery#viewImage", "thumbnail": "https://cdn.bsky.app/b_t"},  # falls back to thumbnail
            ],
        }
    }
    assert bluesky._images_from_post(post) == ["https://cdn.bsky.app/a", "https://cdn.bsky.app/b_t"]


def test_images_from_record_with_media():
    post = {
        "embed": {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "media": {
                "$type": "app.bsky.embed.images#view",
                "images": [{"fullsize": "https://cdn.bsky.app/x"}],
            },
        }
    }
    assert bluesky._images_from_post(post) == ["https://cdn.bsky.app/x"]


def test_images_from_video_embed_uses_thumbnail():
    # Real shape from public.api.bsky.app/xrpc/app.bsky.feed.getPosts.
    post = {
        "embed": {
            "$type": "app.bsky.embed.video#view",
            "cid": "bafkreie2suur674fjn6ek4ab3jdm26mx64lvakbf7nuvjalgcbrci2xv2e",
            "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/playlist.m3u8",
            "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/thumbnail.jpg",
        }
    }
    assert bluesky._images_from_post(post) == ["https://video.bsky.app/watch/did%3Aplc%3Aabc/x/thumbnail.jpg"]


def test_images_from_video_embed_with_no_thumbnail():
    post = {"embed": {"$type": "app.bsky.embed.video#view"}}
    assert bluesky._images_from_post(post) == []


def test_images_from_record_with_media_video():
    post = {
        "embed": {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "media": {
                "$type": "app.bsky.embed.video#view",
                "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/y/thumbnail.jpg",
            },
        }
    }
    assert bluesky._images_from_post(post) == ["https://video.bsky.app/watch/did%3Aplc%3Aabc/y/thumbnail.jpg"]


def test_images_dedup_and_empty():
    assert bluesky._images_from_post({}) == []
    assert bluesky._images_from_post({"embed": {"$type": "app.bsky.embed.external#view"}}) == []
    dup = {"embed": {"$type": "app.bsky.embed.images#view", "images": [{"fullsize": "u"}, {"fullsize": "u"}]}}
    assert bluesky._images_from_post(dup) == ["u"]


def test_fetch_post_images_rejects_non_at_uri():
    assert bluesky.fetch_post_images("") == []
    assert bluesky.fetch_post_images("https://bsky.app/x") == []


def test_fetch_post_video_rejects_non_at_uri():
    assert bluesky.fetch_post_video("") is None
    assert bluesky.fetch_post_video("https://bsky.app/x") is None


def test_video_from_embed():
    embed = {
        "$type": "app.bsky.embed.video#view",
        "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/thumbnail.jpg",
        "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/playlist.m3u8",
    }
    assert bluesky._video_from_embed(embed) == {
        "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/thumbnail.jpg",
        "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/playlist.m3u8",
    }


def test_video_from_embed_carries_aspect_ratio_as_width_height():
    """A portrait (height > width) video stretched to the article's full
    column width via plain CSS renders extremely tall -- width/height let the
    same portrait-cap treatment images get apply here too, without waiting
    for playback (the video is preload="none", so videoWidth/videoHeight
    aren't available until then). Reported live 2026-09-13 as "video is
    super huge and does not play" -- a real 1080x1920 post."""
    embed = {
        "$type": "app.bsky.embed.video#view",
        "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/thumbnail.jpg",
        "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/playlist.m3u8",
        "aspectRatio": {"width": 1080, "height": 1920},
    }
    assert bluesky._video_from_embed(embed) == {
        "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/thumbnail.jpg",
        "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/playlist.m3u8",
        "width": "1080",
        "height": "1920",
    }


def test_video_from_embed_missing_aspect_ratio_omits_width_height():
    embed = {
        "$type": "app.bsky.embed.video#view",
        "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/thumbnail.jpg",
        "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/x/playlist.m3u8",
    }
    result = bluesky._video_from_embed(embed)
    assert result is not None
    assert "width" not in result
    assert "height" not in result


def test_video_from_embed_missing_playlist_or_thumbnail():
    assert bluesky._video_from_embed({"$type": "app.bsky.embed.video#view", "thumbnail": "t"}) is None
    assert bluesky._video_from_embed({"$type": "app.bsky.embed.video#view", "playlist": "p"}) is None
    assert bluesky._video_from_embed({"$type": "app.bsky.embed.video#view"}) is None


def test_video_from_embed_non_video_returns_none():
    assert bluesky._video_from_embed({"$type": "app.bsky.embed.images#view"}) is None
    assert bluesky._video_from_embed(None) is None


def test_video_from_record_with_media():
    embed = {
        "$type": "app.bsky.embed.recordWithMedia#view",
        "media": {
            "$type": "app.bsky.embed.video#view",
            "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/y/thumbnail.jpg",
            "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/y/playlist.m3u8",
        },
    }
    assert bluesky._video_from_embed(embed) == {
        "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/y/thumbnail.jpg",
        "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/y/playlist.m3u8",
    }


def test_fetch_post_images_and_video_share_one_fetch(monkeypatch):
    """fetch_post_images and fetch_post_video must not each hit the API separately
    for the same at:// URI -- they share _fetch_post's cache."""
    calls = []

    def fake_fetch_post(at_uri):
        calls.append(at_uri)
        return {
            "embed": {
                "$type": "app.bsky.embed.video#view",
                "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/z/thumbnail.jpg",
                "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/z/playlist.m3u8",
            }
        }

    monkeypatch.setattr(bluesky, "_fetch_post", fake_fetch_post)
    at_uri = "at://did:plc:abc/app.bsky.feed.post/z"
    images = bluesky.fetch_post_images(at_uri)
    video = bluesky.fetch_post_video(at_uri)
    assert images == ["https://video.bsky.app/watch/did%3Aplc%3Aabc/z/thumbnail.jpg"]
    assert video == {
        "thumbnail": "https://video.bsky.app/watch/did%3Aplc%3Aabc/z/thumbnail.jpg",
        "playlist": "https://video.bsky.app/watch/did%3Aplc%3Aabc/z/playlist.m3u8",
    }
    assert calls == [at_uri, at_uri]  # each public function calls _fetch_post once; caching itself is _fetch_post's job


def test_card_from_external_embed():
    embed = {"$type": "app.bsky.embed.external#view", "external": {"uri": "https://x.test/a", "title": "T", "thumb": "https://cdn/x"}}
    assert bluesky._card_from_embed(embed) == {"uri": "https://x.test/a", "title": "T", "description": "", "thumb": "https://cdn/x"}
    assert bluesky._card_from_embed({"$type": "app.bsky.embed.images#view"}) is None


def test_card_rejects_unsafe_schemes():
    bad = {"$type": "app.bsky.embed.external#view", "external": {"uri": "javascript:alert(1)", "title": "T"}}
    assert bluesky._card_from_embed(bad) is None
    odd_thumb = {"$type": "app.bsky.embed.external#view", "external": {"uri": "https://x.test/a", "thumb": "data:image/png;base64,AA"}}
    card = bluesky._card_from_embed(odd_thumb)
    assert card is not None and card["thumb"] == ""


def test_quote_post_falls_back_to_quoted_media():
    """A quote-post with no media of its own shows the quoted post's images/video/card."""
    img = {"$type": "app.bsky.embed.images#view", "images": [{"fullsize": "https://cdn.bsky.app/q"}]}
    post = {"embed": {"$type": "app.bsky.embed.record#view", "record": {"embeds": [img]}}}
    assert bluesky._images_from_post(post) == ["https://cdn.bsky.app/q"]

    vid = {"$type": "app.bsky.embed.video#view", "thumbnail": "https://t", "playlist": "https://p"}
    post = {"embed": {"$type": "app.bsky.embed.record#view", "record": {"embeds": [vid]}}}
    assert bluesky._video_from_embed(bluesky._quoted_embeds(post["embed"])[0]) == {"thumbnail": "https://t", "playlist": "https://p"}

    # recordWithMedia nests the record one level deeper; own media still wins.
    own = {"$type": "app.bsky.embed.images#view", "images": [{"fullsize": "https://cdn.bsky.app/own"}]}
    post = {"embed": {"$type": "app.bsky.embed.recordWithMedia#view", "media": own, "record": {"record": {"embeds": [img]}}}}
    assert bluesky._images_from_post(post) == ["https://cdn.bsky.app/own"]
    assert bluesky._quoted_embeds(post["embed"]) == [img]

    # A blocked/not-found quoted record has no embeds.
    assert bluesky._images_from_post({"embed": {"$type": "app.bsky.embed.record#view", "record": {"notFound": True}}}) == []


def _quote_post(embed_type="app.bsky.embed.record#view", **rec_extra):
    rec = {"uri": "at://did:plc:x/app.bsky.feed.post/3abc", "author": {"handle": "a.bsky.social"}, **rec_extra}
    return {"embed": {"$type": embed_type, "record": rec}}


def test_fetch_post_quote(monkeypatch):
    post = _quote_post()
    monkeypatch.setattr(bluesky, "_fetch_post", lambda uri: post)
    assert bluesky.fetch_post_quote("at://did:plc:y/app.bsky.feed.post/1") == {
        "url": "https://bsky.app/profile/a.bsky.social/post/3abc",
        "handle": "a.bsky.social",
        "own_media": "",
    }
    post["embed"] = {"$type": "app.bsky.embed.images#view", "images": []}
    assert bluesky.fetch_post_quote("at://did:plc:y/app.bsky.feed.post/1") is None


def test_fetch_post_quote_rejects_and_falls_back(monkeypatch):
    uri = "at://did:plc:y/app.bsky.feed.post/1"
    cases = {
        "unsafe handle": _quote_post(author={"handle": 'x"><script>'}),
        "string author": _quote_post(author="nope"),
        "unverified handle": _quote_post(author={"handle": "handle.invalid"}),
    }
    for name, post in cases.items():
        monkeypatch.setattr(bluesky, "_fetch_post", lambda u, post=post: post)
        quote = bluesky.fetch_post_quote(uri)
        assert quote is not None, name
        assert quote["handle"] == "did:plc:x", name  # falls back to the DID
        assert quote["url"] == "https://bsky.app/profile/did:plc:x/post/3abc", name
    for bad_uri in ("at://did:plc:x/app.bsky.graph.list/3abc", "at://did:plc:x/app.bsky.feed.post/3a/b?x", ""):
        post = _quote_post(uri=bad_uri)
        monkeypatch.setattr(bluesky, "_fetch_post", lambda u, post=post: post)
        assert bluesky.fetch_post_quote(uri) is None
    blocked = {"embed": {"$type": "app.bsky.embed.record#view", "record": {"blocked": True, "author": {"did": "did:plc:x"}}}}
    monkeypatch.setattr(bluesky, "_fetch_post", lambda u: blocked)
    assert bluesky.fetch_post_quote(uri) is None


def test_own_media_is_never_displaced_by_quoted_media(monkeypatch):
    uri = "at://did:plc:y/app.bsky.feed.post/1"
    own_img = {"$type": "app.bsky.embed.images#view", "images": [{"fullsize": "https://cdn.bsky.app/own"}]}
    own_card = {"$type": "app.bsky.embed.external#view", "external": {"uri": "https://example.com/a", "title": "T"}}
    q_vid = {"$type": "app.bsky.embed.video#view", "thumbnail": "https://t", "playlist": "https://p"}
    q_img = {"$type": "app.bsky.embed.images#view", "images": [{"fullsize": "https://cdn.bsky.app/q"}]}

    def rwm(media, quoted):
        return {"embed": {"$type": "app.bsky.embed.recordWithMedia#view", "media": media, "record": {"record": {"embeds": [quoted]}}}}

    # Own images + quoted video: the video must not win.
    monkeypatch.setattr(bluesky, "_fetch_post", lambda u: rwm(own_img, q_vid))
    assert bluesky.fetch_post_video(uri) is None
    assert bluesky.fetch_post_images(uri) == ["https://cdn.bsky.app/own"]
    # Own card + quoted images: the images must not win.
    monkeypatch.setattr(bluesky, "_fetch_post", lambda u: rwm(own_card, q_img))
    assert bluesky.fetch_post_images(uri) == []
    card = bluesky.fetch_post_card(uri)
    assert card is not None and card["uri"] == "https://example.com/a"
    # No own media: the quoted post's video is used.
    post = _quote_post(embeds=[q_vid])
    monkeypatch.setattr(bluesky, "_fetch_post", lambda u: post)
    assert bluesky.fetch_post_video(uri) == {"thumbnail": "https://t", "playlist": "https://p"}


def test_malformed_quoted_data_does_not_raise():
    assert bluesky._quoted_embeds({"$type": "app.bsky.embed.record#view", "record": {"embeds": 5}}) == []
    vid = {"$type": "app.bsky.embed.video#view", "thumbnail": "t", "playlist": "p", "aspectRatio": {"width": "x", "height": 1}}
    assert bluesky._video_from_embed(vid) == {"thumbnail": "t", "playlist": "p"}
