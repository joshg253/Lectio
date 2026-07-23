"""The "Fix URLs" ingest hook: rewrite an author's old domain to the current one
on the raw feedparser result, before reader derives entry ids — so entries land
with the current-domain id and link even when the feed still emits the old host
in its <guid>/<link>.

Rewriting on the raw result (not after processing) is load-bearing: reader's id
derivation and tag/window collection all key off the raw entries, so a
post-processing rewrite would desync them."""
from __future__ import annotations

import pytest

from services import reader_sanitize


class _RawEntry(dict):
    """A minimal feedparser-entry stand-in (dict with .get)."""


class _Result:
    def __init__(self, entries):
        self.entries = entries


@pytest.fixture(autouse=True)
def _clear_provider():
    saved = reader_sanitize._url_rewrite_provider
    yield
    reader_sanitize._url_rewrite_provider = saved


def _rewrite(entries, rules):
    reader_sanitize.set_url_rewrite_provider(lambda url: rules)
    result = _Result(entries)
    reader_sanitize._rewrite_raw_urls("https://tush.ar/rss.xml", result)
    return result.entries


def test_rewrites_guid_and_link_host():
    e = _RawEntry(id="https://tushar.lol/post/x/", link="https://tushar.lol/post/x/",
                  links=[{"href": "https://tushar.lol/post/x/"}])
    _rewrite([e], [("tushar.lol", "tush.ar")])
    assert e["id"] == "https://tush.ar/post/x/"
    assert e["link"] == "https://tush.ar/post/x/"
    assert e["links"][0]["href"] == "https://tush.ar/post/x/"


def test_preserves_path_query_and_scheme():
    e = _RawEntry(id="http://sadh.life/post/y/?a=1#frag")
    _rewrite([e], [("sadh.life", "tush.ar")])
    assert e["id"] == "http://tush.ar/post/y/?a=1#frag"


def test_only_rewrites_matching_hosts():
    e = _RawEntry(id="https://other.com/post/z/", link="https://other.com/post/z/")
    _rewrite([e], [("tushar.lol", "tush.ar")])
    assert e["id"] == "https://other.com/post/z/"  # untouched


def test_multiple_aliases_map_to_one_domain():
    e1 = _RawEntry(id="https://tushar.lol/a/")
    e2 = _RawEntry(id="https://sadh.life/b/")
    _rewrite([e1, e2], [("tushar.lol", "tush.ar"), ("sadh.life", "tush.ar")])
    assert e1["id"] == "https://tush.ar/a/"
    assert e2["id"] == "https://tush.ar/b/"


def test_no_provider_is_a_noop():
    reader_sanitize._url_rewrite_provider = None
    e = _RawEntry(id="https://tushar.lol/a/")
    result = _Result([e])
    reader_sanitize._rewrite_raw_urls("https://tush.ar/rss.xml", result)
    assert e["id"] == "https://tushar.lol/a/"


def test_empty_rules_is_a_noop():
    e = _RawEntry(id="https://tushar.lol/a/")
    _rewrite([e], [])
    assert e["id"] == "https://tushar.lol/a/"


def test_host_match_is_case_insensitive_and_ignores_port():
    e = _RawEntry(id="https://TUSHAR.LOL:443/a/")
    _rewrite([e], [("tushar.lol", "tush.ar")])
    assert e["id"] == "https://tush.ar/a/"


def test_a_broken_url_is_left_alone():
    assert reader_sanitize._swap_host("not a url", {"x": "y"}) == "not a url"
