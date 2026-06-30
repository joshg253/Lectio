"""Reader/web view (build_readability_response) applies the same hotlink image
handling as the entry pane: known hotlink hosts (e.g. fabiensanglard.net, whose
.webp 403 a no-Referer browser load) are routed through /api/img, and other imgs
get referrerpolicy=no-referrer. Without this, the source page's webp images break
in reader view while its jpg loads.
"""
from __future__ import annotations

import httpx
import pytest

import main
from services import url_guard


_PAGE = (
    "<html><head><title>My favorite keyboards</title></head><body><article>"
    "<h1>My favorite keyboards</h1>"
    "<p>Over the years I have used a great many mechanical keyboards, and these "
    "are the ones that left a lasting impression on me for their feel and build "
    "quality. Each one tells a small story about the era it came from.</p>"
    '<p><img src="https://fabiensanglard.net/keyboards/model_m.webp"></p>'
    "<p>The Model M is a classic buckling-spring board that still feels great to "
    "type on decades later, and it remains a benchmark for tactile feedback.</p>"
    '<p><img src="https://fabiensanglard.net/fd_proxy/quake2/john_Carmack_working.jpg"></p>'
    "<p>Here is some more descriptive prose so the readability extractor treats "
    "this as a real article and keeps the images in the summary output.</p>"
    "</article></body></html>"
)


def _stub_page(monkeypatch):
    def _fetch(client, url, *a, **k):
        return httpx.Response(200, request=httpx.Request("GET", url),
                              headers={"content-type": "text/html"}, text=_PAGE)
    monkeypatch.setattr(url_guard, "safe_get", _fetch)


def test_reader_view_proxies_hotlink_webp(monkeypatch):
    _stub_page(monkeypatch)
    resp = main.build_readability_response("https://fabiensanglard.net/keyboards/index.html")
    body = resp.body.decode()
    # The webp (hotlink-protected) image is routed through /api/img...
    assert "/api/img?u=https%3A%2F%2Ffabiensanglard.net%2Fkeyboards%2Fmodel_m.webp" in body
    # ...and is no longer loaded directly.
    assert 'src="https://fabiensanglard.net/keyboards/model_m.webp"' not in body


def test_reader_view_adds_no_referrer(monkeypatch):
    _stub_page(monkeypatch)
    resp = main.build_readability_response("https://fabiensanglard.net/keyboards/index.html")
    body = resp.body.decode()
    # Non-hotlink images keep their direct src but gain referrerpolicy.
    assert "referrerpolicy" in body
