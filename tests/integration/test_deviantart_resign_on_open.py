"""DeviantArt signs mature deviations' images for ~15 minutes, so a stored URL
is usually dead by the time the post is read — the entry then shows neither
image nor thumbnail. A scheduled re-sign can't help at that lifetime, so it
happens when the post is opened.

The cost control is the point of these tests: a permanently-signed image and one
whose bytes the proxy already holds must never reach the API, or every render of
a DeviantArt-heavy list would fan out into hundreds of calls.
"""
from __future__ import annotations

import base64
import html as H
import json
import time

import httpx
import pytest

import main

FEED = "file:///data/deviantart-feeds/test.xml"
ENTRY = "CC48A953-A2AA-B35F-4B30-B33108F953A0"
FRESH = "https://images-wixmp.wixmp.com/f/a/b.png/v1/fill/x.jpg?token=fresh.token.sig"


def _token(exp: int | None) -> str:
    header = base64.urlsafe_b64encode(b'{"typ":"JWT","alg":"HS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({} if exp is None else {"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _body(exp: int | None) -> str:
    url = f"https://images-wixmp.wixmp.com/f/a/b.png/v1/fill/x.jpg?token={_token(exp)}"
    return f'<p><img alt="t" src="{H.escape(url, quote=True)}"></p>'


@pytest.fixture
def api(monkeypatch):
    """Count API calls; never touch the network or the DB."""
    calls: list[str] = []

    def _fetch(deviation_id, access_token):
        calls.append(deviation_id)
        return FRESH

    monkeypatch.setattr(main.deviantart_service, "fetch_fresh_image_url", _fetch)
    monkeypatch.setattr(main, "get_deviantart_user_token", lambda: "tok")
    monkeypatch.setattr(main, "_img_cache_has", lambda url: False)
    # No real network in tests that don't care about the liveness check itself.
    monkeypatch.setattr(main, "_wixmp_url_is_live", lambda url: True)
    monkeypatch.setattr(main, "get_reader", _noop_reader)
    return calls


def test_expired_image_is_resigned(api):
    out = main._resign_expired_deviantart_images(_body(int(time.time()) - 60), FEED, ENTRY)
    assert "token=fresh.token.sig" in H.unescape(out)
    assert api == [ENTRY]


def test_valid_token_never_calls_the_api(api):
    body = _body(int(time.time()) + 3600)
    assert main._resign_expired_deviantart_images(body, FEED, ENTRY) == body
    assert api == []


def test_no_exp_claim_but_live_never_calls_the_api(api):
    """The vast majority of images on the live library carry no exp claim at
    all — those must not cost a DeviantArt API call on every open, only the
    cheap liveness HEAD (mocked live by the fixture)."""
    body = _body(None)
    assert main._resign_expired_deviantart_images(body, FEED, ENTRY) == body
    assert api == []


def test_no_exp_claim_and_dead_is_resigned(api, monkeypatch):
    """A token with no exp claim is NOT necessarily permanent — one turned out
    to already be dead with nothing in the token to say so. The liveness check
    failing must fall through to a real re-sign, not be trusted as-is."""
    monkeypatch.setattr(main, "_wixmp_url_is_live", lambda url: False)
    out = main._resign_expired_deviantart_images(_body(None), FEED, ENTRY)
    assert "token=fresh.token.sig" in H.unescape(out)
    assert api == [ENTRY]


def test_cached_bytes_make_the_token_moot(api, monkeypatch):
    """The proxy's cache key drops the signing token, so once the bytes are
    cached the image renders regardless of expiry — no re-sign needed."""
    monkeypatch.setattr(main, "_img_cache_has", lambda url: True)
    body = _body(int(time.time()) - 60)
    assert main._resign_expired_deviantart_images(body, FEED, ENTRY) == body
    assert api == []


def test_non_deviantart_content_is_untouched(api):
    body = '<p><img src="https://example.com/a.jpg"></p>'
    assert main._resign_expired_deviantart_images(body, FEED, ENTRY) == body
    assert api == []


def test_no_token_available_leaves_the_url_alone(api, monkeypatch):
    """A disconnected DeviantArt account must degrade to the status quo, not an
    exception on every render."""
    monkeypatch.setattr(main, "get_deviantart_user_token", lambda: "")
    body = _body(int(time.time()) - 60)
    assert main._resign_expired_deviantart_images(body, FEED, ENTRY) == body
    assert api == []


# ── _wixmp_url_is_live ────────────────────────────────────────────────────────

def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return _factory


@pytest.fixture(autouse=True)
def _dns_bypass(monkeypatch):
    """Hermetic: the liveness HEAD's SSRF check does a real DNS lookup on the
    wixmp hostname otherwise."""
    monkeypatch.setattr(main.url_guard, "is_safe_outbound_url", lambda url: True)


def test_wixmp_live_check_true_on_200_image(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(
        lambda r: httpx.Response(200, headers={"content-type": "image/png"})))
    assert main._wixmp_url_is_live("https://images-wixmp.wixmp.com/f/a/b.png?token=x") is True


def test_wixmp_live_check_false_on_error_response(monkeypatch):
    """The exact shape observed live: wixmp answers 400 text/plain for a dead
    token, not a 404 or a redirect."""
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(
        lambda r: httpx.Response(400, headers={"content-type": "text/plain"}, text="image is invalid")))
    assert main._wixmp_url_is_live("https://images-wixmp.wixmp.com/f/a/b.png?token=x") is False


def test_wixmp_live_check_false_on_network_error(monkeypatch):
    def _raise(request):
        raise httpx.ConnectError("boom", request=request)
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(_raise))
    assert main._wixmp_url_is_live("https://images-wixmp.wixmp.com/f/a/b.png?token=x") is False


def test_wixmp_live_check_false_when_ssrf_guard_rejects(monkeypatch):
    monkeypatch.setattr(main.url_guard, "is_safe_outbound_url", lambda url: False)
    assert main._wixmp_url_is_live("https://images-wixmp.wixmp.com/f/a/b.png?token=x") is False


class _NoopDB:
    def execute(self, *a, **k):
        return None

    def commit(self):
        pass


class _NoopReader:
    _storage = type("S", (), {"get_db": staticmethod(lambda: _NoopDB())})()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _noop_reader():
    return _NoopReader()
