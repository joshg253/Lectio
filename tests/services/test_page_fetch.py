"""services/page_fetch.py — the shared honest->browser->proxy->flaresolverr
escalation ladder for single-URL page fetches (tag/lead-image scraping and
the saved-article re-fetch path). See tests/services/test_flaresolverr_protocol.py
for the wire-protocol layer this builds on, and
tests/services/test_feed_fetch_escalation.py for the unrelated feed-refresh
ladder this deliberately does NOT share code with.
"""
from __future__ import annotations

import httpx
import pytest

from services import bot_challenge, flaresolverr, page_fetch, url_guard

OFF_BACKENDS = page_fetch.FetchBackends(mode="off", proxy_url="", flaresolverr_url="")


def _patch_build_client(monkeypatch, handler, captured=None):
    """Route url_guard.build_client through a MockTransport, capturing the
    kwargs each tier built its client with (so a test can assert e.g. the
    proxy tier's client was actually constructed with proxy=<url>) without
    needing a real proxy connection — passing proxy= to a real httpx.Client
    makes it build its own SOCKS transport that ignores an injected
    MockTransport entirely, confirmed empirically."""
    transport = httpx.MockTransport(handler)

    def _factory(**kwargs):
        if captured is not None:
            captured.append(dict(kwargs))
        safe_kwargs = {k: v for k, v in kwargs.items() if k in ("headers", "timeout")}
        return httpx.Client(transport=transport, **safe_kwargs)

    monkeypatch.setattr(url_guard, "build_client", _factory)


def _fetcher(*, backends=OFF_BACKENDS, state=None, user_id="u1", clock=None):
    return page_fetch.PageFetcher(
        backends=lambda: backends,
        honest_user_agent="Lectio/0.1",
        user_id=lambda: user_id,
        state=state,
        clock=clock or (lambda: 0.0),
    )


def _is_browser_ua(request: httpx.Request) -> bool:
    return "Mozilla" in request.headers.get("user-agent", "")


CLOUDFLARE_BODY = "<html><head><title>Attention Required! | Cloudflare</title></head><body>blocked</body></html>"


