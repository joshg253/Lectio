"""youtube_embeds recovers video ids from raw feed content that feedparser
strips (it removes the embed <iframe>). It must read the iframe src, bare
watch/share URLs, and youtu.be links, in document order, and report a YouTube
embed with no recoverable id as an empty list (a cacheable negative)."""
from __future__ import annotations

from services import youtube_embeds as y

_RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>T</title>
{items}
</channel></rss>"""


def _feed(*item_bodies: str) -> bytes:
    items = "".join(
        f"<item><guid>e{i}</guid><content:encoded><![CDATA[{body}]]></content:encoded></item>"
        for i, body in enumerate(item_bodies)
    )
    return _RSS.format(items=items).encode()


def test_recovers_id_from_stripped_iframe():
    body = ('<figure class="wp-block-embed is-provider-youtube wp-block-embed-youtube">'
            '<div class="wp-block-embed__wrapper">'
            '<iframe src="https://www.youtube.com/embed/weFUWLfaP28?rel=1"></iframe>'
            '</div></figure>')
    out = y.extract_youtube_embeds(_feed(body))
    assert out == {"e0": ["weFUWLfaP28"]}


def test_recovers_from_watch_and_short_urls():
    out = y.extract_youtube_embeds(_feed(
        '<p>see https://www.youtube.com/watch?v=abcdefghijk here</p>',
        '<p><a href="https://youtu.be/ABCDEFGHIJK">vid</a></p>',
    ))
    assert out == {"e0": ["abcdefghijk"], "e1": ["ABCDEFGHIJK"]}


def test_multiple_ids_in_document_order_deduped():
    body = ('https://youtu.be/aaaaaaaaaaa '
            'https://www.youtube.com/embed/bbbbbbbbbbb '
            'https://youtu.be/aaaaaaaaaaa')  # dup
    out = y.extract_youtube_embeds(_feed(body))
    assert out == {"e0": ["aaaaaaaaaaa", "bbbbbbbbbbb"]}


def test_youtube_marker_without_id_is_negative():
    # A YouTube block whose id can't be parsed still records a (negative) result.
    body = '<figure class="wp-block-embed-youtube"><div class="wp-block-embed__wrapper"></div></figure>'
    out = y.extract_youtube_embeds(_feed(body))
    assert out == {"e0": []}


def test_non_youtube_entries_omitted():
    out = y.extract_youtube_embeds(_feed('<p>just text, no video</p>'))
    assert out == {}


def test_recovers_nocookie_host_embed():
    # Privacy-host embeds (youtube-nocookie.com, e.g. ArtStation) must be
    # recognized — the marker previously only matched youtube.com.
    body = ('<div class="video-wrapper media-asset-container">'
            '<iframe src="https://www.youtube-nocookie.com/embed/oDMjofFNLSk?feature=oembed"></iframe>'
            '</div>')
    out = y.extract_youtube_embeds(_feed(body))
    assert out == {"e0": ["oDMjofFNLSk"]}
