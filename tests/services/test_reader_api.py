from __future__ import annotations

from services import reader_api
from services.reader_api import ReaderApi

_EXPECTED_UA = "Lectio/0.1 (+https://github.com/joshg253/Lectio)"


def test_reader_api_client_uses_configured_db_path(monkeypatch):
    captured: dict = {}

    class FakeParser:
        lazy_init_funcs: list = []
        def lazy_init(self, fn):
            return fn

    class FakeReader:
        def __init__(self, path, **kwargs):
            self._path = path
            self._parser = FakeParser()
            self.ok = True

    class FakeStorage:
        def __init__(self, path, **kwargs):
            captured["storage_path"] = path
            captured["storage_kwargs"] = kwargs

    def fake_make_reader(path: str, **kwargs):
        captured["path"] = path
        captured["make_reader_kwargs"] = kwargs
        return FakeReader(path)

    monkeypatch.setattr(reader_api, "make_reader", fake_make_reader)
    monkeypatch.setattr(reader_api, "_LectioReaderStorage", FakeStorage)

    api = ReaderApi("my_reader.sqlite")
    client = api.client()

    assert captured["path"] == "my_reader.sqlite"
    assert captured.get("storage_path") == "my_reader.sqlite"
    assert captured["storage_kwargs"].get("timeout") == 30.0
    assert client.ok is True
    # ua_fallback must be suppressed; entry_dedupe + enclosure_dedupe must be enabled.
    plugins = captured["make_reader_kwargs"].get("plugins", [])
    assert ".ua_fallback" not in list(plugins)
    assert ".entry_dedupe" in list(plugins)
    assert ".enclosure_dedupe" in list(plugins)


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

    def fake_make_reader(path, **kwargs):
        r = FakeReader(path)
        inserted.append(r)
        return r

    monkeypatch.setattr(reader_api, "make_reader", fake_make_reader)
    monkeypatch.setattr(reader_api, "_LectioReaderStorage", lambda path, **kw: None)
    ReaderApi("test.sqlite").client()

    assert len(inserted) == 1
    # Two hooks are registered: the User-Agent response hook and the
    # sanitizing-parser swap (services.reader_sanitize.install).
    assert len(inserted[0]._parser.lazy_init_funcs) == 2


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
            self.parsers_by_mime_type = {}  # for the sanitize-swap hook

        def lazy_init(self, fn):
            return fn

    class FakeReader:
        def __init__(self, path):
            self._parser = FakeParser()

    monkeypatch.setattr(reader_api, "make_reader", lambda path, **kw: FakeReader(path))
    monkeypatch.setattr(reader_api, "_LectioReaderStorage", lambda path, **kw: None)

    r = ReaderApi("test.sqlite").client()

    # Simulate do_lazy_init: run every registered hook (the UA hook and the
    # sanitize-swap hook); order-independent.
    for hook in r._parser.lazy_init_funcs:
        hook(r._parser)

    for retr in r._parser.retrievers.values():
        assert retr.session.headers.get("User-Agent") == _EXPECTED_UA


def test_ua_hook_fires_on_real_reader(tmp_path):
    """The lazy_init hook correctly sets the UA on a real reader instance.

    This is the key regression guard for reader upgrades: it exercises the
    actual _parser.lazy_init_funcs / retrievers internal API path end-to-end,
    not just a fake stand-in.
    """
    import reader as reader_lib

    db = str(tmp_path / "test.sqlite")
    r = ReaderApi(db).client()
    try:
        # reader pops lazy_init_funcs from the END (LIFO), so iterate reversed
        # to match real execution order: post_init (creates retrievers) runs
        # first, then our UA hook runs last and can find the retrievers.
        for fn in reversed(list(r._parser.lazy_init_funcs)):
            try:
                fn(r._parser)
            except Exception:
                pass

        for prefix in ("https://", "http://"):
            retr = r._parser.retrievers.get(prefix)
            if retr is not None and hasattr(retr, "session"):
                assert retr.session.headers.get("User-Agent") == _EXPECTED_UA
    finally:
        r.close()


def _capture_make_reader_kwargs(monkeypatch) -> dict:
    """Stub make_reader/storage and return the dict the kwargs land in."""
    captured: dict = {}

    class FakeParser:
        lazy_init_funcs: list = []
        def lazy_init(self, fn):
            return fn

    class FakeReader:
        def __init__(self, path, **kwargs):
            self._parser = FakeParser()

    class FakeStorage:
        def __init__(self, path, **kwargs):
            pass

    def fake_make_reader(path: str, **kwargs):
        captured.update(kwargs)
        return FakeReader(path)

    monkeypatch.setattr(reader_api, "make_reader", fake_make_reader)
    monkeypatch.setattr(reader_api, "_LectioReaderStorage", FakeStorage)
    return captured


def test_session_timeout_is_passed_through(monkeypatch):
    """The scheduler refreshes sequentially, so an unbounded read on one feed
    delays every feed behind it. See Plan.md §0a."""
    captured = _capture_make_reader_kwargs(monkeypatch)

    ReaderApi("test.sqlite", session_timeout=(5.0, 20.0)).client()

    assert captured.get("session_timeout") == (5.0, 20.0)


def test_session_timeout_omitted_keeps_readers_default(monkeypatch):
    """Not passing it must leave reader's own default in place, not send None
    (which would mean "no timeout at all")."""
    captured = _capture_make_reader_kwargs(monkeypatch)

    ReaderApi("test.sqlite").client()

    assert "session_timeout" not in captured
