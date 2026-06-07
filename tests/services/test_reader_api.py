from __future__ import annotations

from services import reader_api
from services.reader_api import ReaderApi

_EXPECTED_UA = "Lectio/0.1 (+https://github.com/joshg253/Lectio)"


def test_reader_api_client_uses_configured_db_path(monkeypatch):
    captured: dict[str, str] = {}

    class FakeParser:
        lazy_init_funcs: list = []
        def lazy_init(self, fn):
            return fn

    class FakeReader:
        def __init__(self, path):
            self._path = path
            self._parser = FakeParser()
            self.ok = True

    def fake_make_reader(path: str):
        captured["path"] = path
        return FakeReader(path)

    monkeypatch.setattr(reader_api, "make_reader", fake_make_reader)

    api = ReaderApi("my_reader.sqlite")
    client = api.client()

    assert captured["path"] == "my_reader.sqlite"
    assert client.ok is True


def test_reader_api_registers_ua_lazy_init(monkeypatch):
    """ReaderApi.client() inserts a hook into lazy_init_funcs."""
    inserted: list = []

    class FakeParser:
        lazy_init_funcs: list

        def __init__(self):
            self.lazy_init_funcs = []

        def lazy_init(self, fn):
            return fn

    class FakeReader:
        def __init__(self, path):
            self._parser = FakeParser()

    def fake_make_reader(path):
        r = FakeReader(path)
        inserted.append(r)
        return r

    monkeypatch.setattr(reader_api, "make_reader", fake_make_reader)
    ReaderApi("test.sqlite").client()

    assert len(inserted) == 1
    # Our hook must be inserted at position 0 (runs last = after reader's post_init)
    assert len(inserted[0]._parser.lazy_init_funcs) == 1


def test_reader_api_ua_hook_sets_lectio_header(monkeypatch):
    """The inserted lazy_init hook sets Lectio's User-Agent on retriever sessions."""

    class FakeSession:
        def __init__(self):
            self.headers = {}

    class FakeRetriever:
        def __init__(self):
            self.session = FakeSession()
            self.response_hooks = []

    class FakeParser:
        def __init__(self):
            self.lazy_init_funcs = []
            self.retrievers = {"https://": FakeRetriever(), "http://": FakeRetriever()}

        def lazy_init(self, fn):
            return fn

    class FakeReader:
        def __init__(self, path):
            self._parser = FakeParser()

    monkeypatch.setattr(reader_api, "make_reader", lambda path: FakeReader(path))

    r = ReaderApi("test.sqlite").client()

    # Simulate do_lazy_init: call the inserted hook with the parser
    hook = r._parser.lazy_init_funcs[0]
    hook(r._parser)

    for retr in r._parser.retrievers.values():
        assert retr.session.headers.get("User-Agent") == _EXPECTED_UA
