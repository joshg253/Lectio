"""Readability strips all <iframe> embeds and sometimes keeps the lead image
twice. _reinject_readability_embeds recovers allowlisted players from the raw
page; _dedupe_readability_images drops the duplicate <img>."""
from __future__ import annotations

import main

RAW = (
    "<html><body>"
    '<iframe src="https://open.spotify.com/embed/track/abc"></iframe>'
    '<iframe src="https://www.youtube.com/embed/xyz"></iframe>'
    '<iframe src="https://evil.test/track"></iframe>'
    "</body></html>"
)


def test_reinjects_allowlisted_embeds():
    out = main._reinject_readability_embeds("<p>body</p>", RAW)
    assert "open.spotify.com/embed/track/abc" in out
    assert "youtube.com/embed/xyz" in out
    # Off-allowlist host is not re-injected.
    assert "evil.test" not in out


def test_skips_embed_already_present():
    summary = '<p>x</p><iframe src="https://open.spotify.com/embed/track/abc"></iframe>'
    out = main._reinject_readability_embeds(summary, RAW)
    # Spotify already present → only YouTube is appended.
    assert out.count("open.spotify.com/embed/track/abc") == 1
    assert "youtube.com/embed/xyz" in out


def test_noop_when_no_iframes():
    assert main._reinject_readability_embeds("<p>x</p>", "<p>no embeds</p>") == "<p>x</p>"


def test_dedupes_repeated_lead_image():
    html = '<img src="https://x/a.jpg"/><p>t</p><img src="https://x/a.jpg?w=2"/>'
    out = main._dedupe_readability_images(html)
    assert out.count("<img") == 1
    assert "<p>t</p>" in out


def test_keeps_distinct_images():
    html = '<img src="https://x/a.jpg"/><img src="https://x/b.jpg"/>'
    assert main._dedupe_readability_images(html).count("<img") == 2


def test_absolutizes_relative_media_urls():
    """Reader view is served from Lectio's origin, so relative image/link URLs
    (e.g. fabiensanglard.net's page-relative `model_m.webp`) must be resolved
    against the source page or they 404."""
    html = (
        '<article><img src="model_m.webp">'
        '<img src="../2168/keyboard/the_precious.webp">'
        '<a href="more.html">link</a></article>'
    )
    out = main._absolutize_article_urls(html, "https://fabiensanglard.net/keyboards/index.html")
    assert 'src="https://fabiensanglard.net/keyboards/model_m.webp"' in out
    assert 'src="https://fabiensanglard.net/2168/keyboard/the_precious.webp"' in out
    assert 'href="https://fabiensanglard.net/keyboards/more.html"' in out


def test_absolutize_leaves_absolute_urls_untouched():
    html = '<article><img src="https://cdn.example/a.png"><a href="https://x.test/p">l</a></article>'
    out = main._absolutize_article_urls(html, "https://fabiensanglard.net/keyboards/index.html")
    assert 'src="https://cdn.example/a.png"' in out
    assert 'href="https://x.test/p"' in out


def test_unwraps_wayback_image_proxy_url():
    """An archive-fallback capture (mode="archive", or the auto mismatch/dead
    fallback) resolves relative image src against the snapshot URL, producing
    im_-wrapped web.archive.org URLs. Raised 2026-08-31 against two live
    beehiiv entries: the im_ proxy 404'd while the URL it wraps, fetched
    directly, returned 200 image/jpeg -- unwrap it rather than store the
    fragile proxy."""
    html = (
        '<figure><img src="http://web.archive.org/web/20251005150628im_/'
        'https://beehiiv-images-production.s3.amazonaws.com/uploads/asset/file/'
        'x/max.jpg?t=1759359474"></figure>'
    )
    out = main._unwrap_wayback_image_urls(html)
    assert (
        'src="https://beehiiv-images-production.s3.amazonaws.com/uploads/asset/file/'
        'x/max.jpg?t=1759359474"' in out
    )
    assert "web.archive.org" not in out


def test_unwrap_wayback_is_a_noop_without_it():
    html = '<img src="https://cdn.example/a.png">'
    assert main._unwrap_wayback_image_urls(html) == html


def test_unwrap_wayback_leaves_a_non_image_wayback_link_alone():
    """Only a wrapped src/href *value* (the whole quoted attribute is the
    wrapper) is unwrapped -- a plain link mentioning web.archive.org some
    other way is left untouched."""
    html = '<a href="https://web.archive.org/">Wayback home</a>'
    assert main._unwrap_wayback_image_urls(html) == html
