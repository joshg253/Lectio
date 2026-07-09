"""The Admin → Logs tab reads the instance log via _read_log_tail: last-N lines,
minimum-level filtering, and traceback continuation lines staying attached to
their record."""
from __future__ import annotations

import main

SAMPLE = """\
2026-07-09 12:00:00,001 INFO httpx: GET / 200
2026-07-09 12:00:01,002 WARNING app: database is locked
2026-07-09 12:00:02,003 ERROR app: boom
Traceback (most recent call last):
  File "x.py", line 1, in <module>
sqlite3.OperationalError: boom
2026-07-09 12:00:03,004 INFO app: recovered
"""


def _write_log(tmp_path, monkeypatch, text=SAMPLE):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "lectio.log").write_text(text, encoding="utf-8")
    monkeypatch.setenv("LECTIO_LOG_DIR", str(d))


def test_unavailable_without_log_dir(monkeypatch):
    monkeypatch.delenv("LECTIO_LOG_DIR", raising=False)
    lines, available = main._read_log_tail(100, "")
    assert available is False and lines == []


def test_all_levels_returns_everything(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, available = main._read_log_tail(100, "")
    assert available is True
    assert len(lines) == 7  # every line, incl. the 3 traceback lines


def test_last_n_lines(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _ = main._read_log_tail(2, "")
    assert lines == [
        "sqlite3.OperationalError: boom",
        "2026-07-09 12:00:03,004 INFO app: recovered",
    ]


def test_warning_filter_keeps_warning_and_error_and_traceback(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _ = main._read_log_tail(100, "WARNING")
    # WARNING + ERROR records, and the ERROR's traceback continuation lines;
    # the two INFO records are dropped.
    assert "2026-07-09 12:00:01,002 WARNING app: database is locked" in lines
    assert "2026-07-09 12:00:02,003 ERROR app: boom" in lines
    assert 'sqlite3.OperationalError: boom' in lines
    assert not any("INFO" in ln for ln in lines)


def test_error_filter_only_error_records(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _ = main._read_log_tail(100, "ERROR")
    assert not any("WARNING" in ln or "INFO" in ln for ln in lines)
    assert lines[0].endswith("ERROR app: boom")
    # Traceback continuation stays attached to the ERROR record.
    assert any('File "x.py", line 1, in <module>' in ln for ln in lines)
