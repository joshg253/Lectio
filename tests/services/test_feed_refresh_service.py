from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest
import socks

import services.feed_refresh as _feed_refresh_mod
from services import bot_challenge
from services.feed_refresh import FeedRefreshService


@pytest.fixture(autouse=True)
def _no_real_pace_sleep(monkeypatch):
    """Neutralize the high-fanout pacing sleep so tests don't wait in real time.
    The pacing-specific tests re-patch sleep with their own recorder."""
    monkeypatch.setattr(_feed_refresh_mod.time, "sleep", lambda s: None)


class _ReaderCtx:
    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeReader:
    def __init__(self, fail_urls: set[str] | None = None):
        self.fail_urls = fail_urls or set()
        self.updated: list[str] = []

    def update_feed(self, feed_url: str):
        self.updated.append(feed_url)
        if feed_url in self.fail_urls:
            raise RuntimeError("404 Not Found")


def _make_conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_failure_state (
            feed_url TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL,
            last_error TEXT,
            last_failure_at REAL,
            last_success_at REAL,
            acknowledged_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_failure_state (
            domain TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL,
            last_failure_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_feeds (
            folder_id INTEGER NOT NULL,
            feed_url TEXT NOT NULL,
            PRIMARY KEY(folder_id, feed_url)
        )
        """
    )
    return conn


def _build_service(db_path: Path, reader, yt_calls: list[str], lead_calls: list[str],
                   on_fetch_refused=None, on_fetch_still_blocked=None,
                   on_bot_challenge_still_blocked=None,
                   on_fetch_still_blocked_via_proxy=None, on_proxy_unreachable=None,
                   progress_hook=None):
    def get_meta_connection():
        return _make_conn(db_path)

    return FeedRefreshService(
        progress_hook=progress_hook,
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(reader),
        fetch_and_store_youtube_durations=lambda feed_url: yt_calls.append(feed_url),
        fetch_and_store_lead_images=lambda feed_url: lead_calls.append(feed_url),
        format_datetime_for_ui=lambda _dt: "formatted",
        logger=logging.getLogger("test-refresh"),
        refresh_debug_enabled=False,
        failed_feed_backoff_base_seconds=60,
        failed_feed_backoff_max_seconds=24 * 60 * 60,
        on_fetch_refused=on_fetch_refused,
        on_fetch_still_blocked=on_fetch_still_blocked,
        on_bot_challenge_still_blocked=on_bot_challenge_still_blocked,
        on_fetch_still_blocked_via_proxy=on_fetch_still_blocked_via_proxy,
        on_proxy_unreachable=on_proxy_unreachable,
    )


def test_is_fetch_refusal_classifies_only_refusals():
    f = FeedRefreshService._is_fetch_refusal
    assert f(RuntimeError("HTTP 415 Unsupported Media Type"))
    assert f(RuntimeError("403 Forbidden"))
    assert f(RuntimeError("read operation timed out"))
    assert f(RuntimeError("server returned 503"))
    assert not f(RuntimeError("404 Not Found"))
    assert not f(RuntimeError("410 Gone"))
    assert not f(RuntimeError("401 Unauthorized"))


class _RefusingReader:
    """Fails the first update of a feed with a refusal, succeeds on retry."""
    def __init__(self, refuse_urls: set[str]):
        self.refuse_urls = set(refuse_urls)
        self.attempts: list[str] = []

    def update_feed(self, feed_url: str):
        self.attempts.append(feed_url)
        if feed_url in self.refuse_urls:
            self.refuse_urls.discard(feed_url)  # refuse only the first attempt
            raise RuntimeError("HTTP 415 Unsupported Media Type")


def test_refusal_triggers_flag_and_retry(tmp_path: Path):
    db_path = tmp_path / "m.sqlite"
    reader = _RefusingReader({"https://blocked.test/feed"})
    flagged: list[str] = []

    def on_refused(url: str) -> bool:
        flagged.append(url)
        return True  # newly flagged → service should retry once

    service = _build_service(db_path, reader, [], [], on_fetch_refused=on_refused)
    service.update_feeds(["https://blocked.test/feed"])

    assert flagged == ["https://blocked.test/feed"]
    # Retried after flagging (two attempts total).
    assert reader.attempts == ["https://blocked.test/feed", "https://blocked.test/feed"]
    # The successful retry cleared the failure state.
    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, last_success_at FROM feed_failure_state WHERE feed_url = ?",
            ("https://blocked.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 0
    assert row["last_success_at"] is not None


def test_refusal_no_retry_when_not_newly_flagged(tmp_path: Path):
    # Already-flagged feed (callback returns False) must not retry-loop.
    reader = _RefusingReader({"https://blocked.test/feed"})
    service = _build_service(tmp_path / "m2.sqlite", reader, [], [], on_fetch_refused=lambda _u: False)
    service.update_feeds(["https://blocked.test/feed"])
    assert reader.attempts == ["https://blocked.test/feed"]  # no retry


# --- proxy (as_needed) escalation, on top of browser-UA ---

class _NTimesRefusingReader:
    """Fails a feed's first N attempts with a refusal, succeeds after that."""
    def __init__(self, fail_counts: dict[str, int]):
        self.remaining = dict(fail_counts)
        self.attempts: list[str] = []

    def update_feed(self, feed_url: str):
        self.attempts.append(feed_url)
        left = self.remaining.get(feed_url, 0)
        if left > 0:
            self.remaining[feed_url] = left - 1
            raise RuntimeError("HTTP 415 Unsupported Media Type")


def _wrapped_bot_challenge() -> RuntimeError:
    """A FeedBlockedError wrapped the way reader wraps it in a ParseError
    (`raise exc from e`) — see services/reader_api.py's response hook."""
    try:
        try:
            raise bot_challenge.FeedBlockedError("Cloudflare challenge", "https://blocked.test/feed")
        except bot_challenge.FeedBlockedError as e:
            raise RuntimeError("unexpected error while getting feed") from e
    except RuntimeError as wrapped:
        return wrapped


def test_is_refusal_or_challenge_recognizes_wrapped_bot_challenge():
    f = FeedRefreshService._is_refusal_or_challenge
    assert f(_wrapped_bot_challenge())
    assert f(RuntimeError("403 Forbidden"))  # still classifies plain refusals
    assert not f(RuntimeError("404 Not Found"))


def test_is_bot_challenge_is_narrower_than_is_refusal_or_challenge():
    """FlareSolverr escalation must be gated on an actual challenge page, not
    any refusal — spinning up real Chrome is wasted on a plain IP-block 403."""
    f = FeedRefreshService._is_bot_challenge
    assert f(_wrapped_bot_challenge())
    assert not f(RuntimeError("403 Forbidden"))
    assert not f(RuntimeError("read operation timed out"))


class _NTimesBotChallengeReader:
    """Fails a feed's first N attempts with a wrapped bot-challenge, succeeds
    after that — same shape as _NTimesRefusingReader but for the narrower
    FlareSolverr escalation gate."""
    def __init__(self, fail_counts: dict[str, int]):
        self.remaining = dict(fail_counts)
        self.attempts: list[str] = []

    def update_feed(self, feed_url: str):
        self.attempts.append(feed_url)
        left = self.remaining.get(feed_url, 0)
        if left > 0:
            self.remaining[feed_url] = left - 1
            raise _wrapped_bot_challenge()


def test_proxy_escalation_fires_after_browser_ua_retry_also_fails(tmp_path: Path):
    """browser-UA newly-flags and retries; that retry ALSO fails; proxy
    escalation then flags and retries once more, which succeeds."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 2})
    browser_ua_flagged: list[str] = []
    proxy_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda u: browser_ua_flagged.append(u) or True,
        on_fetch_still_blocked=lambda u: proxy_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert browser_ua_flagged == ["https://blocked.test/feed"]
    assert proxy_flagged == ["https://blocked.test/feed"]
    # original + browser-UA retry + proxy retry = 3 attempts
    assert reader.attempts == ["https://blocked.test/feed"] * 3
    with _make_conn(tmp_path / "m.sqlite") as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?",
            ("https://blocked.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 0


def test_proxy_escalation_fires_directly_when_already_browser_ua_flagged(tmp_path: Path):
    """Feed already browser-UA-flagged (on_fetch_refused returns False, no
    retry) — proxy escalation must still fire off the original failure,
    without waiting for a browser-UA retry that will never happen."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 1})
    proxy_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: False,  # already flagged
        on_fetch_still_blocked=lambda u: proxy_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert proxy_flagged == ["https://blocked.test/feed"]
    # original (fails) + proxy retry (succeeds) = 2 attempts, no browser-UA retry
    assert reader.attempts == ["https://blocked.test/feed"] * 2


def test_proxy_escalation_not_attempted_when_browser_ua_retry_succeeds(tmp_path: Path):
    """The common case: browser-UA alone fixes it — proxy must never be
    consulted at all."""
    reader = _RefusingReader({"https://blocked.test/feed"})
    proxy_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda u: proxy_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert proxy_flagged == []
    assert reader.attempts == ["https://blocked.test/feed"] * 2


def test_proxy_escalation_still_failing_falls_through_to_normal_bookkeeping(tmp_path: Path):
    """Both escalations exhausted — must record the failure (not retry-loop
    forever) using the most recent attempt's error."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 99})
    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda _u: True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    # original + browser-UA retry + proxy retry = 3 attempts, then give up
    assert reader.attempts == ["https://blocked.test/feed"] * 3
    with _make_conn(tmp_path / "m.sqlite") as conn:
        row = conn.execute(
            "SELECT consecutive_failures, last_error FROM feed_failure_state WHERE feed_url = ?",
            ("https://blocked.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 1
    assert "refused" in (row["last_error"] or "").lower() or "unsupported media" in (row["last_error"] or "").lower()


# --- FlareSolverr escalation (one rung past the proxy, before last-resort) ---

def test_flaresolverr_escalation_fires_after_proxy_retry_also_fails(tmp_path: Path):
    """browser-UA and proxy both newly-flag and retry; both retries ALSO fail
    with a real bot-challenge; FlareSolverr escalation then flags and retries
    once more, which succeeds."""
    reader = _NTimesBotChallengeReader({"https://blocked.test/feed": 3})
    proxy_flagged: list[str] = []
    flaresolverr_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda u: True,
        on_fetch_still_blocked=lambda u: proxy_flagged.append(u) or True,
        on_bot_challenge_still_blocked=lambda u: flaresolverr_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert proxy_flagged == ["https://blocked.test/feed"]
    assert flaresolverr_flagged == ["https://blocked.test/feed"]
    # original + browser-UA retry + proxy retry + flaresolverr retry = 4 attempts
    assert reader.attempts == ["https://blocked.test/feed"] * 4
    with _make_conn(tmp_path / "m.sqlite") as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?",
            ("https://blocked.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 0


def test_flaresolverr_escalation_skipped_for_a_plain_refusal(tmp_path: Path):
    """The whole point of the narrower gate: a plain 403 that survives browser-
    UA and proxy escalation must fall straight through to last-resort, never
    spinning up FlareSolverr for a failure that isn't a challenge page."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 3})
    flaresolverr_flagged: list[str] = []
    tailscale_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda u: True,
        on_fetch_still_blocked=lambda u: True,
        on_bot_challenge_still_blocked=lambda u: flaresolverr_flagged.append(u) or True,
        on_fetch_still_blocked_via_proxy=lambda u: tailscale_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert flaresolverr_flagged == []  # never consulted — not a challenge
    assert tailscale_flagged == ["https://blocked.test/feed"]  # went straight there
    # original + browser-UA retry + proxy retry + tailscale retry = 4 attempts
    assert reader.attempts == ["https://blocked.test/feed"] * 4


def test_flaresolverr_escalation_fires_directly_when_already_proxy_flagged(tmp_path: Path):
    """Feed already flagged for both browser-UA and proxy (both callbacks
    return False, no retries) — FlareSolverr escalation must still fire off
    the original bot-challenge failure, without waiting for retries that will
    never happen."""
    reader = _NTimesBotChallengeReader({"https://blocked.test/feed": 1})
    flaresolverr_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: False,  # already flagged
        on_fetch_still_blocked=lambda _u: False,  # already flagged
        on_bot_challenge_still_blocked=lambda u: flaresolverr_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert flaresolverr_flagged == ["https://blocked.test/feed"]
    # original (fails) + flaresolverr retry (succeeds) = 2 attempts
    assert reader.attempts == ["https://blocked.test/feed"] * 2


def test_flaresolverr_escalation_not_attempted_when_proxy_retry_succeeds(tmp_path: Path):
    """The common case: proxy alone fixes it — FlareSolverr must never be
    consulted at all."""
    reader = _NTimesBotChallengeReader({"https://blocked.test/feed": 2})
    flaresolverr_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda _u: True,
        on_bot_challenge_still_blocked=lambda u: flaresolverr_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert flaresolverr_flagged == []
    assert reader.attempts == ["https://blocked.test/feed"] * 3


def test_flaresolverr_escalation_falls_through_to_tailscale_when_still_failing(tmp_path: Path):
    """FlareSolverr tried and STILL a challenge — must cascade to last-resort,
    same as proxy cascading to FlareSolverr."""
    reader = _NTimesBotChallengeReader({"https://blocked.test/feed": 4})
    tailscale_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda _u: True,
        on_bot_challenge_still_blocked=lambda _u: True,
        on_fetch_still_blocked_via_proxy=lambda u: tailscale_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert tailscale_flagged == ["https://blocked.test/feed"]
    # original + browser-UA + proxy + flaresolverr + tailscale retry = 5 attempts
    assert reader.attempts == ["https://blocked.test/feed"] * 5


def test_flaresolverr_escalation_not_consulted_when_callback_absent(tmp_path: Path):
    """No on_bot_challenge_still_blocked wired (e.g. FlareSolverr not
    configured anywhere) — proxy escalation still runs, cascades straight to
    last-resort instead."""
    reader = _NTimesBotChallengeReader({"https://blocked.test/feed": 3})
    tailscale_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda _u: True,
        on_fetch_still_blocked_via_proxy=lambda u: tailscale_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert tailscale_flagged == ["https://blocked.test/feed"]
    # original + browser-UA + proxy + tailscale retry = 4 attempts (flaresolverr skipped)
    assert reader.attempts == ["https://blocked.test/feed"] * 4


# --- last-resort escalation (one rung past the proxy) ---

def test_tailscale_escalation_fires_after_proxy_retry_also_fails(tmp_path: Path):
    """browser-UA and proxy both newly-flag and retry; both retries ALSO fail;
    last-resort escalation then flags and retries once more, which succeeds."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 3})
    proxy_flagged: list[str] = []
    tailscale_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda u: True,
        on_fetch_still_blocked=lambda u: proxy_flagged.append(u) or True,
        on_fetch_still_blocked_via_proxy=lambda u: tailscale_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert proxy_flagged == ["https://blocked.test/feed"]
    assert tailscale_flagged == ["https://blocked.test/feed"]
    # original + browser-UA retry + proxy retry + last-resort retry = 4 attempts
    assert reader.attempts == ["https://blocked.test/feed"] * 4
    with _make_conn(tmp_path / "m.sqlite") as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?",
            ("https://blocked.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 0


def test_tailscale_escalation_fires_directly_when_already_proxy_flagged(tmp_path: Path):
    """Feed already flagged for both browser-UA and proxy (both callbacks
    return False, no retries) — last-resort escalation must still fire off the
    original failure, without waiting for retries that will never happen."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 1})
    tailscale_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: False,  # already flagged
        on_fetch_still_blocked=lambda _u: False,  # already flagged
        on_fetch_still_blocked_via_proxy=lambda u: tailscale_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert tailscale_flagged == ["https://blocked.test/feed"]
    # original (fails) + last-resort retry (succeeds) = 2 attempts
    assert reader.attempts == ["https://blocked.test/feed"] * 2


def test_tailscale_escalation_not_attempted_when_proxy_retry_succeeds(tmp_path: Path):
    """The common case: proxy alone fixes it — last-resort must never be
    consulted at all."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 2})
    tailscale_flagged: list[str] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda _u: True,
        on_fetch_still_blocked_via_proxy=lambda u: tailscale_flagged.append(u) or True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    assert tailscale_flagged == []
    assert reader.attempts == ["https://blocked.test/feed"] * 3


def test_tailscale_escalation_still_failing_falls_through_to_normal_bookkeeping(tmp_path: Path):
    """All three escalations exhausted — must record the failure (not
    retry-loop forever) using the most recent attempt's error."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 99})
    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda _u: True,
        on_fetch_still_blocked_via_proxy=lambda _u: True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    # original + browser-UA + proxy + last-resort retry = 4 attempts, then give up
    assert reader.attempts == ["https://blocked.test/feed"] * 4
    with _make_conn(tmp_path / "m.sqlite") as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?",
            ("https://blocked.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 1


def test_tailscale_escalation_not_consulted_when_callback_absent(tmp_path: Path):
    """No on_fetch_still_blocked_via_proxy wired (e.g. no last-resort backend
    configured anywhere) — proxy escalation still runs, nothing past it."""
    reader = _NTimesRefusingReader({"https://blocked.test/feed": 2})
    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
        on_fetch_still_blocked=lambda _u: True,
    )
    service.update_feeds(["https://blocked.test/feed"])

    # original + browser-UA retry + proxy retry = 3 attempts, then give up
    # (no fourth attempt — nothing left to escalate to)
    assert reader.attempts == ["https://blocked.test/feed"] * 3


# --- proxy-unreachable auto-fallback (the proxy backend itself is down,
#     not the site refusing us) ---

def _wrapped_proxy_unreachable() -> RuntimeError:
    """A proxy-connection failure chained the way requests/urllib3/pysocks
    actually do it: the socks.ProxyError is reachable only via __context__
    (implicit — raised inside an except block with no `from`), not __cause__.
    A __cause__-only chain walk (the original, buggy version of
    _is_proxy_unreachable) would silently miss this."""
    try:
        raise socks.ProxyConnectionError("Error connecting to SOCKS5 proxy gluetun:1080: refused")
    except socks.ProxyConnectionError:
        try:
            raise RuntimeError("Max retries exceeded")  # no `from` -> __context__ only
        except RuntimeError as e:
            return e


def test_is_proxy_unreachable_walks_context_not_just_cause():
    assert FeedRefreshService._is_proxy_unreachable(_wrapped_proxy_unreachable())
    assert not FeedRefreshService._is_proxy_unreachable(RuntimeError("HTTP 415 Unsupported Media Type"))
    assert not FeedRefreshService._is_proxy_unreachable(RuntimeError("404 Not Found"))


class _ProxyDownThenOkReader:
    """First call raises a proxy-unreachable-shaped failure; every call after
    that succeeds — simulating the fallback retry going direct."""
    def __init__(self):
        self.calls = 0

    def update_feed(self, feed_url: str):
        self.calls += 1
        if self.calls == 1:
            raise _wrapped_proxy_unreachable()


def test_proxy_unreachable_falls_back_to_direct_and_succeeds(tmp_path: Path):
    reader = _ProxyDownThenOkReader()
    marked_down: list[bool] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_proxy_unreachable=lambda _u: marked_down.append(True),
    )
    service.update_feeds(["https://example.test/feed"])

    assert marked_down == [True]
    assert reader.calls == 2  # original (proxied, fails) + fallback (direct, succeeds)
    with _make_conn(tmp_path / "m.sqlite") as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 0


class _AlwaysProxyDownReader:
    """Every call raises a proxy-unreachable-shaped failure — the site is
    unreachable full stop, proxy or not."""
    def __init__(self):
        self.calls = 0

    def update_feed(self, feed_url: str):
        self.calls += 1
        raise _wrapped_proxy_unreachable()


def test_proxy_unreachable_fallback_also_failing_falls_through(tmp_path: Path):
    reader = _AlwaysProxyDownReader()
    marked_down: list[bool] = []

    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_proxy_unreachable=lambda _u: marked_down.append(True),
    )
    service.update_feeds(["https://example.test/feed"])

    assert marked_down == [True]
    assert reader.calls == 2  # original + one direct fallback attempt, then give up
    with _make_conn(tmp_path / "m.sqlite") as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 1


