"""Fetch-escalation ladder: flagged feeds step back down once they fetch directly again."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from services import escalation_reprobe as er

OLD = "2026-01-01 00:00:00"
RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>X</title><item><title>a</title></item></channel></rss>'


def _no_sleep(_seconds: float) -> None:
    return None


def _schema(c: sqlite3.Connection) -> sqlite3.Connection:
    for t in er.ESCALATION_TABLES:
        c.execute(f"CREATE TABLE {t} (feed_url TEXT PRIMARY KEY, flagged_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')")
    for t in ("disabled_feeds", "feeds_needing_replacement", "kept_feeds"):
        c.execute(f"CREATE TABLE {t} (feed_url TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE feed_failure_state (feed_url TEXT PRIMARY KEY, consecutive_failures INT)")
    return c


@pytest.fixture
def conn():
    return _schema(sqlite3.connect(":memory:"))


def _flag(conn, url, *tables, at=OLD, reason=""):
    for t in tables:
        conn.execute(f"INSERT INTO {t} (feed_url, flagged_at, reason) VALUES (?, ?, ?)", (url, at, reason))


def _counts(conn, url):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE feed_url=?", (url,)).fetchone()[0] for t in er.ESCALATION_TABLES}


def _due(conn, **kw):
    return er.due_feeds(conn, **kw)


def test_success_clears_every_tier_and_failure_state(conn):
    u = "https://a.example/feed"
    _flag(conn, u, *er.ESCALATION_TABLES)
    conn.execute("INSERT INTO feed_failure_state VALUES (?, 40)", (u,))
    cleared: list[str] = []
    res = er.reprobe_flagged_feeds(conn, "UA", probe=lambda *_: True, sleep=_no_sleep, on_clear=cleared.append)
    assert res == {"probed": 1, "cleared": 1, "renewed": 0}
    assert cleared == [u]
    assert sum(_counts(conn, u).values()) == 0
    assert conn.execute("SELECT COUNT(*) FROM feed_failure_state").fetchone()[0] == 0


def test_refusal_renews_flags_so_next_probe_is_a_full_interval_away(conn):
    u = "https://a.example/feed"
    _flag(conn, u, "browser_ua_feeds", "proxy_feeds")
    res = er.reprobe_flagged_feeds(conn, "UA", probe=lambda *_: False, sleep=_no_sleep)
    assert res == {"probed": 1, "cleared": 0, "renewed": 1}
    assert sum(_counts(conn, u).values()) == 2  # still escalated
    assert _due(conn) == []


def test_recent_flag_is_not_due_and_newest_tier_decides(conn):
    _flag(conn, "https://fresh.example/f", "browser_ua_feeds", at="2999-01-01 00:00:00")
    _flag(conn, "https://mixed.example/f", "browser_ua_feeds")  # old
    _flag(conn, "https://mixed.example/f", "proxy_feeds", at="2999-01-01 00:00:00")  # newest tier is recent
    assert _due(conn) == []


def test_limit_and_ordering(conn):
    for i in range(5):
        _flag(conn, f"https://h{i}.example/f", "browser_ua_feeds", at=f"2026-01-0{i + 1} 00:00:00")
    assert _due(conn, limit=2) == ["https://h0.example/f", "https://h1.example/f"]


def test_user_set_manual_flag_is_never_probed_or_cleared(conn):
    u = "https://manual.example/f"
    _flag(conn, u, "browser_ua_feeds", reason="refused honest UA on refresh")
    _flag(conn, u, "proxy_feeds", reason="manual")  # one manual tier protects the whole feed
    assert _due(conn) == []


@pytest.mark.parametrize("table", ["disabled_feeds", "feeds_needing_replacement", "kept_feeds"])
def test_feeds_no_longer_fetched_are_skipped(conn, table):
    u = "https://gone.example/f"
    _flag(conn, u, "browser_ua_feeds")
    conn.execute(f"INSERT INTO {table} VALUES (?)", (u,))
    assert _due(conn) == []


def test_only_active_feeds_are_probed_and_inactive_ones_do_not_eat_the_limit(conn):
    for i in range(3):
        _flag(conn, f"https://unsubscribed{i}.example/f", "browser_ua_feeds", at=f"2026-01-0{i + 1} 00:00:00")
    _flag(conn, "https://live.example/f", "browser_ua_feeds", at="2026-01-09 00:00:00")
    seen: list[str] = []

    def _probe(u, _ua):
        seen.append(u)
        return True

    er.reprobe_flagged_feeds(conn, "UA", is_active=lambda u: u.startswith("https://live"), probe=_probe, limit=1, sleep=_no_sleep)
    assert seen == ["https://live.example/f"]
    assert sum(_counts(conn, "https://unsubscribed0.example/f").values()) == 1  # untouched


def test_activity_is_rechecked_right_before_each_probe(conn):
    _flag(conn, "https://a.example/f", "browser_ua_feeds", at="2026-01-01 00:00:00")
    _flag(conn, "https://b.example/f", "browser_ua_feeds", at="2026-01-02 00:00:00")
    state = {"active": True}
    seen: list[str] = []

    def _probe(u, _ua):
        seen.append(u)
        state["active"] = False  # the user unsubscribes the next one while this probe runs
        return True

    er.reprobe_flagged_feeds(conn, "UA", is_active=lambda _u: state["active"], probe=_probe, sleep=_no_sleep)
    assert seen == ["https://a.example/f"]


def test_run_stops_when_probing_is_no_longer_allowed(conn):
    for i in range(3):
        _flag(conn, f"https://h{i}.example/f", "browser_ua_feeds")
    allowed = [True, False]
    seen: list[str] = []
    res = er.reprobe_flagged_feeds(
        conn,
        "UA",
        should_continue=lambda: allowed.pop(0) if allowed else False,
        probe=lambda u, _ua: seen.append(u) or True,
        sleep=_no_sleep,
    )
    assert res["probed"] == 1 and len(seen) == 1


def test_a_flag_added_or_reasserted_during_the_probe_survives_the_clear(conn):
    u = "https://a.example/feed"
    _flag(conn, u, "browser_ua_feeds", "proxy_feeds")
    conn.execute("INSERT INTO feed_failure_state VALUES (?, 3)", (u,))

    def _probe(feed_url, _ua):
        # The refresh path escalates a new tier and re-asserts an existing one (flagged_at moves to now) while the probe is in flight.
        conn.execute("INSERT INTO tailscale_feeds VALUES (?, datetime('now'), '')", (feed_url,))
        conn.execute("UPDATE proxy_feeds SET flagged_at = datetime('now') WHERE feed_url = ?", (feed_url,))
        return True

    er.reprobe_flagged_feeds(conn, "UA", probe=_probe, sleep=_no_sleep)
    assert _counts(conn, u) == {"browser_ua_feeds": 0, "proxy_feeds": 1, "tailscale_feeds": 1, "flaresolverr_feeds": 0}
    assert conn.execute("SELECT COUNT(*) FROM feed_failure_state").fetchone()[0] == 1


def test_each_result_is_committed_before_the_next_probe(tmp_path: Path):
    """No write lock is held across the network: a second connection can write mid-run."""
    db = tmp_path / "meta.sqlite3"
    setup = _schema(sqlite3.connect(db))
    for i in range(3):
        _flag(setup, f"https://h{i}.example/f", "browser_ua_feeds")
    setup.commit()
    run = sqlite3.connect(db, timeout=0.2)
    other = sqlite3.connect(db, timeout=0.2)
    wrote: list[int] = []

    def _probe(_u, _ua):
        other.execute("INSERT OR REPLACE INTO feed_failure_state VALUES ('https://other.example/f', 1)")
        other.commit()
        wrote.append(1)
        return True

    er.reprobe_flagged_feeds(run, "UA", probe=_probe, sleep=_no_sleep)
    assert len(wrote) == 3


def test_same_host_probes_are_spaced(conn):
    _flag(conn, "https://same.example/a", "browser_ua_feeds")
    _flag(conn, "https://same.example/b", "browser_ua_feeds")
    waits: list[float] = []
    er.reprobe_flagged_feeds(conn, "UA", probe=lambda *_: False, sleep=waits.append)
    assert waits and max(waits) > er.PROBE_SPACING_SECONDS


def test_time_budget_stops_the_run_and_leaves_the_rest_flagged(conn):
    for i in range(5):
        _flag(conn, f"https://h{i}.example/f", "browser_ua_feeds", at=f"2026-01-0{i + 1} 00:00:00")
    now = [0.0]

    def _probe(_u, _ua):
        now[0] += 100.0  # each probe "takes" 100s
        return True

    res = er.reprobe_flagged_feeds(conn, "UA", probe=_probe, max_seconds=250, sleep=lambda _s: None, clock=lambda: now[0])
    assert res["probed"] == 3  # starts at 0s, 100s, 200s are under budget; 300s is not
    assert len(_due(conn)) == 2


def _patched_client(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(er.url_guard, "is_safe_outbound_url", lambda _u: True)
    monkeypatch.setattr(er.url_guard, "build_client", lambda **kw: real(transport=httpx.MockTransport(handler), headers=kw.get("headers")))


def _respond(monkeypatch, status=200, content=RSS, content_type="application/rss+xml"):
    _patched_client(monkeypatch, lambda req: httpx.Response(status, content=content, headers={"content-type": content_type}))


def test_probe_accepts_a_real_feed(monkeypatch):
    _respond(monkeypatch)
    assert er.probe_direct("https://a.example/feed", "UA") is True


def test_probe_accepts_a_json_feed(monkeypatch):
    body = json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": "X", "items": []}).encode()
    _respond(monkeypatch, content=body, content_type="application/feed+json")
    assert er.probe_direct("https://a.example/feed.json", "UA") is True


def test_probe_rejects_a_json_object_that_is_not_a_json_feed(monkeypatch):
    body = json.dumps({"version": "https://jsonfeed.org/version/1.1"}).encode()  # no items
    _respond(monkeypatch, content=body, content_type="application/json")
    assert er.probe_direct("https://a.example/feed.json", "UA") is False


def test_probe_follows_a_redirect(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://a.example/new"})
        return httpx.Response(200, content=RSS, headers={"content-type": "application/rss+xml"})

    _patched_client(monkeypatch, handler)
    assert er.probe_direct("https://a.example/old", "UA") is True


def test_probe_rejects_a_challenge_even_when_the_body_would_parse(monkeypatch):
    """Challenge detection itself must be what refuses it: the body is a valid feed, the headers say challenge."""
    _patched_client(
        monkeypatch,
        lambda req: httpx.Response(200, content=RSS, headers={"content-type": "application/rss+xml", "x-amzn-waf-action": "challenge"}),
    )
    assert er.probe_direct("https://a.example/feed", "UA") is False


def test_probe_rejects_a_challenge_body_even_if_the_parser_would_accept_it(monkeypatch):
    html = b"<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    _respond(monkeypatch, content=html, content_type="text/html")

    class _Parsed:
        entries = [object()]
        feed = {"title": "x"}

    monkeypatch.setattr(er.feedparser, "parse", lambda _b: _Parsed())
    assert er.probe_direct("https://a.example/feed", "UA") is False


def test_probe_gives_up_on_a_slow_trickle(monkeypatch):
    class _Slow(httpx.SyncByteStream):
        def __iter__(self):
            while True:
                yield b"x"

    _patched_client(monkeypatch, lambda req: httpx.Response(200, stream=_Slow(), headers={"content-type": "application/rss+xml"}))
    ticks = iter(range(0, 10_000, 10))
    monkeypatch.setattr(er.time, "monotonic", lambda: next(ticks))
    assert er.probe_direct("https://a.example/feed", "UA") is False


def test_probe_rejects_403_and_non_feed_200(monkeypatch):
    _respond(monkeypatch, status=403, content=b"no", content_type="text/html")
    assert er.probe_direct("https://a.example/feed", "UA") is False
    _respond(monkeypatch, content=b"<html><body>hello</body></html>", content_type="text/html")
    assert er.probe_direct("https://a.example/feed", "UA") is False


def test_probe_rejects_non_http_urls():
    assert er.probe_direct("file:///tmp/x.xml", "UA") is False


def test_literal_sql_covers_exactly_the_escalation_tables():
    assert set(er._DELETE_OLDER) == set(er._RENEW_OLDER) == set(er.ESCALATION_TABLES)
    for table in er.ESCALATION_TABLES:
        assert table in er._COUNT_FLAGS and table in er._DUE_FEEDS


def test_a_locked_database_is_waited_out_and_does_not_abort_the_run(tmp_path: Path):
    """The nightly VACUUM takes an exclusive lock: the write retries, and a feed whose write never lands is skipped, not fatal."""
    db = tmp_path / "meta.sqlite3"
    setup = _schema(sqlite3.connect(db))
    for i in range(2):
        _flag(setup, f"https://h{i}.example/f", "browser_ua_feeds", at=f"2026-01-0{i + 1} 00:00:00")
    setup.commit()
    locker = sqlite3.connect(db, timeout=0.1, isolation_level=None)
    run = sqlite3.connect(db, timeout=0.1)
    locker.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            locker.execute("COMMIT")  # the lock clears while the run is waiting

    res = er.reprobe_flagged_feeds(run, "UA", probe=lambda *_: True, sleep=_sleep)
    assert res["cleared"] == 2 and res["probed"] == 2
    assert [r[0] for r in sqlite3.connect(db).execute("SELECT feed_url FROM browser_ua_feeds")] == []


def test_a_write_that_never_lands_skips_that_feed_and_carries_on(tmp_path: Path, monkeypatch):
    db = tmp_path / "meta.sqlite3"
    setup = _schema(sqlite3.connect(db))
    _flag(setup, "https://a.example/f", "browser_ua_feeds", at="2026-01-01 00:00:00")
    _flag(setup, "https://b.example/f", "browser_ua_feeds", at="2026-01-02 00:00:00")
    setup.commit()
    locker = sqlite3.connect(db, timeout=0.1, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(er, "_WRITE_RETRIES", 2)
    run = sqlite3.connect(db, timeout=0.1)
    res = er.reprobe_flagged_feeds(run, "UA", probe=lambda *_: True, sleep=_no_sleep)
    assert res == {"probed": 0, "cleared": 0, "renewed": 0}  # nothing recorded, no exception
    locker.execute("ROLLBACK")
    assert len(sqlite3.connect(db).execute("SELECT feed_url FROM browser_ua_feeds").fetchall()) == 2
