"""scripts/clear_url_slug_feed_tags.py must only delete a tag once it is
confirmed to be one of ITS OWN entry's URL path segments — shape alone (the
fused-date regex) is not enough, per Sourcery's review on PR #313: a
same-shaped tag that happens to sit on an unrelated entry must survive.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

import main
import scripts.clear_url_slug_feed_tags as cleaner
from services import tenancy
from services.feed_tags import FeedTagService

FEED = "https://example.test/feed"


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
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": "datagenetics-1",
                "title": "real bad row",
                "link": "http://datagenetics.com/blog/march112020/index.html",
            }
        )
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": "unrelated-1",
                "title": "coincidental shape, wrong entry",
                "link": "https://example.test/totally/unrelated/slug",
            }
        )
    with main.get_meta_connection() as conn:

        @contextmanager
        def get_meta_connection():
            yield conn

        service = FeedTagService(get_meta_connection=get_meta_connection)
        service.record_entry_tags(
            FEED,
            [
                ("datagenetics-1", ["march112020"]),  # genuinely derived from this entry's own URL
                ("unrelated-1", ["march112020"]),  # same shape, but not from THIS entry's URL — must survive
            ],
        )
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _tags_for(entry_id: str) -> list[str]:
    with main.get_meta_connection() as conn:
        return [r[0] for r in conn.execute("SELECT tag FROM entry_feed_tags WHERE feed_url = ? AND entry_id = ?", (FEED, entry_id))]


def test_is_own_url_segment():
    assert cleaner._is_own_url_segment("march112020", "http://datagenetics.com/blog/march112020/index.html") is True
    assert cleaner._is_own_url_segment("march112020", "https://example.test/totally/unrelated/slug") is False
    assert cleaner._is_own_url_segment("march112020", None) is False


def test_only_the_genuinely_derived_tag_is_deleted(configured):
    assert _tags_for("datagenetics-1") == ["march112020"]
    assert _tags_for("unrelated-1") == ["march112020"]

    deleted = cleaner._clear_for_user(tenancy.DEFAULT_USER_ID, apply=True)
    assert deleted == 1

    assert _tags_for("datagenetics-1") == []
    assert _tags_for("unrelated-1") == ["march112020"], "a same-shaped tag not derived from THIS entry's URL must survive"
