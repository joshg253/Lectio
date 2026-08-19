"""Change Feed URL validates the target like Add Feed (unless forced): a URL
that doesn't resolve to a feed returns needs_confirm; force bypasses; a page
that advertises a feed resolves to the discovered feed URL."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed.xml"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/feeds/change-url")(main.change_feed_url_route)
    return TestClient(app)


def _patch_probe(monkeypatch, result):
    from services import feed_discovery
    monkeypatch.setattr(feed_discovery, "probe_url", lambda url, **kw: result)


def test_non_feed_url_needs_confirm(configured, monkeypatch):
    _patch_probe(monkeypatch, {"status": "none", "feeds": [], "message": "No RSS/Atom feed found."})
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": "https://example.test/about"})
    assert r.status_code == 422
    body = r.json()
    assert body["needs_confirm"] is True and "Change anyway" in body["error"]
    # The feed URL is unchanged.
    with main.get_reader() as reader:
        assert reader.get_feed(FEED, None) is not None


def test_force_bypasses_validation(configured, monkeypatch):
    # probe would say "no feed", but force skips it entirely.
    _patch_probe(monkeypatch, {"status": "none", "feeds": []})
    new = "https://auth-walled.test/feed"
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": new, "force": "1"})
    assert r.status_code == 200 and r.json()["new_url"] == new
    with main.get_reader() as reader:
        assert reader.get_feed(new, None) is not None
        assert reader.get_feed(FEED, None) is None


def test_resolves_discovered_feed_from_page(configured, monkeypatch):
    discovered = "https://example.test/discovered.xml"
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": discovered, "title": None}]})
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": "https://example.test/blog"})
    assert r.status_code == 200 and r.json()["new_url"] == discovered
    with main.get_reader() as reader:
        assert reader.get_feed(discovered, None) is not None


def test_schemeless_input_gets_https(configured, monkeypatch):
    captured = {}

    def _probe(url, **kw):
        captured["url"] = url
        return {"status": "feed", "feeds": [{"url": url, "title": None}]}

    from services import feed_discovery
    monkeypatch.setattr(feed_discovery, "probe_url", _probe)
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": "example.test/other.xml"})
    assert r.status_code == 200
    assert captured["url"] == "https://example.test/other.xml"


# --- moving a feed to a new host takes the site identity with it ------------


def _add_to_folder(feed_url: str) -> int:
    with main.get_meta_connection() as conn:
        folder_id = main.get_root_folder_id(conn)
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (folder_id, feed_url),
        )
    return folder_id


def test_host_change_seeds_the_alias_rule(configured, monkeypatch):
    """A feed that moved host moved its site too. Everything downstream — the
    entry-link rebase, the dupe scan, the favicon, re-fetch, and the Website
    shown in Properties — reads feed_url_rewrites, so one rule fixes them all.
    Leaving it to be added by hand meant the Website kept naming a dead domain,
    which does not look like something the URL change caused."""
    new_url = "https://www.newhost.test/feed"
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": new_url}]})

    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": new_url})

    assert r.status_code == 200, r.text
    body = r.json()
    # `www.` is folded by _normalize_alias_host — the alias map is host-identity,
    # and www/non-www is the same site.
    assert body["alias"]["from_host"] == "example.test"
    assert body["alias"]["to_host"] == "newhost.test"
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT from_host, to_host FROM feed_url_rewrites WHERE feed_url = ?", (new_url,)
        ).fetchone()
    assert (row["from_host"], row["to_host"]) == ("example.test", "newhost.test")


def test_same_host_change_seeds_nothing(configured, monkeypatch):
    """Only a HOST move implies the site moved; a path change on the same host
    must not declare the host an alias of itself."""
    new_url = "https://example.test/other-feed.xml"
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": new_url}]})

    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": new_url})

    assert r.json()["alias"] is None
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM feed_url_rewrites WHERE feed_url = ?", (new_url,)
        ).fetchone()[0] == 0


def test_existing_alias_is_not_clobbered(configured, monkeypatch):
    """A rule the user already declared outranks the one we would seed."""
    new_url = "https://www.newhost.test/feed"
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO feed_url_rewrites (feed_url, from_host, to_host) VALUES (?, ?, ?)",
            (new_url, "example.test", "hand.declared.test"),
        )
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": new_url}]})

    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": new_url})

    assert r.json()["alias"] is None, "seeded over a rule the user already had"
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT to_host FROM feed_url_rewrites WHERE feed_url = ? AND from_host = ?",
            (new_url, "example.test"),
        ).fetchone()[0] == "hand.declared.test"


def test_response_carries_the_folder_so_the_tree_can_select_it(configured, monkeypatch):
    """Without this the client redirected to ?list_feed_url=… alone, and a feed
    URL with no folder_id leaves the sidebar nothing to select — the feed opens
    in the list but is invisible in the tree, with no way back to its menu."""
    new_url = "https://www.newhost.test/feed"
    folder_id = _add_to_folder(FEED)
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": new_url}]})

    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": new_url})

    assert r.json()["folder_id"] == folder_id


def test_old_host_is_reported_for_the_alias_prefill(configured, monkeypatch):
    new_url = "https://www.newhost.test/feed"
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": new_url}]})

    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": new_url})

    assert r.json()["old_host"] == "example.test"


# --- a page that advertises someone else's feed must not be adopted silently --


def test_cross_host_discovery_needs_confirm(configured, monkeypatch):
    """Pasting a section page whose HTML advertises the network-wide feed used to
    swap the subscription for that feed AND seed a host-alias rule, with no
    prompt — a music-section URL silently became the whole publisher network."""
    network_feed = "https://publisher.test/posts.atom"
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": network_feed}]})
    with _client() as c:
        r = c.post("/feeds/change-url",
                   data={"old_url": FEED, "new_url": "https://music.publisher.test/c/instruments"})
    assert r.status_code == 422
    body = r.json()
    assert body["needs_confirm"] is True
    assert body["resolved_url"] == network_feed
    with main.get_reader() as reader:                    # nothing changed yet
        assert reader.get_feed(FEED, None) is not None
        assert reader.get_feed(network_feed, None) is None
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT count(*) FROM feed_url_rewrites").fetchone()[0] == 0


def test_confirming_adopts_the_resolved_feed(configured, monkeypatch):
    """The client re-posts the resolved URL with force — not the pasted page."""
    network_feed = "https://publisher.test/posts.atom"
    _patch_probe(monkeypatch, {"status": "none", "feeds": []})   # force skips the probe
    with _client() as c:
        r = c.post("/feeds/change-url",
                   data={"old_url": FEED, "new_url": network_feed, "force": "1"})
    assert r.status_code == 200 and r.json()["new_url"] == network_feed


def test_direct_feed_redirecting_across_hosts_still_resolves_silently(configured, monkeypatch):
    """A feed URL that 301s to another host is the same feed under a new address —
    the canonical-URL case, which must not start prompting."""
    moved = "https://newhost.test/feed.xml"
    _patch_probe(monkeypatch, {"status": "feed", "direct": True, "feeds": [{"url": moved}]})
    with _client() as c:
        r = c.post("/feeds/change-url",
                   data={"old_url": FEED, "new_url": "https://oldhost.test/feed.xml"})
    assert r.status_code == 200 and r.json()["new_url"] == moved


def test_resolving_onto_an_existing_subscription_names_that_url(configured, monkeypatch):
    other = "https://example.test/other.xml"
    with main.get_reader() as reader:
        reader.add_feed(other, exist_ok=True)
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": other}]})
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": "https://example.test/blog"})
    assert r.status_code == 409
    assert other in r.json()["error"]
