from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse


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


DEFAULT_LEAD_IMAGE_PLUGINS: tuple[LeadImagePlugin, ...] = (
    StandardEbooksLeadImagePlugin(),
    FutureSiteLeadImagePlugin(),
)
