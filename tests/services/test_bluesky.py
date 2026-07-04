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


def test_images_dedup_and_empty():
    assert bluesky._images_from_post({}) == []
    assert bluesky._images_from_post({"embed": {"$type": "app.bsky.embed.external#view"}}) == []
    dup = {"embed": {"$type": "app.bsky.embed.images#view",
                     "images": [{"fullsize": "u"}, {"fullsize": "u"}]}}
    assert bluesky._images_from_post(dup) == ["u"]


def test_fetch_post_images_rejects_non_at_uri():
    assert bluesky.fetch_post_images("") == []
    assert bluesky.fetch_post_images("https://bsky.app/x") == []
