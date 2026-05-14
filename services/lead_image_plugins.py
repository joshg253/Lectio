from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from services.url_guard import is_safe_outbound_url

_WP_COMIC_UPLOAD_RE = re.compile(
    r'https?://[^"\'<>\s]+/wp-content/uploads/[^"\'<>\s]+\.(?:jpe?g|png|webp|gif)',
    re.IGNORECASE,
)
_PDL_NAV_RE = re.compile(r'arrow|(?:^|[/_-])(?:prev|next)(?:[/_.-]|$)', re.IGNORECASE)
_MATURE_GATE_PATTERNS = re.compile(
    r"(?:mature|warning|nsfw|adult|age[\-_]?gate|18[\-_+](?:plus|only))",
    re.IGNORECASE,
)
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE_REVERSED = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']',
    re.IGNORECASE,
)


class LeadImagePlugin(Protocol):
    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool: ...

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]: ...

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int: ...

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None: ...


@dataclass(frozen=True)
class FutureSiteLeadImagePlugin:
    """Future publishing sites often emit wrapper image URLs that should be recomputed."""

    host_contains: tuple[str, ...] = (
        "pcgamer.com",
        "tomshardware.com",
        "techradar.com",
        "gamesradar.com",
        "androidcentral.com",
        "laptopmag.com",
    )
    wrapper_path_markers: tuple[str, ...] = (
        "/flexiimages/",
        "/lazyimages/",
    )

    _future_attr_names: tuple[str, ...] = (
        "data-original-mos",
        "data-pin-media",
    )

    def _is_target_host(self, source_url: str) -> bool:
        host = urlparse(source_url).netloc.lower()
        if not host:
            return False
        return any(marker in host for marker in self.host_contains)

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target_host(entry_link):
            return False

        cached_path = urlparse(cached_url).path.lower()
        if not cached_path:
            return False

        return any(marker in cached_path for marker in self.wrapper_path_markers)

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        if not self._is_target_host(source_url):
            return ()
        return self._future_attr_names

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target_host(source_url):
            return 0

        score = 0
        if attrs.get("data-original-mos") or attrs.get("data-pin-media"):
            score += 20

        lower_url = resolved_url.lower()
        if any(marker in lower_url for marker in self.wrapper_path_markers):
            score -= 35

        return score

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        return None


@dataclass(frozen=True)
class StandardEbooksLeadImagePlugin:
    """Prefer canonical ebook cover downloads for Standard Ebooks entries."""

    host: str = "standardebooks.org"

    def _is_target(self, entry_link: str) -> bool:
        parsed = urlparse(entry_link)
        if parsed.scheme not in {"http", "https"}:
            return False
        return parsed.netloc.lower() == self.host

    def _cover_url(self, entry_link: str) -> str | None:
        if not self._is_target(entry_link):
            return None
        parsed = urlparse(entry_link)
        path = parsed.path.rstrip("/")
        if "/ebooks/" not in path:
            return None
        return f"{parsed.scheme}://{parsed.netloc}{path}/downloads/cover.jpg"

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        preferred = self._cover_url(entry_link)
        if not preferred:
            return False
        return cached_url.rstrip("/") != preferred.rstrip("/")

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        return self._cover_url(entry_link)


_WP_COMIC_ENTRY_RE = re.compile(r"[?&]post_type=comic\b|/comic/", re.IGNORECASE)


