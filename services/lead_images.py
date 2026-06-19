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

from services import tenancy
from services import url_guard
from services import svg_sanitize
from services.lead_image_plugins import DEFAULT_LEAD_IMAGE_PLUGINS, LeadImagePlugin
from services.url_guard import is_safe_outbound_url


class LeadImageService:
    """Encapsulates entry lead-image extraction, caching, and persistence."""

    _IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
    _LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
    # Whole inline <svg>…</svg> element (non-greedy), for feeds that express an
    # article icon/hero as raw inline SVG rather than an <img>.
    _INLINE_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg\s*>", re.IGNORECASE | re.DOTALL)
    _IMG_ATTR_RE = re.compile(
        r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*'
        r'(?:"([^"]*)"'
        r"|'([^']*)'"
        r'|([^\s"\'`=<>]+))'
    )
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
    # Blogger CDN w{W}-h{H} crop-format URLs (used for social share cards) distort
    # square app icons into 16:9 aspect ratio.  Normalise to s1600 to get the
    # full uncropped image.  Matches both blogger.googleusercontent.com and *.bp.blogspot.com.
    _BLOGGER_CROP_RE = re.compile(
        r"^(https://(?:\d+\.bp\.blogspot\.com|(?:blogger|lh\d+)\.googleusercontent\.com)/.+?)/w\d+-h\d+[^/]*/(.+)$",
        re.IGNORECASE,
    )
    # BBCode [img]...[/img] found in some feed content (e.g. Nexus Mods).  Only
    # the img tag is converted here — enough for inline image extraction.
    _BBCODE_IMG_RE = re.compile(
        r'\[img(?:=[^\]]*)?](https?://[^\[]{1,500})\[/img]', re.IGNORECASE
    )

    @classmethod
    def _bbcode_img_to_html(cls, text: str) -> str:
        """Convert [img]url[/img] BBCode tags to <img src="url"> for image extraction."""
        return cls._BBCODE_IMG_RE.sub(r'<img src="\1">', text)
    _LOGO_URL_PATTERNS = re.compile(
        r"(?:favicon|site[-_]logo|wordmark|site[-_]icon|app[-_]icon|social[-_]icon|apple-touch-icon|android-chrome|logo(?![a-zA-Z0-9])|sponsor|/flags/|/awards?/|btn_donate|donate[-_]btn|divider|separator|share[-_]image)",
        re.IGNORECASE,
    )
    # Catches pixel/spacer images encoded with tiny dimensions in the filename
    # (e.g. p_1x1a.jpg, blank-2x2.gif). The lookahead is lenient to handle
    # names like "1x1a" where a letter follows the dimension.
    _TINY_DIM_RE = re.compile(r"(?:^|[/_.-])([0-9]{1,2})x([0-9]{1,2})(?:[/_.\-a-z]|$)", re.IGNORECASE)
    # CMS theme/plugin directories and known CMS admin resource CDNs are never
    # article images — always site-level assets. Checked even when skip_logo_patterns=True.
    # Path patterns checked against parsed.path; domain patterns against parsed.netloc.
    _SITE_CHROME_PATH_PATTERNS = re.compile(
        r"/wp-content/(?:themes|plugins)/|/e107_(?:images|themes|plugins)/|hamburger|keyboard[-_]arrow"
        # "/navigation/" — header/menu UI icons served from a nav asset directory
        # (e.g. paizo.com/image/navigation/Personal-Account.png). These are the
        # first <img> on the page and would otherwise win the first-image bonus.
        r"|/navigation/"
        r"|/social/(?:facebook|twitter|instagram|linkedin|youtube|pinterest|reddit|tiktok|discord|digg|tumblr|whatsapp|rss|email|telegram|snapchat|twitterx|bluesky)\."
        # "/social-media/social-*" — per-platform sharing cards (e.g. krita's social-youtube.png)
        r"|/social-media/social-"
        # Theme/static asset product directories (e.g. pythonguis /static/theme/images/products/)
        r"|/(?:static|assets?)/(?:themes?)/images?/products?/"
        # Sidebar widgets and OG/social-share card images are site chrome, not article content.
        # "sidebar" catches CMS sidebar images (e.g. cad-comic.com/wp-content/uploads/.../sidebar.png).
        # "opengraph" catches brand og:image files stored under a predictable URL
        # (e.g. logo_opengraph.jpg) that slip through the logo-pattern check.
        r"|sidebar|opengraph",
        re.IGNORECASE,
    )
    # Domains that serve only CMS admin/template assets (never user content images).
    # www.blogger.com hosts only widget chrome like the "Powered By Blogger" button —
    # Blogger content images live on bp.blogspot.com / blogger.googleusercontent.com.
    # Also includes YouTube image CDNs — YouTube thumbnails are handled explicitly for
    # YouTube feeds (early-return in extract_entry_thumbnail_url lines 343-351); for all
    # other feeds they represent embedded video thumbnails, not the article's lead image.
    _SITE_CHROME_DOMAIN_PATTERNS = re.compile(
        r"(?:resources\.blogblog\.com|www\.blogger\.com|i\.ytimg\.com|img\.youtube\.com)",
        re.IGNORECASE,
    )
    # Keep old name as alias so callers outside this class still work.
    _SITE_CHROME_URL_PATTERNS = _SITE_CHROME_PATH_PATTERNS
    _URL_DIMENSION_RE = re.compile(r"(?:^|[/_.-])([0-9]{1,4})x([0-9]{1,4})(?:[/_.-]|$)")
    # WordPress responsive-image width-only suffix, e.g. "photo-1000w.jpeg"
    _URL_WIDTH_HINT_RE = re.compile(r"(?:^|[-_.])([0-9]{2,4})w(?:[-_.]|$)", re.IGNORECASE)
    # Substack CDN and similar services encode dimensions as ,w_N,h_N, in the URL path.
    _PATH_WIDTH_RE = re.compile(r"(?:^|[,_])w_([0-9]{1,4})(?:[,_]|$)")
    _PATH_HEIGHT_RE = re.compile(r"(?:^|[,_])h_([0-9]{1,4})(?:[,_]|$)")
    # Matches <source type="image/webp" srcset="..."> in either attribute order.
    _WEBP_SOURCE_SRCSET_RE = re.compile(
        r'<source\b[^>]+type=["\']image/webp["\'][^>]+srcset=["\']([^"\']+)["\']'
        r'|<source\b[^>]+srcset=["\']([^"\']+)["\'][^>]+type=["\']image/webp["\']',
        re.IGNORECASE | re.DOTALL,
    )
    _PLACEHOLDER_URL_PATTERNS = re.compile(
        # "blank.<ext>" covers the WordPress.com placeholder (s0.wp.com/i/blank.jpg)
        # that ships as the og:image on image-less posts — a 200x200 white box.
        r"(?:grey-placeholder|image-unavailable|placeholder(?:[._-]|$)|no-image(?:[._-]|$)|fallback(?:[._-]|$)|bg_transparency|blank\.(?:gif|jpe?g|png|webp)|spinner(?:\.|$)|spacer(?:[0-9._-]|$))",
        re.IGNORECASE,
    )
    _TRACKER_URL_PATTERNS = re.compile(
        # statcounter — c.statcounter.com tracking pixels carry alt="Web Analytics"
        # and ship as a 1x1 transparent GIF, which scales up to a grey thumbnail when
        # mistaken for a lead image on an image-less post.
        # addtoany/addthis/sharethis — social share-button widget sprites (e.g.
        # static.addtoany.com/buttons/share_save_171_16.png, alt="Share"); never article content.
        r"(?:scorecardresearch|doubleclick|googletagmanager|google-analytics|adservice|adsystem|pixel|beacon|analytics|statcounter|piwik|matomo|paypalobjects|paypal\.com|jetpack\.com/redirect|share[-_]image|addtoany|addthis|sharethis)",
        re.IGNORECASE,
    )
    # Emoji image sprites embedded inline in post bodies (WordPress wp-smiley served
    # from s.w.org, and IP.Board/twemoji from the twemoji CDN). They are meaningful
    # inline (and sized to ~1em by CSS at render) but must never be picked as a post's
    # lead image / thumbnail — they're decorative glyphs, not article content.
    _EMOJI_URL_PATTERNS = re.compile(
        r"(?:s\.w\.org/images/core/emoji/|/twemoji[/@]|gh/twitter/twemoji)",
        re.IGNORECASE,
    )
    # Advertisement images embedded in feed content / article bodies. Matched
    # against the URL path: an "ads" directory, or an "-ad"/"_ad" filename token
    # followed by a digit or separator (e.g. .../Software-Pro-Cert-ad1.png).
    # Boundaries avoid false positives in words like "uploads", "download", "lead".
    _AD_URL_PATTERNS = re.compile(
        r"(?:[-_/]ads?[-_./]|[-_]ad[0-9]|/advert)",
        re.IGNORECASE,
    )
    # Advertisement images flagged by their alt/title text (e.g. SE Radio's
    # "banner ad that says ..."). Kept tight to avoid rejecting article images
    # that merely discuss advertising.
    _AD_ALT_PATTERNS = re.compile(
        r"(?:\bbanner ad\b|\bad banner\b|\badvertisement\b)",
        re.IGNORECASE,
    )
    _AVATAR_HINT_PATTERNS = re.compile(
        r"(?:avatar|author(?:-image)?\b|byline|profile|headshot|user(?:-image|pic)?|gravatar|(?<![a-zA-Z0-9])round(?![a-zA-Z0-9]))",
        re.IGNORECASE,
    )
    # Code-forge avatar URLs are a single user segment + .png on the forge host
    # (e.g. github.com/octocat.png, gitea.com/delvh.png) — profile pictures, not
    # article images. Repo/asset paths have more segments and don't match.
    _FORGE_AVATAR_HOSTS = frozenset({
        "github.com", "www.github.com", "gitea.com", "gitlab.com", "codeberg.org",
    })
    _FORGE_AVATAR_PATH_RE = re.compile(r"^/[^/]+\.png$", re.IGNORECASE)
    # Detects class attributes on surrounding HTML elements that mark author/bio/speaker sections.
    # Used by _extract_preferred_source_image_data to skip headshot images.
    _AUTHOR_CONTEXT_RE = re.compile(
        r'class=["\'][^"\']*(?:\bauthor\b|\bbio\b|\bbyline\b|\bspeaker\b|\bcontributor\b)',
        re.IGNORECASE,
    )
    # Detects site-chrome structural elements (header logo, branding, navigation,
    # and related/recent-post sidebars) that contain decorative images, not article content.
    _SITE_CHROME_CONTEXT_RE = re.compile(
        r'class=["\'][^"\']*(?:\bbranding\b|\bsite-logo\b|\bsite-header\b|\bsite-name\b|\bsubscribe-dropdown\b|\brelated-content\b|\brelated-posts\b|\brecent-posts\b|\bmobile-banner\b|\bcomic-navigation\b|\bnav-links\b'
        # Nav menus and dropdowns (e.g. krita.org's language-picker icon) and
        # CMS sidebar/footer widgets (e.g. Blogger's "Powered By Blogger" button).
        r'|\bnavbar\b|\bnav-item\b|\bnav-link\b|\bdropdown-toggle\b|\bwidget\b)',
        re.IGNORECASE,
    )
    # Related/recent/more-posts containers whose thumbnails belong to OTHER posts.
    # The per-image site-chrome check only looks ~500 chars back, so images deep
    # in a long related list escape it; stripping the whole container is reliable.
    _RELATED_BLOCK_OPEN_RE = re.compile(
        r'<(div|section|aside|nav|ul)\b[^>]*\bclass=["\'][^"\']*'
        r'(?:related[-_]content|related[-_]posts|recent[-_]posts|more[-_]posts|'
        r'you[-_]might|you[-_]may|see[-_]also|read[-_]next|post[-_]nav)'
        r'[^"\']*["\'][^>]*>',
        re.IGNORECASE,
    )
    # Allow Blogger/Google CDN URLs where the extension is followed by a size
    # param like =s1600 rather than appearing at the end of the path.
    _IMAGE_PATH_SUFFIX_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:[=?#]|$)", re.IGNORECASE)
    # Finds CSS background-image: url(...) values in inline style attributes.
    _CSS_BG_IMAGE_RE = re.compile(
        r'\bstyle=["\'][^"\']*background(?:-image)?\s*:\s*url\(["\']?([^"\')\s]+)["\']?\)',
        re.IGNORECASE,
    )

    # Comic-specific image element identifiers.  When a feed is in webcomic mode
    # these IDs/classes receive a large score bonus so the main comic panel wins
    # over nav buttons, site chrome, and vote/promotion images.
    _WEBCOMIC_IMG_ID_RE = re.compile(
        r'^(?:strip|cc-comic|comic|comicimg|comic-image|comic_image|comicImage|woo-entry-image)$',
        re.IGNORECASE,
    )
    _WEBCOMIC_IMG_CLASS_RE = re.compile(
        r'\b(?:comic-image|comic-strip|comic-img|comicImg|webcomic)\b',
        re.IGNORECASE,
    )

    _LEAD_IMAGE_MIN_WIDTH = 200
    _LEAD_IMAGE_MIN_HEIGHT = 100
    _NEGATIVE_RETRY_SECONDS = 4 * 60 * 60

    # Per-feed injected-block stripping: maps a host substring to a tuple of
    # CSS class markers.  Divs whose class attribute contains ALL markers are
    # removed before image extraction so sidebar/promo thumbnails don't win.
    _FEED_STRIP_RULES: dict[str, tuple[str, ...]] = {
        "mynorthwest.com": ("related", "alignright"),
    }

    _FEED_STRIP_DIV_RE = re.compile(r'<(/?)div\b[^>]*>', re.IGNORECASE)
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
        self._alt_cache: dict[tuple[str, str], str | None] = {}
        self._title_cache: dict[tuple[str, str], str | None] = {}
        self._entry_crop_cache: dict[tuple[str, str], str] = {}
        self._webcomic_feeds: set[str] | None = None
        self._plugins = plugins if plugins is not None else DEFAULT_LEAD_IMAGE_PLUGINS
        # Semaphore ensures at most one chunk-backfill thread runs at a time;
        # subsequent chunk requests skip rather than pile up.
        self._chunk_backfill_sem = threading.Semaphore(1)
        # Tracks (feed_url, entry_id) pairs for which a background source-page
        # fetch is already in flight, so opening the same entry twice quickly
        # doesn't spawn duplicate fetches.
        self._source_fetch_in_progress: set[tuple[str, str]] = set()
        # Events signalled when a queue_source_fetch completes; used to let the
        # first-open entry render wait briefly for image + alt/title to be ready.
        self._source_fetch_events: dict[tuple[str, str], threading.Event] = {}
        # In-memory set of feed URLs whose cache should be bypassed (debug only).
        self._debug_bypass_feeds: set[str] = set()
        # Feeds manually locked to strategy='none' — no lead image for any entry.
        # Loaded from DB on first access; updated when store_feed_strategy is called.
        self._none_strategy_feeds: set[str] | None = None
        # Small bounded cache of recently-fetched source HTML (entry_link → (final_url, html)).
        # Avoids a second HTTP request when extracting img alt text after lead image resolution.
        self._source_html_cache: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._SOURCE_HTML_CACHE_MAX = 8
        # Events signalled when queue_source_html_fetch completes; lets the first-open entry
        # render wait briefly for caption text rather than deferring to the next open.
        self._source_html_fetch_events: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # Feed lead-image strategy helpers
    # ------------------------------------------------------------------

    def _strip_feed_injected_blocks(self, html: str, feed_url: str) -> str:
        """Strip known injected promo/sidebar div blocks for specific feeds."""
        for host_marker, class_markers in self._FEED_STRIP_RULES.items():
            if host_marker not in feed_url:
                continue
            if not all(m in html for m in class_markers):
                continue
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
                for dm in self._FEED_STRIP_DIV_RE.finditer(html, start):
                    if dm.group(1):
                        depth -= 1
                        if depth == 0:
                            end = dm.end()
                            break
                    else:
                        depth += 1
                pos = end if end > start else match.end()
            result.append(html[pos:])
            html = "".join(result)
        return html

    def _strip_related_post_blocks(self, html: str) -> str:
        """Remove related/recent/more-posts containers from source-page HTML.

        Static-site blogs (Hugo, etc.) often render a "related content" list of
        OTHER posts' thumbnails. With no og:image on the page, lead-image scoring
        would otherwise pick one of those, showing a different post's image. We
        drop the whole balanced container so only the article's own images score.
        """
        result: list[str] = []
        pos = 0
        for match in self._RELATED_BLOCK_OPEN_RE.finditer(html):
            start = match.start()
            if start < pos:
                continue
            tag_name = match.group(1).lower()
            result.append(html[pos:start])
            tag_re = re.compile(r'<(/?)' + tag_name + r'\b[^>]*>', re.IGNORECASE)
            depth = 0
            end = start
            for dm in tag_re.finditer(html, start):
                if dm.group(1):
                    depth -= 1
                    if depth == 0:
                        end = dm.end()
                        break
                else:
                    depth += 1
            pos = end if end > start else match.end()
        result.append(html[pos:])
        return "".join(result)

    def _is_feed_none_strategy(self, feed_url: str) -> bool:
        """Return True if this feed is manually locked to strategy='none' (no lead images)."""
        if self._none_strategy_feeds is None:
            try:
                with self._get_meta_connection() as conn:
                    rows = conn.execute(
                        "SELECT feed_url FROM feed_lead_image_strategy WHERE strategy = 'none' AND manual = 1"
                    ).fetchall()
                self._none_strategy_feeds = {str(r["feed_url"]) for r in rows}
            except Exception:
                self._none_strategy_feeds = set()
        return feed_url in self._none_strategy_feeds

    def _is_feed_webcomic(self, feed_url: str) -> bool:
        """Return True if this feed has strategy='webcomic' (auto or manual)."""
        if self._webcomic_feeds is None:
            try:
                with self._get_meta_connection() as conn:
                    rows = conn.execute(
                        "SELECT feed_url FROM feed_lead_image_strategy WHERE strategy = 'webcomic'"
                    ).fetchall()
                self._webcomic_feeds = {str(r["feed_url"]) for r in rows}
            except Exception:
                self._webcomic_feeds = set()
        return feed_url in self._webcomic_feeds

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
        # Keep in-memory none-strategy and webcomic-strategy sets in sync.
        if self._none_strategy_feeds is not None:
            if strategy == "none" and manual:
                self._none_strategy_feeds.add(feed_url)
            else:
                self._none_strategy_feeds.discard(feed_url)
        if self._webcomic_feeds is not None:
            if strategy == "webcomic":
                self._webcomic_feeds.add(feed_url)
            else:
                self._webcomic_feeds.discard(feed_url)

    def warm_cache_from_db(self) -> None:
        """Load stored lead-image records into in-memory caches."""
        try:
            with self._get_meta_connection() as conn:
                rows = conn.execute("SELECT * FROM entry_lead_images").fetchall()
            for row in rows:
                url = row["image_url"]
                key = (str(row["feed_url"]), str(row["entry_id"]))
                if url and not self._is_image_url_acceptable(str(url), None, None, allow_extensionless=True, skip_logo_patterns=True):
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
                alt = row["image_alt"]
                if alt is not None:
                    self._alt_cache[key] = str(alt)
                title = row["image_title"] if "image_title" in row.keys() else None
                if title is not None:
                    self._title_cache[key] = str(title)
                ec = row["thumb_crop"] if "thumb_crop" in row.keys() else None
                if ec:
                    self._entry_crop_cache[key] = str(ec)
                try:
                    self._fetched_at_cache[key] = float(row["fetched_at"])
                except Exception:
                    self._fetched_at_cache[key] = 0.0
        except Exception:
            pass

    def get_entry_image_alt(self, feed_url: str, entry_id: str) -> str | None:
        """Return the persisted raw alt-attribute text for an entry's lead image, or None."""
        return self._alt_cache.get((feed_url, entry_id))

    def get_entry_image_title(self, feed_url: str, entry_id: str) -> str | None:
        """Return the persisted raw title-attribute text for an entry's lead image, or None."""
        return self._title_cache.get((feed_url, entry_id))

    def get_entry_thumb_crop(self, feed_url: str, entry_id: str) -> str | None:
        """Return the per-entry thumbnail crop override, or None to use the feed default."""
        return self._entry_crop_cache.get((feed_url, entry_id))

    def store_entry_thumb_crop(self, feed_url: str, entry_id: str, crop: str | None) -> None:
        """Persist (or clear) a per-entry thumbnail crop override."""
        key = (feed_url, entry_id)
        if crop:
            self._entry_crop_cache[key] = crop
        else:
            self._entry_crop_cache.pop(key, None)
        try:
            with self._get_meta_connection() as conn:
                conn.execute(
                    "UPDATE entry_lead_images SET thumb_crop = ? WHERE feed_url = ? AND entry_id = ?",
                    (crop or None, feed_url, entry_id),
                )
        except Exception:
            pass

    def store_entry_image_alt(
        self,
        feed_url: str,
        entry_id: str,
        alt_text: str | None,
        title_text: str | None = None,
    ) -> None:
        """Persist alt and title text for an entry's lead image to DB and in-memory cache."""
        key = (feed_url, entry_id)
        self._alt_cache[key] = alt_text
        self._title_cache[key] = title_text
        try:
            with self._get_meta_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO entry_lead_images (feed_url, entry_id, image_url, image_alt, image_title, fetched_at)
                    VALUES (?, ?, NULL, ?, ?, ?)
                    ON CONFLICT(feed_url, entry_id) DO UPDATE SET
                        image_alt = excluded.image_alt,
                        image_title = excluded.image_title
                    """,
                    (feed_url, entry_id, alt_text, title_text, time.time()),
                )
        except Exception:
            pass

    def _load_cached_url_from_db(self, feed_url: str, entry_id: str) -> str | None:
        """Read one stored lead-image URL from the DB and warm the in-memory cache.

        Caches the result (including an explicit "no row found" miss) so repeat
        lookups stay in memory. A DB *error* is NOT cached: caching None on a
        transient failure would turn it into a permanent negative entry for the
        life of the process, hiding a valid stored image until restart.
        """
        try:
            with self._get_meta_connection() as conn:
                row = conn.execute(
                    "SELECT image_url FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                ).fetchone()
        except Exception:
            return None
        url = row["image_url"] if row else None
        self._cache[(feed_url, entry_id)] = url
        return url

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

    def persist_lead_image_async(self, feed_url: str, entry_id: str, image_url: str | None) -> None:
        """Request-path lead-image persistence that never blocks the response.

        The article-open path calls this instead of store_entry_lead_image. It
        refreshes the in-memory cache synchronously, then — only when the value
        actually changed — writes the meta DB on a daemon thread (tenancy
        re-bound). Re-opening an already-resolved entry skips the write entirely.
        This keeps the request thread off the single-writer meta-DB lock that the
        background lead-image backfill holds; otherwise opens waited up to the
        meta busy_timeout (10s) whenever a backfill write was in flight.
        """
        key = (feed_url, entry_id)
        unchanged = key in self._cache and self._cache[key] == image_url
        self._cache[key] = image_url
        self._fetched_at_cache[key] = time.time()
        if unchanged:
            return
        uid = tenancy.current_user_id()

        def _bg() -> None:
            with tenancy.user_context(uid):
                self.store_entry_lead_image(feed_url, entry_id, image_url)

        threading.Thread(target=_bg, daemon=True).start()

    def persist_image_alt_async(
        self, feed_url: str, entry_id: str, alt_text: str | None, title_text: str | None = None
    ) -> None:
        """Async + skip-if-unchanged counterpart of store_entry_image_alt for the
        request path (same rationale as persist_lead_image_async)."""
        key = (feed_url, entry_id)
        unchanged = (
            key in self._alt_cache
            and self._alt_cache[key] == alt_text
            and self._title_cache.get(key) == title_text
        )
        self._alt_cache[key] = alt_text
        self._title_cache[key] = title_text
        if unchanged:
            return
        uid = tenancy.current_user_id()

        def _bg() -> None:
            with tenancy.user_context(uid):
                self.store_entry_image_alt(feed_url, entry_id, alt_text, title_text=title_text)

        threading.Thread(target=_bg, daemon=True).start()

    def rename_feed_url_in_cache(self, old_url: str, new_url: str) -> None:
        """Re-key all in-memory cache entries from old_url to new_url after a feed URL change."""
        for cache in (self._cache, self._alt_cache, self._title_cache, self._fetched_at_cache):
            old_keys = [k for k in cache if k[0] == old_url]
            for k in old_keys:
                cache[(new_url, k[1])] = cache.pop(k)
        if old_url in self._debug_bypass_feeds:
            self._debug_bypass_feeds.discard(old_url)
            self._debug_bypass_feeds.add(new_url)

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

    def invalidate_image_url(self, image_url: str) -> None:
        """Null out any cached entry whose stored image_url matches.

        Called when the thumbnail proxy discovers the image is gone (404/410).
        Nulling rather than deleting preserves fetched_at so the source-fetch
        isn't re-triggered on the next page open.
        """
        try:
            with self._get_meta_connection() as conn:
                rows = conn.execute(
                    "SELECT feed_url, entry_id FROM entry_lead_images WHERE image_url = ?",
                    (image_url,),
                ).fetchall()
                conn.execute(
                    "UPDATE entry_lead_images SET image_url = NULL WHERE image_url = ?",
                    (image_url,),
                )
                conn.execute(
                    "UPDATE feed_strategy_cache SET image_url = NULL WHERE image_url = ?",
                    (image_url,),
                )
            for row in rows:
                self._cache[(row[0], row[1])] = None
        except Exception:
            pass

    def _is_cache_key_stale(self, cache_key: tuple[str, str], *, max_age_seconds: int) -> bool:
        fetched_at = self._fetched_at_cache.get(cache_key, 0.0)
        return time.time() - fetched_at >= max_age_seconds

    def get_cached_entry_thumbnail(self, feed_url: str, entry_id: str, entry_link: str) -> str | None:
        """Return only the cached lead-image URL for thumb=auto mode.

        Unlike extract_entry_thumbnail_url, this never falls back to inline
        media_thumbnail / enclosure fields. That fallback is wrong when the
        feed strategy is og_scrape: the inline RSS image is not the article's
        hero image, and showing it contradicts the user's "Auto (same as
        article image source)" choice.

        Returns the cached URL (or None for explicit 'no image' or
        uncached entries). YouTube feeds return their computed thumbnail
        directly without a cache lookup.
        """
        if feed_url and "youtube.com/feeds/videos.xml" in feed_url:
            video_id = self._extract_video_id(entry_link)
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None
        if feed_url and self._is_feed_none_strategy(feed_url):
            return None
        key = (feed_url, entry_id)
        if key in self._cache:
            cached = self._cache[key]
        else:
            # Read-through: the in-memory cache is seeded once at startup under the
            # default tenancy, so a restart (or a different user) leaves it cold.
            # Fall back to the per-user DB so stored lead images survive restarts.
            cached = self._load_cached_url_from_db(feed_url, entry_id)
        if not cached:
            return None
        if self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached):
            return None
        if not self._is_image_url_acceptable(cached, None, None, allow_extensionless=True, skip_logo_patterns=True):
            return None
        return cached

    def extract_entry_thumbnail_url(self, entry: object, include_source_lookup: bool = False, fast_only: bool = False) -> str | None:
        entry_link = str(getattr(entry, "link", "") or "")
        feed_url = str(getattr(entry, "feed_url", "") or "")

        if isinstance(feed_url, str) and "youtube.com/feeds/videos.xml" in feed_url and entry_link:
            video_id = self._extract_video_id(entry_link)
            if video_id:
                return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        # Respect manual 'none' strategy — skip lead images entirely for this feed.
        if feed_url and self._is_feed_none_strategy(feed_url):
            return None

        entry_id = str(getattr(entry, "id", "") or "")
        if entry_id and feed_url not in self._debug_bypass_feeds and (feed_url, entry_id) in self._cache:
            cached = self._cache[(feed_url, entry_id)]
            if cached is None:
                # Explicitly cached as "no image" — don't fall through to feed scan.
                # Background job retries negatives on its own schedule.
                return None
            if cached:
                if self._should_bypass_cached_url(entry_link=entry_link, cached_url=cached):
                    cached = None
                elif not self._is_image_url_acceptable(cached, None, None, allow_extensionless=True, skip_logo_patterns=True):
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

            # reader stores enclosures on entry.enclosures (tuple of Enclosure objects).
            enclosures = getattr(entry, "enclosures", None)
            if enclosures and isinstance(enclosures, (list, tuple)):
                for enc in enclosures:
                    try:
                        # reader's Enclosure uses .href; feedparser dicts use "href" or "url".
                        if isinstance(enc, dict):
                            url = enc.get("href") or enc.get("url")
                            etype = enc.get("type") or ""
                        else:
                            url = getattr(enc, "href", None) or getattr(enc, "url", None)
                            etype = getattr(enc, "type", None) or ""
                    except Exception:
                        continue
                    if url and etype.startswith("image"):
                        if self._is_image_url_acceptable(url, None, None) and not self._should_bypass_cached_url(
                            entry_link=entry_link, cached_url=url
                        ):
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

        if fast_only:
            return None

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
        _VIDEO_POSTER_RE = re.compile(r'<video\b[^>]+\bposter=["\']([^"\']+)["\']', re.IGNORECASE)
        for html_candidate in html_candidates:
            if feed_url:
                html_candidate = self._strip_feed_injected_blocks(html_candidate, feed_url)
            # Check <video poster="..."> before <img> — video posts (e.g. Tumblr)
            # embed the frame thumbnail as the poster, which is the best lead image.
            for vm in _VIDEO_POSTER_RE.finditer(html_candidate):
                poster_url = urljoin(base_url, html.unescape(vm.group(1).strip()))
                if (
                    poster_url
                    and self._is_image_url_acceptable(poster_url, None, None)
                    and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=poster_url)
                ):
                    return poster_url
            # Feed publishers intentionally include images in their content, so
            # extensionless URLs (e.g. server-generated banners) are acceptable here.
            inline_image = self._extract_first_image_url_from_html(html_candidate, base_url, allow_extensionless=True)
            # Skip plugin-flagged wrapper URLs (e.g. PCGamer /flexiimages/) so
            # the background job falls through to a proper source-page fetch
            # rather than being satisfied with an inferior feed thumbnail.
            if (
                inline_image
                and self._is_image_url_acceptable(inline_image, None, None, allow_extensionless=True, source_url=base_url)
                and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=inline_image)
            ):
                return inline_image
            linked_image = self._extract_linked_image_url_from_html(html_candidate, base_url)
            if (
                linked_image
                and self._is_image_url_acceptable(linked_image, None, None, source_url=base_url)
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

    def extract_inline_thumb_url(self, entry: object) -> str | None:
        """Return the first inline image from the entry's feed-content HTML.

        Intentionally bypasses the lead-image cache so the result is independent
        of the feed's primary strategy (e.g. og_scrape).  No HTTP requests are
        made; only the content already stored in the reader DB is used.
        """
        entry_link = str(getattr(entry, "link", "") or "")
        feed_url = str(getattr(entry, "feed_url", "") or "")
        base_url = entry_link or feed_url

        prepared = self._prepared_content_html(entry, feed_url)
        for html_candidate in prepared:
            inline_image = self._extract_first_image_url_from_html(html_candidate, base_url, allow_extensionless=True)
            if (
                inline_image
                and self._is_image_url_acceptable(inline_image, None, None, allow_extensionless=True)
                and not self._should_bypass_cached_url(entry_link=entry_link, cached_url=inline_image)
            ):
                return inline_image
        # No raster image — fall back to a raw inline <svg> (sanitized → data URI).
        for html_candidate in prepared:
            svg_uri = self._extract_inline_svg_data_uri(html_candidate)
            if svg_uri:
                return svg_uri
        return None

    def _prepared_content_html(self, entry: object, feed_url: str) -> list[str]:
        """Entry content + summary HTML, feed-block-stripped and bbcode-normalized.

        Shared by the inline-image and inline-<svg> thumbnail extractors so both
        see the same prepared content. No HTTP requests; reader-DB content only.
        """
        html_candidates: list[str] = []
        try:
            content = getattr(entry, "get_content", lambda **_: None)(prefer_summary=False)
            if content and getattr(content, "value", None) and getattr(content, "is_html", False):
                html_candidates.append(str(content.value))
        except Exception:
            pass
        summary = getattr(entry, "summary", None)
        if isinstance(summary, str) and summary.strip():
            html_candidates.append(summary)

        prepared: list[str] = []
        for html_candidate in html_candidates:
            if feed_url:
                html_candidate = self._strip_feed_injected_blocks(html_candidate, feed_url)
            prepared.append(self._bbcode_img_to_html(html_candidate))
        return prepared

    def extract_inline_svg_thumb_url(self, entry: object) -> str | None:
        """Return a sanitized inline-``<svg>`` data URI from the entry content, or None.

        Last-resort thumbnail/lead source for feeds that express an article icon
        as raw inline SVG. Bypasses the cache; no HTTP requests."""
        feed_url = str(getattr(entry, "feed_url", "") or "")
        for html_candidate in self._prepared_content_html(entry, feed_url):
            uri = self._extract_inline_svg_data_uri(html_candidate)
            if uri:
                return uri
        return None

    def _extract_inline_svg_data_uri(self, html_text: str) -> str | None:
        """Return a sanitized ``data:image/svg+xml`` URI for the first usable inline
        ``<svg>`` in ``html_text``, or None. Scripts/handlers/external refs are
        stripped; see :mod:`services.svg_sanitize`."""
        if not html_text or "<svg" not in html_text.lower():
            return None
        for m in self._INLINE_SVG_RE.finditer(html_text):
            uri = svg_sanitize.svg_to_data_uri(m.group(0))
            if uri:
                return uri
        return None

    def extract_media_rss_thumb_url(self, entry: object) -> str | None:
        """Return a media:thumbnail or image-type media:content URL from the entry's RSS fields.

        Intentionally bypasses the lead-image cache. No HTTP requests; only fields
        already stored in the reader DB are used.
        """
        entry_link = str(getattr(entry, "link", "") or "")
        try:
            media_thumb = getattr(entry, "media_thumbnail", None)
            if media_thumb:
                candidates = media_thumb if isinstance(media_thumb, (list, tuple)) else [media_thumb]
                for item in candidates:
                    url = None
                    if isinstance(item, dict):
                        url = item.get("url") or item.get("href")
                    elif isinstance(item, str):
                        url = item
                    if url and self._is_image_url_acceptable(url, None, None) and not self._should_bypass_cached_url(
                        entry_link=entry_link, cached_url=url
                    ):
                        return url
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
                elif not self._is_image_url_acceptable(cached, None, None, skip_logo_patterns=True):
                    pass  # cached URL now fails our filter (rules may have changed) — re-resolve
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
        skip_source = strategy_for_feed in ("inline", "artwork", "youtube")
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

        # Load stored strategy; skip YouTube and manually-locked none feeds entirely.
        strategy, detected_at, manual = self.get_feed_strategy(feed_url)
        need_redetect = not manual and (strategy == "unknown" or now - detected_at > self._STRATEGY_REDETECT_AFTER_SECONDS)
        if strategy == "youtube":
            return
        if strategy == "none" and manual:
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
                    elif not self._is_image_url_acceptable(cached, None, None, allow_extensionless=True):
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
                # Always persist the inline image so fast_only=True lookups at
                # render time find it in the cache without re-parsing the entry.
                self.store_entry_lead_image(feed_url_str, entry_id_str, inline)
                # For feeds manually locked to og_scrape, the source page is the
                # authoritative image source — fall through even when an inline
                # image exists (e.g. album cover) so we can find the real hero image.
                # Webcomic feeds behave the same way: the inline enclosure is only a
                # small thumbnail (e.g. /comicsthumbs/) with no hover text, while the
                # source page carries the full-resolution comic panel and its alt/title.
                if not ((strategy == "og_scrape" and manual) or strategy == "webcomic"):
                    continue

            entry_link = str(getattr(entry, "link", "") or "")
            if not entry_link:
                continue

            # Check feed-level media thumbnails (e.g. NYT's media:thumbnail) before
            # doing a source-page fetch.  These are unavailable on reader Entry objects
            # but are present in the raw RSS XML.  Webcomic feeds skip this entirely:
            # their RSS enclosure is the same small /comicsthumbs/ image, so we must
            # reach the source-page fetch below to get the full-resolution panel and
            # hover text — fetching the feed XML here would just be a wasted request.
            if strategy != "webcomic":
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
            # Exception: still try source as a fallback when inline extraction
            # found nothing (e.g. the artist omitted the image for this entry
            # or it's behind an age gate that doesn't include it in the feed).
            is_wc = strategy == "webcomic"
            if strategy in ("inline", "artwork") and not need_redetect:
                image_url = self._fetch_source_lead_image(entry_link, is_webcomic=is_wc)
                if image_url:
                    _found_og_scrape = True
                    self._maybe_store_alt_from_cache(feed_url_str, entry_id_str, entry_link, image_url, is_webcomic=is_wc)
                self.store_entry_lead_image(feed_url_str, entry_id_str, image_url)
                time.sleep(0.15)
                continue
            image_url = self._fetch_source_lead_image(entry_link, is_webcomic=is_wc)
            if image_url:
                _found_og_scrape = True
                self._maybe_store_alt_from_cache(feed_url_str, entry_id_str, entry_link, image_url, is_webcomic=is_wc)
                self.store_entry_lead_image(feed_url_str, entry_id_str, image_url)
            elif not inline:
                # Source found nothing and there was no inline image — record the
                # negative result. But when we fell through here from an og_scrape
                # manual feed that DID have an inline image (stored above), a
                # transient source miss must NOT overwrite that good image with
                # None — otherwise brand-new posts (whose og:image isn't generated
                # yet at first fetch) lose their thumbnail until the 4h retry.
                self.store_entry_lead_image(feed_url_str, entry_id_str, None)
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

                # Only use feed media thumbnails when not explicitly locked to og_scrape.
                if strategy != "og_scrape":
                    feed_thumb = feed_media.get(entry_link)
                    if feed_thumb:
                        self.store_entry_lead_image(feed_url, entry_id, feed_thumb)
                        time.sleep(0.05)
                        continue

                # For inline/enclosure/none-classified feeds, source scraping won't help.
                if strategy in ("inline", "artwork", "none", "enclosure"):
                    continue

                is_wc = strategy == "webcomic" or self._is_feed_webcomic(feed_url)
                image_url = self._fetch_source_lead_image(entry_link, is_webcomic=is_wc)
                if not image_url:
                    # Source page yielded nothing — e.g. a JS-only art portfolio
                    # (ArtStation) with no og:image, or a feed whose strategy was
                    # mis-detected. Fall back to the entry's own inline feed-content
                    # image instead of caching a blank thumbnail.
                    image_url = self._inline_from_reader(feed_url, entry_id)
                if image_url:
                    self._maybe_store_alt_from_cache(feed_url, entry_id, entry_link, image_url, is_webcomic=is_wc)
                self.store_entry_lead_image(feed_url, entry_id, image_url)
                time.sleep(0.15)

    def _inline_from_reader(self, feed_url: str, entry_id: str) -> str | None:
        """Best-effort inline lead image from an entry's stored feed content.

        Fallback for the chunk backfill when source-page scraping yields nothing:
        some feeds (art portfolios like ArtStation) embed the image directly in
        the feed but live on JS-only pages with no og:image, so the source fetch
        returns None even though the feed itself carries a perfectly good image.
        """
        try:
            with self._get_reader() as reader:
                entry = reader.get_entry((feed_url, entry_id))
        except Exception:
            return None
        if entry is None:
            return None
        try:
            return self.extract_entry_thumbnail_url(entry, include_source_lookup=False)
        except Exception:
            return None

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

    def _extract_first_image_url_from_html(self, html_text: str, base_url: str, source_url: str | None = None, allow_extensionless: bool = False) -> str | None:
        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
                if key and value:
                    attrs[key] = value

            # Images with percentage-based height (e.g. height="60%") are
            # decorative dividers or CSS-sized banners, not article images.
            if attrs.get("height", "").strip().endswith("%"):
                continue
            # Skip images whose alt/title text flags them as advertisements
            # (e.g. SE Radio's "banner ad that says ...").
            _alt_title = f"{attrs.get('alt', '')} {attrs.get('title', '')}"
            if _alt_title.strip() and self._AD_ALT_PATTERNS.search(_alt_title):
                continue
            for image_url in self._collect_img_candidate_urls(attrs, source_url=source_url):
                if not image_url or image_url.startswith("data:"):
                    continue
                resolved = urljoin(base_url, image_url)
                if source_url and not self._is_source_image_tag_acceptable(attrs, resolved):
                    continue
                if self._is_image_url_acceptable(resolved, None, None, allow_extensionless=allow_extensionless, source_url=base_url):
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
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
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

        # Percentage-based height (e.g. height="60%") marks decorative dividers
        # or banner images sized by CSS — not article content images.
        raw_height = attrs.get("height", "").strip()
        if raw_height.endswith("%"):
            return False

        width_attr = self._parse_positive_int_attr(attrs, "width")
        height_attr = self._parse_positive_int_attr(attrs, "height")

        # Explicit tiny dimensions (e.g. width="1" height="1") → tracking/spacer pixel.
        if width_attr is not None and height_attr is not None:
            if width_attr <= 10 and height_attr <= 10:
                return False

        # Reject images whose alt or title text explicitly calls them a logo/icon,
        # even when the URL path doesn't contain those terms — but only when the
        # image lacks explicit qualifying dimensions. An img with declared
        # width/height >= minimums is intentional article content (e.g. "imdb logo"
        # in an article about IMDB piracy); site chrome logos typically have no
        # explicit dimensions or carry them in the URL instead.
        alt_title = f"{attrs.get('alt', '')} {attrs.get('title', '')}".strip()
        # Advertisement banners flag themselves in alt/title text — never article content.
        if alt_title and self._AD_ALT_PATTERNS.search(alt_title):
            return False
        if alt_title and self._LOGO_URL_PATTERNS.search(alt_title):
            _has_qualifying_dims = (
                width_attr is not None and width_attr >= self._LEAD_IMAGE_MIN_WIDTH
                and height_attr is not None and height_attr >= self._LEAD_IMAGE_MIN_HEIGHT
            )
            if not _has_qualifying_dims:
                return False
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

    def _score_source_image_tag(self, attrs: dict[str, str], resolved_url: str, source_url: str, is_webcomic: bool = False) -> int:
        score = 0

        if is_webcomic:
            img_id = (attrs.get("id") or "").strip()
            if self._WEBCOMIC_IMG_ID_RE.fullmatch(img_id):
                score += 200
            img_class = (attrs.get("class") or "").strip()
            if self._WEBCOMIC_IMG_CLASS_RE.search(img_class):
                score += 80

        class_attr = (attrs.get("class") or "").lower()
        alt_attr = (attrs.get("alt") or "").strip()
        # Fall back to title if alt is empty — some CMS themes use title as the
        # image caption and leave alt blank (e.g. Hugo/Hexo static sites).
        if not alt_attr:
            alt_attr = (attrs.get("title") or "").strip()

        if "hero-image" in class_attr:
            score += 120
        if "hero" in class_attr:
            score += 40
        if any(token in class_attr for token in ("featured", "lead", "article-image", "main-image", "entry-image")):
            score += 30
        if any(token in class_attr for token in ("topic_icon", "topic-icon", "category-icon", "tag-icon")):
            score += 60
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

        # URL path contains keywords typical of article hero/cover images.
        _url_path = resolved_url.lower()
        if any(kw in _url_path for kw in ("/banner", "-banner", "_banner", "/hero", "-hero", "_hero",
                                            "/cover", "-cover", "_cover", "/featured", "-featured",
                                            "_featured", "/thumbnail", "-thumbnail")):
            score += 15

        score += self._plugin_source_score_adjustment(source_url=source_url, attrs=attrs, resolved_url=resolved_url)

        return score

    def _extract_webcomic_panel_image(self, html_text: str, base_url: str, source_url: str) -> str | None:
        """Return the main comic-panel image for a webcomic source page, or None.

        Scans for an <img> whose id/class marks it as the comic panel (e.g.
        ComicControl's id="cc-comic", SMBC's, etc.). This is the webcomic's lead
        image and must outrank og:image, which on many webcomic CMSes is a single
        generic site banner repeated on every page.
        """
        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
                if key and value:
                    attrs[key] = value
            img_id = (attrs.get("id") or "").strip()
            img_class = (attrs.get("class") or "").strip()
            if not (self._WEBCOMIC_IMG_ID_RE.fullmatch(img_id) or self._WEBCOMIC_IMG_CLASS_RE.search(img_class)):
                continue
            for image_url in self._collect_img_candidate_urls(attrs, source_url=source_url):
                if not image_url or image_url.startswith("data:"):
                    continue
                resolved = urljoin(base_url, image_url)
                if urlparse(resolved).path.lower().endswith(".svg"):
                    continue
                if self._is_image_url_acceptable(resolved, None, None, allow_extensionless=True, source_url=source_url):
                    return resolved
        return None

    def _extract_preferred_source_image_url(self, html_text: str, base_url: str, source_url: str, is_webcomic: bool = False) -> str | None:
        url, _ = self._extract_preferred_source_image_data(html_text, base_url, source_url, is_webcomic=is_webcomic)
        return url

    def _extract_preferred_source_image_data(self, html_text: str, base_url: str, source_url: str, is_webcomic: bool = False) -> tuple[str | None, str | None]:
        """Like _extract_preferred_source_image_url but also returns the winning img's alt text."""
        # Drop related/recent-post containers so a sibling post's thumbnail can't
        # win when the article itself has no og:image or hero image of its own.
        html_text = self._strip_related_post_blocks(html_text)
        best_url: str | None = None
        best_alt: str | None = None
        best_score = -1
        _found_first = False  # tracks whether the first valid candidate has been scored

        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            # Skip images inside author/speaker/bio sections — they are headshots.
            # Skip images inside site-chrome branding elements (logo, nav header).
            context_before = html_text[max(0, tag_match.start() - 500):tag_match.start()]
            _am = self._AUTHOR_CONTEXT_RE.search(context_before)
            if _am:
                # If the matched element was an <address> that closed before reaching
                # this img, the img is in a sibling element — don't skip it.
                # (e.g. <address class="article-author">...</address> followed by
                # <figure><img .../></figure> on the same page.)
                _tag_start = context_before.rfind('<', 0, _am.start())
                _in_address = (
                    _tag_start != -1
                    and context_before[_tag_start:_tag_start + 8].lower().startswith('<address')
                )
                _after = context_before[_am.end():]
                if not (_in_address and re.search(r'</address\b', _after, re.IGNORECASE)):
                    continue
            if self._SITE_CHROME_CONTEXT_RE.search(context_before):
                continue
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
                if key and value:
                    attrs[key] = value

            for image_url in self._collect_img_candidate_urls(attrs, source_url=source_url):
                if not image_url or image_url.startswith("data:"):
                    continue
                resolved = urljoin(base_url, image_url)
                if not self._is_source_image_tag_acceptable(attrs, resolved):
                    continue
                if not self._is_image_url_acceptable(resolved, None, None, allow_extensionless=True, source_url=source_url):
                    continue
                # SVG files are icons/logos/diagrams — not photographic article lead images.
                # They slip through allow_extensionless=True because .svg is not a raster format.
                _resolved_path = urlparse(resolved).path.lower()
                if _resolved_path.endswith(".svg"):
                    continue

                # Prefer <source type="image/webp"> from an enclosing <picture> element.
                # The webp source is the browser's preferred format for this image and
                # often carries a larger srcset than the fallback <img src>.
                _pre_ctx = html_text[max(0, tag_match.start() - 600):tag_match.start()]
                _pic_pos = _pre_ctx.rfind("<picture")
                if _pic_pos != -1:
                    _wm = self._WEBP_SOURCE_SRCSET_RE.search(_pre_ctx[_pic_pos:])
                    if _wm:
                        _wsrcset = _wm.group(1) or _wm.group(2)
                        for _wu in self._parse_srcset_urls_descending(_wsrcset):
                            if not _wu or _wu.startswith("data:"):
                                continue
                            _wr = urljoin(base_url, _wu)
                            if self._is_image_url_acceptable(_wr, None, None, allow_extensionless=True):
                                resolved = _wr
                                break

                score = self._score_source_image_tag(attrs, resolved, source_url, is_webcomic=is_webcomic)
                # First valid image in the document gets a position bonus — publishers
                # typically place the primary article image first.
                if not _found_first:
                    score += 10
                    _found_first = True
                if score > best_score:
                    best_score = score
                    best_url = resolved
                    _alt = (attrs.get("alt") or attrs.get("title") or "").strip()
                    best_alt = _alt if _alt else None

        if best_url and best_score >= 0:
            return best_url, best_alt
        return None, None

    def _extract_css_background_image_url(self, html_text: str, base_url: str) -> str | None:
        """Return the first acceptable CSS background-image URL found in inline style attributes."""
        for m in self._CSS_BG_IMAGE_RE.finditer(html_text):
            raw = m.group(1).strip()
            if not raw or raw.startswith("data:"):
                continue
            resolved = urljoin(base_url, raw)
            if self._is_image_url_acceptable(resolved, None, None):
                return resolved
        return None

    def _extract_preloaded_image_url(self, html_text: str, base_url: str) -> str | None:
        for tag_match in self._LINK_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
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

    def _is_image_url_acceptable(self, image_url: str, width: int | None, height: int | None, *, allow_extensionless: bool = False, skip_logo_patterns: bool = False, source_url: str | None = None) -> bool:
        # Sanitized inline-SVG data URIs (from services.svg_sanitize, e.g. a
        # per-feed plugin's hero SVG) are trusted and carry no remote host to vet.
        if image_url.startswith("data:image/svg+xml,"):
            return True
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        # DeviantArt's image CDN (wixmp) serves authoritative deviation images via
        # long auto-generated filenames/UUIDs that trip the junk/avatar/ad heuristics
        # with false positives (e.g. "…profile…" in a title, "ad87" in a UUID). We
        # only ever pass it API-provided content images, so trust the host.
        _nl = parsed.netloc.lower()
        if _nl == "wixmp.com" or _nl.endswith(".wixmp.com"):
            return True
        # An image hosted under the post's own URL directory is the post's own
        # asset, not site chrome — so a content hero named "…-logo.png" (e.g. a
        # product logo that IS the article image) must not be dropped by the
        # logo filter. Site logos live at the site root or on a shared CDN, not
        # under a specific post path, so this stays narrow.
        if source_url and not skip_logo_patterns:
            try:
                _su = urlparse(source_url)
                _su_dir = _su.path if _su.path.endswith("/") else _su.path.rsplit("/", 1)[0] + "/"
                if _su.netloc == parsed.netloc and len(_su_dir) > 1 and parsed.path.startswith(_su_dir):
                    skip_logo_patterns = True
            except ValueError:
                pass
        if (self._TRACKER_URL_PATTERNS.search(parsed.netloc)
                or self._TRACKER_URL_PATTERNS.search(parsed.path)
                or self._TRACKER_URL_PATTERNS.search(image_url)):
            return False
        # Match host+path only (not the query string) so a non-emoji asset with
        # e.g. "?ref=twemoji" in its query isn't mistaken for an emoji sprite.
        if self._EMOJI_URL_PATTERNS.search(parsed.netloc + parsed.path):
            return False
        if self._AVATAR_HINT_PATTERNS.search(parsed.path):
            return False
        if parsed.netloc.lower() in self._FORGE_AVATAR_HOSTS and self._FORGE_AVATAR_PATH_RE.match(parsed.path or ""):
            return False
        if self._SITE_CHROME_PATH_PATTERNS.search(parsed.path):
            return False
        if self._SITE_CHROME_DOMAIN_PATTERNS.search(parsed.netloc):
            return False
        _path_no_qs = parsed.path.lower()
        for _m in self._TINY_DIM_RE.finditer(_path_no_qs):
            try:
                if int(_m.group(1)) <= 10 and int(_m.group(2)) <= 10:
                    return False
            except ValueError:
                pass

        if not skip_logo_patterns and self._LOGO_URL_PATTERNS.search(image_url):
            # Allow logo-pattern URLs when the path encodes a large enough dimension —
            # publisher-sized content images are valid even when "logo" appears in the name.
            _lp_path = image_url.split("?")[0]
            _lp_has_large_dims = False
            # NxN pattern (e.g. "750x476"). Extreme aspect ratios (wider than 4:1
            # or taller than 1:4) are wordmark/banner logos, not article content —
            # e.g. SE Radio's "logo-color-600x100" (6:1) or "site-logo-200x1500"
            # (1:7.5).  Require both a large dimension and a content-like ratio.
            for _m in self._URL_DIMENSION_RE.finditer(_lp_path):
                try:
                    _lpw, _lph = int(_m.group(1)), int(_m.group(2))
                    if (
                        _lpw >= self._LEAD_IMAGE_MIN_WIDTH
                        and _lph >= self._LEAD_IMAGE_MIN_HEIGHT
                        and 0.25 <= _lpw / _lph <= 4.0
                    ):
                        _lp_has_large_dims = True
                        break
                except (ValueError, ZeroDivisionError):
                    pass
            # Width-only hint (e.g. "1000w") — WordPress responsive-image naming
            if not _lp_has_large_dims:
                for _m in self._URL_WIDTH_HINT_RE.finditer(_lp_path):
                    try:
                        if int(_m.group(1)) >= self._LEAD_IMAGE_MIN_WIDTH:
                            _lp_has_large_dims = True
                            break
                    except ValueError:
                        pass
            # WordPress ?fit=W%2CH (URL-encoded comma) and w=/width= query-string hints
            if not _lp_has_large_dims:
                _lp_qs = parsed.query
                for _m in re.finditer(r"(?:^|&)fit=([0-9]+)(?:%2[Cc]|,)([0-9]+)(?:&|$)", _lp_qs, re.IGNORECASE):
                    try:
                        if int(_m.group(1)) >= self._LEAD_IMAGE_MIN_WIDTH and int(_m.group(2)) >= self._LEAD_IMAGE_MIN_HEIGHT:
                            _lp_has_large_dims = True
                            break
                    except ValueError:
                        pass
            if not _lp_has_large_dims:
                for _m in re.finditer(r"(?:^|&)(?:w|width)=([0-9]{1,4})(?:&|$)", parsed.query, re.IGNORECASE):
                    try:
                        if int(_m.group(1)) >= self._LEAD_IMAGE_MIN_WIDTH:
                            _lp_has_large_dims = True
                            break
                    except ValueError:
                        pass
            if not _lp_has_large_dims:
                return False
        if self._PLACEHOLDER_URL_PATTERNS.search(image_url):
            return False
        # Advertisement images (e.g. .../Cert-ad1.png) are never article content.
        # Checked against the path so query strings can't introduce false matches.
        if self._AD_URL_PATTERNS.search(parsed.path):
            return False

        path = parsed.path.lower()
        query = parsed.query.lower()
        looks_like_image_url = bool(self._IMAGE_PATH_SUFFIX_RE.search(path))
        has_image_hint_in_query = any(marker in query for marker in ("format=", "fm=", "image=", "img=", "ext="))
        if not looks_like_image_url and not has_image_hint_in_query and width is None and height is None:
            if not allow_extensionless:
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
            # Jetpack/WP CDN: resize=W,H (or resize=W%2CH) specifies the exact
            # served dimensions — they override filename-embedded dimensions.
            for _rm in re.finditer(r"(?:^|&)resize=([0-9]+)(?:%2[Cc]|,)([0-9]+)(?:&|$)", query, re.IGNORECASE):
                try:
                    _rw, _rh = int(_rm.group(1)), int(_rm.group(2))
                    if _rw < self._LEAD_IMAGE_MIN_WIDTH or _rh < self._LEAD_IMAGE_MIN_HEIGHT:
                        return False
                except ValueError:
                    pass
                break

        if width is None or height is None:
            url_path_no_query = image_url.split("?")[0]
            # Skip URL-dimension filtering for paths that are specifically
            # content/download thumbnail directories — their small dimensions
            # are intentional and they represent the article's primary image.
            _is_download_thumb = bool(re.search(
                r'/(?:download|file|product|entry)?thumbs?(?:nail)?s?/', url_path_no_query, re.IGNORECASE
            ))
            _url_has_large_dim = False
            _url_has_small_dim = False
            for m in self._URL_DIMENSION_RE.finditer(url_path_no_query):
                try:
                    w, h = int(m.group(1)), int(m.group(2))
                    if w >= self._LEAD_IMAGE_MIN_WIDTH and h >= self._LEAD_IMAGE_MIN_HEIGHT:
                        _url_has_large_dim = True
                    elif not _is_download_thumb and (w < self._LEAD_IMAGE_MIN_WIDTH or h < self._LEAD_IMAGE_MIN_HEIGHT):
                        _url_has_small_dim = True
                except ValueError:
                    pass
            # Only reject for small dims when no large dim exists to counteract —
            # e.g. "16x9" format markers don't disqualify "image-1200x675.jpg".
            if _url_has_small_dim and not _url_has_large_dim:
                return False
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

    # Minimum for og:image — matches _LEAD_IMAGE_MIN_WIDTH so that square app
    # icons (e.g. 200×200 declared via og:image:width/height) are not blocked.
    _OG_IMAGE_MIN_WIDTH = 200
    _OG_IMAGE_MIN_HEIGHT = 150

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
            if not self._is_image_url_acceptable(resolved, og_width, og_height, allow_extensionless=True, skip_logo_patterns=True):
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
                except (ValueError, TypeError):
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
                    except (ValueError, TypeError):
                        w = h = 0
                    if w and h and (w < self._LEAD_IMAGE_MIN_WIDTH or h < self._LEAD_IMAGE_MIN_HEIGHT):
                        continue
                    area = w * h
                    if area > best_area:
                        best_area = area
                        best_url = url

            # <enclosure> — feedparser stores these as fp_entry.enclosures (list of dicts
            # with "url", "type", optional "length").  Used by feeds like Invisible Oranges
            # that attach image enclosures instead of media:thumbnail / media:content.
            if best_url is None:
                for enc in getattr(fp_entry, "enclosures", []) or []:
                    if not isinstance(enc, dict):
                        continue
                    url = enc.get("url") or enc.get("href") or ""
                    etype = (enc.get("type") or "").lower()
                    if url and "image" in etype and self._is_image_url_acceptable(url, None, None):
                        best_url = url
                        break

            if best_url and not self._should_bypass_cached_url(entry_link=link, cached_url=best_url):
                result[link] = best_url

        return result

    _JS_COOKIE_CHALLENGE_RE: re.Pattern[str] = re.compile(
        r'document\.cookie\s*=\s*["\']([^"\'=]+=[^"\']+)["\']',
        re.IGNORECASE,
    )

    def _fetch_page_html(self, url: str) -> tuple[str, str, bool] | None:
        """Fetch a page, handling JS cookie challenges (e.g. BlueHost humans_XXXXX).

        Returns (html, final_url, corp_restricted) or None on failure.
        corp_restricted is True when the response has Cross-Origin-Resource-Policy:
        same-site or same-origin, meaning browsers will block cross-origin image loads
        from this domain and images should not be used as lead-image candidates.
        Falls back to urllib when the server disconnects on httpx (e.g. Tumblr
        rejects httpx's TLS fingerprint but accepts stdlib connections).
        """
        _use_urllib = False
        _corp_restricted = False
        try:
            # follow_redirects=False so url_guard.safe_get validates every hop (SSRF).
            with httpx.Client(follow_redirects=False, timeout=15.0, headers={"User-Agent": self._user_agent}) as client:
                response = url_guard.safe_get(client, url)
                if response.status_code == 409:
                    m = self._JS_COOKIE_CHALLENGE_RE.search(response.text)
                    if m:
                        cookie_str = m.group(1)
                        if "=" in cookie_str:
                            cname, cval = cookie_str.split("=", 1)
                            parsed_host = urlparse(url)
                            domain = parsed_host.netloc.lstrip("www.")
                            client.cookies.set(cname.strip(), cval.strip(), domain=domain)
                        response = url_guard.safe_get(client, url)
                response.raise_for_status()
                _corp = response.headers.get("cross-origin-resource-policy", "").lower()
                _corp_restricted = _corp in ("same-site", "same-origin")
        except httpx.RemoteProtocolError:
            _use_urllib = True
        except Exception:
            return None

        if _use_urllib:
            try:
                import urllib.request as _ureq
                _req = _ureq.Request(url, headers={"User-Agent": self._user_agent})
                with _ureq.urlopen(_req, timeout=10) as _resp:
                    _html = _resp.read().decode("utf-8", errors="replace")
                    _final = _resp.url
                    _corp = _resp.headers.get("cross-origin-resource-policy", "").lower()
                    _corp_restricted = _corp in ("same-site", "same-origin")
                return _html, _final, _corp_restricted
            except Exception:
                return None

        return response.text, str(response.url), _corp_restricted

    def _is_image_url_fetchable(
        self, image_url: str, domain_cache: dict[str, bool] | None = None
    ) -> bool:
        """Return True if a server-side HEAD request to image_url succeeds (HTTP < 400).

        Uses an optional per-call domain_cache dict so that all images from the
        same CDN domain share a single HEAD result within one background job run.
        """
        domain = urlparse(image_url).netloc
        if domain_cache is not None and domain in domain_cache:
            return domain_cache[domain]
        try:
            resp = url_guard.safe_head(
                image_url, timeout=4.0, headers={"User-Agent": self._user_agent}
            )
            ok = resp.status_code < 400
        except Exception:
            ok = False
        if domain_cache is not None:
            domain_cache[domain] = ok
        return ok

    # WordPress "Webcomic" plugin (mgsisk) renders the cartoonist's hover/secret
    # text in a hidden balloon: <div class="comic-alt-text"><p>…</p></div>.
    # The comic <img> itself carries no alt/title, so this is the only place the
    # joke lives in the page DOM.
    _WEBCOMIC_ALT_TEXT_RE = re.compile(
        r'<div[^>]*\bclass=["\'][^"\']*\bcomic-alt-text\b[^"\']*["\'][^>]*>(.*?)</div>',
        re.IGNORECASE | re.DOTALL,
    )
    _OG_DESCRIPTION_RE = re.compile(
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
        re.IGNORECASE,
    )

    def _extract_webcomic_alt_text(self, html_text: str) -> str | None:
        """Return the webcomic hover/secret text from a source page, or None.

        Looks first for the `comic-alt-text` balloon used by the WordPress
        Webcomic plugin, then the title/alt attribute on the main comic <img>
        (e.g. SMBC's <img id="cc-comic" title="...">), then falls back to the
        og:description meta tag.
        """
        m = self._WEBCOMIC_ALT_TEXT_RE.search(html_text)
        if m:
            text = re.sub(r"<[^>]+>", " ", m.group(1))
            text = html.unescape(text).strip()
            if text:
                return text
        # The hover-text punchline often lives in the title= (or alt=) attribute of
        # the main comic <img> itself, identified by a known webcomic id/class.
        for tag_match in self._IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                k = attr_match.group(1).strip().lower()
                v = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
                if k and v:
                    attrs[k] = v
            img_id = (attrs.get("id") or "").strip()
            img_class = (attrs.get("class") or "").strip()
            if not (self._WEBCOMIC_IMG_ID_RE.fullmatch(img_id) or self._WEBCOMIC_IMG_CLASS_RE.search(img_class)):
                continue
            text = (attrs.get("title") or attrs.get("alt") or "").strip()
            if text:
                return text
        m = self._OG_DESCRIPTION_RE.search(html_text)
        if m:
            text = html.unescape(m.group(1)).strip()
            if text:
                return text
        return None

    def fetch_entry_image_caption(
        self, entry_link: str, lead_image_url: str | None = None, is_webcomic: bool = False
    ) -> tuple[str | None, str | None]:
        """Return (alt, title) attribute text separately for the lead image on the source page.

        Uses the same HTML cache and scanning logic as fetch_entry_image_alt, but returns
        the raw alt and title attributes independently instead of combining them.
        When lead_image_url is provided, scans for that specific image URL.
        When is_webcomic is set and the image carries no alt/title, falls back to the
        webcomic hover-text balloon / og:description so the joke still surfaces.
        Returns (None, None) if the image is not found or the page cannot be fetched.
        """
        import re as _re

        _strip = lambda s: _re.sub(r"<[^>]+>", "", s).strip() or None if s else None

        def _finalize(alt: str | None, title: str | None, page_html: str | None) -> tuple[str | None, str | None]:
            # Webcomic hover text lives in a balloon element / og:description, not the
            # <img>.  When the image carries no alt/title, surface that text as the title.
            if is_webcomic and not alt and not title and page_html:
                fallback = self._extract_webcomic_alt_text(page_html)
                if fallback:
                    return alt, fallback
            return alt, title

        # Deathbulge SPA: API returns combined alt_text only; return as title, no alt.
        _db_spa_m = re.match(r'https?://(?:www\.)?deathbulge\.com/#/comics/(\d+)', entry_link)
        if _db_spa_m:
            try:
                import json as _json
                import urllib.request as _ureq
                _api_url = f"http://deathbulge.com/api/comics/{_db_spa_m.group(1)}"
                _req = _ureq.Request(_api_url, headers={"User-Agent": self._user_agent})
                with _ureq.urlopen(_req, timeout=10) as _resp:
                    _api_data = _json.load(_resp)
                _text = (_api_data.get("comic", {}).get("alt_text") or "").strip() or None
                return (None, _text)
            except Exception:
                pass
            return (None, None)

        cached = self._source_html_cache.get(entry_link)
        if cached is None:
            if not is_safe_outbound_url(entry_link):
                return (None, None)
            result = self._fetch_page_html(entry_link)
            if result is None:
                return (None, None)
            source_html, final_url, _corp = result
            self._source_html_cache[entry_link] = (final_url, source_html)
            self._source_html_cache.move_to_end(entry_link)
            if len(self._source_html_cache) > self._SOURCE_HTML_CACHE_MAX:
                self._source_html_cache.popitem(last=False)
        else:
            final_url, source_html = cached

        if lead_image_url:
            for tag_match in self._IMG_TAG_RE.finditer(source_html):
                tag = tag_match.group(0)
                attrs: dict[str, str] = {}
                for attr_match in self._IMG_ATTR_RE.finditer(tag):
                    k = attr_match.group(1).strip().lower()
                    v = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
                    if k and v:
                        attrs[k] = v
                for image_url in self._collect_img_candidate_urls(attrs):
                    if not image_url or image_url.startswith("data:"):
                        continue
                    resolved = urljoin(final_url, image_url)
                    if self._urls_equivalent(resolved, lead_image_url):
                        return _finalize(
                            _strip(attrs.get("alt", "")),
                            _strip(attrs.get("title", "")),
                            source_html,
                        )
            # Fallback: lead_image_url may be a WebP srcset URL substituted by
            # _extract_preferred_source_image_data when a <picture>/<source
            # type="image/webp"> element wraps an <img> tag.  The <img src> holds
            # the JPEG/PNG fallback, which won't match above.  Scan for <picture>
            # elements whose WebP srcset contains lead_image_url and return the
            # enclosed <img>'s alt/title.
            for tag_match in self._IMG_TAG_RE.finditer(source_html):
                pre_ctx = source_html[max(0, tag_match.start() - 600):tag_match.start()]
                pic_pos = pre_ctx.rfind("<picture")
                if pic_pos == -1:
                    continue
                wm = self._WEBP_SOURCE_SRCSET_RE.search(pre_ctx[pic_pos:])
                if not wm:
                    continue
                wsrcset = wm.group(1) or wm.group(2)
                for wu in self._parse_srcset_urls_descending(wsrcset):
                    if not wu:
                        continue
                    wr = urljoin(final_url, wu)
                    if self._urls_equivalent(wr, lead_image_url):
                        tag = tag_match.group(0)
                        wb_attrs: dict[str, str] = {}
                        for am in self._IMG_ATTR_RE.finditer(tag):
                            k = am.group(1).strip().lower()
                            v = html.unescape((am.group(2) or am.group(3) or am.group(4) or "").strip())
                            if k and v:
                                wb_attrs[k] = v
                        return _finalize(
                            _strip(wb_attrs.get("alt", "")),
                            _strip(wb_attrs.get("title", "")),
                            source_html,
                        )
            return _finalize(None, None, source_html)

        # No specific URL: for webcomics, prefer the hover-text balloon over a scored
        # img title (which can pick up site-chrome banners). Otherwise use scored path.
        if is_webcomic:
            wc_text = self._extract_webcomic_alt_text(source_html)
            if wc_text:
                return (None, wc_text)
        _, alt_text = self._extract_preferred_source_image_data(source_html, final_url, entry_link)
        return _finalize(None, _strip(alt_text), source_html)

    def _maybe_store_alt_from_cache(
        self, feed_url: str, entry_id: str, entry_link: str, image_url: str, is_webcomic: bool = False
    ) -> None:
        """If source HTML is in cache and alt not yet stored, extract and persist alt/title.

        Called immediately after a source-page lead-image fetch so the caption text
        is available on first entry open without a second HTTP round-trip.
        """
        if (feed_url, entry_id) in self._alt_cache:
            return
        alt, title = self.fetch_entry_image_caption(
            entry_link, lead_image_url=image_url, is_webcomic=is_webcomic
        )
        self.store_entry_image_alt(feed_url, entry_id, alt, title_text=title)

    def _fetch_source_lead_image(self, entry_link: str, is_webcomic: bool = False) -> str | None:
        parsed = urlparse(entry_link)
        if parsed.scheme not in {"http", "https"}:
            return None
        if not is_safe_outbound_url(entry_link):
            return None

        result = self._fetch_page_html(entry_link)
        if result is None:
            return None

        source_html, final_url, corp_restricted = result
        if corp_restricted:
            # The server sent Cross-Origin-Resource-Policy: same-site/same-origin.
            # Browsers will block cross-origin image loads from this domain, so any
            # image URL we return would appear broken in the reader.
            return None
        # Cache for alt-text lookup without a second HTTP fetch.
        self._source_html_cache[entry_link] = (final_url, source_html)
        self._source_html_cache.move_to_end(entry_link)
        if len(self._source_html_cache) > self._SOURCE_HTML_CACHE_MAX:
            self._source_html_cache.popitem(last=False)

        # Webcomic feeds: the main comic panel (e.g. ComicControl's id="cc-comic")
        # is the lead image, and it takes priority over og:image. Many webcomic
        # CMSes set a single generic site banner as og:image on every page; with a
        # sane aspect ratio that banner would win the og:image early-return below
        # and the actual comic would never be considered. Resolve the panel first.
        if is_webcomic:
            panel_image = self._extract_webcomic_panel_image(source_html, final_url, entry_link)
            if panel_image:
                return panel_image

        # og:image is explicitly curated by the publisher for this article.
        og_width, og_height = self._extract_og_image_dimensions(source_html)
        meta_image = self._extract_meta_image_url_from_html(source_html, final_url)
        # Blogger CDN w{W}-h{H} crop URLs (used for og:image social cards) distort
        # square images into 16:9. Normalise to s1600 to display the full image.
        if meta_image:
            _bc = self._BLOGGER_CROP_RE.match(meta_image)
            if _bc:
                meta_image = f"{_bc.group(1)}/s1600/{_bc.group(2)}"
                og_width = og_height = None  # dimensions are for the cropped version
        # Filter out logo/tracker/avatar og:images early so all the return paths
        # below don't need individual checks.  skip_logo_patterns=False so that
        # site brand images like logo_opengraph.jpg are rejected unless they
        # declare large explicit dimensions (the logo-pattern safety-valve inside
        # _is_image_url_acceptable still allows e.g. logo-design-1200x630.jpg).
        if meta_image and not self._is_image_url_acceptable(
            meta_image, og_width, og_height, allow_extensionless=True, skip_logo_patterns=False
        ):
            meta_image = None

        _og_extreme_ratio = False
        if meta_image and og_width is not None and og_height is not None:
            _og_ratio = og_width / og_height if og_height else 1.0
            if 0.4 <= _og_ratio <= 2.5:
                # Publisher declared explicit dimensions — strong curation signal, trust it.
                return meta_image
            # Extreme aspect ratio (banner/screenshot) — fall through to body scan.
            _og_extreme_ratio = True

        preload_image = self._extract_preloaded_image_url(source_html, final_url)
        if preload_image:
            return preload_image

        css_bg_image = self._extract_css_background_image_url(source_html, final_url)

        # If OG image and CSS background-image agree on the same path, both
        # publisher curation signals point to the same image — trust it without
        # scanning body <img> tags (which may only contain nav/chrome icons).
        if meta_image and css_bg_image:
            if urlparse(meta_image).path == urlparse(css_bg_image).path:
                return meta_image

        preferred_image = self._extract_preferred_source_image_url(source_html, final_url, entry_link, is_webcomic=is_webcomic)

        if meta_image and preferred_image and preferred_image != meta_image:
            # og:image has no declared dimensions. If the preferred page image
            # appears before the og:image in the body HTML it is more likely
            # to be the article's primary visual (the og:image may be a later
            # image that the CMS happened to pick for the share preview).
            body_start = source_html.lower().find('<body')
            body_html = source_html[body_start:] if body_start != -1 else source_html
            meta_fname = meta_image.rstrip('/').split('/')[-1].split('?')[0]
            pref_fname = preferred_image.rstrip('/').split('/')[-1].split('?')[0]
            meta_body_pos = body_html.find(meta_fname)
            pref_body_pos = body_html.find(pref_fname)
            if pref_body_pos != -1 and meta_body_pos != -1 and pref_body_pos < meta_body_pos:
                return preferred_image

            # Astro/Vite hashed filenames use the same base stem with different
            # hash suffixes (e.g. foo.D9sM0Dvc_1b0SmR.png vs foo.D9sM0Dvc_1NFnGy.png).
            # The exact filename lookup fails, but the stem before the first dot
            # still matches — confirming the OG is article-specific.
            meta_stem = meta_fname.split('.')[0]
            if len(meta_stem) >= 10 and meta_stem.lower() in body_html.lower():
                return meta_image

        # At this point no strong inline signal (position comparison or Astro stem
        # match) favoured preferred_image over the publisher-curated og:image.
        # Default to og:image — it is the publisher's explicit designation for this
        # article.  preferred_image (first large body image) is a fallback only when
        # og:image is absent.
        # Do NOT call _extract_linked_image_url_from_html on source pages:
        # full-page HTML contains <link rel="apple-touch-icon|icon"> in <head>
        # which would be found as hrefs and mistakenly used as lead images.
        # That method is appropriate only for feed-content HTML snippets.
        # When there's no og:image and a CSS background appears before the body-scanner
        # winner in the page HTML, the publisher deliberately styled it as the article
        # visual (e.g. inside <header class="detail-view-header">).  Prefer it — but
        # try to promote to the full-resolution <img> variant when the css_bg is a
        # responsive-resized crop (e.g. hero-576x324.jpg → find hero-616x347.jpg or hero.jpg).
        if not meta_image and css_bg_image and preferred_image and css_bg_image != preferred_image:
            _body_start = source_html.lower().find('<body')
            _bh = source_html[_body_start:] if _body_start != -1 else source_html
            _css_fname = css_bg_image.rstrip('/').split('/')[-1].split('?')[0]
            _pref_fname = preferred_image.rstrip('/').split('/')[-1].split('?')[0]
            _css_pos = _bh.lower().find(_css_fname.lower())
            _pref_pos = _bh.lower().find(_pref_fname.lower())
            if _css_pos != -1 and (_pref_pos == -1 or _css_pos < _pref_pos):
                # Strip responsive size suffix (e.g. -576x324) from filename stem.
                _css_stem = re.sub(r'-\d{2,4}x\d{2,4}$', '', _css_fname.rsplit('.', 1)[0])
                if len(_css_stem) >= 6:
                    _full_re = re.compile(
                        r'(?:data-)?src=["\']([^"\']*' + re.escape(_css_stem) + r'[^"\']*\.[a-zA-Z]{2,5})["\']',
                        re.IGNORECASE,
                    )
                    _m = _full_re.search(_bh)
                    if _m:
                        _candidate = urljoin(final_url, _m.group(1))
                        if self._is_image_url_acceptable(_candidate, None, None, allow_extensionless=False):
                            return _candidate
                return css_bg_image

        if _og_extreme_ratio:
            return preferred_image or meta_image or css_bg_image
        return meta_image or preferred_image or css_bg_image

    def queue_source_fetch(self, feed_url: str, entry_id: str, entry_link: str) -> None:
        """Fetch the source-page lead image in a background thread and persist it.

        Returns immediately. Deduplicates: if a fetch for this entry is already
        in flight, the call is a no-op.  Callers that need to wait for completion
        can call wait_for_source_fetch() after this returns.
        """
        key = (feed_url, entry_id)
        if key in self._source_fetch_in_progress:
            return
        self._source_fetch_in_progress.add(key)
        event = threading.Event()
        self._source_fetch_events[key] = event
        is_wc = self._is_feed_webcomic(feed_url)
        # store_entry_lead_image writes through the context-bound meta connection;
        # this bare thread won't inherit the request's tenancy user, so capture it
        # and re-bind inside _bg or the image lands in the default tenant's DB.
        uid = tenancy.current_user_id()

        def _bg() -> None:
            try:
                with tenancy.user_context(uid):
                    image_url = self._fetch_source_lead_image(entry_link, is_webcomic=is_wc)
                    self.store_entry_lead_image(feed_url, entry_id, image_url)
                    # HTML is now in _source_html_cache from the lead-image fetch.
                    # Extract and persist alt text while we have it — no second HTTP fetch.
                    if image_url:
                        self._maybe_store_alt_from_cache(feed_url, entry_id, entry_link, image_url, is_webcomic=is_wc)
            except Exception:
                pass
            finally:
                self._source_fetch_in_progress.discard(key)
                event.set()
                self._source_fetch_events.pop(key, None)

        threading.Thread(target=_bg, daemon=True).start()

    def wait_for_source_fetch(self, feed_url: str, entry_id: str, timeout: float = 3.0) -> bool:
        """Block until the in-flight queue_source_fetch for this entry finishes (or timeout).

        Returns True if the fetch completed within the timeout, False otherwise.
        If no fetch is in progress, returns True immediately.
        """
        event = self._source_fetch_events.get((feed_url, entry_id))
        if event is None:
            return True
        return event.wait(timeout=timeout)

    def queue_source_html_fetch(
        self,
        entry_link: str,
        feed_url: str | None = None,
        entry_id: str | None = None,
        lead_image_url: str | None = None,
    ) -> None:
        """Fetch the source-page HTML in a background thread.

        Primes _source_html_cache so fetch_entry_image_alt can run on the next
        render without blocking.  When feed_url, entry_id, and lead_image_url are
        provided, also extracts the image alt/title text and persists it to the DB
        so it survives server restarts.
        Returns immediately; deduplicates concurrent requests for the same URL.
        """
        already_cached = entry_link in self._source_html_cache
        alt_already_set = (feed_url and entry_id) and (feed_url, entry_id) in self._alt_cache
        if already_cached and alt_already_set:
            return
        html_key = ("__html__", entry_link)
        if html_key in self._source_fetch_in_progress:
            return
        self._source_fetch_in_progress.add(html_key)
        event = threading.Event()
        self._source_html_fetch_events[entry_link] = event
        # store_entry_image_alt writes through the context-bound meta connection;
        # capture the request's tenancy user so this bare thread re-binds it
        # rather than persisting alt text to the default tenant's DB.
        uid = tenancy.current_user_id()

        def _bg() -> None:
            try:
                with tenancy.user_context(uid):
                    result = self._fetch_page_html(entry_link)
                    if result:
                        source_html, final_url, _ = result
                        self._source_html_cache[entry_link] = (final_url, source_html)
                        self._source_html_cache.move_to_end(entry_link)
                        if len(self._source_html_cache) > self._SOURCE_HTML_CACHE_MAX:
                            self._source_html_cache.popitem(last=False)
                        if feed_url and entry_id and lead_image_url:
                            alt, title = self.fetch_entry_image_caption(
                                entry_link, lead_image_url=lead_image_url,
                                is_webcomic=self._is_feed_webcomic(feed_url),
                            )
                            self.store_entry_image_alt(feed_url, entry_id, alt, title_text=title)
            except Exception:
                pass
            finally:
                self._source_fetch_in_progress.discard(html_key)
                event.set()
                self._source_html_fetch_events.pop(entry_link, None)

        threading.Thread(target=_bg, daemon=True).start()

    def wait_for_source_html_fetch(self, entry_link: str, timeout: float = 3.0) -> bool:
        """Block until the in-flight queue_source_html_fetch for this entry finishes (or timeout).

        Returns True if the fetch completed within the timeout, False otherwise.
        If no fetch is in progress, returns True immediately.
        """
        event = self._source_html_fetch_events.get(entry_link)
        if event is None:
            return True
        return event.wait(timeout=timeout)

    def test_entry_strategies(self, entry: object) -> list[dict]:
        """Test each lead-image strategy against a single entry in isolation.

        Unlike extract_entry_thumbnail_url, each result only uses sources
        specific to that strategy. Used by the Properties panel Refresh button.

        Returns [{"strategy": str, "image_url": str|None, "image_alt": str|None,
                  "image_title": str|None, "error": str|None}].
        """
        entry_link = str(getattr(entry, "link", "") or "")
        feed_url = str(getattr(entry, "feed_url", "") or "")
        base_url = entry_link or feed_url
        results: list[dict] = []

        # Build feed content HTML candidates once; reused by inline and artwork.
        html_candidates: list[str] = []
        try:
            content = getattr(entry, "get_content", lambda **_: None)(prefer_summary=False)
            if content and getattr(content, "value", None) and getattr(content, "is_html", False):
                html_candidates.append(str(content.value))
        except Exception:
            pass
        summary = getattr(entry, "summary", None)
        if isinstance(summary, str) and summary.strip():
            html_candidates.append(summary)

        # --- inline: first <img> from feed content HTML only ---
        inline_url: str | None = None
        inline_alt: str | None = None
        inline_title: str | None = None
        inline_error: str | None = None
        try:
            for html_candidate in html_candidates:
                if feed_url:
                    html_candidate = self._strip_feed_injected_blocks(html_candidate, feed_url)
                html_candidate = self._bbcode_img_to_html(html_candidate)
                img = self._extract_first_image_url_from_html(html_candidate, base_url, allow_extensionless=True)
                if img:
                    inline_url = img
                    # Extract alt/title from the matching img tag in feed HTML.
                    for tag_match in self._IMG_TAG_RE.finditer(html_candidate):
                        tag = tag_match.group(0)
                        attrs: dict[str, str] = {}
                        for am in self._IMG_ATTR_RE.finditer(tag):
                            k = am.group(1).strip().lower()
                            v = html.unescape((am.group(2) or am.group(3) or am.group(4) or "").strip())
                            if k and v:
                                attrs[k] = v
                        for candidate_url in self._collect_img_candidate_urls(attrs):
                            if candidate_url and self._urls_equivalent(
                                urljoin(base_url, candidate_url), img
                            ):
                                inline_alt = (attrs.get("alt") or "").strip() or None
                                inline_title = (attrs.get("title") or "").strip() or None
                                break
                        if inline_alt is not None or inline_title is not None:
                            break
                    break
                linked = self._extract_linked_image_url_from_html(html_candidate, base_url)
                if linked:
                    inline_url = linked
                    break
            if not inline_url:
                for html_candidate in html_candidates:
                    logo = self._extract_logo_with_dimensions_from_feed(html_candidate, base_url)
                    if logo:
                        inline_url = logo
                        break
        except Exception as exc:
            inline_error = str(exc)
        results.append({
            "strategy": "inline", "image_url": inline_url,
            "image_alt": inline_alt, "image_title": inline_title, "error": inline_error,
        })

        # --- media_rss: media:thumbnail / media:content from the live feed XML,
        #     with fallback to image enclosures on the entry itself ---
        media_url: str | None = None
        media_error: str | None = None
        try:
            if feed_url:
                thumbs = self._fetch_feed_media_thumbnails(feed_url)
                media_url = thumbs.get(entry_link) if entry_link else None
            # Fallback: reader's Enclosure objects (.href) — same fix as the main
            # extraction path.  Lets feeds whose images live in <enclosure> rather
            # than <media:thumbnail> still show a card in the Tuning tab.
            if not media_url:
                for enc in (getattr(entry, "enclosures", None) or []):
                    if isinstance(enc, dict):
                        enc_url = enc.get("href") or enc.get("url")
                        enc_type = enc.get("type") or ""
                    else:
                        enc_url = getattr(enc, "href", None) or getattr(enc, "url", None)
                        enc_type = getattr(enc, "type", None) or ""
                    if enc_url and str(enc_type).startswith("image/"):
                        media_url = enc_url
                        break
        except Exception as exc:
            media_error = str(exc)
        results.append({
            "strategy": "media_rss", "image_url": media_url,
            "image_alt": None, "image_title": None, "error": media_error,
        })

        # --- enclosure: per-entry image already stored by reader (no live re-fetch) ---
        encl_url: str | None = None
        encl_error: str | None = None
        try:
            for enc in (getattr(entry, "enclosures", None) or []):
                if isinstance(enc, dict):
                    eu = enc.get("href") or enc.get("url")
                    et = enc.get("type") or ""
                else:
                    eu = getattr(enc, "href", None) or getattr(enc, "url", None)
                    et = getattr(enc, "type", None) or ""
                if eu and str(et).startswith("image/"):
                    encl_url = eu
                    break
        except Exception as exc:
            encl_error = str(exc)
        results.append({
            "strategy": "enclosure", "image_url": encl_url,
            "image_alt": None, "image_title": None, "error": encl_error,
        })

        # --- og_scrape: og:image / hero image from the article source page ---
        og_url: str | None = None
        og_alt: str | None = None
        og_title: str | None = None
        og_error: str | None = None
        try:
            if entry_link:
                og_url = self._fetch_source_lead_image(entry_link)
                if og_url:
                    # HTML is already in _source_html_cache from the fetch above.
                    og_alt, og_title = self.fetch_entry_image_caption(entry_link, lead_image_url=og_url)
        except Exception as exc:
            og_error = str(exc)
        results.append({
            "strategy": "og_scrape", "image_url": og_url,
            "image_alt": og_alt, "image_title": og_title, "error": og_error,
        })

        # --- webcomic: source-page scrape with comic-strip image scoring ---
        wc_url: str | None = None
        wc_alt: str | None = None
        wc_title: str | None = None
        wc_error: str | None = None
        try:
            if entry_link:
                wc_url = self._fetch_source_lead_image(entry_link, is_webcomic=True)
                if wc_url:
                    wc_alt, wc_title = self.fetch_entry_image_caption(entry_link, lead_image_url=wc_url, is_webcomic=True)
        except Exception as exc:
            wc_error = str(exc)
        results.append({
            "strategy": "webcomic", "image_url": wc_url,
            "image_alt": wc_alt, "image_title": wc_title, "error": wc_error,
        })

        # --- artwork: first large image from feed content HTML (art-portfolio feeds) ---
        art_url: str | None = None
        art_alt: str | None = None
        art_title: str | None = None
        art_error: str | None = None
        try:
            for html_candidate in html_candidates:
                if feed_url:
                    html_candidate = self._strip_feed_injected_blocks(html_candidate, feed_url)
                img = self._extract_first_image_url_from_html(html_candidate, base_url, allow_extensionless=True)
                if img:
                    art_url = img
                    for tag_match in self._IMG_TAG_RE.finditer(html_candidate):
                        tag = tag_match.group(0)
                        attrs: dict[str, str] = {}
                        for am in self._IMG_ATTR_RE.finditer(tag):
                            k = am.group(1).strip().lower()
                            v = html.unescape((am.group(2) or am.group(3) or am.group(4) or "").strip())
                            if k and v:
                                attrs[k] = v
                        for candidate_url in self._collect_img_candidate_urls(attrs):
                            if candidate_url and self._urls_equivalent(
                                urljoin(base_url, candidate_url), img
                            ):
                                art_alt = (attrs.get("alt") or "").strip() or None
                                art_title = (attrs.get("title") or "").strip() or None
                                break
                        if art_url:
                            break
                    break
        except Exception as exc:
            art_error = str(exc)
        results.append({
            "strategy": "artwork", "image_url": art_url,
            "image_alt": art_alt, "image_title": art_title, "error": art_error,
        })

        # --- youtube: hqdefault thumbnail for YouTube video entries ---
        yt_url: str | None = None
        try:
            if entry_link:
                video_id = self._extract_video_id(entry_link)
                if video_id:
                    yt_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        except Exception:
            pass
        results.append({
            "strategy": "youtube", "image_url": yt_url,
            "image_alt": None, "image_title": None, "error": None,
        })

        return results

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
            result = self._fetch_page_html(entry_link)
            if result is None:
                return None
            source_html, final_url, _corp = result
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

    @staticmethod
    def _urls_equivalent(url1: str, url2: str) -> bool:
        """Loose URL match ignoring scheme (http/https) and www. prefix."""
        try:
            def _norm(u: str) -> str:
                p = urlparse(u)
                host = p.netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                return host + p.path + ("?" + p.query if p.query else "")
            return _norm(url1) == _norm(url2)
        except Exception:
            return url1 == url2

    def fetch_entry_image_alt(self, entry_link: str, lead_image_url: str | None = None) -> str | None:
        """Return alt/title text for the lead image on the source page.

        Checks the in-memory source HTML cache first; fetches the source page on-demand
        if it is not cached.

        When lead_image_url is provided, scans specifically for that URL (loose
        http/https/www match). Returns the matching img's alt/title, or None if the
        image is not found in the source page body — never falls back to unrelated imgs.

        When lead_image_url is None, uses the scored path to find the best candidate.
        """
        # Deathbulge is an AngularJS SPA; static page fetch returns only a shell.
        # Their JSON API at /api/comics/{id} exposes alt_text directly.
        _db_spa_m = re.match(r'https?://(?:www\.)?deathbulge\.com/#/comics/(\d+)', entry_link)
        if _db_spa_m:
            try:
                import json as _json
                import urllib.request as _ureq
                _api_url = f"http://deathbulge.com/api/comics/{_db_spa_m.group(1)}"
                _req = _ureq.Request(_api_url, headers={"User-Agent": self._user_agent})
                with _ureq.urlopen(_req, timeout=10) as _resp:
                    _api_data = _json.load(_resp)
                return (_api_data.get("comic", {}).get("alt_text") or "").strip() or None
            except Exception:
                pass
            return None

        cached = self._source_html_cache.get(entry_link)
        if cached is None:
            if not is_safe_outbound_url(entry_link):
                return None
            result = self._fetch_page_html(entry_link)
            if result is None:
                return None
            source_html, final_url, _corp = result
            self._source_html_cache[entry_link] = (final_url, source_html)
            self._source_html_cache.move_to_end(entry_link)
            if len(self._source_html_cache) > self._SOURCE_HTML_CACHE_MAX:
                self._source_html_cache.popitem(last=False)
        else:
            final_url, source_html = cached

        if lead_image_url:
            # Scan specifically for the known lead image URL; do not return alt from
            # unrelated images (avoids "hand holding pint glass" for a blog header,
            # "Mature Content Warning" for an age-gated comic page, etc.).
            for tag_match in self._IMG_TAG_RE.finditer(source_html):
                tag = tag_match.group(0)
                attrs: dict[str, str] = {}
                for attr_match in self._IMG_ATTR_RE.finditer(tag):
                    key = attr_match.group(1).strip().lower()
                    value = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
                    if key and value:
                        attrs[key] = value
                for image_url in self._collect_img_candidate_urls(attrs):
                    if not image_url or image_url.startswith("data:"):
                        continue
                    resolved = urljoin(final_url, image_url)
                    if self._urls_equivalent(resolved, lead_image_url):
                        # Prefer title over alt: title is the tooltip/hovertext that
                        # users see on the real page (e.g. XKCD, Oglaf punchlines).
                        return (attrs.get("title") or attrs.get("alt") or "").strip() or None
            return None

        # No specific lead URL: use the scored path for best confidence.
        _, alt_text = self._extract_preferred_source_image_data(source_html, final_url, entry_link)
        if alt_text:
            return alt_text

        # Fallback: first acceptable img on the page with non-empty alt/title.
        for tag_match in self._IMG_TAG_RE.finditer(source_html):
            tag = tag_match.group(0)
            attrs: dict[str, str] = {}
            for attr_match in self._IMG_ATTR_RE.finditer(tag):
                key = attr_match.group(1).strip().lower()
                value = html.unescape((attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or "").strip())
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
                candidate = (attrs.get("title") or attrs.get("alt") or "").strip()
                if candidate:
                    return candidate
                break  # only consider the first candidate URL per img tag
        return None
