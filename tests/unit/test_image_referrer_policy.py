"""Tests for add_no_referrer_to_images.

Some hosts (e.g. nanolx.org) serve a hotlink-protection placeholder when the
Referer points at a foreign origin. Suppressing the referer on inline body
images makes them load the real asset (regression: nanolx.org git.png).
"""
from __future__ import annotations

from main import (
    _is_hotlink_img_host,
    _lead_image_display_url,
    add_no_referrer_to_images,
    proxy_hotlink_images,
)


def test_adds_referrerpolicy_to_plain_img():
    out = add_no_referrer_to_images('<img src="https://nanolx.org/wp-content/uploads/git.png">')
    assert 'referrerpolicy="no-referrer"' in out


def test_adds_referrerpolicy_to_self_closing_img():
    out = add_no_referrer_to_images('<p><img src="https://x/a.png" width="128"/></p>')
    assert 'referrerpolicy="no-referrer"' in out
    # Self-closing slash is preserved.
    assert "/>" in out


def test_does_not_duplicate_existing_referrerpolicy():
    src = '<img src="https://x/a.png" referrerpolicy="origin">'
    out = add_no_referrer_to_images(src)
    assert out.lower().count("referrerpolicy") == 1
    # The existing value is left untouched.
    assert 'referrerpolicy="origin"' in out


def test_leaves_non_img_tags_alone():
    src = '<a href="https://x"><img src="https://x/a.png"></a><br>'
    out = add_no_referrer_to_images(src)
    assert out.count("referrerpolicy") == 1
    assert "<br>" in out
    assert '<a href="https://x">' in out


# --- hotlink-host /api/img proxying ---


def test_hotlink_host_matching():
    assert _is_hotlink_img_host("nanolx.org")
    assert _is_hotlink_img_host("www.nanolx.org")
    assert _is_hotlink_img_host("nanolx.org:443")
    # A different domain that merely ends in the same letters is not matched.
    assert not _is_hotlink_img_host("notnanolx.org")
    assert not _is_hotlink_img_host("example.com")


def test_proxy_rewrites_hotlink_host_img_to_api_img():
    src = '<p><img alt="git" src="https://nanolx.org/wp-content/uploads/git.png" width="128"/></p>'
    out = proxy_hotlink_images(src)
    assert "/api/img?u=https%3A%2F%2Fnanolx.org%2Fwp-content%2Fuploads%2Fgit.png" in out
    # Original direct URL no longer used as the src.
    assert 'src="https://nanolx.org' not in out


def test_proxy_drops_srcset_for_hotlink_host():
    src = (
        '<img src="https://nanolx.org/a.png" '
        'srcset="https://nanolx.org/a-150.png 150w, https://nanolx.org/a-300.png 300w">'
    )
    out = proxy_hotlink_images(src)
    assert "srcset" not in out.lower()
    assert "/api/img?u=" in out


def test_proxy_leaves_other_hosts_untouched():
    src = '<img src="https://example.com/hero.jpg"><img src="data:image/png;base64,AAAA">'
    out = proxy_hotlink_images(src)
    assert out == src


def test_lead_image_display_url_proxies_hotlink_host():
    out = _lead_image_display_url("https://nanolx.org/wp-content/uploads/utilities-terminal.png")
    assert out is not None and out.startswith("/api/img?u=")