@dataclass(frozen=True)
class WordPressComicPlugin:
    """For WordPress-hosted webcomics, skip mature-content gate images and
    extract the actual comic image from the feed content HTML."""

    host_contains: tuple[str, ...] = ("monstersoupcomic.com", "badmachinery.com")

    def _is_target(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        if not any(h in host for h in self.host_contains):
            return False
        # Only activate for comic entries, not news/blog posts on the same domain.
        return bool(_WP_COMIC_ENTRY_RE.search(url))

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return bool(_MATURE_GATE_PATTERNS.search(cached_url))

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        if not is_safe_outbound_url(entry_link):
            return None
        # Feed carries no inline images; fetch og:image from the source page directly.
        # This avoids _fetch_source_lead_image picking up site-chrome images
        # (title banners, nav buttons) that appear before the comic in the DOM.
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0) as client:
                r = client.get(entry_link)
            r.raise_for_status()
            for pattern in (_OG_IMAGE_RE, _OG_IMAGE_RE_REVERSED):
                m = pattern.search(r.text)
                if m:
                    url = html.unescape(m.group(1).strip())
                    if url and not _MATURE_GATE_PATTERNS.search(url):
                        return url
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class GunnerkriggPlugin:
    """Derive the comic image URL directly from the ?p= page number in the
    entry link — no HTTP request needed."""

    host: str = "gunnerkrigg.com"

    def _comic_url(self, entry_link: str) -> str | None:
        parsed = urlparse(entry_link)
        if self.host not in parsed.netloc.lower():
            return None
        m = re.search(r"[?&]p=(\d+)", entry_link)
        if not m:
            return None
        p = int(m.group(1))
        return f"{parsed.scheme}://{parsed.netloc}/comics/{p:08d}.jpg"

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        preferred = self._comic_url(entry_link)
        if not preferred:
            return False
        return cached_url != preferred

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        return self._comic_url(entry_link)


