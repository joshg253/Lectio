"""Edit a feed's Website in Feed Properties → seed a Fix-URLs host-rewrite rule
and migrate existing posts onto the new domain immediately.

The front door to the feed_url_rewrites engine: an author who moved domains (and
whose feed still advertises the dead one) is fixed by editing the Website, which
rewrites the post links/ids, carries the star/tag/read state, and records the
rule so re-ingested items and the link-rebase stay corrected.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://tush.ar/rss.xml"
OLD = "https://tushar.lol/post/pyc/"
OLD2 = "https://tushar.lol/post/dunders/"
NEW = "https://tush.ar/post/pyc/"
NEW2 = "https://tush.ar/post/dunders/"


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.ensure_starred_archive_schema()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _seed_old_domain_posts():
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        for eid in (OLD, OLD2):
            reader.add_entry({
                "feed_url": FEED, "id": eid, "link": eid,
                "title": eid.rsplit("/", 2)[-2],
                "published": datetime(2020, 1, 1, tzinfo=timezone.utc),
            })
        # Star + read the first, manually tag the second.
        reader.mark_entry_as_read((FEED, OLD))
        reader.set_tag((FEED, OLD2), f"{main.MANUAL_TAG_KEY_PREFIX}keep")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
            (FEED, OLD, "2020-01-01T00:00:00"),
        )
        conn.commit()


def _app():
    app = FastAPI()
    app.post("/feeds/set-website")(main.set_feed_website_route)
    return app


def test_edit_website_migrates_posts_and_seeds_rule(tenant):
    _seed_old_domain_posts()
    with TestClient(_app()) as client:
        r = client.post("/feeds/set-website", data={"feed_url": FEED, "website": "https://tush.ar/"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["from_host"] == "tushar.lol" and body["to_host"] == "tush.ar"
    assert body["migrated"] == 2

    with main.get_reader() as reader:
        # Old ids gone, new ones present.
        assert reader.get_entry((FEED, OLD), None) is None
        assert reader.get_entry((FEED, OLD2), None) is None
        assert reader.get_entry((FEED, NEW), None) is not None
        assert reader.get_entry((FEED, NEW2), None) is not None
        # Read state and manual tag rode along.
        assert reader.get_entry((FEED, NEW)).read is True
        tags = [t[0] if isinstance(t, tuple) else t for t in reader.get_entry((FEED, NEW2)).resource_id and reader.get_tags((FEED, NEW2))]
        assert any(str(t).endswith("keep") for t in tags)

    with main.get_meta_connection() as conn:
        star = conn.execute(
            "SELECT entry_id FROM saved_entries WHERE feed_url = ?", (FEED,)
        ).fetchall()
        assert [row[0] for row in star] == [NEW]  # star re-keyed, old removed
        rule = conn.execute(
            "SELECT from_host, to_host FROM feed_url_rewrites WHERE feed_url = ?", (FEED,)
        ).fetchone()
        assert tuple(rule) == ("tushar.lol", "tush.ar")


def test_edit_website_to_same_domain_is_a_noop(tenant):
    _seed_old_domain_posts()
    with TestClient(_app()) as client:
        r = client.post("/feeds/set-website", data={"feed_url": FEED, "website": "https://tushar.lol/"})
    assert r.json() == {"ok": True, "website": "https://tushar.lol/", "migrated": 0, "unchanged": True}
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, OLD), None) is not None  # nothing migrated


def test_edit_website_rejects_a_bad_url(tenant):
    _seed_old_domain_posts()
    with TestClient(_app()) as client:
        r = client.post("/feeds/set-website", data={"feed_url": FEED, "website": "not-a-url"})
    assert r.status_code == 400
    assert r.json()["ok"] is False
