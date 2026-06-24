"""Quire (quire.io) OAuth2 + task-creation API client.

Outbound destination: turn a Lectio entry into a task in a chosen Quire project.
Tokens are stored per-user by the caller (main.py), same pattern as the DeviantArt
and YouTube OAuth integrations; this module only speaks HTTP to Quire.

Quire rate-limits per *organization* by minute and hour (Free: 50/min, 200/hr) and
exposes no remaining-quota read — only a 429 + ``Retry-After`` when exceeded. So the
app layer keeps its own sliding-window tally of the calls billed through ``_bill`` and
surfaces it as a usage meter, the way the YouTube integration meters daily quota.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

_AUTHORIZE_URL = "https://quire.io/oauth"
_TOKEN_URL = "https://quire.io/oauth/token"
_API_BASE = "https://quire.io/api"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 20

# Optional usage sink: the app sets this to record each billed API call against its
# sliding-window rate meter. Pure parsing/HTTP stays here; the meter lives in the app.
_usage_sink = None


def set_usage_sink(fn) -> None:
    global _usage_sink
    _usage_sink = fn


def _bill(calls: int = 1) -> None:
    if _usage_sink:
        try:
            _usage_sink(calls)
        except Exception:
            pass


class QuireRateLimited(RuntimeError):
    """Raised when Quire returns HTTP 429. ``retry_after`` is seconds to wait."""

    def __init__(self, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Consent-screen URL. Quire issues a refresh token alongside the access token."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(payload: dict, what: str) -> dict:
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        resp = client.post(_TOKEN_URL, data=payload)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code == 200 and data.get("access_token"):
        return data
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:200]}")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }, "token exchange")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token."""
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }, "token refresh")


def _auth_headers(access_token: str) -> dict:
    return {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {access_token}"}


def _raise_for_status(resp: httpx.Response, what: str) -> None:
    if resp.status_code in (200, 201):
        return
    if resp.status_code == 429:
        try:
            retry_after = int(resp.headers.get("Retry-After", "0") or "0")
        except (TypeError, ValueError):
            retry_after = 0
        raise QuireRateLimited(f"{what}: Quire rate limit hit", retry_after)
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:300]}")


def list_projects(access_token: str) -> list[dict]:
    """Return the authorized projects as ``[{id, oid, name}]``."""
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/project/list")
    _raise_for_status(resp, "project.list")
    _bill(1)
    out: list[dict] = []
    for item in resp.json() or []:
        out.append({
            "id": item.get("id", ""),
            "oid": item.get("oid", ""),
            "name": item.get("name", ""),
        })
    return out


def whoami(access_token: str) -> str:
    """Return the authenticated user's display name (best-effort, for the connect notice)."""
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/user/id/me")
    _raise_for_status(resp, "user.me")
    _bill(1)
    data = resp.json() or {}
    return str(data.get("nameText") or data.get("name") or "")


def create_task(access_token: str, project_oid: str, name: str, description: str = "") -> dict:
    """Add a root task named ``name`` to ``project_oid``. Costs 1 call."""
    body: dict = {"name": name}
    if description:
        body["description"] = description
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        resp = client.post(f"{_API_BASE}/task/{project_oid}", json=body)
    _raise_for_status(resp, "task.create")
    _bill(1)
    return resp.json() if resp.content else {}
