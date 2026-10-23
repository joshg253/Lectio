"""Log lines never carry URL query-string secrets (httpx logs every request URL, and the YouTube Data API takes ``&key=``)."""

from __future__ import annotations

import logging

import main


def _format(msg: str) -> str:
    fmt = main._RedactingFormatter("%(levelname)s %(name)s: %(message)s")
    return fmt.format(logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, None, None))


def test_api_key_in_a_request_url_is_masked():
    out = _format('HTTP Request: GET https://www.googleapis.com/youtube/v3/videos?part=snippet&id=abc&key=SECRET123 "HTTP/1.1 200 OK"')
    assert "SECRET123" not in out
    assert "&key=<redacted> " in out and "id=abc" in out


def test_other_secret_params_are_masked_and_plain_text_is_untouched():
    assert "tok123" not in _format("GET https://x.test/api?access_token=tok123&page=2")
    assert _format("GET https://x.test/api?page=2&monkey=1") == "INFO httpx: GET https://x.test/api?page=2&monkey=1"
