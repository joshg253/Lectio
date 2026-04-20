from __future__ import annotations

from services import reader_api
from services.reader_api import ReaderApi


def test_reader_api_client_uses_configured_db_path(monkeypatch):
    captured: dict[str, str] = {}

    def fake_make_reader(path: str):
        captured["path"] = path
        return {"ok": True, "path": path}

    monkeypatch.setattr(reader_api, "make_reader", fake_make_reader)

    api = ReaderApi("my_reader.sqlite")
    client = api.client()

    assert captured["path"] == "my_reader.sqlite"
    assert client["ok"] is True
