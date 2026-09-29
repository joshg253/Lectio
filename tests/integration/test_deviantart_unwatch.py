"""The ✕ on a DeviantArt Watch-feed byline un-Watches the artist on DeviantArt, then keeps, marks read, or purges their posts
already in the Watch feed. Posts are matched by link, not byline (most older posts have no author), purge spares starred and
tagged posts, and the byline falls back to the DA store / link when reader never got an author."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from _tenancy_helpers import configure_test_tenancy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import main
import routes.entries
import routes.home
from routes import integrations_deviantart as deviantart_routes
from services import deviantart as deviantart_service
from services import tenancy

_WID = "0f9d2c1e-3b4a-4c5d-8e6f-7a8b9c0d1e2f"

# (id, link, author) — "zoe" posts and one lookalike ("zoe_2") that a LIKE-only match would catch.
_POSTS = [
    ("z1", "https://www.deviantart.com/zoe/art/One-1", "zoe"),
    ("z2", "https://www.deviantart.com/zoe/art/Two-2", ""),
    ("z3", "https://www.deviantart.com/Zoe/art/Three-3", ""),
    ("o1", "https://www.deviantart.com/zoe_2/art/Other-4", "zoe_2"),
    ("o2", "https://www.deviantart.com/bob/art/Bob-5", "bob"),
]


def _rss(posts) -> str:
    items = "".join(
        f"<item><guid>{i}</guid><title>{i}</title><link>{link}</link>{f'<author>{a}</author>' if a else ''}"
        f"<pubDate>Mon, 01 Sep 2026 00:00:0{n} +0000</pubDate></item>"
        for n, (i, link, a) in enumerate(posts)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>DA Watching</title><link>https://www.deviantart.com</link>{items}</channel></rss>'


@pytest.fixture
def watch_feed(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    deviantart_service.init(tmp_path)
    monkeypatch.setattr(deviantart_routes, "get_deviantart_user_token", lambda: "user-token")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_feeds (id, username, feed_title, created_at, source) VALUES (?, 'deviantsyouwatch', 'W', '', 'watch')",
            (_WID,),
        )
    url = deviantart_service.feed_file_url(_WID)
    (tmp_path / "deviantart-feeds" / f"{_WID}.xml").write_text(_rss(_POSTS))
    with main.get_reader() as reader:
        reader.add_feed(url, exist_ok=True)
        reader.update_feed(url)
    try:
        yield url
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _ids(url, **kw) -> set[str]:
    with main.get_reader() as reader:
        return {e.id for e in reader.get_entries(feed=url, **kw)}


def test_keep_leaves_posts_alone(watch_feed):
    assert main.apply_deviantart_unwatch_posts_action("zoe", "keep") == 0
    assert _ids(watch_feed, read=False) == {"z1", "z2", "z3", "o1", "o2"}


def test_read_marks_only_that_artists_posts_by_link(watch_feed):
    assert main.apply_deviantart_unwatch_posts_action("zoe", "read") == 3
    assert _ids(watch_feed, read=False) == {"o1", "o2"}


def test_purge_spares_starred_and_tagged(watch_feed):
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, 'z1', '')", (watch_feed,))
    with main.get_reader() as reader:
        reader.set_tag((watch_feed, "z2"), main.MANUAL_TAG_KEY_PREFIX + "keepme")
    assert main.apply_deviantart_unwatch_posts_action("zoe", "purge") == 1
    assert _ids(watch_feed) == {"z1", "z2", "o1", "o2"}
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT entry_id FROM deleted_entries").fetchall()[0][0] == "z3"


def test_route_unwatches_then_applies_the_choice(watch_feed, monkeypatch):
    calls = []
    monkeypatch.setattr(deviantart_service, "unwatch_user", lambda tok, u: (calls.append(u), (True, "ok"))[1])
    resp = deviantart_routes.deviantart_unwatch_route(username="zoe", posts="read")
    assert json.loads(resp.body) == {"ok": True, "username": "zoe", "posts": "read", "count": 3, "was_watching": True}
    assert calls == ["zoe"]


def test_route_leaves_posts_alone_when_unwatch_fails(watch_feed, monkeypatch):
    monkeypatch.setattr(deviantart_service, "unwatch_user", lambda tok, u: (False, "HTTP 400"))
    monkeypatch.setattr(deviantart_service, "is_watching", lambda tok, u: True)
    resp = deviantart_routes.deviantart_unwatch_route(username="zoe", posts="purge")
    # 4xx: the reverse proxy replaces 5xx bodies with an HTML page the JS can't parse.
    assert resp.status_code == 409
    assert len(_ids(watch_feed)) == 5


def test_byline_falls_back_and_carries_the_artist(watch_feed):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_entries (id, deviantart_feed_id, deviationid, title, entry_url, published_at, author)"
            " VALUES ('x', ?, 'z2', 't', ?, '', 'ZoeStore')",
            (_WID, "https://www.deviantart.com/zoe/art/Two-2"),
        )
    detail = main.get_entry_detail(watch_feed, "z2")
    assert detail and detail["author"] == "ZoeStore"
    detail = main.get_entry_detail(watch_feed, "z3")
    assert detail
    assert (detail["author"], detail["da_watch_artist"]) == ("Zoe", "Zoe")


def test_route_treats_not_watching_as_done(watch_feed, monkeypatch):
    """DA answers {"success": false} for an artist you no longer Watch, whose old posts are still in the feed."""
    monkeypatch.setattr(deviantart_service, "unwatch_user", lambda tok, u: (False, 'HTTP 200: {"success":false}'))
    monkeypatch.setattr(deviantart_service, "is_watching", lambda tok, u: False)
    resp = deviantart_routes.deviantart_unwatch_route(username="zoe", posts="purge")
    assert json.loads(resp.body) == {"ok": True, "username": "zoe", "posts": "purge", "count": 3, "was_watching": False}


def _app():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    app.get("/")(routes.home.home)
    app.get("/entries/pane")(routes.entries.entry_pane)
    return app


def test_artist_name_link_filters_the_feed_to_that_artist(watch_feed):
    """The Watch-feed byline's artist name opens the feed searched for ``deviantart.com/<user>/``, keeping the read filter. Search
    covers e.link, so it matches the artist's authorless posts and not a lookalike like ``zoe_2``."""
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (root, watch_feed))
    with TestClient(_app()) as client:
        pane = client.get("/entries/pane", params={"folder_id": root, "feed_url": watch_feed, "entry_id": "z3", "read_filter": "all"}).text
        m = re.search(r'class="entry-author-link"[^>]*href="([^"]+)"', pane, re.S)
        assert m, "Watch-feed byline has no artist link"
        href = html.unescape(m.group(1))
        params = parse_qs(urlsplit(href).query)
        assert params["q"] == ["deviantart.com/zoe/"]
        assert params["list_feed_url"] == [watch_feed]
        assert params["read_filter"] == ["all"]
        listing = client.get(href).text
    shown = set(re.findall(r'data-post-entry-id="([^"]+)"', listing))
    assert shown == {"z1", "z2", "z3"}


def test_watch_feed_site_link_is_the_watch_page_not_a_profile(watch_feed, tmp_path):
    """The Watch feed's username is the placeholder "deviantsyouwatch"; its site link must not become that "profile's" gallery."""
    with main.get_meta_connection() as conn:
        deviantart_service._write_feed_file(conn, _WID)
    xml = (tmp_path / "deviantart-feeds" / f"{_WID}.xml").read_text()
    assert "https://www.deviantart.com/notifications/watch/deviations" in xml
    assert "deviantsyouwatch" not in xml
