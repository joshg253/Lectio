from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from services import svg_sanitize
from services import url_guard
from services.url_guard import is_safe_outbound_url

# A whole inline <svg>…</svg> element (non-greedy).
_SVG_ELEMENT_RE = re.compile(r"<svg\b[^>]*>.*?</svg\s*>", re.IGNORECASE | re.DOTALL)


def _guarded_get(url: str, *, timeout: float = 8.0, headers: dict | None = None) -> httpx.Response:
    """SSRF-safe source-page GET shared by the per-site fallback plugins.

    Centralizes the ``follow_redirects=False`` + ``url_guard.safe_get`` policy so
    every hop is re-validated and the redirect-to-internal bypass stays closed in
    one place. Raises ``url_guard.UnsafeURLError`` for an unsafe hop.
    """
    with url_guard.build_client(timeout=timeout) as client:
        return url_guard.safe_get(client, url, headers=headers)


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


def _same_file_key(url: str) -> str:
    """Host and path only, lowercased — "is this the same file?" without the
    parts that vary while the file does not.

    Two things defeat an exact string compare on the same image. A cache-busting
    query (`?v=1785135600`) is one. The **scheme** is the other, and it is the
    subtler one: a URL derived from an entry link inherits that link's scheme, so
    a feed still publishing `http://` links yields an `http://` guess for an
    image the site serves over `https://`.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    return f"{parsed.netloc.lower()}{parsed.path}"


class LeadImagePlugin(Protocol):
    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool: ...

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]: ...

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int: ...

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None: ...

    # Optional: a deterministic, network-free preferred thumbnail URL derived from
    # the entry link alone. Implemented only by plugins that can build one cheaply
    # (e.g. Standard Ebooks covers); consulted on the posts-list fast path. Plugins
    # that need to fetch a page must NOT implement this (leave it to fallback_lead_image_url).
    def cheap_preferred_thumbnail_url(self, *, entry_link: str) -> str | None: ...

    # Optional: a different crop for the LIST than for the article, derived from
    # the already-resolved lead image without a fetch (called on the render
    # path). Penny Arcade uses it to thumbnail one comic panel while the article
    # keeps the full strip, which is illegible at thumbnail size.
    def thumbnail_from_lead_image(self, *, entry_link: str, lead_url: str) -> str | None: ...


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

    def cheap_preferred_thumbnail_url(self, *, entry_link: str) -> str | None:
        # Deterministic cover URL from the entry link — no network — so the posts
        # list gets the SE cover on the fast path instead of waiting for a source
        # scrape or the background backfill. (The feed's media:thumbnail is
        # bypassed in favor of this full-size cover; /thumb downscales it.)
        return self._cover_url(entry_link)

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        return self._cover_url(entry_link)


_WP_COMIC_ENTRY_RE = re.compile(r"[?&]post_type=comic\b|[?&]comic=|/comic/", re.IGNORECASE)


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
            r = _guarded_get(entry_link)
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
        # Compare without the query string. Gunnerkrigg serves the panel with a
        # `?v=<timestamp>` cache-buster, so an exact string compare called the
        # very image this plugin derives "not the preferred one" and bypassed it.
        # The article still showed it (that path does not consult the bypass)
        # while the list thumbnail came back empty — a post with a picture and no
        # thumbnail, which is how it was reported.
        #
        # Only the comparison ignores the query; the cached URL is still served
        # as-is, cache-buster and all. Dropping it from the URL itself would be
        # the ComicControl mistake in _promote_known_thumbnail: a rewritten URL
        # can name a file that does not exist and get a 200 placeholder back.
        return _same_file_key(cached_url) != _same_file_key(preferred)

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        return self._comic_url(entry_link)


@dataclass(frozen=True)
class DresdenCodakPlugin:
    """Keep the whole comic in the article and the site's own crop in the list.

    dresdencodak publishes two files for its "DC Minis" strips:

        article : …/wp-content/uploads/YYYY/MM/dc_minis_<n>.jpg
        list    : …/wp-content/uploads/YYYY/MM/dc_minis_<n>_thumbnail.jpg

    The comic is a tall column — #27 is 1500x4875 — which at thumbnail size is a
    sliver, while the site's own thumbnail is a 2500x1000 landscape crop drawn
    for exactly this. Same split as Penny Arcade's strip/panel, different naming.

    **Restricted to `dc_minis_` on purpose.** The convention is only half kept:
    every DC Minis strip checked has its `_thumbnail.jpg` (25, 26, 27), and no
    Dark Science page has one (`ds_185`, `ds_186_silder`, `ds_187_silder` all
    404). Deriving blind across the feed would 404 every Dark Science thumbnail,
    and `thumbnail_from_lead_image` is network-free by contract, so the plugin
    cannot check — it can only decline where it does not know.

    The reverse derivation is deliberately NOT done. Stripping `_thumbnail` to
    reach the article image works for #26 and #23 and 404s for #25 and #22:
    those posts publish only the `_thumbnail`-named file, which is therefore the
    comic rather than a crop of it. Instead `source_score_adjustment` demotes a
    `_thumbnail` URL while scoring the source page, so when the page offers both,
    the full comic wins the lead on its own merits — and when it offers only the
    `_thumbnail` file, that still wins for want of anything else.
    """

    host: str = "dresdencodak.com"

    _MINIS_IMAGE_RE = re.compile(
        r"^(?P<base>https?://[^/]*dresdencodak\.com/.*?/dc_minis_[0-9]+[a-z0-9_-]*)"
        r"(?P<ext>\.(?:jpg|jpeg|png|webp))$",
        re.IGNORECASE,
    )

    def _is_ours(self, url: str) -> bool:
        try:
            return self.host in urlparse(url).netloc.lower()
        except ValueError:
            return False

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        return False

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        """Demote the site's own thumbnail crop while scoring the source page.

        Only a nudge, not a veto: on a page that offers nothing else, the
        `_thumbnail` file is still the comic and must remain selectable.
        """
        if not self._is_ours(source_url) or not resolved_url:
            return 0
        return -30 if "_thumbnail" in resolved_url.lower() else 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        return None

    def cheap_preferred_thumbnail_url(self, *, entry_link: str) -> str | None:
        return None

    def thumbnail_from_lead_image(self, *, entry_link: str, lead_url: str) -> str | None:
        if not lead_url or not self._is_ours(lead_url):
            return None
        if "_thumbnail" in lead_url.lower():
            # Already the crop — a fine thumbnail exactly as it is.
            return lead_url
        m = self._MINIS_IMAGE_RE.match(lead_url)
        if not m:
            return None
        return f"{m.group('base')}_thumbnail{m.group('ext')}"


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

    def should_skip_source_lookup(self, *, entry_link: str) -> bool:
        """The page scan reads the first <img>, which is panel 1 — so it wins the
        race and stores a single panel as the article image. Skipping it lets
        fallback_lead_image_url below take the og:image (the whole strip), which
        is the point of this plugin. Without this the class docstring described
        behaviour the plugin never actually reached."""
        return self._is_comic_entry(entry_link)

    def thumbnail_from_lead_image(self, *, entry_link: str, lead_url: str) -> str | None:
        """Thumbnail the FIRST PANEL, while the article keeps the whole strip.

        A Penny Arcade strip is ~1050x438 — three panels side by side. Scaled to
        a list thumbnail that is three unreadable smudges, whereas panel 1 is a
        legible square-ish image. The two live at derivable paths:

            article : …/comics/<hash>.jpg
            panel 1 : …/comics/panels/<hash>-p1.jpg

        so no fetch is needed to get from one to the other. Returning None for a
        URL that is already a panel leaves the existing behaviour alone.
        """
        if not self._is_comic_entry(entry_link) or not lead_url:
            return None
        if "/panels/" in lead_url:
            # Already a panel (an older cached value): fine as a thumbnail as-is.
            return lead_url
        m = self._COMIC_IMAGE_RE.match(lead_url)
        if not m:
            return None
        return f"{m.group('base')}/panels/{m.group('slug')}-p1{m.group('ext')}"

    _COMIC_IMAGE_RE = re.compile(
        r"^(?P<base>https?://[^/]*penny-arcade\.com(?:/[^/]+)*?/comics)"
        r"/(?P<slug>[^/]+?)(?P<ext>\.(?:jpg|jpeg|png|webp))$",
        re.IGNORECASE,
    )

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
            r = _guarded_get(entry_link)
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
            r = _guarded_get(entry_link)
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
            r = _guarded_get(entry_link)
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

    def should_skip_source_lookup(self, *, entry_link: str) -> bool:
        # Tumblr post images are always inline in the RSS content when they exist.
        # Source-page og:image returns the reblogged source's image (often a
        # different post's photo), which is wrong for text-only reply posts.
        return self._is_target(entry_link)

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
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
    """Webtoons — take the panel from the feed, never from the episode page.

    An episode page's og:image is the *series* thumbnail: the same
    ``.../thumbnail.jpg`` on every episode of a series (verified across two
    False Knees episodes, which return byte-identical og:image URLs). So source
    scraping stays off.

    The feed is a different story. Every ``<description>`` carries that
    episode's own panel on the same CDN — distinct per episode across all nine
    subscribed Webtoons feeds, 6 of 6 unique on each — and it serves 200 to an
    ordinary fetch. This plugin previously suppressed image resolution
    altogether, on the belief that the feed only repeated the series thumbnail;
    it does not, and the result was every episode showing the series thumbnail
    (or nothing) while the real panel sat unused in the entry body."""

    _HOST: str = "webtoons.com"
    _MEDIA_HOST: str = "pstatic.net"

    def _is_target(self, url: str) -> bool:
        return self._HOST in urlparse(url).netloc.lower()

    @staticmethod
    def _is_series_thumbnail(url: str) -> bool:
        """A ``.../thumbnail.<ext>`` basename — the series-wide og:image."""
        name = urlparse(url).path.rsplit("/", 1)[-1].lower()
        return name.rsplit(".", 1)[0] == "thumbnail"

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        # Only the series thumbnail: a cached episode panel is the right answer
        # and re-resolving it every render would be pure churn. Rows cached
        # before this plugin learned to read the feed body are thumbnails, so
        # they still get replaced.
        return self._is_target(entry_link) and self._is_series_thumbnail(cached_url)

    def should_skip_source_lookup(self, *, entry_link: str) -> bool:
        return self._is_target(entry_link)

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
                r'src=["\']([^"\']*' + re.escape(self._MEDIA_HOST) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m and not self._is_series_thumbnail(m.group(1)):
                return html.unescape(m.group(1).strip())
        return None


@dataclass(frozen=True)
class TapasPlugin:
    """Tapas — the same shape as Webtoons, on a different host.

    An episode page's og:image is a **social card** (`.png` on `us-a.tapas.io`),
    distinct per episode so it does not look like a series thumbnail, but it is
    not the comic. The panel is in the feed's own `<content:encoded>` as a
    `.jpg` on the same CDN. Verified on episode 2721164 ("Rudolph"): og:image is
    `.../sa/34/00700065-….png`, the body carries `.../sa/18/cb6052ae-….jpg`.

    The distinct-per-episode part is why this needed its own plugin rather than
    the thumbnail-basename test Webtoons uses — nothing in the *URL* says which
    is the card. What separates them is where they came from."""

    _HOST: str = "tapas.io"
    _MEDIA_HOST: str = "tapas.io"

    def _is_target(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host == self._HOST or host.endswith("." + self._HOST)

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        # Bypass anything that is not the feed's own panel spelling. The cards
        # are .png and the panels .jpg on the same CDN, so the extension is the
        # only URL-level signal — enough to force a re-resolve of a cached card
        # without churning a cached panel on every render.
        if not self._is_target(entry_link):
            return False
        return not urlparse(cached_url).path.lower().endswith((".jpg", ".jpeg"))

    def should_skip_source_lookup(self, *, entry_link: str) -> bool:
        return self._is_target(entry_link)

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
                r'src=["\']([^"\']*' + re.escape(self._MEDIA_HOST) + r'[^"\']+\.(?:png|jpe?g|gif|webp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return html.unescape(m.group(1).strip())
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
        # Bypass feed thumbnails, promotional/off-domain images, and CMS template
        # assets (e.g. Pillowfort promo image stored as og:image for age-gated pages).
        parsed_path = urlparse(cached_url).path
        return (
            self._THUMB_PATH in cached_url
            or "slipshine.net" in cached_url
            or "/templates/" in parsed_path
            # Site assets (e.g. .../images/logo.png served as og:image on the
            # age gate, cached 2026-07-10) — comics only ever live in /comics/.
            or "/images/" in parsed_path
            or (self.host not in urlparse(cached_url).netloc.lower() and "patreon" in cached_url.lower())
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
        if "/templates/" in urlparse(resolved_url).path:
            return -200
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
                    resp = url_guard.safe_head(comic_url, timeout=3.0)
                    if resp.headers.get("content-type", "").startswith("image"):
                        return comic_url
                except Exception:
                    pass
                break  # /comics/ URL is age-gated; fall through to og:image fetch
        # The derived /comics/ URL didn't work — fetch og:image from the source page.
        if not entry_link:
            return None
        try:
            resp = _guarded_get(entry_link, timeout=6.0, headers={"User-Agent": "Mozilla/5.0"})
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


_OGLAF_COMIC_ACCESSIBLE: dict[str, bool | str] = {}  # slug → True/False/actual-url, in-process cache


@dataclass(frozen=True)
class OglafPlugin:
    """Oglaf — try the full comic URL (media.oglaf.com/comic/*.jpg) first.
    The current unrotated strip may return 403; fall back to the story thumbnail
    (media.oglaf.com/story/tt*.jpg) embedded in the feed.  The static.oglaf.com
    domain serves only site chrome (masthead, icons) — downrank those in scoring."""

    host: str = "oglaf.com"
    _MEDIA_HOST: str = "media.oglaf.com"
    _STORY_PATH: str = "/story/tt"
    _COMIC_PATH: str = "/comic/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def _slug(self, entry_link: str) -> str | None:
        slug = urlparse(entry_link).path.strip("/")
        return slug if slug and "/" not in slug else None

    def _comic_url(self, entry_link: str) -> str | None:
        slug = self._slug(entry_link)
        return f"https://{self._MEDIA_HOST}{self._COMIC_PATH}{slug}.jpg" if slug else None

    def _story_url(self, entry_link: str) -> str | None:
        slug = self._slug(entry_link)
        return f"https://{self._MEDIA_HOST}{self._STORY_PATH}{slug}.jpg" if slug else None

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        # Bypass story thumbnails (story/tt*) so the full comic URL is tried next open.
        # Also bypass anything not from media.oglaf.com (e.g., site masthead logo).
        parsed = urlparse(cached_url)
        if self._MEDIA_HOST not in parsed.netloc.lower():
            return True
        return self._STORY_PATH in parsed.path

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        parsed = urlparse(resolved_url)
        # Strongly prefer full comic images from media.oglaf.com/comic/.
        if self._MEDIA_HOST in parsed.netloc and self._COMIC_PATH in parsed.path:
            return 100
        # Downrank story thumbnails (tt*) — they're feed previews, not the full comic.
        if self._MEDIA_HOST in parsed.netloc and self._STORY_PATH in parsed.path:
            return -50
        # Downrank static.oglaf.com (masthead, icons, vote buttons — never article images).
        if "static.oglaf.com" in parsed.netloc:
            return -200
        return 0

    def _comic_url_from_page(self, entry_link: str) -> str | None:
        """Scrape the comic page to find the actual /comic/ image URL.

        Oglaf's feed slug (e.g. 'rubsalt') doesn't always match the comic
        filename (e.g. 'rub-that-salt.jpg'), so the guessed URL can 403 even
        after the strip rotates.  Scraping the page gives the canonical URL.
        """
        try:
            resp = _guarded_get(entry_link, timeout=4.0, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return None
            m = re.search(
                r'src="(https://' + re.escape(self._MEDIA_HOST) + re.escape(self._COMIC_PATH) + r'[^"]+\.(?:jpg|gif|png|webp))"',
                resp.text,
                re.IGNORECASE,
            )
            return m.group(1) if m else None
        except Exception:
            return None

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        slug = self._slug(entry_link) or ""

        # Check in-process cache first.
        cached = _OGLAF_COMIC_ACCESSIBLE.get(slug)
        if isinstance(cached, str):
            return cached  # previously found actual URL
        if cached is True:
            return self._comic_url(entry_link)

        # Try the slug-guessed comic URL.
        comic_url = self._comic_url(entry_link)
        if comic_url:
            try:
                resp = url_guard.safe_head(
                    comic_url, timeout=2.0, headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code == 200:
                    _OGLAF_COMIC_ACCESSIBLE[slug] = True
                    return comic_url
            except Exception:
                pass

        # Slug URL didn't work — scrape the page for the canonical /comic/ URL.
        # This handles cases where the filename differs from the path slug.
        if slug not in _OGLAF_COMIC_ACCESSIBLE:
            page_url = self._comic_url_from_page(entry_link)
            if page_url:
                _OGLAF_COMIC_ACCESSIBLE[slug] = page_url
                return page_url
            _OGLAF_COMIC_ACCESSIBLE[slug] = False

        # Fall back to story thumbnail from feed content.
        for source in (content_html, summary):
            if not isinstance(source, str):
                continue
            m = re.search(
                r'src=["\']([^"\']*' + re.escape(self._MEDIA_HOST) + re.escape(self._STORY_PATH) + r'[^"\']+\.(?:jpg|gif|png|bmp))["\']',
                source,
                re.IGNORECASE,
            )
            if m:
                return html.unescape(m.group(1).strip())
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
        # Bypass feed thumbnails — the full comic lives at /comics/.
        # Also bypass CMS-uploaded branding images (e.g. Hiveworks logo at /uploads/)
        # that the source scraper may have stored by mistake.
        parsed_path = urlparse(cached_url).path
        return self._THUMB_PATH in cached_url or "/uploads/" in parsed_path

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        if self._THUMB_PATH in resolved_url:
            return -100
        if "/uploads/" in urlparse(resolved_url).path:
            return -150
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


@dataclass(frozen=True)
class WinPenPackPlugin:
    """winpenpack.com — feed content embeds language-flag icons (/e107_images/icons/)
    as images; the actual app thumbnail is at /e107_files/downloadthumbs/ on the
    source page.  Bypass flag icons and fetch the real thumbnail."""

    host: str = "winpenpack.com"
    _FLAG_PATH: str = "/e107_images/icons/"
    _BANNER_HINT: str = "wpp_banner"
    _THUMB_PATH: str = "e107_files/downloadthumbs/"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._FLAG_PATH in cached_url or self._BANNER_HINT in cached_url

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        if self._FLAG_PATH in resolved_url or self._BANNER_HINT in resolved_url:
            return -200
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not self._is_target(entry_link):
            return None
        if not is_safe_outbound_url(entry_link):
            return None
        try:
            r = _guarded_get(entry_link, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            m = re.search(
                r'([^"\'<>\s]*' + re.escape(self._THUMB_PATH) + r'[^"\'<>\s]+\.(?:png|jpe?g|gif|webp))',
                r.text,
                re.IGNORECASE,
            )
            if m:
                from urllib.parse import urljoin
                return urljoin(str(r.url), m.group(1).strip())
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class AnalogueLeadImagePlugin:
    """Analogue (analogue.co) firmware/announcement pages have no image in the
    feed — the article's hero is a monochrome device illustration rendered as an
    inline ``<svg>`` on the source page. Fetch the page, pull out that hero SVG
    (the only one with the large responsive ``w-[34vw]`` width class; the rest are
    small fixed-size UI icons), sanitize it, and return it as a ``data:`` URI.
    """

    host_contains: tuple[str, ...] = ("analogue.co",)
    # Class marker unique to the hero device illustration across analogue pages.
    _hero_class_marker: str = "w-[34vw]"

    def _is_target(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(h in host for h in self.host_contains)

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        return False

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        return 0

    def fallback_lead_image_url(self, *, entry_link: str, content_html: str | None, summary: str | None) -> str | None:
        if not entry_link or not self._is_target(entry_link):
            return None
        try:
            r = _guarded_get(entry_link, timeout=8.0, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            page = r.text
        except Exception:
            return None
        for m in _SVG_ELEMENT_RE.finditer(page):
            svg = m.group(0)
            # Match only the hero illustration, not the 20+ small UI-chrome icons.
            open_tag = svg[: svg.find(">") + 1]
            if self._hero_class_marker not in open_tag:
                continue
            uri = svg_sanitize.svg_to_data_uri(svg)
            if uri:
                return uri
        return None


@dataclass(frozen=True)
class TinyviewPlugin:
    """tinyview.com — a JS app whose served HTML is a loading skeleton.

    The generic scan found `Tinyview_skeleton-animation.gif` and cached it as
    the lead image, so the reader rendered the site's pre-hydration skeleton
    where the comic should be. The panels *are* in that HTML though: every one
    is an absolute cdn.tinyview.com URL under the entry's own path
    (cdn.tinyview.com/<comic>/<yyyy>/<mm>/<dd>/<slug>/IMG_*.jpeg), so no
    rendering is needed — only knowing where to look.

    Strips are multi-panel; this returns the first panel as the lead image.
    Turn on the feed's "inject source images" preference to show them all.
    """

    host: str = "tinyview.com"
    _CDN: str = "cdn.tinyview.com"
    # Everything under assets.tinyview.com is site chrome: the skeleton
    # animation, the wordmark, the icons8 button set, favicons.
    _ASSETS: str = "assets.tinyview.com"

    def _is_target(self, url: str) -> bool:
        return self.host in urlparse(url).netloc.lower()

    def should_bypass_cached_url(self, *, entry_link: str, cached_url: str) -> bool:
        if not self._is_target(entry_link):
            return False
        return self._CDN not in urlparse(cached_url).netloc.lower()

    def extra_candidate_attrs(self, *, source_url: str) -> tuple[str, ...]:
        return ()

    def source_score_adjustment(self, *, source_url: str, attrs: dict[str, str], resolved_url: str) -> int:
        if not self._is_target(source_url):
            return 0
        host = urlparse(resolved_url).netloc.lower()
        if self._ASSETS in host:
            return -200
        if self._CDN in host:
            return 100
        return 0


DEFAULT_LEAD_IMAGE_PLUGINS: tuple[LeadImagePlugin, ...] = (
    TinyviewPlugin(),
    AnalogueLeadImagePlugin(),
    StandardEbooksLeadImagePlugin(),
    FutureSiteLeadImagePlugin(),
    WordPressComicPlugin(),
    GunnerkriggPlugin(),
    DresdenCodakPlugin(),
    PennyArcadePlugin(),
    SMBCPlugin(),
    MisfilePlugin(),
    JohnnyWanderPlugin(),
    PoorlyDrawnLinesPlugin(),
    TumblrPlugin(),
    QwantzPlugin(),
    WebtoonsPlugin(),
    TapasPlugin(),
    BloggerPlugin(),
    TheRockCocksPlugin(),
    ComicFuryPlugin(),
    OglafPlugin(),
    ComicEaselPlugin(),
    WinPenPackPlugin(),
)
