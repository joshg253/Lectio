"""Tests for add_no_referrer_to_images.

Some hosts (e.g. nanolx.org) serve a hotlink-protection placeholder when the
Referer points at a foreign origin. Suppressing the referer on inline body
images makes them load the real asset (regression: nanolx.org git.png).
"""
from __future__ import annotations

from main import add_no_referrer_to_images


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
