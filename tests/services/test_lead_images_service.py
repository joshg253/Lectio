from __future__ import annotations

import sqlite3
from pathlib import Path

from services.lead_images import LeadImageService


class _ReaderCtx:
    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeContent:
    def __init__(self, value: str, is_html: bool = True):
        self.value = value
        self.is_html = is_html


class _FakeEntry:
    def __init__(
        self,
        *,
        feed_url: str,
        entry_id: str,
        link: str,
        summary: str | None = None,
        content_html: str | None = None,
    ):
        self.feed_url = feed_url
        self.id = entry_id
        self.link = link
        self.summary = summary
        self._content_html = content_html

    def get_content(self, prefer_summary: bool = False):
        if self._content_html is None:
            return None
        return _FakeContent(self._content_html)


class _FakeReader:
    def __init__(self, entries):
        self._entries = entries

    def get_entries(self, feed: str):
        return list(self._entries)


def _make_conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_lead_images (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            image_url TEXT,
            image_alt TEXT,
            image_title TEXT,
            fetched_at REAL,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    return conn


def _build_service(db_path: Path, entries: list[_FakeEntry]):
    def get_meta_connection():
        return _make_conn(db_path)

    return LeadImageService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
        extract_video_id=lambda link: "ABCDEFGHIJK" if "youtube.com/watch?v=" in link else None,
    )


def test_extract_thumbnail_uses_youtube_thumbnail(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://www.youtube.com/feeds/videos.xml?channel_id=abc",
        entry_id="yt-1",
        link="https://www.youtube.com/watch?v=ABCDEFGHIJK",
    )

    thumb = service.extract_entry_thumbnail_url(entry)

    assert thumb == "https://i.ytimg.com/vi/ABCDEFGHIJK/hqdefault.jpg"


def test_extract_thumbnail_reads_lazy_loaded_img(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://example.com/feed.xml",
        entry_id="p-1",
        link="https://example.com/article",
        content_html='<p>hello</p><img data-src="/images/hero.jpg" src="data:image/gif;base64,AAAA" />',
    )

    thumb = service.extract_entry_thumbnail_url(entry)

    assert thumb == "https://example.com/images/hero.jpg"


def test_resolve_lead_image_checks_inline_even_when_cached_negative(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    feed_url = "https://example.com/feed.xml"
    entry_id = "p-2"

    # Prime a negative cache entry.
    service.store_entry_lead_image(feed_url, entry_id, None)

    entry = _FakeEntry(
        feed_url=feed_url,
        entry_id=entry_id,
        link="https://example.com/article",
        summary='<img src="https://cdn.example.com/photo.jpg" />',
    )

    resolved = service.resolve_entry_lead_image_url(entry, None, entry.summary)

    assert resolved == "https://cdn.example.com/photo.jpg"


def test_warm_cache_drops_placeholder_urls(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    service = _build_service(db_path, [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO entry_lead_images(feed_url, entry_id, image_url, fetched_at) VALUES (?, ?, ?, ?)",
            ("https://example.com/feed.xml", "p-3", "https://cdn.example.com/grey-placeholder.png", 1234.0),
        )

    service.warm_cache_from_db()

    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
            ("https://example.com/feed.xml", "p-3"),
        ).fetchone()

    assert row is None


def test_fetch_and_store_lead_images_backfills_missing_inline(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "meta.sqlite"
    entry = _FakeEntry(
        feed_url="https://example.com/feed.xml",
        entry_id="p-4",
        link="https://example.com/article",
        content_html="<p>no images here</p>",
    )
    service = _build_service(db_path, [entry])

    monkeypatch.setattr(service, "_fetch_source_lead_image", lambda _link, **kw: "https://cdn.example.com/source-hero.jpg")

    service.fetch_and_store_lead_images_for_feed("https://example.com/feed.xml", force_retry_negative=True)

    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
            ("https://example.com/feed.xml", "p-4"),
        ).fetchone()

    assert row is not None
    assert row["image_url"] == "https://cdn.example.com/source-hero.jpg"


def test_negative_retry_window_skips_recent_null(tmp_path: Path):
    """Entries fetched as NULL within the 4-hour retry window must be skipped."""
    import time

    db_path = tmp_path / "meta.sqlite"
    entry = _FakeEntry(
        feed_url="https://example.com/feed.xml",
        entry_id="p-5",
        link="https://example.com/article",
        content_html="<p>no images</p>",
    )
    service = _build_service(db_path, [entry])

    fetched = []
    service._fetch_source_lead_image = lambda link, **kw: fetched.append(link) or None

    # Store NULL less than 4 hours ago.
    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO entry_lead_images(feed_url, entry_id, image_url, fetched_at) VALUES (?, ?, NULL, ?)",
            ("https://example.com/feed.xml", "p-5", time.time() - 60),
        )
    service.warm_cache_from_db()

    service.fetch_and_store_lead_images_for_feed("https://example.com/feed.xml")

    assert fetched == [], "source fetch should be skipped within the retry window"


def test_negative_retry_window_retries_after_4h(tmp_path: Path):
    """Entries whose NULL was stored more than 4 hours ago must be retried."""
    import time

    db_path = tmp_path / "meta.sqlite"
    entry = _FakeEntry(
        feed_url="https://example.com/feed.xml",
        entry_id="p-6",
        link="https://example.com/old-article",
        content_html="<p>no images</p>",
    )
    service = _build_service(db_path, [entry])
    service._fetch_source_lead_image = lambda link, **kw: "https://cdn.example.com/late.jpg"

    # Store NULL more than 4 hours ago.
    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO entry_lead_images(feed_url, entry_id, image_url, fetched_at) VALUES (?, ?, NULL, ?)",
            ("https://example.com/feed.xml", "p-6", time.time() - (4 * 3600 + 60)),
        )
    service.warm_cache_from_db()

    service.fetch_and_store_lead_images_for_feed("https://example.com/feed.xml", force_retry_negative=True)

    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
            ("https://example.com/feed.xml", "p-6"),
        ).fetchone()

    assert row is not None
    assert row["image_url"] == "https://cdn.example.com/late.jpg"


