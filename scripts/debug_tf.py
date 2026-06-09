import urllib.request, re
from urllib.parse import urljoin

headers = {"User-Agent": "Mozilla/5.0"}
req = urllib.request.Request("https://torrentfreak.com/?p=278542", headers=headers)
resp = urllib.request.urlopen(req, timeout=15)
html = resp.read().decode("utf-8", errors="replace")

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"\s+([\w:-]+)(?:\s*=\s*(?:[\x27\x22]([^\x27\x22]*)[\x27\x22]|([^\s>]*)))?", re.IGNORECASE)
_LOGO_URL_PATTERNS = re.compile(
    r"(?:favicon|site[-_]logo|wordmark|site[-_]icon|app[-_]icon|social[-_]icon|apple-touch-icon|android-chrome|logo|sponsor|/flags/|/awards?/|btn_donate|donate[-_]btn|divider|separator|share[-_]image)",
    re.IGNORECASE,
)
_LEAD_IMAGE_MIN_WIDTH = 200
_LEAD_IMAGE_MIN_HEIGHT = 100
base_url = "https://torrentfreak.com/?p=278542"

for m in _IMG_TAG_RE.finditer(html):
    tag = m.group(0)
    if "imdb" not in tag.lower():
        continue
    attrs = {}
    for am in _ATTR_RE.finditer(tag):
        k = (am.group(1) or "").strip().lower()
        v = (am.group(2) or am.group(3) or "").strip()
        if k and v:
            attrs[k] = v
    src = attrs.get("src", "")
    alt = attrs.get("alt", "")
    w = attrs.get("width", "")
    h = attrs.get("height", "")
    resolved = urljoin(base_url, src)
    print("SRC:", src)
    print("ALT:", repr(alt), "W:", w, "H:", h)
    alt_title = (alt + " " + attrs.get("title", "")).strip()
    logo_alt = _LOGO_URL_PATTERNS.search(alt_title)
    print("  Logo in alt_title", repr(alt_title), ":", logo_alt.group() if logo_alt else None)
    logo_url = _LOGO_URL_PATTERNS.search(resolved)
    print("  Logo in URL:", logo_url.group() if logo_url else None)
    try:
        wi, hi = int(w), int(h)
        print("  Dims OK:", wi >= _LEAD_IMAGE_MIN_WIDTH and hi >= _LEAD_IMAGE_MIN_HEIGHT)
    except Exception:
        print("  Dims: N/A")
    print()
