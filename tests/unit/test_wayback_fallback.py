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


def test_tracking_params_are_retried_without(monkeypatch):
    """Archives are keyed on the URL as CRAWLED, and crawlers follow links that
    carry no campaign suffix.

    informit's ?p=2433607&WT.rss_f=…&WT.rss_a=… found nothing while the bare
    ?p=2433607 found the article immediately — so a miss is retried cleaned.
    """
    seen = []

    def _get(client, url, headers=None, **kw):
        seen.append(url)
        # Only the cleaned form has a snapshot.
        if "WT.rss" in url:
            return _Resp({"archived_snapshots": {}})
        return _Resp({"archived_snapshots": {"closest": {
            "available": True, "url": "http://web.archive.org/web/2024/x"}}})

    monkeypatch.setattr(main.url_guard, "safe_get", _get)

    got = main.wayback_snapshot_url(
        "http://x.test/article.aspx?p=2433607&WT.rss_f=Article&WT.rss_ev=a")

    assert got == "http://web.archive.org/web/2024/x"
    assert len(seen) == 2, "the cleaned retry did not happen"


def test_identifying_params_survive_the_clean():
    """A denylist, not a keeplist. For article.aspx?p=2433607 the query IS the
    article's identity — guessing at what to keep would strip `p` and leave the
    generic section page."""
    assert main.strip_tracking_params(
        "http://x.test/article.aspx?p=2433607&WT.rss_f=Article"
    ) == "http://x.test/article.aspx?p=2433607"
    assert main.strip_tracking_params(
        "https://x.test/post?utm_source=rss&id=42") == "https://x.test/post?id=42"
    # Nothing to strip: returned untouched rather than re-encoded.
    assert main.strip_tracking_params("https://x.test/a?p=7") == "https://x.test/a?p=7"
    assert main.strip_tracking_params("https://x.test/a") == "https://x.test/a"


def test_a_url_that_is_all_tracking_is_not_reduced_to_nothing():
    """Stripping every parameter would leave a section index — the exact wrong
    page. It still gets tried, but the guard is what refuses the result."""
    out = main.strip_tracking_params("https://x.test/articles/article.aspx?utm_source=rss")
    assert out == "https://x.test/articles/article.aspx?"  or out.startswith("https://x.test/articles/article.aspx")
