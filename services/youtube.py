from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx


class YouTubeDurationService:
    """Encapsulates YouTube-specific duration parsing, caching, and persistence."""

    # /embed/ is here because feeds ship it as a plain LINK, not only as an
    # iframe src: sonarsource.com/blog puts the post's video in the body as
    # <a href="https://www.youtube.com/embed/<id>?si=…">Escape from AppleScript</a>,
    # which is a watchable video the reader could not name.
    _YT_VID_PATTERN = re.compile(
        r"[?&]v=([\w-]{11})|youtu\.be/([\w-]{11})|/shorts/([\w-]{11})|/embed/([\w-]{11})"
    )

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
        # video_id -> (liveBroadcastContent, scheduledStartTime), fetched from the
        # same videos.list call as duration. "upcoming" means the video hasn't
        # premiered yet.
        self._live_cache: dict[str, tuple[str | None, str | None]] = {}
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
                "SELECT video_id, duration_seconds, duration_display,"
                " live_broadcast_content, scheduled_start_time FROM youtube_video_duration"
            ).fetchall()
        for row in rows:
            video_id = str(row["video_id"])
            self._cache[video_id] = (row["duration_seconds"], row["duration_display"])
            self._live_cache[video_id] = (row["live_broadcast_content"], row["scheduled_start_time"])

    def extract_video_id(self, link: str) -> str | None:
        match = self._YT_VID_PATTERN.search(link)
        if not match:
            return None
        return match.group(1) or match.group(2) or match.group(3) or match.group(4)

    def get_cached_duration(self, video_id: str) -> tuple[int | None, str | None]:
        cached = self._cache.get(video_id)
        if cached is not None:
            return cached

        db_value = self._get_duration_db(video_id)
        if db_value is not None:
            self._cache[video_id] = db_value
            return db_value

        return (None, None)

    def get_cached_live_status(self, video_id: str) -> tuple[str | None, str | None]:
        """Return (liveBroadcastContent, scheduledStartTime) for a video id.

        liveBroadcastContent is "upcoming" (scheduled, not yet aired), "live"
        (streaming now), or "none"/None (a normal, already-published video)."""
        cached = self._live_cache.get(video_id)
        if cached is not None:
            return cached

        db_value = self._get_live_status_db(video_id)
        if db_value is not None:
            self._live_cache[video_id] = db_value
            return db_value

        return (None, None)

    def refresh_upcoming_videos(self) -> int:
        """Re-poll every video still cached as "upcoming", regardless of which
        feed it belongs to or that feed's own refresh cadence.

        fetch_and_store_durations_for_feed only re-checks a video when its
        feed itself gets refreshed, so a channel that refreshes rarely could
        leave a premiere's status stale for hours after it actually airs.
        Meant to run on a short, fixed interval independent of any feed
        (see main.py's maintenance loop) — cheap, since there are rarely more
        than a handful of upcoming videos across a whole library at once, and
        batched the same way (up to 50 ids/call, 1 quota unit per call).

        Returns the number of video ids checked."""
        with self._get_durations_connection() as conn:
            video_ids = [
                str(row["video_id"]) for row in conn.execute(
                    "SELECT video_id FROM youtube_video_duration WHERE live_broadcast_content = 'upcoming'"
                )
            ]
        if not video_ids:
            return 0
        results = self.get_video_durations_batch(video_ids)
        if not results:
            # No API key for this tenancy context (or every id failed at
            # once, e.g. a quota error) — never overwrite existing cached
            # rows with blanks; skip this tick, a context with a real key
            # will pick it up on a later one.
            return 0
        for vid in video_ids:
            seconds, display, live_broadcast_content, scheduled_start_time = results.get(
                vid, (None, None, None, None)
            )
            self._cache[vid] = (seconds, display)
            self._live_cache[vid] = (live_broadcast_content, scheduled_start_time)
            self._upsert_duration_db(vid, seconds, display, live_broadcast_content, scheduled_start_time)
        return len(video_ids)

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
        if not results:
            # No API key for this tenancy context (or every id failed at
            # once) — never overwrite existing cached rows with blanks. This
            # matters most for an "upcoming" video: its duration is always
            # None by design, so unlike a genuinely-unfetched video it never
            # ages out of to_fetch, and without this guard a background user
            # with no key would blank its live status/scheduled time on
            # every feed refresh that touches it.
            return
        for vid in to_fetch:
            seconds, display, live_broadcast_content, scheduled_start_time = results.get(
                vid, (None, None, None, None)
            )
            self._cache[vid] = (seconds, display)
            self._live_cache[vid] = (live_broadcast_content, scheduled_start_time)
            self._upsert_duration_db(vid, seconds, display, live_broadcast_content, scheduled_start_time)

    def get_video_durations_batch(
        self, video_ids: list[str]
    ) -> dict[str, tuple[int | None, str | None, str | None, str | None]]:
        """Fetch durations + live/premiere status for many videos with videos.list
        (up to 50 ids/call, 1 quota unit per call — snippet and liveStreamingDetails
        ride along for free on the same call as contentDetails). Each value is
        (duration_seconds, duration_display, live_broadcast_content, scheduled_start_time).
        Ids the API returns no item for are absent from the result."""
        out: dict[str, tuple[int | None, str | None, str | None, str | None]] = {}
        api_key = self._api_key_provider() if self._api_key_provider else os.getenv("YOUTUBE_API_KEY")
        if not api_key:
            return out
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i + 50]
            try:
                response = httpx.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "part": "snippet,contentDetails,liveStreamingDetails",
                        "id": ",".join(chunk),
                        "key": api_key,
                    },
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
                    live_broadcast_content = (item.get("snippet") or {}).get("liveBroadcastContent")
                    scheduled_start_time = (item.get("liveStreamingDetails") or {}).get("scheduledStartTime")
                    if vid:
                        out[vid] = (
                            seconds,
                            self._format_seconds_hms(seconds),
                            live_broadcast_content,
                            scheduled_start_time,
                        )
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

    def _get_live_status_db(self, video_id: str) -> tuple[str | None, str | None] | None:
        with self._get_durations_connection() as conn:
            row = conn.execute(
                "SELECT live_broadcast_content, scheduled_start_time"
                " FROM youtube_video_duration WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        if row is None:
            return None
        return (row["live_broadcast_content"], row["scheduled_start_time"])

    def _upsert_duration_db(
        self,
        video_id: str,
        duration_seconds: int | None,
        duration_display: str | None,
        live_broadcast_content: str | None = None,
        scheduled_start_time: str | None = None,
    ) -> None:
        with self._get_durations_connection() as conn:
            conn.execute(
                """
                INSERT INTO youtube_video_duration
                    (video_id, duration_seconds, duration_display, live_broadcast_content, scheduled_start_time, fetched_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(video_id) DO UPDATE SET
                    duration_seconds = excluded.duration_seconds,
                    duration_display = excluded.duration_display,
                    live_broadcast_content = excluded.live_broadcast_content,
                    scheduled_start_time = excluded.scheduled_start_time,
                    fetched_at = excluded.fetched_at
                """,
                (video_id, duration_seconds, duration_display, live_broadcast_content, scheduled_start_time),
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
