"""Avatar heuristics must not eat a post whose title happens to contain a hint word.

"round" is a *shape* hint for a cropped avatar (avatar-round.png). It used to be
matched against the whole URL path, so Standard Ebooks' "The Third Round" lost
its cover: the image at

    …/ebooks/h-c-mcneile/the-third-round/downloads/cover.jpg

is a 1400x2100 JPEG, and the word "round" was matching in a *directory* segment
that is a book title. The lead-image pipeline then cached a permanent negative,
so the entry showed no image while every other book in the same feed was fine.

Same class of false positive the "profile" guard in the pattern already
documents (DeviantArt filenames carrying "profile" as a title word).
"""
from __future__ import annotations

import pytest

from services.lead_images import LeadImageService as S


@pytest.mark.parametrize("url", [
    "https://standardebooks.org/ebooks/h-c-mcneile/the-third-round/downloads/cover.jpg",
    "https://example.com/blog/2025/01/05/clamp-round-2/hero.jpg",
    "https://example.com/the-third-round/photo.jpg",
    "https://example.com/final-round/image.png",
])
def test_a_title_slug_containing_round_is_not_an_avatar(url):
    assert S._looks_like_avatar_url(url) is False


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/avatar-round.png",
    "https://cdn.example.com/u/round.jpg",
    "https://cdn.example.com/user_round.jpeg",
])
def test_a_round_shaped_avatar_filename_is_still_rejected(url):
    """The hint keeps working where it was actually meant to: the filename."""
    assert S._looks_like_avatar_url(url) is True


@pytest.mark.parametrize("url", [
    "https://example.com/avatar/x.jpg",
    "https://example.com/gravatar/x.jpg",
    "https://example.com/author-image/x.jpg",
    "https://example.com/headshot.jpg",
])
def test_the_other_avatar_hints_are_unchanged(url):
    assert S._looks_like_avatar_url(url) is True


def test_round_in_a_directory_does_not_rescue_a_real_avatar_filename():
    """Both halves are checked — a title-ish directory does not launder a
    filename that is still plainly an avatar."""
    assert S._looks_like_avatar_url(
        "https://example.com/the-third-round/avatar.jpg") is True
