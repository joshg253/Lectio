import pytest

from services.redirects import local_url


@pytest.mark.parametrize(
    "url",
    ["/", "/?folder_id=3", "/?folder_id=3&message=Done+%26+dusted", "/read?scope=feeds", "/a/b?next=%2F%2Fevil.com"],
)
def test_same_origin_paths_pass_through(url):
    assert local_url(url) == url


@pytest.mark.parametrize(
    "url",
    [None, "", "evil.com", "https://evil.com/x", "//evil.com", "/\\evil.com", "/\r\nSet-Cookie: a=b", "/ok\tthen", "javascript:alert(1)"],
)
def test_anything_else_becomes_root(url):
    assert local_url(url) == "/"
