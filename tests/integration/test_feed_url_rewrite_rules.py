"""Managing declared domain aliases from Feed Properties.

`POST /feeds/set-website` can only seed a rule for a host it can *infer* (the
channel <link>, or the host most posts link to), so an author's older dead
domain with no surviving entries had no way into `feed_url_rewrites` at all.
These routes are that way in: add, list (via the properties payload), remove.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://tush.ar/rss.xml"


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
        reader.add_entry({
            "feed_url": FEED, "id": "https://sadh.life/post/old/",
            "link": "https://sadh.life/post/old/", "title": "on the dead domain",
        })
        reader.add_entry({
            "feed_url": FEED, "id": "https://tush.ar/post/new/",
            "link": "https://tush.ar/post/new/", "title": "on the live domain",
        })
        db = reader._storage.get_db()
        db.execute("UPDATE feeds SET link = ? WHERE url = ?", ("https://tush.ar/", FEED))
        db.commit()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/feeds/url-rewrites")(main.add_feed_url_rewrite_route)
    app.post("/feeds/url-rewrites/delete")(main.delete_feed_url_rewrite_route)
    return TestClient(app)


def _rules() -> list[tuple[str, str]]:
    return main.get_feed_url_rewrites(FEED)


def test_add_alias_migrates_existing_entries(configured):
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": "sadh.life"})
    body = r.json()
    assert body["ok"] is True and body["migrated"] == 1
    assert body["from_host"] == "sadh.life" and body["to_host"] == "tush.ar"
    assert ("sadh.life", "tush.ar") in _rules()
    with main.get_reader() as reader:
        ids = sorted(e.id for e in reader.get_entries(feed=FEED))
    assert ids == ["https://tush.ar/post/new/", "https://tush.ar/post/old/"]


def test_add_alias_for_a_domain_with_no_entries(configured):
    """The case the UI exists for: a dead domain with nothing left on it. The
    rule still has to stick — it governs ingest and the global dedupe alias map
    from here on."""
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": "tushar.bio"})
    assert r.json() == {"ok": True, "from_host": "tushar.bio", "to_host": "tush.ar", "migrated": 0}
    assert ("tushar.bio", "tush.ar") in _rules()


@pytest.mark.parametrize("raw,expected", [
    ("https://tushar.bio/", "tushar.bio"),
    ("http://www.tushar.bio", "tushar.bio"),
    ("  TUSHAR.BIO  ", "tushar.bio"),
    ("tushar.bio:8080", "tushar.bio"),
])
def test_add_alias_accepts_pasted_urls(configured, raw, expected):
    """Users copy what's in the address bar, so a full URL has to work as
    readily as a bare host — and `www.` is dropped to match how
    get_dedupe_host_aliases stores and compares keys."""
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": raw})
    assert r.json()["from_host"] == expected


@pytest.mark.parametrize("bad", ["   ", "not a domain", "nodot", "/path/only"])
def test_add_alias_rejects_junk(configured, bad):
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": bad})
    assert r.status_code == 400 and r.json()["ok"] is False
    assert _rules() == []


def test_add_alias_rejects_an_empty_host(configured):
    """An omitted field is FastAPI's 422 rather than our 400 — either way it is
    refused and stores nothing. The UI never sends this (the add button returns
    early on an empty input); this pins the server's own floor."""
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": ""})
    assert r.status_code in (400, 422)
    assert _rules() == []


def test_add_alias_rejects_the_feeds_own_domain(configured):
    """Aliasing a host to itself would make every link rewrite a no-op loop."""
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": "tush.ar"})
    assert r.status_code == 400 and "already this feed" in r.json()["error"]
    assert _rules() == []


def test_add_alias_rejects_unknown_feed(configured):
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={"feed_url": "https://nope.test/rss", "from_host": "old.test"})
    assert r.status_code == 404


def test_explicit_to_host_overrides_the_website(configured):
    with _client() as c:
        r = c.post("/feeds/url-rewrites", data={
            "feed_url": FEED, "from_host": "sadh.life", "to_host": "https://elsewhere.test/",
        })
    assert r.json()["to_host"] == "elsewhere.test"


def test_delete_alias(configured):
    with _client() as c:
        c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": "tushar.bio"})
        assert ("tushar.bio", "tush.ar") in _rules()
        r = c.post("/feeds/url-rewrites/delete", data={"feed_url": FEED, "from_host": "tushar.bio"})
    assert r.json()["ok"] is True
    assert _rules() == []


def test_delete_unknown_alias_is_a_404(configured):
    with _client() as c:
        r = c.post("/feeds/url-rewrites/delete", data={"feed_url": FEED, "from_host": "never.set"})
    assert r.status_code == 404


def test_properties_payload_exposes_the_rules(configured):
    """The list has to be visible somewhere — until this, the rules acted
    invisibly at ingest and in the dedupe alias map."""
    with _client() as c:
        c.post("/feeds/url-rewrites", data={"feed_url": FEED, "from_host": "tushar.bio"})
    props = main.get_feed_properties(FEED)
    assert {"from_host": "tushar.bio", "to_host": "tush.ar"} in props["url_rewrites"]
