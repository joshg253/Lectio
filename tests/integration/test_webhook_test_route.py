"""Integration tests for the webhook test-send route."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _build_app(monkeypatch, *, safe=True, send_result=(True, None)):
    app = FastAPI()
    app.post("/rules/webhook-test")(main.webhook_test_route)
    monkeypatch.setattr(main.url_guard, "is_safe_outbound_url", lambda _u: safe)
    captured = {}

    def _send(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return send_result

    monkeypatch.setattr(main, "send_webhook", _send)
    return app, captured


def test_test_send_posts_sample_payload(monkeypatch):
    app, captured = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/rules/webhook-test", data={"webhook_url": "https://hooks.example.com/x", "webhook_format": "generic"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert captured["url"] == "https://hooks.example.com/x"
    assert captured["payload"]["title"] == "Lectio webhook test"


def test_test_send_ifttt_format(monkeypatch):
    app, captured = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/rules/webhook-test", data={"webhook_url": "https://maker.ifttt.com/trigger/e/with/key/k", "webhook_format": "ifttt"})
    assert r.status_code == 200
    assert set(captured["payload"]) == {"value1", "value2", "value3"}


def test_test_send_rejects_unsafe_url(monkeypatch):
    app, _ = _build_app(monkeypatch, safe=False)
    with TestClient(app) as client:
        r = client.post("/rules/webhook-test", data={"webhook_url": "http://169.254.169.254/", "webhook_format": "generic"})
    assert r.status_code == 400


def test_test_send_surfaces_send_failure(monkeypatch):
    app, _ = _build_app(monkeypatch, send_result=(False, "HTTP 500"))
    with TestClient(app) as client:
        r = client.post("/rules/webhook-test", data={"webhook_url": "https://hooks.example.com/x", "webhook_format": "generic"})
    assert r.status_code == 400 and "HTTP 500" in r.json()["error"]
