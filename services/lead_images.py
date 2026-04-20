from __future__ import annotations

import html
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


class LeadImageService:
    """Encapsulates entry lead-image extraction, caching, and persistence."""

    _IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
    _IMG_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["\']([^"\']+)["\']')
    _OG_IMAGE_RE = re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    _OG_IMAGE_RE_REVERSED = re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']',
        re.IGNORECASE,
    )
    _OG_IMAGE_WIDTH_RE = re.compile(
        r'<meta[^>]+(?:property|name)=["\']og:image:width["\'][^>]+content=["\']([0-9]+)["\']',
        re.IGNORECASE,
    )
    _OG_IMAGE_WIDTH_RE_REVERSED = re.compile(
        r'<meta[^>]+content=["\']([0-9]+)["\'][^>]+(?:property|name)=["\']og:image:width["\']',
        re.IGNORECASE,
    )
    _OG_IMAGE_HEIGHT_RE = re.compile(
        r'<meta[^>]+(?:property|name)=["\']og:image:height["\'][^>]+content=["\']([0-9]+)["\']',
        re.IGNORECASE,
    )
    _OG_IMAGE_HEIGHT_RE_REVERSED = re.compile(
        r'<meta[^>]+content=["\']([0-9]+)["\'][^>]+(?:property|name)=["\']og:image:height["\']',
        re.IGNORECASE,
    )
    _TAG_RE = re.compile(r"<[^>]+>", re.IGNORECASE)
    _HREF_IMAGE_RE = re.compile(r'href=["\']([^"\']+\.(?:jpe?g|png|webp|gif|avif)(?:\?[^"\']*)?)["\']', re.IGNORECASE)
    _LOGO_URL_PATTERNS = re.compile(
        r"(?:favicon|site[-_]logo|wordmark|site[-_]icon|app[-_]icon|social[-_]icon)",
        re.IGNORECASE,
    )
    _URL_DIMENSION_RE = re.compile(r"(?:^|[/_.-])([0-9]{1,4})x([0-9]{1,4})(?:[/_.-]|$)")
    _PLACEHOLDER_URL_PATTERNS = re.compile(
        r"(?:grey-placeholder|image-unavailable|placeholder(?:[._-]|$)|no-image(?:[._-]|$)|fallback(?:[._-]|$))",
        re.IGNORECASE,
    )

    _LEAD_IMAGE_MIN_WIDTH = 200
    _LEAD_IMAGE_MIN_HEIGHT = 100
    _NEGATIVE_RETRY_SECONDS = 24 * 60 * 60

    def __init__(
        self,
        *,
        get_meta_connection: Callable[[], sqlite3.Connection],
        get_reader: Callable[[], Any],
        user_agent: str,
        extract_video_id: Callable[[str], str | None],
        cache: dict[tuple[str, str], str | None] | None = None,
        fetched_at_cache: dict[tuple[str, str], float] | None = None,
    ) -> None:
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._user_agent = user_agent
        self._extract_video_id = extract_video_id
        self._cache = cache if cache is not None else {}
        self._fetched_at_cache = fetched_at_cache if fetched_at_cache is not None else {}

    def warm_cache_from_db(self) -> None:
        """Load stored lead-image records into in-memory caches."""
        try:
            with self._get_meta_connection() as conn:
                rows = conn.execute("SELECT feed_url, entry_id, image_url, fetched_at FROM entry_lead_images").fetchall()
            for row in rows:
                url = row["image_url"]
                key = (str(row["feed_url"]), str(row["entry_id"]))
                if url and not self._is_image_url_acceptable(str(url), None, None):
                    try:
                        with self._get_meta_connection() as conn:
                            conn.execute(
                                "DELETE FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
                                key,
                            )
                    except Exception:
                        pass
                    continue
                self._cache[key] = url
                try:
                    self._fetched_at_cache[key] = float(row["fetched_at"])
                except Exception:
                    self._fetched_at_cache[key] = 0.0
        except Exception:
            pass

    def store_entry_lead_image(self, feed_url: str, entry_id: str, image_url: str | None) -> None:
        """Persist a discovered (or absent) lead image to DB and in-memory cache."""
        fetched_at = time.time()
        self._cache[(feed_url, entry_id)] = image_url
        self._fetched_at_cache[(feed_url, entry_id)] = fetched_at
        try:
            with self._get_meta_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(feed_url, entry_id) DO UPDATE SET
                        image_url = excluded.image_url,
                        fetched_at = excluded.fetched_at
                    """,
                    (feed_url, entry_id, image_url, fetched_at),
                )
        except Exception:
            pass

    def extract_entry_thumbnail_url(self, entry: object, include_source_lookup: bool = False) -> str | None:
        entry_link = str(getattr(entry, "link", "") or "")
        feed_url = str(getattr(entry, "feed_url", "") or "")

        if isinstance(feed_url, str) and "youtube.com/feeds/videos.xml" in feed_url and entry_link:
            video_id = self._extract_video_id(entry_link)
            if video_id:
                return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        entry_id = str(getattr(entry, "id", "") or "")
        if entry_id and (feed_url, entry_id) in self._cache:
            cached = self._cache[(feed_url, entry_id)]
            if cached:
                if not self._is_image_url_acceptable(cached, None, None):
                    return None
                return cached

        html_candidates: list[str] = []
        content_html: str | None = None

        try:
            content = getattr(entry, "get_content", lambda **_: None)(prefer_summary=False)
            if content and getattr(content, "value", None) and getattr(content, "is_html", False):
                content_html = str(content.value)
                html_candidates.append(content_html)
        except Exception:
            pass

        summary = getattr(entry, "summary", None)
        if isinstance(summary, str) and summary.strip():
            html_candidates.append(summary)

        base_url = entry_link or feed_url
        for html_candidate in html_candidates:
            inline_image = self._extract_first_image_url_from_html(html_candidate, base_url)
            if inline_image:
                return inline_image
            linked_image = self._extract_linked_image_url_from_html(html_candidate, base_url)
            if linked_image:
                return linked_image

        if include_source_lookup and entry_link and self._is_short_entry_blurb(content_html, summary):
            standard_ebooks_cover = self._standard_ebooks_cover_url(entry_link)
            if standard_ebooks_cover:
                return standard_ebooks_cover

        return None

    def resolve_entry_lead_image_url(self, entry: object, content_html: str | None, summary: str | None) -> str | None:
        entry_link = str(getattr(entry, "link", "") or "")
        feed_url_str = str(getattr(entry, "feed_url", "") or "")
        entry_id_str = str(getattr(entry, "id", "") or "")
        base_url = entry_link or feed_url_str

        cached_negative = False
        if feed_url_str and entry_id_str and (feed_url_str, entry_id_str) in self._cache:
            cached = self._cache[(feed_url_str, entry_id_str)]
            if cached:
                if not self._is_image_url_acceptable(cached, None, None):
                    return None
                return cached
            cached_negative = True

        for candidate_html in (content_html, summary):
            if not isinstance(candidate_html, str) or not candidate_html.strip():
                continue
            image_url = self._extract_first_image_url_from_html(candidate_html, base_url)
            if image_url:
                return image_url

        if cached_negative or not entry_link:
            return None

        return self._fetch_source_lead_image(entry_link)

    def fetch_and_store_lead_images_for_feed(self, feed_url: str, force_retry_negative: bool = False) -> None:
        """Backfill source-page lead images for entries missing inline images."""
        try:
            with self._get_reader() as reader:
                entries = list(reader.get_entries(feed=feed_url))
        except Exception:
            return

        now = time.time()

        for entry in entries:
            feed_url_str = str(getattr(entry, "feed_url", "") or "")
            entry_id_str = str(getattr(entry, "id", "") or "")
            if not feed_url_str or not entry_id_str:
                continue

            cache_key = (feed_url_str, entry_id_str)

            if cache_key in self._cache:
                cached = self._cache[cache_key]
                if cached:
                    continue
                fetched_at = self._fetched_at_cache.get(cache_key, 0.0)
                if (not force_retry_negative) and now - fetched_at < self._NEGATIVE_RETRY_SECONDS:
                    continue

            inline = self.extract_entry_thumbnail_url(entry, include_source_lookup=False)
            if inline:
                continue

            entry_link = str(getattr(entry, "link", "") or "")
            if not entry_link:
                continue

            image_url = self._fetch_source_lead_image(entry_link)
            self.store_entry_lead_image(feed_url_str, entry_id_str, image_url)
            time.sleep(0.15)

    def _standard_ebooks_cover_url(self, entry_link: str) -> str | None:
        parsed = urlparse(entry_link)
        if parsed.scheme not in {"http", "https"}:
            return None
        if parsed.netloc.lower() != "standardebooks.org":
            return None

        path = parsed.path.rstrip("/")
        if "/ebooks/" not in path:
            return None
        return f"{parsed.scheme}://{parsed.netloc}{path}/downloads/cover.jpg"

    def _extract_first_image_url_from_html(self, html_text: str, base_url: str) -> str | None:
        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape(attr_match.group(2).strip())
                if key and value:
                    attrs[key] = value

            candidate_urls: list[str] = []
            for attr_name in ("src", "data-src", "data-lazy-src", "data-original", "data-image"):
                value = attrs.get(attr_name)
                if value:
                    candidate_urls.append(value)

            srcset = attrs.get("srcset") or attrs.get("data-srcset")
            if srcset:
                for part in srcset.split(","):
                    first = part.strip().split(" ")[0].strip()
                    if first:
                        candidate_urls.append(first)

            for image_url in candidate_urls:
                if not image_url or image_url.startswith("data:"):
                    continue
                resolved = urljoin(base_url, image_url)
                if self._is_image_url_acceptable(resolved, None, None):
                    return resolved
        return None

    def _extract_og_image_dimensions(self, html_text: str) -> tuple[int | None, int | None]:
        width: int | None = None
        height: int | None = None
        for pattern in (self._OG_IMAGE_WIDTH_RE, self._OG_IMAGE_WIDTH_RE_REVERSED):
            m = pattern.search(html_text)
            if m:
                try:
                    width = int(m.group(1))
                except ValueError:
                    pass
                break
        for pattern in (self._OG_IMAGE_HEIGHT_RE, self._OG_IMAGE_HEIGHT_RE_REVERSED):
            m = pattern.search(html_text)
            if m:
                try:
                    height = int(m.group(1))
                except ValueError:
                    pass
                break
        return width, height

    def _is_image_url_acceptable(self, image_url: str, width: int | None, height: int | None) -> bool:
        if self._LOGO_URL_PATTERNS.search(image_url):
            return False
        if self._PLACEHOLDER_URL_PATTERNS.search(image_url):
            return False
        if width is None or height is None:
            for m in self._URL_DIMENSION_RE.finditer(image_url.split("?")[0]):
                try:
                    w, h = int(m.group(1)), int(m.group(2))
                    if w < self._LEAD_IMAGE_MIN_WIDTH or h < self._LEAD_IMAGE_MIN_HEIGHT:
                        return False
                except ValueError:
                    pass
        if width is not None and width < self._LEAD_IMAGE_MIN_WIDTH:
            return False
        if height is not None and height < self._LEAD_IMAGE_MIN_HEIGHT:
            return False
        return True

    def _extract_meta_image_url_from_html(self, html_text: str, base_url: str) -> str | None:
        og_width, og_height = self._extract_og_image_dimensions(html_text)
        for pattern in (self._OG_IMAGE_RE, self._OG_IMAGE_RE_REVERSED):
            match = pattern.search(html_text)
            if not match:
                continue
            image_url = html.unescape(match.group(1).strip())
            if not image_url or image_url.startswith("data:"):
                continue
            resolved = urljoin(base_url, image_url)
            if not self._is_image_url_acceptable(resolved, og_width, og_height):
                continue
            return resolved
        return None

    def _extract_linked_image_url_from_html(self, html_text: str, base_url: str) -> str | None:
        matches = self._HREF_IMAGE_RE.findall(html_text)
        if not matches:
            return None

        prioritized = sorted(
            (html.unescape(match.strip()) for match in matches if match and not match.startswith("data:")),
            key=lambda url: 0 if ("thumbnail" in url.lower() or "cover" in url.lower()) else 1,
        )
        for raw_url in prioritized:
            resolved = urljoin(base_url, raw_url)
            if self._is_image_url_acceptable(resolved, None, None):
                return resolved
        return None

    def _is_short_entry_blurb(self, content_html: str | None, summary: str | None) -> bool:
        source_html = ""
        if isinstance(content_html, str) and content_html.strip():
            source_html = content_html
        elif isinstance(summary, str) and summary.strip():
            source_html = summary
        if not source_html:
            return True

        text_content = self._TAG_RE.sub(" ", source_html)
        text_content = html.unescape(re.sub(r"\s+", " ", text_content)).strip()
        return len(text_content) <= 420

    def _fetch_source_lead_image(self, entry_link: str) -> str | None:
        parsed = urlparse(entry_link)
        if parsed.scheme not in {"http", "https"}:
            return None

        try:
            with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": self._user_agent}) as client:
                response = client.get(entry_link)
            response.raise_for_status()
        except Exception:
            return None

        source_html = response.text
        final_url = str(response.url)
        meta_image = self._extract_meta_image_url_from_html(source_html, final_url)
        if meta_image:
            return meta_image
        inline_image = self._extract_first_image_url_from_html(source_html, final_url)
        if inline_image:
            return inline_image
        return self._extract_linked_image_url_from_html(source_html, final_url)
