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


def test_oauth_callback_and_signed_url_params_are_masked():
    out = _format('GET /integrations/youtube/oauth/callback?code=AUTHCODE&state=STATE1 "200"')
    assert "AUTHCODE" not in out and "STATE1" not in out
    assert "SIGVALUE" not in _format("GET https://cdn.test/a.jpg?w=100&X-Amz-Signature=SIGVALUE")


def _clean_root(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    return root


def test_the_configured_console_handler_masks_secrets(monkeypatch, capsys):
    _clean_root(monkeypatch)
    main._configure_console_logging()
    logging.getLogger("httpx").info("GET https://api.test/v1?id=1&key=CONSOLESECRET")
    err = capsys.readouterr().err
    assert "CONSOLESECRET" not in err and "key=<redacted>" in err


def test_an_existing_console_handler_gets_the_redacting_formatter(monkeypatch, capsys):
    root = _clean_root(monkeypatch)
    existing = logging.StreamHandler()
    existing.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.addHandler(existing)
    main._configure_console_logging()
    logging.getLogger("httpx").warning("GET https://api.test/v1?key=EXISTINGSECRET")
    assert "EXISTINGSECRET" not in capsys.readouterr().err


def test_the_configured_file_handler_masks_secrets(monkeypatch, tmp_path):
    _clean_root(monkeypatch)
    monkeypatch.setenv("LECTIO_LOG_DIR", str(tmp_path))
    main._configure_persistent_logging()
    logging.getLogger("httpx").info("GET https://api.test/v1?key=FILESECRET")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "lectio.log").read_text()
    assert "FILESECRET" not in text and "key=<redacted>" in text
