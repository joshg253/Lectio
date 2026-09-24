"""Page topics at ingest (services/page_topics.py): queueing NEW entries on opted-in feeds, the capped once-per-entry drain, host
pausing, and that page tags survive the feed re-delivering an entry (entry_feed_tags.source)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from services import full_content_fetch as fcf
from services import page_topics
from services.feed_tags import FeedTagService

FEED = "https://www.pcgamer.com/feeds.xml"
PAGE = '<html><head><meta property="article:tag" content="Games"><meta property="article:tag" content="Krafton"></head>'


def _meta(tmp_path):
    path = tmp_path / "meta.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE folders (id INTEGER PRIMARY KEY, name TEXT NOT NULL, parent_id INTEGER);
        CREATE TABLE folder_feeds (folder_id INTEGER NOT NULL, feed_url TEXT NOT NULL, PRIMARY KEY (folder_id, feed_url));
        CREATE TABLE feed_display_prefs (feed_url TEXT PRIMARY KEY);
        CREATE TABLE entry_feed_tags (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, tag TEXT NOT NULL, first_seen_at REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'feed', PRIMARY KEY (feed_url, entry_id, tag));
        INSERT INTO folders (id, name) VALUES (1, 'Games');
        INSERT INTO folder_feeds VALUES (1, 'https://www.pcgamer.com/feeds.xml');
        """
    )
    fcf.ensure_schema(conn)
    page_topics.ensure_schema(conn)
    page_topics.ensure_schema(conn)  # idempotent
    conn.commit()
    conn.close()

    @contextmanager
    def connect():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    return connect


class _Reader:
    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_entry(self, key, default=None):
        return self.entries.get(key, default)


def _entry(eid, link=None):
    return SimpleNamespace(feed_url=FEED, id=eid, link=link if link is not None else f"https://www.pcgamer.com/games/{eid}/")


def _service(connect, entries, responses=None):
    calls: list[str] = []
    tags = FeedTagService(get_meta_connection=connect)

    def fetch(url):
        calls.append(url)
        r = (responses or {}).get(url, (PAGE, 200))
        if isinstance(r, Exception):
            raise r
        return r

    svc = page_topics.PageTopicsService(
        get_meta_connection=connect,
        get_reader=lambda: _Reader({(FEED, e.id): e for e in entries}),
        fetch_prefix=fetch,
        extract_tags=lambda html, url: [t for t in ("Games", "Krafton") if f'content="{t}"' in html],
        record_tags=lambda fu, eid, t: tags.record_entry_tags(fu, [(eid, t)], source="page"),
    )
    return svc, calls, tags


def _set_folder(connect, on):
    with connect() as c:
        c.execute("UPDATE folders SET capture_page_topics = ? WHERE id = 1", (1 if on else None,))


def _queued(connect):
    with connect() as c:
        return sorted(r[0] for r in c.execute("SELECT entry_id FROM page_topics_queue"))


def _tags(connect, eid):
    with connect() as c:
        return sorted((r[0], r[1]) for r in c.execute("SELECT tag, source FROM entry_feed_tags WHERE entry_id = ?", (eid,)))


def test_only_new_linked_entries_on_opted_in_feeds_are_queued(tmp_path):
    connect = _meta(tmp_path)
    svc, _, _ = _service(connect, [])
    svc.on_entry_updated(_entry("off"), is_new=True)
    _set_folder(connect, on=True)
    svc.on_entry_updated(_entry("new"), is_new=True)
    svc.on_entry_updated(_entry("seen"), is_new=False)
    svc.on_entry_updated(_entry("nolink", link=""), is_new=True)
    assert _queued(connect) == ["new"]


def test_feed_off_overrides_folder_on(tmp_path):
    connect = _meta(tmp_path)
    _set_folder(connect, on=True)
    with connect() as c:
        c.execute("INSERT INTO feed_display_prefs (feed_url, capture_page_topics) VALUES (?, 0)", (FEED,))
    svc, _, _ = _service(connect, [])
    svc.on_entry_updated(_entry("a"), is_new=True)
    assert _queued(connect) == []


def test_drain_records_page_tags_and_is_capped(tmp_path):
    connect = _meta(tmp_path)
    _set_folder(connect, on=True)
    entries = [_entry(f"e{i}") for i in range(3)]
    svc, calls, _ = _service(connect, entries)
    for e in entries:
        svc.on_entry_updated(e, is_new=True)
    svc.drain([FEED], "u1", per_feed_cap=2)
    assert len(calls) == 2 and _queued(connect) == ["e2"]
    assert _tags(connect, "e0") == [("Games", "page"), ("Krafton", "page")]


def test_page_tags_survive_the_feed_redelivering_the_entry(tmp_path):
    connect = _meta(tmp_path)
    _set_folder(connect, on=True)
    e = _entry("a")
    svc, _, tags = _service(connect, [e])
    tags.record_entry_tags(FEED, [("a", ["FPS", "Games"])])  # ingest
    svc.on_entry_updated(e, is_new=True)
    svc.drain([FEED], "u1")
    tags.record_entry_tags(FEED, [("a", ["FPS", "Games"])])  # the next refresh re-delivers it
    assert _tags(connect, "a") == [("FPS", "feed"), ("Games", "feed"), ("Krafton", "page")]
    assert sorted(tags.get_tags_for_entry(FEED, "a")) == ["FPS", "Games", "Krafton"]


def test_a_refusal_pauses_the_host_and_its_other_entries_wait(tmp_path):
    connect = _meta(tmp_path)
    _set_folder(connect, on=True)
    a, b = _entry("a"), _entry("b")
    svc, calls, _ = _service(connect, [a, b], responses={a.link: ("", 403)})
    svc.on_entry_updated(a, is_new=True)
    svc.on_entry_updated(b, is_new=True)
    svc.drain([FEED], "u1")
    assert calls == [a.link]  # b not fetched: host paused
    assert _queued(connect) == ["b"]
    assert _tags(connect, "a") == []


def test_turning_it_off_drops_the_backlog(tmp_path):
    connect = _meta(tmp_path)
    _set_folder(connect, on=True)
    e = _entry("a")
    svc, calls, _ = _service(connect, [e])
    svc.on_entry_updated(e, is_new=True)
    _set_folder(connect, on=False)
    svc.drain([FEED], "u1")
    assert calls == [] and _queued(connect) == []
