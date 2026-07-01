from __future__ import annotations

import collections
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable, Iterable, Sequence, cast
from urllib.parse import parse_qs, parse_qsl, quote, quote_plus, unquote, urlencode, urlparse, urlunparse

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
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image as _PILImage
from readability import Document
from reader.exceptions import InvalidFeedURLError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from services import deviantart as deviantart_service
from services import youtube_oauth as youtube_oauth_service
from services import pinterest_oauth as pinterest_oauth_service
from services import quire as quire_service
from services import reddit as reddit_service
from services import inoreader as inoreader_service
from services import miniflux_import as miniflux_import_service
from services import freshrss as freshrss_service
from services import ttrss as ttrss_service
from services import passwords
from services import podcast_audio
from services import podcast_feed_discovery
from services import scraper_service
from services import html_sanitize
from services import takeout_service
from services import tenancy
from services import youtube_embeds
from services import url_guard
from services.webhooks import WEBHOOK_VALID_FORMATS, build_webhook_batch_payload, build_webhook_payload, send_webhook
from services.users import UserExistsError, UserStore
from services.email import send_article_email, send_digest_email
from services.feed_discovery import discover_feed_urls, discover_feed_urls_ex
from services.feed_refresh import FeedRefreshService
from services.lead_images import LeadImageService
from services.reader_api import ReaderApi
from services.starred_archive import StarredArchiveService
from services.youtube import YouTubeDurationService
from services.websub import WebSubService
from services.fever import FeverService
from services.greader import GReaderService
from services.miniflux import MinifluxService
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
# Default to BASE_DIR/data (matching the Docker ./data:/data mapping) so a local
# run without LECTIO_DATA_DIR set writes DBs and service dirs (scraped-feeds/,
# deviantart-feeds/) into ./data instead of polluting the repo root.
DATA_DIR = Path(os.getenv("LECTIO_DATA_DIR", str(BASE_DIR / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
META_DB_PATH = DATA_DIR / "lectio_meta.sqlite3"
READER_DB_PATH = DATA_DIR / "lectio_reader.sqlite"
THUMB_DB_PATH = DATA_DIR / "lectio_thumb_cache.sqlite"
# Global, content-addressed (source-URL hash -> original image bytes) cache for the
# /api/img proxy. Like the thumb cache, it holds no per-user data and is NOT routed
# through tenancy. See ensure_img_cache_schema / api_img_proxy.
IMG_CACHE_DB_PATH = DATA_DIR / "lectio_img_cache.sqlite"
# Global, content-addressed (video_id -> duration): a fact about the video, not
# per-user, so it's shared — a user without a YouTube key still gets durations
# another user's key already fetched. Like the thumb cache, NOT routed through tenancy.
YT_DURATION_DB_PATH = DATA_DIR / "lectio_yt_durations.sqlite"
STARRED_ARCHIVE_DB_PATH = DATA_DIR / "lectio_starred_archive.sqlite"
# Global account registry (NOT per-user, NOT routed through tenancy): one users
# table for the whole instance. Only used in multi-user (security) mode.
AUTH_DB_PATH = DATA_DIR / "lectio_auth.sqlite"
THUMB_CACHE_DIR = DATA_DIR / "thumb_cache"  # legacy on-disk cache; entries migrate lazily on access

# Bind the tenancy resolver. The DEFAULT_USER_ID resolves to these legacy paths,
# so single-user behavior is unchanged and no migration is needed yet. The thumb
# cache (THUMB_DB_PATH) is intentionally NOT routed through tenancy — it is a
# content-addressed global cache shared across all users. See services/tenancy.py.
tenancy.configure(
    data_dir=DATA_DIR,
    legacy_reader=READER_DB_PATH,
    legacy_meta=META_DB_PATH,
    legacy_starred=STARRED_ARCHIVE_DB_PATH,
)

ROOT_FOLDER_NAME = "All Feeds"
_LECTIO_FOLDER_NAME = "_Lectio"

# Virtual, derived folder pinned to the bottom of the sidebar that holds every
# subscribed feed with no folder_feeds row (e.g. feeds imported via OPML/reader
# migration that never got a folder). It has no DB row; its id is a negative
# sentinel so it never collides with real (positive) folder ids, and its
# membership is computed per-render as (all reader feeds − foldered feeds).
UNCATEGORIZED_FOLDER_ID = -1
UNCATEGORIZED_FOLDER_NAME = "Uncategorized"

scraper_service.init(DATA_DIR)
deviantart_service.init(DATA_DIR)


def _da_seed_lead_image(feed_url: str, entry_id: str, image_url: str) -> None:
    """Sink for the DeviantArt service: store the API image as the entry's lead
    image (DB + live cache) unless one is already cached (preserve customizations)."""
    try:
        if not lead_image_service.get_cached_entry_thumbnail(feed_url, entry_id, ""):
            lead_image_service.store_entry_lead_image(feed_url, entry_id, image_url)
    except Exception:
        LOGGER.exception("[deviantart] failed to seed lead image for %s", entry_id)


deviantart_service.set_lead_image_sink(_da_seed_lead_image)
DEFAULT_AUTO_REFRESH_MINUTES = 60
MIN_AUTO_REFRESH_MINUTES = 5
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
SETTING_IMG_CACHE_DAYS = "img_cache_days"
SETTING_IMG_CACHE_MAX_DIM = "img_cache_max_dim"
SETTING_YT_API_KEY = "yt_api_key"
SETTING_YT_CHANNEL_ID = "yt_channel_id"
SETTING_YT_FOLDER_NAME = "yt_folder_name"
# Per-user: use the standard youtube.com embed host (account features — Share /
# Watch Later — work) instead of the privacy-enhanced youtube-nocookie.com. Off
# (privacy) by default; the standard host sets YouTube cookies. "1"/"0".
SETTING_YT_EMBED_ACCOUNT_FEATURES = "yt_embed_account_features"
# Global per-user toggle: auto-mark YouTube Shorts read across ALL YouTube feeds at
# refresh, regardless of the per-feed hide_shorts pref. Off ("0") by default.
SETTING_YT_HIDE_SHORTS_GLOBAL = "yt_hide_shorts_global"
# Daily YouTube Data API quota cap (units). Google's default is 10,000/day; make it
# a setting in case a higher quota is granted.
SETTING_YT_QUOTA_CAP = "yt_quota_cap"
YT_QUOTA_DEFAULT_CAP = 10000
# "On star, also send to…" — per-user destinations fired when an article is starred.
SETTING_STAR_SEND_INSTAPAPER = "star_send_instapaper"   # "1"/"0"
SETTING_STAR_SEND_YT_PLAYLIST = "star_send_yt_playlist"  # playlist id ("" = off)
SETTING_STAR_SEND_YT_PLAYLIST_TITLE = "star_send_yt_playlist_title"
SETTING_STAR_SEND_EMAIL = "star_send_email"             # address ("" = off)
SETTING_RESEND_API_KEY = "resend_api_key"
SETTING_EMAIL_FROM = "email_from"
SETTING_INSTAPAPER_USERNAME = "instapaper_username"
SETTING_INSTAPAPER_PASSWORD = "instapaper_password"
SETTING_DEVIANTART_CLIENT_ID = "deviantart_client_id"
SETTING_DEVIANTART_CLIENT_SECRET = "deviantart_client_secret"
SETTING_DEVIANTART_ACCESS_TOKEN = "deviantart_access_token"
SETTING_DEVIANTART_REFRESH_TOKEN = "deviantart_refresh_token"
SETTING_DEVIANTART_TOKEN_EXPIRES_AT = "deviantart_token_expires_at"
SETTING_DEVIANTART_USERNAME = "deviantart_username"
SETTING_DEVIANTART_OAUTH_STATE = "deviantart_oauth_state"
SETTING_DEVIANTART_OAUTH_VERIFIER = "deviantart_oauth_verifier"
SETTING_DEVIANTART_FOLDER_NAME = "deviantart_folder_name"
SETTING_DEVIANTART_SYNC_STATUS = "deviantart_sync_status"
SETTING_YT_OAUTH_CLIENT_ID = "yt_oauth_client_id"
SETTING_YT_OAUTH_CLIENT_SECRET = "yt_oauth_client_secret"
SETTING_YT_OAUTH_ACCESS_TOKEN = "yt_oauth_access_token"
SETTING_YT_OAUTH_REFRESH_TOKEN = "yt_oauth_refresh_token"
SETTING_YT_OAUTH_TOKEN_EXPIRES_AT = "yt_oauth_token_expires_at"
SETTING_YT_OAUTH_STATE = "yt_oauth_state"
# Shared-instance YouTube OAuth creds stored in the admin's app_settings.
SETTING_SHARED_YT_OAUTH_CLIENT_ID = "shared_yt_oauth_client_id"
SETTING_SHARED_YT_OAUTH_CLIENT_SECRET = "shared_yt_oauth_client_secret"
# Pinterest outbound (save / pin an article to a board).
SETTING_PINTEREST_OAUTH_CLIENT_ID = "pinterest_oauth_client_id"
SETTING_PINTEREST_OAUTH_CLIENT_SECRET = "pinterest_oauth_client_secret"
SETTING_PINTEREST_OAUTH_ACCESS_TOKEN = "pinterest_oauth_access_token"
SETTING_PINTEREST_OAUTH_REFRESH_TOKEN = "pinterest_oauth_refresh_token"
SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT = "pinterest_oauth_token_expires_at"
SETTING_PINTEREST_OAUTH_STATE = "pinterest_oauth_state"
# Shared-instance Pinterest OAuth creds stored in the admin's app_settings.
SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID = "shared_pinterest_oauth_client_id"
SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET = "shared_pinterest_oauth_client_secret"
# Inoreader migration — OAuth tokens and import checkpoint.
SETTING_INOREADER_CLIENT_ID = "inoreader_client_id"
SETTING_INOREADER_CLIENT_SECRET = "inoreader_client_secret"
SETTING_INOREADER_ACCESS_TOKEN = "inoreader_access_token"
SETTING_INOREADER_REFRESH_TOKEN = "inoreader_refresh_token"
SETTING_INOREADER_TOKEN_EXPIRES_AT = "inoreader_token_expires_at"
SETTING_INOREADER_OAUTH_STATE = "inoreader_oauth_state"
SETTING_INOREADER_IMPORT_STATE = "inoreader_import_state"
SETTING_INOREADER_EXPORT_DIR = "inoreader_export_dir"  # server-side path to JSON export files
# Miniflux migration (FROM a Miniflux instance — distinct from the Miniflux API compat server).
SETTING_MINIFLUX_IMPORT_URL = "miniflux_import_url"
SETTING_MINIFLUX_IMPORT_TOKEN = "miniflux_import_token"
SETTING_MINIFLUX_IMPORT_STATE = "miniflux_import_state"
# FreshRSS migration.
SETTING_FRESHRSS_URL = "freshrss_url"
SETTING_FRESHRSS_USERNAME = "freshrss_username"
SETTING_FRESHRSS_PASSWORD = "freshrss_password"
SETTING_FRESHRSS_IMPORT_STATE = "freshrss_import_state"
# tt-rss migration.
SETTING_TTRSS_URL = "ttrss_url"
SETTING_TTRSS_USERNAME = "ttrss_username"
SETTING_TTRSS_PASSWORD = "ttrss_password"
SETTING_TTRSS_IMPORT_STATE = "ttrss_import_state"
# Reddit OAuth integration.
SETTING_REDDIT_CLIENT_ID = "reddit_client_id"
SETTING_REDDIT_CLIENT_SECRET = "reddit_client_secret"
SETTING_REDDIT_ACCESS_TOKEN = "reddit_oauth_access_token"
SETTING_REDDIT_REFRESH_TOKEN = "reddit_oauth_refresh_token"
SETTING_REDDIT_TOKEN_EXPIRES_AT = "reddit_oauth_token_expires_at"
SETTING_REDDIT_OAUTH_STATE = "reddit_oauth_state"
SETTING_REDDIT_USERNAME = "reddit_username"
SETTING_SHARED_REDDIT_CLIENT_ID = "shared_reddit_client_id"
SETTING_SHARED_REDDIT_CLIENT_SECRET = "shared_reddit_client_secret"
SETTING_STAR_SEND_REDDIT_SUBREDDIT = "star_send_reddit_subreddit"
# Instance tuning settings (admin-only, stored in admin's app_settings).
SETTING_FETCH_HISTORY_KEEP = "fetch_history_keep"
SETTING_FETCH_HISTORY_MAX_AGE_DAYS = "fetch_history_max_age_days"
SETTING_LOGIN_MAX_FAILURES = "login_max_failures"
SETTING_LOGIN_WINDOW_SECONDS = "login_window_seconds"
SETTING_DEFAULT_AUTO_REFRESH_MINUTES = "default_auto_refresh_minutes"
# Quire outbound (turn an article into a task in a chosen Quire project).
SETTING_QUIRE_CLIENT_ID = "quire_client_id"
SETTING_QUIRE_CLIENT_SECRET = "quire_client_secret"
SETTING_QUIRE_ACCESS_TOKEN = "quire_access_token"
SETTING_QUIRE_REFRESH_TOKEN = "quire_refresh_token"
SETTING_QUIRE_TOKEN_EXPIRES_AT = "quire_token_expires_at"
SETTING_QUIRE_OAUTH_STATE = "quire_oauth_state"
SETTING_QUIRE_USERNAME = "quire_username"
SETTING_QUIRE_PROJECT_OID = "quire_project_oid"   # default destination project ("" = none)
SETTING_QUIRE_PROJECT_NAME = "quire_project_name"  # cached display name of that project
SETTING_STAR_SEND_QUIRE = "star_send_quire"        # "1"/"0"
# Quire rate limits are per-organization, per-minute and per-hour (Free: 50/min, 200/hr).
# These are Lectio's own sliding-window caps used to drive the usage meter + back off.
SETTING_QUIRE_RATE_CAP_MIN = "quire_rate_cap_min"
SETTING_QUIRE_RATE_CAP_HOUR = "quire_rate_cap_hour"
SETTING_QUIRE_PLAN = "quire_plan"  # detected plan name of the destination project's org (display only)
QUIRE_RATE_DEFAULT_CAP_MIN = 50
QUIRE_RATE_DEFAULT_CAP_HOUR = 200
AUTO_REFRESH_OPTION_MINUTES = (0, 5, 15, 30, 60, 360, 720)
SCHEDULER_POLL_SECONDS = 30
DEFAULT_SORT_BY = "post"
DEFAULT_SORT_DIR = "asc"
CHUNK_SIZE = 10
# feed_fetch_history retention configured via the Administration panel.
READABILITY_USER_AGENT = "Lectio/0.1 (+https://localhost)"
# Honest identifier for outbound fetches — names the app and links to the repo,
# the good-citizen behavior some hosts (e.g. rachelbythebay.com) explicitly
# reward. This is the DEFAULT for podcast discovery/borrow fetches.
LECTIO_HONEST_USER_AGENT = "Lectio/0.1 (+https://github.com/joshg253/Lectio)"
# Some podcast-host feeds (Buzzsprout, Libsyn, …) sit behind Cloudflare, which
# 403s any non-browser UA. We escalate to this browser UA ONLY after the honest
# request is actually refused (see _polite_safe_get) — never preemptively, so we
# don't spoof hosts that are happy to serve Lectio.
PODCAST_FETCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# In-memory cache of domains known to have Cross-Origin-Resource-Policy restrictions.
# Values: True = same-site/same-origin (proxy needed), False = no restriction.
_CORP_DOMAIN_CACHE: dict[str, bool] = {}

# Hosts that serve a "this image was hotlinked" placeholder when the request
# carries a foreign Referer (or no same-origin signal). referrerpolicy="no-referrer"
# defeats this for *fresh* image loads, but a browser that cached the placeholder
# under the (unchanged) image URL keeps serving it. Routing these hosts' images
# through the /api/img proxy gives the browser a new same-origin URL it hasn't
# cached, and the server fetch carries no Referer — so the real image loads and
# stays correct. Add a registrable domain (matches it and any subdomain).
# wixmp.com serves DeviantArt images behind short-lived signed (?token=) URLs;
# proxying caches the bytes server-side so the article image survives token expiry.
# private-user-images.githubusercontent.com serves GitHub release/issue screenshots
# behind short-lived (~5 min) JWT-signed URLs — same problem, same fix.
# fabiensanglard.net is the *inverse* hotlink case: it 403s an image fetched with
# no Referer (e.g. its .webp files) but serves it with a same-origin Referer. The
# browser can't send fabiensanglard's own origin as Referer, so a direct <img>
# load fails (its .webp break in reader/web view while its .jpg loads); routing
# through /api/img lets the proxy's same-origin-Referer retry (api_img_proxy)
# fetch the real bytes. See ARCHITECTURE "Same-origin Referer".
_HOTLINK_IMG_HOSTS: frozenset[str] = frozenset(
    {"nanolx.org", "wixmp.com", "private-user-images.githubusercontent.com",
     "fabiensanglard.net"}
)


def _is_hotlink_img_host(netloc: str) -> bool:
    host = (netloc or "").split("@")[-1].split(":")[0].lower()
    return any(host == h or host.endswith("." + h) for h in _HOTLINK_IMG_HOSTS)
MANUAL_TAG_KEY_PREFIX = "lectio.manual_tag."
MAX_MANUAL_TAGS = 12
MAX_FEED_TAG_SUGGESTIONS = 8
FEED_TAG_SUGGESTION_CACHE_TTL_SECONDS = 900
TAG_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.#+][A-Za-z0-9_.#+-]{0,31}$")
def _static_asset_version() -> str:
    try:
        _static = Path(__file__).parent / "static"
        combined = b"".join(
            (_static / name).read_bytes()
            for name in ("style.css", "themes/dark.css")
        )
        return hashlib.md5(combined).hexdigest()[:10]
    except Exception:
        return "dev"

STATIC_ASSET_VERSION = os.getenv("LECTIO_ASSET_VERSION") or _static_asset_version()
REFRESH_DEBUG_ENABLED = os.getenv("LECTIO_REFRESH_DEBUG", "0") == "1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# /api/img proxy cache tuning (instance-level; admins can override in the
# Administration page, which takes precedence over these env fallbacks).
#   LECTIO_IMG_CACHE_DAYS    last-accessed TTL in days; 0 = keep forever (default 90)
#   LECTIO_IMG_CACHE_MAX_DIM longest-side px to downscale stored images to (default 3840)
_ENV_IMG_CACHE_DAYS = _env_int("LECTIO_IMG_CACHE_DAYS", 90)
_ENV_IMG_CACHE_MAX_DIM = _env_int("LECTIO_IMG_CACHE_MAX_DIM", 3840)
DEBUG_MODE = os.getenv("LECTIO_DEBUG", "0") == "1"
# Public base URL of this instance (e.g. https://lectio.example.com).
# Required for WebSub: hubs need a reachable callback URL.  Leave blank to disable WebSub.
LECTIO_PUBLIC_URL = os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/")

_FEVER_PASSWORD = os.getenv("LECTIO_FEVER_PASSWORD", "").strip()

# --- Email (Resend) config — env vars are fallbacks; DB settings take precedence at runtime ---
_ENV_RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
_ENV_RESEND_FROM = os.getenv("LECTIO_EMAIL_FROM", "").strip()
# Seed value for the bootstrap admin's default recipient (then per-user).
_ENV_RESEND_TO = os.getenv("LECTIO_EMAIL_TO", "").strip()


def get_resend_api_key() -> str:
    # Instance-shared: the Resend account/key is owned at the instance level
    # (one verified domain), so the env value is the shared default for everyone.
    return get_runtime_setting(SETTING_RESEND_API_KEY, _ENV_RESEND_API_KEY)


def get_resend_from() -> str:
    # Instance sending identity (admin-managed, like the Resend key): env value is
    # the shared default for everyone; an admin override lives in their settings.
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
# DeviantArt API creds — per-user DB settings take precedence; env is single-user fallback.
_ENV_DEVIANTART_CLIENT_ID = os.getenv("DEVIANTART_CLIENT_ID", "").strip()
_ENV_DEVIANTART_CLIENT_SECRET = os.getenv("DEVIANTART_CLIENT_SECRET", "").strip()
# YouTube OAuth client creds are app-level (one registered Google app), not
# per-user — only the resulting tokens are per-user. So these are read straight
# from env in both single and multi mode (sharing the app, never the tokens).
_ENV_YT_OAUTH_CLIENT_ID = os.getenv("YOUTUBE_OAUTH_CLIENT_ID", "").strip()
_ENV_YT_OAUTH_CLIENT_SECRET = os.getenv("YOUTUBE_OAUTH_CLIENT_SECRET", "").strip()
# Pinterest OAuth client creds are app-level (one registered Pinterest app), same
# as YouTube above — only the resulting tokens are per-user.
_ENV_PINTEREST_OAUTH_CLIENT_ID = os.getenv("PINTEREST_OAUTH_CLIENT_ID", "").strip()
_ENV_PINTEREST_OAUTH_CLIENT_SECRET = os.getenv("PINTEREST_OAUTH_CLIENT_SECRET", "").strip()
# Quire API creds — per-user DB settings take precedence; env is single-user fallback
# (same pattern as DeviantArt). Only the resulting tokens are ever per-user.
_ENV_QUIRE_CLIENT_ID = os.getenv("QUIRE_CLIENT_ID", "").strip()
_ENV_QUIRE_CLIENT_SECRET = os.getenv("QUIRE_CLIENT_SECRET", "").strip()
# Inoreader migration OAuth creds — env is the fallback; DB settings take precedence.
_ENV_INOREADER_CLIENT_ID = os.getenv("INOREADER_CLIENT_ID", "").strip()
_ENV_INOREADER_CLIENT_SECRET = os.getenv("INOREADER_CLIENT_SECRET", "").strip()
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


def youtube_embed_account_features_enabled() -> bool:
    """Per-user: True if YouTube embeds should use the standard youtube.com host
    (Share / Watch Later) instead of the privacy-enhanced -nocookie host.
    Privacy-enhanced is the default."""
    return get_runtime_setting(SETTING_YT_EMBED_ACCOUNT_FEATURES, "0") == "1"


def youtube_hide_shorts_global() -> bool:
    """Per-user: auto-mark Shorts read on ALL YouTube feeds at refresh (overrides the
    per-feed pref). Off by default."""
    return get_runtime_setting(SETTING_YT_HIDE_SHORTS_GLOBAL, "0") == "1"


def _pacific_today() -> str:
    """Today's date (YYYY-MM-DD) in US/Pacific — the timezone YouTube resets quota on."""
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        # Fallback: fixed -08:00 offset (good enough for a day-bucket if tzdata is absent).
        return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def youtube_quota_cap() -> int:
    try:
        return max(1, int(get_runtime_setting(SETTING_YT_QUOTA_CAP, str(YT_QUOTA_DEFAULT_CAP)) or YT_QUOTA_DEFAULT_CAP))
    except (TypeError, ValueError):
        return YT_QUOTA_DEFAULT_CAP


def record_yt_quota_spend(units: int) -> None:
    """Add ``units`` to the current user's Pacific-day YouTube quota tally. Called by
    the YT services after each billed API call (sink wired in at startup)."""
    if units <= 0:
        return
    try:
        with get_meta_connection() as conn:
            conn.execute(
                "INSERT INTO yt_quota_spend (day, units) VALUES (?, ?)"
                " ON CONFLICT(day) DO UPDATE SET units = units + excluded.units",
                (_pacific_today(), int(units)),
            )
    except Exception:
        LOGGER.debug("[yt-quota] failed to record %d units", units, exc_info=True)


def mark_yt_quota_exhausted() -> None:
    """Snap today's tally to the cap after an actual quotaExceeded response."""
    try:
        cap = youtube_quota_cap()
        with get_meta_connection() as conn:
            conn.execute(
                "INSERT INTO yt_quota_spend (day, units) VALUES (?, ?)"
                " ON CONFLICT(day) DO UPDATE SET units = MAX(units, excluded.units)",
                (_pacific_today(), cap),
            )
    except Exception:
        LOGGER.debug("[yt-quota] failed to mark exhausted", exc_info=True)


def get_yt_quota_spent_today() -> int:
    try:
        with get_meta_connection() as conn:
            row = conn.execute(
                "SELECT units FROM yt_quota_spend WHERE day = ?", (_pacific_today(),)
            ).fetchone()
        return int(row["units"]) if row else 0
    except Exception:
        return 0


def get_yt_quota_status() -> dict:
    """Quota meter payload for the UI: spent / cap / remaining + a low/exhausted flag."""
    cap = youtube_quota_cap()
    spent = get_yt_quota_spent_today()
    remaining = max(0, cap - spent)
    state = "exhausted" if remaining <= 0 else ("low" if remaining < 500 else "ok")
    return {"spent": spent, "cap": cap, "remaining": remaining, "state": state}


def youtube_embed_host() -> str:
    """The YouTube embed host for the current user's privacy/features preference."""
    return "www.youtube.com" if youtube_embed_account_features_enabled() else "www.youtube-nocookie.com"


def _get_shared_credential(key: str) -> str:
    """Read a shared-instance credential from the first admin user's settings.

    Shared-instance OAuth creds are stored in the admin's own app_settings
    under a namespaced key (e.g. shared_yt_oauth_client_id) and are used as a
    middle tier: per-user setting → shared-instance → env var.
    """
    if user_store is None:
        return ""
    for u in user_store.list_users():
        if u.get("is_admin") and not u.get("disabled"):
            with tenancy.user_context(u["user_id"]):
                val = get_runtime_setting(key)
                if val:
                    return val
    return ""


def get_fetch_history_keep() -> int:
    return int(get_runtime_setting(SETTING_FETCH_HISTORY_KEEP) or 50)


def get_fetch_history_max_age_days() -> int:
    return int(get_runtime_setting(SETTING_FETCH_HISTORY_MAX_AGE_DAYS) or 30)


def get_login_max_failures() -> int:
    return int(get_runtime_setting(SETTING_LOGIN_MAX_FAILURES) or 5)


def get_login_window_seconds() -> int:
    return int(get_runtime_setting(SETTING_LOGIN_WINDOW_SECONDS) or 300)


def get_instance_default_auto_refresh() -> int:
    raw = int(get_runtime_setting(SETTING_DEFAULT_AUTO_REFRESH_MINUTES) or DEFAULT_AUTO_REFRESH_MINUTES)
    return 0 if raw <= 0 else max(raw, MIN_AUTO_REFRESH_MINUTES)


def get_youtube_oauth_credentials() -> tuple[str, str]:
    """YouTube OAuth client (client_id, client_secret).

    Resolution: per-user setting → shared-instance (admin's settings) → env var.
    """
    cid = (get_runtime_setting(SETTING_YT_OAUTH_CLIENT_ID)
           or _get_shared_credential(SETTING_SHARED_YT_OAUTH_CLIENT_ID)
           or _ENV_YT_OAUTH_CLIENT_ID)
    secret = (get_runtime_setting(SETTING_YT_OAUTH_CLIENT_SECRET)
              or _get_shared_credential(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET)
              or _ENV_YT_OAUTH_CLIENT_SECRET)
    return cid, secret


def youtube_oauth_connected() -> bool:
    """True if the current user has connected their YouTube account."""
    with get_meta_connection() as conn:
        return bool(get_setting(conn, SETTING_YT_OAUTH_REFRESH_TOKEN))


def get_youtube_oauth_token() -> str:
    """Return a valid user access token for playlist writes, refreshing if needed.

    Empty string if the user hasn't connected their YouTube account or the
    refresh failed (revoked / testing-mode 7-day expiry) — caller must prompt a
    reconnect.
    """
    with get_meta_connection() as conn:
        access = get_setting(conn, SETTING_YT_OAUTH_ACCESS_TOKEN) or ""
        refresh = get_setting(conn, SETTING_YT_OAUTH_REFRESH_TOKEN) or ""
        try:
            expires_at = float(get_setting(conn, SETTING_YT_OAUTH_TOKEN_EXPIRES_AT) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
    if not refresh:
        return ""
    if access and time.time() < expires_at - 60:
        return access
    cid, secret = get_youtube_oauth_credentials()
    try:
        data = youtube_oauth_service.refresh_access_token(cid, secret, refresh)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[youtube-oauth] token refresh failed; reconnect required: %s", exc)
        return ""
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_YT_OAUTH_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_YT_OAUTH_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_YT_OAUTH_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
    return data["access_token"]


def get_pinterest_oauth_credentials() -> tuple[str, str]:
    """Pinterest OAuth client (client_id, client_secret).

    Resolution: per-user setting → shared-instance (admin's settings) → env var.
    """
    cid = (get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_ID)
           or _get_shared_credential(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID)
           or _ENV_PINTEREST_OAUTH_CLIENT_ID)
    secret = (get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_SECRET)
              or _get_shared_credential(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET)
              or _ENV_PINTEREST_OAUTH_CLIENT_SECRET)
    return cid, secret


def pinterest_oauth_connected() -> bool:
    """True if the current user has connected their Pinterest account."""
    with get_meta_connection() as conn:
        return bool(get_setting(conn, SETTING_PINTEREST_OAUTH_REFRESH_TOKEN))


def get_pinterest_oauth_token() -> str:
    """Return a valid user access token for pin writes, refreshing if needed.

    Empty string if the user hasn't connected Pinterest or the refresh failed
    (revoked / expired) — caller must prompt a reconnect."""
    with get_meta_connection() as conn:
        access = get_setting(conn, SETTING_PINTEREST_OAUTH_ACCESS_TOKEN) or ""
        refresh = get_setting(conn, SETTING_PINTEREST_OAUTH_REFRESH_TOKEN) or ""
        try:
            expires_at = float(get_setting(conn, SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
    if not refresh:
        return ""
    if access and time.time() < expires_at - 60:
        return access
    cid, secret = get_pinterest_oauth_credentials()
    try:
        data = pinterest_oauth_service.refresh_access_token(cid, secret, refresh)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[pinterest-oauth] token refresh failed; reconnect required: %s", exc)
        return ""
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_PINTEREST_OAUTH_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_PINTEREST_OAUTH_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
    return data["access_token"]


def get_reddit_credentials() -> tuple[str, str]:
    """Reddit OAuth client (client_id, client_secret).

    Resolution: per-user setting → shared-instance (admin's settings) → empty.
    No env-var fallback — credentials are DB-only for this integration.
    """
    cid = (get_runtime_setting(SETTING_REDDIT_CLIENT_ID)
           or _get_shared_credential(SETTING_SHARED_REDDIT_CLIENT_ID)
           or "")
    secret = (get_runtime_setting(SETTING_REDDIT_CLIENT_SECRET)
              or _get_shared_credential(SETTING_SHARED_REDDIT_CLIENT_SECRET)
              or "")
    return cid, secret


def reddit_connected() -> bool:
    """True if the current user has connected their Reddit account."""
    with get_meta_connection() as conn:
        return bool(get_setting(conn, SETTING_REDDIT_REFRESH_TOKEN))


def get_reddit_user_token() -> str:
    """Return a valid Reddit access token, refreshing if needed.

    Empty string if not connected or refresh failed — caller must prompt reconnect.
    """
    with get_meta_connection() as conn:
        access = get_setting(conn, SETTING_REDDIT_ACCESS_TOKEN) or ""
        refresh = get_setting(conn, SETTING_REDDIT_REFRESH_TOKEN) or ""
        try:
            expires_at = float(get_setting(conn, SETTING_REDDIT_TOKEN_EXPIRES_AT) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
    if not refresh:
        return ""
    if access and time.time() < expires_at - 60:
        return access
    cid, secret = get_reddit_credentials()
    if not cid or not secret:
        return ""
    try:
        data = reddit_service.refresh_access_token(cid, secret, refresh)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[reddit] token refresh failed; reconnect required: %s", exc)
        return ""
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_REDDIT_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_REDDIT_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_REDDIT_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
    return data["access_token"]


def _reddit_redirect_uri(request: Request) -> str:
    base = os.getenv("LECTIO_PUBLIC_URL", "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/integrations/reddit/oauth/callback"


def get_inoreader_credentials() -> tuple[str, str]:
    """Inoreader OAuth credentials (client_id, client_secret).

    DB settings take precedence over env vars so any user can override them
    without restarting the container.
    """
    cid = get_runtime_setting(SETTING_INOREADER_CLIENT_ID, _ENV_INOREADER_CLIENT_ID)
    secret = get_runtime_setting(SETTING_INOREADER_CLIENT_SECRET, _ENV_INOREADER_CLIENT_SECRET)
    return cid, secret


def inoreader_connected() -> bool:
    with get_meta_connection() as conn:
        return bool(get_setting(conn, SETTING_INOREADER_REFRESH_TOKEN))


def get_inoreader_token() -> str:
    """Return a valid Inoreader access token, refreshing it if expired."""
    with get_meta_connection() as conn:
        access = get_setting(conn, SETTING_INOREADER_ACCESS_TOKEN) or ""
        refresh = get_setting(conn, SETTING_INOREADER_REFRESH_TOKEN) or ""
        expires_at = float(get_setting(conn, SETTING_INOREADER_TOKEN_EXPIRES_AT) or 0)
    if not refresh:
        return ""
    if access and time.time() < expires_at - 60:
        return access
    cid, secret = get_inoreader_credentials()
    try:
        data = inoreader_service.refresh_access_token(cid, secret, refresh)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[inoreader] token refresh failed; reconnect required: %s", exc)
        return ""
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_INOREADER_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_INOREADER_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
    return data["access_token"]


def get_deviantart_credentials() -> tuple[str, str]:
    """Per-user DeviantArt API credentials (client_id, client_secret).

    Stored per-user in app-settings; env vars are instance-wide fallbacks.
    """
    cid = get_runtime_setting(SETTING_DEVIANTART_CLIENT_ID, _ENV_DEVIANTART_CLIENT_ID)
    secret = get_runtime_setting(SETTING_DEVIANTART_CLIENT_SECRET, _ENV_DEVIANTART_CLIENT_SECRET)
    return cid, secret


def get_deviantart_user_token() -> str:
    """Return a valid user access token for watch-list ops, refreshing if expired.

    Empty string if the user hasn't connected their DeviantArt account.
    """
    with get_meta_connection() as conn:
        access = get_setting(conn, SETTING_DEVIANTART_ACCESS_TOKEN) or ""
        refresh = get_setting(conn, SETTING_DEVIANTART_REFRESH_TOKEN) or ""
        try:
            expires_at = float(get_setting(conn, SETTING_DEVIANTART_TOKEN_EXPIRES_AT) or 0)
        except (TypeError, ValueError):
            # A corrupt/manually-edited value must not break every token read;
            # treat it as expired so we fall through to a forced refresh.
            expires_at = 0.0
    if not access:
        return ""
    if time.time() < expires_at - 60:
        return access
    if not refresh:
        return access  # no refresh token; try the (possibly stale) access token
    cid, secret = get_deviantart_credentials()
    try:
        data = deviantart_service.refresh_access_token(cid, secret, refresh)
    except Exception as exc:  # noqa: BLE001
        # Refresh failed (expired/rotated refresh token, or the client_secret no
        # longer matches the app) — the session is dead; signal "reconnect needed"
        # rather than handing back a stale token that 401s deep in a sync.
        LOGGER.warning("[deviantart] token refresh failed; reconnect required: %s", exc)
        return ""
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_DEVIANTART_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_DEVIANTART_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
    return data["access_token"]


def get_quire_credentials() -> tuple[str, str]:
    """Per-user Quire API credentials (client_id, client_secret).

    Stored per-user in app-settings; env vars are instance-wide fallbacks.
    """
    cid = get_runtime_setting(SETTING_QUIRE_CLIENT_ID, _ENV_QUIRE_CLIENT_ID)
    secret = get_runtime_setting(SETTING_QUIRE_CLIENT_SECRET, _ENV_QUIRE_CLIENT_SECRET)
    return cid, secret


def is_quire_connected() -> bool:
    """True once the user has completed the Quire OAuth flow (has a token)."""
    return bool(get_runtime_setting(SETTING_QUIRE_ACCESS_TOKEN))


def quire_project_oid() -> str:
    """The chosen default destination project's OID ("" if none picked yet)."""
    return (get_runtime_setting(SETTING_QUIRE_PROJECT_OID) or "").strip()


def quire_project_name() -> str:
    """Display name of the chosen default destination project ("" if none)."""
    return (get_runtime_setting(SETTING_QUIRE_PROJECT_NAME) or "").strip()


def is_quire_configured() -> bool:
    """True when Quire is connected AND a default destination project is set —
    the precondition for the entry button, On-Star, and Automation rules."""
    return bool(is_quire_connected() and quire_project_oid())


def get_quire_user_token() -> str:
    """Return a valid Quire access token, refreshing if expired. Empty string if the
    user hasn't connected their Quire account."""
    with get_meta_connection() as conn:
        access = get_setting(conn, SETTING_QUIRE_ACCESS_TOKEN) or ""
        refresh = get_setting(conn, SETTING_QUIRE_REFRESH_TOKEN) or ""
        try:
            expires_at = float(get_setting(conn, SETTING_QUIRE_TOKEN_EXPIRES_AT) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
    if not access:
        return ""
    if time.time() < expires_at - 60:
        return access
    if not refresh:
        return access
    cid, secret = get_quire_credentials()
    try:
        data = quire_service.refresh_access_token(cid, secret, refresh)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[quire] token refresh failed; reconnect required: %s", exc)
        return ""
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_QUIRE_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_QUIRE_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_QUIRE_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
    return data["access_token"]


def quire_rate_caps() -> tuple[int, int]:
    """(per-minute, per-hour) caps Lectio meters its Quire calls against."""
    def _cap(key: str, default: int) -> int:
        try:
            return max(1, int(get_runtime_setting(key, str(default)) or default))
        except (TypeError, ValueError):
            return default
    return (_cap(SETTING_QUIRE_RATE_CAP_MIN, QUIRE_RATE_DEFAULT_CAP_MIN),
            _cap(SETTING_QUIRE_RATE_CAP_HOUR, QUIRE_RATE_DEFAULT_CAP_HOUR))


def record_quire_call(calls: int = 1) -> None:
    """Log ``calls`` Quire API calls into the sliding-window meter (wired as the
    service usage sink at startup). Best-effort; prunes rows older than an hour."""
    if calls <= 0:
        return
    try:
        now = int(time.time())
        with get_meta_connection() as conn:
            conn.executemany(
                "INSERT INTO quire_call_log (ts) VALUES (?)",
                [(now,) for _ in range(int(calls))],
            )
            conn.execute("DELETE FROM quire_call_log WHERE ts < ?", (now - 3600,))
    except Exception:
        LOGGER.debug("[quire] failed to record %d calls", calls, exc_info=True)


def get_quire_usage_status() -> dict:
    """Usage-meter payload: calls in the last minute/hour vs caps, plus a state flag."""
    cap_min, cap_hour = quire_rate_caps()
    try:
        now = int(time.time())
        with get_meta_connection() as conn:
            minute_used = conn.execute(
                "SELECT COUNT(*) AS n FROM quire_call_log WHERE ts >= ?", (now - 60,)
            ).fetchone()["n"]
            hour_used = conn.execute(
                "SELECT COUNT(*) AS n FROM quire_call_log WHERE ts >= ?", (now - 3600,)
            ).fetchone()["n"]
    except Exception:
        minute_used = hour_used = 0
    minute_used, hour_used = int(minute_used), int(hour_used)
    blocked = minute_used >= cap_min or hour_used >= cap_hour
    low = (minute_used >= cap_min * 0.8) or (hour_used >= cap_hour * 0.8)
    state = "blocked" if blocked else ("low" if low else "ok")
    return {"minute_used": minute_used, "minute_cap": cap_min,
            "hour_used": hour_used, "hour_cap": cap_hour, "state": state}


def detect_quire_plan_and_caps() -> str:
    """Detect the destination project's organization plan and align the rate-meter
    caps to it (Free 50/200, Professional 300/1250, Premium 1000/5000). Stores the
    plan name for display. Best-effort; returns the detected plan ("" on failure).

    Quire rate-limits per organization, so the cap that matters is the plan of the
    org owning the chosen project."""
    project_oid = quire_project_oid()
    if not project_oid:
        return ""
    token = get_quire_user_token()
    if not token:
        return ""
    try:
        plan = quire_service.get_project_plan(token, project_oid)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[quire] plan detection failed: %s", exc)
        return ""
    caps = quire_service.PLAN_RATE_CAPS.get(plan.strip().lower())
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_QUIRE_PLAN, plan)
        if caps:
            # Enterprise (and any unknown plan) keeps the existing/default caps since
            # its quota scales with member count and isn't a fixed pair.
            set_setting(conn, SETTING_QUIRE_RATE_CAP_MIN, str(caps[0]))
            set_setting(conn, SETTING_QUIRE_RATE_CAP_HOUR, str(caps[1]))
    return plan


def _deviantart_folder_name() -> str:
    return get_runtime_setting(SETTING_DEVIANTART_FOLDER_NAME) or "DeviantArt"


def _apply_deviantart_image_strategy(conn: sqlite3.Connection, file_url: str) -> None:
    """Pin a DeviantArt feed to the 'inline' image strategy.

    DA feeds embed the authoritative full-size image inline (a stable, non-expiring
    wixmp URL), so deriving both the article lead image and the list thumbnail from
    that content — statelessly, every render — avoids source-page scraping and the
    lead-image cache getting clobbered to 'no image'. Locked (manual) so auto-detect
    can't change it.
    """
    try:
        lead_image_service.store_feed_strategy(file_url, "inline", manual=True)
    except Exception:
        LOGGER.exception("[deviantart] failed to pin inline strategy for %s", file_url)
    try:
        conn.execute("INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING", (file_url,))
        conn.execute("UPDATE feed_display_prefs SET thumb_strategy = 'inline' WHERE feed_url = ?", (file_url,))
    except Exception:
        LOGGER.exception("[deviantart] failed to set inline thumb_strategy for %s", file_url)


def sync_deviantart_watchlist() -> dict:
    """Add a gallery feed for every artist the user Watches (add-only).

    Skips artists already subscribed (anywhere) so it won't duplicate existing
    DeviantArt feeds. Returns a result summary dict.
    """
    token = get_deviantart_user_token()
    if not token:
        return {"added": 0, "total": 0, "error": "DeviantArt account not connected."}
    cid, secret = get_deviantart_credentials()
    try:
        username = get_runtime_setting(SETTING_DEVIANTART_USERNAME) or deviantart_service.whoami(token)
        watching = deviantart_service.list_watching(token, username)
    except deviantart_service.DeviantArtRateLimited as exc:
        return {"added": 0, "total": 0, "error": f"DeviantArt rate limit hit — try again in a few minutes. ({exc})"}
    except Exception as exc:  # noqa: BLE001
        return {"added": 0, "total": 0, "error": f"Could not read your watch list: {exc}"}

    folder_name = _deviantart_folder_name()
    with get_meta_connection() as conn:
        existing = {str(r["username"]).lower() for r in conn.execute("SELECT username FROM deviantart_feeds").fetchall()}
        folder_id = _get_or_create_folder_by_name(conn, folder_name)

    to_add = [a for a in watching if a.lower() not in existing]
    LOGGER.info("[deviantart] watchlist sync: %d watched, %d to add into %r", len(watching), len(to_add), folder_name)
    added = 0
    failed = 0
    rate_limited = False
    for i, artist in enumerate(to_add, 1):
        if i > 1:
            time.sleep(0.4)  # pace requests to stay under DeviantArt's rate limit
        try:
            with get_meta_connection() as conn:
                with get_reader() as reader:
                    _fid, file_url = deviantart_service.create_deviantart_feed(conn, reader, artist, cid, secret, access_token=token, limit=24)
                conn.execute(
                    "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                    (folder_id, file_url),
                )
            added += 1
        except deviantart_service.DeviantArtRateLimited:
            # Quota exhausted — stop now (the rest are re-runnable; sync is add-only
            # and dedupes, so clicking Sync again later resumes where this left off).
            rate_limited = True
            LOGGER.info("[deviantart] watchlist sync paused at %d/%d (added=%d) — rate limited", i, len(to_add), added)
            break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            LOGGER.warning("[deviantart] watchlist add failed for %s: %s", artist, exc)
        if i % 25 == 0:
            LOGGER.info("[deviantart] watchlist sync progress: %d/%d (added=%d failed=%d)", i, len(to_add), added, failed)
            with get_meta_connection() as conn:
                set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, f"Syncing {i}/{len(to_add)} (added {added})")
            invalidate_meta_structure_cache()
            invalidate_unread_counts_cache()
    invalidate_meta_structure_cache()
    invalidate_unread_counts_cache()
    remaining = len(to_add) - added - failed
    if rate_limited:
        final = (f"Rate limited — added {added} so far, ~{remaining} left. "
                 "DeviantArt caps requests; click Sync again later to continue.")
    else:
        already = len(watching) - len(to_add)
        parts = [f"Added {added} of {len(watching)} watched"]
        if already:
            parts.append(f"{already} already subscribed")
        if failed:
            parts.append(f"{failed} failed")
        final = ", ".join(parts)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, final)
    return {"added": added, "failed": failed, "total": len(watching), "folder": folder_name,
            "rate_limited": rate_limited}


def push_galleries_to_deviantart_watchlist() -> dict:
    """Watch (on DeviantArt) every artist you have a gallery feed for. Add-only."""
    token = get_deviantart_user_token()
    if not token:
        return {"watched": 0, "total": 0, "error": "DeviantArt account not connected."}
    with get_meta_connection() as conn:
        usernames = [str(r["username"]) for r in conn.execute("SELECT DISTINCT username FROM deviantart_feeds").fetchall()]
    watched = 0
    errors = 0
    for name in usernames:
        ok, _msg = deviantart_service.watch_user(token, name)
        if ok:
            watched += 1
        else:
            errors += 1
    return {"watched": watched, "total": len(usernames), "errors": errors}


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


def get_img_cache_days() -> int:
    """Last-accessed TTL (days) for the /api/img cache. 0 = keep forever.
    DB override (admin-managed) takes precedence over the env fallback."""
    val = get_runtime_setting(SETTING_IMG_CACHE_DAYS, "")
    if val:
        try:
            d = int(val)
            if d >= 0:
                return d
        except ValueError:
            pass
    return _ENV_IMG_CACHE_DAYS


def get_img_cache_max_dim() -> int:
    """Longest-side px to downscale cached images to. 0 = store originals as-is."""
    val = get_runtime_setting(SETTING_IMG_CACHE_MAX_DIM, "")
    if val:
        try:
            d = int(val)
            if d >= 0:
                return d
        except ValueError:
            pass
    return _ENV_IMG_CACHE_MAX_DIM

# --- Auth config ---
# When set, skip the login form and auto-authenticate as the admin on every request.
# Intended for local/private-network installs that don't need a password prompt.
AUTO_LOGIN = os.getenv("LECTIO_AUTO_LOGIN", "0") == "1"

# Password hashing scheme for stored credentials. scrypt and pbkdf2_sha256 are stdlib;
# argon2 needs the optional argon2-cffi package.
PASSWORD_HASH_SCHEME = os.getenv("LECTIO_PASSWORD_HASH_SCHEME", passwords.DEFAULT_SCHEME).strip().lower()
if PASSWORD_HASH_SCHEME not in passwords.available_schemes():
    LOGGER.warning(
        "LECTIO_PASSWORD_HASH_SCHEME=%r not available (have: %s); using %s.",
        PASSWORD_HASH_SCHEME, ", ".join(passwords.available_schemes()), passwords.DEFAULT_SCHEME,
    )
    PASSWORD_HASH_SCHEME = passwords.DEFAULT_SCHEME

# Bootstrap admin account, seeded on first startup when the users table is empty.
# CHANGE the default password before exposing the instance.
_DEFAULT_ADMIN_PASSWORD = "ChangeA$ap"
BOOTSTRAP_ADMIN_USERNAME = os.getenv("LECTIO_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("LECTIO_ADMIN_PASSWORD", _DEFAULT_ADMIN_PASSWORD)

AUTH_ENABLED = True  # kept as a module-level bool so tests can monkeypatch it
user_store = UserStore(AUTH_DB_PATH)
SESSION_SECRET_KEY = os.getenv("LECTIO_SECRET_KEY") or secrets.token_hex(32)
if AUTH_ENABLED and not os.getenv("LECTIO_SECRET_KEY"):
    LOGGER.warning(
        "LECTIO_SECRET_KEY is not set — using a random key. Sessions will not survive server restarts. Set a stable key in your .env."
    )
SESSION_MAX_AGE_SECONDS = 365 * 24 * 3600  # 1 year; baked into SessionMiddleware at startup
# Set LECTIO_HTTPS_ONLY=1 when running behind a TLS-terminating reverse proxy.
_HTTPS_ONLY = os.getenv("LECTIO_HTTPS_ONLY", "0") == "1"
# Proxies trusted for X-Forwarded-* headers. "*" (default) trusts any upstream —
# fine when the app port is only reachable via your reverse proxy. Set a
# comma-separated allowlist (e.g. the proxy's IP) for a stricter posture.
_TRUSTED_PROXIES = os.getenv("LECTIO_TRUSTED_PROXIES", "*").strip()
# Have the app emit baseline security headers itself. Default off because a
# reverse proxy (e.g. the bundled Traefik config) usually sets them; enable when
# fronting Lectio with a proxy that does not (Caddy without a header block, a
# bare/no-proxy setup, etc.). Keeps the headers from depending on the proxy.
_SECURITY_HEADERS_ENABLED = os.getenv("LECTIO_SECURITY_HEADERS", "0") == "1"
# Paths that are always public (no login required)
_AUTH_EXEMPT_PREFIXES = ("/login", "/static", "/healthz", "/api/img", "/api/favicon", "/dev/feeds/", "/fever", "/greader/", "/websub/")

manual_refresh_lock = threading.Lock()
last_manual_refresh_started_at = 0.0
updating_feeds_lock = threading.Lock()
updating_feeds: set[str] = set()
class _PerUserDict:
    """Dict-like cache partitioned by the current tenancy user, so per-user cached
    data (folder structure, unread counts, tags, settings) never bleeds across
    users. Implements the subset of dict operations the cache sites use; each op
    resolves the current user via tenancy.current_user_id()."""

    __slots__ = ("_by_user",)

    def __init__(self) -> None:
        self._by_user: dict[str, dict] = {}

    def _d(self) -> dict:
        return self._by_user.setdefault(tenancy.current_user_id(), {})

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def __delitem__(self, k):
        del self._d()[k]

    def __contains__(self, k):
        return k in self._d()

    def __bool__(self):
        return bool(self._d())

    def __len__(self):
        return len(self._d())

    def __iter__(self):
        return iter(self._d())

    def get(self, k, default=None):
        return self._d().get(k, default)

    def pop(self, k, *a):
        return self._d().pop(k, *a)

    def setdefault(self, k, default=None):
        return self._d().setdefault(k, default)

    def update(self, *a, **kw):
        self._d().update(*a, **kw)

    def clear(self):
        self._d().clear()

    def items(self):
        return self._d().items()

    def keys(self):
        return self._d().keys()

    def values(self):
        return self._d().values()


feed_tag_suggestion_cache_lock = threading.Lock()
feed_tag_suggestion_cache = _PerUserDict()
_feed_tag_fetch_in_progress: set[str] = set()
# Short in-memory TTL cache for tag counts to avoid repeatedly scanning
# reader entries on every request. Small TTL keeps counts fresh while
# preventing repeated expensive work during rapid navigation.
TAG_COUNTS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_TAG_COUNTS_CACHE_TTL", "300"))
tag_counts_cache_lock = threading.Lock()
tag_counts_cache = _PerUserDict()

# Short in-memory TTL cache for unread counts so the UI doesn't scan the
# entire reader DB on every load. TTL is small to stay responsive to new
# incoming posts.
UNREAD_COUNTS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_UNREAD_COUNTS_CACHE_TTL", "300"))
unread_counts_cache_lock = threading.Lock()
unread_counts_cache = _PerUserDict()
# Stale-while-revalidate: when the cache is stale we serve the prior value and
# spawn ONE background refresh. Concurrent renders never wait on the scan.
unread_counts_compute_lock = threading.Lock()
unread_counts_refresh_inflight = False
# Incremented on every invalidation so in-flight background refreshes that
# started before the invalidation don't write stale counts back to the cache.
_unread_counts_generation: int = 0
# Lead-image / YouTube-duration enhancement is network-heavy, so manual refresh
# runs it off the request path. Track in-flight feeds to skip overlapping work.
_enhancement_inflight_lock = threading.Lock()
_enhancement_inflight_feeds: set[str] = set()
# Feed-title map: hits the reader DB to enumerate every feed. Cache it — feed
# titles barely change between page renders.
FEED_TITLE_MAP_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_FEED_TITLE_MAP_CACHE_TTL", "300"))
feed_title_map_cache_lock = threading.Lock()
feed_title_map_cache = _PerUserDict()

# Cache the meta-DB structure snapshot. Folders / folder_feeds change only on
# explicit user actions (subscribe, unsubscribe, add/delete folder, move feed),
# so we cache the read-side queries indefinitely and invalidate on mutation.
# This collapses ~5 SQL roundtrips per home render to one dict lookup.
_meta_structure_lock = threading.Lock()
_meta_structure_cache = _PerUserDict()


def invalidate_meta_structure_cache() -> None:
    with _meta_structure_lock:
        _meta_structure_cache.clear()


def invalidate_unread_counts_cache() -> None:
    """Bump the generation + clear the cache so folder/feed unread badges recompute."""
    global _unread_counts_generation
    with unread_counts_cache_lock:
        _unread_counts_generation += 1
        unread_counts_cache.clear()


# Cache for problematic-feeds list. Only changes when a refresh succeeds/fails,
# so a TTL is fine — we don't need exact freshness on the home page.
PROBLEMATIC_FEEDS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_PROBLEMATIC_FEEDS_CACHE_TTL", "60"))
_problematic_feeds_cache_lock = threading.Lock()
_problematic_feeds_cache = _PerUserDict()


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
    ensure_img_cache_schema()
    ensure_yt_duration_schema()
    ensure_starred_archive_schema()
    ensure_websub_schema()
    ensure_reader_indexes()
    bootstrap_admin()

    # Bring every existing user's meta/starred schema up to current.  The bare
    # ensure_*_schema() calls above only touch the default tenant; per-user DBs
    # are otherwise schema-init'd only at provision time, so any table added
    # after a user was provisioned (e.g. feed_fetch_history) is missing from
    # their DB and surfaces as a "no such table" 500 (Feed Properties, etc.).
    # ensure_*_schema are idempotent, so this is a cheap no-op once migrated.
    def _ensure_user_schema() -> None:
        ensure_meta_schema()
        ensure_starred_archive_schema()
        ensure_reader_indexes()
    _for_each_background_user("per-user schema migration", _ensure_user_schema)

    if LECTIO_PUBLIC_URL:
        _migrate_websub_to_shared()

    with get_meta_connection() as conn:
        purge_lower_level_folders(conn)
        app.state.auto_refresh_minutes = get_auto_refresh_minutes(conn)
        # Pre-load the default user's settings cache (startup runs unbound).
        with _app_settings_cache_lock:
            _app_settings_cache[tenancy.current_user_id()] = _load_app_settings_cache(conn)
    app.state.last_scheduled_refresh_started_at = time.monotonic()

    # Checkpoint both WAL files at startup so the first user request does not
    # have to rebuild the WAL index from a large file left over from the
    # previous run.  Use direct connections so we're not racing the reader.
    for _ckpt_path in (READER_DB_PATH, META_DB_PATH):
        try:
            _ckpt_conn = sqlite3.connect(str(_ckpt_path), timeout=5)
            _ckpt_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _ckpt_conn.close()
        except Exception:
            pass

    # Ensure reader db is created at startup.
    with get_reader():
        pass

    # Warm YouTube duration in-memory cache from DB so first renders are instant.
    youtube_duration_service.warm_cache_from_db()

    # Warm lead image cache from DB so thumbnails are available on first render.
    # Must run per-user: lead images live in each tenant's own meta DB, and the
    # render path (get_cached_entry_thumbnail) only consults the in-memory cache
    # with no per-user DB fallback. Warming bare resolves to the default tenant,
    # leaving every other user's thumbnails blank until the rate-limited
    # background backfill catches up after each restart.
    _for_each_background_user("lead-image cache warm", lead_image_service.warm_cache_from_db)

    # Warm the per-user unread-counts cache so the first home load after a restart
    # doesn't pay the synchronous reader-DB scan under startup contention (cold
    # cache + concurrent first requests + a refresh kicking off was producing
    # multi-second — occasionally ~20s — home loads right after a redeploy).
    def _warm_unread_counts() -> None:
        try:
            counts = _compute_unread_counts_by_feed()
            with unread_counts_cache_lock:
                unread_counts_cache["unread_counts"] = (time.time(), counts)
        except Exception:
            LOGGER.debug("unread-counts cache warm failed", exc_info=True)
    _for_each_background_user("unread-counts cache warm", _warm_unread_counts)

    # Ensure existing scraped file:// feeds have their entries imported into the
    # reader DB. Feeds may be in backoff from stale "no retriever" errors that
    # pre-date the feed_root='' fix — clear that state and force a reader sync.
    def _sync_scraped_feeds() -> None:
        try:
            with get_meta_connection() as _sc:
                _sf_rows = _sc.execute("SELECT id, mode FROM scraped_feeds").fetchall()
            for _sf_row in _sf_rows:
                _sf_id = str(_sf_row["id"])
                _sf_url = scraper_service.feed_file_url(_sf_id)
                try:
                    with get_meta_connection() as _sc:
                        _sc.execute("DELETE FROM feed_failure_state WHERE feed_url = ?", (_sf_url,))
                        # link_list feeds seed initial links as hidden so only new links
                        # appear as entries.  If ALL entries are still hidden (nothing has
                        # appeared yet), unhide them so the feed isn't permanently empty.
                        if str(_sf_row["mode"]) == "link_list":
                            _visible = _sc.execute(
                                "SELECT count(*) FROM scraped_entries WHERE scraped_feed_id=? AND NOT hidden",
                                (_sf_id,),
                            ).fetchone()[0]
                            if _visible == 0:
                                _sc.execute(
                                    "UPDATE scraped_entries SET hidden=0 WHERE scraped_feed_id=?",
                                    (_sf_id,),
                                )
                                scraper_service._write_feed_file(_sc, _sf_id)
                    with get_reader() as _r:
                        _r.update_feed(_sf_url)
                except Exception:
                    pass
        except Exception:
            LOGGER.exception("[scraper] startup scraped-feed sync failed")
    threading.Thread(
        target=lambda: _for_each_background_user("scraped-feed sync", _sync_scraped_feeds),
        daemon=True,
        name="sync-scraped-feeds",
    ).start()

    # Auto-taggers and the dedup cleanup write per-user tag/strategy state, so
    # run them once per background user (each under its own tenancy context).
    def _startup_auto_tag_and_dedup() -> None:
        # Artwork tagger runs first so webcomic tagger won't clobber ArtStation
        # feeds that live in folders whose name also contains "comic".
        _auto_tag_artwork_feeds()
        # Auto-tag feeds in "comic*" folders with strategy='webcomic'.
        _auto_tag_webcomic_feeds()
        # GitHub release feeds get og_scrape + no list thumbnail.
        _auto_tag_github_release_feeds()

        # Retroactive dedup: suppress duplicate unread entries (same slug or
        # title+date) that accumulated before _suppress_guid_churn could catch them.
        try:
            with get_reader() as reader:
                with get_meta_connection() as conn:
                    cleaned = _cleanup_intra_feed_slug_dupes(reader, conn)
            if cleaned:
                LOGGER.info("[guid-churn-cleanup] suppressed %d pre-existing duplicate entries", cleaned)
        except Exception:
            LOGGER.exception("[guid-churn-cleanup] startup cleanup failed")

    _for_each_background_user("auto-tag/dedup", _startup_auto_tag_and_dedup)

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
        threading.Thread(
            target=lambda: _for_each_background_user("youtube-duration backfill", _backfill),
            daemon=True,
        ).start()

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
        threading.Thread(
            target=lambda: _for_each_background_user("lead-image backfill", _backfill_lead_images),
            daemon=True,
        ).start()

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
        target=lambda: _for_each_background_user("starred-archive backfill", _archive_backfill_task),
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

    threading.Thread(
        target=lambda: _for_each_background_user("read-history backfill", _backfill_read_history),
        daemon=True,
        name="read-history-backfill",
    ).start()

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


def _session_logged_in(request: Request) -> bool:
    """Whether the session is fully authenticated (requires a valid user_id)."""
    if not request.session.get("authenticated"):
        return False
    uid = request.session.get("user_id")
    return bool(uid and tenancy.is_valid_user_id(uid))


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)
        if _session_logged_in(request):
            return await call_next(request)
        if AUTO_LOGIN and user_store is not None:
            # Auto-authenticate as the first non-disabled admin user without
            # showing the login form. Intended for local/private-network installs.
            for _u in user_store.list_users():
                if _u.get("is_admin") and not _u.get("disabled"):
                    request.session["authenticated"] = True
                    request.session["user_id"] = _u["user_id"]
                    break
            if _session_logged_in(request):
                return await call_next(request)
        next_url = str(request.url)
        return RedirectResponse(url=f"/login?next={quote_plus(next_url)}", status_code=303)


class _SecurityHeadersMiddleware:
    """Emit baseline security response headers from the app (opt-in via
    LECTIO_SECURITY_HEADERS=1), so they hold regardless of the reverse proxy.

    Mirrors the headers the bundled Traefik config sets. Existing headers are not
    overwritten, so a proxy that already sets them wins and there are no
    duplicates. HSTS is only added when LECTIO_HTTPS_ONLY=1."""

    def __init__(self, app, *, hsts: bool):
        self.app = app
        self._headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer-when-downgrade"),
        ]
        if hsts:
            self._headers.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                for key, val in self._headers:
                    if key not in present:
                        headers.append((key, val))
            await send(message)

        await self.app(scope, receive, send_wrapper)


_LAST_SEEN_THROTTLE_SECONDS = 300
_last_seen_touch: dict[str, float] = {}
_last_seen_touch_lock = threading.Lock()


def _touch_user_last_seen(uid: str) -> None:
    """Record per-user activity time, throttled (this runs on every request)."""
    if user_store is None:
        return
    now = time.time()
    with _last_seen_touch_lock:
        if now - _last_seen_touch.get(uid, 0.0) < _LAST_SEEN_THROTTLE_SECONDS:
            return
        _last_seen_touch[uid] = now
    try:
        user_store.touch_last_seen(uid, now)
    except Exception:
        LOGGER.debug("touch_last_seen failed", exc_info=True)


class _TenancyMiddleware:
    """Bind the request's user into the tenancy context (multi mode only).

    Pure-ASGI and registered innermost (downstream of the auth gate), so the
    binding wraps the route handler. Sync handlers run via anyio's threadpool,
    which copies the current contextvars into the worker thread, so a value set
    here is visible to get_reader() / get_meta_connection() deep in the call
    stack.

    Requests without a valid authenticated session user (static assets, the
    Fever/GReader APIs, unauthenticated hits) resolve to the default user; those
    API protocols carry their own per-user identity in a later phase.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _greader_user_from_scope(scope) -> str | None:
        """Resolve a /greader/ request's bearer token to a user, using only the
        Authorization header / query string (no body read needed)."""
        if user_store is None:
            return None
        token = ""
        for key, val in scope.get("headers", []):
            if key == b"authorization":
                auth = val.decode("latin-1")
                if auth.startswith("GoogleLogin auth="):
                    token = auth[17:].strip()
                elif auth.startswith("Bearer "):
                    token = auth[7:].strip()
                break
        if not token:
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            token = (qs.get("token") or [""])[0]
        return user_store.resolve_greader_token(token)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        uid: str | None = None
        if path.startswith("/greader/"):
            # API requests carry their identity in a bearer token, not a session.
            uid = self._greader_user_from_scope(scope)
        else:
            session = scope.get("session") or {}
            sid = session.get("user_id")
            if sid and session.get("authenticated") and tenancy.is_valid_user_id(sid):
                uid = sid
        if uid and tenancy.is_valid_user_id(uid):
            _touch_user_last_seen(uid)
            token = tenancy.set_current_user(uid)
            try:
                return await self.app(scope, receive, send)
            finally:
                tenancy.reset_current_user(token)
        return await self.app(scope, receive, send)


# Paths exempt from CSRF validation. /login is the auth gate itself (rate-
# limited separately). /static and /healthz are GET-only anyway, but listing
# explicitly documents intent.
_CSRF_EXEMPT_PREFIXES = ("/login", "/static", "/healthz", "/api/img", "/api/favicon", "/fever", "/greader/", "/websub/")
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

        _t0 = time.monotonic()
        status_holder = [0]

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status_holder[0] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            status = status_holder[0]
            elapsed_ms = int((time.monotonic() - _t0) * 1000)
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
                duration = f" {elapsed_ms}ms" if elapsed_ms >= 200 else ""
                LOGGER.info('%s - "%s" %d %s%s', client_addr, request_line, status, reason, duration)


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

    PASSTHROUGH_PREFIXES = ("/healthz", "/static", "/login", "/api/img", "/api/favicon")

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

        if path == "/" or path.startswith("/?"):
            await self.app(scope, receive, send)
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
# _TenancyMiddleware is added before Auth so it is innermost of all — it binds
# the per-user context right around the endpoint, after the auth gate has run.
app.add_middleware(_TenancyMiddleware)
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

    _trusted_hosts = (
        "*" if _TRUSTED_PROXIES == "*"
        else [h.strip() for h in _TRUSTED_PROXIES.split(",") if h.strip()]
    )
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_trusted_hosts)
# Drop prefetch traffic before any other middleware does work.
app.add_middleware(_RejectPrefetchMiddleware)
# App-emitted security headers (opt-in), outermost so they apply to every
# response regardless of which inner middleware produced it.
if _SECURITY_HEADERS_ENABLED:
    app.add_middleware(_SecurityHeadersMiddleware, hsts=_HTTPS_ONLY)
# Compress responses before they reach Traefik so Traefik's own compress
# middleware sees Content-Encoding: gzip and passes through without buffering.
# Buffering the full 500KB page before sending a single byte was the main
# culprit for slow first-byte times on variable connections.
app.add_middleware(GZipMiddleware, minimum_size=1000)
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
    has_error: bool = False
    disabled: bool = False


_meta_conn_local = threading.local()


def get_meta_connection() -> sqlite3.Connection:
    """Per-(thread, user) persistent SQLite connection to the meta DB.

    The target DB is resolved through the tenancy seam from the current user
    (defaults to the single legacy user). Each worker thread keeps one
    connection per user_id it has served.

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
    uid = tenancy.current_user_id()
    pool = getattr(_meta_conn_local, "pool", None)
    if pool is None:
        pool = {}
        _meta_conn_local.pool = pool
    conn = pool.get(uid)
    if conn is not None:
        return conn
    conn = sqlite3.connect(str(tenancy.meta_db_path(uid)), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so overlapping writers (e.g. background refresh writing
    # folder_feeds while a request persists a setting) wait briefly instead of
    # immediately failing with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    # Keep WAL small: checkpoint at 200 pages (~800KB) rather than the default
    # 1000 pages so the file never balloons to tens of MB between restarts.
    conn.execute("PRAGMA wal_autocheckpoint=200")
    pool[uid] = conn
    return conn


_websub_conn_local = threading.local()
WEBSUB_DB_PATH = DATA_DIR / "lectio_websub.sqlite"


def get_websub_connection() -> sqlite3.Connection:
    """Per-thread connection to the shared (non-per-user) WebSub subscription DB."""
    pool = getattr(_websub_conn_local, "pool", None)
    if pool is not None:
        return pool
    conn = sqlite3.connect(str(WEBSUB_DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA wal_autocheckpoint=200")
    _websub_conn_local.pool = conn
    return conn


def get_thumb_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(THUMB_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_websub_schema() -> None:
    """Create the shared (non-per-user) WebSub tables if they don't exist."""
    with get_websub_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS websub_subscriptions (
                feed_url      TEXT PRIMARY KEY,
                hub_url       TEXT,
                secret        TEXT,
                subscribed_at REAL,
                verified      INTEGER DEFAULT 0,
                expires_at    REAL,
                lease_seconds INTEGER,
                hub_tried_at  REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS websub_subscribers (
                feed_url TEXT NOT NULL,
                user_id  TEXT NOT NULL,
                PRIMARY KEY (feed_url, user_id)
            )
            """
        )


def _migrate_websub_to_shared() -> None:
    """One-time migration: copy per-user websub_subscriptions rows to the shared DB.

    Idempotent (INSERT OR IGNORE) so safe to re-run on every startup.
    After this runs, the service only writes to the shared DB."""
    for uid in _background_user_ids():
        with tenancy.user_context(uid):
            try:
                conn = get_meta_connection()
                rows = conn.execute(
                    "SELECT feed_url, hub_url, secret, subscribed_at, verified, "
                    "expires_at, lease_seconds, hub_tried_at "
                    "FROM websub_subscriptions"
                ).fetchall()
            except Exception:
                continue  # table may not exist for new users
            if not rows:
                continue
            with get_websub_connection() as wconn:
                for r in rows:
                    wconn.execute(
                        "INSERT OR IGNORE INTO websub_subscriptions "
                        "(feed_url, hub_url, secret, subscribed_at, verified, "
                        "expires_at, lease_seconds, hub_tried_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (r["feed_url"], r["hub_url"], r["secret"], r["subscribed_at"],
                         r["verified"], r["expires_at"], r["lease_seconds"], r["hub_tried_at"]),
                    )
                    wconn.execute(
                        "INSERT OR IGNORE INTO websub_subscribers (feed_url, user_id) VALUES (?, ?)",
                        (r["feed_url"], uid),
                    )


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


def get_img_cache_connection() -> sqlite3.Connection:
    """Connection to the global (shared) /api/img proxy cache."""
    conn = sqlite3.connect(str(IMG_CACHE_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_img_cache_schema() -> None:
    with get_img_cache_connection() as conn:
        # WAL for concurrent readers; cached bytes are regeneratable so
        # synchronous=NORMAL is the right durability/throughput tradeoff.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS img_cache (
                cache_key TEXT PRIMARY KEY,
                content_type TEXT NOT NULL,
                body BLOB NOT NULL,
                size INTEGER NOT NULL,
                created_at REAL NOT NULL,
                last_accessed REAL NOT NULL
            )
            """
        )
        # Eviction scans by last_accessed; index keeps the daily sweep cheap.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_img_cache_last_accessed ON img_cache(last_accessed)"
        )


def get_yt_duration_connection() -> sqlite3.Connection:
    """Connection to the global (shared) YouTube-duration cache."""
    conn = sqlite3.connect(str(YT_DURATION_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_yt_duration_schema() -> None:
    with get_yt_duration_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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


def get_starred_archive_connection() -> sqlite3.Connection:
    # Per-user DB (resolved via tenancy); returns a fresh connection per call.
    conn = sqlite3.connect(str(tenancy.starred_archive_db_path()))
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


def ensure_reader_indexes() -> None:
    """Add Lectio-owned performance indexes to the current tenant's reader DB.

    reader manages the `entries` table; we add a partial index it doesn't ship.
    The per-feed unread-count query (`WHERE read=0 GROUP BY feed`) otherwise scans
    the whole entries table (75k+ rows at scale) on each cache refresh, which under
    refresh write-contention stretched to several seconds and made the UI sluggish.
    A partial covering index over only the unread rows makes it a ~1k-row scan.
    Idempotent and safe (an extra index reader never drops)."""
    try:
        conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5)
        try:
            has_entries = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
            ).fetchone()
            if has_entries:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS entries_unread_by_feed "
                    "ON entries(feed) WHERE read=0"
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        LOGGER.debug("ensure_reader_indexes failed", exc_info=True)


def provision_user_storage(user_id: str) -> None:
    """Create and schema-init a user's isolated databases (keyed by stable
    user_id).

    Routes through the tenancy seam: under ``user_context`` the schema-init
    helpers and reader open resolve to this user's own DB files. Idempotent —
    safe to call on an already-provisioned user.
    """
    tenancy.ensure_user_data_dir(user_id)
    with tenancy.user_context(user_id):
        ensure_meta_schema()
        ensure_starred_archive_schema()
        # reader builds its own schema on first client(); opening provisions it.
        with get_reader():
            pass
        ensure_reader_indexes()


def delete_user_storage(user_id: str) -> None:
    """Recursively remove a user's isolated data directory
    (``DATA_DIR/users/<user_id>/`` and all its DBs). No-op for the default
    (legacy top-level) user, whose files are never owned by a deletable account.

    Content-addressed caches (thumbnails, image proxy, lead-image/strategy
    results) are global and hold no per-user data, so they are intentionally
    left untouched.
    """
    if user_id == tenancy.DEFAULT_USER_ID:
        return
    data_dir = tenancy.user_data_dir(user_id)
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)


def _seed_admin_integrations_from_env(admin_id: str) -> None:
    """Copy env-provided per-user integration defaults into the admin's settings,
    so a fresh multi-user instance has the admin configured (env is "used for
    bootstrap then ignored" for these per-user values). Only seeds keys not
    already set; the instance-shared Resend API key is NOT seeded here (it stays
    env-read for everyone via get_resend_api_key)."""
    seeds = [
        (SETTING_EMAIL_FROM, _ENV_RESEND_FROM),
        (EMAIL_TO_SETTING_KEY, _ENV_RESEND_TO),
        # YouTube is per-user; the env vars seed the first (admin) user only.
        (SETTING_YT_API_KEY, _ENV_YT_API_KEY),
        (SETTING_YT_CHANNEL_ID, _ENV_YT_CHANNEL_ID),
        (SETTING_YT_FOLDER_NAME, _ENV_YT_FOLDER_NAME),
    ]
    with tenancy.user_context(admin_id):
        with get_meta_connection() as conn:
            for key, val in seeds:
                if val and not get_setting(conn, key):
                    set_setting(conn, key, val)


def bootstrap_admin() -> None:
    """Seed the bootstrap admin on first startup when no users exist.

    No-op once any user exists. Provisions the new admin's isolated storage
    and warns loudly if the default password is still in use.
    """
    if user_store is None:
        return
    if user_store.count() > 0:
        return
    username = BOOTSTRAP_ADMIN_USERNAME
    if not tenancy.is_valid_user_id(username):
        LOGGER.error(
            "Cannot bootstrap admin: LECTIO_ADMIN_USERNAME=%r is not a valid "
            "username (must match {A-Za-z0-9_-}, 1-64 chars).",
            username,
        )
        return
    try:
        admin_id = user_store.create(username, BOOTSTRAP_ADMIN_PASSWORD, is_admin=True, scheme=PASSWORD_HASH_SCHEME)
        provision_user_storage(admin_id)
        _seed_admin_integrations_from_env(admin_id)
    except Exception:
        LOGGER.exception("failed to bootstrap admin user %r", username)
        return
    if BOOTSTRAP_ADMIN_PASSWORD == _DEFAULT_ADMIN_PASSWORD:
        LOGGER.warning(
            "Bootstrapped admin %r with the DEFAULT password. Set LECTIO_ADMIN_PASSWORD "
            "(and change the account password) before exposing this instance.",
            username,
        )
    else:
        LOGGER.info("Bootstrapped admin user %r.", username)


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
        try:
            conn.execute("ALTER TABLE folders ADD COLUMN cadence_minutes INTEGER DEFAULT NULL")
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
            conn.execute("ALTER TABLE entry_lead_images ADD COLUMN thumb_crop TEXT")
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
            CREATE TABLE IF NOT EXISTS browser_ua_feeds (
                feed_url TEXT PRIMARY KEY,
                flagged_at TEXT NOT NULL DEFAULT (datetime('now')),
                reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_display_prefs (
                feed_url TEXT PRIMARY KEY,
                show_lead_image_in_article INTEGER NOT NULL DEFAULT 1,
                show_lead_image_as_thumb INTEGER NOT NULL DEFAULT 1,
                show_image_caption INTEGER NOT NULL DEFAULT -1,
                caption_source TEXT,
                inject_source_images INTEGER NOT NULL DEFAULT 0
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
                image_alt TEXT,
                image_title TEXT,
                PRIMARY KEY (feed_url, strategy)
            )
            """
        )
        # Per-refresh fetch history — one row per non-skipped refresh attempt, so
        # Feed Properties can show why a feed is stale or flagged problematic.
        # Bounded by a per-feed cap + age prune in daily maintenance.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_fetch_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                new_entries INTEGER,
                duration_ms INTEGER,
                error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS feed_fetch_history_by_feed"
            " ON feed_fetch_history (feed_url, fetched_at DESC)"
        )
        # Recovered media:content podcast audio (reader drops media:content, so we
        # re-parse the raw feed on demand). entry_media_audio holds per-entry audio
        # URLs found; feed_media_scan tracks when a feed was last scanned (and
        # whether any audio was found) so we don't re-fetch non-podcast feeds.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entry_media_audio (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                audio_url TEXT NOT NULL,
                PRIMARY KEY (feed_url, entry_id)
            )
            """
        )
        # Recovered YouTube embeds (feedparser strips the <iframe>, so a WP
        # YouTube block reaches us as an empty figure). video_ids is a
        # space-separated list in document order; '' means a YouTube embed was
        # present but no id was recoverable (a cached negative). Populated by the
        # same raw-feed re-parse as entry_media_audio.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entry_media_video (
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                video_ids TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (feed_url, entry_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_media_scan (
                feed_url TEXT PRIMARY KEY,
                scanned_at REAL NOT NULL,
                found INTEGER NOT NULL DEFAULT 0,
                suggested_audio_feed TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # Added after feed_media_scan shipped: the podcast-host feed discovered
        # for an audio-less feed (so we can suggest subscribing to the audio feed).
        try:
            conn.execute("ALTER TABLE feed_media_scan ADD COLUMN suggested_audio_feed TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # Added later: distinguish a scan that completed cleanly with no audio
        # (back off a long time) from one that errored mid-way — e.g. a host feed
        # we discovered but couldn't fetch (Cloudflare 403) — so transient
        # failures retry soon instead of inheriting the 7-day "empty" backoff.
        try:
            conn.execute("ALTER TABLE feed_media_scan ADD COLUMN ok INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
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
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN webhook_url TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN webhook_format TEXT NOT NULL DEFAULT 'generic'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN webhook_batch INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # youtube_playlist rule type: target playlist + behavior toggles.
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN yt_playlist_id TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN yt_playlist_title TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN yt_include_shorts INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN yt_mark_read INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
        # Duration filter for youtube_playlist (minutes; 0 = no limit).
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN yt_min_minutes INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE highlight_keywords ADD COLUMN yt_max_minutes INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Dedup guard for the youtube_playlist rule: playlistItems.insert is not
        # idempotent, so record each (rule, entry) we've added to avoid re-adding the
        # same video on a later refresh (the cutoff window alone can re-match).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_playlist_added (
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '',
                keyword TEXT NOT NULL DEFAULT '',
                entry_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (scope, scope_id, keyword, entry_id, video_id)
            )
            """
        )
        # Per-user YouTube Data API quota spend, keyed by the Pacific calendar date
        # (Google resets quota at midnight Pacific). The API exposes no remaining-quota
        # read, so we estimate by summing each call's documented unit cost.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yt_quota_spend (
                day TEXT PRIMARY KEY,
                units INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Quire API call log — one row per billed call, used for the sliding-window
        # rate meter (Quire limits per org by minute + hour, with no remaining-quota
        # read). Rows older than an hour are pruned on each insert.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quire_call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quire_call_log_ts ON quire_call_log (ts)")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deviantart_feeds (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                feed_title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_synced_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deviantart_entries (
                id TEXT PRIMARY KEY,
                deviantart_feed_id TEXT NOT NULL REFERENCES deviantart_feeds(id),
                deviationid TEXT NOT NULL,
                title TEXT NOT NULL,
                entry_url TEXT,
                content TEXT,
                published_at TEXT NOT NULL,
                UNIQUE(deviantart_feed_id, deviationid)
            )
            """
        )
        try:
            conn.execute("ALTER TABLE deviantart_feeds ADD COLUMN source TEXT NOT NULL DEFAULT 'gallery'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN hide_shorts INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN inject_source_images INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN feed_thumbnail_url TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN thumb_crop TEXT NOT NULL DEFAULT 'cover'")
        except Exception:
            pass
        # Migrate legacy 'left' value to 'cover-left'
        conn.execute("UPDATE feed_display_prefs SET thumb_crop = 'cover-left' WHERE thumb_crop = 'left'")
        # Normalize any unknown values to 'cover'
        conn.execute(
            "UPDATE feed_display_prefs SET thumb_crop = 'cover' WHERE thumb_crop NOT IN "
            "('cover','cover-top-left','cover-top','cover-top-right','cover-left','cover-right',"
            "'cover-bottom-left','cover-bottom','cover-bottom-right','contain','smart')"
        )
        try:
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN thumb_strategy TEXT")
        except Exception:
            pass
        try:
            # Per-feed SmartCrop min_scale (NULL = default 0.9); applies when thumb_crop='smart'.
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN smart_min_scale REAL")
        except Exception:
            pass
        try:
            # Per-feed Fill zoom multiplier (NULL = default 1.0); applies to cover crop modes.
            conn.execute("ALTER TABLE feed_display_prefs ADD COLUMN fill_zoom REAL")
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS websub_subscriptions (
                feed_url      TEXT PRIMARY KEY,
                hub_url       TEXT,
                secret        TEXT,
                lease_seconds INTEGER DEFAULT 0,
                subscribed_at REAL    DEFAULT 0,
                expires_at    REAL    DEFAULT 0,
                verified      INTEGER DEFAULT 0,
                hub_tried_at  REAL    DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fever_feed_map (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url TEXT UNIQUE NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fever_group_map (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fever_entry_map (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                UNIQUE(feed_url, entry_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fever_entry_map_feed"
            " ON fever_entry_map(feed_url)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS miniflux_feed_map (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url TEXT UNIQUE NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS miniflux_entry_map (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                UNIQUE(feed_url, entry_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_miniflux_entry_map_feed"
            " ON miniflux_entry_map(feed_url)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS greader_tokens (
                token      TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            )
            """
        )
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
            (AUTO_REFRESH_SETTING_KEY, str(get_instance_default_auto_refresh())),
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


# Per-user (app_settings lives in each user's meta DB): user_id -> {key: value}.
# A user absent from the map means "not loaded yet" (was the None sentinel).
_app_settings_cache: dict[str, dict[str, str]] = {}
_app_settings_cache_lock = threading.Lock()


def _load_app_settings_cache(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {str(r["key"]): str(r["value"]) for r in rows}


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a setting from the current user's in-memory cache. Loaded once per
    user on first access, kept consistent through set_setting writes."""
    uid = tenancy.current_user_id()
    with _app_settings_cache_lock:
        cache = _app_settings_cache.get(uid)
        if cache is None:
            cache = _load_app_settings_cache(conn)
            _app_settings_cache[uid] = cache
        return cache.get(key)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Idempotent setting write. Updates the current user's in-memory cache and
    persists only when the value actually changed."""
    uid = tenancy.current_user_id()
    with _app_settings_cache_lock:
        cache = _app_settings_cache.get(uid)
        if cache is None:
            cache = _load_app_settings_cache(conn)
            _app_settings_cache[uid] = cache
        if cache.get(key) == value:
            return
        cache[key] = value
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (key, value),
    )


def get_cached_setting(key: str) -> str | None:
    """Read the current user's cache without a DB connection. None if unloaded."""
    uid = tenancy.current_user_id()
    with _app_settings_cache_lock:
        cache = _app_settings_cache.get(uid)
        if cache is None:
            return None
        return cache.get(key)


def get_runtime_setting(key: str, env_fallback: str = "") -> str:
    """DB setting takes precedence; env_fallback (typically os.getenv) is the fallback."""
    val = get_cached_setting(key)
    if val is not None:
        return val
    return env_fallback


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    uid = tenancy.current_user_id()
    with _app_settings_cache_lock:
        cache = _app_settings_cache.get(uid)
        if cache is not None:
            cache.pop(key, None)
    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))


_DISPLAY_PREF_KEYS = frozenset({"show_lead_image_in_article", "show_lead_image_as_thumb", "show_image_caption", "hide_shorts", "inject_source_images"})
# Pre-built UPDATE statements (one per column) so conn.execute() never receives an f-string.
_DISPLAY_PREF_COLS: dict[str, str] = {k: k for k in _DISPLAY_PREF_KEYS}
_DISPLAY_PREF_SQLS: dict[str, str] = {
    k: f"UPDATE feed_display_prefs SET {k} = ? WHERE feed_url = ?"
    for k in _DISPLAY_PREF_KEYS
}
_DISPLAY_PREF_DEFAULTS: dict = {"show_lead_image_in_article": 1, "show_lead_image_as_thumb": 1, "show_image_caption": -1, "hide_shorts": 0, "inject_source_images": 0, "feed_thumbnail_url": None, "thumb_crop": "cover", "thumb_strategy": None, "smart_min_scale": None, "fill_zoom": None}
_VALID_THUMB_CROPS = frozenset({
    "cover", "cover-top-left", "cover-top", "cover-top-right",
    "cover-left", "cover-right",
    "cover-bottom-left", "cover-bottom", "cover-bottom-right",
    "contain", "smart",
})
_VALID_THUMB_STRATEGIES = frozenset({"inline", "media_rss"})

# Caption text that is purely a date (e.g. "June 12, 2026" or "6/12/2026") — strip it.
_DATE_ONLY_CAP_RE = re.compile(
    r"^\s*(?:\w+ \d{1,2},?\s*\d{4}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2})\s*$",
    re.IGNORECASE,
)


def get_feed_display_prefs(conn: sqlite3.Connection, feed_url: str) -> dict:
    row = conn.execute("SELECT * FROM feed_display_prefs WHERE feed_url = ?", (feed_url,)).fetchone()
    if row:
        return dict(row)
    return {"feed_url": feed_url, **_DISPLAY_PREF_DEFAULTS}


def get_all_feed_display_prefs(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM feed_display_prefs").fetchall()
    return {row["feed_url"]: dict(row) for row in rows}


def upsert_feed_display_pref(conn: sqlite3.Connection, feed_url: str, key: str, value: int) -> None:
    col = _DISPLAY_PREF_COLS.get(key)
    if col is None:
        raise ValueError(f"Unknown display pref key: {key}")
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(_DISPLAY_PREF_SQLS[col], (value, feed_url))


def upsert_feed_thumbnail_url(conn: sqlite3.Connection, feed_url: str, thumbnail_url: str | None) -> None:
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(
        "UPDATE feed_display_prefs SET feed_thumbnail_url = ? WHERE feed_url = ?",
        (thumbnail_url or None, feed_url),
    )


def upsert_feed_thumb_crop(conn: sqlite3.Connection, feed_url: str, crop: str) -> None:
    crop = crop if crop in _VALID_THUMB_CROPS else "cover"
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(
        "UPDATE feed_display_prefs SET thumb_crop = ? WHERE feed_url = ?",
        (crop, feed_url),
    )


def upsert_feed_smart_min_scale(conn: sqlite3.Connection, feed_url: str, min_scale: float | None) -> None:
    value = min(1.0, max(0.5, min_scale)) if min_scale is not None else None
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(
        "UPDATE feed_display_prefs SET smart_min_scale = ? WHERE feed_url = ?",
        (value, feed_url),
    )


def upsert_feed_fill_zoom(conn: sqlite3.Connection, feed_url: str, zoom: float | None) -> None:
    value = min(2.0, max(0.5, zoom)) if zoom is not None else None
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(
        "UPDATE feed_display_prefs SET fill_zoom = ? WHERE feed_url = ?",
        (value, feed_url),
    )


def upsert_feed_thumb_strategy(conn: sqlite3.Connection, feed_url: str, strategy: str | None) -> None:
    s = strategy if strategy in _VALID_THUMB_STRATEGIES else None
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(
        "UPDATE feed_display_prefs SET thumb_strategy = ? WHERE feed_url = ?",
        (s, feed_url),
    )


_HIGHLIGHT_VALID_COLORS = frozenset({'yellow', 'green', 'blue', 'pink', 'orange'})
_HIGHLIGHT_VALID_SCOPES = frozenset({'global', 'folder', 'feed', 'feeds'})


_HIGHLIGHT_VALID_TYPES = {"highlight", "mark_as_read", "email_article", "deduplicate", "webhook", "youtube_playlist", "instapaper", "quire"}
_HIGHLIGHT_VALID_SEARCH_IN = {"title", "body", "both"}
_HIGHLIGHT_VALID_DELIVERY = {"immediately", "batch"}
_DEDUP_VALID_MATCH_METHODS = {"slug", "title", "both", "fuzzy", "safe"}


def get_highlight_keywords(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT scope, scope_id, keyword, color, is_regex, enabled, type, search_in, delivery,"
        " email_to, batch_time, batch_count, cc_me, dedup_window_hours, exclude_scope_ids, sort_order,"
        " webhook_url, webhook_format, webhook_batch,"
        " yt_playlist_id, yt_playlist_title, yt_include_shorts, yt_mark_read,"
        " yt_min_minutes, yt_max_minutes"
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
    webhook_url: str = "",
    webhook_format: str = "generic",
    webhook_batch: bool = False,
    yt_playlist_id: str = "",
    yt_playlist_title: str = "",
    yt_include_shorts: bool = False,
    yt_mark_read: bool = True,
    yt_min_minutes: int = 0,
    yt_max_minutes: int = 0,
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
    if webhook_format not in WEBHOOK_VALID_FORMATS:
        webhook_format = "generic"
    conn.execute(
        "INSERT OR REPLACE INTO highlight_keywords"
        " (scope, scope_id, keyword, color, is_regex, enabled, type, search_in, delivery,"
        "  email_to, batch_time, batch_count, cc_me, dedup_window_hours, exclude_scope_ids,"
        "  webhook_url, webhook_format, webhook_batch,"
        "  yt_playlist_id, yt_playlist_title, yt_include_shorts, yt_mark_read,"
        "  yt_min_minutes, yt_max_minutes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, scope_id, keyword.strip(), color, 1 if is_regex else 0, 1 if enabled else 0,
         rule_type, search_in, delivery,
         email_to.strip(), batch_time.strip(), max(0, int(batch_count or 0)), 1 if cc_me else 0,
         max(1, int(dedup_window_hours or 168)), exclude_scope_ids.strip(),
         webhook_url.strip(), webhook_format, 1 if webhook_batch else 0,
         yt_playlist_id.strip(), yt_playlist_title.strip(),
         1 if yt_include_shorts else 0, 1 if yt_mark_read else 0,
         max(0, int(yt_min_minutes or 0)), max(0, int(yt_max_minutes or 0))),
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
        return get_instance_default_auto_refresh()
    try:
        return normalize_auto_refresh_minutes(int(raw_value))
    except ValueError:
        return get_instance_default_auto_refresh()


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


def _get_or_create_folder_by_name(conn: sqlite3.Connection, name: str) -> int:
    """Return the id of a top-level folder with `name`, creating it under root if absent."""
    row = conn.execute("SELECT id FROM folders WHERE name = ? LIMIT 1", (name,)).fetchone()
    if row:
        return int(row["id"])
    root_id = get_root_folder_id(conn)
    conn.execute("INSERT INTO folders (name, parent_id) VALUES (?, ?)", (name, root_id))
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


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
            f.cadence_minutes,
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
    # The virtual "Uncategorized" folder has no folder_feeds rows; its members are
    # every reader feed not in any folder. Resolving it here lets all folder
    # actions (mark-read, refresh, …) operate on it uniformly.
    if folder_id == UNCATEGORIZED_FOLDER_ID:
        return get_all_reader_feed_urls() - get_all_feed_urls(conn)
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


def get_all_reader_feed_urls() -> set[str]:
    """Return every feed URL the reader is subscribed to.

    Unlike get_all_feed_urls (which reads folder_feeds), this covers feeds that
    were never assigned to a folder — the source set for the virtual
    "Uncategorized" folder. Read straight from the reader DB to stay O(feeds)
    without materializing full feed objects.
    """
    conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5.0)
    try:
        return {str(r[0]) for r in conn.execute("SELECT url FROM feeds")}
    finally:
        conn.close()


# A "feeds" rule scope targets an explicit set of feeds (not a whole folder). Its
# scope_id is the feed URLs joined by newline (URLs may contain commas, so newline
# is the safe separator). These helpers centralize parse + scope→feed-set + the
# per-feed "is this feed in scope" check used by the after-refresh runners.
_FEEDS_SCOPE_SEP = "\n"


def parse_feeds_scope_id(scope_id: str) -> list[str]:
    return [u.strip() for u in (scope_id or "").split(_FEEDS_SCOPE_SEP) if u.strip()]


def resolve_rule_feed_urls(conn: sqlite3.Connection, scope: str, scope_id: str) -> set[str] | None:
    """Return the set of feed URLs a rule applies to, or None for global (all feeds)."""
    if scope == "global":
        return None
    if scope == "folder":
        try:
            return get_folder_feed_urls(conn, int(scope_id))
        except (ValueError, TypeError):
            return set()
    if scope == "feed":
        return {scope_id} if scope_id else set()
    if scope == "feeds":
        return set(parse_feeds_scope_id(scope_id))
    return set()


def feed_in_rule_scope(scope: str, scope_id: str, feed_url: str, folder_feed_urls: set[str] | None) -> bool:
    """Per-feed scope test for the after-refresh runners. ``folder_feed_urls`` is the
    prefetched feed set for a folder-scoped rule (ignored for other scopes)."""
    if scope == "global":
        return True
    if scope == "folder":
        return feed_url in (folder_feed_urls or set())
    if scope == "feed":
        return scope_id == feed_url
    if scope == "feeds":
        return feed_url in parse_feeds_scope_id(scope_id)
    return False


def get_disabled_feed_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT feed_url FROM disabled_feeds").fetchall()
    return {str(r["feed_url"]) for r in rows}


def get_browser_ua_feed_urls(conn: sqlite3.Connection) -> set[str]:
    """Feeds whose fetch should use a browser identity (UA + headers).

    A feed lands here only after an honest-UA fetch was *refused* (HTTP
    403/415/429/503 or a hang) — see `_maybe_flag_browser_ua`. The request hook in
    services.reader_api consults this set so the next fetch escalates. This is a
    good-citizen escalation (only on refusal, never preemptive); it does not evade
    IP-level blocks."""
    rows = conn.execute("SELECT feed_url FROM browser_ua_feeds").fetchall()
    return {str(r["feed_url"]) for r in rows}


def flag_browser_ua_feed(conn: sqlite3.Connection, feed_url: str, reason: str = "") -> bool:
    """Mark a feed for browser-identity fetches. Returns True if newly flagged."""
    feed_url = feed_url.strip()
    if not feed_url:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO browser_ua_feeds (feed_url, reason) VALUES (?, ?)",
        (feed_url, reason[:200]),
    )
    return cur.rowcount > 0


def unflag_browser_ua_feed(conn: sqlite3.Connection, feed_url: str) -> None:
    conn.execute("DELETE FROM browser_ua_feeds WHERE feed_url = ?", (feed_url.strip(),))


def disable_feed(feed_url: str) -> None:
    """Disable a feed (no fetching) without unsubscribing. Pause and Disable are the
    same state: this sets both Lectio's disabled_feeds row AND reader's
    updates_enabled flag, so the Settings tree and Feed Properties agree and the
    scheduler skips it regardless of which one is consulted."""
    feed_url = feed_url.strip()
    if not feed_url:
        return
    with get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO disabled_feeds (feed_url) VALUES (?)",
            (feed_url,),
        )
    try:
        with get_reader() as reader:
            reader.disable_feed_updates(feed_url)
    except Exception:
        LOGGER.debug("disable_feed: reader.disable_feed_updates failed for %s", feed_url, exc_info=True)
    invalidate_meta_structure_cache()


def enable_feed(feed_url: str) -> None:
    """Re-enable a disabled/paused feed: clears both flags and resets the backoff so
    it's checked on the next cycle (the inverse of disable_feed)."""
    feed_url = feed_url.strip()
    if not feed_url:
        return
    with get_meta_connection() as conn:
        conn.execute("DELETE FROM disabled_feeds WHERE feed_url = ?", (feed_url,))
        # Clear the backoff so the feed is checked next cycle, not after the old
        # retry window.
        conn.execute(
            "UPDATE feed_failure_state SET next_retry_at = NULL WHERE feed_url = ?",
            (feed_url,),
        )
    try:
        with get_reader() as reader:
            reader.enable_feed_updates(feed_url)
    except Exception:
        LOGGER.debug("enable_feed: reader.enable_feed_updates failed for %s", feed_url, exc_info=True)
    invalidate_problematic_feeds_cache()
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
            return dict(_meta_structure_cache)
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
    folder_rows: Sequence[sqlite3.Row | dict],
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
        conn = sqlite3.connect(str(tenancy.reader_db_path()), uri=False, check_same_thread=False)
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
                threading.Thread(target=_run_in_user_context, args=(tenancy.current_user_id(), _refresh_unread_counts_async, current_gen), daemon=True).start()
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


def _resolve_dedup_feed_urls(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    exclude_scope_ids: str = "",
    custom_feed_urls: set[str] | None = None,
) -> set[str] | dict:
    """Resolve the in-scope feed URLs for a dedup run (shared by preview + apply).

    Returns the feed-URL set, or an ``{"error": ...}`` dict for an invalid scope.
    A custom feed set (the manual compare picker) bypasses scope/exclude resolution."""
    if custom_feed_urls is not None:
        return custom_feed_urls
    if scope == "global":
        feed_urls = get_all_feed_urls(conn)
    elif scope == "folder":
        try:
            fid = int(scope_id)
        except (ValueError, TypeError):
            return {"error": "invalid scope_id"}
        feed_urls = get_folder_feed_urls(conn, fid)
    elif scope == "feeds":
        # Dedupe across an explicit set of selected feeds (>=2 needed; checked by caller).
        feed_urls = set(parse_feeds_scope_id(scope_id))
    else:
        return {"error": "deduplicate rules require global, folder, or multi-feed scope"}
    if exclude_scope_ids:
        excluded: set[str] = set()
        for fid_str in exclude_scope_ids.split(","):
            fid_str = fid_str.strip()
            if fid_str.isdigit():
                excluded |= get_folder_feed_urls(conn, int(fid_str))
        feed_urls -= excluded
    return feed_urls


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
    feed_urls = _resolve_dedup_feed_urls(conn, scope, scope_id, exclude_scope_ids, custom_feed_urls)
    if isinstance(feed_urls, dict):
        return feed_urls  # {"error": ...}
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
    match_all_if_empty: bool = False,
    exclude_shorts: bool = False,
    min_secs: int = 0,
    max_secs: int = 0,
) -> dict:
    """Preview which entries a pattern-based rule would affect (read + unread, newest first).

    ``match_all_if_empty`` supports rules whose keyword is an optional filter (e.g.
    youtube_playlist: a blank keyword means "every entry in scope"). ``exclude_shorts``
    drops YouTube Shorts from the preview so it matches what a youtube_playlist rule
    with Include-Shorts off would actually add."""
    import re as _re

    if not keyword:
        if not match_all_if_empty:
            return {"matches": [], "total_scanned": 0, "total_matches": 0, "truncated": False}
        match_fn = lambda text: True
    else:
        try:
            if is_regex:
                pattern = _re.compile(keyword, _re.IGNORECASE)
                match_fn = lambda text: bool(pattern.search(text)) if text else False
            else:
                kw_lower = keyword.lower()
                match_fn = lambda text: kw_lower in (text or "").lower()
        except _re.error as e:
            return {"error": f"Invalid regex: {e}"}

    if scope == "folder":
        try:
            int(scope_id)
        except (ValueError, TypeError):
            return {"error": "invalid scope_id"}
    feed_urls: set[str] | None = resolve_rule_feed_urls(conn, scope, scope_id)

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
            if exclude_shorts and _is_youtube_short(entry):
                continue
            if (min_secs or max_secs):
                # Duration filter preview: use the entry's primary video (its link).
                _vid = youtube_duration_service.extract_video_id(str(entry.link or ""))
                _dur = youtube_duration_service.get_cached_duration(_vid)[0] if _vid else None
                if _dur is None or (min_secs and _dur < min_secs) or (max_secs and _dur > max_secs):
                    continue
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
    feed_urls = _resolve_dedup_feed_urls(conn, scope, scope_id, exclude_scope_ids)
    if isinstance(feed_urls, dict):
        return feed_urls  # {"error": ...}
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

    if scope == "folder":
        try:
            int(scope_id)
        except (ValueError, TypeError):
            return {"error": "invalid scope_id"}
    feed_urls: set[str] | None = resolve_rule_feed_urls(conn, scope, scope_id)

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


_SHORTS_HASHTAGS = ("#shorts", "#short", "#ytshorts", "#youtubeshorts")

def _is_youtube_short(entry: object) -> bool:
    """Return True if the entry is a YouTube Short.

    YouTube channel feeds use watch?v= links for all videos, so /shorts/ in the
    link is rare. Detect via three signals:
    1. /shorts/ in the link (direct shorts URLs, playlist feeds).
    2. #shorts/#short hashtag in title or description (creator-applied tag;
       YouTube surfaces Shorts on the Shorts shelf when creators add this).
    3. Cached duration ≤ 60 s (in-memory lookup, no I/O).
    """
    link = str(getattr(entry, "link", None) or "")
    if "youtube.com/shorts/" in link:
        return True

    title = (getattr(entry, "title", None) or "").lower()
    summary = (getattr(entry, "summary", None) or "").lower()
    content_text = "".join(
        (getattr(c, "value", None) or "").lower()
        for c in (getattr(entry, "content", None) or [])
    )
    for text in (title, summary, content_text):
        if any(tag in text for tag in _SHORTS_HASHTAGS):
            return True

    vid = youtube_duration_service.extract_video_id(link)
    if vid:
        dur, _ = youtube_duration_service.get_cached_duration(vid)
        if dur is not None and dur <= 60:
            return True

    return False


def _mark_existing_shorts_read(feed_urls: Iterable[str]) -> int:
    """Mark every currently-unread YouTube Short in ``feed_urls`` as read.

    Shared by the after-refresh hide-shorts pass and the per-feed Hide-Shorts
    toggle (so flipping it on immediately clears the backlog of Shorts, not just
    future ones). Returns the number of entries marked read."""
    feed_urls = set(feed_urls)
    if not feed_urls:
        return 0
    global _unread_counts_generation
    now_str = datetime.now().isoformat()
    to_mark: list[tuple[str, str]] = []
    with get_reader() as reader:
        for feed_url in feed_urls:
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
    return len(to_mark)


_CHURN_TITLE_MIN_WORDS = 4   # require at least 4 words — avoids "New post" / "Update" false positives
_CHURN_TITLE_DATE_DAYS  = 7  # published dates must be within this many days of each other


def _suppress_guid_churn(reader, conn, feed_url: str) -> int:
    """Auto-mark newly-seen unread entries as read when another entry in the same
    feed already has the same URL slug or the same title + publication date —
    indicating the publisher re-issued the same article with a new GUID and/or
    URL (CMS migration, permalink rebuild, etc.).

    Two matching strategies:
    - Slug match: URL path slug is identical (near-zero false-positive rate).
    - Title+date match: normalised title is identical AND published dates are
      within _CHURN_TITLE_DATE_DAYS.  Requires at least _CHURN_TITLE_MIN_WORDS
      words to guard against short/generic titles like "Update" or "Episode 12".
      Title-only matching is deliberately not used because some feeds reuse
      titles across unrelated entries (weekly digests, daily roundups).

    Compares new entries against both READ history and EXISTING UNREAD entries so
    that duplicate re-publications are caught even when the originals were never read.

    Returns the number of entries suppressed.
    """
    from datetime import datetime, timedelta

    recent_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=90)

    all_unread = list(reader.get_entries(feed=feed_url, read=False))
    new_unread = [e for e in all_unread if getattr(e, "added", None) and e.added >= recent_cutoff]
    if not new_unread:
        return 0

    old_unread = [e for e in all_unread if e not in new_unread]

    # Build slug set and title→[pub_dates] map from read entries (last 6 months).
    history_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=180)
    known_slugs: set[str] = set()
    known_title_dates: dict[str, list[datetime]] = {}
    for entry in reader.get_entries(feed=feed_url, read=True, limit=5000):
        pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
        if pub and pub < history_cutoff:
            break
        if entry.link:
            slug = _safe_dedup_entry_slug(entry.link)
            if slug:
                known_slugs.add(slug)
        if entry.title and pub:
            norm = normalize_entry_title_for_dedupe(entry.title)
            if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS:
                known_title_dates.setdefault(norm, []).append(pub)

    # Also index old unread entries — catches dupes that arrived before the user read any copy.
    for entry in old_unread:
        if entry.link:
            slug = _safe_dedup_entry_slug(entry.link)
            if slug:
                known_slugs.add(slug)
        pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
        if entry.title and pub:
            norm = normalize_entry_title_for_dedupe(entry.title)
            if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS:
                known_title_dates.setdefault(norm, []).append(pub)

    to_suppress = []
    for entry in new_unread:
        # Slug match (highest confidence).
        if entry.link:
            slug = _safe_dedup_entry_slug(entry.link)
            if slug and slug in known_slugs:
                to_suppress.append(entry)
                continue

        # Title+date match (handles feeds that change both GUID and URL).
        if entry.title and known_title_dates:
            norm = normalize_entry_title_for_dedupe(entry.title)
            if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS and norm in known_title_dates:
                entry_pub = (
                    getattr(entry, "published", None)
                    or getattr(entry, "updated", None)
                    or getattr(entry, "added", None)
                )
                if entry_pub:
                    threshold = _CHURN_TITLE_DATE_DAYS * 86400
                    for known_pub in known_title_dates[norm]:
                        if abs((entry_pub - known_pub).total_seconds()) <= threshold:
                            to_suppress.append(entry)
                            break

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


def _cleanup_intra_feed_slug_dupes(reader, conn) -> int:
    """Retroactive scan: suppress duplicate unread entries that the web UI already
    hides at render time but that mobile GReader clients still see as separate items.

    Two passes:

    Pass 1 — per-feed: slug or title+date duplicates within the same feed.
      Keeps the oldest copy, marks the rest read.

    Pass 2 — cross-feed: entries in different feeds with the same canonical link URL.
      Same article syndicated to two feeds (e.g. blog + planet aggregator).
      Keeps the oldest copy across feeds, marks the rest read.

    Returns total entries suppressed.
    """
    from datetime import datetime

    when = datetime.now().isoformat()
    suppressed_ids: set[tuple[str, str]] = set()
    all_to_suppress: list = []
    threshold = _CHURN_TITLE_DATE_DAYS * 86400

    # ── Pass 1: per-feed slug + title+date dedup ──────────────────────────────
    for feed in reader.get_feeds():
        feed_url = str(feed.url)
        try:
            slug_entries: dict[str, list] = {}
            title_entries: dict[str, list] = {}
            for entry in reader.get_entries(feed=feed_url, read=False):
                pub = (getattr(entry, "published", None)
                       or getattr(entry, "updated", None)
                       or getattr(entry, "added", None))
                pub_ts = pub.timestamp() if pub else 0.0
                if entry.link:
                    slug = _safe_dedup_entry_slug(entry.link)
                    if slug:
                        slug_entries.setdefault(slug, []).append((pub_ts, entry))
                if entry.title and pub:
                    norm = normalize_entry_title_for_dedupe(entry.title)
                    if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS:
                        title_entries.setdefault(norm, []).append((pub_ts, entry))

            for slug, items in slug_entries.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda x: x[0])
                for _, entry in items[1:]:
                    eid = (str(entry.feed_url), str(entry.id))
                    if eid not in suppressed_ids:
                        suppressed_ids.add(eid)
                        all_to_suppress.append(entry)

            for norm, items in title_entries.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda x: x[0])
                oldest_ts = items[0][0]
                for pub_ts, entry in items[1:]:
                    if abs(pub_ts - oldest_ts) <= threshold:
                        eid = (str(entry.feed_url), str(entry.id))
                        if eid not in suppressed_ids:
                            suppressed_ids.add(eid)
                            all_to_suppress.append(entry)
        except Exception:
            LOGGER.exception("[guid-churn-cleanup] error on feed %s", feed_url)

    # ── Pass 2: cross-feed identical-link dedup ───────────────────────────────
    # Mirrors the build_entry_dedupe_key logic the web UI uses at render time.
    # An identical canonical link in two different feeds is a near-certain
    # syndication duplicate (blog + planet, RSS + Atom of same feed, etc.).
    try:
        link_entries: dict[str, list] = {}
        for entry in reader.get_entries(read=False):
            if not entry.link:
                continue
            canon = normalize_entry_link_for_dedupe(entry.link)
            if not canon:
                continue
            pub = (getattr(entry, "published", None)
                   or getattr(entry, "updated", None)
                   or getattr(entry, "added", None))
            pub_ts = pub.timestamp() if pub else 0.0
            link_entries.setdefault(canon, []).append((pub_ts, entry))

        for canon, items in link_entries.items():
            # Only act when entries come from at least two different feeds.
            if len({str(e.feed_url) for _, e in items}) < 2:
                continue
            items.sort(key=lambda x: x[0])
            for _, entry in items[1:]:
                eid = (str(entry.feed_url), str(entry.id))
                if eid not in suppressed_ids:
                    suppressed_ids.add(eid)
                    all_to_suppress.append(entry)
    except Exception:
        LOGGER.exception("[guid-churn-cleanup] error during cross-feed link dedup")

    if not all_to_suppress:
        return 0

    for entry in all_to_suppress:
        reader.mark_entry_as_read((str(entry.feed_url), str(entry.id)))
    conn.executemany(
        "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)"
        " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at=excluded.read_at",
        [(str(e.feed_url), str(e.id), when) for e in all_to_suppress],
    )
    return len(all_to_suppress)


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

    # Cross-feed identical-link dedup: catches syndication dupes (blog + planet, etc.)
    # that the web UI hides at render time but GReader clients see as separate items.
    try:
        with get_reader() as reader:
            with get_meta_connection() as conn:
                cross_suppressed = _cleanup_intra_feed_slug_dupes(reader, conn)
        if cross_suppressed:
            _unread_counts_generation += 1
            LOGGER.info("[guid-churn] suppressed %d cross-feed duplicate entries", cross_suppressed)
    except Exception:
        LOGGER.exception("[guid-churn] error during cross-feed dedup")

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
        # Global toggle: hide Shorts on every refreshed YouTube feed, regardless of
        # the per-feed pref.
        if youtube_hide_shorts_global():
            shorts_targets = shorts_targets | {
                u for u in refreshed_feed_urls if "youtube.com/feeds/videos.xml" in u
            }
        if shorts_targets:
            _mark_existing_shorts_read(shorts_targets)
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
                        _folder_set = folder_feed_map.get(int(scope_id)) if (scope == "folder" and str(scope_id).isdigit()) else None
                        in_scope = feed_in_rule_scope(scope, scope_id, feed_url, _folder_set)

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
                    elif scope == "feeds":
                        in_scope = bool(refreshed_feed_urls & set(parse_feeds_scope_id(scope_id)))
                    else:
                        in_scope = False  # dedup requires global / folder / multi-feed scope

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
        # Webhook rules likewise fire after mark_as_read/dedup.
        _run_webhook_rules_after_refresh(refreshed_feed_urls)
        _run_instapaper_rules_after_refresh(refreshed_feed_urls)
        _run_quire_rules_after_refresh(refreshed_feed_urls)
        # YouTube auto-add-to-playlist rules (after mark_as_read so a "mark read after
        # add" doesn't fight an earlier rule).
        _run_youtube_playlist_rules_after_refresh(refreshed_feed_urls)
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
                        _folder_set = folder_feed_map.get(int(scope_id)) if (scope == "folder" and str(scope_id).isdigit()) else None
                        in_scope = feed_in_rule_scope(scope, scope_id, feed_url, _folder_set)
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


_WEBHOOK_AUTO_PER_RUN_CAP = 50  # max webhook POSTs per refresh cycle


def _run_webhook_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """POST webhook-rule matches for freshly-refreshed feeds (immediate delivery)."""
    if not refreshed_feed_urls:
        return

    try:
        from datetime import timedelta, timezone as _tz
        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

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

        webhook_rules = [
            r for r in all_rules
            if r.get("enabled") and r.get("type") == "webhook" and r.get("webhook_url")
        ]
        if not webhook_rules:
            return

        sent = 0
        now_str = datetime.now().isoformat()

        for rule in webhook_rules:
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                webhook_url = str(rule.get("webhook_url") or "")
                webhook_format = str(rule.get("webhook_format") or "generic")
                webhook_batch = bool(rule.get("webhook_batch"))

                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    batch_articles: list[dict] = []

                    for feed_url in refreshed_feed_urls:
                        _folder_set = folder_feed_map.get(int(scope_id)) if (scope == "folder" and str(scope_id).isdigit()) else None
                        in_scope = feed_in_rule_scope(scope, scope_id, feed_url, _folder_set)
                        if not in_scope:
                            continue

                        for entry in reader.get_entries(feed=feed_url):
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            if not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            if sent >= _WEBHOOK_AUTO_PER_RUN_CAP:
                                continue

                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    f = reader.get_feed(fu)
                                    feed_title_cache[fu] = str(getattr(f, "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu

                            published = getattr(entry, "published", None) or getattr(entry, "updated", None)
                            article = {
                                "feed_url": fu,
                                "entry_id": str(entry.id),
                                "title": str(entry.title or ""),
                                "link": str(entry.link or ""),
                                "feed_title": feed_title_cache.get(fu, fu),
                                "excerpt": _get_entry_excerpt(entry),
                                "published": published.isoformat() if published else "",
                                "tags": get_manual_tags_for_entry(fu, str(entry.id)),
                            }

                            if webhook_batch:
                                batch_articles.append(article)
                                sent += 1
                            else:
                                payload = build_webhook_payload(article, webhook_format)
                                ok, err = send_webhook(webhook_url, payload)
                                if ok:
                                    sent += 1
                                    with get_meta_connection() as conn:
                                        _log_auto_run(conn, now_str, "webhook", scope, scope_id, keyword, {
                                            "count": 1,
                                            "entries": [article],
                                        })
                                else:
                                    LOGGER.warning("[webhook-auto] POST failed: %s", err)

                if webhook_batch and batch_articles:
                    payload = build_webhook_batch_payload(batch_articles, webhook_format)
                    ok, err = send_webhook(webhook_url, payload)
                    if ok:
                        with get_meta_connection() as conn:
                            _log_auto_run(conn, now_str, "webhook", scope, scope_id, keyword, {
                                "count": len(batch_articles),
                                "entries": batch_articles,
                            })
                    else:
                        LOGGER.warning("[webhook-auto] batch POST failed: %s", err)
            except Exception:
                LOGGER.exception("[webhook-auto] error processing webhook rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[webhook-auto] error in _run_webhook_rules_after_refresh")


_INSTAPAPER_AUTO_PER_RUN_CAP = 50  # max Instapaper saves per refresh cycle


def _run_instapaper_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Save matching freshly-refreshed entries to Instapaper. Instapaper dedupes by
    URL, so re-saves are harmless; the 15-min cutoff + per-run cap bound the calls."""
    if not refreshed_feed_urls:
        return
    username = get_runtime_setting(SETTING_INSTAPAPER_USERNAME).strip()
    password = get_runtime_setting(SETTING_INSTAPAPER_PASSWORD).strip()
    if not (username and password):
        return

    try:
        from datetime import timedelta, timezone as _tz
        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            folder_ids_needed = {
                int(r["scope_id"]) for r in all_rules
                if r.get("enabled") and r["scope"] == "folder" and str(r.get("scope_id", "")).isdigit()
            }
            folder_feed_map: dict[int, set[str]] = {
                fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed
            }

        rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "instapaper"]
        if not rules:
            return

        sent = 0
        now_str = datetime.now().isoformat()
        for rule in rules:
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    for feed_url in refreshed_feed_urls:
                        _folder_set = folder_feed_map.get(int(scope_id)) if (scope == "folder" and str(scope_id).isdigit()) else None
                        if not feed_in_rule_scope(scope, scope_id, feed_url, _folder_set):
                            continue
                        for entry in reader.get_entries(feed=feed_url):
                            if sent >= _INSTAPAPER_AUTO_PER_RUN_CAP:
                                break
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            # Empty keyword = save every new entry in scope.
                            if keyword and not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            link = str(entry.link or "")
                            if not link:
                                continue
                            ok, err = _instapaper_save_url(username, password, link, str(entry.title or ""))
                            if not ok:
                                LOGGER.warning("[instapaper-auto] save failed: %s", err)
                                continue
                            sent += 1
                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    feed_title_cache[fu] = str(getattr(reader.get_feed(fu), "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu
                            with get_meta_connection() as conn:
                                _log_auto_run(conn, now_str, "instapaper", scope, scope_id, keyword, {
                                    "count": 1,
                                    "entries": [{"feed_url": fu, "entry_id": str(entry.id),
                                                 "title": str(entry.title or ""), "link": link,
                                                 "feed_title": feed_title_cache.get(fu, fu)}],
                                })
            except Exception:
                LOGGER.exception("[instapaper-auto] error processing rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[instapaper-auto] error in _run_instapaper_rules_after_refresh")


# Cap a run well under the Free-tier 50/min so an automation burst never trips the
# Quire rate limit; the sliding-window meter is also consulted before each add.
_QUIRE_AUTO_PER_RUN_CAP = 20


def _run_quire_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Add matching freshly-refreshed entries as tasks to the default Quire project.
    Bounded by the 15-min cutoff, a per-run cap, the usage meter, and 429 backoff."""
    if not refreshed_feed_urls:
        return
    if not is_quire_configured():
        return
    project_oid = quire_project_oid()
    token = get_quire_user_token()
    if not token:
        return

    try:
        from datetime import timedelta, timezone as _tz
        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            folder_ids_needed = {
                int(r["scope_id"]) for r in all_rules
                if r.get("enabled") and r["scope"] == "folder" and str(r.get("scope_id", "")).isdigit()
            }
            folder_feed_map: dict[int, set[str]] = {
                fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed
            }

        rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "quire"]
        if not rules:
            return

        sent = 0
        now_str = datetime.now().isoformat()
        for rule in rules:
            if sent >= _QUIRE_AUTO_PER_RUN_CAP:
                break
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    for feed_url in refreshed_feed_urls:
                        _folder_set = folder_feed_map.get(int(scope_id)) if (scope == "folder" and str(scope_id).isdigit()) else None
                        if not feed_in_rule_scope(scope, scope_id, feed_url, _folder_set):
                            continue
                        for entry in reader.get_entries(feed=feed_url):
                            if sent >= _QUIRE_AUTO_PER_RUN_CAP:
                                break
                            if get_quire_usage_status()["state"] == "blocked":
                                LOGGER.warning("[quire-auto] rate limit reached; %d added this run", sent)
                                return
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            if keyword and not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            link = str(entry.link or "")
                            if not link:
                                continue
                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    feed_title_cache[fu] = str(getattr(reader.get_feed(fu), "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu
                            ok, err = _quire_add_entry(token, project_oid, str(entry.title or ""), link, feed_title_cache.get(fu, fu))
                            if not ok:
                                LOGGER.warning("[quire-auto] add failed: %s", err)
                                if isinstance(err, str) and "rate limit" in err.lower():
                                    return  # back off the whole run on 429
                                continue
                            sent += 1
                            with get_meta_connection() as conn:
                                _log_auto_run(conn, now_str, "quire", scope, scope_id, keyword, {
                                    "count": 1,
                                    "entries": [{"feed_url": fu, "entry_id": str(entry.id),
                                                 "title": str(entry.title or ""), "link": link,
                                                 "feed_title": feed_title_cache.get(fu, fu)}],
                                })
            except Exception:
                LOGGER.exception("[quire-auto] error processing rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[quire-auto] error in _run_quire_rules_after_refresh")


# Each playlistItems.insert costs 50 quota units; cap a run well under the daily
# 10k so auto-add never exhausts the quota on a burst of new uploads.
_YT_PLAYLIST_AUTO_PER_RUN_CAP = 25


def _run_youtube_playlist_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Add newly-refreshed matching entries' YouTube videos to a target playlist.

    A YouTube video can be embedded in any feed, so this is a general rule (any
    feed/folder), and one entry can carry several videos. Extracts all video ids
    from the entry link + content, inserts each into the rule's playlist, and
    optionally marks the post read. Non-idempotent inserts are guarded by the
    youtube_playlist_added table so a video is never added twice.
    """
    if not refreshed_feed_urls:
        return
    global _unread_counts_generation

    try:
        from datetime import timedelta, timezone as _tz
        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            yt_rules = [
                r for r in all_rules
                if r.get("enabled") and r.get("type") == "youtube_playlist" and r.get("yt_playlist_id")
            ]
            if not yt_rules:
                return
            folder_feed_map: dict[int, set[str]] = {
                int(r["scope_id"]): get_folder_feed_urls(conn, int(r["scope_id"]))
                for r in yt_rules
                if r["scope"] == "folder" and str(r.get("scope_id", "")).isdigit()
            }

        token = get_youtube_oauth_token()
        if not token:
            LOGGER.warning("[yt-playlist-auto] %d rule(s) enabled but no YouTube token — reconnect needed", len(yt_rules))
            return

        added_total = 0
        now_str = datetime.now().isoformat()

        for rule in yt_rules:
            if added_total >= _YT_PLAYLIST_AUTO_PER_RUN_CAP:
                break
            scope = str(rule.get("scope", ""))
            scope_id = str(rule.get("scope_id") or "")
            keyword = str(rule.get("keyword", ""))
            is_regex = bool(rule.get("is_regex"))
            search_in = str(rule.get("search_in") or "title")
            playlist_id = str(rule.get("yt_playlist_id") or "")
            include_shorts = bool(rule.get("yt_include_shorts"))
            mark_read = bool(rule.get("yt_mark_read"))
            min_secs = max(0, int(rule.get("yt_min_minutes") or 0)) * 60
            max_secs = max(0, int(rule.get("yt_max_minutes") or 0)) * 60
            run_entries: list[dict] = []
            marked: list[tuple[str, str]] = []
            try:
                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    for feed_url in refreshed_feed_urls:
                        _folder_set = folder_feed_map.get(int(scope_id)) if (scope == "folder" and str(scope_id).isdigit()) else None
                        in_scope = feed_in_rule_scope(scope, scope_id, feed_url, _folder_set)
                        if not in_scope:
                            continue

                        for entry in reader.get_entries(feed=feed_url):
                            if added_total >= _YT_PLAYLIST_AUTO_PER_RUN_CAP:
                                break
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            # Empty keyword = add every new video in scope.
                            if keyword and not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            if not include_shorts and _is_youtube_short(entry):
                                continue
                            link = str(entry.link or "")
                            body = "".join((c.value or "") for c in (entry.content or []))
                            body += str(entry.summary or "")
                            vids = youtube_embeds.video_ids_in_text(link, body)
                            if not vids:
                                continue
                            fu = str(entry.feed_url or "")
                            eid = str(entry.id)
                            entry_added_any = False
                            for vid in vids:
                                if added_total >= _YT_PLAYLIST_AUTO_PER_RUN_CAP:
                                    break
                                # Duration filter (minutes; 0 = no limit). The video's
                                # length comes from the same cache that powers the
                                # [duration] title prefix; an unknown duration is skipped
                                # this run (it's retried once the duration is cached).
                                if min_secs or max_secs:
                                    dur = youtube_duration_service.get_cached_duration(vid)[0]
                                    if dur is None:
                                        continue
                                    if min_secs and dur < min_secs:
                                        continue
                                    if max_secs and dur > max_secs:
                                        continue
                                # Dedup guard: claim the (rule, entry, video) row first;
                                # rowcount 0 means we've added it before — skip.
                                with get_meta_connection() as conn:
                                    cur = conn.execute(
                                        "INSERT OR IGNORE INTO youtube_playlist_added"
                                        " (scope, scope_id, keyword, entry_id, video_id, added_at)"
                                        " VALUES (?,?,?,?,?,?)",
                                        (scope, scope_id, keyword, eid, vid, now_str),
                                    )
                                    claimed = cur.rowcount > 0
                                if not claimed:
                                    continue
                                try:
                                    youtube_oauth_service.add_video_to_playlist(token, playlist_id, vid)
                                    added_total += 1
                                    entry_added_any = True
                                except youtube_oauth_service.QuotaExceeded:
                                    # Release the claim so it retries once quota resets,
                                    # and stop the whole run.
                                    with get_meta_connection() as conn:
                                        conn.execute(
                                            "DELETE FROM youtube_playlist_added"
                                            " WHERE scope=? AND scope_id=? AND keyword=? AND entry_id=? AND video_id=?",
                                            (scope, scope_id, keyword, eid, vid),
                                        )
                                    mark_yt_quota_exhausted()
                                    LOGGER.warning("[yt-playlist-auto] quota exceeded; %d added this run", added_total)
                                    raise
                                except Exception as exc:  # noqa: BLE001
                                    with get_meta_connection() as conn:
                                        conn.execute(
                                            "DELETE FROM youtube_playlist_added"
                                            " WHERE scope=? AND scope_id=? AND keyword=? AND entry_id=? AND video_id=?",
                                            (scope, scope_id, keyword, eid, vid),
                                        )
                                    LOGGER.warning("[yt-playlist-auto] add failed for %s: %s", vid, exc)
                            if entry_added_any:
                                if fu not in feed_title_cache:
                                    try:
                                        feed_title_cache[fu] = str(getattr(reader.get_feed(fu), "title", None) or fu)
                                    except Exception:
                                        feed_title_cache[fu] = fu
                                run_entries.append({
                                    "feed_url": fu, "entry_id": eid,
                                    "title": str(entry.title or ""), "link": link,
                                    "feed_title": feed_title_cache.get(fu, fu),
                                })
                                if mark_read:
                                    reader.mark_entry_as_read((fu, eid))
                                    marked.append((fu, eid))
            except youtube_oauth_service.QuotaExceeded:
                pass  # stop processing further rules this run
            except Exception:
                LOGGER.exception("[yt-playlist-auto] error processing rule %s/%s", scope, keyword)

            if marked:
                when = datetime.now().isoformat()
                with get_meta_connection() as conn:
                    conn.executemany(
                        "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
                        " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
                        [(fu, eid, when) for fu, eid in marked],
                    )
                _unread_counts_generation += 1
            if run_entries:
                with get_meta_connection() as conn:
                    _log_auto_run(conn, now_str, "youtube_playlist", scope, scope_id, keyword, {
                        "count": len(run_entries), "entries": run_entries,
                    })
    except Exception:
        LOGGER.exception("[yt-playlist-auto] error in _run_youtube_playlist_rules_after_refresh")


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


_reader_thread_local = threading.local()

# Cap on distinct users' Reader handles kept open per worker thread. Reader open
# is expensive, so we persist handles; but with many users a thread could
# accumulate one connection per user, so we LRU-evict (and close) the least
# recently used beyond this bound. Generous for the small-tenant target; a
# handful of users never hit it.
_READER_POOL_MAX_PER_THREAD = 8


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


# Per-user cache of browser-UA-flagged feed URLs, consulted by reader's per-feed
# request hook on every fetch. Refreshed on a short TTL (the set changes only when
# a feed is flagged/unflagged) and invalidated immediately on those writes.
_browser_ua_cache: dict[str, set[str]] = {}
_browser_ua_cache_at: dict[str, float] = {}
_browser_ua_cache_lock = threading.Lock()
_BROWSER_UA_CACHE_TTL = 30.0


def _browser_ua_feeds_for(uid: str) -> set[str]:
    now = time.monotonic()
    with _browser_ua_cache_lock:
        cached = _browser_ua_cache.get(uid)
        if cached is not None and (now - _browser_ua_cache_at.get(uid, 0.0)) < _BROWSER_UA_CACHE_TTL:
            return cached
    try:
        with tenancy.user_context(uid):
            with get_meta_connection() as conn:
                feeds = get_browser_ua_feed_urls(conn)
    except Exception:
        feeds = set()
    with _browser_ua_cache_lock:
        _browser_ua_cache[uid] = feeds
        _browser_ua_cache_at[uid] = now
    return feeds


def _invalidate_browser_ua_cache() -> None:
    with _browser_ua_cache_lock:
        _browser_ua_cache.clear()
        _browser_ua_cache_at.clear()


def _flag_browser_ua_on_refusal(feed_url: str) -> bool:
    """Flag a feed for browser-identity fetches after an honest-UA refusal and
    invalidate the cache so an immediate retry escalates. Returns True if newly
    flagged (a feed already flagged returns False, so the caller won't retry-loop)."""
    try:
        with get_meta_connection() as conn:
            newly = flag_browser_ua_feed(conn, feed_url, reason="refused honest UA on refresh")
    except Exception:
        return False
    if newly:
        _invalidate_browser_ua_cache()
        LOGGER.info("[refresh] flagged %s for browser-identity fetches", feed_url)
    return newly


def get_reader():
    """Per-(thread, user) persistent Reader, resolved via the tenancy seam.

    The current user comes from :func:`tenancy.current_user_id` (defaults to the
    single legacy user). Each worker thread keeps an LRU pool of Reader handles
    keyed by user_id so cross-user requests on the same thread don't reopen, and
    so one user's handle is never shared across threads (SQLite affinity)."""
    uid = tenancy.current_user_id()
    pool = getattr(_reader_thread_local, "pool", None)
    if pool is None:
        pool = collections.OrderedDict()
        _reader_thread_local.pool = pool

    proxy = pool.get(uid)
    if proxy is not None:
        pool.move_to_end(uid)  # mark most-recently-used
        return proxy

    proxy = _PersistentReaderProxy(
        ReaderApi(
            tenancy.reader_db_path(uid),
            browser_ua_provider=lambda u=uid: _browser_ua_feeds_for(u),
        ).client()
    )
    pool[uid] = proxy
    # Evict + close the least-recently-used handles beyond the per-thread cap.
    # Safe because the pool is thread-local and a thread serves one request at a
    # time, so an evicted handle is never mid-use on another thread.
    while len(pool) > _READER_POOL_MAX_PER_THREAD:
        _evicted_uid, evicted = pool.popitem(last=False)
        try:
            evicted._reader.close()
        except Exception:
            LOGGER.debug("evicted reader close failed", exc_info=True)
    return proxy


youtube_duration_service = YouTubeDurationService(
    get_durations_connection=get_yt_duration_connection,
    get_reader=get_reader,
    user_agent=READABILITY_USER_AGENT,
    # Per-user API key (with env fallback) so each user's key drives durations.
    api_key_provider=lambda: get_yt_api_key(),
    quota_sink=record_yt_quota_spend,
)

# Wire the quota-spend sink into the stateless YT modules so each billed API call
# (playlist list/insert, sub-sync channels/subscriptions) tallies against the
# current user's daily quota meter.
youtube_oauth_service.set_quota_sink(record_yt_quota_spend)
import services.youtube_sync as _youtube_sync_mod
_youtube_sync_mod.set_quota_sink(record_yt_quota_spend)

# Quire calls feed the per-user sliding-window rate meter (per-minute/hour).
quire_service.set_usage_sink(record_quire_call)

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
    # Lazy: _background_user_ids is defined later in this module; the worker
    # only calls this at runtime, so the name resolves by then.
    background_user_ids=lambda: _background_user_ids(),
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
    invalidate_tag_counts_cache()
    return next_tags


def delete_manual_tag_everywhere(tag: str | None) -> int:
    """Strip a manual tag from every entry that carries it. Returns the number
    of entries the tag was removed from. Used by the tag-management delete
    action so a tag leaves the sidebar once nothing references it."""
    normalized = normalize_tag_value(tag)
    if not normalized:
        return 0

    key = f"{MANUAL_TAG_KEY_PREFIX}{normalized}"
    removed = 0
    failed = 0
    with get_reader() as reader:
        # reader filters entries by tag key in SQL, so this stays a single
        # pass across the whole library rather than a per-entry scan.
        for entry in list(reader.get_entries(tags=[key])):
            try:
                reader.delete_tag(entry.resource_id, key)
                removed += 1
            except Exception:
                # Keep going so one bad entry doesn't abort the whole cleanup,
                # but log it so the failure is diagnosable.
                failed += 1
                LOGGER.warning(
                    "delete_manual_tag_everywhere: failed to remove %r from %s",
                    normalized, entry.resource_id, exc_info=True,
                )
    if failed:
        LOGGER.warning(
            "delete_manual_tag_everywhere: %r removed from %d entries, %d failed",
            normalized, removed, failed,
        )

    if removed:
        invalidate_has_manual_tags_cache()
        invalidate_tag_counts_cache()
    return removed


def rename_manual_tag_everywhere(old_tag: str | None, new_tag: str | None) -> tuple[int, bool]:
    """Rename a manual tag across every entry that carries it.

    Returns ``(count, merged)`` where *count* is the number of entries
    updated and *merged* is True if *new_tag* already had entries before
    the rename (i.e. the two tags were combined).
    """
    old_norm = normalize_tag_value(old_tag)
    new_norm = normalize_tag_value(new_tag)
    if not old_norm or not new_norm or old_norm == new_norm:
        return 0, False

    old_key = f"{MANUAL_TAG_KEY_PREFIX}{old_norm}"
    new_key = f"{MANUAL_TAG_KEY_PREFIX}{new_norm}"
    updated = 0
    with get_reader() as reader:
        merged = reader.get_entry_counts(tags=[new_key]).total > 0
        for entry in list(reader.get_entries(tags=[old_key])):
            try:
                reader.set_tag(entry.resource_id, new_key)
                reader.delete_tag(entry.resource_id, old_key)
                updated += 1
            except Exception:
                LOGGER.warning(
                    "rename_manual_tag_everywhere: failed on %s", entry.resource_id, exc_info=True
                )
    if updated:
        invalidate_has_manual_tags_cache()
        invalidate_tag_counts_cache()
    return updated, merged


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
                    with httpx.Client(follow_redirects=False, timeout=8.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
                        response = url_guard.safe_get(client, url, headers={"User-Agent": READABILITY_USER_AGENT})
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


_has_manual_tags_cache = _PerUserDict()
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
        conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=2.0)
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


def invalidate_tag_counts_cache() -> None:
    with tag_counts_cache_lock:
        tag_counts_cache.clear()


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
        conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5.0)
        try:
            # COUNT(DISTINCT id), not COUNT(*): the same article syndicated
            # across two feeds (same entry id under two feed URLs) is one row per
            # feed in entry_tags, but the post list collapses it to a single item
            # via build_entry_dedupe_key. Counting distinct ids keeps the sidebar
            # tally consistent with what clicking the tag actually shows.
            rows = conn.execute(
                f"SELECT key, COUNT(DISTINCT id) FROM entry_tags"
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
    return f"/api/favicon?domain={quote_plus(host)}"


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


def get_feed_fetch_history(conn, feed_url: str, limit: int = 30) -> list[dict]:
    """Recent per-refresh fetch attempts for a feed, newest first, for the
    Feed Properties → History tab."""
    rows = conn.execute(
        "SELECT fetched_at, status, http_status, new_entries, duration_ms, error"
        " FROM feed_fetch_history WHERE feed_url = ? ORDER BY fetched_at DESC LIMIT ?",
        (feed_url, limit),
    ).fetchall()
    history: list[dict] = []
    for r in rows:
        raw_at = r["fetched_at"]
        try:
            fetched_at = format_datetime_for_ui(datetime.fromtimestamp(float(raw_at), tz=timezone.utc)) if raw_at else None
        except Exception:
            fetched_at = None
        history.append({
            "fetched_at": fetched_at,
            "status": str(r["status"] or ""),
            "http_status": r["http_status"],
            "new_entries": r["new_entries"],
            "duration_ms": r["duration_ms"],
            "error": r["error"],
        })
    return history
_AUTOMATION_TYPE_LABELS = {
    "highlight": "Highlight",
    "mark_as_read": "Auto mark read",
    "deduplicate": "Deduplicate",
    "email_article": "Email article",
    "webhook": "Webhook",
    "youtube_playlist": "Add to YT playlist",
    "instapaper": "Save to Instapaper",
    "quire": "Add to Quire",
}
_AUTOMATION_SCOPE_LABELS = {"global": "All feeds", "folder": "Folder", "feed": "This feed", "feeds": "Selected feeds"}


def _automation_rule_detail(rule: dict) -> str:
    """A short human description of what a rule does, per rule type."""
    rule_type = str(rule.get("type", ""))
    search_in = str(rule.get("search_in") or "title")
    if rule_type == "highlight":
        return f"{rule.get('color') or 'yellow'} · matches {search_in}"
    if rule_type == "mark_as_read":
        return f"matches {search_in}"
    if rule_type == "deduplicate":
        method = str(rule.get("keyword") or "slug")
        window = int(rule.get("dedup_window_hours") or 24)
        return f"{method} match · {window}h window"
    if rule_type == "email_article":
        to = str(rule.get("email_to") or "").strip()
        return f"to {to}" if to else "email"
    if rule_type == "webhook":
        fmt = str(rule.get("webhook_format") or "generic")
        return f"POST · {fmt} · matches {search_in}"
    if rule_type == "instapaper":
        return f"save · matches {search_in}"
    if rule_type == "youtube_playlist":
        parts = ["→ " + (str(rule.get("yt_playlist_title") or "") or "playlist")]
        kw = str(rule.get("keyword") or "").strip()
        parts.append(f"keyword “{kw}”" if kw else "all videos")
        if rule.get("yt_include_shorts"):
            parts.append("incl. Shorts")
        lo = int(rule.get("yt_min_minutes") or 0)
        hi = int(rule.get("yt_max_minutes") or 0)
        if lo or hi:
            parts.append(f"{lo}–{hi}m" if hi else f"≥{lo}m")
        if rule.get("yt_mark_read"):
            parts.append("mark read")
        return " · ".join(parts)
    return ""


def collect_feed_automations(conn, feed_url: str, folder_ids: list[int]) -> dict:
    """Automations that act on this feed: configured rules in scope + recent runs.

    Rules come from the highlight_keywords table (which holds every rule type, not
    just highlights); a rule applies to this feed when its scope is global, this
    feed directly, or a folder the feed belongs to. Recent runs come from
    rule_run_log_entries, the per-entry record of what automation actually fired
    on this feed's entries — so the user can see what Lectio is doing without
    reading code."""
    folder_id_set = {int(f) for f in folder_ids}
    rules: list[dict] = []
    for rule in get_highlight_keywords(conn):
        scope = str(rule.get("scope", ""))
        scope_id = str(rule.get("scope_id") or "")
        if scope == "global":
            applies = True
        elif scope == "feed":
            applies = scope_id == feed_url
        elif scope == "feeds":
            applies = feed_url in parse_feeds_scope_id(scope_id)
        elif scope == "folder":
            applies = scope_id.isdigit() and int(scope_id) in folder_id_set
        else:
            applies = False
        if not applies:
            continue
        rule_type = str(rule.get("type", ""))
        rules.append({
            "type": rule_type,
            "type_label": _AUTOMATION_TYPE_LABELS.get(rule_type, rule_type or "Rule"),
            "scope_label": _AUTOMATION_SCOPE_LABELS.get(scope, scope),
            "keyword": str(rule.get("keyword") or ""),
            "enabled": bool(rule.get("enabled")),
            "detail": _automation_rule_detail(rule),
        })

    recent_runs: list[dict] = []
    run_rows = conn.execute(
        "SELECT l.run_at AS run_at, l.rule_type AS rule_type, l.keyword AS keyword,"
        " l.trigger AS trigger, COUNT(e.entry_id) AS affected"
        " FROM rule_run_log l JOIN rule_run_log_entries e ON e.log_id = l.id"
        " WHERE e.feed_url = ?"
        " GROUP BY l.id ORDER BY l.run_at DESC LIMIT 15",
        (feed_url,),
    ).fetchall()
    for r in run_rows:
        run_at_raw = r["run_at"]
        try:
            run_at = format_datetime_for_ui(datetime.fromisoformat(str(run_at_raw)))
        except Exception:
            run_at = str(run_at_raw or "")
        rule_type = str(r["rule_type"] or "")
        recent_runs.append({
            "run_at": run_at,
            "type_label": _AUTOMATION_TYPE_LABELS.get(rule_type, rule_type or "Rule"),
            "keyword": str(r["keyword"] or ""),
            "trigger": str(r["trigger"] or ""),
            "affected": int(r["affected"] or 0),
        })

    return {"rules": rules, "recent_runs": recent_runs}


def get_feed_properties(feed_url: str) -> dict:
    with get_reader() as reader:
        feed_obj = reader.get_feed(feed_url, None)

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
        for entry in reader.get_entries(feed=feed_url):
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

        # If the reader reports no active error but our failure counter is still
        # non-zero, surface a warning so the user isn't confused by "No known errors"
        # next to a failure count.  This happens when the feed last fetched
        # successfully (clearing last_exception) but the failure counter hasn't been
        # reset yet (e.g. the feed was in backoff when the last success occurred).
        if health == "ok" and _feed_failures > 0:
            health = "warning"
            _nf = _feed_failures
            health_detail = (
                f"Last update succeeded, but {_nf} consecutive failure"
                f"{'s' if _nf != 1 else ''} still on record. "
                "Will auto-clear on next successful refresh."
            )

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
            "inject_source_images": bool(_disp.get("inject_source_images", 0)),
            "feed_thumbnail_url": _disp.get("feed_thumbnail_url") or None,
            "thumb_crop": str(_disp.get("thumb_crop") or "cover"),
            "thumb_strategy": _disp.get("thumb_strategy") or None,
            "smart_min_scale": float(_disp["smart_min_scale"]) if _disp.get("smart_min_scale") is not None else None,
            "fill_zoom": float(_disp["fill_zoom"]) if _disp.get("fill_zoom") is not None else None,
            "is_youtube_feed": "youtube.com/feeds/videos.xml" in feed_url,
            "browser_ua": feed_url in get_browser_ua_feed_urls(_pc),
            "strategy_cache": _strat_cache,
            "folder_ids": [int(r["folder_id"]) for r in _folder_id_rows],
            "fetch_history": get_feed_fetch_history(_pc, feed_url),
            "automations": collect_feed_automations(
                _pc, feed_url, [int(r["folder_id"]) for r in _folder_id_rows]
            ),
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
            SELECT f.id, f.name, f.cadence_minutes,
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
            "cadence_minutes": folder_row["cadence_minutes"],
            "feed_count": 0,
            "total_articles": 0,
            "unread_articles": 0,
            "top_feeds": [],
        }

    total_articles = 0
    unread_articles = 0
    feed_stats: dict[str, dict] = {}

    with get_reader() as reader:
        for url in feed_urls:
            for entry in reader.get_entries(feed=url):
                total_articles += 1
                if not entry.read:
                    unread_articles += 1
                fs = feed_stats.setdefault(url, {
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

        for url in list(feed_stats):
            feed_obj = reader.get_feed(url, None)
            if feed_obj:
                feed_stats[url]["title"] = (
                    getattr(feed_obj, "resolved_title", None)
                    or getattr(feed_obj, "title", None)
                    or url
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
        "cadence_minutes": folder_row["cadence_minutes"],
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


def build_sort_query(sort_by: str | None, sort_dir: str | None) -> str:
    """Return URL fragment for sort params, omitting defaults (post/asc)."""
    sb = normalize_sort_by(sort_by)
    sd = normalize_sort_dir(sort_dir)
    parts = []
    if sb != DEFAULT_SORT_BY:
        parts.append(f"&sort_by={quote_plus(sb)}")
    if sd != DEFAULT_SORT_DIR:
        parts.append(f"&sort_dir={quote_plus(sd)}")
    return "".join(parts)


def build_read_filter_query(read_filter: str | None) -> str:
    """Return URL fragment for read_filter, omitting default 'all'."""
    rf = normalize_read_filter(read_filter)
    return f"&read_filter={quote_plus(rf)}" if rf != "all" else ""


def build_resume_read_filter_query(read_filter: str | None, *, active_read_filter: str | None = None) -> str:
    """Return URL fragment for resume_read_filter, omitting when same as active filter."""
    rrf = normalize_resume_read_filter(read_filter)
    active = normalize_read_filter(active_read_filter) if active_read_filter is not None else "all"
    return f"&resume_read_filter={quote_plus(rrf)}" if rrf != active else ""


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

# Webcomic navigation button detection — alt text and src filename patterns.
_COMIC_NAV_ALT_RE = re.compile(
    r'\balt=["\'](?:prev(?:ious)?|next(?:\s+page)?|first|last|back|forward|newer|older|beginning)["\']',
    re.IGNORECASE,
)
_COMIC_NAV_SRC_RE = re.compile(
    r'src=["\'][^"\']*[/_-](?:prev(?:ious)?|next|first|last|nav|back)[^"\']*\.(?:png|gif|jpe?g|svg)["\']',
    re.IGNORECASE,
)


def _strip_comic_nav_images(content: str) -> str:
    """Strip <a href="..."><img alt="prev/next/..."></a> navigation button combos from webcomic feeds."""
    def _check_nav(m: re.Match) -> str:
        tag = m.group(0)
        if _COMIC_NAV_ALT_RE.search(tag) or _COMIC_NAV_SRC_RE.search(tag):
            return ""
        return tag
    result = re.sub(
        r'<a\b[^>]*>(?:\s*<img\b[^>]*/?\s*>\s*)+</a>',
        _check_nav,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Clean up empty <p> blocks left behind
    result = re.sub(r'<p\b[^>]*>\s*</p>', "", result, flags=re.IGNORECASE)
    return result


def _strip_qwantz_nav(content: str) -> str:
    """Strip site-nav table and prev/next row from Dinosaur Comics (qwantz.com) entries."""
    # Remove the site-nav table that appears before the comic image
    content = re.sub(
        r'<table\b[^>]*>.*?</table>\s*(?=<img\b)',
        "",
        content,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Remove the table row containing the prev/next navigation links (rel="prev" anchor)
    content = re.sub(
        r'<tr\b[^>]*>.*?rel=["\']prev["\'].*?</tr>',
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return content


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


# WordPress "rss_footer" boilerplate appended to feed content: "The post <title>
# appeared first on <site>." Plugins add near-duplicate variants ("first appeared
# on") and some double-encode the wrapping <p> (so the reader shows literal
# "<p>...</p>" text). Match the paragraph text tolerantly of leading/trailing
# literal p-tags, anchored on "The post … appeared … on …".
_WP_POST_FOOTER_RE = re.compile(
    r'^\s*(?:</?p>\s*)*The post\b.*?\b(?:first appeared|appeared first)\s+on\b.*?\.\s*(?:</?p>\s*)*$',
    re.IGNORECASE | re.DOTALL,
)


def _fix_wp_post_footer(content_html: str) -> str:
    """Tidy the trailing WordPress "The post … appeared … on …" footer.

    The footer itself is fine to keep, but feeds mangle it two ways: plugins emit
    it twice ("first appeared on" + "appeared first on"), and some double-encode
    the wrapping <p> so it renders as literal "<p>…</p>" text. Keep a single
    footer paragraph, drop the duplicates, and strip the literal tag artifacts
    from the one kept. Only trailing <p> blocks are touched (stop at the first
    non-footer paragraph), so real content is never affected."""
    if "appeared" not in content_html.lower():
        return content_html  # cheap guard
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content_html, "html.parser")
    paras = soup.find_all("p")
    footer_ps = []
    for p in reversed(paras):
        if p.parent is None:
            continue
        if _WP_POST_FOOTER_RE.match(p.get_text(" ", strip=True)):
            footer_ps.append(p)
        else:
            break  # contiguous trailing footers only
    if not footer_ps:
        return content_html
    footer_ps.reverse()  # document order
    keep = footer_ps[0]
    for dup in footer_ps[1:]:
        dup.decompose()
    # Remove literal "<p>"/"</p>" text artifacts (double-encoded tags) from the
    # kept footer, leaving the sentence intact.
    from bs4 import NavigableString as _NS
    for s in list(keep.strings):
        if not isinstance(s, _NS):
            continue
        cleaned = re.sub(r"</?p\s*>", "", str(s), flags=re.IGNORECASE)
        if cleaned != str(s):
            s.replace_with(cleaned)
    return str(soup)


def _clean_qwantz_content(content_html: str) -> str:
    """Strip Dinosaur Comics (qwantz.com) nav chrome, keep comic + commentary.

    The feed wraps the comic in ``<center>`` with a nav table above the image
    (archive / contact / merch / search / about) and another below it whose first
    row is prev/date/next nav and whose second row is the dated author
    commentary. Rebuild the body as just the comic ``<img>`` (its ``title`` holds
    the secret hover text, kept for the caption) followed by that commentary, so
    the duplicated nav and date links don't clutter the reading view."""
    if "qwantz.com" not in content_html and 'class="comic"' not in content_html:
        return content_html
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content_html, "html.parser")
    img = soup.find("img", class_="comic") or soup.find("img")
    if img is None:
        return content_html  # not the shape we expect; leave untouched
    # The commentary lives in the only wide (colspan=3) cell with real text.
    commentary_html = ""
    for td in soup.find_all("td"):
        if str(td.get("colspan")) == "3" and td.get_text(strip=True):
            commentary_html = "".join(str(c) for c in td.contents)
            break
    return str(img) + commentary_html


_AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")


def _url_has_audio_ext(url: str) -> bool:
    """True if the URL's path ends in a known audio extension.

    Matches on the path only, so a tracking/auth query string (e.g.
    ``…/ep1.mp3?token=abc``) — common on podcast CDNs — doesn't defeat the
    extension check the way a naive endswith on the whole URL would.
    """
    if not url:
        return False
    # Trim surrounding whitespace some feeds leave around URLs, which would
    # otherwise land in the parsed path and defeat the extension check.
    return urlparse(url.strip()).path.lower().endswith(_AUDIO_EXTS)


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg")


def _url_has_image_ext(url: str) -> bool:
    """True if the URL's path ends in a known image extension (query string
    tolerated, like _url_has_audio_ext)."""
    if not url:
        return False
    return urlparse(url.strip()).path.lower().endswith(_IMAGE_EXTS)


_BUZZSPROUT_ENCLOSURE_RE = re.compile(
    r"^(https://(?:www\.)?buzzsprout\.com/\d+/episodes/[^?#]+)\.mp3", re.IGNORECASE
)


def _derived_entry_link(entry) -> str | None:
    """Best-effort page URL for entries whose feed ships no ``<link>``.

    Buzzsprout podcast feeds carry only a guid + audio enclosure; the episode page
    is the enclosure URL without the ``.mp3`` extension, so the post title can link
    somewhere instead of being inert."""
    if getattr(entry, "link", None):
        return None
    for enc in (getattr(entry, "enclosures", None) or []):
        url = (getattr(enc, "href", None) or getattr(enc, "url", None) or "").strip()
        m = _BUZZSPROUT_ENCLOSURE_RE.match(url)
        if m:
            return m.group(1)
    return None


def _find_entry_audio_url(entry) -> str | None:
    """Return a usable audio URL for an entry, or None.

    Checks, in order: audio enclosures (by MIME type, or an audio extension on
    the URL path so query strings and untyped/oddly-typed enclosures still
    match), then the entry link when it points straight at an audio file (some
    podcast feeds carry no enclosure and set the link to the media URL).

    Note: audio that lives only in ``<media:content>`` is not recoverable here —
    the reader library keeps standard ``<enclosure>`` elements but drops
    media:content, so it never reaches this entry object.
    """
    for enc in (getattr(entry, "enclosures", None) or []):
        enc_url = getattr(enc, "href", None) or getattr(enc, "url", None) or ""
        enc_type = (getattr(enc, "type", None) or "").lower()
        if enc_url and (enc_type.startswith("audio/") or _url_has_audio_ext(enc_url)):
            return enc_url
    link = getattr(entry, "link", None) or ""
    if _url_has_audio_ext(link):
        return link
    return None


def _format_enclosure_size(length) -> str:
    """Render an enclosure ``length`` (bytes) as a short human label, or ''.

    Feeds often set a placeholder length (e.g. ``1024``) when the real size is
    unknown, so anything under ~2 KB is treated as meaningless and dropped.
    """
    try:
        size = int(length)
    except (TypeError, ValueError):
        return ""
    if size < 2048:
        return ""
    for unit in ("KB", "MB", "GB"):
        size /= 1024.0
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return ""


def _enclosure_label(enc_url: str, enc_type: str) -> str:
    """Friendly label for a download link: the file name, else the MIME type."""
    name = unquote(urlparse(enc_url).path.rsplit("/", 1)[-1]).strip()
    if name:
        return name
    return enc_type or "attachment"


def _render_entry_attachments(entry, audio_url: str | None) -> str:
    """Render a footer "Attachments" section for non-audio enclosures.

    Magazine/document feeds (e.g. Full Circle) attach the issue PDF/EPUB as
    ``<enclosure>`` elements that never appear in the article body. The audio
    enclosure (if any) is already surfaced as a player, so it's skipped here to
    avoid a duplicate link.
    """
    seen: set[str] = set()
    items: list[str] = []
    for enc in (getattr(entry, "enclosures", None) or []):
        enc_url = (getattr(enc, "href", None) or getattr(enc, "url", None) or "").strip()
        if not enc_url or enc_url in seen:
            continue
        enc_type = (getattr(enc, "type", None) or "").lower()
        if enc_type.startswith("audio/") or _url_has_audio_ext(enc_url):
            continue  # surfaced as the audio player
        # Image enclosures are the post's lead/inline image (e.g. gottadeal's deal
        # photo) — surfaced as the lead image, not a download link. Listing them
        # here also poisoned the lead-image dedup (the URL appearing in the
        # attachments markup made the lead look "already in content", nulling it).
        if enc_type.startswith("image/") or _url_has_image_ext(enc_url):
            continue
        if audio_url and enc_url == audio_url:
            continue
        seen.add(enc_url)
        label = html.escape(_enclosure_label(enc_url, enc_type))
        size = _format_enclosure_size(getattr(enc, "length", None))
        meta = f" <span style=\"color:var(--muted,#888);\">({size})</span>" if size else ""
        safe_url = html.escape(enc_url, quote=True)
        items.append(
            f'<li><a href="{safe_url}" target="_blank" rel="noopener noreferrer" download>'
            f'{label}</a>{meta}</li>'
        )
    if not items:
        return ""
    return (
        '<div class="entry-attachments" style="margin:1.5em 0 0.5em; '
        'padding-top:0.75em; border-top:1px solid var(--border,#ddd);">'
        '<div style="font-weight:600; margin-bottom:0.35em;">Attachments</div>'
        f'<ul style="margin:0; padding-left:1.25em;">{"".join(items)}</ul>'
        '</div>'
    )


# media:content scan cadence: rescan podcast feeds (audio found) often enough to
# catch new episodes; barely ever rescan feeds that had none.
_MEDIA_SCAN_TTL_FOUND = 6 * 3600
_MEDIA_SCAN_TTL_EMPTY = 7 * 24 * 3600
# A scan that errored mid-way (e.g. discovered a host feed but couldn't fetch it)
# isn't proof there's no audio — retry well before the long "empty" backoff.
_MEDIA_SCAN_TTL_ERROR = 6 * 3600
_media_scan_in_progress: set[tuple[str, str]] = set()
_media_scan_lock = threading.Lock()


def _lookup_media_audio(conn: sqlite3.Connection, feed_url: str, entry_id: str) -> str | None:
    row = conn.execute(
        "SELECT audio_url FROM entry_media_audio WHERE feed_url = ? AND entry_id = ?",
        (feed_url, entry_id),
    ).fetchone()
    return (row[0] or None) if row else None


def _lookup_media_video(conn: sqlite3.Connection, feed_url: str, entry_id: str) -> list[str] | None:
    """Recovered YouTube ids for an entry: ``None`` = not scanned yet, ``[]`` =
    scanned but nothing recoverable, otherwise the ids in document order."""
    row = conn.execute(
        "SELECT video_ids FROM entry_media_video WHERE feed_url = ? AND entry_id = ?",
        (feed_url, entry_id),
    ).fetchone()
    if row is None:
        return None
    return list(str(row[0] or "").split())


def _media_scan_due(conn: sqlite3.Connection, feed_url: str) -> bool:
    """True if this feed has never been scanned for media:content audio, or its
    cadence TTL has elapsed."""
    row = conn.execute(
        "SELECT scanned_at, found, ok FROM feed_media_scan WHERE feed_url = ?", (feed_url,)
    ).fetchone()
    if not row:
        return True
    if row[1]:
        ttl = _MEDIA_SCAN_TTL_FOUND
    elif not row[2]:  # errored mid-scan — not proof of "no audio"
        ttl = _MEDIA_SCAN_TTL_ERROR
    else:
        ttl = _MEDIA_SCAN_TTL_EMPTY
    return (time.time() - float(row[0])) > ttl


def _scan_feed_media_audio(feed_url: str) -> None:
    """Fetch the raw feed (SSRF-guarded) and persist any media:content audio URLs.

    Runs in a background thread; the caller captures and re-binds tenancy. Best
    effort — a fetch/parse failure still records the scan attempt so we back off."""
    found_map: dict[str, str] = {}
    raw_feed: bytes | None = None
    try:
        with httpx.Client(follow_redirects=False, timeout=8.0,
                          headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            resp = url_guard.safe_get(client, feed_url)
        if resp.status_code == 200 and resp.content:
            raw_feed = resp.content
            found_map = podcast_audio.extract_media_audio(raw_feed)
    except Exception:
        LOGGER.debug("media:content scan failed for %s", feed_url, exc_info=True)

    # The same raw feed also carries any YouTube embeds that feedparser/reader
    # stripped from the stored content (the <iframe>, and thus the video id, is
    # gone). Recover them here so the pane can rebuild the player.
    video_map: dict[str, list[str]] = {}
    if raw_feed is not None:
        try:
            video_map = youtube_embeds.extract_youtube_embeds(raw_feed)
        except Exception:
            LOGGER.debug("youtube-embed scan failed for %s", feed_url, exc_info=True)

    # When the feed carries no audio of its own, the audio often lives in a
    # separate podcast-host feed (Libsyn/Buzzsprout/…) referenced on the episode
    # page. Detect it so we can (a) borrow its audio into this feed's entries and
    # (b) suggest subscribing to the audio feed if borrowing didn't cover an entry.
    suggested = ""
    errored = False
    if not found_map:
        suggested = _discover_suggested_audio_feed(feed_url)
        if suggested:
            borrowed = _borrow_audio_from_feed(feed_url, suggested)
            if borrowed is None:
                # We found a host feed but couldn't fetch it (network/HTTP error,
                # e.g. a Cloudflare 403) — don't bank this as a settled "no audio".
                errored = True
            else:
                found_map = borrowed

    # ``ok`` is False only when we have positive reason to believe the scan was
    # incomplete; a clean scan that simply found nothing is ok=1 (long backoff).
    ok = 0 if (errored and not found_map) else 1
    with get_meta_connection() as conn:
        for entry_id, audio_url in found_map.items():
            conn.execute(
                "INSERT INTO entry_media_audio (feed_url, entry_id, audio_url) VALUES (?, ?, ?)"
                " ON CONFLICT(feed_url, entry_id) DO UPDATE SET audio_url = excluded.audio_url",
                (feed_url, entry_id, audio_url),
            )
        for entry_id, vids in video_map.items():
            conn.execute(
                "INSERT INTO entry_media_video (feed_url, entry_id, video_ids) VALUES (?, ?, ?)"
                " ON CONFLICT(feed_url, entry_id) DO UPDATE SET video_ids = excluded.video_ids",
                (feed_url, entry_id, " ".join(vids)),
            )
        conn.execute(
            "INSERT INTO feed_media_scan (feed_url, scanned_at, found, suggested_audio_feed, ok)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(feed_url) DO UPDATE SET scanned_at = excluded.scanned_at,"
            " found = excluded.found, suggested_audio_feed = excluded.suggested_audio_feed,"
            " ok = excluded.ok",
            (feed_url, time.time(), 1 if found_map else 0, suggested, ok),
        )
        conn.commit()


def _polite_safe_get(url: str, *, timeout: float):
    """SSRF-safe GET that identifies honestly as Lectio, escalating to a browser
    UA only if the honest request is actually refused (HTTP 403).

    Honest-by-default keeps us a good citizen for hosts that reward it (and never
    spoofs a host that's happy to serve Lectio); the browser-UA fallback is a
    last resort for Cloudflare-gated podcast hosts that blanket-403 non-browsers.
    Returns the final response (caller checks status)."""
    resp = None
    for ua in (LECTIO_HONEST_USER_AGENT, PODCAST_FETCH_USER_AGENT):
        with httpx.Client(follow_redirects=False, timeout=timeout,
                          headers={"User-Agent": ua}) as client:
            resp = url_guard.safe_get(client, url)
        if resp.status_code != 403:
            break  # served (or a non-403 error) — don't escalate
    return resp


def _discover_suggested_audio_feed(feed_url: str) -> str:
    """Fetch a recent entry's page and look for a podcast-host audio feed.

    Returns the discovered feed URL (empty string if none). Best effort; the feed
    itself isn't re-fetched — we use a recent entry's link page, where the host
    player embed / feed URL lives."""
    try:
        with get_reader() as reader:
            link = ""
            for entry in reader.get_entries(feed=feed_url, sort="recent"):
                link = (entry.link or "").strip()
                if link:
                    break
        if not link or not url_guard.is_safe_outbound_url(link):
            return ""
        resp = _polite_safe_get(link, timeout=8.0)
        if resp.status_code != 200 or not resp.text:
            return ""
        found = podcast_feed_discovery.find_podcast_host_feed(resp.text) or ""
        # Don't suggest the feed we're already looking at.
        if found and found.rstrip("/") == feed_url.rstrip("/"):
            return ""
        return found
    except Exception:
        LOGGER.debug("audio-feed discovery failed for %s", feed_url, exc_info=True)
        return ""


def _borrow_audio_from_feed(feed_url: str, host_feed_url: str) -> dict[str, str] | None:
    """Match this feed's entries to audio in a podcast-host feed and return
    entry_id -> audio URL for the matches.

    Lets a notes-only website feed gain a player without a second subscription:
    the audio is sourced from the matching episode (by title, then episode
    number) in the linked podcast feed.

    Returns ``None`` when the host feed couldn't be fetched (network/HTTP error)
    — distinct from an empty dict, which means it was fetched fine but nothing
    matched. The caller uses that to retry a failed fetch soon rather than
    backing it off as a settled "no audio" result."""
    try:
        with get_reader() as reader:
            titles = {
                str(e.id): (e.title or "")
                for e in reader.get_entries(feed=feed_url, sort="recent", limit=120)
            }
        if not titles or not url_guard.is_safe_outbound_url(host_feed_url):
            return {}
        resp = _polite_safe_get(host_feed_url, timeout=10.0)
        if resp.status_code != 200 or not resp.content:
            return None
        return podcast_feed_discovery.match_episode_audio(resp.content, titles)
    except Exception:
        LOGGER.debug("audio borrow failed for %s from %s", feed_url, host_feed_url, exc_info=True)
        return None


def _get_suggested_audio_feed(conn: sqlite3.Connection, feed_url: str) -> str | None:
    row = conn.execute(
        "SELECT suggested_audio_feed FROM feed_media_scan WHERE feed_url = ?", (feed_url,)
    ).fetchone()
    return (row[0] or None) if row else None


def _is_feed_subscribed(conn: sqlite3.Connection, feed_url: str) -> bool:
    """True if the URL (slash-insensitive) is in any folder."""
    fu = feed_url.rstrip("/")
    row = conn.execute(
        "SELECT 1 FROM folder_feeds WHERE feed_url = ? OR feed_url = ? LIMIT 1",
        (fu, fu + "/"),
    ).fetchone()
    return bool(row)


def _queue_media_audio_scan(feed_url: str) -> None:
    """Background-scan a feed for media:content audio (deduped, tenancy-bound)."""
    uid = tenancy.current_user_id()
    key = (uid, feed_url)
    with _media_scan_lock:
        if key in _media_scan_in_progress:
            return
        _media_scan_in_progress.add(key)

    def _bg() -> None:
        try:
            with tenancy.user_context(uid):
                _scan_feed_media_audio(feed_url)
        finally:
            with _media_scan_lock:
                _media_scan_in_progress.discard(key)

    threading.Thread(target=_bg, daemon=True).start()


def _resolve_entry_audio_url(
    conn: sqlite3.Connection, feed_url: str, entry_id: str, entry,
) -> str | None:
    """Standard enclosure/link audio detection, with a media:content fallback.

    The fallback is cached per entry; on a cache miss for a feed that's due a
    scan, a background re-parse is enqueued (so the article open never blocks)
    and the player fills in on a later open."""
    direct = _find_entry_audio_url(entry)
    if direct:
        return direct
    cached = _lookup_media_audio(conn, feed_url, entry_id)
    if cached:
        return cached
    if _media_scan_due(conn, feed_url):
        _queue_media_audio_scan(feed_url)
    return None


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


# HTML sanitization lives in services.html_sanitize — the single allowlist
# chokepoint shared by feed ingest (services.reader_sanitize) and the
# readability/source-HTML paths below. It keeps safe embeds (iframes from a
# curated host allowlist, sanitized SVG/MathML) instead of feedparser's
# destroy-everything behavior.
def _sanitize_html_allowlist(content: str) -> str:
    return html_sanitize.sanitize_html(content)


def sanitize_readability_html(content: str) -> str:
    """Sanitize Readability-extracted article HTML (rendered with `| safe`)."""
    return html_sanitize.sanitize_html(content)


def sanitize_source_html(content: str) -> str:
    """Sanitize proxied source-page HTML (rendered with `| safe`)."""
    return html_sanitize.sanitize_html(content)


def _reinject_readability_embeds(summary_html: str, raw_html: str) -> str:
    """Recover allowlisted <iframe> embeds that Readability stripped.

    `python-readability`'s `.summary()` drops *every* `<iframe>` during
    extraction, so YouTube/Spotify/Bandcamp players vanish from Reader view.
    Pull the allowlisted embeds out of the raw page and append any the extracted
    article is missing. The combined HTML is sanitized by the caller, so the
    re-injected iframes still pass through `_sanitize_iframe` (sandbox/referrer).
    """
    if "<iframe" not in raw_html.lower():
        return summary_html
    try:
        from bs4 import BeautifulSoup
        raw_soup = BeautifulSoup(raw_html, "html.parser")
    except Exception:
        return summary_html

    existing = summary_html.lower()
    seen: set[str] = set()
    missing: list[str] = []
    for ifr in raw_soup.find_all("iframe"):
        src = str(ifr.get("src") or ifr.get("data-src") or "").strip()
        if not src or not html_sanitize._embed_host_allowed(src):
            continue
        key = src.split("?", 1)[0].lower()
        if key in seen or key in existing:
            continue
        seen.add(key)
        missing.append(str(ifr))
    if not missing:
        return summary_html
    block = "".join(f'<p class="lectio-embed">{m}</p>' for m in missing)
    return summary_html + block


_READABILITY_IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_READABILITY_IMG_SRC_RE = re.compile(r'\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _dedupe_readability_images(article_html: str) -> str:
    """Drop repeated <img> tags that share an src (readability sometimes keeps
    the lead image twice — e.g. og:image plus the in-body copy)."""
    seen: set[str] = set()

    def _strip(match: re.Match[str]) -> str:
        src_match = _READABILITY_IMG_SRC_RE.search(match.group(0))
        if not src_match:
            return match.group(0)
        src = src_match.group(1).split("?", 1)[0].strip().lower()
        if src in seen:
            return ""
        seen.add(src)
        return match.group(0)

    return _READABILITY_IMG_TAG_RE.sub(_strip, article_html)


def _absolutize_article_urls(article_html: str, base_url: str) -> str:
    """Resolve relative ``src``/``href`` URLs in reader-view HTML against ``base_url``.

    The reader view is served from Lectio's own origin, so a relative image like
    ``model_m.webp`` (e.g. fabiensanglard.net, which uses page-relative image
    paths and ships no ``<base>`` tag) would otherwise resolve against Lectio and
    404. Readability's summary path absolutizes only when ``Document(url=...)`` is
    set; the BS4 content fallback returns its element verbatim, so apply one
    uniform pass here. Idempotent for already-absolute URLs."""
    if not article_html or not base_url:
        return article_html
    try:
        from lxml import html as lxml_html

        frag = lxml_html.fromstring(article_html)
        frag.make_links_absolute(base_url, resolve_base_href=False)
        return lxml_html.tostring(frag, encoding="unicode")
    except Exception:
        return article_html


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


def _entry_query_suffix(feed_url: str, entry_id: str | None, include: bool = True) -> str:
    """Build the ``&feed_url=…&entry_id=…`` suffix used to re-select an entry after a
    redirect, or ``""`` when no entry should be selected. Centralizes the
    quote_plus pair that was repeated across the mark-read / range routes."""
    if not include or not entry_id:
        return ""
    return f"&feed_url={quote_plus(feed_url)}&entry_id={quote_plus(entry_id)}"


def add_no_referrer_to_images(content: str) -> str:
    """Add referrerpolicy="no-referrer" to inline <img> tags that lack it.

    Some sites (e.g. nanolx.org) serve a hotlink-protection placeholder image
    when the Referer header points at a foreign origin. The reader loads body
    images directly, so the browser sends the Lectio page URL as Referer and the
    real image is swapped for the placeholder. Suppressing the referer makes the
    request look like a direct hit, so hotlink-protected images load — and it
    avoids leaking the reader URL to image hosts.
    """
    if "<img" not in content.lower():
        return content
    return re.sub(
        r"<img\b(?![^>]*\breferrerpolicy\s*=)([^>]*?)(/?)>",
        r'<img\1 referrerpolicy="no-referrer"\2>',
        content,
        flags=re.IGNORECASE,
    )


def proxy_hotlink_images(content: str) -> str:
    """Rewrite <img> src/srcset for hotlink-protected hosts to the /api/img proxy.

    referrerpolicy="no-referrer" fixes fresh loads, but a browser that already
    cached the host's "image was hotlinked" placeholder (under the unchanged
    image URL, with no Vary header) keeps serving it. Pointing src at
    /api/img?u=<url> is a new same-origin URL the browser hasn't cached, and the
    server-side fetch carries no Referer — so the real image loads. srcset is
    dropped for these images so the proxied src is the one used.
    """
    if "<img" not in content.lower():
        return content

    def _rewrite(m: re.Match) -> str:
        tag = m.group(0)
        src_m = re.search(r'\bsrc\s*=\s*(?:"([^"]*)"|\x27([^\x27]*)\x27)', tag, re.IGNORECASE)
        if not src_m:
            return tag
        src = html.unescape((src_m.group(1) or src_m.group(2) or "").strip())
        if not src or src.startswith("data:"):
            return tag
        if not _is_hotlink_img_host(urlparse(src).netloc):
            return tag
        proxied = f"/api/img?u={quote(src, safe='')}"
        tag = tag[: src_m.start()] + f'src="{proxied}"' + tag[src_m.end():]
        # Drop srcset/data-* so the browser uses the proxied src, not a direct URL.
        tag = re.sub(
            r'\s+(?:srcset|data-srcset|data-src|data-lazy-src)\s*=\s*(?:"[^"]*"|\x27[^\x27]*\x27)',
            "",
            tag,
            flags=re.IGNORECASE,
        )
        return tag

    return re.sub(r"<img\b[^>]*>", _rewrite, content, flags=re.IGNORECASE)


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
    on_fetch_refused=_flag_browser_ua_on_refusal,
)

websub_service: WebSubService | None = (
    WebSubService(
        get_shared_connection=get_websub_connection,
        public_url=LECTIO_PUBLIC_URL,
        user_agent=READABILITY_USER_AGENT,
        logger=LOGGER,
    )
    if LECTIO_PUBLIC_URL
    else None
)

# Auth is per-user via user_store; each request runs under a bound tenancy context.
fever_service: FeverService | None = FeverService(
    get_meta_connection=get_meta_connection,
    get_reader=get_reader,
    fever_api_key="",
    root_folder_name=ROOT_FOLDER_NAME,
    current_user=tenancy.current_user_id,
    presync=False,
)

greader_service: GReaderService | None = GReaderService(
    get_meta_connection=get_meta_connection,
    get_reader=get_reader,
    username="",
    password=_FEVER_PASSWORD,
    root_folder_name=ROOT_FOLDER_NAME,
)

miniflux_service = MinifluxService(
    get_meta_connection=get_meta_connection,
    get_reader=get_reader,
    root_folder_name=ROOT_FOLDER_NAME,
    current_user=tenancy.current_user_id,
)


def build_source_proxy_response(source_url: str) -> HTMLResponse:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return HTMLResponse("<h1>Unsupported URL scheme.</h1>", status_code=400)

    try:
        with httpx.Client(follow_redirects=False, timeout=12.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            response = url_guard.safe_get(client, source_url, headers={"User-Agent": READABILITY_USER_AGENT})
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
        if not url_guard.is_safe_outbound_url(source_url):
            raise url_guard.UnsafeURLError(source_url)
        with httpx.Client(follow_redirects=False, timeout=8.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
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
                elem = soup.find(class_=lambda c, v=value: bool(c) and v in c.split())  # type: ignore[arg-type]
            else:
                elem = soup.find(value)
            if elem:
                for nav_div in elem.find_all("div", class_=lambda c: c and "nav" in c.split()):
                    nav_div.decompose()
                return str(elem)
        return ""
    except Exception:
        return ""


_BBCODE_SIGNAL_RE = re.compile(
    r'\[(?:b|i|u|s|strike|url|img|quote|code|list|color|size|h[1-6]|center|spoiler)[\]=\s/\]]',
    re.IGNORECASE,
)


def _looks_like_bbcode(text: str) -> bool:
    """True when text contains BBCode markup (at least two BBCode tag patterns).

    The old heuristic (bb > ht) rejected mixed-content feeds like Nexus Mods
    where <br> tags pushed the HTML count above the BBCode count even though
    the article body is primarily BBCode.  _bbcode_to_html() already handles
    mixed content safely (it skips html.escape when HTML tags are present).

    But genuine authored HTML — anything with block structure (<p>, <div>,
    headings, lists, tables) — is never BBCode, even when stray brackets trip the
    signal. Sphinx/Pelican math blogs (eli.thegreenplace.net) carry LaTeX alt text
    like "[I=\\int ...]" / "[s(x)=...]" that reads as a "[i]"/"[s]" tag. Treating
    such HTML as BBCode runs _bbcode_to_html's newline->\\<br\\> step over its
    newline-formatted source, shredding every paragraph into one line per break
    ("poem" layout). BBCode feeds use <br> for breaks, not these tags, so the guard
    is safe for them.
    """
    if re.search(r"<(?:p|div|h[1-6]|ul|ol|table|blockquote|section|article)\b", text, re.IGNORECASE):
        return False
    return len(_BBCODE_SIGNAL_RE.findall(text)) >= 2


def _safe_bb_url(raw: str) -> str:
    """Return raw only if it's http(s); block everything else."""
    stripped = raw.strip()
    return stripped if re.match(r'https?://', stripped, re.IGNORECASE) else '#'


def _bbcode_to_html(text: str) -> str:
    """Convert common BBCode tags to HTML.

    Only HTML-escapes the source when it contains no existing HTML markup
    (i.e. pure BBCode text).  For mixed content (feedparser adds <br/> etc.)
    we leave the existing tags intact and only apply BBCode substitutions.
    """
    import html as _html
    if re.search(r'<[a-z!]', text, re.IGNORECASE):
        out = text  # already has HTML — don't double-escape
    else:
        out = _html.escape(text, quote=False)

    # block-level
    def _list_items(s: str) -> str:
        return re.sub(r'\[\*\]', '<li>', s, flags=re.IGNORECASE)

    out = re.sub(r'\[list=1\](.*?)\[/list\]',
                 lambda m: '<ol>' + _list_items(m.group(1)) + '</ol>', out, flags=re.I | re.S)
    out = re.sub(r'\[list\](.*?)\[/list\]',
                 lambda m: '<ul>' + _list_items(m.group(1)) + '</ul>', out, flags=re.I | re.S)
    out = re.sub(r'\[quote=([^\]]{1,100})\](.*?)\[/quote\]',
                 r'<blockquote><cite>\1</cite>\2</blockquote>', out, flags=re.I | re.S)
    out = re.sub(r'\[quote\](.*?)\[/quote\]',
                 r'<blockquote>\1</blockquote>', out, flags=re.I | re.S)
    out = re.sub(r'\[code\](.*?)\[/code\]',
                 r'<pre><code>\1</code></pre>', out, flags=re.I | re.S)
    out = re.sub(r'\[center\](.*?)\[/center\]',
                 r'<div style="text-align:center">\1</div>', out, flags=re.I | re.S)
    out = re.sub(r'\[spoiler(?:=[^\]]*)?\](.*?)\[/spoiler\]',
                 r'<details><summary>Spoiler</summary>\1</details>', out, flags=re.I | re.S)
    out = re.sub(r'\[h([1-6])\](.*?)\[/h\1\]',
                 r'<h\1>\2</h\1>', out, flags=re.I | re.S)

    # inline
    out = re.sub(r'\[b\](.*?)\[/b\]', r'<strong>\1</strong>', out, flags=re.I | re.S)
    out = re.sub(r'\[i\](.*?)\[/i\]', r'<em>\1</em>', out, flags=re.I | re.S)
    out = re.sub(r'\[u\](.*?)\[/u\]', r'<u>\1</u>', out, flags=re.I | re.S)
    out = re.sub(r'\[s\](.*?)\[/s\]', r'<s>\1</s>', out, flags=re.I | re.S)
    out = re.sub(r'\[strike\](.*?)\[/strike\]', r'<s>\1</s>', out, flags=re.I | re.S)
    out = re.sub(r'\[color=([^\]]{1,30})\](.*?)\[/color\]',
                 r'<span style="color:\1">\2</span>', out, flags=re.I | re.S)
    # [size=1..7] maps to the HTML <font size> scale; larger values treated as px.
    _BB_SIZE_EM = {1: 0.6, 2: 0.75, 3: 1.0, 4: 1.15, 5: 1.4, 6: 2.0, 7: 3.0}
    def _bb_size(m: re.Match) -> str:
        n = int(m.group(1))
        css = f'{_BB_SIZE_EM[n]}em' if n in _BB_SIZE_EM else f'{n}px'
        return f'<span style="font-size:{css}">{m.group(2)}</span>'
    out = re.sub(r'\[size=(\d{1,3})\](.*?)\[/size\]', _bb_size, out, flags=re.I | re.S)

    # links / images — sanitize URLs
    def _url_tag(m: re.Match) -> str:
        href = _safe_bb_url(_html.unescape(m.group(1)))
        label = m.group(2).strip() or href
        return f'<a href="{_html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>'

    out = re.sub(r'\[url=([^\]]{1,500})\](.*?)\[/url\]', _url_tag, out, flags=re.I | re.S)
    out = re.sub(
        r'\[url\](https?://[^\[]{1,500})\[/url\]',
        lambda m: f'<a href="{_html.escape(_safe_bb_url(_html.unescape(m.group(1))), quote=True)}"'
                  f' target="_blank" rel="noopener noreferrer">{_html.escape(m.group(1))}</a>',
        out, flags=re.I | re.S,
    )
    out = re.sub(
        r'\[img(?:=[^\]]*)?\](https?://[^\[]{1,500})\[/img\]',
        lambda m: f'<img src="{_html.escape(_safe_bb_url(_html.unescape(m.group(1))), quote=True)}"'
                  f' loading="lazy" style="max-width:100%">',
        out, flags=re.I | re.S,
    )

    # newlines
    out = out.replace('\n', '<br>\n')
    return out


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
            src = str(img.get("src") or "")
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
                    src = str(img.get("src") or "")
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
                    src = str(img.get("src") or "")
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


# Reader-view media CSS, shared by build_readability_response and
# _wrap_readability_html so the two paths can't diverge. iframes default to 16:9;
# the block below collects every exception (audio players that need a fixed
# height instead) in one place — add new embed-host overrides here.
_READER_VIEW_MEDIA_CSS = (
    "article img{max-width:100%;height:auto;max-height:240px;}article a>img{max-height:1.4em;vertical-align:middle;}"
    "article iframe{max-width:100%;width:100%;aspect-ratio:16/9;height:auto;border:0;}"
    # --- audio embeds: not 16:9, give each its own fixed height ---
    "p.lectio-embed{margin:1rem 0;}p.lectio-embed iframe[src*='spotify.com']{aspect-ratio:auto;height:152px;}"
    "article iframe[src*='bandcamp.com/EmbeddedPlayer']{aspect-ratio:auto;height:470px;}"
    "article iframe[src*='bandcamp.com/EmbeddedPlayer'][src*='size=small']{height:42px;}"
    "article iframe[src*='soundcloud.com']{aspect-ratio:auto;height:166px;}"
    # --- end audio embeds ---
    "article svg{width:1.2em;height:1.2em;vertical-align:middle;flex-shrink:0;}"
)


def build_readability_response(source_url: str) -> HTMLResponse:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return HTMLResponse("<h1>Unsupported URL scheme.</h1>", status_code=400)

    try:
        with httpx.Client(follow_redirects=False, timeout=12.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            response = url_guard.safe_get(client, source_url, headers={"User-Agent": READABILITY_USER_AGENT})
        response.raise_for_status()
        raw_html = response.text
        doc = Document(raw_html, url=source_url)
        title = doc.short_title() or source_url
        summary = doc.summary(html_partial=True)
        summary = _reinject_readability_embeds(summary, raw_html)
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
        article_html = _dedupe_readability_images(article_html)
        article_html = _strip_bandcamp_track_signature(article_html)
        # Resolve any remaining relative src/href (esp. the BS4 fallback path,
        # which returns its element verbatim) against the source page URL.
        article_html = _absolutize_article_urls(article_html, source_url)
        # Apply the same hotlink handling as the entry pane (runs after
        # absolutization so host-matching sees absolute src): route known
        # hotlink hosts through /api/img (e.g. fabiensanglard.net, whose .webp
        # 403 a no-Referer browser load), and strip the Referer on the rest so
        # foreign-Referer placeholder hosts serve the real image.
        article_html = proxy_hotlink_images(article_html)
        article_html = add_no_referrer_to_images(article_html)
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
            + _READER_VIEW_MEDIA_CSS +
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


# Blogger posts can ship an empty feed <title> while the real title lives only
# as the first body heading and in the post URL slug (e.g. treecardgames). Recover
# a readable title from the slug so the list/article don't show "(untitled)".
# Scoped to Blogger so genuinely-untitled posts elsewhere (e.g. Tumblr reblogs)
# keep their "(untitled)" label.
_BLOGGER_FEED_RE = re.compile(r"blogspot\.com|blogger\.com|/feeds/posts/default", re.IGNORECASE)


def _title_from_blogger_slug(link: str) -> str | None:
    """Humanize a Blogger post URL slug into a title, or None.

    e.g. ``https://x.blogspot.com/2026/06/gin-rummy-strategies-essential.html``
    → ``Gin Rummy Strategies Essential``.
    """
    if not link:
        return None
    slug = urlparse(link).path.rsplit("/", 1)[-1]
    slug = re.sub(r"\.html?$", "", slug, flags=re.IGNORECASE)
    words = [w for w in slug.split("-") if w]
    if not words:
        return None
    return " ".join(w.capitalize() for w in words)


def _decode_display_entities(text: str) -> str:
    """Fully decode HTML entities for plain-text display (e.g. titles).

    Titles are rendered as escaped text, so a stored literal entity shows raw
    ("Magus&rsquo; Castle"). Some feeds (notably Tumblr) double-encode
    ("&amp;rsquo;"), so unescape repeatedly until stable to surface the real
    character. Safe for display: the value is plain text that the template /
    JSON layer escapes again, so no markup can be introduced."""
    prev = text
    for _ in range(3):
        cur = html.unescape(prev)
        if cur == prev:
            break
        prev = cur
    return prev


def _display_title(entry) -> str:
    """Entry title for display, recovering Blogger empty-title posts from the slug.

    Returns the feed title when present; otherwise, for Blogger feeds only, a
    title humanized from the URL slug. Falls back to "" (template renders
    "(untitled)") for genuinely-untitled posts on other sites."""
    title = (getattr(entry, "title", None) or "").strip()
    if title:
        return _decode_display_entities(title)
    feed_url = str(getattr(entry, "feed_url", "") or "")
    link = str(getattr(entry, "link", "") or "")
    if _BLOGGER_FEED_RE.search(feed_url) or _BLOGGER_FEED_RE.search(link):
        return _title_from_blogger_slug(link) or ""
    return ""


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
        # Build feed_url → site homepage URL map via direct SQL rather than
        # iterating all feed objects via reader. Keeps this O(feeds in view)
        # instead of O(all feeds in the library).
        feed_site_map: dict[str, str | None] = {url: None for url in feed_urls}
        if feed_urls:
            _sm_conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5.0)
            _sm_conn.row_factory = sqlite3.Row
            try:
                _sm_list = list(feed_urls)
                for _i in range(0, len(_sm_list), 999):
                    _chunk = _sm_list[_i:_i + 999]
                    _ph = ",".join("?" for _ in _chunk)
                    for _row in _sm_conn.execute(
                        f"SELECT url, link FROM feeds WHERE url IN ({_ph})", _chunk
                    ).fetchall():
                        feed_site_map[str(_row["url"])] = _row["link"] or None
            finally:
                _sm_conn.close()

        all_feed_entries = []
        fetch_limit = max(1, int(limit))
        need_all = bool(search_terms or normalized_sort_dir == "asc")
        # When a manual tag is selected, push the filter into reader's native
        # tags= argument so the match happens in SQL across the whole library.
        # Previously the tag was applied only as a post-filter on the newest-N
        # window fetched below, so tagged entries outside that window (tags are
        # sparse) never surfaced — clicking a tag showed nothing.
        tag_filter = (
            [f"{MANUAL_TAG_KEY_PREFIX}{normalized_selected_tag}"]
            if normalized_selected_tag
            else None
        )
        PER_FEED_QUERY_THRESHOLD = 32

        if history_fast_keys:
            # Fast history path: fetch each entry by primary key (indexed lookup)
            # instead of scanning all read entries. N small lookups vs. one huge scan.
            for furl, eid in history_fast_keys:
                e = reader.get_entry((furl, eid), None)
                if e is not None:
                    all_feed_entries.append(e)
        elif normalized_sort_dir == "asc" and not search_terms and len(feed_urls) > PER_FEED_QUERY_THRESHOLD and not tag_filter:
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
                _rconn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5.0)
                _rconn.row_factory = sqlite3.Row
                try:
                    if len(feed_urls) <= 999:
                        _feed_list = list(feed_urls)
                        _placeholders = ",".join("?" for _ in _feed_list)
                        rows = _rconn.execute(
                            f"SELECT feed, id FROM entries WHERE feed IN ({_placeholders}){read_clause}"
                            f" ORDER BY published ASC LIMIT ?",
                            _feed_list + [fetch_limit],
                        ).fetchall()
                    else:
                        # >999 feeds: SQLite's variable limit prevents a single IN clause.
                        # A global scan without a feed filter picks up entries from feeds
                        # outside feed_urls (e.g. import-synthesised feeds) and the Python
                        # filter can discard the entire result window. Batch instead.
                        _feed_list = list(feed_urls)
                        batch_rows: list = []
                        for _i in range(0, len(_feed_list), 999):
                            _chunk = _feed_list[_i:_i + 999]
                            _ph = ",".join("?" for _ in _chunk)
                            chunk_rows = _rconn.execute(
                                f"SELECT feed, id, published FROM entries"
                                f" WHERE feed IN ({_ph}){read_clause}"
                                f" ORDER BY published ASC LIMIT ?",
                                _chunk + [fetch_limit],
                            ).fetchall()
                            batch_rows.extend(chunk_rows)
                        batch_rows.sort(key=lambda r: r["published"] or "")
                        rows = batch_rows
                finally:
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
        elif not search_terms and len(feed_urls) > PER_FEED_QUERY_THRESHOLD and not tag_filter:
            # DESC (newest-first) with many feeds: same SQL-batch approach as the
            # ASC path. Avoids a Python-side global scan of every entry in the DB.
            # "post" sort uses the entry publish date; "received" uses the time
            # reader first observed the entry (recent_sort).
            read_sql = {None: "", True: " AND read IS NOT NULL", False: " AND (read IS NULL OR read != 1)"}
            read_clause = read_sql.get(reader_read_filter, "")
            sort_col = "recent_sort" if normalized_sort_by == "received" else "coalesce(published, first_updated)"
            try:
                _rconn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5.0)
                _rconn.row_factory = sqlite3.Row
                try:
                    _feed_list = list(feed_urls)
                    if len(_feed_list) <= 999:
                        _placeholders = ",".join("?" for _ in _feed_list)
                        rows = _rconn.execute(
                            f"SELECT feed, id FROM entries WHERE feed IN ({_placeholders}){read_clause}"
                            f" ORDER BY {sort_col} DESC LIMIT ?",
                            _feed_list + [fetch_limit],
                        ).fetchall()
                    else:
                        batch_rows_desc: list = []
                        for _i in range(0, len(_feed_list), 999):
                            _chunk = _feed_list[_i:_i + 999]
                            _ph = ",".join("?" for _ in _chunk)
                            chunk_rows = _rconn.execute(
                                f"SELECT feed, id, {sort_col} AS sort_val FROM entries"
                                f" WHERE feed IN ({_ph}){read_clause}"
                                f" ORDER BY {sort_col} DESC LIMIT ?",
                                _chunk + [fetch_limit],
                            ).fetchall()
                            batch_rows_desc.extend(chunk_rows)
                        batch_rows_desc.sort(key=lambda r: r["sort_val"] or "", reverse=True)
                        rows = batch_rows_desc[:fetch_limit]
                finally:
                    _rconn.close()
                for row in rows:
                    e = reader.get_entry((str(row["feed"]), str(row["id"])), None)
                    if e is not None:
                        all_feed_entries.append(e)
                    if len(all_feed_entries) >= fetch_limit:
                        break
            except Exception:
                LOGGER.exception("[perf] desc-sql fast path failed, falling back")
                for entry in reader.get_entries(read=reader_read_filter, tags=tag_filter):
                    if entry.feed_url not in feed_urls:
                        continue
                    all_feed_entries.append(entry)
                    if not need_all and len(all_feed_entries) >= fetch_limit:
                        break
        else:
            # Few feeds: query per feed with the SQL feed= filter.
            for feed_url in feed_urls:
                for entry in reader.get_entries(feed=feed_url, read=reader_read_filter, tags=tag_filter):
                    all_feed_entries.append(entry)
                    if not need_all and len(all_feed_entries) >= fetch_limit:
                        break
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

            title_text = _display_title(entry) or entry.title
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
                    "link": entry.link or _derived_entry_link(entry),
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
        _feed_thumb_setting = _feed_prefs.get("feed_thumbnail_url")
        _show_thumb = bool(_feed_prefs.get("show_lead_image_as_thumb", 1))
        _thumb_strategy = _feed_prefs.get("thumb_strategy") or None
        if _feed_thumb_setting and _feed_thumb_setting != "__favicon__":
            _raw_thumb = str(_feed_thumb_setting)  # pinned URL override
        elif _feed_thumb_setting == "__favicon__":
            _raw_thumb = None
        else:
            if _thumb_strategy == "inline":
                # Use first image from feed content HTML — bypasses the og_scrape cache
                _raw_thumb = lead_image_service.extract_inline_thumb_url(entry)
            elif _thumb_strategy == "media_rss":
                _raw_thumb = lead_image_service.extract_media_rss_thumb_url(entry)
            else:
                _raw_thumb = None
            if not _raw_thumb:
                # Strategy-specific extraction found nothing — e.g. a feed whose
                # thumb_strategy was auto-detected as media_rss/inline but whose
                # reader Entry carries no usable media field. Fall back to the
                # cached lead image (same one shown in the article view) instead
                # of rendering a blank thumbnail.
                _raw_thumb = lead_image_service.get_cached_entry_thumbnail(
                    feed_url_str,
                    str(getattr(entry, "id", "") or ""),
                    str(getattr(entry, "link", "") or ""),
                )
            if not _raw_thumb:
                # Last resort: a raw inline <svg> in the entry content (sanitized
                # → data URI). Keeps the list thumb consistent with the article.
                _raw_thumb = lead_image_service.extract_inline_svg_thumb_url(entry)
        _thumb = _raw_thumb if _show_thumb else None
        _feed_thumb_crop = str(_feed_prefs.get("thumb_crop") or "cover")
        if _feed_thumb_crop not in _VALID_THUMB_CROPS:
            _feed_thumb_crop = "cover"
        _entry_crop_override = lead_image_service.get_entry_thumb_crop(feed_url_str, str(getattr(entry, "id", "") or ""))
        _thumb_crop = _entry_crop_override if _entry_crop_override else _feed_thumb_crop
        _smart_ms = _feed_prefs.get("smart_min_scale")
        _fill_zm = _feed_prefs.get("fill_zoom")
        rec.update(
            {
                "thumbnail_url": _thumb,
                "show_thumbnail": _show_thumb,
                "thumb_crop": _thumb_crop,
                "smart_min_scale": float(_smart_ms) if _smart_ms is not None else None,
                "fill_zoom": float(_fill_zm) if _fill_zm is not None else None,
                "thumb_strategy": _thumb_strategy or "",
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
    only_feed_url: str | None = None,
) -> list[dict]:
    """Append archive-only saved entries (orphans), then re-sort + clip.

    Orphans are starred entries whose feed is no longer in any folder; their
    metadata comes entirely from the starred archive. Rendered alongside live
    saved entries so unsubscribing a feed doesn't make its saves disappear.

    When *only_feed_url* is given, restrict orphans to that single feed (matched
    canonically) — used when the user clicks the feed link of an orphaned save
    to browse just that unsubscribed feed's archived items.
    """
    orphans = starred_archive_service.get_orphan_saved_entries(live_feed_urls)
    if only_feed_url is not None:
        target = normalize_feed_url(only_feed_url)
        orphans = [o for o in orphans if normalize_feed_url(o["feed_url"]) == target]
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
    # Hotlink-protected hosts: proxy unconditionally (the placeholder is served
    # with HTTP 200, so there's nothing to detect — the host is known-bad).
    if _is_hotlink_img_host(domain):
        return f"/api/img?u={quote(image_url)}"
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


def _derive_article_lead_image(entry) -> str | None:
    """Pick the article lead image, honoring an inline / media_rss feed strategy.

    Those strategies derive the image straight from the entry's own content or
    RSS fields and deliberately bypass the lead-image cache — the same extractors
    the post-list thumbnail uses. The article view previously always called the
    cache-consulting ``extract_entry_thumbnail_url``, so a stale negative cache
    entry (image resolved as "none" before the feed was pinned to inline, e.g. a
    DeviantArt gallery) made the article show no image while the list still
    showed the inline thumbnail. Routing by strategy keeps the two consistent."""
    feed_url = str(getattr(entry, "feed_url", "") or "")
    strategy, _, _ = lead_image_service.get_feed_strategy(feed_url)
    # Each strategy has a primary extractor that bypasses the lead-image cache
    # (so the article matches the list thumbnail's own logic):
    #   inline / webcomic → inline <img> (claycomix ships the full strip inline;
    #                       DeviantArt galleries dodge a stale negative cache)
    #   media_rss         → <media:content> (reader keeps media:thumbnail/enclosure)
    # Every strategy then falls back to the cache-consulting extractor — covering
    # feeds whose image lives in an <enclosure>/media field reader dropped (gottadeal,
    # paizo) — and finally to a raw inline <svg>. Without the cache fallback the
    # article showed no image AND persisted a negative on open, poisoning the thumb.
    if strategy in ("inline", "webcomic"):
        primary = lead_image_service.extract_inline_thumb_url(entry)
    elif strategy == "media_rss":
        primary = lead_image_service.extract_media_rss_thumb_url(entry)
    else:
        primary = None
    return (
        primary
        or lead_image_service.extract_entry_thumbnail_url(entry, include_source_lookup=False)
        or lead_image_service.extract_inline_svg_thumb_url(entry)
    )
_PLAINTEXT_PROMOTE_RE = re.compile(r"https?://|&lt;br|<br", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# A bare URL that points straight at an image — rendered inline as <img> rather
# than a link when it appears in a promoted plain-text summary. Trailing
# punctuation is excluded by _BARE_URL_RE's character class already.
_BARE_IMG_URL_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp|svg)$", re.IGNORECASE)


# A real HTML tag (block or inline) — used to distinguish genuine HTML content
# from plain text that a feed has merely escaped and mislabeled as text/html.
_REAL_HTML_TAG_RE = re.compile(
    r"<(?:p|div|br|a|img|ul|ol|li|table|tr|td|blockquote|pre|code|h[1-6]|span|strong|em|b|i)\b",
    re.IGNORECASE,
)


def _looks_like_escaped_plaintext(value: str | None) -> bool:
    """True when content declared as HTML is really escaped plain text: it carries
    escaped break markers (``&lt;br&gt;``) but no actual HTML tags. Such feeds
    (e.g. orpheus.network news) otherwise render as inert escaped text."""
    if not value:
        return False
    if "&lt;br" not in value.lower():
        return False
    return not _REAL_HTML_TAG_RE.search(value)


def _promote_plaintext_summary(summary: str | None) -> str | None:
    """Turn a bare-text summary into renderable HTML, or None to leave it as-is.

    Some feeds (e.g. orpheus.network news) ship no HTML content — only a
    plain-text summary carrying bare ``https://`` URLs and line breaks encoded as
    literal ``<br>`` or double-escaped ``&lt;br&gt;``. Rendered in the template's
    ``<pre>`` fallback those URLs aren't clickable and the breaks show as literal
    text. When a summary actually contains URLs or break markers, promote it to
    content_html: normalize the breaks to real ``<br>`` and linkify the URLs.
    Genuinely plain prose (no URLs, no breaks) returns None so the ``<pre>``
    fallback keeps preserving its whitespace layout."""
    if not summary or not summary.strip():
        return None
    if not _PLAINTEXT_PROMOTE_RE.search(summary):
        return None
    # Collapse escaped/literal <br> (possibly multiply-escaped, e.g. &amp;lt;br&amp;gt;)
    # to newlines, then treat the whole thing as plain text.
    text = re.sub(r"&(?:amp;)*lt;br\s*/?\s*&(?:amp;)*gt;", "\n", summary, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # These feeds escape inconsistently — break markers once but ampersands in
    # URLs twice (&amp;amp;). Fully unescape (bounded) so URLs are correct, then
    # re-escape exactly once below. Breaks are already newlines, so unescaping
    # any remaining &lt;…&gt; just yields inert text we re-escape anyway.
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    # Tame runs of blank lines (the feed pairs each <br> with a real newline).
    text = re.sub(r"\n{3,}", "\n\n", text)
    escaped = html.escape(text)

    def _linkify(m: re.Match) -> str:
        seg = m.group(0)  # already HTML-escaped; src/href-safe (no spaces/quotes)
        if _BARE_IMG_URL_RE.search(html.unescape(seg)):
            return (f'<img src="{seg}" alt="" loading="lazy" '
                    f'referrerpolicy="no-referrer" style="max-width:100%;height:auto;">')
        return f'<a href="{seg}" target="_blank" rel="noopener noreferrer">{seg}</a>'

    escaped = _BARE_URL_RE.sub(_linkify, escaped)
    return escaped.replace("\n", "<br>")
def _youtube_embed_html(video_id: str) -> str:
    """Inline YouTube player markup for a video id.

    Host follows the per-user privacy/features preference (``youtube_embed_host``):
    privacy-enhanced ``youtube-nocookie.com`` by default, or the standard
    ``www.youtube.com`` (Share / Watch Later) when the user opts in. Uses
    ``referrerpolicy`` rather than the JS API. We deliberately omit
    ``enablejsapi=1`` — nothing in the app drives the IFrame JS API, and YouTube
    refuses playback when it is set without a matching ``origin=`` parameter,
    which silently broke the inline player.

    The result is injected into content_html (rendered with ``| safe``), so the
    video id is HTML-escaped before interpolation — defense in depth in case the
    upstream extractor's validation is ever loosened (the id is otherwise always
    ``[A-Za-z0-9_-]``)."""
    safe_id = html.escape(video_id, quote=True)
    src = f"https://{youtube_embed_host()}/embed/{safe_id}?rel=0"
    return (
        '<div class="youtube-embed-container" style="max-width:560px;margin:1em auto;">'
        f'<iframe width="100%" height="315" src="{src}" title="YouTube video player" '
        'frameborder="0" allowfullscreen loading="lazy" '
        'referrerpolicy="strict-origin-when-cross-origin" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"'
        "></iframe></div>"
    )


_BC_EMBED_IFRAME_RE = re.compile(
    r'(<iframe\b[^>]*\bsrc=["\'])([^"\']*bandcamp\.com/EmbeddedPlayer/[^"\']*)(["\'])',
    re.I,
)


def _strip_bandcamp_track_signature(content_html: str) -> str:
    """Drop the domain-locked single-track signature from Bandcamp embeds.

    Bandcamp's ``.../tracks=<ids>/esig=<sig>/`` single-track player form is bound
    to the publisher's domain — Bandcamp validates the Referer, so it renders
    "Sorry, this track or album is not available." anywhere else (including
    Lectio). The plain ``album=<id>`` form embeds on any site and plays the same
    (pre-order/premiere) album, so strip the ``tracks``/``esig`` path segments and
    fall back to it rather than show a dead player."""
    if not isinstance(content_html, str) or "bandcamp.com/EmbeddedPlayer" not in content_html:
        return content_html
    if "tracks=" not in content_html and "esig=" not in content_html:
        return content_html

    def _fix(m: re.Match) -> str:
        src = m.group(2)
        src = re.sub(r"/tracks=[\w,]+", "", src)
        src = re.sub(r"/esig=[0-9a-f]+", "", src, flags=re.IGNORECASE)
        return m.group(1) + src + m.group(3)

    return _BC_EMBED_IFRAME_RE.sub(_fix, content_html)


_YT_EMBED_SRC_HOST_RE = re.compile(
    r'(?P<pre><iframe\b[^>]*\bsrc=["\']https://)'
    r'(?:www\.)?youtube(?:-nocookie)?\.com'
    r'(?P<post>/embed/)',
    re.IGNORECASE,
)


def _apply_youtube_embed_host(content_html: str) -> str:
    """Rewrite feed-native YouTube embed iframe hosts to the current user's
    privacy/features preference (youtube.com vs youtube-nocookie.com). Only
    touches iframe /embed/ URLs, not plain youtube.com watch links."""
    if not isinstance(content_html, str) or "/embed/" not in content_html:
        return content_html
    host = youtube_embed_host()
    return _YT_EMBED_SRC_HOST_RE.sub(rf'\g<pre>{host}\g<post>', content_html)


_YT_EMBED_FIGURE_CLASS_RE = re.compile(r"is-provider-youtube|wp-block-embed-youtube", re.I)
# Empty placeholders left behind when an embed <iframe> was stripped at ingest:
# WordPress' provider figure, or ArtStation's video-wrapper div.
_YT_EMBED_PLACEHOLDER_RE = re.compile(
    r"is-provider-youtube|wp-block-embed-youtube|video-wrapper", re.I
)


def _inject_recovered_youtube_embeds(content_html: str, video_ids: list[str]) -> str:
    """Replace stripped YouTube embed placeholders with real players.

    feedparser/ingest removes the embed ``<iframe>``, leaving an empty shell:
    WordPress' ``<figure class="wp-block-embed ... is-provider-youtube">`` or
    ArtStation's ``<div class="video-wrapper media-asset...">``. Replace each such
    placeholder, in document order, with the player for the matching recovered
    video id. Placeholders past the end of ``video_ids`` are left untouched."""
    if not video_ids:
        return content_html
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content_html, "html.parser")
    placeholders = []
    for tag in soup.find_all(["figure", "div"]):
        classes = " ".join(tag.get("class") or [])  # type: ignore[arg-type]
        if tag.name == "figure" and _YT_EMBED_FIGURE_CLASS_RE.search(classes):
            placeholders.append(tag)
        elif (tag.name == "div" and "video-wrapper" in classes.lower()
                and not tag.find(["iframe", "video"])):
            placeholders.append(tag)
    if not placeholders:
        return content_html
    for ph, vid in zip(placeholders, video_ids, strict=False):
        ph.replace_with(BeautifulSoup(_youtube_embed_html(vid), "html.parser"))
    return str(soup)


_YT_WATCH_URL_RE = re.compile(
    r'^(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/watch\?[^\s]*\bv=|youtu\.be/|'
    r'youtube\.com/shorts/)[\w-]+',
    re.IGNORECASE,
)

_BC_URL_RE = re.compile(
    r'^https?://[^/]*\.bandcamp\.com/(album|track)/[^/?#\s]+',
    re.IGNORECASE,
)


def _embed_standalone_youtube_links(content_html: str) -> str:
    """Turn a paragraph/anchor that is *only* a bare YouTube link into a player.

    Some feeds (WordPress with the oEmbed iframe stripped) deliver the video as
    a standalone watch/youtu.be/shorts link in its own paragraph rather than an
    embed. When a <p> (or a lone <a>) contains nothing but such a link, replace
    it with the inline player. Inline mentions inside prose are left untouched —
    we only convert when the link is the element's sole meaningful content."""
    if not isinstance(content_html, str) or "youtu" not in content_html.lower():
        return content_html
    if "/embed/" in content_html and "youtube" in content_html.lower():
        # An embed already exists; still scan, but each match is gated below.
        pass
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content_html, "html.parser")
    changed = False
    for a in list(soup.find_all("a")):
        href = str(a.get("href") or "").strip()
        if not href or not _YT_WATCH_URL_RE.match(href):
            continue
        # The link must be the sole content of its block — convert a paragraph
        # (or lone anchor) that is just this link, but not a worded link sitting
        # inside a sentence. Compare the anchor's text against its container's
        # full text: if they match, the anchor is the container's only content.
        anchor_text = a.get_text(strip=True)
        parent = a.parent
        target = a
        if parent is not None and parent.name == "p":
            if parent.get_text(strip=True) != anchor_text:
                continue  # other prose in the paragraph → inline mention
            target = parent
        vid = youtube_duration_service.extract_video_id(href)
        if not vid:
            continue
        target.replace_with(BeautifulSoup(_youtube_embed_html(vid), "html.parser"))
        changed = True
    return str(soup) if changed else content_html


def _extract_bc_numeric_id(page_html: str, embed_type: str) -> str | None:
    """Extract the numeric Bandcamp album or track ID from page HTML.

    Tries the EmbeddedPlayer URL (present in og:video and inline scripts) then
    a data-album-id / data-track-id attribute as fallback."""
    m = re.search(rf'EmbeddedPlayer/{embed_type}=(\d+)', page_html, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(rf'data-{embed_type}-id=["\'](\d+)["\']', page_html)
    if m:
        return m.group(1)
    return None


def _bc_embed_html(embed_type: str, numeric_id: str, album_url: str) -> str:
    height = "120px" if embed_type == "album" else "42px"
    src = (
        f"https://bandcamp.com/EmbeddedPlayer/{embed_type}={numeric_id}"
        "/size=large/bgcol=ffffff/linkcol=0687f5/tracklist=false/transparent=true/"
    )
    safe_url = html.escape(album_url, quote=True)
    return (
        f'<p class="lectio-embed">'
        f'<iframe style="border:0;width:100%;height:{height}"'
        f' src="{src}" seamless>'
        f'<a href="{safe_url}">{html.escape(album_url)}</a>'
        f'</iframe></p>'
    )


def _embed_standalone_bandcamp_links(content_html: str) -> str:
    """Turn a lone bare Bandcamp album/track link in its own paragraph into an embed.

    Mirrors _embed_standalone_youtube_links but requires the album page to be
    fetched first (the numeric ID is not in the public URL). Cache-first: if
    the album page is already in _source_html_cache the embed is inlined on
    this open; otherwise a background fetch is queued so it appears next open."""
    if not isinstance(content_html, str) or "bandcamp.com" not in content_html.lower():
        return content_html
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content_html, "html.parser")
    changed = False
    for a in list(soup.find_all("a", href=True)):
        href = str(a.get("href") or "").strip()
        m = _BC_URL_RE.match(href)
        if not m:
            continue
        embed_type = m.group(1).lower()  # "album" or "track"
        anchor_text = a.get_text(strip=True)
        parent = a.parent
        target = a
        if parent is not None and parent.name == "p":
            if parent.get_text(strip=True) != anchor_text:
                continue  # inline mention — other text present
            target = parent
        cached = lead_image_service.get_cached_source_html(href)
        if cached:
            _, page_html = cached
            numeric_id = _extract_bc_numeric_id(page_html, embed_type)
            if numeric_id:
                target.replace_with(BeautifulSoup(_bc_embed_html(embed_type, numeric_id, href), "html.parser"))
                changed = True
                continue
        # Album page not cached or ID not extractable — queue background fetch;
        # embed resolves on the next open once the fetch populates the cache.
        lead_image_service.queue_source_html_fetch(href)
    return str(soup) if changed else content_html


def _extract_source_embed_iframes(
    raw_html: str, existing_html: str = "", limit: int = 8
) -> list[tuple[str | None, str]]:
    """Pull allowlisted media-embed players out of a source page's raw HTML.

    Returns ``(canonical_link, embed_html)`` pairs in document order, skipping any
    whose src is already present in ``existing_html`` (deduped on the src path).
    YouTube embeds are rebuilt via ``_youtube_embed_html`` so they honor the
    user's host preference; every other embed is run through the shared sanitizer
    (sandbox + referrer policy) since it's injected at render time. The canonical
    link (``yt:<id>`` for YouTube, else the embed's fallback ``<a href>`` so a
    Bandcamp/SoundCloud player can be matched to its bare album/track link in the
    body) drives in-context placement; ``None`` when no link can be derived."""
    if not raw_html or "<iframe" not in raw_html.lower():
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw_html, "html.parser")
    existing = (existing_html or "").lower()
    seen: set[str] = set()
    out: list[tuple[str | None, str]] = []
    for ifr in soup.find_all("iframe"):
        src = str(ifr.get("src") or ifr.get("data-src") or "").strip()
        if not src or not html_sanitize._embed_host_allowed(src):
            continue
        key = src.split("?", 1)[0].lower()
        if key in seen or key in existing:
            continue
        seen.add(key)
        vid = None
        if "youtu" in src.lower():
            _em = re.search(r"/embed/([\w-]{11})", src)
            vid = _em.group(1) if _em else youtube_duration_service.extract_video_id(src)
        if vid:
            out.append((f"yt:{vid}", _youtube_embed_html(vid)))
        else:
            cleaned = html_sanitize.sanitize_html(str(ifr))
            if "<iframe" in cleaned.lower():
                # iframe content is parsed as raw text (the fallback <a> is a
                # string, not a tag), so match its href out of the inner markup —
                # the canonical album/track link for matching the body's bare link.
                _am = re.search(r'href=["\']([^"\']+)["\']', ifr.decode_contents())
                canonical = _am.group(1) if _am else None
                out.append((canonical, cleaned))
        if len(out) >= limit:
            break
    return out


def _norm_media_link(url: str | None) -> str:
    """Scheme/trailing-slash-insensitive key for matching media URLs."""
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u).split("?", 1)[0].rstrip("/")
    return u


_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _place_recovered_embeds(content_html: str, items: list[tuple[str | None, str]] | list[tuple[str, str]]) -> str:
    """Insert recovered embeds where they belong, not just at the bottom.

    Three passes, in order: (1) replace a bare body link that points at the same
    media — so the player takes the place of the link the feed showed instead of
    the embed; (2) fill empty ``<p></p>`` placeholders that follow a heading (the
    stripped embed slots, e.g. theobelisk's ``<h3>title</h3><p></p>``), in
    document order; (3) append whatever's left at the bottom."""
    if not items:
        return content_html
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content_html or "", "html.parser")
    remaining = list(items)

    # Pass 1 — replace a matching bare link.
    for canonical, embed in list(remaining):
        if not canonical:
            continue
        target = None
        for a in soup.find_all("a"):
            href = str(a.get("href") or "")
            if canonical.startswith("yt:"):
                vid = canonical[3:]
                if youtube_duration_service.extract_video_id(href) == vid or f"/embed/{vid}" in href:
                    target = a
                    break
            elif _norm_media_link(href) and _norm_media_link(href) == _norm_media_link(canonical):
                target = a
                break
        if target is None:
            continue
        repl = BeautifulSoup(embed, "html.parser")
        parent = target.parent
        if (parent is not None and parent.name == "p"
                and parent.get_text(strip=True) == target.get_text(strip=True)):
            parent.replace_with(repl)  # link is the paragraph's sole content
        else:
            target.replace_with(repl)
        remaining.remove((canonical, embed))

    # Pass 2 — fill empty <p> placeholders that follow a heading.
    if remaining:
        empties = []
        for p in soup.find_all("p"):
            if p.get_text(strip=True) or p.find(["img", "iframe", "audio", "video", "figure"]):
                continue
            prev = p.find_previous_sibling()
            if prev is not None and prev.name in _HEADING_TAGS:
                empties.append(p)
        for p, (canonical, embed) in zip(empties, list(remaining), strict=False):
            p.replace_with(BeautifulSoup(embed, "html.parser"))
            remaining.remove((canonical, embed))

    out = str(soup)
    # Pass 3 — append leftovers.
    if remaining:
        out += "".join(f'<p class="lectio-embed">{e}</p>' for _, e in remaining)
    return out


def _inject_recovered_source_embeds(content_html, entry):
    """Recover media embeds from the source page when the feed body has none.

    Entries ingested before Lectio stopped stripping ``<iframe>`` at feed-parse
    time lost their YouTube/Bandcamp/SoundCloud players, and — unlike WordPress
    embed blocks — leave no placeholder figure to refill, so the feed-side
    recovery can't help. When the stored body carries no embed and the entry has a
    source link, fetch the page once (cached, SSRF-guarded via the lead-image
    source-HTML cache) and re-attach any allowlisted players it has, placed in
    context (see ``_place_recovered_embeds``) — mirroring the Reader-view
    ``_reinject_readability_embeds`` recovery."""
    link = (getattr(entry, "link", "") or "").strip()
    if not link:
        return content_html
    body = content_html if isinstance(content_html, str) else ""
    if "<iframe" in body.lower():
        return content_html  # already has an embed — nothing to recover
    cached = lead_image_service.get_cached_source_html(link)
    if cached is None:
        # Don't block the render on a network GET — queue a background fetch and
        # leave the body unchanged; the embed fills in on a later open. Many pages
        # are already cached by the lead-image scraper, so this often hits.
        lead_image_service.queue_source_html_fetch(link)
        return content_html
    _base, raw_html = cached
    items = _extract_source_embed_iframes(raw_html, body)
    if not items:
        return content_html
    return _place_recovered_embeds(body, items)


def _inject_source_gallery(content_html, entry, lead_image_url):
    """Append the source article's images to an image-less feed body (paizo blog).

    Extracted from get_entry_detail; the caller gates this on the per-feed
    inject_source_images pref + a present entry.link. Collects the source page's
    images (deduped against the lead and existing body images), priming the source
    HTML in the background and filling on a later open if it isn't cached yet."""
    _exclude_imgs: set[str] = set()
    if lead_image_url:
        _exclude_imgs.add(lead_image_url)
    for _m in re.finditer(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', content_html or "", re.IGNORECASE):
        _exclude_imgs.add(html.unescape(_m.group(1)))
    _gallery = lead_image_service.extract_source_gallery_urls(entry.link, exclude_urls=_exclude_imgs)
    if not _gallery:
        # Prime the source HTML in the background, wait briefly, then retry — so the
        # gallery fills on first open for fast sites, later otherwise.
        lead_image_service.queue_source_html_fetch(entry.link)
        lead_image_service.wait_for_source_html_fetch(entry.link, timeout=0.8)
        _gallery = lead_image_service.extract_source_gallery_urls(entry.link, exclude_urls=_exclude_imgs)
    if _gallery:
        _figs = "".join(
            f'<figure><img src="{html.escape(u, quote=True)}" loading="lazy" '
            f'referrerpolicy="no-referrer"></figure>'
            for u in _gallery
        )
        content_html = (content_html or "") + f'<div class="source-gallery">{_figs}</div>'
    return content_html


_CAPTION_IMG_TAG_RE = re.compile(r'<img\b[^>]*/?>', re.IGNORECASE | re.DOTALL)
_CAPTION_ATTR_RE = re.compile(
    r'\b(src|title)\s*=\s*(?:"([^"]*)"|\x27([^\x27]*)\x27|(\S+))', re.IGNORECASE
)
_TRIVIAL_ALT_TEXTS = frozenset({
    "responsive image", "image", "photo", "picture", "img", "thumbnail", "banner",
    "featured image", "previous", "next", "first", "last", "random", "prev",
    "newer", "older",
    # Social share-button / analytics-pixel alt text (AddToAny "Share", statcounter
    # "Web Analytics") — never a real photo caption.
    "share", "web analytics", "analytics",
})
_DECORATIVE_CAP_WORDS = frozenset({
    "banner", "header", "image", "cover", "featured", "photo", "thumbnail", "logo",
    "graphic", "artwork", "illustration",
})


def _initial_image_caption(content_html, entry, lead_image_url):
    """Find an initial caption (img title/alt) before opener stripping intactness is lost.

    Extracted from get_entry_detail. Prefers a ``title=`` on the img whose src matches
    the lead image (high confidence); otherwise accepts a title from a different img
    only when no separate lead image exists. Persisted alt/title (from a prior source
    scrape) override the in-feed title. Returns
    ``(image_title_text, in_feed_title_is_lead_img, persisted_alt, persisted_title)``."""
    image_title_text: str | None = None
    in_feed_title_is_lead_img = False
    for _search_html in (content_html, entry.summary):
        if not isinstance(_search_html, str):
            continue
        for _tag_m in _CAPTION_IMG_TAG_RE.finditer(_search_html):
            _tag_attrs: dict[str, str] = {}
            for _am in _CAPTION_ATTR_RE.finditer(_tag_m.group(0)):
                _k = _am.group(1).lower()
                _v = html.unescape((_am.group(2) or _am.group(3) or _am.group(4) or "").strip())
                if _k and _v:
                    _tag_attrs[_k] = _v
            _title_val = re.sub(r"<[^>]+>", "", html.unescape(_tag_attrs.get("title", ""))).strip()
            if not _title_val:
                continue
            _src_val = _tag_attrs.get("src", "")
            _matches_lead = bool(lead_image_url and _src_val and
                                 (_src_val in lead_image_url or lead_image_url in _src_val or
                                  _src_val.split("?")[0].rstrip("/") == lead_image_url.split("?")[0].rstrip("/")))
            if _matches_lead:
                image_title_text = _title_val
                in_feed_title_is_lead_img = True
                break
            if image_title_text is None and not lead_image_url:
                # Low-confidence title from a different image — only when no separate
                # lead image is known (else it'd belong to an unrelated inline img).
                image_title_text = _title_val
        if in_feed_title_is_lead_img:
            break

    # Persisted alt/title (from a prior background source-page scrape) wins over a
    # low-confidence in-feed title.
    persisted_alt = lead_image_service.get_entry_image_alt(str(entry.feed_url), str(entry.id))
    persisted_title = lead_image_service.get_entry_image_title(str(entry.feed_url), str(entry.id))
    if persisted_alt or persisted_title:
        image_title_text = persisted_title or persisted_alt  # title preferred
    return image_title_text, in_feed_title_is_lead_img, persisted_alt, persisted_title


def _suppress_junk_caption(image_title_text, entry):
    """Drop captions that aren't real photo captions: trivial alt text, date-only
    strings, or text that merely restates the article title (incl. auto-generated
    banner captions). Extracted from get_entry_detail; returns the caption or None."""
    if not image_title_text:
        return image_title_text
    if image_title_text.lower() in _TRIVIAL_ALT_TEXTS:
        return None
    if _DATE_ONLY_CAP_RE.match(image_title_text):
        return None
    if entry.title:
        _norm_cap = " ".join(re.sub(r"<[^>]+>", "", image_title_text).split()).lower()
        _norm_etitle = " ".join(html.unescape(str(entry.title)).split()).lower()
        if _norm_etitle and (
            _norm_cap == _norm_etitle
            or (len(_norm_etitle) > 20 and _norm_etitle in _norm_cap)
            or (len(_norm_cap) >= 8 and _norm_etitle.endswith(_norm_cap))
        ):
            return None
        # Auto-generated banner captions restate the title plus a decorative word
        # and/or a date — strip those tokens; if the remainder is wholly in the title,
        # the caption adds nothing. Only when the caption actually looked banner-like.
        _cap_tokens = re.findall(r"[a-z0-9]+", _norm_cap)
        _title_tokens = set(re.findall(r"[a-z0-9]+", _norm_etitle))
        _looks_banner_like = any(t in _DECORATIVE_CAP_WORDS for t in _cap_tokens) or bool(
            re.search(r"\b\d{4}\b|\b\d{1,2}[/.\-]\d{1,2}\b", _norm_cap)
        )
        _core_tokens = [t for t in _cap_tokens if t not in _DECORATIVE_CAP_WORDS and not t.isdigit()]
        if _looks_banner_like and _core_tokens and all(t in _title_tokens for t in _core_tokens):
            return None
    return image_title_text


def _apply_caption_source_pref(image_title_text, disp, entry, content_html):
    """Apply the per-feed caption_source preference (which raw attr to show).

    Extracted from get_entry_detail. "auto" keeps the computed caption (then runs the
    auto-suppress heuristic); none/alt/title/both override it from the persisted alt/
    title. Returns the final caption or None."""
    _caption_source = str(disp.get("caption_source") or "auto")
    _feed_url = str(entry.feed_url)
    _entry_id = str(entry.id)
    if _caption_source == "none":
        return None
    if _caption_source == "alt":
        return lead_image_service.get_entry_image_alt(_feed_url, _entry_id)
    if _caption_source == "title":
        return lead_image_service.get_entry_image_title(_feed_url, _entry_id)
    if _caption_source == "both":
        _ct = lead_image_service.get_entry_image_title(_feed_url, _entry_id)
        _ca = lead_image_service.get_entry_image_alt(_feed_url, _entry_id)
        return f"{_ct} — {_ca}" if (_ct and _ca and _ct != _ca) else (_ct or _ca)
    # "auto": keep the computed caption, but run the auto-suppress heuristic.
    if not should_show_caption(
        image_title_text, entry_title=entry.title, content_html=content_html,
        pref=int(disp.get("show_image_caption", -1)),
    ):
        return None
    return image_title_text


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
_TUMBLR_MEDIA_PREFIX_RE = re.compile(r"^(https://64\.media\.tumblr\.com/[^/]+/[^/]+)/", re.IGNORECASE)


def _strip_lead_image_opener(content_html, lead_image_url, feed_url: str, show_lead_in_article: bool):
    """Dedup the lead image against the article body. Returns (content_html, lead_image_url).

    Extracted from get_entry_detail. When the lead image is shown at the top:
    - if the body opens with that image, strip the opener (BS4 to remove empty
      ancestor containers cleanly; regex fallback when the opener is a *different*
      image, e.g. a thumbnail placeholder vs the full-size lead);
    - if the lead image appears later in the body, drop the separate lead (show it in
      place) — except artwork feeds, where it's hoisted to the top instead;
    - Tumblr size-variant dedup (same media hash, different size suffix);
    - when the lead came from source scraping and the body is just a thumbnail
      wrapper (minimal text), strip the inline imgs."""
    if not (show_lead_in_article and lead_image_url and isinstance(content_html, str)):
        return content_html, lead_image_url

    _m = _LEAD_IMG_OPENER_RE.match(content_html)
    if _m:
        # BS4 removes the opener <img> and its now-empty ancestor containers without
        # touching sibling figures or anchored link text ("New comic!").
        _bs4_stripped = _bs4_strip_opener(content_html, lead_image_url)
        if _bs4_stripped is not None:
            content_html = _bs4_stripped or None
            if content_html:
                content_html = re.sub(
                    r"^(?:\s*(?:<p\b[^>]*>\s*(?:&nbsp;\s*)*</p>|<br\s*/?>\s*))+",
                    "", content_html, flags=re.IGNORECASE,
                ).strip() or None
        else:
            # lead_image_url isn't in the opener's <img> src — the opener is a
            # different image (e.g. a comicsthumbs placeholder) while lead_image_url
            # is the full-size source image. Raw-strip the opener; restore any
            # anchored "New comic!" link text.
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
        _entry_strategy, _, _ = lead_image_service.get_feed_strategy(feed_url)
        if _entry_strategy == "artwork":
            # Artwork mode (e.g. ArtStation): the image follows the description —
            # hoist it to the top by stripping it from its position in the content.
            _bs4_stripped = _bs4_strip_opener(content_html, lead_image_url)
            if _bs4_stripped is not None:
                content_html = _bs4_stripped or None
            else:
                lead_image_url = None
        else:
            # Lead URL is buried mid-article (author placed it there) — show it in
            # its natural position, not as a separate top lead.
            lead_image_url = None

    # Tumblr CDN size-variant dedup: the cached lead (s1280x1920) and the in-content
    # photo (s640x960) share the {media_hash}/{token} prefix but differ by size, so
    # string equality above misses it.
    if show_lead_in_article and lead_image_url and isinstance(content_html, str):
        _tumblr_prefix_m = _TUMBLR_MEDIA_PREFIX_RE.match(lead_image_url)
        if _tumblr_prefix_m:
            _tumblr_prefix = _tumblr_prefix_m.group(1) + "/"
            if _tumblr_prefix in content_html or _tumblr_prefix in html.unescape(content_html):
                lead_image_url = None

    # Source-scraped lead + body that's essentially just a thumbnail wrapper (minimal
    # text) → strip the inline imgs so a small thumb doesn't sit below the full lead.
    if show_lead_in_article and lead_image_url and isinstance(content_html, str) and lead_image_url not in content_html:
        _remaining_imgs = len(re.findall(r"<img\b", content_html, re.IGNORECASE))
        if _remaining_imgs <= 1:
            # Tumblr guard: a remaining image with a different media prefix is a
            # genuine second photo — keep it.
            _skip_strip = False
            if _remaining_imgs == 1:
                _tumblr_lead_m = _TUMBLR_MEDIA_PREFIX_RE.match(lead_image_url)
                if _tumblr_lead_m:
                    _rem_src_m = re.search(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', content_html, re.IGNORECASE)
                    if _rem_src_m:
                        _tumblr_rem_m = _TUMBLR_MEDIA_PREFIX_RE.match(html.unescape(_rem_src_m.group(1)))
                        if _tumblr_rem_m and _tumblr_rem_m.group(1) != _tumblr_lead_m.group(1):
                            _skip_strip = True
            if not _skip_strip:
                _no_imgs = re.sub(r"<img\b[^>]*/?>", "", content_html, flags=re.IGNORECASE)
                _text_only = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _no_imgs))).strip()
                if len(_text_only) < 120:
                    content_html = _no_imgs.strip() or None
    return content_html, lead_image_url


def _resolve_article_lead_image(entry, video_id, show_lead_in_article: bool):
    """Resolve the article's lead image and whether it's still pending.

    Extracted from get_entry_detail. Derives the lead image by feed strategy,
    discards avatar/headshot images, and — for a never-processed entry with no
    inline image — kicks off a background source-page fetch (waiting ≤0.8s so fast
    sites fill on first open). Returns ``(lead_image_url, pending)``; both are None/
    False when a YouTube embed was injected (the player replaces the lead image)."""
    lead_image_url = _derive_article_lead_image(entry)
    # Discard avatar/portrait images (author headshots, profile pics) that some
    # feeds embed as the first image; prefer no image over a face. Check path only —
    # CDN domains like "googleusercontent.com" contain "user" and would
    # false-positive on a full-URL search.
    if lead_image_url:
        _lead_parsed = urlparse(lead_image_url)
        if lead_image_service._AVATAR_HINT_PATTERNS.search(_lead_parsed.path):
            lead_image_url = None
    # Try source scraping when the entry has never been processed (ABSENT cache) and
    # the feed provides no inline image — covers article-only feeds where the best
    # image lives on the source page. (A stored None is left alone: it conflates
    # auto-discovered "none" with a user-cleared image; the background job retries
    # negatives on its own schedule.)
    _cache_key = (str(entry.feed_url), str(entry.id))
    _cached_val = lead_image_service._cache.get(_cache_key, "ABSENT")
    _should_source_fetch = (
        _cached_val == "ABSENT"
        and lead_image_url is None
        and not lead_image_service._is_feed_none_strategy(str(entry.feed_url))
    )
    pending = False
    if _should_source_fetch and entry.link:
        # Queue the source-page fetch in the background, then wait briefly so a
        # fast-responding site fills the image on the very first open without a
        # polling round-trip. Cap at 0.8s — slow hosts (Squarespace, WordPress.com)
        # exceed it and fill in on the next open instead.
        lead_image_service.queue_source_fetch(str(entry.feed_url), str(entry.id), entry.link)
        _fetch_t0 = time.monotonic()
        lead_image_service.wait_for_source_fetch(str(entry.feed_url), str(entry.id), timeout=0.8)
        _fetch_ms = int((time.monotonic() - _fetch_t0) * 1000)
        LOGGER.info("[perf] entry_detail: source_fetch_wait=%dms %s", _fetch_ms, entry.link)
        lead_image_url = lead_image_service.extract_entry_thumbnail_url(entry, include_source_lookup=False)
        pending = (lead_image_url is None) and show_lead_in_article and not video_id
    # A YouTube embed already shows the video; a separate lead image (its thumbnail)
    # above the player would be redundant.
    if video_id:
        return None, False
    return lead_image_url, pending


def _apply_entry_media(content_html, entry, feed_url: str, entry_id: str):
    """Prepend the podcast audio player and append footer attachments to an entry.

    Extracted from get_entry_detail. Returns ``(content_html, audio_feed_suggestion)``:
    - injects an ``<audio>`` player (pointed at /entries/media/audio so expired signed
      URLs refresh server-side) when the entry has playable audio and none inline;
    - appends a download list for non-audio/non-image enclosures;
    - when there's no playable audio, surfaces a separate podcast-host feed to
      subscribe to (if discovered and not already subscribed)."""
    audio_feed_suggestion: str | None = None
    with get_meta_connection() as _mconn:
        audio_url = _resolve_entry_audio_url(_mconn, feed_url, entry_id, entry)
        if not audio_url:
            _sugg = _get_suggested_audio_feed(_mconn, feed_url)
            if _sugg and not _is_feed_subscribed(_mconn, _sugg):
                audio_feed_suggestion = _sugg
    if audio_url and (not isinstance(content_html, str) or "<audio" not in content_html.lower()):
        _safe_feed_url = quote_plus(feed_url)
        _safe_entry_id = quote_plus(entry_id)
        _media_play_url = f"/entries/media/audio?feed_url={_safe_feed_url}&entry_id={_safe_entry_id}"
        _media_dl_url = f"/entries/media/download?feed_url={_safe_feed_url}&entry_id={_safe_entry_id}"
        _audio_player = (
            f'<div class="podcast-player" style="margin:1em 0;">'
            f'<audio controls preload="metadata" style="width:100%" src="{_media_play_url}"></audio>'
            f'<div style="margin-top:6px; font-size:0.85em;">'
            f'<a href="{_media_dl_url}" download>Download audio</a>'
            f'</div>'
            f'</div>'
        )
        content_html = _audio_player + (content_html or "")

    # Footer attachments — non-audio enclosures (magazine PDFs, EPUBs, etc.) that
    # never appear in the article body. The audio enclosure, if any, is already
    # shown as a player above, so it's excluded here.
    _attachments_html = _render_entry_attachments(entry, audio_url)
    if _attachments_html:
        content_html = (content_html or "") + _attachments_html
    return content_html, audio_feed_suggestion


def _resolve_entry_content_html(entry):
    """Resolve an entry's display HTML from its content/summary.

    Extracted from get_entry_detail: prefers HTML content, falls back to BBCode
    conversion (Nexus Mods), promotes bare-text/escaped-plaintext summaries
    (orpheus.network) to real HTML, and repairs URL-encoded ``http%3A/`` schemes
    the reader library mangles into relative paths. Returns the HTML or None."""
    content = entry.get_content(prefer_summary=False)
    content_html = None
    if content and content.value and content.is_html:
        content_html = content.value
    # BBCode fallback: feeds that emit BBCode markup instead of HTML
    # (e.g. Nexus Mods).  feedparser marks description as text/html but
    # the value has [b]...[/b] brackets.  Also handles is_html=False text.
    if content and content.value:
        _cv = content_html or content.value
        if _looks_like_bbcode(_cv):
            content_html = _bbcode_to_html(content_html if content_html else content.value)

    # Bare-text feeds (no HTML content): promote a URL/break-bearing plain-text
    # summary to content_html so links work and breaks render, instead of the
    # unstyled <pre> fallback. Returns None for genuinely plain prose, which
    # keeps the <pre> path. Runs before the <br>->paragraph pipeline below so a
    # promoted summary benefits from it too.
    if not content_html:
        content_html = _promote_plaintext_summary(getattr(entry, "summary", None))
    elif _looks_like_escaped_plaintext(content_html):
        # Some feeds (e.g. orpheus.network news) declare their content as
        # text/html but actually ship escaped plain text — literal
        # ``&lt;br&gt;`` breaks, bare URLs, and double-escaped ``&amp;amp;``
        # ampersands — so it renders as inert escaped text. Promote it the
        # same way as a bare-text summary.
        content_html = _promote_plaintext_summary(content_html) or content_html

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
    return content_html


_COMICSTHUMBS_IMG_SRC_RE = re.compile(
    r'(<img\b[^>]*?\bsrc=["\'])([^"\']*?/comicsthumbs/[^"\']+)(["\'])', re.IGNORECASE
)
# ComicControl filenames carry a cache-bust unix-timestamp prefix, e.g.
# "1782426356-ARV1701_05.jpg". The stable part is everything after it.
_COMICCONTROL_TS_PREFIX_RE = re.compile(r'^\d{6,}-')


def _comiccontrol_stable_name(url: str) -> str:
    """Return a ComicControl image's filename with its cache-bust timestamp
    prefix removed (e.g. '…/1782426356-ARV1701_05.jpg' -> 'ARV1701_05.jpg')."""
    fname = url.rsplit("/", 1)[-1].split("?", 1)[0]
    return _COMICCONTROL_TS_PREFIX_RE.sub("", fname)


def _promote_comicsthumbs_in_content(content_html: str, full_lead_url: str | None) -> str:
    """Promote inline ComicControl /comicsthumbs/ images to the full panel.

    ComicControl gives the thumbnail and the full panel DIFFERENT cache-bust
    timestamp prefixes (e.g. comicsthumbs/1782426356-X.jpg vs comics/1782426355-X.jpg),
    so a naive /comicsthumbs/ -> /comics/ swap keeps the thumb's timestamp — and
    ComicControl answers that nonexistent timestamp with a 200 *HTML* page, not the
    image, breaking the comic. When the resolved lead image (the real /comics/<ts>-<file>
    read from the page) shares the same timestamp-stripped filename, substitute it
    directly. Otherwise fall back to the directory swap (correct whenever the thumb
    and panel happen to share a timestamp)."""
    lead_name = (
        _comiccontrol_stable_name(full_lead_url)
        if full_lead_url and "/comics/" in full_lead_url
        else None
    )

    def _sub(m: re.Match) -> str:
        src = m.group(2)
        if lead_name and _comiccontrol_stable_name(src).lower() == lead_name.lower():
            return f"{m.group(1)}{full_lead_url}{m.group(3)}"
        swapped = re.sub(r'(?<=/)comicsthumbs(?=/)', "comics", src, flags=re.IGNORECASE)
        return f"{m.group(1)}{swapped}{m.group(3)}"

    return _COMICSTHUMBS_IMG_SRC_RE.sub(_sub, content_html)


def _apply_feed_content_cleanups(content_html, feed_url: str, entry_id: str):
    """Apply the per-site / generic feed-content cleanups to an entry's HTML.

    Extracted from get_entry_detail; operates on content_html only (returning the
    cleaned value) so the render path reads as a sequence of named stages. Covers:
    NASA leading-nav strip, mynorthwest "RELATED STORIES" block, Ghost kg-audio
    cards, the WordPress "appeared first on" footer, qwantz nav tables, sanitized
    embed-container iframes, and recovery of stripped YouTube embeds."""
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

    # Tidy the WordPress "The post … appeared first on …" footer: keep one,
    # drop plugin duplicates, and clean the double-encoded literal "<p>" tags
    # some feeds emit. Generic across feeds.
    if isinstance(content_html, str):
        content_html = _fix_wp_post_footer(content_html)

    # Dinosaur Comics (qwantz): strip the nav tables wrapping the comic and
    # keep only the comic image + dated author commentary.
    if isinstance(content_html, str) and "qwantz.com" in feed_url:
        content_html = _clean_qwantz_content(content_html)

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
            return _youtube_embed_html(vid)
        content_html = re.sub(
            r'<div[^>]*class=["\']embed-container["\'][^>]*>\s*<strong>iframe</strong>\s*</div>'
            r'\s*<a[^>]+href=["\']'
            r'(https?://(?:www\.)?(?:youtube\.com/watch\?[^"\'<\s]+|youtu\.be/[^"\'<\s]+))'
            r'["\'][^>]*>[^<]*</a>',
            _replace_bad_iframe,
            content_html,
            flags=re.IGNORECASE,
        )

    # Recover stripped YouTube embeds — feedparser removes the <iframe> from
    # WordPress YouTube blocks, leaving an empty figure with the video id
    # gone. Re-parse of the raw feed (sanitize off) caches the ids; inject
    # the player when cached, else queue a scan so it fills in next open.
    # Only needed for entries stored *before* feed ingest stopped stripping
    # iframes (services.reader_sanitize) — newer entries keep the real embed,
    # so skip recovery when an <iframe> is already present.
    if (isinstance(content_html, str)
            and _YT_EMBED_PLACEHOLDER_RE.search(content_html)
            and "<iframe" not in content_html.lower()):
        with get_meta_connection() as _vconn:
            _vids = _lookup_media_video(_vconn, feed_url, entry_id)
        if _vids:
            content_html = _inject_recovered_youtube_embeds(content_html, _vids)
        elif _vids is None:
            # Not scanned yet — force a scan (bypasses the audio TTL so a new
            # post's embed isn't held back by a long no-audio backoff).
            _queue_media_audio_scan(feed_url)

    # ComicControl webcomics (atomic-robo, everblue, …) embed only the small
    # /comicsthumbs/ image in feed content; the full-resolution panel is the
    # same filename under /comics/. Promote inline so the reader shows the
    # readable comic, not the thumbnail.
    if isinstance(content_html, str) and "comicsthumbs" in content_html.lower():
        _full_lead = lead_image_service.get_cached_lead_image_url(feed_url, entry_id)
        content_html = _promote_comicsthumbs_in_content(content_html, _full_lead)

    # Standalone bare YouTube links (own paragraph / lone anchor) → inline player.
    # Covers feeds where the oEmbed iframe was stripped and only the link remains.
    if isinstance(content_html, str):
        content_html = _embed_standalone_youtube_links(content_html)

    # Standalone bare Bandcamp album/track links → embed. Cache-first: resolves
    # immediately when the album page HTML is already cached; otherwise queues a
    # background fetch so the embed appears on the next open.
    if isinstance(content_html, str):
        content_html = _embed_standalone_bandcamp_links(content_html)

    # Apply the per-user YouTube embed-host preference to feed-native players
    # (the recovered/injected player above already uses youtube_embed_host()).
    if isinstance(content_html, str):
        content_html = _apply_youtube_embed_host(content_html)

    return content_html


def get_entry_detail(feed_url: str, entry_id: str) -> dict | None:
    _t0 = time.monotonic()
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            return _build_orphan_entry_detail(feed_url, entry_id)
        _reader_get_ms = int((time.monotonic() - _t0) * 1000)
        if _reader_get_ms > 200:
            LOGGER.info("[perf] entry_detail: reader.get_entry=%dms %s", _reader_get_ms, entry_id)

        published_dt = entry.published or entry.updated or entry.added
        author_name = (getattr(entry, "authors_str", None) or "").strip() or None

        content_html = _resolve_entry_content_html(entry)

        # Per-site / generic feed-content cleanups (NASA nav, mynorthwest related
        # block, Ghost audio cards, WordPress footer, qwantz nav, embed-container
        # iframes, recovered YouTube embeds). Operates on content_html only.
        content_html = _apply_feed_content_cleanups(content_html, feed_url, entry_id)

        # Recover media embeds (YouTube/Bandcamp/SoundCloud/…) from the source
        # page for older entries whose <iframe> was stripped at ingest and left no
        # placeholder to refill. Skips when the body already has an embed; fetch is
        # cached and SSRF-guarded. Runs before the YouTube-feed injection below so
        # that path still wins for native YouTube feeds.
        if not (isinstance(feed_url, str)
                and feed_url.startswith("https://www.youtube.com/feeds/videos.xml?")):
            content_html = _inject_recovered_source_embeds(content_html, entry)

        # Bandcamp single-track esig players are domain-locked to the publisher and
        # show "not available" in Lectio; fall back to the album player. Covers
        # both feed-native and source-recovered embeds.
        content_html = _strip_bandcamp_track_signature(content_html)

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
            if video_id and ("/embed/" not in str(base_html)):
                embed_html = _youtube_embed_html(video_id)
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

        # Podcast audio player + footer attachments + (when no audio) a suggestion
        # to subscribe to a separate podcast-host feed.
        content_html, _audio_feed_suggestion = _apply_entry_media(content_html, entry, feed_url, entry_id)

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

        with get_meta_connection() as _prefs_conn:
            _disp = get_feed_display_prefs(_prefs_conn, str(entry.feed_url))
        _show_lead_in_article = bool(_disp.get("show_lead_image_in_article", 1))

        lead_image_url, _pending_lead_image = _resolve_article_lead_image(
            entry, video_id, _show_lead_in_article
        )

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

        # Webcomic nav button cleanup — strip <a><img alt="Previous/Next/..."></a> combos
        # that comic feeds embed before and after the strip image for in-site navigation.
        if isinstance(content_html, str) and _COMIC_NAV_ALT_RE.search(content_html):
            content_html = _strip_comic_nav_images(content_html) or None

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
                content_html = re.sub(r"<source\b[^>]*/?>", "", content_html or "", flags=re.IGNORECASE) or None

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

        image_title_text, _in_feed_title_is_lead_img, _persisted_alt, _persisted_title = (
            _initial_image_caption(content_html, entry, lead_image_url)
        )

        # Strip the opener thumbnail and dedup against the remaining content.
        # Only when we will actually display the lead image at the top — if
        # show_lead_image_in_article is off the image should stay in place.
        # Order matters: strip the leading <img> first, then check if the lead
        # image URL still appears in what remains.  This prevents the case where
        # the lead image IS the opener thumbnail (e.g. comicsthumbs) from being
        # incorrectly suppressed just because it appears at the top of content.
        content_html, lead_image_url = _strip_lead_image_opener(
            content_html, lead_image_url, str(entry.feed_url), _show_lead_in_article
        )

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
                                (not _in_feed_title_is_lead_img and not _persisted_alt and not _persisted_title))
        _is_wc_feed = lead_image_service._is_feed_webcomic(str(entry.feed_url))
        if _needs_source_scrape and lead_image_url and entry.link:
            if entry.link in lead_image_service._source_html_cache:
                _fa, _ft = lead_image_service.fetch_entry_image_caption(entry.link, lead_image_url=lead_image_url, is_webcomic=_is_wc_feed)
                _fa = (re.sub(r"<[^>]+>", "", _fa).strip() or None) if _fa else None
                _ft = (re.sub(r"<[^>]+>", "", _ft).strip() or None) if _ft else None
                if _fa or _ft:
                    image_title_text = _ft or _fa  # title preferred for auto display
                    lead_image_service.persist_image_alt_async(str(entry.feed_url), str(entry.id), _fa, title_text=_ft)
            else:
                # Source HTML isn't cached — fetching it is a slow network GET (several
                # seconds for some og_scrape hosts, e.g. mynorthwest). Do it fully in
                # the background: queue_source_html_fetch (given feed_url/entry_id/
                # lead_image_url) fetches the page AND extracts + persists the caption,
                # so it appears on the next open from the DB. Previously the render
                # thread blocked up to 3s here waiting for that fetch just to maybe show
                # the caption on this first open — a big first-open stall for no durable
                # benefit. Cache-first / defer, matching the async lead-image fetch.
                lead_image_service.queue_source_html_fetch(
                    entry.link,
                    feed_url=str(entry.feed_url),
                    entry_id=str(entry.id),
                    lead_image_url=lead_image_url,
                )

        # Drop trivially generic alt texts that add no information (e.g. Bootstrap
        # class names used as alt values, single-word placeholders, or navigation
        # labels from wrongly-selected nav thumbnails like "Previous" / "Next").
        image_title_text = _suppress_junk_caption(image_title_text, entry)

        # Inject image_title_text as alt attribute on the first <img> in content_html
        # and insert a caption <p> immediately after it so it appears inline under
        # the image rather than at the bottom of the article.
        if image_title_text and not lead_image_url and isinstance(content_html, str):
            _caption_injected = False

            _title_str: str = image_title_text  # narrowed by outer `if image_title_text`

            def _inject_alt(m: re.Match) -> str:
                nonlocal _caption_injected
                tag = m.group(0)
                if re.search(r"\balt\s*=", tag, re.IGNORECASE):
                    tag = re.sub(
                        r'(\balt\s*=\s*)(?:"[^"]*"|\x27[^\x27]*\x27)',
                        lambda a: a.group(1) + '"' + _title_str.replace('"', "&quot;") + '"',
                        tag,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                else:
                    tag = tag[:-1] + ' alt="' + _title_str.replace('"', "&quot;") + '"' + tag[-1]
                _caption_injected = True
                caption = f'<p class="entry-image-title-text">{html.escape(_title_str)}</p>'
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

        # Persist off the request thread (and skip when unchanged) so an open
        # never blocks on the meta-DB writer held by the background backfill.
        # Skip for webcomic feeds: their cached lead is the single-pane source
        # preview used as the list thumbnail, while the article shows the full
        # inline strip — persisting the article's full image here would clobber
        # the preview thumbnail. The webcomic source-scrape owns that cache.
        _persist_strategy, _, _ = lead_image_service.get_feed_strategy(str(entry.feed_url))
        if _persist_strategy != "webcomic":
            lead_image_service.persist_lead_image_async(str(entry.feed_url), str(entry.id), lead_image_url)

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

        # Source-page image gallery (opt-in per feed) — runs before the hotlink/
        # no-referrer pass below so injected images are proxied/referrer-stripped too.
        if _disp.get("inject_source_images") and entry.link:
            content_html = _inject_source_gallery(content_html, entry, lead_image_url)

        # Suppress the Referer on inline body images so hotlink-protected hosts
        # serve the real image instead of a placeholder, and route known
        # hotlink hosts (e.g. nanolx.org) through the /api/img proxy so a
        # browser-cached placeholder under the original URL is bypassed. Skip
        # locally-served starred assets (same-origin, no hotlink concern).
        if isinstance(content_html, str) and content_html and not is_saved:
            content_html = proxy_hotlink_images(content_html)
            content_html = add_no_referrer_to_images(content_html)

        if not _show_lead_in_article:
            lead_image_url = None

        image_title_text = _apply_caption_source_pref(image_title_text, _disp, entry, content_html)

        _channel_link = getattr(entry.feed, "link", None) if hasattr(entry, "feed") else None
        _display_link = _rebase_proxy_entry_link(entry.link or _derived_entry_link(entry), feed_url, _channel_link)

        # Suppress summaries that consist entirely of img tags with no text (e.g. xkcd,
        # Deathbulge).  After the lead image is shown above the content, rendering the
        # raw <img> tag in a <pre> block would show it as literal HTML text.
        _summary = entry.summary
        if isinstance(_summary, str):
            # Dinosaur Comics: strip site-nav table and prev/next row from the summary
            if "qwantz.com" in (entry.link or ""):
                _summary = _strip_qwantz_nav(_summary)
            # Webcomic nav button images in summary (same as content_html path above)
            if _COMIC_NAV_ALT_RE.search(_summary):
                _summary = _strip_comic_nav_images(_summary)
            _summary_no_imgs = re.sub(r"<img\b[^>]*/?>", "", _summary, flags=re.IGNORECASE).strip()
            _summary_text_only = re.sub(r"<[^>]+>", " ", _summary_no_imgs)
            _summary_text_only = html.unescape(re.sub(r"\s+", " ", _summary_text_only)).strip()
            if not _summary_text_only:
                _summary = None

        _total_ms = int((time.monotonic() - _t0) * 1000)
        if _total_ms > 500:
            LOGGER.info("[perf] entry_detail: total=%dms %s", _total_ms, entry_id)
        return {
            "feed_url": entry.feed_url,
            "id": entry.id,
            "title": _display_title(entry) or entry.title,
            "link": _display_link,
            "summary": _summary,
            "content_html": content_html,
            "lead_image_url": _lead_image_display_url(lead_image_url),
            "show_lead_in_article": _show_lead_in_article,
            "show_as_thumb": bool(_disp.get("show_lead_image_as_thumb", 1)) and not _disp.get("feed_thumbnail_url"),
            # Webcomic feeds show the FULL strip in the article but keep the
            # single-pane scraped preview as the list thumbnail — so don't let the
            # client sync the list row's thumbnail to the article lead image on open.
            "sync_list_thumb": _persist_strategy != "webcomic",
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
            "audio_feed_suggestion": _audio_feed_suggestion,
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

    _pane_t0 = time.monotonic()
    selected_entry = get_entry_detail(feed_url, entry_id)
    _detail_ms = int((time.monotonic() - _pane_t0) * 1000)
    if _detail_ms > 500:
        LOGGER.info("[perf] entry_pane: get_entry_detail=%dms feed=%s", _detail_ms, feed_url)
    if selected_entry and not selected_entry["read"]:
        selected_entry["read"] = True
        _fu, _eid = feed_url, entry_id
        _title = str(selected_entry.get("title") or "")
        _link = str(selected_entry.get("link") or "")
        _feed_title = str(selected_entry.get("feed_title") or "")
        # Capture the current user now: the daemon thread below does not inherit
        # this request's contextvars, so without re-binding it would mark the
        # entry read in the default (legacy) user's DB and the post would keep
        # showing as unread for the actual user.
        _uid = tenancy.current_user_id()

        def _bg_mark_read() -> None:
            try:
                with get_reader() as reader:
                    reader.mark_entry_as_read((_fu, _eid))
            except Exception:
                LOGGER.warning("background mark_entry_as_read failed for %s/%s", _fu, _eid, exc_info=True)
            try:
                upsert_entry_read_state(_fu, _eid)
            except Exception:
                LOGGER.warning("background upsert_entry_read_state failed for %s/%s", _fu, _eid, exc_info=True)
            try:
                append_read_history(_fu, _eid, _title, _link, _feed_title)
            except Exception:
                LOGGER.warning("background append_read_history failed for %s/%s", _fu, _eid, exc_info=True)
            with unread_counts_cache_lock:
                global _unread_counts_generation
                _unread_counts_generation += 1
                unread_counts_cache.clear()

        threading.Thread(target=_run_in_user_context, args=(_uid, _bg_mark_read), daemon=True).start()

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
            "pinterest_connected": pinterest_oauth_connected(),
            "quire_configured": is_quire_configured(),
            "reddit_connected": reddit_connected(),
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


_DOMAIN_ALIASES: dict[str, str] = {
    # old.reddit.com and www.reddit.com serve identical RSS content
    "old.reddit.com": "www.reddit.com",
    # Tapastic rebranded to Tapas; the old domain's feeds 404. Same path structure
    # (/rss/series/<id>), so rewriting the host keeps existing subscriptions working.
    "tapastic.com": "tapas.io",
    "www.tapastic.com": "tapas.io",
}


def normalize_feed_url(feed_url: str) -> str:
    """Normalize a feed URL for consistent storage and deduplication.

    - Strips trailing slashes from paths longer than "/".
    - Strips format-selector query params (e.g. Blogger's ?alt=rss) that
      select a serialization format without changing the feed content, so
      the Atom and RSS variants of the same Blogger feed are treated as one.
    - Rewrites ArtStation subdomain feeds (username.artstation.com/rss) to the
      main-domain form (www.artstation.com/username.rss) which works for all
      usernames including those with underscores that fail TLS hostname validation.
    - Rewrites known domain aliases (e.g. old.reddit.com → www.reddit.com).
    - Other normalization (YouTube links) is handled separately.
    """
    import re as _re
    _as_m = _re.match(r'(https?)://([^.]+)\.artstation\.com/rss$', feed_url.strip(), _re.IGNORECASE)
    if _as_m:
        feed_url = f'{_as_m.group(1)}://www.artstation.com/{_as_m.group(2)}.rss'
    try:
        parsed = urlparse(feed_url)
        if parsed.netloc.lower() in _DOMAIN_ALIASES:
            feed_url = parsed._replace(netloc=_DOMAIN_ALIASES[parsed.netloc.lower()]).geturl()
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

    # Case-normalize scheme + host only. Per RFC 3986 the scheme and host are
    # case-insensitive, but the path and query ARE case-sensitive (e.g. YouTube
    # channel IDs, signed/base64 tokens, Reddit /user/Name), so they're left
    # untouched. Userinfo (rare in feeds) is also case-sensitive — preserved.
    try:
        parsed = urlparse(feed_url)
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, hostport = netloc.rsplit("@", 1)
            new_netloc = userinfo + "@" + hostport.lower()
        else:
            new_netloc = netloc.lower()
        if parsed.scheme != parsed.scheme.lower() or netloc != new_netloc:
            feed_url = parsed._replace(scheme=parsed.scheme.lower(), netloc=new_netloc).geturl()
    except Exception:
        pass
    return feed_url


def canonical_feed_url(raw_url: str) -> str:
    """Canonical form of a feed URL for storage/dedupe at import time.

    Composes the same normalization the interactive Add-Feed path uses
    (YouTube channel canonicalization + ``normalize_feed_url``: host aliases
    like old.reddit→www.reddit, format-selector params, trailing slashes,
    scheme/host case). Importers must run every incoming feed URL through this
    so a variant (old.reddit, a trailing ``?``, ``alt=rss``) attaches to an
    existing subscription instead of creating a duplicate: ``reader.add_feed``
    with ``exist_ok=True`` then merges into the existing feed, and any
    per-entry tags/stars key off the same canonical URL. Returns the stripped
    input unchanged if normalization fails.
    """
    url = (raw_url or "").strip()
    if not url:
        return url
    try:
        url = normalize_youtube_feed_url(url)
    except Exception:
        pass
    try:
        url = normalize_feed_url(url)
    except Exception:
        pass
    return url


def _canonicalize_item_feed_urls(items: list[dict]) -> None:
    """Rewrite each item's ``feed_url`` to its canonical form, in place.

    Importers that key both feed subscription and per-entry tag/star state off
    ``item["feed_url"]`` call this once up front so every downstream use shares
    the canonical value — variants (old.reddit, ``?alt=rss``, trailing slash)
    then merge into an existing subscription instead of spawning a duplicate.
    """
    for item in items:
        furl = item.get("feed_url") or ""
        if furl:
            item["feed_url"] = canonical_feed_url(furl)


def _is_subscribable_feed_url(url: str) -> bool:
    """True only for http(s) feed URLs supplied by users.

    The ``reader`` library natively fetches ``file://`` (used internally for
    scraped feeds), so an unrestricted user-supplied URL — via Add Feed, a
    discovered ``<link>``, or an OPML ``xmlUrl`` — could read arbitrary local
    files (``file:///etc/passwd``, another tenant's DB, ``.env``) on refresh.
    Restrict user-facing subscription to http/https; internal scraped feeds add
    their ``file://`` URLs through ``reader.add_feed`` directly, not here.
    """
    try:
        return urlparse(url).scheme in ("http", "https")
    except ValueError:
        return False


def add_feed_to_folder(feed_url: str, folder_id: int) -> None:
    feed_url = feed_url.strip()
    if not feed_url:
        raise ValueError("Feed URL is required.")
    if not _is_subscribable_feed_url(feed_url):
        raise ValueError("Only http:// and https:// feed URLs can be added.")

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
    if websub_service:
        _uid = tenancy.current_user_id()
        threading.Thread(
            target=websub_service._discover_and_subscribe,
            args=(feed_url, _uid),
            daemon=True,
        ).start()


def purge_orphaned_feed(
    reader,
    conn: sqlite3.Connection,
    feed_url: str,
    *,
    archive_pending: bool = True,
    rescue_to: str | None = None,
) -> int:
    """Delete a feed that has already been confirmed orphaned (no folder_feeds rows).

    Must be called AFTER the caller has removed all folder_feeds rows and
    confirmed the feed is no longer referenced.  Does NOT call
    ``invalidate_meta_structure_cache`` — the caller is responsible.

    Steps (in order):
    1. Force-archive any pending saved/starred entries so they don't become
       content-less archive shells after the reader-side data disappears.
       Skipped when *archive_pending* is False (e.g. dedup/upgrade where entries
       survive under the kept/canonical feed).
    2. Rescue unread entries into the kept feed when *rescue_to* is given (dedup
       and format-upgrade paths only).
    3. Delete the feed via the appropriate path:
       - DeviantArt rendered feed → deviantart_service.delete_deviantart_feed
       - Scraped (file://) feed   → scraper_service.delete_scraped_feed
       - Plain feed               → reader.delete_feed
    4. Unsubscribe from the WebSub hub (best-effort; guard for disabled websub).

    Parameters
    ----------
    reader:
        An already-open reader instance (from ``get_reader()``).
    conn:
        An already-open meta-DB connection (from ``get_meta_connection()``).
    feed_url:
        The URL of the feed to remove.
    archive_pending:
        When True (default), force-flush any pending starred-archive captures
        for this feed before deletion.
    rescue_to:
        When set, mark read entries in *rescue_to* as unread when the removed
        feed had them unread (slug-matched).  Used by dedup/upgrade paths.

    Returns
    -------
    int
        The number of entries rescued into *rescue_to* (0 when *rescue_to* is
        None).  Dedup callers accumulate this into their JSON ``rescued_count``.
    """
    # Step 1 — force-archive pending saves.
    if archive_pending:
        try:
            forced = starred_archive_service.force_archive_pending_for_feed(feed_url)
            if forced:
                LOGGER.info("[purge] force-archived %d pending captures for %s", forced, feed_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[purge] force-archive failed for %s: %s", feed_url, exc)

    # Step 2 — rescue unread entries into the surviving feed.
    rescued = _rescue_unread_entries(reader, feed_url, rescue_to) if rescue_to else 0

    # Step 3 — dispatch the delete via the appropriate path.
    da_id = deviantart_service.deviantart_feed_id_from_url(feed_url)
    feed_id = None if da_id else scraper_service.scraped_feed_id_from_url(feed_url)
    if da_id:
        deviantart_service.delete_deviantart_feed(conn, reader, da_id)
    elif feed_id:
        scraper_service.delete_scraped_feed(conn, reader, feed_id)
    else:
        reader.delete_feed(feed_url, missing_ok=True)

    # Step 4 — WebSub unsubscribe (best-effort; websub_service may be None).
    if websub_service:
        websub_service.unsubscribe(feed_url, tenancy.current_user_id())

    return rescued


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
        with get_reader() as reader:
            with get_meta_connection() as conn:
                purge_orphaned_feed(reader, conn, feed_url, archive_pending=True)
    invalidate_meta_structure_cache()


def get_push_active_feed_urls() -> set[str]:
    """Return the set of feed URLs with a verified, active WebSub push subscription.

    Queries the shared (non-per-user) websub DB. Returns an empty set when
    WebSub is disabled (LECTIO_PUBLIC_URL not set) or the table is empty.
    Only feeds with verified=1 AND hub_url IS NOT NULL are included.
    """
    if not websub_service:
        return set()
    try:
        conn = get_websub_connection()
        rows = conn.execute(
            "SELECT feed_url FROM websub_subscriptions"
            " WHERE verified=1 AND hub_url IS NOT NULL"
        ).fetchall()
        return {str(row["feed_url"]) for row in rows}
    except Exception:
        LOGGER.debug("[websub] could not load push-active feed URLs", exc_info=True)
        return set()


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
            with get_meta_connection() as conn:
                for feed_url in orphaned_feed_urls:
                    try:
                        purge_orphaned_feed(reader, conn, feed_url, archive_pending=True)
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


_FOLDER_CADENCE_LAST_REFRESH_PREFIX = "folder_cadence_last_refresh:"


def _background_user_ids() -> list[str]:
    """Users that background work (scheduled refresh, maintenance) should run for.

    Returns every enabled account; each is processed under its own tenancy context."""
    if user_store is None:
        return [tenancy.DEFAULT_USER_ID]
    return [u["user_id"] for u in user_store.list_users() if not u["disabled"]]


def _for_each_background_user(label: str, fn: Callable[[], None]) -> None:
    """Run ``fn()`` once per background user, each under its own tenancy context.

    Startup backfills, syncs and one-off cleanups touch per-user DBs through the
    context-bound ``get_reader()`` / ``get_meta_connection()`` helpers. Run bare,
    they resolve to :data:`tenancy.DEFAULT_USER_ID` and write the legacy
    top-level DBs. Wrapping the call here binds each enabled user in turn.
    One user's failure is logged and does not abort the rest."""
    for uid in _background_user_ids():
        with tenancy.user_context(uid):
            try:
                fn()
            except Exception:
                LOGGER.exception("[startup] %s failed for user %r", label, uid)


def _effective_auto_refresh_minutes() -> int:
    """Auto-refresh cadence for the currently-bound user."""
    with get_meta_connection() as conn:
        return get_auto_refresh_minutes(conn)


_scheduled_refresh_rotation = 0


def _rotate_for_fairness(uids: list[str]) -> list[str]:
    """Rotate the per-tick user order round-robin so the same user isn't always
    processed first.

    Users are refreshed sequentially within a tick (adequate at the 1–3 trusted
    user target — every user is still processed every tick). Rotating the start
    point each pass spreads any first-mover advantage and means a slow or hanging
    user delays a different set of downstream users each time rather than always
    the same ones. Deeper fairness at scale (per-user concurrency, fetch budgets)
    stays deferred behind this seam per the multi-user plan."""
    global _scheduled_refresh_rotation
    if len(uids) <= 1:
        return uids
    offset = _scheduled_refresh_rotation % len(uids)
    _scheduled_refresh_rotation = (_scheduled_refresh_rotation + 1) % len(uids)
    return uids[offset:] + uids[:offset]


def _run_scheduled_refresh_for_all_users() -> None:
    """One scheduled-refresh pass across every background user, each under its
    own tenancy context so the refresh hits that user's databases."""
    for uid in _rotate_for_fairness(_background_user_ids()):
        with tenancy.user_context(uid):
            try:
                _scheduled_refresh_tick()
            except Exception:
                LOGGER.exception("scheduled refresh failed for user %r", uid)
    # Renewal is global (shared DB) — run once per scheduler tick, not per user.
    if websub_service:
        websub_service.renew_expiring_subscriptions()


def scheduled_refresh_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(SCHEDULER_POLL_SECONDS):
        _run_scheduled_refresh_for_all_users()


def _scheduled_refresh_tick() -> None:
    """One scheduled-refresh pass for the currently-bound tenancy user."""
    global_minutes = _effective_auto_refresh_minutes()
    if global_minutes <= 0:
        return

    now_ts = time.time()
    feeds_to_refresh: set[str] = set()
    folders_to_mark: list[tuple[str, str]] = []

    # Feeds paused via Feed Properties set reader's updates_enabled=0. The scheduler
    # drives per-feed reader.update_feed(), which ignores that flag, so we must
    # exclude paused feeds here ourselves (alongside Lectio's own disabled_feeds).
    with get_reader() as reader:
        paused = {str(f.url) for f in reader.get_feeds(updates_enabled=False)}
    with get_meta_connection() as conn:
        disabled = get_disabled_feed_urls(conn) | paused
        # Load all folders and their per-folder cadence settings.
        folder_rows = conn.execute("SELECT id, cadence_minutes FROM folders").fetchall()
        for folder in folder_rows:
            fid = int(folder["id"])
            cadence = folder["cadence_minutes"]
            effective_minutes = cadence if (cadence and cadence > 0) else global_minutes
            key = f"{_FOLDER_CADENCE_LAST_REFRESH_PREFIX}{fid}"
            last_ts_str = get_setting(conn, key)
            last_ts = float(last_ts_str) if last_ts_str else 0.0
            if (now_ts - last_ts) < effective_minutes * 60:
                continue
            # This folder is due — collect its feeds.
            folder_feed_rows = conn.execute(
                "SELECT feed_url FROM folder_feeds WHERE folder_id = ?", (fid,)
            ).fetchall()
            for row in folder_feed_rows:
                url = str(row["feed_url"])
                if url not in disabled:
                    feeds_to_refresh.add(url)
            folders_to_mark.append((key, str(now_ts)))
        if folders_to_mark:
            for key, val in folders_to_mark:
                set_setting(conn, key, val)
            # Keep legacy global timestamp for monitoring / debug.
            app.state.last_scheduled_refresh_started_at = time.monotonic()
            with get_meta_connection() as _sc:
                scraper_service.refresh_all_scraped_feeds(_sc)
                _da_cid, _da_secret = get_deviantart_credentials()
                deviantart_service.refresh_all_deviantart_feeds(_sc, _da_cid, _da_secret, access_token=get_deviantart_user_token())
                _reddit_token = get_reddit_user_token()
                if _reddit_token:
                    try:
                        with get_reader() as _rdr:
                            reddit_service.refresh_all_reddit_feeds(_sc, _rdr, _reddit_token)
                    except Exception:
                        LOGGER.exception("[reddit] feed sync error in scheduler")
            if inoreader_connected():
                try:
                    _inoreader_drip_step()
                except Exception:
                    LOGGER.exception("[inoreader] drip step error in scheduler")

    if not feeds_to_refresh:
        return

    if REFRESH_DEBUG_ENABLED:
        LOGGER.info(
            "[refresh] scheduled run: global_minutes=%d feed_count=%d folder_count=%d",
            global_minutes,
            len(feeds_to_refresh),
            len(folders_to_mark),
        )
    feed_refresh_service.update_feeds(feeds_to_refresh)
    _run_automation_after_refresh(feeds_to_refresh)
    invalidate_unread_counts_cache()
    if websub_service:
        websub_service.maybe_discover_hubs(list(feeds_to_refresh), tenancy.current_user_id())


def _run_daily_maintenance() -> None:
    """Nightly cleanup. Per-user work (log prune, orphan cleanup, VACUUM of the
    user's meta/starred DBs, email-batch flush) runs once per enabled user under
    their tenancy context; global work (thumb-cache VACUUM, YouTube sync) once."""
    LOGGER.info("[maintenance] starting daily maintenance")
    for _uid in _background_user_ids():
        with tenancy.user_context(_uid):
            try:
                _daily_maintenance_for_user()
            except Exception:
                LOGGER.exception("[maintenance] failed for user %r", _uid)
    _run_global_maintenance()
    LOGGER.info("[maintenance] daily maintenance complete")


def _daily_maintenance_for_user() -> None:
    """Per-user nightly cleanup for the currently-bound tenancy user."""

    # 1. Prune rule_run_log older than 90 days. run_at is stored as a naive
    # ISO-8601 string (datetime.now().isoformat()), so compare against an ISO
    # cutoff — comparing the TEXT column to an int epoch never matched, and the
    # column was misnamed ran_at, so this prune silently never ran.
    try:
        cutoff = (datetime.now() - timedelta(days=90)).isoformat()
        with get_meta_connection() as conn:
            old_ids = [r[0] for r in conn.execute(
                "SELECT id FROM rule_run_log WHERE run_at < ?", (cutoff,)
            ).fetchall()]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM rule_run_log_entries WHERE log_id IN ({placeholders})", old_ids)
                conn.execute(f"DELETE FROM rule_run_log WHERE id IN ({placeholders})", old_ids)
                LOGGER.info("[maintenance] pruned %d old rule run log entries", len(old_ids))
    except Exception:
        LOGGER.exception("[maintenance] rule log prune failed")

    # 1b. Bound feed_fetch_history: keep the most recent FEED_FETCH_HISTORY_KEEP
    # rows per feed and drop anything older than the age cap, so the diagnostic
    # log can't grow without limit on busy installs.
    try:
        cutoff = time.time() - get_fetch_history_max_age_days() * 86400
        with get_meta_connection() as conn:
            cur = conn.execute(
                """
                DELETE FROM feed_fetch_history WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY feed_url ORDER BY fetched_at DESC
                        ) AS rn FROM feed_fetch_history
                    ) WHERE rn > ?
                )
                """,
                (get_fetch_history_keep(),),
            )
            pruned = cur.rowcount
            cur = conn.execute("DELETE FROM feed_fetch_history WHERE fetched_at < ?", (cutoff,))
            pruned += cur.rowcount
            if pruned:
                LOGGER.info("[maintenance] pruned %d feed fetch-history rows", pruned)
    except Exception:
        LOGGER.exception("[maintenance] fetch-history prune failed")

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

    # 3. VACUUM this user's own SQLite DBs (the shared thumb cache is global and
    # vacuumed once in _run_global_maintenance).
    for label, path in [
        ("meta", tenancy.meta_db_path()),
        ("starred-archive", tenancy.starred_archive_db_path()),
    ]:
        try:
            if not Path(path).exists():
                continue
            conn = sqlite3.connect(str(path))
            conn.execute("VACUUM")
            conn.close()
            LOGGER.info("[maintenance] VACUUM %s done", label)
        except Exception:
            LOGGER.exception("[maintenance] VACUUM %s failed", label)

    # 4. Flush pending email batch queues.
    try:
        _flush_all_email_batches()
        LOGGER.info("[maintenance] email batch flush done")
    except Exception:
        LOGGER.exception("[maintenance] email batch flush failed")

    # 5. YouTube subscription sync — per-user (this user's own key + channel).
    if get_yt_api_key() and get_yt_channel_id():
        try:
            result = _run_youtube_sync()
            if result.get("error"):
                LOGGER.error("[maintenance] YouTube sync error: %s", result["error"])
            else:
                LOGGER.info("[maintenance] YouTube sync: +%d -%d total=%d",
                            result["added"], result["removed"], result["total"])
        except Exception:
            LOGGER.exception("[maintenance] YouTube sync failed")

    # 6. DeviantArt watch-list → gallery feeds sync — per-user, only when the
    # account is connected. Mirrors the YouTube sync above so the watch list no
    # longer needs the manual Settings button. sync_deviantart_watchlist is
    # add-only and self-throttles against DeviantArt's rate limit.
    if get_deviantart_user_token():
        try:
            result = sync_deviantart_watchlist()
            rate_suffix = " (rate limited)" if result.get("rate_limited") else ""
            if result.get("error"):
                LOGGER.error("[maintenance] DeviantArt watch-list sync error%s: %s",
                             rate_suffix, result["error"])
            else:
                LOGGER.info("[maintenance] DeviantArt watch-list sync: +%d watched=%d%s",
                            result.get("added", 0), result.get("total", 0), rate_suffix)
        except Exception:
            LOGGER.exception("[maintenance] DeviantArt watch-list sync failed")

    # 7. Record this user's last-ran timestamp.
    with get_meta_connection() as conn:
        set_setting(conn, "maintenance_last_ran_at", time.strftime("%Y-%m-%d %H:%M %Z"))


def _evict_img_cache() -> None:
    """Drop /api/img cache entries not served within the configured TTL. The TTL
    is on last_accessed (not created_at), so actively-browsed images stay cached
    and only stale/long-unread ones age out. 0 days = keep forever (no eviction)."""
    days = get_img_cache_days()
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    try:
        with get_img_cache_connection() as conn:
            cur = conn.execute("DELETE FROM img_cache WHERE last_accessed < ?", (cutoff,))
            LOGGER.info("[maintenance] img cache: evicted %d entries older than %d days", cur.rowcount, days)
    except Exception:
        LOGGER.exception("[maintenance] img cache eviction failed")


def _run_global_maintenance() -> None:
    """Nightly cleanup that is not per-user: evict stale /api/img cache entries,
    then VACUUM the shared (global) caches."""
    _evict_img_cache()
    for label, path in (
        ("thumb", THUMB_DB_PATH),
        ("img-cache", IMG_CACHE_DB_PATH),
        ("yt-durations", YT_DURATION_DB_PATH),
    ):
        try:
            conn = sqlite3.connect(str(path))
            conn.execute("VACUUM")
            conn.close()
            LOGGER.info("[maintenance] VACUUM %s done", label)
        except Exception:
            LOGGER.exception("[maintenance] VACUUM %s failed", label)


def _daily_maintenance_loop(stop_event: threading.Event) -> None:
    """Thread that fires _run_daily_maintenance() once per day and flushes email
    batch queues at their configured batch_time each minute."""
    last_ran_date: str | None = None
    last_batch_check_hhmm: str | None = None
    while not stop_event.wait(30):
        # Flush email batches at their configured batch_time (once per clock
        # minute), per user — email_batch_queue lives in each user's meta DB.
        now_hhmm = time.strftime("%H:%M")
        if now_hhmm != last_batch_check_hhmm:
            last_batch_check_hhmm = now_hhmm
            for _uid in _background_user_ids():
                with tenancy.user_context(_uid):
                    try:
                        _check_and_flush_batch_times()
                    except Exception:
                        LOGGER.exception("[maintenance] email batch flush failed for user %r", _uid)

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
                    if not _is_subscribable_feed_url(feed_url):
                        # Reject non-http(s) schemes (e.g. file://) — reader would
                        # otherwise read local files when refreshing the feed.
                        LOGGER.warning("OPML import: skipping non-http(s) entry %r", feed_url)
                        return
                    # Canonicalize so a variant (old.reddit, ?alt=rss, trailing
                    # slash) attaches to an existing subscription instead of
                    # creating an uncategorized duplicate.
                    feed_url = canonical_feed_url(feed_url)
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


@app.get("/entries/media/audio")
def media_audio_redirect(feed_url: str, entry_id: str):
    """Redirect to the entry's audio enclosure URL.

    If the stored URL returns a non-2xx response (e.g. Patreon signed URLs
    expire after ~24 h), the feed is refreshed once to obtain a fresh URL
    before redirecting.
    """
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")
        with get_meta_connection() as _mconn:
            audio_url = _resolve_entry_audio_url(_mconn, feed_url, entry_id, entry)
        if not audio_url:
            raise HTTPException(status_code=404, detail="No audio enclosure found")

        # Quick validity check; refresh the feed if the URL is expired.
        try:
            head = httpx.head(
                audio_url,
                follow_redirects=True,
                timeout=4.0,
                headers={"User-Agent": READABILITY_USER_AGENT},
            )
            if head.status_code not in (200, 206):
                try:
                    feed_refresh_service.update_feeds([feed_url])
                    fresh = reader.get_entry((feed_url, entry_id), None)
                    if fresh:
                        fresh_url = _find_entry_audio_url(fresh)
                        if fresh_url:
                            audio_url = fresh_url
                except Exception:
                    LOGGER.warning("Audio URL refresh failed for %s", feed_url, exc_info=True)
        except Exception:
            pass  # Network error on HEAD — try the stored URL anyway

    return RedirectResponse(audio_url, status_code=302)


@app.get("/entries/media/download")
def media_audio_download(feed_url: str, entry_id: str):
    """Proxy the entry's audio enclosure as an attachment download.

    Handles expired signed URLs the same way as /entries/media/audio.
    Uses a streaming proxy so the file is downloaded through the server,
    which avoids cross-origin restrictions on the browser download attribute.
    """
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")
        with get_meta_connection() as _mconn:
            audio_url = _resolve_entry_audio_url(_mconn, feed_url, entry_id, entry)
        if not audio_url:
            raise HTTPException(status_code=404, detail="No audio enclosure found")
        entry_title = str(entry.title or "audio")

        # Refresh if expired.
        try:
            head = httpx.head(
                audio_url,
                follow_redirects=True,
                timeout=4.0,
                headers={"User-Agent": READABILITY_USER_AGENT},
            )
            if head.status_code not in (200, 206):
                try:
                    feed_refresh_service.update_feeds([feed_url])
                    fresh = reader.get_entry((feed_url, entry_id), None)
                    if fresh:
                        fresh_url = _find_entry_audio_url(fresh)
                        if fresh_url:
                            audio_url = fresh_url
                except Exception:
                    LOGGER.warning("Audio URL refresh failed for %s", feed_url, exc_info=True)
        except Exception:
            pass

    # Derive a clean filename from the URL path, falling back to entry title.
    parsed_path = urlparse(audio_url).path.rstrip("/").split("/")[-1]
    if parsed_path and "." in parsed_path:
        filename = re.sub(r"[^\w.\-]", "_", parsed_path)
    else:
        safe_title = re.sub(r"[^\w\- ]", "", entry_title).strip()[:80] or "audio"
        filename = safe_title.replace(" ", "_") + ".mp3"

    def _stream():
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
            headers={"User-Agent": READABILITY_USER_AGENT},
        ) as client:
            with client.stream("GET", audio_url) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=65536):
                    yield chunk

    return StreamingResponse(
        _stream(),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/entries/thumb-crop")
def set_entry_thumb_crop_route(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    crop: str = Form(default=""),
):
    """Save (or clear) a per-entry thumbnail crop override."""
    effective = crop.strip() if crop.strip() in _VALID_THUMB_CROPS else None
    lead_image_service.store_entry_thumb_crop(feed_url, entry_id, effective)
    return JSONResponse({"ok": True, "crop": effective})


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
    article_html = _strip_bandcamp_track_signature(article_html)
    return HTMLResponse(
        (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Reader view</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<style>body{margin:0;background:#f6f8fb;color:#1a2430;font-family:Georgia,serif;}"
            "main{max-width:760px;margin:0 auto;padding:1.2rem 1rem 2rem;}"
            "header{font-family:Segoe UI,Arial,sans-serif;margin-bottom:1rem;padding-bottom:.75rem;border-bottom:1px solid #d4dbe5;}"
            "a{color:#0a5ca4;}article{font-size:1.05rem;line-height:1.7;}"
            + _READER_VIEW_MEDIA_CSS +
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
# ---------------------------------------------------------------------------


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if not AUTH_ENABLED or _session_logged_in(request):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": None, "static_asset_version": STATIC_ASSET_VERSION},
    )


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
    window = get_login_window_seconds()
    cutoff = now - window
    with _login_failures_lock:
        timestamps = [t for t in _login_failures.get(ip, []) if t >= cutoff]
        _login_failures[ip] = timestamps
        return len(timestamps) >= get_login_max_failures()


def _record_login_failure(ip: str, now: float) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(ip, []).append(now)


def _clear_login_failures(ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(ip, None)


def _safe_next(next_url: str | None) -> str:
    """Return ``next_url`` only if it is a safe same-origin path, else ``/``.

    Prevents post-login open redirects: rejects off-site absolute URLs and the
    protocol-relative (``//evil.com``) / backslash (``/\\evil.com``) forms that
    browsers normalise to an external authority.
    """
    if not next_url or not next_url.startswith("/"):
        return "/"
    if next_url.startswith("//") or next_url.startswith("/\\"):
        return "/"
    return next_url


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
                "error": f"Too many failed login attempts. Try again in {get_login_window_seconds() // 60} minutes.",
                "static_asset_version": STATIC_ASSET_VERSION,
            },
            status_code=429,
        )

    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    resolved = (
        user_store.verify_login(username, password, default_scheme=PASSWORD_HASH_SCHEME)
        if user_store is not None
        else None
    )
    if resolved is not None:
        _clear_login_failures(ip)
        request.session.clear()  # rotate session on login (anti-fixation)
        request.session["authenticated"] = True
        request.session["user_id"] = resolved
        return RedirectResponse(url=_safe_next(next), status_code=303)
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


# --- Account / user management ---


def _current_web_user(request: Request) -> str | None:
    """The logged-in web session's user_id, or None."""
    if not request.session.get("authenticated"):
        return None
    uid = request.session.get("user_id")
    return uid if uid and tenancy.is_valid_user_id(uid) else None


def _current_web_username(request: Request) -> str | None:
    """Display name (mutable username) for the current session user, or None."""
    uid = _current_web_user(request)
    if not uid or user_store is None:
        return None
    row = user_store.get_by_id(uid)
    return row["username"] if row else None


def _is_web_admin(user_id: str | None) -> bool:
    if not user_id or user_store is None:
        return False
    row = user_store.get_by_id(user_id)
    return bool(row and row["is_admin"] and not row["disabled"])


USERNAME_MIN_LEN, USERNAME_MAX_LEN = 4, 10
PASSWORD_MIN_LEN, PASSWORD_MAX_LEN = 6, 36


def _username_error(name: str) -> str | None:
    if not (USERNAME_MIN_LEN <= len(name) <= USERNAME_MAX_LEN):
        return f"Username must be {USERNAME_MIN_LEN}–{USERNAME_MAX_LEN} characters."
    if not tenancy.is_valid_user_id(name):
        return "Username may use only letters, digits, _ and -."
    return None


def _password_error(pw: str) -> str | None:
    if not (PASSWORD_MIN_LEN <= len(pw) <= PASSWORD_MAX_LEN):
        return f"Password must be {PASSWORD_MIN_LEN}–{PASSWORD_MAX_LEN} characters."
    return None


def _account_redirect(*, msg: str | None = None, error: str | None = None) -> RedirectResponse:
    params: dict[str, str] = {}
    if msg:
        params["msg"] = msg
    if error:
        params["error"] = error
    url = "/administration" + ("?" + urlencode(params) if params else "")
    return RedirectResponse(url=url, status_code=303)


def _human_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.0f} {units[i]}" if i == 0 else f"{f:.1f} {units[i]}"


def _dir_bytes(*paths: Path) -> int:
    """Total bytes for a set of DB files including their -wal/-shm/.search sidecars."""
    total = 0
    for p in paths:
        for f in p.parent.glob(p.name + "*"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _format_last_active(ts: float | None) -> str:
    if not ts:
        return "never"
    try:
        return format_datetime_for_ui(datetime.fromtimestamp(float(ts), tz=timezone.utc)) or "—"
    except Exception:
        return "—"


def _admin_user_rows() -> list[dict]:
    """User list for the Administration page, enriched with per-user stats
    (feed count, personal DB size, last-active). Each user's stats are read under
    its own tenancy context."""
    rows: list[dict] = []
    for u in user_store.list_users():  # type: ignore[union-attr]
        uid = u["user_id"]
        feeds = 0
        db_bytes = 0
        try:
            with tenancy.user_context(uid):
                with get_meta_connection() as c:
                    feeds = int(c.execute("SELECT COUNT(DISTINCT feed_url) FROM folder_feeds").fetchone()[0])
            db_bytes = _dir_bytes(
                tenancy.reader_db_path(uid),
                tenancy.meta_db_path(uid),
                tenancy.starred_archive_db_path(uid),
            )
        except Exception:
            LOGGER.debug("admin stats failed for %r", uid, exc_info=True)
        rows.append({
            **u,
            "feed_count": feeds,
            "db_human": _human_bytes(db_bytes),
            "last_active": _format_last_active(u.get("last_seen_at")),
        })
    return rows


@app.get("/administration")
def account_page(request: Request, msg: str | None = None, error: str | None = None):
    """Admin page: user management + instance configuration. Admin-only."""
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login?next=/administration", status_code=303)
    row = user_store.get_by_id(uid)
    if not row:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not row["is_admin"]:
        # Non-admins have no admin page; their per-user settings live in Settings.
        return RedirectResponse(url="/", status_code=303)

    def _masked(v: str) -> str:
        return "••••••••" if v else ""

    resend_key = get_resend_api_key()
    with get_meta_connection() as conn:
        maint_last = get_setting(conn, "maintenance_last_ran_at") or ""
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "user_id": uid,
            "username": row["username"],
            "users": _admin_user_rows(),
            "message": msg,
            "error": error,
            # Instance configuration (admin-managed): email (Resend) + maintenance.
            "resend_key_set": bool(resend_key),
            "resend_key_masked": _masked(resend_key),
            "email_from": get_resend_from(),
            "maintenance_hour": get_runtime_setting(SETTING_MAINTENANCE_HOUR),
            "maintenance_last": maint_last,
            "img_cache_days": get_img_cache_days(),
            "img_cache_max_dim": get_img_cache_max_dim(),
            # Shared OAuth apps (stored in admin's own app_settings).
            "shared_yt_oauth_client_id": get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_ID, ""),
            "shared_yt_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET)),
            "shared_yt_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET, "")),
            "shared_pinterest_oauth_client_id": get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID, ""),
            "shared_pinterest_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET)),
            "shared_pinterest_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET, "")),
            "shared_reddit_client_id": get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_ID, ""),
            "shared_reddit_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET)),
            "shared_reddit_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET, "")),
            # Instance tuning
            "fetch_history_keep": get_fetch_history_keep(),
            "fetch_history_max_age_days": get_fetch_history_max_age_days(),
            "login_max_failures": get_login_max_failures(),
            "login_window_seconds": get_login_window_seconds(),
            "instance_auto_refresh": get_instance_default_auto_refresh(),
            "public_url": LECTIO_PUBLIC_URL,
            "static_asset_version": STATIC_ASSET_VERSION,
        },
    )


@app.post("/account/password")
async def account_change_password(request: Request):
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    current = str(form.get("current_password") or "")
    new = str(form.get("new_password") or "")
    confirm = str(form.get("confirm_password") or "")
    row = user_store.get_by_id(uid)
    if row is None:
        return RedirectResponse(url="/login", status_code=303)
    # verify_login takes the (typed) username; we have the user_id from session.
    if user_store.verify_login(row["username"], current, default_scheme=PASSWORD_HASH_SCHEME) != uid:
        return RedirectResponse(url="/?message=" + quote_plus("Current password is incorrect."), status_code=303)
    if not new or new != confirm:
        return RedirectResponse(url="/?message=" + quote_plus("New password and confirmation do not match."), status_code=303)
    perr = _password_error(new)
    if perr:
        return RedirectResponse(url="/?message=" + quote_plus(perr), status_code=303)
    user_store.set_password(uid, new, scheme=PASSWORD_HASH_SCHEME)
    return RedirectResponse(url="/?message=" + quote_plus("Password changed."), status_code=303)


@app.post("/account/api-token/regenerate")
async def account_regenerate_token(request: Request):
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    user_store.regenerate_api_token(uid)
    return RedirectResponse(
        url="/?message=" + quote_plus("API token regenerated — update your RSS clients."),
        status_code=303,
    )


@app.post("/account/username")
async def account_change_username(request: Request):
    """Self-service username change. Identity (user_id) is unchanged, so the
    session stays valid and data/tokens are unaffected."""
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    new_username = str(form.get("new_username") or "").strip()
    uerr = _username_error(new_username)
    if uerr:
        return RedirectResponse(url="/?message=" + quote_plus(uerr), status_code=303)
    try:
        user_store.rename_user(uid, new_username)
    except UserExistsError:
        return RedirectResponse(url="/?message=" + quote_plus(f"Username {new_username!r} is taken."), status_code=303)
    except ValueError:
        return RedirectResponse(url="/?message=" + quote_plus("Invalid username."), status_code=303)
    return RedirectResponse(url="/?message=" + quote_plus(f"Username changed to {new_username!r}."), status_code=303)


@app.post("/admin/users/create")
async def admin_create_user(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    is_admin = bool(form.get("is_admin"))
    uerr = _username_error(username)
    if uerr:
        return _account_redirect(error=uerr)
    perr = _password_error(password)
    if perr:
        return _account_redirect(error=perr)
    try:
        new_user_id = user_store.create(username, password, is_admin=is_admin, scheme=PASSWORD_HASH_SCHEME)
        provision_user_storage(new_user_id)
    except UserExistsError:
        return _account_redirect(error=f"User {username!r} already exists.")
    except Exception:
        LOGGER.exception("admin create user failed")
        return _account_redirect(error="Could not create user (see server logs).")
    return _account_redirect(msg=f"Created user {username!r}.")


@app.post("/admin/users/disable")
async def admin_disable_user(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    disabled = str(form.get("disabled") or "0") == "1"
    if target_id == admin and disabled:
        return _account_redirect(error="You cannot disable your own account.")
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    user_store.set_disabled(target_id, disabled)
    return _account_redirect(msg=f"{'Disabled' if disabled else 'Enabled'} {target['username']!r}.")


@app.post("/admin/users/delete")
async def admin_delete_user(request: Request):
    """Permanently remove a user: drops the account row + GReader tokens and
    deletes the user's isolated data directory. Admin-only; cannot delete your
    own account or the last remaining admin."""
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    if target_id == admin:
        return _account_redirect(error="You cannot delete your own account.")
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    if target["is_admin"] and user_store.count_admins() <= 1:
        return _account_redirect(error="Cannot delete the last admin account.")
    try:
        delete_user_storage(target_id)
        user_store.delete_user(target_id)
    except Exception:
        LOGGER.exception("admin delete user failed for %r", target_id)
        return _account_redirect(error="Could not delete user (see server logs).")
    return _account_redirect(msg=f"Deleted user {target['username']!r} and all their data.")


@app.post("/admin/users/reset-password")
async def admin_reset_password(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    new = str(form.get("new_password") or "")
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    perr = _password_error(new)
    if perr:
        return _account_redirect(error=perr)
    user_store.set_password(target_id, new, scheme=PASSWORD_HASH_SCHEME)
    return _account_redirect(msg=f"Reset password for {target['username']!r}.")


@app.post("/admin/users/rename")
async def admin_rename_user(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    new_username = str(form.get("new_username") or "").strip()
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    old_username = target["username"]
    uerr = _username_error(new_username)
    if uerr:
        return _account_redirect(error=uerr)
    try:
        user_store.rename_user(target_id, new_username)
    except UserExistsError:
        return _account_redirect(error=f"Username {new_username!r} is already taken.")
    except ValueError:
        return _account_redirect(error="Invalid username — use 1–64 letters, digits, _ or -.")
    return _account_redirect(msg=f"Renamed {old_username!r} to {new_username!r} (data and tokens unchanged).")


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
    # Limit concurrent expensive home renders (DB queries + context building).
    # Release before returning StreamingResponse so slow network delivery on the
    # client side doesn't hold the semaphore and block new renders with 503s.
    if not _home_request_semaphore.acquire(blocking=False):
        return Response(
            status_code=503,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
    try:
        return _home_inner(
            request=request,
            folder_id=folder_id,
            list_feed_url=list_feed_url,
            tag=tag,
            sort_by=sort_by,
            sort_dir=sort_dir,
            read_filter=read_filter,
            star_only=star_only,
            resume_read_filter=resume_read_filter,
            feed_url=feed_url,
            entry_id=entry_id,
            q=q,
            message=message,
            no_rss_url=no_rss_url,
            chunk=chunk,
            chunk_delta=chunk_delta,
        )
    finally:
        _home_request_semaphore.release()


def _home_inner(
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

        # Derive the virtual "Uncategorized" folder: feeds the reader knows about
        # that live in no folder_feeds row. Copy the snapshot's per-folder maps
        # (shared with the cached snapshot) before adding derived keys so we never
        # mutate cross-request cache state. Also widen "All Feeds" (root) to cover
        # every reader feed, not just foldered ones, so orphan feeds and their
        # unreads are reachable from the top of the tree.
        all_reader_feed_urls = get_all_reader_feed_urls()
        uncategorized_feed_urls = all_reader_feed_urls - all_feed_urls
        folder_feed_urls_by_id = dict(folder_feed_urls_by_id)
        direct_feed_urls_by_folder = dict(direct_feed_urls_by_folder)
        folder_feed_urls_by_id[root_id] = set(all_reader_feed_urls)
        folder_feed_urls_by_id[UNCATEGORIZED_FOLDER_ID] = uncategorized_feed_urls
        direct_feed_urls_by_folder[UNCATEGORIZED_FOLDER_ID] = sorted(uncategorized_feed_urls)
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
        # The virtual Uncategorized folder isn't in raw_folder_rows, so count it
        # here and fold it into the root ("All Feeds") total.
        uncategorized_unread = sum(
            active_unread_counts_by_feed.get(url, 0) for url in uncategorized_feed_urls
        )
        unread_counts_by_folder[UNCATEGORIZED_FOLDER_ID] = uncategorized_unread
        unread_counts_by_folder[root_id] = unread_counts_by_folder.get(root_id, 0) + uncategorized_unread
        _tick("counts_by_folder")
        folder_rows = []
        for row in raw_folder_rows:
            folder_dict = dict(row)
            folder_dict["unread_count"] = unread_counts_by_folder.get(int(row["id"]), 0)
            folder_rows.append(folder_dict)
        # Append the virtual Uncategorized folder last so it pins to the bottom of
        # the tree. Only when it actually holds feeds. `virtual` flags the template
        # and context menu to suppress edit affordances (rename/delete/props).
        if uncategorized_feed_urls:
            folder_rows.append({
                "id": UNCATEGORIZED_FOLDER_ID,
                "name": UNCATEGORIZED_FOLDER_NAME,
                "cadence_minutes": None,
                "depth": 1,
                "path": UNCATEGORIZED_FOLDER_NAME,
                "feed_count": len(uncategorized_feed_urls),
                "unread_count": uncategorized_unread,
                "virtual": True,
            })
        global_note = get_setting(conn, GLOBAL_NOTE_SETTING_KEY) or ""
        email_to_default = get_setting(conn, EMAIL_TO_SETTING_KEY) or "" if is_email_configured() else ""
        highlight_rules = get_highlight_keywords(conn)
        profile_name = get_setting(conn, PROFILE_NAME_SETTING_KEY) or ""
        profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
        _profile_lower = profile_email.lower()
        _avatar_hash = hashlib.md5(_profile_lower.strip().encode()).hexdigest() if _profile_lower.strip() else ""
        profile_avatar_url = (
            f"https://www.gravatar.com/avatar/{_avatar_hash}?d=identicon&s=128"
            if _avatar_hash else
            "https://www.gravatar.com/avatar/?d=identicon&s=128"
        )
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
    # Sort alphabetically by title (the SQL orders by disabled_at, which reads as
    # random in the list); title isn't known until feed_title_map is applied above.
    inactive_feeds.sort(key=lambda x: x["feed_title"].casefold())
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
    error_feed_urls: set[str] = {
        cast(str, pf["feed_url"])
        for pf in problematic_feeds
        if not pf.get("acknowledged_at")
    }
    feeds_by_folder: dict[int, list[FeedInFolder]] = {}
    # Like feeds_by_folder but also includes disabled feeds (flagged), for the
    # Settings → Feeds folders tree where disabled feeds show greyed out. The
    # sidebar uses feeds_by_folder and keeps excluding them.
    settings_feeds_by_folder: dict[int, list[FeedInFolder]] = {}
    # feed_url → containing_folder_id, so feed-name links in posts/entry can
    # navigate to the feed's own folder rather than the currently-viewed one.
    feed_to_folder: dict[str, int] = {}
    for row in folder_rows:
        folder_row_id = int(row["id"])
        urls = direct_feed_urls_by_folder.get(folder_row_id, [])
        all_folder_feeds = [
            FeedInFolder(
                url=url,
                title=feed_title_map.get(url, url),
                icon_url=get_favicon_url(url),
                unread_count=unread_counts_by_feed.get(url, 0),
                has_error=url in error_feed_urls,
                disabled=url in disabled_feed_urls,
            )
            for url in urls
        ]
        # Active feeds first (alphabetical), disabled greyed at the bottom.
        all_folder_feeds.sort(key=lambda f: (f.disabled, f.title.casefold()))
        settings_feeds_by_folder[folder_row_id] = all_folder_feeds
        feeds_by_folder[folder_row_id] = [f for f in all_folder_feeds if not f.disabled]
        for url in urls:
            feed_to_folder[url] = folder_row_id

    root_folder_row = next((row for row in folder_rows if int(row["depth"]) == 0), None)
    child_folder_rows = [row for row in folder_rows if int(row["depth"]) == 1]
    folder_failing_counts: dict[int, int] = {
        fid: sum(1 for f in feeds if f.has_error)
        for fid, feeds in feeds_by_folder.items()
        if any(f.has_error for f in feeds)
    }

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
    # unsubscribed). Two entry points, both gated on the saved filter:
    #   1. Root "All Feeds" with no feed/tag/query — orphans belong to no
    #      folder, so per-folder views legitimately exclude them.
    #   2. A specific feed selected that is no longer live (the user clicked
    #      the feed link on an orphaned save) — show just that feed's archive.
    orphan_only_feed = (
        selected_feed_url
        if (selected_star_only and selected_feed_url and selected_feed_url not in all_feed_urls)
        else None
    )
    if orphan_only_feed:
        try:
            posts = merge_orphan_saved_entries(
                posts,
                live_feed_urls=all_feed_urls,
                sort_by=selected_sort_by,
                sort_dir=selected_sort_dir,
                limit=limit,
                only_feed_url=orphan_only_feed,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("orphan saved entry merge (feed %s) failed: %s", orphan_only_feed, exc)
    elif (
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
        # Re-bind the request's tenancy user inside the daemon thread; a bare
        # thread does not inherit contextvars, so backfill_entry_list would
        # otherwise persist images to the default user's DB and the thumbnails
        # would not stick for this user across refreshes.
        _bf_uid = tenancy.current_user_id()
        threading.Thread(
            target=_run_in_user_context,
            args=(_bf_uid, lead_image_service.backfill_entry_list, uncached_posts),
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

    # Auto-refresh cadence shown in the menu: the bound user's own value in multi
    # mode (the menu form posts to set *their* setting), the cached global in single.
    _arm = _effective_auto_refresh_minutes()
    _tmpl_ctx = {
        "request": request,
        "folder_rows": folder_rows,
        "root_folder_row": root_folder_row,
        "child_folder_rows": child_folder_rows,
        "folder_failing_counts": folder_failing_counts,
        "folder_options": folder_options,
        "feeds_by_folder": feeds_by_folder,
        "settings_feeds_by_folder": settings_feeds_by_folder,
        "feed_to_folder": feed_to_folder,
        "push_feed_urls": get_push_active_feed_urls(),
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
        "yt_oauth_connected": youtube_oauth_connected(),
        "yt_embed_account_features": youtube_embed_account_features_enabled(),
        "pinterest_oauth_connected": pinterest_oauth_connected(),
        "pinterest_connected": pinterest_oauth_connected(),
        "inoreader_connected": inoreader_connected(),
        "pinterest_configured": bool(_ENV_PINTEREST_OAUTH_CLIENT_ID and _ENV_PINTEREST_OAUTH_CLIENT_SECRET),
        "email_to_default": email_to_default,
        "instapaper_configured": is_instapaper_configured(),
        "quire_configured": is_quire_configured(),
        "quire_project_name": quire_project_name(),
        "reddit_connected": reddit_connected(),
        "youtube_sync_last_at": youtube_sync_last_at,
        "youtube_sync_last_result": youtube_sync_last_result,
        "inactive_feeds": inactive_feeds,
        "inactive_feed_count": len(inactive_feeds),
        "posts": posts,
        "selected_entry": selected_entry,
        "message": message,
        "no_rss_url": no_rss_url,
        "auto_refresh_enabled": _arm > 0,
        "auto_refresh_minutes": _arm,
        "auto_refresh_option_minutes": AUTO_REFRESH_OPTION_MINUTES,
        "static_asset_version": STATIC_ASSET_VERSION,
        "debug_mode": DEBUG_MODE,
        "highlight_rules": highlight_rules,
        "email_contacts": email_contacts,
        "email_bcc": email_bcc,
        "profile_name": profile_name,
        "profile_email": profile_email,
        "profile_avatar_url": profile_avatar_url,
        "current_user": _current_web_username(request),
        "is_admin": _is_web_admin(_current_web_user(request)),
        "current_api_token": (
            user_store.get_api_token(_uid) if (user_store and (_uid := _current_web_user(request))) else ""
        ),
        "no_feeds": len(all_feed_urls) == 0,
    }
    _stream = templates.env.get_template("index.html").stream(_tmpl_ctx)
    _stream.enable_buffering(50)
    return StreamingResponse(_stream, media_type="text/html")


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
    _all_crops = list(_THUMB_COVER_POS) + ["contain", "smart"]
    keys: list[str] = []
    for url in urls:
        if not url:
            continue
        # Purge all crop variants (new format) plus the legacy no-crop key.
        for crop in _all_crops:
            keys.append(hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}|{crop}".encode()).hexdigest())
        old_key = hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}".encode()).hexdigest()
        keys.append(old_key)
        try:
            (THUMB_CACHE_DIR / f"{old_key}.jpg").unlink(missing_ok=True)
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

# Cover-mode crop: map crop value → (horizontal fraction, vertical fraction), 0=start 1=end.
_THUMB_COVER_POS: dict[str, tuple[float, float]] = {
    "cover":              (0.5, 0.5),
    "cover-top-left":     (0.0, 0.0),
    "cover-top":          (0.5, 0.0),
    "cover-top-right":    (1.0, 0.0),
    "cover-left":         (0.0, 0.5),
    "cover-right":        (1.0, 0.5),
    "cover-bottom-left":  (0.0, 1.0),
    "cover-bottom":       (0.5, 1.0),
    "cover-bottom-right": (1.0, 1.0),
    "left":               (0.0, 0.5),  # legacy alias
}


# Short-lived negative cache for /thumb fetches that failed (timeout, 5xx, blocked).
# A folder full of server-blocked images (e.g. Cloudflare-403 washingtonstatestandard)
# would otherwise re-hit every dead host on every page load, each tying up a worker
# thread. Keyed by the source image URL; brief TTL so transient failures recover.
_THUMB_FETCH_FAIL_CACHE: dict[str, float] = {}
_THUMB_FETCH_FAIL_LOCK = threading.Lock()
_THUMB_FETCH_FAIL_TTL = 10 * 60  # seconds
# Cap total time per /thumb fetch so one hanging host can't block a worker ~24s
# (httpx's float timeout applies per-phase, so 12.0 could mean connect+read = 24s).
_THUMB_FETCH_TIMEOUT = httpx.Timeout(6.0, connect=4.0)


def _thumb_fetch_recently_failed(url: str) -> bool:
    now = time.monotonic()
    with _THUMB_FETCH_FAIL_LOCK:
        exp = _THUMB_FETCH_FAIL_CACHE.get(url)
        if exp is None:
            return False
        if exp < now:
            _THUMB_FETCH_FAIL_CACHE.pop(url, None)
            return False
        return True


def _mark_thumb_fetch_failed(url: str) -> None:
    with _THUMB_FETCH_FAIL_LOCK:
        # Opportunistic prune so the dict can't grow unbounded.
        if len(_THUMB_FETCH_FAIL_CACHE) > 2000:
            now = time.monotonic()
            for k in [k for k, v in _THUMB_FETCH_FAIL_CACHE.items() if v < now]:
                _THUMB_FETCH_FAIL_CACHE.pop(k, None)
        _THUMB_FETCH_FAIL_CACHE[url] = time.monotonic() + _THUMB_FETCH_FAIL_TTL


@app.get("/thumb")
def thumbnail_proxy(url: str = Query(...), crop: str = Query(default="cover"), ms: str = Query(default=""), fz: str = Query(default="")) -> Response:
    """Fetch a remote image, resize it to thumbnail dimensions with LANCZOS, and
    return a cached JPEG.  This eliminates the progressive-load flicker caused by
    downloading full-size hero images into the small post-list thumbnail slot."""
    # Sanitized inline-SVG lead images arrive as data:image/svg+xml URIs. There's
    # nothing to rasterize/crop (they're vector); decode and serve the SVG directly
    # so every /thumb consumer (post list, Feed Properties, previews) renders them.
    if url.startswith("data:image/svg+xml,"):
        svg = unquote(url[len("data:image/svg+xml,"):])
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return Response(status_code=400)

    # Normalise: unknown values → cover; contain/smart use fit-scale (no crop).
    if crop not in _THUMB_COVER_POS and crop not in ("contain", "smart"):
        crop = "cover"

    # Per-feed SmartCrop min_scale arrives as the `ms` query param (clamped 0.5–1.0,
    # default 0.9). Set in Feed Properties; absent for feeds using the default.
    try:
        _smart_min_scale = min(1.0, max(0.5, float(ms or "0.9")))
    except (ValueError, TypeError):
        _smart_min_scale = 0.9

    # Per-feed Fill zoom multiplier arrives as the `fz` query param (clamped 0.5–2.0,
    # default 1.0). Values < 1.0 show more of the image with black letterbox bars;
    # values > 1.0 crop more aggressively than the default tight fill.
    try:
        _fill_zoom = min(2.0, max(0.5, float(fz or "1.0")))
    except (ValueError, TypeError):
        _fill_zoom = 1.0

    # "smart.2" busts old center-crop smart-mode entries when switching to content-aware crop.
    # Include min_scale / fill_zoom in the cache key so changing either busts stale thumbnails.
    if crop == "smart":
        _crop_cache_key = f"smart.2_m{_smart_min_scale:.2f}"
    elif crop in _THUMB_COVER_POS or crop == "cover":
        # "_p2" suffix busts entries cached before position-aware paste (zoom < 1 fix).
        _crop_cache_key = f"{crop}_z{_fill_zoom:.2f}" + ("_p2" if _fill_zoom < 1.0 else "")
    else:
        _crop_cache_key = crop
    cache_key = hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}|{_crop_cache_key}".encode()).hexdigest()
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

    # Short-circuit hosts that just failed: avoids re-hitting (and blocking a worker
    # on) a folder full of server-blocked images on every page load.
    if _thumb_fetch_recently_failed(url):
        return Response(status_code=502)

    try:
        # follow_redirects=False so url_guard.safe_get validates every hop
        # (SSRF: a public thumbnail URL must not redirect to an internal target).
        with httpx.Client(follow_redirects=False, timeout=_THUMB_FETCH_TIMEOUT, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
            _headers = {"User-Agent": READABILITY_USER_AGENT}
            resp = url_guard.safe_get(client, url, headers=_headers)
            # Hotlink protection: retry once with a same-origin Referer only after
            # an honest request is refused (see api_img_proxy / lead_images).
            if resp.status_code in _HOTLINK_REFUSAL_CODES:
                _referer = _same_origin_referer(url)
                if _referer:
                    resp = url_guard.safe_get(client, url, headers={**_headers, "Referer": _referer})
            if resp.status_code in (404, 410):
                # Image is permanently gone — null it out so it isn't re-attempted
                lead_image_service.invalidate_image_url(url)
            resp.raise_for_status()
            raw = resp.content
            src_content_type = resp.headers.get("content-type", "")
    except url_guard.UnsafeURLError:
        return Response(status_code=403)
    except Exception:
        _mark_thumb_fetch_failed(url)
        return Response(status_code=502)

    try:
        img = _PILImage.open(io.BytesIO(raw)).convert("RGB")
        iw, ih = img.size
        if crop == "smart":
            # Content-aware crop: use SmartCrop to find the most interesting
            # region at exactly the target dimensions. Falls back to the old
            # 40%-cap adaptive algorithm if the library is unavailable.
            _sc_done = False
            try:
                import smartcrop as _sc_mod
                # Downsample before analysis: SmartCrop's scoring is coarse
                # enough that 800px gives identical results at 2-3× less CPU.
                _SC_MAX = 800
                _sc_scale = min(1.0, _SC_MAX / max(iw, ih))
                _sc_img = (
                    img.resize(
                        (max(1, round(iw * _sc_scale)), max(1, round(ih * _sc_scale))),
                        _PILImage.Resampling.BILINEAR,
                    )
                    if _sc_scale < 1.0
                    else img
                )
                _sc_res = _sc_mod.SmartCrop().crop(_sc_img, _THUMB_W, _THUMB_H, min_scale=_smart_min_scale)
                _c = _sc_res["top_crop"]
                # Convert crop coords back to original image space then crop.
                _x1 = max(0, round(_c["x"] / _sc_scale))
                _y1 = max(0, round(_c["y"] / _sc_scale))
                _x2 = min(iw, round((_c["x"] + _c["width"]) / _sc_scale))
                _y2 = min(ih, round((_c["y"] + _c["height"]) / _sc_scale))
                img = img.crop((_x1, _y1, _x2, _y2))
                img = img.resize((_THUMB_W, _THUMB_H), _PILImage.Resampling.LANCZOS)
                _sc_done = True
            except Exception:
                pass
            if not _sc_done:
                _MAX_CROP = 0.4
                cover_s   = max(_THUMB_W / iw, _THUMB_H / ih)
                contain_s = min(_THUMB_W / iw, _THUMB_H / ih)
                cap_w = _THUMB_W / (iw * (1.0 - _MAX_CROP))
                cap_h = _THUMB_H / (ih * (1.0 - _MAX_CROP))
                scale = max(contain_s, min(cover_s, cap_w, cap_h))
                new_w = max(1, round(iw * scale))
                new_h = max(1, round(ih * scale))
                img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
                if new_w > _THUMB_W or new_h > _THUMB_H:
                    left = max(0, (new_w - _THUMB_W) // 2)
                    top  = max(0, (new_h - _THUMB_H) // 2)
                    img  = img.crop((left, top,
                                     left + min(new_w, _THUMB_W),
                                     top  + min(new_h, _THUMB_H)))
        elif crop == "contain":
            # Scale to fit; CSS handles letterboxing / blurred backdrop.
            scale = min(_THUMB_W / iw, _THUMB_H / ih)
            new_w = max(1, round(iw * scale))
            new_h = max(1, round(ih * scale))
            img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
            if new_w > _THUMB_W or new_h > _THUMB_H:
                left = max(0, (new_w - _THUMB_W) // 2)
                top  = max(0, (new_h - _THUMB_H) // 2)
                img  = img.crop((left, top,
                                 left + min(new_w, _THUMB_W),
                                 top  + min(new_h, _THUMB_H)))
        else:
            scale = max(_THUMB_W / iw, _THUMB_H / ih) * _fill_zoom
            new_w = max(1, round(iw * scale))
            new_h = max(1, round(ih * scale))
            img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
            if new_w >= _THUMB_W and new_h >= _THUMB_H:
                # Zoom ≥ 1.0: image fills frame — crop with anchor position.
                h_frac, v_frac = _THUMB_COVER_POS.get(crop, (0.5, 0.5))
                ex = max(0, new_w - _THUMB_W)
                ey = max(0, new_h - _THUMB_H)
                left = round(ex * h_frac)
                top = round(ey * v_frac)
                img = img.crop((left, top, left + _THUMB_W, top + _THUMB_H))
            else:
                # Zoom < 1.0: image smaller than frame — paste on black canvas at anchor position.
                canvas = _PILImage.new("RGB", (_THUMB_W, _THUMB_H), (0, 0, 0))
                h_frac, v_frac = _THUMB_COVER_POS.get(crop, (0.5, 0.5))
                paste_left = round((_THUMB_W - new_w) * h_frac)
                paste_top = round((_THUMB_H - new_h) * v_frac)
                canvas.paste(img, (paste_left, paste_top))
                img = canvas
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


@app.get("/feeds/discover")
def discover_feed_route(url: str = Query(...)):
    from services.feed_discovery import probe_url as _probe_url
    return JSONResponse(_probe_url(url.strip()))


def _guid_type(ids: list[str]) -> str:
    """Return 'url', 'string', or 'none' based on a sample of entry IDs."""
    if not ids:
        return "none"
    url_count = sum(1 for g in ids if g.startswith("http://") or g.startswith("https://"))
    return "url" if url_count > len(ids) // 2 else "string"


def _compare_one_feed(url: str) -> dict:
    """Fetch and parse one feed URL, returning metadata for the Add Feed comparison picker."""
    _headers = {"User-Agent": "Lectio/1.0 (feed comparison; +https://github.com/joshg253/Lectio)"}
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True, headers=_headers)
    except Exception as exc:
        return {"url": url, "error": str(exc).split("\n")[0][:120]}
    if not resp.is_success:
        return {"url": url, "error": f"HTTP {resp.status_code}"}

    ct = resp.headers.get("content-type", "").lower()

    # JSON Feed (feedparser doesn't handle it)
    if "json" in ct:
        try:
            import json as _j
            data = _j.loads(resp.text)
            items = data.get("items", [])
            image_count = sum(
                1 for item in items
                if item.get("image") or item.get("banner_image") or
                any(a.get("mime_type", "").startswith("image") for a in item.get("attachments") or [])
            )
            full_text_count = sum(
                1 for item in items
                if len(item.get("content_html") or item.get("content_text") or "") > 200
            )
            date_field = "none"
            if items:
                if items[0].get("date_published"):
                    date_field = "published"
                elif items[0].get("date_modified"):
                    date_field = "modified_only"
            latest = None
            if items:
                raw = items[0].get("date_published") or items[0].get("date_modified")
                if raw:
                    try:
                        latest = datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%b %-d, %Y %H:%M")
                    except Exception:
                        latest = raw[:10]
            sample_title = next((i["title"] for i in items if i.get("title")), None)
            return {"url": url, "format": "JSON Feed", "title": data.get("title"),
                    "entry_count": len(items), "image_count": image_count,
                    "full_text": full_text_count > len(items) // 2 if items else False,
                    "date_field": date_field,
                    "guid_type": _guid_type([i.get("id", "") for i in items if i.get("id")]),
                    "latest_date": latest, "sample_title": sample_title}
        except Exception as exc:
            return {"url": url, "error": f"JSON parse error: {exc}"}

    # RSS / Atom via feedparser
    parsed = feedparser.parse(resp.text)
    if parsed.bozo and not parsed.entries:
        return {"url": url, "error": "Could not parse feed"}

    entries = parsed.entries
    image_count = 0
    full_text_count = 0
    for entry in entries:
        has_enc = any(enc.get("type", "").startswith("image") for enc in entry.get("enclosures", []))
        has_media = bool(entry.get("media_content") or entry.get("media_thumbnail"))
        if has_enc or has_media:
            image_count += 1
        content_val = (entry.get("content") or [{}])[0].get("value") or ""
        if len(content_val) > 200:
            full_text_count += 1

    date_field = "none"
    if entries:
        if entries[0].get("published_parsed"):
            date_field = "published"
        elif entries[0].get("updated_parsed"):
            date_field = "modified_only"

    version_map = {
        "rss20": "RSS 2.0", "rss10": "RSS 1.0", "rss092": "RSS 0.92",
        "rss091n": "RSS 0.91", "atom10": "Atom 1.0", "atom03": "Atom 0.3",
    }
    ver = parsed.get("version", "")
    fmt = version_map.get(ver) or (ver.upper() if ver else None)
    if not fmt:
        # feedparser didn't identify the version; sniff the body
        snip = resp.text[:2000].lower()
        if "<rss" in snip:
            fmt = "RSS"
        elif "<feed" in snip:
            fmt = "Atom"
        elif "<rdf:rdf" in snip:
            fmt = "RDF"
        else:
            fmt = "Feed"

    latest = None
    if entries:
        ts = entries[0].get("published_parsed") or entries[0].get("updated_parsed")
        if ts:
            try:
                latest = datetime(*ts[:6]).strftime("%b %-d, %Y %H:%M")
            except Exception:
                pass

    sample_title = next((e.get("title") for e in entries if e.get("title")), None)

    return {"url": url, "format": fmt, "title": parsed.feed.get("title"),
            "entry_count": len(entries), "image_count": image_count,
            "full_text": full_text_count > len(entries) // 2 if entries else False,
            "date_field": date_field,
            "guid_type": _guid_type([e.get("id", "") for e in entries if e.get("id")]),
            "latest_date": latest, "sample_title": sample_title}


@app.get("/feeds/compare")
def compare_feeds_route(urls: list[str] = Query(..., alias="url")):
    from concurrent.futures import ThreadPoolExecutor
    capped = [u.strip() for u in urls[:6]]
    with ThreadPoolExecutor(max_workers=len(capped)) as ex:
        results = list(ex.map(_compare_one_feed, capped))
    return JSONResponse(results)


@app.post("/api/folders")
def api_create_folder(name: str = Form(...)):
    name = name.strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Name required"}, status_code=400)
    with get_meta_connection() as conn:
        root_id = get_root_folder_id(conn)
        conn.execute(
            "INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)",
            (name, root_id),
        )
        row = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
            (name, root_id),
        ).fetchone()
        folder_id = int(row["id"]) if row else root_id
    invalidate_meta_structure_cache()
    return JSONResponse({"ok": True, "id": folder_id, "name": name})


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

    # DeviantArt: when connected, "adding" an artist just Watches them on DeviantArt
    # — their posts arrive via the single combined Watch feed, so we don't create a
    # per-artist local feed. (If not connected, fall back to a standalone gallery feed.)
    da_username = deviantart_service.username_from_url(url)
    if da_username:
        token = get_deviantart_user_token()
        if token:
            try:
                ok, detail = deviantart_service.watch_user(token, da_username)
                msg = (f"Now watching {da_username} on DeviantArt — new posts appear in your Watch feed."
                       if ok else f"Couldn't watch {da_username}: {detail}")
            except deviantart_service.DeviantArtRateLimited:
                msg = "DeviantArt rate limit — try again in a bit."
            except Exception as exc:  # noqa: BLE001
                msg = f"DeviantArt watch failed: {exc}"
            return RedirectResponse(url=f"/?folder_id={folder_id}&message={quote_plus(msg)}", status_code=303)
        # Not connected → standalone gallery feed (best effort with app creds).
        cid, secret = get_deviantart_credentials()
        if not cid or not secret:
            return RedirectResponse(
                url=(f"/?folder_id={folder_id}"
                     f"&message={quote_plus('Connect your DeviantArt account in Settings first.')}"),
                status_code=303,
            )
        try:
            with get_meta_connection() as conn:
                with get_reader() as reader:
                    _fid, file_url = deviantart_service.create_deviantart_feed(conn, reader, da_username, cid, secret)
                conn.execute(
                    "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                    (folder_id, file_url),
                )
                _apply_deviantart_image_strategy(conn, file_url)
            invalidate_meta_structure_cache()
            msg = f"DeviantArt gallery added ({da_username})."
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[deviantart] add failed for %s: %s", da_username, exc)
            msg = f"DeviantArt add failed: {exc}"
        return RedirectResponse(url=f"/?folder_id={folder_id}&message={quote_plus(msg)}", status_code=303)

    # For non-YouTube URLs, probe whether the URL is a feed and run
    # auto-discovery if it looks like a webpage instead.
    discovery_escalated = False
    if not _is_youtube_url(url):
        candidates, discovery_escalated = discover_feed_urls_ex(url)
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
        # If the feed was only reachable with a browser identity, flag it so
        # reader's refresh fetch escalates too (otherwise it subscribes but never
        # updates). Good-citizen: only after an honest fetch was refused.
        if discovery_escalated:
            with get_meta_connection() as conn:
                flag_browser_ua_feed(conn, target_url, reason="discovery refused honest UA")
            _invalidate_browser_ua_cache()
            message += " (using browser identity — this site blocks default clients.)"
        # Fetch the feed's entries in the background so Add Feed returns
        # immediately. The first refresh can take 10-30s (network + parse +
        # per-entry processing); blocking on it made the dialog spin long
        # enough that users assumed it failed and re-added. The feed shows in
        # the sidebar right away; its entries populate a moment later (and the
        # scheduled refresh would catch it regardless).
        threading.Thread(
            target=_run_in_user_context,
            args=(tenancy.current_user_id(), feed_refresh_service.update_feeds, [target_url]),
            daemon=True,
        ).start()
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


@app.post("/feeds/fix-url-titles")
def fix_url_titles():
    """Find feeds whose display title is still a raw URL and queue them for refresh."""
    with get_reader() as reader:
        stale_urls = [
            f.url
            for f in reader.get_feeds()
            if not (f.resolved_title or f.title)
            or (f.resolved_title or f.title or "").lower().startswith("http")
        ]
    if stale_urls:
        threading.Thread(
            target=feed_refresh_service.update_feeds,
            args=(stale_urls,),
            daemon=True,
            name="fix-url-titles",
        ).start()
    return JSONResponse({"queued": len(stale_urls)})


@app.get("/folders/properties")
def folder_properties(folder_id: int):
    return JSONResponse(get_folder_properties(folder_id))


@app.post("/folders/cadence")
def set_folder_cadence(folder_id: int = Form(...), cadence_minutes: str = Form(...)):
    """Set or clear the per-folder refresh cadence."""
    try:
        minutes = int(cadence_minutes)
        if minutes < 0:
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "error": "cadence_minutes must be a non-negative integer"}, status_code=400)
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE folders SET cadence_minutes = ? WHERE id = ?",
            (minutes if minutes > 0 else None, folder_id),
        )
        # Clear the last-refresh timestamp so the next cycle picks up the new cadence immediately.
        set_setting(conn, f"{_FOLDER_CADENCE_LAST_REFRESH_PREFIX}{folder_id}", "0")
    return JSONResponse({"ok": True, "cadence_minutes": minutes if minutes > 0 else None})


_VALID_MANUAL_STRATEGIES = {"auto", "inline", "og_scrape", "media_rss", "enclosure", "none", "webcomic", "artwork"}


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


def _auto_tag_github_release_feeds() -> None:
    """Set strategy='og_scrape' and suppress list thumbnails for GitHub release feeds.

    GitHub generates a unique social-preview card (og:image) per release, making
    og_scrape the right strategy.  Thumbnails are suppressed because the card is
    contextual, not a post image.  Skips feeds where strategy is manually locked.
    """
    try:
        with get_meta_connection() as conn:
            now = time.time()
            rows = conn.execute(
                "SELECT DISTINCT feed_url FROM folder_feeds"
                " WHERE lower(feed_url) LIKE '%github.com%/releases.atom'"
            ).fetchall()
            for row in rows:
                feed_url = str(row["feed_url"])
                existing = conn.execute(
                    "SELECT strategy, manual FROM feed_lead_image_strategy WHERE feed_url = ?",
                    (feed_url,),
                ).fetchone()
                if existing and existing["manual"]:
                    continue
                if not (existing and existing["strategy"] == "og_scrape"):
                    conn.execute(
                        """
                        INSERT INTO feed_lead_image_strategy (feed_url, strategy, detected_at, manual)
                        VALUES (?, 'og_scrape', ?, 0)
                        ON CONFLICT(feed_url) DO UPDATE SET
                            strategy = 'og_scrape',
                            detected_at = excluded.detected_at
                        WHERE manual = 0
                        """,
                        (feed_url, now),
                    )
                    lead_image_service.store_feed_strategy(feed_url, "og_scrape", manual=False)
                # Ensure row exists and thumbnail is off.
                conn.execute(
                    "INSERT INTO feed_display_prefs (feed_url, show_lead_image_as_thumb)"
                    " VALUES (?, 0)"
                    " ON CONFLICT(feed_url) DO UPDATE SET show_lead_image_as_thumb = 0",
                    (feed_url,),
                )
    except Exception:
        LOGGER.exception("_auto_tag_github_release_feeds failed")


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
    # Clear cached images and the strategy comparison grid so entries
    # re-resolve under the new strategy.
    lead_image_service.clear_lead_image_cache(feed_url)
    try:
        with get_meta_connection() as conn:
            conn.execute("DELETE FROM feed_strategy_cache WHERE feed_url = ?", (feed_url,))
    except Exception:
        pass
    # Re-fetch images for recent entries using the new strategy.  Bypass the
    # chunk-backfill semaphore so this isn't silently dropped if another
    # backfill is in flight.
    if strategy not in ("auto", "none"):
        def _refetch(furl: str) -> None:
            try:
                with get_reader() as reader:
                    entries = list(reader.get_entries(feed=furl, limit=50))
                if strategy in ("inline", "artwork", "enclosure"):
                    # _do_backfill_entry_list skips inline/artwork/enclosure (no source-page
                    # fetches needed), so run inline extraction directly using full
                    # Entry objects which carry the feed content and enclosures.
                    for entry in entries:
                        furl_str = str(getattr(entry, "feed_url", "") or "")
                        eid = str(getattr(entry, "id", "") or "")
                        if not furl_str or not eid:
                            continue
                        url = lead_image_service.extract_entry_thumbnail_url(entry)
                        lead_image_service.store_entry_lead_image(furl_str, eid, url)
                else:
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
        # Capture the request's tenancy user; a raw daemon thread does not
        # inherit contextvars and would otherwise re-fetch as the default user,
        # writing to the wrong DB and leaving this user's cache empty.
        _uid = tenancy.current_user_id()
        threading.Thread(
            target=_run_in_user_context, args=(_uid, _refetch, feed_url), daemon=True
        ).start()
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
    # Turning Hide Shorts on clears the existing backlog immediately, not just
    # future refreshes.
    marked = 0
    if key == "hide_shorts" and value:
        try:
            marked = _mark_existing_shorts_read({feed_url})
        except Exception:
            LOGGER.exception("[display-prefs] error marking existing shorts read")
    return JSONResponse({"ok": True, "key": key, "value": value, "marked_read": marked})


@app.post("/feeds/backfill-hide-shorts")
def backfill_hide_shorts_route():
    """Re-run the hide-shorts cleanup across all feeds that have hide_shorts=1.

    Useful after the Shorts detection logic is improved (e.g. #shorts hashtag
    or cached duration), so previously-missed Shorts get marked read without
    the user having to re-toggle the pref on every feed."""
    with get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT feed_url FROM feed_display_prefs WHERE hide_shorts = 1"
        ).fetchall()
    feed_urls = {str(r["feed_url"]) for r in rows}
    if youtube_hide_shorts_global():
        with get_meta_connection() as conn:
            all_yt = conn.execute(
                "SELECT DISTINCT feed_url FROM feed_display_prefs"
                " WHERE feed_url LIKE 'https://www.youtube.com/%'"
            ).fetchall()
        feed_urls |= {str(r["feed_url"]) for r in all_yt}
    try:
        marked = _mark_existing_shorts_read(feed_urls)
    except Exception:
        LOGGER.exception("[backfill-hide-shorts] error")
        return JSONResponse({"error": "backfill failed"}, status_code=500)
    return JSONResponse({"ok": True, "marked": marked})


@app.post("/feeds/thumbnail-url")
def set_feed_thumbnail_url_route(
    feed_url: str = Form(...),
    thumbnail_url: str = Form(default=""),
):
    with get_meta_connection() as conn:
        cleaned = thumbnail_url.strip() or None
        upsert_feed_thumbnail_url(conn, feed_url, cleaned)
        # Pinning a thumbnail URL implies the user wants thumbnails visible —
        # re-enable them if the feed was previously set to Disabled.
        if cleaned:
            upsert_feed_display_pref(conn, feed_url, "show_lead_image_as_thumb", 1)
    return JSONResponse({"ok": True})


@app.post("/feeds/thumb-crop")
def set_feed_thumb_crop_route(
    feed_url: str = Form(...),
    crop: str = Form(...),
):
    if crop not in _VALID_THUMB_CROPS:
        return JSONResponse({"error": "invalid crop"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_thumb_crop(conn, feed_url, crop)
    return JSONResponse({"ok": True})


@app.post("/feeds/smart-min-scale")
def set_feed_smart_min_scale_route(
    feed_url: str = Form(...),
    min_scale: str = Form(default=""),  # empty → clear back to default
):
    parsed: float | None = None
    if min_scale.strip():
        try:
            parsed = float(min_scale)
        except ValueError:
            return JSONResponse({"error": "invalid min_scale"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_smart_min_scale(conn, feed_url, parsed)
    return JSONResponse({"ok": True})


@app.post("/feeds/fill-zoom")
def set_feed_fill_zoom_route(
    feed_url: str = Form(...),
    zoom: str = Form(default=""),  # empty → clear back to default 1.0
):
    parsed: float | None = None
    if zoom.strip():
        try:
            parsed = float(zoom)
        except ValueError:
            return JSONResponse({"error": "invalid zoom"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_fill_zoom(conn, feed_url, parsed)
    return JSONResponse({"ok": True})


@app.post("/feeds/thumb-strategy")
def set_feed_thumb_strategy_route(
    feed_url: str = Form(...),
    strategy: str = Form(default=""),  # Pydantic v2: empty form field → missing, so use default
):
    with get_meta_connection() as conn:
        upsert_feed_thumb_strategy(conn, feed_url, strategy or None)
    # When switching to auto (no override), backfill any entries not yet in
    # entry_lead_images so thumbnails appear without waiting for the next
    # scheduled refresh.  Already-cached entries are skipped by the backfill.
    if not strategy:
        def _backfill(furl: str) -> None:
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
        # Re-bind the request's tenancy user inside the daemon thread; otherwise
        # the backfill runs as the default user and writes to the wrong DB.
        _uid = tenancy.current_user_id()
        threading.Thread(
            target=_run_in_user_context, args=(_uid, _backfill, feed_url), daemon=True
        ).start()
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
    # Optional at the form layer (a youtube_playlist rule allows a blank keyword =
    # "all videos"); per-type validation below still requires it for other types.
    keyword: str = Form(""),
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
    webhook_url: str = Form(""),
    webhook_format: str = Form("generic"),
    webhook_batch: int = Form(0),
    yt_playlist_id: str = Form(""),
    yt_playlist_title: str = Form(""),
    yt_include_shorts: int = Form(0),
    yt_mark_read: int = Form(1),
    yt_min_minutes: int = Form(0),
    yt_max_minutes: int = Form(0),
):
    keyword = keyword.strip()
    if scope not in _HIGHLIGHT_VALID_SCOPES:
        return JSONResponse({"error": "invalid scope"}, status_code=400)
    if type == "deduplicate":
        if keyword not in _DEDUP_VALID_MATCH_METHODS:
            return JSONResponse({"error": "invalid match method for deduplicate rule"}, status_code=400)
        if scope == "feed":
            return JSONResponse({"error": "deduplicate needs at least two feeds — select multiple feeds or a folder"}, status_code=400)
        if scope == "feeds" and len(parse_feeds_scope_id(scope_id)) < 2:
            return JSONResponse({"error": "deduplicate needs at least two feeds selected"}, status_code=400)
    elif type == "youtube_playlist":
        # Keyword is optional for this rule (empty = add every new video in scope).
        if not yt_playlist_id.strip():
            return JSONResponse({"error": "a target playlist is required"}, status_code=400)
        if not youtube_oauth_connected():
            return JSONResponse({"error": "connect a YouTube account first"}, status_code=400)
    elif type == "instapaper":
        # Keyword is optional (blank = save every new entry in scope).
        if not is_instapaper_configured():
            return JSONResponse({"error": "configure Instapaper first"}, status_code=400)
    elif type == "quire":
        # Keyword is optional (blank = add every new entry in scope).
        if not is_quire_connected():
            return JSONResponse({"error": "connect Quire first"}, status_code=400)
        if not quire_project_oid():
            return JSONResponse({"error": "pick a Quire destination project in Settings first"}, status_code=400)
    else:
        if not keyword:
            return JSONResponse({"error": "keyword is required"}, status_code=400)
    if type == "webhook":
        webhook_url = webhook_url.strip()
        if not webhook_url or not url_guard.is_safe_outbound_url(webhook_url):
            return JSONResponse({"error": "a valid public webhook URL is required"}, status_code=400)
        if webhook_format not in WEBHOOK_VALID_FORMATS:
            return JSONResponse({"error": "invalid webhook format"}, status_code=400)
    with get_meta_connection() as conn:
        add_highlight_keyword(conn, scope, scope_id, keyword, color, bool(is_regex),
                              type, search_in, delivery, email_to, batch_time, batch_count,
                              bool(cc_me), enabled, dedup_window_hours, exclude_scope_ids,
                              webhook_url, webhook_format, bool(webhook_batch),
                              yt_playlist_id, yt_playlist_title,
                              bool(yt_include_shorts), bool(yt_mark_read),
                              yt_min_minutes, yt_max_minutes)
    return JSONResponse({"ok": True, "scope": scope, "scope_id": scope_id, "keyword": keyword,
                         "color": color, "is_regex": bool(is_regex), "type": type,
                         "search_in": search_in, "delivery": delivery,
                         "email_to": email_to, "batch_time": batch_time, "batch_count": batch_count,
                         "cc_me": bool(cc_me), "enabled": bool(enabled),
                         "dedup_window_hours": dedup_window_hours,
                         "exclude_scope_ids": exclude_scope_ids.strip(),
                         "webhook_url": webhook_url.strip(), "webhook_format": webhook_format,
                         "webhook_batch": bool(webhook_batch),
                         "yt_playlist_id": yt_playlist_id.strip(),
                         "yt_playlist_title": yt_playlist_title.strip(),
                         "yt_include_shorts": bool(yt_include_shorts),
                         "yt_mark_read": bool(yt_mark_read),
                         "yt_min_minutes": max(0, int(yt_min_minutes or 0)),
                         "yt_max_minutes": max(0, int(yt_max_minutes or 0))})


@app.post("/highlights/remove")
def remove_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    # Optional: youtube_playlist rules can have a blank keyword ("all videos"), so
    # remove/toggle (and the edit flow, which is remove+add) must accept "".
    keyword: str = Form(""),
):
    with get_meta_connection() as conn:
        remove_highlight_keyword(conn, scope, scope_id, keyword)
    return JSONResponse({"ok": True})


@app.post("/highlights/toggle")
def toggle_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(""),
    enabled: int = Form(...),
):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE highlight_keywords SET enabled = ? WHERE scope = ? AND scope_id = ? AND keyword = ?",
            (1 if enabled else 0, scope, scope_id, keyword.strip()),
        )
    return JSONResponse({"ok": True, "enabled": bool(enabled)})


@app.post("/rules/webhook-test")
def webhook_test_route(
    webhook_url: str = Form(...),
    webhook_format: str = Form("generic"),
):
    """Send a sample payload to a webhook URL so the user can verify the endpoint
    (IFTTT applet, Zapier hook, etc.) without waiting for a matching entry at the
    next refresh. SSRF-guarded by send_webhook."""
    webhook_url = webhook_url.strip()
    if not webhook_url or not url_guard.is_safe_outbound_url(webhook_url):
        return JSONResponse({"ok": False, "error": "a valid public webhook URL is required"}, status_code=400)
    if webhook_format not in WEBHOOK_VALID_FORMATS:
        webhook_format = "generic"
    sample = {
        "feed_url": "https://example.com/feed",
        "entry_id": "lectio-webhook-test",
        "title": "Lectio webhook test",
        "link": "https://example.com/test-article",
        "feed_title": "Lectio Test Feed",
        "excerpt": "This is a test payload sent from Lectio to verify your webhook.",
        "published": datetime.now().isoformat(),
        "tags": ["lectio", "test"],
    }
    ok, err = send_webhook(webhook_url, build_webhook_payload(sample, webhook_format))
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": err or "send failed"}, status_code=400)


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
    yt_include_shorts: int = Query(1),
    yt_min_minutes: int = Query(0),
    yt_max_minutes: int = Query(0),
):
    with get_meta_connection() as conn:
        if type == "deduplicate":
            match_method = keyword if keyword in _DEDUP_VALID_MATCH_METHODS else "slug"
            custom: set[str] | None = None
            if feed_urls:
                custom = {u.strip() for u in feed_urls.split(",") if u.strip()}
            result = _dry_run_dedup(conn, scope, scope_id, match_method, max(1, dedup_window_hours),
                                    exclude_scope_ids=exclude_scope_ids, custom_feed_urls=custom)
        elif type in ("highlight", "mark_as_read", "email_article", "webhook", "youtube_playlist", "instapaper"):
            # youtube_playlist's keyword is an optional filter — a blank keyword
            # previews every entry in scope (all videos); Shorts are excluded unless
            # the rule opts in, matching what the rule would actually add.
            _is_yt = type == "youtube_playlist"
            # youtube_playlist and instapaper treat a blank keyword as "all in scope".
            result = _dry_run_pattern(conn, scope, scope_id, keyword, bool(is_regex), search_in,
                                      match_all_if_empty=(_is_yt or type == "instapaper"),
                                      exclude_shorts=(_is_yt and not yt_include_shorts),
                                      min_secs=(max(0, yt_min_minutes) * 60 if _is_yt else 0),
                                      max_secs=(max(0, yt_max_minutes) * 60 if _is_yt else 0))
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


@app.post("/settings/deviantart/verify")
def verify_deviantart_credentials_route():
    """Validate the saved DeviantArt creds by requesting an app token."""
    cid, secret = get_deviantart_credentials()
    ok, message = deviantart_service.verify_credentials(cid, secret)
    return JSONResponse({"ok": ok, "message": message})


def _deviantart_redirect_uri(request: Request) -> str:
    """Callback URL DeviantArt redirects back to (must match the app whitelist)."""
    base = (os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/"))
    if base:
        return f"{base}/deviantart/callback"
    return str(request.url_for("deviantart_callback"))


@app.get("/deviantart/connect")
def deviantart_connect(request: Request):
    """Kick off the DeviantArt OAuth flow → redirect to their consent page."""
    cid, secret = get_deviantart_credentials()
    if not cid or not secret:
        return RedirectResponse(url="/?message=" + quote_plus("Add your DeviantArt API keys in Settings first."), status_code=303)
    state = secrets.token_urlsafe(24)
    verifier, challenge = deviantart_service.generate_pkce_pair()
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_OAUTH_STATE, state)
        set_setting(conn, SETTING_DEVIANTART_OAUTH_VERIFIER, verifier)
    url = deviantart_service.authorize_url(cid, _deviantart_redirect_uri(request), state, challenge)
    return RedirectResponse(url=url, status_code=303)


@app.get("/deviantart/callback")
def deviantart_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"DeviantArt authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_DEVIANTART_OAUTH_STATE) or ""
        verifier = get_setting(conn, SETTING_DEVIANTART_OAUTH_VERIFIER) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("DeviantArt authorization failed (bad state)."), status_code=303)
    cid, secret = get_deviantart_credentials()
    try:
        data = deviantart_service.exchange_code(cid, secret, code, _deviantart_redirect_uri(request), verifier)
        token = data["access_token"]
        username = deviantart_service.whoami(token)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"DeviantArt connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_ACCESS_TOKEN, token)
        if data.get("refresh_token"):
            set_setting(conn, SETTING_DEVIANTART_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_DEVIANTART_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        set_setting(conn, SETTING_DEVIANTART_USERNAME, username)
        delete_setting(conn, SETTING_DEVIANTART_OAUTH_STATE)
        delete_setting(conn, SETTING_DEVIANTART_OAUTH_VERIFIER)
    return RedirectResponse(url="/?message=" + quote_plus(f"DeviantArt connected as {username}."), status_code=303)


def _quire_redirect_uri(request: Request) -> str:
    """Callback URL Quire redirects back to (must match the app's whitelist)."""
    base = (os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/"))
    if base:
        return f"{base}/quire/callback"
    return str(request.url_for("quire_callback"))


@app.get("/quire/connect")
def quire_connect(request: Request):
    """Kick off the Quire OAuth flow → redirect to their consent page."""
    cid, secret = get_quire_credentials()
    if not cid or not secret:
        return RedirectResponse(url="/?message=" + quote_plus("Add your Quire API keys in Settings first."), status_code=303)
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_QUIRE_OAUTH_STATE, state)
    url = quire_service.authorize_url(cid, _quire_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@app.get("/quire/callback")
def quire_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"Quire authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_QUIRE_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("Quire authorization failed (bad state)."), status_code=303)
    cid, secret = get_quire_credentials()
    try:
        data = quire_service.exchange_code(cid, secret, code, _quire_redirect_uri(request))
        token = data["access_token"]
        try:
            username = quire_service.whoami(token)
        except Exception:
            username = ""
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"Quire connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_QUIRE_ACCESS_TOKEN, token)
        if data.get("refresh_token"):
            set_setting(conn, SETTING_QUIRE_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_QUIRE_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        set_setting(conn, SETTING_QUIRE_USERNAME, username)
        delete_setting(conn, SETTING_QUIRE_OAUTH_STATE)
    notice = f"Quire connected as {username}." if username else "Quire connected."
    return RedirectResponse(url="/?message=" + quote_plus(notice + " Pick a destination project in Settings."), status_code=303)


@app.get("/api/quire/projects")
def quire_projects_route():
    """List the connected user's Quire projects for the Settings destination picker."""
    token = get_quire_user_token()
    if not token:
        return JSONResponse({"ok": False, "error": "Quire not connected."}, status_code=503)
    try:
        projects = quire_service.list_projects(token)
    except quire_service.QuireRateLimited:
        return JSONResponse({"ok": False, "error": "Quire rate limit hit — try again shortly."}, status_code=429)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[quire] project list failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "projects": projects})


def _youtube_oauth_redirect_uri(request: Request) -> str:
    """Callback URL Google redirects back to — MUST exactly match the URI
    registered on the OAuth client in Google Cloud."""
    base = (os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/"))
    if base:
        return f"{base}/integrations/youtube/oauth/callback"
    return str(request.url_for("youtube_oauth_callback"))


@app.get("/integrations/youtube/oauth/connect")
def youtube_oauth_connect(request: Request):
    """Kick off the YouTube OAuth flow → redirect to Google's consent page."""
    cid, secret = get_youtube_oauth_credentials()
    if not cid or not secret:
        return RedirectResponse(url="/?message=" + quote_plus("YouTube OAuth client is not configured (set YOUTUBE_OAUTH_CLIENT_ID/SECRET)."), status_code=303)
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_YT_OAUTH_STATE, state)
    url = youtube_oauth_service.authorize_url(cid, _youtube_oauth_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@app.get("/integrations/youtube/oauth/callback")
def youtube_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them per-user."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"YouTube authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_YT_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("YouTube authorization failed (bad state)."), status_code=303)
    cid, secret = get_youtube_oauth_credentials()
    try:
        data = youtube_oauth_service.exchange_code(cid, secret, code, _youtube_oauth_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"YouTube connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_YT_OAUTH_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_YT_OAUTH_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_YT_OAUTH_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        delete_setting(conn, SETTING_YT_OAUTH_STATE)
    return RedirectResponse(url="/?message=" + quote_plus("YouTube account connected."), status_code=303)


@app.post("/integrations/youtube/oauth/disconnect")
def youtube_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (SETTING_YT_OAUTH_ACCESS_TOKEN, SETTING_YT_OAUTH_REFRESH_TOKEN,
                    SETTING_YT_OAUTH_TOKEN_EXPIRES_AT, SETTING_YT_OAUTH_STATE):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Pinterest OAuth + save-to-board (per-entry "Pin" button)
# ---------------------------------------------------------------------------
def _pinterest_oauth_redirect_uri(request: Request) -> str:
    """Callback URL Pinterest redirects back to — MUST exactly match the URI
    registered on the OAuth app in the Pinterest developer console."""
    base = (os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/"))
    if base:
        return f"{base}/integrations/pinterest/oauth/callback"
    return str(request.url_for("pinterest_oauth_callback"))


@app.get("/integrations/pinterest/oauth/connect")
def pinterest_oauth_connect(request: Request):
    """Kick off the Pinterest OAuth flow → redirect to Pinterest's consent page."""
    cid, secret = get_pinterest_oauth_credentials()
    if not cid or not secret:
        return RedirectResponse(url="/?message=" + quote_plus("Pinterest OAuth client is not configured (set PINTEREST_OAUTH_CLIENT_ID/SECRET)."), status_code=303)
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_PINTEREST_OAUTH_STATE, state)
    url = pinterest_oauth_service.authorize_url(cid, _pinterest_oauth_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@app.get("/integrations/pinterest/oauth/callback")
def pinterest_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them per-user."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"Pinterest authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_PINTEREST_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("Pinterest authorization failed (bad state)."), status_code=303)
    cid, secret = get_pinterest_oauth_credentials()
    try:
        data = pinterest_oauth_service.exchange_code(cid, secret, code, _pinterest_oauth_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"Pinterest connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_PINTEREST_OAUTH_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_PINTEREST_OAUTH_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        delete_setting(conn, SETTING_PINTEREST_OAUTH_STATE)
    return RedirectResponse(url="/?message=" + quote_plus("Pinterest account connected."), status_code=303)


@app.post("/integrations/pinterest/oauth/disconnect")
def pinterest_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (SETTING_PINTEREST_OAUTH_ACCESS_TOKEN, SETTING_PINTEREST_OAUTH_REFRESH_TOKEN,
                    SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT, SETTING_PINTEREST_OAUTH_STATE):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Reddit OAuth routes
# ---------------------------------------------------------------------------

@app.get("/integrations/reddit/oauth/connect")
def reddit_oauth_connect(request: Request):
    """Kick off the Reddit OAuth flow → redirect to Reddit's consent page."""
    cid, secret = get_reddit_credentials()
    if not cid or not secret:
        return RedirectResponse(url="/?message=" + quote_plus("Reddit OAuth client is not configured (enter client ID and secret in Integrations → Reddit)."), status_code=303)
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_REDDIT_OAUTH_STATE, state)
    url = reddit_service.authorize_url(cid, _reddit_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@app.get("/integrations/reddit/oauth/callback")
def reddit_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them per-user."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"Reddit authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_REDDIT_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("Reddit authorization failed (bad state)."), status_code=303)
    cid, secret = get_reddit_credentials()
    try:
        data = reddit_service.exchange_code(cid, secret, code, _reddit_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"Reddit connect failed: {exc}"), status_code=303)
    username = ""
    try:
        me = reddit_service.get_me(data["access_token"])
        username = me.get("name", "")
    except Exception:  # noqa: BLE001
        pass
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_REDDIT_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_REDDIT_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_REDDIT_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        if username:
            set_setting(conn, SETTING_REDDIT_USERNAME, username)
        delete_setting(conn, SETTING_REDDIT_OAUTH_STATE)
    msg = f"Reddit connected as /u/{username}." if username else "Reddit account connected."
    return RedirectResponse(url="/?message=" + quote_plus(msg), status_code=303)


@app.post("/integrations/reddit/oauth/disconnect")
def reddit_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (SETTING_REDDIT_ACCESS_TOKEN, SETTING_REDDIT_REFRESH_TOKEN,
                    SETTING_REDDIT_TOKEN_EXPIRES_AT, SETTING_REDDIT_OAUTH_STATE,
                    SETTING_REDDIT_USERNAME):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Reddit submit route
# ---------------------------------------------------------------------------

@app.post("/api/reddit/submit")
async def reddit_submit_route(request: Request):
    """Submit an article link to a subreddit."""
    body = await request.json()
    subreddit = str(body.get("subreddit", "")).strip().lstrip("r/").strip("/")
    title = str(body.get("title", "")).strip()
    url = str(body.get("url", "")).strip()
    feed_url_param = str(body.get("feed_url", "")).strip()
    entry_id_param = str(body.get("entry_id", "")).strip()

    if not subreddit:
        return JSONResponse({"ok": False, "error": "subreddit is required"}, status_code=400)

    token = get_reddit_user_token()
    if not token:
        return JSONResponse({"connected": False, "error": "Reddit not connected"}, status_code=401)

    # Resolve link and title from entry if not provided directly.
    if feed_url_param and entry_id_param and not url:
        try:
            with get_reader() as reader:
                entry = reader.get_entry((feed_url_param, entry_id_param))
            url = str(getattr(entry, "link", "") or "")
            if not title:
                title = str(getattr(entry, "title", "") or url)
        except Exception:  # noqa: BLE001
            pass

    if not url:
        return JSONResponse({"ok": False, "error": "could not determine article URL"}, status_code=400)

    title = title or url
    try:
        result = reddit_service.submit_link(token, subreddit, title, url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "post_url": result.get("url", "")})


# ---------------------------------------------------------------------------
# Inoreader OAuth + import routes
# ---------------------------------------------------------------------------

def _inoreader_redirect_uri(request: Request) -> str:
    """The registered callback URL — must exactly match Inoreader developer console."""
    base = LECTIO_PUBLIC_URL
    if base:
        return f"{base}/inoreader/oauth/callback"
    return str(request.url_for("inoreader_oauth_callback"))


@app.get("/integrations/inoreader/oauth/connect")
def inoreader_oauth_connect(request: Request):
    """Kick off the Inoreader OAuth flow → redirect to Inoreader's consent page."""
    cid, secret = get_inoreader_credentials()
    if not cid or not secret:
        return RedirectResponse(
            url="/?message=" + quote_plus("Inoreader OAuth client is not configured (set INOREADER_CLIENT_ID/SECRET or enter them in Settings → Integrations → Inoreader)."),
            status_code=303,
        )
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_OAUTH_STATE, state)
    url = inoreader_service.authorize_url(cid, _inoreader_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@app.get("/inoreader/oauth/callback", name="inoreader_oauth_callback")
def inoreader_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """OAuth redirect target registered at inoreader.com developer console.

    The exact path ``/inoreader/oauth/callback`` must match what is registered
    there — do not rename or move this route without updating the console.
    """
    if error:
        return RedirectResponse(
            url="/?message=" + quote_plus(f"Inoreader authorization failed: {error}"),
            status_code=303,
        )
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_INOREADER_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(
            url="/?message=" + quote_plus("Inoreader authorization failed (bad state)."),
            status_code=303,
        )
    cid, secret = get_inoreader_credentials()
    try:
        data = inoreader_service.exchange_code(cid, secret, code, _inoreader_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url="/?message=" + quote_plus(f"Inoreader connect failed: {exc}"),
            status_code=303,
        )
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_INOREADER_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_INOREADER_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        delete_setting(conn, SETTING_INOREADER_OAUTH_STATE)
    return RedirectResponse(
        url="/?message=" + quote_plus("Inoreader account connected. Go to Settings → Integrations → Inoreader to start the migration."),
        status_code=303,
    )


@app.post("/integrations/inoreader/oauth/disconnect")
def inoreader_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_INOREADER_ACCESS_TOKEN,
            SETTING_INOREADER_REFRESH_TOKEN,
            SETTING_INOREADER_TOKEN_EXPIRES_AT,
            SETTING_INOREADER_OAUTH_STATE,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


@app.get("/integrations/inoreader/import/status")
def inoreader_import_status():
    """Return the current import checkpoint state as JSON."""
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_INOREADER_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"phase": None, "done": False, "running": False})
    try:
        state = json.loads(raw)
    except Exception:
        return JSONResponse({"phase": "error", "done": False, "running": False})
    return JSONResponse({**state, "running": False})


@app.post("/integrations/inoreader/import/start")
def inoreader_import_start(delete_mode: int = Form(default=0), since: str = Form(default="")):
    """Initialise (or reinitialise) the API-driven import state and run the first drip step.

    ``since`` — optional ISO date (YYYY-MM-DD) or Unix timestamp string; only fetch
    items newer than this. Useful when a file import already covered older history.
    """
    if not inoreader_connected():
        return JSONResponse({"ok": False, "error": "Not connected"}, status_code=400)
    since_ot: int | None = None
    if since:
        try:
            since_ot = int(since)
        except ValueError:
            try:
                from datetime import date
                since_ot = int(datetime.fromisoformat(since.strip()).timestamp())
            except Exception:
                pass
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "subscriptions",
        "subs_added": 0,
        "label_ids": [],
        "label_cursor": 0,
        "label_continuation": None,
        "starred_continuation": None,
        "items_tagged": 0,
        "items_starred": 0,
        "delete_mode": bool(delete_mode),
        "since_ot": since_ot,
        "z1_remaining": None,
        "error": None,
        "done": False,
        "started_at": now,
        "updated_at": now,
    }
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))
    # Run the first drip step immediately (best-effort, errors surface in status).
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_drip_step),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@app.post("/integrations/inoreader/import/run")
def inoreader_import_run():
    """Manually trigger one drip step (for "Run now" button in UI)."""
    if not inoreader_connected():
        return JSONResponse({"ok": False, "error": "Not connected"}, status_code=400)
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_drip_step),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True})


@app.post("/integrations/inoreader/import/reset")
def inoreader_import_reset():
    """Clear the import checkpoint so the migration can be restarted."""
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_INOREADER_IMPORT_STATE)
    return JSONResponse({"ok": True})


@app.post("/integrations/inoreader/import/local")
def inoreader_import_local():
    """Server-side import: scan the configured export directory for JSON files and
    import them all in background. Supports both InoreaderExportTool format (plain
    list) and native Inoreader export format (dict with 'items' key).

    The directory is set via Settings → Integrations → Inoreader (export dir field)
    or the INOREADER_EXPORT_DIR env var. It must contain .json files — typically one
    per label from ExportTool, plus starred-*.json from a native export ZIP.
    """
    export_dir = get_runtime_setting(SETTING_INOREADER_EXPORT_DIR, "").strip()
    if not export_dir:
        return JSONResponse({"ok": False, "error": "No export directory configured."}, status_code=400)
    dir_path = Path(export_dir)
    if not dir_path.is_dir():
        return JSONResponse({"ok": False, "error": f"Path is not a directory: {export_dir}"}, status_code=400)
    json_files = sorted(dir_path.glob("*.json"))
    if not json_files:
        return JSONResponse({"ok": False, "error": "No .json files found in that directory."}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "local_files",
        "files_total": len(json_files),
        "files_done": 0,
        "current_file": "",
        "subs_added": 0,
        "items_tagged": 0,
        "items_starred": 0,
        "errors": 0,
        "done": False,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))

    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_local_import_worker, json_files),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "files": len(json_files), "state": state})


@app.post("/integrations/inoreader/import/upload")
async def inoreader_import_upload(files: list[UploadFile] = File(...)):
    """Browser-upload import: accept one or more .json files or a single .zip.

    Extracts all .json files to a server-side temp dir and starts the same
    background worker as the server-path import. Returns immediately; poll
    /integrations/inoreader/import/status for progress.
    """
    # Read all uploaded content first — this can take many seconds for large ZIPs.
    # Only touch the meta DB after all I/O is done to avoid lock timeouts.
    tmp_dir = Path(tempfile.mkdtemp(prefix="ino_import_"))
    json_paths: list[Path] = []

    for upload in files:
        name = upload.filename or ""
        content = await upload.read()
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    for member in zf.namelist():
                        if member.lower().endswith(".json") and not member.startswith("__MACOSX"):
                            out = tmp_dir / Path(member).name
                            out.write_bytes(zf.read(member))
                            json_paths.append(out)
            except zipfile.BadZipFile:
                return JSONResponse({"ok": False, "error": "Invalid or corrupt ZIP file."}, status_code=400)
        elif name.lower().endswith(".json"):
            out = tmp_dir / name
            out.write_bytes(content)
            json_paths.append(out)

    if not json_paths:
        return JSONResponse({"ok": False, "error": "No .json files found in upload."}, status_code=400)

    # All file I/O is done. Open a fresh short-lived connection with a generous
    # timeout — the thread-local pool connection can't be used safely here because
    # async coroutines on the same thread share it and may have it mid-transaction.
    json_paths = sorted(json_paths)
    now = datetime.now(timezone.utc).isoformat()
    uid = tenancy.current_user_id()
    _meta_conn = sqlite3.connect(str(tenancy.meta_db_path(uid)), timeout=30.0)
    _meta_conn.row_factory = sqlite3.Row
    _meta_conn.execute("PRAGMA journal_mode=WAL")
    _meta_conn.execute("PRAGMA busy_timeout=30000")
    try:
        raw = get_setting(_meta_conn, SETTING_INOREADER_IMPORT_STATE) or "{}"
        existing = json.loads(raw)
        if existing.get("phase") == "local_files" and not existing.get("done") and not existing.get("error"):
            return JSONResponse({"ok": False, "error": "An import is already running. Reset first."}, status_code=409)
        state = {
            "phase": "local_files",
            "files_total": len(json_paths),
            "files_done": 0,
            "current_file": "",
            "subs_added": 0,
            "items_tagged": 0,
            "items_starred": 0,
            "errors": 0,
            "done": False,
            "error": None,
            "started_at": now,
            "updated_at": now,
        }
        set_setting(_meta_conn, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))
        _meta_conn.commit()
    finally:
        _meta_conn.close()

    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_local_import_worker, json_paths, tmp_dir),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "files": len(json_paths)})


def _inoreader_local_import_worker(json_files: list, cleanup_dir: Path | None = None) -> None:
    """Background worker: iterate JSON files and import all items."""
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_INOREADER_IMPORT_STATE) or "{}"
    try:
        state = json.loads(raw)
    except Exception:
        return

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as _c:
            set_setting(_c, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))

    try:
        _run_import_loop(json_files, state, _save)
    except Exception as exc:
        state["error"] = f"Worker crashed: {exc}"
        state["done"] = False
        _save()
        LOGGER.exception("[inoreader-local] worker crashed")
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def _run_import_loop(json_files: list, state: dict, _save) -> None:
    reader_db = str(tenancy.reader_db_path())
    for i, json_path in enumerate(json_files):
        state["current_file"] = json_path.name
        state["files_done"] = i
        _save()
        try:
            with open(json_path, encoding="utf-8") as fh:
                raw_data = json.load(fh)
            items = inoreader_service.parse_export_json(raw_data)
        except Exception as exc:
            LOGGER.warning("[inoreader-local] failed to parse %s: %s", json_path.name, exc)
            state["errors"] = state.get("errors", 0) + 1
            continue

        # Canonicalize each item's feed URL once, in place, so variants
        # (old.reddit, ?alt=rss, trailing slash) merge into existing feeds
        # rather than spawning uncategorized duplicates — and so the subscribe
        # loop and the per-entry tag/star keying below stay in sync.
        _canonicalize_item_feed_urls(items)

        new_feed_urls = {item["feed_url"] for item in items if item["feed_url"]}
        if new_feed_urls:
            with get_reader() as reader:
                existing = {str(f.url) for f in reader.get_feeds()}
                for furl in new_feed_urls:
                    if furl not in existing:
                        try:
                            reader.add_feed(furl, exist_ok=True)
                            state["subs_added"] = state.get("subs_added", 0) + 1
                            existing.add(furl)
                        except Exception:
                            pass

        # Apply stars and tags, inserting the entry into the reader if not yet fetched.
        # Reader entries use the feed's <guid>/<id> element as their ID, which often
        # differs from the canonical web URL (e.g. WordPress ?p=123 vs /slug).
        # Strategy:
        #   1. Try lookup by (feed_url, canonical_url) — works when id == link.
        #   2. Try lookup by link column directly in the reader DB — handles id≠link.
        #   3. If still not found, synthesize and add_entry so tags/stars apply now.
        with get_reader() as reader:
            with get_meta_connection() as conn:
                with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                    rconn.row_factory = sqlite3.Row
                    for item in items:
                        entry_url = item["url"]
                        # feed_url was canonicalized in place above, so tags/
                        # stars key off the canonical feed URL and land on the
                        # (possibly pre-existing) merged feed's entries.
                        feed_url = item["feed_url"]
                        if not entry_url or not feed_url:
                            continue

                        tagged = False
                        starred = False

                        # Tags from labels: lowercase = Lectio tag, Mixed Case = folder (skip).
                        label_tags = [
                            f"{MANUAL_TAG_KEY_PREFIX}{lbl.lower()}"
                            for lbl in item["labels"]
                            if inoreader_service.label_is_tag(lbl)
                        ]
                        if label_tags or item["starred"]:
                            entry = None
                            try:
                                if feed_url:
                                    # 1. Fast path: entry ID == canonical URL.
                                    entry = reader.get_entry((feed_url, entry_url), None)

                                    if entry is None:
                                        # 2. Look up by link column (id ≠ link case).
                                        row = rconn.execute(
                                            "SELECT id FROM entries WHERE feed = ? AND link = ? LIMIT 1",
                                            (feed_url, entry_url),
                                        ).fetchone()
                                        if row:
                                            entry = reader.get_entry((feed_url, row["id"]), None)

                                    if entry is None:
                                        # 3. Not in reader at all — synthesize and insert.
                                        # Ensure the feed exists first (handles feeds that
                                        # are broken/unfetchable but still need entries).
                                        try:
                                            reader.add_feed(feed_url, exist_ok=True)
                                        except Exception:
                                            pass
                                        pub = item.get("published")
                                        entry_dict: dict = {
                                            "feed_url": feed_url,
                                            "id": entry_url,
                                            "title": item.get("title") or "",
                                            "link": entry_url,
                                        }
                                        if pub:
                                            entry_dict["published"] = datetime.fromtimestamp(pub, timezone.utc)
                                        if item.get("content"):
                                            entry_dict["content"] = [{"value": item["content"]}]
                                        try:
                                            reader.add_entry(entry_dict)
                                            entry = reader.get_entry((feed_url, entry_url), None)
                                        except Exception:
                                            pass
                                else:
                                    # JSON Feed format: no per-item feed URL — search by link across all feeds.
                                    row = rconn.execute(
                                        "SELECT id, feed FROM entries WHERE link = ? LIMIT 1",
                                        (entry_url,),
                                    ).fetchone()
                                    if row is None:
                                        row = rconn.execute(
                                            "SELECT id, feed FROM entries WHERE id = ? LIMIT 1",
                                            (entry_url,),
                                        ).fetchone()
                                    if row:
                                        entry = reader.get_entry((row["feed"], row["id"]), None)

                                if entry:
                                    for tag_key in label_tags:
                                        try:
                                            reader.set_tag(entry, tag_key)
                                            tagged = True
                                        except Exception:
                                            pass
                                    if item["starred"]:
                                        conn.execute(
                                            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                            (entry.feed_url, entry.id),
                                        )
                                        starred = True
                            except Exception:
                                pass

                        if tagged:
                            state["items_tagged"] = state.get("items_tagged", 0) + 1
                        if starred:
                            state["items_starred"] = state.get("items_starred", 0) + 1

    state["files_done"] = len(json_files)
    state["current_file"] = ""
    state["done"] = True
    _save()
    LOGGER.info(
        "[inoreader-local] done: %d files, %d subs, %d tagged, %d starred, %d errors",
        len(json_files), state["subs_added"], state["items_tagged"],
        state["items_starred"], state["errors"],
    )


@app.post("/integrations/inoreader/import/json")
async def inoreader_import_json(request: Request, file: UploadFile = File(...)):
    """Path B: import from an InoreaderExportTool JSON file (no API calls).

    Subscribes any unknown feeds and applies starred/label state to any entries
    that are already in the reader (best-effort — new feeds won't have entries
    until after their first fetch).
    """
    try:
        raw = await file.read()
        data = json.loads(raw)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Invalid JSON: {exc}"}, status_code=400)

    items = inoreader_service.parse_export_json(data if isinstance(data, list) else data.get("items", []))

    # Canonicalize each incoming feed URL once so the subscribe loop and the
    # per-entry tag/star keying below share the same value — variants merge into
    # an existing subscription instead of creating duplicates.
    _canonicalize_item_feed_urls(items)

    feeds_added = 0
    items_starred = 0
    items_tagged = 0

    # Subscribe missing feeds
    feed_urls = {item["feed_url"] for item in items if item["feed_url"]}
    with get_reader() as reader:
        existing = {str(f.url) for f in reader.get_feeds()}
        for furl in feed_urls:
            if furl not in existing:
                try:
                    reader.add_feed(furl, exist_ok=True)
                    feeds_added += 1
                except Exception:
                    pass

    # Apply stars and tags to entries already in reader
    with get_reader() as reader:
        with get_meta_connection() as conn:
            for item in items:
                entry_url = item["url"]
                feed_url = item["feed_url"]
                if not entry_url or not feed_url:
                    continue
                # Tag
                for label_name in item["labels"]:
                    if inoreader_service.label_is_tag(label_name):
                        tag_key = f"{MANUAL_TAG_KEY_PREFIX}{label_name.lower()}"
                        try:
                            entry = reader.get_entry((feed_url, entry_url), None)
                            if entry:
                                reader.set_tag(entry, tag_key)
                                items_tagged += 1
                        except Exception:
                            pass
                # Star
                if item["starred"]:
                    try:
                        entry = reader.get_entry((feed_url, entry_url), None)
                        if entry:
                            conn.execute(
                                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                (feed_url, entry_url),
                            )
                            items_starred += 1
                    except Exception:
                        pass

    return JSONResponse({
        "ok": True,
        "feeds_added": feeds_added,
        "items_starred": items_starred,
        "items_tagged": items_tagged,
        "total_items": len(items),
    })


def _api_resolve_entry(reader, rconn, feed_url: str, entry_url: str, item: dict):
    """3-pass entry lookup for API drip items (same strategy as file import).

    1. get_entry by id == canonical URL (fast path)
    2. link-column lookup (handles id≠link WordPress/Atom feeds)
    3. add_entry synthesis (entry not yet fetched; ensures feed exists first)
    Returns the Entry or None.
    """
    entry = reader.get_entry((feed_url, entry_url), None)
    if entry is None:
        row = rconn.execute(
            "SELECT id FROM entries WHERE feed = ? AND link = ? LIMIT 1",
            (feed_url, entry_url),
        ).fetchone()
        if row:
            entry = reader.get_entry((feed_url, row["id"]), None)
    if entry is None:
        try:
            reader.add_feed(feed_url, exist_ok=True)
        except Exception:
            pass
        pub = item.get("published")
        summary = item.get("summary") or {}
        entry_dict: dict = {
            "feed_url": feed_url,
            "id": entry_url,
            "title": item.get("title") or "",
            "link": entry_url,
        }
        if pub:
            entry_dict["published"] = datetime.fromtimestamp(pub, timezone.utc)
        if summary.get("content"):
            entry_dict["content"] = [{"value": summary["content"]}]
        try:
            reader.add_entry(entry_dict)
            entry = reader.get_entry((feed_url, entry_url), None)
        except Exception:
            pass
    return entry


def _apply_migration_items(items: list[dict], state: dict, save_fn) -> None:
    """Apply a list of normalized migration items to the current user's Lectio data.

    Normalized item shape:
      url        — article canonical URL (empty string = subscription-only record)
      title      — article title
      published  — Unix timestamp (int) or None
      feed_url   — feed subscription URL
      feed_title — feed display name
      content    — article HTML
      starred    — True to star in Lectio
      tags       — list of plain tag name strings (will be lower-cased)
      folder     — folder name for the feed (empty = no folder assignment)

    Updates ``state`` counters (subs_added, items_tagged, items_starred, errors)
    and calls ``save_fn()`` after the subscription phase and after the tagging
    phase.
    """
    reader_db = str(tenancy.reader_db_path())

    # Canonicalize every incoming feed URL once, up front, so both the subscribe
    # phase and the per-entry tag/star keying below use the same canonical value
    # (merging variants into an existing subscription instead of duplicating).
    _canonicalize_item_feed_urls(items)

    # --- Phase 1: subscribe feeds and assign folders ---
    feed_folders: dict[str, str] = {}
    all_feed_urls: set[str] = set()
    for item in items:
        furl = item.get("feed_url") or ""
        if furl:
            all_feed_urls.add(furl)
            folder = item.get("folder") or ""
            if folder and furl not in feed_folders:
                feed_folders[furl] = folder

    with get_reader() as reader:
        existing = {str(f.url) for f in reader.get_feeds()}
        for furl in all_feed_urls:
            if furl not in existing:
                try:
                    reader.add_feed(furl, exist_ok=True)
                    state["subs_added"] = state.get("subs_added", 0) + 1
                    existing.add(furl)
                except Exception:
                    pass

    if feed_folders:
        with get_meta_connection() as conn:
            for furl, folder_name in feed_folders.items():
                try:
                    folder_id = _get_or_create_folder_by_name(conn, folder_name)
                    conn.execute(
                        "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                        (folder_id, furl),
                    )
                except Exception:
                    pass
        invalidate_meta_structure_cache()

    save_fn()

    # --- Phase 2: apply tags and stars to articles ---
    with get_reader() as reader:
        with get_meta_connection() as conn:
            with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                rconn.row_factory = sqlite3.Row
                for item in items:
                    entry_url = item.get("url") or ""
                    feed_url = item.get("feed_url") or ""
                    if not entry_url:
                        continue

                    tag_keys = [
                        f"{MANUAL_TAG_KEY_PREFIX}{t.strip().lower()}"
                        for t in (item.get("tags") or [])
                        if t and t.strip()
                    ]
                    want_star = bool(item.get("starred"))
                    if not tag_keys and not want_star:
                        continue

                    try:
                        entry = None
                        if feed_url:
                            entry = reader.get_entry((feed_url, entry_url), None)
                            if entry is None:
                                row = rconn.execute(
                                    "SELECT id FROM entries WHERE feed = ? AND link = ? LIMIT 1",
                                    (feed_url, entry_url),
                                ).fetchone()
                                if row:
                                    entry = reader.get_entry((feed_url, row["id"]), None)
                            if entry is None:
                                try:
                                    reader.add_feed(feed_url, exist_ok=True)
                                except Exception:
                                    pass
                                entry_dict: dict = {
                                    "feed_url": feed_url,
                                    "id": entry_url,
                                    "title": item.get("title") or "",
                                    "link": entry_url,
                                }
                                pub = item.get("published")
                                if pub:
                                    entry_dict["published"] = datetime.fromtimestamp(pub, timezone.utc)
                                if item.get("content"):
                                    entry_dict["content"] = [{"value": item["content"]}]
                                try:
                                    reader.add_entry(entry_dict)
                                    entry = reader.get_entry((feed_url, entry_url), None)
                                except Exception:
                                    pass
                        else:
                            row = rconn.execute(
                                "SELECT id, feed FROM entries WHERE link = ? LIMIT 1",
                                (entry_url,),
                            ).fetchone()
                            if row:
                                entry = reader.get_entry((row["feed"], row["id"]), None)

                        if entry:
                            for tag_key in tag_keys:
                                try:
                                    reader.set_tag(entry, tag_key)
                                    state["items_tagged"] = state.get("items_tagged", 0) + 1
                                except Exception:
                                    pass
                            if want_star:
                                conn.execute(
                                    "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                    (entry.feed_url, entry.id),
                                )
                                state["items_starred"] = state.get("items_starred", 0) + 1
                    except Exception:
                        state["errors"] = state.get("errors", 0) + 1

    save_fn()


def _inoreader_drip_step(calls_budget: int = 10) -> None:
    """One drip-import step: advance the API-driven import by up to calls_budget API calls."""
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_INOREADER_IMPORT_STATE) or ""
    if not raw:
        return
    try:
        state = json.loads(raw)
    except Exception:
        return
    if state.get("done") or state.get("phase") == "error":
        return

    token = get_inoreader_token()
    if not token:
        return

    calls_made = 0

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as _c:
            set_setting(_c, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))

    try:
        # Phase: subscriptions
        if state.get("phase") == "subscriptions" and calls_made < calls_budget:
            subs, rl = inoreader_service.get_subscriptions(token)
            calls_made += 1
            state["z1_remaining"] = inoreader_service.z1_remaining(rl)
            with get_reader() as reader:
                existing = {str(f.url) for f in reader.get_feeds()}
                added = 0
                for sub in subs:
                    furl = canonical_feed_url(sub.get("feed_url", ""))
                    if furl and furl not in existing:
                        try:
                            reader.add_feed(furl, exist_ok=True)
                            added += 1
                        except Exception:
                            pass
            state["subs_added"] = state.get("subs_added", 0) + added
            state["phase"] = "labels_list"
            _save()

        # Phase: build label list
        if state.get("phase") == "labels_list" and calls_made < calls_budget:
            tags, rl = inoreader_service.get_tags(token)
            calls_made += 1
            state["z1_remaining"] = inoreader_service.z1_remaining(rl)
            label_ids = [
                t["id"] for t in tags
                if (name := inoreader_service.label_name_from_tag_id(t.get("id", "")))
                and inoreader_service.label_is_tag(name)
            ]
            state["label_ids"] = label_ids
            state["label_cursor"] = 0
            state["label_continuation"] = None
            state["phase"] = "labels_items"
            _save()

        # Phase: page through each label stream and apply tags
        if state.get("phase") == "labels_items":
            label_ids = state.get("label_ids", [])
            cursor = state.get("label_cursor", 0)
            since_ot = state.get("since_ot")  # Unix timestamp cutoff — skip older items
            reader_db = str(tenancy.reader_db_path())
            while cursor < len(label_ids) and calls_made < calls_budget:
                tag_id = label_ids[cursor]
                label_name = inoreader_service.label_name_from_tag_id(tag_id) or tag_id
                stream_id = inoreader_service.label_stream_id(label_name)
                continuation = state.get("label_continuation")
                items, next_cont, rl = inoreader_service.get_stream_contents(
                    token, stream_id, continuation=continuation, n=100, ot=since_ot
                )
                calls_made += 1
                state["z1_remaining"] = inoreader_service.z1_remaining(rl)
                if inoreader_service.label_is_tag(label_name):
                    tag_key = f"{MANUAL_TAG_KEY_PREFIX}{label_name.lower()}"
                    with get_reader() as reader:
                        with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                            rconn.row_factory = sqlite3.Row
                            for item in items:
                                canonical = item.get("canonical") or []
                                entry_url = canonical[0].get("href", "") if canonical else ""
                                origin = item.get("origin") or {}
                                raw_stream = origin.get("streamId", "")
                                feed_url = raw_stream[len("feed/"):] if raw_stream.startswith("feed/") else raw_stream
                                feed_url = canonical_feed_url(feed_url)
                                if not entry_url or not feed_url:
                                    continue
                                try:
                                    entry = _api_resolve_entry(reader, rconn, feed_url, entry_url, item)
                                    if entry:
                                        reader.set_tag(entry, tag_key)
                                        state["items_tagged"] = state.get("items_tagged", 0) + 1
                                except Exception:
                                    pass
                if next_cont:
                    state["label_continuation"] = next_cont
                else:
                    cursor += 1
                    state["label_cursor"] = cursor
                    state["label_continuation"] = None
                _save()
            if cursor >= len(label_ids):
                state["phase"] = "starred"
                state["starred_continuation"] = None
                _save()

        # Phase: page through starred stream and star entries
        if state.get("phase") == "starred" and calls_made < calls_budget:
            since_ot = state.get("since_ot")
            reader_db = str(tenancy.reader_db_path())
            continuation = state.get("starred_continuation")
            items, next_cont, rl = inoreader_service.get_stream_contents(
                token, inoreader_service.STARRED_STREAM_ID, continuation=continuation, n=100, ot=since_ot
            )
            calls_made += 1
            state["z1_remaining"] = inoreader_service.z1_remaining(rl)
            with get_reader() as reader:
                with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                    rconn.row_factory = sqlite3.Row
                    for item in items:
                        canonical = item.get("canonical") or []
                        entry_url = canonical[0].get("href", "") if canonical else ""
                        origin = item.get("origin") or {}
                        raw_stream = origin.get("streamId", "")
                        feed_url = raw_stream[len("feed/"):] if raw_stream.startswith("feed/") else raw_stream
                        feed_url = canonical_feed_url(feed_url)
                        if not entry_url or not feed_url:
                            continue
                        try:
                            entry = _api_resolve_entry(reader, rconn, feed_url, entry_url, item)
                            if entry:
                                with get_meta_connection() as conn:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                        (entry.feed_url, entry.id),
                                    )
                                state["items_starred"] = state.get("items_starred", 0) + 1
                        except Exception:
                            pass
            if next_cont:
                state["starred_continuation"] = next_cont
            else:
                state["phase"] = "done"
                state["done"] = True
                # Advance the since_ot cutoff for the next cycle.
                state["since_ot"] = int(datetime.now(timezone.utc).timestamp())
            _save()

    except inoreader_service.QuotaExceeded:
        LOGGER.info("[inoreader] drip paused: quota exhausted")
        _save()
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        LOGGER.warning("[inoreader] drip paused: network issue (%s)", exc)
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        _save()
        LOGGER.exception("[inoreader] drip step error")


# ---------------------------------------------------------------------------
# Miniflux migration routes
# ---------------------------------------------------------------------------

@app.post("/integrations/miniflux/import/test")
async def miniflux_import_test(request: Request):
    """Test connection to a Miniflux instance. Body: {url, token} (empty fields fall back to stored settings)."""
    body = await request.json()
    with get_meta_connection() as conn:
        base_url = (body.get("url") or "").strip() or (get_setting(conn, SETTING_MINIFLUX_IMPORT_URL) or "")
        token = (body.get("token") or "").strip() or (get_setting(conn, SETTING_MINIFLUX_IMPORT_TOKEN) or "")
    if not base_url or not token:
        return JSONResponse({"ok": False, "error": "url and token are required"}, status_code=400)
    try:
        info = miniflux_import_service.test_connection(base_url, token)
        return JSONResponse({"ok": True, **info})
    except miniflux_import_service.AuthError:
        return JSONResponse({"ok": False, "error": "Authentication failed — check your API token."}, status_code=401)
    except url_guard.UnsafeURLError:
        return JSONResponse({"ok": False, "error": "That server URL is not allowed (use a public http(s) address)."}, status_code=400)
    except Exception:
        # Don't echo the raw exception to the client (CodeQL: info exposure); log it.
        LOGGER.warning("Miniflux connection test failed", exc_info=True)
        return JSONResponse({"ok": False, "error": "Could not connect to the Miniflux server."}, status_code=502)


@app.get("/integrations/miniflux/import/status")
def miniflux_import_status():
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_MINIFLUX_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"state": None})
    try:
        return JSONResponse({"state": json.loads(raw)})
    except Exception:
        return JSONResponse({"state": None})


@app.post("/integrations/miniflux/import/start")
def miniflux_import_start():
    with get_meta_connection() as conn:
        base_url = get_setting(conn, SETTING_MINIFLUX_IMPORT_URL) or ""
        token = get_setting(conn, SETTING_MINIFLUX_IMPORT_TOKEN) or ""
    if not base_url or not token:
        return JSONResponse({"ok": False, "error": "Miniflux URL and token not configured"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "running",
        "subs_added": 0, "items_starred": 0, "items_tagged": 0, "errors": 0,
        "done": False, "error": None, "started_at": now, "updated_at": now,
    }
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _miniflux_import_worker),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@app.post("/integrations/miniflux/import/reset")
def miniflux_import_reset():
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_MINIFLUX_IMPORT_STATE)
    return JSONResponse({"ok": True})


def _miniflux_import_worker() -> None:
    with get_meta_connection() as conn:
        base_url = get_setting(conn, SETTING_MINIFLUX_IMPORT_URL) or ""
        token = get_setting(conn, SETTING_MINIFLUX_IMPORT_TOKEN) or ""

    now = datetime.now(timezone.utc).isoformat()
    state: dict = {
        "phase": "running",
        "subs_added": 0, "items_starred": 0, "items_tagged": 0, "errors": 0,
        "done": False, "error": None, "started_at": now, "updated_at": now,
    }

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as c:
            set_setting(c, SETTING_MINIFLUX_IMPORT_STATE, json.dumps(state))

    _save()

    try:
        feeds = miniflux_import_service.get_feeds(base_url, token)
        # Subscription-only records (no article to tag/star).
        sub_items = [
            {"url": "", "title": "", "published": None,
             "feed_url": f["feed_url"], "feed_title": f["title"],
             "content": "", "starred": False, "tags": [], "folder": f["folder"]}
            for f in feeds
        ]
        starred = miniflux_import_service.get_starred_entries(base_url, token)
        _apply_migration_items(sub_items + starred, state, _save)
        state["phase"] = "done"
        state["done"] = True
        _save()
        LOGGER.info(
            "[miniflux-import] done: %d subs, %d starred, %d tagged, %d errors",
            state.get("subs_added", 0), state.get("items_starred", 0),
            state.get("items_tagged", 0), state.get("errors", 0),
        )
    except miniflux_import_service.AuthError as exc:
        state["error"] = str(exc)
        state["done"] = False
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        state["done"] = False
        _save()
        LOGGER.exception("[miniflux-import] worker error")


# ---------------------------------------------------------------------------
# FreshRSS migration routes
# ---------------------------------------------------------------------------

@app.post("/integrations/freshrss/import/test")
async def freshrss_import_test(request: Request):
    """Test connection to a FreshRSS instance. Body: {url, username, password} (empty fields fall back to stored settings)."""
    body = await request.json()
    with get_meta_connection() as conn:
        url = (body.get("url") or "").strip() or (get_setting(conn, SETTING_FRESHRSS_URL) or "")
        username = (body.get("username") or "").strip() or (get_setting(conn, SETTING_FRESHRSS_USERNAME) or "")
        password = (body.get("password") or "").strip() or (get_setting(conn, SETTING_FRESHRSS_PASSWORD) or "")
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "url, username and password are required"}, status_code=400)
    try:
        info = freshrss_service.test_connection(url, username, password)
        return JSONResponse({"ok": True, **info})
    except freshrss_service.AuthError:
        return JSONResponse({"ok": False, "error": "Authentication failed — check your credentials."}, status_code=401)
    except url_guard.UnsafeURLError:
        return JSONResponse({"ok": False, "error": "That server URL is not allowed (use a public http(s) address)."}, status_code=400)
    except Exception:
        # Don't echo the raw exception to the client (CodeQL: info exposure); log it.
        LOGGER.warning("FreshRSS connection test failed", exc_info=True)
        return JSONResponse({"ok": False, "error": "Could not connect to the FreshRSS server."}, status_code=502)


@app.get("/integrations/freshrss/import/status")
def freshrss_import_status():
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_FRESHRSS_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"state": None})
    try:
        return JSONResponse({"state": json.loads(raw)})
    except Exception:
        return JSONResponse({"state": None})


@app.post("/integrations/freshrss/import/start")
def freshrss_import_start():
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_FRESHRSS_URL) or ""
        username = get_setting(conn, SETTING_FRESHRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_FRESHRSS_PASSWORD) or ""
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "FreshRSS URL, username and password not configured"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "running",
        "subs_added": 0, "items_starred": 0, "items_tagged": 0, "errors": 0,
        "done": False, "error": None, "started_at": now, "updated_at": now,
    }
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _freshrss_import_worker),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@app.post("/integrations/freshrss/import/reset")
def freshrss_import_reset():
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_FRESHRSS_IMPORT_STATE)
    return JSONResponse({"ok": True})


def _freshrss_import_worker() -> None:
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_FRESHRSS_URL) or ""
        username = get_setting(conn, SETTING_FRESHRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_FRESHRSS_PASSWORD) or ""

    now = datetime.now(timezone.utc).isoformat()
    state: dict = {
        "phase": "running",
        "subs_added": 0, "items_starred": 0, "items_tagged": 0, "errors": 0,
        "done": False, "error": None, "started_at": now, "updated_at": now,
    }

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as c:
            set_setting(c, SETTING_FRESHRSS_IMPORT_STATE, json.dumps(state))

    _save()

    try:
        token = freshrss_service.login(url, username, password)

        # Phase 1: subscriptions + folders.
        subs = freshrss_service.get_subscriptions(url, token)
        sub_items = []
        for sub in subs:
            feed_url = sub.get("feed_url", "")
            cats = sub.get("categories") or []
            folder = ""
            for cat in cats:
                label = cat.get("label") or ""
                if label and not freshrss_service.label_is_tag(label):
                    folder = label
                    break
            sub_items.append({
                "url": "", "title": sub.get("title", ""), "published": None,
                "feed_url": feed_url, "feed_title": sub.get("title", ""),
                "content": "", "starred": False, "tags": [], "folder": folder,
            })
        _apply_migration_items(sub_items, state, _save)

        # Phase 2: labels → tags (page through each label stream).
        tags = freshrss_service.get_tags(url, token)
        label_names = [
            freshrss_service.label_name_from_tag_id(t["id"])
            for t in tags
            if freshrss_service.label_name_from_tag_id(t.get("id", ""))
            and freshrss_service.label_is_tag(freshrss_service.label_name_from_tag_id(t["id"]))  # type: ignore
        ]
        for label_name in label_names:
            stream_id = freshrss_service.label_stream_id(label_name)
            continuation = None
            while True:
                items_raw, continuation = freshrss_service.get_stream_contents(
                    url, token, stream_id, continuation=continuation, n=100
                )
                items = [freshrss_service.normalize_item(i) for i in items_raw]
                for item in items:
                    item["tags"] = [label_name]
                _apply_migration_items(items, state, _save)
                if not continuation:
                    break

        # Phase 3: starred entries.
        continuation = None
        while True:
            items_raw, continuation = freshrss_service.get_stream_contents(
                url, token, freshrss_service.STARRED_STREAM_ID, continuation=continuation, n=100
            )
            items = [freshrss_service.normalize_item(i) for i in items_raw]
            for item in items:
                item["starred"] = True
            _apply_migration_items(items, state, _save)
            if not continuation:
                break

        state["phase"] = "done"
        state["done"] = True
        _save()
        LOGGER.info(
            "[freshrss-import] done: %d subs, %d starred, %d tagged, %d errors",
            state.get("subs_added", 0), state.get("items_starred", 0),
            state.get("items_tagged", 0), state.get("errors", 0),
        )
    except freshrss_service.AuthError as exc:
        state["error"] = str(exc)
        state["done"] = False
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        state["done"] = False
        _save()
        LOGGER.exception("[freshrss-import] worker error")


# ---------------------------------------------------------------------------
# tt-rss migration routes
# ---------------------------------------------------------------------------

@app.post("/integrations/ttrss/import/test")
async def ttrss_import_test(request: Request):
    """Test connection to a tt-rss instance. Body: {url, username, password} (empty fields fall back to stored settings)."""
    body = await request.json()
    with get_meta_connection() as conn:
        url = (body.get("url") or "").strip() or (get_setting(conn, SETTING_TTRSS_URL) or "")
        username = (body.get("username") or "").strip() or (get_setting(conn, SETTING_TTRSS_USERNAME) or "")
        password = (body.get("password") or "").strip() or (get_setting(conn, SETTING_TTRSS_PASSWORD) or "")
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "url, username and password are required"}, status_code=400)
    try:
        info = ttrss_service.test_connection(url, username, password)
        return JSONResponse({"ok": True, **info})
    except ttrss_service.AuthError:
        return JSONResponse({"ok": False, "error": "Authentication failed — check your credentials."}, status_code=401)
    except url_guard.UnsafeURLError:
        return JSONResponse({"ok": False, "error": "That server URL is not allowed (use a public http(s) address)."}, status_code=400)
    except Exception:
        # Don't echo the raw exception to the client (CodeQL: info exposure); log it.
        LOGGER.warning("tt-rss connection test failed", exc_info=True)
        return JSONResponse({"ok": False, "error": "Could not connect to the tt-rss server."}, status_code=502)


@app.get("/integrations/ttrss/import/status")
def ttrss_import_status():
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_TTRSS_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"state": None})
    try:
        return JSONResponse({"state": json.loads(raw)})
    except Exception:
        return JSONResponse({"state": None})


@app.post("/integrations/ttrss/import/start")
def ttrss_import_start():
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_TTRSS_URL) or ""
        username = get_setting(conn, SETTING_TTRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_TTRSS_PASSWORD) or ""
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "tt-rss URL, username and password not configured"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "running",
        "subs_added": 0, "items_starred": 0, "items_tagged": 0, "errors": 0,
        "done": False, "error": None, "started_at": now, "updated_at": now,
    }
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _ttrss_import_worker),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@app.post("/integrations/ttrss/import/reset")
def ttrss_import_reset():
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_TTRSS_IMPORT_STATE)
    return JSONResponse({"ok": True})


def _ttrss_import_worker() -> None:
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_TTRSS_URL) or ""
        username = get_setting(conn, SETTING_TTRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_TTRSS_PASSWORD) or ""

    now = datetime.now(timezone.utc).isoformat()
    state: dict = {
        "phase": "running",
        "subs_added": 0, "items_starred": 0, "items_tagged": 0, "errors": 0,
        "done": False, "error": None, "started_at": now, "updated_at": now,
    }

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as c:
            set_setting(c, SETTING_TTRSS_IMPORT_STATE, json.dumps(state))

    _save()

    try:
        sid = ttrss_service.login(url, username, password)

        # Build category (folder) map.
        cats = ttrss_service.get_categories(url, sid)
        cat_name_map: dict[int, str] = {c["id"]: c["title"] for c in cats if c.get("title")}

        # Subscribe all feeds and assign folders.
        feeds = ttrss_service.get_all_feeds(url, sid)
        feed_info_map: dict[int, dict] = {}
        sub_items = []
        for idx, f in enumerate(feeds):
            feed_info_map[idx] = f  # placeholder; actual feed_id from headlines
            folder = cat_name_map.get(f.get("cat_id", 0), "")
            sub_items.append({
                "url": "", "title": f.get("title", ""), "published": None,
                "feed_url": f.get("feed_url", ""), "feed_title": f.get("title", ""),
                "content": "", "starred": False, "tags": [], "folder": folder,
            })
        # Build a real feed_url → cat_id map for headline normalisation.
        url_to_cat: dict[str, int] = {f["feed_url"]: f.get("cat_id", 0) for f in feeds}
        # tt-rss headlines carry feed_id (int), not feed_url; build id→info map via a
        # second pass once we fetch headlines (which include feed_url in their data).
        _apply_migration_items(sub_items, state, _save)

        # Page through all starred headlines.
        limit = 200
        skip = 0
        while True:
            headlines = ttrss_service.get_starred_headlines(url, sid, limit=limit, skip=skip)
            if not headlines:
                break
            # Build feed_info_map from actual headline data (each headline has feed_url
            # and feed_title fields that appear in some tt-rss versions).
            feed_info_from_hl: dict[int, dict] = {}
            for hl in headlines:
                fid = hl.get("feed_id")
                if isinstance(fid, int) and fid not in feed_info_from_hl:
                    # Try to find the feed_url from the sub list or the headline itself.
                    hl_feed_url = hl.get("feed_url", "")
                    hl_feed_title = hl.get("feed_title", "")
                    cat_id = url_to_cat.get(hl_feed_url, 0)
                    feed_info_from_hl[fid] = {
                        "feed_url": hl_feed_url,
                        "title": hl_feed_title,
                        "cat_id": cat_id,
                    }
            items = [
                ttrss_service.normalize_headline(hl, feed_info_from_hl, cat_name_map)
                for hl in headlines
            ]
            _apply_migration_items(items, state, _save)
            if len(headlines) < limit:
                break
            skip += limit

        state["phase"] = "done"
        state["done"] = True
        _save()
        LOGGER.info(
            "[ttrss-import] done: %d subs, %d starred, %d tagged, %d errors",
            state.get("subs_added", 0), state.get("items_starred", 0),
            state.get("items_tagged", 0), state.get("errors", 0),
        )
    except ttrss_service.AuthError as exc:
        state["error"] = str(exc)
        state["done"] = False
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        state["done"] = False
        _save()
        LOGGER.exception("[ttrss-import] worker error")


@app.get("/api/pinterest/boards")
def pinterest_boards_route():
    """List the connected user's boards for the Pin board-picker."""
    token = get_pinterest_oauth_token()
    if not token:
        return JSONResponse({"connected": False, "boards": []})
    try:
        boards = pinterest_oauth_service.list_boards(token)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"connected": True, "error": str(exc), "boards": []}, status_code=502)
    return JSONResponse({"connected": True, "boards": boards})


@app.post("/api/pinterest/pin")
async def pinterest_pin_route(request: Request):
    """Pin an entry to a board. Body: {feed_url, entry_id, board_id}.

    Uses the entry's lead image; entries without one can't be pinned (Pinterest
    requires an image). Links the pin back to the entry's source URL."""
    body = await request.json()
    feed_url = (body.get("feed_url") or "").strip()
    entry_id = (body.get("entry_id") or "").strip()
    board_id = (body.get("board_id") or "").strip()
    if not feed_url or not entry_id or not board_id:
        return JSONResponse({"error": "feed_url, entry_id and board_id are required"}, status_code=400)
    token = get_pinterest_oauth_token()
    if not token:
        return JSONResponse({"connected": False, "error": "Pinterest not connected"}, status_code=401)
    with get_reader() as reader:
        try:
            entry = reader.get_entry((feed_url, entry_id), None)
        except Exception:
            entry = None
    if not entry:
        return JSONResponse({"error": "entry not found"}, status_code=404)
    image_url = _derive_article_lead_image(entry)
    if not image_url:
        return JSONResponse({"error": "no image to pin for this entry"}, status_code=422)
    link = str(getattr(entry, "link", "") or "")
    title = str(getattr(entry, "title", "") or "")
    try:
        pin = pinterest_oauth_service.create_pin(token, board_id, image_url, link, title=title)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "pin_id": pin.get("id", "")})


@app.get("/api/youtube/playlists")
def youtube_playlists_route():
    """List the connected user's playlists for the Add-to-playlist dropdown."""
    token = get_youtube_oauth_token()
    if not token:
        return JSONResponse({"connected": False, "playlists": []})
    try:
        playlists = youtube_oauth_service.list_playlists(token)
    except youtube_oauth_service.QuotaExceeded:
        mark_yt_quota_exhausted()
        return JSONResponse({"connected": True, "error": "quota", "playlists": []}, status_code=429)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"connected": True, "error": str(exc), "playlists": []}, status_code=502)
    return JSONResponse({"connected": True, "playlists": playlists})


@app.post("/api/youtube/playlists/add")
async def youtube_playlist_add_route(request: Request):
    """Add a video to a playlist (or a new one). Body: {video_id, playlist_id?, new_title?}."""
    body = await request.json()
    video_id = (body.get("video_id") or "").strip()
    playlist_id = (body.get("playlist_id") or "").strip()
    new_title = (body.get("new_title") or "").strip()
    if not video_id or (not playlist_id and not new_title):
        return JSONResponse({"ok": False, "error": "video_id and playlist_id or new_title required"}, status_code=400)
    token = get_youtube_oauth_token()
    if not token:
        return JSONResponse({"ok": False, "error": "not_connected"}, status_code=401)
    try:
        if not playlist_id:
            created = youtube_oauth_service.create_playlist(token, new_title)
            playlist_id = created["id"]
        youtube_oauth_service.add_video_to_playlist(token, playlist_id, video_id)
    except youtube_oauth_service.QuotaExceeded:
        mark_yt_quota_exhausted()
        return JSONResponse({"ok": False, "error": "quota"}, status_code=429)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "playlist_id": playlist_id})


@app.post("/deviantart/disconnect")
def deviantart_disconnect():
    with get_meta_connection() as conn:
        for key in (SETTING_DEVIANTART_ACCESS_TOKEN, SETTING_DEVIANTART_REFRESH_TOKEN,
                    SETTING_DEVIANTART_TOKEN_EXPIRES_AT, SETTING_DEVIANTART_USERNAME):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


@app.post("/quire/disconnect")
def quire_disconnect():
    with get_meta_connection() as conn:
        for key in (SETTING_QUIRE_ACCESS_TOKEN, SETTING_QUIRE_REFRESH_TOKEN,
                    SETTING_QUIRE_TOKEN_EXPIRES_AT, SETTING_QUIRE_USERNAME,
                    SETTING_QUIRE_PROJECT_OID, SETTING_QUIRE_PROJECT_NAME):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


@app.post("/deviantart/sync-watchlist")
def deviantart_sync_watchlist_route():
    """Start the watch-list → feeds sync in the background (it can take minutes)."""
    uid = tenancy.current_user_id()

    def _job():
        try:
            with get_meta_connection() as conn:
                set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, "Starting…")
            # sync_deviantart_watchlist sets its own final status (done / rate-limited).
            result = sync_deviantart_watchlist()
            if result.get("error"):
                with get_meta_connection() as conn:
                    set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, f"Sync error: {result['error']}")
        except Exception:
            LOGGER.exception("[deviantart] background watchlist sync failed")
            with get_meta_connection() as conn:
                set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, "Sync failed — see logs.")

    threading.Thread(target=_run_in_user_context, args=(uid, _job), daemon=True).start()
    return JSONResponse({"started": True})


@app.post("/deviantart/push-watchlist")
def deviantart_push_watchlist_route():
    result = push_galleries_to_deviantart_watchlist()
    return JSONResponse(result)


@app.post("/deviantart/add-watch-feed")
def deviantart_add_watch_feed_route():
    """Add the single combined 'deviations from everyone you Watch' feed."""
    token = get_deviantart_user_token()
    if not token:
        return JSONResponse({"ok": False, "error": "DeviantArt account not connected."}, status_code=400)
    with get_meta_connection() as conn:
        existing = conn.execute("SELECT id FROM deviantart_feeds WHERE source = 'watch' LIMIT 1").fetchone()
        if existing:
            file_url = deviantart_service.feed_file_url(str(existing["id"]))
            folder_id = _get_or_create_folder_by_name(conn, _deviantart_folder_name())
            conn.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, file_url))
            invalidate_meta_structure_cache()
            return JSONResponse({"ok": True, "message": "Watch feed already exists."})
    try:
        with get_meta_connection() as conn:
            folder_id = _get_or_create_folder_by_name(conn, _deviantart_folder_name())
            with get_reader() as reader:
                _fid, file_url = deviantart_service.create_watch_feed(conn, reader, token)
            conn.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, file_url))
            _apply_deviantart_image_strategy(conn, file_url)
        invalidate_meta_structure_cache()
        invalidate_unread_counts_cache()
        return JSONResponse({"ok": True, "message": "Added your DeviantArt Watch feed."})
    except deviantart_service.DeviantArtRateLimited:
        return JSONResponse({"ok": False, "error": "DeviantArt rate limit — try again in a bit."}, status_code=429)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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
    da_cid, da_secret = get_deviantart_credentials()
    quire_cid, quire_secret = get_quire_credentials()
    # Lazily detect the Quire plan once (sets the meter caps) if a project is chosen
    # but no plan has been recorded yet — e.g. projects picked before plan detection
    # existed, so users see correct caps without re-picking. Best-effort, ~one call.
    if is_quire_configured() and not get_runtime_setting(SETTING_QUIRE_PLAN):
        detect_quire_plan_and_caps()

    return JSONResponse({
        "profile_name": profile_name,
        "profile_email": profile_email,
        "tz_display": get_runtime_setting(SETTING_TZ_DISPLAY),
        "tz_default": os.environ.get("TZ") or "UTC",
        "maintenance_hour": get_runtime_setting(SETTING_MAINTENANCE_HOUR),
        "maintenance_last_ran_at": maint_last,
        "yt_api_key_set": bool(yt_api_key),
        "yt_api_key_masked": _masked(yt_api_key),
        "yt_channel_id": get_yt_channel_id(),
        "yt_folder_name": get_yt_folder_name(),
        "yt_embed_account_features": youtube_embed_account_features_enabled(),
        "yt_hide_shorts_global": youtube_hide_shorts_global(),
        "yt_quota": get_yt_quota_status(),
        "yt_quota_cap": youtube_quota_cap(),
        "star_send_instapaper": get_runtime_setting(SETTING_STAR_SEND_INSTAPAPER, "0") == "1",
        "star_send_yt_playlist": get_runtime_setting(SETTING_STAR_SEND_YT_PLAYLIST) or "",
        "star_send_yt_playlist_title": get_runtime_setting(SETTING_STAR_SEND_YT_PLAYLIST_TITLE) or "",
        "star_send_email": get_runtime_setting(SETTING_STAR_SEND_EMAIL) or "",
        "yt_oauth_client_id": get_runtime_setting(SETTING_YT_OAUTH_CLIENT_ID, ""),
        "yt_oauth_client_secret_set": bool(get_runtime_setting(SETTING_YT_OAUTH_CLIENT_SECRET)),
        "yt_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_YT_OAUTH_CLIENT_SECRET, "")),
        "yt_oauth_configured": all(get_youtube_oauth_credentials()),
        "yt_oauth_connected": bool(get_runtime_setting(SETTING_YT_OAUTH_REFRESH_TOKEN)),
        "shared_yt_oauth_client_id": get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_ID, ""),
        "shared_yt_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET)),
        "shared_yt_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET, "")),
        "pinterest_oauth_client_id": get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_ID, ""),
        "pinterest_oauth_client_secret_set": bool(get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_SECRET)),
        "pinterest_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_SECRET, "")),
        "pinterest_oauth_configured": all(get_pinterest_oauth_credentials()),
        "pinterest_oauth_connected": bool(get_runtime_setting(SETTING_PINTEREST_OAUTH_REFRESH_TOKEN)),
        "shared_pinterest_oauth_client_id": get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID, ""),
        # Miniflux / FreshRSS / tt-rss migrations
        "miniflux_import_url": get_runtime_setting(SETTING_MINIFLUX_IMPORT_URL, ""),
        "miniflux_import_token_set": bool(get_runtime_setting(SETTING_MINIFLUX_IMPORT_TOKEN)),
        "miniflux_import_token_masked": _masked(get_runtime_setting(SETTING_MINIFLUX_IMPORT_TOKEN, "")),
        "freshrss_url": get_runtime_setting(SETTING_FRESHRSS_URL, ""),
        "freshrss_username": get_runtime_setting(SETTING_FRESHRSS_USERNAME, ""),
        "freshrss_password_set": bool(get_runtime_setting(SETTING_FRESHRSS_PASSWORD)),
        "freshrss_password_masked": _masked(get_runtime_setting(SETTING_FRESHRSS_PASSWORD, "")),
        "ttrss_url": get_runtime_setting(SETTING_TTRSS_URL, ""),
        "ttrss_username": get_runtime_setting(SETTING_TTRSS_USERNAME, ""),
        "ttrss_password_set": bool(get_runtime_setting(SETTING_TTRSS_PASSWORD)),
        "ttrss_password_masked": _masked(get_runtime_setting(SETTING_TTRSS_PASSWORD, "")),
        # Inoreader migration
        "inoreader_client_id": get_runtime_setting(SETTING_INOREADER_CLIENT_ID, _ENV_INOREADER_CLIENT_ID),
        "inoreader_client_secret_set": bool(get_runtime_setting(SETTING_INOREADER_CLIENT_SECRET, _ENV_INOREADER_CLIENT_SECRET)),
        "inoreader_client_secret_masked": _masked(get_runtime_setting(SETTING_INOREADER_CLIENT_SECRET, _ENV_INOREADER_CLIENT_SECRET)),
        "inoreader_configured": bool(all(get_inoreader_credentials())),
        "inoreader_connected": inoreader_connected(),
        "inoreader_export_dir": get_runtime_setting(SETTING_INOREADER_EXPORT_DIR, ""),
        "shared_pinterest_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET)),
        "shared_pinterest_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET, "")),
        # Reddit OAuth
        "reddit_client_id": get_runtime_setting(SETTING_REDDIT_CLIENT_ID, ""),
        "reddit_client_secret_set": bool(get_runtime_setting(SETTING_REDDIT_CLIENT_SECRET)),
        "reddit_client_secret_masked": _masked(get_runtime_setting(SETTING_REDDIT_CLIENT_SECRET, "")),
        "reddit_configured": all(get_reddit_credentials()),
        "reddit_connected": reddit_connected(),
        "reddit_username": get_runtime_setting(SETTING_REDDIT_USERNAME, ""),
        "shared_reddit_client_id": get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_ID, ""),
        "shared_reddit_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET)),
        "shared_reddit_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET, "")),
        "star_send_reddit_subreddit": get_runtime_setting(SETTING_STAR_SEND_REDDIT_SUBREDDIT, ""),
        "resend_api_key_set": bool(resend_key),
        "resend_api_key_masked": _masked(resend_key),
        "email_from": get_resend_from(),
        "instapaper_username": get_runtime_setting(SETTING_INSTAPAPER_USERNAME),
        "instapaper_password_set": bool(instapaper_pw),
        "instapaper_password_masked": _masked(instapaper_pw),
        "deviantart_client_id": da_cid,
        "deviantart_client_secret_set": bool(da_secret),
        "deviantart_client_secret_masked": _masked(da_secret),
        "deviantart_connected": bool(get_runtime_setting(SETTING_DEVIANTART_ACCESS_TOKEN)),
        "deviantart_username": get_runtime_setting(SETTING_DEVIANTART_USERNAME),
        "deviantart_sync_status": get_runtime_setting(SETTING_DEVIANTART_SYNC_STATUS),
        "deviantart_folder_name": _deviantart_folder_name(),
        "quire_client_id": quire_cid,
        "quire_client_secret_set": bool(quire_secret),
        "quire_client_secret_masked": _masked(quire_secret),
        "quire_connected": is_quire_connected(),
        "quire_username": get_runtime_setting(SETTING_QUIRE_USERNAME),
        "quire_project_oid": quire_project_oid(),
        "quire_project_name": get_runtime_setting(SETTING_QUIRE_PROJECT_NAME),
        "quire_usage": get_quire_usage_status(),
        "quire_plan": get_runtime_setting(SETTING_QUIRE_PLAN),
        "star_send_quire": get_runtime_setting(SETTING_STAR_SEND_QUIRE, "0") == "1",
        "contacts": contacts,
        "email_to_default": email_to_default,
        "public_url": LECTIO_PUBLIC_URL,
        "fetch_history_keep": get_fetch_history_keep(),
        "fetch_history_max_age_days": get_fetch_history_max_age_days(),
        "login_max_failures": get_login_max_failures(),
        "login_window_seconds": get_login_window_seconds(),
        "instance_auto_refresh": get_instance_default_auto_refresh(),
    })


def _keep_existing_sensitive(key: str, str_val: str, sensitive: set[str]) -> bool:
    """True if a sensitive-field save should be ignored (leave the stored value).

    Masked secret fields reload blank in the UI, so saving the form with the
    field untouched sends "" (or the masked "••" placeholder). Treat both as
    "leave unchanged" so a routine re-save never silently wipes a stored secret.
    """
    return key in sensitive and (not str_val or str_val.startswith("••"))


@app.post("/settings/all")
async def save_all_settings(request: Request):
    """Save any subset of user-configurable settings. Empty string clears a value."""
    import json as _json
    body = await request.json()

    _SENSITIVE = {SETTING_RESEND_API_KEY, SETTING_YT_API_KEY, SETTING_INSTAPAPER_PASSWORD,
                  SETTING_DEVIANTART_CLIENT_SECRET, SETTING_QUIRE_CLIENT_SECRET,
                  SETTING_YT_OAUTH_CLIENT_SECRET, SETTING_PINTEREST_OAUTH_CLIENT_SECRET,
                  SETTING_SHARED_YT_OAUTH_CLIENT_SECRET, SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
                  SETTING_INOREADER_CLIENT_SECRET,
                  SETTING_REDDIT_CLIENT_SECRET, SETTING_SHARED_REDDIT_CLIENT_SECRET,
                  SETTING_MINIFLUX_IMPORT_TOKEN,
                  SETTING_FRESHRSS_PASSWORD, SETTING_TTRSS_PASSWORD}
    _ALLOWED = {
        PROFILE_NAME_SETTING_KEY, PROFILE_EMAIL_SETTING_KEY,
        SETTING_TZ_DISPLAY, SETTING_MAINTENANCE_HOUR,
        SETTING_IMG_CACHE_DAYS, SETTING_IMG_CACHE_MAX_DIM,
        SETTING_YT_API_KEY, SETTING_YT_CHANNEL_ID, SETTING_YT_FOLDER_NAME,
        SETTING_YT_EMBED_ACCOUNT_FEATURES, SETTING_YT_HIDE_SHORTS_GLOBAL, SETTING_YT_QUOTA_CAP,
        SETTING_YT_OAUTH_CLIENT_ID, SETTING_YT_OAUTH_CLIENT_SECRET,
        SETTING_STAR_SEND_INSTAPAPER, SETTING_STAR_SEND_YT_PLAYLIST,
        SETTING_STAR_SEND_YT_PLAYLIST_TITLE, SETTING_STAR_SEND_EMAIL,
        SETTING_RESEND_API_KEY, SETTING_EMAIL_FROM,
        SETTING_INSTAPAPER_USERNAME, SETTING_INSTAPAPER_PASSWORD,
        SETTING_DEVIANTART_CLIENT_ID, SETTING_DEVIANTART_CLIENT_SECRET,
        SETTING_DEVIANTART_FOLDER_NAME,
        SETTING_QUIRE_CLIENT_ID, SETTING_QUIRE_CLIENT_SECRET,
        SETTING_QUIRE_PROJECT_OID, SETTING_QUIRE_PROJECT_NAME, SETTING_STAR_SEND_QUIRE,
        SETTING_QUIRE_RATE_CAP_MIN, SETTING_QUIRE_RATE_CAP_HOUR,
        SETTING_PINTEREST_OAUTH_CLIENT_ID, SETTING_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_YT_OAUTH_CLIENT_ID, SETTING_SHARED_YT_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID, SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_INOREADER_CLIENT_ID, SETTING_INOREADER_CLIENT_SECRET,
        SETTING_INOREADER_EXPORT_DIR,
        SETTING_MINIFLUX_IMPORT_URL, SETTING_MINIFLUX_IMPORT_TOKEN,
        SETTING_FRESHRSS_URL, SETTING_FRESHRSS_USERNAME, SETTING_FRESHRSS_PASSWORD,
        SETTING_TTRSS_URL, SETTING_TTRSS_USERNAME, SETTING_TTRSS_PASSWORD,
        SETTING_REDDIT_CLIENT_ID, SETTING_REDDIT_CLIENT_SECRET,
        SETTING_SHARED_REDDIT_CLIENT_ID, SETTING_SHARED_REDDIT_CLIENT_SECRET,
        SETTING_STAR_SEND_REDDIT_SUBREDDIT,
        SETTING_FETCH_HISTORY_KEEP, SETTING_FETCH_HISTORY_MAX_AGE_DAYS,
        SETTING_LOGIN_MAX_FAILURES, SETTING_LOGIN_WINDOW_SECONDS,
        SETTING_DEFAULT_AUTO_REFRESH_MINUTES,
        "email_contacts", EMAIL_TO_SETTING_KEY,
    }
    # Instance-level config — only admins may change it (in multi mode). Non-admin
    # requests silently drop these keys, even if the client sends them.
    _ADMIN_ONLY = {
        SETTING_RESEND_API_KEY, SETTING_EMAIL_FROM,
        SETTING_MAINTENANCE_HOUR,
        SETTING_IMG_CACHE_DAYS, SETTING_IMG_CACHE_MAX_DIM,
        SETTING_SHARED_YT_OAUTH_CLIENT_ID, SETTING_SHARED_YT_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID, SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_REDDIT_CLIENT_ID, SETTING_SHARED_REDDIT_CLIENT_SECRET,
        SETTING_FETCH_HISTORY_KEEP, SETTING_FETCH_HISTORY_MAX_AGE_DAYS,
        SETTING_LOGIN_MAX_FAILURES, SETTING_LOGIN_WINDOW_SECONDS,
        SETTING_DEFAULT_AUTO_REFRESH_MINUTES,
        SETTING_INOREADER_EXPORT_DIR,
    }
    is_admin = _is_web_admin(_current_web_user(request))

    # Detect a YouTube fill-in so we can kick off an immediate sync (rather than
    # waiting for daily maintenance) when it goes from unconfigured -> configured.
    yt_configured_before = bool(get_yt_api_key() and get_yt_channel_id())
    quire_project_before = quire_project_oid()

    import json as _json
    with get_meta_connection() as conn:
        for key, value in body.items():
            if key not in _ALLOWED:
                continue
            if key in _ADMIN_ONLY and not is_admin:
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
            if _keep_existing_sensitive(key, str_val, _SENSITIVE):
                continue
            if str_val:
                set_setting(conn, key, str_val)
            else:
                delete_setting(conn, key)

    # Newly-configured YouTube → sync now, in the configuring user's context.
    if not yt_configured_before and get_yt_api_key() and get_yt_channel_id():
        threading.Thread(
            target=_run_in_user_context,
            args=(tenancy.current_user_id(), _run_youtube_sync),
            daemon=True, name="youtube-sync-on-config",
        ).start()

    # Quire destination project changed → detect its org's plan and align the
    # rate-meter caps (Free/Pro/Premium) to it, in the configuring user's context.
    if quire_project_oid() and quire_project_oid() != quire_project_before:
        threading.Thread(
            target=_run_in_user_context,
            args=(tenancy.current_user_id(), detect_quire_plan_and_caps),
            daemon=True, name="quire-plan-detect",
        ).start()

    return JSONResponse({"ok": True})


@app.post("/settings/maintenance/run-now")
def run_maintenance_now(request: Request):
    """Trigger daily maintenance immediately. Admin-only."""
    if not _is_web_admin(_current_web_user(request)):
        return JSONResponse({"ok": False, "error": "Admins only."}, status_code=403)
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

    # Sync the active strategy's alt/title into entry_lead_images so caption_source
    # rendering can read them immediately without waiting for the next feed refresh.
    _active_strat, _, _ = lead_image_service.get_feed_strategy(feed_url)
    _active_row = next(
        (r for r in strategy_rows if r["strategy"] == _active_strat and r.get("image_url")),
        None,
    )
    if _active_row and sample_entry:
        lead_image_service.store_entry_image_alt(
            feed_url,
            str(sample_entry.id),
            _active_row.get("image_alt"),
            title_text=_active_row.get("image_title"),
        )
        lead_image_service.store_entry_lead_image(
            feed_url,
            str(sample_entry.id),
            _active_row["image_url"],
        )

    return JSONResponse({"ok": True, "strategy_cache": results})


@app.post("/feeds/browser-ua")
def set_feed_browser_ua_route(feed_url: str = Form(...), enabled: int = Form(...)):
    """Manually flag/unflag a feed for browser-identity fetches. Auto-set on
    refusal; this lets the user reset a feed back to the honest identity (or force
    it on)."""
    feed_url = feed_url.strip()
    with get_meta_connection() as conn:
        if enabled:
            flag_browser_ua_feed(conn, feed_url, reason="manual")
        else:
            unflag_browser_ua_feed(conn, feed_url)
    _invalidate_browser_ua_cache()
    return JSONResponse({"ok": True, "browser_ua": bool(enabled)})


@app.post("/feeds/reparse")
def reparse_feed_route(feed_url: str = Form(...)):
    """Force a full re-fetch + re-parse of one feed to backfill embeds on old
    entries.

    Entries stored before ingest stopped sanitizing feed HTML (see
    services.reader_sanitize) have their iframe/SVG embeds stripped; they only
    return when reader re-stores the entry on a content change. reader skips
    unchanged feeds via conditional GET, so we mark the feed stale first
    (reader's own mechanism to ignore the cached ETag/Last-Modified) and then
    update it: the now-unsanitized re-parse yields a different content hash for
    those old entries, so reader re-stores them with embeds intact. Read/star
    state is preserved (reader keys on entry id, not content)."""
    try:
        with get_reader() as reader:
            # set_feed_stale is reader's supported "ignore HTTP caching on next
            # update" flag (used by its own --new=False path); private attr but
            # stable across reader 3.x.
            reader._storage.set_feed_stale(feed_url, True)
            try:
                updated = reader.update_feed(feed_url)
            except Exception as exc:
                # If the host refused our honest UA, flag it for browser identity
                # and retry once — otherwise a WAF-blocked feed can never backfill.
                if FeedRefreshService._is_fetch_refusal(exc) and _flag_browser_ua_on_refusal(feed_url):
                    updated = reader.update_feed(feed_url)
                else:
                    raise
    except Exception as exc:  # FeedNotFoundError, network/parse errors
        LOGGER.warning("[reparse] failed for %s: %s", feed_url, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    modified = int(getattr(updated, "modified", 0)) if updated else 0
    new = int(getattr(updated, "new", 0)) if updated else 0
    return JSONResponse({"ok": True, "modified": modified, "new": new})


@app.post("/feeds/move")
def move_feed(
    request: Request,
    feed_url: str = Form(...),
    from_folder_id: int = Form(...),
    to_folder_id: int = Form(...),
    current_folder_id: int | None = Form(default=None),
    current_list_feed_url: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_read_filter = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    read_filter_query = build_read_filter_query(read_filter)

    # Only "follow" the feed to its new folder if it's the feed the user is
    # currently viewing. Right-clicking a feed you aren't looking at (to file it)
    # should leave your current view put.
    following = bool(current_list_feed_url) and current_list_feed_url == feed_url
    if following:
        dest_folder_id: int = to_folder_id
        dest_feed = feed_url
    else:
        dest_folder_id = current_folder_id if current_folder_id is not None else to_folder_id
        dest_feed = current_list_feed_url or ""

    def _dest(message: str) -> str:
        feed_q = f"&list_feed_url={quote_plus(dest_feed)}" if dest_feed else ""
        return (
            f"/?folder_id={dest_folder_id}{feed_q}"
            f"{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}"
            f"&message={quote_plus(message)}"
        )

    def _respond(message: str, ok: bool = True):
        # AJAX caller (sidebar move submenu) wants JSON so it can relocate the
        # feed node in place instead of a full-page reload.
        requested_with = (request.headers.get("x-requested-with") or "").lower()
        if "lectio" in requested_with or requested_with == "xmlhttprequest":
            return JSONResponse(
                {"ok": ok, "message": message, "following": following,
                 "feed_url": feed_url, "from_folder_id": from_folder_id, "to_folder_id": to_folder_id},
                status_code=200 if ok else 500,
            )
        return RedirectResponse(url=_dest(message), status_code=303)

    if from_folder_id == to_folder_id:
        return _respond("Feed is already in that folder.")

    message = "Feed moved."
    ok = True
    try:
        move_feed_to_folder(feed_url, from_folder_id, to_folder_id)
    except ValueError:
        message = "Couldn't move the feed to that folder."
        ok = False
    except Exception:
        LOGGER.exception("[feeds/move] failed feed=%s -> folder=%s", feed_url, to_folder_id)
        message = "Feed move failed."
        ok = False

    return _respond(message, ok)


@app.post("/feeds/disable")
def disable_feed_route(request: Request, folder_id: int = Form(...), feed_url: str = Form(...)):
    disable_feed(feed_url)
    # AJAX caller (e.g. the Feeds settings tree) wants JSON so it can update the
    # DOM in place instead of navigating away and closing the settings modal.
    requested_with = request.headers.get("x-requested-with", "").lower()
    if "lectio" in requested_with or requested_with == "xmlhttprequest":
        return JSONResponse({"ok": True, "feed_url": feed_url}, status_code=200)
    return RedirectResponse(url=f"/?folder_id={folder_id}", status_code=303)


@app.post("/feeds/enable")
def enable_feed_route(request: Request, folder_id: int | None = Form(default=None), feed_url: str = Form(...)):
    enable_feed(feed_url)
    requested_with = request.headers.get("x-requested-with", "").lower()
    if "lectio" in requested_with or requested_with == "xmlhttprequest":
        return JSONResponse({"ok": True, "feed_url": feed_url}, status_code=200)
    dest = f"/?folder_id={folder_id}" if folder_id else "/"
    return RedirectResponse(url=dest, status_code=303)


@app.post("/feeds/toggle-updates")
def toggle_feed_updates(feed_url: str = Form(...), enabled: str = Form(...)):
    """Pause/resume = enable/disable a feed (same unified state). Delegates to
    enable_feed/disable_feed, which set both Lectio's disabled_feeds row and
    reader's updates_enabled flag so every surface agrees."""
    want_enabled = enabled.lower() in ("1", "true", "yes")
    try:
        if want_enabled:
            enable_feed(feed_url)
        else:
            disable_feed(feed_url)
        return JSONResponse({"ok": True, "updates_enabled": want_enabled})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/feeds/change-url")
def change_feed_url_route(old_url: str = Form(...), new_url: str = Form(...)):
    """Change the URL of a feed, migrating all associated data."""
    new_url = new_url.strip()
    parsed = urlparse(new_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return JSONResponse({"ok": False, "error": "Invalid URL — must be http or https."}, status_code=400)
    if new_url == old_url:
        return JSONResponse({"ok": False, "error": "New URL is the same as the current URL."}, status_code=400)

    try:
        with get_reader() as reader:
            reader.change_feed_url(old_url, new_url)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # Migrate all meta DB tables that reference the old feed_url.
    _feed_url_tables = [
        "archived_entry",
        "archived_asset_link",
        "folder_feeds",
        "saved_entries",
        "entry_read_state",
        "read_history",
        "feed_failure_state",
        "entry_lead_images",
        "feed_lead_image_strategy",
        "disabled_feeds",
        "feed_display_prefs",
        "feed_strategy_cache",
        "rule_run_log_entries",
        "email_batch_queue",
    ]
    with get_meta_connection() as conn:
        for table in _feed_url_tables:
            try:
                conn.execute(f"UPDATE {table} SET feed_url = ? WHERE feed_url = ?", (new_url, old_url))
            except Exception:
                pass
        # highlight_keywords uses scope/scope_id rather than feed_url
        try:
            conn.execute(
                "UPDATE highlight_keywords SET scope_id = ? WHERE scope = 'feed' AND scope_id = ?",
                (new_url, old_url),
            )
        except Exception:
            pass

    # Migrate starred archive DB tables.
    try:
        with get_starred_archive_connection() as arch_conn:
            for table in ("archived_entry", "archived_asset_link"):
                try:
                    arch_conn.execute(f"UPDATE {table} SET feed_url = ? WHERE feed_url = ?", (new_url, old_url))
                except Exception:
                    pass
    except Exception:
        pass

    # Migrate lead-image in-memory caches: re-key (old_url, entry_id) → (new_url, entry_id).
    lead_image_service.rename_feed_url_in_cache(old_url, new_url)

    # Clear any backoff/failure state the old URL accumulated so the new URL
    # gets fetched immediately rather than waiting for the next scheduled retry.
    with get_meta_connection() as conn:
        conn.execute("DELETE FROM feed_failure_state WHERE feed_url = ?", (new_url,))

    invalidate_meta_structure_cache()
    invalidate_problematic_feeds_cache()
    global _unread_counts_generation
    _unread_counts_generation += 1

    threading.Thread(
        target=feed_refresh_service.update_feeds,
        args=([new_url],),
        daemon=True,
        name="refresh-after-url-change",
    ).start()

    return JSONResponse({"ok": True, "new_url": new_url})


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
    normalized_read_filter = normalize_read_filter(read_filter)
    sort_query_s = build_sort_query(sort_by, sort_dir)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    read_filter_query_s = build_read_filter_query(read_filter)

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
            with get_reader() as reader:
                with get_meta_connection() as conn:
                    purge_orphaned_feed(reader, conn, feed_url, archive_pending=True)
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
            f"{sort_query_s}"
            f"{read_filter_query_s}"
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


def _rescue_unread_entries(reader, remove_url: str, keep_url: str) -> int:
    """Mark read entries in keep_url as unread when the removed feed had them unread.

    Collects URL slugs (last path segment) of unread entries in remove_url, then
    finds read entries in keep_url with matching slugs and marks them unread.
    Slug matching is domain-agnostic, so old.reddit.com vs www.reddit.com works.
    Returns the number of entries rescued.
    """
    unread_slugs: set[str] = set()
    try:
        for entry in reader.get_entries(feed=remove_url, read=False):
            slug = entry_url_slug(entry.link) if entry.link else None
            if slug:
                unread_slugs.add(slug)
    except Exception:
        LOGGER.exception("[dedup] error collecting unread slugs from %s", remove_url)
        return 0

    if not unread_slugs:
        return 0

    rescued = 0
    try:
        for entry in reader.get_entries(feed=keep_url, read=True):
            slug = entry_url_slug(entry.link) if entry.link else None
            if slug and slug in unread_slugs:
                reader.mark_entry_as_unread(entry)
                rescued += 1
    except Exception:
        LOGGER.exception("[dedup] error rescuing unread entries in %s", keep_url)

    if rescued:
        LOGGER.info("[dedup] rescued %d unread entries in %s (removed %s)", rescued, keep_url, remove_url)
    return rescued


@app.post("/feeds/deduplicate")
async def deduplicate_feeds(request: Request):
    """Remove slash-duplicate feeds and optionally upgrade format-selector URLs.

    Body (JSON):
      cross_folder_choices: list of {keep, remove, folder_ids} — user-selected folder assignments.
      upgrade_choices: list of {current, upgrade_to} — feeds to switch from RSS to Atom URL.
      rescue_unread: bool — if true, mark read entries in surviving feed as unread
                     when the removed feed had them unread (default false).
    """
    body = await request.json()
    cross_choices: list[dict] = body.get("cross_folder_choices", [])
    upgrade_choices: list[dict] = body.get("upgrade_choices", [])
    rescue_unread: bool = bool(body.get("rescue_unread", False))

    data = get_feed_duplicates()
    import json as _json
    dup_data = _json.loads(data.body)
    same = dup_data["same_folder"]

    removed: list[dict] = []
    rescued_count = 0

    # Same-folder: auto-remove slash variant from the shared folder.
    for dup in same:
        feed_url = dup["remove"]
        keep_url = dup["keep"]
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
                with get_meta_connection() as conn:
                    rescued_count += purge_orphaned_feed(
                        reader, conn, feed_url,
                        archive_pending=False,
                        rescue_to=keep_url if rescue_unread else None,
                    )
        removed.append({"removed": feed_url, "kept": keep_url})
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
                with get_meta_connection() as conn:
                    rescued_count += purge_orphaned_feed(
                        reader, conn, remove,
                        archive_pending=False,
                        rescue_to=keep if rescue_unread else None,
                    )
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
                with get_meta_connection() as conn:
                    purge_orphaned_feed(
                        reader, conn, current,
                        archive_pending=False,
                        rescue_to=upgrade_to,
                    )
        upgraded.append({"from": current, "to": upgrade_to})
        LOGGER.info("[deduplicate] upgraded %s → %s", current, upgrade_to)

    invalidate_meta_structure_cache()
    return JSONResponse({"removed": removed, "count": len(removed), "upgraded": upgraded, "upgraded_count": len(upgraded), "rescued_count": rescued_count})


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
    normalized_read_filter = normalize_read_filter(read_filter)
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    sort_query = (
        build_sort_query(sort_by, sort_dir)
        + build_read_filter_query(read_filter)
        + build_star_only_query(star_only)
        + build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    )
    entry_query = ""
    if feed_url and entry_id:
        entry_query = _entry_query_suffix(feed_url, entry_id)
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
        # Only touch DeviantArt when it's actually configured — avoids needless work
        # (and the access-token settings lookup) for the common no-DA setup.
        _da_cid, _da_secret = get_deviantart_credentials()
        if _da_cid and _da_secret:
            deviantart_service.refresh_all_deviantart_feeds(conn, _da_cid, _da_secret, access_token=get_deviantart_user_token())
    feed_refresh_service.update_feeds(feed_urls, enhance=False)
    _run_automation_after_refresh(feed_urls)
    invalidate_unread_counts_cache()
    # Lead images / durations are network-heavy; fetch them off the request path
    # so the redirect (and updated "new" badges) returns promptly.
    _spawn_feed_enhancement(feed_urls)
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
    normalized_read_filter = normalize_read_filter(read_filter)
    retry_after_seconds = 0 if _is_local_dev_feed(feed_url) else check_and_mark_manual_refresh()
    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = (
        build_sort_query(sort_by, sort_dir)
        + build_read_filter_query(read_filter)
        + build_star_only_query(star_only)
        + build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    )
    entry_query = _entry_query_suffix(feed_url, entry_id)
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
    feed_refresh_service.update_feeds([feed_url], enhance=False)
    _run_automation_after_refresh({feed_url})
    invalidate_unread_counts_cache()
    _spawn_feed_enhancement([feed_url])
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
    normalized_read_filter_mr = normalize_read_filter(read_filter)
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter_mr)
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


@app.post("/feeds/bulk")
def bulk_feed_action(
    request: Request,
    action: str = Form(...),
    feed_urls: str = Form(...),
    to_folder_id: int | None = Form(default=None),
):
    """Apply one action to a set of selected feeds (Settings → Feeds toolbar).

    feed_urls is newline-separated. Returns JSON {ok, action, count}. Reuses the
    same per-feed helpers as the single-feed routes so behavior stays identical.
    """
    urls = [u.strip() for u in feed_urls.split("\n") if u.strip()]
    if not urls:
        return JSONResponse({"ok": False, "error": "No feeds selected."}, status_code=400)
    count = 0
    try:
        if action == "disable":
            for u in urls:
                disable_feed(u)
                count += 1
        elif action == "enable":
            for u in urls:
                enable_feed(u)
                count += 1
        elif action == "mark-read":
            count = mark_feeds_as_read(set(urls))
            invalidate_unread_counts_cache()
        elif action == "refresh":
            feed_refresh_service.update_feeds(urls, enhance=False)
            _run_automation_after_refresh(set(urls))
            invalidate_unread_counts_cache()
            _spawn_feed_enhancement(urls)
            count = len(urls)
        elif action == "move":
            if not to_folder_id:
                return JSONResponse({"ok": False, "error": "No target folder."}, status_code=400)
            with get_meta_connection() as conn:
                if not conn.execute("SELECT 1 FROM folders WHERE id = ?", (to_folder_id,)).fetchone():
                    return JSONResponse({"ok": False, "error": "Target folder does not exist."}, status_code=400)
                # Clean move: drop every existing membership, then add to target.
                for u in urls:
                    conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (u,))
                    conn.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (to_folder_id, u))
                    count += 1
            invalidate_meta_structure_cache()
        elif action == "unsubscribe":
            # Reuse a single reader + meta connection across the whole batch.
            with get_reader() as reader, get_meta_connection() as conn:
                for u in urls:
                    conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (u,))
                    still_used = conn.execute("SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1", (u,)).fetchone()
                    if not still_used:
                        purge_orphaned_feed(reader, conn, u, archive_pending=True)
                    count += 1
            invalidate_meta_structure_cache()
        else:
            return JSONResponse({"ok": False, "error": f"Unknown action: {action}"}, status_code=400)
    except Exception as exc:
        LOGGER.exception("[feeds/bulk] action=%s failed", action)
        # Don't leak internal exception detail to the client; it's in the logs.
        return JSONResponse({"ok": False, "error": "Action failed."}, status_code=500)
    return JSONResponse({"ok": True, "action": action, "count": count})


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
    _nrf_fmr = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_fmr)
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
    is_async = is_async_action_request(request, "lectio-post-read-toggle") or is_async_action_request(request, "lectio-entry-read-toggle")
    include_history = is_async_action_request(request, "lectio-entry-read-toggle")

    # For async toggles (post-list checkbox, entry pane toggle) pre-read the
    # entry/feed for history while the reader lock is not contested, then fire
    # the writes in a background thread so the JSON response returns immediately
    # regardless of any ongoing background-refresh write lock on the reader DB.
    if is_async:
        _title, _link, _feed_title = "", "", ""
        if read and include_history:
            with get_reader() as reader:
                entry_obj = reader.get_entry((feed_url, entry_id), None)
                feed_obj = reader.get_feed(feed_url, None)
            _title = str(getattr(entry_obj, "title", None) or "")
            _link = str(getattr(entry_obj, "link", None) or "")
            _feed_title = str(getattr(feed_obj, "title", None) or "")

        _fu, _eid, _read, _do_hist = feed_url, entry_id, bool(read), include_history
        _hist_args = (_title, _link, _feed_title)
        # Capture the current user: the daemon thread below does not inherit this
        # request's contextvars, so without re-binding the (un)read write would
        # land in the default (legacy) user's DB instead of the actual user's.
        _uid = tenancy.current_user_id()

        def _bg_toggle() -> None:
            try:
                with get_reader() as reader:
                    if _read:
                        reader.mark_entry_as_read((_fu, _eid))
                    else:
                        reader.mark_entry_as_unread((_fu, _eid))
            except Exception:
                LOGGER.warning("background mark_entry_(un)read failed for %s/%s", _fu, _eid, exc_info=True)
            try:
                if _read:
                    upsert_entry_read_state(_fu, _eid)
                else:
                    delete_entry_read_state(_fu, _eid)
            except Exception:
                LOGGER.warning("background entry_read_state write failed for %s/%s", _fu, _eid, exc_info=True)
            if _read and _do_hist:
                try:
                    append_read_history(_fu, _eid, *_hist_args)
                except Exception:
                    LOGGER.warning("background append_read_history failed for %s/%s", _fu, _eid, exc_info=True)
            with unread_counts_cache_lock:
                global _unread_counts_generation
                _unread_counts_generation += 1
                unread_counts_cache.clear()

        threading.Thread(target=_run_in_user_context, args=(_uid, _bg_toggle), daemon=True).start()
        return JSONResponse({"ok": True, "feed_url": feed_url, "entry_id": entry_id, "read": bool(read)})

    # Synchronous (full-page redirect) path — wait for writes before redirecting
    # so the reloaded page reflects the correct read state.
    with get_reader() as reader:
        if read:
            reader.mark_entry_as_read((feed_url, entry_id))
            try:
                upsert_entry_read_state(feed_url, entry_id)
            except Exception:
                LOGGER.warning("upsert_entry_read_state failed in mark_entry_read (db contention?)", exc_info=True)
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

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    _nrf_er = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_er)

    entry_query = _entry_query_suffix(feed_url, entry_id, include=bool(select_entry))

    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}",
        status_code=303,
    )


def _run_on_star_destinations(feed_url: str, entry_id: str) -> None:
    """Fire the configured "on star" destinations for a freshly-starred entry.
    One-way (never on unstar), best-effort; runs in a background thread."""
    try:
        send_ip = get_runtime_setting(SETTING_STAR_SEND_INSTAPAPER, "0") == "1"
        playlist_id = (get_runtime_setting(SETTING_STAR_SEND_YT_PLAYLIST) or "").strip()
        email_to = (get_runtime_setting(SETTING_STAR_SEND_EMAIL) or "").strip()
        send_quire = get_runtime_setting(SETTING_STAR_SEND_QUIRE, "0") == "1" and is_quire_configured()
        if not (send_ip or playlist_id or email_to or send_quire):
            return
        with get_reader() as reader:
            entry = reader.get_entry((feed_url, entry_id), None)
            feed_title = ""
            if entry:
                try:
                    feed_title = str(getattr(reader.get_feed(feed_url), "title", None) or "")
                except Exception:
                    feed_title = ""
        if not entry:
            return
        link = str(getattr(entry, "link", "") or "")
        title = str(getattr(entry, "title", "") or "")

        if send_ip and is_instapaper_configured() and link:
            u = get_runtime_setting(SETTING_INSTAPAPER_USERNAME).strip()
            p = get_runtime_setting(SETTING_INSTAPAPER_PASSWORD).strip()
            if u and p:
                ok, err = _instapaper_save_url(u, p, link, title)
                if not ok:
                    LOGGER.warning("[on-star] instapaper save failed: %s", err)

        if playlist_id:
            token = get_youtube_oauth_token()
            if token:
                body = "".join((c.value or "") for c in (getattr(entry, "content", None) or []))
                body += str(getattr(entry, "summary", "") or "")
                for vid in youtube_embeds.video_ids_in_text(link, body):
                    try:
                        youtube_oauth_service.add_video_to_playlist(token, playlist_id, vid)
                    except youtube_oauth_service.QuotaExceeded:
                        mark_yt_quota_exhausted()
                        break
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("[on-star] playlist add failed for %s: %s", vid, exc)

        if email_to and is_email_configured() and link:
            try:
                ok, err = send_article_email(get_resend_api_key(), get_resend_from(), email_to,
                                             title, feed_title, link, _get_entry_excerpt(entry))
                if not ok:
                    LOGGER.warning("[on-star] email failed: %s", err)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[on-star] email failed: %s", exc)

        if send_quire and link:
            if get_quire_usage_status()["state"] == "blocked":
                LOGGER.warning("[on-star] quire skipped: rate limit reached")
            else:
                token = get_quire_user_token()
                if token:
                    ok, err = _quire_add_entry(token, quire_project_oid(), title, link, feed_title)
                    if not ok:
                        LOGGER.warning("[on-star] quire add failed: %s", err)

        reddit_sub = (get_runtime_setting(SETTING_STAR_SEND_REDDIT_SUBREDDIT) or "").strip().lstrip("r/").strip("/")
        if reddit_sub and link:
            reddit_token = get_reddit_user_token()
            if reddit_token:
                try:
                    reddit_service.submit_link(reddit_token, reddit_sub, title, link)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("[on-star] reddit submit failed: %s", exc)
    except Exception:
        LOGGER.exception("[on-star] error for %s/%s", feed_url, entry_id)


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
    newly_starred = False
    with get_meta_connection() as conn:
        if saved:
            cur = conn.execute(
                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                (feed_url, entry_id),
            )
            newly_starred = cur.rowcount > 0
        else:
            conn.execute(
                "DELETE FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )
        conn.commit()

    # "On star, also send to…" — fire configured destinations once, only on a
    # genuine new star (rowcount), off-request so the star stays snappy.
    if newly_starred:
        _uid = tenancy.current_user_id()
        threading.Thread(
            target=lambda: _run_in_user_context(_uid, _run_on_star_destinations, feed_url, entry_id),
            daemon=True,
        ).start()

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
    _nrf_es = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_es)

    entry_query = _entry_query_suffix(feed_url, entry_id, include=bool(select_entry))

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
    _nrf_et = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_et)

    entry_query = _entry_query_suffix(feed_url, entry_id, include=bool(select_entry))
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


@app.post("/tags/delete")
def delete_manual_tag(
    request: Request,
    tag: str = Form(...),
):
    normalized = normalize_tag_value(tag)
    if not normalized:
        if request.headers.get("X-Requested-With") in ("lectio-ajax", "lectio-sidebar"):
            return JSONResponse({"ok": False, "error": "Invalid tag."}, status_code=400)
        return RedirectResponse(url="/", status_code=303)

    removed = delete_manual_tag_everywhere(normalized)

    if request.headers.get("X-Requested-With") in ("lectio-ajax", "lectio-sidebar"):
        return JSONResponse({"ok": True, "tag": normalized, "removed": removed})

    message = f"Removed #{normalized} from {removed} post{'' if removed == 1 else 's'}."
    return RedirectResponse(url=f"/?message={quote_plus(message)}", status_code=303)


@app.post("/tags/rename")
def rename_manual_tag(
    request: Request,
    old_tag: str = Form(...),
    new_tag: str = Form(...),
    force: str = Form(default=""),
):
    old_norm = normalize_tag_value(old_tag)
    new_norm = normalize_tag_value(new_tag)
    is_ajax = request.headers.get("X-Requested-With") in ("lectio-ajax", "lectio-sidebar")

    if not old_norm or not new_norm:
        if is_ajax:
            return JSONResponse({"ok": False, "error": "Invalid tag name."}, status_code=400)
        return RedirectResponse(url="/", status_code=303)
    if old_norm == new_norm:
        if is_ajax:
            return JSONResponse({"ok": False, "error": "New name is the same as the old name."}, status_code=400)
        return RedirectResponse(url="/", status_code=303)

    # Without force, warn the caller if new_tag already exists so the UI can
    # ask for explicit confirmation before merging two tags.
    if not force:
        with get_reader() as reader:
            new_key = f"{MANUAL_TAG_KEY_PREFIX}{new_norm}"
            if reader.get_entry_counts(tags=[new_key]).total > 0:
                if is_ajax:
                    return JSONResponse({"ok": False, "exists": True, "new_tag": new_norm})
                return RedirectResponse(url="/", status_code=303)

    count, merged = rename_manual_tag_everywhere(old_norm, new_norm)
    if is_ajax:
        return JSONResponse({"ok": True, "old_tag": old_norm, "new_tag": new_norm, "count": count, "merged": merged})
    return RedirectResponse(url="/", status_code=303)


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
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(normalized_star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    entry_query = _entry_query_suffix(feed_url, entry_id)

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
    _nrf_mot = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_mot)
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
    _nrf_mnu = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_mnu)
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
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_query = normalize_search_query(q)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
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
            f"{build_sort_query(sort_by, sort_dir)}"
            f"{build_read_filter_query(read_filter)}"
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
    cc_me: bool = Form(False),
):
    if not is_email_configured():
        return JSONResponse({"ok": False, "error": "Email not configured."}, status_code=503)

    to_addr = to_addr.strip()
    if not to_addr:
        return JSONResponse({"ok": False, "error": "No recipient address."}, status_code=400)

    # "Cc me" makes the share a repliable thread: set Reply-To to the sender's
    # profile email so a recipient's reply reaches them (the From is the Resend
    # sender domain, which may not accept mail), and Cc the sender a copy. Skip the
    # Cc when the sender would be Cc'ing their own to_addr (Reply-To to self is
    # harmless and still wanted).
    cc_addr: str | None = None
    reply_to: str | None = None
    if cc_me:
        with get_meta_connection() as conn:
            profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
        if profile_email:
            reply_to = profile_email
            if profile_email.lower() != to_addr.lower():
                cc_addr = profile_email

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
        cc_addr=cc_addr,
        reply_to=reply_to,
    )
    if ok:
        msg = f"Sent to {to_addr}" + (f" (Cc {cc_addr})" if cc_addr else "")
        return JSONResponse({"ok": True, "message": msg})
    LOGGER.warning("email send failed for %s/%s: %s", feed_url, entry_id, error)
    return JSONResponse({"ok": False, "error": error or "Send failed."}, status_code=500)


def _instapaper_save_url(username: str, password: str, url: str, title: str) -> tuple[bool, str | None]:
    """POST a URL to the Instapaper Simple API. Returns (ok, error). Instapaper
    dedupes by URL on its side, so re-saving the same URL is harmless."""
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({
            "username": username, "password": password, "url": url, "title": title or "",
        }).encode()
        req = urllib.request.Request(
            "https://www.instapaper.com/api/add", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        if status in (200, 201):
            return True, None
        return False, f"Instapaper returned {status}."
    except Exception as exc:  # noqa: BLE001
        return False, str(exc) or "Request failed."


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

    ok, err = _instapaper_save_url(username, password, url, entry.title or "")
    if ok:
        return JSONResponse({"ok": True})
    LOGGER.warning("Instapaper save failed for %s: %s", url, err)
    return JSONResponse({"ok": False, "error": err}, status_code=502)


def _quire_add_entry(token: str, project_oid: str, title: str, link: str, feed_title: str = "") -> tuple[bool, str | None]:
    """Create a Quire task for an entry. Returns (ok, error). Raises nothing —
    rate-limit and HTTP errors are mapped to (False, message)."""
    name = (title or link or "Untitled").strip()
    desc_parts = [p for p in (link, feed_title) if p]
    description = "\n".join(desc_parts)
    try:
        quire_service.create_task(token, project_oid, name, description)
        return True, None
    except quire_service.QuireRateLimited as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc) or "Request failed."


@app.post("/entries/quire")
def add_to_quire(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    project_oid: str | None = Form(None),
):
    if not is_quire_connected():
        return JSONResponse({"ok": False, "error": "Quire not connected."}, status_code=503)
    project_oid = (project_oid or "").strip() or quire_project_oid()
    if not project_oid:
        return JSONResponse({"ok": False, "error": "Pick a Quire destination project."}, status_code=503)
    if get_quire_usage_status()["state"] == "blocked":
        return JSONResponse({"ok": False, "error": "Quire rate limit reached — try again shortly."}, status_code=429)
    token = get_quire_user_token()
    if not token:
        return JSONResponse({"ok": False, "error": "Quire session expired — reconnect in Settings."}, status_code=503)

    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        feed_title = ""
        if entry:
            try:
                feed_title = str(getattr(reader.get_feed(feed_url), "title", None) or "")
            except Exception:
                feed_title = ""
    if not entry:
        return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)

    ok, err = _quire_add_entry(token, project_oid, entry.title or "", entry.link or "", feed_title)
    if ok:
        return JSONResponse({"ok": True})
    LOGGER.warning("Quire add failed for %s: %s", entry.link or entry_id, err)
    return JSONResponse({"ok": False, "error": err}, status_code=502)


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


# Limit concurrent takeout exports to 1: each one reads the full reader DB + meta DB
# and builds an in-memory ZIP, so letting them pile up would multiply RAM + lock
# contention. OPML export is fast/small and is not gated by this semaphore.
_takeout_export_sem = threading.Semaphore(1)


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
    if not _takeout_export_sem.acquire(blocking=False):
        return Response(
            content="An export is already in progress. Please wait and try again.",
            status_code=429,
            media_type="text/plain",
        )
    try:
        with get_meta_connection() as conn:
            opml_text = export_opml_text(conn)
            zip_bytes = takeout_service.build_takeout_zip(
                conn, tenancy.reader_db_path(), opml_text, app_version=STATIC_ASSET_VERSION
            )
        date_str = datetime.now().strftime("%Y%m%d")
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=lectio-takeout-{date_str}.zip"},
        )
    finally:
        _takeout_export_sem.release()


@app.post("/takeout/import")
async def takeout_import(request: Request, takeout_file: Annotated[UploadFile, File(...)]):
    data = await takeout_file.read()
    try:
        with get_meta_connection() as conn:
            summary = takeout_service.import_takeout_zip(conn, tenancy.reader_db_path(), data)
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


# Formats we re-encode when downscaling /api/img cache entries. Anything else
# (SVG, animated GIF/APNG, unknown) is stored byte-for-byte so we never flatten
# an animation or corrupt a vector image.
_IMG_DOWNSCALE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})

# Default freshness hint for cache hits (the upstream Cache-Control is gone by then).
_IMG_CACHE_CONTROL = "public, max-age=86400"

# Safety caps for proxied/cached images — the bytes are untrusted remote input.
#  - Byte cap: an oversized response is served through but NOT decoded or cached,
#    so a single huge image can't bloat the cache DB or be re-encoded in memory.
#  - Pixel cap: img.resize() materializes the *full source* bitmap (a 12000x8000
#    image is ~275 MB of RGB), so we skip downscaling above this and store the
#    original bytes instead — the browser decodes them, not our worker. This
#    closes a decompression-bomb / memory-DoS vector that Pillow's default
#    MAX_IMAGE_PIXELS (only trips at ~2x ~89 Mpx) leaves open.
_IMG_CACHE_MAX_BYTES = 16 * 1024 * 1024   # 16 MB
_IMG_MAX_DECODE_PIXELS = 40_000_000       # 40 megapixels


def _maybe_downscale_image(raw: bytes, max_dim: int) -> tuple[bytes, str | None]:
    """Downscale so the longest side is <= max_dim, preserving aspect ratio and
    never upscaling. Returns (bytes, content_type). The content_type is None when
    the original bytes are returned unchanged (animated/vector/unknown formats,
    already-small images, max_dim<=0, or any failure) so the caller keeps the
    upstream content-type."""
    if max_dim <= 0:
        return raw, None
    try:
        img = _PILImage.open(io.BytesIO(raw))
        fmt = (img.format or "").upper()
        # Don't touch animations (would flatten) or formats we don't re-encode.
        if fmt not in _IMG_DOWNSCALE_FORMATS or getattr(img, "is_animated", False):
            return raw, None
        w, h = img.size
        if max(w, h) <= max_dim:
            return raw, None  # already small enough; never upscale
        if w * h > _IMG_MAX_DECODE_PIXELS:
            # Too large to resize safely (resize loads the whole source bitmap);
            # store the original bytes rather than materialize it in the worker.
            return raw, None
        scale = max_dim / max(w, h)
        new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
        buf = io.BytesIO()
        if fmt == "JPEG":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.resize(new_size, _PILImage.Resampling.LANCZOS).save(buf, format="JPEG", quality=90, optimize=True)
            return buf.getvalue(), "image/jpeg"
        if fmt == "PNG":
            # P/1-bit modes resize poorly; promote to RGBA to keep transparency.
            if img.mode in ("P", "1"):
                img = img.convert("RGBA")
            img.resize(new_size, _PILImage.Resampling.LANCZOS).save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "image/png"
        # WEBP
        if img.mode == "P":
            img = img.convert("RGBA")
        img.resize(new_size, _PILImage.Resampling.LANCZOS).save(buf, format="WEBP", quality=90, method=4)
        return buf.getvalue(), "image/webp"
    except Exception:
        return raw, None


def _img_cache_get(cache_key: str) -> tuple[bytes, str] | None:
    """Return (body, content_type) for a cache hit and bump last_accessed (the
    last-accessed TTL is what keeps actively-browsed images alive). None on miss."""
    try:
        with get_img_cache_connection() as conn:
            row = conn.execute(
                "SELECT body, content_type FROM img_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE img_cache SET last_accessed = ? WHERE cache_key = ?",
                (time.time(), cache_key),
            )
            return bytes(row["body"]), row["content_type"]
    except Exception:
        # Non-fatal (we fall back to a fetch), but log so a failing cache backend
        # — DB corruption, permissions, schema drift — isn't silently invisible.
        LOGGER.warning("[img-cache] read failed for %s; treating as miss", cache_key, exc_info=True)
        return None


def _img_cache_store(cache_key: str, body: bytes, content_type: str) -> None:
    now = time.time()
    try:
        with get_img_cache_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO img_cache
                    (cache_key, content_type, body, size, created_at, last_accessed)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cache_key, content_type, body, len(body), now, now),
            )
    except Exception:
        LOGGER.warning("[img-cache] store failed for %s", cache_key, exc_info=True)


# Query params that are per-request signing tokens, not image identity. Stripping
# them from the cache key lets a signed-CDN image (GitHub private-user-images JWT,
# wixmp/S3 ?token/X-Amz-*) stay cache-resident across token rotations, so it keeps
# loading after the original short-lived URL expires. The full URL (with token) is
# still used for the actual fetch.
_IMG_CACHE_VOLATILE_PARAMS = frozenset({
    "jwt", "token", "sig", "signature", "expires", "exp",
    "x-amz-algorithm", "x-amz-credential", "x-amz-date", "x-amz-expires",
    "x-amz-security-token", "x-amz-signature", "x-amz-signedheaders",
})


def _img_cache_key_url(u: str) -> str:
    """Normalize an image URL for the cache key by dropping volatile signing params."""
    try:
        parsed = urlparse(u)
    except ValueError:
        return u
    kept = [
        (k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _IMG_CACHE_VOLATILE_PARAMS
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


# Statuses that signal a hotlink/WAF refusal of an honest image fetch — the cue to
# retry once with a same-origin Referer. Shared by api_img_proxy and thumbnail_proxy
# so the two proxies' escalation policy can't drift apart.
_HOTLINK_REFUSAL_CODES = frozenset({403, 503})


def _same_origin_referer(u: str) -> str | None:
    """Origin root (scheme://host/) of an image URL, for use as a same-origin
    Referer when a host hotlink-protects its images. None if the URL has no
    usable http(s) origin."""
    try:
        parsed = urlparse(u)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


@app.get("/api/img")
async def api_img_proxy(u: str) -> Response:
    """Server-side image proxy with a content-addressed cache.

    Fetches external images on behalf of the browser so that
    Cross-Origin-Resource-Policy restrictions (same-site / same-origin) set by
    the image server do not prevent them from loading in the entry pane.
    Only http:// and https:// URLs are accepted. SSRF is prevented by
    url_guard.safe_get_async, which validates the initial URL and every redirect
    hop against private / loopback / link-local IP space.

    On a cache miss the fetched bytes are optionally downscaled (longest side ->
    LECTIO_IMG_CACHE_MAX_DIM) and stored in the global img_cache, keyed by a hash
    of the source URL. Entries are evicted by last-accessed TTL in daily
    maintenance (see _run_global_maintenance), so this also makes hotlink images
    behind short-lived signed URLs (e.g. wixmp) survive token expiry.
    """
    if urlparse(u).scheme not in ("http", "https"):
        return Response(status_code=400)
    cache_key = hashlib.sha256(_img_cache_key_url(u).encode("utf-8")).hexdigest()
    cached = _img_cache_get(cache_key)
    if cached is not None:
        body, content_type = cached
        return Response(content=body, media_type=content_type, headers={"Cache-Control": _IMG_CACHE_CONTROL})
    try:
        # follow_redirects=False so safe_get_async controls (and re-validates)
        # each hop instead of httpx silently bouncing to an internal address.
        async with httpx.AsyncClient(follow_redirects=False, timeout=12.0) as client:
            headers = {"User-Agent": READABILITY_USER_AGENT}
            resp = await url_guard.safe_get_async(client, u, headers=headers)
            # Hotlink protection: some hosts serve a 403 (often text/html) for an
            # image fetched without a Referer, but 200 image/* once a same-origin
            # Referer is present (e.g. fabiensanglard.net's .webp files). Only
            # after an honest request is actually refused do we retry with the
            # image's own origin as Referer — never preemptively. Mirrors the
            # honest-first WAF escalation in services/lead_images.py.
            if resp.status_code in _HOTLINK_REFUSAL_CODES:
                referer = _same_origin_referer(u)
                if referer:
                    resp = await url_guard.safe_get_async(
                        client, u, headers={**headers, "Referer": referer}
                    )
    except url_guard.UnsafeURLError:
        return Response(status_code=403)
    except Exception:
        return Response(status_code=502)
    content_type = resp.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return Response(status_code=422)
    body = resp.content
    cache_ctrl = resp.headers.get("cache-control", _IMG_CACHE_CONTROL)
    if len(body) > _IMG_CACHE_MAX_BYTES:
        # Too large to cache/re-encode; pass the original through untouched so it
        # still displays, but don't decode or store it.
        return Response(content=body, media_type=content_type, headers={"Cache-Control": cache_ctrl})
    downscaled, new_ct = _maybe_downscale_image(body, get_img_cache_max_dim())
    if new_ct is not None:
        body, content_type = downscaled, new_ct
    _img_cache_store(cache_key, body, content_type)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": cache_ctrl},
    )


_FAVICON_CACHE_CONTROL = "public, max-age=86400"
_FAVICON_FALLBACK_PATH = BASE_DIR / "static" / "favicon-fallback.svg"


@app.get("/api/favicon")
async def api_favicon(domain: str) -> Response:
    """Favicon resolver with a three-hop fallback chain.

    Tries in order:
      1. Google's faviconV2 service (via s2/favicons redirect).
      2. The site's own /favicon.ico.
      3. A bundled neutral SVG placeholder.

    Every outbound fetch goes through url_guard.safe_get_async with
    follow_redirects=False so SSRF is prevented on the domain parameter and all
    redirect hops. Results (including the placeholder) are cached in the shared
    img_cache DB under the key "favicon:<host>", so repeat lookups skip the
    chain entirely.

    Auth-exempt and CSRF-exempt (same as /api/img) — the browser's <img> tag
    fetches it without cookies/tokens.
    """
    # Reject clearly unsafe domain values before any DNS/network work.
    host = domain.strip().lower()
    if not host:
        return Response(status_code=400)
    # Reject anything carrying a path or an explicit scheme prefix (SSRF vector).
    # Legitimate domain params are plain hostnames like "example.com". Match the
    # scheme as "http:"/"https:" (not a bare "http" prefix, which would wrongly
    # reject real hosts such as "httpbin.org").
    if "/" in host or host.startswith(("http:", "https:")):
        return Response(status_code=400)

    cache_key = f"favicon:{host}"
    cached = _img_cache_get(cache_key)
    if cached is not None:
        body, content_type = cached
        return Response(content=body, media_type=content_type, headers={"Cache-Control": _FAVICON_CACHE_CONTROL})

    google_url = f"https://www.google.com/s2/favicons?domain={quote_plus(host)}&sz=32"
    favicon_ico_url = f"https://{host}/favicon.ico"

    async with httpx.AsyncClient(follow_redirects=False, timeout=8.0) as client:
        # Hop 1: Google faviconV2 (via redirect from s2/favicons).
        try:
            resp = await url_guard.safe_get_async(
                client, google_url, headers={"User-Agent": READABILITY_USER_AGENT}
            )
            ct = resp.headers.get("content-type", "")
            if resp.status_code == 200 and ct.startswith("image/"):
                body = resp.content
                _img_cache_store(cache_key, body, ct)
                return Response(content=body, media_type=ct, headers={"Cache-Control": _FAVICON_CACHE_CONTROL})
        except (url_guard.UnsafeURLError, Exception):
            pass

        # Hop 2: site's own /favicon.ico.
        if url_guard.is_safe_outbound_url(favicon_ico_url):
            try:
                resp = await url_guard.safe_get_async(
                    client, favicon_ico_url, headers={"User-Agent": READABILITY_USER_AGENT}
                )
                ct = resp.headers.get("content-type", "")
                if resp.status_code == 200 and (ct.startswith("image/") or ct.startswith("application/octet")):
                    body = resp.content
                    ct_store = ct if ct.startswith("image/") else "image/x-icon"
                    _img_cache_store(cache_key, body, ct_store)
                    return Response(content=body, media_type=ct_store, headers={"Cache-Control": _FAVICON_CACHE_CONTROL})
            except (url_guard.UnsafeURLError, Exception):
                pass

    # Hop 3: bundled neutral SVG placeholder.
    try:
        placeholder = _FAVICON_FALLBACK_PATH.read_bytes()
    except OSError:
        return Response(status_code=502)
    _img_cache_store(cache_key, placeholder, "image/svg+xml")
    return Response(content=placeholder, media_type="image/svg+xml", headers={"Cache-Control": _FAVICON_CACHE_CONTROL})


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

    reader_db_bytes = _db_bytes(tenancy.reader_db_path())
    meta_db_bytes = _db_bytes(tenancy.meta_db_path())
    thumb_db_bytes = _db_bytes(THUMB_DB_PATH)
    img_cache_db_bytes = _db_bytes(IMG_CACHE_DB_PATH)
    starred_archive_db_bytes = _db_bytes(tenancy.starred_archive_db_path())
    archive_stats = starred_archive_service.get_stats()

    thumb_count = 0
    try:
        with get_thumb_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM thumb_cache").fetchone()
            thumb_count = int(row["c"]) if row else 0
    except Exception:
        pass

    img_cache_count = 0
    try:
        with get_img_cache_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM img_cache").fetchone()
            img_cache_count = int(row["c"]) if row else 0
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
            "img_cache_db_bytes": img_cache_db_bytes,
            "img_cache_count": img_cache_count,
            "starred_archive_db_bytes": starred_archive_db_bytes,
            "starred_archive_complete": archive_stats["complete"],
            "starred_archive_pending": archive_stats["pending"],
            "starred_archive_in_progress": archive_stats["in_progress"],
            "starred_archive_failed": archive_stats["failed"],
            "starred_archive_pending_removal": archive_stats["pending_removal"],
            "starred_archive_asset_count": archive_stats["asset_count"],
        }
    )


# ---------------------------------------------------------------------------
# WebSub callback routes
# ---------------------------------------------------------------------------

@app.get("/websub/callback")
def websub_verify(
    feed: str = Query(default=""),
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_topic: str = Query(default="", alias="hub.topic"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
    hub_lease_seconds: int | None = Query(default=None, alias="hub.lease_seconds"),
):
    """Hub challenge-response verification (step 2 of subscribe handshake)."""
    if not websub_service or not feed or hub_mode != "subscribe" or not hub_challenge:
        return Response(status_code=404)
    challenge = _websub_verify_fanout(feed, hub_topic, hub_challenge, hub_lease_seconds)
    if challenge is None:
        return Response(status_code=404)
    return Response(content=challenge, media_type="text/plain")


@app.post("/websub/callback")
async def websub_push(request: Request, feed: str = Query(default="")):
    """Receive a push notification from a WebSub hub."""
    if not websub_service or not feed:
        return Response(status_code=400)
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256") or request.headers.get("x-hub-signature", "")
    # Fan out across every subscribing user in the background (signature is
    # verified per-user inside), so the push refreshes each subscriber's own
    # reader through the normal pipeline (dedup, automation, lead images). The
    # hub only needs a prompt 2xx ack, so don't block on the work.
    threading.Thread(
        target=_process_websub_push,
        args=(feed, body, sig),
        daemon=True,
    ).start()
    return Response(status_code=204)


async def _fever_handler(request: Request) -> Response:
    """Shared handler for GET and POST requests to the Fever API endpoint."""
    if not fever_service:
        return JSONResponse({"api_version": 3, "auth": 0}, status_code=503)

    query_params = dict(request.query_params)
    form_data: dict = {}
    if request.method == "POST":
        try:
            body = await request.form()
            form_data = dict(body)
        except Exception:
            pass
    params = {**query_params, **form_data}

    if "api" not in params:
        return Response(status_code=404)

    api_key = params.get("api_key", "")
    # Resolve the api_key (md5(username:api_token)) to a user_id and bind
    # the tenancy context so the dispatch reads that user's data.
    uid = user_store.fever_user_for_key(api_key) if user_store else None
    if not uid:
        return JSONResponse({"api_version": 3, "auth": 0})
    with tenancy.user_context(uid):
        return JSONResponse(_fever_build_result(params))


def _fever_build_result(params: dict) -> dict:
    """Build the Fever response for an authenticated request. Runs under the
    caller's tenancy context (the bound user in multi mode)."""
    assert fever_service is not None  # caller already checked
    result: dict = {
        "api_version": 3,
        "auth": 1,
        "last_refreshed_on_time": int(time.time()),
    }

    # Mark actions (processed before data requests).
    if "mark" in params:
        mark_type = params.get("mark", "")
        action = params.get("as", "")
        try:
            item_id_raw = params.get("id", "")
            before_raw = params.get("before", "0")
            if mark_type == "item" and item_id_raw:
                fever_service.mark_item(int(item_id_raw), action)
            elif mark_type == "feed" and action == "read":
                fever_service.mark_feed_read(int(item_id_raw), int(before_raw))
            elif mark_type == "group" and action == "read":
                fever_service.mark_group_read(int(item_id_raw), int(before_raw))
        except (ValueError, Exception):
            pass

    # Data requests.
    if "feeds" in params or "groups" in params:
        data = fever_service.get_feeds_and_groups()
        result["feeds_groups"] = data["feeds_groups"]
        if "feeds" in params:
            result["feeds"] = data["feeds"]
        if "groups" in params:
            result["groups"] = data["groups"]

    if "items" in params:
        try:
            since_id = int(params["since_id"]) if "since_id" in params else None
            max_id = int(params["max_id"]) if "max_id" in params else None
            with_ids = params.get("with_ids")
            result.update(fever_service.get_items(since_id=since_id, max_id=max_id, with_ids=with_ids))
        except (ValueError, Exception) as exc:
            LOGGER.warning("[fever] get_items error: %s", exc)

    if "unread_item_ids" in params:
        result["unread_item_ids"] = fever_service.get_unread_item_ids()

    if "saved_item_ids" in params:
        result["saved_item_ids"] = fever_service.get_saved_item_ids()

    if "links" in params:
        result["links"] = []

    if "favicons" in params:
        result["favicons"] = []

    return result


@app.get("/fever")
async def fever_get(request: Request) -> Response:
    """Fever API endpoint (GET) — used by some clients for initial auth checks."""
    return await _fever_handler(request)


@app.post("/fever")
async def fever_post(request: Request) -> Response:
    """Fever API endpoint (POST) — primary method used by Fever-compatible clients."""
    return await _fever_handler(request)


# ================================================================== GReader API


def _run_in_user_context(uid: str, fn, *args, **kwargs) -> None:
    """Run ``fn`` in a background thread under tenancy user ``uid``.

    Manually-created threads do not inherit contextvars, so a request that
    captures its user and hands work to a daemon thread must re-bind the context
    there or the work runs as the default user."""
    with tenancy.user_context(uid):
        fn(*args, **kwargs)


def _enhance_feeds_background(feed_urls: list[str]) -> None:
    """Run lead-image + YouTube-duration enhancement for ``feed_urls``, skipping
    feeds another enhancement run is already handling so concurrent manual /
    scheduled refreshes don't duplicate the network work."""
    with _enhancement_inflight_lock:
        todo = [u for u in feed_urls if u not in _enhancement_inflight_feeds]
        _enhancement_inflight_feeds.update(todo)
    if not todo:
        return
    try:
        feed_refresh_service.enhance_feeds(todo)
    finally:
        with _enhancement_inflight_lock:
            _enhancement_inflight_feeds.difference_update(todo)


def _spawn_feed_enhancement(feed_urls: Iterable[str]) -> None:
    """Spawn the lead-image / duration enhancement on a daemon thread bound to
    the current tenancy user, so a refresh request can return immediately."""
    urls = list(feed_urls)
    if not urls:
        return
    threading.Thread(
        target=_run_in_user_context,
        args=(tenancy.current_user_id(), _enhance_feeds_background, urls),
        daemon=True,
    ).start()


def _websub_verify_fanout(feed: str, hub_topic: str, challenge: str, lease: int | None) -> str | None:
    """Confirm a WebSub subscription handshake against the shared subscription store."""
    if websub_service is None:
        return None
    try:
        return websub_service.handle_verification(feed, hub_topic, challenge, lease)
    except Exception:
        LOGGER.exception("[websub] verification failed for %r", feed)
        return None


def _process_websub_push(feed: str, body: bytes, sig: str) -> None:
    """Fan a WebSub push out to every user subscribed to the topic.

    Verifies the push signature once against the shared subscription secret,
    then refreshes each subscriber's reader DB under its own tenancy context."""
    if not websub_service:
        return
    subscribers = websub_service.get_subscribers(feed)
    if not subscribers:
        return
    if not websub_service.verify_push_signature(feed, body, sig):
        LOGGER.warning("[websub] push for %s failed signature check; ignoring", feed)
        return
    for uid in subscribers:
        with tenancy.user_context(uid):
            try:
                feed_refresh_service.update_feeds([feed])
            except Exception:
                LOGGER.exception("[websub] push refresh failed for user %r", uid)


def _greader_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("GoogleLogin auth="):
        return auth[17:].strip()
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "")


def _resolve_greader_user(request: Request) -> str | None:
    """Username authorized for this GReader request, or None.

    """
    token = _greader_token(request)
    return user_store.resolve_greader_token(token) if user_store else None


def _greader_ok(request: Request) -> bool:
    return _resolve_greader_user(request) is not None


@app.post("/greader/accounts/ClientLogin")
async def greader_login(request: Request) -> Response:
    """Authenticate and return a GReader auth token (username + API token)."""
    if user_store is None:
        return Response("Error=ServiceUnavailable\n", status_code=503)
    form = await request.form()
    email = str(form.get("Email") or form.get("email") or "")
    passwd = str(form.get("Passwd") or form.get("passwd") or "")
    local = email.split("@")[0] if "@" in email else email
    uid = user_store.verify_api_token(local, passwd)
    if not uid:
        return Response("Error=BadAuthentication\n", status_code=403)
    token = user_store.issue_greader_token(uid)
    return Response(f"SID={token}\nLSID={token}\nAuth={token}\n", media_type="text/plain")


@app.get("/greader/reader/api/0/user-info")
def greader_user_info(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    display_name = None
    if user_store is not None:
        row = user_store.get_by_id(tenancy.current_user_id())
        display_name = row["username"] if row else tenancy.current_user_id()
    return JSONResponse(greader_service.get_user_info(display_name))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@app.get("/greader/reader/api/0/tag/list")
def greader_tag_list(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return JSONResponse(greader_service.get_tag_list())  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@app.get("/greader/reader/api/0/subscription/list")
def greader_subscription_list(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return JSONResponse(greader_service.get_subscription_list())  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@app.post("/greader/reader/api/0/subscription/edit")
async def greader_subscription_edit(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return Response("OK")


@app.post("/greader/reader/api/0/subscription/quickadd")
async def greader_subscription_quickadd(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return Response("OK")


@app.get("/greader/reader/api/0/unread-count")
def greader_unread_count(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return JSONResponse(greader_service.get_unread_counts())  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@app.get("/greader/reader/api/0/token")
def greader_action_token(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return Response(_greader_token(request), media_type="text/plain")


@app.get("/greader/reader/api/0/stream/items/ids")
def greader_stream_item_ids(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    p = request.query_params
    stream_id = p.get("s", "user/-/state/com.google/reading-list")
    try:
        count = min(int(p.get("n", "20")), 10_000)
    except ValueError:
        count = 20
    continuation = p.get("c") or None
    xt_values = p.getlist("xt")
    exclude_read = "user/-/state/com.google/read" in xt_values
    start_time = int(p["ot"]) if "ot" in p else None
    stop_time = int(p["nt"]) if "nt" in p else None
    oldest_first = p.get("r") == "o"
    return JSONResponse(greader_service.get_stream_item_ids(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        stream_id, count=count, continuation=continuation,
        exclude_read=exclude_read, start_time=start_time,
        stop_time=stop_time, oldest_first=oldest_first,
    ))


@app.post("/greader/reader/api/0/stream/items/contents")
async def greader_stream_items_contents(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    form = await request.form()
    item_ids = [v for v in form.getlist("i") if isinstance(v, str)]
    return JSONResponse(greader_service.get_items_contents(item_ids))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@app.get("/greader/reader/api/0/stream/contents/{stream_id:path}")
def greader_stream_contents_path(stream_id: str, request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    p = request.query_params
    if not stream_id:
        stream_id = p.get("s", "user/-/state/com.google/reading-list")
    try:
        count = min(int(p.get("n", "20")), 10_000)
    except ValueError:
        count = 20
    return JSONResponse(greader_service.get_stream_contents(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        stream_id,
        count=count,
        continuation=p.get("c") or None,
        exclude_read="user/-/state/com.google/read" in p.getlist("xt"),
        oldest_first=p.get("r") == "o",
    ))


@app.get("/greader/reader/api/0/stream/contents")
def greader_stream_contents_query(request: Request) -> Response:
    stream_id = request.query_params.get("s", "user/-/state/com.google/reading-list")
    return greader_stream_contents_path(stream_id, request)


@app.post("/greader/reader/api/0/edit-tag")
async def greader_edit_tag(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    form = await request.form()
    greader_service.edit_tag(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        [v for v in form.getlist("i") if isinstance(v, str)],
        [v for v in form.getlist("a") if isinstance(v, str)],
        [v for v in form.getlist("r") if isinstance(v, str)],
    )
    return Response("OK")


@app.post("/greader/reader/api/0/mark-all-as-read")
async def greader_mark_all_as_read(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    form = await request.form()
    stream_id = str(form.get("s") or "user/-/state/com.google/reading-list")
    ts_raw = form.get("ts")
    # ts is in microseconds; convert to seconds for the service.
    timestamp = int(str(ts_raw)) // 1_000_000 if ts_raw and isinstance(ts_raw, str) else None
    threading.Thread(
        target=_run_in_user_context,
        args=(tenancy.current_user_id(), greader_service.mark_all_as_read, stream_id, timestamp),  # ty: ignore[unresolved-attribute]
        daemon=True,
    ).start()
    return Response("OK")


# ================================================================== Miniflux v1 API


def _resolve_miniflux_user(request: Request) -> str | None:
    """Resolve a Miniflux X-Auth-Token header to a tenancy user_id.

    Returns the user_id on success, None on missing/invalid token."""
    if not user_store:
        return None
    token = request.headers.get("X-Auth-Token", "").strip()
    if not token:
        # Basic auth: username:api_token (some clients send token as password)
        import base64
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, _, token = decoded.partition(":")
                token = token.strip()
            except Exception:
                token = ""
    if not token:
        return None
    return user_store.miniflux_user_for_token(token)


def _miniflux_ok(request: Request) -> str | None:
    """Return the user_id if the request carries a valid Miniflux token, else None."""
    return _resolve_miniflux_user(request)


@app.get("/v1/version")
def miniflux_version() -> Response:
    """Version endpoint — some clients call this to detect Miniflux compatibility."""
    return JSONResponse({"version": "2.2.0"})


@app.post("/v1/auth/token")
async def miniflux_auth_token(request: Request) -> Response:
    """Token login endpoint for clients that prefer JWT-style auth.

    We return the user's api_token as a plain bearer token (not a real JWT)
    since we use X-Auth-Token for all subsequent requests."""
    if not user_store:
        return JSONResponse({"error_message": "Auth not available."}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error_message": "Invalid JSON."}, status_code=400)
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    uid = user_store.verify_login(username, password) if username and password else None
    if not uid:
        return JSONResponse({"error_message": "Invalid credentials."}, status_code=401)
    with tenancy.user_context(uid):
        with get_meta_connection() as conn:
            tok = get_runtime_setting(conn, "api_token") if False else None
    # Resolve the api_token directly from the auth DB
    with user_store._connect() as conn:
        row = conn.execute(
            "SELECT api_token FROM users WHERE user_id=?", (uid,)
        ).fetchone()
    if not row or not row["api_token"]:
        return JSONResponse({"error_message": "API token not set."}, status_code=403)
    return JSONResponse({"token": row["api_token"]})


@app.get("/v1/me")
def miniflux_me(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        with user_store._connect() as conn:
            row = conn.execute(
                "SELECT username, is_admin FROM users WHERE user_id=?", (uid,)
            ).fetchone()
    if not row:
        return JSONResponse({"error_message": "User not found."}, status_code=404)
    return JSONResponse({
        "id": 1,
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "theme": "system_serif",
        "language": "en_US",
        "timezone": "UTC",
        "entry_direction": "asc",
        "entries_per_page": 100,
        "keyboard_shortcuts": True,
        "show_reading_time": True,
        "entry_swipe": True,
        "gesture_nav": "tap",
        "last_login_at": datetime.now(timezone.utc).isoformat(),
        "display_mode": "standalone",
        "default_reading_speed": 265,
        "cjk_reading_speed": 500,
        "default_home_page": "unread",
        "categories_sorting_order": "alphabetical",
        "mark_read_on_view": False,
        "media_playback_rate": 1,
    })


@app.get("/v1/categories")
def miniflux_categories(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        cats = miniflux_service.get_categories()
    return JSONResponse(cats)


@app.get("/v1/feeds")
def miniflux_feeds(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        feeds = miniflux_service.get_feeds()
    return JSONResponse(feeds)


def _miniflux_entries_response(request: Request, *, feed_id: int | None = None, category_id: int | None = None) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    p = request.query_params
    status = p.get("status") or None
    starred_str = p.get("starred")
    starred: bool | None = None
    if starred_str == "true":
        starred = True
    elif starred_str == "false":
        starred = False
    try:
        limit = max(1, min(int(p.get("limit", 100)), 200))
        after_id = int(p["after_entry_id"]) if "after_entry_id" in p else None
        before_id = int(p["before_entry_id"]) if "before_entry_id" in p else None
    except (ValueError, TypeError):
        return JSONResponse({"error_message": "Invalid parameter."}, status_code=400)
    direction = p.get("direction", "desc")
    with tenancy.user_context(uid):
        result = miniflux_service.get_entries(
            status=status,
            feed_id=feed_id,
            category_id=category_id,
            starred=starred,
            limit=limit,
            direction=direction,
            after_entry_id=after_id,
            before_entry_id=before_id,
        )
    return JSONResponse(result)


@app.get("/v1/entries")
def miniflux_entries(request: Request) -> Response:
    return _miniflux_entries_response(request)


@app.get("/v1/feeds/{feed_id}/entries")
def miniflux_feed_entries(feed_id: int, request: Request) -> Response:
    return _miniflux_entries_response(request, feed_id=feed_id)


@app.get("/v1/categories/{category_id}/entries")
def miniflux_category_entries(category_id: int, request: Request) -> Response:
    return _miniflux_entries_response(request, category_id=category_id)


@app.get("/v1/entries/{entry_id}")
def miniflux_entry(entry_id: int, request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        entry = miniflux_service.get_entry(entry_id)
    if entry is None:
        return JSONResponse({"error_message": "Entry not found."}, status_code=404)
    return JSONResponse(entry)


@app.put("/v1/entries")
async def miniflux_update_entries(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error_message": "Invalid JSON."}, status_code=400)
    entry_ids = [int(x) for x in (body.get("entry_ids") or []) if str(x).isdigit() or isinstance(x, int)]
    status = str(body.get("status", ""))
    if not entry_ids or status not in ("read", "unread"):
        return JSONResponse({"error_message": "entry_ids and status (read|unread) required."}, status_code=400)
    with tenancy.user_context(uid):
        miniflux_service.update_entries(entry_ids, status)
    return Response(status_code=204)


@app.put("/v1/entries/{entry_id}/bookmark")
def miniflux_toggle_bookmark(entry_id: int, request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        result = miniflux_service.toggle_bookmark(entry_id)
    if result is None:
        return JSONResponse({"error_message": "Entry not found."}, status_code=404)
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