def test_og_image_regex_matches_name_attribute():
    """og:image with name= attribute order (not property=) must be found."""
    service = _build_service(Path("/tmp"), [])
    html = (
        '<meta name="og:image" content="https://cdn.example.com/hero.png" data-next-head=""/>'
    )
    result = service._extract_meta_image_url_from_html(html, "https://example.com/article")
    assert result == "https://cdn.example.com/hero.png"


def test_og_image_regex_matches_property_attribute():
    """og:image with the standard property= attribute must be found."""
    service = _build_service(Path("/tmp"), [])
    html = '<meta property="og:image" content="https://cdn.example.com/banner.jpg"/>'
    result = service._extract_meta_image_url_from_html(html, "https://example.com/article")
    assert result == "https://cdn.example.com/banner.jpg"


def test_og_image_regex_matches_content_first_order():
    """og:image where content= appears before property= must be found via reversed regex."""
    service = _build_service(Path("/tmp"), [])
    html = '<meta content="https://cdn.example.com/thumb.jpg" property="og:image"/>'
    result = service._extract_meta_image_url_from_html(html, "https://example.com/article")
    assert result == "https://cdn.example.com/thumb.jpg"


def test_og_image_extensionless_cdn_url_accepted():
    """Extensionless og:image URLs (e.g. CDN token URLs) must not be rejected."""
    service = _build_service(Path("/tmp"), [])
    # CDN URL with no file extension — common for DO / Fastly image URLs.
    html = (
        '<meta property="og:image" '
        'content="https://community-cdn-example.global.ssl.fastly.net/ABC123"/>'
    )
    result = service._extract_meta_image_url_from_html(html, "https://example.com/article")
    assert result == "https://community-cdn-example.global.ssl.fastly.net/ABC123"


# --- _AVATAR_HINT_PATTERNS word-boundary fix ---

def test_avatar_hint_does_not_match_authorities():
    """'author' in _AVATAR_HINT_PATTERNS must not match substrings like 'authorities'."""
    service = _build_service(Path("/tmp"), [])
    # 'authorities' must NOT trigger the avatar filter
    assert not service._AVATAR_HINT_PATTERNS.search("Neither Bee nor the Bajoran authorities")


