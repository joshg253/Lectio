"""Per-site adapters for article capture.

The counterpart to :mod:`services.lead_image_plugins`, for the article *body*
rather than its lead image. Two things a site can ask for:

- **A capture mode.** Readability is right for prose and wrong for a page whose
  content IS a set of images — it keeps the one it scores highest and drops the
  rest.
- **Extra embed HTML.** A player that the page fills in with JS is not in the
  HTML we fetched, so nothing downstream can recover it. A plugin that knows the
  site's resolver can fetch the embed and hand it back.

Both hooks are optional and default to "no opinion", so a site with no plugin
behaves exactly as before.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from services import url_guard

LOGGER = logging.getLogger(__name__)

_EMBED_FETCH_TIMEOUT = 8.0


class SiteContentPlugin(Protocol):
    def handles(self, *, source_url: str) -> bool: ...

    # True when readability would mangle this page and the whole body is wanted.
    def prefers_full_page(self, *, source_url: str) -> bool: ...

    # CSS selectors to remove before extraction. Full-page capture keeps
    # everything by design, including chrome the page never shows; a site that
    # asks for full-page usually has to name that chrome itself.
    def strip_selectors(self, *, source_url: str) -> tuple[str, ...]: ...

    # Extra HTML to append to the captured body — an embed the page itself only
    # produces via JS. Returns None when there is nothing to add. May make ONE
    # network request; must never raise.
    def extra_embed_html(self, *, source_url: str, raw_html: str) -> str | None: ...


@dataclass(frozen=True)
class BasslessonsPlugin:
    """basslessons.be transcription pages: sheet-music scans plus a JS-loaded video.

    The body is ``div.transImgBorders`` holding N sheet pages (``/partituren/
    <id>-1.png`` …). Readability keeps one of them and scores the cookie banner
    above the rest, so these pages capture as full-page.

    The video is NOT in the HTML — the page ships an empty ``div.videoMask``
    ("Searching far and wide for the video") and fills it from a resolver that
    is reachable server-side with no auth and no JS:

        POST /ajax/a_transcriptionVideo.php  trans_id=<the ?i= value>
        -> {"status":"success","message":"<iframe … youtube-nocookie.com/embed/… >"}

    ``youtube-nocookie.com`` is already on the sanitizer's embed host allowlist,
    so the returned iframe survives sanitization.
    """

    _HOST = "basslessons.be"
    _PATH = "/transcriptions.php"
    _RESOLVER = "https://basslessons.be/ajax/a_transcriptionVideo.php"
    # Only an iframe, and only one — the resolver's payload is site-controlled
    # but still third-party text, so it is matched rather than trusted wholesale.
    _IFRAME_RE = re.compile(r"<iframe\b[^>]*>(?:</iframe\s*>)?", re.IGNORECASE)

    def _host_matches(self, source_url: str) -> bool:
        try:
            host = (urlparse(source_url).netloc or "").lower()
        except ValueError:
            return False
        host = host.split(":", 1)[0]
        return host == self._HOST or host.endswith("." + self._HOST)

    def _transcription_id(self, source_url: str) -> str | None:
        """The ``?i=`` value, which is also the resolver's ``trans_id``."""
        try:
            parsed = urlparse(source_url)
        except ValueError:
            return None
        if parsed.path.lower() != self._PATH:
            return None
        value = (parse_qs(parsed.query).get("i") or [""])[0].strip()
        # Digits only: it goes into a POST body, and the page's own links never
        # carry anything else.
        return value if value.isdigit() else None

    def handles(self, *, source_url: str) -> bool:
        return self._host_matches(source_url) and self._transcription_id(source_url) is not None

    def prefers_full_page(self, *, source_url: str) -> bool:
        return self.handles(source_url=source_url)

    def strip_selectors(self, *, source_url: str) -> tuple[str, ...]:
        # The consent banner ships as `display: none` and is revealed by JS, so
        # it is not visible on the page — but full-page capture keeps every node,
        # and it sits first in the DOM. Left in, every transcription opens with
        # ~700 characters of cookie policy ahead of the music.
        return ("#cookieInfoBanner", ".cookie-info-banner")

    def extra_embed_html(self, *, source_url: str, raw_html: str) -> str | None:
        trans_id = self._transcription_id(source_url) if self._host_matches(source_url) else None
        if trans_id is None:
            return None
        try:
            with url_guard.build_client(timeout=_EMBED_FETCH_TIMEOUT) as client:
                response = url_guard.safe_post(
                    client, self._RESOLVER, data={"trans_id": trans_id}
                )
            if not response.is_success:
                return None
            payload = json.loads(response.text)
        except Exception:  # noqa: BLE001 — a missing video must not fail the capture
            LOGGER.debug("basslessons: video resolve failed for %s", source_url, exc_info=True)
            return None
        if not isinstance(payload, dict) or payload.get("status") != "success":
            return None
        match = self._IFRAME_RE.search(str(payload.get("message") or ""))
        if match is None:
            return None
        return match.group(0)


DEFAULT_SITE_CONTENT_PLUGINS: tuple[SiteContentPlugin, ...] = (
    BasslessonsPlugin(),
)


def plugin_for(
    source_url: str, plugins: tuple[SiteContentPlugin, ...] = DEFAULT_SITE_CONTENT_PLUGINS
) -> SiteContentPlugin | None:
    for plugin in plugins:
        try:
            if plugin.handles(source_url=source_url):
                return plugin
        except Exception:  # noqa: BLE001 — one bad plugin must not break capture
            LOGGER.debug("site content plugin %r raised on %s", plugin, source_url, exc_info=True)
    return None


def prefers_full_page(
    source_url: str, plugins: tuple[SiteContentPlugin, ...] = DEFAULT_SITE_CONTENT_PLUGINS
) -> bool:
    plugin = plugin_for(source_url, plugins)
    if plugin is None:
        return False
    try:
        return bool(plugin.prefers_full_page(source_url=source_url))
    except Exception:  # noqa: BLE001
        return False


def strip_selectors(
    source_url: str, plugins: tuple[SiteContentPlugin, ...] = DEFAULT_SITE_CONTENT_PLUGINS
) -> tuple[str, ...]:
    plugin = plugin_for(source_url, plugins)
    if plugin is None:
        return ()
    try:
        return tuple(plugin.strip_selectors(source_url=source_url))
    except Exception:  # noqa: BLE001
        return ()


def extra_embed_html(
    source_url: str,
    raw_html: str,
    plugins: tuple[SiteContentPlugin, ...] = DEFAULT_SITE_CONTENT_PLUGINS,
) -> str | None:
    plugin = plugin_for(source_url, plugins)
    if plugin is None:
        return None
    try:
        return plugin.extra_embed_html(source_url=source_url, raw_html=raw_html)
    except Exception:  # noqa: BLE001 — never fail a capture over a missing embed
        LOGGER.debug("site content plugin %r embed failed for %s", plugin, source_url, exc_info=True)
        return None
