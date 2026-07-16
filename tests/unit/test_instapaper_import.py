"""Instapaper CSV export → Saved Items import plan (pure parser/mapper)."""
from __future__ import annotations

from services import instapaper_import


def _norm_url(u):
    u = (u or "").strip()
    return u if u.startswith(("http://", "https://")) else None


def _norm_tag(name):
    n = (name or "").strip().lower().replace(" ", "-")
    return n or None


def _plan(csv_text: str):
    return instapaper_import.plan_import(
        csv_text.encode("utf-8"),
        normalize_url=_norm_url,
        normalize_tag=_norm_tag,
    )


HEADER = "URL,Title,Selection,Folder,Timestamp\n"


def test_unread_and_archive_split():
    csv = HEADER + (
        "https://a.test/1,First,,Unread,1600000000\n"
        "https://a.test/2,Second,,Archive,1600000001\n"
    )
    plan = {b.url: b for b in _plan(csv)}
    assert plan["https://a.test/1"].archived is False
    assert plan["https://a.test/2"].archived is True
    assert plan["https://a.test/1"].saved_at == 1600000000.0
    assert plan["https://a.test/2"].title == "Second"


def test_custom_folder_becomes_tag():
    csv = HEADER + "https://a.test/1,Recipe,,Recipes To Try,1600000000\n"
    (bm,) = _plan(csv)
    assert bm.tags == ["recipes-to-try"]
    assert bm.archived is False


def test_starred_folder_becomes_starred_tag():
    csv = HEADER + "https://a.test/1,Fav,,Starred,1600000000\n"
    (bm,) = _plan(csv)
    assert bm.tags == [instapaper_import.STARRED_TAG]


def test_duplicate_url_merges_archive_and_tags():
    # Same URL in a custom folder AND archived — archived wins, tags union,
    # earliest timestamp kept.
    csv = HEADER + (
        "https://a.test/1,Title,,Recipes,1600000500\n"
        "https://a.test/1,Title,,Archive,1600000100\n"
    )
    (bm,) = _plan(csv)
    assert bm.archived is True
    assert bm.tags == ["recipes"]
    assert bm.saved_at == 1600000100.0


def test_invalid_and_empty_rows_skipped():
    csv = HEADER + (
        "not-a-url,Bad,,Unread,1600000000\n"       # rejected by normalize_url
        ",Missing URL,,Unread,1600000000\n"          # no URL
        "https://a.test/ok,Good,,Unread,1600000000\n"
    )
    plan = _plan(csv)
    assert [b.url for b in plan] == ["https://a.test/ok"]


def test_missing_title_falls_back_to_url():
    csv = HEADER + "https://a.test/x,,,Unread,1600000000\n"
    (bm,) = _plan(csv)
    assert bm.title == "https://a.test/x"


def test_bom_and_reordered_columns():
    # UTF-8 BOM + a different column order must still parse by header name.
    csv = "﻿Timestamp,Folder,Title,URL\n1600000000,Archive,T,https://a.test/z\n"
    (bm,) = _plan(csv)
    assert bm.url == "https://a.test/z"
    assert bm.archived is True
    assert bm.title == "T"


def test_non_csv_bytes_yield_empty_plan():
    assert _plan("this is not a csv at all") == []
    assert instapaper_import.plan_import(
        b"\x00\x01\x02", normalize_url=_norm_url, normalize_tag=_norm_tag
    ) == []


def test_missing_timestamp_is_none():
    csv = HEADER + "https://a.test/1,T,,Unread,\n"
    (bm,) = _plan(csv)
    assert bm.saved_at is None
