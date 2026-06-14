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

    def __init__(
        self,
        *,
        get_meta_connection: Callable[[], sqlite3.Connection],
        get_reader: Callable[[], Any],
        user_agent: str,
        cache: dict[str, tuple[int | None, str | None]] | None = None,
        api_key_provider: Callable[[], str] | None = None,
    ) -> None:
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._user_agent = user_agent
        self._cache = cache if cache is not None else {}
        # Resolves the API key per call — in multi mode this returns the current
        # user's key (with env fallback); None falls back to the env var.
        self._api_key_provider = api_key_provider

    @property
    def cache(self) -> dict[str, tuple[int | None, str | None]]:
        return self._cache

    def warm_cache_from_db(self) -> None:
        with self._get_meta_connection() as conn:
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

        for entry in entries:
            if not entry.link:
                continue
            video_id = self.extract_video_id(entry.link)
            if not video_id:
                continue
            if video_id in self._cache:
                continue

            db_value = self._get_duration_db(video_id)
            if db_value is not None:
                self._cache[video_id] = db_value
                continue

            try:
                result = self.get_video_duration(video_id)
            except Exception:
                result = (None, None)

            self._cache[video_id] = result
            self._upsert_duration_db(video_id, result[0], result[1])

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
        api_key = (self._api_key_provider() if self._api_key_provider else "") or os.getenv("YOUTUBE_API_KEY")
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
        with self._get_meta_connection() as conn:
            row = conn.execute(
                "SELECT duration_seconds, duration_display FROM youtube_video_duration WHERE video_id = ?",
                (video_id,),
            ).fetchone()

        if row is None:
            return None
        return (row["duration_seconds"], row["duration_display"])

    def _upsert_duration_db(
        self,
        video_id: str,
        duration_seconds: int | None,
        duration_display: str | None,
    ) -> None:
        with self._get_meta_connection() as conn:
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
