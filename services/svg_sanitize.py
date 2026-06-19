"""Sanitize inline ``<svg>`` markup so it can be served as a standalone image.

Feeds occasionally express an article's hero/icon as a raw inline ``<svg>``
element in the content HTML (e.g. analogue.co firmware notes). Lectio surfaces
that as the list thumbnail / article lead image by extracting the element,
stripping everything that could execute or phone home, and serving the result
as a ``data:image/svg+xml`` URI that CSS sizes — no rasterization, no outbound
fetch, no new dependency.

Security model (this is the whole point of the feature — "no scripts"):
  - parse + rebuild with BeautifulSoup rather than regex-stripping;
  - drop the dangerous element subtrees outright (``script``, ``style``,
    ``foreignObject``, ``image``, ``use``, ``a``, animation/`set`/`handler`);
  - drop every ``on*`` event handler and any ``href``/``xlink:href`` (so no
    external references / SSRF / javascript: URLs survive);
  - only keep a curated presentation/geometry attribute allowlist, and within
    those reject any ``url(...)`` that isn't an internal ``url(#fragment)``.
"""
from __future__ import annotations

from urllib.parse import quote

# Presentation/structure elements we keep. Everything else (including the
# dangerous set below) is dropped subtree-and-all.
_ALLOWED_TAGS = frozenset({
    "svg", "g", "defs", "title", "desc", "symbol", "marker",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan",
    "lineargradient", "radialgradient", "stop",
    "clippath", "mask",
})
# Elements that can execute, embed, or fetch — remove with their children.
# Note: ``a`` is intentionally NOT here — it's simply not in _ALLOWED_TAGS, so it
# unwraps (drops the link + its href, keeps any geometry children).
_DROP_TAGS = frozenset({
    "script", "style", "foreignobject", "image", "use", "iframe",
    "audio", "video", "animate", "animatetransform", "animatemotion",
    "set", "handler", "filter", "feimage",
})
# Curated attribute allowlist applied to every kept element. No ``href`` /
# ``xlink:href`` (external refs), no ``style`` (url() leaks), no ``on*``.
_ALLOWED_ATTRS = frozenset({
    "xmlns", "viewbox", "version", "preserveaspectratio", "class",
    "width", "height", "x", "y", "x1", "y1", "x2", "y2",
    "cx", "cy", "r", "rx", "ry", "points", "d", "transform", "offset",
    "fill", "fill-rule", "fill-opacity", "clip-rule", "clip-path",
    "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "stroke-dashoffset", "stroke-opacity", "stroke-miterlimit",
    "opacity", "color", "stop-color", "stop-opacity",
    "gradientunits", "gradienttransform", "spreadmethod",
    "clippathunits", "maskunits", "maskcontentunits", "markerwidth",
    "markerheight", "id",
})

# Default color so a ``currentColor``-driven monochrome icon stays visible in a
# standalone <img> (which has no parent to inherit from). Mid-gray reads on both
# light and dark thumbnail slots; theming it is a future refinement.
_CURRENT_COLOR_FALLBACK = "#888"

_MAX_SVG_BYTES = 64_000  # cap; oversized inline SVGs are almost never real thumbnails


def _attr_value_safe(value: str) -> bool:
    """Reject any ``url(...)`` that points outside the document (only ``url(#id)``
    internal fragment references are allowed)."""
    low = value.lower()
    idx = low.find("url(")
    while idx != -1:
        rest = low[idx + 4:].lstrip(" '\"")
        if not rest.startswith("#"):
            return False
        idx = low.find("url(", idx + 4)
    return True


def sanitize_svg(markup: str) -> str | None:
    """Return sanitized standalone ``<svg>`` markup, or ``None`` if unusable.

    ``markup`` should be a single ``<svg>`` element (with its children). The
    result is safe to embed as a ``data:image/svg+xml`` URI.
    """
    if not markup or "<svg" not in markup.lower():
        return None
    from bs4 import BeautifulSoup, Comment

    soup = BeautifulSoup(markup, "html.parser")
    root = soup.find("svg")
    if root is None:
        return None

    for comment in root.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Scrub the root <svg> itself (it can carry e.g. onload=) plus all descendants.
    for tag in [root, *root.find_all(True)]:
        if tag is not root and tag.parent is None:
            continue  # already removed when an ancestor was dropped/unwrapped
        name = (tag.name or "").lower()
        if tag is not root and name in _DROP_TAGS:
            tag.decompose()
            continue
        if tag is not root and name not in _ALLOWED_TAGS:
            tag.unwrap()  # keep geometry children, drop the unknown wrapper
            continue
        for attr_name in list(tag.attrs):
            la = attr_name.lower()
            keep = (
                not la.startswith("on")
                and la != "style"
                and "href" not in la  # href, xlink:href, etc.
                and la in _ALLOWED_ATTRS
                and _attr_value_safe(str(tag.attrs.get(attr_name, "")))
            )
            if not keep:
                del tag.attrs[attr_name]

    # Re-fetch root attrs after the scrub above.
    if root.name is None or root.name.lower() != "svg":
        return None
    root.attrs.setdefault("xmlns", "http://www.w3.org/2000/svg")

    rebuilt = str(root)
    if "currentColor".lower() in rebuilt.lower() and "color=" not in rebuilt.lower():
        root.attrs["color"] = _CURRENT_COLOR_FALLBACK
        rebuilt = str(root)

    if not rebuilt or len(rebuilt.encode("utf-8")) > _MAX_SVG_BYTES:
        return None
    # A sanitized shell with no drawing primitives isn't a real image.
    if not any(t in rebuilt.lower() for t in ("<path", "<rect", "<circle", "<ellipse",
                                              "<line", "<polyline", "<polygon", "<text")):
        return None
    return rebuilt


def svg_to_data_uri(markup: str) -> str | None:
    """Sanitize ``markup`` and return a ``data:image/svg+xml`` URI, or ``None``."""
    cleaned = sanitize_svg(markup)
    if cleaned is None:
        return None
    return "data:image/svg+xml," + quote(cleaned, safe="")
