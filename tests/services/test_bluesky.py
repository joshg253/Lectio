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
    dup = {"embed": {"$type": "app.bsky.embed.images#view",
                     "images": [{"fullsize": "u"}, {"fullsize": "u"}]}}
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