def test_avatar_hint_matches_author_standalone():
    service = _build_service(Path("/tmp"), [])
    assert service._AVATAR_HINT_PATTERNS.search("article-author")
    assert service._AVATAR_HINT_PATTERNS.search("author-image")
    assert service._AVATAR_HINT_PATTERNS.search("author bio section")


def test_avatar_hint_does_not_match_authoritative():
    service = _build_service(Path("/tmp"), [])
    assert not service._AVATAR_HINT_PATTERNS.search("authoritative source")
    assert not service._AVATAR_HINT_PATTERNS.search("authorization required")


# --- css_bg before preferred → promote to full-res img ---

def test_css_bg_before_preferred_returns_fullres_img(tmp_path):
    """When a CSS background (resized crop) appears before the body-scanner winner
    and a full-res <img> with the same filename stem is present, the full-res URL
    is returned instead of the resized css_bg or the body-scanner winner.

    Mirrors the Astroneer blog layout where detail-view-header contains a
    bg-blur div (resized crop) and a full-res <img>, both before the body image.
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head></head><body>"
        "<header class=\"detail-view-header\">"
        "<div class=\"bg-blur\" style=\"background-image:"
        " url('https://cdn.example.com/uploads/U40-Header-576x324.jpg')\"></div>"
        "<img alt=\"\" src=\"https://cdn.example.com/uploads/U40-Header-616x347.jpg\"/>"
        "</header>"
        "<article>"
        "<img src=\"https://cdn.example.com/uploads/U40-Body1-1920x1080.jpg\""
        " srcset=\"U40-Body1-1920x1080.jpg 1920w, U40-Body1-768x432.jpg 768w\"/>"
        "</article>"
        "</body></html>"
    )
    service._fetch_page_html = lambda url, **kw: (fake_html, url, False)

    result = service._fetch_source_lead_image("https://example.com/article")

    assert result == "https://cdn.example.com/uploads/U40-Header-616x347.jpg"


def test_css_bg_after_preferred_does_not_override(tmp_path):
    """When the body-scanner winner appears before the CSS background, keep the
    body image (normal article-first layout)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head></head><body>"
        "<article>"
        "<img src=\"https://cdn.example.com/uploads/article-hero.jpg\""
        " srcset=\"article-hero.jpg 1920w\"/>"
        "</article>"
        "<footer>"
        "<div style=\"background-image:"
        " url('https://cdn.example.com/uploads/footer-decor-576x324.jpg')\"></div>"
        "</footer>"
        "</body></html>"
    )
    service._fetch_page_html = lambda url, **kw: (fake_html, url, False)

    result = service._fetch_source_lead_image("https://example.com/article")

    assert result == "https://cdn.example.com/uploads/article-hero.jpg"


def test_extract_thumbnail_uses_reader_enclosure_href(tmp_path: Path):
    """reader's Enclosure exposes .href (not .url) — the enclosure fast path
    must read it (regression: invisibleoranges.com entries got no images)."""
    from reader import Enclosure

    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://www.invisibleoranges.com/feed/",
        entry_id="https://www.invisibleoranges.com/?p=63411",
        link="https://www.invisibleoranges.com/upcoming-metal-releases",
    )
    entry.enclosures = (
        Enclosure(href="https://media.invisibleoranges.com/uploads/2025/04/25/UMR.png", type="image/png", length=286389),
    )

    thumb = service.extract_entry_thumbnail_url(entry)

    assert thumb == "https://media.invisibleoranges.com/uploads/2025/04/25/UMR.png"