def test_proxy_unreachable_not_consulted_when_callback_absent(tmp_path: Path):
    """No on_proxy_unreachable configured — must behave exactly like before
    this fix (single attempt, normal failure bookkeeping, no retry)."""
    reader = _ProxyDownThenOkReader()
    service = _build_service(tmp_path / "m.sqlite", reader, [], [])
    service.update_feeds(["https://example.test/feed"])
    assert reader.calls == 1


def test_proxy_escalation_not_consulted_when_callback_absent(tmp_path: Path):
    """No on_fetch_still_blocked configured (proxy feature entirely unwired,
    e.g. an older config) — behavior must be identical to before this PR."""
    reader = _RefusingReader({"https://blocked.test/feed"})
    service = _build_service(
        tmp_path / "m.sqlite", reader, [], [],
        on_fetch_refused=lambda _u: True,
    )
    service.update_feeds(["https://blocked.test/feed"])
    assert reader.attempts == ["https://blocked.test/feed"] * 2  # browser-UA retry only


def test_compute_backoff_caps_at_max(tmp_path: Path):
    reader = _FakeReader()
    calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(tmp_path / "meta.sqlite", reader, calls, lead_calls)

    assert service.compute_failed_feed_backoff_seconds(1) == 60
    assert service.compute_failed_feed_backoff_seconds(2) == 120
    assert service.compute_failed_feed_backoff_seconds(30) == 24 * 60 * 60


