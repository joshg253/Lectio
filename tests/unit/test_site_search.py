"""The `site:<host>` search operator scopes a query to entries on a given link
host — precise where a bare host term also matches the host in an article's body
or in another feed's post. It backs the File-Saved "review this host" link, which
opens the Saved Articles feed filtered to a multi-feed host's unfiled saves.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main


def _host(link: str) -> str:
    return main._entry_link_site_host(SimpleNamespace(link=link))


@pytest.mark.parametrize("link,expected", [
    ("https://www.freecodecamp.org/news/x", "freecodecamp.org"),   # www folded
    ("https://freecodecamp.org/news/x", "freecodecamp.org"),
    ("http://user@host.example.com:8080/p", "host.example.com"),   # userinfo/port stripped
    ("HTTPS://Medium.COM/@a/post", "medium.com"),                  # lowercased
    ("", ""),
])
def test_entry_link_site_host(link, expected):
    assert _host(link) == expected


def test_split_pulls_site_tokens_out():
    regular, sites = main._split_site_terms(["site:medium.com", "kotlin", "site:ars.example"])
    assert regular == ["kotlin"]
    assert sites == ["medium.com", "ars.example"]


def test_split_normalizes_the_host():
    _r, sites = main._split_site_terms(["site:www.freecodecamp.org/news"])
    assert sites == ["freecodecamp.org"]          # www folded, path dropped


def test_bare_site_token_is_ignored():
    regular, sites = main._split_site_terms(["site:", "real"])
    assert regular == ["site:", "real"] and sites == []


def _matches(link: str, host: str) -> bool:
    h = _host(link)
    return h == host or h.endswith("." + host)


@pytest.mark.parametrize("link,host,ok", [
    ("https://freecodecamp.org/x", "freecodecamp.org", True),      # apex
    ("https://www.freecodecamp.org/x", "freecodecamp.org", True),  # subdomain (www)
    ("https://blog.medium.com/x", "medium.com", True),             # subdomain
    ("https://notfreecodecamp.org/x", "freecodecamp.org", False),  # not a suffix boundary
    ("https://freecodecamp.org.evil.com/x", "freecodecamp.org", False),  # suffix hijack
    ("https://medium.com.other.net/x", "medium.com", False),
])
def test_site_host_boundary_matching(link, host, ok):
    assert _matches(link, host) is ok
