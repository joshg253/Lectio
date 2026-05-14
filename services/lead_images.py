from __future__ import annotations

import html
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from services.lead_image_plugins import DEFAULT_LEAD_IMAGE_PLUGINS, LeadImagePlugin
from services.url_guard import is_safe_outbound_url


class LeadImageService:
    """Encapsulates entry lead-image extraction, caching, and persistence."""

    _IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
    _LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
    _IMG_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"' + r"|'([^']*)')")
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
        r"(?:favicon|site[-_]logo|wordmark|site[-_]icon|app[-_]icon|social[-_]icon|logo|banner|sponsor|/flags/|header)",
        re.IGNORECASE,
    )
    _URL_DIMENSION_RE = re.compile(r"(?:^|[/_.-])([0-9]{1,4})x([0-9]{1,4})(?:[/_.-]|$)")
    # Substack CDN and similar services encode dimensions as ,w_N,h_N, in the URL path.
    _PATH_WIDTH_RE = re.compile(r"(?:^|[,_])w_([0-9]{1,4})(?:[,_]|$)")
    _PATH_HEIGHT_RE = re.compile(r"(?:^|[,_])h_([0-9]{1,4})(?:[,_]|$)")
    _PLACEHOLDER_URL_PATTERNS = re.compile(
        r"(?:grey-placeholder|image-unavailable|placeholder(?:[._-]|$)|no-image(?:[._-]|$)|fallback(?:[._-]|$)|bg_transparency|blank\.gif)",
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
    # Detects class attributes on surrounding HTML elements that mark author/bio/speaker sections.
    # Used by _extract_preferred_source_image_data to skip headshot images.
    _AUTHOR_CONTEXT_RE = re.compile(
        r'class=["\'][^"\']*(?:\bauthor\b|\bbio\b|\bbyline\b|\bspeaker\b|\bcontributor\b)',
        re.IGNORECASE,
    )
    # Allow Blogger/Google CDN URLs where the extension is followed by a size
    # param like =s1600 rather than appearing at the end of the path.
    _IMAGE_PATH_SUFFIX_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:[=?#]|$)", re.IGNORECASE)

    _LEAD_IMAGE_MIN_WIDTH = 200
    _LEAD_IMAGE_MIN_HEIGHT = 100
    _NEGATIVE_RETRY_SECONDS = 24 * 60 * 60
    _POSITIVE_REVALIDATE_SECONDS = 12 * 60 * 60
    _POSITIVE_REVALIDATE_PER_FEED_LIMIT = 12
    # Re-detect feed strategy weekly (or when still 'unknown')
    _STRATEGY_REDETECT_AFTER_SECONDS = 7 * 24 * 3600

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
        # Semaphore ensures at most one chunk-backfill thread runs at a time;
        # subsequent chunk requests skip rather than pile up.
        self._chunk_backfill_sem = threading.Semaphore(1)
        # In-memory set of feed URLs whose cache should be bypassed (debug only).
        self._debug_bypass_feeds: set[str] = set()
        # Small bounded cache of recently-fetched source HTML (entry_link → (final_url, html)).
        # Avoids a second HTTP request when extracting img alt text after lead image resolution.
        self._source_html_cache: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._SOURCE_HTML_CACHE_MAX = 8

    # ------------------------------------------------------------------
    # Feed lead-image strategy helpers
    # ------------------------------------------------------------------

    def get_feed_strategy(self, feed_url: str) -> tuple[str, float, bool]:
        """Return (strategy, detected_at, manual) from DB, or ('unknown', 0.0, False)."""
        try:
            with self._get_meta_connection() as conn:
                row = conn.execute(
                    "SELECT strategy, detected_at, manual FROM feed_lead_image_strategy WHERE feed_url = ?",
                    (feed_url,),
                ).fetchone()
            if row:
                return str(row["strategy"]), float(row["detected_at"]), bool(row["manual"])
        except Exception:
            pass
        return "unknown", 0.0, False

    def store_feed_strategy(self, feed_url: str, strategy: str, *, manual: bool = False) -> None:
        """Persist a lead-image strategy for a feed.

        manual=True locks the strategy so auto-detection never overwrites it.
        """
        try:
            with self._get_meta_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO feed_lead_image_strategy (feed_url, strategy, detected_at, manual)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(feed_url) DO UPDATE SET
                        strategy = excluded.strategy,
                        detected_at = excluded.detected_at,
                        manual = excluded.manual
                    """,
                    (feed_url, strategy, time.time(), int(manual)),
                )
        except Exception:
            pass

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

    def toggle_feed_bypass(self, feed_url: str) -> bool:
        """Toggle debug cache bypass for a feed. Returns the new bypass state."""
        if feed_url in self._debug_bypass_feeds:
            self._debug_bypass_feeds.discard(feed_url)
            return False
        self._debug_bypass_feeds.add(feed_url)
        return True

    def get_bypassed_feeds(self) -> frozenset[str]:
        return frozenset(self._debug_bypass_feeds)

    def clear_lead_image_cache(self, feed_url: str | None = None) -> tuple[int, list[str]]:
        """Delete lead image cache entries from DB and in-memory cache.

        If feed_url is given, clears only that feed. Otherwise clears all.
        Returns (rows_deleted, list_of_image_urls_that_were_cached).
        """
        # Collect URLs that are being evicted (for thumb-file purge by the caller).
        if feed_url:
            keys_to_drop = [k for k in self._cache if k[0] == feed_url]
        else:
            keys_to_drop = list(self._cache.keys())
        evicted_urls = [self._cache[k] for k in keys_to_drop if self._cache.get(k)]

        try:
            with self._get_meta_connection() as conn:
                if feed_url:
                    deleted = conn.execute("DELETE FROM entry_lead_images WHERE feed_url = ?", (feed_url,)).rowcount
                else:
                    deleted = conn.execute("DELETE FROM entry_lead_images").rowcount
        except Exception:
            deleted = 0

        for k in keys_to_drop:
            self._cache.pop(k, None)
            self._fetched_at_cache.pop(k, None)

        return deleted, evicted_urls

    def clear_entry_lead_image_cache(self, feed_url: str, entry_id: str) -> str | None:
        """Delete the lead image cache entry for a single (feed_url, entry_id).

        Returns the image URL that was cached (for thumb-file purge), or None.
        """
        key = (feed_url, entry_id)
        old_url = self._cache.get(key)
        self._cache.pop(key, None)
        self._fetched_at_cache.pop(key, None)
        try:
            with self._get_meta_connection() as conn:
                conn.execute(
                    "DELETE FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
        except Exception:
            pass
        return old_url

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
        if entry_id and feed_url not in self._debug_bypass_feeds and (feed_url, entry_id) in self._cache:
            cached = self._cache[(feed_url, entry_id)]
            if cached:
                if self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached):
                    cached = None
                elif not self._is_image_url_acceptable(cached, None, None):
                    cached = None
                else:
                    return cached

        # Some feeds (for example NYTimes) expose media thumbnail fields or
        # image enclosures on the entry object. Check common locations before
        # parsing HTML so we can surface thumbnails reliably in the posts list.
        try:
            # media_thumbnail may be a list of dicts or a single dict
            media_thumb = getattr(entry, "media_thumbnail", None)
            if media_thumb:
                # normalize list/dict
                candidates = media_thumb if isinstance(media_thumb, (list, tuple)) else [media_thumb]
                for item in candidates:
                    url = None
                    if isinstance(item, dict):
                        url = item.get("url") or item.get("href")
                    elif isinstance(item, str):
                        url = item
                    if url:
                        resolved = url
                        if self._is_image_url_acceptable(resolved, None, None) and not self._should_bypass_cached_url(
                            entry_link=entry_link, cached_url=resolved
                        ):
                            return resolved

            # media_content is often a list of dicts with 'url' and 'type'
            media_content = getattr(entry, "media_content", None)
            if media_content:
                candidates = media_content if isinstance(media_content, (list, tuple)) else [media_content]
                for item in candidates:
                    if isinstance(item, dict):
                        url = item.get("url")
                        mtype = item.get("type", "")
                        if url and (mtype.startswith("image") or self._is_image_url_acceptable(url, None, None)):
                            if not self._should_bypass_cached_url(entry_link=entry_link, cached_url=url):
                                return url

            # Some parsers expose enclosure/link entries on `links`.
            links = getattr(entry, "links", None)
            if links and isinstance(links, (list, tuple)):
                for link_obj in links:
                    try:
                        href = link_obj.get("href") if isinstance(link_obj, dict) else getattr(link_obj, "href", None)
                        rel = (link_obj.get("rel") if isinstance(link_obj, dict) else getattr(link_obj, "rel", None)) or ""
                        ltype = (link_obj.get("type") if isinstance(link_obj, dict) else getattr(link_obj, "type", None)) or ""
                    except Exception:
                        continue
                    if not href:
                        continue
                    if rel == "enclosure" and ltype.startswith("image"):
                        if self._is_image_url_acceptable(href, None, None) and not self._should_bypass_cached_url(
                            entry_link=entry_link, cached_url=href
                        ):
                            return href
                    # fallback: if link looks like an image URL
                    if self._is_image_url_acceptable(href, None, None) and not self._should_bypass_cached_url(
                        entry_link=entry_link, cached_url=href
                    ):
                        return href
        except Exception:
            pass

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
            # Skip plugin-flagged wrapper URLs (e.g. PCGamer /flexiimages/) so
            # the background job falls through to a proper source-page fetch
            # rather than being satisfied with an inferior feed thumbnail.
            if (
                inline_image
                and self._is_image_url_acceptable(inline_image, None, None)
                and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=inline_image)
            ):
                return inline_image
            linked_image = self._extract_linked_image_url_from_html(html_candidate, base_url)
            if (
                linked_image
                and self._is_image_url_acceptable(linked_image, None, None)
                and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=linked_image)
            ):
                return linked_image

        # Fallback: allow logo-pattern images from feed content that have large
        # declared dimensions — product/brand logos in press-release feeds are
        # valid lead images when the publisher explicitly sized them.
        for html_candidate in html_candidates:
            logo_image = self._extract_logo_with_dimensions_from_feed(html_candidate, base_url)
            if logo_image and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=logo_image):
                return logo_image

        # Plugin fallbacks run regardless of include_source_lookup — they handle
        # site-specific logic and may do their own targeted HTTP fetch.
        if entry_link:
            plugin_fallback = self._plugin_fallback_lead_image_url(entry_link=entry_link, content_html=content_html, summary=summary)
            if plugin_fallback and self._is_image_url_acceptable(plugin_fallback, None, None):
                return plugin_fallback

        if include_source_lookup and entry_link and self._is_short_entry_blurb(content_html, summary):
            try:
                source_image = self._fetch_source_lead_image(entry_link)
                if source_image and self._is_image_url_acceptable(source_image, None, None):
                    return source_image
            except Exception:
                pass

        return None

    def resolve_entry_lead_image_url(self, entry: object, content_html: str | None, summary: str | None) -> str | None:
        entry_link = str(getattr(entry, "link", "") or "")
        feed_url_str = str(getattr(entry, "feed_url", "") or "")
        entry_id_str = str(getattr(entry, "id", "") or "")
        base_url = entry_link or feed_url_str

        cached_negative = False
        if feed_url_str and entry_id_str and feed_url_str not in self._debug_bypass_feeds and (feed_url_str, entry_id_str) in self._cache:
            cache_key = (feed_url_str, entry_id_str)
            cached = self._cache[(feed_url_str, entry_id_str)]
            if cached:
                if self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached):
                    pass  # stale/wrong URL — re-run full resolution (plugin fallback first)
                elif not self._is_image_url_acceptable(cached, None, None):
                    cached_negative = True
                else:
                    should_revalidate = (
                        bool(entry_link)
                        and self._is_short_entry_blurb(content_html, summary)
                        and self._is_cache_key_stale(cache_key, max_age_seconds=self._POSITIVE_REVALIDATE_SECONDS)
                    )
                    if should_revalidate:
                        # Plugin takes priority: if it provides a preferred URL, trust it
                        # over generic source-page scraping (prevents SE cover → OG churn).
                        plugin_preferred = self._plugin_fallback_lead_image_url(
                            entry_link=entry_link, content_html=content_html, summary=summary
                        )
                        if plugin_preferred:
                            if plugin_preferred != cached:
                                return plugin_preferred
                            # plugin confirms cached value — skip source fetch
                        else:
                            source_image = self._fetch_source_lead_image(entry_link)
                            if source_image and source_image != cached:
                                return source_image
                    inline_candidate = None
                    if isinstance(content_html, str) and content_html.strip():
                        inline_candidate = self._extract_first_image_url_from_html(content_html, base_url)
                    if entry_link and inline_candidate and inline_candidate == cached:
                        plugin_preferred = self._plugin_fallback_lead_image_url(
                            entry_link=entry_link, content_html=content_html, summary=summary
                        )
                        if plugin_preferred:
                            if plugin_preferred != cached:
                                return plugin_preferred
                            # plugin confirms cached value — skip source fetch
                        else:
                            source_image = self._fetch_source_lead_image(entry_link)
                            if source_image and source_image != cached:
                                return source_image
                    return cached
            else:
                cached_negative = True

        # Prefer source-page metadata/image selection when available.
        # This usually picks a truer hero image than the first inline body image.
        strategy_for_feed, _, _ = self.get_feed_strategy(feed_url_str)
        skip_source = strategy_for_feed in ("inline", "youtube")
        if not cached_negative and entry_link:
            plugin_fallback = self._plugin_fallback_lead_image_url(entry_link=entry_link, content_html=content_html, summary=summary)
            if plugin_fallback and self._is_image_url_acceptable(plugin_fallback, None, None):
                return plugin_fallback

            if not skip_source and not self._plugin_should_skip_source_lookup(entry_link=entry_link):
                source_image = self._fetch_source_lead_image(entry_link)
                if source_image:
                    return source_image

        for candidate_html in (content_html, summary):
            if not isinstance(candidate_html, str) or not candidate_html.strip():
                continue
            image_url = self._extract_first_image_url_from_html(candidate_html, base_url)
            if image_url and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=image_url):
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
        feed_media_thumbs: dict[str, str] | None = None  # lazy: fetched once if needed

        # Load stored strategy; skip YouTube feeds entirely (thumbnail from video ID).
        strategy, detected_at, manual = self.get_feed_strategy(feed_url)
        need_redetect = not manual and (strategy == "unknown" or now - detected_at > self._STRATEGY_REDETECT_AFTER_SECONDS)
        if strategy == "youtube":
            return

        # Track which methods actually yield images so we can store/update strategy.
        _found_inline = False
        _found_media_rss = False
        _found_og_scrape = False

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

            if cache_key in self._cache and feed_url not in self._debug_bypass_feeds:
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
                _found_inline = True
                # For feeds manually locked to og_scrape, the source page is the
                # authoritative image source — fall through even when an inline
                # image exists (e.g. album cover) so we can find the real hero image.
                if not (strategy == "og_scrape" and manual):
                    continue

            entry_link = str(getattr(entry, "link", "") or "")
            if not entry_link:
                continue

            # Check feed-level media thumbnails (e.g. NYT's media:thumbnail) before
            # doing a source-page fetch.  These are unavailable on reader Entry objects
            # but are present in the raw RSS XML.
            if feed_media_thumbs is None:
                feed_media_thumbs = self._fetch_feed_media_thumbnails(feed_url)
                # Piggyback strategy detection: if the feedparser parse returned any
                # media thumbnails we know this is a media_rss feed.
                if need_redetect and feed_media_thumbs:
                    strategy = "media_rss"
                    need_redetect = False
            feed_thumb = feed_media_thumbs.get(entry_link)
            if feed_thumb:
                _found_media_rss = True
                self.store_entry_lead_image(feed_url_str, entry_id_str, feed_thumb)
                time.sleep(0.05)
                continue

            if self._plugin_should_skip_source_lookup(entry_link=entry_link):
                self.store_entry_lead_image(feed_url_str, entry_id_str, None)
                time.sleep(0.05)
                continue
            # For feeds whose images are reliably inline, source scraping rarely
            # improves on the feed content and frequently picks up site chrome.
            if strategy == "inline" and not need_redetect:
                self.store_entry_lead_image(feed_url_str, entry_id_str, None)
                time.sleep(0.05)
                continue
            image_url = self._fetch_source_lead_image(entry_link)
            if image_url:
                _found_og_scrape = True
            self.store_entry_lead_image(feed_url_str, entry_id_str, image_url)
            time.sleep(0.15)

        # Store detected strategy based on what actually worked this cycle.
        if need_redetect:
            if _found_media_rss:
                self.store_feed_strategy(feed_url, "media_rss")
            elif _found_og_scrape:
                self.store_feed_strategy(feed_url, "og_scrape")
            elif _found_inline:
                self.store_feed_strategy(feed_url, "inline")
            # else: leave as 'unknown' — feed may have no images at all

    # ------------------------------------------------------------------
    # Chunk-level visible-entry backfill
    # ------------------------------------------------------------------

    def backfill_entry_list(self, posts: list[dict]) -> None:
        """Fire-and-forget: fetch lead images for a specific list of entries.

        Designed for chunk-level prioritization — call this with entries
        currently visible that have no cached thumbnail.  Uses a non-blocking
        semaphore so concurrent chunk requests simply skip rather than queue.
        """
        if not self._chunk_backfill_sem.acquire(blocking=False):
            return
        try:
            self._do_backfill_entry_list(posts)
        finally:
            self._chunk_backfill_sem.release()

    def _do_backfill_entry_list(self, posts: list[dict]) -> None:
        # Group (entry_id, entry_link) by feed_url, skipping already-cached entries.
        by_feed: dict[str, list[tuple[str, str]]] = {}
        for post in posts:
            feed_url = str(post.get("feed_url") or "")
            entry_id = str(post.get("id") or "")
            entry_link = str(post.get("link") or "")
            if not feed_url or not entry_id:
                continue
            cached = self._cache.get((feed_url, entry_id))
            if (
                cached
                and feed_url not in self._debug_bypass_feeds
                and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached)
            ):
                continue
            by_feed.setdefault(feed_url, []).append((entry_id, entry_link))

        if not by_feed:
            return

        # Load all feed strategies in one query.
        feed_urls = list(by_feed.keys())
        strategies: dict[str, str] = {}
        try:
            placeholders = ",".join("?" for _ in feed_urls)
            with self._get_meta_connection() as conn:
                rows = conn.execute(
                    f"SELECT feed_url, strategy FROM feed_lead_image_strategy WHERE feed_url IN ({placeholders})",
                    feed_urls,
                ).fetchall()
            strategies = {str(row["feed_url"]): str(row["strategy"]) for row in rows}
        except Exception:
            pass

        for feed_url, entry_pairs in by_feed.items():
            strategy = strategies.get(feed_url, "unknown")
            if strategy == "youtube":
                continue

            # One feedparser call per feed to grab any media:thumbnail/content.
            feed_media = self._fetch_feed_media_thumbnails(feed_url)

            for entry_id, entry_link in entry_pairs:
                if not entry_link:
                    continue
                # Re-check cache — may have been populated by the regular backfill.
                cached = self._cache.get((feed_url, entry_id))
                if (
                    cached
                    and feed_url not in self._debug_bypass_feeds
                    and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached)
                ):
                    continue

                feed_thumb = feed_media.get(entry_link)
                if feed_thumb:
                    self.store_entry_lead_image(feed_url, entry_id, feed_thumb)
                    time.sleep(0.05)
                    continue

                # For inline/none-classified feeds, source scraping won't help.
                if strategy in ("inline", "none"):
                    continue

                image_url = self._fetch_source_lead_image(entry_link)
                self.store_entry_lead_image(feed_url, entry_id, image_url)
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
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or "").strip())
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

    def _extract_logo_with_dimensions_from_feed(self, html_text: str, base_url: str) -> str | None:
        """Scan feed content for logo-URL images that have explicit large dimensions.

        Product/brand logos in press-release feeds are valid lead images even when
        their URL contains "logo" — the publisher's declared width/height signals
        these are intentional content images, not site-chrome icons.
        """
        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or "").strip())
                if key and value:
                    attrs[key] = value
            src = attrs.get("src", "")
            if not src or src.startswith("data:"):
                continue
            resolved = urljoin(base_url, src)
            if not self._LOGO_URL_PATTERNS.search(resolved):
                continue
            if (
                self._TRACKER_URL_PATTERNS.search(resolved)
                or self._AVATAR_HINT_PATTERNS.search(resolved)
                or self._PLACEHOLDER_URL_PATTERNS.search(resolved)
            ):
                continue
            if not self._IMAGE_PATH_SUFFIX_RE.search(resolved.split("?")[0].lower()):
                continue
            w = self._parse_positive_int_attr(attrs, "width")
            h = self._parse_positive_int_attr(attrs, "height")
            if (
                w is not None and w >= self._LEAD_IMAGE_MIN_WIDTH
                and h is not None and h >= self._LEAD_IMAGE_MIN_HEIGHT
            ):
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
        # Square images at small scales are almost always author headshots.
        # Article lead images are virtually never 1:1 aspect ratio at ≤400 px.
        if (
            width_attr is not None
            and height_attr is not None
            and width_attr == height_attr
            and width_attr <= 400
        ):
            return False

        # Lazy-loaded site chrome (logos, nav images) uses a data: placeholder src
        # with no srcset and no explicit dimensions. The real URL lives in data-src,
        # but without any sizing signal we can't tell it apart from a logo.
        src_attr = (attrs.get("src") or "").strip()
        if src_attr.startswith("data:"):
            has_srcset = bool(attrs.get("srcset") or attrs.get("data-srcset"))
            if not has_srcset and width_attr is None and height_attr is None:
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
        url, _ = self._extract_preferred_source_image_data(html_text, base_url, source_url)
        return url

    def _extract_preferred_source_image_data(self, html_text: str, base_url: str, source_url: str) -> tuple[str | None, str | None]:
        """Like _extract_preferred_source_image_url but also returns the winning img's alt text."""
        best_url: str | None = None
        best_alt: str | None = None
        best_score = -1

        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            # Skip images inside author/speaker/bio sections — they are headshots.
            context_before = html_text[max(0, tag_match.start() - 500):tag_match.start()]
            if self._AUTHOR_CONTEXT_RE.search(context_before):
                continue
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or "").strip())
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
                    _alt = (attrs.get("alt") or attrs.get("title") or "").strip()
                    best_alt = _alt if _alt else None

        if best_url and best_score >= 10:
            return best_url, best_alt
        return None, None

    def _extract_preloaded_image_url(self, html_text: str, base_url: str) -> str | None:
        for tag_match in self._LINK_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or "").strip())
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

    def _plugin_should_skip_source_lookup(self, *, entry_link: str) -> bool:
        """Returns True if any plugin requests that source-page lookup be skipped entirely."""
        for plugin in self._plugins:
            try:
                if getattr(plugin, "should_skip_source_lookup", lambda **kw: False)(entry_link=entry_link):
                    return True
            except Exception:
                continue
        return False

    def _is_image_url_acceptable(self, image_url: str, width: int | None, height: int | None) -> bool:
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if self._TRACKER_URL_PATTERNS.search(parsed.netloc) or self._TRACKER_URL_PATTERNS.search(parsed.path):
            return False
        if self._AVATAR_HINT_PATTERNS.search(parsed.path):
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
            url_path_no_query = image_url.split("?")[0]
            for m in self._URL_DIMENSION_RE.finditer(url_path_no_query):
                try:
                    w, h = int(m.group(1)), int(m.group(2))
                    if w < self._LEAD_IMAGE_MIN_WIDTH or h < self._LEAD_IMAGE_MIN_HEIGHT:
                        return False
                except ValueError:
                    pass
            # Substack CDN and similar: ,w_N,h_N, path parameters.
            for mw in self._PATH_WIDTH_RE.finditer(url_path_no_query):
                try:
                    if int(mw.group(1)) < self._LEAD_IMAGE_MIN_WIDTH:
                        return False
                except ValueError:
                    pass
            for mh in self._PATH_HEIGHT_RE.finditer(url_path_no_query):
                try:
                    if int(mh.group(1)) < self._LEAD_IMAGE_MIN_HEIGHT:
                        return False
                except ValueError:
                    pass
        if width is not None and width < self._LEAD_IMAGE_MIN_WIDTH:
            return False
        if height is not None and height < self._LEAD_IMAGE_MIN_HEIGHT:
            return False
        return True

    # Minimum for og:image — sized to allow article-specific 300×200 images
    # (common on WordPress themes) while still blocking tiny icons.
    _OG_IMAGE_MIN_WIDTH = 300
    _OG_IMAGE_MIN_HEIGHT = 200

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
            if og_width is not None and og_width < self._OG_IMAGE_MIN_WIDTH:
                continue
            if og_height is not None and og_height < self._OG_IMAGE_MIN_HEIGHT:
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

    def _fetch_feed_media_thumbnails(self, feed_url: str) -> dict[str, str]:
        """Fetch the RSS/Atom feed and return {entry_link: best_thumbnail_url} from
        media:thumbnail / media:content elements.  These fields are not stored by
        python-reader on the Entry object so we parse the live feed once per
        background cycle to recover them."""
        try:
            parsed = feedparser.parse(
                feed_url,
                agent=self._user_agent,
                request_headers={"User-Agent": self._user_agent},
            )
        except Exception:
            return {}

        result: dict[str, str] = {}
        for fp_entry in getattr(parsed, "entries", []):
            link = str(getattr(fp_entry, "link", "") or "")
            if not link:
                continue

            best_url: str | None = None
            best_area = -1  # -1 so zero-dimension entries still get selected

            # media:thumbnail — list of dicts with 'url', optional 'width'/'height'
            for thumb in getattr(fp_entry, "media_thumbnail", []) or []:
                url = (thumb.get("url") if isinstance(thumb, dict) else None) or ""
                if not url or not self._is_image_url_acceptable(url, None, None):
                    continue
                try:
                    w = int(thumb.get("width", 0) or 0)
                    h = int(thumb.get("height", 0) or 0)
                except ValueError, TypeError:
                    w = h = 0
                if w and h and (w < self._LEAD_IMAGE_MIN_WIDTH or h < self._LEAD_IMAGE_MIN_HEIGHT):
                    continue
                area = w * h
                if area > best_area:
                    best_area = area
                    best_url = url

            # media:content — may contain images too
            if best_url is None:
                for mc in getattr(fp_entry, "media_content", []) or []:
                    if not isinstance(mc, dict):
                        continue
                    mtype = (mc.get("medium") or mc.get("type") or "").lower()
                    if "image" not in mtype and not mc.get("url", "").lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        continue
                    url = mc.get("url") or ""
                    if not url or not self._is_image_url_acceptable(url, None, None):
                        continue
                    try:
                        w = int(mc.get("width", 0) or 0)
                        h = int(mc.get("height", 0) or 0)
                    except ValueError, TypeError:
                        w = h = 0
                    if w and h and (w < self._LEAD_IMAGE_MIN_WIDTH or h < self._LEAD_IMAGE_MIN_HEIGHT):
                        continue
                    area = w * h
                    if area > best_area:
                        best_area = area
                        best_url = url

            if best_url and not self._should_bypass_cached_url(entry_link=link, cached_url=best_url):
                result[link] = best_url

        return result

    def _fetch_source_lead_image(self, entry_link: str) -> str | None:
        parsed = urlparse(entry_link)
        if parsed.scheme not in {"http", "https"}:
            return None
        if not is_safe_outbound_url(entry_link):
            return None

        try:
            with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": self._user_agent}) as client:
                response = client.get(entry_link)
            response.raise_for_status()
        except Exception:
            return None

        source_html = response.text
        final_url = str(response.url)
        # Cache for alt-text lookup without a second HTTP fetch.
        self._source_html_cache[entry_link] = (final_url, source_html)
        self._source_html_cache.move_to_end(entry_link)
        if len(self._source_html_cache) > self._SOURCE_HTML_CACHE_MAX:
            self._source_html_cache.popitem(last=False)

        preload_image = self._extract_preloaded_image_url(source_html, final_url)
        if preload_image:
            return preload_image
        preferred_image = self._extract_preferred_source_image_url(source_html, final_url, entry_link)
        if preferred_image:
            return preferred_image
        meta_image = self._extract_meta_image_url_from_html(source_html, final_url)
        if meta_image:
            return meta_image
        return self._extract_linked_image_url_from_html(source_html, final_url)

    _SMBC_HOST: str = "smbc-comics.com"
    _SMBC_AFTER_RE: re.Pattern[str] = re.compile(
        r'id=["\']aftercomic["\'][^>]*>.*?<img\b[^>]+src=["\']([^"\']+comics/[^"\']+after[^"\']*\.(?:png|jpe?g|gif|webp))["\']',
        re.IGNORECASE | re.DOTALL,
    )

    def fetch_smbc_bonus_panel_url(self, entry_link: str) -> str | None:
        """Return the SMBC bonus-panel image URL for an entry.

        Checks the in-memory source HTML cache first; fetches the source page
        only if it is not already cached (e.g. on cold-start or first open).
        Returns None on failure or if the entry is not an SMBC comic.
        """
        if self._SMBC_HOST not in urlparse(entry_link).netloc.lower():
            return None

        cached = self._source_html_cache.get(entry_link)
        if cached is None:
            if not is_safe_outbound_url(entry_link):
                return None
            try:
                with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": self._user_agent}) as client:
                    response = client.get(entry_link)
                response.raise_for_status()
            except Exception:
                return None
            source_html = response.text
            final_url = str(response.url)
            self._source_html_cache[entry_link] = (final_url, source_html)
            self._source_html_cache.move_to_end(entry_link)
            if len(self._source_html_cache) > self._SOURCE_HTML_CACHE_MAX:
                self._source_html_cache.popitem(last=False)
        else:
            _, source_html = cached

        m = self._SMBC_AFTER_RE.search(source_html)
        if m:
            url = html.unescape(m.group(1).strip())
            if url.startswith("/"):
                url = f"https://www.{self._SMBC_HOST}{url}"
            return url
        return None

    def fetch_entry_image_alt(self, entry_link: str) -> str | None:
        """Return alt/title text of the main scored image on the source page.

        Uses the in-memory source HTML cache populated by _fetch_source_lead_image.
        Returns None on cache miss — never fetches the source page on demand, since
        get_entry_detail calls extract_entry_thumbnail_url with include_source_lookup=False
        and an on-demand fetch would block the entry pane response for every entry.
        """
        cached = self._source_html_cache.get(entry_link)
        if cached is None:
            return None
        final_url, source_html = cached

        # Try the scored path first (high confidence).
        _, alt_text = self._extract_preferred_source_image_data(source_html, final_url, entry_link)
        if alt_text:
            return alt_text

        # Fallback: take the alt/title from the first acceptable img on the page.
        # Useful for simple comic pages where the img has no hero/featured class.
        for tag_match in self._IMG_TAG_RE.finditer(source_html):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or "").strip())
                if key and value:
                    attrs[key] = value
            for image_url in self._collect_img_candidate_urls(attrs):
                if not image_url or image_url.startswith("data:"):
                    continue
                resolved = urljoin(final_url, image_url)
                if not self._is_source_image_tag_acceptable(attrs, resolved):
                    continue
                if not self._is_image_url_acceptable(resolved, None, None):
                    continue
                candidate = (attrs.get("alt") or attrs.get("title") or "").strip()
                if candidate:
                    return candidate
                break  # first acceptable img found but no alt text — stop
        return None
