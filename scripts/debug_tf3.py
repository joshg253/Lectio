import sys
sys.path.insert(0, '/app')
import urllib.request, re
from urllib.parse import urljoin, urlparse

headers = {"User-Agent": "Lectio/0.1 (+https://localhost)"}
req = urllib.request.Request("https://torrentfreak.com/?p=278542", headers=headers)
resp = urllib.request.urlopen(req, timeout=15)
html = resp.read().decode("utf-8", errors="replace")
final_url = "https://torrentfreak.com/?p=278542"
base_url = final_url

# Reproduce the scan logic inline (no feedparser needed)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"""\s+([\w:-]+)(?:\s*=\s*(?:["']([^"']*)["']|([^\s>]*)))?""", re.IGNORECASE)
_LOGO_URL_PATTERNS = re.compile(
    r"(?:favicon|site[-_]logo|wordmark|site[-_]icon|app[-_]icon|social[-_]icon|apple-touch-icon|android-chrome|logo|sponsor|/flags/|/awards?/|btn_donate|donate[-_]btn|divider|separator|share[-_]image)",
    re.IGNORECASE,
)
_AVATAR_HINTS = re.compile(r"(?:avatar|gravatar|author.photo|profile.pic|headshot|speaker.photo|bio.photo|byline.photo)", re.IGNORECASE)
_SITE_CHROME_CTX = re.compile(r"""class=["'][^"']*(?:\bbranding\b|\bsite-logo\b|\bsite-header\b|\bsite-name\b|\bsubscribe-dropdown\b|\brelated-content\b|\brelated-posts\b|\brecent-posts\b|\bmobile-banner\b)""", re.IGNORECASE)
_AUTHOR_CTX = re.compile(r"""class=["'][^"']*(?:\bauthor\b|\bbio\b|\bbyline\b|\bspeaker\b|\bcontributor\b)""", re.IGNORECASE)
_SITE_CHROME_PATH = re.compile(r"/wp-content/(?:themes|plugins)/|sidebar|opengraph", re.IGNORECASE)
_SITE_CHROME_DOMAIN = re.compile(r"(?:resources\.blogblog\.com|i\.ytimg\.com|img\.youtube\.com)", re.IGNORECASE)
_TINY_DIM_RE = re.compile(r"(?:^|[/_.-])([0-9]{1,2})x([0-9]{1,2})(?:[/_.\-a-z]|$)", re.IGNORECASE)
_LEAD_IMAGE_MIN_WIDTH = 200
_LEAD_IMAGE_MIN_HEIGHT = 100
_IMAGE_PATH_SUFFIX_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:[=?#]|$)", re.IGNORECASE)

best_url = None
best_score = -1
best_alt = None

for tag_m in _IMG_TAG_RE.finditer(html):
    ctx = html[max(0, tag_m.start()-500):tag_m.start()]
    if _AUTHOR_CTX.search(ctx):
        continue
    if _SITE_CHROME_CTX.search(ctx):
        continue
    tag = tag_m.group(0)
    attrs = {}
    for am in _ATTR_RE.finditer(tag):
        k = (am.group(1) or "").strip().lower()
        v = (am.group(2) or am.group(3) or "").strip()
        if k and v:
            attrs[k] = v
    
    src = attrs.get("src", "")
    if not src or src.startswith("data:"):
        continue
    resolved = urljoin(base_url, src)
    
    # _is_source_image_tag_acceptable
    combined = " ".join([attrs.get("class",""), attrs.get("id",""), attrs.get("alt",""), attrs.get("title",""), resolved])
    if _AVATAR_HINTS.search(combined):
        print(f"SKIP (avatar hints): {src[:60]}")
        continue
    alt_title = (attrs.get("alt","") + " " + attrs.get("title","")).strip()
    if alt_title and _LOGO_URL_PATTERNS.search(alt_title):
        print(f"SKIP (logo in alt): {src[:60]} | alt={alt_title!r}")
        continue
    w_str = attrs.get("width","")
    h_str = attrs.get("height","")
    try:
        w_int = int(re.match(r"^([0-9]{1,4})", w_str).group(1)) if w_str and re.match(r"^([0-9]{1,4})", w_str) else None
    except: w_int = None
    try:
        h_int = int(re.match(r"^([0-9]{1,4})", h_str).group(1)) if h_str and re.match(r"^([0-9]{1,4})", h_str) else None
    except: h_int = None
    if w_int is not None and w_int < _LEAD_IMAGE_MIN_WIDTH:
        print(f"SKIP (width too small {w_int}): {src[:60]}")
        continue
    if h_int is not None and h_int < _LEAD_IMAGE_MIN_HEIGHT:
        print(f"SKIP (height too small {h_int}): {src[:60]}")
        continue

    # _is_image_url_acceptable
    parsed = urlparse(resolved)
    path = parsed.path.lower()
    if _LOGO_URL_PATTERNS.search(resolved):
        print(f"SKIP (logo in URL): {src[:60]}")
        continue
    if _SITE_CHROME_PATH.search(path):
        print(f"SKIP (site chrome path): {src[:60]}")
        continue
    if _SITE_CHROME_DOMAIN.search(parsed.netloc):
        print(f"SKIP (site chrome domain): {src[:60]}")
        continue
    # Check extension
    if not _IMAGE_PATH_SUFFIX_RE.search(path):
        print(f"SKIP (no image extension): {src[:60]}")
        continue

    # Score
    score = 0
    cls = attrs.get("class","").lower()
    if "hero-image" in cls: score += 120
    if "hero" in cls: score += 40
    if any(t in cls for t in ("featured","lead","article-image","main-image","entry-image")): score += 30
    if (attrs.get("fetchpriority","")).lower() == "high": score += 40
    if attrs.get("srcset") or attrs.get("data-srcset"): score += 10
    alt = attrs.get("alt","")
    if len(alt) >= 40: score += 10
    elif len(alt) >= 16: score += 5

    print(f"CANDIDATE (score={score}): {src[:80]} | alt={alt!r}")
    if score > best_score:
        best_score = score
        best_url = resolved
        best_alt = alt or None

print(f"\nBEST: {best_url} (score={best_score})")
