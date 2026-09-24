"""Full-content fetch at ingest (services/full_content_fetch.py): folder/feed setting resolution, queueing NEW thin entries, and the
capped once-per-entry drain."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace

from services import full_content_fetch as fcf
from services.reader_api import ReaderApi

FEED = "https://example.test/feed"
OTHER = "https://other.test/feed"
THIN = "<p>teaser</p>"
FULL = "<p>" + "real article " * 100 + "</p>"


def _meta(tmp_path):
    path = tmp_path / "meta.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE folders (id INTEGER PRIMARY KEY, name TEXT NOT NULL, parent_id INTEGER);
        CREATE TABLE folder_feeds (folder_id INTEGER NOT NULL, feed_url TEXT NOT NULL, PRIMARY KEY (folder_id, feed_url));
        CREATE TABLE feed_display_prefs (feed_url TEXT PRIMARY KEY);
        INSERT INTO folders (id, name) VALUES (1, 'Tech');
        """
    )
    fcf.ensure_schema(conn)
    fcf.ensure_schema(conn)  # idempotent
    conn.commit()
    conn.close()

    @contextmanager
    def connect():
        c = sqlite3.connect(str(path))
        try:
            yield c
            c.commit()
        finally:
            c.close()

    return connect


def _set_feed(connect, feed_url, value):
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO feed_display_prefs (feed_url) VALUES (?)", (feed_url,))
        c.execute("UPDATE feed_display_prefs SET fetch_full_content = ? WHERE feed_url = ?", (value, feed_url))


def _set_folder(connect, on, feed_url=FEED):
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO folder_feeds VALUES (1, ?)", (feed_url,))
        c.execute("UPDATE folders SET fetch_full_content = ? WHERE id = 1", (1 if on else None,))


def _enabled(connect, feed_url=FEED):
    with connect() as c:
        return fcf.is_enabled_for_feed(c, feed_url)


def test_feed_setting_overrides_folder(tmp_path):
    connect = _meta(tmp_path)
    assert not _enabled(connect)  # nothing set anywhere
    _set_folder(connect, on=True)
    assert _enabled(connect)  # no feed row -> inherits
    _set_feed(connect, FEED, fcf.FEED_INHERIT)
    assert _enabled(connect)
    _set_feed(connect, FEED, fcf.FEED_OFF)
    assert not _enabled(connect)
    _set_folder(connect, on=False)
    _set_feed(connect, FEED, fcf.FEED_ON)
    assert _enabled(connect)


class _Reader:
    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_entry(self, key, default=None):
        return self.entries.get(key, default)


def _entry(entry_id, body=THIN, link="https://example.test/post", feed_url=FEED):
    return SimpleNamespace(feed_url=feed_url, id=entry_id, link=link, summary=None, content=(SimpleNamespace(value=body),) if body else ())


def _service(connect, entries=None, refetch_result=None, cooldown=()):
    calls = {"refetch": [], "failed": []}

    def refetch(feed_url, entry_id):
        calls["refetch"].append(entry_id)
        return refetch_result or {"ok": True}

    svc = fcf.FullContentFetchService(
        get_meta_connection=connect,
        get_reader=lambda: _Reader(entries or {}),
        refetch=refetch,
        is_thin=lambda html: len(html) < 200,
        host_in_cooldown=lambda host: host in cooldown,
        mark_host_failed=calls["failed"].append,
        is_excluded_feed=lambda feed_url: feed_url == OTHER,
    )
    return svc, calls


def _queued(connect):
    with connect() as c:
        return sorted(r[0] for r in c.execute("SELECT entry_id FROM full_content_fetch_queue"))


def test_hook_queues_only_new_thin_linked_entries_on_enabled_feeds(tmp_path):
    connect = _meta(tmp_path)
    svc, _ = _service(connect)
    svc.on_entry_updated(_entry("off"), is_new=True)
    assert _queued(connect) == []  # feed not opted in

    _set_feed(connect, FEED, fcf.FEED_ON)
    svc.on_entry_updated(_entry("thin"), is_new=True)
    svc.on_entry_updated(_entry("modified"), is_new=False)
    svc.on_entry_updated(_entry("full", body=FULL), is_new=True)
    svc.on_entry_updated(_entry("nolink", link=""), is_new=True)
    _set_feed(connect, OTHER, fcf.FEED_ON)
    svc.on_entry_updated(_entry("excluded", feed_url=OTHER), is_new=True)
    assert _queued(connect) == ["thin"]


