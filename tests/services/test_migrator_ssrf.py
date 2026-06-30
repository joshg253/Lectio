"""SSRF guard coverage for the migration source clients.

The FreshRSS / Miniflux / tt-rss importers fetch a user-supplied server URL, so
each must refuse private / loopback / link-local targets before issuing any
request (CodeQL: full SSRF). The guard lives in each service's URL builder, so a
private base URL raises UnsafeURLError without a network call ever happening.
"""
from __future__ import annotations

import httpx
import pytest

from services import freshrss, miniflux_import, ttrss, url_guard


@pytest.fixture(autouse=True)
def _no_debug_no_network(monkeypatch):
    # Ensure the LECTIO_DEBUG bypass is off so the guard actually runs.
    monkeypatch.setattr(url_guard, "_debug_bypass_enabled", lambda: False)
    # Any actual outbound request in these tests is a bug — make it explode.
    def _boom(*_a, **_k):
        raise AssertionError("network request must not happen for an unsafe URL")
    monkeypatch.setattr(httpx.Client, "get", _boom)
    monkeypatch.setattr(httpx.Client, "post", _boom)


def test_freshrss_refuses_loopback():
    with pytest.raises(url_guard.UnsafeURLError):
        freshrss.login("http://127.0.0.1", "user", "pass")


def test_miniflux_refuses_link_local_metadata():
    with pytest.raises(url_guard.UnsafeURLError):
        miniflux_import.test_connection("http://169.254.169.254", "token")


def test_ttrss_refuses_private_ip():
    with pytest.raises(url_guard.UnsafeURLError):
        ttrss.login("http://192.168.1.10", "user", "pass")
