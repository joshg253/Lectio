"""fetch_readability_article/fetch_full_page_article on the shared
services/page_fetch.py ladder (step 5 of Plan.md's shared proxy/FlareSolverr
helper) — the re-fetch path's half of the gap raised 2026-08-31 on three
tamriel-rebuilt.org entries that 403'd even after the browser-UA retry.

tests/services/test_page_fetch.py covers the ladder itself in isolation and
tests/services/test_refetch_blocked_hosts.py covers dead-vs-blocked
classification against a synthetic extract() double; this covers the real
main.fetch_readability_article/_refresh_captured_article_for_current_user
wiring end to end, including that a PageFetchError's status_code/.response
duck-types the same way an httpx.HTTPStatusError always did.
"""
from __future__ import annotations

import httpx
import pytest

import main
from services import flaresolverr, page_fetch, saved_articles, tenancy

ADMIN_ID = "u_1e57adm1n0000000000000000"
FEED = "https://example.com/feed"
ENTRY = "https://example.com/post"


class _StubUserStore:
    def list_users(self) -> list[dict]:
        return [{"user_id": ADMIN_ID, "username": "admin", "is_admin": True, "disabled": False}]


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
    monkeypatch.setattr(main, "user_store", _StubUserStore())
    monkeypatch.setattr(main, "_ENV_PROXY_URL", "")
    monkeypatch.setattr(main, "_ENV_TAILSCALE_URL", "")
    monkeypatch.setattr(main, "_ENV_FLARESOLVERR_URL", "")
    main.invalidate_instance_setting_cache()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    with main._proxy_down_lock:
        main._proxy_down_until.clear()
    # The page-fetch ladder's own host memory is a process-global singleton
    # (main.page_fetcher) — clear it so one test's learned/blocked state for
    # "example.com" can't leak into the next.
    main.page_fetcher._state.clear()
    main.ensure_meta_schema()
    main.provision_user_storage(ADMIN_ID)
    with tenancy.user_context(ADMIN_ID):
        with main.get_reader() as reader:
            reader.add_feed(FEED, exist_ok=True)
            reader.add_entry({"feed_url": FEED, "id": ENTRY, "title": "post", "link": ENTRY})
    main.invalidate_instance_setting_cache()
    try:
        yield
    finally:
        main.page_fetcher._state.clear()
        main.invalidate_instance_setting_cache()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        with main._proxy_down_lock:
            main._proxy_down_until.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _set_admin_setting(key: str, value: str) -> None:
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.set_setting(conn, key, value)
    main.invalidate_instance_setting_cache()


def _patch_build_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def _factory(**kwargs):
        safe_kwargs = {k: v for k, v in kwargs.items() if k in ("headers", "timeout")}
        return httpx.Client(transport=transport, **safe_kwargs)

    monkeypatch.setattr(page_fetch.url_guard, "build_client", _factory)


def test_404_is_reported_dead_through_the_real_ladder(configured, monkeypatch):
    """The highest-value test in this file: a page_fetch.PageFetchError must
    duck-type .response.status_code exactly like the httpx.HTTPStatusError
    this replaced, or saved_articles.refresh_captured_article's dead-vs-
    blocked classification silently breaks for every real fetch."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)

    with tenancy.user_context(ADMIN_ID), main.get_meta_connection() as conn:
        result = saved_articles.refresh_captured_article(
            main.get_reader(), conn, FEED, ENTRY, extract=main.fetch_readability_article,
        )
    assert result["ok"] is False
    assert result["dead"] is True


def test_403_is_reported_blocked_not_dead_through_the_real_ladder(configured, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)

    with tenancy.user_context(ADMIN_ID), main.get_meta_connection() as conn:
        result = saved_articles.refresh_captured_article(
            main.get_reader(), conn, FEED, ENTRY, extract=main.fetch_readability_article,
        )
    assert result["ok"] is False
    assert result.get("dead") is not True
    assert "blocked the fetch" in result["error"]


def test_reader_view_path_never_reaches_flaresolverr_even_when_configured(configured, monkeypatch):
    """build_readability_response/resolve_reader_article_html run on the
    synchronous request path — fetch_readability_article's max_tier default
    ("proxy") must keep them off the ~55s FlareSolverr rung even when a real
    challenge marker is served and FlareSolverr IS configured."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")

    solve_calls = []
    monkeypatch.setattr(flaresolverr, "solve", lambda *a, **kw: solve_calls.append(1))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text="<html><head><title>Attention Required! | Cloudflare</title></head></html>",
            headers={"content-type": "text/html"},
        )

    _patch_build_client(monkeypatch, handler)

    with tenancy.user_context(ADMIN_ID):
        response = main.build_readability_response(ENTRY)
    assert response.status_code == 200  # the error page, not a raised exception
    assert solve_calls == []


def test_refetch_route_reaches_flaresolverr_when_challenged_and_configured(configured, monkeypatch):
    """The opposite of the previous test: _refresh_captured_article_for_current_user
    (the re-fetch path, never on a request's hot path) opts into the
    FlareSolverr rung."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")

    def fake_solve(endpoint, target_url, **kw):
        return flaresolverr.Solution(html="<html><body>the real article</body></html>", status=200, url=target_url)

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text="<html><head><title>Attention Required! | Cloudflare</title></head></html>",
            headers={"content-type": "text/html"},
        )

    _patch_build_client(monkeypatch, handler)

    with tenancy.user_context(ADMIN_ID):
        result = main._refresh_captured_article_for_current_user(FEED, ENTRY, ignore_cooldown=True)

    assert result["ok"] is True


def test_proxy_tier_success_produces_a_normal_refetch_not_an_archive_fallback(configured, monkeypatch):
    """A live proxy-tier success must satisfy the re-fetch outright — Wayback
    is a last resort, not tried when a live tier already worked."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(403, text="forbidden", headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html><body>the real article, at last</body></html>", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    monkeypatch.setattr(main, "wayback_snapshot_url", lambda url: pytest.fail("should not have asked the archive"))

    with tenancy.user_context(ADMIN_ID):
        result = main._refresh_captured_article_for_current_user(FEED, ENTRY, ignore_cooldown=True)

    assert result["ok"] is True
    assert len(calls) == 3  # honest, browser, proxy — proxy succeeded


def test_wayback_fallback_only_fires_after_every_live_tier_is_exhausted(configured, monkeypatch):
    """No proxy/FlareSolverr configured at all — honest+browser both come back
    404 (dead, which is what actually gates the archive fallback — a plain
    "blocked" refusal does not, see _refresh_captured_article_for_current_user),
    and only THEN does the archive fallback get asked."""
    wayback_calls = []
    monkeypatch.setattr(main, "wayback_snapshot_url", lambda url: wayback_calls.append(url) or None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)

    with tenancy.user_context(ADMIN_ID):
        result = main._refresh_captured_article_for_current_user(FEED, ENTRY, ignore_cooldown=True)

    assert result["ok"] is False
    assert wayback_calls == [ENTRY]  # asked exactly once, after the live ladder gave up
