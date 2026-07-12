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
    # align is a legacy presentational attr some feeds still use for table
    # layout (Old New Thing centers its spanning before/after rows with
    # td align="center"); value-constrained, no scripting surface.
    "td": frozenset({"colspan", "rowspan", "align"}),
    "th": frozenset({"colspan", "rowspan", "scope", "align"}),
    "tr": frozenset({"align"}),
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
# Sphinx/dvisvgm math (eli.thegreenplace.net etc.) ships each formula's true
# rendered height as an inline ``style="height: Npx"``. We strip inline styles, so
# this height is lifted onto a real (allowlisted) ``height`` attribute instead; CSS
# then honors it plus the valign-* baseline class rather than flattening every
# glyph to one size. _MATH_SCALE is the single tuning lever — 1.0 reproduces the
# author's px faithfully; raise it (e.g. 1.15) to enlarge all math (re-ingest to apply).
_STYLE_HEIGHT_PX_RE = re.compile(r"height\s*:\s*(\d+(?:\.\d+)?)px", re.IGNORECASE)
_MATH_SCALE = 1.0
# Feeds sometimes embed HTML as entity-escaped text inside element text nodes
# (e.g. &lt;em&gt;Title&lt;/em&gt; stored as literal "<em>Title</em>" in the
# NavigableString). Strip these from non-code contexts to prevent raw tag names
# appearing as visible page text.
_PSEUDO_TAG_IN_TEXT_RE = re.compile(
    r"</?(?:a|abbr|b|cite|code|del|em|i|ins|mark|q|s|small|span|strong|sub|sup|u)"
    r"(?:\s[^>]{0,100})?/?>",
    re.IGNORECASE,
)
_NO_STRIP_ANCESTORS = frozenset({"code", "pre", "samp", "kbd", "var"})

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


def _promote_math_height(tag, style_value: str) -> None:
    """Lift a Sphinx/dvisvgm math element's ``height: Npx`` inline style onto a real
    ``height`` attribute so the true rendered size (and the px-based valign baseline)
    survive the style-strip below. ``height`` is already in the img allowlist."""
    if not style_value or tag.attrs.get("height"):
        return
    m = _STYLE_HEIGHT_PX_RE.search(style_value)
    if m:
        tag.attrs["height"] = str(max(1, round(float(m.group(1)) * _MATH_SCALE)))


def _is_math_img(tag) -> bool:
    """True for Sphinx-math <img> (inline ``valign-*`` or display ``align-center``)."""
    cls = tag.attrs.get("class") or []
    cls = cls if isinstance(cls, list) else [cls]
    return any(str(c).startswith("valign-") or str(c) == "align-center" for c in cls)


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

    # Convert <object type="image/svg+xml" data="url"> to <img src="url">.
    # Sphinx-based blogs (e.g. eli.thegreenplace.net) emit math formulas this way;
    # the element text is the LaTeX source used as alt. Other <object> types stay
    # in _DROP_TAGS and are decomposed below.
    for obj in soup.find_all("object"):
        obj_type = str(obj.attrs.get("type", "")).strip().lower()
        data_url = str(obj.attrs.get("data", "")).strip()
        if obj_type == "image/svg+xml" and data_url and _is_safe_attr_url("src", data_url):
            img = soup.new_tag("img", src=data_url)
            alt = " ".join(obj.get_text(separator=" ", strip=True).split())
            if alt:
                img.attrs["alt"] = alt
            obj_class = obj.attrs.get("class")
            classes = list(obj_class) if isinstance(obj_class, list) else ([obj_class] if obj_class else [])
            classes.append("lectio-math-svg")
            img.attrs["class"] = classes
            # The object's style carries the true rendered px height; the new img has
            # no style of its own, so copy it across before the strip pass below.
            _promote_math_height(img, str(obj.attrs.get("style", "")))
            obj.replace_with(img)

    # Pre-existing math <img> (PNG inline formulas, older display math) carry their
    # true height inline too; lift it before the generic style-strip loop runs.
    for img in soup.find_all("img"):
        if _is_math_img(img):
            _promote_math_height(img, str(img.attrs.get("style", "")))

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

    # Render pseudo-HTML tags that feeds sometimes embed as text inside elements
    # (e.g. &lt;em&gt;title&lt;/em&gt; in link text, stored as literal "<em>" in
    # the NavigableString). Re-parse as HTML so they become real elements and
    # render correctly. Skip code/pre contexts where literal angle brackets are
    # intentional. Newly inserted elements are sanitized inline.
    for text_node in soup.find_all(string=True):
        text = str(text_node)
        if "<" not in text or isinstance(text_node, Comment):
            continue
        if any((p.name or "").lower() in _NO_STRIP_ANCESTORS for p in text_node.parents):
            continue
        if not _PSEUDO_TAG_IN_TEXT_RE.search(text):
            continue
        frag = BeautifulSoup(text, "html.parser")
        for el in list(frag.find_all(True)):
            name = (el.name or "").lower()
            if name not in _ALLOWED_TAGS:
                el.unwrap()
                continue
            allowed_el = _ALLOWED_ATTRS.get(name, frozenset())
            for attr in list(el.attrs):
                la = attr.lower()
                if la.startswith("on") or la == "style" or (la not in allowed_el and la not in _GLOBAL_ALLOWED_ATTRS):
                    del el.attrs[attr]
                elif la in _URL_ATTRS and not _is_safe_attr_url(la, str(el.attrs.get(attr, ""))):
                    del el.attrs[attr]
        # Splice only the parsed children, not the BeautifulSoup document wrapper
        # (replace_with(frag) can introduce <html>/<body> tags around the content).
        for child in list(frag.contents):
            text_node.insert_before(child)
        text_node.extract()

    return str(soup)
