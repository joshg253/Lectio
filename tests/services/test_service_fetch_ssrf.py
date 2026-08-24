"""SSRF regression tests for service-layer outbound fetches.

These pin the PR-4 migration: scraper_service, lead_images and starred_archive
now route their externally-influenced GETs through url_guard.safe_get, which
re-validates every redirect hop. Before the migration a public page could
302-bounce the fetch to an internal target (e.g. the cloud metadata IP); these
tests confirm that hop is now refused.

NB: only scraper_service and starred_archive have test functions below —
lead_images' fetch path (services/lead_images.py, ~3365/3470) goes through
the same url_guard.safe_get guard but isn't independently exercised here.

httpx.MockTransport + a stubbed is_safe_outbound_url keep this deterministic
(no real DNS or network I/O).
"""
from __future__ import annotations

import httpx
import pytest

from services import scraper_service, starred_archive, url_guard


def _unsafe_host(url: str) -> bool:
    return "169.254.169.254" in url or ".internal" in url


def _handler(request: httpx.Request) -> httpx.Response:
    routes = {
        "https://pub.test/page": httpx.Response(200, content=b"<title>ok</title>"),
        "https://pub.test/redir-evil": httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data"}
        ),
        "http://169.254.169.254/latest/meta-data": httpx.Response(200, content=b"SECRET"),
    }
    return routes.get(str(request.url), httpx.Response(404))


@pytest.fixture
def mock_net(monkeypatch):
    """Block obviously-internal targets and force every httpx.Client created in
    the services under test onto the in-memory MockTransport."""
    monkeypatch.setattr(
        url_guard, "is_safe_outbound_url", lambda u: not _unsafe_host(u)
    )

    real_client = httpx.Client

    def _patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    # httpx is a singleton module — patching Client here reaches every module
    # that did `import httpx`, scraper_service/lead_images/starred_archive
    # included, without needing a handle through each one individually.
    monkeypatch.setattr(httpx, "Client", _patched)


def test_scraper_fetch_blocks_internal_redirect(mock_net):
    with pytest.raises(url_guard.UnsafeURLError):
        scraper_service._fetch_html("https://pub.test/redir-evil")


def test_scraper_fetch_allows_public(mock_net):
    assert "ok" in scraper_service._fetch_html("https://pub.test/page")


def test_starred_archive_fetch_blocks_internal_redirect(mock_net):
    svc = starred_archive.StarredArchiveService.__new__(
        starred_archive.StarredArchiveService
    )
    svc._user_agent = "test"
    # safe_get raises UnsafeURLError; _fetch_text swallows it and returns None.
    assert svc._fetch_text("https://pub.test/redir-evil") is None
    assert svc._fetch_text("https://pub.test/page") == "<title>ok</title>"
