import urllib.request, re
headers = {"User-Agent": "Mozilla/5.0"}
req = urllib.request.Request("https://torrentfreak.com/?p=278542", headers=headers)
resp = urllib.request.urlopen(req, timeout=15)
html = resp.read().decode("utf-8", errors="replace")

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_AUTHOR_CTX = re.compile(r"""class=["'][^"']*(?:\bauthor\b|\bbio\b|\bbyline\b|\bspeaker\b|\bcontributor\b)""", re.IGNORECASE)
_CHROME_CTX = re.compile(r"""class=["'][^"']*(?:\bbranding\b|\bsite-logo\b|\bsite-header\b|\bsite-name\b|\bsubscribe-dropdown\b|\brelated-content\b|\brelated-posts\b|\brecent-posts\b|\bmobile-banner\b)""", re.IGNORECASE)

keywords = ("imdblogo", "playimdb", "addplay", "imdborder")
for m in _IMG_TAG_RE.finditer(html):
    tag = m.group(0)
    if not any(kw in tag.lower() for kw in keywords):
        continue
    context_before = html[max(0, m.start()-500):m.start()]
    author = _AUTHOR_CTX.search(context_before)
    chrome = _CHROME_CTX.search(context_before)
    print("IMG:", tag[:80])
    print("  author_ctx:", author.group()[:80] if author else None)
    print("  chrome_ctx:", chrome.group()[:80] if chrome else None)
    print()
