"""The app's logs must reach the console, not only the rotating file inside the container."""

from __future__ import annotations

import logging

import main


def _clean_root(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [], raising=False)
    return root


def test_console_handler_is_attached(monkeypatch):
    root = _clean_root(monkeypatch)

    main._configure_console_logging()

    console = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
    assert len(console) == 1, "app logging must be visible to `docker compose logs`"
    assert root.level <= logging.INFO


def test_console_handler_is_not_duplicated(monkeypatch):
    root = _clean_root(monkeypatch)

    main._configure_console_logging()
    main._configure_console_logging()

    assert len([h for h in root.handlers if isinstance(h, logging.StreamHandler)]) == 1


def test_rotating_file_handler_does_not_count_as_a_console_handler(monkeypatch, tmp_path):
    """RotatingFileHandler subclasses StreamHandler, so a naive isinstance check would see the file
    handler and skip the console one — which is exactly the bug this guards."""
    root = _clean_root(monkeypatch)
    from logging.handlers import RotatingFileHandler

    root.addHandler(RotatingFileHandler(tmp_path / "lectio.log"))

    main._configure_console_logging()

    console = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
    assert len(console) == 1


def test_lectio_logger_reaches_the_console(monkeypatch, capsys):
    _clean_root(monkeypatch)

    main._configure_console_logging()
    logging.getLogger("lectio").error("[scheduler] stalled — exiting so the container restarts")

    assert "exiting so the container restarts" in capsys.readouterr().err


def test_explicit_log_level_is_not_overruled(monkeypatch):
    """Lowering the root level is a decision about someone else's process, so a deployment that asks for
    a quieter console gets one rather than being tightened back to INFO."""
    root = _clean_root(monkeypatch)
    monkeypatch.setenv("LECTIO_LOG_LEVEL", "WARNING")

    main._configure_console_logging()

    assert root.level == logging.WARNING


def test_default_is_info_so_app_logs_are_actually_visible(monkeypatch):
    root = _clean_root(monkeypatch)
    monkeypatch.delenv("LECTIO_LOG_LEVEL", raising=False)
    root.setLevel(logging.WARNING)

    main._configure_console_logging()

    assert root.level == logging.INFO
