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
    lines, available, _ = main._read_log_tail(100, "")
    assert available is False and lines == []


def test_all_levels_returns_everything(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, available, _ = main._read_log_tail(100, "")
    assert available is True
    assert len(lines) == 7  # every line, incl. the 3 traceback lines


def test_last_n_lines(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(2, "")
    assert lines == [
        "sqlite3.OperationalError: boom",
        "2026-07-09 12:00:03,004 INFO app: recovered",
    ]


def test_warning_filter_keeps_warning_and_error_and_traceback(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(100, "WARNING")
    # WARNING + ERROR records, and the ERROR's traceback continuation lines;
    # the two INFO records are dropped.
    assert "2026-07-09 12:00:01,002 WARNING app: database is locked" in lines
    assert "2026-07-09 12:00:02,003 ERROR app: boom" in lines
    assert 'sqlite3.OperationalError: boom' in lines
    assert not any("INFO" in ln for ln in lines)


def test_error_filter_only_error_records(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(100, "ERROR")
    assert not any("WARNING" in ln or "INFO" in ln for ln in lines)
    assert lines[0].endswith("ERROR app: boom")
    # Traceback continuation stays attached to the ERROR record.
    assert any('File "x.py", line 1, in <module>' in ln for ln in lines)


# ── `since` timestamp filter (Logs tab datetime picker) ──────────────────────

from datetime import datetime  # noqa: E402


def test_since_drops_older_records(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(100, "", datetime(2026, 7, 9, 12, 0, 3))
    assert lines == ["2026-07-09 12:00:03,004 INFO app: recovered"]


def test_since_keeps_boundary_and_rides_traceback(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(100, "", datetime(2026, 7, 9, 12, 0, 1))
    assert not any("12:00:00" in ln for ln in lines)          # 12:00:00 dropped
    assert any(ln.endswith("database is locked") for ln in lines)  # 12:00:01 kept
    assert any('File "x.py", line 1, in <module>' in ln for ln in lines)  # traceback rides


def test_since_and_level_combine(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(100, "ERROR", datetime(2026, 7, 9, 12, 0, 3))
    assert lines == []  # the only ERROR is at 12:00:02, before the cutoff


def test_log_line_dt_parses_record_not_continuation():
    assert main._log_line_dt("2026-07-09 12:00:02,003 ERROR app: boom") == datetime(2026, 7, 9, 12, 0, 2)
    assert main._log_line_dt('  File "x.py", line 1, in <module>') is None


# ── daily-maintenance catch-up scheduling ────────────────────────────────────

def test_maintenance_due_catches_up_after_missed_hour():
    # Missed the exact hour (restart/deploy near 3am): still due later same day.
    assert main._maintenance_due(9, 3, "2026-07-17", "2026-07-18") is True


def test_maintenance_not_due_before_hour():
    assert main._maintenance_due(2, 3, "2026-07-17", "2026-07-18") is False


def test_maintenance_not_due_if_already_ran_today():
    assert main._maintenance_due(3, 3, "2026-07-18", "2026-07-18") is False
    assert main._maintenance_due(23, 3, "2026-07-18", "2026-07-18") is False


def test_maintenance_disabled_never_due():
    assert main._maintenance_due(12, None, "", "2026-07-18") is False


# ── `until` bound + truncation flag ──────────────────────────────────────────

def test_until_drops_newer_records(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(100, "", None, datetime(2026, 7, 9, 12, 0, 1))
    # only 12:00:00 and 12:00:01 kept; 12:00:02/03 dropped
    assert any("12:00:00" in ln for ln in lines)
    assert any(ln.endswith("database is locked") for ln in lines)
    assert not any("12:00:03" in ln for ln in lines)


def test_since_until_window(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    lines, _, _ = main._read_log_tail(100, "", datetime(2026, 7, 9, 12, 0, 1),
                                      datetime(2026, 7, 9, 12, 0, 2))
    assert not any("12:00:00" in ln or "12:00:03" in ln for ln in lines)
    assert any(ln.endswith("database is locked") for ln in lines)  # 12:00:01
    assert any(ln.endswith("ERROR app: boom") for ln in lines)      # 12:00:02


def test_truncated_flag_when_cap_hit(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch)
    _, _, truncated = main._read_log_tail(2, "", None, None)
    assert truncated is True
    _, _, truncated = main._read_log_tail(100, "", None, None)
    assert truncated is False


def test_parse_local_ts_minute_vs_seconds():
    assert main._parse_local_ts("2026-07-18T03:30") == (datetime(2026, 7, 18, 3, 30), False)
    assert main._parse_local_ts("2026-07-18T03:30:45") == (datetime(2026, 7, 18, 3, 30, 45), True)
    assert main._parse_local_ts("") == (None, False)
    assert main._parse_local_ts("garbage") == (None, False)
