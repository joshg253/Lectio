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
from urllib.parse import quote_plus, urlparse

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

from services.feed_refresh import FeedRefreshService
from services.lead_images import LeadImageService
from services.reader_api import ReaderApi
from services.starred_archive import StarredArchiveService
from services.youtube import YouTubeDurationService

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
AUTO_REFRESH_OPTION_MINUTES = (0, 15, 30, 60, 360, 720)
SCHEDULER_POLL_SECONDS = 30
DEFAULT_SORT_BY = "post"
DEFAULT_SORT_DIR = "asc"
CHUNK_SIZE = 10
READABILITY_USER_AGENT = "Lectio/0.1 (+https://localhost)"
MANUAL_TAG_KEY_PREFIX = "lectio.manual_tag."
MAX_MANUAL_TAGS = 12
MAX_FEED_TAG_SUGGESTIONS = 8
FEED_TAG_SUGGESTION_CACHE_TTL_SECONDS = 900
TAG_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$")
STATIC_ASSET_VERSION = os.getenv("LECTIO_ASSET_VERSION", "20260513c")
REFRESH_DEBUG_ENABLED = os.getenv("LECTIO_REFRESH_DEBUG", "0") == "1"
DEBUG_MODE = os.getenv("LECTIO_DEBUG", "0") == "1"

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
_AUTH_EXEMPT_PREFIXES = ("/login", "/static", "/healthz")

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
dedupe_log_cache_lock = threading.Lock()
dedupe_log_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}
dedupe_log_compute_lock = threading.Lock()
dedupe_log_refresh_inflight: dict[str, bool] = {}
DEDUPE_LOG_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_DEDUPE_LOG_CACHE_TTL", "300"))
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
    app.state.last_scheduled_refresh_started_at = time.monotonic()

    # Ensure reader db is created at startup.
    with get_reader():
        pass

    # Warm YouTube duration in-memory cache from DB so first renders are instant.
    youtube_duration_service.warm_cache_from_db()

    # Warm lead image cache from DB so thumbnails are available on first render.
    lead_image_service.warm_cache_from_db()

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
        starred_archive_service.backfill_missing_archives()
        starred_archive_service.backfill_metadata_for_complete_rows()

    threading.Thread(
        target=_archive_backfill_task,
        daemon=True,
        name="starred-archive-backfill",
    ).start()

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
_CSRF_EXEMPT_PREFIXES = ("/login", "/static", "/healthz")
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
            if status in self._SUPPRESS_STATUSES and "list_feed_url=" in request_path:
                return
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

    PASSTHROUGH_PREFIXES = ("/healthz", "/static", "/login")

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
        root = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id IS NULL",
            (ROOT_FOLDER_NAME,),
        ).fetchone()
        if not root:
            conn.execute(
                "INSERT INTO folders (name, parent_id) VALUES (?, NULL)",
                (ROOT_FOLDER_NAME,),
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
    return conn.execute(
        """
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
        WHERE f.parent_id IS NULL OR f.parent_id = (
            SELECT id FROM folders WHERE name = ? AND parent_id IS NULL
        )
        ORDER BY path
        """,
        (ROOT_FOLDER_NAME,),
    ).fetchall()


def get_folder_options(conn: sqlite3.Connection) -> list[FolderOption]:
    rows = conn.execute(
        """
        SELECT
            f.id,
            f.name,
            CASE WHEN f.parent_id IS NULL THEN 0 ELSE 1 END AS depth,
            CASE WHEN f.parent_id IS NULL THEN f.name ELSE root.name || ' / ' || f.name END AS path
        FROM folders f
        LEFT JOIN folders root ON f.parent_id = root.id
        WHERE f.parent_id IS NULL OR f.parent_id = (
            SELECT id FROM folders WHERE name = ? AND parent_id IS NULL
        )
        ORDER BY path
        """,
        (ROOT_FOLDER_NAME,),
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


def _refresh_dedupe_log_async(limit: int) -> None:
    cache_key = f"limit={int(limit)}"
    try:
        result = _compute_unread_dedupe_log(limit)
        with dedupe_log_cache_lock:
            dedupe_log_cache[cache_key] = (time.time(), [dict(row) for row in result])
    except Exception:
        LOGGER.exception("background dedupe log refresh failed")
    finally:
        with dedupe_log_compute_lock:
            dedupe_log_refresh_inflight[cache_key] = False


def get_unread_dedupe_log(limit: int = 100) -> list[dict[str, object]]:
    """Stale-while-revalidate, same as get_unread_counts_by_feed."""
    cache_key = f"limit={int(limit)}"
    now = time.time()
    with dedupe_log_cache_lock:
        cached = dedupe_log_cache.get(cache_key)
    if cached:
        ts, value = cached
        if now - ts < DEDUPE_LOG_CACHE_TTL_SECONDS:
            return [dict(row) for row in value]
        with dedupe_log_compute_lock:
            if not dedupe_log_refresh_inflight.get(cache_key):
                dedupe_log_refresh_inflight[cache_key] = True
                threading.Thread(target=_refresh_dedupe_log_async, args=(limit,), daemon=True).start()
        return [dict(row) for row in value]

    with dedupe_log_compute_lock:
        with dedupe_log_cache_lock:
            cached = dedupe_log_cache.get(cache_key)
            if cached:
                return [dict(row) for row in cached[1]]
        result = _compute_unread_dedupe_log(limit)
        with dedupe_log_cache_lock:
            dedupe_log_cache[cache_key] = (time.time(), [dict(row) for row in result])
        return [dict(row) for row in result]


def _compute_unread_dedupe_log(limit: int) -> list[dict[str, object]]:
    """Cross-feed unread dedupe report.

    Disabled on the home-render path: the only way to compute this is to
    iterate every unread entry, which is unworkable past a few thousand. The
    feature mainly catches RSS+Atom mirrors of the same source — uncommon
    enough to defer to an explicit user action. The dedupe modal will simply
    be empty; future work can wire it to an on-demand compute endpoint."""
    if os.getenv("LECTIO_ENABLE_DEDUPE_LOG", "0") != "1":
        return []
    seen_keys: set[str] = set()
    first_seen_by_key: dict[str, dict[str, str]] = {}
    duplicate_groups: dict[str, dict[str, object]] = {}

    with get_reader() as reader:
        for entry in reader.get_entries():
            if entry.read:
                continue

            dedupe_key = build_entry_dedupe_key(entry.link, entry.title)
            if not dedupe_key:
                continue

            normalized_link = normalize_entry_link_for_dedupe(entry.link)
            if dedupe_key in seen_keys:
                group = duplicate_groups.setdefault(
                    dedupe_key,
                    {
                        "link": normalized_link,
                        "collapsed_count": 0,
                        "feed_urls": set(),
                        "sample_titles": [],
                    },
                )

                group["collapsed_count"] = int(group["collapsed_count"]) + 1

                feed_urls = cast(set[str], group["feed_urls"])
                if entry.feed_url:
                    feed_urls.add(entry.feed_url)

                first_seen = first_seen_by_key.get(dedupe_key)
                if first_seen:
                    first_feed_url = first_seen.get("feed_url")
                    if first_feed_url:
                        feed_urls.add(first_feed_url)

                sample_titles = cast(list[str], group["sample_titles"])
                if first_seen:
                    first_title = first_seen.get("title")
                    if first_title and first_title not in sample_titles and len(sample_titles) < 3:
                        sample_titles.append(first_title)
                current_title = str(entry.title or "")
                if current_title and current_title not in sample_titles and len(sample_titles) < 3:
                    sample_titles.append(current_title)

                continue

            seen_keys.add(dedupe_key)
            first_seen_by_key[dedupe_key] = {
                "feed_url": str(entry.feed_url or ""),
                "title": str(entry.title or ""),
            }

    rows: list[dict[str, object]] = []
    for group in duplicate_groups.values():
        collapsed_count = int(group["collapsed_count"])
        rows.append(
            {
                "link": cast(str, group["link"]),
                "collapsed_count": collapsed_count,
                "total_occurrences": collapsed_count + 1,
                "feed_urls": sorted(cast(set[str], group["feed_urls"])),
                "sample_titles": cast(list[str], group["sample_titles"]),
            }
        )

    rows.sort(
        key=lambda row: (
            -int(cast(int, row["collapsed_count"])),
            str(cast(str, row["link"])),
        )
    )
    return rows[: max(1, int(limit))]


def normalize_entry_link_for_dedupe(link: str | None) -> str | None:
    if not link:
        return None
    normalized_link = str(link).split("#")[0].rstrip("/")
    return normalized_link or None


def normalize_entry_title_for_dedupe(title: str | None) -> str:
    if not title:
        return ""
    return " ".join(str(title).strip().lower().split())


def build_entry_dedupe_key(link: str | None, title: str | None) -> str | None:
    normalized_link = normalize_entry_link_for_dedupe(link)
    if not normalized_link:
        return None
    normalized_title = normalize_entry_title_for_dedupe(title)
    return f"{normalized_link}::{normalized_title}"


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

    # Fast path: zero manual tags exist anywhere → no per-entry scan needed.
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

    counts: dict[str, int] = {}
    with get_reader() as reader:
        for feed_url in feed_urls:
            for entry in reader.get_entries(feed=feed_url, limit=None):
                for tag in get_manual_tags_for_resource(reader, entry.resource_id):
                    counts[tag] = counts.get(tag, 0) + 1

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
        props = {
            "feed_url": feed_url,
            "found": True,
            "title": getattr(feed_obj, "resolved_title", None) or getattr(feed_obj, "title", None) or feed_url,
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


_IMG_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["\']([^"\']+)["\']')
_DIV_TAG_RE = re.compile(r'<(/?)div\b[^>]*>', re.IGNORECASE)
_AUDIO_SRC_RE = re.compile(r'<audio\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_KG_AUDIO_CARD_RE = re.compile(r'<div\b[^>]*\bkg-audio-card\b[^>]*>', re.IGNORECASE)


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
    proxy_style = (
        "<style>"
        "img[alt*='image unavailable' i],"
        "img[src*='grey-placeholder'],"
        "img[src*='placeholder']{display:none!important;}"
        "img[data-src],img[data-lazy-src],img[loading='lazy']{"
        "opacity:1!important;visibility:visible!important;filter:none!important;}"
        "</style>"
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


def build_readability_response(source_url: str) -> HTMLResponse:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return HTMLResponse("<h1>Unsupported URL scheme.</h1>", status_code=400)

    try:
        with httpx.Client(follow_redirects=True, timeout=12.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            response = client.get(source_url)
        response.raise_for_status()
        doc = Document(response.text)
        title = doc.short_title() or source_url
        summary = doc.summary(html_partial=True)
        article_html = sanitize_readability_html(summary).strip()
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
    with get_meta_connection() as conn:
        rows = conn.execute(
            f"SELECT feed_url, entry_id FROM saved_entries WHERE feed_url IN ({placeholders})",
            feed_url_values,
        ).fetchall()
        saved_entries_set = {(row["feed_url"], row["entry_id"]) for row in rows}
        if normalized_read_filter == "history" and not normalized_star_only:
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

        # Two strategies:
        #   - few feeds (e.g. user clicked one feed): query per feed with the
        #     SQL feed= filter. Avoids scanning every entry across the library.
        #   - many feeds (root / large folder): one global query and filter in
        #     Python. Avoids 2000+ tiny queries with their per-call overhead.
        all_feed_entries = []
        fetch_limit = max(1, int(limit))
        need_all = bool(search_terms or normalized_sort_dir == "asc")
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
                        str(getattr(entry, "author", None) or ""),
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

        rec.update(
            {
                "thumbnail_url": lead_image_service.extract_entry_thumbnail_url(entry, include_source_lookup=False),
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


def get_entry_detail(feed_url: str, entry_id: str) -> dict | None:
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            return _build_orphan_entry_detail(feed_url, entry_id)

        published_dt = entry.published or entry.updated or entry.added
        author_name = (getattr(entry, "author", None) or "").strip() or None

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
        # If we injected a YouTube embed for this entry, avoid showing a
        # separate lead image (typically the video thumbnail) above the
        # embedded player — it looks redundant and visually noisy.
        if video_id:
            lead_image_url = None

        # Remove inline images whose src URL is a logo, tracker, or avatar — these
        # are typically brand assets or analytics pixels embedded by feed publishers
        # and should not appear as article visuals in the reader.
        if isinstance(content_html, str):
            def _strip_bad_img(m: re.Match) -> str:
                src_m = re.search(r'\bsrc=(?:"([^"]*)"|\x27([^\x27]*)\x27)', m.group(0), re.IGNORECASE)
                if not src_m:
                    return m.group(0)
                src = src_m.group(1) or src_m.group(2) or ""
                if (
                    lead_image_service._LOGO_URL_PATTERNS.search(src)
                    or lead_image_service._TRACKER_URL_PATTERNS.search(src)
                    or lead_image_service._AVATAR_HINT_PATTERNS.search(src)
                ):
                    return ""
                return m.group(0)
            content_html = re.sub(r"<img\b[^>]*/?>", _strip_bad_img, content_html, flags=re.IGNORECASE) or None

        # Extract img title/alt text before opener stripping so content_html is intact.
        # Useful for comics where the hover text is the punchline (xkcd, etc.).
        # Checks content_html first, then entry.summary (xkcd: content is stripped away
        # but summary still has the img with title= attribute).
        image_title_text: str | None = None
        _img_title_re = re.compile(
            r'<img\b[^>]+\btitle=(?:"([^"]*)"|\x27([^\x27]*)\x27)',
            re.IGNORECASE,
        )
        for _search_html in [content_html, entry.summary]:
            if not isinstance(_search_html, str):
                continue
            _img_title_match = _img_title_re.search(_search_html)
            if _img_title_match:
                _candidate = html.unescape((_img_title_match.group(1) or _img_title_match.group(2) or "")).strip()
                if _candidate:
                    image_title_text = _candidate
                    break

        # Strip the opener thumbnail and dedup against the remaining content.
        # Order matters: strip the leading <img> first, then check if the lead
        # image URL still appears in what remains.  This prevents the case where
        # the lead image IS the opener thumbnail (e.g. comicsthumbs) from being
        # incorrectly suppressed just because it appears at the top of content.
        _LEAD_IMG_OPENER_RE = re.compile(
            r"^\s*(?:<!--.*?-->\s*)*"  # skip leading HTML comments (e.g. Ghost kg-card-begin)
            r"(?:<(?:p|figure|div)\b[^>]*>\s*)?"
            r"(?:<a\b[^>]*>\s*)?"
            r"<img\b[^>]*/?>",
            re.IGNORECASE | re.DOTALL,
        )
        if lead_image_url and isinstance(content_html, str):
            _m = _LEAD_IMG_OPENER_RE.match(content_html)
            if _m:
                # Always strip the opener, then check if lead URL appears in the body
                content_html = content_html[_m.end() :].lstrip() or None
                if content_html and (
                    lead_image_url in content_html or lead_image_url in html.unescape(content_html)
                ):
                    lead_image_url = None
            elif lead_image_url in content_html or lead_image_url in html.unescape(content_html):
                # Lead URL is in content but not at the opener position — suppress it
                # so the same image doesn't render both above and inside the content.
                lead_image_url = None

        # If lead_image_url came from source scraping and the remaining content
        # is essentially just a thumbnail wrapper (minimal text after stripping
        # all imgs), strip the inline img tags so thumbnails don't appear below
        # the full-size lead image.
        if lead_image_url and isinstance(content_html, str) and lead_image_url not in content_html:
            _no_imgs = re.sub(r"<img\b[^>]*/?>", "", content_html, flags=re.IGNORECASE)
            _text_only = re.sub(r"<[^>]+>", " ", _no_imgs)
            _text_only = html.unescape(re.sub(r"\s+", " ", _text_only)).strip()
            if len(_text_only) < 120:
                content_html = _no_imgs.strip() or None

        # Fallback: check the alt text on the main image on the source page.
        # Covers feeds that only supply a thumbnail in the content (e.g. Wilde Life)
        # where the alt text lives on the full-size img on the article page.
        # Uses the source HTML cache from lead image resolution — no extra HTTP call
        # when the entry was just opened for the first time.
        if image_title_text is None and lead_image_url and entry.link:
            image_title_text = lead_image_service.fetch_entry_image_alt(entry.link) or None

        # Inject image_title_text as alt attribute on the first <img> in content_html
        # when the image is inline (no separate lead_image_url, e.g. xkcd).
        if image_title_text and not lead_image_url and isinstance(content_html, str):

            def _inject_alt(m: re.Match) -> str:
                tag = m.group(0)
                if re.search(r"\balt\s*=", tag, re.IGNORECASE):
                    # Replace existing (possibly empty) alt value
                    tag = re.sub(
                        r'(\balt\s*=\s*)(?:"[^"]*"|\x27[^\x27]*\x27)',
                        lambda a: a.group(1) + '"' + image_title_text.replace('"', "&quot;") + '"',
                        tag,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                else:
                    tag = tag[:-1] + ' alt="' + image_title_text.replace('"', "&quot;") + '"' + tag[-1]
                return tag

            content_html = re.sub(r"<img\b[^>]*/?>", _inject_alt, content_html, count=1, flags=re.IGNORECASE)

        # SMBC: append the bonus panel image from the source page.
        if entry.link and "smbc-comics.com" in (entry.link or ""):
            _bonus_url = lead_image_service.fetch_smbc_bonus_panel_url(entry.link)
            if _bonus_url:
                _bonus_img = f'<p><img src="{html.escape(_bonus_url, quote=False)}" alt="Bonus panel" /></p>'
                content_html = (content_html or "") + _bonus_img

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

        _channel_link = getattr(entry.feed, "link", None) if hasattr(entry, "feed") else None
        _display_link = _rebase_proxy_entry_link(entry.link, feed_url, _channel_link)
        return {
            "feed_url": entry.feed_url,
            "id": entry.id,
            "title": entry.title,
            "link": _display_link,
            "summary": entry.summary,
            "content_html": content_html,
            "lead_image_url": lead_image_url,
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
        }


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
        upsert_entry_read_state(feed_url, entry_id)
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

    with get_reader() as reader:
        reader.add_feed(feed_url, exist_ok=True)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (folder_id, feed_url),
        )
    invalidate_meta_structure_cache()


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
    def _run() -> None:
        with updating_feeds_lock:
            if feed_url in updating_feeds:
                return
            updating_feeds.add(feed_url)
        try:
            feed_refresh_service.update_feeds([feed_url])
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
        if REFRESH_DEBUG_ENABLED:
            LOGGER.info(
                "[refresh] scheduled run triggered: interval_minutes=%d feed_count=%d",
                auto_refresh_minutes,
                len(feed_urls),
            )
        app.state.last_scheduled_refresh_started_at = time.monotonic()
        feed_refresh_service.update_feeds(feed_urls)


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
        dedupe_log_rows = get_unread_dedupe_log(limit=80)
        _tick("dedupe_log")
        unread_counts_by_folder = get_unread_counts_by_folder(
            raw_folder_rows,
            unread_counts_by_feed,
            direct_feed_urls_by_folder,
        )
        _tick("counts_by_folder")
        folder_rows = []
        for row in raw_folder_rows:
            folder_dict = dict(row)
            folder_dict["unread_count"] = unread_counts_by_folder.get(int(row["id"]), 0)
            folder_rows.append(folder_dict)
        global_note = get_setting(conn, GLOBAL_NOTE_SETTING_KEY) or ""
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
    for dedupe_row in dedupe_log_rows:
        feed_urls = cast(list[str], dedupe_row.get("feed_urls", []))
        dedupe_row["feed_titles"] = [feed_title_map.get(url, url) for url in feed_urls]
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
        feeds_by_folder[folder_row_id] = [
            FeedInFolder(
                url=url,
                title=feed_title_map.get(url, url),
                icon_url=get_favicon_url(url),
                unread_count=unread_counts_by_feed.get(url, 0),
            )
            for url in urls
        ]
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
    posts = list_entries_for_feeds(
        filtered_feed_urls,
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
            upsert_entry_read_state(feed_url, entry_id)
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
            "dedupe_log_rows": dedupe_log_rows,
            "dedupe_log_count": len(dedupe_log_rows),
            "selected_sort_by": selected_sort_by,
            "selected_sort_dir": selected_sort_dir,
            "selected_read_filter": selected_read_filter,
            "selected_star_only": selected_star_only,
            "selected_resume_read_filter": selected_resume_read_filter,
            "global_note": global_note,
            "posts": posts,
            "selected_entry": selected_entry,
            "message": message,
            "auto_refresh_enabled": getattr(app.state, "auto_refresh_minutes", 0) > 0,
            "auto_refresh_minutes": getattr(app.state, "auto_refresh_minutes", 0),
            "auto_refresh_option_minutes": AUTO_REFRESH_OPTION_MINUTES,
            "static_asset_version": STATIC_ASSET_VERSION,
            "debug_mode": DEBUG_MODE,
        },
    )


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


@app.post("/feeds")
def create_feed(feed_url: str = Form(...), folder_id: int = Form(...)):
    message = "Feed added."
    try:
        add_feed_to_folder(feed_url, folder_id)
        normalized_feed_url = feed_url.strip()
        if normalized_feed_url:
            feed_refresh_service.update_feeds([normalized_feed_url])
    except Exception as exc:
        message = f"Feed add failed: {exc}"
    return RedirectResponse(
        url=f"/?folder_id={folder_id}&message={quote_plus(message)}",
        status_code=303,
    )


@app.get("/feeds/properties")
def feed_properties(feed_url: str):
    return JSONResponse(get_feed_properties(feed_url))


_VALID_MANUAL_STRATEGIES = {"auto", "inline", "og_scrape", "none"}


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
    return JSONResponse({"ok": True, "strategy": strategy})


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
    feed_refresh_service.update_feeds(feed_urls)
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
    retry_after_seconds = check_and_mark_manual_refresh()
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
    feed_refresh_service.update_feeds([feed_url])
    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{entry_query}&message={quote_plus('Feed refresh complete.')}"
        ),
        status_code=303,
    )


@app.post("/folders/mark-read")
def mark_folder_as_read(
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
    with dedupe_log_cache_lock:
        dedupe_log_cache.clear()
    message = "All posts already read." if marked_count == 0 else f"Marked {marked_count} posts as read."
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}",
        status_code=303,
    )


@app.post("/feeds/mark-read")
def mark_feed_as_read(
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
    with dedupe_log_cache_lock:
        dedupe_log_cache.clear()
    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = ""
    if sort_by is not None or sort_dir is not None:
        sort_query = f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}" if read_filter is not None else ""
    star_only_query = build_star_only_query(star_only) if star_only is not None else ""
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter) if resume_read_filter is not None else ""
    message = "All posts already read." if marked_count == 0 else f"Marked {marked_count} posts as read."
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
            upsert_entry_read_state(feed_url, entry_id)
        else:
            reader.mark_entry_as_unread((feed_url, entry_id))
            delete_entry_read_state(feed_url, entry_id)
    with unread_counts_cache_lock:
        global _unread_counts_generation
        _unread_counts_generation += 1
        unread_counts_cache.clear()
    with dedupe_log_cache_lock:
        dedupe_log_cache.clear()

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
        for tag in existing_tags + appended_tags:
            normalized = normalize_tag_value(tag)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged_tags.append(normalized)
            if len(merged_tags) >= MAX_MANUAL_TAGS:
                break
        tags = set_manual_tags_for_entry(feed_url, entry_id, " ".join(merged_tags))
    else:
        tags = set_manual_tags_for_entry(feed_url, entry_id, tags_text)
    normalized_tag = normalize_tag_value(tag)

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = f"&sort_by={quote_plus(normalize_sort_by(sort_by))}&sort_dir={quote_plus(normalize_sort_dir(sort_dir))}"
    read_filter_query = f"&read_filter={quote_plus(normalize_read_filter(read_filter))}"
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter)

    entry_query = f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}" if select_entry else ""
    message = "Tags updated." if tags else "Tags cleared."

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
