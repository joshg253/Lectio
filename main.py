from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Sequence, cast
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from readability import Document

from services.feed_refresh import FeedRefreshService
from services.lead_images import LeadImageService
from services.reader_api import ReaderApi
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


_configure_reader_logging()


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
META_DB_PATH = BASE_DIR / "lectio_meta.sqlite3"
READER_DB_PATH = BASE_DIR / "lectio_reader.sqlite"
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
STATIC_ASSET_VERSION = os.getenv("LECTIO_ASSET_VERSION", "20260421r")
REFRESH_DEBUG_ENABLED = os.getenv("LECTIO_REFRESH_DEBUG", "1") == "1"

_configured_refresh_minutes = int(os.getenv("LECTIO_AUTO_REFRESH_MINUTES", str(DEFAULT_AUTO_REFRESH_MINUTES)))
AUTO_REFRESH_MINUTES = 0 if _configured_refresh_minutes <= 0 else max(_configured_refresh_minutes, MIN_AUTO_REFRESH_MINUTES)
manual_refresh_lock = threading.Lock()
last_manual_refresh_started_at = 0.0
updating_feeds_lock = threading.Lock()
updating_feeds: set[str] = set()
feed_tag_suggestion_cache_lock = threading.Lock()
feed_tag_suggestion_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}


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
    ensure_meta_schema()
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

    stop_event = threading.Event()
    thread = threading.Thread(
        target=scheduled_refresh_loop,
        args=(stop_event,),
        daemon=True,
    )
    app.state.refresh_stop_event = stop_event
    app.state.refresh_thread = thread
    thread.start()

    # Backfill durations for any existing YouTube entries not yet stored.
    def _backfill() -> None:
        with get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT feed_url FROM folder_feeds WHERE feed_url LIKE '%youtube.com/feeds/videos.xml%'"
            ).fetchall()
        for row in rows:
            youtube_duration_service.fetch_and_store_durations_for_feed(str(row["feed_url"]))

    threading.Thread(target=_backfill, daemon=True).start()

    # Backfill lead images for all feeds whose entries haven't been checked yet.
    def _backfill_lead_images() -> None:
        with get_meta_connection() as conn:
            rows = conn.execute("SELECT DISTINCT feed_url FROM folder_feeds").fetchall()
        for row in rows:
            lead_image_service.fetch_and_store_lead_images_for_feed(str(row["feed_url"]), force_retry_negative=True)

    threading.Thread(target=_backfill_lead_images, daemon=True).start()

    try:
        yield
    finally:
        stop_event = getattr(app.state, "refresh_stop_event", None)
        thread = getattr(app.state, "refresh_thread", None)
        if stop_event and thread:
            stop_event.set()
            thread.join(timeout=2)


