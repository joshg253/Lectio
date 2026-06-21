"""_img_cache_key_url strips per-request signing params so signed-CDN images
(GitHub private-user-images JWT, wixmp/S3 tokens) stay cache-resident across token
rotations and keep loading after the original short-lived URL expires."""
from __future__ import annotations

import main


def test_github_jwt_stripped():
    base = "https://private-user-images.githubusercontent.com/5920850/610797733-x.png"
    assert main._img_cache_key_url(base + "?jwt=AAA") == base
    assert main._img_cache_key_url(base + "?jwt=AAA") == main._img_cache_key_url(base + "?jwt=BBB")


def test_amz_signing_params_stripped_identity_kept():
    u = "https://x.s3.amazonaws.com/a.jpg?X-Amz-Signature=zzz&X-Amz-Date=20260101&w=200"
    assert main._img_cache_key_url(u) == "https://x.s3.amazonaws.com/a.jpg?w=200"


def test_no_query_unchanged():
    u = "https://cdn.test/a.jpg"
    assert main._img_cache_key_url(u) == u


def test_github_private_images_is_hotlink_host():
    assert main._is_hotlink_img_host("private-user-images.githubusercontent.com")
