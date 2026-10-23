"""The Admin → Logs tab reads the instance log via _read_log_tail: last-N lines,
minimum-level filtering, and traceback continuation lines staying attached to
their record."""

from __future__ import annotations

import re

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
    assert "sqlite3.OperationalError: boom" in lines
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
    assert not any("12:00:00" in ln for ln in lines)  # 12:00:00 dropped
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
    lines, _, _ = main._read_log_tail(100, "", datetime(2026, 7, 9, 12, 0, 1), datetime(2026, 7, 9, 12, 0, 2))
    assert not any("12:00:00" in ln or "12:00:03" in ln for ln in lines)
    assert any(ln.endswith("database is locked") for ln in lines)  # 12:00:01
    assert any(ln.endswith("ERROR app: boom") for ln in lines)  # 12:00:02


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


# ── Category filter (Logs tab dropdown) ──────────────────────────────────────

CATEGORY_SAMPLE = """\
2026-07-09 12:00:00,001 INFO httpx: GET / 200
2026-07-09 12:00:01,002 INFO lectio: [maintenance] vacuum done
2026-07-09 12:00:02,003 ERROR lectio: [automation] rule failed
Traceback (most recent call last):
ValueError: nope
2026-07-09 12:00:03,004 INFO lectio: [yt-playlist-auto] added 3
2026-07-09 12:00:04,005 INFO lectio: [deviantart] synced
2026-07-09 12:00:05,006 INFO lectio: [mystery] unlisted tag
"""


def test_log_categories_mapping():
    cat = main._log_categories
    assert cat("2026-07-09 12:00:01,002 INFO lectio: [maintenance] x") == {"maintenance"}
    assert cat("2026-07-09 12:00:01,002 INFO lectio: [guid-churn-cleanup] x") == {"maintenance"}
    assert cat("2026-07-09 12:00:01,002 INFO lectio: [yt-playlist-auto] x") == {"automations", "youtube"}
    assert cat("2026-07-09 12:00:01,002 INFO lectio: [yt-quota] x") == {"youtube"}
    assert cat('2026-07-09 12:00:01,002 INFO httpx: HTTP Request: POST https://www.googleapis.com/youtube/v3/playlistItems "200"') == {
        "youtube"
    }
    assert cat("2026-07-09 12:00:01,002 INFO httpx: GET / 200") == {"other"}
    assert cat("2026-07-09 12:00:01,002 INFO lectio: [mystery] x") == {"other"}


def test_youtube_category_includes_automation_adds_and_api_calls(tmp_path, monkeypatch):
    text = CATEGORY_SAMPLE + (
        '2026-07-09 12:00:06,007 INFO httpx: HTTP Request: POST https://www.googleapis.com/youtube/v3/playlistItems "200"\n'
    )
    _write_log(tmp_path, monkeypatch, text)
    lines, _, _ = main._read_log_tail(100, "", category="youtube")
    records = [ln for ln in lines if ln.startswith("2026")]
    assert len(records) == 2
    assert "[yt-playlist-auto] added 3" in records[0] and "googleapis.com/youtube" in records[1]


def test_category_filter_keeps_matching_records_and_tracebacks(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, CATEGORY_SAMPLE)
    lines, _, _ = main._read_log_tail(100, "", category="automations")
    assert [ln for ln in lines if ln.startswith("2026")] == [
        "2026-07-09 12:00:02,003 ERROR lectio: [automation] rule failed",
        "2026-07-09 12:00:03,004 INFO lectio: [yt-playlist-auto] added 3",
    ]
    assert "ValueError: nope" in lines  # traceback stays with its record


def test_category_other_and_all(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, CATEGORY_SAMPLE)
    other, _, _ = main._read_log_tail(100, "", category="other")
    assert len(other) == 2 and "httpx" in other[0] and "[mystery]" in other[1]
    everything, _, _ = main._read_log_tail(100, "", category="all")
    assert len(everything) == 8  # 6 records + 2 traceback lines


def test_tags_added_after_review_are_mapped():
    cat = main._log_categories
    line = "2026-07-09 12:00:01,002 INFO lectio: [{}] x"
    assert cat(line.format("admin-vacuum")) == {"maintenance"}
    assert cat(line.format("quire-auto")) == {"automations", "integrations"}
    assert cat(line.format("hide-unpremiered")) == {"youtube"}
    assert cat(line.format("freshrss-import")) == {"integrations"}


def test_a_bracketed_word_quoted_later_in_an_untagged_line_is_not_a_tag():
    line = '2026-07-09 12:00:01,002 INFO lectio: 10.0.0.1:0 - "GET /?q=[reddit] HTTP/1.1" 200 OK'
    assert main._log_categories(line) == {"other"}


def test_category_combines_with_level_and_window(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, CATEGORY_SAMPLE)
    lines, _, _ = main._read_log_tail(100, "ERROR", category="automations")
    assert [ln for ln in lines if ln.startswith("2026")] == ["2026-07-09 12:00:02,003 ERROR lectio: [automation] rule failed"]
    lines, _, _ = main._read_log_tail(100, "", datetime(2026, 7, 9, 12, 0, 3), category="automations")
    assert [ln for ln in lines if ln.startswith("2026")] == ["2026-07-09 12:00:03,004 INFO lectio: [yt-playlist-auto] added 3"]


def test_log_category_names_cover_the_dropdown():
    from pathlib import Path

    html = (Path(main.__file__).parent / "templates" / "administration.html").read_text()
    select = html[html.index('id="adm-log-category"') :]
    select = select[: select.index("</select>")]
    options = set(re.findall(r'<option value="([a-z]+)"', select))
    assert options == main.LOG_CATEGORY_NAMES | {"all"}