@dataclass(frozen=True)
class PennyArcadePlugin:
    """For comic entries, use og:image (full comic) instead of the first-panel
    image that body-scanning picks up from the source page."""

    host: str = "penny-arcade.com"

    def _is_comic_entry(self, entry_link: str) -> bool:
        parsed = urlparse(entry_link)
        return self.host in parsed.netloc.lower() and "/comic/" in parsed.path

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_comic_entry(entry_link):
            return False
        return "/panels/" in cached_url

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_comic_entry(entry_link):
            return None
        if not is_safe_outbound_url(entry_link):
            return None
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0) as client:
                r = client.get(entry_link)
            r.raise_for_status()
            for pattern in (_OG_IMAGE_RE, _OG_IMAGE_RE_REVERSED):
                m = pattern.search(r.text)
                if m:
                    url = html.unescape(m.group(1).strip())
                    if url and "/panels/" not in url:
                        return url
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class SMBCPlugin:
    """SMBC comic images are in the feed content but the source page loads them
    via JS, so source scraping picks up the site logo instead. Extract directly
    from content_html to skip the source fetch entirely."""

    host: str = "smbc-comics.com"
    _COMIC_PATH: str = "/comics/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._COMIC_PATH not in cached_url

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']+' + re.escape(self._COMIC_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return m.group(1)
        return None


@dataclass(frozen=True)
class MisfilePlugin:
    """Misfile webcomics — the feed and og:image both carry site/network
    branding.  Scrape the source page for the actual comic strip image."""

    host: str = "misfile.com"
    _COMIC_PATH: str = "/comics/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._COMIC_PATH not in cached_url

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        if not is_safe_outbound_url(entry_link):
            return None
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0) as client:
                r = client.get(entry_link)
            r.raise_for_status()
            m = re.search(
                r'src=["\']([^"\']+' + re.escape(self._COMIC_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                r.text,
                re.IGNORECASE,
            )
            if m:
                return html.unescape(m.group(1).strip())
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class JohnnyWanderPlugin:
    """Johnny Wander — og:image / source scraping returns the series banner.
    Scrape the source page for the actual comic image (served from /comics/)."""

    host: str = "johnnywander.com"
    _COMIC_PATH: str = "/comics/"
    # Series banners live under /images/ — not comic pages.
    _BANNER_PATH: str = "/images/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._COMIC_PATH not in cached_url

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        # Penalise banner images so they don't win over the actual comic.
        if self._BANNER_PATH in urlparse(resolved_url).path:
            return -100
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        if not is_safe_outbound_url(entry_link):
            return None
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0) as client:
                r = client.get(entry_link)
            r.raise_for_status()
            m = re.search(
                r'src=["\']([^"\']+' + re.escape(self._COMIC_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                r.text,
                re.IGNORECASE,
            )
            if m:
                return html.unescape(m.group(1).strip())
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class QwantzPlugin:
    """Dinosaur Comics (qwantz.com) — comic image is in the feed content but
    source-page scraping returns the site logo instead."""

    host: str = "qwantz.com"
    _COMIC_PATH: str = "/comics/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._COMIC_PATH not in cached_url

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']+' + re.escape(self._COMIC_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return m.group(1)
        return None


@dataclass(frozen=True)
class PoorlyDrawnLinesPlugin:
    """poorlydrawnlines.com — comic image is inline in feed content but source-
    page scraping picks up nav arrows from /wp-content/uploads/ instead."""

    host: str = "poorlydrawnlines.com"
    _UPLOAD_PATH: str = "/wp-content/uploads/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return bool(_PDL_NAV_RE.search(urlparse(cached_url).path))

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        if _PDL_NAV_RE.search(urlparse(resolved_url).path):
            return -100
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            for m in re.finditer(
                r'src=["\']([^"\']+' + re.escape(self._UPLOAD_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            ):
                url = m.group(1)
                if not _PDL_NAV_RE.search(url):
                    return url
        return None


@dataclass(frozen=True)
class TumblrPlugin:
    """Tumblr feeds include the post image inline in the RSS content.
    Skip source-page scraping, which returns an avatar or logo instead.
    Also handles Tumblr custom domains (e.g. blog.loish.net)."""

    _MEDIA_HOST: str = "media.tumblr.com"
    # Known custom domains backed by Tumblr — entry links use /post/<id> paths.
    _CUSTOM_DOMAINS: tuple[str, ...] = ("blog.loish.net", "mandatoryrollercoaster.com")

    def _is_target(self, url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        if "tumblr.com" in netloc:
            return True
        return any(d in netloc for d in self._CUSTOM_DOMAINS)

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._MEDIA_HOST not in urlparse(cached_url).netloc.lower()

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        # Heavily penalise all source-scraped images from Tumblr pages so that
        # profile pics / reblog thumbnails never displace the post's own image.
        if self._is_target(source_url):
            return -200
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']*' + re.escape(self._MEDIA_HOST) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return m.group(1)
        return None


@dataclass(frozen=True)
class WebtoonsPlugin:
    """Webtoons Canvas — the feed and og:image return the series thumbnail for
    every episode.  Suppress image resolution entirely; there is no
    episode-specific image accessible without authentication."""

    _HOST: str = "webtoons.com"

    def _is_target(self, url: str) -> bool:
        return self._HOST in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        # Any previously cached URL came from source scraping (series thumbnail).
        # Always bypass so re-resolution runs and stores None.
        return self._is_target(entry_link)

    def should_skip_source_lookup(self, *, entry_link: str) -> bool:
        return self._is_target(entry_link)

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        return None


@dataclass(frozen=True)
class BloggerPlugin:
    """Blogger / blogspot blogs include the post image in the RSS content.
    The og:image is a social-media landscape crop (w1200-h630), not the
    original portrait image.  Extract from content_html to skip source
    scraping and use the correct full-size version."""

    _CDN_HOST: str = "blogger.googleusercontent.com"
    _CDN_PATH: str = "/img/b/"

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        return False

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']*' + re.escape(self._CDN_HOST) + re.escape(self._CDN_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return html.unescape(m.group(1).strip())
        return None


@dataclass(frozen=True)
class TheRockCocksPlugin:
    """therockcocks.com — the feed embeds a small /comicsthumbs/ thumbnail.
    The full comic lives at the same filename under /comics/, but the exact
    URL differs by one second in the timestamp compared to og:image on the
    source page.  We try the derived /comics/ URL; if it returns HTML (age-
    gated), return None so source-page scraping picks up the correct og:image."""

    host: str = "therockcocks.com"
    _THUMB_PATH: str = "/comicsthumbs/"
    _COMIC_PATH: str = "/comics/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        # Bypass comicsthumbs thumbnails (age-gated /comics/ entries got stashed here;
        # re-resolve so source scraping can find the real og:image URL).
        # Also bypass promotional/off-domain images.
        return self._THUMB_PATH in cached_url or "slipshine.net" in cached_url or (
            self.host not in urlparse(cached_url).netloc.lower()
            and "patreon" in cached_url.lower()
        )

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        if self._THUMB_PATH in resolved_url:
            return -100
        if "/uploads/" in resolved_url:
            return -50
        return 0

    def should_skip_source_lookup(self, *, entry_link: str) -> bool:
        # Generic source scraping picks up banner ads before og:image on this site.
        # We handle the source page fetch ourselves in fallback_lead_image_url.
        return self._is_target(entry_link)

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']*' + re.escape(self._THUMB_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                comic_url = m.group(1).replace(self._THUMB_PATH, self._COMIC_PATH)
                try:
                    resp = httpx.head(comic_url, follow_redirects=True, timeout=3.0)
                    if resp.headers.get("content-type", "").startswith("image"):
                        return comic_url
                except Exception:
                    pass
                break  # /comics/ URL is age-gated; fall through to og:image fetch
        # The derived /comics/ URL didn't work — fetch og:image from the source page.
        if not entry_link:
            return None
        try:
            resp = httpx.get(
                entry_link,
                follow_redirects=True,
                timeout=6.0,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            for pattern in (
                re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I),
                re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I),
            ):
                og_m = pattern.search(resp.text)
                if og_m:
                    og_url = og_m.group(1).strip()
                    if og_url.startswith(("http://", "https://")):
                        return og_url
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class ComicFuryPlugin:
    """ComicFury-hosted comics (thecomicseries.com, comicfury.com).
    The comic image is in the feed content at img.comicfury.com/comics/.
    Source-page scraping picks up the site header instead."""

    _CDN_HOST: str = "img.comicfury.com"
    _COMIC_PATH: str = "/comics/"

    def _is_target(self, url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return "thecomicseries.com" in netloc or "comicfury.com" in netloc

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        # Bypass if cached URL is not from the ComicFury image CDN.
        return self._CDN_HOST not in urlparse(cached_url).netloc.lower()

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']*' + re.escape(self._CDN_HOST) + re.escape(self._COMIC_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return html.unescape(m.group(1).strip())
        return None


@dataclass(frozen=True)
class OglafPlugin:
    """Oglaf — use the story thumbnail from the feed (media.oglaf.com/story/tt*.jpg).
    The full comic URL (media.oglaf.com/comic/) returns 403 for the current strip
    until it rotates out.  Source-page scraping returns the masthead logo instead."""

    host: str = "oglaf.com"
    _MEDIA_HOST: str = "media.oglaf.com"
    _STORY_PATH: str = "/story/tt"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def _story_url(self, entry_link: str) -> str | None:
        parsed = urlparse(entry_link)
        if not self._is_target(entry_link):
            return None
        slug = parsed.path.strip("/")
        if not slug or "/" in slug:
            return None
        return f"https://{self._MEDIA_HOST}{self._STORY_PATH}{slug}.jpg"

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._MEDIA_HOST not in urlparse(cached_url).netloc.lower()

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        # Prefer story thumbnail explicitly embedded in feed content.
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']*' + re.escape(self._MEDIA_HOST) + re.escape(self._STORY_PATH) + r'[^"\']+\.jpg)["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return html.unescape(m.group(1).strip())
        # Derive story thumbnail from entry link slug as fallback.
        return self._story_url(entry_link)


@dataclass(frozen=True)
class ComicEaselPlugin:
    """ComicEasel WordPress plugin — the feed embeds a /comicsthumbs/ thumbnail.
    The full comic lives at the same filename under /comics/, derivable by a
    simple path substitution with no HTTP request required."""

    host_contains: tuple[str, ...] = (
        "whompcomic.com",
        "wildelifecomic.com",
        "shortpacked.com",
    )
    _THUMB_PATH: str = "/comicsthumbs/"
    _COMIC_PATH: str = "/comics/"

    def _is_target(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(h in host for h in self.host_contains)

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._THUMB_PATH in cached_url

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        if self._THUMB_PATH in resolved_url:
            return -100
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']*' + re.escape(self._THUMB_PATH) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return m.group(1).replace(self._THUMB_PATH, self._COMIC_PATH)
        return None


DEFAULT_LEAD_IMAGE_PLUGINS: tuple[LeadImagePlugin, ...] = (
    StandardEbooksLeadImagePlugin(),
    FutureSiteLeadImagePlugin(),
    WordPressComicPlugin(),
    GunnerkriggPlugin(),
    PennyArcadePlugin(),
    SMBCPlugin(),
    MisfilePlugin(),
    JohnnyWanderPlugin(),
    PoorlyDrawnLinesPlugin(),
    TumblrPlugin(),
    QwantzPlugin(),
    WebtoonsPlugin(),
    BloggerPlugin(),
    TheRockCocksPlugin(),
    ComicFuryPlugin(),
    OglafPlugin(),
    ComicEaselPlugin(),
)
