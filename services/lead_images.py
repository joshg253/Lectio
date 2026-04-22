from __future__ import annotations

import html
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from services.lead_image_plugins import DEFAULT_LEAD_IMAGE_PLUGINS, LeadImagePlugin


class LeadImageService:
    """Encapsulates entry lead-image extraction, caching, and persistence."""

    _IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
    _LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
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
    _TRACKER_URL_PATTERNS = re.compile(
        r"(?:scorecardresearch|doubleclick|googletagmanager|google-analytics|adservice|adsystem|pixel|beacon|analytics)",
        re.IGNORECASE,
    )
    _AVATAR_HINT_PATTERNS = re.compile(
        r"(?:avatar|author(?:-image)?|byline|profile|headshot|user(?:-image|pic)?|gravatar)",
        re.IGNORECASE,
    )
    _IMAGE_PATH_SUFFIX_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)$", re.IGNORECASE)

    _LEAD_IMAGE_MIN_WIDTH = 200
    _LEAD_IMAGE_MIN_HEIGHT = 100
    _NEGATIVE_RETRY_SECONDS = 24 * 60 * 60
    _POSITIVE_REVALIDATE_SECONDS = 12 * 60 * 60
    _POSITIVE_REVALIDATE_PER_FEED_LIMIT = 12

    def __init__(
        self,
        *,
        get_meta_connection: Callable[[], sqlite3.Connection],
        get_reader: Callable[[], Any],
        user_agent: str,
        extract_video_id: Callable[[str], str | None],
        cache: dict[tuple[str, str], str | None] | None = None,
        fetched_at_cache: dict[tuple[str, str], float] | None = None,
        plugins: tuple[LeadImagePlugin, ...] | None = None,
    ) -> None:
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._user_agent = user_agent
        self._extract_video_id = extract_video_id
        self._cache = cache if cache is not None else {}
        self._fetched_at_cache = fetched_at_cache if fetched_at_cache is not None else {}
        self._plugins = plugins if plugins is not None else DEFAULT_LEAD_IMAGE_PLUGINS

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

    def _is_cache_key_stale(self, cache_key: tuple[str, str], *, max_age_seconds: int) -> bool:
        fetched_at = self._fetched_at_cache.get(cache_key, 0.0)
        return time.time() - fetched_at >= max_age_seconds

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
                if self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached):
                    cached = None
                elif not self._is_image_url_acceptable(cached, None, None):
                    cached = None
                else:
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
            plugin_fallback = self._plugin_fallback_lead_image_url(entry_link=entry_link, content_html=content_html, summary=summary)
            if plugin_fallback and self._is_image_url_acceptable(plugin_fallback, None, None):
                return plugin_fallback

        return None

    def resolve_entry_lead_image_url(self, entry: object, content_html: str | None, summary: str | None) -> str | None:
        entry_link = str(getattr(entry, "link", "") or "")
        feed_url_str = str(getattr(entry, "feed_url", "") or "")
        entry_id_str = str(getattr(entry, "id", "") or "")
        base_url = entry_link or feed_url_str

        cached_negative = False
        if feed_url_str and entry_id_str and (feed_url_str, entry_id_str) in self._cache:
            cache_key = (feed_url_str, entry_id_str)
            cached = self._cache[(feed_url_str, entry_id_str)]
            if cached:
                if self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached):
                    cached = None
                elif not self._is_image_url_acceptable(cached, None, None):
                    cached = None
                else:
                    should_revalidate = (
                        bool(entry_link)
                        and self._is_short_entry_blurb(content_html, summary)
                        and self._is_cache_key_stale(cache_key, max_age_seconds=self._POSITIVE_REVALIDATE_SECONDS)
                    )
                    if should_revalidate:
                        source_image = self._fetch_source_lead_image(entry_link)
                        if source_image and source_image != cached:
                            return source_image
                    inline_candidate = None
                    if isinstance(content_html, str) and content_html.strip():
                        inline_candidate = self._extract_first_image_url_from_html(content_html, base_url)
                    if entry_link and inline_candidate and inline_candidate == cached:
                        source_image = self._fetch_source_lead_image(entry_link)
                        if source_image and source_image != cached:
                            return source_image
                    return cached
            cached_negative = True

        # Prefer source-page metadata/image selection when available.
        # This usually picks a truer hero image than the first inline body image.
        if not cached_negative and entry_link:
            plugin_fallback = self._plugin_fallback_lead_image_url(entry_link=entry_link, content_html=content_html, summary=summary)
            if plugin_fallback and self._is_image_url_acceptable(plugin_fallback, None, None):
                return plugin_fallback

            source_image = self._fetch_source_lead_image(entry_link)
            if source_image:
                return source_image

        for candidate_html in (content_html, summary):
            if not isinstance(candidate_html, str) or not candidate_html.strip():
                continue
            image_url = self._extract_first_image_url_from_html(candidate_html, base_url)
            if image_url:
                return image_url

        if entry_link:
            plugin_fallback = self._plugin_fallback_lead_image_url(entry_link=entry_link, content_html=content_html, summary=summary)
            if plugin_fallback and self._is_image_url_acceptable(plugin_fallback, None, None):
                return plugin_fallback

        if cached_negative or not entry_link:
            return None
        return None

    def fetch_and_store_lead_images_for_feed(self, feed_url: str, force_retry_negative: bool = False) -> None:
        """Backfill source-page lead images for entries missing inline images."""
        try:
            with self._get_reader() as reader:
                entries = list(reader.get_entries(feed=feed_url))
        except Exception:
            return

        saved_entry_ids: set[str] = set()
        try:
            with self._get_meta_connection() as conn:
                rows = conn.execute(
                    "SELECT entry_id FROM saved_entries WHERE feed_url = ?",
                    (feed_url,),
                ).fetchall()
            saved_entry_ids = {str(row["entry_id"]) for row in rows}
        except Exception:
            saved_entry_ids = set()

        now = time.time()
        positive_revalidated = 0

        for entry in entries:
            feed_url_str = str(getattr(entry, "feed_url", "") or "")
            entry_id_str = str(getattr(entry, "id", "") or "")
            if not feed_url_str or not entry_id_str:
                continue

            is_unread = not bool(getattr(entry, "read", False))
            is_saved = entry_id_str in saved_entry_ids
            is_manual_tagged = False
            if not is_unread and not is_saved:
                is_manual_tagged = self._entry_has_manual_tags(reader, entry)

            # Restrict background thumbnail refresh to entries users still care
            # about in active views: unread, saved, or manually tagged.
            if not (is_unread or is_saved or is_manual_tagged):
                continue

            cache_key = (feed_url_str, entry_id_str)

            if cache_key in self._cache:
                cached = self._cache[cache_key]
                if cached:
                    if self._should_bypass_cached_url(entry_link=str(getattr(entry, "link", "") or ""), cached_url=cached):
                        pass
                    elif not self._is_image_url_acceptable(cached, None, None):
                        pass
                    elif positive_revalidated >= self._POSITIVE_REVALIDATE_PER_FEED_LIMIT:
                        continue
                    elif (not force_retry_negative) and now - self._fetched_at_cache.get(
                        cache_key, 0.0
                    ) < self._POSITIVE_REVALIDATE_SECONDS:
                        continue
                    else:
                        entry_link = str(getattr(entry, "link", "") or "")
                        if not entry_link:
                            continue
                        source_image = self._fetch_source_lead_image(entry_link)
                        if source_image:
                            self.store_entry_lead_image(feed_url_str, entry_id_str, source_image)
                        else:
                            # Keep existing image but advance fetch time to avoid repeated stale retries.
                            self.store_entry_lead_image(feed_url_str, entry_id_str, cached)
                        positive_revalidated += 1
                        time.sleep(0.15)
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

    def _extract_tag_key(self, tag_record: object) -> str | None:
        if isinstance(tag_record, tuple):
            if len(tag_record) == 0:
                return None
            return str(tag_record[0])
        if isinstance(tag_record, str):
            return tag_record
        key = getattr(tag_record, "key", None)
        if key is None:
            return None
        return str(key)

    def _entry_has_manual_tags(self, reader: Any, entry: object) -> bool:
        manual_prefix = "tag:lectio:"
        resource_id = getattr(entry, "resource_id", None)
        if not resource_id:
            return False
        try:
            tags = reader.get_tags(resource_id)
        except Exception:
            return False
        for tag_record in tags:
            key = self._extract_tag_key(tag_record)
            if key and key.startswith(manual_prefix):
                return True
        return False

    def _extract_first_image_url_from_html(self, html_text: str, base_url: str, source_url: str | None = None) -> str | None:
        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape(attr_match.group(2).strip())
                if key and value:
                    attrs[key] = value

            for image_url in self._collect_img_candidate_urls(attrs, source_url=source_url):
                if not image_url or image_url.startswith("data:"):
                    continue
                resolved = urljoin(base_url, image_url)
                if source_url and not self._is_source_image_tag_acceptable(attrs, resolved):
                    continue
                if self._is_image_url_acceptable(resolved, None, None):
                    return resolved
        return None

    def _parse_srcset_urls_descending(self, srcset: str) -> list[str]:
        ranked: list[tuple[float, str]] = []
        for part in srcset.split(","):
            token = part.strip()
            if not token:
                continue
            pieces = token.split()
            if not pieces:
                continue
            url = pieces[0].strip()
            if not url:
                continue
            score = 0.0
            if len(pieces) > 1:
                descriptor = pieces[1].strip().lower()
                if descriptor.endswith("w"):
                    try:
                        score = float(descriptor[:-1])
                    except ValueError:
                        score = 0.0
                elif descriptor.endswith("x"):
                    try:
                        score = float(descriptor[:-1]) * 1000.0
                    except ValueError:
                        score = 0.0
            ranked.append((score, url))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in ranked]

    def _collect_img_candidate_urls(self, attrs: dict[str, str], source_url: str | None = None) -> list[str]:
        candidates: list[str] = []
        plugin_candidate_attrs = self._plugin_extra_candidate_attrs(source_url)

        for attr_name in plugin_candidate_attrs:
            value = attrs.get(attr_name)
            if value:
                candidates.append(value)

        for attr_name in (
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-image",
        ):
            value = attrs.get(attr_name)
            if value:
                candidates.append(value)

        srcset = attrs.get("srcset") or attrs.get("data-srcset")
        if srcset:
            candidates.extend(self._parse_srcset_urls_descending(srcset))

        deduped: list[str] = []
        seen: set[str] = set()
        for url in candidates:
            normalized = url.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _parse_positive_int_attr(self, attrs: dict[str, str], key: str) -> int | None:
        raw = (attrs.get(key) or "").strip()
        if not raw:
            return None
        m = re.match(r"^([0-9]{1,4})", raw)
        if not m:
            return None
        try:
            value = int(m.group(1))
        except ValueError:
            return None
        return value if value > 0 else None

    def _is_source_image_tag_acceptable(self, attrs: dict[str, str], resolved_url: str) -> bool:
        combined_hint_text = " ".join(
            [
                attrs.get("class", ""),
                attrs.get("id", ""),
                attrs.get("alt", ""),
                attrs.get("title", ""),
                attrs.get("aria-label", ""),
                attrs.get("data-testid", ""),
                resolved_url,
            ]
        )
        if self._AVATAR_HINT_PATTERNS.search(combined_hint_text):
            return False

        width_attr = self._parse_positive_int_attr(attrs, "width")
        height_attr = self._parse_positive_int_attr(attrs, "height")
        if width_attr is not None and width_attr < self._LEAD_IMAGE_MIN_WIDTH:
            return False
        if height_attr is not None and height_attr < self._LEAD_IMAGE_MIN_HEIGHT:
            return False

        return True

    def _score_source_image_tag(self, attrs: dict[str, str], resolved_url: str, source_url: str) -> int:
        score = 0
        class_attr = (attrs.get("class") or "").lower()
        alt_attr = (attrs.get("alt") or "").strip()

        if "hero-image" in class_attr:
            score += 120
        if "hero" in class_attr:
            score += 40
        if any(token in class_attr for token in ("featured", "lead", "article-image", "main-image", "entry-image")):
            score += 30
        if (attrs.get("fetchpriority") or "").strip().lower() == "high":
            score += 40
        if (attrs.get("data-component-name") or "").strip().lower() == "image":
            score += 20
        if attrs.get("srcset") or attrs.get("data-srcset"):
            score += 10

        if len(alt_attr) >= 40:
            score += 10
        elif len(alt_attr) >= 16:
            score += 5

        score += self._plugin_source_score_adjustment(source_url=source_url, attrs=attrs, resolved_url=resolved_url)

        return score

    def _extract_preferred_source_image_url(self, html_text: str, base_url: str, source_url: str) -> str | None:
        best_url: str | None = None
        best_score = -1

        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape(attr_match.group(2).strip())
                if key and value:
                    attrs[key] = value

            for image_url in self._collect_img_candidate_urls(attrs, source_url=source_url):
                if not image_url or image_url.startswith("data:"):
                    continue
                resolved = urljoin(base_url, image_url)
                if not self._is_source_image_tag_acceptable(attrs, resolved):
                    continue
                if not self._is_image_url_acceptable(resolved, None, None):
                    continue

                score = self._score_source_image_tag(attrs, resolved, source_url)
                if score > best_score:
                    best_score = score
                    best_url = resolved

        if best_url and best_score >= 30:
            return best_url
        return None

    def _extract_preloaded_image_url(self, html_text: str, base_url: str) -> str | None:
        for tag_match in self._LINK_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape(attr_match.group(2).strip())
                if key and value:
                    attrs[key] = value

            rel_attr = (attrs.get("rel") or "").lower()
            as_attr = (attrs.get("as") or "").lower()
            if "preload" not in rel_attr or as_attr != "image":
                continue

            candidates: list[str] = []
            href = attrs.get("href")
            if href:
                candidates.append(href)

            image_srcset = attrs.get("imagesrcset")
            if image_srcset:
                candidates.extend(self._parse_srcset_urls_descending(image_srcset))

            for candidate in candidates:
                if not candidate or candidate.startswith("data:"):
                    continue
                resolved = urljoin(base_url, candidate)
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

    def _should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        for plugin in self._plugins:
            try:
                if plugin.should_bypass_cached_url(entry_link=entry_link, cached_url=cached_url):
                    return True
            except Exception:
                continue
        return False

    def _plugin_extra_candidate_attrs(self, source_url: str | None) -> tuple[str, ...]:
        if not source_url:
            return ()

        attrs: list[str] = []
        seen: set[str] = set()
        for plugin in self._plugins:
            try:
                extra = plugin.extra_candidate_attrs(source_url=source_url)
            except Exception:
                continue
            for attr_name in extra:
                normalized = attr_name.strip().lower()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                attrs.append(normalized)
        return tuple(attrs)

    def _plugin_source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        total = 0
        for plugin in self._plugins:
            try:
                total += int(plugin.source_score_adjustment(source_url=source_url, attrs=attrs, resolved_url=resolved_url))
            except Exception:
                continue
        return total

    def _plugin_fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        for plugin in self._plugins:
            try:
                candidate = plugin.fallback_lead_image_url(entry_link=entry_link, content_html=content_html, summary=summary)
            except Exception:
                continue
            if candidate:
                return candidate
        return None

    def _is_image_url_acceptable(self, image_url: str, width: int | None, height: int | None) -> bool:
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if self._TRACKER_URL_PATTERNS.search(parsed.netloc) or self._TRACKER_URL_PATTERNS.search(parsed.path):
            return False
        if self._AVATAR_HINT_PATTERNS.search(image_url):
            return False

        if self._LOGO_URL_PATTERNS.search(image_url):
            return False
        if self._PLACEHOLDER_URL_PATTERNS.search(image_url):
            return False

        path = parsed.path.lower()
        query = parsed.query.lower()
        looks_like_image_url = bool(self._IMAGE_PATH_SUFFIX_RE.search(path))
        has_image_hint_in_query = any(marker in query for marker in ("format=", "fm=", "image=", "img=", "ext="))
        if not looks_like_image_url and not has_image_hint_in_query and width is None and height is None:
            return False

        if width is None or height is None:
            query_w: int | None = None
            query_h: int | None = None
            for m in re.finditer(r"(?:^|&)(?:w|width)=([0-9]{1,4})(?:&|$)", query):
                try:
                    query_w = int(m.group(1))
                except ValueError:
                    continue
                break
            for m in re.finditer(r"(?:^|&)(?:h|height)=([0-9]{1,4})(?:&|$)", query):
                try:
                    query_h = int(m.group(1))
                except ValueError:
                    continue
                break
            if query_w is not None and query_w < self._LEAD_IMAGE_MIN_WIDTH:
                return False
            if query_h is not None and query_h < self._LEAD_IMAGE_MIN_HEIGHT:
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
        preload_image = self._extract_preloaded_image_url(source_html, final_url)
        if preload_image:
            return preload_image
        preferred_image = self._extract_preferred_source_image_url(source_html, final_url, entry_link)
        if preferred_image:
            return preferred_image
        inline_image = self._extract_first_image_url_from_html(source_html, final_url, source_url=entry_link)
        if inline_image:
            return inline_image
        meta_image = self._extract_meta_image_url_from_html(source_html, final_url)
        if meta_image:
            return meta_image
        return self._extract_linked_image_url_from_html(source_html, final_url)
