"""Characterization tests for get_entry_detail — the 851-line entry-render pipeline.

These pin the OBSERVABLE behavior of the returned detail dict across the major
branches (content cleanups, lead image, audio/attachments, title/link recovery,
gallery injection, sync flags) so the planned decomposition of the function can be
verified behavior-preserving. Hermetic: all source-page/network fetches are stubbed.
"""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://ex.test/feed"
BASE = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stub every outbound fetch so get_entry_detail stays hermetic."""
    li = main.lead_image_service
    monkeypatch.setattr(li, "queue_source_fetch", lambda *a, **k: None)
    monkeypatch.setattr(li, "wait_for_source_fetch", lambda *a, **k: False)
    monkeypatch.setattr(li, "queue_source_html_fetch", lambda *a, **k: None)
    monkeypatch.setattr(li, "wait_for_source_html_fetch", lambda *a, **k: False)
    # No-op the async lead-image persistence: its background writer thread would
    # otherwise race a meta-DB write into the *next* test (database is locked). The
    # dict output we characterize doesn't depend on persistence.
    monkeypatch.setattr(li, "persist_lead_image_async", lambda *a, **k: None)
    monkeypatch.setattr(li, "persist_image_alt_async", lambda *a, **k: None)
    # No-op the background media-audio scan: for no-audio entries get_entry_detail
    # otherwise spawns a thread that re-parses the raw feed (network) AND writes the
    # meta DB concurrently — the source of intermittent "database is locked".
    monkeypatch.setattr(main, "_queue_media_audio_scan", lambda *a, **k: None)
    # _lead_image_display_url spawns a background CORP HEAD for unknown domains.
    import httpx
    monkeypatch.setattr(httpx, "head", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.ensure_yt_duration_schema()
    # lead_image_service is a process-global singleton; clear its in-memory caches so
    # one test's resolved/negative image (or pinned strategy) can't leak into another
    # (tests reuse the same feed/entry ids over fresh per-test tmp DBs).
    li = main.lead_image_service
    for _attr in ("_cache", "_fetched_at_cache", "_alt_cache", "_title_cache",
                  "_entry_crop_cache", "_source_fetch_in_progress", "_source_fetch_events",
                  "_debug_bypass_feeds", "_source_html_cache", "_waf_block_until",
                  "_source_html_fetch_events"):
        getattr(li, _attr).clear()
    li._webcomic_feeds = None
    li._none_strategy_feeds = None
    # Drain any lead-image write callables queued by *other* tests: the shared write
    # worker resolves get_meta_connection() against the current global tenancy at run
    # time, so a stale write would land on THIS test's tmp meta DB and lock it.
    import queue as _queue
    while True:
        try:
            li._write_queue.get_nowait()
            li._write_queue.task_done()
        except _queue.Empty:
            break
    try:
        yield
    finally:
        main._reader_thread_local.pool = None
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def _add(entry_id="e1", *, feed=FEED, title="Title", link="https://ex.test/p1",
         summary=None, content=None, enclosures=None):
    reader = main.get_reader()
    try:
        reader.add_feed(feed, allow_invalid_url=True)
    except Exception:
        pass
    data = {"feed_url": feed, "id": entry_id, "title": title, "link": link, "published": BASE}
    if summary is not None:
        data["summary"] = summary
    if content is not None:
        data["content"] = [{"value": content, "type": "text/html"}]
    if enclosures is not None:
        data["enclosures"] = enclosures
    reader.add_entry(data)
    return entry_id


def _detail(entry_id="e1", feed=FEED):
    return main.get_entry_detail(feed, entry_id)


# --- basic shape -----------------------------------------------------------

def test_plain_content_renders(env):
    _add(content="<p>Hello <strong>world</strong></p>")
    d = _detail()
    assert d["title"] == "Title"
    assert d["link"] == "https://ex.test/p1"
    assert "Hello" in d["content_html"]
    assert "<strong>world</strong>" in d["content_html"]


# --- content cleanups ------------------------------------------------------

def test_wordpress_footer_stripped(env):
    _add(content='<p>Body.</p><p>The post <a href="x">T</a> appeared first on <a href="y">Site</a>.</p>')
    d = _detail()
    assert "Body." in d["content_html"]
    assert d["content_html"].lower().count("the post") <= 1


def test_qwantz_nav_stripped(env):
    qw = (
        '<center><table><tr><td colspan=4><a href="http://www.qwantz.com/archive.php">archive</a></td></tr></table>'
        '<img src="http://www.qwantz.com/comics/c.png" class="comic" title="secret">'
        '<table><tr><td colspan=3><b>June:</b> commentary</td></tr></table></center>'
    )
    _add(feed="https://qwantz.com/rssfeed.php", link="http://www.qwantz.com/index.php?comic=1",
         summary=qw)
    d = main.get_entry_detail("https://qwantz.com/rssfeed.php", "e1")
    assert "commentary" in d["content_html"]
    assert "archive.php" not in (d["content_html"] or "")


# --- title / link recovery -------------------------------------------------

def test_blogger_untitled_recovers_title_from_slug(env):
    _add(feed="https://x.blogspot.com/feeds/posts/default", title="",
         link="https://x.blogspot.com/2026/06/gin-rummy-essential.html", content="<p>x</p>")
    d = main.get_entry_detail("https://x.blogspot.com/feeds/posts/default", "e1")
    assert d["title"] == "Gin Rummy Essential"


def test_buzzsprout_link_derived_from_enclosure(env):
    _add(feed="https://rss.buzzsprout.com/1.rss", title="Ep", link=None,
         summary="<p>notes</p>",
         enclosures=[{"href": "https://www.buzzsprout.com/1/episodes/2-x.mp3", "type": "audio/mpeg", "length": 9999999}])
    d = main.get_entry_detail("https://rss.buzzsprout.com/1.rss", "e1")
    assert d["link"] == "https://www.buzzsprout.com/1/episodes/2-x"


# --- audio / attachments ---------------------------------------------------

def test_audio_enclosure_injects_player(env):
    _add(content="<p>show notes</p>",
         enclosures=[{"href": "https://cdn.test/ep1.mp3", "type": "audio/mpeg", "length": 9999999}])
    d = _detail()
    assert "<audio" in d["content_html"]
    assert "/entries/media/audio" in d["content_html"]


def test_pdf_enclosure_listed_as_attachment(env):
    _add(content="<p>issue</p>",
         enclosures=[{"href": "https://dl.test/mag.pdf", "type": "application/pdf", "length": 5000000}])
    d = _detail()
    assert "Attachments" in d["content_html"]
    assert "mag.pdf" in d["content_html"]


def test_image_enclosure_not_attachment_but_lead(env):
    # gottadeal case: image enclosure becomes the lead image, not a download link.
    _add(content="<p>deal</p>",
         enclosures=[{"href": "https://cdn.test/deal.jpg", "type": "image/jpeg", "length": 200000}])
    d = _detail()
    assert "Attachments" not in (d["content_html"] or "")
    assert d["lead_image_url"] and "deal.jpg" in d["lead_image_url"]


# --- sync flag -------------------------------------------------------------

def test_sync_list_thumb_true_for_normal_feed(env):
    _add(content='<p><img src="https://cdn.test/a.jpg"></p>')
    assert _detail()["sync_list_thumb"] is True


def test_sync_list_thumb_false_for_webcomic(env):
    _add(content='<p><img src="https://cdn.test/strip.jpg"></p>')
    # store_feed_strategy manages its own meta connection — don't nest one here.
    main.lead_image_service.store_feed_strategy(FEED, "webcomic", manual=True)
    assert _detail()["sync_list_thumb"] is False


# --- inject source gallery -------------------------------------------------

def test_inject_source_images_gallery(env, monkeypatch):
    _add(content="<p>text only</p>", link="https://site.test/post")
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "inject_source_images", 1)
    monkeypatch.setattr(
        main.lead_image_service, "extract_source_gallery_urls",
        lambda link, exclude_urls=None, **k: ["https://site.test/g1.jpg", "https://site.test/g2.jpg"],
    )
    d = _detail()
    assert "source-gallery" in d["content_html"]
    assert "g1.jpg" in d["content_html"] and "g2.jpg" in d["content_html"]


def test_no_gallery_when_pref_off(env, monkeypatch):
    _add(content="<p>text only</p>", link="https://site.test/post")
    monkeypatch.setattr(
        main.lead_image_service, "extract_source_gallery_urls",
        lambda *a, **k: ["https://site.test/should-not-appear.jpg"],
    )
    d = _detail()
    assert "source-gallery" not in (d["content_html"] or "")


# --- missing entry ---------------------------------------------------------

def test_missing_entry_returns_orphan_or_none(env):
    d = main.get_entry_detail(FEED, "does-not-exist")
    # Either None or an orphan-detail dict, but never raises.
    assert d is None or isinstance(d, dict)