def test_escalates_honest_to_browser_then_succeeds(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_is_browser_ua(request))
        if not _is_browser_ua(request):
            return httpx.Response(403, text="nope", headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    fetcher = _fetcher()
    result = fetcher.fetch("https://example.com/page")
    assert result.status == 200
    assert result.tier == "browser"
    assert calls == [False, True]


def test_no_escalation_on_immediate_success(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    result = _fetcher().fetch("https://example.com/page")
    assert result.status == 200
    assert result.tier == "honest"
    assert len(calls) == 1


def test_non_refusal_status_stops_immediately(monkeypatch):
    """404 isn't in the default refusal set — treated as the real answer, not
    something a different identity would fix."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="gone", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    with pytest.raises(page_fetch.PageFetchError) as exc_info:
        _fetcher().fetch("https://example.com/page")
    assert exc_info.value.status_code == 404
    assert len(calls) == 1


def test_proxy_rung_skipped_when_not_configured(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, text="nope", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    with pytest.raises(page_fetch.PageFetchError):
        _fetcher(backends=OFF_BACKENDS).fetch("https://example.com/page")
    assert len(calls) == 2  # honest, browser — no proxy attempt, none configured


def test_proxy_rung_used_when_configured_and_reaches_the_client(monkeypatch):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        if len(captured) < 3:
            return httpx.Response(403, text="nope", headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler, captured=captured)
    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="socks5h://gluetun:1080", flaresolverr_url="")
    result = _fetcher(backends=backends).fetch("https://example.com/page")
    assert result.status == 200
    assert result.tier == "proxy"
    assert captured[2].get("proxy") == "socks5h://gluetun:1080"
    assert "proxy" not in captured[0]
    assert "proxy" not in captured[1]


def test_flaresolverr_not_attempted_on_plain_403(monkeypatch):
    solve_calls = []
    monkeypatch.setattr(flaresolverr, "solve", lambda *a, **kw: solve_calls.append(1))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="plain refusal, no vendor marker", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="", flaresolverr_url="http://flaresolverr:8191/v1")
    with pytest.raises(page_fetch.PageFetchError):
        _fetcher(backends=backends).fetch("https://example.com/page")
    assert solve_calls == []


def test_flaresolverr_attempted_on_cloudflare_challenge_marker(monkeypatch):
    solve_calls = []

    def fake_solve(endpoint, target_url, **kw):
        solve_calls.append((endpoint, target_url, kw))
        return flaresolverr.Solution(html="<html>solved</html>", status=200, url=target_url)

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=CLOUDFLARE_BODY, headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="", flaresolverr_url="http://flaresolverr:8191/v1")
    result = _fetcher(backends=backends).fetch("https://example.com/page")
    assert result.status == 200
    assert result.tier == "flaresolverr"
    assert result.html == "<html>solved</html>"
    assert len(solve_calls) == 1
    assert solve_calls[0][1] == "https://example.com/page"


def test_flaresolverr_origin_status_propagates_through_pagefetcherror(monkeypatch):
    def fake_solve(endpoint, target_url, **kw):
        return flaresolverr.Solution(html="<html>still blocked</html>", status=403, url=target_url)

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=CLOUDFLARE_BODY, headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="", flaresolverr_url="http://flaresolverr:8191/v1")
    with pytest.raises(page_fetch.PageFetchError) as exc_info:
        _fetcher(backends=backends).fetch("https://example.com/page")
    assert exc_info.value.status_code == 403
    assert exc_info.value.tier == "flaresolverr"
    assert exc_info.value.response is not None
    assert exc_info.value.response.status_code == 403


def test_best_response_wins_across_rungs(monkeypatch):
    """A later attempt with a DIFFERENT status than the current best always
    replaces it — new information either way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if not _is_browser_ua(request):
            return httpx.Response(404, text="honest sees 404", headers={"content-type": "text/html"})
        return httpx.Response(403, text="browser sees 403", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    with pytest.raises(page_fetch.PageFetchError) as exc_info:
        _fetcher().fetch("https://example.com/page", refusal_statuses=frozenset({403, 404, 429, 503, 415}))
    # honest=404 escalates (404 in the widened refusal set) -> browser=403
    # replaces it (different status) -> no proxy/flaresolverr configured, so
    # 403/browser is what's reported.
    assert exc_info.value.status_code == 403
    assert exc_info.value.tier == "browser"


def test_best_response_prefers_deepest_tier_on_a_tie(monkeypatch):
    """Same status at every rung — "even FlareSolverr got 403" is more
    diagnostic than "the honest fetch got 403" (no cheaper tier would ever
    have helped either), so the deepest tier tried is what's reported."""

    def fake_solve(endpoint, target_url, **kw):
        return flaresolverr.Solution(html=CLOUDFLARE_BODY, status=403, url=target_url)

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=CLOUDFLARE_BODY, headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="socks5h://gluetun:1080", flaresolverr_url="http://flaresolverr:8191/v1")
    with pytest.raises(page_fetch.PageFetchError) as exc_info:
        _fetcher(backends=backends).fetch("https://example.com/page")
    assert exc_info.value.status_code == 403
    assert exc_info.value.tier == "flaresolverr"


def test_ignore_cooldown_bypasses_a_recorded_block(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, text="nope", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    state = page_fetch.HostEscalationState()
    fetcher = _fetcher(state=state)
    with pytest.raises(page_fetch.PageFetchError):
        fetcher.fetch("https://example.com/page")
    first_round = len(calls)
    assert first_round == 2  # honest + browser, host now in cooldown

    with pytest.raises(page_fetch.PageFetchError):
        fetcher.fetch("https://example.com/page")
    assert len(calls) == first_round  # cooldown short-circuited — no new attempts

    with pytest.raises(page_fetch.PageFetchError):
        fetcher.fetch("https://example.com/page", ignore_cooldown=True)
    assert len(calls) == first_round + 2  # ignore_cooldown re-ran the ladder


def test_cooldown_lifted_once_a_deeper_tier_becomes_available():
    state = page_fetch.HostEscalationState(cooldown_seconds=6 * 3600)
    state.record_block("u1", "example.com", deepest_available="browser", challenge=None, now=100.0)
    assert state.is_blocked("u1", "example.com", best_available="browser", now=101.0) is True
    # A proxy URL is now configured — browser was the ceiling when we gave up,
    # proxy ranks deeper, so the cooldown no longer applies.
    assert state.is_blocked("u1", "example.com", best_available="proxy", now=101.0) is False


def test_learned_flaresolverr_tier_shortcuts_straight_there(monkeypatch):
    httpx_calls = []

    def fake_solve(endpoint, target_url, **kw):
        return flaresolverr.Solution(html="<html>solved</html>", status=200, url=target_url)

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    def handler(request: httpx.Request) -> httpx.Response:
        httpx_calls.append(1)
        return httpx.Response(403, text=CLOUDFLARE_BODY, headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    state = page_fetch.HostEscalationState()
    state.record_success("u1", "example.com", "flaresolverr")
    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="", flaresolverr_url="http://flaresolverr:8191/v1")
    result = _fetcher(backends=backends, state=state).fetch("https://example.com/page")
    assert result.tier == "flaresolverr"
    assert httpx_calls == []  # went straight to flaresolverr, no honest/browser attempt


def test_max_tier_clamp_prevents_flaresolverr_even_when_challenged(monkeypatch):
    solve_calls = []
    monkeypatch.setattr(flaresolverr, "solve", lambda *a, **kw: solve_calls.append(1))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=CLOUDFLARE_BODY, headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    backends = page_fetch.FetchBackends(mode="as_needed", proxy_url="socks5h://gluetun:1080", flaresolverr_url="http://flaresolverr:8191/v1")
    with pytest.raises(page_fetch.PageFetchError) as exc_info:
        _fetcher(backends=backends).fetch("https://example.com/page", max_tier="proxy")
    assert solve_calls == []
    assert exc_info.value.tier == "proxy"


def test_unsafe_url_propagates_without_any_attempt(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="unreachable")

    _patch_build_client(monkeypatch, handler)
    with pytest.raises(url_guard.UnsafeURLError):
        _fetcher().fetch("http://127.0.0.1:8000/internal")
    assert calls == []


def test_unsafe_redirect_hop_propagates_not_wrapped(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/internal"})

    _patch_build_client(monkeypatch, handler)
    with pytest.raises(url_guard.UnsafeURLError):
        _fetcher().fetch("https://example.com/redirects-to-internal")


def test_successful_fetch_records_learned_tier(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if not _is_browser_ua(request):
            return httpx.Response(403, text="nope", headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    _patch_build_client(monkeypatch, handler)
    state = page_fetch.HostEscalationState()
    _fetcher(state=state).fetch("https://example.com/page")
    assert state.learned_tier("u1", "example.com") == "browser"


def test_challenge_body_recognized_by_bot_challenge_module():
    """Sanity check that the fixture body used above is actually recognized —
    otherwise the flaresolverr-gating tests would be vacuously true."""
    assert bot_challenge.detect_challenge("text/html", CLOUDFLARE_BODY.encode()) == "Cloudflare block"