def test_update_feeds_records_success_and_failure(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader(fail_urls={"https://example.com/fail.xml"})
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    service.update_feeds(["https://example.com/good.xml", "https://example.com/fail.xml"])

    with _make_conn(db_path) as conn:
        ok_row = conn.execute(
            "SELECT consecutive_failures, next_retry_at, last_error FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.com/good.xml",),
        ).fetchone()
        fail_row = conn.execute(
            "SELECT consecutive_failures, next_retry_at, last_error FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.com/fail.xml",),
        ).fetchone()

    assert ok_row is not None
    assert ok_row["consecutive_failures"] == 0
    assert ok_row["next_retry_at"] is None
    assert ok_row["last_error"] is None

    assert fail_row is not None
    assert fail_row["consecutive_failures"] == 1
    assert fail_row["next_retry_at"] is not None
    assert "404" in fail_row["last_error"]

    assert reader.updated == ["https://example.com/good.xml", "https://example.com/fail.xml"]
    assert yt_calls == ["https://example.com/good.xml", "https://example.com/fail.xml"]
    assert lead_calls == ["https://example.com/good.xml", "https://example.com/fail.xml"]


def test_update_feeds_enhance_false_defers_enhancement(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    # enhance=False ingests entries but skips the network-heavy enhancement,
    service.update_feeds(["https://example.com/good.xml"], enhance=False)
    assert reader.updated == ["https://example.com/good.xml"]
    assert yt_calls == []
    assert lead_calls == []

    # which a caller can then run off the request path via enhance_feeds().
    service.enhance_feeds(["https://example.com/good.xml"])
    assert yt_calls == ["https://example.com/good.xml"]
    assert lead_calls == ["https://example.com/good.xml"]


def test_update_feeds_skips_when_backoff_not_elapsed(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            ("https://example.com/skip.xml", 3, time.time() + 3600, "some error"),
        )

    service.update_feeds(["https://example.com/skip.xml"])

    assert reader.updated == []
    # Current behavior: youtube duration follow-up still runs for each requested feed URL.
    assert yt_calls == ["https://example.com/skip.xml"]
    assert lead_calls == ["https://example.com/skip.xml"]


def test_update_feeds_bypass_backoff_fetches_anyway(tmp_path: Path):
    """A deliberate single-feed refresh (bypass_backoff=True) must actually
    fetch a feed still inside its backoff window, not silently no-op like the
    scheduler's own calls do."""
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    service = _build_service(db_path, reader, [], [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            ("https://example.com/skip.xml", 3, time.time() + 3600, "some error"),
        )

    service.update_feeds(["https://example.com/skip.xml"], bypass_backoff=True)

    assert reader.updated == ["https://example.com/skip.xml"]


def test_update_feeds_bypass_backoff_still_honors_reader_update_after(tmp_path: Path):
    """bypass_backoff skips our own feed/domain backoff, but not reader's own
    update_after (Retry-After/Cache-Control) — that's the server's own
    instruction, not just Lectio's pacing."""
    db_path = tmp_path / "meta.sqlite"

    class _UpdateAfterReader(_FakeReader):
        def get_feed(self, feed_url: str, _default=None):
            class _F:
                update_after = _UpdateAfterTs()
                last_updated = 1.0
            return _F()

    class _UpdateAfterTs:
        def timestamp(self) -> float:
            return time.time() + 3600

    reader = _UpdateAfterReader()
    service = _build_service(db_path, reader, [], [])

    service.update_feeds(["https://example.com/retry-after.xml"], bypass_backoff=True)

    assert reader.updated == []


def test_get_problematic_feeds_formats_retry_display(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO folder_feeds(folder_id, feed_url) VALUES (?, ?)",
            (1, "https://example.com/problem.xml"),
        )
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            ("https://example.com/problem.xml", 2, 1_900_000_000.0, "bad feed"),
        )
        rows = service.get_problematic_feeds(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["feed_url"] == "https://example.com/problem.xml"
    assert row["next_retry_display"] == "formatted"


class _NewFeedReader(_FakeReader):
    """get_feed reports last_updated=None (never fetched) for the given URLs."""
    def __init__(self, never_updated_urls: set[str], fail_urls: set[str] | None = None):
        super().__init__(fail_urls)
        self.never_updated_urls = set(never_updated_urls)

    def get_feed(self, feed_url: str, _default=None):
        class _F:
            update_after = None
            last_updated = None if feed_url in self.never_updated_urls else 1.0
        return _F()


def test_domain_backoff_skips_already_fetched_feed(tmp_path: Path):
    """A feed that has fetched before stays skipped while its domain is in backoff."""
    db_path = tmp_path / "meta.sqlite"
    reader = _NewFeedReader(never_updated_urls=set())
    service = _build_service(db_path, reader, [], [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO domain_failure_state(domain, consecutive_failures, next_retry_at) VALUES (?, ?, ?)",
            ("example.com", 13, time.time() + 3600),
        )

    service.update_feeds(["https://example.com/other.xml"])
    assert reader.updated == []


def test_domain_backoff_does_not_block_first_fetch_of_new_feed(tmp_path: Path):
    """A just-subscribed feed (never fetched) gets its initial refresh even while
    the domain is in backoff earned by other feeds' failures — otherwise the new
    subscription sits empty and invisible for hours (pinboard t:tag 500s put
    feeds.pinboard.in in backoff, silently skipping a newly added feed)."""
    db_path = tmp_path / "meta.sqlite"
    new_url = "https://example.com/new.xml"
    reader = _NewFeedReader(never_updated_urls={new_url})
    service = _build_service(db_path, reader, [], [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO domain_failure_state(domain, consecutive_failures, next_retry_at) VALUES (?, ?, ?)",
            ("example.com", 13, time.time() + 3600),
        )

    service.update_feeds([new_url])
    assert reader.updated == [new_url]


def test_feed_level_backoff_still_applies_to_new_feed(tmp_path: Path):
    """The new-feed exemption bypasses only the domain backoff; a feed-level
    backoff on the feed itself is still honored."""
    db_path = tmp_path / "meta.sqlite"
    new_url = "https://example.com/new.xml"
    reader = _NewFeedReader(never_updated_urls={new_url})
    service = _build_service(db_path, reader, [], [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            (new_url, 2, time.time() + 3600, "boom"),
        )

    service.update_feeds([new_url])
    assert reader.updated == []


class _NotFoundError(RuntimeError):
    """Mimics a reader update exception carrying HTTP status via http_info."""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        class _Info:
            def __init__(self, status: int):
                self.status = status
        self.http_info = _Info(status)


class _StatusFailReader(_FakeReader):
    def __init__(self, fail_statuses: dict[str, int]):
        super().__init__()
        self.fail_statuses = fail_statuses

    def update_feed(self, feed_url: str):
        self.updated.append(feed_url)
        status = self.fail_statuses.get(feed_url)
        if status:
            raise _NotFoundError(status)

    def get_feed(self, feed_url: str, _default=None):
        class _F:
            update_after = None
            last_updated = 1.0
        return _F()


class _FailReader(_FakeReader):
    """Fails the given URLs. If a url maps to an int it raises with that HTTP
    status (via http_info); otherwise it raises a transport-style error with no
    http_info (mirroring reader, whose 404s often arrive with status=None)."""
    def __init__(self, fails):
        super().__init__()
        self.fails = dict(fails) if isinstance(fails, dict) else {u: None for u in fails}

    def update_feed(self, feed_url: str):
        self.updated.append(feed_url)
        if feed_url in self.fails:
            status = self.fails[feed_url]
            raise _NotFoundError(status) if status else RuntimeError("connection refused")

    def get_feed(self, feed_url: str, _default=None):
        class _F:
            update_after = None
            last_updated = 1.0
        return _F()


def _yt(n: int) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id=C{n}"


def test_high_fanout_domain_never_backs_off(tmp_path: Path):
    """A host with many feeds in the batch (youtube.com) is exempt from
    domain-level backoff: a few dead channels must never create a lock that
    stalls the other subscriptions. Per-feed backoff still applies to the dead
    ones."""
    db_path = tmp_path / "meta.sqlite"
    feeds = [_yt(i) for i in range(10)]
    dead = {feeds[0]: 404, feeds[1]: 404, feeds[2]: 404}  # 3 dead channels
    reader = _FailReader(dead)
    service = _build_service(db_path, reader, [], [])

    service.update_feeds(feeds)
    assert set(reader.updated) == set(feeds)  # every feed attempted, none skipped
    with _make_conn(db_path) as conn:
        domain_row = conn.execute("SELECT * FROM domain_failure_state WHERE domain = 'www.youtube.com'").fetchone()
        feed_row = conn.execute("SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?", (feeds[0],)).fetchone()
    assert domain_row is None          # high-fanout host never domain-backed-off
    assert feed_row and feed_row[0] == 1  # dead channel still earns per-feed backoff


def test_high_fanout_domain_ignores_stale_backoff(tmp_path: Path):
    """A pre-existing (stuck) domain backoff must not skip a high-fanout host's
    feeds — that was the original starvation."""
    db_path = tmp_path / "meta.sqlite"
    feeds = [_yt(i) for i in range(10)]
    reader = _FailReader({})  # all succeed
    service = _build_service(db_path, reader, [], [])
    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO domain_failure_state(domain, consecutive_failures, next_retry_at, last_failure_at) VALUES (?, ?, ?, ?)",
            ("www.youtube.com", 17, time.time() + 80000, time.time()),
        )

    service.update_feeds(feeds)
    assert set(reader.updated) == set(feeds)  # not skipped despite the stale lock


def test_low_fanout_single_failure_no_skip(tmp_path: Path):
    """One failure on a small host is tracked but doesn't activate the skip."""
    db_path = tmp_path / "meta.sqlite"
    url = "https://down.example/feed.xml"
    reader = _FailReader({url: None})  # transport-style
    service = _build_service(db_path, reader, [], [])

    service.update_feeds([url])
    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, next_retry_at FROM domain_failure_state WHERE domain = 'down.example'"
        ).fetchone()
    assert row and row[0] == 1 and row[1] is None  # tracked, no active backoff


def test_low_fanout_backs_off_after_threshold_capped(tmp_path: Path):
    """A small host that's genuinely down (>= MIN_FAILURES consecutive failures)
    backs off, capped so it re-probes within ~1h."""
    db_path = tmp_path / "meta.sqlite"
    urls = ["https://down.example/a.xml", "https://down.example/b.xml", "https://down.example/c.xml"]
    reader = _FailReader({u: None for u in urls})
    service = _build_service(db_path, reader, [], [])

    service.update_feeds(urls)  # 3 consecutive failures on a low-fanout host
    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, next_retry_at FROM domain_failure_state WHERE domain = 'down.example'"
        ).fetchone()
    assert row and row[0] >= 3
    assert row[1] is not None and row[1] <= time.time() + 3600 + 5  # active, capped ~1h


def test_high_fanout_requests_are_paced(tmp_path: Path, monkeypatch):
    """Requests to a high-fanout host are spaced out so a big burst isn't
    throttled — every request after the first on that host waits."""
    import services.feed_refresh as fr
    db_path = tmp_path / "meta.sqlite"
    feeds = [_yt(i) for i in range(10)]  # 10 feeds on one host
    reader = _FailReader({})  # all succeed instantly
    service = _build_service(db_path, reader, [], [])
    sleeps: list[float] = []
    monkeypatch.setattr(fr.time, "sleep", lambda s: sleeps.append(s))

    service.update_feeds(feeds)
    assert len([s for s in sleeps if s > 0]) >= len(feeds) - 1  # all but the first paced


def test_low_fanout_requests_are_not_paced(tmp_path: Path, monkeypatch):
    """Small hosts are not paced — no needless delay on ordinary feeds."""
    import services.feed_refresh as fr
    db_path = tmp_path / "meta.sqlite"
    feeds = ["https://a.example/f.xml", "https://b.example/f.xml"]
    reader = _FailReader({})
    service = _build_service(db_path, reader, [], [])
    sleeps: list[float] = []
    monkeypatch.setattr(fr.time, "sleep", lambda s: sleeps.append(s))

    service.update_feeds(feeds)
    assert not [s for s in sleeps if s > 0]


def test_progress_hook_fires_per_feed_before_the_fetch(tmp_path):
    """The scheduler watchdog trips on lack of progress, so every feed must tick
    it — and BEFORE the fetch, so a hung feed is the one the log names."""
    reader = _FakeReader()
    stages: list[str] = []
    svc = _build_service(tmp_path / "m.sqlite", reader, [], [],
                         progress_hook=stages.append)

    svc.update_feeds(["https://a.test/feed", "https://b.test/feed"], enhance=False)

    assert any("https://a.test/feed" in s for s in stages)
    assert any("https://b.test/feed" in s for s in stages)
    # The a-feed's stage is recorded before the b-feed's, so a hang on a leaves
    # "a" as the reported stage rather than "b".
    a_at = next(i for i, s in enumerate(stages) if "a.test" in s)
    b_at = next(i for i, s in enumerate(stages) if "b.test" in s)
    assert a_at < b_at


def test_progress_hook_fires_for_a_failing_feed_too(tmp_path):
    """A batch of dead feeds still counts as progress — it is moving, just failing."""
    reader = _FakeReader(fail_urls={"https://dead.test/feed"})
    stages: list[str] = []
    svc = _build_service(tmp_path / "m.sqlite", reader, [], [],
                         progress_hook=stages.append)

    svc.update_feeds(["https://dead.test/feed"], enhance=False)

    assert any("https://dead.test/feed" in s for s in stages)


def test_progress_hook_fires_during_the_enhancement_pass(tmp_path):
    """Enhancement is the network-heavy half and runs in the scheduler thread, so
    a stall there must be visible too."""
    reader = _FakeReader()
    stages: list[str] = []
    svc = _build_service(tmp_path / "m.sqlite", reader, [], [],
                         progress_hook=stages.append)

    svc.update_feeds(["https://a.test/feed"], enhance=True)

    assert any(s.startswith("enhance ") for s in stages)


def test_a_raising_progress_hook_never_breaks_a_refresh(tmp_path):
    """It is telemetry. It must not be able to stop feeds updating."""
    reader = _FakeReader()

    def _boom(_stage):
        raise RuntimeError("telemetry exploded")

    svc = _build_service(tmp_path / "m.sqlite", reader, [], [], progress_hook=_boom)

    svc.update_feeds(["https://a.test/feed"], enhance=False)

    assert reader.updated == ["https://a.test/feed"]
