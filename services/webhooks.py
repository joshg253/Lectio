"""Outbound webhook delivery for automation rules.

A ``webhook`` automation rule POSTs each matching entry to an external URL when
its feed refreshes — for IFTTT (Maker/Webhooks), Zapier Catch Hooks, n8n, Make,
Home Assistant, or any custom endpoint. SSRF-guarded (same outbound policy as the
image proxy / WebSub): the target must resolve to a public address and redirects
are never followed.

Two payload formats:
- ``generic`` — a flat JSON object with the article fields.
- ``ifttt``   — IFTTT Maker's ``value1``/``value2``/``value3`` shape
  (title / link / feed title), which IFTTT Webhooks specifically expect.
"""
from __future__ import annotations

import httpx

from services.url_guard import is_safe_outbound_url

WEBHOOK_VALID_FORMATS = frozenset({"generic", "ifttt"})

_TIMEOUT_SECONDS = 10.0


def build_webhook_payload(article: dict, fmt: str = "generic") -> dict:
    """Return the JSON body for one matched entry in the requested format.

    ``article`` carries: title, link, feed_url, feed_title, excerpt, entry_id,
    published, tags.
    """
    if fmt == "ifttt":
        # IFTTT Maker only reads value1/value2/value3.
        return {
            "value1": str(article.get("title") or ""),
            "value2": str(article.get("link") or ""),
            "value3": str(article.get("feed_title") or ""),
        }
    return {
        "title": str(article.get("title") or ""),
        "link": str(article.get("link") or ""),
        "feed_url": str(article.get("feed_url") or ""),
        "feed_title": str(article.get("feed_title") or ""),
        "excerpt": str(article.get("excerpt") or ""),
        "entry_id": str(article.get("entry_id") or ""),
        "published": str(article.get("published") or ""),
        "tags": list(article.get("tags") or []),
    }


def send_webhook(url: str, payload: dict) -> tuple[bool, str | None]:
    """POST ``payload`` as JSON to ``url``. Returns ``(ok, error)``.

    SSRF-guarded: the URL must pass ``is_safe_outbound_url`` and redirects are not
    followed (a 3xx is treated as a non-2xx failure rather than chased to a
    possibly-internal target).
    """
    if not url or not is_safe_outbound_url(url):
        return False, "unsafe or empty webhook URL"
    try:
        with httpx.Client(follow_redirects=False, timeout=_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
        if 200 <= resp.status_code < 300:
            return True, None
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001 — surface any transport error to the caller
        return False, str(exc)
