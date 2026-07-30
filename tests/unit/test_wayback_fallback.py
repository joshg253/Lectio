"""Wayback lookup used as a re-fetch fallback.

When a publisher serves a parked page or a section index over its own article URL,
the re-fetch guard correctly refuses it — and on its own that leaves the user with
nothing better. Josh found the real article by hand on web.archive.org for two
separate entries, which is the case this automates.
"""
from __future__ import annotations

import pytest

import main


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_get(monkeypatch):
    calls = []

    def _install(payload, status=200):
        def _get(client, url, headers=None, **kw):
            calls.append(url)
            return _Resp(payload, status)
        monkeypatch.setattr(main.url_guard, "safe_get", _get)
        return calls
    return _install


def test_returns_the_closest_snapshot(fake_get):
    calls = fake_get({"archived_snapshots": {"closest": {
        "available": True,
        "url": "http://web.archive.org/web/20241112100517/https://x.test/a",
    }}})

    got = main.wayback_snapshot_url("https://x.test/a")

    assert got == "http://web.archive.org/web/20241112100517/https://x.test/a"
    assert calls and "archive.org/wayback/available" in calls[0]
    assert "x.test" in calls[0]          # the URL is passed, encoded


def test_no_snapshot_is_not_an_error(fake_get):
    fake_get({"archived_snapshots": {}})
    assert main.wayback_snapshot_url("https://x.test/a") is None


def test_an_unavailable_snapshot_is_refused(fake_get):
    """archive.org reports the record with available=false; taking its URL anyway
    would fetch a page that is not there."""
    fake_get({"archived_snapshots": {"closest": {"available": False, "url": "http://w/a"}}})
    assert main.wayback_snapshot_url("https://x.test/a") is None


def test_a_failing_lookup_is_swallowed(fake_get):
    """A missing fallback is not an error — the re-fetch has already refused, and
    the stored copy is intact either way."""
    fake_get({}, status=503)
    assert main.wayback_snapshot_url("https://x.test/a") is None


def test_non_http_urls_are_not_looked_up(fake_get):
    calls = fake_get({"archived_snapshots": {}})
    assert main.wayback_snapshot_url("lectio:saved") is None
    assert main.wayback_snapshot_url("") is None
    assert calls == []                    # no request was made
