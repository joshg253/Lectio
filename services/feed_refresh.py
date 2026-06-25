from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


def _feed_domain(feed_url: str) -> str:
    """Return the netloc (host[:port]) of a feed URL, lower-cased."""
    try:
        return urlparse(feed_url).netloc.lower()
    except Exception:
        return ""


class FeedRefreshService:
    """Encapsulates feed refresh orchestration and failure backoff state."""

    def __init__(
        self,
        *,
        get_meta_connection: Callable[[], sqlite3.Connection],
        get_reader: Callable[[], Any],
        fetch_and_store_youtube_durations: Callable[[str], None],
        fetch_and_store_lead_images: Callable[[str], None],
        format_datetime_for_ui: Callable[[datetime | None], str | None],
        logger: logging.Logger,
        refresh_debug_enabled: bool,
        failed_feed_backoff_base_seconds: int,
        failed_feed_backoff_max_seconds: int,
        on_fetch_refused: Callable[[str], bool] | None = None,
    ) -> None:
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._fetch_and_store_youtube_durations = fetch_and_store_youtube_durations
        self._fetch_and_store_lead_images = fetch_and_store_lead_images
        self._format_datetime_for_ui = format_datetime_for_ui
        self._logger = logger
        self._refresh_debug_enabled = refresh_debug_enabled
        self._failed_feed_backoff_base_seconds = failed_feed_backoff_base_seconds
        self._failed_feed_backoff_max_seconds = failed_feed_backoff_max_seconds
        # Called with a feed_url when an honest-UA fetch is refused (403/415/429/
        # 503/timeout). Should flag the feed for browser-identity escalation and
        # return True if it was newly flagged (so this cycle can retry it). Keeps
        # the good-citizen escalation policy out of the service layer.
        self._on_fetch_refused = on_fetch_refused

    @staticmethod
    def _is_fetch_refusal(exc: Exception) -> bool:
        """True when an exception looks like a host refusing our honest client —
        an HTTP 403/415/429/503 or a connection timeout/hang — i.e. something a
        browser identity might get past. 401/404/410 are NOT refusals of this kind
        (auth/missing/gone), so they don't escalate."""
        status = None
        http_info = getattr(exc, "http_info", None)
        if http_info is not None:
            status = getattr(http_info, "status", None)
        if status in (403, 415, 429, 503):
            return True
        detail = str(exc).lower()
        if any(code in detail for code in ("403", "415", "429", "503")):
            return True
        return "timed out" in detail or "timeout" in detail or "connecttimeout" in detail

    def compute_failed_feed_backoff_seconds(self, consecutive_failures: int) -> int:
        failures = max(1, int(consecutive_failures))
        backoff = self._failed_feed_backoff_base_seconds * (2 ** (failures - 1))
        return min(backoff, self._failed_feed_backoff_max_seconds)

    def format_retry_epoch_for_ui(self, epoch_seconds: float | int | None) -> str | None:
        if epoch_seconds is None:
            return None
        try:
            dt = datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
        except Exception:
            return None
        return self._format_datetime_for_ui(dt)

    def get_problematic_feeds(self, conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, object]]:
        rows = conn.execute(
            """
            SELECT
                s.feed_url,
                s.consecutive_failures,
                s.next_retry_at,
                s.last_error,
                s.last_failure_at,
                s.acknowledged_at,
                MIN(ff.folder_id) AS folder_id
            FROM feed_failure_state s
            JOIN folder_feeds ff ON ff.feed_url = s.feed_url
            WHERE s.consecutive_failures > 0
            GROUP BY s.feed_url, s.consecutive_failures, s.next_retry_at, s.last_error, s.last_failure_at, s.acknowledged_at
            ORDER BY s.acknowledged_at ASC NULLS FIRST, s.next_retry_at DESC, s.consecutive_failures DESC, s.feed_url ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        result: list[dict[str, object]] = []
        for row in rows:
            result.append(
                {
                    "feed_url": str(row["feed_url"]),
                    "consecutive_failures": int(row["consecutive_failures"] or 0),
                    "next_retry_at": float(row["next_retry_at"]) if row["next_retry_at"] is not None else None,
                    "last_failure_at": float(row["last_failure_at"]) if row["last_failure_at"] is not None else None,
                    "next_retry_display": self.format_retry_epoch_for_ui(row["next_retry_at"]),
                    "last_error": str(row["last_error"] or ""),
                    "folder_id": int(row["folder_id"]) if row["folder_id"] is not None else None,
                    "acknowledged_at": float(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None,
                }
            )
        return result

    def humanize_feed_exception(self, last_exception: object) -> str:
        raw_detail = str(getattr(last_exception, "value_str", None) or str(last_exception))
        detail = " ".join(raw_detail.split())
        lowered = detail.lower()

        if "connecttimeout" in lowered or "timed out" in lowered:
            return (
                "Connection timed out while contacting this feed server. "
                "The feed host may be down or slow, or the network path may be blocked."
            )
        if "name or service not known" in lowered or "temporary failure in name resolution" in lowered:
            return "Could not resolve the feed host name (DNS lookup failed)."
        if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
            return "TLS/SSL handshake failed while connecting to the feed URL."
        if "403" in lowered or "forbidden" in lowered:
            return "The feed server denied access (HTTP 403 Forbidden). The server may be blocking this IP or user-agent."
        if "410" in lowered or "gone" in lowered:
            return "The feed has been permanently removed (HTTP 410 Gone). Feed updates have been disabled."
        if "404" in lowered or "not found" in lowered:
            return "The feed URL returned not found (HTTP 404)."
        if "401" in lowered or "unauthorized" in lowered:
            return "The feed requires authentication (HTTP 401 Unauthorized)."
        if "429" in lowered or "too many requests" in lowered:
            return "The feed server is rate-limiting requests (HTTP 429)."
        if "text/html" in lowered or "no parser for mime type" in lowered:
            return "The feed URL returned an HTML page instead of RSS/Atom. The server may be blocking automated requests or the URL may be wrong."
        if "parseerror" in lowered or "xml" in lowered or "invalid" in lowered:
            return "The feed response could not be parsed as a valid RSS/Atom document."

        if len(detail) > 260:
            return f"{detail[:257]}..."
        return detail or "An unknown feed retrieval error occurred."

    @staticmethod
    def _http_status_of(exc: object) -> int | None:
        """Pull the HTTP status off a reader update exception, if it carries one."""
        http_info = getattr(exc, "http_info", None)
        status = getattr(http_info, "status", None)
        return int(status) if isinstance(status, int) else None

    def _record_fetch_history(
        self,
        conn,
        feed_url: str,
        status: str,
        *,
        http_status: int | None = None,
        new_entries: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        """Append one row to the per-feed fetch history. Best-effort: history is
        diagnostic, so a logging failure must never break the refresh itself."""
        try:
            conn.execute(
                "INSERT INTO feed_fetch_history"
                " (feed_url, fetched_at, status, http_status, new_entries, duration_ms, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (feed_url, time.time(), status, http_status, new_entries, duration_ms, error),
            )
        except Exception:
            self._logger.debug("[refresh] fetch-history insert failed for %s", feed_url, exc_info=True)

    def update_feeds(self, feed_urls: Iterable[str]) -> None:
        feed_url_list = list(feed_urls)
        if self._refresh_debug_enabled:
            self._logger.info("[refresh] start: feed_count=%d", len(feed_url_list))

        if not feed_url_list:
            if self._refresh_debug_enabled:
                self._logger.info("[refresh] no feeds to update")
            return

        started_at = time.perf_counter()
        success_count = 0
        error_count = 0
        skipped_count = 0
        now_ts = time.time()

        feed_state_map: dict[str, dict[str, int | float | None]] = {}
        domain_state_map: dict[str, dict[str, int | float | None]] = {}
        # Short read transaction: load backoff state, then release the lock
        # immediately so the per-feed HTTP fetches below don't hold it open.
        with self._get_meta_connection() as conn:
            placeholders = ",".join("?" for _ in feed_url_list)
            rows = conn.execute(
                f"SELECT feed_url, consecutive_failures, next_retry_at FROM feed_failure_state WHERE feed_url IN ({placeholders})",
                feed_url_list,
            ).fetchall()
            for row in rows:
                feed_state_map[str(row["feed_url"])] = {
                    "consecutive_failures": int(row["consecutive_failures"] or 0),
                    "next_retry_at": float(row["next_retry_at"]) if row["next_retry_at"] is not None else None,
                }

            # Load domain-level failure state for all domains in this batch.
            domains = list({_feed_domain(u) for u in feed_url_list if _feed_domain(u)})
            if domains:
                domain_placeholders = ",".join("?" for _ in domains)
                domain_rows = conn.execute(
                    f"SELECT domain, consecutive_failures, next_retry_at FROM domain_failure_state WHERE domain IN ({domain_placeholders})",
                    domains,
                ).fetchall()
                for row in domain_rows:
                    domain_state_map[str(row["domain"])] = {
                        "consecutive_failures": int(row["consecutive_failures"] or 0),
                        "next_retry_at": float(row["next_retry_at"]) if row["next_retry_at"] is not None else None,
                    }
        # Read transaction committed; meta DB lock released before any HTTP fetches.

        with self._get_reader() as reader:
            for idx, feed_url in enumerate(feed_url_list, start=1):
                    feed_started_at = time.perf_counter()
                    feed_state = feed_state_map.get(feed_url) or {}
                    domain = _feed_domain(feed_url)
                    domain_state = domain_state_map.get(domain) or {}

                    # Skip if either the feed-level or domain-level backoff is active.
                    feed_next_retry = feed_state.get("next_retry_at")
                    domain_next_retry = domain_state.get("next_retry_at")

                    # Also respect reader's built-in update_after, which captures
                    # Retry-After from 429/503 responses and Cache-Control max-age.
                    reader_update_after: float | None = None
                    try:
                        _feed_obj = reader.get_feed(feed_url, None)
                        if _feed_obj and _feed_obj.update_after:
                            reader_update_after = _feed_obj.update_after.timestamp()
                    except Exception:
                        pass

                    effective_next_retry = (
                        max(
                            feed_next_retry if feed_next_retry is not None else 0.0,
                            domain_next_retry if domain_next_retry is not None else 0.0,
                            reader_update_after if reader_update_after is not None else 0.0,
                        )
                        or None
                    )
                    if effective_next_retry is not None and effective_next_retry > now_ts:
                        skipped_count += 1
                        if self._refresh_debug_enabled:
                            retry_in_seconds = int(max(1, effective_next_retry - now_ts))
                            _rua = reader_update_after or 0.0
                            if _rua >= (feed_next_retry or 0) and _rua >= (domain_next_retry or 0):
                                source = "reader(429/cache-control)"
                            elif (domain_next_retry or 0) >= (feed_next_retry or 0):
                                source = "domain"
                            else:
                                source = "feed"
                            self._logger.info(
                                "[refresh] skipping %d/%d for %ds %s-backoff: %s",
                                idx,
                                len(feed_url_list),
                                retry_in_seconds,
                                source,
                                feed_url,
                            )
                        continue

                    try:
                        if self._refresh_debug_enabled:
                            self._logger.info("[refresh] updating %d/%d: %s", idx, len(feed_url_list), feed_url)
                        _updated = reader.update_feed(feed_url)
                        success_count += 1
                        # update_feed returns an UpdatedFeed (with new/modified
                        # counts) or None when the feed was unchanged (304).
                        _new_entries = int(getattr(_updated, "new", 0)) if _updated else 0
                        _ok_duration_ms = int((time.perf_counter() - feed_started_at) * 1000)
                        if self._refresh_debug_enabled:
                            elapsed_ms = int((time.perf_counter() - feed_started_at) * 1000)
                            self._logger.info(
                                "[refresh] updated %d/%d in %dms: %s",
                                idx,
                                len(feed_url_list),
                                elapsed_ms,
                                feed_url,
                            )

                        # Short write transaction: released immediately after each feed.
                        with self._get_meta_connection() as conn:
                            conn.execute(
                                """
                                INSERT INTO feed_failure_state (feed_url, consecutive_failures, next_retry_at, last_error, last_success_at)
                                VALUES (?, 0, NULL, NULL, ?)
                                ON CONFLICT(feed_url) DO UPDATE SET
                                    consecutive_failures = 0,
                                    next_retry_at = NULL,
                                    last_error = NULL,
                                    last_success_at = excluded.last_success_at,
                                    acknowledged_at = NULL
                                """,
                                (feed_url, now_ts),
                            )
                            feed_state_map[feed_url] = {"consecutive_failures": 0, "next_retry_at": None}
                            # A successful connection to the domain clears the domain-level backoff.
                            if domain:
                                conn.execute(
                                    "DELETE FROM domain_failure_state WHERE domain = ?",
                                    (domain,),
                                )
                                domain_state_map.pop(domain, None)
                            self._record_fetch_history(
                                conn, feed_url, "ok",
                                new_entries=_new_entries, duration_ms=_ok_duration_ms,
                            )
                    except Exception as exc:
                        # 410 Gone: the feed is permanently removed. Disable updates
                        # immediately instead of backing off and retrying forever.
                        try:
                            _http_info = getattr(exc, 'http_info', None)
                            if _http_info and getattr(_http_info, 'status', None) == 410:
                                try:
                                    reader.disable_feed_updates(feed_url)
                                except Exception:
                                    pass
                                self._logger.info("[refresh] 410 Gone — disabled updates for %s", feed_url)
                                error_count += 1
                                with self._get_meta_connection() as conn:
                                    conn.execute(
                                        """
                                        INSERT INTO feed_failure_state (feed_url, consecutive_failures, next_retry_at, last_error, last_failure_at)
                                        VALUES (?, 1, NULL, '410 Gone: feed has been permanently removed', ?)
                                        ON CONFLICT(feed_url) DO UPDATE SET
                                            consecutive_failures = excluded.consecutive_failures,
                                            next_retry_at = NULL,
                                            last_error = excluded.last_error,
                                            last_failure_at = excluded.last_failure_at
                                        """,
                                        (feed_url, now_ts),
                                    )
                                    self._record_fetch_history(
                                        conn, feed_url, "error", http_status=410,
                                        duration_ms=int((time.perf_counter() - feed_started_at) * 1000),
                                        error="410 Gone: feed has been permanently removed",
                                    )
                                continue
                        except Exception:
                            pass

                        # Refusal escalation: if the honest UA was refused (403/415/
                        # 429/503/timeout) and this feed isn't already flagged, flag
                        # it for browser identity and retry once now. Good-citizen:
                        # only after a real refusal, never preemptively.
                        if self._on_fetch_refused is not None and self._is_fetch_refusal(exc):
                            try:
                                newly_flagged = self._on_fetch_refused(feed_url)
                            except Exception:
                                newly_flagged = False
                            if newly_flagged:
                                try:
                                    _updated = reader.update_feed(feed_url)
                                    success_count += 1
                                    _new_entries = int(getattr(_updated, "new", 0)) if _updated else 0
                                    self._logger.info(
                                        "[refresh] browser-identity retry succeeded for %s", feed_url
                                    )
                                    with self._get_meta_connection() as conn:
                                        conn.execute(
                                            """
                                            INSERT INTO feed_failure_state (feed_url, consecutive_failures, next_retry_at, last_error, last_success_at)
                                            VALUES (?, 0, NULL, NULL, ?)
                                            ON CONFLICT(feed_url) DO UPDATE SET
                                                consecutive_failures = 0,
                                                next_retry_at = NULL,
                                                last_error = NULL,
                                                last_success_at = excluded.last_success_at,
                                                acknowledged_at = NULL
                                            """,
                                            (feed_url, now_ts),
                                        )
                                        feed_state_map[feed_url] = {"consecutive_failures": 0, "next_retry_at": None}
                                        self._record_fetch_history(
                                            conn, feed_url, "ok",
                                            new_entries=_new_entries,
                                            duration_ms=int((time.perf_counter() - feed_started_at) * 1000),
                                        )
                                    continue
                                except Exception:
                                    # Retry also failed — fall through to normal
                                    # failure bookkeeping below.
                                    self._logger.info(
                                        "[refresh] browser-identity retry failed for %s", feed_url
                                    )

                        error_count += 1
                        raw_failures = feed_state.get("consecutive_failures")
                        if isinstance(raw_failures, (int, float, str)):
                            previous_failures = int(raw_failures)
                        else:
                            previous_failures = 0

                        consecutive_failures = previous_failures + 1
                        backoff_seconds = self.compute_failed_feed_backoff_seconds(consecutive_failures)
                        next_retry = now_ts + backoff_seconds
                        error_message = self.humanize_feed_exception(exc)

                        # Short write transaction: released immediately after each feed.
                        with self._get_meta_connection() as conn:
                            conn.execute(
                                """
                                INSERT INTO feed_failure_state (
                                    feed_url,
                                    consecutive_failures,
                                    next_retry_at,
                                    last_error,
                                    last_failure_at
                                )
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(feed_url) DO UPDATE SET
                                    consecutive_failures = excluded.consecutive_failures,
                                    next_retry_at = excluded.next_retry_at,
                                    last_error = excluded.last_error,
                                    last_failure_at = excluded.last_failure_at
                                """,
                                (feed_url, consecutive_failures, next_retry, error_message, now_ts),
                            )
                            feed_state_map[feed_url] = {
                                "consecutive_failures": consecutive_failures,
                                "next_retry_at": next_retry,
                            }

                            # Update domain-level backoff: use the max consecutive_failures
                            # seen from any feed on this domain so far in this cycle.
                            if domain:
                                prev_domain = domain_state_map.get(domain) or {}
                                prev_domain_failures = int(prev_domain.get("consecutive_failures") or 0)
                                domain_consecutive = max(prev_domain_failures + 1, consecutive_failures)
                                domain_backoff = self.compute_failed_feed_backoff_seconds(domain_consecutive)
                                domain_next_retry_new = now_ts + domain_backoff
                                conn.execute(
                                    """
                                    INSERT INTO domain_failure_state (domain, consecutive_failures, next_retry_at, last_failure_at)
                                    VALUES (?, ?, ?, ?)
                                    ON CONFLICT(domain) DO UPDATE SET
                                        consecutive_failures = excluded.consecutive_failures,
                                        next_retry_at = excluded.next_retry_at,
                                        last_failure_at = excluded.last_failure_at
                                    """,
                                    (domain, domain_consecutive, domain_next_retry_new, now_ts),
                                )
                                domain_state_map[domain] = {
                                    "consecutive_failures": domain_consecutive,
                                    "next_retry_at": domain_next_retry_new,
                                }
                            self._record_fetch_history(
                                conn, feed_url, "error",
                                http_status=self._http_status_of(exc),
                                duration_ms=int((time.perf_counter() - feed_started_at) * 1000),
                                error=error_message,
                            )

                        if self._refresh_debug_enabled:
                            elapsed_ms = int((time.perf_counter() - feed_started_at) * 1000)
                            self._logger.warning(
                                "[refresh] failed %d/%d in %dms: %s (%s, failures=%d, retry_in=%ds)",
                                idx,
                                len(feed_url_list),
                                elapsed_ms,
                                feed_url,
                                error_message,
                                consecutive_failures,
                                backoff_seconds,
                            )
                        continue

        for feed_url in feed_url_list:
            self._fetch_and_store_youtube_durations(feed_url)
            self._fetch_and_store_lead_images(feed_url)

        if self._refresh_debug_enabled:
            total_ms = int((time.perf_counter() - started_at) * 1000)
            self._logger.info(
                "[refresh] done: total=%d ok=%d failed=%d skipped=%d elapsed_ms=%d",
                len(feed_url_list),
                success_count,
                error_count,
                skipped_count,
                total_ms,
            )
