"""Allowlist HTML sanitizer for untrusted feed / fetched content.

This is Lectio's single sanitization chokepoint. ``reader`` runs feedparser with
``sanitize_html=True`` and does no sanitizing of its own, but feedparser's
sanitizer *destroys* (rather than escapes) anything off its allowlist — iframes,
SVG, MathML, audio/video, and many attributes — which silently strips embeds
from articles. Lectio instead parses feeds with sanitization disabled and runs
content through this module, so we keep what's safe (including embeds from a
curated host allowlist) and drop only what's dangerous.

Regex sanitizing is unsafe (unquoted ``onerror=``, ``href="javascript:"``, and
countless encoding tricks slip through), so we parse and rebuild with
BeautifulSoup instead.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from services import svg_sanitize

_ALLOWED_TAGS = frozenset({
    "a", "abbr", "address", "article", "aside", "b", "blockquote", "br", "caption",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl",
    "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "i", "img", "ins", "kbd", "li", "main", "mark", "nav", "ol", "p",
    "picture", "pre", "q", "s", "samp", "section", "small", "span", "strong", "sub",
    "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr",
    "u", "ul", "var", "wbr", "audio", "video", "source", "iframe",
})
# Dangerous tags whose entire subtree is dropped. Anything else not in the allow
# list is unwrapped (its text/children kept) rather than deleted. ``svg`` and
# ``math`` are handled specially (sanitized in place), so they're not here.
_DROP_TAGS = frozenset({
    "script", "style", "object", "embed", "form", "link", "meta", "base",
    "noscript", "template", "applet", "frame", "frameset", "title",
    "button", "input", "select", "textarea", "option",
})
_ALLOWED_ATTRS = {
    "a": frozenset({"href", "title"}),
    # width/height feed the lead-image scorer (rank + size-filter); sizes/decoding
    # are harmless responsive hints. data-src/data-srcset/data-lazy-src/data-original/
    # data-image are lazyload sources the lead-image extractor (and lazy-media
    # render normalizer) read — stripping them broke lead images on inline feeds.
    "img": frozenset({
        "src", "srcset", "alt", "title", "loading", "width", "height", "sizes",
        "decoding", "data-src", "data-srcset", "data-lazy-src", "data-original",
        "data-image",
    }),
    "source": frozenset({"src", "srcset", "type", "media", "width", "height", "sizes", "data-srcset"}),
    "video": frozenset({"src", "controls", "poster", "preload", "width", "height"}),
    "audio": frozenset({"src", "controls", "preload"}),
    "iframe": frozenset({
        "src", "width", "height", "allow", "allowfullscreen", "frameborder",
        "loading", "title", "referrerpolicy", "scrolling",
    }),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "time": frozenset({"datetime"}),
}
# Attributes kept on every element. class carries no styling effect (feed CSS is
# never loaded) but Lectio's content-cleanup passes key off it — e.g. stripping
# "RELATED STORIES" / NASA-block / Ghost audio-card / embed-container widgets and
# detecting webcomic/YouTube-embed figures. Dropping it silently broke those
# cleanups on freshly-ingested (sanitized) content. (id is deliberately NOT kept:
# a feed id could collide with the app's own element IDs the page JS looks up.)
_GLOBAL_ALLOWED_ATTRS = frozenset({"class"})
# Single-URL attributes scheme-validated against javascript:/data:/vbscript:. The
# lazyload data-* attrs are included so an unsafe value can't survive sanitization
# and later be promoted into src by the lazy-media normalizer. (srcset/data-srcset
# are multi-URL and not validated here, matching the existing srcset handling.)
_URL_ATTRS = frozenset({"href", "src", "poster", "data-src", "data-lazy-src", "data-original", "data-image"})
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x20]")

# Hosts whose <iframe> embeds are allowed. Matched against the iframe src host by
# exact match or dot-suffix (so "www.youtube.com" and "youtube.com" both match
# "youtube.com"). Players run sandboxed (see _IFRAME_SANDBOX) so they can't reach
# Lectio's origin. Curated for video/audio + a few social/code embeds.
_EMBED_HOST_ALLOWLIST = frozenset({
    "youtube.com", "youtube-nocookie.com", "youtu.be",
    "player.vimeo.com", "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "soundcloud.com",
    "bandcamp.com",
    "spotify.com",
    "platform.twitter.com",
    "codepen.io",
    "redditmedia.com",
    "archive.org",
})
# allow-same-origin refers to the *embed's* origin (a different host), so the
# player runs while remaining unable to script Lectio's page.
_IFRAME_SANDBOX = "allow-scripts allow-same-origin allow-popups allow-presentation allow-forms"

# MathML elements kept (attribute-stripped except a safe few). Anything else
# inside <math> is unwrapped.
_MATHML_TAGS = frozenset({
    "math", "maction", "menclose", "merror", "mfenced", "mfrac", "mglyph", "mi",
    "mlabeledtr", "mmultiscripts", "mn", "mo", "mover", "mpadded", "mphantom",
    "mroot", "mrow", "ms", "mspace", "msqrt", "mstyle", "msub", "msubsup", "msup",
    "mtable", "mtd", "mtext", "mtr", "munder", "munderover", "semantics",
    "annotation", "annotation-xml",
})
_MATHML_ATTRS = frozenset({
    "display", "displaystyle", "mathvariant", "dir", "href", "mathcolor",
    "mathbackground", "scriptlevel", "columnalign", "rowalign", "open", "close",
    "separators", "stretchy", "fence", "accent", "notation",
})


def _is_safe_attr_url(attr: str, value: str) -> bool:
    """Reject javascript:/vbscript:/data: (and control-char-obfuscated variants);
    allow relative URLs and http(s) (plus mailto/tel for href)."""
    if not value or not value.strip():
        return False
    stripped = _CONTROL_CHARS_RE.sub("", value).lower()
    if stripped.startswith(("javascript:", "vbscript:", "data:")):
        return False
    try:
        scheme = urlparse(value).scheme.lower()
    except ValueError:
        return False
    if scheme == "":
        return True  # relative URL
    if attr == "href":
        return scheme in ("http", "https", "mailto", "tel")
    return scheme in ("http", "https")


def _embed_host_allowed(src: str) -> bool:
    """True if an iframe src points at a host on the embed allowlist (https only)."""
    try:
        parsed = urlparse(src)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("https", ""):
        return False  # require TLS for embeds (relative src can't be an embed anyway)
    host = (parsed.hostname or "").lower().lstrip(".")
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _EMBED_HOST_ALLOWLIST)


def _sanitize_iframe(tag) -> bool:
    """Clean an <iframe> in place. Return True to keep it, False to drop it."""
    src = str(tag.attrs.get("src", "")).strip()
    if not _embed_host_allowed(src):
        return False
    allowed = _ALLOWED_ATTRS["iframe"]
    for attr_name in list(tag.attrs):
        if attr_name.lower() not in allowed:
            del tag.attrs[attr_name]
    # Force a sandbox + conservative referrer regardless of what the feed set.
    tag.attrs["sandbox"] = _IFRAME_SANDBOX
    tag.attrs["referrerpolicy"] = "strict-origin-when-cross-origin"
    tag.attrs["loading"] = "lazy"
    return True


def _sanitize_mathml(root) -> None:
    """Strip attributes/unknown tags inside a <math> subtree, in place."""
    for el in root.find_all(True):
        name = (el.name or "").lower()
        if name not in _MATHML_TAGS:
            el.unwrap()
            continue
        for attr_name in list(el.attrs):
            la = attr_name.lower()
            if la not in _MATHML_ATTRS or la.startswith("on"):
                del el.attrs[attr_name]
            elif la == "href" and not _is_safe_attr_url("href", str(el.attrs.get(attr_name, ""))):
                del el.attrs[attr_name]


def sanitize_html(content: str) -> str:
    """Return ``content`` with only allowlisted tags/attributes (embeds kept)."""
    if not content:
        return content
    from bs4 import BeautifulSoup, Comment

    soup = BeautifulSoup(content, "html.parser")
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Inline SVG: sanitize the whole subtree via the dedicated SVG cleaner, then
    # splice the cleaned markup back (or drop it if unusable).
    for svg in soup.find_all("svg"):
        cleaned = svg_sanitize.sanitize_svg(str(svg))
        if cleaned:
            svg.replace_with(BeautifulSoup(cleaned, "html.parser"))
        else:
            svg.decompose()

    for math in soup.find_all("math"):
        _sanitize_mathml(math)

    for tag in soup.find_all(True):
        if tag.parent is None:
            continue  # already removed along with a decomposed ancestor
        name = (tag.name or "").lower()
        if name in ("svg", "math") or tag.find_parent(["svg", "math"]) is not None:
            continue  # SVG/MathML subtrees were sanitized in their own passes
        if name in _DROP_TAGS:
            tag.decompose()
            continue
        if name == "iframe":
            if not _sanitize_iframe(tag):
                tag.decompose()
            continue
        if name not in _ALLOWED_TAGS:
            tag.unwrap()  # keep inner text/children, drop the unknown wrapper
            continue
        allowed = _ALLOWED_ATTRS.get(name, frozenset())
        for attr_name in list(tag.attrs):
            la = attr_name.lower()
            if la.startswith("on") or la == "style" or (la not in allowed and la not in _GLOBAL_ALLOWED_ATTRS):
                del tag.attrs[attr_name]
                continue
            if la in _URL_ATTRS and not _is_safe_attr_url(la, str(tag.attrs.get(attr_name, ""))):
                del tag.attrs[attr_name]
    return str(soup)