def test_extract_thumbnail_reads_dict_enclosures(tmp_path: Path):
    """feedparser-style dict enclosures work with either 'href' or 'url' keys."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    for key in ("href", "url"):
        entry = _FakeEntry(
            feed_url="https://example.com/feed.xml",
            entry_id=f"enc-{key}",
            link="https://example.com/article",
        )
        entry.enclosures = [{key: "https://cdn.example.com/photos/cover.jpg", "type": "image/jpeg"}]

        assert service.extract_entry_thumbnail_url(entry) == "https://cdn.example.com/photos/cover.jpg"


def test_blogger_chrome_domain_rejected(tmp_path: Path):
    """www.blogger.com hosts only widget chrome (e.g. the 'Powered By Blogger'
    button) — never article images (regression: greasespot.net lead image)."""
    service = _build_service(tmp_path / "meta.sqlite", [])

    assert not service._is_image_url_acceptable(
        "https://www.blogger.com/buttons/blogger-simple-kahki.gif", None, None
    )


def test_source_scan_skips_nav_menu_icons(tmp_path: Path):
    """Images inside nav menus/dropdowns are site chrome, not lead images
    (regression: krita.org language-picker icon)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head></head><body>"
        '<ul class="navbar-nav"><li class="nav-item dropdown">'
        '<a class="nav-link dropdown-toggle" href="#">'
        '<img src="/aether/languages.png" alt="">'
        "</a></li></ul>"
        "</body></html>"
    )
    service._fetch_page_html = lambda url, **kw: (fake_html, url, False)

    assert service._fetch_source_lead_image("https://krita.org/en/posts/release/") is None


def test_source_scan_skips_widget_images_but_keeps_article_image(tmp_path: Path):
    """Sidebar/footer widget images are skipped; a real article image still wins."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head></head><body>"
        "<article>"
        '<img src="https://cdn.example.com/uploads/article-photo.jpg" srcset="article-photo.jpg 1920w">'
        "</article>"
        "<div class='widget BloggerButton'><div class='widget-content'>"
        "<a href='https://example.org'><img alt='Powered By Example' src='https://cdn.example.org/buttons/button.gif'/></a>"
        "</div></div>"
        "</body></html>"
    )
    service._fetch_page_html = lambda url, **kw: (fake_html, url, False)

    result = service._fetch_source_lead_image("https://example.com/article")

    assert result == "https://cdn.example.com/uploads/article-photo.jpg"


# --- piwik/matomo tracker URLs rejected ---

def test_piwik_url_rejected_as_tracker(tmp_path: Path):
    """piwik.php tracking pixels must be rejected (regression: krita.org 1×1 image)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert not service._is_image_url_acceptable(
        "https://stats.kde.org/piwik.php?idsite=13", None, None
    )
    assert not service._is_image_url_acceptable(
        "https://example.com/matomo/matomo.php?idsite=1", None, None
    )


# --- tiny explicit dimensions rejected as spacers/tracking pixels ---

def test_small_explicit_dims_rejected(tmp_path: Path):
    """Images with both explicit dims ≤ 10px must be rejected as tracking/spacer pixels."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    # Classic 1×1 tracking pixel
    assert not service._is_source_image_tag_acceptable(
        {"width": "1", "height": "1"}, "https://stats.example.com/tracker.gif"
    )
    # 10×10 is still within the tiny-dims threshold
    assert not service._is_source_image_tag_acceptable(
        {"width": "10", "height": "10"}, "https://cdn.example.com/spacer.gif"
    )


# --- enclosure fallback in test_entry_strategies media_rss card ---

def test_strategy_test_includes_enclosure_in_media_rss(tmp_path: Path, monkeypatch):
    """Tuning tab media_rss card must fall back to entry enclosures when the feed has
    no <media:thumbnail> elements (regression: Invisible Oranges Tuning showed nothing)."""
    from reader import Enclosure

    service = _build_service(tmp_path / "meta.sqlite", [])
    monkeypatch.setattr(service, "_fetch_feed_media_thumbnails", lambda _url: {})
    monkeypatch.setattr(service, "_fetch_source_lead_image", lambda *a, **kw: None)

    entry = _FakeEntry(
        feed_url="https://www.invisibleoranges.com/feed/",
        entry_id="io-tuning-1",
        link="https://www.invisibleoranges.com/review",
    )
    entry.enclosures = (
        Enclosure(
            href="https://media.invisibleoranges.com/uploads/2025/04/cover.png",
            type="image/png",
            length=42000,
        ),
    )

    results = service.test_entry_strategies(entry)
    media_rss = next(r for r in results if r["strategy"] == "media_rss")

    assert media_rss["image_url"] == "https://media.invisibleoranges.com/uploads/2025/04/cover.png"
    assert media_rss["error"] is None


# --- WebP picture <source srcset> fallback for alt/title ---

def test_fetch_caption_webp_picture_fallback(tmp_path: Path):
    """Alt/title from <img> inside <picture>/<source type=image/webp> must be returned
    when lead_image_url is the WebP srcset URL (regression: Wondermark captions)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry_link = "https://wondermark.com/1k75/"
    webp_url = "https://cdn.example.com/strips/1k75.webp"
    html = (
        "<html><body><article>"
        "<picture>"
        '<source type="image/webp" srcset="https://cdn.example.com/strips/1k75.webp">'
        '<img src="https://cdn.example.com/strips/1k75.png"'
        ' alt="Panel one" title="In a world where everything went wrong">'
        "</picture>"
        "</article></body></html>"
    )
    service._source_html_cache[entry_link] = (entry_link, html)

    alt, title = service.fetch_entry_image_caption(entry_link, lead_image_url=webp_url)

    assert alt == "Panel one"
    assert title == "In a world where everything went wrong"