app = FastAPI(title="Lectio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["urlencode"] = lambda value: quote_plus(str(value))


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


def get_meta_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(META_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


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


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    return str(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
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


def get_unread_counts_by_feed() -> dict[str, int]:
    counts: dict[str, int] = {}
    with get_reader() as reader:
        for entry in reader.get_entries():
            if entry.read:
                continue
            counts[entry.feed_url] = counts.get(entry.feed_url, 0) + 1
    return counts


def get_reader():
    return reader_api.client()


reader_api = ReaderApi(READER_DB_PATH)


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
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
                response = client.get(feed_url)
            response.raise_for_status()
        except Exception:
            return []

        parsed = feedparser.parse(response.content)
        candidate_entries = []
        for raw_entry in list(parsed.entries)[:120]:
            tags = extract_feed_entry_tags(raw_entry)
            if not tags:
                continue
            candidate_entries.append(
                {
                    "id": str(getattr(raw_entry, "id", "") or ""),
                    "link": str(getattr(raw_entry, "link", "") or ""),
                    "title": str(getattr(raw_entry, "title", "") or ""),
                    "tags": tags,
                }
            )
        with feed_tag_suggestion_cache_lock:
            feed_tag_suggestion_cache[feed_url] = (now, candidate_entries)

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


def get_tag_counts_for_feeds(feed_urls: set[str]) -> list[dict[str, int | str]]:
    if not feed_urls:
        return []

    counts: dict[str, int] = {}
    with get_reader() as reader:
        for feed_url in feed_urls:
            for entry in reader.get_entries(feed=feed_url, limit=None):
                for tag in get_manual_tags_for_resource(reader, entry.resource_id):
                    counts[tag] = counts.get(tag, 0) + 1

    return [{"name": tag, "count": counts[tag]} for tag in sorted(counts)]


def get_favicon_url(feed_url: str, site_url: str | None = None) -> str | None:
    url_for_host = site_url or feed_url
    host = urlparse(url_for_host).hostname
    if not host:
        host = urlparse(feed_url).hostname
    if not host:
        return None
    return f"https://www.google.com/s2/favicons?domain={quote_plus(host)}&sz=32"


def get_feed_title_map() -> dict[str, str]:
    titles: dict[str, str] = {}
    with get_reader() as reader:
        for feed in reader.get_feeds():
            titles[feed.url] = feed.resolved_title or feed.title or feed.url
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
    if dt.tzinfo is not None:
        dt = dt.astimezone()
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
        sanitized = f"<!DOCTYPE html><html><head><base href=\"{escaped_source}\"><meta charset='utf-8'>{proxy_style}</head><body>{sanitized}</body></html>"

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
        feed_site_map: dict[str, str | None] = {}
        for feed_url in feed_urls:
            try:
                feed_obj = reader.get_feed(feed_url, None)
                feed_site_map[feed_url] = getattr(feed_obj, "link", None) if feed_obj else None
            except Exception:
                feed_site_map[feed_url] = None

        # Fetch entries from only the specified feeds to avoid limit issues
        all_feed_entries = []
        fetch_limit = max(1, int(limit))
        for feed_url in feed_urls:
            # Search should operate over the whole feed slice to keep ordering
            # and result inclusion consistent with the selected sort controls.
            # For ascending sort (oldest-first), avoid per-feed truncation so
            # global oldest ordering remains stable after read/unread actions.
            if search_terms or normalized_sort_dir == "asc":
                search_fetch_limit = None
            else:
                search_fetch_limit = fetch_limit
            all_feed_entries.extend(reader.get_entries(feed=feed_url, read=reader_read_filter, limit=search_fetch_limit))

        for entry in all_feed_entries:
            is_read = bool(entry.read)
            is_saved = (entry.feed_url, entry.id) in saved_entries_set
            manual_tags: list[str] = []
            if normalized_selected_tag:
                manual_tags = get_manual_tags_for_resource(reader, entry.resource_id)
                if normalized_selected_tag not in manual_tags:
                    continue
            # Star-only is a hard override over read/history filters.
            if normalized_star_only and not is_saved:
                continue
            if not normalized_star_only:
                if normalized_read_filter == "unread" and is_read:
                    continue
                if normalized_read_filter == "history" and not is_read:
                    continue
            published_dt = entry.published or entry.updated
            read_dt = read_state_map.get((entry.feed_url, entry.id))
            if read_dt is None:
                read_dt = getattr(entry, "read_modified", None)
            # For YouTube channel feeds, attempt to fetch video duration and
            # prefix the post title with the duration like Inoreader does.
            duration_seconds = None
            duration_display = None
            title_text = entry.title
            try:
                if isinstance(entry.feed_url, str) and "youtube.com/feeds/videos.xml" in entry.feed_url and entry.link:
                    vid = youtube_duration_service.extract_video_id(entry.link)
                    if vid:
                        duration_seconds, duration_display = youtube_duration_service.get_cached_duration(vid)
                        if duration_display:
                            title_text = f"[{duration_display}] {title_text}"
            except Exception:
                duration_seconds = None
                duration_display = None

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

            entries.append(
                {
                    "feed_url": entry.feed_url,
                    "id": entry.id,
                    "title": title_text,
                    "link": entry.link,
                    "thumbnail_url": lead_image_service.extract_entry_thumbnail_url(entry, include_source_lookup=True),
                    "feed_title": entry.feed_resolved_title or entry.feed_url,
                    "feed_icon_url": get_favicon_url(entry.feed_url, feed_site_map.get(entry.feed_url)),
                    "manual_tags": manual_tags,
                    "read": is_read,
                    "saved": is_saved,
                    "post_sort_value": datetime_sort_value(published_dt),
                    "received_sort_value": datetime_sort_value(entry.added),
                    "history_sort_value": datetime_sort_value(read_dt),
                    "post_timestamp": published_dt.isoformat() if published_dt else None,
                    "received_timestamp": entry.added.isoformat() if entry.added else None,
                    "read_timestamp": read_dt.isoformat() if read_dt else None,
                    "post_display": format_datetime_for_ui(published_dt),
                    "received_display": format_datetime_for_ui(entry.added),
                    "read_display": format_datetime_for_ui(read_dt),
                    "duration_seconds": duration_seconds,
                    "duration_display": duration_display,
                }
            )

    if normalized_read_filter == "history" and not normalized_star_only:
        sort_key = "history_sort_value"
        sort_desc = True
    else:
        sort_key = "post_sort_value" if normalized_sort_by == "post" else "received_sort_value"
        sort_desc = normalized_sort_dir == "desc"
    entries.sort(
        key=lambda item: item[sort_key],
        reverse=sort_desc,
    )

    # Deduplicate by normalized link URL (strips fragment; keeps first occurrence
    # after sort, so the newest/most-relevant version wins). Entries without a
    # link are never considered duplicates of each other.
    seen_links: set[str] = set()
    deduped: list[dict] = []
    for entry in entries:
        link = entry.get("link")
        if link:
            normalized_link = link.split("#")[0].rstrip("/")
            if normalized_link in seen_links:
                continue
            seen_links.add(normalized_link)
        deduped.append(entry)
    entries = deduped

    entries = entries[:limit]

    for entry in entries:
        entry.pop("post_sort_value", None)
        entry.pop("received_sort_value", None)
        entry.pop("history_sort_value", None)

    return entries


def filter_feed_urls(feed_urls: set[str], list_feed_url: str | None) -> set[str]:
    if not list_feed_url:
        return feed_urls
    if list_feed_url in feed_urls:
        return {list_feed_url}
    return set()


def get_entry_detail(feed_url: str, entry_id: str) -> dict | None:
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            return None

        published_dt = entry.published or entry.updated
        author_name = (getattr(entry, "author", None) or "").strip() or None

        content = entry.get_content(prefer_summary=False)
        content_html = None
        if content and content.value and content.is_html:
            content_html = content.value

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

        lead_image_url = lead_image_service.resolve_entry_lead_image_url(entry, content_html, entry.summary)
        lead_image_service.store_entry_lead_image(str(entry.feed_url), str(entry.id), lead_image_url)

        return {
            "feed_url": entry.feed_url,
            "id": entry.id,
            "title": entry.title,
            "link": entry.link,
            "summary": entry.summary,
            "content_html": content_html,
            "lead_image_url": lead_image_url,
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
        },
    )


def mark_feeds_as_read(feed_urls: set[str]) -> int:
    if not feed_urls:
        return 0

    marked_count = 0
    with get_reader() as reader:
        for entry in reader.get_entries():
            if entry.feed_url not in feed_urls or entry.read:
                continue
            reader.mark_entry_as_read((entry.feed_url, entry.id))
            upsert_entry_read_state(entry.feed_url, entry.id)
            marked_count += 1
    return marked_count


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

    with get_reader() as reader:

        def walk(outline: ET.Element, target_folder_id: int, may_create_folder: bool) -> None:
            nonlocal imported
            feed_url = outline.attrib.get("xmlUrl")
            if feed_url:
                feed_url = feed_url.strip()
                if feed_url:
                    reader.add_feed(feed_url, exist_ok=True)
                    conn.execute(
                        "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                        (target_folder_id, feed_url),
                    )
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
def entry_readability(url: str):
    return build_readability_response(url)


@app.get("/entries/source")
def entry_source(url: str):
    return build_source_proxy_response(url)


@app.get("/entries/frame-check")
def entry_frame_check(url: str):
    return JSONResponse(probe_frameability(url))


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

    with get_meta_connection() as conn:
        preferred_sort_by = normalize_sort_by(get_setting(conn, SORT_BY_SETTING_KEY))
        preferred_sort_dir = normalize_sort_dir(get_setting(conn, SORT_DIR_SETTING_KEY))
        problematic_feeds_last_viewed_at = parse_epoch_setting(get_setting(conn, PROBLEMATIC_FEEDS_LAST_VIEWED_AT_SETTING_KEY))
        selected_sort_by = normalize_sort_by(sort_by or preferred_sort_by)
        selected_sort_dir = normalize_sort_dir(sort_dir or preferred_sort_dir)
        set_setting(conn, SORT_BY_SETTING_KEY, selected_sort_by)
        set_setting(conn, SORT_DIR_SETTING_KEY, selected_sort_dir)

        root_id = get_root_folder_id(conn)
        selected_folder_id = folder_id or root_id

        raw_folder_rows = get_folder_rows(conn)
        direct_feed_urls_by_folder = get_direct_feed_urls_by_folder(conn)
        unread_counts_by_feed = get_unread_counts_by_feed()
        unread_counts_by_folder = get_unread_counts_by_folder(
            raw_folder_rows,
            unread_counts_by_feed,
            direct_feed_urls_by_folder,
        )
        folder_rows = []
        for row in raw_folder_rows:
            folder_dict = dict(row)
            folder_dict["unread_count"] = unread_counts_by_folder.get(int(row["id"]), 0)
            folder_rows.append(folder_dict)
        folder_options = get_folder_options(conn)
        global_note = get_setting(conn, GLOBAL_NOTE_SETTING_KEY) or ""
        problematic_feeds = feed_refresh_service.get_problematic_feeds(conn, limit=50)
        feed_urls = get_folder_feed_urls(conn, selected_folder_id)
        all_feed_urls = get_all_feed_urls(conn)

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

    tag_rows = get_tag_counts_for_feeds(filtered_feed_urls)

    feed_title_map = get_feed_title_map()
    problematic_unseen_count = 0
    for problematic_feed in problematic_feeds:
        pf_url = cast(str, problematic_feed["feed_url"])
        problematic_feed["feed_title"] = feed_title_map.get(pf_url, pf_url)
        pf_last_failure_at = problematic_feed.get("last_failure_at")
        if not isinstance(pf_last_failure_at, (int, float)):
            continue
        if problematic_feeds_last_viewed_at is None or float(pf_last_failure_at) > problematic_feeds_last_viewed_at:
            problematic_unseen_count += 1
    feeds_by_folder: dict[int, list[FeedInFolder]] = {}
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
            "posts": posts,
            "selected_entry": selected_entry,
            "message": message,
            "auto_refresh_enabled": getattr(app.state, "auto_refresh_minutes", 0) > 0,
            "auto_refresh_minutes": getattr(app.state, "auto_refresh_minutes", 0),
            "auto_refresh_option_minutes": AUTO_REFRESH_OPTION_MINUTES,
            "static_asset_version": STATIC_ASSET_VERSION,
        },
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
    return RedirectResponse(url=f"/?folder_id={target_id}", status_code=303)


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
                reader.delete_feed(feed_url, missing_ok=True)
    except Exception as exc:
        message = f"Unsubscribe failed: {exc}"

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
def mark_folder_as_read(folder_id: int = Form(...), tag: str | None = Form(default=None)):
    normalized_tag = normalize_tag_value(tag)
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)

    marked_count = mark_feeds_as_read(feed_urls)
    message = "All posts already read." if marked_count == 0 else f"Marked {marked_count} posts as read."
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{tag_query}&message={quote_plus(message)}",
        status_code=303,
    )


@app.post("/feeds/mark-read")
def mark_feed_as_read(
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    marked_count = mark_feeds_as_read({feed_url})
    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    message = "All posts already read." if marked_count == 0 else f"Marked {marked_count} posts as read."
    return RedirectResponse(
        url=(f"/?folder_id={folder_id}{list_feed_query}{tag_query}&message={quote_plus(message)}"),
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
            with get_reader() as reader:
                for post in target_posts:
                    if post["read"]:
                        continue
                    try:
                        reader.mark_entry_as_read((post["feed_url"], post["id"]))
                    except Exception:
                        continue
                    upsert_entry_read_state(post["feed_url"], post["id"])
                    marked_count += 1

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


@app.post("/opml/import")
async def opml_import(opml_file: Annotated[UploadFile, File(...)]):
    data = await opml_file.read()
    with get_meta_connection() as conn:
        imported = import_opml(conn, data)
        root_id = get_root_folder_id(conn)

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
