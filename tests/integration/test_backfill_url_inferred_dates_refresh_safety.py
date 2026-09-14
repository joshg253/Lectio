"""scripts/backfill_url_inferred_dates.py's --refresh must never recompute a
manual date correction — only rows this tool itself wrote.

Sourcery caught this live on PR #312: `--refresh` originally loaded every
override for the selected feed and rewrote each one, with no way to tell a
manual /entries/set-date correction apart from this script's own inferred
row. entry_date_overrides now carries a `source` column ('manual' | 'inferred')
and --refresh filters on it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import scripts.backfill_url_inferred_dates as backfill
from services import tenancy

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
        # No published/updated at all -- eligible for a normal backfill pass,
        # and its slug resolves via url_inferred_pubmonth.
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": "inferred-1",
                "title": "no real date",
                "link": "https://example.test/blog/march32020/index.html",
            }
        )
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": "manual-1",
                "title": "manually corrected",
                "link": "https://example.test/blog/march32020/index.html",
                "published": datetime(1970, 1, 1, tzinfo=timezone.utc),
            }
        )
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/entries/set-date")(main.set_entry_date_route)
    return TestClient(app)


def _override_row(entry_id: str):
    with main.get_meta_connection() as conn:
        return conn.execute(
            "SELECT published, source FROM entry_date_overrides WHERE feed_url = ? AND entry_id = ?",
            (FEED, entry_id),
        ).fetchone()


def test_refresh_recomputes_only_rows_this_tool_wrote(configured):
    # A real manual correction, via the same route the UI uses.
    with _client() as c:
        r = c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "manual-1", "published": "2019-01-01"})
    assert r.status_code == 200 and r.json()["ok"] is True
    manual_before = _override_row("manual-1")
    assert manual_before is not None and manual_before[1] == "manual"

    # This tool's own write, from a normal backfill pass.
    changed = backfill.backfill_for_user(tenancy.DEFAULT_USER_ID, apply=True, feed_filter=FEED, refresh=False)
    assert changed == 1
    inferred_before = _override_row("inferred-1")
    assert inferred_before is not None and inferred_before[1] == "inferred"

    # --refresh over the same feed must recompute the inferred row and leave
    # the manual one byte-for-byte untouched.
    refreshed = backfill.backfill_for_user(tenancy.DEFAULT_USER_ID, apply=True, feed_filter=FEED, refresh=True)
    assert refreshed == 1

    manual_after = _override_row("manual-1")
    assert manual_after == manual_before, "a manual correction must never be touched by --refresh"

    inferred_after = _override_row("inferred-1")
    assert inferred_after is not None and inferred_after[1] == "inferred"
