"""Pinterest API v5 OAuth + pin writes.

Outbound-only integration: lets a user save (pin) an article to one of their
boards from the article view. Pinterest has no write-without-OAuth path, so this
needs a per-user OAuth grant; tokens are stored per-user by the caller (main.py),
same pattern as the YouTube / DeviantArt integrations. This module only speaks
HTTP to Pinterest.

Scopes: ``boards:read`` (list the user's boards for the picker) and
``pins:write`` (create the pin). The token endpoint authenticates the *client*
with HTTP Basic (base64 of client_id:client_secret); the body is form-encoded.
"""
from __future__ import annotations

import base64

import httpx

_AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
_API_BASE = "https://api.pinterest.com/v5"
_SCOPE = "boards:read,pins:write"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 20


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Consent-screen URL."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SCOPE,
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _post_token(client_id: str, client_secret: str, payload: dict, what: str) -> dict:
    headers = {
        "User-Agent": _USER_AGENT,
        "Authorization": _basic_auth_header(client_id, client_secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
        resp = client.post(_TOKEN_URL, data=payload)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code == 200 and data.get("access_token"):
        return data
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:200]}")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    return _post_token(client_id, client_secret, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }, "token exchange")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token. Pinterest may omit ``refresh_token`` from
    the response, so the caller keeps the existing one."""
    return _post_token(client_id, client_secret, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": _SCOPE,
    }, "token refresh")


def _auth_headers(access_token: str) -> dict:
    return {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {access_token}"}


def list_boards(access_token: str) -> list[dict]:
    """Return the authenticated user's boards as ``[{id, name}]``.

    Pages through the user's boards (``page_size`` 100, bookmark pagination)."""
    out: list[dict] = []
    bookmark = ""
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        while True:
            params: dict = {"page_size": 100}
            if bookmark:
                params["bookmark"] = bookmark
            resp = client.get(f"{_API_BASE}/boards", params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"boards.list failed: HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            for item in data.get("items", []):
                out.append({"id": item.get("id", ""), "name": item.get("name", "")})
            bookmark = data.get("bookmark") or ""
            if not bookmark:
                break
    return out


def create_pin(access_token: str, board_id: str, image_url: str, link: str,
               title: str = "", description: str = "") -> dict:
    """Create a pin on ``board_id`` from ``image_url`` linking back to ``link``.

    Returns the created pin object. Pinterest requires an image — callers must
    not pass an empty ``image_url``."""
    body = {
        "board_id": board_id,
        "link": link or None,
        "title": (title or "")[:100] or None,
        "description": (description or "")[:500] or None,
        "media_source": {"source_type": "image_url", "url": image_url},
    }
    body = {k: v for k, v in body.items() if v is not None}
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        resp = client.post(f"{_API_BASE}/pins", json=body)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"pins.create failed: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()
