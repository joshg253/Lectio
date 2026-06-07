from __future__ import annotations

import hashlib
import html
import io
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Sequence, cast
from urllib.parse import quote, quote_plus, urlparse

import feedparser
import httpx


def _parse_month_first_pubdate(date_string: str):
    # Handles non-RFC-2822 dates like "May 11, 2026 19:15:50 +0000" (no day-of-week).
    try:
        dt = datetime.strptime(date_string.strip(), "%B %d, %Y %H:%M:%S %z")
        return dt.utctimetuple()
    except ValueError:
        return None


feedparser.registerDateHandler(_parse_month_first_pubdate)
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image as _PILImage
from readability import Document
from reader.exceptions import InvalidFeedURLError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from services import scraper_service
from services import takeout_service
from services.email import send_article_email, send_digest_email
from services.feed_discovery import discover_feed_urls
from services.feed_refresh import FeedRefreshService
from services.lead_images import LeadImageService
from services.reader_api import ReaderApi
from services.starred_archive import StarredArchiveService
from services.youtube import YouTubeDurationService
from services.youtube_sync import sync_youtube_folder

BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("uvicorn.error")


class _ReaderNonFatalParseWarningFilter(logging.Filter):
    """Filter known non-fatal feed parsing warnings from `reader` logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if not message.startswith("parse "):
            return True

        suppressed_markers = (
            "got NonXMLContentType(",
            "got CharacterEncodingOverride(",
        )
        return not any(marker in message for marker in suppressed_markers)


def _configure_reader_logging() -> None:
    if os.getenv("LECTIO_SUPPRESS_READER_PARSE_WARNINGS", "1") != "1":
        return
    logging.getLogger("reader").addFilter(_ReaderNonFatalParseWarningFilter())


def _configure_persistent_logging() -> None:
    """Attach a rotating file handler when LECTIO_LOG_DIR is set.

    Defaults: 5 MB per file, 5 backups (LECTIO_LOG_MAX_BYTES, LECTIO_LOG_BACKUPS).
    Stdout logging is left untouched so local dev (and uvicorn defaults) keep
    working. Without LECTIO_LOG_DIR the app behaves exactly as before.
    """
    log_dir_str = os.getenv("LECTIO_LOG_DIR", "").strip()
    if not log_dir_str:
        return
    from logging.handlers import RotatingFileHandler

    log_dir = Path(log_dir_str)
    log_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = int(os.getenv("LECTIO_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    backup_count = int(os.getenv("LECTIO_LOG_BACKUPS", "5"))
    handler = RotatingFileHandler(
        log_dir / "lectio.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # Attach to the root logger so it captures everything (uvicorn, reader, app).
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(handler)


def _configure_access_log_filter() -> None:
    # Kept for backward-compat with previous wiring; the actual access log is
    # now emitted by _AccessLogMiddleware (uvicorn's --no-access-log disables
    # the built-in one whose filters we couldn't reliably hook).
    pass


def _attach_pending_access_filter() -> None:
    pass


_configure_reader_logging()
_configure_persistent_logging()
_configure_access_log_filter()


def _load_local_env(env_path: str | Path | None = None) -> None:
    """Load simple KEY=VALUE pairs from a .env file into os.environ if not already set.

    This is intentionally minimal (no interpolation) and avoids adding a dependency.
    """
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(BASE_DIR / ".env")
    candidates.append(Path.cwd() / ".env")
    for p in candidates:
        try:
            if not p or not p.exists():
                continue
            with p.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    # Do not overwrite existing environment vars
                    if key and os.getenv(key) is None:
                        os.environ[key] = val
            break
        except Exception:
            continue


# Load local .env early so os.getenv() below can pick up values provided by the developer
_load_local_env()
DATA_DIR = Path(os.getenv("LECTIO_DATA_DIR", str(BASE_DIR))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
META_DB_PATH = DATA_DIR / "lectio_meta.sqlite3"
READER_DB_PATH = DATA_DIR / "lectio_reader.sqlite"
THUMB_DB_PATH = DATA_DIR / "lectio_thumb_cache.sqlite"
STARRED_ARCHIVE_DB_PATH = DATA_DIR / "lectio_starred_archive.sqlite"
THUMB_CACHE_DIR = DATA_DIR / "thumb_cache"  # legacy on-disk cache; entries migrate lazily on access
ROOT_FOLDER_NAME = "All Feeds"
_LECTIO_FOLDER_NAME = "_Lectio"

scraper_service.init(DATA_DIR)
DEFAULT_AUTO_REFRESH_MINUTES = 60
MIN_AUTO_REFRESH_MINUTES = 15
MANUAL_REFRESH_COOLDOWN_SECONDS = 60
FAILED_FEED_BACKOFF_BASE_SECONDS = 60
FAILED_FEED_BACKOFF_MAX_SECONDS = 60 * 60 * 24
AUTO_REFRESH_SETTING_KEY = "auto_refresh_minutes"
SORT_BY_SETTING_KEY = "sort_by"
SORT_DIR_SETTING_KEY = "sort_dir"
GLOBAL_NOTE_SETTING_KEY = "global_note"
PROBLEMATIC_FEEDS_LAST_VIEWED_AT_SETTING_KEY = "problematic_feeds_last_viewed_at"
YOUTUBE_SYNC_LAST_AT_KEY = "youtube_sync_last_at"
YOUTUBE_SYNC_LAST_RESULT_KEY = "youtube_sync_last_result"
EMAIL_TO_SETTING_KEY = "email_to"
EMAIL_BCC_SETTING_KEY = "email_bcc_address"
PROFILE_NAME_SETTING_KEY = "profile_name"
PROFILE_EMAIL_SETTING_KEY = "profile_email"
SETTING_TZ_DISPLAY = "tz_display"
SETTING_MAINTENANCE_HOUR = "maintenance_hour"
SETTING_YT_API_KEY = "yt_api_key"
SETTING_YT_CHANNEL_ID = "yt_channel_id"
SETTING_YT_FOLDER_NAME = "yt_folder_name"
SETTING_RESEND_API_KEY = "resend_api_key"
SETTING_EMAIL_FROM = "email_from"
SETTING_INSTAPAPER_USERNAME = "instapaper_username"
SETTING_INSTAPAPER_PASSWORD = "instapaper_password"
AUTO_REFRESH_OPTION_MINUTES = (0, 15, 30, 60, 360, 720)
SCHEDULER_POLL_SECONDS = 30
DEFAULT_SORT_BY = "post"
DEFAULT_SORT_DIR = "asc"
CHUNK_SIZE = 10
READABILITY_USER_AGENT = "Lectio/0.1 (+https://localhost)"
# In-memory cache of domains known to have Cross-Origin-Resource-Policy restrictions.
# Values: True = same-site/same-origin (proxy needed), False = no restriction.
_CORP_DOMAIN_CACHE: dict[str, bool] = {}
MANUAL_TAG_KEY_PREFIX = "lectio.manual_tag."
MAX_MANUAL_TAGS = 12
MAX_FEED_TAG_SUGGESTIONS = 8
FEED_TAG_SUGGESTION_CACHE_TTL_SECONDS = 900
TAG_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.#+][A-Za-z0-9_.#+-]{0,31}$")
STATIC_ASSET_VERSION = os.getenv("LECTIO_ASSET_VERSION", "20260527t")
REFRESH_DEBUG_ENABLED = os.getenv("LECTIO_REFRESH_DEBUG", "0") == "1"
DEBUG_MODE = os.getenv("LECTIO_DEBUG", "0") == "1"

# --- Email (Resend) config — env vars are fallbacks; DB settings take precedence at runtime ---
_ENV_RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
_ENV_RESEND_FROM = os.getenv("LECTIO_EMAIL_FROM", "").strip()
# LECTIO_EMAIL_TO removed — use profile email / Contacts instead


def get_resend_api_key() -> str:
    return get_runtime_setting(SETTING_RESEND_API_KEY, _ENV_RESEND_API_KEY)


def get_resend_from() -> str:
    return get_runtime_setting(SETTING_EMAIL_FROM, _ENV_RESEND_FROM)


def is_email_configured() -> bool:
    return bool(get_resend_api_key() and get_resend_from())


def is_instapaper_configured() -> bool:
    return bool(
        get_runtime_setting(SETTING_INSTAPAPER_USERNAME)
        and get_runtime_setting(SETTING_INSTAPAPER_PASSWORD)
    )


# --- YouTube subscription sync config — env vars are fallbacks; DB settings take precedence ---
_ENV_YT_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
_ENV_YT_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
_ENV_YT_FOLDER_NAME = (os.getenv("YOUTUBE_FOLDER_NAME", "").strip() or "YouTube Subscriptions")
_yt_sync_hour_raw = os.getenv("YOUTUBE_SYNC_HOUR", "").strip()
_ENV_MAINTENANCE_HOUR: int | None = None
if _yt_sync_hour_raw:
    try:
        _h = int(_yt_sync_hour_raw)
        if 0 <= _h <= 23:
            _ENV_MAINTENANCE_HOUR = _h
    except ValueError:
        pass


def get_yt_api_key() -> str:
    return get_runtime_setting(SETTING_YT_API_KEY, _ENV_YT_API_KEY)


def get_yt_channel_id() -> str:
    return get_runtime_setting(SETTING_YT_CHANNEL_ID, _ENV_YT_CHANNEL_ID)


def get_yt_folder_name() -> str:
    return get_runtime_setting(SETTING_YT_FOLDER_NAME, _ENV_YT_FOLDER_NAME) or "YouTube Subscriptions"


def get_maintenance_hour() -> int | None:
    val = get_runtime_setting(SETTING_MAINTENANCE_HOUR, "")
    if val:
        try:
            h = int(val)
            if 0 <= h <= 23:
                return h
        except ValueError:
            pass
    return _ENV_MAINTENANCE_HOUR

# --- Auth config ---
# Set LECTIO_USERNAME and LECTIO_PASSWORD to enable authentication.
# If either is absent, auth is disabled (safe for local-only use).
# LECTIO_SECRET_KEY is used to sign session cookies; generate one with:
#   python -c "import secrets; print(secrets.token_hex(32))"
# If not set a random key is generated at startup (sessions won't survive restarts).
AUTH_USERNAME = os.getenv("LECTIO_USERNAME", "")
AUTH_PASSWORD = os.getenv("LECTIO_PASSWORD", "")
AUTH_ENABLED = bool(AUTH_USERNAME and AUTH_PASSWORD)
SESSION_SECRET_KEY = os.getenv("LECTIO_SECRET_KEY") or secrets.token_hex(32)
if AUTH_ENABLED and not os.getenv("LECTIO_SECRET_KEY"):
    LOGGER.warning(
        "LECTIO_SECRET_KEY is not set — using a random key. Sessions will not survive server restarts. Set a stable key in your .env."
    )
# Cookie lifetime: 1 year. Changing this requires users to log in again.
SESSION_MAX_AGE_SECONDS = int(os.getenv("LECTIO_SESSION_MAX_AGE", str(365 * 24 * 3600)))
# Set LECTIO_HTTPS_ONLY=1 when running behind a TLS-terminating reverse proxy.
_HTTPS_ONLY = os.getenv("LECTIO_HTTPS_ONLY", "0") == "1"
# Paths that are always public (no login required)
_AUTH_EXEMPT_PREFIXES = ("/login", "/static", "/healthz", "/api/img", "/dev/feeds/")

_configured_refresh_minutes = int(os.getenv("LECTIO_AUTO_REFRESH_MINUTES", str(DEFAULT_AUTO_REFRESH_MINUTES)))
AUTO_REFRESH_MINUTES = 0 if _configured_refresh_minutes <= 0 else max(_configured_refresh_minutes, MIN_AUTO_REFRESH_MINUTES)
manual_refresh_lock = threading.Lock()
last_manual_refresh_started_at = 0.0
updating_feeds_lock = threading.Lock()
updating_feeds: set[str] = set()
feed_tag_suggestion_cache_lock = threading.Lock()
feed_tag_suggestion_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}
_feed_tag_fetch_in_progress: set[str] = set()
# Short in-memory TTL cache for tag counts to avoid repeatedly scanning
# reader entries on every request. Small TTL keeps counts fresh while
# preventing repeated expensive work during rapid navigation.
TAG_COUNTS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_TAG_COUNTS_CACHE_TTL", "300"))
tag_counts_cache_lock = threading.Lock()
tag_counts_cache: dict[tuple[str, ...], tuple[float, list[dict[str, int | str]]]] = {}

# Short in-memory TTL cache for unread counts so the UI doesn't scan the
# entire reader DB on every load. TTL is small to stay responsive to new
# incoming posts.
UNREAD_COUNTS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_UNREAD_COUNTS_CACHE_TTL", "300"))
unread_counts_cache_lock = threading.Lock()
unread_counts_cache: dict[str, tuple[float, dict[str, int]]] = {}
# Stale-while-revalidate: when the cache is stale we serve the prior value and
# spawn ONE background refresh. Concurrent renders never wait on the scan.
unread_counts_compute_lock = threading.Lock()
unread_counts_refresh_inflight = False
# Incremented on every invalidation so in-flight background refreshes that
# started before the invalidation don't write stale counts back to the cache.
_unread_counts_generation: int = 0
# Feed-title map: hits the reader DB to enumerate every feed. Cache it — feed
# titles barely change between page renders.
FEED_TITLE_MAP_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_FEED_TITLE_MAP_CACHE_TTL", "300"))
feed_title_map_cache_lock = threading.Lock()
feed_title_map_cache: dict[str, tuple[float, dict[str, str]]] = {}

# Cache the meta-DB structure snapshot. Folders / folder_feeds change only on
# explicit user actions (subscribe, unsubscribe, add/delete folder, move feed),
# so we cache the read-side queries indefinitely and invalidate on mutation.
# This collapses ~5 SQL roundtrips per home render to one dict lookup.
_meta_structure_lock = threading.Lock()
_meta_structure_cache: dict[str, object] = {}


def invalidate_meta_structure_cache() -> None:
    with _meta_structure_lock:
        _meta_structure_cache.clear()


# Cache for problematic-feeds list. Only changes when a refresh succeeds/fails,
# so a TTL is fine — we don't need exact freshness on the home page.
PROBLEMATIC_FEEDS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_PROBLEMATIC_FEEDS_CACHE_TTL", "60"))
_problematic_feeds_cache_lock = threading.Lock()
_problematic_feeds_cache: dict[int, tuple[float, list[dict[str, object]]]] = {}


def invalidate_problematic_feeds_cache() -> None:
    with _problematic_feeds_cache_lock:
        _problematic_feeds_cache.clear()


def is_async_action_request(request: Request, expected_header: str | None = None) -> bool:
    requested_with = (request.headers.get("X-Requested-With") or "").strip()
    if not requested_with:
        return False
    if expected_header is None:
        return requested_with.startswith("lectio-")
    return requested_with == expected_header


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _attach_pending_access_filter()
    ensure_meta_schema()
    ensure_thumb_schema()
    ensure_starred_archive_schema()
    with get_meta_connection() as conn:
        purge_lower_level_folders(conn)
        app.state.auto_refresh_minutes = get_auto_refresh_minutes(conn)
        # Pre-load settings cache so get_cached_setting works before first request.
        global _app_settings_cache
        with _app_settings_cache_lock:
            _app_settings_cache = _load_app_settings_cache(conn)
    app.state.last_scheduled_refresh_started_at = time.monotonic()

    # Ensure reader db is created at startup.
    with get_reader():
        pass

    # Warm YouTube duration in-memory cache from DB so first renders are instant.
    youtube_duration_service.warm_cache_from_db()

    # Warm lead image cache from DB so thumbnails are available on first render.
    lead_image_service.warm_cache_from_db()

    # Artwork tagger runs first so webcomic tagger won't clobber ArtStation feeds
    # that live in folders whose name also contains "comic".
    _auto_tag_artwork_feeds()
    # Auto-tag feeds in "comic*" folders with strategy='webcomic'.
    _auto_tag_webcomic_feeds()

    # Kill switch for heavy startup work. Set LECTIO_DISABLE_STARTUP_BACKFILL=1
    # to skip the scheduled-refresh loop and the lead-image / YouTube backfills.
    # Useful after an OPML import of hundreds of feeds: boot in a calm state,
    # let pages render, then unset and restart once things settle.
    backfill_disabled = os.getenv("LECTIO_DISABLE_STARTUP_BACKFILL", "0") == "1"
    if backfill_disabled:
        LOGGER.warning("LECTIO_DISABLE_STARTUP_BACKFILL=1 — skipping scheduled refresh and backfill threads")

    stop_event = threading.Event()
    if not backfill_disabled:
        thread = threading.Thread(
            target=scheduled_refresh_loop,
            args=(stop_event,),
            daemon=True,
        )
        app.state.refresh_stop_event = stop_event
        app.state.refresh_thread = thread
        thread.start()
    else:
        app.state.refresh_stop_event = stop_event
        app.state.refresh_thread = None

    # Backfill durations for any existing YouTube entries not yet stored.
    def _backfill() -> None:
        with get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT feed_url FROM folder_feeds WHERE feed_url LIKE '%youtube.com/feeds/videos.xml%'"
            ).fetchall()
        for row in rows:
            youtube_duration_service.fetch_and_store_durations_for_feed(str(row["feed_url"]))

    if not backfill_disabled:
        threading.Thread(target=_backfill, daemon=True).start()

    # Backfill lead images for all feeds whose entries haven't been checked yet.
    # `force_retry_negative=False` keeps this incremental: only feeds that have
    # never been checked do work, so a startup right after an OPML import of
    # hundreds of feeds doesn't flood the meta DB with writes and starve the
    # request path.
    def _backfill_lead_images() -> None:
        with get_meta_connection() as conn:
            rows = conn.execute("SELECT DISTINCT feed_url FROM folder_feeds").fetchall()
        for row in rows:
            lead_image_service.fetch_and_store_lead_images_for_feed(str(row["feed_url"]), force_retry_negative=False)

    if not backfill_disabled:
        threading.Thread(target=_backfill_lead_images, daemon=True).start()

    # Start the starred archive worker, then backfill pending rows for any
    # saved entries that don't yet have a complete archive (covers the
    # initial rollout and any re-stars after maintenance pruning). Also
    # one-shot metadata backfill for archive rows that completed before
    # title/link/etc columns were added to the schema.
    starred_archive_service.start_worker()

    def _archive_backfill_task() -> None:
        starred_archive_service.backfill_saved_entries_from_archive()
        starred_archive_service.backfill_missing_archives()
        starred_archive_service.backfill_metadata_for_complete_rows()

    threading.Thread(
        target=_archive_backfill_task,
        daemon=True,
        name="starred-archive-backfill",
    ).start()

    # One-time backfill: populate read_history from entry_read_state if history is empty.
    def _backfill_read_history() -> None:
        try:
            with get_meta_connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM read_history").fetchone()[0]
                if count > 0:
                    return
                rows = conn.execute(
                    "SELECT feed_url, entry_id, read_at FROM entry_read_state"
                    " ORDER BY read_at DESC LIMIT ?",
                    (READ_HISTORY_CAP,),
                ).fetchall()
            if not rows:
                return
            needed = {(str(r["feed_url"]), str(r["entry_id"])): str(r["read_at"]) for r in rows}
            to_insert = []
            with get_reader() as reader:
                for (feed_url, entry_id), read_at in needed.items():
                    entry = reader.get_entry((feed_url, entry_id), None)
                    if entry is None:
                        continue
                    feed = reader.get_feed(feed_url, None)
                    to_insert.append((
                        feed_url,
                        entry_id,
                        str(getattr(entry, "title", None) or ""),
                        str(getattr(entry, "link", None) or ""),
                        str(getattr(feed, "title", None) or ""),
                        read_at,
                    ))
            if not to_insert:
                return
            with get_meta_connection() as conn:
                conn.executemany(
                    "INSERT INTO read_history (feed_url, entry_id, title, link, feed_title, read_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(feed_url, entry_id) DO NOTHING",
                    to_insert,
                )
            LOGGER.info("[read_history] backfilled %d entries from entry_read_state", len(to_insert))
        except Exception:
            LOGGER.exception("[read_history] backfill error")

    threading.Thread(target=_backfill_read_history, daemon=True, name="read-history-backfill").start()

    # Daily Maintenance loop — runs once per day at the configured maintenance hour.
    maint_stop_event = threading.Event()
    app.state.maint_stop_event = maint_stop_event
    threading.Thread(
        target=_daily_maintenance_loop,
        args=(maint_stop_event,),
        daemon=True,
        name="daily-maintenance",
    ).start()

    # In debug mode, auto-subscribe dev feeds to the _Lectio folder.
    if DEBUG_MODE:
        _dev_base = "http://127.0.0.1:8000"
        _dev_urls = [
            f"{_dev_base}/dev/feeds/email-match.xml",
            f"{_dev_base}/dev/feeds/email-match.atom",
            f"{_dev_base}/dev/feeds/email-match.json",
            f"{_dev_base}/dev/feeds/email-skip.xml",
            f"{_dev_base}/dev/feeds/email-skip.atom",
            f"{_dev_base}/dev/feeds/email-skip.json",
        ]
        with get_meta_connection() as conn:
            _lectio_folder_id = _get_lectio_folder_id(conn)
        if _lectio_folder_id:
            _dev_newly_added: list[str] = []
            for _dev_url in _dev_urls:
                try:
                    with get_meta_connection() as _c:
                        _already = _c.execute(
                            "SELECT 1 FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                            (_lectio_folder_id, _dev_url),
                        ).fetchone()
                    if not _already:
                        add_feed_to_folder(_dev_url, _lectio_folder_id)
                        _dev_newly_added.append(_dev_url)
                except Exception:
                    LOGGER.exception("[dev-feeds] failed to subscribe %s", _dev_url)
            if _dev_newly_added:
                feed_refresh_service.update_feeds(_dev_newly_added)

    try:
        yield
    finally:
        stop_event = getattr(app.state, "refresh_stop_event", None)
        thread = getattr(app.state, "refresh_thread", None)
        if stop_event and thread:
            stop_event.set()
            # A scheduled refresh in flight can take tens of seconds (one HTTP
            # timeout per feed). Wait long enough to let it finish cleanly so
            # we don't leave the meta DB in a partial-write state.
            shutdown_timeout = float(os.getenv("LECTIO_SHUTDOWN_TIMEOUT_SECONDS", "30"))
            thread.join(timeout=shutdown_timeout)
            if thread.is_alive():
                LOGGER.warning(
                    "[shutdown] refresh worker did not finish within %.0fs; "
                    "abandoning (daemon thread will be killed by interpreter exit)",
                    shutdown_timeout,
                )
        # Stop the starred archive worker — short timeout, since the only
        # work in flight is HTTP fetches with 15s ceilings.
        try:
            starred_archive_service.stop_worker(timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[shutdown] starred archive worker stop failed: %s", exc)
        yt_stop = getattr(app.state, "yt_stop_event", None)
        if yt_stop:
            yt_stop.set()


app = FastAPI(title="Lectio", lifespan=lifespan)


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)
        if request.session.get("authenticated"):
            return await call_next(request)
        next_url = str(request.url)
        return RedirectResponse(url=f"/login?next={quote_plus(next_url)}", status_code=303)


# Paths exempt from CSRF validation. /login is the auth gate itself (rate-
# limited separately). /static and /healthz are GET-only anyway, but listing
# explicitly documents intent.
_CSRF_EXEMPT_PREFIXES = ("/login", "/static", "/healthz", "/api/img")
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CSRF_SESSION_KEY = "csrf_token"
_CSRF_HEADER_NAME = "x-csrf-token"
_CSRF_FORM_FIELD = "_csrf"


def _ensure_csrf_token(session: dict) -> str:
    """Return the session's CSRF token, generating one on first use.

    Token is per-session (rotated whenever the session itself rotates, e.g.
    on logout/login). Stored in the session cookie which is HttpOnly + signed
    so attackers on other origins can neither read nor forge it.
    """
    token = session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


_home_request_semaphore = threading.Semaphore(int(os.getenv("LECTIO_MAX_CONCURRENT_HOME_REQUESTS", "4")))
_prefetch_header_log_remaining = [10]  # diagnostic: log headers of the first N suspect requests
_prefetch_header_log_lock = threading.Lock()


class _AccessLogMiddleware:
    """Replaces uvicorn's built-in access log so we can suppress the prefetch
    204/503s without wrestling with uvicorn's logger config (filters added to
    uvicorn.access weren't taking effect in this deployment).

    Format mirrors uvicorn's: ``CLIENT - "METHOD PATH HTTP/x.y" STATUS REASON``.
    """

    _SUPPRESS_STATUSES = (204, 503)
    _STATUS_REASONS = {
        200: "OK",
        204: "No Content",
        301: "Moved Permanently",
        302: "Found",
        303: "See Other",
        304: "Not Modified",
        307: "Temporary Redirect",
        308: "Permanent Redirect",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        413: "Payload Too Large",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_holder = [0]

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status_holder[0] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            status = status_holder[0]
            client = scope.get("client") or ("?", 0)
            client_addr = f"{client[0]}:{client[1]}"
            method = scope.get("method", "?")
            raw_path = scope.get("raw_path", b"") or scope.get("path", "").encode()
            qs = scope.get("query_string", b"") or b""
            request_path = raw_path.decode("latin-1", errors="replace")
            if qs:
                request_path = f"{request_path}?{qs.decode('latin-1', errors='replace')}"
            http_version = scope.get("http_version", "1.1")
            request_line = f"{method} {request_path} HTTP/{http_version}"
            # Suppression: prefetch 204/503 with list_feed_url= are pure noise.
            if not (status in self._SUPPRESS_STATUSES and "list_feed_url=" in request_path):
                reason = self._STATUS_REASONS.get(status, "")
                LOGGER.info('%s - "%s" %d %s', client_addr, request_line, status, reason)


class _RejectPrefetchMiddleware:
    """Two-layer defense against browser link-prefetch floods.

    1. **Drop prefetches outright.** Browsers tag them with one of several
       headers (Sec-Purpose, Purpose, X-Moz, Sec-Fetch-Dest=empty for nav-style
       prefetch). Match generously — false positives just make a prefetch miss,
       which is fine.
    2. **Concurrency cap on home() requests.** Backstop in case detection
       misses a browser variant: hold a semaphore so only N home renders run
       at once. Excess returns 503, which tells the prefetcher to stop. /healthz
       and /static bypass both layers so probes and asset prefetches still work.
    """

    PASSTHROUGH_PREFIXES = ("/healthz", "/static", "/login", "/api/img")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(path.startswith(p) for p in self.PASSTHROUGH_PREFIXES):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        is_prefetch = False
        is_spa_fetch = False
        sec_fetch_dest = b""
        sec_fetch_mode = b""
        sec_purpose_value = b""
        purpose_value = b""
        x_requested_with = b""
        user_agent = b""
        referer = b""
        for header_name, header_value in headers:
            name = header_name.decode("latin-1").lower()
            if name == "sec-purpose":
                sec_purpose_value = header_value
                if b"prefetch" in header_value.lower() or b"prerender" in header_value.lower():
                    is_prefetch = True
            elif name == "purpose":
                purpose_value = header_value
                low = header_value.lower()
                if b"prefetch" in low or b"preview" in low or b"prerender" in low:
                    is_prefetch = True
            elif name == "x-moz":
                if b"prefetch" in header_value.lower() or b"prerender" in header_value.lower():
                    is_prefetch = True
            elif name == "sec-fetch-dest":
                sec_fetch_dest = header_value
            elif name == "sec-fetch-mode":
                sec_fetch_mode = header_value
            elif name == "x-requested-with":
                x_requested_with = header_value
                if b"lectio" in header_value.lower():
                    is_spa_fetch = True
            elif name == "user-agent":
                user_agent = header_value
            elif name == "referer":
                referer = header_value
        # Heuristic: browser-side prefetch via fetch() (Chrome NoState Prefetch
        # and similar) doesn't always set Sec-Purpose, but it has a unique
        # fingerprint — Sec-Fetch-Dest=empty + Sec-Fetch-Mode=cors on a path
        # that isn't an actual API endpoint, with no X-Requested-With from our
        # SPA. Real SPA fetches always send X-Requested-With=lectio-...; real
        # top-level navigations send Sec-Fetch-Dest=document Sec-Fetch-Mode=navigate.
        if (
            not is_prefetch
            and not is_spa_fetch
            and sec_fetch_dest == b"empty"
            and sec_fetch_mode == b"cors"
            and path == "/"
        ):
            is_prefetch = True

        # Diagnostic: dump the first few suspect-looking requests so we can
        # see what's actually firing them. Capped to avoid log spam.
        with _prefetch_header_log_lock:
            remaining = _prefetch_header_log_remaining[0]
        if remaining > 0:
            with _prefetch_header_log_lock:
                if _prefetch_header_log_remaining[0] > 0:
                    _prefetch_header_log_remaining[0] -= 1
                    LOGGER.info(
                        "[prefetch-diag] path=%s qs=%r dest=%r mode=%r xrw=%r referer=%r ua=%r",
                        path,
                        scope.get("query_string", b"")[:200],
                        sec_fetch_dest,
                        sec_fetch_mode,
                        x_requested_with,
                        referer[:120],
                        user_agent[:120],
                    )

        if is_prefetch:
            response = Response(status_code=204, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return

        # Backstop: concurrency cap. Non-blocking acquire — if N home renders
        # are already in flight, drop the request with 503 + Retry-After.
        if path == "/" or path.startswith("/?"):
            acquired = _home_request_semaphore.acquire(blocking=False)
            if not acquired:
                response = Response(
                    status_code=503,
                    headers={"Retry-After": "2", "Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            try:
                await self.app(scope, receive, send)
            finally:
                _home_request_semaphore.release()
            return

        await self.app(scope, receive, send)


class _CSRFMiddleware:
    """Pure-ASGI CSRF protection.

    On unsafe methods (POST/PUT/PATCH/DELETE) requires a token that matches
    the session's stored token. Token is supplied either via:
      - X-CSRF-Token header (used by SPA fetch handlers), or
      - `_csrf` form field (for plain HTML form submits / multipart uploads).

    Read-only methods are allowed through unchanged. The body is buffered and
    re-streamed to downstream handlers via a replay receive callable so route
    handlers can still parse it normally.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Ensure every request has a CSRF token in its session so templates
        # can render it and SPA fetches can read it from a meta tag. The
        # token is created lazily on first request after a fresh session.
        session = scope.get("session")
        if session is not None and not session.get(_CSRF_SESSION_KEY):
            session[_CSRF_SESSION_KEY] = secrets.token_urlsafe(32)

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        if method in _CSRF_SAFE_METHODS or any(path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Buffer the request body so downstream handlers can still read it.
        body_chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                # Unexpected (e.g. http.disconnect); pass through unchanged.
                await self.app(scope, receive, send)
                return
            body_chunks.append(message.get("body", b""))
            more_body = bool(message.get("more_body", False))
        body = b"".join(body_chunks)

        # Token from header (preferred for SPA fetches).
        submitted: str | None = None
        for header_name, header_value in scope.get("headers") or []:
            if header_name.decode("latin-1").lower() == _CSRF_HEADER_NAME:
                submitted = header_value.decode("latin-1")
                break

        # Fall back to a `_csrf` field in form-encoded bodies.
        if not submitted and body:
            content_type = b""
            for header_name, header_value in scope.get("headers") or []:
                if header_name.decode("latin-1").lower() == "content-type":
                    content_type = header_value
                    break
            ctype = content_type.decode("latin-1", errors="ignore").lower()
            if ctype.startswith("application/x-www-form-urlencoded"):
                from urllib.parse import parse_qs

                try:
                    form = parse_qs(body.decode("utf-8", errors="ignore"))
                    values = form.get(_CSRF_FORM_FIELD, [])
                    if values:
                        submitted = values[0]
                except Exception:
                    submitted = None
            elif ctype.startswith("multipart/form-data"):
                # Cheap multipart probe: scan the raw body for the field name.
                # Avoids pulling in a streaming parser for the one OPML form.
                marker = b'name="' + _CSRF_FORM_FIELD.encode() + b'"'
                idx = body.find(marker)
                if idx >= 0:
                    # The value sits two CRLFs after the disposition line.
                    sep = b"\r\n\r\n"
                    val_start = body.find(sep, idx)
                    if val_start >= 0:
                        val_start += len(sep)
                        val_end = body.find(b"\r\n", val_start)
                        if val_end >= 0:
                            try:
                                submitted = body[val_start:val_end].decode("utf-8", errors="ignore")
                            except Exception:
                                submitted = None

        expected = (scope.get("session") or {}).get(_CSRF_SESSION_KEY)
        if not expected or not submitted or not secrets.compare_digest(str(submitted), str(expected)):
            response = JSONResponse(
                {"detail": "CSRF token missing or invalid."},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        # Replay the buffered body to downstream handlers.
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


# Middleware order matters. Starlette's add_middleware does insert(0) and the
# stack-builder reverses, so LAST add_middleware = OUTERMOST = runs FIRST
# inbound. We need this inbound chain:
#   _RejectPrefetchMiddleware (drop browser speculation-rules prefetches)
#   ProxyHeaders (rewrite client IP / scheme from X-Forwarded-*)
#   → SessionMiddleware (parse signed session cookie → scope["session"])
#   → _CSRFMiddleware (validate POST tokens; needs scope["session"])
#   → _AuthMiddleware (gate on session["authenticated"]; needs scope["session"])
#   → app
# Therefore add Auth FIRST (innermost) and outer middlewares LAST.
app.add_middleware(_AuthMiddleware)
app.add_middleware(_CSRFMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    max_age=SESSION_MAX_AGE_SECONDS,
    https_only=_HTTPS_ONLY,
    same_site="lax",
)
# When running behind Traefik/nginx/Caddy, trust forwarded headers so
# uvicorn sees the real scheme and client IP.
if _HTTPS_ONLY:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
# Drop prefetch traffic before any other middleware does work.
app.add_middleware(_RejectPrefetchMiddleware)
# Access log replaces uvicorn's built-in (disabled via --no-access-log) so we
# can suppress prefetch 204/503 noise. Add LAST so it sees the final response.
app.add_middleware(_AccessLogMiddleware)


class _CachedStaticFiles(StaticFiles):
    """StaticFiles that sends a long-lived Cache-Control header.

    Safe because every <link>/<script> URL includes a `?v={STATIC_ASSET_VERSION}`
    cache-buster — bump the version to invalidate.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/static", _CachedStaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["urlencode"] = lambda value: quote_plus(str(value))


def _csrf_token_for(request: Request) -> str:
    """Read the request's CSRF token; ensure one exists if the session lacks it."""
    return _ensure_csrf_token(request.session)


def _csrf_input(request: Request) -> str:
    """Render a hidden CSRF input for inclusion in <form> blocks."""
    token = _csrf_token_for(request)
    return f'<input type="hidden" name="{_CSRF_FORM_FIELD}" value="{token}" />'


# Expose helpers as Jinja globals so templates can call csrf_input(request).
# ty's strict view of globals' value-type doesn't accept arbitrary callables;
# Jinja itself does. Suppress the false positives.
templates.env.globals["csrf_input"] = _csrf_input  # ty: ignore[invalid-assignment]
templates.env.globals["csrf_token_for"] = _csrf_token_for  # ty: ignore[invalid-assignment]


@dataclass
class FolderOption:
    id: int
    name: str
    path: str
    depth: int


@dataclass
class FeedInFolder:
    url: str
    title: str
    icon_url: str | None
    unread_count: int


_meta_conn_local = threading.local()


def get_meta_connection() -> sqlite3.Connection:
    """Per-thread persistent SQLite connection.

    Opening a fresh connection per request was a measured bottleneck: simple
    SELECTs that take 5ms standalone took 2-3s under 8-way concurrency because
    every request paid for file open, schema load, mmap setup, and PRAGMA
    application. Each uvicorn worker thread now opens once and reuses, so the
    cost is paid 8-40 times total instead of N-times-per-request.

    Note: sqlite3.Connection's context-manager protocol commits the transaction
    on success but does NOT close the connection — the existing
    `with get_meta_connection() as conn:` pattern continues to work and is
    transaction-scoped per call site, while the underlying connection persists.
    """
    conn = getattr(_meta_conn_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(str(META_DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so overlapping writers (e.g. background refresh writing
    # folder_feeds while a request persists a setting) wait briefly instead of
    # immediately failing with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    _meta_conn_local.conn = conn
    return conn


def get_thumb_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(THUMB_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_thumb_schema() -> None:
    with get_thumb_connection() as conn:
        # WAL allows concurrent readers; thumb content is regeneratable so
        # synchronous=NORMAL is the right tradeoff (durability not critical).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thumb_cache (
                cache_key TEXT PRIMARY KEY,
                jpeg BLOB NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )


def get_starred_archive_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(STARRED_ARCHIVE_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_starred_archive_schema() -> None:
    with get_starred_archive_connection() as conn:
        # Archive content is irreplaceable if the source goes down — leave
        # synchronous at the default FULL for durability. WAL still helps with
        # concurrent reads from the render path.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_entry (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                status TEXT NOT NULL,
                starred_at REAL NOT NULL,
                archived_at REAL,
                error TEXT,
                source_html_zlib BLOB,
                readability_html_zlib BLOB,
                content_html_zlib BLOB,
                title TEXT,
                link TEXT,
                feed_title TEXT,
                author TEXT,
                published_at REAL,
                received_at REAL,
                PRIMARY KEY (feed_url, entry_id)
            )
            """
        )
        # Migration: add columns to pre-existing tables created before the
        # orphan-save support landed. PRAGMA table_info is the standard way
        # to detect what's already there.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(archived_entry)").fetchall()}
        for col_name, col_decl in (
            ("content_html_zlib", "BLOB"),
            ("title", "TEXT"),
            ("link", "TEXT"),
            ("feed_title", "TEXT"),
            ("author", "TEXT"),
            ("published_at", "REAL"),
            ("received_at", "REAL"),
        ):
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE archived_entry ADD COLUMN {col_name} {col_decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archived_entry_status ON archived_entry (status)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_asset (
                asset_hash TEXT PRIMARY KEY,
                data BLOB NOT NULL,
                content_type TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                byte_size INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_asset_link (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                asset_hash TEXT NOT NULL,
                PRIMARY KEY (feed_url, entry_id, source_url)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archived_asset_link_hash ON archived_asset_link (asset_hash)")


def ensure_meta_schema() -> None:
    with get_meta_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_id INTEGER,
                UNIQUE(name, parent_id),
                FOREIGN KEY(parent_id) REFERENCES folders(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS folder_feeds (
                folder_id INTEGER NOT NULL,
                feed_url TEXT NOT NULL,
                PRIMARY KEY(folder_id, feed_url),
                FOREIGN KEY(folder_id) REFERENCES folders(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_entries (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(feed_url, entry_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entry_read_state (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                read_at TEXT NOT NULL,
                PRIMARY KEY(feed_url, entry_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS read_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                title TEXT,
                link TEXT,
                feed_title TEXT,
                read_at TEXT NOT NULL,
                UNIQUE(feed_url, entry_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_read_history_read_at ON read_history(read_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_video_duration (
                video_id TEXT PRIMARY KEY,
                duration_seconds INTEGER,
                duration_display TEXT,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_failure_state (
                feed_url TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL,
                last_error TEXT,
                last_failure_at REAL,
                last_success_at REAL
            )
            """
        )
        try:
            conn.execute("ALTER TABLE feed_failure_state ADD COLUMN acknowledged_at REAL")
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entry_lead_images (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                image_url TEXT,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (feed_url, entry_id)
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
            CREATE TABLE IF NOT EXISTS feed_lead_image_strategy (
                feed_url TEXT PRIMARY KEY,
                strategy TEXT NOT NULL DEFAULT 'unknown',
                detected_at REAL NOT NULL,
                manual INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        try:
            conn.execute("ALTER TABLE feed_lead_image_strategy ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE entry_lead_images ADD COLUMN image_alt TEXT")
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE entry_lead_images ADD COLUMN image_title TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_strategy_cache ADD COLUMN image_alt TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_strategy_cache ADD COLUMN image_title TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN caption_source TEXT")
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disabled_feeds (
                feed_url TEXT PRIMARY KEY,
                disabled_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_display_prefs (
                feed_url TEXT PRIMARY KEY,
                show_lead_image_in_article INTEGER NOT NULL DEFAULT 1,
                show_lead_image_as_thumb INTEGER NOT NULL DEFAULT 1,
                show_image_caption INTEGER NOT NULL DEFAULT -1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_strategy_cache (
                feed_url TEXT NOT NULL,
                strategy TEXT NOT NULL,
                image_url TEXT,
                fetched_at REAL NOT NULL,
                error TEXT,
                PRIMARY KEY (feed_url, strategy)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS highlight_keywords (
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT 'yellow',
                PRIMARY KEY (scope, scope_id, keyword)
            )
            """
        )
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN is_regex INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN type TEXT NOT NULL DEFAULT 'highlight'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN search_in TEXT NOT NULL DEFAULT 'title'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN delivery TEXT NOT NULL DEFAULT 'immediately'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN email_to TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN batch_time TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN batch_count INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN cc_me INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN dedup_window_hours INTEGER NOT NULL DEFAULT 24")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN exclude_scope_ids TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE highlight_keywords SET sort_order = rowid WHERE sort_order = 0")
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                rule_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '',
                keyword TEXT NOT NULL DEFAULT '',
                entries_affected INTEGER NOT NULL DEFAULT 0,
                trigger TEXT NOT NULL DEFAULT 'manual'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_run_log_entries (
                log_id   INTEGER NOT NULL,
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                title    TEXT,
                link     TEXT,
                feed_title TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS rule_run_log_entries_log_id"
            " ON rule_run_log_entries (log_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dedup_false_matches (
                keep_link TEXT NOT NULL,
                mark_link TEXT NOT NULL,
                added_at  TEXT NOT NULL,
                PRIMARY KEY (keep_link, mark_link)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                address TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_batch_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_scope TEXT NOT NULL,
                rule_scope_id TEXT NOT NULL,
                rule_keyword TEXT NOT NULL,
                queued_at TEXT NOT NULL,
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                title TEXT,
                link TEXT,
                feed_title TEXT,
                excerpt TEXT,
                email_to TEXT NOT NULL,
                cc_me INTEGER NOT NULL DEFAULT 0,
                UNIQUE(rule_scope, rule_scope_id, rule_keyword, entry_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scraped_feeds (
                id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                mode TEXT NOT NULL,
                selector TEXT,
                feed_title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_scraped_at TEXT,
                last_content_hash TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scraped_entries (
                id TEXT PRIMARY KEY,
                scraped_feed_id TEXT NOT NULL REFERENCES scraped_feeds(id),
                entry_url TEXT,
                title TEXT NOT NULL,
                content TEXT,
                published_at TEXT NOT NULL,
                hidden INTEGER NOT NULL DEFAULT 0,
                UNIQUE(scraped_feed_id, entry_url)
            )
            """
        )
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN hide_shorts INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN feed_thumbnail_url TEXT")
        except Exception:
            pass
        root = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id IS NULL",
            (ROOT_FOLDER_NAME,),
        ).fetchone()
        if not root:
            conn.execute(
                "INSERT INTO folders (name, parent_id) VALUES (?, NULL)",
                (ROOT_FOLDER_NAME,),
            )
        if DEBUG_MODE:
            # Ensure the _Lectio system folder exists (used for dev feeds).
            root_id = conn.execute(
                "SELECT id FROM folders WHERE name = ? AND parent_id IS NULL", (ROOT_FOLDER_NAME,)
            ).fetchone()
            if root_id:
                conn.execute(
                    "INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)",
                    (_LECTIO_FOLDER_NAME, int(root_id["id"])),
                )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (AUTO_REFRESH_SETTING_KEY, str(AUTO_REFRESH_MINUTES)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (SORT_BY_SETTING_KEY, DEFAULT_SORT_BY),
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (SORT_DIR_SETTING_KEY, DEFAULT_SORT_DIR),
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (GLOBAL_NOTE_SETTING_KEY, ""),
        )
        pass  # email_to seeding removed; Contacts tab manages recipients


_app_settings_cache: dict[str, str] | None = None
_app_settings_cache_lock = threading.Lock()


def _load_app_settings_cache(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {str(r["key"]): str(r["value"]) for r in rows}


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a setting from the in-memory cache. Loaded once on first access,
    kept consistent through set_setting writes. Avoids ~4 SELECTs per home
    render that, while individually fast, queue up under concurrency."""
    global _app_settings_cache
    with _app_settings_cache_lock:
        cache = _app_settings_cache
        if cache is None:
            cache = _load_app_settings_cache(conn)
            _app_settings_cache = cache
        return cache.get(key)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Idempotent setting write. Updates the in-memory cache and persists only
    when the value actually changed. The hot path (home render) calls this on
    every request to persist sort preferences; without the no-op guard each
    page view took a meta-DB writer lock, blocking concurrent renders."""
    global _app_settings_cache
    with _app_settings_cache_lock:
        cache = _app_settings_cache
        if cache is None:
            cache = _load_app_settings_cache(conn)
            _app_settings_cache = cache
        if cache.get(key) == value:
            return
        cache[key] = value
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (key, value),
    )


def get_cached_setting(key: str) -> str | None:
    """Read from in-memory cache without a DB connection. Returns None if cache unloaded."""
    with _app_settings_cache_lock:
        if _app_settings_cache is None:
            return None
        return _app_settings_cache.get(key)


def get_runtime_setting(key: str, env_fallback: str = "") -> str:
    """DB setting takes precedence; env_fallback (typically os.getenv) is the fallback."""
    val = get_cached_setting(key)
    if val is not None:
        return val
    return env_fallback


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    global _app_settings_cache
    with _app_settings_cache_lock:
        if _app_settings_cache is not None:
            _app_settings_cache.pop(key, None)
    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))


_DISPLAY_PREF_KEYS = frozenset({"show_lead_image_in_article", "show_lead_image_as_thumb", "show_image_caption", "hide_shorts"})
_DISPLAY_PREF_DEFAULTS: dict = {"show_lead_image_in_article": 1, "show_lead_image_as_thumb": 1, "show_image_caption": -1, "hide_shorts": 0, "feed_thumbnail_url": None}


def get_feed_display_prefs(conn: sqlite3.Connection, feed_url: str) -> dict:
    row = conn.execute("SELECT * FROM feed_display_prefs WHERE feed_url = ?", (feed_url,)).fetchone()
    if row:
        return dict(row)
    return {"feed_url": feed_url, **_DISPLAY_PREF_DEFAULTS}


def get_all_feed_display_prefs(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM feed_display_prefs").fetchall()
    return {row["feed_url"]: dict(row) for row in rows}


def upsert_feed_display_pref(conn: sqlite3.Connection, feed_url: str, key: str, value: int) -> None:
    if key not in _DISPLAY_PREF_KEYS:
        raise ValueError(f"Unknown display pref key: {key}")
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(f"UPDATE feed_display_prefs SET {key} = ? WHERE feed_url = ?", (value, feed_url))


def upsert_feed_thumbnail_url(conn: sqlite3.Connection, feed_url: str, thumbnail_url: str | None) -> None:
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(
        "UPDATE feed_display_prefs SET feed_thumbnail_url = ? WHERE feed_url = ?",
        (thumbnail_url or None, feed_url),
    )


_HIGHLIGHT_VALID_COLORS = frozenset({'yellow', 'green', 'blue', 'pink', 'orange'})
_HIGHLIGHT_VALID_SCOPES = frozenset({'global', 'folder', 'feed'})


_HIGHLIGHT_VALID_TYPES = {"highlight", "mark_as_read", "email_article", "deduplicate"}
_HIGHLIGHT_VALID_SEARCH_IN = {"title", "body", "both"}
_HIGHLIGHT_VALID_DELIVERY = {"immediately", "batch"}
_DEDUP_VALID_MATCH_METHODS = {"slug", "title", "both", "fuzzy", "safe"}


def get_highlight_keywords(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT scope, scope_id, keyword, color, is_regex, enabled, type, search_in, delivery,"
        " email_to, batch_time, batch_count, cc_me, dedup_window_hours, exclude_scope_ids, sort_order"
        " FROM highlight_keywords ORDER BY sort_order ASC, rowid ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def add_highlight_keyword(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    keyword: str,
    color: str,
    is_regex: bool = False,
    rule_type: str = "highlight",
    search_in: str = "title",
    delivery: str = "immediately",
    email_to: str = "",
    batch_time: str = "",
    batch_count: int = 0,
    cc_me: bool = False,
    enabled: int = 0,
    dedup_window_hours: int = 168,
    exclude_scope_ids: str = "",
) -> None:
    if scope not in _HIGHLIGHT_VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope}")
    if color not in _HIGHLIGHT_VALID_COLORS:
        color = "yellow"
    if rule_type not in _HIGHLIGHT_VALID_TYPES:
        rule_type = "highlight"
    if search_in not in _HIGHLIGHT_VALID_SEARCH_IN:
        search_in = "title"
    if delivery not in _HIGHLIGHT_VALID_DELIVERY:
        delivery = "immediately"
    conn.execute(
        "INSERT OR REPLACE INTO highlight_keywords"
        " (scope, scope_id, keyword, color, is_regex, enabled, type, search_in, delivery,"
        "  email_to, batch_time, batch_count, cc_me, dedup_window_hours, exclude_scope_ids)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, scope_id, keyword.strip(), color, 1 if is_regex else 0, 1 if enabled else 0,
         rule_type, search_in, delivery,
         email_to.strip(), batch_time.strip(), max(0, int(batch_count or 0)), 1 if cc_me else 0,
         max(1, int(dedup_window_hours or 168)), exclude_scope_ids.strip()),
    )


def get_email_contacts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, label, address FROM email_contacts ORDER BY label"
    ).fetchall()
    return [dict(r) for r in rows]


def add_email_contact(conn: sqlite3.Connection, label: str, address: str) -> dict:
    label = label.strip()
    address = address.strip()
    if not label or not address or "@" not in address:
        raise ValueError("Invalid label or address")
    conn.execute(
        "INSERT OR IGNORE INTO email_contacts (label, address) VALUES (?, ?)",
        (label, address),
    )
    row = conn.execute(
        "SELECT id, label, address FROM email_contacts WHERE address = ?", (address,)
    ).fetchone()
    return dict(row)


def remove_email_contact(conn: sqlite3.Connection, contact_id: int) -> None:
    conn.execute("DELETE FROM email_contacts WHERE id = ?", (contact_id,))


def remove_highlight_keyword(conn: sqlite3.Connection, scope: str, scope_id: str, keyword: str) -> None:
    conn.execute(
        "DELETE FROM highlight_keywords WHERE scope = ? AND scope_id = ? AND keyword = ?",
        (scope, scope_id, keyword.strip()),
    )


_TRIVIAL_CAPTIONS = frozenset({
    "responsive image", "image", "photo", "picture", "img",
    "thumbnail", "banner", "featured image", "header image",
})
_FILENAME_CAPTION_RE = re.compile(
    r"(?:\.(jpe?g|png|gif|webp|avif|svg|bmp|tiff?)$|^(?:DSC|IMG|dsc|img)[_-]?\d{3,})",
    re.IGNORECASE,
)


def should_show_caption(caption: str | None, *, entry_title: str | None, content_html: str | None, pref: int) -> bool:
    """Return whether the image caption should be rendered.

    pref: -1 = auto-suppress heuristics, 0 = never, 1 = always.
    """
    if not caption:
        return False
    if pref == 0:
        return False
    if pref == 1:
        return True
    # Auto heuristics
    stripped = caption.strip()
    if stripped.lower() in _TRIVIAL_CAPTIONS:
        return False
    if _FILENAME_CAPTION_RE.search(stripped):
        return False
    if entry_title and stripped.lower() == entry_title.strip().lower():
        return False
    if content_html and stripped in content_html:
        return False
    return True


def normalize_auto_refresh_minutes(value: int) -> int:
    if value <= 0:
        return 0
    return max(value, MIN_AUTO_REFRESH_MINUTES)


def parse_epoch_setting(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return None
    if parsed <= 0:
        return None
    return parsed


def get_auto_refresh_minutes(conn: sqlite3.Connection) -> int:
    raw_value = get_setting(conn, AUTO_REFRESH_SETTING_KEY)
    if raw_value is None:
        return AUTO_REFRESH_MINUTES
    try:
        return normalize_auto_refresh_minutes(int(raw_value))
    except ValueError:
        return AUTO_REFRESH_MINUTES


def purge_lower_level_folders(conn: sqlite3.Connection) -> None:
    root_id = get_root_folder_id(conn)
    rows = conn.execute(
        """
        WITH RECURSIVE tree(id, parent_id, depth, top_level_id) AS (
            SELECT id, parent_id, 0 AS depth, id AS top_level_id
            FROM folders
            WHERE id = ?
            UNION ALL
            SELECT f.id,
                   f.parent_id,
                   t.depth + 1,
                   CASE WHEN t.depth = 0 THEN f.id ELSE t.top_level_id END AS top_level_id
            FROM folders f
            JOIN tree t ON f.parent_id = t.id
        )
        SELECT id, depth, top_level_id
        FROM tree
        WHERE depth >= 2
        """,
        (root_id,),
    ).fetchall()

    nested_folder_ids: list[int] = []
    for row in rows:
        folder_id = int(row["id"])
        top_level_id = int(row["top_level_id"])
        nested_folder_ids.append(folder_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url)
            SELECT ?, feed_url
            FROM folder_feeds
            WHERE folder_id = ?
            """,
            (top_level_id, folder_id),
        )

    if not nested_folder_ids:
        return

    placeholders = ",".join("?" for _ in nested_folder_ids)
    conn.execute(
        f"DELETE FROM folder_feeds WHERE folder_id IN ({placeholders})",
        nested_folder_ids,
    )
    conn.execute(
        f"DELETE FROM folders WHERE id IN ({placeholders})",
        nested_folder_ids,
    )


def get_root_folder_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM folders WHERE name = ? AND parent_id IS NULL",
        (ROOT_FOLDER_NAME,),
    ).fetchone()
    if not row:
        raise RuntimeError("Root folder is missing.")
    return int(row["id"])


def _get_lectio_folder_id(conn: sqlite3.Connection) -> int | None:
    root_id = get_root_folder_id(conn)
    row = conn.execute(
        "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
        (_LECTIO_FOLDER_NAME, root_id),
    ).fetchone()
    return int(row["id"]) if row else None


def get_descendant_folder_ids(conn: sqlite3.Connection, folder_id: int) -> list[int]:
    rows = conn.execute(
        """
        WITH RECURSIVE descendants(id) AS (
            SELECT id FROM folders WHERE id = ?
            UNION ALL
            SELECT f.id
            FROM folders f
            JOIN descendants d ON f.parent_id = d.id
        )
        SELECT id FROM descendants
        """,
        (folder_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def get_folder_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    debug_clause = "" if DEBUG_MODE else " AND f.name != :exclude"
    params: dict = {"root": ROOT_FOLDER_NAME}
    if not DEBUG_MODE:
        params["exclude"] = _LECTIO_FOLDER_NAME
    return conn.execute(
        f"""
        SELECT
            f.id,
            f.name,
            CASE WHEN f.parent_id IS NULL THEN 0 ELSE 1 END AS depth,
            CASE WHEN f.parent_id IS NULL THEN f.name ELSE root.name || ' / ' || f.name END AS path,
            (
                SELECT COUNT(*)
                FROM folder_feeds ff
                WHERE ff.folder_id = f.id
            ) AS feed_count
        FROM folders f
        LEFT JOIN folders root ON f.parent_id = root.id
        WHERE (f.parent_id IS NULL OR f.parent_id = (
            SELECT id FROM folders WHERE name = :root AND parent_id IS NULL
        )){debug_clause}
        ORDER BY path
        """,
        params,
    ).fetchall()


def get_folder_options(conn: sqlite3.Connection) -> list[FolderOption]:
    debug_clause = "" if DEBUG_MODE else " AND f.name != :exclude"
    params: dict = {"root": ROOT_FOLDER_NAME}
    if not DEBUG_MODE:
        params["exclude"] = _LECTIO_FOLDER_NAME
    rows = conn.execute(
        f"""
        SELECT
            f.id,
            f.name,
            CASE WHEN f.parent_id IS NULL THEN 0 ELSE 1 END AS depth,
            CASE WHEN f.parent_id IS NULL THEN f.name ELSE root.name || ' / ' || f.name END AS path
        FROM folders f
        LEFT JOIN folders root ON f.parent_id = root.id
        WHERE (f.parent_id IS NULL OR f.parent_id = (
            SELECT id FROM folders WHERE name = :root AND parent_id IS NULL
        )){debug_clause}
        ORDER BY path
        """,
        params,
    ).fetchall()
    return [FolderOption(id=int(r["id"]), name=r["name"], path=r["path"], depth=int(r["depth"])) for r in rows]


def get_direct_feed_urls_by_folder(conn: sqlite3.Connection) -> dict[int, list[str]]:
    rows = conn.execute("SELECT folder_id, feed_url FROM folder_feeds ORDER BY feed_url").fetchall()
    by_folder: dict[int, list[str]] = {}
    for row in rows:
        folder_id = int(row["folder_id"])
        by_folder.setdefault(folder_id, []).append(str(row["feed_url"]))
    return by_folder


def get_folder_feed_urls(conn: sqlite3.Connection, folder_id: int) -> set[str]:
    descendant_ids = get_descendant_folder_ids(conn, folder_id)
    placeholders = ",".join("?" for _ in descendant_ids)
    rows = conn.execute(
        f"SELECT DISTINCT feed_url FROM folder_feeds WHERE folder_id IN ({placeholders})",
        descendant_ids,
    ).fetchall()
    return {str(r["feed_url"]) for r in rows}


def get_all_feed_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT DISTINCT feed_url FROM folder_feeds").fetchall()
    return {str(r["feed_url"]) for r in rows}


def get_disabled_feed_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT feed_url FROM disabled_feeds").fetchall()
    return {str(r["feed_url"]) for r in rows}


def disable_feed(feed_url: str) -> None:
    feed_url = feed_url.strip()
    if not feed_url:
        return
    with get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO disabled_feeds (feed_url) VALUES (?)",
            (feed_url,),
        )
    invalidate_meta_structure_cache()


def enable_feed(feed_url: str) -> None:
    feed_url = feed_url.strip()
    if not feed_url:
        return
    with get_meta_connection() as conn:
        conn.execute("DELETE FROM disabled_feeds WHERE feed_url = ?", (feed_url,))
    invalidate_meta_structure_cache()


def get_meta_structure_snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    """Cached bundle of folder/feed structure queries.

    These tables change only on explicit user mutations, so once computed the
    snapshot stays valid until something invalidates it. Collapses ~5 SQL calls
    per home render to a dict lookup, which is what we actually need under
    concurrent navigation: tiny SELECTs that are fine standalone but
    catastrophic when 8 worker threads each issue 5 of them per page render.

    Also builds folder_feed_urls_by_id (root → all feeds; non-root → that
    folder's feeds), exploiting Lectio's flat one-level-below-root hierarchy
    so we don't need a recursive CTE per request.
    """
    with _meta_structure_lock:
        if _meta_structure_cache:
            return _meta_structure_cache  # type: ignore[return-value]
    raw_folder_rows = get_folder_rows(conn)
    direct_feed_urls_by_folder = get_direct_feed_urls_by_folder(conn)
    folder_options = get_folder_options(conn)
    all_feed_urls = get_all_feed_urls(conn)
    root_id = get_root_folder_id(conn)

    folder_feed_urls_by_id: dict[int, set[str]] = {}
    for row in raw_folder_rows:
        fid = int(row["id"])
        if fid == root_id:
            folder_feed_urls_by_id[fid] = set(all_feed_urls)
        else:
            folder_feed_urls_by_id[fid] = set(direct_feed_urls_by_folder.get(fid, []))

    snapshot: dict[str, object] = {
        "raw_folder_rows": [dict(r) for r in raw_folder_rows],
        "direct_feed_urls_by_folder": {k: list(v) for k, v in direct_feed_urls_by_folder.items()},
        "folder_options": folder_options,
        "all_feed_urls": all_feed_urls,
        "root_id": root_id,
        "folder_feed_urls_by_id": folder_feed_urls_by_id,
    }
    with _meta_structure_lock:
        _meta_structure_cache.update(snapshot)
    return snapshot


def get_unread_counts_by_folder(
    folder_rows: Sequence[sqlite3.Row],
    unread_counts_by_feed: dict[str, int],
    direct_feed_urls_by_folder: dict[int, list[str]],
) -> dict[int, int]:
    counts = {int(cast(int, row["id"])): 0 for row in folder_rows}
    if not counts:
        return counts

    root_folder_id = None
    for row in folder_rows:
        folder_id = int(cast(int, row["id"]))
        depth = int(cast(int, row["depth"]))
        direct_count = sum(unread_counts_by_feed.get(feed_url, 0) for feed_url in direct_feed_urls_by_folder.get(folder_id, []))
        counts[folder_id] = direct_count
        if depth == 0:
            root_folder_id = folder_id

    if root_folder_id is not None:
        counts[root_folder_id] = sum(counts.values())

    return counts


def _compute_unread_counts_by_feed() -> dict[str, int]:
    """Per-feed unread count via a single SQL GROUP BY — one query for all feeds.

    Cross-feed dedupe (same article appearing in RSS+Atom mirrors of one source)
    is intentionally dropped — it required a full Python scan of every entry on
    every cache miss, which doesn't scale past a few thousand entries. Users
    who run into double-counted feeds should unsubscribe one of the mirrors."""
    try:
        conn = sqlite3.connect(str(READER_DB_PATH), uri=False, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT feed, COUNT(*) FROM entries WHERE read=0 GROUP BY feed"
        ).fetchall()
        conn.close()
        return {str(row[0]): int(row[1]) for row in rows}
    except Exception:
        LOGGER.exception("_compute_unread_counts_by_feed direct SQL failed, falling back")
        counts: dict[str, int] = {}
        with get_reader() as reader:
            for feed in reader.get_feeds():
                counts[feed.url] = reader.get_entry_counts(feed=feed.url, read=False).total or 0
        return counts


def _refresh_unread_counts_async(generation: int) -> None:
    """Single-flight background scan. Updates cache when done, unless the
    cache was invalidated (generation bumped) after this refresh started."""
    global unread_counts_refresh_inflight
    try:
        counts = _compute_unread_counts_by_feed()
        with unread_counts_cache_lock:
            if _unread_counts_generation == generation:
                unread_counts_cache["unread_counts"] = (time.time(), counts)
    except Exception:
        LOGGER.exception("background unread counts refresh failed")
    finally:
        with unread_counts_compute_lock:
            unread_counts_refresh_inflight = False


def get_unread_counts_by_feed() -> dict[str, int]:
    """Stale-while-revalidate: never block the request on the 33k-entry scan.
    Fresh cache → return it. Stale cache → return stale, kick off background
    refresh. Cold cache → first caller computes synchronously, others wait."""
    global unread_counts_refresh_inflight
    now = time.time()
    with unread_counts_cache_lock:
        cached = unread_counts_cache.get("unread_counts")
        current_gen = _unread_counts_generation
    if cached:
        ts, value = cached
        if now - ts < UNREAD_COUNTS_CACHE_TTL_SECONDS:
            return value.copy()
        # Stale — serve it, spawn one refresh.
        with unread_counts_compute_lock:
            if not unread_counts_refresh_inflight:
                unread_counts_refresh_inflight = True
                threading.Thread(target=_refresh_unread_counts_async, args=(current_gen,), daemon=True).start()
        return value.copy()

    # Cold cache: first arriver computes synchronously, others wait on lock.
    with unread_counts_compute_lock:
        with unread_counts_cache_lock:
            cached = unread_counts_cache.get("unread_counts")
            if cached:
                return cached[1].copy()
        counts = _compute_unread_counts_by_feed()
        with unread_counts_cache_lock:
            unread_counts_cache["unread_counts"] = (time.time(), counts)
        return counts.copy()



def normalize_entry_link_for_dedupe(link: str | None) -> str | None:
    if not link:
        return None
    normalized_link = str(link).split("#")[0].rstrip("/")
    return normalized_link or None


def normalize_entry_title_for_dedupe(title: str | None) -> str:
    if not title:
        return ""
    return " ".join(str(title).strip().lower().split())


def title_word_similarity(t1: str, t2: str) -> float:
    """Jaccard similarity on word sets. Returns 0.0–1.0."""
    w1 = set(t1.split())
    w2 = set(t2.split())
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


# ── Safe multi-signal dedup ───────────────────────────────────────────────────

_SAFE_DEDUP_FUZZY_THRESH      = 0.80
_SAFE_DEDUP_BODY_FUZZY_THRESH = 0.75
_SAFE_DEDUP_BODY_CHARS        = 400
_SAFE_DEDUP_MIN_BODY_CHARS    = 30
_SAFE_DEDUP_MIN_SLUG_LEN      = 4
_SAFE_DEDUP_MIN_TITLE_WORDS   = 4
_SAFE_DEDUP_MIN_SLUG_NO_HYPHEN = 16

_SAFE_DEDUP_SLUG_EXTS = frozenset({
    ".php", ".html", ".htm", ".asp", ".aspx", ".cgi", ".pl", ".jsp", ".cfm", ".shtml"
})
_SAFE_DEDUP_SLUG_BLOCKLIST = frozenset({
    "watch", "shorts", "video", "videos", "post", "posts", "article", "articles",
    "page", "pages", "index", "home", "feed", "rss", "atom", "news", "story",
    "entry", "item", "read", "view", "show", "detail", "details", "content",
    "about", "contact", "search", "archive", "archives", "category", "categories",
    "tag", "tags", "author", "user", "profile", "default", "main", "welcome",
    "latest", "recent", "popular", "trending", "featured", "top", "new",
    "forum", "forums", "thread", "threads", "topic", "topics", "blog",
    "comic", "comics", "gallery", "photo", "photos", "image", "images",
    "release", "releases", "pre-release", "download", "downloads", "changelog",
    "update", "updates",
    "p", "s", "t", "r", "q", "m", "n", "a", "e",
})
_SAFE_DEDUP_UNICODE_TRANS = str.maketrans({
    '‘': "'", '’': "'",
    '“': '"', '”': '"',
    '–': '-', '—': '-',
    '…': '...',
    ' ': ' ',
})
_SAFE_DEDUP_TAG_RE = re.compile(r"<[^>]+>")

_SAFE_DEDUP_COMBOS: frozenset[frozenset] = frozenset({
    frozenset({"slug", "title", "body"}),
    frozenset({"slug", "fuzzy_near", "body"}),
    frozenset({"title", "body"}),
    frozenset({"slug", "title", "body_fuzzy"}),
    frozenset({"slug", "fuzzy_near", "body_fuzzy"}),
    frozenset({"title", "body_fuzzy"}),
    frozenset({"slug", "body"}),
    frozenset({"fuzzy_near", "body_fuzzy"}),
    frozenset({"fuzzy_near", "body"}),
    frozenset({"body_fuzzy"}),
    frozenset({"slug", "title", "body", "body_fuzzy"}),
    frozenset({"slug", "fuzzy_near", "body", "body_fuzzy"}),
    frozenset({"title", "body", "body_fuzzy"}),
    frozenset({"slug", "body", "body_fuzzy"}),
    frozenset({"fuzzy_near", "body", "body_fuzzy"}),
})


def _safe_dedup_entry_slug(url: str | None) -> str | None:
    if not url:
        return None
    path = url.split("#")[0].split("?")[0].rstrip("/")
    slug = path.rsplit("/", 1)[-1].lower()
    for ext in _SAFE_DEDUP_SLUG_EXTS:
        if slug.endswith(ext):
            slug = slug[: -len(ext)]
            break
    if len(slug) < _SAFE_DEDUP_MIN_SLUG_LEN or slug in _SAFE_DEDUP_SLUG_BLOCKLIST:
        return None
    if "-" not in slug and len(slug) < _SAFE_DEDUP_MIN_SLUG_NO_HYPHEN:
        return None
    return slug


def _safe_dedup_norm_title(t: str | None) -> str:
    if not t:
        return ""
    import unicodedata
    t = unicodedata.normalize("NFC", t).translate(_SAFE_DEDUP_UNICODE_TRANS)
    return " ".join(t.strip().lower().split())


def _safe_dedup_norm_body(entry) -> str:
    import html as _html
    raw = ""
    if entry.content:
        raw = entry.content[0].value or ""
    if not raw:
        raw = entry.summary or ""
    text = _SAFE_DEDUP_TAG_RE.sub(" ", raw)
    text = _html.unescape(text)
    return " ".join(text.split())[: _SAFE_DEDUP_BODY_CHARS].lower()


def _safe_dedup_collect(reader, feed_urls: set[str], max_per_feed: int, read_filter) -> list[dict]:
    """Read entries from each feed and build the record list for safe-dedup."""
    records: list[dict] = []
    feed_title_map = {f.url: (f.title or str(f.url)) for f in reader.get_feeds()}
    for feed_url in feed_urls:
        try:
            kwargs: dict = {"feed": feed_url, "limit": max_per_feed}
            if read_filter is not None:
                kwargs["read"] = read_filter
            for entry in reader.get_entries(**kwargs):
                published = entry.published or entry.updated or entry.added
                ntitle = _safe_dedup_norm_title(entry.title)
                records.append({
                    "feed_url":   str(entry.feed_url or ""),
                    "entry_id":   str(entry.id),
                    "title":      str(entry.title or ""),
                    "link":       str(entry.link or ""),
                    "feed_title": feed_title_map.get(str(entry.feed_url or ""), str(entry.feed_url or "")),
                    "published":  published.isoformat() if published else None,
                    "published_ts": published.timestamp() if published else 0.0,
                    "slug":       _safe_dedup_entry_slug(entry.link),
                    "ntitle":     ntitle,
                    "body":       _safe_dedup_norm_body(entry),
                })
        except Exception:
            LOGGER.exception("safe-dedup: error reading feed %s", feed_url)
    return records


def _safe_dedup_find_pairs(records: list[dict]) -> dict[tuple[str, str], list[str]]:
    """Run the multi-signal safe-dedup algorithm. Returns {(keep_link, mark_link): [modes]}."""
    from collections import defaultdict as _dd

    def _mk_pair(a: dict, b: dict) -> tuple[str, str]:
        return (a["link"], b["link"]) if a["published_ts"] <= b["published_ts"] else (b["link"], a["link"])

    def _index_to_pairs(idx: dict) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for entries in idx.values():
            if len({e["feed_url"] for e in entries}) < 2:
                continue
            for i, a in enumerate(sorted(entries, key=lambda e: e["published_ts"] or 0)):
                for b in sorted(entries, key=lambda e: e["published_ts"] or 0)[i + 1:]:
                    if a["feed_url"] != b["feed_url"] and a["link"] != b["link"]:
                        pairs.add(_mk_pair(a, b))
        return pairs

    slug_idx:  dict = _dd(list)
    title_idx: dict = _dd(list)
    body_idx:  dict = _dd(list)
    by_feed:   dict = _dd(list)

    for r in records:
        if r["slug"]:
            slug_idx[r["slug"]].append(r)
        if r["ntitle"] and len(r["ntitle"].split()) >= _SAFE_DEDUP_MIN_TITLE_WORDS:
            title_idx[r["ntitle"]].append(r)
        if r["body"] and len(r["body"]) >= _SAFE_DEDUP_MIN_BODY_CHARS:
            body_idx[r["body"]].append(r)
        if r["ntitle"] and len(r["ntitle"].split()) >= _SAFE_DEDUP_MIN_TITLE_WORDS:
            by_feed[r["feed_url"]].append(r)

    slug_pairs  = _index_to_pairs(slug_idx)
    title_pairs = _index_to_pairs(title_idx)
    body_pairs  = _index_to_pairs(body_idx)

    link_feed: dict[str, str] = {r["link"]: r["feed_url"] for r in records if r["link"]}
    cand_pairs: set[tuple[str, str]] = set()
    for pk in slug_pairs | title_pairs:
        fu_a = link_feed.get(pk[0])
        fu_b = link_feed.get(pk[1])
        if fu_a and fu_b and fu_a != fu_b:
            cand_pairs.add((min(fu_a, fu_b), max(fu_a, fu_b)))

    fuzzy_pairs:      set[tuple[str, str]] = set()
    body_fuzzy_pairs: set[tuple[str, str]] = set()

    for fu_i, fu_j in cand_pairs:
        for a in by_feed.get(fu_i, []):
            for b in by_feed.get(fu_j, []):
                if a["link"] == b["link"]:
                    continue
                sim_t = title_word_similarity(a["ntitle"], b["ntitle"])
                if _SAFE_DEDUP_FUZZY_THRESH <= sim_t < 1.0:
                    fuzzy_pairs.add(_mk_pair(a, b))
                if (len(a["body"]) >= _SAFE_DEDUP_MIN_BODY_CHARS
                        and len(b["body"]) >= _SAFE_DEDUP_MIN_BODY_CHARS):
                    sim_b = title_word_similarity(a["body"], b["body"])
                    if sim_b >= _SAFE_DEDUP_BODY_FUZZY_THRESH:
                        body_fuzzy_pairs.add(_mk_pair(a, b))

    all_pairs = slug_pairs | title_pairs | fuzzy_pairs | body_pairs | body_fuzzy_pairs
    pair_modes: dict[tuple[str, str], list[str]] = {}
    for pk in all_pairs:
        modes: list[str] = []
        if pk in slug_pairs:        modes.append("slug")
        if pk in title_pairs:       modes.append("title")
        if pk in fuzzy_pairs:       modes.append("fuzzy_near")
        if pk in body_pairs:        modes.append("body")
        if pk in body_fuzzy_pairs:  modes.append("body_fuzzy")
        if frozenset(modes) in _SAFE_DEDUP_COMBOS:
            pair_modes[pk] = modes
    return pair_modes


def build_entry_dedupe_key(link: str | None, title: str | None) -> str | None:
    normalized_link = normalize_entry_link_for_dedupe(link)
    if not normalized_link:
        return None
    normalized_title = normalize_entry_title_for_dedupe(title)
    return f"{normalized_link}::{normalized_title}"


def entry_url_slug(url: str | None) -> str | None:
    """Extract the last non-empty path segment from a URL (before query/fragment)."""
    if not url:
        return None
    path = url.split("#")[0].split("?")[0].rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    return slug.lower() if slug else None


def _dry_run_dedup(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    match_method: str,
    window_hours: int,
    max_entries: int = 5000,
    exclude_scope_ids: str = "",
    custom_feed_urls: set[str] | None = None,
) -> dict:
    """Preview which entries a deduplicate rule would mark read."""
    if custom_feed_urls is not None:
        feed_urls = custom_feed_urls
    elif scope == "global":
        feed_urls = get_all_feed_urls(conn)
    elif scope == "folder":
        try:
            fid = int(scope_id)
        except (ValueError, TypeError):
            return {"error": "invalid scope_id"}
        feed_urls = get_folder_feed_urls(conn, fid)
    else:
        return {"error": "deduplicate rules require global or folder scope"}

    if exclude_scope_ids and custom_feed_urls is None:
        excluded: set[str] = set()
        for fid_str in exclude_scope_ids.split(","):
            fid_str = fid_str.strip()
            if fid_str.isdigit():
                excluded |= get_folder_feed_urls(conn, int(fid_str))
        feed_urls -= excluded

    if len(feed_urls) < 2:
        return {
            "groups": [], "total_entries_scanned": 0, "total_would_mark_read": 0,
            "message": "Need at least 2 feeds in scope to deduplicate",
        }

    if match_method == "safe":
        per_feed_limit = max(1, max_entries // max(1, len(feed_urls)))
        false_matches: set[str] = set()
        rows = conn.execute(
            "SELECT keep_link, mark_link FROM dedup_false_matches"
        ).fetchall()
        false_matches = {r[0] + "||" + r[1] for r in rows}
        with get_reader() as reader:
            records = _safe_dedup_collect(reader, feed_urls, per_feed_limit, None)
        pair_modes = _safe_dedup_find_pairs(records)
        link_to_rec = {r["link"]: r for r in records if r["link"]}
        by_keep: dict[str, dict] = {}
        seen_mark: set[str] = set()
        for (keep_link, mark_link), modes in sorted(
            pair_modes.items(), key=lambda kv: -len(kv[1])  # most signals first
        ):
            if keep_link + "||" + mark_link in false_matches:
                continue
            keep_rec = link_to_rec.get(keep_link)
            mark_rec = link_to_rec.get(mark_link)
            if not keep_rec or not mark_rec:
                continue
            if keep_link not in by_keep:
                by_keep[keep_link] = {
                    "match_by": "safe",
                    "matched_value": "+".join(modes),
                    "keep": keep_rec,
                    "mark_read": [],
                }
            if mark_link not in seen_mark:
                by_keep[keep_link]["mark_read"].append(mark_rec)
                seen_mark.add(mark_link)
        groups = [g for g in by_keep.values() if g["mark_read"]]
        return {
            "groups": groups,
            "total_entries_scanned": len(records),
            "total_would_mark_read": len(seen_mark),
        }

    per_feed_limit = max(1, max_entries // max(1, len(feed_urls)))

    with get_reader() as reader:
        feed_title_map = {f.url: (f.title or str(f.url)) for f in reader.get_feeds()}
        slug_index: dict[str, list[dict]] = {}
        title_index: dict[str, list[dict]] = {}
        combined_index: dict[tuple[str, str], list[dict]] = {}
        fuzzy_entries: dict[str, list[dict]] = {}
        total_scanned = 0

        for feed_url in feed_urls:
            if total_scanned >= max_entries:
                break
            try:
                for entry in reader.get_entries(feed=feed_url, limit=per_feed_limit):
                    if total_scanned >= max_entries:
                        break
                    total_scanned += 1
                    published = entry.published or entry.updated or entry.added
                    info: dict = {
                        "title": str(entry.title or ""),
                        "link": str(entry.link or ""),
                        "feed_url": str(entry.feed_url or ""),
                        "feed_title": feed_title_map.get(str(entry.feed_url or ""), str(entry.feed_url or "")),
                        "published": published.isoformat() if published else None,
                        "published_ts": published.timestamp() if published else 0.0,
                    }
                    if match_method == "slug" and entry.link:
                        slug = entry_url_slug(entry.link)
                        if slug and len(slug) >= 4:
                            slug_index.setdefault(slug, []).append(info)
                    if match_method == "title" and entry.title:
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if norm:
                            title_index.setdefault(norm, []).append(info)
                    if match_method == "both" and entry.link and entry.title:
                        slug = entry_url_slug(entry.link)
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if slug and norm:
                            combined_index.setdefault((slug, norm), []).append(info)
                    if match_method == "fuzzy" and entry.title:
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if norm and len(norm.split()) >= 4:
                            info["norm_title"] = norm
                            fuzzy_entries.setdefault(str(entry.feed_url or ""), []).append(info)
            except Exception:
                LOGGER.exception("dry-run-dedup: error reading feed %s", feed_url)

    groups: list[dict] = []
    seen_links: set[str] = set()
    window_secs = window_hours * 3600
    _FUZZY_THRESHOLD = 0.80

    if match_method == "slug":
        for slug, entries in slug_index.items():
            if len({e["feed_url"] for e in entries}) < 2:
                continue
            sorted_entries = sorted(entries, key=lambda e: e["published_ts"] or 0)
            keep = sorted_entries[0]
            mark_read = sorted_entries[1:]
            groups.append({"match_by": "slug", "matched_value": slug, "keep": keep, "mark_read": mark_read})
            for e in entries:
                seen_links.add(e["link"])

    if match_method == "title":
        for norm_title, entries in title_index.items():
            if len({e["feed_url"] for e in entries}) < 2:
                continue
            sorted_entries = sorted(entries, key=lambda e: e["published_ts"] or 0)
            oldest_ts = sorted_entries[0]["published_ts"] or 0.0
            newest_ts = sorted_entries[-1]["published_ts"] or 0.0
            if oldest_ts > 0 and newest_ts > 0 and (newest_ts - oldest_ts) > window_secs:
                continue
            keep = sorted_entries[0]
            mark_read = sorted_entries[1:]
            groups.append({"match_by": "title", "matched_value": norm_title, "keep": keep, "mark_read": mark_read})

    if match_method == "both":
        for (slug, norm_title), entries in combined_index.items():
            if len({e["feed_url"] for e in entries}) < 2:
                continue
            sorted_entries = sorted(entries, key=lambda e: e["published_ts"] or 0)
            oldest_ts = sorted_entries[0]["published_ts"] or 0.0
            newest_ts = sorted_entries[-1]["published_ts"] or 0.0
            if oldest_ts > 0 and newest_ts > 0 and (newest_ts - oldest_ts) > window_secs:
                continue
            keep = sorted_entries[0]
            mark_read = sorted_entries[1:]
            groups.append({"match_by": "slug+title", "matched_value": norm_title, "keep": keep, "mark_read": mark_read})

    if match_method == "fuzzy":
        feed_list = [u for u in feed_urls if u in fuzzy_entries]
        seen_mark_links: set[str] = set()
        for i, feed_i in enumerate(feed_list):
            for feed_j in feed_list[i + 1:]:
                for ei in fuzzy_entries[feed_i]:
                    for ej in fuzzy_entries[feed_j]:
                        ts_i = ei["published_ts"] or 0.0
                        ts_j = ej["published_ts"] or 0.0
                        if window_secs > 0 and abs(ts_i - ts_j) > window_secs:
                            continue
                        sim = title_word_similarity(ei["norm_title"], ej["norm_title"])
                        if sim < _FUZZY_THRESHOLD:
                            continue
                        keep, newer = (ei, ej) if ts_i <= ts_j else (ej, ei)
                        if newer["link"] in seen_mark_links:
                            continue
                        seen_mark_links.add(newer["link"])
                        groups.append({
                            "match_by": "fuzzy",
                            "matched_value": f"{round(sim * 100)}% similar",
                            "keep": keep,
                            "mark_read": [newer],
                        })

    return {
        "groups": groups,
        "total_entries_scanned": total_scanned,
        "total_would_mark_read": sum(len(g["mark_read"]) for g in groups),
    }


def _dry_run_pattern(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    keyword: str,
    is_regex: bool,
    search_in: str,
    max_entries: int = 1000,
    result_limit: int = 20,
) -> dict:
    """Preview which entries a pattern-based rule would affect (read + unread, newest first)."""
    import re as _re

    if not keyword:
        return {"matches": [], "total_scanned": 0, "total_matches": 0, "truncated": False}

    try:
        if is_regex:
            pattern = _re.compile(keyword, _re.IGNORECASE)
            match_fn = lambda text: bool(pattern.search(text)) if text else False
        else:
            kw_lower = keyword.lower()
            match_fn = lambda text: kw_lower in (text or "").lower()
    except _re.error as e:
        return {"error": f"Invalid regex: {e}"}

    if scope == "global":
        feed_urls: set[str] | None = None
    elif scope == "folder":
        try:
            fid = int(scope_id)
        except (ValueError, TypeError):
            return {"error": "invalid scope_id"}
        feed_urls = get_folder_feed_urls(conn, fid)
    elif scope == "feed":
        feed_urls = {scope_id} if scope_id else None
    else:
        feed_urls = None

    matches: list[dict] = []
    total_scanned = 0
    total_matches = 0

    with get_reader() as reader:
        feed_title_map = {str(f.url): (f.title or str(f.url)) for f in reader.get_feeds()}

        def iter_entries():
            if feed_urls is None:
                yield from reader.get_entries(limit=max_entries)
            elif len(feed_urls) == 1:
                yield from reader.get_entries(feed=next(iter(feed_urls)), limit=max_entries)
            else:
                per_feed = max(1, max_entries // len(feed_urls))
                for furl in feed_urls:
                    yield from reader.get_entries(feed=furl, limit=per_feed)

        for entry in iter_entries():
            if total_scanned >= max_entries:
                break
            total_scanned += 1
            title_text = str(entry.title or "")
            body_text = ""
            if search_in in ("body", "both"):
                for c in (entry.content or []):
                    body_text += (c.value or "") + " "
                body_text += str(entry.summary or "")

            if search_in == "title":
                matched = match_fn(title_text)
            elif search_in == "body":
                matched = match_fn(body_text)
            else:
                matched = match_fn(title_text) or match_fn(body_text)

            if matched:
                total_matches += 1
                if len(matches) < result_limit:
                    published = entry.published or entry.updated or entry.added
                    matches.append({
                        "title": title_text,
                        "link": str(entry.link or ""),
                        "feed_url": str(entry.feed_url or ""),
                        "feed_title": feed_title_map.get(str(entry.feed_url or ""), str(entry.feed_url or "")),
                        "published": published.isoformat() if published else None,
                        "read": bool(entry.read),
                    })

    return {
        "matches": matches,
        "total_scanned": total_scanned,
        "total_matches": total_matches,
        "truncated": total_matches > result_limit,
    }


def _run_now_dedup(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    match_method: str,
    window_hours: int,
    max_per_feed: int = 500,
    exclude_scope_ids: str = "",
) -> dict:
    """Execute dedup rule on unread entries. Mark newer duplicates as read."""
    global _unread_counts_generation
    if scope == "global":
        feed_urls = get_all_feed_urls(conn)
    elif scope == "folder":
        try:
            fid = int(scope_id)
        except (ValueError, TypeError):
            return {"error": "invalid scope_id"}
        feed_urls = get_folder_feed_urls(conn, fid)
    else:
        return {"error": "deduplicate rules require global or folder scope"}

    if exclude_scope_ids:
        excluded: set[str] = set()
        for fid_str in exclude_scope_ids.split(","):
            fid_str = fid_str.strip()
            if fid_str.isdigit():
                excluded |= get_folder_feed_urls(conn, int(fid_str))
        feed_urls -= excluded

    if len(feed_urls) < 2:
        return {"count": 0, "message": "Need at least 2 feeds in scope"}

    if match_method == "safe":
        false_rows = conn.execute(
            "SELECT keep_link, mark_link FROM dedup_false_matches"
        ).fetchall()
        false_matches: set[str] = {r[0] + "||" + r[1] for r in false_rows}
        with get_reader() as reader:
            records = _safe_dedup_collect(reader, feed_urls, max_per_feed, False)
        pair_modes = _safe_dedup_find_pairs(records)
        link_to_rec = {r["link"]: r for r in records if r["link"]}
        to_mark: set[tuple[str, str]] = set()
        for (keep_link, mark_link), _modes in pair_modes.items():
            if keep_link + "||" + mark_link in false_matches:
                continue
            mark_rec = link_to_rec.get(mark_link)
            if mark_rec:
                to_mark.add((mark_rec["feed_url"], mark_rec["entry_id"]))
        with get_reader() as reader:
            for feed_url, entry_id in to_mark:
                reader.mark_entry_as_read((feed_url, entry_id))
        if to_mark:
            when = datetime.now().isoformat()
            conn.executemany(
                "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
                " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
                [(fu, eid, when) for fu, eid in to_mark],
            )
            _unread_counts_generation += 1
        rec_map = {(r["feed_url"], r["entry_id"]): r for r in records}
        matched_entries = [
            {"feed_url": fu, "entry_id": eid,
             "title": rec_map.get((fu, eid), {}).get("title", ""),
             "link": rec_map.get((fu, eid), {}).get("link", ""),
             "feed_title": rec_map.get((fu, eid), {}).get("feed_title", "")}
            for fu, eid in to_mark
        ]
        return {"count": len(to_mark), "entries": matched_entries}

    slug_index: dict[str, list[dict]] = {}
    title_index: dict[str, list[dict]] = {}
    combined_index: dict[tuple[str, str], list[dict]] = {}
    fuzzy_entries: dict[str, list[dict]] = {}
    window_secs = window_hours * 3600
    _FUZZY_THRESHOLD = 0.80

    with get_reader() as reader:
        for feed_url in feed_urls:
            try:
                for entry in reader.get_entries(feed=feed_url, read=False, limit=max_per_feed):
                    published = entry.published or entry.updated or entry.added
                    info = {
                        "feed_url": str(entry.feed_url or ""),
                        "entry_id": str(entry.id),
                        "link": str(entry.link or ""),
                        "title": str(entry.title or ""),
                        "feed_title": str(getattr(entry, "feed_resolved_title", None) or entry.feed_url or ""),
                        "published_ts": published.timestamp() if published else 0.0,
                    }
                    if match_method == "slug" and entry.link:
                        slug = entry_url_slug(entry.link)
                        if slug and len(slug) >= 4:
                            slug_index.setdefault(slug, []).append(info)
                    if match_method == "title" and entry.title:
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if norm:
                            title_index.setdefault(norm, []).append(info)
                    if match_method == "both" and entry.link and entry.title:
                        slug = entry_url_slug(entry.link)
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if slug and norm:
                            combined_index.setdefault((slug, norm), []).append(info)
                    if match_method == "fuzzy" and entry.title:
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if norm and len(norm.split()) >= 4:
                            info["norm_title"] = norm
                            fuzzy_entries.setdefault(str(entry.feed_url or ""), []).append(info)
            except Exception:
                LOGGER.exception("run-now-dedup: error reading feed %s", feed_url)

        to_mark: set[tuple[str, str]] = set()

        if match_method == "slug":
            for slug, entries in slug_index.items():
                if len({e["feed_url"] for e in entries}) < 2:
                    continue
                for e in sorted(entries, key=lambda e: e["published_ts"] or 0)[1:]:
                    to_mark.add((e["feed_url"], e["entry_id"]))

        if match_method == "title":
            for norm_title, entries in title_index.items():
                if len({e["feed_url"] for e in entries}) < 2:
                    continue
                sorted_entries = sorted(entries, key=lambda e: e["published_ts"] or 0)
                oldest_ts = sorted_entries[0]["published_ts"] or 0.0
                newest_ts = sorted_entries[-1]["published_ts"] or 0.0
                if oldest_ts > 0 and newest_ts > 0 and (newest_ts - oldest_ts) > window_secs:
                    continue
                for e in sorted_entries[1:]:
                    to_mark.add((e["feed_url"], e["entry_id"]))

        if match_method == "both":
            for (slug, norm_title), entries in combined_index.items():
                if len({e["feed_url"] for e in entries}) < 2:
                    continue
                sorted_entries = sorted(entries, key=lambda e: e["published_ts"] or 0)
                oldest_ts = sorted_entries[0]["published_ts"] or 0.0
                newest_ts = sorted_entries[-1]["published_ts"] or 0.0
                if oldest_ts > 0 and newest_ts > 0 and (newest_ts - oldest_ts) > window_secs:
                    continue
                for e in sorted_entries[1:]:
                    to_mark.add((e["feed_url"], e["entry_id"]))

        if match_method == "fuzzy":
            feed_list = [u for u in feed_urls if u in fuzzy_entries]
            for i, feed_i in enumerate(feed_list):
                for feed_j in feed_list[i + 1:]:
                    for ei in fuzzy_entries[feed_i]:
                        for ej in fuzzy_entries[feed_j]:
                            ts_i = ei["published_ts"] or 0.0
                            ts_j = ej["published_ts"] or 0.0
                            if window_secs > 0 and abs(ts_i - ts_j) > window_secs:
                                continue
                            sim = title_word_similarity(ei["norm_title"], ej["norm_title"])
                            if sim < _FUZZY_THRESHOLD:
                                continue
                            newer = ej if ts_i <= ts_j else ei
                            to_mark.add((newer["feed_url"], newer["entry_id"]))

        for feed_url, entry_id in to_mark:
            reader.mark_entry_as_read((feed_url, entry_id))

    if to_mark:
        when = datetime.now().isoformat()
        conn.executemany(
            "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
            [(fu, eid, when) for fu, eid in to_mark],
        )
        _unread_counts_generation += 1

    all_info = (
        list(slug_index.get(k, []) for k in slug_index)
        + list(title_index.get(k, []) for k in title_index)
        + list(combined_index.get(k, []) for k in combined_index)
        + list(fuzzy_entries.get(k, []) for k in fuzzy_entries)
    )
    entry_map = {(r["feed_url"], r["entry_id"]): r for sublist in all_info for r in sublist}
    matched_entries = [
        {"feed_url": fu, "entry_id": eid,
         "title": entry_map.get((fu, eid), {}).get("title", ""),
         "link": entry_map.get((fu, eid), {}).get("link", ""),
         "feed_title": entry_map.get((fu, eid), {}).get("feed_title", "")}
        for fu, eid in to_mark
    ]
    return {"count": len(to_mark), "entries": matched_entries}


def _run_now_pattern(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    keyword: str,
    is_regex: bool,
    search_in: str,
) -> dict:
    """Execute mark_as_read rule: find matching unread entries and mark them read."""
    import re as _re
    global _unread_counts_generation

    if not keyword:
        return {"count": 0}

    try:
        if is_regex:
            pattern = _re.compile(keyword, _re.IGNORECASE)
            match_fn = lambda text: bool(pattern.search(text)) if text else False
        else:
            kw_lower = keyword.lower()
            match_fn = lambda text: kw_lower in (text or "").lower()
    except _re.error as e:
        return {"error": f"Invalid regex: {e}"}

    if scope == "global":
        feed_urls: set[str] | None = None
    elif scope == "folder":
        try:
            fid = int(scope_id)
        except (ValueError, TypeError):
            return {"error": "invalid scope_id"}
        feed_urls = get_folder_feed_urls(conn, fid)
    elif scope == "feed":
        feed_urls = {scope_id} if scope_id else None
    else:
        feed_urls = None

    to_mark: list[tuple[str, str]] = []
    matched_entries: list[dict] = []
    _ENTRY_DETAIL_CAP = 50

    with get_reader() as reader:
        feed_title_cache: dict[str, str] = {}

        def iter_unread():
            if feed_urls is None:
                yield from reader.get_entries(read=False)
            else:
                for furl in feed_urls:
                    yield from reader.get_entries(feed=furl, read=False)

        for entry in iter_unread():
            title_text = str(entry.title or "")
            body_text = ""
            if search_in in ("body", "both"):
                for c in (entry.content or []):
                    body_text += (c.value or "") + " "
                body_text += str(entry.summary or "")

            if search_in == "title":
                matched = match_fn(title_text)
            elif search_in == "body":
                matched = match_fn(body_text)
            else:
                matched = match_fn(title_text) or match_fn(body_text)

            if matched:
                fu = str(entry.feed_url or "")
                to_mark.append((fu, str(entry.id)))
                if len(matched_entries) < _ENTRY_DETAIL_CAP:
                    if fu not in feed_title_cache:
                        try:
                            f = reader.get_feed(fu)
                            feed_title_cache[fu] = str(getattr(f, "title", None) or fu)
                        except Exception:
                            feed_title_cache[fu] = fu
                    matched_entries.append({
                        "feed_url": fu,
                        "entry_id": str(entry.id),
                        "title": str(entry.title or ""),
                        "link": str(entry.link or ""),
                        "feed_title": feed_title_cache.get(fu, fu),
                    })

        for feed_url, entry_id in to_mark:
            reader.mark_entry_as_read((feed_url, entry_id))

    if to_mark:
        when = datetime.now().isoformat()
        conn.executemany(
            "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
            [(fu, eid, when) for fu, eid in to_mark],
        )
        _unread_counts_generation += 1

    return {"count": len(to_mark), "entries": matched_entries}


def _log_auto_run(conn: sqlite3.Connection, now: str, rule_type: str, scope: str,
                  scope_id: str, keyword: str, result: dict) -> None:
    """Write a rule_run_log row (+ matched entries) in the caller's transaction."""
    cur = conn.execute(
        "INSERT INTO rule_run_log"
        " (run_at, rule_type, scope, scope_id, keyword, entries_affected, trigger)"
        " VALUES (?, ?, ?, ?, ?, ?, 'auto')",
        (now, rule_type, scope, scope_id, keyword, result["count"]),
    )
    auto_entries = result.get("entries") or []
    if auto_entries and cur.lastrowid:
        conn.executemany(
            "INSERT INTO rule_run_log_entries"
            " (log_id, feed_url, entry_id, title, link, feed_title)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(cur.lastrowid, e["feed_url"], e["entry_id"],
              e["title"], e["link"], e["feed_title"])
             for e in auto_entries],
        )


def _is_youtube_short(entry: object) -> bool:
    """Return True if the entry is a YouTube Short (link contains /shorts/)."""
    link = str(getattr(entry, "link", None) or "")
    return "youtube.com/shorts/" in link


def _suppress_guid_churn(reader, conn, feed_url: str) -> int:
    """Auto-mark newly-seen unread entries as read when a read entry in the same
    feed already has the same URL slug — indicating the publisher re-issued the
    same article with a new GUID (CMS migration, permalink rebuild, etc.).

    Only URL-slug matching is used: it has near-zero false-positive rate because
    the same slug unambiguously means the same permalink.  Title-only matching is
    too risky for feeds that reuse titles (weekly digests, daily roundups).

    Returns the number of entries suppressed.
    """
    from datetime import datetime, timedelta

    recent_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=90)

    new_unread = [
        e for e in reader.get_entries(feed=feed_url, read=False)
        if getattr(e, "added", None) and e.added >= recent_cutoff
    ]
    if not new_unread:
        return 0

    # Build slug → True index from read entries in this feed (last 6 months, capped).
    history_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=180)
    read_slugs: set[str] = set()
    for entry in reader.get_entries(feed=feed_url, read=True, limit=5000):
        pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
        if pub and pub < history_cutoff:
            break
        if entry.link:
            slug = _safe_dedup_entry_slug(entry.link)
            if slug:
                read_slugs.add(slug)

    if not read_slugs:
        return 0

    to_suppress = []
    for entry in new_unread:
        if not entry.link:
            continue
        slug = _safe_dedup_entry_slug(entry.link)
        if slug and slug in read_slugs:
            to_suppress.append(entry)

    if not to_suppress:
        return 0

    when = datetime.now().isoformat()
    for entry in to_suppress:
        reader.mark_entry_as_read((str(entry.feed_url), str(entry.id)))
    conn.executemany(
        "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)"
        " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at=excluded.read_at",
        [(str(e.feed_url), str(e.id), when) for e in to_suppress],
    )
    return len(to_suppress)


def _run_automation_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Run enabled mark_as_read, deduplicate, email_article, and hide-shorts for refreshed feeds."""
    if not refreshed_feed_urls:
        return

    global _unread_counts_generation

    # GUID-churn suppression: auto-mark re-issued entries (same slug, new GUID) as read.
    try:
        suppressed_total = 0
        with get_reader() as reader:
            with get_meta_connection() as conn:
                for feed_url in refreshed_feed_urls:
                    suppressed_total += _suppress_guid_churn(reader, conn, feed_url)
        if suppressed_total:
            _unread_counts_generation += 1
            LOGGER.info("[guid-churn] suppressed %d re-issued entries", suppressed_total)
    except Exception:
        LOGGER.exception("[guid-churn] error during suppression")

    # Hide Shorts: auto-mark Shorts as read for YouTube feeds that have it enabled.
    try:
        with get_meta_connection() as conn:
            shorts_urls = {
                str(r["feed_url"])
                for r in conn.execute(
                    "SELECT feed_url FROM feed_display_prefs WHERE hide_shorts = 1"
                ).fetchall()
            }
        shorts_targets = refreshed_feed_urls & shorts_urls
        if shorts_targets:
            now_str = datetime.now().isoformat()
            with get_reader() as reader:
                to_mark = []
                for feed_url in shorts_targets:
                    for entry in reader.get_entries(feed=feed_url, read=False):
                        if _is_youtube_short(entry):
                            to_mark.append((str(entry.feed_url), str(entry.id)))
                for fu, eid in to_mark:
                    reader.mark_entry_as_read((fu, eid))
            if to_mark:
                with get_meta_connection() as conn:
                    conn.executemany(
                        "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)"
                        " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at=excluded.read_at",
                        [(fu, eid, now_str) for fu, eid in to_mark],
                    )
                _unread_counts_generation += 1
    except Exception:
        LOGGER.exception("[automation] error applying hide-shorts")
    try:
        # ── Read phase (no write lock held) ──────────────────────────────────
        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            folder_ids_needed = {
                int(r["scope_id"]) for r in all_rules
                if r.get("enabled") and r["scope"] == "folder"
                and str(r.get("scope_id", "")).isdigit()
            }
            folder_feed_map: dict[int, set[str]] = {
                fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed
            }

        enabled_rules = [
            r for r in all_rules
            if r.get("enabled") and r.get("type") in ("mark_as_read", "deduplicate", "email_article")
        ]
        if not enabled_rules:
            return

        now = datetime.now().isoformat()
        ran_dedup_keys: set[tuple[str, str, str]] = set()

        # ── Run phase: each result gets its own short write transaction ───────
        for rule in enabled_rules:
            try:
                rule_type = str(rule.get("type", ""))
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")

                if rule_type == "mark_as_read":
                    for feed_url in refreshed_feed_urls:
                        if scope == "global":
                            in_scope = True
                        elif scope == "folder":
                            try:
                                in_scope = feed_url in folder_feed_map.get(int(scope_id), set())
                            except (ValueError, TypeError):
                                in_scope = False
                        elif scope == "feed":
                            in_scope = scope_id == feed_url
                        else:
                            in_scope = False

                        if not in_scope:
                            continue

                        with get_meta_connection() as conn:
                            result = _run_now_pattern(conn, "feed", feed_url, keyword, is_regex, search_in)
                            if "error" not in result and result.get("count", 0) > 0:
                                _log_auto_run(conn, now, rule_type, scope, scope_id, keyword, result)

                elif rule_type == "deduplicate":
                    rule_key = (scope, scope_id, keyword)
                    if rule_key in ran_dedup_keys:
                        continue

                    if scope == "global":
                        in_scope = True
                    elif scope == "folder":
                        try:
                            in_scope = bool(refreshed_feed_urls & folder_feed_map.get(int(scope_id), set()))
                        except (ValueError, TypeError):
                            in_scope = False
                    else:
                        in_scope = False  # dedup requires global or folder scope

                    if not in_scope:
                        continue

                    ran_dedup_keys.add(rule_key)
                    match_method = keyword if keyword in _DEDUP_VALID_MATCH_METHODS else "slug"
                    window_hours = max(1, int(rule.get("dedup_window_hours") or 24))
                    exclude_scope_ids = str(rule.get("exclude_scope_ids") or "")
                    with get_meta_connection() as conn:
                        result = _run_now_dedup(
                            conn, scope, scope_id, match_method, window_hours,
                            exclude_scope_ids=exclude_scope_ids,
                        )
                        if "error" not in result and result.get("count", 0) > 0:
                            _log_auto_run(conn, now, rule_type, scope, scope_id, keyword, result)
                elif rule_type == "email_article":
                    pass  # handled separately by _run_email_rules_after_refresh below
            except Exception:
                LOGGER.exception("[automation] error processing rule %s/%s/%s", rule_type, scope, keyword)

        # Email rules run after mark_as_read/dedup to avoid re-emailing articles
        # that were just auto-marked as read.
        _run_email_rules_after_refresh(refreshed_feed_urls)
    except Exception:
        LOGGER.exception("[automation] error running automation rules after refresh")


def _get_entry_excerpt(entry: object) -> str:
    """Return a short plain-text excerpt from an entry's content or summary."""
    import re as _re
    raw = ""
    content = getattr(entry, "content", None) or []
    for c in content:
        val = getattr(c, "value", None) or ""
        if val:
            raw = val
            break
    if not raw:
        raw = str(getattr(entry, "summary", None) or "")
    # Strip HTML tags
    plain = _re.sub(r"<[^>]+>", " ", raw)
    plain = " ".join(plain.split())
    return plain[:300]


def _is_local_dev_feed(feed_url: str) -> bool:
    """Return True for feeds served by Lectio itself (bypass refresh cooldown)."""
    try:
        return urlparse(feed_url).path.startswith("/dev/feeds/")
    except Exception:
        return False


def _entry_matches_rule(entry: object, keyword: str, is_regex: bool, search_in: str) -> bool:
    import re as _re
    if not keyword:
        return False
    try:
        if is_regex:
            pattern = _re.compile(keyword, _re.IGNORECASE)
            match_fn = lambda t: bool(pattern.search(t)) if t else False
        else:
            kw_lower = keyword.lower()
            match_fn = lambda t: kw_lower in (t or "").lower()
    except _re.error:
        return False

    title = str(getattr(entry, "title", None) or "")
    body = ""
    if search_in in ("body", "both"):
        for c in (getattr(entry, "content", None) or []):
            body += (getattr(c, "value", None) or "") + " "
        body += str(getattr(entry, "summary", None) or "")

    if search_in == "body":
        return match_fn(body)
    if search_in == "both":
        return match_fn(title) or match_fn(body)
    return match_fn(title)


_EMAIL_AUTO_PER_RUN_CAP = 10  # max immediate emails per refresh cycle


def _run_email_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Send or queue email_article rule matches for freshly-refreshed feeds."""
    if not is_email_configured():
        return
    if not refreshed_feed_urls:
        return

    try:
        from datetime import timedelta, timezone as _tz
        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
            folder_ids_needed = {
                int(r["scope_id"]) for r in all_rules
                if r.get("enabled") and r["scope"] == "folder"
                and str(r.get("scope_id", "")).isdigit()
            }
            folder_feed_map: dict[int, set[str]] = {
                fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed
            }

        email_rules = [
            r for r in all_rules
            if r.get("enabled") and r.get("type") == "email_article" and r.get("email_to")
        ]
        if not email_rules:
            return

        immediate_sent = 0
        now_str = datetime.now().isoformat()

        for rule in email_rules:
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                delivery = str(rule.get("delivery") or "immediately")
                email_to = str(rule.get("email_to") or "")
                batch_count = int(rule.get("batch_count") or 0)
                cc_me = bool(rule.get("cc_me"))
                # Suppress Cc when profile email is already the To recipient
                cc_addr = (
                    profile_email
                    if cc_me and profile_email and profile_email.lower() != email_to.lower()
                    else None
                )

                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}

                    for feed_url in refreshed_feed_urls:
                        # Scope check
                        if scope == "global":
                            in_scope = True
                        elif scope == "folder":
                            try:
                                in_scope = feed_url in folder_feed_map.get(int(scope_id), set())
                            except (ValueError, TypeError):
                                in_scope = False
                        elif scope == "feed":
                            in_scope = scope_id == feed_url
                        else:
                            in_scope = False
                        if not in_scope:
                            continue

                        for entry in reader.get_entries(feed=feed_url):
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            if not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue

                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    f = reader.get_feed(fu)
                                    feed_title_cache[fu] = str(getattr(f, "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu

                            article = {
                                "feed_url": fu,
                                "entry_id": str(entry.id),
                                "title": str(entry.title or ""),
                                "link": str(entry.link or ""),
                                "feed_title": feed_title_cache.get(fu, fu),
                                "excerpt": _get_entry_excerpt(entry),
                            }

                            if delivery == "immediately":
                                if immediate_sent >= _EMAIL_AUTO_PER_RUN_CAP:
                                    continue
                                ok, err = send_article_email(
                                    get_resend_api_key(), get_resend_from(), email_to,
                                    article["title"], article["feed_title"],
                                    article["link"], article["excerpt"],
                                    cc_addr=cc_addr,
                                )
                                if ok:
                                    immediate_sent += 1
                                    with get_meta_connection() as conn:
                                        _log_auto_run(conn, now_str, "email_article", scope, scope_id, keyword, {
                                            "count": 1,
                                            "entries": [article],
                                        })
                                else:
                                    LOGGER.warning("[email-auto] send failed: %s", err)
                            else:
                                # batch mode — queue for digest
                                with get_meta_connection() as conn:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO email_batch_queue"
                                        " (rule_scope, rule_scope_id, rule_keyword, queued_at,"
                                        "  feed_url, entry_id, title, link, feed_title, excerpt,"
                                        "  email_to, cc_me)"
                                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (scope, scope_id, keyword, now_str,
                                         fu, article["entry_id"], article["title"],
                                         article["link"], article["feed_title"], article["excerpt"],
                                         email_to, 1 if cc_me else 0),
                                    )
                                    # Flush immediately if batch_count threshold is reached
                                    if batch_count > 0:
                                        pending = conn.execute(
                                            "SELECT COUNT(*) FROM email_batch_queue"
                                            " WHERE rule_scope=? AND rule_scope_id=? AND rule_keyword=?"
                                            " AND email_to=?",
                                            (scope, scope_id, keyword, email_to),
                                        ).fetchone()[0]
                                        if pending >= batch_count:
                                            _flush_email_batch_for_rule(
                                                conn, scope, scope_id, keyword, email_to,
                                                cc_addr, now_str,
                                            )
            except Exception:
                LOGGER.exception("[email-auto] error processing email rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[email-auto] error in _run_email_rules_after_refresh")


def _flush_email_batch_for_rule(
    conn: "sqlite3.Connection",
    scope: str, scope_id: str, keyword: str, email_to: str,
    cc_addr: str | None, now_str: str,
) -> None:
    """Send a digest email for one rule's queued entries and clear the queue."""
    rows = conn.execute(
        "SELECT id, title, link, feed_title, excerpt, cc_me FROM email_batch_queue"
        " WHERE rule_scope=? AND rule_scope_id=? AND rule_keyword=? AND email_to=?",
        (scope, scope_id, keyword, email_to),
    ).fetchall()
    if not rows:
        return
    articles = [
        {"title": r["title"], "link": r["link"], "feed_title": r["feed_title"], "excerpt": r["excerpt"]}
        for r in rows
    ]
    use_cc = cc_addr if any(r["cc_me"] for r in rows) else None
    ok, err = send_digest_email(
        get_resend_api_key(), get_resend_from(), email_to, articles, cc_addr=use_cc,
    )
    if ok:
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM email_batch_queue WHERE id IN ({placeholders})", ids)
        _log_auto_run(conn, now_str, "email_article", scope, scope_id, keyword, {
            "count": len(articles),
            "entries": [{"feed_url": "", "entry_id": "", "title": a["title"],
                         "link": a["link"], "feed_title": a["feed_title"]} for a in articles],
        })
    else:
        LOGGER.warning("[email-auto] digest send failed for %s/%s: %s", scope, keyword, err)


def _flush_all_email_batches() -> None:
    """Flush all pending batch queues — called by daily maintenance as a safety net."""
    if not is_email_configured():
        return
    try:
        with get_meta_connection() as conn:
            profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
            groups = conn.execute(
                "SELECT DISTINCT rule_scope, rule_scope_id, rule_keyword, email_to, MAX(cc_me) as cc_me"
                " FROM email_batch_queue GROUP BY rule_scope, rule_scope_id, rule_keyword, email_to",
            ).fetchall()
        for g in groups:
            cc_addr = profile_email if g["cc_me"] and profile_email else None
            with get_meta_connection() as conn:
                _flush_email_batch_for_rule(
                    conn,
                    str(g["rule_scope"]), str(g["rule_scope_id"]),
                    str(g["rule_keyword"]), str(g["email_to"]),
                    cc_addr, datetime.now().isoformat(),
                )
    except Exception:
        LOGGER.exception("[email-auto] error flushing all email batches")


def _check_and_flush_batch_times() -> None:
    """Flush email batch queues whose batch_time matches the current local HH:MM.

    Called every minute by the maintenance loop. Each rule's batch_time is a
    "HH:MM" string (24-hour, local time); when the clock matches, the pending
    queue for that rule is flushed and an email digest is sent.
    """
    if not is_email_configured():
        return
    try:
        now_hhmm = time.strftime("%H:%M")
        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
        for rule in all_rules:
            if not (rule.get("enabled") and rule.get("delivery") == "batch"):
                continue
            bt = str(rule.get("batch_time") or "").strip()
            if bt != now_hhmm:
                continue
            email_to = str(rule.get("email_to") or "").strip()
            if not email_to:
                continue
            scope = str(rule.get("scope", ""))
            scope_id = str(rule.get("scope_id") or "")
            keyword = str(rule.get("keyword", ""))
            cc_me = bool(rule.get("cc_me"))
            cc_addr = (
                profile_email
                if cc_me and profile_email and profile_email.lower() != email_to.lower()
                else None
            )
            with get_meta_connection() as conn:
                _flush_email_batch_for_rule(
                    conn, scope, scope_id, keyword, email_to,
                    cc_addr, datetime.now().isoformat(),
                )
            LOGGER.info("[email-batch] flushed batch for %s/%s at %s", scope, keyword, now_hhmm)
    except Exception:
        LOGGER.exception("[email-batch] error in _check_and_flush_batch_times")


reader_api = ReaderApi(READER_DB_PATH)
_reader_thread_local = threading.local()


class _PersistentReaderProxy:
    """Wraps a thread-persistent Reader so the existing
    ``with get_reader() as r:`` pattern works without actually closing the
    underlying connections on exit. Reader open is expensive (schema /
    migration checks); paying it once per worker thread instead of per
    request avoids a major source of contention under load."""

    __slots__ = ("_reader",)

    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, *exc_info):
        return False

    def __getattr__(self, name):
        return getattr(self._reader, name)


def get_reader():
    proxy = getattr(_reader_thread_local, "proxy", None)
    if proxy is not None:
        return proxy
    proxy = _PersistentReaderProxy(reader_api.client())
    _reader_thread_local.proxy = proxy
    return proxy


youtube_duration_service = YouTubeDurationService(
    get_meta_connection=get_meta_connection,
    get_reader=get_reader,
    user_agent=READABILITY_USER_AGENT,
)

lead_image_service = LeadImageService(
    get_meta_connection=get_meta_connection,
    get_reader=get_reader,
    user_agent=READABILITY_USER_AGENT,
    extract_video_id=youtube_duration_service.extract_video_id,
)

# Lambda-wrapped so `sanitize_readability_html` resolves at call time (it's
# defined further down in this module).
starred_archive_service = StarredArchiveService(
    get_archive_connection=get_starred_archive_connection,
    get_meta_connection=get_meta_connection,
    get_reader=get_reader,
    user_agent=READABILITY_USER_AGENT,
    sanitize_readability_html=lambda html_text: sanitize_readability_html(html_text),
)


def normalize_tag_value(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip().lower().lstrip("#")
    if not normalized:
        return None
    if not TAG_VALUE_PATTERN.fullmatch(normalized):
        return None
    return normalized


def normalize_search_query(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def parse_manual_hashtags(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []

    tags: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,]+", raw_value):
        normalized = normalize_tag_value(token)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        tags.append(normalized)
        if len(tags) >= MAX_MANUAL_TAGS:
            break
    return tags


def _extract_tag_key(tag_record: object) -> str | None:
    if isinstance(tag_record, tuple) and tag_record:
        return str(tag_record[0])
    if isinstance(tag_record, str):
        return tag_record
    key = getattr(tag_record, "key", None)
    if key is None:
        return None
    return str(key)


def get_manual_tags_for_resource(reader, resource_id: tuple[str, str]) -> list[str]:
    tags: list[str] = []
    for tag_record in reader.get_tags(resource_id):
        key = _extract_tag_key(tag_record)
        if not key or not key.startswith(MANUAL_TAG_KEY_PREFIX):
            continue
        tag = key[len(MANUAL_TAG_KEY_PREFIX) :].strip().lower()
        if tag:
            tags.append(tag)
    return sorted(set(tags))


def set_manual_tags_for_entry(feed_url: str, entry_id: str, raw_tags: str | None) -> list[str]:
    next_tags = parse_manual_hashtags(raw_tags)

    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            return []

        resource_id = entry.resource_id
        existing_tags = get_manual_tags_for_resource(reader, resource_id)
        existing_set = set(existing_tags)
        next_set = set(next_tags)

        for removed in existing_set - next_set:
            reader.delete_tag(resource_id, f"{MANUAL_TAG_KEY_PREFIX}{removed}")

        for added in next_tags:
            if added in existing_set:
                continue
            # Use presence-only tag (no JSON value) to avoid type issues
            # with reader.set_tag's typed `value` parameter.
            reader.set_tag(resource_id, f"{MANUAL_TAG_KEY_PREFIX}{added}")

    invalidate_has_manual_tags_cache()
    return next_tags


def get_manual_tags_for_entry(feed_url: str, entry_id: str) -> list[str]:
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            return []
        return get_manual_tags_for_resource(reader, entry.resource_id)


def extract_feed_entry_tags(raw_entry: object) -> list[str]:
    values: list[str] = []

    raw_tags = getattr(raw_entry, "tags", None)
    if raw_tags:
        for raw_tag in raw_tags:
            term = getattr(raw_tag, "term", None)
            label = getattr(raw_tag, "label", None)
            scheme = getattr(raw_tag, "scheme", None)
            if isinstance(raw_tag, dict):
                term = term or raw_tag.get("term")
                label = label or raw_tag.get("label")
                scheme = scheme or raw_tag.get("scheme")
            tag_value = term or label or scheme
            if tag_value:
                values.append(str(tag_value))

    category = getattr(raw_entry, "category", None)
    if category:
        values.append(str(category))

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = " ".join(value.strip().split())
        if not compact:
            continue
        lowered = compact.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(compact)
    return cleaned


def _score_feed_entry_match(
    raw_entry: object,
    entry_id: str,
    entry_link: str | None,
    entry_title: str | None,
) -> int:
    score = 0
    candidate_id = str(getattr(raw_entry, "id", "") or "")
    candidate_link = str(getattr(raw_entry, "link", "") or "")
    candidate_title = str(getattr(raw_entry, "title", "") or "")

    if candidate_id and candidate_id == entry_id:
        score += 6
    if entry_link and candidate_link and candidate_link == entry_link:
        score += 5
    if entry_link and candidate_link and candidate_link.split("?")[0] == entry_link.split("?")[0]:
        score += 2
    if entry_title and candidate_title and candidate_title.strip() == entry_title.strip():
        score += 1

    return score


def get_feed_tag_suggestions(
    feed_url: str,
    entry_id: str,
    entry_link: str | None,
    entry_title: str | None,
) -> list[str]:
    now = time.monotonic()
    with feed_tag_suggestion_cache_lock:
        cached = feed_tag_suggestion_cache.get(feed_url)
    if cached and (now - cached[0]) < FEED_TAG_SUGGESTION_CACHE_TTL_SECONDS:
        candidate_entries = cached[1]
    else:
        # Don't block the entry-detail response on a live HTTP feed fetch.
        # Populate the cache in a background thread; return [] for this request.
        # Tag suggestions will appear on the next entry open from the same feed.
        with feed_tag_suggestion_cache_lock:
            already_fetching = feed_url in _feed_tag_fetch_in_progress
            if not already_fetching:
                _feed_tag_fetch_in_progress.add(feed_url)

        if not already_fetching:
            def _fetch_tags(url: str = feed_url) -> None:
                try:
                    with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
                        response = client.get(url)
                    response.raise_for_status()
                    parsed = feedparser.parse(response.content)
                    candidates: list[dict[str, object]] = []
                    for raw_entry in list(parsed.entries)[:120]:
                        tags = extract_feed_entry_tags(raw_entry)
                        if not tags:
                            continue
                        candidates.append({
                            "id": str(getattr(raw_entry, "id", "") or ""),
                            "link": str(getattr(raw_entry, "link", "") or ""),
                            "title": str(getattr(raw_entry, "title", "") or ""),
                            "tags": tags,
                        })
                    with feed_tag_suggestion_cache_lock:
                        feed_tag_suggestion_cache[url] = (time.monotonic(), candidates)
                except Exception:
                    pass
                finally:
                    with feed_tag_suggestion_cache_lock:
                        _feed_tag_fetch_in_progress.discard(url)

            threading.Thread(target=_fetch_tags, daemon=True).start()
        return []

    best_score = 0
    best_tags: list[str] = []
    for candidate in candidate_entries:
        score = 0
        candidate_id = str(candidate.get("id", "") or "")
        candidate_link = str(candidate.get("link", "") or "")
        candidate_title = str(candidate.get("title", "") or "")

        if candidate_id and candidate_id == entry_id:
            score += 6
        if entry_link and candidate_link and candidate_link == entry_link:
            score += 5
        if entry_link and candidate_link and candidate_link.split("?")[0] == entry_link.split("?")[0]:
            score += 2
        if entry_title and candidate_title and candidate_title.strip() == entry_title.strip():
            score += 1

        if score > best_score:
            best_score = score
            candidate_tags = candidate.get("tags", [])
            if isinstance(candidate_tags, list):
                best_tags = [str(tag) for tag in candidate_tags][:MAX_FEED_TAG_SUGGESTIONS]
            else:
                best_tags = []
            if score >= 8:
                break

    if best_score <= 0:
        return []
    return best_tags[:MAX_FEED_TAG_SUGGESTIONS]


_has_manual_tags_cache: dict[str, tuple[float, bool]] = {}
_has_manual_tags_lock = threading.Lock()
HAS_MANUAL_TAGS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_HAS_MANUAL_TAGS_CACHE_TTL", "60"))


def has_any_manual_tags() -> bool:
    """Single fast SQL probe of reader's entry_tags table to check whether
    *any* entry has a manual tag. Lets us skip the per-entry tag scan entirely
    for the common case (no manual tags). Cached for a minute; mutations can
    invalidate via invalidate_has_manual_tags_cache()."""
    now = time.time()
    with _has_manual_tags_lock:
        cached = _has_manual_tags_cache.get("any")
        if cached and now - cached[0] < HAS_MANUAL_TAGS_CACHE_TTL_SECONDS:
            return cached[1]
    try:
        conn = sqlite3.connect(str(READER_DB_PATH), timeout=2.0)
        try:
            row = conn.execute(
                "SELECT 1 FROM entry_tags WHERE key LIKE ? LIMIT 1",
                (f"{MANUAL_TAG_KEY_PREFIX}%",),
            ).fetchone()
            present = row is not None
        finally:
            conn.close()
    except Exception:
        present = True  # safe default — fall back to the slow path
    with _has_manual_tags_lock:
        _has_manual_tags_cache["any"] = (time.time(), present)
    return present


def invalidate_has_manual_tags_cache() -> None:
    with _has_manual_tags_lock:
        _has_manual_tags_cache.clear()


def get_tag_counts_for_feeds(feed_urls: set[str]) -> list[dict[str, int | str]]:
    if not feed_urls:
        return []

    # Fast path: zero manual tags exist anywhere → skip entirely.
    if not has_any_manual_tags():
        return []

    key = tuple(sorted(feed_urls))
    now = time.time()
    with tag_counts_cache_lock:
        cached = tag_counts_cache.get(key)
        if cached:
            ts, value = cached
            if now - ts < TAG_COUNTS_CACHE_TTL_SECONDS:
                return value

    # Single bulk query instead of per-entry reader.get_tags() calls.
    prefix = MANUAL_TAG_KEY_PREFIX
    sorted_feeds = sorted(feed_urls)
    placeholders = ",".join("?" * len(sorted_feeds))
    try:
        conn = sqlite3.connect(str(READER_DB_PATH), timeout=5.0)
        try:
            rows = conn.execute(
                f"SELECT key, COUNT(*) FROM entry_tags"
                f" WHERE key LIKE ? AND feed IN ({placeholders})"
                f" GROUP BY key",
                [f"{prefix}%", *sorted_feeds],
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []

    counts: dict[str, int] = {}
    for raw_key, count in rows:
        tag = raw_key[len(prefix):].strip().lower()
        if tag:
            counts[tag] = counts.get(tag, 0) + count

    result = [{"name": tag, "count": counts[tag]} for tag in sorted(counts)]
    with tag_counts_cache_lock:
        tag_counts_cache[key] = (now, result)
    return result


def get_favicon_url(feed_url: str, site_url: str | None = None) -> str | None:
    url_for_host = site_url or feed_url
    host = urlparse(url_for_host).hostname
    if not host:
        host = urlparse(feed_url).hostname
    if not host:
        return None
    return f"https://www.google.com/s2/favicons?domain={quote_plus(host)}&sz=32"


def get_feed_title_map() -> dict[str, str]:
    now = time.time()
    with feed_title_map_cache_lock:
        cached = feed_title_map_cache.get("titles")
        if cached and now - cached[0] < FEED_TITLE_MAP_CACHE_TTL_SECONDS:
            return dict(cached[1])
    titles: dict[str, str] = {}
    with get_reader() as reader:
        for feed in reader.get_feeds():
            titles[feed.url] = feed.resolved_title or feed.title or feed.url
    with feed_title_map_cache_lock:
        feed_title_map_cache["titles"] = (time.time(), dict(titles))
    return titles


def get_feed_properties(feed_url: str) -> dict:
    with get_reader() as reader:
        feed_obj = None
        for feed in reader.get_feeds():
            if feed.url == feed_url:
                feed_obj = feed
                break

        if feed_obj is None:
            return {
                "feed_url": feed_url,
                "found": False,
                "error": "Feed not found.",
            }

        total_posts = 0
        unread_posts = 0
        newest_post_dt = None
        last_received_dt = None
        for entry in reader.get_entries():
            if entry.feed_url != feed_url:
                continue
            total_posts += 1
            if not entry.read:
                unread_posts += 1

            published_dt = entry.published or entry.updated
            if published_dt and (newest_post_dt is None or published_dt > newest_post_dt):
                newest_post_dt = published_dt

            if entry.added and (last_received_dt is None or entry.added > last_received_dt):
                last_received_dt = entry.added

        last_exception = getattr(feed_obj, "last_exception", None)
        if last_exception:
            health = "error"
            health_detail = feed_refresh_service.humanize_feed_exception(last_exception)
        elif getattr(feed_obj, "updates_enabled", True):
            health = "ok"
            health_detail = "No known errors."
        else:
            health = "paused"
            health_detail = "Updates are disabled for this feed."

        img_strategy, _, img_strategy_manual = lead_image_service.get_feed_strategy(feed_url)
        _feed_domain = urlparse(feed_url).netloc.lower()
        with get_meta_connection() as _pc:
            _disp = get_feed_display_prefs(_pc, feed_url)
            _strat_rows = _pc.execute(
                "SELECT strategy, image_url, fetched_at, error, image_alt, image_title FROM feed_strategy_cache WHERE feed_url = ? ORDER BY strategy",
                (feed_url,),
            ).fetchall()
            _feed_backoff_row = _pc.execute(
                "SELECT consecutive_failures, next_retry_at FROM feed_failure_state WHERE feed_url = ?",
                (feed_url,),
            ).fetchone()
            _domain_backoff_row = _pc.execute(
                "SELECT consecutive_failures, next_retry_at FROM domain_failure_state WHERE domain = ?",
                (_feed_domain,),
            ).fetchone() if _feed_domain else None
        _strat_cache = [
            {
                "strategy": r["strategy"],
                "image_url": r["image_url"],
                "fetched_at": format_datetime_for_ui(datetime.fromtimestamp(r["fetched_at"], tz=timezone.utc)) if r["fetched_at"] else None,
                "error": r["error"],
                "image_alt": r["image_alt"],
                "image_title": r["image_title"],
            }
            for r in _strat_rows
        ]
        _folder_id_rows = _pc.execute(
            "SELECT DISTINCT folder_id FROM folder_feeds WHERE feed_url = ?",
            (feed_url,),
        ).fetchall()

        _now_ts = time.time()
        _feed_next_retry = float(_feed_backoff_row["next_retry_at"]) if _feed_backoff_row and _feed_backoff_row["next_retry_at"] else None
        _domain_next_retry = float(_domain_backoff_row["next_retry_at"]) if _domain_backoff_row and _domain_backoff_row["next_retry_at"] else None
        _feed_failures = int(_feed_backoff_row["consecutive_failures"]) if _feed_backoff_row else 0
        _domain_failures = int(_domain_backoff_row["consecutive_failures"]) if _domain_backoff_row else 0
        _effective_next_retry = max(f for f in [_feed_next_retry or 0.0, _domain_next_retry or 0.0]) or None
        _backoff_active = bool(_effective_next_retry and _effective_next_retry > _now_ts)
        _backoff_domain_driven = _backoff_active and (_domain_next_retry or 0.0) > (_feed_next_retry or 0.0)

        props = {
            "feed_url": feed_url,
            "found": True,
            "title": getattr(feed_obj, "resolved_title", None) or getattr(feed_obj, "title", None) or feed_url,
            "user_title": getattr(feed_obj, "user_title", None),
            "real_title": getattr(feed_obj, "title", None),
            "website": getattr(feed_obj, "link", None),
            "added": format_datetime_for_ui(getattr(feed_obj, "added", None)),
            "last_updated": format_datetime_for_ui(getattr(feed_obj, "last_updated", None)),
            "last_received": format_datetime_for_ui(last_received_dt),
            "last_post": format_datetime_for_ui(newest_post_dt),
            "total_posts": total_posts,
            "unread_posts": unread_posts,
            "updates_enabled": bool(getattr(feed_obj, "updates_enabled", True)),
            "health": health,
            "health_detail": health_detail,
            "image_strategy": img_strategy if img_strategy_manual else "auto",
            "image_strategy_detected": img_strategy,
            "show_lead_image_in_article": bool(_disp.get("show_lead_image_in_article", 1)),
            "show_lead_image_as_thumb": bool(_disp.get("show_lead_image_as_thumb", 1)),
            "show_image_caption": int(_disp.get("show_image_caption", -1)),
            "caption_source": _disp.get("caption_source") or "auto",
            "hide_shorts": bool(_disp.get("hide_shorts", 0)),
            "feed_thumbnail_url": _disp.get("feed_thumbnail_url") or None,
            "is_youtube_feed": "youtube.com/feeds/videos.xml" in feed_url,
            "strategy_cache": _strat_cache,
            "folder_ids": [int(r["folder_id"]) for r in _folder_id_rows],
            "backoff_active": _backoff_active,
            "backoff_domain_driven": _backoff_domain_driven,
            "backoff_domain": _feed_domain if _backoff_domain_driven else None,
            "backoff_retry_at": format_datetime_for_ui(datetime.fromtimestamp(_effective_next_retry, tz=timezone.utc)) if _effective_next_retry else None,
            "backoff_feed_failures": _feed_failures,
            "backoff_domain_failures": _domain_failures,
        }

        # If the stored title looks like a URL (often when a feed was just added),
        # try a short metadata fetch of the feed to get a human-friendly title.
        try:
            current_title = str(props.get("title") or "")
            if current_title == feed_url or current_title.startswith("http"):
                try:
                    resp = httpx.get(feed_url, timeout=3.0, follow_redirects=True)
                    if resp.status_code == 200 and resp.content:
                        parsed = feedparser.parse(resp.content)
                        fetched_title = None
                        try:
                            feed_section = getattr(parsed, "feed", None)
                            if isinstance(feed_section, dict):
                                fetched_title = feed_section.get("title")
                        except Exception:
                            fetched_title = None

                        if not fetched_title:
                            try:
                                # parsed may be a FeedParserDict-like object; cast to dict
                                parsed_dict = dict(parsed)
                                feed_obj = parsed_dict.get("feed")
                                if isinstance(feed_obj, dict):
                                    fetched_title = feed_obj.get("title")
                            except Exception:
                                fetched_title = None

                        if fetched_title:
                            fetched_title = html.unescape(str(fetched_title).strip())
                            if fetched_title and fetched_title != current_title:
                                props["title"] = fetched_title
                                if not props.get("real_title"):
                                    props["real_title"] = fetched_title
                except Exception:
                    # Don't fail the request for a metadata fetch error.
                    pass
        except Exception:
            pass

        # If there are no posts yet, and updates are enabled and there is no
        # known feed error, start a background update so the UI can show posts
        # shortly after the request. We don't block the request on the update.
        try:
            should_queue_update = total_posts == 0 and health != "error" and bool(getattr(feed_obj, "updates_enabled", True))
        except Exception:
            should_queue_update = False

        if should_queue_update:
            with updating_feeds_lock:
                already_updating = feed_url in updating_feeds
            if not already_updating:
                _start_background_update(feed_url)
                props["update_queued"] = True
            else:
                props["update_queued"] = True

        return props


def get_folder_properties(folder_id: int) -> dict:
    with get_meta_connection() as conn:
        folder_row = conn.execute(
            """
            SELECT f.id, f.name,
                CASE WHEN f.parent_id IS NULL THEN f.name
                     ELSE root.name || ' / ' || f.name END AS path
            FROM folders f
            LEFT JOIN folders root ON f.parent_id = root.id
            WHERE f.id = ?
            """,
            (folder_id,),
        ).fetchone()

        if not folder_row:
            return {"found": False, "error": "Folder not found."}

        feed_urls = get_folder_feed_urls(conn, folder_id)
        feed_count = len(feed_urls)

    if not feed_urls:
        return {
            "found": True,
            "folder_id": folder_id,
            "name": folder_row["name"],
            "path": folder_row["path"],
            "feed_count": 0,
            "total_articles": 0,
            "unread_articles": 0,
            "top_feeds": [],
        }

    total_articles = 0
    unread_articles = 0
    feed_stats: dict[str, dict] = {}

    with get_reader() as reader:
        for entry in reader.get_entries():
            if entry.feed_url not in feed_urls:
                continue
            total_articles += 1
            if not entry.read:
                unread_articles += 1
            fs = feed_stats.setdefault(entry.feed_url, {
                "title": None,
                "count": 0,
                "oldest": None,
                "newest": None,
            })
            fs["count"] += 1
            published = entry.published or entry.updated or entry.added
            if published:
                if fs["oldest"] is None or published < fs["oldest"]:
                    fs["oldest"] = published
                if fs["newest"] is None or published > fs["newest"]:
                    fs["newest"] = published

        for feed in reader.get_feeds():
            if feed.url in feed_stats:
                feed_stats[feed.url]["title"] = (
                    getattr(feed, "resolved_title", None)
                    or getattr(feed, "title", None)
                    or feed.url
                )

    now = datetime.now(tz=timezone.utc)
    top_feeds = []
    for url, fs in feed_stats.items():
        count = fs["count"]
        oldest = fs["oldest"]
        try:
            if oldest and count > 0:
                # Span from oldest article to today; floor at 1 week so
                # bulk-imported or very new feeds don't produce absurd numbers.
                span_weeks = max((now - oldest).total_seconds() / (7 * 86400), 1.0)
                avg_per_week = round(count / span_weeks, 1)
            else:
                avg_per_week = 0.0
        except Exception:
            avg_per_week = 0.0
        top_feeds.append({
            "feed_url": url,
            "title": fs["title"] or url,
            "avg_per_week": avg_per_week,
            "total": count,
        })

    top_feeds.sort(key=lambda x: x["avg_per_week"], reverse=True)

    return {
        "found": True,
        "folder_id": folder_id,
        "name": folder_row["name"],
        "path": folder_row["path"],
        "feed_count": feed_count,
        "total_articles": total_articles,
        "unread_articles": unread_articles,
        "top_feeds": top_feeds[:8],
    }


def format_datetime_for_ui(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    # A handful of feeds publish dates that are technically valid datetimes but
    # outside the range Python can convert to local time (e.g. year 1 or far
    # future). astimezone() raises OverflowError on those. Fall back to a
    # tz-naive render rather than 500'ing the whole page on one bad entry.
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone()
        except (OverflowError, ValueError):
            dt = dt.replace(tzinfo=None)
    hour = dt.hour % 12 or 12
    minute = f":{dt.minute:02d}"
    am_pm = "am" if dt.hour < 12 else "pm"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} {hour}{minute}{am_pm}"


def datetime_sort_value(dt: datetime | None) -> float:
    if dt is None:
        return float("-inf")
    try:
        if dt.tzinfo is not None:
            return dt.timestamp()
        return dt.replace(tzinfo=None).timestamp()
    except Exception:
        return float("-inf")


# Many feeds (web comics in particular) omit <pubDate> but encode the date in
# the entry URL path. Sort breaks badly without it — every undated entry maps
# to -inf and clusters at one end. This regex catches the common /YYYY/MM/DD/
# pattern; we use it only as a sort fallback so display still shows what the
# feed actually said (or '—').
_URL_PUBDATE_RE = re.compile(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$|\?)")


def url_inferred_pubdate(link: str | None) -> datetime | None:
    if not link:
        return None
    match = _URL_PUBDATE_RE.search(link)
    if not match:
        return None
    try:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    except ValueError:
        return None
    if not (2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


# Some feeds prefix entry titles with the date ("2024-01-15: …", "2024/01/15
# …"). Same idea as URL-based inference — useful as a sort fallback. ISO-ish
# only; ambiguous mm/dd/yyyy vs dd/mm/yyyy patterns intentionally ignored.
_TITLE_PUBDATE_RE = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")


def title_inferred_pubdate(title: str | None) -> datetime | None:
    if not title:
        return None
    match = _TITLE_PUBDATE_RE.match(title)
    if not match:
        return None
    try:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    except ValueError:
        return None
    if not (2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def normalize_sort_by(sort_by: str | None) -> str:
    if sort_by in {"post", "received"}:
        return sort_by
    return DEFAULT_SORT_BY


def normalize_sort_dir(sort_dir: str | None) -> str:
    if sort_dir in {"asc", "desc"}:
        return sort_dir
    return DEFAULT_SORT_DIR


def normalize_read_filter(read_filter: str | None) -> str:
    # Keep legacy values working while consolidating into all/unread/history modes.
    if read_filter in {"all", "unread", "history"}:
        return read_filter
    if read_filter == "read":
        return "history"
    if read_filter == "saved":
        return "all"
    return "all"


def normalize_resume_read_filter(read_filter: str | None) -> str:
    if read_filter in {"all", "unread"}:
        return read_filter
    return "all"


def get_next_read_filter(read_filter: str) -> str:
    return {
        "all": "unread",
        "unread": "history",
        "history": "all",
    }[normalize_read_filter(read_filter)]


def normalize_star_only(star_only: str | int | bool | None) -> bool:
    if isinstance(star_only, bool):
        return star_only
    if isinstance(star_only, int):
        return star_only != 0
    if isinstance(star_only, str):
        return star_only.strip().lower() in {"1", "true", "yes", "on"}
    return False


def build_star_only_query(star_only: str | int | bool | None) -> str:
    return "&star_only=1" if normalize_star_only(star_only) else ""


def build_resume_read_filter_query(read_filter: str | None) -> str:
    return f"&resume_read_filter={quote_plus(normalize_resume_read_filter(read_filter))}"


def upsert_entry_read_state(feed_url: str, entry_id: str, read_at: datetime | None = None) -> None:
    when = (read_at or datetime.now()).isoformat()
    with get_meta_connection() as conn:
        conn.execute(
            """
            INSERT INTO entry_read_state (feed_url, entry_id, read_at)
            VALUES (?, ?, ?)
            ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at
            """,
            (feed_url, entry_id, when),
        )


def delete_entry_read_state(feed_url: str, entry_id: str) -> None:
    with get_meta_connection() as conn:
        conn.execute(
            "DELETE FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        )


READ_HISTORY_CAP = 2_000


def append_read_history(
    feed_url: str,
    entry_id: str,
    title: str,
    link: str,
    feed_title: str,
) -> None:
    now = datetime.now().isoformat()
    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO read_history (feed_url, entry_id, title, link, feed_title, read_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET"
            "  title=excluded.title, link=excluded.link,"
            "  feed_title=excluded.feed_title, read_at=excluded.read_at",
            (feed_url, entry_id, title, link, feed_title, now),
        )
        conn.execute(
            "DELETE FROM read_history WHERE id NOT IN"
            " (SELECT id FROM read_history ORDER BY read_at DESC LIMIT ?)",
            (READ_HISTORY_CAP,),
        )


_IMG_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["\']([^"\']+)["\']')
_DIV_TAG_RE = re.compile(r'<(/?)div\b[^>]*>', re.IGNORECASE)
_AUDIO_SRC_RE = re.compile(r'<audio\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_KG_AUDIO_CARD_RE = re.compile(r'<div\b[^>]*\bkg-audio-card\b[^>]*>', re.IGNORECASE)


def _strip_div_blocks_by_class(html: str, *class_markers: str) -> str:
    """Remove every <div> block whose opening tag's class contains all of class_markers.

    Uses depth-tracking to find the matching closing </div> so nested divs are
    handled correctly.  Multiple non-overlapping matches are all stripped.
    """
    open_re = re.compile(
        r'<div\b[^>]+class=["\'][^"\']*' + r'[^"\']*'.join(re.escape(m) for m in class_markers) + r'[^"\']*["\'][^>]*>',
        re.IGNORECASE,
    )
    result: list[str] = []
    pos = 0
    for match in open_re.finditer(html):
        start = match.start()
        if start < pos:
            continue
        result.append(html[pos:start])
        depth = 0
        end = start
        for dm in _DIV_TAG_RE.finditer(html, start):
            if dm.group(1):  # closing tag
                depth -= 1
                if depth == 0:
                    end = dm.end()
                    break
            else:
                depth += 1
        pos = end if end > start else match.end()
    result.append(html[pos:])
    return "".join(result)


def _transform_kg_audio_cards(content_html: str) -> str:
    """Replace Ghost CMS kg-audio-card widgets with plain <audio controls> elements.

    Ghost's custom player relies on JS to format the duration (stored as raw seconds)
    and to wire up play/pause controls. Without the JS it renders as a broken UI with
    values like '518.82585'. This replaces the whole card div with a native audio player.
    """
    result: list[str] = []
    pos = 0
    for card_m in _KG_AUDIO_CARD_RE.finditer(content_html):
        result.append(content_html[pos : card_m.start()])
        # Walk div open/close tags from here to find the matching end tag.
        depth = 1
        end_pos = len(content_html)
        for tag_m in _DIV_TAG_RE.finditer(content_html, card_m.end()):
            if tag_m.group(1):  # closing </div>
                depth -= 1
                if depth == 0:
                    end_pos = tag_m.end()
                    break
            else:
                depth += 1
        card_html = content_html[card_m.start() : end_pos]
        src_m = _AUDIO_SRC_RE.search(card_html)
        if src_m:
            src = html.escape(src_m.group(1), quote=True)
            result.append(
                f'<audio controls preload="metadata" src="{src}" style="width:100%">'
                f'<a href="{src}">Download audio</a></audio>'
            )
        else:
            result.append(card_html)
        pos = end_pos
    result.append(content_html[pos:])
    return "".join(result)


def sanitize_readability_html(content: str) -> str:
    # Remove active content and potentially dangerous tags from extracted article HTML.
    cleaned = re.sub(r"<\s*(script|style|iframe|object|embed|form)[^>]*>.*?<\s*/\s*\1\s*>", "", content, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\son[a-zA-Z]+\s*=\s*(['\"]).*?\1", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\sjavascript:\s*", "", cleaned, flags=re.IGNORECASE)
    # Strip sizing attrs/styles from img tags so CSS fully controls image dimensions.
    cleaned = re.sub(r"(<img\b[^>]*?)\s+(?:width|height)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(<img\b[^>]*?)\s+(?:width|height)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(<img\b[^>]*?)\s+style\s*=\s*(?:\"[^\"]*\"|'[^']*')", r"\1", cleaned, flags=re.IGNORECASE)
    return cleaned


def sanitize_source_html(content: str) -> str:
    # Keep article/page structure, but remove active scriptable content.
    cleaned = re.sub(r"<\s*(script|iframe|object|embed|form|meta)[^>]*>.*?<\s*/\s*\1\s*>", "", content, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<\s*(script|iframe|object|embed|form|meta)\b[^>]*?/?>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\son[a-zA-Z]+\s*=\s*(['\"]).*?\1", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\sjavascript:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _set_or_replace_tag_attr(tag_html: str, attr_name: str, value: str) -> str:
    attr_re = re.compile(rf'\b{re.escape(attr_name)}\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)
    attr_literal = f'{attr_name}="{html.escape(value, quote=True)}"'
    if attr_re.search(tag_html):
        return attr_re.sub(attr_literal, tag_html, count=1)
    insert_at = tag_html.rfind("/>")
    if insert_at != -1:
        return f"{tag_html[:insert_at]} {attr_literal}{tag_html[insert_at:]}"
    insert_at = tag_html.rfind(">")
    if insert_at != -1:
        return f"{tag_html[:insert_at]} {attr_literal}{tag_html[insert_at:]}"
    return f"{tag_html} {attr_literal}"


def normalize_proxy_lazy_media(content: str) -> str:
    """Promote common lazy-loading attrs so media renders without site scripts."""

    def _normalize_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        attrs: dict[str, str] = {}
        for attr_match in _IMG_ATTR_RE.finditer(tag):
            key = attr_match.group(1).strip().lower()
            value = html.unescape(attr_match.group(2).strip())
            if key and value:
                attrs[key] = value

        lower_tag = tag.lower()
        is_img = lower_tag.startswith("<img")
        is_source = lower_tag.startswith("<source")
        if not (is_img or is_source):
            return tag

        lazy_src = attrs.get("data-src") or attrs.get("data-lazy-src") or attrs.get("data-original") or attrs.get("data-image")
        lazy_srcset = attrs.get("data-srcset") or attrs.get("data-lazy-srcset")
        current_src = attrs.get("src", "")
        current_srcset = attrs.get("srcset", "")

        # Common placeholders are 1x1 data URIs or empty src; replace with lazy attrs.
        placeholder_src = (not current_src) or current_src.startswith("data:")

        if is_img and lazy_src and placeholder_src:
            tag = _set_or_replace_tag_attr(tag, "src", lazy_src)

        if lazy_srcset and not current_srcset:
            tag = _set_or_replace_tag_attr(tag, "srcset", lazy_srcset)

        if is_source and lazy_src and (not current_src):
            tag = _set_or_replace_tag_attr(tag, "src", lazy_src)

        return tag

    return re.sub(r"<(?:img|source)\b[^>]*>", _normalize_tag, content, flags=re.IGNORECASE)


feed_refresh_service = FeedRefreshService(
    get_meta_connection=get_meta_connection,
    get_reader=get_reader,
    fetch_and_store_youtube_durations=youtube_duration_service.fetch_and_store_durations_for_feed,
    fetch_and_store_lead_images=lead_image_service.fetch_and_store_lead_images_for_feed,
    format_datetime_for_ui=format_datetime_for_ui,
    logger=LOGGER,
    refresh_debug_enabled=REFRESH_DEBUG_ENABLED,
    failed_feed_backoff_base_seconds=FAILED_FEED_BACKOFF_BASE_SECONDS,
    failed_feed_backoff_max_seconds=FAILED_FEED_BACKOFF_MAX_SECONDS,
)


def build_source_proxy_response(source_url: str) -> HTMLResponse:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return HTMLResponse("<h1>Unsupported URL scheme.</h1>", status_code=400)

    try:
        with httpx.Client(follow_redirects=True, timeout=12.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            response = client.get(source_url)
        response.raise_for_status()
    except Exception as exc:
        escaped_url = html.escape(source_url)
        escaped_error = html.escape(str(exc))
        return HTMLResponse(
            (
                "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Source unavailable</title>"
                "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:0;padding:1rem;line-height:1.45;}"
                "h1{font-size:1.05rem;margin:0 0 .5rem;}p{margin:.35rem 0;color:#555;}a{color:#0a5ca4;}</style></head>"
                f"<body><h1>Could not load the source page.</h1><p>{escaped_error}</p>"
                f"<p><a href='{escaped_url}' target='_blank' rel='noopener noreferrer'>Open original page</a></p></body></html>"
            ),
            status_code=200,
        )

    raw_html = response.text

    # Detect Cloudflare bot-challenge pages before sanitising (scripts are stripped,
    # making the challenge invisible and leaving a blank white page).
    _CF_MARKERS = ("challenges.cloudflare.com", "_cf_chl_opt", "cf-chl-", "Just a moment...")
    if any(m in raw_html[:4096] for m in _CF_MARKERS):
        escaped_url = html.escape(source_url)
        escaped_display = html.escape(parsed.netloc or source_url)
        return HTMLResponse(
            (
                "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Verification required</title>"
                "<style>"
                "html,body{margin:0;padding:0;height:100%;background:transparent;}"
                "body{display:flex;align-items:center;justify-content:center;"
                "font-family:Segoe UI,system-ui,Arial,sans-serif;}"
                ".wall{text-align:center;padding:2rem 1.5rem;max-width:340px;}"
                ".wall-icon{font-size:2.2rem;margin-bottom:.6rem;opacity:.6;}"
                ".wall-title{font-size:1rem;font-weight:600;margin:0 0 .4rem;}"
                ".wall-body{font-size:.85rem;color:#666;margin:0 0 1.2rem;}"
                ".wall-btn{display:inline-block;padding:.5rem 1.1rem;background:#1a73e8;"
                "color:#fff;text-decoration:none;border-radius:6px;"
                "font-size:.85rem;font-weight:500;}"
                ".wall-btn:hover{background:#1558b0;}"
                "</style></head>"
                "<body><div class='wall'>"
                "<div class='wall-icon'>🛡️</div>"
                "<div class='wall-title'>Bot verification required</div>"
                f"<div class='wall-body'>{escaped_display} uses Cloudflare bot protection "
                "which can't be passed in the embedded view.</div>"
                f"<a class='wall-btn' href='{escaped_url}' target='_blank' rel='noopener noreferrer'>"
                "Open in new tab</a>"
                "</div></body></html>"
            ),
            status_code=200,
        )

    sanitized = sanitize_source_html(raw_html)

    # Detect common paywall patterns before rendering, and return a clear
    # inline notice so the user knows to open in their browser.
    _PAYWALL_PATTERNS = re.compile(
        r"(?:"
        r"subscribe\s+to\s+(?:read|continue|access)"
        r"|subscriber[- ]only"
        r"|subscription\s+required"
        r"|create\s+a\s+(?:free\s+)?account\s+to\s+(?:read|continue)"
        r"|sign\s+in\s+to\s+(?:read|continue)"
        r"|this\s+article\s+is\s+(?:for\s+)?(?:subscribers|members)"
        r"|already\s+a\s+subscriber"
        r"|you(?:'ve|\s+have)\s+reached\s+your\s+(?:free\s+)?(?:article|story)\s+limit"
        r"|metered.paywall"
        r"|data-paywall"
        r")",
        re.IGNORECASE,
    )
    # Only check the first ~8 KB to avoid scanning whole articles.
    _probe_text = sanitized[:8192]
    if _PAYWALL_PATTERNS.search(_probe_text):
        escaped_url = html.escape(source_url)
        escaped_display = html.escape(parsed.netloc or source_url)
        return HTMLResponse(
            (
                "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Subscription required</title>"
                "<style>"
                "html,body{margin:0;padding:0;height:100%;background:transparent;}"
                "body{display:flex;align-items:center;justify-content:center;"
                "font-family:Segoe UI,system-ui,Arial,sans-serif;}"
                ".wall{text-align:center;padding:2rem 1.5rem;max-width:340px;}"
                ".wall-icon{font-size:2.2rem;margin-bottom:.6rem;opacity:.6;}"
                ".wall-title{font-size:1rem;font-weight:600;margin:0 0 .4rem;}"
                ".wall-body{font-size:.85rem;color:#666;margin:0 0 1.2rem;}"
                ".wall-btn{"
                "display:inline-block;padding:.5rem 1.1rem;background:#1a73e8;"
                "color:#fff;text-decoration:none;border-radius:6px;"
                "font-size:.85rem;font-weight:500;}"
                ".wall-btn:hover{background:#1558b0;}"
                "</style></head>"
                "<body><div class='wall'>"
                "<div class='wall-icon'>🔒</div>"
                f"<div class='wall-title'>Subscription required</div>"
                f"<div class='wall-body'>{escaped_display} requires a subscription. "
                "Open in your browser to read with your account.</div>"
                f"<a class='wall-btn' href='{escaped_url}' target='_blank' rel='noopener noreferrer'>"
                "Open in new tab</a>"
                "</div></body></html>"
            ),
            status_code=200,
        )

    sanitized = normalize_proxy_lazy_media(sanitized)
    escaped_source = html.escape(str(response.url))
    unescaped_source = str(response.url)
    proxy_style = (
        "<style>"
        "img[alt*='image unavailable' i],"
        "img[src*='grey-placeholder'],"
        "img[src*='placeholder']{display:none!important;}"
        "img[data-src],img[data-lazy-src],img[loading='lazy']{"
        "opacity:1!important;visibility:visible!important;filter:none!important;}"
        "#lectio-bar{position:fixed;bottom:0;left:0;right:0;z-index:2147483647;"
        "background:rgba(20,20,20,.82);color:#ccc;backdrop-filter:blur(4px);"
        "font:11px/1.5 system-ui,-apple-system,sans-serif;padding:5px 12px;"
        "display:flex;align-items:center;gap:8px;}"
        "#lectio-bar a{color:#7ec8f7;text-decoration:none;}"
        "#lectio-bar a:hover{text-decoration:underline;}"
        "#lectio-bar-close{margin-left:auto;cursor:pointer;opacity:.6;font-size:15px;line-height:1;padding:0 2px;}"
        "#lectio-bar-close:hover{opacity:1;}"
        "</style>"
    )
    proxy_bar = (
        f"<div id='lectio-bar'>"
        f"<span>Proxied view</span>"
        f"<span style='opacity:.4'>·</span>"
        f"<a href='{escaped_source}' target='_blank' rel='noopener noreferrer'>Open original ↗</a>"
        f"<span id='lectio-bar-close' onclick=\"document.getElementById('lectio-bar').remove()\" title='Dismiss'>×</span>"
        f"</div>"
    )

    # Ensure relative links and assets resolve against the fetched document URL.
    if "<head" in sanitized.lower():
        sanitized = re.sub(
            r"(<head\b[^>]*>)",
            r"\1" + f'<base href="{escaped_source}">{proxy_style}',
            sanitized,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        sanitized = (
            f'<!DOCTYPE html><html><head><base href="{escaped_source}">'
            f"<meta charset='utf-8'>{proxy_style}</head>"
            f"<body>{sanitized}</body></html>"
        )

    # Inject the proxy bar before </body> (or append if not found).
    if re.search(r"</body\s*>", sanitized, re.IGNORECASE):
        sanitized = re.sub(r"(</body\s*>)", proxy_bar + r"\1", sanitized, count=1, flags=re.IGNORECASE)
    else:
        sanitized += proxy_bar

    return HTMLResponse(sanitized, status_code=200)


def is_probably_frame_blocked(headers: httpx.Headers) -> tuple[bool, str]:
    xfo = (headers.get("x-frame-options") or "").lower()
    if "deny" in xfo:
        return True, "x-frame-options=deny"
    if "sameorigin" in xfo:
        return True, "x-frame-options=sameorigin"

    csp = (headers.get("content-security-policy") or "").lower()
    if "frame-ancestors" in csp:
        match = re.search(r"frame-ancestors\s+([^;]+)", csp)
        if match:
            value = match.group(1).strip()
            if "*" not in value:
                return True, f"csp frame-ancestors={value}"
    return False, ""


def probe_frameability(source_url: str) -> dict[str, object]:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return {"blocked": True, "reason": "unsupported-url-scheme"}

    try:
        with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            with client.stream("GET", source_url) as response:
                blocked, reason = is_probably_frame_blocked(response.headers)
                final_url = str(response.url)
        return {
            "blocked": blocked,
            "reason": reason,
            "final_url": final_url,
        }
    except Exception as exc:
        # If probing fails, do not force-block; UI can still attempt direct load then fallback.
        return {
            "blocked": False,
            "reason": f"probe-failed: {exc}",
            "final_url": source_url,
        }


def _bs4_content_fallback(raw_html: str) -> str:
    """Extract article content via BS4 using known content-area selectors.

    Tries selectors in priority order and returns the cleaned HTML of the first
    matching element. Used when readability fails (too short) or strips images.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup.find_all(["nav", "header", "footer", "script", "style"]):
            tag.decompose()
        for selector_type, value in [
            ("class", "post-body"),       # Blogger
            ("class", "entry-content"),   # WordPress / Blogger
            ("class", "post-content"),    # Ghost, common themes
            ("class", "article-body"),
            ("tag",   "article"),
            ("tag",   "main"),
        ]:
            if selector_type == "class":
                elem = soup.find(class_=lambda c, v=value: c and v in c.split())
            else:
                elem = soup.find(value)
            if elem:
                for nav_div in elem.find_all("div", class_=lambda c: c and "nav" in c.split()):
                    nav_div.decompose()
                return str(elem)
        return ""
    except Exception:
        return ""


def _bs4_strip_opener(content_html: str, lead_image_url: str) -> str | None:
    """Remove the lead-image opener from content HTML using BeautifulSoup.

    Regex stripping leaves orphaned closing tags when the opener is nested inside
    a container that also holds sibling elements (e.g. Tumblr npf_row with multiple
    figures). BS4 removes the image and walks up to clean empty parent containers
    without breaking the surrounding HTML structure.

    Returns the stripped HTML string (may be empty), or None if the lead image
    URL was not found in the content (caller should not strip anything).
    """
    try:
        from bs4 import BeautifulSoup, NavigableString
        soup = BeautifulSoup(content_html, "html.parser")
        target_img: object = None
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src == lead_image_url or html.unescape(src) == lead_image_url:
                target_img = img
                break
        if target_img is None:
            # Exact URL not found. Try Tumblr CDN size-variant matching:
            # URLs share https://64.media.tumblr.com/{media_hash}/{token}/ across sizes,
            # so a cached s1280x1920 lead can be matched to the s640x960 content image.
            _TUMBLR_CDN_PREFIX_RE = re.compile(
                r"^(https://64\.media\.tumblr\.com/[^/]+/[^/]+)/", re.IGNORECASE
            )
            _lead_prefix_m = _TUMBLR_CDN_PREFIX_RE.match(lead_image_url)
            if _lead_prefix_m:
                _lead_prefix = _lead_prefix_m.group(1) + "/"
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if src.startswith(_lead_prefix):
                        target_img = img
                        break
        if target_img is None:
            # Blogger CDN size-variant matching: URLs differ only in the size
            # segment before the filename.  Supports both simple size codes
            # (s320, s500, s1600) and complex crop codes (w1200-h630-p-k-no-nu).
            _BLOGGER_CDN_RE = re.compile(
                r"^(https://(?:\d+\.bp\.blogspot\.com|(?:blogger|lh\d+)\.googleusercontent\.com)/.+?)/[sw]\d+[^/]*/(.+)$",
                re.IGNORECASE,
            )
            _lead_blogger_m = _BLOGGER_CDN_RE.match(lead_image_url)
            if _lead_blogger_m:
                _lead_base = _lead_blogger_m.group(1)
                _lead_file = _lead_blogger_m.group(2)
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    _src_m = _BLOGGER_CDN_RE.match(src)
                    if _src_m and _src_m.group(1) == _lead_base and _src_m.group(2) == _lead_file:
                        target_img = img
                        break
        if target_img is None:
            return None  # Lead URL not in content — caller should clear lead_image_url
        node_to_remove = target_img
        while True:
            parent = node_to_remove.parent
            if parent is None or getattr(parent, "name", None) == "[document]":
                break
            meaningful = [
                c for c in parent.children
                if not (isinstance(c, NavigableString) and not str(c).strip())
            ]
            if len(meaningful) == 1:
                node_to_remove = parent  # Container becomes empty — hoist removal
            else:
                break  # Siblings exist — remove only the current node
        node_to_remove.decompose()
        body = soup.body
        result = body.decode_contents().strip() if body else str(soup).strip()
        return result
    except Exception:
        return None


def build_readability_response(source_url: str) -> HTMLResponse:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return HTMLResponse("<h1>Unsupported URL scheme.</h1>", status_code=400)

    try:
        with httpx.Client(follow_redirects=True, timeout=12.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            response = client.get(source_url)
        response.raise_for_status()
        raw_html = response.text
        doc = Document(raw_html)
        title = doc.short_title() or source_url
        summary = doc.summary(html_partial=True)
        article_html = sanitize_readability_html(summary).strip()
        _bs4_fallback_used = False
        if len(article_html) < 300:
            # Readability found nothing meaningful (or just a short tagline/subtitle).
            fallback = _bs4_content_fallback(raw_html)
            if fallback:
                fallback_clean = sanitize_readability_html(fallback).strip()
                if fallback_clean and len(fallback_clean) > len(article_html):
                    article_html = fallback_clean
                    _bs4_fallback_used = True
        if not _bs4_fallback_used and "<img" in raw_html:
            raw_img_count = raw_html.lower().count("<img")
            art_img_count = article_html.lower().count("<img")
            # Fall back if readability stripped all images, or if the page is
            # image-heavy (>4 imgs) and readability kept fewer than half of them.
            needs_fallback = art_img_count == 0 or (
                raw_img_count > 4 and art_img_count < raw_img_count // 2
            )
            if needs_fallback:
                fallback = _bs4_content_fallback(raw_html)
                if fallback and fallback.lower().count("<img") > art_img_count:
                    article_html = sanitize_readability_html(fallback).strip()
        if not article_html:
            raise ValueError("No readable article content was found.")
    except Exception as exc:
        escaped_url = html.escape(source_url)
        escaped_error = html.escape(str(exc))
        return HTMLResponse(
            (
                "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Readability unavailable</title>"
                "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:0;padding:1rem;line-height:1.45;}"
                "h1{font-size:1.05rem;margin:0 0 .5rem;}p{margin:.35rem 0;color:#555;}a{color:#0a5ca4;}</style></head>"
                f"<body><h1>Could not extract a readable article.</h1><p>{escaped_error}</p>"
                f"<p><a href='{escaped_url}' target='_blank' rel='noopener noreferrer'>Open original page</a></p></body></html>"
            ),
            status_code=200,
        )

    escaped_title = html.escape(title)
    escaped_source = html.escape(source_url)
    return HTMLResponse(
        (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{escaped_title}</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<style>body{margin:0;background:#f6f8fb;color:#1a2430;font-family:Georgia,serif;}"
            "main{max-width:760px;margin:0 auto;padding:1.2rem 1rem 2rem;}"
            "header{font-family:Segoe UI,Arial,sans-serif;margin-bottom:1rem;padding-bottom:.75rem;border-bottom:1px solid #d4dbe5;}"
            "h1{margin:0;font-size:1.28rem;line-height:1.3;}"
            "a{color:#0a5ca4;}article{font-size:1.05rem;line-height:1.7;}"
            "article img{max-width:100%;height:auto;max-height:240px;}article a>img{max-height:1.4em;vertical-align:middle;}"
            "article svg{width:1.2em;height:1.2em;vertical-align:middle;flex-shrink:0;}"
            "article pre{white-space:pre-wrap;}"
            "article *{color:inherit !important;background-color:transparent !important;}"
            "</style></head>"
            f"<body><main><header><h1>{escaped_title}</h1>"
            f"<a href='{escaped_source}' target='_blank' rel='noopener noreferrer'>"
            "Open original</a></header>"
            f"<article>{article_html}</article></main></body></html>"
        ),
        status_code=200,
    )


def list_entries_for_feeds(
    feed_urls: set[str],
    limit: int = 250,
    sort_by: str = "post",
    sort_dir: str = "asc",
    read_filter: str = "all",
    star_only: bool = False,
    selected_tag: str | None = None,
    search_query: str | None = None,
) -> list[dict]:
    entries: list[dict] = []
    if not feed_urls:
        return entries

    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_star_only = normalize_star_only(star_only)
    normalized_selected_tag = normalize_tag_value(selected_tag)
    normalized_search_query = normalize_search_query(search_query)
    search_terms = [token.lower() for token in normalized_search_query.split()] if normalized_search_query else []

    reader_read_filter: bool | None = None
    if not normalized_star_only:
        if normalized_read_filter == "unread":
            reader_read_filter = False
        elif normalized_read_filter == "history":
            reader_read_filter = True

    # Fetch app-managed entry metadata only for feeds in the active view.
    saved_entries_set: set[tuple[str, str]] = set()
    read_state_map: dict[tuple[str, str], datetime] = {}
    feed_url_values = tuple(feed_urls)
    placeholders = ",".join("?" for _ in feed_url_values)
    history_fast_keys: list[tuple[str, str]] = []
    with get_meta_connection() as conn:
        rows = conn.execute(
            f"SELECT feed_url, entry_id FROM saved_entries WHERE feed_url IN ({placeholders})",
            feed_url_values,
        ).fetchall()
        saved_entries_set = {(row["feed_url"], row["entry_id"]) for row in rows}
        if normalized_read_filter == "history" and not normalized_star_only:
            # Fast path: read_history table stores recent individually-read entries
            # in read order with timestamps. Avoids scanning all read entries in
            # reader's DB when the user has been using the app with this version.
            hist_rows = conn.execute(
                f"SELECT feed_url, entry_id, read_at FROM read_history"
                f" WHERE feed_url IN ({placeholders}) ORDER BY read_at DESC LIMIT ?",
                feed_url_values + (max(1, int(limit)),),
            ).fetchall()
            if hist_rows:
                for row in hist_rows:
                    history_fast_keys.append((row["feed_url"], row["entry_id"]))
                    raw_read_at = row["read_at"]
                    if raw_read_at:
                        try:
                            read_state_map[(row["feed_url"], row["entry_id"])] = datetime.fromisoformat(str(raw_read_at))
                        except Exception:
                            pass
            else:
                # Fall back to legacy entry_read_state scan (pre-read_history entries)
                read_rows = conn.execute(
                    f"SELECT feed_url, entry_id, read_at FROM entry_read_state WHERE feed_url IN ({placeholders})",
                    feed_url_values,
                ).fetchall()
                for row in read_rows:
                    raw_read_at = row["read_at"]
                    if not raw_read_at:
                        continue
                    try:
                        read_state_map[(row["feed_url"], row["entry_id"])] = datetime.fromisoformat(str(raw_read_at))
                    except Exception:
                        continue

    with get_reader() as reader:
        # Build a map of feed_url → site homepage URL so favicons use the
        # actual site domain rather than the feed host (e.g. hnrss.org).
        # Single get_feeds() call instead of one get_feed() per feed.
        fetch_start = time.perf_counter()
        feed_site_map: dict[str, str | None] = {url: None for url in feed_urls}
        for feed_obj in reader.get_feeds():
            if feed_obj.url in feed_site_map:
                feed_site_map[feed_obj.url] = getattr(feed_obj, "link", None) or None

        all_feed_entries = []
        fetch_limit = max(1, int(limit))
        need_all = bool(search_terms or normalized_sort_dir == "asc")

        if history_fast_keys:
            # Fast history path: fetch each entry by primary key (indexed lookup)
            # instead of scanning all read entries. N small lookups vs. one huge scan.
            for furl, eid in history_fast_keys:
                e = reader.get_entry((furl, eid), None)
                if e is not None:
                    all_feed_entries.append(e)
        elif normalized_sort_dir == "asc" and not search_terms and len(feed_urls) > 32:
            # ASC (oldest-first) with many feeds: reader only supports newest-first,
            # so normally we'd pull everything into Python and sort. Instead, use a
            # direct SQL query sorted ASC and fetch Entry objects only for matched rows.
            # When feed count fits within SQLite's 999-variable limit use an IN clause
            # so only the target folder's entries are scanned (avoids returning the
            # globally-oldest rows from unrelated feeds). For >999 feeds fall back to
            # a buffer-based global scan.
            read_sql = {None: "", True: " AND read IS NOT NULL", False: " AND (read IS NULL OR read != 1)"}
            read_clause = read_sql.get(reader_read_filter, "")
            try:
                _rconn = sqlite3.connect(str(READER_DB_PATH), timeout=5.0)
                _rconn.row_factory = sqlite3.Row
                if len(feed_urls) <= 999:
                    _feed_list = list(feed_urls)
                    _placeholders = ",".join("?" for _ in _feed_list)
                    rows = _rconn.execute(
                        f"SELECT feed, id FROM entries WHERE feed IN ({_placeholders}){read_clause}"
                        f" ORDER BY published ASC LIMIT ?",
                        _feed_list + [fetch_limit],
                    ).fetchall()
                else:
                    sql_limit = max(fetch_limit * 4, fetch_limit + 500)
                    rows = _rconn.execute(
                        f"SELECT feed, id FROM entries WHERE 1=1{read_clause}"
                        f" ORDER BY published ASC LIMIT ?",
                        (sql_limit,),
                    ).fetchall()
                _rconn.close()
                for row in rows:
                    if str(row["feed"]) not in feed_urls:
                        continue
                    e = reader.get_entry((str(row["feed"]), str(row["id"])), None)
                    if e is not None:
                        all_feed_entries.append(e)
                    if len(all_feed_entries) >= fetch_limit:
                        break
            except Exception:
                LOGGER.exception("[perf] asc-sql fast path failed, falling back")
                for entry in reader.get_entries(read=reader_read_filter):
                    if entry.feed_url not in feed_urls:
                        continue
                    all_feed_entries.append(entry)
        else:
            # Two strategies:
            #   - few feeds (e.g. user clicked one feed): query per feed with the
            #     SQL feed= filter. Avoids scanning every entry across the library.
            #   - many feeds (root / large folder): one global query and filter in
            #     Python. Avoids 2000+ tiny queries with their per-call overhead.
            PER_FEED_QUERY_THRESHOLD = 32
            if len(feed_urls) <= PER_FEED_QUERY_THRESHOLD:
                for feed_url in feed_urls:
                    for entry in reader.get_entries(feed=feed_url, read=reader_read_filter):
                        all_feed_entries.append(entry)
                        if not need_all and len(all_feed_entries) >= fetch_limit:
                            break
                    if not need_all and len(all_feed_entries) >= fetch_limit:
                        break
            else:
                for entry in reader.get_entries(read=reader_read_filter):
                    if entry.feed_url not in feed_urls:
                        continue
                    all_feed_entries.append(entry)
                    if not need_all and len(all_feed_entries) >= fetch_limit:
                        break

        fetch_ms = int((time.perf_counter() - fetch_start) * 1000)
        LOGGER.info(
            "[perf] list_entries: feeds=%d entries_fetched=%d fetch_ms=%d",
            len(feed_urls),
            len(all_feed_entries),
            fetch_ms,
        )

        # Two-phase processing: build a lightweight per-entry record that has
        # *only* what filter/sort/dedupe needs, then enrich the surviving top-N
        # entries with display fields (thumbnails, formatted dates, favicons,
        # YouTube duration). Avoids paying ~24ms × 2000 entries of display work
        # for entries that won't be displayed.
        process_start = time.perf_counter()
        if normalized_read_filter == "history" and not normalized_star_only:
            sort_key = "history_sort_value"
            sort_desc = True
        else:
            sort_key = "post_sort_value" if normalized_sort_by == "post" else "received_sort_value"
            sort_desc = normalized_sort_dir == "desc"

        light_records: list[dict] = []
        for entry in all_feed_entries:
            is_read = bool(entry.read)
            is_saved = (entry.feed_url, entry.id) in saved_entries_set
            manual_tags_for_record: list[str] = []
            if normalized_selected_tag:
                manual_tags_for_record = get_manual_tags_for_resource(reader, entry.resource_id)
                if normalized_selected_tag not in manual_tags_for_record:
                    continue
            if normalized_star_only and not is_saved:
                continue
            if not normalized_star_only:
                if normalized_read_filter == "unread" and is_read:
                    continue
                if normalized_read_filter == "history" and not is_read:
                    continue
            published_dt = entry.published or entry.updated or entry.added
            read_dt = read_state_map.get((entry.feed_url, entry.id))
            if read_dt is None:
                read_dt = getattr(entry, "read_modified", None)

            title_text = entry.title
            if search_terms:
                search_haystack = " ".join(
                    [
                        str(title_text or ""),
                        str(entry.feed_resolved_title or entry.feed_url or ""),
                        str(entry.link or ""),
                        str(getattr(entry, "authors_str", None) or ""),
                        str(entry.summary or ""),
                    ]
                ).lower()
                if not all(term in search_haystack for term in search_terms):
                    continue

            sort_value: float
            if sort_key == "history_sort_value":
                sort_value = datetime_sort_value(read_dt)
            elif sort_key == "post_sort_value":
                # Fall back to URL-inferred (link or id) → title-inferred →
                # received-time, so entries from feeds that don't supply
                # <pubDate> still sort in a sensible order instead of all
                # clustering at -inf. entry.link and entry.id can differ;
                # the date may live in either.
                effective_pub_dt = (
                    published_dt
                    or url_inferred_pubdate(entry.link)
                    or url_inferred_pubdate(entry.id)
                    or title_inferred_pubdate(entry.title)
                    or entry.added
                )
                sort_value = datetime_sort_value(effective_pub_dt)
            else:
                sort_value = datetime_sort_value(entry.added)

            light_records.append(
                {
                    "_entry": entry,
                    "_published_dt": published_dt,
                    "_read_dt": read_dt,
                    "_manual_tags": manual_tags_for_record,
                    "feed_url": entry.feed_url,
                    "id": entry.id,
                    "title": title_text,
                    "link": entry.link,
                    "read": is_read,
                    "saved": is_saved,
                    sort_key: sort_value,
                }
            )

    filter_ms = int((time.perf_counter() - process_start) * 1000)
    # Dedupe + sort + limit on the lightweight records.
    best_by_key: dict[str, dict] = {}
    passthrough: list[dict] = []
    for rec in light_records:
        dedupe_key = build_entry_dedupe_key(cast(str | None, rec.get("link")), cast(str | None, rec.get("title")))
        if not dedupe_key:
            passthrough.append(rec)
            continue
        existing = best_by_key.get(dedupe_key)
        if existing is None or rec[sort_key] > existing[sort_key]:
            best_by_key[dedupe_key] = rec
    light_records = passthrough + list(best_by_key.values())
    light_records.sort(key=lambda item: item[sort_key], reverse=sort_desc)
    light_records = light_records[:limit]

    enrich_start = time.perf_counter()
    with get_meta_connection() as _prefs_conn:
        _all_display_prefs = get_all_feed_display_prefs(_prefs_conn)
    # Enrich the surviving (top-N) records with display fields.
    entries = []
    for rec in light_records:
        entry = cast(object, rec.pop("_entry"))
        published_dt = rec.pop("_published_dt")
        read_dt = rec.pop("_read_dt")
        rec.pop(sort_key, None)

        feed_url_str = cast(str, rec["feed_url"])
        title_text = cast(str, rec["title"])

        duration_seconds = None
        duration_display = None
        try:
            entry_feed_url = getattr(entry, "feed_url", None)
            entry_link = getattr(entry, "link", None)
            if isinstance(entry_feed_url, str) and "youtube.com/feeds/videos.xml" in entry_feed_url and entry_link:
                vid = youtube_duration_service.extract_video_id(entry_link)
                if vid:
                    duration_seconds, duration_display = youtube_duration_service.get_cached_duration(vid)
                    if duration_display:
                        title_text = f"[{duration_display}] {title_text}"
                        rec["title"] = title_text
        except Exception:
            duration_seconds = None
            duration_display = None

        manual_tags = cast(list[str], rec.pop("_manual_tags"))

        # Rebase proxy-feed entry links (e.g. feedburner) to the real publisher host.
        if rec.get("link") and hasattr(entry, "feed"):
            _ch = getattr(entry.feed, "link", None)
            rec["link"] = _rebase_proxy_entry_link(str(rec["link"]), feed_url_str, _ch)

        _feed_prefs = _all_display_prefs.get(feed_url_str, _DISPLAY_PREF_DEFAULTS)
        _raw_thumb = lead_image_service.extract_entry_thumbnail_url(entry, include_source_lookup=False)
        _feed_thumb_setting = _feed_prefs.get("feed_thumbnail_url")
        _show_thumb = bool(_feed_prefs.get("show_lead_image_as_thumb", 1))
        if _feed_thumb_setting and _feed_thumb_setting != "__favicon__":
            _raw_thumb = str(_feed_thumb_setting)  # override per-entry
        elif _feed_thumb_setting == "__favicon__":
            _raw_thumb = None  # favicon mode treated as no-image
        _thumb = _raw_thumb if _show_thumb else None
        rec.update(
            {
                "thumbnail_url": _thumb,
                "show_thumbnail": _show_thumb,
                "feed_title": getattr(entry, "feed_resolved_title", None) or feed_url_str,
                "feed_icon_url": get_favicon_url(feed_url_str, feed_site_map.get(feed_url_str)),
                "manual_tags": manual_tags,
                "post_timestamp": published_dt.isoformat() if published_dt else None,
                "received_timestamp": getattr(entry, "added").isoformat() if getattr(entry, "added", None) else None,
                "read_timestamp": read_dt.isoformat() if read_dt else None,
                "post_display": format_datetime_for_ui(published_dt),
                "received_display": format_datetime_for_ui(getattr(entry, "added", None)),
                "read_display": format_datetime_for_ui(read_dt),
                "duration_seconds": duration_seconds,
                "duration_display": duration_display,
            }
        )
        entries.append(rec)

    enrich_ms = int((time.perf_counter() - enrich_start) * 1000)
    process_ms = int((time.perf_counter() - process_start) * 1000)
    LOGGER.info(
        "[perf] list_entries: entries_processed=%d filter_ms=%d enrich_ms=%d process_ms=%d",
        len(entries), filter_ms, enrich_ms, process_ms,
    )

    return entries


def merge_orphan_saved_entries(
    posts: list[dict],
    *,
    live_feed_urls: set[str],
    sort_by: str,
    sort_dir: str,
    limit: int,
) -> list[dict]:
    """Append archive-only saved entries (orphans), then re-sort + clip.

    Orphans are starred entries whose feed is no longer in any folder; their
    metadata comes entirely from the starred archive. Rendered alongside live
    saved entries so unsubscribing a feed doesn't make its saves disappear.
    """
    orphans = starred_archive_service.get_orphan_saved_entries(live_feed_urls)
    if not orphans:
        return posts

    sort_desc = normalize_sort_dir(sort_dir) == "desc"
    use_post = normalize_sort_by(sort_by) == "post"

    def _sort_value_from_epoch(epoch: float | None) -> str:
        # Mirrors datetime_sort_value — ISO-format string, empty for None.
        if epoch is None:
            return ""
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""

    existing_keys = {(p["feed_url"], p["id"]) for p in posts}
    sort_key = "post_sort_value" if use_post else "received_sort_value"

    additions: list[dict] = []
    for orphan in orphans:
        key = (orphan["feed_url"], orphan["id"])
        if key in existing_keys:
            continue
        post_iso = _sort_value_from_epoch(orphan.get("published_at"))
        recv_iso = _sort_value_from_epoch(orphan.get("received_at"))
        additions.append(
            {
                "feed_url": orphan["feed_url"],
                "id": orphan["id"],
                "title": orphan["title"],
                "link": orphan["link"],
                "thumbnail_url": None,
                "feed_title": orphan["feed_title"],
                "feed_icon_url": None,
                "manual_tags": [],
                "read": True,
                "saved": True,
                "post_sort_value": post_iso,
                "received_sort_value": recv_iso,
                "history_sort_value": "",
                "post_timestamp": post_iso or None,
                "received_timestamp": recv_iso or None,
                "read_timestamp": None,
                "post_display": format_datetime_for_ui(
                    datetime.fromtimestamp(orphan["published_at"], tz=timezone.utc) if orphan.get("published_at") else None
                ),
                "received_display": format_datetime_for_ui(
                    datetime.fromtimestamp(orphan["received_at"], tz=timezone.utc) if orphan.get("received_at") else None
                ),
                "read_display": None,
                "duration_seconds": None,
                "duration_display": None,
                "is_orphan_archive": True,
            }
        )

    # Re-augment existing posts with sort_value strings (list_entries_for_feeds
    # popped them before returning); use ISO strings derived from the timestamps
    # the template path keeps around.
    for p in posts:
        if "post_sort_value" not in p:
            p["post_sort_value"] = p.get("post_timestamp") or ""
        if "received_sort_value" not in p:
            p["received_sort_value"] = p.get("received_timestamp") or ""

    combined = posts + additions
    combined.sort(key=lambda item: item.get(sort_key) or "", reverse=sort_desc)
    combined = combined[:limit]

    for p in combined:
        p.pop("post_sort_value", None)
        p.pop("received_sort_value", None)
        p.pop("history_sort_value", None)

    return combined


@app.post("/internal/warm-lead-image-cache")
def internal_warm_lead_image_cache():
    """Reload the lead-image cache from the meta DB.

    Useful for forcing the running process to pick up lead images that
    were backfilled or modified on disk without restarting the server.
    """
    try:
        lead_image_service.warm_cache_from_db()
        # Expose how many entries are now cached for quick verification.
        cached = getattr(lead_image_service, "_cache", {})
        sample_keys = list(cached.keys())[:5]
        return JSONResponse({"status": "ok", "cached": len(cached), "sample": sample_keys})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


def filter_feed_urls(feed_urls: set[str], list_feed_url: str | None) -> set[str]:
    if not list_feed_url:
        return feed_urls
    if list_feed_url in feed_urls:
        return {list_feed_url}
    return set()


def _build_orphan_entry_detail(feed_url: str, entry_id: str) -> dict | None:
    """Render-shaped dict for a starred entry whose feed is gone from the reader.

    Sourced entirely from the starred archive — used as the fallback when
    `reader.get_entry()` returns None. Returns None if the archive has no
    completed row for this entry.
    """
    archived = starred_archive_service.get_archived_entry_detail(feed_url, entry_id)
    if not archived:
        return None
    asset_map = starred_archive_service.get_entry_asset_map(feed_url, entry_id)
    content_html = archived.get("content_html")
    if isinstance(content_html, str) and content_html and asset_map:
        content_html = starred_archive_service.rewrite_html_assets(
            content_html, asset_map, STARRED_ASSET_URL_PREFIX
        )

    def _fmt(epoch: float | None) -> str | None:
        if epoch is None:
            return None
        try:
            dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return format_datetime_for_ui(dt)

    def _iso(epoch: float | None) -> str | None:
        if epoch is None:
            return None
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    published_at = archived.get("published_at")
    received_at = archived.get("received_at")

    return {
        "feed_url": feed_url,
        "id": entry_id,
        "title": archived.get("title") or "",
        "link": archived.get("link") or "",
        "summary": "",
        "content_html": content_html,
        "lead_image_url": None,
        "image_title_text": None,
        "duration_seconds": None,
        "duration_display": None,
        "feed_title": archived.get("feed_title") or feed_url,
        "post_timestamp": _iso(published_at),
        "received_timestamp": _iso(received_at),
        "post_display": _fmt(published_at),
        "received_display": _fmt(received_at),
        "author": archived.get("author"),
        "read": True,
        "saved": True,
        "manual_tags": [],
        "manual_tags_text": "",
        "feed_tag_suggestions": [],
        "feed_icon_url": None,
        "is_orphan_archive": True,
    }


def _rebase_proxy_entry_link(entry_link: str | None, feed_url: str, channel_link: str | None) -> str | None:
    """Rebase an entry link that points to a proxy host (e.g. feedburner) back to the
    real publisher host stored in the feed's channel link element."""
    if not entry_link or not channel_link:
        return entry_link
    ep = urlparse(str(entry_link))
    fp = urlparse(str(feed_url))
    cp = urlparse(str(channel_link))
    if ep.netloc and ep.netloc == fp.netloc and cp.netloc and cp.netloc != fp.netloc:
        return (
            cp.scheme + "://" + cp.netloc
            + ep.path
            + (("?" + ep.query) if ep.query else "")
            + (("#" + ep.fragment) if ep.fragment else "")
        )
    return entry_link


def _lead_image_display_url(image_url: str | None) -> str | None:
    """Return the URL to use in the browser for a lead image.

    Some sites set Cross-Origin-Resource-Policy: same-site on their images,
    which modern browsers enforce for cross-origin <img> loads — the image
    appears as a broken icon even though the server returns HTTP 200.
    For such domains we route the image through our server-side proxy so the
    browser only ever sees a same-origin request.

    Results are cached in _CORP_DOMAIN_CACHE (per process, per domain) so only
    the first new domain incurs a HEAD request.
    """
    if not image_url:
        return None
    domain = urlparse(image_url).netloc
    if domain not in _CORP_DOMAIN_CACHE:
        # Optimistically return the direct URL and check CORP in the background.
        # First render may show a broken image for CORP-gated domains; subsequent
        # renders proxy correctly once the cache is warm.
        def _check_corp(url: str = image_url, d: str = domain) -> None:
            try:
                resp = httpx.head(url, follow_redirects=True, timeout=3.0, headers={"User-Agent": READABILITY_USER_AGENT})
                corp = resp.headers.get("cross-origin-resource-policy", "").strip().lower()
                _CORP_DOMAIN_CACHE[d] = corp in ("same-site", "same-origin")
            except Exception:
                _CORP_DOMAIN_CACHE[d] = False
        threading.Thread(target=_check_corp, daemon=True).start()
        return image_url
    if _CORP_DOMAIN_CACHE.get(domain):
        return f"/api/img?u={quote(image_url)}"
    return image_url


def get_entry_detail(feed_url: str, entry_id: str) -> dict | None:
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            return _build_orphan_entry_detail(feed_url, entry_id)

        published_dt = entry.published or entry.updated or entry.added
        author_name = (getattr(entry, "authors_str", None) or "").strip() or None

        content = entry.get_content(prefer_summary=False)
        content_html = None
        if content and content.value and content.is_html:
            content_html = content.value

        # Some feeds embed URL-encoded protocols in src attributes (e.g. http%3A// instead
        # of http://).  The reader library resolves these as relative paths, producing
        # URLs like https://example.com/path/http%3A/actual-host.com/image.png.
        # Recover the original URL by extracting and decoding the embedded scheme.
        if isinstance(content_html, str) and "%3A/" in content_html:
            content_html = re.sub(
                r'https?://[^"\'<\s]+/(https?)%3A/([^"\'<\s]+)',
                r'\1://\2',
                content_html,
                flags=re.IGNORECASE,
            )

        # NASA Science RSS (earthobservatory.nasa.gov) injects the full site secondary-navigation
        # into content:encoded before the article body. Strip any leading wp-block-nasa-blocks-*
        # divs by tracking div nesting depth so the article starts at actual content.
        if isinstance(content_html, str) and re.search(r'<div[^>]*\bwp-block-nasa-blocks-', content_html[:300], re.IGNORECASE):
            _stripped = content_html
            while True:
                _nm = re.match(r'\s*<div[^>]*\bwp-block-nasa-blocks-\w', _stripped, re.IGNORECASE)
                if not _nm:
                    break
                _depth = 0
                _strip_end = 0
                for _dm in re.finditer(r'<(/?)div\b[^>]*>', _stripped, re.IGNORECASE):
                    if _dm.group(1):
                        _depth -= 1
                        if _depth == 0:
                            _strip_end = _dm.end()
                            break
                    else:
                        _depth += 1
                if _strip_end > 0:
                    _stripped = _stripped[_strip_end:].lstrip()
                else:
                    break
            content_html = _stripped or None

        # MyNorthwest injects a "RELATED STORIES" sidebar block (div.related.alignright)
        # after the first paragraph. It contains external article thumbnails that
        # confuse lead-image extraction and clutter the reading view.
        if isinstance(content_html, str) and "mynorthwest.com" in feed_url and "related" in content_html:
            content_html = _strip_div_blocks_by_class(content_html, "related", "alignright")

        # Ghost CMS embeds a JS-powered audio card that renders as a broken custom player
        # without its scripts. Replace the entire kg-audio-card widget with a native
        # <audio controls> element so it works in the reader.
        if isinstance(content_html, str) and "kg-audio-card" in content_html:
            content_html = _transform_kg_audio_cards(content_html)

        # Some feeds (e.g. Introversion Blog via feedburner) sanitize <iframe> tags by
        # replacing them with the literal text "<strong>iframe</strong>" inside a
        # class="embed-container" div, leaving an adjacent plain-text YouTube link.
        # Convert these pairs into a proper YouTube embed.
        if isinstance(content_html, str) and "embed-container" in content_html and "strong" in content_html:
            def _replace_bad_iframe(m: re.Match) -> str:
                raw_url = m.group(1)
                vid = youtube_duration_service.extract_video_id(raw_url)
                if not vid:
                    return ""
                params = "?rel=0&modestbranding=0&controls=1&enablejsapi=1"
                embed_src = f"https://www.youtube.com/embed/{vid}{params}"
                return (
                    f'<div class="youtube-embed-container" style="max-width:560px;margin:1em auto;">'
                    f'<iframe width="100%" height="315" src="{embed_src}" '
                    'frameborder="0" allowfullscreen loading="lazy" '
                    'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"'
                    "></iframe></div>"
                )
            content_html = re.sub(
                r'<div[^>]*class=["\']embed-container["\'][^>]*>\s*<strong>iframe</strong>\s*</div>'
                r'\s*<a[^>]+href=["\']'
                r'(https?://(?:www\.)?(?:youtube\.com/watch\?[^"\'<\s]+|youtu\.be/[^"\'<\s]+))'
                r'["\'][^>]*>[^<]*</a>',
                _replace_bad_iframe,
                content_html,
                flags=re.IGNORECASE,
            )

        # --- YouTube embed injection ---
        # Only for YouTube feeds (feeds/videos.xml?channel_id=...)
        duration_seconds = None
        duration_display = None
        video_id = None
        if feed_url.startswith("https://www.youtube.com/feeds/videos.xml?") and entry.link:
            video_id = youtube_duration_service.extract_video_id(entry.link)
            if video_id:
                duration_seconds, duration_display = youtube_duration_service.get_cached_duration(video_id)

            # Determine base HTML to attach embed to (use existing content_html or fallback to summary)
            base_html = content_html if isinstance(content_html, str) and content_html.strip() else (entry.summary or "")

            # Only inject if not already present
            if video_id and ("youtube.com/embed/" not in str(base_html)):
                # Fixed player parameters: controls enabled, modest branding off, no related videos, enable JS API
                params = "?rel=0&modestbranding=0&controls=1&enablejsapi=1"
                embed_src = f"https://www.youtube.com/embed/{video_id}{params}"
                embed_html = (
                    f'<div class="youtube-embed-container" style="max-width:560px;margin:1em auto;">'
                    f'<iframe width="100%" height="315" src="{embed_src}" '
                    'frameborder="0" allowfullscreen loading="lazy" '
                    'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"'
                    "></iframe></div>"
                )
                # Ensure base_html is wrapped as HTML
                if not isinstance(base_html, str):
                    base_html = ""
                # If base_html is plain text (no HTML tags), linkify bare URLs
                # so they render as clickable links rather than plain text.
                if base_html and not re.search(r"<[a-z]", base_html, re.IGNORECASE):
                    def _linkify_url(m: re.Match) -> str:
                        url = m.group(0)
                        esc = html.escape(url, quote=True)
                        return f'<a href="{esc}" target="_blank" rel="noopener noreferrer">{html.escape(url)}</a>'
                    base_html = re.sub(r"https?://[^\s<>\"']+", _linkify_url, html.escape(base_html))
                content_html = embed_html + f"<div>{base_html}</div>"

        manual_tags = get_manual_tags_for_resource(reader, entry.resource_id)
        feed_tag_suggestions = get_feed_tag_suggestions(
            feed_url=entry.feed_url,
            entry_id=entry.id,
            entry_link=entry.link,
            entry_title=entry.title,
        )
        manual_tag_set = {tag.lower() for tag in manual_tags}
        feed_tag_suggestions = [tag for tag in feed_tag_suggestions if tag.strip().lower() not in manual_tag_set]

        # Check if entry is saved
        is_saved = False
        with get_meta_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            ).fetchone()
            is_saved = bool(row)

        lead_image_url = lead_image_service.extract_entry_thumbnail_url(entry, include_source_lookup=False)
        # Discard avatar/portrait images (author headshots, profile pics) that
        # some feeds embed as the first image; prefer no image over a face.
        # Check path only — CDN domains like "googleusercontent.com" contain
        # "user" and would false-positive on a full-URL search.
        if lead_image_url:
            _lead_parsed = urlparse(lead_image_url)
            if lead_image_service._AVATAR_HINT_PATTERNS.search(_lead_parsed.path):
                lead_image_url = None
        # Try source scraping when the entry has never been processed (ABSENT cache)
        # and the feed provides no inline image — covers article-only feeds where
        # the best image lives on the source page, not in the feed content.
        # (The previous Condition B — "None stored but feed now has image" — was
        # removed: it conflated auto-discovered None with user-cleared None,
        # causing the OG to pop back whenever the user cleared an entry's image.
        # The background job handles auto-retry of negative entries on its own schedule.)
        _cache_key = (str(entry.feed_url), str(entry.id))
        _cached_val = lead_image_service._cache.get(_cache_key, "ABSENT")
        _should_source_fetch = (
            _cached_val == "ABSENT"
            and lead_image_url is None
            and not lead_image_service._is_feed_none_strategy(str(entry.feed_url))
        )
        _pending_lead_image = False
        if _should_source_fetch and entry.link:
            # Don't block the request on a source-page HTTP fetch.  Queue it in
            # the background; the result is persisted and will appear next open.
            lead_image_service.queue_source_fetch(
                str(entry.feed_url), str(entry.id), entry.link
            )
            _pending_lead_image = _show_lead_in_article and not video_id
        # If we injected a YouTube embed for this entry, avoid showing a
        # separate lead image (typically the video thumbnail) above the
        # embedded player — it looks redundant and visually noisy.
        if video_id:
            lead_image_url = None
            _pending_lead_image = False

        # Remove inline images whose src URL is a logo, tracker, or avatar — these
        # are typically brand assets or analytics pixels embedded by feed publishers
        # and should not appear as article visuals in the reader.
        if isinstance(content_html, str):
            def _strip_bad_img(m: re.Match) -> str:
                src_m = re.search(r'\bsrc=(?:"([^"]*)"|\x27([^\x27]*)\x27)', m.group(0), re.IGNORECASE)
                if not src_m:
                    return m.group(0)
                src = src_m.group(1) or src_m.group(2) or ""
                _src_parsed = urlparse(src)
                # Check avatar hints against path only — CDN domains like
                # "googleusercontent.com" contain "user" and would false-positive
                # on the full URL.
                if (
                    lead_image_service._LOGO_URL_PATTERNS.search(src)
                    or lead_image_service._TRACKER_URL_PATTERNS.search(_src_parsed.netloc)
                    or lead_image_service._TRACKER_URL_PATTERNS.search(_src_parsed.path)
                    or lead_image_service._AVATAR_HINT_PATTERNS.search(_src_parsed.path)
                    or lead_image_service._SITE_CHROME_DOMAIN_PATTERNS.search(_src_parsed.netloc)
                    or lead_image_service._SITE_CHROME_PATH_PATTERNS.search(_src_parsed.path)
                ):
                    return ""
                return m.group(0)
            content_html = re.sub(r"<img\b[^>]*/?>", _strip_bad_img, content_html, flags=re.IGNORECASE) or None

        # Strip orphaned <source> elements (meaningless outside <picture>/<video>,
        # but some feeds — notably Substack — include them for WebP alternatives).
        # Safe to remove: the <img> fallback still renders the image.
        # Skip <source> elements inside <video> or <picture> — those are required.
        if isinstance(content_html, str) and "<source" in content_html.lower():
            try:
                from bs4 import BeautifulSoup as _BS4
                _src_soup = _BS4(content_html, "html.parser")
                for _src_tag in _src_soup.find_all("source"):
                    if getattr(_src_tag.parent, "name", None) not in ("video", "picture"):
                        _src_tag.decompose()
                content_html = (
                    _src_soup.body.decode_contents() if _src_soup.body else str(_src_soup)
                ).strip() or None
            except Exception:
                content_html = re.sub(r"<source\b[^>]*/?>", "", content_html, flags=re.IGNORECASE) or None

        # Steam CDN serves localized images as /hash/english.png (404s for non-English)
        # with an onerror fallback to /hash.png (200). Strip the language subfolder so
        # the image loads directly without needing the stripped onerror handler.
        if isinstance(content_html, str) and "clan.fastly.steamstatic.com" in content_html:
            content_html = re.sub(
                r"(https://clan\.fastly\.steamstatic\.com/images/\d+/[a-f0-9]{40})/[a-z]+(\.\w+)",
                r"\1\2",
                content_html,
                flags=re.IGNORECASE,
            )

        # Flat-text feeds (e.g. forum/tracker news) use <br> for line breaks with no
        # <p> structure.  When there are at least 3 <br> tags and more <br>s than <p>s,
        # promote consecutive <br> runs to paragraph breaks so the entry renders as
        # readable paragraphs instead of a wall of text.
        if isinstance(content_html, str):
            # Some feeds (e.g. Orpheus) double-encode <br> as &lt;br&gt; inside CDATA;
            # normalize those to actual <br> tags before the conversion check.
            if content_html.lower().count("&lt;br") >= 3:
                content_html = re.sub(r"&lt;br\s*/?\s*&gt;", "<br>", content_html, flags=re.IGNORECASE)
            _br_count = content_html.lower().count("<br")
            _p_count = content_html.lower().count("<p")
            if _br_count >= 3 and _br_count > _p_count:
                content_html = re.sub(
                    r"(?:<br\s*/?>\s*){2,}",
                    "</p><p>",
                    content_html,
                    flags=re.IGNORECASE,
                )
                if not content_html.startswith("<p"):
                    content_html = "<p>" + content_html
                if not content_html.rstrip().endswith("</p>"):
                    content_html = content_html + "</p>"

        # Strip Substack React UI chrome elements that render as broken/orphaned
        # widgets without Substack's CSS (expand buttons, pencraft layout divs).
        if isinstance(content_html, str) and (
            "image-link-expand" in content_html or "pencraft" in content_html
        ):
            try:
                from bs4 import BeautifulSoup
                _cs = BeautifulSoup(content_html, "html.parser")
                for _junk in _cs.select(".image-link-expand, [class*=pencraft]"):
                    _junk.decompose()
                content_html = (_cs.body.decode_contents() if _cs.body else str(_cs)).strip() or None
            except Exception:
                pass

        # Extract img title/alt text before opener stripping so content_html is intact.
        # Useful for comics where the hover text is the punchline (xkcd, etc.).
        # Checks content_html first, then entry.summary (xkcd: content is stripped away
        # but summary still has the img with title= attribute).
        #
        # We prefer a title= found on the img whose src matches lead_image_url (high
        # confidence — it IS the lead image).  A title= on a different img (e.g. a
        # story-thumbnail in Oglaf's feed while the lead is the full comic from the
        # source page) is stored as low-confidence: it shows on first open but is
        # replaced by the source-page scrape which finds the real hovertext.
        image_title_text: str | None = None
        _in_feed_title_is_lead_img = False  # True only when title= came from the lead img
        _img_tag_re_full = re.compile(r'<img\b[^>]*/?>',  re.IGNORECASE | re.DOTALL)
        _attr_extract_re = re.compile(
            r'\b(src|title)\s*=\s*(?:"([^"]*)"|\x27([^\x27]*)\x27|(\S+))',
            re.IGNORECASE,
        )
        for _search_html in [content_html, entry.summary]:
            if not isinstance(_search_html, str):
                continue
            for _tag_m in _img_tag_re_full.finditer(_search_html):
                _tag = _tag_m.group(0)
                _tag_attrs: dict[str, str] = {}
                for _am in _attr_extract_re.finditer(_tag):
                    _k = _am.group(1).lower()
                    _v = html.unescape((_am.group(2) or _am.group(3) or _am.group(4) or "").strip())
                    if _k and _v:
                        _tag_attrs[_k] = _v
                _title_val = _tag_attrs.get("title", "")
                _title_val = re.sub(r"<[^>]+>", "", html.unescape(_title_val)).strip()
                if not _title_val:
                    continue
                _src_val = _tag_attrs.get("src", "")
                _matches_lead = bool(lead_image_url and _src_val and
                                     (_src_val in lead_image_url or lead_image_url in _src_val or
                                      _src_val.split("?")[0].rstrip("/") == lead_image_url.split("?")[0].rstrip("/")))
                if _matches_lead:
                    image_title_text = _title_val
                    _in_feed_title_is_lead_img = True
                    break
                if image_title_text is None:
                    image_title_text = _title_val  # low-confidence; may be overridden below
            if _in_feed_title_is_lead_img:
                break

        # Check persisted alt text (from a previous background HTML fetch).
        # Always prefer persisted (from source-page scrape) over a low-confidence
        # in-feed title that came from a different image.
        _persisted_alt = lead_image_service.get_entry_image_alt(str(entry.feed_url), str(entry.id))
        if _persisted_alt:
            image_title_text = _persisted_alt
        elif image_title_text is None:
            pass  # stays None; source-page scrape below will attempt to fill it

        with get_meta_connection() as _prefs_conn:
            _disp = get_feed_display_prefs(_prefs_conn, str(entry.feed_url))
        _show_lead_in_article = bool(_disp.get("show_lead_image_in_article", 1))

        # Strip the opener thumbnail and dedup against the remaining content.
        # Only when we will actually display the lead image at the top — if
        # show_lead_image_in_article is off the image should stay in place.
        # Order matters: strip the leading <img> first, then check if the lead
        # image URL still appears in what remains.  This prevents the case where
        # the lead image IS the opener thumbnail (e.g. comicsthumbs) from being
        # incorrectly suppressed just because it appears at the top of content.
        _LEAD_IMG_OPENER_RE = re.compile(
            r"^\s*(?:<!--.*?-->\s*)*"  # skip leading HTML comments (e.g. Ghost kg-card-begin)
            r"(?:<p\b[^>]*>\s*(?:&nbsp;|\s)*</p>\s*)*"  # skip blank paragraphs (e.g. Blogger <p>&nbsp;</p>)
            r"(?:<(?:p|figure|div)\b[^>]*>\s*){0,3}"
            r"(?:<a\b[^>]*>\s*)?"
            r"(?:<div\b[^>]*>\s*)?"   # allow one extra wrapper div after <a> (e.g. Substack image2-inset)
            r"<img\b[^>]*/?>",
            re.IGNORECASE | re.DOTALL,
        )
        _CLOSE_A_RE = re.compile(r"</a\s*>", re.IGNORECASE)
        if _show_lead_in_article and lead_image_url and isinstance(content_html, str):
            _m = _LEAD_IMG_OPENER_RE.match(content_html)
            if _m:
                # Use BS4 for structurally-safe opener removal. Regex stripping
                # leaves orphaned closing tags when the opener is nested inside a
                # container with siblings (e.g. Tumblr npf_row with 2 figures —
                # stripping "<div><figure><img/>" leaves "</figure></div>" which
                # the browser uses to close .entry-content, pushing remaining
                # images outside the constrained container). BS4 walks up and
                # removes exactly the empty ancestor containers without touching
                # siblings.  Anchored-link text ("New comic!") is preserved
                # automatically: BS4 removes only the <img> (or its empty parent
                # chain) and leaves surrounding text + <a> intact.
                _bs4_stripped = _bs4_strip_opener(content_html, lead_image_url)
                if _bs4_stripped is not None:
                    content_html = _bs4_stripped or None
                    # Strip blank artifacts (empty paragraphs, lone <br>s, &nbsp;-only
                    # paragraphs) left at the top after the image container is removed.
                    if content_html:
                        content_html = re.sub(
                            r"^(?:\s*(?:<p\b[^>]*>\s*(?:&nbsp;\s*)*</p>|<br\s*/?>\s*))+",
                            "",
                            content_html,
                            flags=re.IGNORECASE,
                        ).strip() or None
                else:
                    # BS4 returned None — lead_image_url is not in any <img> src in
                    # the opener.  This means the opener is a *different* image (e.g.
                    # a comicsthumbs placeholder) while lead_image_url is the full-size
                    # source page image.  Fall back to raw regex strip of the opener so
                    # lead_image_url still shows at top and the thumbnail is removed.
                    # The <a>-restoration logic preserves any "New comic!" link text.
                    _matched_opener = _m.group(0)
                    content_html = content_html[_m.end():].lstrip() or None
                    _a_opener_m = re.search(r"<a\b[^>]*>", _matched_opener, re.IGNORECASE)
                    if _a_opener_m and content_html:
                        _close_m = _CLOSE_A_RE.search(content_html)
                        if _close_m:
                            _between_text = re.sub(r"<[^>]+>", "", content_html[:_close_m.start()]).strip()
                            if _between_text:
                                content_html = _a_opener_m.group(0) + content_html
                            else:
                                content_html = content_html[_close_m.end():].lstrip() or None
                if content_html and lead_image_url and (
                    lead_image_url in content_html or lead_image_url in html.unescape(content_html)
                ):
                    lead_image_url = None
            elif lead_image_url and (lead_image_url in content_html or lead_image_url in html.unescape(content_html)):
                _entry_strategy, _, _ = lead_image_service.get_feed_strategy(str(entry.feed_url))
                if _entry_strategy == "artwork":
                    # Artwork mode: the image appears after the description (e.g. ArtStation).
                    # Hoist it to the top by stripping it from its position in the content.
                    _bs4_stripped = _bs4_strip_opener(content_html, lead_image_url)
                    if _bs4_stripped is not None:
                        content_html = _bs4_stripped or None
                    else:
                        lead_image_url = None
                else:
                    # Lead URL is buried in the content (not at the opener position).
                    # The image was intentionally placed mid-article by the author —
                    # don't move it. Show it in its natural position only.
                    lead_image_url = None

        # Tumblr CDN size-variant dedup: the background job may cache a s1280x1920 lead
        # while the feed content has the same photo at s640x960.  String equality misses
        # this so the normal dedup above doesn't fire.  Check by the shared
        # {media_hash}/{token} URL prefix — if that prefix is still present in the
        # (possibly BS4-stripped) content the photo was NOT removed from the article,
        # so showing it again as a separate lead image would be a duplicate.
        if _show_lead_in_article and lead_image_url and isinstance(content_html, str):
            _tumblr_prefix_m = re.match(
                r"^(https://64\.media\.tumblr\.com/[^/]+/[^/]+)/", lead_image_url, re.IGNORECASE
            )
            if _tumblr_prefix_m:
                _tumblr_prefix = _tumblr_prefix_m.group(1) + "/"
                if _tumblr_prefix in content_html or _tumblr_prefix in html.unescape(content_html):
                    lead_image_url = None

        # If lead_image_url came from source scraping and the remaining content
        # is essentially just a thumbnail wrapper (minimal text after stripping
        # all imgs), strip the inline img tags so thumbnails don't appear below
        # the full-size lead image.
        if _show_lead_in_article and lead_image_url and isinstance(content_html, str) and lead_image_url not in content_html:
            _remaining_imgs = len(re.findall(r"<img\b", content_html, re.IGNORECASE))
            if _remaining_imgs <= 1:
                # Tumblr guard: a remaining image with a different media hash/token
                # prefix than the lead image is a genuine second photo — keep it.
                _skip_strip = False
                if _remaining_imgs == 1 and lead_image_url:
                    _tumblr_lead_m = re.match(
                        r"^(https://64\.media\.tumblr\.com/[^/]+/[^/]+)/",
                        lead_image_url, re.IGNORECASE,
                    )
                    if _tumblr_lead_m:
                        _lead_pfx = _tumblr_lead_m.group(1)
                        _rem_src_m = re.search(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', content_html, re.IGNORECASE)
                        if _rem_src_m:
                            _rem_url = html.unescape(_rem_src_m.group(1))
                            _tumblr_rem_m = re.match(
                                r"^(https://64\.media\.tumblr\.com/[^/]+/[^/]+)/",
                                _rem_url, re.IGNORECASE,
                            )
                            if _tumblr_rem_m and _tumblr_rem_m.group(1) != _lead_pfx:
                                _skip_strip = True
                if not _skip_strip:
                    _no_imgs = re.sub(r"<img\b[^>]*/?>", "", content_html, flags=re.IGNORECASE)
                    _text_only = re.sub(r"<[^>]+>", " ", _no_imgs)
                    _text_only = html.unescape(re.sub(r"\s+", " ", _text_only)).strip()
                    if len(_text_only) < 120:
                        content_html = _no_imgs.strip() or None

        # Fallback: check the alt text on the main image on the source page.
        # Covers feeds that only supply a thumbnail in the content (e.g. Wilde Life)
        # where the alt text lives on the full-size img on the article page.
        # Also runs when the in-feed title came from a different image than the lead
        # (e.g. Oglaf story thumbnail in feed vs. full comic from source page) and no
        # persisted result exists yet — source scrape may find a better hovertext.
        # If the source HTML is already in-memory (same session as the lead-image
        # fetch) run synchronously — no network cost.  Otherwise queue a background
        # fetch so the render doesn't block on a slow HTTP GET; alt text will appear
        # on the next open once the background thread stores it in the DB.
        _needs_source_scrape = (image_title_text is None or
                                (not _in_feed_title_is_lead_img and not _persisted_alt))
        if _needs_source_scrape and lead_image_url and entry.link:
            if entry.link in lead_image_service._source_html_cache:
                _fetched_alt = lead_image_service.fetch_entry_image_alt(entry.link, lead_image_url=lead_image_url)
                if _fetched_alt:
                    _fetched_alt = re.sub(r"<[^>]+>", "", _fetched_alt).strip() or None
                if _fetched_alt:
                    image_title_text = _fetched_alt
                    lead_image_service.store_entry_image_alt(str(entry.feed_url), str(entry.id), image_title_text)
                elif image_title_text is None:
                    pass  # nothing from either source; stays None
            else:
                lead_image_service.queue_source_html_fetch(
                    entry.link,
                    feed_url=str(entry.feed_url),
                    entry_id=str(entry.id),
                    lead_image_url=lead_image_url,
                )

        # Drop trivially generic alt texts that add no information (e.g. Bootstrap
        # class names used as alt values, or single-word placeholder strings).
        _TRIVIAL_ALT_TEXTS = frozenset({"responsive image", "image", "photo", "picture",
                                         "img", "thumbnail", "banner", "featured image"})
        if image_title_text and image_title_text.lower() in _TRIVIAL_ALT_TEXTS:
            image_title_text = None

        # Inject image_title_text as alt attribute on the first <img> in content_html
        # and insert a caption <p> immediately after it so it appears inline under
        # the image rather than at the bottom of the article.
        if image_title_text and not lead_image_url and isinstance(content_html, str):
            _caption_injected = False

            def _inject_alt(m: re.Match) -> str:
                nonlocal _caption_injected
                tag = m.group(0)
                if re.search(r"\balt\s*=", tag, re.IGNORECASE):
                    tag = re.sub(
                        r'(\balt\s*=\s*)(?:"[^"]*"|\x27[^\x27]*\x27)',
                        lambda a: a.group(1) + '"' + image_title_text.replace('"', "&quot;") + '"',
                        tag,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                else:
                    tag = tag[:-1] + ' alt="' + image_title_text.replace('"', "&quot;") + '"' + tag[-1]
                _caption_injected = True
                caption = f'<p class="entry-image-title-text">{html.escape(image_title_text)}</p>'
                return tag + caption

            content_html = re.sub(r"<img\b[^>]*/?>", _inject_alt, content_html, count=1, flags=re.IGNORECASE)
            if _caption_injected:
                # Caption is now inline in content_html; clear it so the template
                # doesn't also render it at the bottom of the article.
                image_title_text = None

        # SMBC: append the bonus panel image from the source page.
        if entry.link and "smbc-comics.com" in (entry.link or ""):
            _bonus_url = lead_image_service.fetch_smbc_bonus_panel_url(entry.link)
            if _bonus_url:
                _bonus_img = f'<p><img src="{html.escape(_bonus_url, quote=False)}" alt="Bonus panel" /></p>'
                content_html = (content_html or "") + _bonus_img
            # The SMBC feed wraps the comic <img> in an <a> with the text
            # "Click here to go see the bonus panel!".  After the lead-image
            # opener strip removes the <img>, that link text is left as an
            # orphaned anchor.  It's redundant because we show the panel inline.
            if isinstance(content_html, str):
                content_html = re.sub(
                    r'<a\b[^>]*>(?:\s*<br\s*/?>\s*)*Click here to go see the bonus panel!\s*</a\s*>',
                    '',
                    content_html,
                    flags=re.IGNORECASE,
                ).strip() or None

        lead_image_service.store_entry_lead_image(str(entry.feed_url), str(entry.id), lead_image_url)

        # If this entry is starred and the archive worker has captured assets,
        # swap inline image URLs to the local /starred-asset route so the
        # entry remains readable even if the source goes down.
        if is_saved:
            asset_map = starred_archive_service.get_entry_asset_map(str(entry.feed_url), str(entry.id))
            if asset_map:
                if isinstance(content_html, str) and content_html:
                    content_html = starred_archive_service.rewrite_html_assets(
                        content_html, asset_map, STARRED_ASSET_URL_PREFIX
                    )
                if lead_image_url and lead_image_url in asset_map:
                    lead_image_url = f"{STARRED_ASSET_URL_PREFIX}{asset_map[lead_image_url]}"

        if not _show_lead_in_article:
            lead_image_url = None

        # Apply caption_source preference (which raw attribute(s) to use).
        # "auto" (default) keeps whatever was computed above (title-preferred combined).
        _caption_source = str(_disp.get("caption_source") or "auto")
        _entry_feed_url = str(entry.feed_url)
        _entry_id = str(entry.id)
        if _caption_source == "none":
            image_title_text = None
        elif _caption_source == "alt":
            image_title_text = lead_image_service.get_entry_image_alt(_entry_feed_url, _entry_id)
        elif _caption_source == "title":
            image_title_text = lead_image_service.get_entry_image_title(_entry_feed_url, _entry_id)
        elif _caption_source == "both":
            _ct = lead_image_service.get_entry_image_title(_entry_feed_url, _entry_id)
            _ca = lead_image_service.get_entry_image_alt(_entry_feed_url, _entry_id)
            if _ct and _ca and _ct != _ca:
                image_title_text = f"{_ct} — {_ca}"
            else:
                image_title_text = _ct or _ca
        # else "auto": keep image_title_text as already computed

        if not should_show_caption(
            image_title_text,
            entry_title=entry.title,
            content_html=content_html,
            pref=int(_disp.get("show_image_caption", -1)),
        ):
            image_title_text = None

        _channel_link = getattr(entry.feed, "link", None) if hasattr(entry, "feed") else None
        _display_link = _rebase_proxy_entry_link(entry.link, feed_url, _channel_link)

        # Suppress summaries that consist entirely of img tags with no text (e.g. xkcd,
        # Deathbulge).  After the lead image is shown above the content, rendering the
        # raw <img> tag in a <pre> block would show it as literal HTML text.
        _summary = entry.summary
        if isinstance(_summary, str):
            _summary_no_imgs = re.sub(r"<img\b[^>]*/?>", "", _summary, flags=re.IGNORECASE).strip()
            _summary_text_only = re.sub(r"<[^>]+>", " ", _summary_no_imgs)
            _summary_text_only = html.unescape(re.sub(r"\s+", " ", _summary_text_only)).strip()
            if not _summary_text_only:
                _summary = None

        return {
            "feed_url": entry.feed_url,
            "id": entry.id,
            "title": entry.title,
            "link": _display_link,
            "summary": _summary,
            "content_html": content_html,
            "lead_image_url": _lead_image_display_url(lead_image_url),
            "show_as_thumb": bool(_disp.get("show_lead_image_as_thumb", 1)) and not _disp.get("feed_thumbnail_url"),
            "image_title_text": image_title_text,
            "duration_seconds": duration_seconds,
            "duration_display": duration_display,
            "feed_title": entry.feed_resolved_title or entry.feed_url,
            "post_timestamp": published_dt.isoformat() if published_dt else None,
            "received_timestamp": entry.added.isoformat() if entry.added else None,
            "post_display": format_datetime_for_ui(published_dt),
            "received_display": format_datetime_for_ui(entry.added),
            "author": author_name,
            "read": bool(entry.read),
            "saved": is_saved,
            "manual_tags": manual_tags,
            "manual_tags_text": " ".join(manual_tags),
            "feed_tag_suggestions": feed_tag_suggestions,
            "feed_icon_url": get_favicon_url(entry.feed_url, getattr(entry.feed, "link", None) if hasattr(entry, "feed") else None),
            "pending_lead_image": _pending_lead_image,
        }


def _get_email_to_default() -> str:
    if not is_email_configured():
        return ""
    with get_meta_connection() as conn:
        return get_setting(conn, EMAIL_TO_SETTING_KEY) or ""


@app.get("/entries/pane", response_class=HTMLResponse)
def entry_pane(
    request: Request,
    folder_id: int,
    feed_url: str,
    entry_id: str,
    list_feed_url: str | None = None,
    tag: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    read_filter: str | None = None,
    star_only: str | None = None,
    resume_read_filter: str | None = None,
):
    normalized_tag = normalize_tag_value(tag)
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_star_only = normalize_star_only(star_only)
    normalized_resume_read_filter = normalize_resume_read_filter(resume_read_filter)

    selected_entry = get_entry_detail(feed_url, entry_id)
    if selected_entry and not selected_entry["read"]:
        with get_reader() as reader:
            reader.mark_entry_as_read((feed_url, entry_id))
        try:
            upsert_entry_read_state(feed_url, entry_id)
        except Exception:
            LOGGER.warning("upsert_entry_read_state failed (db contention?); entry still marked read in reader", exc_info=True)
        with unread_counts_cache_lock:
            global _unread_counts_generation
            _unread_counts_generation += 1
            unread_counts_cache.clear()
        selected_entry["read"] = True

    # Build a tiny feed_url→folder_id map for the entry pane's feed-name link
    # so it lands in the feed's actual containing folder.
    feed_to_folder: dict[str, int] = {}
    with get_meta_connection() as conn:
        snapshot = get_meta_structure_snapshot(conn)
    direct = cast(dict[int, list[str]], snapshot["direct_feed_urls_by_folder"])
    for fid, urls in direct.items():
        for url in urls:
            feed_to_folder[url] = fid

    return templates.TemplateResponse(
        request,
        "_entry_pane.html",
        {
            "selected_folder_id": folder_id,
            "selected_feed_url": list_feed_url,
            "selected_tag": normalized_tag,
            "selected_sort_by": normalized_sort_by,
            "selected_sort_dir": normalized_sort_dir,
            "selected_read_filter": normalized_read_filter,
            "selected_star_only": normalized_star_only,
            "selected_resume_read_filter": normalized_resume_read_filter,
            "selected_entry": selected_entry,
            "feed_to_folder": feed_to_folder,
            "email_configured": is_email_configured(),
            "email_to_default": _get_email_to_default(),
            "instapaper_configured": is_instapaper_configured(),
        },
    )


def mark_feeds_as_read(feed_urls: set[str]) -> int:
    if not feed_urls:
        return 0

    to_sync: list[tuple[str, str]] = []
    with get_reader() as reader:
        for feed_url in feed_urls:
            for entry in reader.get_entries(feed=feed_url, read=False):
                reader.mark_entry_as_read((entry.feed_url, entry.id))
                to_sync.append((entry.feed_url, entry.id))

    if to_sync:
        when = datetime.now().isoformat()
        with get_meta_connection() as conn:
            conn.executemany(
                """
                INSERT INTO entry_read_state (feed_url, entry_id, read_at)
                VALUES (?, ?, ?)
                ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at
                """,
                [(fu, eid, when) for fu, eid in to_sync],
            )
    return len(to_sync)


def normalize_youtube_feed_url(feed_url: str) -> str:
    """Normalize various YouTube URLs (channel, handle, short links, video pages)
    to the canonical channel feed URL when possible.

    Returns the original URL if normalization fails or is not applicable.
    """
    try:
        parsed = urlparse(feed_url)
        host = (parsed.netloc or "").lower()

        # Already a YouTube feed
        if "youtube.com" in host and parsed.path and "feeds/videos.xml" in parsed.path:
            return feed_url

        # Direct /channel/UC... path
        m = re.search(r"/channel/(UC[0-9A-Za-z_-]+)", parsed.path or "")
        if m:
            channel_id = m.group(1)
            normalized = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            logging.info("Normalized YouTube channel URL %s -> %s", feed_url, normalized)
            return normalized

        # If it's any youtube domain (including youtu.be), try fetching page and extracting channelId
        if "youtube.com" in host or "youtu.be" in host:
            try:
                resp = httpx.get(feed_url, timeout=6.0, follow_redirects=True, headers={"User-Agent": "Lectio/1.0"})
                text = resp.text
            except Exception:
                return feed_url

            # meta itemprop channelId
            m = re.search(r'<meta\s+itemprop=["\']channelId["\']\s+content=["\'](UC[0-9A-Za-z_-]+)["\']', text)
            if not m:
                # look for JSON field "channelId":"UC..."
                m = re.search(r'"channelId"\s*[:=]\s*"(UC[0-9A-Za-z_-]+)"', text)

            if m:
                channel_id = m.group(1)
                normalized = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
                logging.info("Resolved YouTube %s -> channel id %s", feed_url, channel_id)
                return normalized
            # Fallback: look for any /channel/UC... path in the HTML (appears in some pages)
            m = re.search(r"/channel/(UC[0-9A-Za-z_-]+)", text)
            if m:
                channel_id = m.group(1)
                normalized = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
                logging.info("Resolved YouTube %s -> channel id %s (from /channel/ in HTML)", feed_url, channel_id)
                return normalized
    except Exception:
        return feed_url

    return feed_url


_FORMAT_SELECTOR_PARAMS = frozenset({"alt"})
_FORMAT_SELECTOR_VALUES = frozenset({"rss", "rss2", "atom"})


def normalize_feed_url(feed_url: str) -> str:
    """Normalize a feed URL for consistent storage and deduplication.

    - Strips trailing slashes from paths longer than "/".
    - Strips format-selector query params (e.g. Blogger's ?alt=rss) that
      select a serialization format without changing the feed content, so
      the Atom and RSS variants of the same Blogger feed are treated as one.
    - Rewrites ArtStation subdomain feeds (username.artstation.com/rss) to the
      main-domain form (www.artstation.com/username.rss) which works for all
      usernames including those with underscores that fail TLS hostname validation.
    - Other normalization (YouTube links) is handled separately.
    """
    import re as _re
    _as_m = _re.match(r'(https?)://([^.]+)\.artstation\.com/rss$', feed_url.strip(), _re.IGNORECASE)
    if _as_m:
        feed_url = f'{_as_m.group(1)}://www.artstation.com/{_as_m.group(2)}.rss'
    try:
        parsed = urlparse(feed_url)
        path = parsed.path
        if path and path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        # Strip pure format-selector params (alt=rss, alt=atom, etc.) only when
        # at least one such param is actually present — avoids re-encoding the
        # query string (which would turn e.g. ":" into "%3A" unnecessarily).
        path_changed = path != parsed.path
        if parsed.query:
            from urllib.parse import parse_qsl, urlencode
            all_pairs = parse_qsl(parsed.query, keep_blank_values=True)
            kept = [(k, v) for k, v in all_pairs
                    if not (k in _FORMAT_SELECTOR_PARAMS and v.lower() in _FORMAT_SELECTOR_VALUES)]
            if len(kept) != len(all_pairs):
                new_query = urlencode(kept)
                feed_url = parsed._replace(path=path, query=new_query).geturl()
            elif path_changed:
                feed_url = parsed._replace(path=path).geturl()
        elif path_changed:
            feed_url = parsed._replace(path=path).geturl()
    except Exception:
        pass
    return feed_url


def add_feed_to_folder(feed_url: str, folder_id: int) -> None:
    feed_url = feed_url.strip()
    if not feed_url:
        raise ValueError("Feed URL is required.")

    # Normalize YouTube-like links into canonical channel feed URLs when possible.
    try:
        feed_url = normalize_youtube_feed_url(feed_url)
    except Exception:
        # If normalization fails for any reason, proceed with the original URL.
        pass

    # Strip trailing slashes for consistent deduplication.
    feed_url = normalize_feed_url(feed_url)

    # If the slash-normalized URL isn't yet in this folder but the un-normalized
    # variant already is, reuse the existing URL to prevent duplicates.
    with get_meta_connection() as conn:
        existing = conn.execute(
            "SELECT feed_url FROM folder_feeds WHERE folder_id = ? AND (feed_url = ? OR feed_url = ?)",
            (folder_id, feed_url, feed_url + "/"),
        ).fetchone()
    if existing and existing[0] != feed_url:
        feed_url = existing[0]

    with get_reader() as reader:
        reader.add_feed(feed_url, exist_ok=True)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (folder_id, feed_url),
        )
    invalidate_meta_structure_cache()


def remove_feed_from_folder(feed_url: str, folder_id: int) -> None:
    """Remove a feed from a folder, and delete it from reader if it's in no other folder."""
    feed_url = feed_url.strip()
    if not feed_url:
        return
    with get_meta_connection() as conn:
        conn.execute(
            "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
            (folder_id, feed_url),
        )
        still_exists = conn.execute(
            "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1",
            (feed_url,),
        ).fetchone()
    if not still_exists:
        feed_id = scraper_service.scraped_feed_id_from_url(feed_url)
        with get_reader() as reader:
            if feed_id:
                with get_meta_connection() as _conn:
                    scraper_service.delete_scraped_feed(_conn, reader, feed_id)
            else:
                reader.delete_feed(feed_url, missing_ok=True)
    invalidate_meta_structure_cache()


def _run_youtube_sync(folder_id: int | None = None) -> dict:
    """Run YouTube subscription sync, creating the target folder if needed.

    If folder_id is None, looks up or creates yt_folder_name.
    Returns the result dict from sync_youtube_folder.
    """
    yt_api_key = get_yt_api_key()
    yt_channel_id = get_yt_channel_id()
    yt_folder_name = get_yt_folder_name()
    if not yt_api_key:
        return {"added": 0, "removed": 0, "total": 0, "error": "YouTube API key is not configured."}
    if not yt_channel_id:
        return {"added": 0, "removed": 0, "total": 0, "error": "YouTube channel ID is not configured."}

    # Resolve or create the target folder.
    if folder_id is None:
        with get_meta_connection() as conn:
            row = conn.execute(
                "SELECT id FROM folders WHERE name = ? LIMIT 1",
                (yt_folder_name,),
            ).fetchone()
            if row:
                folder_id = int(row["id"])
            else:
                root_id = get_root_folder_id(conn)
                conn.execute(
                    "INSERT INTO folders (name, parent_id) VALUES (?, ?)",
                    (yt_folder_name, root_id),
                )
                folder_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        invalidate_meta_structure_cache()

    def _get_folder_feed_urls(fid: int) -> list[str]:
        with get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT feed_url FROM folder_feeds WHERE folder_id = ?",
                (fid,),
            ).fetchall()
        return [str(r["feed_url"]) for r in rows]

    result = sync_youtube_folder(
        api_key=yt_api_key,
        channel_identifier=yt_channel_id,
        folder_id=folder_id,
        get_folder_feed_urls=_get_folder_feed_urls,
        add_feed=add_feed_to_folder,
        remove_feed=remove_feed_from_folder,
    )

    # Record last-sync time and summary for display in the UI.
    from datetime import datetime
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M %Z")
    if result["error"]:
        last_result = f"Error: {result['error']}"
    else:
        last_result = f"+{result['added']} / -{result['removed']} ({result['total']} subs)"
    with get_meta_connection() as conn:
        set_setting(conn, YOUTUBE_SYNC_LAST_AT_KEY, now_iso)
        set_setting(conn, YOUTUBE_SYNC_LAST_RESULT_KEY, last_result)

    return result


def move_feed_to_folder(feed_url: str, from_folder_id: int, to_folder_id: int) -> None:
    feed_url = feed_url.strip()
    if not feed_url:
        raise ValueError("Feed URL is required.")
    if from_folder_id == to_folder_id:
        return

    with get_meta_connection() as conn:
        target_row = conn.execute(
            "SELECT id FROM folders WHERE id = ?",
            (to_folder_id,),
        ).fetchone()
        if not target_row:
            raise ValueError("Target folder does not exist.")

        conn.execute(
            "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
            (from_folder_id, feed_url),
        )
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (to_folder_id, feed_url),
        )
    invalidate_meta_structure_cache()


def delete_folder(folder_id: int) -> tuple[int, int]:
    with get_meta_connection() as conn:
        root_id = get_root_folder_id(conn)
        row = conn.execute(
            "SELECT id, parent_id FROM folders WHERE id = ?",
            (folder_id,),
        ).fetchone()
        if not row:
            return (0, 0)
        if int(row["id"]) == root_id:
            raise ValueError("Cannot delete root folder.")

        descendant_ids = get_descendant_folder_ids(conn, folder_id)
        placeholders = ",".join("?" for _ in descendant_ids)

        feed_rows = conn.execute(
            f"SELECT DISTINCT feed_url FROM folder_feeds WHERE folder_id IN ({placeholders})",
            descendant_ids,
        ).fetchall()
        affected_feed_urls = [str(r["feed_url"]) for r in feed_rows]

        conn.execute(
            f"DELETE FROM folder_feeds WHERE folder_id IN ({placeholders})",
            descendant_ids,
        )
        conn.execute(
            f"DELETE FROM folders WHERE id IN ({placeholders})",
            descendant_ids,
        )

        orphaned_feed_urls: list[str] = []
        for feed_url in affected_feed_urls:
            still_used = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1",
                (feed_url,),
            ).fetchone()
            if not still_used:
                orphaned_feed_urls.append(feed_url)

    removed_feed_count = 0
    if orphaned_feed_urls:
        with get_reader() as reader:
            for feed_url in orphaned_feed_urls:
                try:
                    reader.delete_feed(feed_url, missing_ok=True)
                except Exception:
                    continue
                removed_feed_count += 1

    invalidate_meta_structure_cache()
    return (len(descendant_ids), removed_feed_count)


def _start_background_update(feed_url: str) -> None:
    with get_meta_connection() as conn:
        if feed_url in get_disabled_feed_urls(conn):
            return

    def _run() -> None:
        with updating_feeds_lock:
            if feed_url in updating_feeds:
                return
            updating_feeds.add(feed_url)
        try:
            feed_refresh_service.update_feeds([feed_url])
            _run_automation_after_refresh({feed_url})
        finally:
            with updating_feeds_lock:
                updating_feeds.discard(feed_url)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def scheduled_refresh_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(SCHEDULER_POLL_SECONDS):
        auto_refresh_minutes = getattr(app.state, "auto_refresh_minutes", 0)
        if auto_refresh_minutes <= 0:
            continue

        last_run_at = getattr(app.state, "last_scheduled_refresh_started_at", 0.0)
        if (time.monotonic() - last_run_at) < auto_refresh_minutes * 60:
            continue

        with get_meta_connection() as conn:
            feed_urls = get_all_feed_urls(conn)
            disabled = get_disabled_feed_urls(conn)
        feed_urls -= disabled
        if REFRESH_DEBUG_ENABLED:
            LOGGER.info(
                "[refresh] scheduled run triggered: interval_minutes=%d feed_count=%d",
                auto_refresh_minutes,
                len(feed_urls),
            )
        app.state.last_scheduled_refresh_started_at = time.monotonic()
        with get_meta_connection() as conn:
            scraper_service.refresh_all_scraped_feeds(conn)
        feed_refresh_service.update_feeds(feed_urls)
        _run_automation_after_refresh(feed_urls)


def _run_daily_maintenance() -> None:
    """Nightly cleanup: VACUUM DBs, prune old logs, purge orphaned rows, sync YouTube."""
    LOGGER.info("[maintenance] starting daily maintenance")

    # 1. Prune rule_run_log older than 90 days.
    try:
        cutoff = int(time.time()) - 90 * 86400
        with get_meta_connection() as conn:
            old_ids = [r[0] for r in conn.execute(
                "SELECT id FROM rule_run_log WHERE ran_at < ?", (cutoff,)
            ).fetchall()]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM rule_run_log_entries WHERE log_id IN ({placeholders})", old_ids)
                conn.execute(f"DELETE FROM rule_run_log WHERE id IN ({placeholders})", old_ids)
                LOGGER.info("[maintenance] pruned %d old rule run log entries", len(old_ids))
    except Exception:
        LOGGER.exception("[maintenance] rule log prune failed")

    # 2. Purge orphaned meta DB rows (feeds that no longer exist in the reader).
    try:
        with get_reader() as reader:
            live_urls = {f.url for f in reader.get_feeds()}
        with get_meta_connection() as conn:
            for table, col in [
                ("feed_strategy_cache", "feed_url"),
                ("feed_display_prefs", "feed_url"),
                ("feed_failure_state", "feed_url"),
            ]:
                conn.execute(
                    f"DELETE FROM {table} WHERE {col} NOT IN "
                    f"(SELECT feed_url FROM folder_feeds)",
                )
            # domain_failure_state: remove domains with no remaining feeds
            live_domains = {urlparse(u).netloc.lower() for u in live_urls}
            rows = conn.execute("SELECT domain FROM domain_failure_state").fetchall()
            stale_domains = [r["domain"] for r in rows if r["domain"] not in live_domains]
            if stale_domains:
                conn.executemany(
                    "DELETE FROM domain_failure_state WHERE domain = ?",
                    [(d,) for d in stale_domains],
                )
        LOGGER.info("[maintenance] orphaned row cleanup done")
    except Exception:
        LOGGER.exception("[maintenance] orphan cleanup failed")

    # 3. VACUUM all app-owned SQLite DBs.
    for label, path in [
        ("meta", META_DB_PATH),
        ("thumb", THUMB_DB_PATH),
    ]:
        try:
            conn = sqlite3.connect(str(path))
            conn.execute("VACUUM")
            conn.close()
            LOGGER.info("[maintenance] VACUUM %s done", label)
        except Exception:
            LOGGER.exception("[maintenance] VACUUM %s failed", label)

    # Starred archive DB (may not exist).
    try:
        archive_path = getattr(starred_archive_service, "db_path", None)
        if archive_path and Path(archive_path).exists():
            conn = sqlite3.connect(str(archive_path))
            conn.execute("VACUUM")
            conn.close()
            LOGGER.info("[maintenance] VACUUM starred-archive done")
    except Exception:
        LOGGER.exception("[maintenance] VACUUM starred-archive failed")

    # 4. Flush pending email batch queues.
    try:
        _flush_all_email_batches()
        LOGGER.info("[maintenance] email batch flush done")
    except Exception:
        LOGGER.exception("[maintenance] email batch flush failed")

    # 5. YouTube subscription sync (if configured).
    if get_yt_api_key() and get_yt_channel_id():
        try:
            result = _run_youtube_sync()
            if result.get("error"):
                LOGGER.error("[maintenance] YouTube sync error: %s", result["error"])
            else:
                LOGGER.info(
                    "[maintenance] YouTube sync complete: +%d -%d total=%d",
                    result["added"], result["removed"], result["total"],
                )
        except Exception:
            LOGGER.exception("[maintenance] YouTube sync failed")

    LOGGER.info("[maintenance] daily maintenance complete")
    with get_meta_connection() as conn:
        set_setting(conn, "maintenance_last_ran_at", time.strftime("%Y-%m-%d %H:%M %Z"))


def _daily_maintenance_loop(stop_event: threading.Event) -> None:
    """Thread that fires _run_daily_maintenance() once per day and flushes email
    batch queues at their configured batch_time each minute."""
    last_ran_date: str | None = None
    last_batch_check_hhmm: str | None = None
    while not stop_event.wait(30):
        # Flush email batches at their configured batch_time (once per clock minute).
        now_hhmm = time.strftime("%H:%M")
        if now_hhmm != last_batch_check_hhmm:
            last_batch_check_hhmm = now_hhmm
            _check_and_flush_batch_times()

        # Daily maintenance once per day at configured hour.
        maint_hour = get_maintenance_hour()
        if maint_hour is None:
            continue
        now_lt = time.localtime()
        today = time.strftime("%Y-%m-%d")
        if now_lt.tm_hour == maint_hour and last_ran_date != today:
            last_ran_date = today
            try:
                _run_daily_maintenance()
            except Exception:
                LOGGER.exception("[maintenance] unhandled error in daily maintenance")
            stop_event.wait(61)  # skip rest of the minute


def check_and_mark_manual_refresh() -> int:
    global last_manual_refresh_started_at
    with manual_refresh_lock:
        now = time.monotonic()
        elapsed = now - last_manual_refresh_started_at
        if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
            return int(MANUAL_REFRESH_COOLDOWN_SECONDS - elapsed)
        last_manual_refresh_started_at = now
        return 0


def export_opml_text(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        WITH RECURSIVE tree(id, name, parent_id, path) AS (
            SELECT id, name, parent_id, name as path
            FROM folders
            WHERE parent_id IS NULL
            UNION ALL
            SELECT f.id, f.name, f.parent_id, t.path || ' / ' || f.name
            FROM folders f
            JOIN tree t ON f.parent_id = t.id
        )
        SELECT id, name, parent_id FROM tree ORDER BY path
        """
    ).fetchall()

    folder_map = {
        int(row["id"]): {
            "name": row["name"],
            "parent_id": row["parent_id"],
            "children": [],
            "feeds": [],
        }
        for row in rows
    }

    for folder_id, data in folder_map.items():
        parent_id = data.get("parent_id")
        parent_idx = None
        if isinstance(parent_id, (int, str)):
            try:
                parent_idx = int(parent_id)
            except Exception:
                parent_idx = None
        if parent_idx is not None and parent_idx in folder_map:
            folder_map[parent_idx]["children"].append(folder_id)

    for row in conn.execute("SELECT folder_id, feed_url FROM folder_feeds ORDER BY feed_url"):
        try:
            fid = int(row["folder_id"])
        except Exception:
            continue
        folder_map[fid]["feeds"].append(str(row["feed_url"]))

    root_id = get_root_folder_id(conn)
    opml = ET.Element("opml", version="1.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "Lectio Export"
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).isoformat()
    body = ET.SubElement(opml, "body")

    def add_folder(parent: ET.Element, folder_id: int) -> None:
        data = folder_map[folder_id]
        if folder_id != root_id:
            node = ET.SubElement(parent, "outline", text=str(data.get("name", "")), title=str(data.get("name", "")))
        else:
            node = parent

        for feed_url in data["feeds"]:
            ET.SubElement(
                node,
                "outline",
                type="rss",
                text=feed_url,
                title=feed_url,
                xmlUrl=feed_url,
            )

        for child_id in sorted(data["children"], key=lambda cid: str(folder_map[cid].get("name", "")).lower()):
            add_folder(node, child_id)

    add_folder(body, root_id)
    return ET.tostring(opml, encoding="utf-8", xml_declaration=True).decode("utf-8")


def import_opml(conn: sqlite3.Connection, opml_data: bytes) -> int:
    root = ET.fromstring(opml_data)
    body = root.find("body")
    if body is None:
        return 0

    root_id = get_root_folder_id(conn)
    imported = 0

    def ensure_folder(name: str, parent_id: int) -> int:
        name = name.strip() or "Untitled"
        row = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
            (name, parent_id),
        ).fetchone()
        if row:
            return int(row["id"])

        cursor = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES (?, ?)",
            (name, parent_id),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Could not determine id for inserted folder.")
        return int(cursor.lastrowid)

    # Track feeds already assigned to a folder (including existing subscriptions)
    feeds_with_folder = set(row["feed_url"] for row in conn.execute("SELECT feed_url FROM folder_feeds"))

    with get_reader() as reader:

        def walk(outline: ET.Element, target_folder_id: int, may_create_folder: bool) -> None:
            nonlocal imported
            feed_url = outline.attrib.get("xmlUrl")
            if feed_url:
                feed_url = feed_url.strip()
                if feed_url:
                    if feed_url in feeds_with_folder:
                        # Already assigned to a folder, skip
                        return
                    try:
                        reader.add_feed(feed_url, exist_ok=True)
                    except InvalidFeedURLError:
                        LOGGER.warning("OPML import: skipping non-URL entry %r", feed_url)
                        return
                    conn.execute(
                        "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                        (target_folder_id, feed_url),
                    )
                    feeds_with_folder.add(feed_url)
                    imported += 1
                return

            folder_name = outline.attrib.get("text") or outline.attrib.get("title")
            folder_id = target_folder_id
            if may_create_folder and folder_name:
                # Lectio keeps a single folder level below the root.
                folder_id = ensure_folder(folder_name, root_id)

            for child in outline.findall("outline"):
                walk(child, folder_id, False)

        for outline in body.findall("outline"):
            walk(outline, root_id, True)

    return imported


# Startup/shutdown handled by the lifespan() context manager above.


@app.get("/entries/lead-image")
def entry_lead_image_status(feed_url: str, entry_id: str):
    """Lightweight polling endpoint for background lead-image fetch status.

    Returns {"status": "pending"|"none"|"ready", "url": str|null}.
    """
    key = (feed_url, entry_id)
    cached = lead_image_service._cache.get(key, "ABSENT")
    in_progress = key in lead_image_service._source_fetch_in_progress
    if cached != "ABSENT" and cached is not None:
        display_url = _lead_image_display_url(cached)
        return JSONResponse({"status": "ready", "url": display_url})
    if in_progress:
        return JSONResponse({"status": "pending", "url": None})
    return JSONResponse({"status": "none", "url": None})


@app.get("/entries/readability")
def entry_readability(
    url: str,
    feed_url: str | None = Query(default=None),
    entry_id: str | None = Query(default=None),
):
    # If this entry is starred and a complete archive exists, serve the
    # archived readability HTML so the view stays available even if the
    # source is gone. Otherwise fall through to the live extractor.
    if feed_url and entry_id:
        archived_html = starred_archive_service.get_archived_readability_html(feed_url, entry_id)
        if archived_html:
            asset_map = starred_archive_service.get_entry_asset_map(feed_url, entry_id)
            if asset_map:
                archived_html = starred_archive_service.rewrite_html_assets(
                    archived_html, asset_map, STARRED_ASSET_URL_PREFIX
                )
            return _wrap_readability_html(archived_html, url)
    return build_readability_response(url)


def _wrap_readability_html(article_html: str, source_url: str) -> HTMLResponse:
    escaped_source = html.escape(source_url)
    return HTMLResponse(
        (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Reader view</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<style>body{margin:0;background:#f6f8fb;color:#1a2430;font-family:Georgia,serif;}"
            "main{max-width:760px;margin:0 auto;padding:1.2rem 1rem 2rem;}"
            "header{font-family:Segoe UI,Arial,sans-serif;margin-bottom:1rem;padding-bottom:.75rem;border-bottom:1px solid #d4dbe5;}"
            "a{color:#0a5ca4;}article{font-size:1.05rem;line-height:1.7;}"
            "article img{max-width:100%;height:auto;max-height:240px;}article a>img{max-height:1.4em;vertical-align:middle;}"
            "article svg{width:1.2em;height:1.2em;vertical-align:middle;flex-shrink:0;}"
            "article pre{white-space:pre-wrap;}"
            "article *{color:inherit !important;background-color:transparent !important;}"
            "</style></head>"
            f"<body><main><header>"
            f"<a href='{escaped_source}' target='_blank' rel='noopener noreferrer'>Open original</a>"
            "</header>"
            f"<article>{article_html}</article></main></body></html>"
        ),
        status_code=200,
    )


@app.get("/entries/source")
def entry_source(url: str):
    return build_source_proxy_response(url)


@app.get("/entries/frame-check")
def entry_frame_check(url: str):
    return JSONResponse(probe_frameability(url))


# ---------------------------------------------------------------------------
# Auth routes (/login, /logout)
# These are only active when LECTIO_USERNAME + LECTIO_PASSWORD are set.
# ---------------------------------------------------------------------------


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if not AUTH_ENABLED or request.session.get("authenticated"):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": None, "static_asset_version": STATIC_ASSET_VERSION},
    )


_LOGIN_RATE_LIMIT_MAX = int(os.getenv("LECTIO_LOGIN_MAX_FAILURES", "5"))
_LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LECTIO_LOGIN_WINDOW_SECONDS", "300"))
_login_failures: dict[str, list[float]] = {}
_login_failures_lock = threading.Lock()


def _client_ip_for_rate_limit(request: Request) -> str:
    """Best-effort client identifier for rate limiting.

    Uses request.client.host directly. When behind Traefik with
    LECTIO_HTTPS_ONLY=1, ProxyHeadersMiddleware rewrites client.host to the
    real client IP via X-Forwarded-For — see app.add_middleware setup above.
    """
    return request.client.host if request.client else "unknown"


def _login_attempt_blocked(ip: str, now: float) -> bool:
    if DEBUG_MODE:
        return False
    cutoff = now - _LOGIN_RATE_LIMIT_WINDOW_SECONDS
    with _login_failures_lock:
        timestamps = [t for t in _login_failures.get(ip, []) if t >= cutoff]
        _login_failures[ip] = timestamps
        return len(timestamps) >= _LOGIN_RATE_LIMIT_MAX


def _record_login_failure(ip: str, now: float) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(ip, []).append(now)


def _clear_login_failures(ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(ip, None)


@app.post("/login")
async def login_submit(request: Request, next: str = "/"):
    now = time.time()
    ip = _client_ip_for_rate_limit(request)
    if _login_attempt_blocked(ip, now):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next,
                "error": f"Too many failed login attempts. Try again in {_LOGIN_RATE_LIMIT_WINDOW_SECONDS // 60} minutes.",
                "static_asset_version": STATIC_ASSET_VERSION,
            },
            status_code=429,
        )

    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    if AUTH_ENABLED and secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(password, AUTH_PASSWORD):
        _clear_login_failures(ip)
        request.session["authenticated"] = True
        return RedirectResponse(url=next or "/", status_code=303)
    _record_login_failure(ip, now)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": "Invalid username or password.", "static_asset_version": STATIC_ASSET_VERSION},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/")
def home(
    request: Request,
    folder_id: int | None = None,
    list_feed_url: str | None = None,
    tag: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    read_filter: str | None = None,
    star_only: str | None = None,
    resume_read_filter: str | None = None,
    feed_url: str | None = None,
    entry_id: str | None = None,
    q: str | None = None,
    message: str | None = None,
    no_rss_url: str | None = None,
    chunk: int | None = None,
    chunk_delta: str | None = None,
):
    # Allow client to suggest a preferred read_filter via header for SPA/AJAX calls.
    # Use it only when no explicit `read_filter` was supplied in the URL/form.
    # Explicit query/form state must win to avoid client preference overriding
    # an intentional filter selection during navigation.
    header_rf = request.headers.get("X-Lectio-Read-Filter")
    if read_filter is None and header_rf:
        read_filter = header_rf

    # Only use `resume_read_filter` as a fallback when no explicit
    # `read_filter` was supplied.
    if read_filter is None and resume_read_filter is not None:
        read_filter = resume_read_filter

    # If still no explicit/read-resume value, fall back to cookie-based
    # persisted preference for full navigations.
    if read_filter is None:
        cookie_rf = request.cookies.get("lectio_read_filter")
        if cookie_rf:
            read_filter = cookie_rf

    start_req = time.perf_counter()
    _t = time.perf_counter()
    def _tick(label: str) -> None:
        nonlocal _t
        ms = int((time.perf_counter() - _t) * 1000)
        if ms >= 50:
            LOGGER.info("[perf]   meta.%s=%dms", label, ms)
        _t = time.perf_counter()

    with get_meta_connection() as conn:
        _tick("connect")
        preferred_sort_by = normalize_sort_by(get_setting(conn, SORT_BY_SETTING_KEY))
        preferred_sort_dir = normalize_sort_dir(get_setting(conn, SORT_DIR_SETTING_KEY))
        problematic_feeds_last_viewed_at = parse_epoch_setting(get_setting(conn, PROBLEMATIC_FEEDS_LAST_VIEWED_AT_SETTING_KEY))
        selected_sort_by = normalize_sort_by(sort_by or preferred_sort_by)
        selected_sort_dir = normalize_sort_dir(sort_dir or preferred_sort_dir)
        set_setting(conn, SORT_BY_SETTING_KEY, selected_sort_by)
        set_setting(conn, SORT_DIR_SETTING_KEY, selected_sort_dir)
        _tick("settings")

        snapshot = get_meta_structure_snapshot(conn)
        raw_folder_rows = cast(list[dict], snapshot["raw_folder_rows"])
        direct_feed_urls_by_folder = cast(dict[int, list[str]], snapshot["direct_feed_urls_by_folder"])
        folder_options = cast(list[FolderOption], snapshot["folder_options"])
        all_feed_urls = cast(set[str], snapshot["all_feed_urls"])
        root_id = cast(int, snapshot["root_id"])
        folder_feed_urls_by_id = cast(dict[int, set[str]], snapshot["folder_feed_urls_by_id"])
        selected_folder_id = folder_id or root_id
        _tick("structure_snapshot")

        unread_counts_by_feed = get_unread_counts_by_feed()
        _tick("unread_counts")
        disabled_feed_urls = get_disabled_feed_urls(conn)
        # Exclude disabled feeds from unread counts so folder badges stay clean.
        active_unread_counts_by_feed = {
            url: count for url, count in unread_counts_by_feed.items()
            if url not in disabled_feed_urls
        }
        unread_counts_by_folder = get_unread_counts_by_folder(
            raw_folder_rows,
            active_unread_counts_by_feed,
            direct_feed_urls_by_folder,
        )
        _tick("counts_by_folder")
        folder_rows = []
        for row in raw_folder_rows:
            folder_dict = dict(row)
            folder_dict["unread_count"] = unread_counts_by_folder.get(int(row["id"]), 0)
            folder_rows.append(folder_dict)
        global_note = get_setting(conn, GLOBAL_NOTE_SETTING_KEY) or ""
        email_to_default = get_setting(conn, EMAIL_TO_SETTING_KEY) or "" if is_email_configured() else ""
        highlight_rules = get_highlight_keywords(conn)
        profile_name = get_setting(conn, PROFILE_NAME_SETTING_KEY) or ""
        profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
        _profile_lower = profile_email.lower()
        email_contacts = [
            c for c in get_email_contacts(conn)
            if c["address"].lower() != _profile_lower
        ]
        email_bcc = get_setting(conn, EMAIL_BCC_SETTING_KEY) or ""
        youtube_sync_last_at = get_setting(conn, YOUTUBE_SYNC_LAST_AT_KEY) or ""
        youtube_sync_last_result = get_setting(conn, YOUTUBE_SYNC_LAST_RESULT_KEY) or ""
        # Build inactive feed list (feed_url + folder membership).
        inactive_feed_rows = conn.execute(
            """
            SELECT df.feed_url, df.disabled_at, ff.folder_id, f.name AS folder_name
            FROM disabled_feeds df
            LEFT JOIN folder_feeds ff ON ff.feed_url = df.feed_url
            LEFT JOIN folders f ON f.id = ff.folder_id
            ORDER BY df.disabled_at DESC
            """
        ).fetchall()
        _tick("global_note")
        now_pf = time.time()
        with _problematic_feeds_cache_lock:
            cached_pf = _problematic_feeds_cache.get(50)
        if cached_pf and now_pf - cached_pf[0] < PROBLEMATIC_FEEDS_CACHE_TTL_SECONDS:
            problematic_feeds = [dict(r) for r in cached_pf[1]]
        else:
            problematic_feeds = feed_refresh_service.get_problematic_feeds(conn, limit=50)
            with _problematic_feeds_cache_lock:
                _problematic_feeds_cache[50] = (time.time(), [dict(r) for r in problematic_feeds])
        _tick("problematic_feeds")
        feed_urls = folder_feed_urls_by_id.get(selected_folder_id, set())

    meta_block_ms = int((time.perf_counter() - start_req) * 1000)
    LOGGER.info("[perf] home: meta_block=%dms", meta_block_ms)

    filtered_feed_urls = filter_feed_urls(feed_urls, list_feed_url)
    selected_feed_url = list_feed_url
    selected_tag = normalize_tag_value(tag)
    selected_query = normalize_search_query(q)
    legacy_saved_mode = (read_filter or "").strip().lower() == "saved"
    selected_read_filter = normalize_read_filter(read_filter)
    selected_star_only = normalize_star_only(star_only) or legacy_saved_mode
    selected_resume_read_filter = normalize_resume_read_filter(resume_read_filter)
    # Respect an explicit resume_read_filter provided by the caller. Only
    # default to the current read selection when no explicit resume value was
    # supplied (so tag navigation can pass the previous view in resume_read_filter).
    if resume_read_filter is None and selected_read_filter in {"all", "unread"}:
        selected_resume_read_filter = selected_read_filter

    if selected_read_filter == "history":
        filtered_feed_urls = all_feed_urls
        selected_feed_url = None
        selected_tag = None
        selected_star_only = False

    tag_start = time.perf_counter()
    tag_rows = get_tag_counts_for_feeds(filtered_feed_urls)
    tag_block_ms = int((time.perf_counter() - tag_start) * 1000)
    LOGGER.info("[perf] home: tag_block=%dms", tag_block_ms)

    feed_title_map = get_feed_title_map()
    inactive_feeds = [
        {
            "feed_url": str(r["feed_url"]),
            "feed_title": feed_title_map.get(str(r["feed_url"]), str(r["feed_url"])),
            "disabled_at": str(r["disabled_at"] or ""),
            "folder_id": r["folder_id"],
            "folder_name": str(r["folder_name"] or ""),
        }
        for r in inactive_feed_rows
    ]
    problematic_unseen_count = 0
    for problematic_feed in problematic_feeds:
        pf_url = cast(str, problematic_feed["feed_url"])
        problematic_feed["feed_title"] = feed_title_map.get(pf_url, pf_url)
        pf_last_failure_at = problematic_feed.get("last_failure_at")
        if not isinstance(pf_last_failure_at, (int, float)):
            continue
        if problematic_feed.get("acknowledged_at"):
            continue
        if problematic_feeds_last_viewed_at is None or float(pf_last_failure_at) > problematic_feeds_last_viewed_at:
            problematic_unseen_count += 1
    feeds_by_folder: dict[int, list[FeedInFolder]] = {}
    # feed_url → containing_folder_id, so feed-name links in posts/entry can
    # navigate to the feed's own folder rather than the currently-viewed one.
    feed_to_folder: dict[str, int] = {}
    for row in folder_rows:
        folder_row_id = int(row["id"])
        urls = direct_feed_urls_by_folder.get(folder_row_id, [])
        folder_feeds = [
            FeedInFolder(
                url=url,
                title=feed_title_map.get(url, url),
                icon_url=get_favicon_url(url),
                unread_count=unread_counts_by_feed.get(url, 0),
            )
            for url in urls
            if url not in disabled_feed_urls
        ]
        folder_feeds.sort(key=lambda f: f.title.casefold())
        feeds_by_folder[folder_row_id] = folder_feeds
        for url in urls:
            feed_to_folder[url] = folder_row_id

    root_folder_row = next((row for row in folder_rows if int(row["depth"]) == 0), None)
    child_folder_rows = [row for row in folder_rows if int(row["depth"]) == 1]

    # Determine server-side limit. If the client requested a "chunk" (page) use
    # CHUNK_SIZE per chunk; otherwise use the default limit from list_entries_for_feeds.
    try:
        if chunk and int(chunk) > 0:
            requested_chunk = int(chunk)
            limit = min(requested_chunk * CHUNK_SIZE, 2000)
        else:
            limit = 250
    except Exception:
        limit = 250

    posts_start = time.perf_counter()
    # Exclude disabled feeds from the entry list unless the user has selected a
    # specific feed directly (clicking it should still let you browse its content).
    entry_feed_urls = filtered_feed_urls if list_feed_url else filtered_feed_urls - disabled_feed_urls
    posts = list_entries_for_feeds(
        entry_feed_urls,
        limit=limit,
        sort_by=selected_sort_by,
        sort_dir=selected_sort_dir,
        read_filter=selected_read_filter,
        star_only=selected_star_only,
        selected_tag=selected_tag,
        search_query=selected_query,
    )

    # Surface orphan archive entries (saved articles whose feed has been
    # unsubscribed) only when viewing the root "All Feeds" with the saved
    # filter on — they don't belong to any folder, so per-folder views
    # legitimately exclude them.
    if (
        selected_star_only
        and selected_folder_id == root_id
        and not selected_feed_url
        and not selected_tag
        and not selected_query
    ):
        try:
            posts = merge_orphan_saved_entries(
                posts,
                live_feed_urls=all_feed_urls,
                sort_by=selected_sort_by,
                sort_dir=selected_sort_dir,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("orphan saved entry merge failed: %s", exc)

    posts_block_ms = int((time.perf_counter() - posts_start) * 1000)
    LOGGER.info("[perf] home: posts_block=%dms", posts_block_ms)

    # Prioritize lead-image backfill for entries currently visible in this chunk.
    # Fire-and-forget: returns immediately; semaphore prevents concurrent pile-up.
    uncached_posts = [p for p in posts if not p.get("thumbnail_url")]
    if uncached_posts:
        threading.Thread(
            target=lead_image_service.backfill_entry_list,
            args=(uncached_posts,),
            daemon=True,
        ).start()

    # If the client requested a delta chunk (incremental load), return only
    # the slice for that chunk rather than the cumulative list up to limit.
    try:
        if chunk and chunk_delta:
            requested_chunk = int(chunk)
            if requested_chunk > 0:
                start = (requested_chunk - 1) * CHUNK_SIZE
                end = requested_chunk * CHUNK_SIZE
                posts = posts[start:end]
    except Exception:
        # On any error, fall back to the cumulative behavior.
        pass

    selected_entry = None
    if feed_url and entry_id:
        selected_entry = get_entry_detail(feed_url, entry_id)
        if selected_entry and not selected_entry["read"]:
            with get_reader() as reader:
                reader.mark_entry_as_read((feed_url, entry_id))
            try:
                upsert_entry_read_state(feed_url, entry_id)
            except Exception:
                LOGGER.warning("upsert_entry_read_state failed in home (db contention?); entry still marked read in reader", exc_info=True)
            with unread_counts_cache_lock:
                global _unread_counts_generation
                _unread_counts_generation += 1
                unread_counts_cache.clear()
            selected_entry["read"] = True
            for post in posts:
                if post["feed_url"] == feed_url and post["id"] == entry_id:
                    post["read"] = True
                    break

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "folder_rows": folder_rows,
            "root_folder_row": root_folder_row,
            "child_folder_rows": child_folder_rows,
            "folder_options": folder_options,
            "feeds_by_folder": feeds_by_folder,
            "feed_to_folder": feed_to_folder,
            "tag_rows": tag_rows,
            "selected_folder_id": selected_folder_id,
            "selected_feed_url": selected_feed_url,
            "selected_tag": selected_tag,
            "selected_query": selected_query,
            "problematic_feeds": problematic_feeds,
            "problematic_feed_count": len(problematic_feeds),
            "problematic_unseen_count": problematic_unseen_count,
            "selected_sort_by": selected_sort_by,
            "selected_sort_dir": selected_sort_dir,
            "selected_read_filter": selected_read_filter,
            "selected_star_only": selected_star_only,
            "selected_resume_read_filter": selected_resume_read_filter,
            "global_note": global_note,
            "email_configured": is_email_configured(),
            "email_to_default": email_to_default,
            "instapaper_configured": is_instapaper_configured(),
            "youtube_sync_last_at": youtube_sync_last_at,
            "youtube_sync_last_result": youtube_sync_last_result,
            "inactive_feeds": inactive_feeds,
            "inactive_feed_count": len(inactive_feeds),
            "posts": posts,
            "selected_entry": selected_entry,
            "message": message,
            "no_rss_url": no_rss_url,
            "auto_refresh_enabled": getattr(app.state, "auto_refresh_minutes", 0) > 0,
            "auto_refresh_minutes": getattr(app.state, "auto_refresh_minutes", 0),
            "auto_refresh_option_minutes": AUTO_REFRESH_OPTION_MINUTES,
            "static_asset_version": STATIC_ASSET_VERSION,
            "debug_mode": DEBUG_MODE,
            "highlight_rules": highlight_rules,
            "email_contacts": email_contacts,
            "email_bcc": email_bcc,
            "profile_name": profile_name,
            "profile_email": profile_email,
        },
    )


@app.get("/dev/feeds/email-match.xml")
@app.get("/dev/feeds/email-match.rss")
def dev_feed_email_match_rss():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-match", "Lectio Dev — Email Match (RSS)", "MATCH", 5, "rss")


@app.get("/dev/feeds/email-match.atom")
def dev_feed_email_match_atom():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-match", "Lectio Dev — Email Match (Atom)", "MATCH", 5, "atom")


@app.get("/dev/feeds/email-match.json")
def dev_feed_email_match_json():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-match", "Lectio Dev — Email Match (JSON Feed)", "MATCH", 5, "json")


@app.get("/dev/feeds/email-skip.xml")
@app.get("/dev/feeds/email-skip.rss")
def dev_feed_email_skip_rss():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-skip", "Lectio Dev — Email Skip (RSS)", "SKIP", 5, "rss")


@app.get("/dev/feeds/email-skip.atom")
def dev_feed_email_skip_atom():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-skip", "Lectio Dev — Email Skip (Atom)", "SKIP", 5, "atom")


@app.get("/dev/feeds/email-skip.json")
def dev_feed_email_skip_json():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-skip", "Lectio Dev — Email Skip (JSON Feed)", "SKIP", 5, "json")


def _make_dev_feed(feed_id: str, title: str, prefix: str, count: int, fmt: str) -> Response:
    """Generate a dev feed in RSS, Atom, or JSON Feed format.

    GUIDs are keyed to the current minute so all three formats produce identical
    entry IDs when fetched in the same minute — they all advance together.
    """
    import json as _json
    now_ts = time.time()
    minute = int(now_ts / 60) * 60  # floor to minute boundary

    entries = []
    for i in range(count):
        entry_minute = minute - i * 60
        entries.append({
            "id": f"urn:lectio-dev:{feed_id}:{entry_minute}:{i}",
            "title": f"{prefix}: Dev article {i + 1} ({entry_minute})",
            "url": f"https://example.com/dev/{feed_id}/{entry_minute}/{i}",
            "iso": datetime.fromtimestamp(entry_minute, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rfc": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(entry_minute)),
        })

    if fmt == "atom":
        items_xml = ""
        for e in entries:
            items_xml += (
                f"<entry>"
                f"<id>{e['id']}</id>"
                f"<title>{e['title']}</title>"
                f"<link href='{e['url']}' rel='alternate'/>"
                f"<published>{e['iso']}</published>"
                f"<updated>{e['iso']}</updated>"
                f"<content type='text'>Dev test entry for rule testing.</content>"
                f"</entry>\n"
            )
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            "<feed xmlns='http://www.w3.org/2005/Atom'>\n"
            f"<title>{title}</title>\n"
            "<link href='https://example.com' rel='alternate'/>\n"
            f"<id>urn:lectio-dev:{feed_id}-atom</id>\n"
            f"<updated>{entries[0]['iso']}</updated>\n"
            f"{items_xml}"
            "</feed>"
        )
        return Response(content=body, media_type="application/atom+xml")

    if fmt == "json":
        items = [
            {
                "id": e["id"],
                "title": e["title"],
                "url": e["url"],
                "date_published": e["iso"],
                "content_text": "Dev test entry for rule testing.",
            }
            for e in entries
        ]
        body = _json.dumps({
            "version": "https://jsonfeed.org/version/1.1",
            "title": title,
            "home_page_url": "https://example.com",
            "items": items,
        })
        return Response(content=body, media_type="application/feed+json")

    # RSS 2.0 (default)
    items_xml = ""
    for e in entries:
        items_xml += (
            f"<item>"
            f"<title>{e['title']}</title>"
            f"<link>{e['url']}</link>"
            f"<guid isPermaLink='false'>{e['id']}</guid>"
            f"<pubDate>{e['rfc']}</pubDate>"
            f"<description>Dev test entry for rule testing.</description>"
            f"</item>\n"
        )
    body = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<rss version='2.0'><channel>\n"
        f"<title>{title}</title>\n"
        "<link>https://example.com</link>\n"
        f"<description>{title}</description>\n"
        f"<lastBuildDate>{entries[0]['rfc']}</lastBuildDate>\n"
        f"{items_xml}"
        "</channel></rss>"
    )
    return Response(content=body, media_type="application/rss+xml")


@app.post("/dev/flush-email-batch")
def dev_flush_email_batch():
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    try:
        _flush_all_email_batches()
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/debug/starred-archive/largest")
def debug_starred_archive_largest(limit: int = Query(default=50, ge=1, le=500)):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    rows = starred_archive_service.largest_archived_entries(limit=limit)
    # Annotate with entry titles where we still have them in the reader DB.
    titles: dict[tuple[str, str], str] = {}
    try:
        with get_reader() as reader:
            for row in rows:
                key = (row["feed_url"], row["entry_id"])
                try:
                    entry = reader.get_entry(key, None)
                except Exception:
                    entry = None
                if entry is not None:
                    titles[key] = str(getattr(entry, "title", "") or "")
    except Exception:
        pass
    for row in rows:
        row["title"] = titles.get((row["feed_url"], row["entry_id"]), "")
    return JSONResponse({"ok": True, "rows": rows})


@app.post("/debug/clear-lead-image-cache")
def debug_clear_lead_image_cache(
    request: Request,
    feed_url: str | None = Form(default=None),
):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    deleted, evicted_urls = lead_image_service.clear_lead_image_cache(feed_url or None)
    _purge_thumb_cache_for_urls(evicted_urls)
    return JSONResponse({"ok": True, "deleted": deleted, "feed_url": feed_url})


def _purge_thumb_cache_for_urls(urls: list[str]) -> None:
    """Delete /thumb cache entries for the given image URLs (DB + legacy files)."""
    keys: list[str] = []
    for url in urls:
        if not url:
            continue
        cache_key = hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}".encode()).hexdigest()
        keys.append(cache_key)
        try:
            (THUMB_CACHE_DIR / f"{cache_key}.jpg").unlink(missing_ok=True)
        except Exception:
            pass
    if keys:
        try:
            with get_thumb_connection() as conn:
                conn.executemany("DELETE FROM thumb_cache WHERE cache_key = ?", [(k,) for k in keys])
        except Exception:
            pass


@app.post("/debug/clear-entry-lead-image-cache")
def debug_clear_entry_lead_image_cache(
    request: Request,
    feed_url: str = Form(...),
    entry_id: str = Form(...),
):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    old_url = lead_image_service.clear_entry_lead_image_cache(feed_url, entry_id)
    if old_url:
        _purge_thumb_cache_for_urls([old_url])
    return JSONResponse({"ok": True, "cleared": old_url is not None})


@app.get("/debug/feed-bypass-state")
def debug_feed_bypass_state(request: Request, feed_url: str = Query(...)):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    return JSONResponse({"ok": True, "bypassed": feed_url in lead_image_service.get_bypassed_feeds()})


@app.post("/debug/toggle-feed-bypass")
def debug_toggle_feed_bypass(request: Request, feed_url: str = Form(...)):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    new_state = lead_image_service.toggle_feed_bypass(feed_url)
    return JSONResponse({"ok": True, "bypassed": new_state})


# Thumbnail dimensions: 2× the CSS slot (4.5rem ≈ 72px, tile height ≈ 84px) for retina.
_THUMB_W = 144
_THUMB_H = 168


@app.get("/thumb")
def thumbnail_proxy(url: str = Query(...)) -> Response:
    """Fetch a remote image, resize it to thumbnail dimensions with LANCZOS, and
    return a cached JPEG.  This eliminates the progressive-load flicker caused by
    downloading full-size hero images into the small post-list thumbnail slot."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return Response(status_code=400)

    cache_key = hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}".encode()).hexdigest()
    cached_headers = {"Cache-Control": "public, max-age=604800, immutable"}

    try:
        with get_thumb_connection() as conn:
            row = conn.execute(
                "SELECT jpeg FROM thumb_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if row is not None:
            return Response(content=bytes(row["jpeg"]), media_type="image/jpeg", headers=cached_headers)
    except Exception:
        pass

    # Lazy migration: legacy on-disk cache from the per-file era. Promote to DB
    # and remove the file so the thumb_cache/ directory drains over time.
    legacy_path = THUMB_CACHE_DIR / f"{cache_key}.jpg"
    if legacy_path.exists():
        try:
            jpeg_bytes = legacy_path.read_bytes()
        except Exception:
            jpeg_bytes = None
        if jpeg_bytes:
            _store_thumb(cache_key, jpeg_bytes)
            try:
                legacy_path.unlink(missing_ok=True)
            except Exception:
                pass
            return Response(content=jpeg_bytes, media_type="image/jpeg", headers=cached_headers)

    try:
        with httpx.Client(follow_redirects=True, timeout=12.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            raw = resp.content
            src_content_type = resp.headers.get("content-type", "")
    except Exception:
        return Response(status_code=502)

    try:
        img = _PILImage.open(io.BytesIO(raw)).convert("RGB")
        iw, ih = img.size
        scale = max(_THUMB_W / iw, _THUMB_H / ih)
        new_w = max(1, round(iw * scale))
        new_h = max(1, round(ih * scale))
        img = img.resize((new_w, new_h), _PILImage.LANCZOS)
        left = (new_w - _THUMB_W) // 2
        top = (new_h - _THUMB_H) // 2
        img = img.crop((left, top, left + _THUMB_W, top + _THUMB_H))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        jpeg_bytes = buf.getvalue()
        _store_thumb(cache_key, jpeg_bytes)
    except Exception:
        # Pillow failed (corrupt image, unsupported format, etc.) — serve original.
        return Response(
            content=raw,
            media_type=src_content_type or "image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return Response(content=jpeg_bytes, media_type="image/jpeg", headers=cached_headers)


def _store_thumb(cache_key: str, jpeg_bytes: bytes) -> None:
    try:
        with get_thumb_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO thumb_cache (cache_key, jpeg, created_at) VALUES (?, ?, ?)",
                (cache_key, jpeg_bytes, time.time()),
            )
    except Exception:
        pass


# 64-char hex sha256 — the only shape we issue.
_ASSET_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
STARRED_ASSET_URL_PREFIX = "/starred-asset/"


@app.get("/starred-asset/{asset_hash}")
def starred_asset(asset_hash: str) -> Response:
    if not _ASSET_HASH_RE.match(asset_hash):
        return Response(status_code=400)
    found = starred_archive_service.get_asset(asset_hash)
    if found is None:
        return Response(status_code=404)
    data, content_type = found
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/folders")
def create_folder(name: str = Form(...)):
    with get_meta_connection() as conn:
        root_id = get_root_folder_id(conn)
        conn.execute(
            "INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)",
            (name.strip(), root_id),
        )
        row = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
            (name.strip(), root_id),
        ).fetchone()
        target_id = root_id if not row else int(row["id"])
    invalidate_meta_structure_cache()
    return RedirectResponse(url=f"/?folder_id={target_id}", status_code=303)


@app.post("/folders/rename")
def rename_folder_route(folder_id: int = Form(...), name: str = Form(...)):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE folders SET name = ? WHERE id = ?",
            (name.strip(), folder_id),
        )
    invalidate_meta_structure_cache()
    return RedirectResponse(url=f"/?folder_id={folder_id}", status_code=303)


@app.post("/youtube/sync")
def youtube_sync_route(folder_id: int = Form(...)):
    result = _run_youtube_sync(folder_id=folder_id)
    if result["error"]:
        message = f"YouTube sync error: {result['error']}"
    else:
        message = f"YouTube sync: +{result['added']} / -{result['removed']} ({result['total']} subs)"
    return RedirectResponse(url=f"/?folder_id={folder_id}&message={message}", status_code=303)


@app.post("/folders/delete")
def delete_folder_route(folder_id: int = Form(...)):
    root_id = None
    try:
        with get_meta_connection() as conn:
            root_id = get_root_folder_id(conn)
        deleted_folders, deleted_feeds = delete_folder(folder_id)
        message = f"Deleted {deleted_folders} folder(s). Removed {deleted_feeds} feed subscription(s)."
    except ValueError as exc:
        message = str(exc)
        if root_id is None:
            with get_meta_connection() as conn:
                root_id = get_root_folder_id(conn)

    if root_id is None:
        with get_meta_connection() as conn:
            root_id = get_root_folder_id(conn)

    return RedirectResponse(
        url=f"/?folder_id={root_id}&message={quote_plus(message)}",
        status_code=303,
    )


def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


@app.post("/feeds")
def create_feed(feed_url: str = Form(...), folder_id: int = Form(...)):
    url = feed_url.strip()
    target_url = url
    auto_discovered = False

    # For non-YouTube URLs, probe whether the URL is a feed and run
    # auto-discovery if it looks like a webpage instead.
    if not _is_youtube_url(url):
        candidates = discover_feed_urls(url)
        if not candidates:
            return RedirectResponse(
                url=(
                    f"/?folder_id={folder_id}"
                    f"&message={quote_plus('No RSS/Atom feed found at that URL.')}"
                    f"&no_rss_url={quote_plus(url)}"
                ),
                status_code=303,
            )
        target_url = candidates[0]
        auto_discovered = target_url.rstrip("/") != url.rstrip("/")

    message = "Feed added."
    if auto_discovered:
        message = f"Feed added (discovered from {url})."
    try:
        add_feed_to_folder(target_url, folder_id)
        feed_refresh_service.update_feeds([target_url])
    except Exception as exc:
        message = f"Feed add failed: {exc}"
    return RedirectResponse(
        url=f"/?folder_id={folder_id}&message={quote_plus(message)}",
        status_code=303,
    )


@app.post("/scraped-feeds")
def create_scraped_feed_route(
    source_url: str = Form(...),
    mode: str = Form(...),
    selector: str = Form(default=""),
    feed_title: str = Form(default=""),
    folder_id: int | None = Form(default=None),
):
    source_url = source_url.strip()
    if not source_url:
        return RedirectResponse(url="/?message=URL+required", status_code=303)
    if mode not in ("change_detect", "link_list"):
        mode = "change_detect"

    with get_meta_connection() as conn:
        target_folder_id = folder_id or get_root_folder_id(conn)

    try:
        with get_meta_connection() as conn:
            with get_reader() as reader:
                feed_id, file_url = scraper_service.create_scraped_feed(
                    conn, reader, source_url, mode,
                    selector.strip() or None,
                    feed_title.strip() or None,
                )
            conn.execute(
                "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                (target_folder_id, file_url),
            )
    except Exception as exc:
        LOGGER.warning("[scraper] create failed for %s: %s", source_url, exc)
        return RedirectResponse(
            url=f"/?folder_id={target_folder_id}&message={quote_plus(f'Page feed failed: {exc}')}",
            status_code=303,
        )

    invalidate_meta_structure_cache()
    return RedirectResponse(
        url=f"/?folder_id={target_folder_id}&message={quote_plus('Page feed created.')}",
        status_code=303,
    )


@app.post("/scraped-feeds/delete")
def delete_scraped_feed_route(
    feed_id: str = Form(...),
    folder_id: int = Form(...),
):
    with get_meta_connection() as conn:
        file_url = scraper_service.feed_file_url(feed_id)
        conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (file_url,))
        with get_reader() as reader:
            scraper_service.delete_scraped_feed(conn, reader, feed_id)
    invalidate_meta_structure_cache()
    return RedirectResponse(
        url=f"/?folder_id={folder_id}&message={quote_plus('Page feed removed.')}",
        status_code=303,
    )


@app.get("/feeds/properties")
def feed_properties(feed_url: str):
    return JSONResponse(get_feed_properties(feed_url))


@app.post("/feeds/set-user-title")
def set_feed_user_title_route(feed_url: str = Form(...), user_title: str = Form(...)):
    with get_reader() as reader:
        title_to_set = user_title.strip() or None
        reader.set_feed_user_title(feed_url, title_to_set)
    with feed_title_map_cache_lock:
        feed_title_map_cache.clear()
    return JSONResponse({"ok": True, "user_title": user_title.strip() or None})


@app.get("/folders/properties")
def folder_properties(folder_id: int):
    return JSONResponse(get_folder_properties(folder_id))


_VALID_MANUAL_STRATEGIES = {"auto", "inline", "og_scrape", "media_rss", "none", "webcomic", "artwork"}


def _auto_tag_webcomic_feeds() -> None:
    """Set strategy='webcomic' for all feeds in folders whose name contains 'comic'.

    Only sets non-manually-locked feeds so user overrides are respected.
    Runs at startup so new feeds added to comics folders are tagged immediately.
    """
    try:
        with get_meta_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT ff.feed_url
                FROM folder_feeds ff
                JOIN folders f ON ff.folder_id = f.id
                WHERE lower(f.name) LIKE '%comic%'
                """
            ).fetchall()
            feed_urls = [str(r["feed_url"]) for r in rows]
            if not feed_urls:
                return
            now = time.time()
            for feed_url in feed_urls:
                existing = conn.execute(
                    "SELECT strategy, manual FROM feed_lead_image_strategy WHERE feed_url = ?",
                    (feed_url,),
                ).fetchone()
                if existing and existing["manual"]:
                    continue
                if existing and existing["strategy"] in ("webcomic", "artwork"):
                    continue
                conn.execute(
                    """
                    INSERT INTO feed_lead_image_strategy (feed_url, strategy, detected_at, manual)
                    VALUES (?, 'webcomic', ?, 0)
                    ON CONFLICT(feed_url) DO UPDATE SET
                        strategy = 'webcomic',
                        detected_at = excluded.detected_at
                    WHERE manual = 0
                    """,
                    (feed_url, now),
                )
                lead_image_service.store_feed_strategy(feed_url, "webcomic", manual=False)
    except Exception:
        LOGGER.exception("_auto_tag_webcomic_feeds failed")


# Domains whose feeds use the artwork image layout (image appended after text).
# Feed URLs matching any of these get strategy='artwork' automatically.
_ARTWORK_FEED_DOMAINS = (
    "artstation.com",
    "deviantart.com",
)


def _auto_tag_artwork_feeds() -> None:
    """Set strategy='artwork' for feeds from known art-portfolio domains.

    Matches on feed URL, not folder name.  Skips manually-locked strategies.
    Runs at startup so new and existing feeds are migrated automatically.
    """
    try:
        with get_meta_connection() as conn:
            now = time.time()
            for domain in _ARTWORK_FEED_DOMAINS:
                rows = conn.execute(
                    "SELECT DISTINCT feed_url FROM folder_feeds WHERE lower(feed_url) LIKE ?",
                    (f"%{domain}%",),
                ).fetchall()
                for row in rows:
                    feed_url = str(row["feed_url"])
                    existing = conn.execute(
                        "SELECT strategy, manual FROM feed_lead_image_strategy WHERE feed_url = ?",
                        (feed_url,),
                    ).fetchone()
                    if existing and existing["manual"]:
                        continue
                    if existing and existing["strategy"] == "artwork":
                        continue
                    conn.execute(
                        """
                        INSERT INTO feed_lead_image_strategy (feed_url, strategy, detected_at, manual)
                        VALUES (?, 'artwork', ?, 0)
                        ON CONFLICT(feed_url) DO UPDATE SET
                            strategy = 'artwork',
                            detected_at = excluded.detected_at
                        WHERE manual = 0
                        """,
                        (feed_url, now),
                    )
                    lead_image_service.store_feed_strategy(feed_url, "artwork", manual=False)
    except Exception:
        LOGGER.exception("_auto_tag_artwork_feeds failed")


@app.post("/feeds/strategy")
def set_feed_image_strategy(feed_url: str = Form(...), strategy: str = Form(...)):
    if strategy not in _VALID_MANUAL_STRATEGIES:
        return JSONResponse({"error": "invalid strategy"}, status_code=400)
    if strategy == "auto":
        # Remove manual lock — delete so auto-detection starts fresh.
        try:
            with get_meta_connection() as conn:
                conn.execute("DELETE FROM feed_lead_image_strategy WHERE feed_url = ?", (feed_url,))
        except Exception:
            pass
    else:
        lead_image_service.store_feed_strategy(feed_url, strategy, manual=True)
    # Clear cached images so entries re-resolve under the new strategy.
    lead_image_service.clear_lead_image_cache(feed_url)
    # Re-fetch images for recent entries using the new strategy.  Bypass the
    # chunk-backfill semaphore so this isn't silently dropped if another
    # backfill is in flight.  _do_backfill_entry_list already skips entries
    # that are in cache (none are — we just cleared) and respects strategy
    # (source-fetches for og_scrape, skips for inline/none).
    if strategy not in ("auto", "none"):
        def _refetch(furl: str) -> None:
            try:
                with get_reader() as reader:
                    entries = list(reader.get_entries(feed=furl, limit=50))
                posts = [
                    {
                        "feed_url": str(getattr(e, "feed_url", "") or ""),
                        "id": str(getattr(e, "id", "") or ""),
                        "link": str(getattr(e, "link", "") or ""),
                    }
                    for e in entries
                ]
                lead_image_service._do_backfill_entry_list(posts)
            except Exception:
                pass
        threading.Thread(target=_refetch, args=(feed_url,), daemon=True).start()
    return JSONResponse({"ok": True, "strategy": strategy})


@app.post("/feeds/display-prefs")
def set_feed_display_pref_route(
    feed_url: str = Form(...),
    key: str = Form(...),
    value: int = Form(...),
):
    if key not in _DISPLAY_PREF_KEYS:
        return JSONResponse({"error": "invalid key"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_display_pref(conn, feed_url, key, value)
    return JSONResponse({"ok": True, "key": key, "value": value})


@app.post("/feeds/thumbnail-url")
def set_feed_thumbnail_url_route(
    feed_url: str = Form(...),
    thumbnail_url: str = Form(default=""),
):
    with get_meta_connection() as conn:
        upsert_feed_thumbnail_url(conn, feed_url, thumbnail_url.strip() or None)
    return JSONResponse({"ok": True})


@app.post("/feeds/caption-source")
def set_feed_caption_source(
    feed_url: str = Form(...),
    source: str = Form(...),
):
    _VALID = {"auto", "alt", "title", "both", "none"}
    if source not in _VALID:
        return JSONResponse({"error": "invalid source"}, status_code=400)
    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
            (feed_url,),
        )
        conn.execute(
            "UPDATE feed_display_prefs SET caption_source = ? WHERE feed_url = ?",
            (None if source == "auto" else source, feed_url),
        )
    return JSONResponse({"ok": True, "source": source})


@app.get("/highlights")
def get_highlights_route():
    with get_meta_connection() as conn:
        rows = get_highlight_keywords(conn)
    return JSONResponse({"ok": True, "keywords": rows})


@app.post("/highlights/add")
def add_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(...),
    color: str = Form("yellow"),
    is_regex: int = Form(0),
    type: str = Form("highlight"),
    search_in: str = Form("title"),
    delivery: str = Form("immediately"),
    email_to: str = Form(""),
    batch_time: str = Form(""),
    batch_count: int = Form(0),
    cc_me: int = Form(0),
    enabled: int = Form(0),
    dedup_window_hours: int = Form(168),
    exclude_scope_ids: str = Form(""),
):
    keyword = keyword.strip()
    if scope not in _HIGHLIGHT_VALID_SCOPES:
        return JSONResponse({"error": "invalid scope"}, status_code=400)
    if type == "deduplicate":
        if keyword not in _DEDUP_VALID_MATCH_METHODS:
            return JSONResponse({"error": "invalid match method for deduplicate rule"}, status_code=400)
        if scope == "feed":
            return JSONResponse({"error": "deduplicate rules cannot be scoped to a single feed"}, status_code=400)
    else:
        if not keyword:
            return JSONResponse({"error": "keyword is required"}, status_code=400)
    with get_meta_connection() as conn:
        add_highlight_keyword(conn, scope, scope_id, keyword, color, bool(is_regex),
                              type, search_in, delivery, email_to, batch_time, batch_count,
                              bool(cc_me), enabled, dedup_window_hours, exclude_scope_ids)
    return JSONResponse({"ok": True, "scope": scope, "scope_id": scope_id, "keyword": keyword,
                         "color": color, "is_regex": bool(is_regex), "type": type,
                         "search_in": search_in, "delivery": delivery,
                         "email_to": email_to, "batch_time": batch_time, "batch_count": batch_count,
                         "cc_me": bool(cc_me), "enabled": bool(enabled),
                         "dedup_window_hours": dedup_window_hours,
                         "exclude_scope_ids": exclude_scope_ids.strip()})


@app.post("/highlights/remove")
def remove_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(...),
):
    with get_meta_connection() as conn:
        remove_highlight_keyword(conn, scope, scope_id, keyword)
    return JSONResponse({"ok": True})


@app.post("/highlights/toggle")
def toggle_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(...),
    enabled: int = Form(...),
):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE highlight_keywords SET enabled = ? WHERE scope = ? AND scope_id = ? AND keyword = ?",
            (1 if enabled else 0, scope, scope_id, keyword.strip()),
        )
    return JSONResponse({"ok": True, "enabled": bool(enabled)})


@app.get("/rules/dry-run")
def rules_dry_run_route(
    type: str = Query("highlight"),
    scope: str = Query("global"),
    scope_id: str = Query(""),
    keyword: str = Query(""),
    is_regex: int = Query(0),
    search_in: str = Query("title"),
    dedup_window_hours: int = Query(168),
    exclude_scope_ids: str = Query(""),
    feed_urls: str = Query(""),  # comma-separated; overrides scope for dedup
):
    with get_meta_connection() as conn:
        if type == "deduplicate":
            match_method = keyword if keyword in _DEDUP_VALID_MATCH_METHODS else "slug"
            custom: set[str] | None = None
            if feed_urls:
                custom = {u.strip() for u in feed_urls.split(",") if u.strip()}
            result = _dry_run_dedup(conn, scope, scope_id, match_method, max(1, dedup_window_hours),
                                    exclude_scope_ids=exclude_scope_ids, custom_feed_urls=custom)
        elif type in ("highlight", "mark_as_read", "email_article"):
            result = _dry_run_pattern(conn, scope, scope_id, keyword, bool(is_regex), search_in)
        else:
            return JSONResponse({"error": "unknown rule type"}, status_code=400)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    result["ok"] = True
    result["type"] = type
    return JSONResponse(result)


@app.post("/rules/run-now")
def rules_run_now_route(
    type: str = Form(...),
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(""),
    is_regex: int = Form(0),
    search_in: str = Form("title"),
    dedup_window_hours: int = Form(168),
    exclude_scope_ids: str = Form(""),
):
    with get_meta_connection() as conn:
        if type == "deduplicate":
            match_method = keyword if keyword in _DEDUP_VALID_MATCH_METHODS else "slug"
            result = _run_now_dedup(conn, scope, scope_id, match_method, max(1, dedup_window_hours),
                                    exclude_scope_ids=exclude_scope_ids)
        elif type == "mark_as_read":
            result = _run_now_pattern(conn, scope, scope_id, keyword, bool(is_regex), search_in)
        else:
            return JSONResponse({"error": f"Run Now not supported for type '{type}'"}, status_code=400)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    with get_meta_connection() as conn:
        cur = conn.execute(
            "INSERT INTO rule_run_log (run_at, rule_type, scope, scope_id, keyword, entries_affected, trigger)"
            " VALUES (?, ?, ?, ?, ?, ?, 'manual')",
            (datetime.now().isoformat(), type, scope, scope_id, keyword, result.get("count", 0)),
        )
        log_id = cur.lastrowid
        entries = result.get("entries") or []
        if entries and log_id:
            conn.executemany(
                "INSERT INTO rule_run_log_entries (log_id, feed_url, entry_id, title, link, feed_title)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(log_id, e["feed_url"], e["entry_id"], e["title"], e["link"], e["feed_title"])
                 for e in entries],
            )
    result["ok"] = True
    return JSONResponse(result)


@app.post("/highlights/reorder")
async def reorder_highlights_route(request: Request):
    body = await request.json()
    order: list[dict] = body.get("order", [])
    with get_meta_connection() as conn:
        for i, item in enumerate(order):
            conn.execute(
                "UPDATE highlight_keywords SET sort_order = ? WHERE scope = ? AND scope_id = ? AND keyword = ?",
                (i, item.get("scope", ""), item.get("scope_id", ""), item.get("keyword", "")),
            )
    return JSONResponse({"ok": True})


@app.get("/automation/history")
def automation_history_route(
    limit: int = Query(200),
    scope: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
):
    filters: list[str] = []
    params: list[object] = []
    if scope is not None:
        filters.append("scope = ?")
        params.append(scope)
    if scope_id is not None:
        filters.append("scope_id = ?")
        params.append(scope_id)
    if keyword is not None:
        filters.append("keyword = ?")
        params.append(keyword)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    params.append(limit)
    with get_meta_connection() as conn:
        rows = conn.execute(
            f"SELECT id, run_at, rule_type, scope, scope_id, keyword, entries_affected, trigger"
            f" FROM rule_run_log {where} ORDER BY run_at DESC LIMIT ?",
            params,
        ).fetchall()
    return JSONResponse({"ok": True, "history": [dict(r) for r in rows]})


@app.get("/automation/history/{log_id}/entries")
def automation_history_entries_route(log_id: int):
    with get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT feed_url, entry_id, title, link, feed_title"
            " FROM rule_run_log_entries WHERE log_id = ? ORDER BY rowid",
            (log_id,),
        ).fetchall()
    return JSONResponse({"ok": True, "entries": [dict(r) for r in rows]})


@app.get("/dedup/false-matches")
def get_dedup_false_matches():
    with get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT keep_link, mark_link FROM dedup_false_matches ORDER BY added_at DESC"
        ).fetchall()
    return JSONResponse({"ok": True, "pairs": [{"keep_link": r[0], "mark_link": r[1]} for r in rows]})


@app.post("/dedup/false-match")
async def toggle_dedup_false_match(request: Request):
    data = await request.json()
    keep_link = str(data.get("keep_link") or "").strip()
    mark_link = str(data.get("mark_link") or "").strip()
    if not keep_link or not mark_link:
        return JSONResponse({"ok": False, "error": "keep_link and mark_link required"}, status_code=400)
    with get_meta_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM dedup_false_matches WHERE keep_link = ? AND mark_link = ?",
            (keep_link, mark_link),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM dedup_false_matches WHERE keep_link = ? AND mark_link = ?",
                (keep_link, mark_link),
            )
            active = False
        else:
            conn.execute(
                "INSERT INTO dedup_false_matches (keep_link, mark_link, added_at) VALUES (?, ?, ?)",
                (keep_link, mark_link, datetime.now().isoformat()),
            )
            active = True
    return JSONResponse({"ok": True, "active": active})


@app.get("/email-contacts")
def list_email_contacts_route():
    with get_meta_connection() as conn:
        return JSONResponse({"ok": True, "contacts": get_email_contacts(conn)})


@app.post("/email-contacts/add")
def add_email_contact_route(label: str = Form(...), address: str = Form(...)):
    try:
        with get_meta_connection() as conn:
            contact = add_email_contact(conn, label, address)
        return JSONResponse({"ok": True, "contact": contact})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/email-contacts/remove")
def remove_email_contact_route(contact_id: int = Form(...)):
    with get_meta_connection() as conn:
        remove_email_contact(conn, contact_id)
    return JSONResponse({"ok": True})


@app.post("/settings/email-bcc")
def save_email_bcc_route(address: str = Form("")):
    with get_meta_connection() as conn:
        set_setting(conn, EMAIL_BCC_SETTING_KEY, address.strip())
    return JSONResponse({"ok": True})


@app.post("/settings/profile")
def save_profile_route(name: str = Form(""), email: str = Form("")):
    with get_meta_connection() as conn:
        set_setting(conn, PROFILE_NAME_SETTING_KEY, name.strip())
        set_setting(conn, PROFILE_EMAIL_SETTING_KEY, email.strip())
    return JSONResponse({"ok": True})


@app.get("/settings/all")
def get_all_settings():
    """Return all user-configurable settings. Sensitive values are masked if set."""
    def _masked(val: str) -> str:
        return "••••••••" if val else ""

    with get_meta_connection() as conn:
        profile_name = get_setting(conn, PROFILE_NAME_SETTING_KEY) or ""
        profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
        maint_last = get_setting(conn, "maintenance_last_ran_at") or ""
        email_to_default = get_setting(conn, EMAIL_TO_SETTING_KEY) or ""
        # Contacts live in the email_contacts table; filter out the profile email
        # since it's already represented by the synthetic "Me" row in the UI.
        all_contacts = get_email_contacts(conn)
        profile_lower = profile_email.lower()
        contacts = [
            {"label": c["label"], "address": c["address"]}
            for c in all_contacts
            if c["address"].lower() != profile_lower
        ]

    yt_api_key = get_yt_api_key()
    resend_key = get_resend_api_key()
    instapaper_pw = get_runtime_setting(SETTING_INSTAPAPER_PASSWORD)

    return JSONResponse({
        "profile_name": profile_name,
        "profile_email": profile_email,
        "tz_display": get_runtime_setting(SETTING_TZ_DISPLAY),
        "maintenance_hour": get_runtime_setting(SETTING_MAINTENANCE_HOUR),
        "maintenance_last_ran_at": maint_last,
        "yt_api_key_set": bool(yt_api_key),
        "yt_api_key_masked": _masked(yt_api_key),
        "yt_channel_id": get_yt_channel_id(),
        "yt_folder_name": get_yt_folder_name(),
        "resend_api_key_set": bool(resend_key),
        "resend_api_key_masked": _masked(resend_key),
        "email_from": get_resend_from(),
        "instapaper_username": get_runtime_setting(SETTING_INSTAPAPER_USERNAME),
        "instapaper_password_set": bool(instapaper_pw),
        "instapaper_password_masked": _masked(instapaper_pw),
        "contacts": contacts,
        "email_to_default": email_to_default,
    })


@app.post("/settings/all")
async def save_all_settings(request: Request):
    """Save any subset of user-configurable settings. Empty string clears a value."""
    import json as _json
    body = await request.json()

    _SENSITIVE = {SETTING_RESEND_API_KEY, SETTING_YT_API_KEY, SETTING_INSTAPAPER_PASSWORD}
    _ALLOWED = {
        PROFILE_NAME_SETTING_KEY, PROFILE_EMAIL_SETTING_KEY,
        SETTING_TZ_DISPLAY, SETTING_MAINTENANCE_HOUR,
        SETTING_YT_API_KEY, SETTING_YT_CHANNEL_ID, SETTING_YT_FOLDER_NAME,
        SETTING_RESEND_API_KEY, SETTING_EMAIL_FROM,
        SETTING_INSTAPAPER_USERNAME, SETTING_INSTAPAPER_PASSWORD,
        "email_contacts", EMAIL_TO_SETTING_KEY,
    }

    import json as _json
    with get_meta_connection() as conn:
        for key, value in body.items():
            if key not in _ALLOWED:
                continue
            if key == "email_contacts":
                # Contacts are stored in the email_contacts table, not app_settings.
                # The payload is a JSON array of {label, address} objects.
                try:
                    incoming = _json.loads(str(value)) if isinstance(value, str) else list(value)
                except Exception:
                    incoming = []
                profile_email_lower = (get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or "").lower()
                # Clear all existing contacts, then re-insert the full list.
                # Keep entries whose address matches profile_email out — they're synthetic "Me".
                conn.execute("DELETE FROM email_contacts")
                for c in incoming:
                    addr = str(c.get("address") or "").strip()
                    label = str(c.get("label") or "").strip()
                    if not addr or addr.lower() == profile_email_lower:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO email_contacts (label, address) VALUES (?, ?)",
                        (label, addr),
                    )
                continue
            str_val = str(value).strip() if value is not None else ""
            # Don't overwrite a real secret with the masked placeholder
            if key in _SENSITIVE and str_val.startswith("••"):
                continue
            if str_val:
                set_setting(conn, key, str_val)
            else:
                delete_setting(conn, key)

    return JSONResponse({"ok": True})


@app.post("/settings/maintenance/run-now")
def run_maintenance_now():
    """Trigger daily maintenance immediately (for testing / manual runs)."""
    threading.Thread(target=_run_daily_maintenance, daemon=True, name="maintenance-manual").start()
    return JSONResponse({"ok": True, "message": "Maintenance started in background."})


@app.post("/feeds/strategy-refresh")
def refresh_feed_strategy_cache_route(
    feed_url: str = Form(...),
    entry_id: str | None = Form(None),
):
    with get_reader() as reader:
        entries = list(reader.get_entries(feed=feed_url, read=None))
    if not entries:
        return JSONResponse({"ok": False, "error": "No entries found for this feed."}, status_code=404)

    sample_entry = None
    if entry_id:
        sample_entry = next((e for e in entries if str(getattr(e, "id", "")) == entry_id), None)

    if sample_entry is None:
        def _best_date(e: object) -> float:
            for attr in ("published", "updated", "added"):
                dt = getattr(e, attr, None)
                if dt:
                    return dt.timestamp()
            return 0.0
        sample_entry = max(entries, key=_best_date)
    strategy_rows = lead_image_service.test_entry_strategies(sample_entry)

    now = time.time()
    formatted_now = format_datetime_for_ui(datetime.fromtimestamp(now, tz=timezone.utc))
    results: list[dict] = []
    with get_meta_connection() as conn:
        for row in strategy_rows:
            conn.execute(
                "INSERT OR REPLACE INTO feed_strategy_cache "
                "(feed_url, strategy, image_url, fetched_at, error, image_alt, image_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (feed_url, row["strategy"], row["image_url"], now, row["error"],
                 row.get("image_alt"), row.get("image_title")),
            )
            results.append({
                "strategy": row["strategy"],
                "image_url": row["image_url"],
                "fetched_at": formatted_now,
                "error": row["error"],
                "image_alt": row.get("image_alt"),
                "image_title": row.get("image_title"),
            })
    return JSONResponse({"ok": True, "strategy_cache": results})


@app.post("/feeds/move")
def move_feed(
    feed_url: str = Form(...),
    from_folder_id: int = Form(...),
    to_folder_id: int = Form(...),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)

    if from_folder_id == to_folder_id:
        return RedirectResponse(
            url=(
                f"/?folder_id={to_folder_id}&list_feed_url={quote_plus(feed_url)}"
                f"&sort_by={quote_plus(normalized_sort_by)}"
                f"&sort_dir={quote_plus(normalized_sort_dir)}"
                f"&read_filter={quote_plus(normalized_read_filter)}"
                f"{star_only_query}"
                f"{resume_read_filter_query}"
                f"&message={quote_plus('Feed is already in that folder.')}"
            ),
            status_code=303,
        )

    message = "Feed moved."
    try:
        move_feed_to_folder(feed_url, from_folder_id, to_folder_id)
    except ValueError as exc:
        message = str(exc)
    except Exception as exc:
        message = f"Feed move failed: {exc}"

    return RedirectResponse(
        url=(
            f"/?folder_id={to_folder_id}&list_feed_url={quote_plus(feed_url)}"
            f"&sort_by={quote_plus(normalized_sort_by)}"
            f"&sort_dir={quote_plus(normalized_sort_dir)}"
            f"&read_filter={quote_plus(normalized_read_filter)}"
            f"{star_only_query}"
            f"{resume_read_filter_query}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@app.post("/feeds/disable")
def disable_feed_route(folder_id: int = Form(...), feed_url: str = Form(...)):
    disable_feed(feed_url)
    return RedirectResponse(url=f"/?folder_id={folder_id}", status_code=303)


@app.post("/feeds/enable")
def enable_feed_route(folder_id: int | None = Form(default=None), feed_url: str = Form(...)):
    enable_feed(feed_url)
    dest = f"/?folder_id={folder_id}" if folder_id else "/"
    return RedirectResponse(url=dest, status_code=303)


@app.post("/feeds/unsubscribe")
def unsubscribe_feed(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)

    ok = True
    message = "Feed unsubscribed."
    try:
        with get_meta_connection() as conn:
            conn.execute(
                "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                (folder_id, feed_url),
            )
            still_used = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1",
                (feed_url,),
            ).fetchone()

        if not still_used:
            # Feed is leaving the reader — give the archive worker a chance
            # to finish capturing any saved entries from this feed before the
            # reader-side data disappears. Otherwise saved-but-uncaptured
            # entries become content-less archive shells.
            try:
                forced = starred_archive_service.force_archive_pending_for_feed(feed_url)
                if forced:
                    LOGGER.info("[unsubscribe] force-archived %d pending captures for %s", forced, feed_url)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[unsubscribe] force-archive failed for %s: %s", feed_url, exc)
            with get_reader() as reader:
                reader.delete_feed(feed_url, missing_ok=True)
        invalidate_meta_structure_cache()
    except Exception as exc:
        ok = False
        message = f"Unsubscribe failed: {exc}"

    # AJAX caller (e.g. problematic-feeds modal trash button) wants a JSON
    # response so it can update the DOM in place instead of navigating away.
    requested_with = request.headers.get("x-requested-with", "").lower()
    if "lectio" in requested_with or requested_with == "xmlhttprequest":
        return JSONResponse({"ok": ok, "feed_url": feed_url, "message": message}, status_code=200 if ok else 500)

    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}"
            f"&sort_by={quote_plus(normalized_sort_by)}"
            f"&sort_dir={quote_plus(normalized_sort_dir)}"
            f"&read_filter={quote_plus(normalized_read_filter)}"
            f"{star_only_query}"
            f"{resume_read_filter_query}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@app.get("/feeds/duplicates")
def get_feed_duplicates():
    """Return same-folder and cross-folder slash-duplicate feed pairs."""
    with get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT ff.folder_id, ff.feed_url, f.name AS folder_name"
            " FROM folder_feeds ff JOIN folders f ON f.id = ff.folder_id"
            " ORDER BY ff.feed_url"
        ).fetchall()

    # url → [(folder_id, folder_name), ...]
    url_folders: dict[str, list[tuple[int, str]]] = {}
    for folder_id, feed_url, folder_name in rows:
        url_folders.setdefault(feed_url, []).append((folder_id, folder_name))

    # canonical → [url, url/, ...] — group all URL variants by their normalized form
    by_canonical: dict[str, list[str]] = {}
    for url in url_folders:
        canonical = normalize_feed_url(url)
        by_canonical.setdefault(canonical, []).append(url)

    same_folder: list[dict] = []
    cross_folder: list[dict] = []

    for canonical, variants in by_canonical.items():
        if len(variants) < 2:
            continue
        # Always keep the canonical (no trailing slash) form; remove the slash variant(s).
        keep = canonical
        for remove in variants:
            if remove == keep:
                continue
            keep_folder_ids = {fid for fid, _ in url_folders.get(keep, [])}
            remove_folder_ids = {fid for fid, _ in url_folders.get(remove, [])}
            shared = keep_folder_ids & remove_folder_ids
            only_in_remove = remove_folder_ids - keep_folder_ids

            # Same-folder entries: both URLs exist in this folder → auto-fix.
            for fid, fname in url_folders.get(remove, []):
                if fid in keep_folder_ids:
                    same_folder.append({
                        "folder_id": fid,
                        "folder_name": fname,
                        "keep": keep,
                        "remove": remove,
                    })

            # Cross-folder entries: remove URL is in folders the keep URL is not → user picks.
            if only_in_remove:
                all_folders = {fid: fname for fid, fname in url_folders.get(keep, []) + url_folders.get(remove, [])}
                cross_folder.append({
                    "keep": keep,
                    "remove": remove,
                    "keep_folders": [{"id": fid, "name": fname} for fid, fname in url_folders.get(keep, [])],
                    "remove_folders": [{"id": fid, "name": fname} for fid, fname in url_folders.get(remove, []) if fid in only_in_remove],
                    "all_folders": sorted(
                        [{"id": fid, "name": fname} for fid, fname in all_folders.items()],
                        key=lambda x: x["name"],
                    ),
                })

    # Upgradable: URL carries a format-selector query param (e.g. ?alt=rss)
    # whose canonical form is not already subscribed anywhere.
    # Trailing-slash-only differences are intentionally excluded — those are
    # handled by the same/cross-folder dedup logic above.
    upgradable: list[dict] = []
    for url, folders in url_folders.items():
        canonical = normalize_feed_url(url)
        if canonical == url or canonical in url_folders:
            continue
        # Skip if only the path changed (trailing slash stripped) — query is unchanged.
        if urlparse(url).query == urlparse(canonical).query:
            continue
        upgradable.append({
            "current": url,
            "upgrade_to": canonical,
            "folders": [{"id": fid, "name": fname} for fid, fname in folders],
        })

    return JSONResponse({"same_folder": same_folder, "cross_folder": cross_folder, "upgradable": upgradable})


@app.post("/feeds/deduplicate")
async def deduplicate_feeds(request: Request):
    """Remove slash-duplicate feeds and optionally upgrade format-selector URLs.

    Body (JSON):
      cross_folder_choices: list of {keep, remove, folder_ids} — user-selected folder assignments.
      upgrade_choices: list of {current, upgrade_to} — feeds to switch from RSS to Atom URL.
    """
    body = await request.json()
    cross_choices: list[dict] = body.get("cross_folder_choices", [])
    upgrade_choices: list[dict] = body.get("upgrade_choices", [])

    data = get_feed_duplicates()
    import json as _json
    dup_data = _json.loads(data.body)
    same = dup_data["same_folder"]

    removed: list[dict] = []

    # Same-folder: auto-remove slash variant from the shared folder.
    for dup in same:
        feed_url = dup["remove"]
        folder_id = dup["folder_id"]
        with get_meta_connection() as conn:
            conn.execute(
                "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                (folder_id, feed_url),
            )
            still_used = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1", (feed_url,)
            ).fetchone()
        if not still_used:
            with get_reader() as reader:
                reader.delete_feed(feed_url, missing_ok=True)
        removed.append({"removed": feed_url, "kept": dup["keep"]})
        LOGGER.info("[deduplicate] same-folder: removed %s from folder %d", feed_url, folder_id)

    # Cross-folder: apply user's folder choices.
    for choice in cross_choices:
        keep = choice["keep"]
        remove = choice["remove"]
        target_folder_ids: list[int] = choice.get("folder_ids", [])
        # Remove the slash variant from all its folders.
        with get_meta_connection() as conn:
            conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (remove,))
            still_used = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1", (remove,)
            ).fetchone()
        if not still_used:
            with get_reader() as reader:
                reader.delete_feed(remove, missing_ok=True)
        # Ensure the canonical URL is in each selected folder.
        with get_reader() as reader:
            reader.add_feed(keep, exist_ok=True)
        with get_meta_connection() as conn:
            for fid in target_folder_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                    (fid, keep),
                )
        removed.append({"removed": remove, "kept": keep, "folders": target_folder_ids})
        LOGGER.info("[deduplicate] cross-folder: removed %s, kept %s in folders %s", remove, keep, target_folder_ids)

    # Format upgrades: replace the RSS-variant URL with its Atom canonical in-place.
    upgraded: list[dict] = []
    for choice in upgrade_choices:
        current = choice["current"]
        upgrade_to = choice["upgrade_to"]
        with get_meta_connection() as conn:
            folder_ids = [r[0] for r in conn.execute(
                "SELECT folder_id FROM folder_feeds WHERE feed_url = ?", (current,)
            ).fetchall()]
        # Add canonical Atom URL to the same folders.
        with get_reader() as reader:
            reader.add_feed(upgrade_to, exist_ok=True)
        with get_meta_connection() as conn:
            for fid in folder_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                    (fid, upgrade_to),
                )
        # Remove the RSS variant from all folders; delete if no longer referenced.
        with get_meta_connection() as conn:
            conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (current,))
            still_used = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1", (current,)
            ).fetchone()
        if not still_used:
            with get_reader() as reader:
                reader.delete_feed(current, missing_ok=True)
        upgraded.append({"from": current, "to": upgrade_to})
        LOGGER.info("[deduplicate] upgraded %s → %s", current, upgrade_to)

    invalidate_meta_structure_cache()
    return JSONResponse({"removed": removed, "count": len(removed), "upgraded": upgraded, "upgraded_count": len(upgraded)})


@app.post("/refresh")
def refresh(
    folder_id: int = Form(...),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    list_feed_url: str | None = Form(default=None),
    feed_url: str | None = Form(default=None),
    entry_id: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    sort_query = (
        f"&sort_by={quote_plus(normalized_sort_by)}"
        f"&sort_dir={quote_plus(normalized_sort_dir)}"
        f"&read_filter={quote_plus(normalized_read_filter)}"
        f"{star_only_query}"
        f"{resume_read_filter_query}"
    )
    entry_query = ""
    if feed_url and entry_id:
        entry_query = f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}"
    retry_after_seconds = check_and_mark_manual_refresh()
    if retry_after_seconds > 0:
        return RedirectResponse(
            url=(
                f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{entry_query}&message="
                f"{quote_plus(f'Refresh throttled. Try again in {retry_after_seconds}s.')}"
            ),
            status_code=303,
        )

    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)
    if REFRESH_DEBUG_ENABLED:
        LOGGER.info(
            "[refresh] manual folder refresh: folder_id=%s feed_count=%d list_feed_url=%s tag=%s",
            folder_id,
            len(feed_urls),
            list_feed_url,
            normalized_tag,
        )
    with get_meta_connection() as conn:
        scraper_service.refresh_all_scraped_feeds(conn)
    feed_refresh_service.update_feeds(feed_urls)
    _run_automation_after_refresh(feed_urls)
    return RedirectResponse(
        url=(f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{entry_query}&message={quote_plus('Refresh complete.')}"),
        status_code=303,
    )


@app.post("/refresh/feed")
def refresh_feed(
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    entry_id: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)
    retry_after_seconds = 0 if _is_local_dev_feed(feed_url) else check_and_mark_manual_refresh()
    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = (
        f"&sort_by={quote_plus(normalized_sort_by)}"
        f"&sort_dir={quote_plus(normalized_sort_dir)}"
        f"&read_filter={quote_plus(normalized_read_filter)}"
        f"{star_only_query}"
        f"{resume_read_filter_query}"
    )
    entry_query = f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}" if entry_id else ""
    if retry_after_seconds > 0:
        return RedirectResponse(
            url=(
                f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{entry_query}&message="
                f"{quote_plus(f'Refresh throttled. Try again in {retry_after_seconds}s.')}"
            ),
            status_code=303,
        )

    if REFRESH_DEBUG_ENABLED:
        LOGGER.info(
            "[refresh] manual single-feed refresh: folder_id=%s feed_url=%s list_feed_url=%s tag=%s",
            folder_id,
            feed_url,
            list_feed_url,
            normalized_tag,
        )
    feed_id = scraper_service.scraped_feed_id_from_url(feed_url)
    if feed_id:
        with get_meta_connection() as conn:
            scraper_service.refresh_scraped_feed_by_id(conn, feed_id)
    feed_refresh_service.update_feeds([feed_url])
    _run_automation_after_refresh({feed_url})
    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{entry_query}&message={quote_plus('Feed refresh complete.')}"
        ),
        status_code=303,
    )


@app.post("/folders/mark-read")
def mark_folder_as_read(
    request: Request,
    folder_id: int = Form(...),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = ""
    if sort_by is not None or sort_dir is not None:
        sort_query = f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}" if read_filter is not None else ""
    star_only_query = build_star_only_query(star_only) if star_only is not None else ""
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter) if resume_read_filter is not None else ""
    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)

    marked_count = mark_feeds_as_read(feed_urls)
    with unread_counts_cache_lock:
        global _unread_counts_generation
        _unread_counts_generation += 1
        unread_counts_cache.clear()
    message = "All posts already read." if marked_count == 0 else f"Marked {marked_count} posts as read."
    if is_async_action_request(request, "lectio-mark-read"):
        return JSONResponse({"ok": True, "marked": marked_count, "message": message})
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}",
        status_code=303,
    )


@app.post("/feeds/mark-read")
def mark_feed_as_read(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    marked_count = mark_feeds_as_read({feed_url})
    with unread_counts_cache_lock:
        global _unread_counts_generation
        _unread_counts_generation += 1
        unread_counts_cache.clear()
    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = ""
    if sort_by is not None or sort_dir is not None:
        sort_query = f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}" if read_filter is not None else ""
    star_only_query = build_star_only_query(star_only) if star_only is not None else ""
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter) if resume_read_filter is not None else ""
    message = "All posts already read." if marked_count == 0 else f"Marked {marked_count} posts as read."
    if is_async_action_request(request, "lectio-mark-read"):
        return JSONResponse({"ok": True, "marked": marked_count, "feed_url": feed_url, "message": message})
    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}"
            f"{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@app.post("/entries/read")
def mark_entry_read(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    read: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    select_entry: int = Form(default=1),
):
    normalized_tag = normalize_tag_value(tag)
    with get_reader() as reader:
        if read:
            reader.mark_entry_as_read((feed_url, entry_id))
            try:
                upsert_entry_read_state(feed_url, entry_id)
            except Exception:
                LOGGER.warning("upsert_entry_read_state failed in mark_entry_read (db contention?)", exc_info=True)
            if is_async_action_request(request, "lectio-entry-read-toggle"):
                entry_obj = reader.get_entry((feed_url, entry_id), None)
                feed_obj = reader.get_feed(feed_url, None)
                append_read_history(
                    feed_url,
                    entry_id,
                    str(getattr(entry_obj, "title", None) or ""),
                    str(getattr(entry_obj, "link", None) or ""),
                    str(getattr(feed_obj, "title", None) or ""),
                )
        else:
            reader.mark_entry_as_unread((feed_url, entry_id))
            try:
                delete_entry_read_state(feed_url, entry_id)
            except Exception:
                LOGGER.warning("delete_entry_read_state failed in mark_entry_read (db contention?)", exc_info=True)
    with unread_counts_cache_lock:
        global _unread_counts_generation
        _unread_counts_generation += 1
        unread_counts_cache.clear()

    if is_async_action_request(request, "lectio-post-read-toggle") or is_async_action_request(request, "lectio-entry-read-toggle"):
        return JSONResponse({"ok": True, "feed_url": feed_url, "entry_id": entry_id, "read": bool(read)})

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}"
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)

    entry_query = f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}" if select_entry else ""

    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}",
        status_code=303,
    )


@app.post("/entries/saved")
def toggle_entry_saved(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    saved: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    select_entry: int = Form(default=1),
):
    normalized_tag = normalize_tag_value(tag)
    with get_meta_connection() as conn:
        if saved:
            conn.execute(
                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                (feed_url, entry_id),
            )
        else:
            conn.execute(
                "DELETE FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )
        conn.commit()

    # Mirror the save state into the archive: queue a capture when starred,
    # mark for later removal when unstarred. The archive worker handles the
    # actual fetching off-request.
    try:
        if saved:
            starred_archive_service.enqueue_archive(feed_url, entry_id)
        else:
            starred_archive_service.enqueue_removal(feed_url, entry_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("starred archive enqueue failed for %s/%s: %s", feed_url, entry_id, exc)

    if is_async_action_request(request, "lectio-post-save-toggle"):
        return JSONResponse({"ok": True, "feed_url": feed_url, "entry_id": entry_id, "saved": bool(saved)})

    if is_async_action_request(request, "lectio-entry-save-toggle"):
        return JSONResponse({"ok": True, "feed_url": feed_url, "entry_id": entry_id, "saved": bool(saved)})

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}"
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)

    entry_query = f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}" if select_entry else ""

    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}",
        status_code=303,
    )


@app.post("/entries/tags")
def set_entry_manual_tags(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    tags_text: str = Form(default=""),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    select_entry: int = Form(default=1),
    append_mode: int = Form(default=0),
):
    if append_mode:
        existing_tags = get_manual_tags_for_entry(feed_url, entry_id)
        appended_tags = parse_manual_hashtags(tags_text)
        merged_tags: list[str] = []
        seen: set[str] = set()
        for _tok in existing_tags + appended_tags:
            normalized = normalize_tag_value(_tok)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged_tags.append(normalized)
            if len(merged_tags) >= MAX_MANUAL_TAGS:
                break
        tags = set_manual_tags_for_entry(feed_url, entry_id, " ".join(merged_tags))
    else:
        tags = set_manual_tags_for_entry(feed_url, entry_id, tags_text)
    normalized_tag = normalize_tag_value(tag)  # `tag` = the active tag filter, not the loop var

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}"
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)

    entry_query = f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}" if select_entry else ""
    message = "Tags updated." if tags else "Tags cleared."

    if request.headers.get("X-Requested-With") == "lectio-ajax":
        return JSONResponse({"ok": True, "tags": tags})

    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@app.post("/entries/mark-range-read")
def mark_entries_range_read(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    direction: str = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_star_only = normalize_star_only(star_only)
    normalized_tag = normalize_tag_value(tag)

    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)

    filtered_feed_urls = filter_feed_urls(feed_urls, list_feed_url)
    posts = list_entries_for_feeds(
        filtered_feed_urls,
        sort_by=normalized_sort_by,
        sort_dir=normalized_sort_dir,
        read_filter=normalized_read_filter,
        star_only=normalized_star_only,
        selected_tag=normalized_tag,
    )

    anchor_index = next(
        (index for index, post in enumerate(posts) if post["feed_url"] == feed_url and post["id"] == entry_id),
        None,
    )

    # If the anchor is missing in the filtered list (e.g. stale read_filter
    # state in the client), retry against the same scoped list with read_filter
    # relaxed to all so range actions still resolve around the selected post.
    if anchor_index is None and normalized_read_filter != "all":
        posts = list_entries_for_feeds(
            filtered_feed_urls,
            sort_by=normalized_sort_by,
            sort_dir=normalized_sort_dir,
            read_filter="all",
            star_only=normalized_star_only,
            selected_tag=normalized_tag,
        )
        anchor_index = next(
            (index for index, post in enumerate(posts) if post["feed_url"] == feed_url and post["id"] == entry_id),
            None,
        )

    if anchor_index is None:
        message = "Could not find that post in the current view."
    else:
        if direction == "above":
            target_posts = posts[:anchor_index]
        elif direction == "below":
            target_posts = posts[anchor_index + 1 :]
        else:
            target_posts = []

        marked_count = 0
        if target_posts:
            to_sync: list[tuple[str, str]] = []
            with get_reader() as reader:
                for post in target_posts:
                    if post["read"]:
                        continue
                    try:
                        reader.mark_entry_as_read((post["feed_url"], post["id"]))
                    except Exception:
                        continue
                    to_sync.append((post["feed_url"], post["id"]))
            if to_sync:
                when = datetime.now().isoformat()
                with get_meta_connection() as conn:
                    conn.executemany(
                        """
                        INSERT INTO entry_read_state (feed_url, entry_id, read_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at
                        """,
                        [(fu, eid, when) for fu, eid in to_sync],
                    )
            marked_count = len(to_sync)

        if direction not in {"above", "below"}:
            message = "Invalid range option."
        elif marked_count == 0:
            message = "No unread posts in that range."
        else:
            range_label = "above" if direction == "above" else "below"
            message = f"Marked {marked_count} posts {range_label} as read."

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = f"&sort_by={quote_plus(normalized_sort_by)}&sort_dir={quote_plus(normalized_sort_dir)}"
    read_filter_query = f"&read_filter={quote_plus(normalized_read_filter)}"
    star_only_query = build_star_only_query(normalized_star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)
    entry_query = f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}"

    if is_async_action_request(request, "lectio-post-range-read"):
        return JSONResponse({"ok": True, "message": message, "feed_url": feed_url, "entry_id": entry_id, "direction": direction})

    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@app.post("/entries/mark-older-than-read")
def mark_entries_older_than_read(
    request: Request,
    folder_id: int = Form(...),
    max_age_days: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)
    filtered_feed_urls = filter_feed_urls(feed_urls, list_feed_url)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    marked_count = 0
    to_sync: list[tuple[str, str]] = []
    with get_reader() as reader:
        for fu in filtered_feed_urls:
            for entry in reader.get_entries(feed=fu, read=False):
                date = entry.published or entry.updated
                if date is None:
                    continue
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if date >= cutoff:
                    continue
                try:
                    reader.mark_entry_as_read((entry.feed_url, entry.id))
                except Exception:
                    continue
                to_sync.append((entry.feed_url, entry.id))
                marked_count += 1

    if to_sync:
        when = datetime.now().isoformat()
        with get_meta_connection() as conn:
            conn.executemany(
                """
                INSERT INTO entry_read_state (feed_url, entry_id, read_at)
                VALUES (?, ?, ?)
                ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at
                """,
                [(fu, eid, when) for fu, eid in to_sync],
            )
        global _unread_counts_generation
        _unread_counts_generation += 1
        unread_counts_cache.clear()

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = (
        f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
        if sort_by is not None or sort_dir is not None
        else ""
    )
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}" if read_filter is not None else ""
    star_only_query = build_star_only_query(star_only) if star_only is not None else ""
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter) if resume_read_filter is not None else ""
    message = "No unread posts older than that." if marked_count == 0 else f"Marked {marked_count} posts as read."
    if is_async_action_request(request, "lectio-mark-read"):
        return JSONResponse({"ok": True, "marked": marked_count, "max_age_days": max_age_days, "message": message})
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}",
        status_code=303,
    )


@app.post("/entries/mark-newer-than-unread")
def mark_entries_newer_than_unread(
    request: Request,
    min_age_days: int = Form(...),
    folder_id: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)
    filtered_feed_urls = filter_feed_urls(feed_urls, list_feed_url)

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    unmarked_count = 0
    to_delete: list[tuple[str, str]] = []
    with get_reader() as reader:
        for fu in filtered_feed_urls:
            for entry in reader.get_entries(feed=fu, read=True):
                date = entry.published or entry.updated
                if date is None:
                    continue
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if date < cutoff:
                    continue
                try:
                    reader.mark_entry_as_unread((entry.feed_url, entry.id))
                except Exception:
                    continue
                to_delete.append((entry.feed_url, entry.id))
                unmarked_count += 1

    if to_delete:
        with get_meta_connection() as conn:
            conn.executemany(
                "DELETE FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
                to_delete,
            )
        global _unread_counts_generation
        _unread_counts_generation += 1
        unread_counts_cache.clear()

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = (
        f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
        if sort_by is not None or sort_dir is not None
        else ""
    )
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}" if read_filter is not None else ""
    star_only_query = build_star_only_query(star_only) if star_only is not None else ""
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter) if resume_read_filter is not None else ""
    message = "No read posts newer than that." if unmarked_count == 0 else f"Marked {unmarked_count} posts as unread."
    if is_async_action_request(request, "lectio-mark-read"):
        return JSONResponse({"ok": True, "unmarked": unmarked_count, "min_age_days": min_age_days, "message": message})
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}",
        status_code=303,
    )


@app.post("/settings/auto-refresh")
def update_auto_refresh_setting(
    refresh_minutes: int = Form(...),
    folder_id: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    if refresh_minutes not in AUTO_REFRESH_OPTION_MINUTES:
        message = "Invalid auto-refresh interval."
    else:
        normalized_minutes = normalize_auto_refresh_minutes(refresh_minutes)
        with get_meta_connection() as conn:
            set_setting(conn, AUTO_REFRESH_SETTING_KEY, str(normalized_minutes))
        app.state.auto_refresh_minutes = normalized_minutes
        app.state.last_scheduled_refresh_started_at = time.monotonic()
        if normalized_minutes <= 0:
            message = "Auto-refresh disabled."
        else:
            message = f"Auto-refresh set to {normalized_minutes // 60}h."

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    return RedirectResponse(
        url=(f"/?folder_id={folder_id}{list_feed_query}{tag_query}&message={quote_plus(message)}"),
        status_code=303,
    )


@app.post("/settings/global-note")
def update_global_note_setting(
    request: Request,
    note_text: str = Form(default=""),
    folder_id: int | None = Form(default=None),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    q: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_query = normalize_search_query(q)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)
    trimmed_note = note_text.strip()

    with get_meta_connection() as conn:
        set_setting(conn, GLOBAL_NOTE_SETTING_KEY, trimmed_note)

    if is_async_action_request(request, "lectio-global-note-save"):
        return JSONResponse(
            {
                "ok": True,
                "message": "Note saved.",
                "note_text": trimmed_note,
            }
        )

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    q_query = f"&q={quote_plus(normalized_query)}" if normalized_query else ""
    target_folder_id = folder_id
    if target_folder_id is None:
        with get_meta_connection() as conn:
            target_folder_id = get_root_folder_id(conn)
    return RedirectResponse(
        url=(
            f"/?folder_id={target_folder_id}{list_feed_query}{tag_query}"
            f"&sort_by={quote_plus(normalized_sort_by)}"
            f"&sort_dir={quote_plus(normalized_sort_dir)}"
            f"&read_filter={quote_plus(normalized_read_filter)}"
            f"{star_only_query}{resume_read_filter_query}{q_query}"
            f"&message={quote_plus('Note saved.')}"
        ),
        status_code=303,
    )


@app.post("/settings/problematic-feeds/viewed")
def mark_problematic_feeds_viewed(request: Request):
    viewed_at = time.time()
    with get_meta_connection() as conn:
        set_setting(conn, PROBLEMATIC_FEEDS_LAST_VIEWED_AT_SETTING_KEY, str(viewed_at))

    if is_async_action_request(request, "lectio-problematic-feeds-viewed"):
        return JSONResponse({"ok": True, "viewed_at": viewed_at})

    return RedirectResponse(url="/", status_code=303)


@app.post("/settings/problematic-feeds/acknowledge")
def acknowledge_problematic_feed(request: Request, feed_url: str = Form(...)):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE feed_failure_state SET acknowledged_at = ? WHERE feed_url = ?",
            (time.time(), feed_url),
        )
    invalidate_problematic_feeds_cache()
    if is_async_action_request(request, "lectio-problem-feed-ack"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings/problematic-feeds/unacknowledge")
def unacknowledge_problematic_feed(request: Request, feed_url: str = Form(...)):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE feed_failure_state SET acknowledged_at = NULL WHERE feed_url = ?",
            (feed_url,),
        )
    invalidate_problematic_feeds_cache()
    if is_async_action_request(request, "lectio-problem-feed-unack"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=303)


@app.post("/entries/email")
def email_entry(
    request: Request,
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    to_addr: str = Form(...),
):
    if not is_email_configured():
        return JSONResponse({"ok": False, "error": "Email not configured."}, status_code=503)

    to_addr = to_addr.strip()
    if not to_addr:
        return JSONResponse({"ok": False, "error": "No recipient address."}, status_code=400)

    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)

    if not entry:
        return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)

    title = entry.title or ""
    link = entry.link or ""
    feed_title = (entry.feed.title if entry.feed else None) or ""

    # Prefer plain-text summary; fall back to stripping HTML content.
    excerpt = ""
    if entry.summary:
        excerpt = re.sub(r"<[^>]+>", " ", entry.summary)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()
    elif entry.content:
        raw = entry.content[0].value if entry.content else ""
        excerpt = re.sub(r"<[^>]+>", " ", raw)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if excerpt and len(excerpt) > 300:
        excerpt = excerpt[:297] + "…"

    ok, error = send_article_email(
        api_key=get_resend_api_key(),
        from_addr=get_resend_from(),
        to_addr=to_addr,
        title=title,
        feed_title=feed_title,
        link=link,
        excerpt=excerpt,
    )
    if ok:
        return JSONResponse({"ok": True, "message": f"Sent to {to_addr}"})
    LOGGER.warning("email send failed for %s/%s: %s", feed_url, entry_id, error)
    return JSONResponse({"ok": False, "error": error or "Send failed."}, status_code=500)


@app.post("/entries/instapaper")
def save_to_instapaper(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
):
    username = get_runtime_setting(SETTING_INSTAPAPER_USERNAME).strip()
    password = get_runtime_setting(SETTING_INSTAPAPER_PASSWORD).strip()
    if not (username and password):
        return JSONResponse({"ok": False, "error": "Instapaper not configured."}, status_code=503)

    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)

    if not entry:
        return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)

    url = entry.link or ""
    if not url:
        return JSONResponse({"ok": False, "error": "Entry has no URL."}, status_code=400)

    try:
        import urllib.request, urllib.parse, urllib.error
        data = urllib.parse.urlencode({
            "username": username,
            "password": password,
            "url": url,
            "title": entry.title or "",
        }).encode()
        req = urllib.request.Request(
            "https://www.instapaper.com/api/add",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        if status in (200, 201):
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": f"Instapaper returned {status}."}, status_code=502)
    except Exception as exc:
        LOGGER.warning("Instapaper save failed for %s: %s", url, exc)
        return JSONResponse({"ok": False, "error": str(exc) or "Request failed."}, status_code=502)


@app.post("/opml/import")
async def opml_import(opml_file: Annotated[UploadFile, File(...)]):
    data = await opml_file.read()
    with get_meta_connection() as conn:
        imported = import_opml(conn, data)
        root_id = get_root_folder_id(conn)
    invalidate_meta_structure_cache()

    return RedirectResponse(
        url=f"/?folder_id={root_id}&message={quote_plus(f'Imported {imported} feed(s) from OPML.')}",
        status_code=303,
    )


@app.get("/opml/export")
def opml_export():
    with get_meta_connection() as conn:
        text = export_opml_text(conn)
    return Response(
        content=text,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=lectio-export.opml"},
    )


@app.get("/takeout/export")
def takeout_export():
    with get_meta_connection() as conn:
        opml_text = export_opml_text(conn)
        zip_bytes = takeout_service.build_takeout_zip(
            conn, READER_DB_PATH, opml_text, app_version=STATIC_ASSET_VERSION
        )
    date_str = datetime.now().strftime("%Y%m%d")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=lectio-takeout-{date_str}.zip"},
    )


@app.post("/takeout/import")
async def takeout_import(request: Request, takeout_file: Annotated[UploadFile, File(...)]):
    data = await takeout_file.read()
    try:
        with get_meta_connection() as conn:
            summary = takeout_service.import_takeout_zip(conn, READER_DB_PATH, data)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/?message={quote_plus(str(exc))}",
            status_code=303,
        )
    parts = [f"{v} {k.replace('_', ' ')}" for k, v in summary.items() if v]
    msg = "Takeout imported: " + ", ".join(parts) if parts else "Takeout imported (nothing new to add)."
    return RedirectResponse(url=f"/?message={quote_plus(msg)}", status_code=303)


@app.get("/api/unread-counts")
def api_unread_counts() -> JSONResponse:
    """Return per-feed unread counts. Used by the client to refresh sidebar
    badges after bulk mark-read actions that may affect off-screen entries."""
    counts = get_unread_counts_by_feed()
    return JSONResponse(counts)


@app.get("/api/img")
async def api_img_proxy(u: str) -> Response:
    """Server-side image proxy.

    Fetches external images on behalf of the browser so that
    Cross-Origin-Resource-Policy restrictions (same-site / same-origin) set by
    the image server do not prevent them from loading in the entry pane.
    Only http:// and https:// URLs are accepted; private / loopback addresses
    are blocked to prevent SSRF.
    """
    import ipaddress
    import socket as _socket
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https"):
        return Response(status_code=400)
    host = parsed.hostname or ""
    try:
        addr_infos = _socket.getaddrinfo(host, None, type=_socket.SOCK_STREAM)
        for _fam, _typ, _proto, _can, _sockaddr in addr_infos:
            ip = ipaddress.ip_address(_sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return Response(status_code=403)
    except Exception:
        return Response(status_code=400)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            resp = await client.get(
                u,
                headers={"User-Agent": READABILITY_USER_AGENT},
            )
            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                return Response(status_code=422)
            cache_ctrl = resp.headers.get("cache-control", "public, max-age=86400")
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={"Cache-Control": cache_ctrl},
            )
    except Exception:
        return Response(status_code=502)


@app.get("/healthz")
def healthz():
    """Liveness probe for reverse proxies (Traefik, etc.). Returns 200 as long
    as the process is serving requests. Intentionally does NOT touch the DB:
    under bulk-refresh load the meta DB can be locked for several seconds, and
    a probe that waits on it will time out and cause the proxy to withdraw the
    backend even though the app is still functioning."""
    return JSONResponse({"status": "ok"})


@app.get("/stats")
def get_stats():
    def _db_bytes(*paths: Path) -> int:
        total = 0
        for p in paths:
            for f in p.parent.glob(p.name + "*"):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    with get_meta_connection() as conn:
        feed_count = conn.execute("SELECT COUNT(DISTINCT feed_url) FROM folder_feeds").fetchone()[0]
        # Exclude the root folder from the folder count
        root_id = get_root_folder_id(conn)
        folder_count = conn.execute("SELECT COUNT(*) FROM folders WHERE id != ?", (root_id,)).fetchone()[0]
        saved_count = conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0]

    with get_reader() as reader:
        counts = reader.get_entry_counts()
        entry_total = counts.total
        entry_read = counts.read
        entry_unread = entry_total - entry_read

    reader_db_bytes = _db_bytes(READER_DB_PATH)
    meta_db_bytes = _db_bytes(META_DB_PATH)
    thumb_db_bytes = _db_bytes(THUMB_DB_PATH)
    starred_archive_db_bytes = _db_bytes(STARRED_ARCHIVE_DB_PATH)
    archive_stats = starred_archive_service.get_stats()

    thumb_count = 0
    try:
        with get_thumb_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM thumb_cache").fetchone()
            thumb_count = int(row["c"]) if row else 0
    except Exception:
        pass

    return JSONResponse(
        {
            "feed_count": feed_count,
            "folder_count": folder_count,
            "entry_total": entry_total,
            "entry_unread": entry_unread,
            "entry_read": entry_read,
            "entry_saved": saved_count,
            "reader_db_bytes": reader_db_bytes,
            "meta_db_bytes": meta_db_bytes,
            "thumb_db_bytes": thumb_db_bytes,
            "thumb_count": thumb_count,
            "starred_archive_db_bytes": starred_archive_db_bytes,
            "starred_archive_complete": archive_stats["complete"],
            "starred_archive_pending": archive_stats["pending"],
            "starred_archive_in_progress": archive_stats["in_progress"],
            "starred_archive_failed": archive_stats["failed"],
            "starred_archive_pending_removal": archive_stats["pending_removal"],
            "starred_archive_asset_count": archive_stats["asset_count"],
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
