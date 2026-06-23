from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx


class YouTubeDurationService:
    """Encapsulates YouTube-specific duration parsing, caching, and persistence."""

    _YT_VID_PATTERN = re.compile(r"[?&]v=([\w-]{11})|youtu\.be/([\w-]{11})|/shorts/([\w-]{11})")

    # A "no duration" result (API error/quota, or a live/upcoming stream with no
    # length yet) must NOT be cached forever — otherwise a transient failure
    # permanently blanks the [duration] title prefix. Retry such negatives after
    # this long so they self-heal once the API recovers / the stream ends.
    _NEGATIVE_RETRY_SECONDS = 6 * 3600

    def __init__(
        self,
        *,
        get_durations_connection: Callable[[], sqlite3.Connection],
        get_reader: Callable[[], Any],
        user_agent: str,
        cache: dict[str, tuple[int | None, str | None]] | None = None,
        api_key_provider: Callable[[], str] | None = None,
        quota_sink: Callable[[int], None] | None = None,
    ) -> None:
        # The duration cache (video_id -> length) is a GLOBAL store shared across
        # users, since a video's length is a fact, not per-user data.
        self._get_durations_connection = get_durations_connection
        self._get_reader = get_reader
        self._user_agent = user_agent
        self._cache = cache if cache is not None else {}
        # Resolves the API key per call — in multi mode this returns the current
        # user's key (with env fallback); None falls back to the env var.
        self._api_key_provider = api_key_provider
        # Records each videos.list call's quota cost (1 unit/call); set by the app.
        self._quota_sink = quota_sink

    @property
    def cache(self) -> dict[str, tuple[int | None, str | None]]:
        return self._cache

    def warm_cache_from_db(self) -> None:
        with self._get_durations_connection() as conn:
            rows = conn.execute(
                "SELECT video_id, duration_seconds, duration_display FROM youtube_video_duration"
            ).fetchall()
        for row in rows:
            self._cache[str(row["video_id"])] = (row["duration_seconds"], row["duration_display"])

    def extract_video_id(self, link: str) -> str | None:
        match = self._YT_VID_PATTERN.search(link)
        if not match:
            return None
        return match.group(1) or match.group(2) or match.group(3)

    def get_cached_duration(self, video_id: str) -> tuple[int | None, str | None]:
        cached = self._cache.get(video_id)
        if cached is not None:
            return cached

        db_value = self._get_duration_db(video_id)
        if db_value is not None:
            self._cache[video_id] = db_value
            return db_value

        return (None, None)

    def fetch_and_store_durations_for_feed(self, feed_url: str) -> None:
        if "youtube.com/feeds/videos.xml" not in feed_url:
            return

        try:
            with self._get_reader() as reader:
                entries = list(reader.get_entries(feed=feed_url, limit=50))
        except Exception:
            return

        # Collect the video ids that still need a fetch. videos.list bills 1 quota
        # unit PER CALL (up to 50 ids), not per video — so batching is ~50x cheaper
        # than one call per video and avoids exhausting the daily quota on large
        # subscription sets (which left ~13% of videos perpetually duration-less).
        to_fetch: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if not entry.link:
                continue
            video_id = self.extract_video_id(entry.link)
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            cached = self._cache.get(video_id)
            if cached is not None and cached[0] is not None:
                continue  # known positive in memory
            row = self._get_duration_row(video_id)
            if row is not None and row[0] is not None:
                self._cache[video_id] = (row[0], row[1])  # positive in DB
                continue
            # Absent, or a cached negative. Refetch only when there's no row yet or
            # the negative has gone stale (don't re-hit the API every refresh for
            # genuinely length-less videos).
            if row is not None and not self._negative_is_stale(row[2]):
                self._cache[video_id] = (None, None)
                continue
            to_fetch.append(video_id)

        if not to_fetch:
            return
        results = self.get_video_durations_batch(to_fetch)
        for vid in to_fetch:
            res = results.get(vid, (None, None))
            self._cache[vid] = res
            self._upsert_duration_db(vid, res[0], res[1])

    def get_video_durations_batch(self, video_ids: list[str]) -> dict[str, tuple[int | None, str | None]]:
        """Fetch durations for many videos with videos.list (up to 50 ids/call, 1
        quota unit per call). Ids the API returns no item for map to (None, None)."""
        out: dict[str, tuple[int | None, str | None]] = {}
        api_key = self._api_key_provider() if self._api_key_provider else os.getenv("YOUTUBE_API_KEY")
        if not api_key:
            return out
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i + 50]
            try:
                response = httpx.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"part": "contentDetails", "id": ",".join(chunk), "key": api_key},
                    timeout=10.0,
                )
                response.raise_for_status()
                if self._quota_sink:
                    try:
                        self._quota_sink(1)  # videos.list = 1 unit per call
                    except Exception:
                        pass
                for item in (response.json().get("items") or []):
                    vid = item.get("id")
                    duration_iso = (item.get("contentDetails") or {}).get("duration")
                    seconds = self._parse_iso8601_duration_to_seconds(duration_iso) if duration_iso else None
                    if vid:
                        out[vid] = (seconds, self._format_seconds_hms(seconds))
            except Exception:
                # A failed chunk (timeout/quota) just yields no entries for those ids;
                # they stay absent and are retried next refresh.
                continue
        return out

    def upsert_duration(
        self,
        video_id: str,
        duration_seconds: int | None,
        duration_display: str | None,
    ) -> None:
        self._cache[video_id] = (duration_seconds, duration_display)
        self._upsert_duration_db(video_id, duration_seconds, duration_display)

    def get_video_duration(self, video_id: str) -> tuple[int | None, str | None]:
        """Return (seconds, display) for a YouTube video id via the Data API.

        API-only: with no API key (per-user setting / env), durations are skipped
        entirely — we no longer scrape the watch page."""
        # Provider resolves the per-user key (with single-mode env fallback baked
        # in); only fall back to env directly when no provider was wired.
        api_key = self._api_key_provider() if self._api_key_provider else os.getenv("YOUTUBE_API_KEY")
        if not api_key:
            return None, None
        try:
            url = (
                "https://www.googleapis.com/youtube/v3/videos"
                f"?part=contentDetails&id={video_id}&key={api_key}"
            )
            response = httpx.get(url, timeout=6.0)
            response.raise_for_status()
            data = response.json()
            items = data.get("items") or []
            if items:
                content_details = items[0].get("contentDetails", {})
                duration_iso = content_details.get("duration")
                seconds = (
                    self._parse_iso8601_duration_to_seconds(duration_iso)
                    if duration_iso
                    else None
                )
                return seconds, self._format_seconds_hms(seconds)
        except Exception:
            pass
        return None, None

    def _get_duration_db(self, video_id: str) -> tuple[int | None, str | None] | None:
        with self._get_durations_connection() as conn:
            row = conn.execute(
                "SELECT duration_seconds, duration_display FROM youtube_video_duration WHERE video_id = ?",
                (video_id,),
            ).fetchone()

        if row is None:
            return None
        return (row["duration_seconds"], row["duration_display"])

    def _get_duration_row(self, video_id: str) -> tuple[int | None, str | None, str | None] | None:
        """Like ``_get_duration_db`` but also returns ``fetched_at`` so the refresh
        path can decide whether a cached negative is stale enough to retry."""
        with self._get_durations_connection() as conn:
            row = conn.execute(
                "SELECT duration_seconds, duration_display, fetched_at"
                " FROM youtube_video_duration WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        if row is None:
            return None
        return (row["duration_seconds"], row["duration_display"], row["fetched_at"])

    def _negative_is_stale(self, fetched_at: str | None) -> bool:
        """True if a cached no-duration row is old enough to retry (or unparseable)."""
        if not fetched_at:
            return True
        import datetime as _dt
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                when = _dt.datetime.strptime(fetched_at, fmt).replace(tzinfo=_dt.timezone.utc)
                break
            except ValueError:
                continue
        else:
            return True  # unparseable → allow a retry
        age = (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds()
        return age >= self._NEGATIVE_RETRY_SECONDS

    def _upsert_duration_db(
        self,
        video_id: str,
        duration_seconds: int | None,
        duration_display: str | None,
    ) -> None:
        with self._get_durations_connection() as conn:
            conn.execute(
                """
                INSERT INTO youtube_video_duration (video_id, duration_seconds, duration_display, fetched_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(video_id) DO UPDATE SET
                    duration_seconds = excluded.duration_seconds,
                    duration_display = excluded.duration_display,
                    fetched_at = excluded.fetched_at
                """,
                (video_id, duration_seconds, duration_display),
            )

    @staticmethod
    def _parse_iso8601_duration_to_seconds(duration_iso: str) -> int | None:
        try:
            match = re.match(r"P(?:T(?:(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?)", duration_iso)
            if not match:
                return None
            hours = int(match.group(1)) if match.group(1) else 0
            minutes = int(match.group(2)) if match.group(2) else 0
            seconds = int(match.group(3)) if match.group(3) else 0
            return hours * 3600 + minutes * 60 + seconds
        except Exception:
            return None

    @staticmethod
    def _format_seconds_hms(seconds: int | None) -> str | None:
        if seconds is None:
            return None
        try:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            remaining_seconds = seconds % 60
            if hours:
                return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
            return f"{minutes}:{remaining_seconds:02d}"
        except Exception:
            return None
