"""Regex rules are stored as /pattern/ with is_regex = 0: new rules are normalized on write, old rows are rewritten once at startup."""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
from services import takeout_service, tenancy


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _rows(conn):
    return [tuple(r) for r in conn.execute("SELECT keyword, is_regex, type FROM highlight_keywords ORDER BY rowid").fetchall()]


def test_add_normalizes_regex_to_slash_form(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", r"\bfoo\b", "yellow", True, "mark_as_read")
        main.add_highlight_keyword(conn, "global", "", "plain", "yellow", False, "mark_as_read")
        main.add_highlight_keyword(conn, "global", "", "-a, +b", "yellow", True, "tag_filter")
        main.add_highlight_keyword(conn, "global", "", "", "yellow", True, "youtube_playlist")
        assert _rows(conn) == [
            (r"/\bfoo\b/", 0, "mark_as_read"),
            ("plain", 0, "mark_as_read"),
            ("-a, +b", 1, "tag_filter"),  # not a search pattern: left alone
            ("", 1, "youtube_playlist"),  # blank keyword (= all videos): nothing to wrap
        ]


def test_startup_migration_rewrites_old_regex_rows_once_and_keeps_a_backup(env):
    with main.get_meta_connection() as conn:
        for kw in ("^How to", "REVIEW$"):
            conn.execute(
                "INSERT INTO highlight_keywords (scope, scope_id, keyword, color, is_regex, type)"
                " VALUES ('global', '', ?, 'yellow', 1, 'mark_as_read')",
                (kw,),
            )
        conn.execute(
            "INSERT INTO highlight_keywords (scope, scope_id, keyword, color, is_regex) VALUES ('global', '', 'plain', 'yellow', 0)"
        )
        conn.commit()
    main.ensure_meta_schema()
    main.ensure_meta_schema()  # idempotent
    with main.get_meta_connection() as conn:
        assert _rows(conn) == [("/^How to/", 0, "mark_as_read"), ("/REVIEW$/", 0, "mark_as_read"), ("plain", 0, "highlight")]
        backup = conn.execute("SELECT old_keyword, new_keyword FROM highlight_keywords_pre_slash ORDER BY rowid").fetchall()
    assert [tuple(b) for b in backup] == [("^How to", "/^How to/"), ("REVIEW$", "/REVIEW$/")]


def test_startup_migration_skips_a_row_whose_slash_form_already_exists(env):
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO highlight_keywords (scope, scope_id, keyword, color, is_regex) VALUES ('global', '', 'foo', 'yellow', 1)")
        conn.execute(
            "INSERT INTO highlight_keywords (scope, scope_id, keyword, color, is_regex) VALUES ('global', '', '/foo/', 'yellow', 0)"
        )
        conn.commit()
    main.ensure_meta_schema()
    with main.get_meta_connection() as conn:
        assert sorted(_rows(conn)) == [("/foo/", 0, "highlight"), ("foo", 1, "highlight")]  # left as is; the matcher still honors the flag


def test_migrated_and_legacy_rules_match_the_same_titles():
    legacy = main.build_keyword_matcher(r"\b(?:Pixel\s+Watch|Ryobi)\b", True)
    migrated = main.build_keyword_matcher(r"/\b(?:Pixel\s+Watch|Ryobi)\b/", False)
    for title in ["Pixel  Watch 3", "ryobi drill", "Pixelwatch", "nothing"]:
        assert legacy(title) == migrated(title)


def test_takeout_import_wraps_old_exports(env):
    with main.get_meta_connection() as conn:
        takeout_service._import_rules(
            conn, [{"scope": "global", "scope_id": "", "keyword": "^How to", "type": "mark_as_read", "is_regex": 1}]
        )
        conn.commit()
        assert _rows(conn) == [("/^How to/", 0, "mark_as_read")]


def test_takeout_import_skips_a_malformed_rule_and_keeps_the_rest(env):
    with main.get_meta_connection() as conn:
        takeout_service._import_rules(
            conn,
            [
                {"scope": "global", "scope_id": "", "keyword": "bad", "type": "mark_as_read", "is_regex": "yes"},
                {"scope": "global", "scope_id": "", "keyword": "good", "type": "mark_as_read", "is_regex": 0},
            ],
        )
        conn.commit()
        assert _rows(conn) == [("good", 0, "mark_as_read")]
