"""podcast_audio.extract_media_audio recovers audio that lives only in
``<media:content>`` / ``<media:group>`` — which the reader library drops."""
from __future__ import annotations

from services import podcast_audio


def _feed(items: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
        "<channel><title>T</title>" + items + "</channel></rss>"
    )


def _item(guid: str, media: str = "", link: str = "") -> str:
    link_el = f"<link>{link}</link>" if link else ""
    guid_el = f"<guid>{guid}</guid>" if guid else ""
    return f"<item><title>x</title>{link_el}{guid_el}{media}</item>"


def test_typed_audio_media_content():
    feed = _feed(_item("g1", '<media:content url="https://cdn.test/a.mp3" type="audio/mpeg"/>'))
    assert podcast_audio.extract_media_audio(feed) == {"g1": "https://cdn.test/a.mp3"}


def test_medium_audio_without_type():
    feed = _feed(_item("g1", '<media:content url="https://cdn.test/a.bin" medium="audio"/>'))
    assert podcast_audio.extract_media_audio(feed) == {"g1": "https://cdn.test/a.bin"}


def test_extension_only_media_content():
    feed = _feed(_item("g1", '<media:content url="https://cdn.test/a.m4a"/>'))
    assert podcast_audio.extract_media_audio(feed) == {"g1": "https://cdn.test/a.m4a"}


def test_media_group_is_flattened():
    feed = _feed(_item("g1", '<media:group><media:content url="https://cdn.test/g.mp3"/></media:group>'))
    assert podcast_audio.extract_media_audio(feed) == {"g1": "https://cdn.test/g.mp3"}


def test_image_and_video_media_are_ignored():
    feed = _feed(
        _item("g1", '<media:content url="https://cdn.test/c.jpg" type="image/jpeg" medium="image"/>')
        + _item("g2", '<media:content url="https://cdn.test/v.mp4" medium="video"/>')
    )
    assert podcast_audio.extract_media_audio(feed) == {}


def test_audio_ext_with_query_string():
    feed = _feed(_item("g1", '<media:content url="https://cdn.test/a.mp3?token=xyz"/>'))
    assert podcast_audio.extract_media_audio(feed) == {"g1": "https://cdn.test/a.mp3?token=xyz"}


def test_falls_back_to_link_when_no_guid():
    feed = _feed(_item("", '<media:content url="https://cdn.test/a.mp3" type="audio/mpeg"/>',
                       link="https://x.test/ep"))
    assert podcast_audio.extract_media_audio(feed) == {"https://x.test/ep": "https://cdn.test/a.mp3"}


def test_entry_without_media_is_omitted():
    feed = _feed(_item("g1") + _item("g2", '<media:content url="https://cdn.test/a.mp3" type="audio/mpeg"/>'))
    assert podcast_audio.extract_media_audio(feed) == {"g2": "https://cdn.test/a.mp3"}


def test_empty_feed():
    assert podcast_audio.extract_media_audio(_feed("")) == {}
