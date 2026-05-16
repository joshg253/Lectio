"""Integration tests for the pre-VPS hardening additions:
- /healthz liveness/readiness endpoint
- /static/* long-lived Cache-Control header
"""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _ok_meta_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_healthz_returns_ok_when_db_reachable(monkeypatch):
    app = FastAPI()
    app.get("/healthz")(main.healthz)
    monkeypatch.setattr(main, "get_meta_connection", _ok_meta_connection)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_returns_ok_even_when_db_unreachable(monkeypatch):
    """healthz is a lightweight liveness probe that intentionally does NOT
    touch the DB — under bulk-refresh load the DB can be locked for several
    seconds, so a probe that waits would cause the proxy to withdraw the
    backend even though the app is still serving requests."""
    app = FastAPI()
    app.get("/healthz")(main.healthz)

    def _broken_conn():
        raise sqlite3.OperationalError("simulated DB outage")

    monkeypatch.setattr(main, "get_meta_connection", _broken_conn)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_assets_get_long_lived_cache_control():
    """Static asset responses must include immutable Cache-Control so reverse
    proxies / browsers can cache aggressively. Safe because the SPA appends a
    `?v={STATIC_ASSET_VERSION}` cache-buster to all <link>/<script> URLs."""
    with TestClient(main.app) as client:
        response = client.get("/static/style.css")

    assert response.status_code == 200
    cache_control = response.headers.get("Cache-Control", "")
    assert "max-age=31536000" in cache_control
    assert "immutable" in cache_control
