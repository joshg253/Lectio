"""``chunk_size`` lets "Filter this view" pull the rest of the view in one delta instead of ~100 round trips of CHUNK_SIZE."""

from __future__ import annotations

import re

import pytest
from _tenancy_helpers import configure_test_tenancy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import main  # noqa: F401 -- must import before routes.home (see routes/__init__.py's circular-import note)
import routes.home
from services import tenancy

FEED = "https://example.test/feed"
TOTAL = 400


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    main.invalidate_meta_structure_cache()
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        for i in range(TOTAL):
            reader.add_entry({"feed_url": FEED, "id": f"e{i}", "title": f"Post {i}", "link": f"https://example.test/p/{i}"})
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.invalidate_meta_structure_cache()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _count(html: str) -> int:
    return len(re.findall(r'class="post-item[ "]', html))


def _get(**params) -> str:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    app.get("/")(routes.home.home)
    with TestClient(app) as client:
        return client.get("/", params={"folder_id": main.UNCATEGORIZED_FOLDER_ID, "read_filter": "unread", **params}).text


def test_delta_without_chunk_size_is_one_chunk(tenant):
    assert _count(_get(chunk=250, chunk_delta=1)) == main.CHUNK_SIZE


def test_delta_chunk_size_is_the_page_size(tenant):
    assert _count(_get(chunk=250, chunk_delta=1, chunk_size=50)) == 50


def test_filtered_delta_returns_only_matches_and_reports_the_end(tenant):
    html = _get(chunk=1, chunk_delta=1, chunk_size=500, filter_term='"post 39"')
    titles = set(re.findall(r'data-post-title="(Post [0-9]+)"', html))
    assert titles <= {"Post 39"} | {f"Post {n}" for n in range(390, 400)}
    assert len(titles) >= 10  # all 11 matches, less the one row skipped by the offset
    assert 'data-view-exhausted="1"' in html


def test_filtered_delta_pages_by_match_count_and_reports_where_to_resume(tenant):
    html = _get(chunk=0 + 1, chunk_delta=1, chunk_size=5, filter_term="post")
    assert _count(html) == 5
    assert 'data-view-exhausted="0"' in html
    found = re.search(r'data-next-chunk="(\d+)"', html)
    assert found
    nxt = int(found.group(1))
    assert nxt > 1
