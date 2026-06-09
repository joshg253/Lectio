import sys, re
sys.path.insert(0, '/app')

# Simulate the scan with all new rules applied
import urllib.request
from urllib.parse import urljoin, urlparse

headers = {"User-Agent": "Lectio/0.1 (+https://localhost)"}
req = urllib.request.Request("https://torrentfreak.com/?p=278542", headers=headers)
resp = urllib.request.urlopen(req, timeout=15)
html = resp.read().decode("utf-8", errors="replace")

_LOGO_URL_PATTERNS = re.compile(
    r"(?:favicon|site[-_]logo|wordmark|site[-_]icon|app[-_]icon|social[-_]icon|apple-touch-icon|android-chrome|(?<![a-zA-Z0-9])logo|sponsor|/flags/|/awards?/|btn_donate|donate[-_]btn|divider|separator|share[-_]image)",
    re.IGNORECASE,
)
_WEBP_SOURCE_SRCSET_RE = re.compile(
    r'<source\b[^>]+type=["\']image/webp["\'][^>]+srcset=["\']([^"\']+)["\']'
    r'|<source\b[^>]+srcset=["\']([^"\']+)["\'][^>]+type=["\']image/webp["\']',
    re.IGNORECASE | re.DOTALL,
)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"""\s+([\w:-]+)(?:\s*=\s*(?:["']([^"']*)["']|([^\s>]*)))?""", re.IGNORECASE)
_SITE_CHROME_CTX = re.compile(r"""class=["'][^"']*(?:\bbranding\b|\bsite-logo\b|\bsite-header\b)""", re.IGNORECASE)
_LEAD_IMAGE_MIN_WIDTH = 200
_LEAD_IMAGE_MIN_HEIGHT = 100
_IMAGE_PATH_SUFFIX_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:[=?#]|$)", re.IGNORECASE)

def parse_srcset(srcset):
    results = []
    for part in srcset.split(","):
        pieces = part.strip().split()
        if pieces:
            results.append(pieces[0])
    return results

base_url = "https://torrentfreak.com/?p=278542"
best_url = None
best_score = -1
_found_first = False

for m in _IMG_TAG_RE.finditer(html):
    ctx = html[max(0, m.start()-500):m.start()]
    if _SITE_CHROME_CTX.search(ctx): continue
    tag = m.group(0)
    attrs = {}
    for am in _ATTR_RE.finditer(tag):
        k=(am.group(1) or "").strip().lower(); v=(am.group(2) or am.group(3) or "").strip()
        if k and v: attrs[k]=v
    
    src = attrs.get("src","")
    if not src or src.startswith("data:"): continue
    resolved = urljoin(base_url, src)
    
    # _is_source_image_tag_acceptable  
    alt = attrs.get("alt",""); title_a = attrs.get("title","")
    alt_title = (alt + " " + title_a).strip()
    w_str=attrs.get("width",""); h_str=attrs.get("height","")
    try: w_int=int(re.match(r"^([0-9]{1,4})",w_str).group(1)) if w_str else None
    except: w_int=None
    try: h_int=int(re.match(r"^([0-9]{1,4})",h_str).group(1)) if h_str else None
    except: h_int=None
    
    if w_int is not None and w_int < _LEAD_IMAGE_MIN_WIDTH: 
        print(f"SKIP (w<{_LEAD_IMAGE_MIN_WIDTH}): {src[:60]}"); continue
    if h_int is not None and h_int < _LEAD_IMAGE_MIN_HEIGHT: 
        print(f"SKIP (h<{_LEAD_IMAGE_MIN_HEIGHT}): {src[:60]}"); continue
    
    if alt_title and _LOGO_URL_PATTERNS.search(alt_title):
        has_dims = (w_int is not None and w_int >= _LEAD_IMAGE_MIN_WIDTH 
                    and h_int is not None and h_int >= _LEAD_IMAGE_MIN_HEIGHT)
        if not has_dims:
            print(f"SKIP (logo alt, no dims): {src[:60]} | alt={alt_title!r}"); continue
        print(f"NOTE (logo alt but has dims, keeping): {src[:60]}")
    
    # _is_image_url_acceptable
    if _LOGO_URL_PATTERNS.search(resolved):
        print(f"SKIP (logo in URL): {src[:60]}"); continue
    if not _IMAGE_PATH_SUFFIX_RE.search(urlparse(resolved).path.lower()):
        print(f"SKIP (no extension): {src[:60]}"); continue
    
    # webp substitution
    _pre_ctx = html[max(0, m.start() - 600):m.start()]
    _pic_pos = _pre_ctx.rfind("<picture")
    if _pic_pos != -1:
        _wm = _WEBP_SOURCE_SRCSET_RE.search(_pre_ctx[_pic_pos:])
        if _wm:
            _wsrcset = _wm.group(1) or _wm.group(2)
            for _wu in parse_srcset(_wsrcset):
                if not _wu or _wu.startswith("data:"): continue
                _wr = urljoin(base_url, _wu)
                if not _LOGO_URL_PATTERNS.search(_wr) and _IMAGE_PATH_SUFFIX_RE.search(urlparse(_wr).path.lower()):
                    resolved = _wr
                    break
    
    # Score
    score = 0
    cls = attrs.get("class","").lower()
    if "hero-image" in cls: score += 120
    if attrs.get("srcset") or attrs.get("data-srcset"): score += 10
    if len(alt) >= 16: score += 5
    if not _found_first:
        score += 10; _found_first = True
    
    print(f"CANDIDATE (score={score}): {resolved[:80]} | alt={alt!r}")
    if score > best_score:
        best_score = score; best_url = resolved

print(f"\nWINNER: {best_url}")
