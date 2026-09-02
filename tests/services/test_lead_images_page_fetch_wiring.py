"""End-to-end wiring for the tag/lead-image page-fetch gap (Plan.md): a
Cloudflare-walled article page should still get its tags captured, via
services/page_fetch.py's FlareSolverr tier, through
LeadImageService.queue_source_html_fetch -> the page-tag sink — the same
path a real feed like gottadeal.com hits (feed ships zero <category> data,
article page is Cloudflare-walled).

tests/services/test_page_fetch.py covers the ladder itself in isolation;
this covers that LeadImageService actually reaches it end to end, including
tenancy: the background thread must resolve backends for the SAME user that
queued the fetch, not whichever user happens to be bound when the ladder
resolves settings.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import httpx

from services import flaresolverr, page_fetch, tenancy
from services.lead_images import LeadImageService

CLOUDFLARE_BODY = "<html><head><title>Attention Required! | Cloudflare</title></head><body>blocked</body></html>"


def _patch_build_client(monkeypatch, handler):
    import services.page_fetch as pf

    transport = httpx.MockTransport(handler)

    def _factory(**kwargs):
        safe_kwargs = {k: v for k, v in kwargs.items() if k in ("headers", "timeout")}
        return httpx.Client(transport=transport, **safe_kwargs)

    monkeypatch.setattr(pf.url_guard, "build_client", _factory)


def _svc(tmp_path: Path, *, page_fetcher: page_fetch.PageFetcher) -> LeadImageService:
    def get_meta():
        c = sqlite3.connect(str(tmp_path / "m.sqlite"))
        c.row_factory = sqlite3.Row
        return c

    return LeadImageService(
        get_meta_connection=get_meta,
        get_reader=lambda: None,
        user_agent="LectioTest/1.0",
        extract_video_id=lambda link: None,
        page_fetcher=page_fetcher,
    )


def test_challenged_page_still_yields_tags_via_flaresolverr(tmp_path, monkeypatch):
    def fake_solve(endpoint, target_url, **kw):
        page = '<html><body><article><a rel="tag" href="/tag/x">Steam Deck</a></article></body></html>'
        return flaresolverr.Solution(html=page, status=200, url=target_url)

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=CLOUDFLARE_BODY, headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)

    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="", flaresolverr_url="http://flaresolverr:8191/v1")
    fetcher = page_fetch.PageFetcher(backends=lambda: backends, honest_user_agent="LectioTest/1.0")
    svc = _svc(tmp_path, page_fetcher=fetcher)

    sunk: list[tuple[str, str, str, str]] = []
    svc.set_page_tag_sink(lambda feed_url, entry_id, html, source_url: sunk.append((feed_url, entry_id, html, source_url)))

    svc.queue_source_html_fetch("https://example.com/post", feed_url="https://example.com/feed", entry_id="e1")
    assert svc.wait_for_source_html_fetch("https://example.com/post", timeout=3.0)

    assert len(sunk) == 1
    feed_url, entry_id, html, source_url = sunk[0]
    assert feed_url == "https://example.com/feed"
    assert entry_id == "e1"
    assert 'rel="tag"' in html
    assert source_url == "https://example.com/post"


def test_background_fetch_resolves_backends_for_the_queuing_user(tmp_path, monkeypatch):
    """queue_source_html_fetch captures tenancy.current_user_id() at queue
    time and rebinds it in the background thread (see its own docstring) —
    the backends provider must see that SAME user, not whatever's bound (or
    unbound) by the time the thread actually runs."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)

    seen_uids: list[str] = []

    def backends() -> page_fetch.FetchBackends:
        seen_uids.append(tenancy.current_user_id())
        return page_fetch.FetchBackends(mode="off", proxy_url="", flaresolverr_url="")

    fetcher = page_fetch.PageFetcher(backends=backends, honest_user_agent="LectioTest/1.0")
    svc = _svc(tmp_path, page_fetcher=fetcher)

    with tenancy.user_context("u_alice"):
        svc.queue_source_html_fetch("https://example.com/post")

    # queue_source_html_fetch returns immediately (background thread) —
    # wait_for_source_html_fetch only works from within the same call stack
    # in real use, but here we just poll the event/cache directly.
    for _ in range(50):
        if "https://example.com/post" in svc._source_html_cache:
            break
        time.sleep(0.05)

    assert seen_uids == ["u_alice"]


def test_background_fetch_does_not_leak_into_other_threads_default_user(tmp_path, monkeypatch):
    """Sanity check on the fixture above: without the user_context binding,
    the background thread (and thus the backends provider) sees the
    unbound default, not 'u_alice' — confirming the previous test's
    assertion is actually exercising the rebind, not a coincidence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)

    seen_uids: list[str] = []
    done = threading.Event()

    def backends() -> page_fetch.FetchBackends:
        seen_uids.append(tenancy.current_user_id())
        done.set()
        return page_fetch.FetchBackends(mode="off", proxy_url="", flaresolverr_url="")

    fetcher = page_fetch.PageFetcher(backends=backends, honest_user_agent="LectioTest/1.0")
    svc = _svc(tmp_path, page_fetcher=fetcher)

    svc.queue_source_html_fetch("https://example.com/post2")
    assert done.wait(timeout=3.0)
    assert seen_uids == [tenancy.DEFAULT_USER_ID]
