"""Backfill script for entries whose signed DeviantArt lead image expired
before the go-forward pinning fix existed (2026-08-24, see Plan.md).

Candidate selection and the apply loop are the two things worth pinning down:
only un-pinned wixmp rows are ever touched, an already-pinned row is skipped
before spending any DeviantArt API quota on it, and a missing access token
short-circuits the whole run rather than burning through every candidate to
individually fail.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://deviantart.test/feed"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    try:
        yield monkeypatch
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _store_lead_image(feed_url: str, entry_id: str, url: str | None) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at) VALUES (?, ?, ?, 0)",
            (feed_url, entry_id, url),
        )
        conn.commit()


def test_candidates_finds_unpinned_wixmp_rows(configured):
    import scripts.backfill_expired_deviantart_thumbnails as cli

    _store_lead_image(FEED, "wixmp-unpinned", "https://images-wixmp.test/a.jpg?token=x")
    _store_lead_image(FEED, "not-deviantart", "https://example.test/b.jpg")

    out = cli._candidates(limit=0)

    assert [(f, e) for f, e, _u in out] == [(FEED, "wixmp-unpinned")]


def test_candidates_skips_already_pinned_rows(configured, monkeypatch):
    import scripts.backfill_expired_deviantart_thumbnails as cli

    _store_lead_image(FEED, "wixmp-pinned", "https://images-wixmp.test/a.jpg?token=x")
    monkeypatch.setattr(main, "has_pinned_entry_thumbnail", lambda fu, eid: eid == "wixmp-pinned")

    assert cli._candidates(limit=0) == []


def test_candidates_respects_limit(configured):
    import scripts.backfill_expired_deviantart_thumbnails as cli

    for i in range(5):
        _store_lead_image(FEED, f"e{i}", f"https://images-wixmp.test/{i}.jpg?token=x")

    assert len(cli._candidates(limit=2)) == 2


def test_dry_run_reports_without_touching_anything(configured, monkeypatch):
    import scripts.backfill_expired_deviantart_thumbnails as cli

    _store_lead_image(FEED, "e1", "https://images-wixmp.test/1.jpg?token=x")
    resign_calls = []
    monkeypatch.setattr(main, "_resign_expired_deviantart_url", lambda url, eid: resign_calls.append(eid) or url)

    result = cli.backfill_for_user("u_test", apply=False, limit=0, delay=0)

    assert result == {"candidates": 1}
    assert resign_calls == []


def test_apply_resigns_and_pins_each_candidate(configured, monkeypatch):
    import scripts.backfill_expired_deviantart_thumbnails as cli

    _store_lead_image(FEED, "e1", "https://images-wixmp.test/1.jpg?token=old")
    monkeypatch.setattr(main, "get_deviantart_user_token", lambda: "tok")
    resign_calls = []
    monkeypatch.setattr(
        main, "_resign_expired_deviantart_url",
        lambda url, eid: resign_calls.append((url, eid)) or "https://images-wixmp.test/1.jpg?token=fresh",
    )
    stored = []
    monkeypatch.setattr(main.lead_image_service, "store_entry_lead_image",
                         lambda fu, eid, url: stored.append((fu, eid, url)))
    # Pinned only after store_entry_lead_image ran, mirroring the real pin sink.
    monkeypatch.setattr(main, "has_pinned_entry_thumbnail", lambda fu, eid: bool(stored))

    result = cli.backfill_for_user("u_test", apply=True, limit=0, delay=0)

    assert resign_calls == [("https://images-wixmp.test/1.jpg?token=old", "e1")]
    assert stored == [(FEED, "e1", "https://images-wixmp.test/1.jpg?token=fresh")]
    assert result == {"candidates": 1, "pinned": 1}


def test_apply_counts_a_pin_that_did_not_take(configured, monkeypatch):
    import scripts.backfill_expired_deviantart_thumbnails as cli

    _store_lead_image(FEED, "e1", "https://images-wixmp.test/1.jpg?token=old")
    monkeypatch.setattr(main, "get_deviantart_user_token", lambda: "tok")
    monkeypatch.setattr(main, "_resign_expired_deviantart_url", lambda url, eid: url)
    monkeypatch.setattr(main.lead_image_service, "store_entry_lead_image", lambda fu, eid, url: None)
    monkeypatch.setattr(main, "has_pinned_entry_thumbnail", lambda fu, eid: False)

    result = cli.backfill_for_user("u_test", apply=True, limit=0, delay=0)

    assert result == {"candidates": 1, "could_not_pin": 1}


def test_apply_without_a_token_is_a_no_op(configured, monkeypatch):
    import scripts.backfill_expired_deviantart_thumbnails as cli

    _store_lead_image(FEED, "e1", "https://images-wixmp.test/1.jpg?token=old")
    monkeypatch.setattr(main, "get_deviantart_user_token", lambda: None)
    resign_calls = []
    monkeypatch.setattr(main, "_resign_expired_deviantart_url", lambda url, eid: resign_calls.append(eid) or url)

    result = cli.backfill_for_user("u_test", apply=True, limit=0, delay=0)

    assert result == {"candidates": 1, "skipped_no_token": 1}
    assert resign_calls == []
