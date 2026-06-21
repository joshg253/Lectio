"""extract_source_gallery_urls collects all acceptable article images from a
cached source page (for feeds with image-less bodies, e.g. paizo), applying the
same author/site-chrome/related/junk filters as the lead-image scraper."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from services.lead_images import LeadImageService


def _svc(tmp_path: Path) -> LeadImageService:
    def get_meta():
        c = sqlite3.connect(str(tmp_path / "m.sqlite"))
        c.row_factory = sqlite3.Row
        return c

    return LeadImageService(
        get_meta_connection=get_meta,
        get_reader=lambda: None,
        user_agent="LectioTest/1.0",
        extract_video_id=lambda link: None,
    )


# Realistic spacing: the 500-char context lookback (shared with the lead scraper)
# would false-positive if chrome sits right next to an article image, so pad the
# article body the way a real post does.
_PAD = "<p>" + ("lorem ipsum dolor sit amet " * 30) + "</p>"
PAGE = f"""
<html><body>
  <header class="site-header"><img src="https://x.test/logo.png"></header>
  {_PAD}
  <article>
    <img src="https://x.test/img/one.jpg">
    {_PAD}
    <img src="https://x.test/img/two.jpg">
    {_PAD}
    <img src="https://x.test/img/lead.jpg">
  </article>
  <aside class="related-posts"><img src="https://x.test/img/sibling.jpg"></aside>
  <div class="author-bio">{_PAD}<img src="https://x.test/img/headshot.jpg"></div>
</body></html>
"""


def test_collects_article_images_excluding_chrome_and_lead(tmp_path):
    svc = _svc(tmp_path)
    link = "https://x.test/post"
    svc._source_html_cache[link] = (link, PAGE)
    urls = svc.extract_source_gallery_urls(link, exclude_urls={"https://x.test/img/lead.jpg"})
    assert "https://x.test/img/one.jpg" in urls
    assert "https://x.test/img/two.jpg" in urls
    # lead excluded, site-chrome/related/author images filtered out
    assert "https://x.test/img/lead.jpg" not in urls
    assert "https://x.test/logo.png" not in urls
    assert "https://x.test/img/sibling.jpg" not in urls
    assert "https://x.test/img/headshot.jpg" not in urls
    # order preserved
    assert urls.index("https://x.test/img/one.jpg") < urls.index("https://x.test/img/two.jpg")


def test_cache_miss_returns_empty(tmp_path):
    svc = _svc(tmp_path)
    assert svc.extract_source_gallery_urls("https://x.test/uncached") == []


def test_fetch_page_escalates_to_browser_ua_on_503(tmp_path, monkeypatch):
    """paizo-style WAF: honest UA gets 503, browser UA succeeds. _fetch_page_html
    must escalate (not give up) after the honest request is refused."""
    import services.lead_images as li

    svc = _svc(tmp_path)
    seen_uas = []

    class _Resp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text
            self.url = "https://paizo.test/post"
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    def fake_safe_get(client, url):
        ua = client.headers.get("user-agent", "")
        seen_uas.append(ua)
        if ua == li._BROWSER_USER_AGENT:
            return _Resp(200, "<html><body>ok</body></html>")
        return _Resp(503)

    monkeypatch.setattr(li.url_guard, "safe_get", fake_safe_get)
    result = svc._fetch_page_html("https://paizo.test/post")
    assert result is not None
    assert result[0] == "<html><body>ok</body></html>"
    # honest UA tried first, then escalated to the browser UA
    assert seen_uas[0] == svc._user_agent
    assert li._BROWSER_USER_AGENT in seen_uas


def test_waf_block_backs_off_after_double_refusal(tmp_path, monkeypatch):
    """When a site (paizo) WAF-refuses both UAs, record a cooldown and stop
    re-fetching so we don't hammer it / get the IP rate-limited."""
    import services.lead_images as li

    svc = _svc(tmp_path)
    calls = {"n": 0}

    class _Resp:
        status_code = 503
        text = ""
        url = "https://paizo.test/post"
        headers: dict = {}

        def raise_for_status(self):
            raise RuntimeError("503")

    def fake_safe_get(client, url):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(li.url_guard, "safe_get", fake_safe_get)
    assert svc._fetch_page_html("https://paizo.test/post") is None
    first = calls["n"]
    assert first == 2  # tried honest + browser UA once
    # second fetch is short-circuited by the cooldown — no new requests
    assert svc._fetch_page_html("https://paizo.test/post2") is None
    assert calls["n"] == first


def test_dedupes_repeated_images(tmp_path):
    svc = _svc(tmp_path)
    link = "https://x.test/p2"
    svc._source_html_cache[link] = (link, '<img src="https://x.test/a.jpg"><img src="https://x.test/a.jpg">')
    assert svc.extract_source_gallery_urls(link) == ["https://x.test/a.jpg"]