# --- Webcomic hover-text balloon / og:description fallback ---

def test_webcomic_caption_uses_alt_text_balloon(tmp_path: Path):
    """When the comic <img> has no alt/title, the WordPress Webcomic plugin's
    comic-alt-text balloon supplies the hover joke (regression: Wondermark)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry_link = "https://wondermark.com/c/1586/"
    img_url = "https://wondermark.com/wp-content/uploads/1586jet.png"
    html = (
        "<html><head>"
        '<meta property="og:description" content="A meta description that differs.">'
        "</head><body><article>"
        f'<img src="{img_url}" alt="" />'
        '<div class="comic-alt-text"><p>The joke in the hover text.</p></div>'
        "</article></body></html>"
    )
    service._source_html_cache[entry_link] = (entry_link, html)

    alt, title = service.fetch_entry_image_caption(
        entry_link, lead_image_url=img_url, is_webcomic=True
    )

    assert alt is None
    assert title == "The joke in the hover text."


def test_webcomic_caption_falls_back_to_og_description(tmp_path: Path):
    """No balloon present: og:description supplies the caption for webcomic feeds."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry_link = "https://example-comic.com/strip/42/"
    img_url = "https://example-comic.com/strips/42.png"
    html = (
        "<html><head>"
        '<meta property="og:description" content="Otto needs investment capital.">'
        "</head><body>"
        f'<img src="{img_url}" alt="" />'
        "</body></html>"
    )
    service._source_html_cache[entry_link] = (entry_link, html)

    alt, title = service.fetch_entry_image_caption(
        entry_link, lead_image_url=img_url, is_webcomic=True
    )

    assert title == "Otto needs investment capital."


def test_non_webcomic_does_not_use_og_description(tmp_path: Path):
    """Non-webcomic feeds must NOT pull og:description as an image caption — it is
    the article excerpt, not the image's alt/title."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry_link = "https://news.example.com/article/"
    img_url = "https://news.example.com/hero.jpg"
    html = (
        "<html><head>"
        '<meta property="og:description" content="Article excerpt text.">'
        "</head><body>"
        f'<img src="{img_url}" alt="" />'
        "</body></html>"
    )
    service._source_html_cache[entry_link] = (entry_link, html)

    alt, title = service.fetch_entry_image_caption(
        entry_link, lead_image_url=img_url, is_webcomic=False
    )

    assert alt is None
    assert title is None


# --- BBCode [img] conversion ---

def test_bbcode_img_converted_before_extraction(tmp_path: Path):
    """[img]…[/img] BBCode must be converted to <img src=…> before inline extraction
    (regression: Nexus Mods Tuning tab showed no images)."""
    service = _build_service(tmp_path / "meta.sqlite", [])

    # Converter unit test
    assert service._bbcode_img_to_html("[img]https://cdn.example.com/art.jpg[/img]") == (
        '<img src="https://cdn.example.com/art.jpg">'
    )

    # End-to-end: extract_inline_thumb_url must surface the image
    entry = _FakeEntry(
        feed_url="https://www.nexusmods.com/rss/",
        entry_id="nexus-1",
        link="https://www.nexusmods.com/mods/12345",
        content_html="Mod description\n[img]https://staticdelivery.nexusmods.com/mods/img.jpg[/img]\n",
    )
    thumb = service.extract_inline_thumb_url(entry)

    assert thumb == "https://staticdelivery.nexusmods.com/mods/img.jpg"
