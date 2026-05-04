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


@dataclass(frozen=True)
class WordPressComicPlugin:
    """For WordPress-hosted webcomics, skip mature-content gate images and
    extract the actual comic image from the feed content HTML."""

    host_contains: tuple[str, ...] = ("monstersoupcomic.com", "badmachinery.com")

    def _is_target(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(h in host for h in self.host_contains)

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


DEFAULT_LEAD_IMAGE_PLUGINS: tuple[LeadImagePlugin, ...] = (
    StandardEbooksLeadImagePlugin(),
    FutureSiteLeadImagePlugin(),
    WordPressComicPlugin(),
    GunnerkriggPlugin(),
    PennyArcadePlugin(),
    SMBCPlugin(),
)
