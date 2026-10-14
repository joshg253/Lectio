"""Disabled feeds carry who disabled them: '' for the user, a short label for the system."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from _tenancy_helpers import configure_test_tenancy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment

import main
import routes.feeds
import routes.settings
from services import tenancy

TEMPLATE = Path(main.__file__).parent / "templates" / "_settings_modal.html"


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    """The label renders in server-local time; pin it so the calendar dates asserted below do not depend on the machine."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def meta(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    try:
        yield main.get_meta_connection()
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _row(meta, url):
    r = meta.execute("SELECT reason, disabled_at FROM disabled_feeds WHERE feed_url = ?", (url,)).fetchone()
    return None if r is None else (r["reason"], r["disabled_at"])


def test_user_disable_has_no_reason_and_a_timestamp(meta):
    main.disable_feed("https://a.test/feed")
    reason, at = _row(meta, "https://a.test/feed")
    assert reason == "" and at


def test_system_disable_records_its_reason(meta):
    main.disable_feed("https://a.test/feed", reason="410 Gone")
    assert _row(meta, "https://a.test/feed")[0] == "410 Gone"


def test_a_system_reason_never_overwrites_a_user_pause(meta):
    main.disable_feed("https://a.test/feed")
    main.disable_feed("https://a.test/feed", reason="410 Gone")
    assert _row(meta, "https://a.test/feed")[0] == ""


def test_a_user_disable_claims_a_system_disabled_feed_and_keeps_the_date(meta):
    main.disable_feed("https://a.test/feed", reason="410 Gone")
    meta.execute("UPDATE disabled_feeds SET disabled_at = '2026-01-01 00:00:00' WHERE feed_url = 'https://a.test/feed'")
    main.disable_feed("https://a.test/feed")
    assert _row(meta, "https://a.test/feed") == ("", "2026-01-01 00:00:00")


def test_enable_clears_the_row_and_a_stale_410_record(meta):
    url = "https://a.test/feed"
    main.disable_feed(url, reason="410 Gone")
    meta.execute(
        "INSERT INTO feed_failure_state (feed_url, consecutive_failures, last_error)"
        " VALUES (?, 1, '410 Gone: feed has been permanently removed')",
        (url,),
    )
    other = "https://b.test/feed"
    meta.execute("INSERT INTO feed_failure_state (feed_url, consecutive_failures, last_error) VALUES (?, 5, 'timed out')", (other,))
    main.enable_feed(url)
    assert _row(meta, url) is None
    assert meta.execute("SELECT COUNT(*) FROM feed_failure_state WHERE feed_url = ?", (url,)).fetchone()[0] == 0
    main.enable_feed(other)  # a different failure is not the 410 record's business
    assert meta.execute("SELECT COUNT(*) FROM feed_failure_state WHERE feed_url = ?", (other,)).fetchone()[0] == 1


def test_existing_databases_gain_the_column_as_user_pauses(meta):
    meta.execute("DROP TABLE disabled_feeds")
    meta.execute("CREATE TABLE disabled_feeds (feed_url TEXT PRIMARY KEY, disabled_at TEXT NOT NULL DEFAULT (datetime('now')))")
    meta.execute("INSERT INTO disabled_feeds (feed_url) VALUES ('https://old.test/feed')")
    meta.commit()
    main.ensure_meta_schema()  # idempotent: also what runs for every existing tenant at startup
    main.ensure_meta_schema()
    assert _row(meta, "https://old.test/feed")[0] == ""


def _render_panel(inactive_feeds):
    src = TEMPLATE.read_text()
    start = src.index('<div id="inactive-feeds-modal"')
    end = src.index('<div class="feeds-tab-panel" data-feeds-panel="stale"')
    return Environment(autoescape=True).from_string(src[start:end]).render(inactive_feeds=inactive_feeds)


def _feed(title, reason="", at="2026-10-12 12:00:00"):
    return {
        "feed_url": f"https://{title}.test/f",
        "feed_title": title,
        "reason": reason,
        "disabled_at": at,
        "disabled_label": main._disabled_label(reason, at),
        "folder_id": 1,
        "folder_name": "F",
    }


def test_list_groups_automatic_before_the_users_own_and_shows_reason_and_date():
    html = _render_panel([_feed("mine"), _feed("gone", reason="410 Gone")])
    assert html.index("Disabled automatically") < html.index("gone") < html.index("Disabled by you") < html.index("mine")
    assert "Disabled (automatic: 410 Gone, Oct 12, 2026" in html
    assert "Disabled (manual, Oct 12, 2026" in html


def test_list_has_no_group_headings_when_everything_is_the_users_own():
    html = _render_panel([_feed("mine"), _feed("also-mine")])
    assert "Disabled automatically" not in html and "Disabled by you" not in html
    assert html.count("Disabled (manual, Oct 12, 2026") == 2 and "automatic:" not in html


def test_label_reads_who_and_when_and_survives_a_bad_timestamp():
    assert main._disabled_label("", "2026-01-01 12:00:00").startswith("manual, Jan 1, 2026")
    assert main._disabled_label("410 Gone", "2026-01-01 12:00:00").startswith("automatic: 410 Gone, Jan 1, 2026")
    assert main._disabled_label("", "garbage") == "manual"


def test_feed_names_open_feed_properties_like_every_other_settings_list():
    html = _render_panel([_feed("mine")])
    assert 'data-feed-properties-url="https://mine.test/f"' in html


def test_hostile_titles_and_reasons_are_escaped_in_the_list():
    feed = _feed('<script>alert(1)</script>"', reason="<img src=x onerror=1>")
    html = _render_panel([feed])
    assert "<script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_bulk_and_triage_disables_do_not_claim_an_automatic_one(meta):
    main.disable_feed("https://a.test/feed", reason="410 Gone")
    main.disable_feed("https://a.test/feed", claim=False)  # bulk disable / mark-dead
    assert _row(meta, "https://a.test/feed")[0] == "410 Gone"
    main.disable_feed("https://b.test/feed", claim=False)  # still creates a manual row when there is none
    assert _row(meta, "https://b.test/feed")[0] == ""


def test_changing_the_url_of_a_system_disabled_feed_gives_the_new_url_a_clean_start(meta, monkeypatch):
    old, new = "https://old.test/feed.xml", "https://new.test/feed.xml"
    with main.get_reader() as reader:
        reader.add_feed(old, allow_invalid_url=True)
    main.disable_feed(old, reason="410 Gone")
    from services import feed_discovery

    monkeypatch.setattr(feed_discovery, "probe_url", lambda url, **kw: {"status": "feed", "feeds": [{"url": url}]})
    app = FastAPI()
    app.post("/feeds/change-url")(routes.feeds.change_feed_url_route)
    with TestClient(app) as c:
        r = c.post("/feeds/change-url", data={"old_url": old, "new_url": new, "force": 1})
    assert r.status_code == 200, r.text
    assert _row(meta, new) is None and _row(meta, old) is None  # the reason belonged to the old URL


def test_changing_the_url_of_a_feed_the_user_paused_keeps_it_paused(meta, monkeypatch):
    old, new = "https://old.test/feed.xml", "https://new.test/feed.xml"
    with main.get_reader() as reader:
        reader.add_feed(old, allow_invalid_url=True)
    main.disable_feed(old)
    app = FastAPI()
    app.post("/feeds/change-url")(routes.feeds.change_feed_url_route)
    with TestClient(app) as c:
        r = c.post("/feeds/change-url", data={"old_url": old, "new_url": new, "force": 1})
    assert r.status_code == 200, r.text
    assert _row(meta, new) is not None and _row(meta, new)[0] == ""
