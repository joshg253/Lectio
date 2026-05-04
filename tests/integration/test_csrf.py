"""CSRF middleware tests.

Exercises the full middleware stack via TestClient. Cookies/sessions are
preserved across calls automatically, so a real-browser-like flow (GET a
page → submit a POST) works.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import main


def test_post_without_token_is_rejected_with_403():
    """A POST to a state-changing endpoint without any CSRF token must 403."""
    with TestClient(main.app) as client:
        # GET first so the session cookie is established (token also generated)
        r = client.get("/healthz")
        assert r.status_code == 200

        # POST without supplying the token — must be rejected
        r = client.post("/entries/saved", data={
            "folder_id": "1",
            "feed_url": "https://example.com/feed.xml",
            "entry_id": "x1",
            "saved": "1",
        })
        assert r.status_code == 403
        assert "CSRF" in r.text


def _read_csrf_token_from_session_cookie(client: TestClient) -> str:
    """Decode the SessionMiddleware-signed cookie to extract the CSRF token."""
    import base64
    import json

    from itsdangerous import TimestampSigner

    cookie = client.cookies.get("session")
    assert cookie, "session cookie missing — ensure a request has been made first"
    signer = TimestampSigner(main.SESSION_SECRET_KEY)
    decoded = signer.unsign(cookie, max_age=main.SESSION_MAX_AGE_SECONDS)
    session_data = json.loads(base64.b64decode(decoded))
    token = session_data.get("csrf_token")
    assert token, "session should carry a csrf_token after first request"
    return token


def test_post_with_valid_form_field_is_accepted(monkeypatch):
    """A POST whose body carries `_csrf=<token>` (the path used by native
    HTML form submits with the JS shim) must pass the CSRF gate."""
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    with TestClient(main.app) as client:
        client.get("/healthz")  # establish session + token
        token = _read_csrf_token_from_session_cookie(client)

        r = client.post(
            "/entries/saved",
            data={
                "_csrf": token,
                "folder_id": "1",
                "feed_url": "https://example.com/feed.xml",
                "entry_id": "x1",
                "saved": "1",
            },
        )
        assert r.status_code != 403, f"valid form-field token rejected: {r.status_code} {r.text[:200]}"


def test_post_with_valid_header_is_accepted(monkeypatch):
    """X-CSRF-Token header matching the session token (the path used by SPA
    fetch handlers via the JS shim) must pass the CSRF gate."""
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    with TestClient(main.app) as client:
        client.get("/healthz")  # establish session + token
        token = _read_csrf_token_from_session_cookie(client)

        r = client.post(
            "/entries/saved",
            data={
                "folder_id": "1",
                "feed_url": "https://example.com/feed.xml",
                "entry_id": "x1",
                "saved": "1",
            },
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code != 403, f"valid token rejected: {r.status_code} {r.text[:200]}"


def test_post_with_wrong_token_is_rejected():
    """Submitting the wrong token must 403, not silently pass."""
    with TestClient(main.app) as client:
        client.get("/healthz")  # establish session + token
        r = client.post(
            "/entries/saved",
            data={"folder_id": "1", "feed_url": "x", "entry_id": "x", "saved": "1"},
            headers={"X-CSRF-Token": "definitely-wrong-token"},
        )
        assert r.status_code == 403


def test_login_post_is_csrf_exempt(monkeypatch):
    """/login must work without a CSRF token (auth gate, rate-limited
    separately). Otherwise nobody could log in."""
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_USERNAME", "tester")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "secret")
    monkeypatch.setattr(main, "DEBUG_MODE", False)
    main._login_failures.clear()

    with TestClient(main.app) as client:
        r = client.post("/login", data={"username": "tester", "password": "secret"}, follow_redirects=False)
        assert r.status_code == 303, f"login should redirect on success, got {r.status_code}"


def test_safe_methods_pass_without_token():
    """GET/HEAD/OPTIONS must never require a CSRF token."""
    with TestClient(main.app) as client:
        assert client.get("/healthz").status_code == 200
        # Multiple GETs should keep working without ever submitting a token
        assert client.get("/healthz").status_code == 200
