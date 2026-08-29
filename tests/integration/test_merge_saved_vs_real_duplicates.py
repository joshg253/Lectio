"""One-time cleanup script for the saved<->real duplicate backlog found
2026-08-28 (bulk re-imports insert saves directly, bypassing the live
_move_entry_to_feed event auto-filing normally rides on).

Coverage: a clean 1:1 duplicate gets merged via the same, already-tested
_move_entry_to_feed action live auto-filing uses; an ambiguous group (more
than one real-feed member sharing a canonical link) is reported and never
guessed at; dry-run touches nothing.
"""
from __future__ import annotations

import pytest

import main
from services import saved_articles as saved_articles_service
from services import tenancy

SAVED = saved_articles_service.SAVED_FEED_URL
REAL_A = "https://a.test/feed"
REAL_B = "https://b.test/feed"
ARTICLE_LINK = "https://a.test/articles/one"


def _reset_pools() -> None:
    main.close_thread_db_pools()
    main._websub_conn_local.pool = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved_layout = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "lectio_websub.sqlite")
    main.ensure_meta_schema()
    try:
        yield tmp_path
    finally:
        _reset_pools()
        tenancy._layout = saved_layout


def _star(feed_url: str, entry_id: str) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (feed_url, entry_id),
        )
        conn.commit()


def _add_entry(feed_url: str, entry_id: str, link: str, title: str = "Article") -> None:
    with main.get_reader() as reader:
        reader.add_feed(feed_url, allow_invalid_url=True, exist_ok=True)
        reader.add_entry({"feed_url": feed_url, "id": entry_id, "link": link, "title": title})


def _run(apply: bool, limit: int = 0):
    import scripts.merge_saved_vs_real_duplicates as cli
    return cli.run_for_user("test-uid", apply, limit)


def test_dry_run_reports_a_clean_pair_without_touching_it(env):
    _add_entry(SAVED, "saved1", ARTICLE_LINK)
    _add_entry(REAL_A, "real1", ARTICLE_LINK)
    _star(SAVED, "saved1")
    _star(REAL_A, "real1")

    result = _run(apply=False)

    assert result == {"safe_pairs": 1, "ambiguous_groups_skipped": 0}
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0] == 2
    with main.get_reader() as reader:
        assert not reader.get_entry((SAVED, "saved1")).read


def test_apply_merges_a_clean_pair(env):
    _add_entry(SAVED, "saved1", ARTICLE_LINK)
    _add_entry(REAL_A, "real1", ARTICLE_LINK)
    _star(SAVED, "saved1")
    _star(REAL_A, "real1")

    result = _run(apply=True)

    assert result["safe_pairs"] == 1
    assert result["merged"] == 1
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?", (REAL_A, "real1")
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?", (SAVED, "saved1")
        ).fetchone()
    with main.get_reader() as reader:
        # lectio:saved is user-added, not feed-provided, so _move_entry_to_feed
        # hard-deletes the source entirely instead of leaving a read husk --
        # the same behavior that took lectio:saved from 4,334 to 424 entries
        # during the 2026-07-21 auto-filing rollout.
        assert reader.get_entry((SAVED, "saved1"), None) is None


def test_ambiguous_group_is_reported_and_never_merged(env):
    """Two real feeds sharing the same canonical link as one saved copy --
    which one should the star land on? Not guessed at; both stay untouched."""
    _add_entry(SAVED, "saved1", ARTICLE_LINK)
    _add_entry(REAL_A, "real1", ARTICLE_LINK)
    _add_entry(REAL_B, "real2", ARTICLE_LINK)
    _star(SAVED, "saved1")
    _star(REAL_A, "real1")
    _star(REAL_B, "real2")

    result = _run(apply=True)

    assert result["safe_pairs"] == 0
    assert result["ambiguous_groups_skipped"] == 1
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0] == 3


def test_same_feed_duplicate_is_not_considered(env):
    """Two starred copies on the SAME real feed sharing a link is a
    same_feed dupe, not this script's job -- must not be touched."""
    _add_entry(REAL_A, "real1", ARTICLE_LINK)
    _add_entry(REAL_A, "real2", ARTICLE_LINK)
    _star(REAL_A, "real1")
    _star(REAL_A, "real2")

    result = _run(apply=True)

    assert result == {"safe_pairs": 0, "ambiguous_groups_skipped": 0}


def test_limit_caps_the_number_merged(env):
    for i in range(3):
        link = f"https://a.test/articles/{i}"
        _add_entry(SAVED, f"saved{i}", link)
        _add_entry(REAL_A, f"real{i}", link)
        _star(SAVED, f"saved{i}")
        _star(REAL_A, f"real{i}")

    result = _run(apply=True, limit=2)

    assert result["safe_pairs"] == 2
    assert result["merged"] == 2
