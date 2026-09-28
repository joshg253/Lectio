"""scripts/probe_empty_archives.py --write backfills archived_entry.source_fetch_status for empty complete archives: failures get the
capture-time classifier's value, a page reachable now stays NULL (it needs a recapture, not an "ok"), and a row that already has a
status is never touched."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

import main
from services import tenancy

UID = tenancy.DEFAULT_USER_ID
_SCRIPT = pathlib.Path(main.BASE_DIR) / "scripts" / "probe_empty_archives.py"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    spec = importlib.util.spec_from_file_location("probe_empty_archives", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    with tenancy.user_context(UID):
        main.ensure_starred_archive_schema()
        with main.archive_conn() as conn:
            for eid, status in (("dead", None), ("gone", None), ("live", None), ("done", "timeout")):
                conn.execute(
                    "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, link, source_fetch_status)"
                    " VALUES ('f', ?, 'complete', 0, ?, ?)",
                    (eid, f"https://x.test/{eid}", status),
                )
    try:
        yield mod
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _statuses():
    with tenancy.user_context(UID):
        with main.archive_conn() as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT entry_id, source_fetch_status FROM archived_entry")}


def test_write_backfills_failures_only(probe, monkeypatch):
    verdicts = {
        "https://x.test/dead": {"verdict": "dead", "status": 404, "fetch_status": "http_404"},
        "https://x.test/gone": {"verdict": "unreachable", "fetch_status": "connect"},
        "https://x.test/live": {"verdict": "reachable", "status": 200, "bytes": 9000},
    }
    probed: list[str] = []
    monkeypatch.setattr(probe, "_probe", lambda url: (probed.append(url), verdicts[url])[1])
    probe.run(UID, None, None, write=True)
    assert sorted(probed) == sorted(verdicts)  # the row already carrying a status isn't even probed
    assert _statuses() == {"dead": "http_404", "gone": "connect", "live": None, "done": "timeout"}


def test_without_write_nothing_changes(probe, monkeypatch):
    monkeypatch.setattr(probe, "_probe", lambda url: {"verdict": "dead", "status": 404, "fetch_status": "http_404"})
    probe.run(UID, None, None)
    assert _statuses() == {"dead": None, "gone": None, "live": None, "done": "timeout"}
