"""The /thumb negative cache short-circuits recently-failed image fetches so a
folder full of server-blocked images doesn't re-hit every dead host per page load."""
from __future__ import annotations

import time

import main


def _clear():
    with main._THUMB_FETCH_FAIL_LOCK:
        main._THUMB_FETCH_FAIL_CACHE.clear()


def test_unseen_url_not_failed():
    _clear()
    assert main._thumb_fetch_recently_failed("https://x.test/a.jpg") is False


def test_marked_url_is_failed():
    _clear()
    main._mark_thumb_fetch_failed("https://x.test/a.jpg")
    assert main._thumb_fetch_recently_failed("https://x.test/a.jpg") is True
    # Distinct URL is unaffected.
    assert main._thumb_fetch_recently_failed("https://x.test/b.jpg") is False


def test_expired_entry_clears(monkeypatch):
    _clear()
    main._mark_thumb_fetch_failed("https://x.test/a.jpg")
    # Force expiry by rewinding the stored deadline into the past.
    with main._THUMB_FETCH_FAIL_LOCK:
        main._THUMB_FETCH_FAIL_CACHE["https://x.test/a.jpg"] = time.monotonic() - 1
    assert main._thumb_fetch_recently_failed("https://x.test/a.jpg") is False
    # And the expired key is pruned on read.
    assert "https://x.test/a.jpg" not in main._THUMB_FETCH_FAIL_CACHE


def test_timeout_is_capped():
    # Per-phase float timeout (12.0) could total ~24s; we cap with an explicit Timeout.
    assert main._THUMB_FETCH_TIMEOUT.read == 6.0
    assert main._THUMB_FETCH_TIMEOUT.connect == 4.0
