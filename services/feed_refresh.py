from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, cast


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
                MIN(ff.folder_id) AS folder_id
            FROM feed_failure_state s
            JOIN folder_feeds ff ON ff.feed_url = s.feed_url
            WHERE s.consecutive_failures > 0
            GROUP BY s.feed_url, s.consecutive_failures, s.next_retry_at, s.last_error, s.last_failure_at
            ORDER BY s.next_retry_at DESC, s.consecutive_failures DESC, s.feed_url ASC
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
            return "The feed server denied access (HTTP 403 Forbidden)."
        if "404" in lowered or "not found" in lowered:
            return "The feed URL returned not found (HTTP 404)."
        if "401" in lowered or "unauthorized" in lowered:
            return "The feed requires authentication (HTTP 401 Unauthorized)."
        if "429" in lowered or "too many requests" in lowered:
            return "The feed server is rate-limiting requests (HTTP 429)."
        if "parseerror" in lowered or "xml" in lowered or "invalid" in lowered:
            return "The feed response could not be parsed as a valid RSS/Atom document."

        if len(detail) > 260:
            return f"{detail[:257]}..."
        return detail or "An unknown feed retrieval error occurred."

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

        feed_state_map: dict[str, dict[str, object]] = {}
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

            with self._get_reader() as reader:
                for idx, feed_url in enumerate(feed_url_list, start=1):
                    feed_started_at = time.perf_counter()
                    feed_state = feed_state_map.get(feed_url) or {}
                    next_retry_at = cast(float | None, feed_state.get("next_retry_at"))
                    if next_retry_at is not None and next_retry_at > now_ts:
                        skipped_count += 1
                        if self._refresh_debug_enabled:
                            retry_in_seconds = int(max(1, next_retry_at - now_ts))
                            self._logger.info(
                                "[refresh] skipping %d/%d for %ds backoff: %s",
                                idx,
                                len(feed_url_list),
                                retry_in_seconds,
                                feed_url,
                            )
                        continue

                    try:
                        if self._refresh_debug_enabled:
                            self._logger.info("[refresh] updating %d/%d: %s", idx, len(feed_url_list), feed_url)
                        reader.update_feed(feed_url)
                        success_count += 1
                        if self._refresh_debug_enabled:
                            elapsed_ms = int((time.perf_counter() - feed_started_at) * 1000)
                            self._logger.info(
                                "[refresh] updated %d/%d in %dms: %s",
                                idx,
                                len(feed_url_list),
                                elapsed_ms,
                                feed_url,
                            )

                        conn.execute(
                            """
                            INSERT INTO feed_failure_state (feed_url, consecutive_failures, next_retry_at, last_error, last_success_at)
                            VALUES (?, 0, NULL, NULL, ?)
                            ON CONFLICT(feed_url) DO UPDATE SET
                                consecutive_failures = 0,
                                next_retry_at = NULL,
                                last_error = NULL,
                                last_success_at = excluded.last_success_at
                            """,
                            (feed_url, now_ts),
                        )
                        feed_state_map[feed_url] = {
                            "consecutive_failures": 0,
                            "next_retry_at": None,
                        }
                    except Exception as exc:
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