def test_drain_is_capped_and_attempts_each_entry_once(tmp_path):
    connect = _meta(tmp_path)
    _set_feed(connect, FEED, fcf.FEED_ON)
    entries = {(FEED, f"e{i}"): _entry(f"e{i}") for i in range(4)}
    svc, calls = _service(connect, entries)
    for i in range(4):
        svc.on_entry_updated(entries[(FEED, f"e{i}")], is_new=True)

    svc.drain([FEED], "u1", per_feed_cap=3)
    assert calls["refetch"] == ["e0", "e1", "e2"]
    assert _queued(connect) == ["e3"]

    svc.drain([FEED], "u1", per_feed_cap=3)
    assert calls["refetch"] == ["e0", "e1", "e2", "e3"]
    assert _queued(connect) == []


def test_drain_skips_entries_no_longer_thin_or_gone(tmp_path):
    connect = _meta(tmp_path)
    _set_feed(connect, FEED, fcf.FEED_ON)
    svc, calls = _service(connect, {(FEED, "fixed"): _entry("fixed", body=FULL)})
    svc.on_entry_updated(_entry("fixed"), is_new=True)  # thin when it arrived, refetched by hand since
    svc.on_entry_updated(_entry("purged"), is_new=True)
    svc.drain([FEED], "u1")
    assert calls["refetch"] == []
    assert _queued(connect) == []


def test_turning_it_off_drops_the_backlog(tmp_path):
    connect = _meta(tmp_path)
    _set_feed(connect, FEED, fcf.FEED_ON)
    svc, calls = _service(connect, {(FEED, "a"): _entry("a")})
    svc.on_entry_updated(_entry("a"), is_new=True)
    _set_feed(connect, FEED, fcf.FEED_OFF)
    svc.drain([FEED], "u1")
    assert calls["refetch"] == []
    assert _queued(connect) == []


def test_host_in_cooldown_stays_queued_and_failure_pauses_host(tmp_path):
    connect = _meta(tmp_path)
    _set_feed(connect, FEED, fcf.FEED_ON)
    entries = {(FEED, "a"): _entry("a")}
    svc, calls = _service(connect, entries, cooldown={"example.test"})
    svc.on_entry_updated(entries[(FEED, "a")], is_new=True)
    svc.drain([FEED], "u1")
    assert calls["refetch"] == [] and _queued(connect) == ["a"]

    svc, calls = _service(connect, entries, refetch_result={"ok": False, "error": "blocked"})
    svc.drain([FEED], "u1")
    assert calls["refetch"] == ["a"] and calls["failed"] == ["example.test"]
    assert _queued(connect) == []  # attempted once, not retried


def test_reader_hook_reports_new_only_on_first_sight(tmp_path):
    feed_path = tmp_path / "feed.xml"
    item = "<item><guid>{0}</guid><title>{0}</title><link>https://example.test/{0}</link><description>x</description></item>"

    def write_feed(*guids):
        items = "".join(item.format(g) for g in guids)
        feed_path.write_text(
            f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><link>https://example.test/</link>{items}</channel></rss>'
        )

    seen: list[tuple[str, bool]] = []
    r = ReaderApi(str(tmp_path / "reader.sqlite"), entry_update_hook=lambda e, is_new: seen.append((e.id, is_new))).client()
    try:
        write_feed("g1")
        r.add_feed(str(feed_path))
        r.update_feeds()
        write_feed("g1", "g2")
        os.utime(feed_path, (time.time() + 60, time.time() + 60))  # reader's file retriever keys "changed" on mtime
        r.update_feed(str(feed_path))
    finally:
        r.close()
    assert [e for e in seen if e[1]] == [("g1", True), ("g2", True)]
