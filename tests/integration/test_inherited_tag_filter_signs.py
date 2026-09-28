"""Post-header tag-filter chips show a sign set by an enabled folder/global rule (colored apart from the feed's own rule), so a
filter set at a broader level is visible from the post. Display-only: get_inherited_tag_filter_signs."""

from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://a.test/feed"
OTHER = "https://b.test/feed"


@pytest.fixture
def conn(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_meta_connection() as c:
        c.execute("INSERT INTO folders (id, name) VALUES (7, 'Games'), (8, 'News')")
        c.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (7, ?), (8, ?)", (FEED, OTHER))
        yield c
    main.close_thread_db_pools()
    tenancy._layout = saved


def _rule(conn, scope, scope_id, spec, enabled=1):
    main.add_highlight_keyword(conn, scope, scope_id, spec, "", rule_type="tag_filter", enabled=enabled)


def test_enabled_folder_and_global_rules_light_the_chips(conn):
    _rule(conn, "folder", "7", "-deals, +reviews")
    _rule(conn, "global", "", "-sponsored")
    assert main.get_inherited_tag_filter_signs(conn, FEED) == {"deals": "-", "reviews": "+", "sponsored": "-"}


def test_a_folder_the_feed_is_not_in_does_not(conn):
    _rule(conn, "folder", "8", "-deals")
    assert main.get_inherited_tag_filter_signs(conn, FEED) == {}


def test_disabled_rules_and_the_feeds_own_rule_are_not_inherited(conn):
    _rule(conn, "global", "", "-deals", enabled=0)
    _rule(conn, "feed", FEED, "-reviews", enabled=1)
    assert main.get_inherited_tag_filter_signs(conn, FEED) == {}


def test_multi_folder_scope_counts(conn):
    _rule(conn, "folders", main._FOLDERS_SCOPE_SEP.join(["8", "7"]), "-deals")
    assert main.get_inherited_tag_filter_signs(conn, FEED) == {"deals": "-"}
