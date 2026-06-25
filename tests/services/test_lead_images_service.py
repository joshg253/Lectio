from __future__ import annotations

import sqlite3
import threading
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
        self.enclosures: tuple | list = ()

    def get_content(self, prefer_summary: bool = False):
        if self._content_html is None:
            return None
        return _FakeContent(self._content_html)


class _FakeReader:
    def __init__(self, entries):
        self._entries = entries

    def get_entries(self, feed: str):
        return list(self._entries)

    def get_entry(self, resource_id):
        feed_url, entry_id = resource_id
        for entry in self._entries:
            if entry.feed_url == feed_url and entry.id == entry_id:
                return entry
        raise KeyError(resource_id)


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_lead_image_strategy (
            feed_url TEXT PRIMARY KEY,
            strategy TEXT NOT NULL,
            detected_at REAL,
            manual INTEGER DEFAULT 0
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


def test_comiccontrol_thumb_promoted_to_full_res(tmp_path: Path):
    # ComicControl feeds (atomic-robo, everblue) ship a small /comicsthumbs/
    # image; the full panel is the same filename under /comics/.
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://www.atomic-robo.com/atomicrobo/rss",
        entry_id="ar-1",
        link="https://www.atomic-robo.com/atomicrobo/17ch1-page-1",
        content_html='<p>x</p><img src="https://www.atomic-robo.com/comicsthumbs/1781025836-ARV1701_01.jpg" />',
    )

    thumb = service.extract_entry_thumbnail_url(entry)

    assert thumb == "https://www.atomic-robo.com/comics/1781025836-ARV1701_01.jpg"


def test_promote_known_thumbnail_is_noop_for_other_urls(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    # Substring-but-not-segment must not be rewritten.
    assert (
        service._promote_known_thumbnail("https://x/comicsthumbsfoo/a.jpg")
        == "https://x/comicsthumbsfoo/a.jpg"
    )
    assert service._promote_known_thumbnail(None) is None


def test_inline_thumb_url_promotes_comiccontrol(tmp_path: Path):
    # Webcomic-strategy feeds derive the article lead from the inline image; the
    # ComicControl thumb must be promoted there too (not just the cache path).
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://www.atomic-robo.com/atomicrobo/rss",
        entry_id="ar-2",
        link="https://www.atomic-robo.com/atomicrobo/17ch1-page-1",
        content_html='<a href="x"><img src="https://www.atomic-robo.com/comicsthumbs/1-ARV.jpg"/></a>',
    )
    assert service.extract_inline_thumb_url(entry) == "https://www.atomic-robo.com/comics/1-ARV.jpg"


def test_podcast_title_branding_image_rejected(tmp_path: Path):
    # og:scrape can fall back to a show-title branding graphic on a post with no
    # real featured image; reject it even on the cached (skip_logo_patterns) path.
    service = _build_service(tmp_path / "meta.sqlite", [])
    url = "https://ii.techdirt.com/s/t/i/podcast-title-small.png"
    assert service._is_image_url_acceptable(url, None, None, skip_logo_patterns=True) is False


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


def test_webcomic_class_matches_wp_post_image():
    """On a webcomic feed the WordPress featured image (wp-post-image) is the comic
    panel (e.g. claycomix), so the webcomic class matcher must recognize it."""
    from services.lead_images import LeadImageService

    assert LeadImageService._WEBCOMIC_IMG_CLASS_RE.search("attachment-full size-full wp-post-image")
    assert LeadImageService._WEBCOMIC_IMG_CLASS_RE.search("comic-image")
    assert not LeadImageService._WEBCOMIC_IMG_CLASS_RE.search("sidebar-thumb")


def test_badge_and_kofi_widgets_rejected(tmp_path: Path):
    """shields.io status badges and Ko-fi tip buttons are widget chrome embedded
    near content (regression: openmw grabbed a shields.io follow badge, claycomix
    grabbed the Ko-fi button) — never the post's lead image."""
    service = _build_service(tmp_path / "meta.sqlite", [])

    assert not service._is_image_url_acceptable(
        "https://img.shields.io/twitter/follow/openmw_org?style=social", None, None
    )
    assert not service._is_image_url_acceptable(
        "https://storage.ko-fi.com/cdn/kofi3.png?v=3", None, None
    )


def test_nav_directory_icons_rejected(tmp_path: Path):
    """Images served from a /navigation/ asset directory are header/menu UI
    chrome, not article images (regression: paizo.com blog scrape returning
    the 'Account' nav icon)."""
    service = _build_service(tmp_path / "meta.sqlite", [])

    assert not service._is_image_url_acceptable(
        "https://cdn.paizo.com/image/navigation/Personal-Account.png", None, None
    )
    # A path that merely contains the word "navigation" as part of a segment
    # (not its own directory) is still a valid article image.
    assert service._is_image_url_acceptable(
        "https://cdn.example.com/blog/ship-navigation-guide/hero.jpg", None, None
    )


def test_source_scan_prefers_article_image_over_nav_icon(tmp_path: Path):
    """A nav-directory icon appearing before the article image must not win the
    first-image position bonus (regression: paizo.com 'Account' icon)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head></head><body>"
        '<img alt="Account" src="https://cdn.paizo.com/image/navigation/Personal-Account.png">'
        '<img src="https://cdn.paizo.com/covers/product-cover.jpg?w=300" alt="Cover">'
        "</body></html>"
    )
    service._fetch_page_html = lambda url, **kw: (fake_html, url, False)

    result = service._fetch_source_lead_image("https://paizo.com/blog/find-your-path")

    assert result == "https://cdn.paizo.com/covers/product-cover.jpg?w=300"


def test_wordpress_blank_placeholder_rejected(tmp_path: Path):
    """WordPress.com ships s0.wp.com/i/blank.jpg as the og:image for image-less
    posts — a 200x200 white box (regression: giodicanio.com C++ article)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert not service._is_image_url_acceptable(
        "https://s0.wp.com/i/blank.jpg?m=1383295312i", 200, 200, allow_extensionless=True
    )
    # A real .jpg is still fine.
    assert service._is_image_url_acceptable(
        "https://cdn.example.com/uploads/hero.jpg", None, None
    )


def test_statcounter_pixel_rejected(tmp_path: Path):
    """c.statcounter.com tracking pixels (alt='Web Analytics') must never be a
    lead image — they ship as a 1x1 GIF that scales to a grey thumbnail
    (regression: andreinc.net image-less post)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert not service._is_image_url_acceptable(
        "https://c.statcounter.com/7153286/0/b3053c1d/1/", None, None, allow_extensionless=True
    )
    # skip_logo_patterns=True is used at the render cache-gate; the tracker check
    # still fires there, so a stale cached statcounter URL is dropped on display.
    assert not service._is_image_url_acceptable(
        "https://c.statcounter.com/7153286/0/b3053c1d/1/",
        None,
        None,
        allow_extensionless=True,
        skip_logo_patterns=True,
    )


def test_addtoany_share_button_rejected(tmp_path: Path):
    """AddToAny/AddThis share-button sprites (alt='Share') are social widgets,
    not article images (regression: nuonsoft.com 'Share' caption)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert not service._is_image_url_acceptable(
        "https://static.addtoany.com/buttons/share_save_171_16.png", None, None
    )


def test_emoji_sprite_rejected_as_lead_image(tmp_path: Path):
    """WordPress wp-smiley (s.w.org) and twemoji CDN glyphs are inline emoji, not
    a post's lead image (regression: nuonsoft ➡, Vintage Story 🙃). They remain
    inline at render — only lead-image selection rejects them."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert not service._is_image_url_acceptable(
        "https://s.w.org/images/core/emoji/17.0.2/72x72/27a1.png", None, None
    )
    assert not service._is_image_url_acceptable(
        "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f643.png",
        None,
        None,
    )
    # Rejected even under non-default flags (the emoji check sits before the
    # skip_logo_patterns logic, so it must hold regardless).
    assert not service._is_image_url_acceptable(
        "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f643.png",
        None,
        None,
        skip_logo_patterns=True,
        allow_extensionless=True,
    )
    # A non-emoji asset that merely carries "twemoji" in its query string is NOT
    # rejected (host+path match only).
    assert service._is_image_url_acceptable(
        "https://cdn.example.com/uploads/hero.jpg?ref=twemoji", None, None
    )
    # A normal article image on an unrelated CDN is unaffected.
    assert service._is_image_url_acceptable(
        "https://cdn.example.com/uploads/hero.jpg", None, None
    )


def test_source_scan_skips_share_button(tmp_path: Path):
    """A page whose only body images are a share button and an emoji yields no
    lead image (and no bogus 'Share' alt) — regression: nuonsoft.com."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head></head><body><p>Text.</p>"
        '<img alt="➡" class="wp-smiley" '
        'src="https://s.w.org/images/core/emoji/17.0.2/72x72/27a1.png">'
        '<a href="https://www.addtoany.com/share">'
        '<img alt="Share" src="https://static.addtoany.com/buttons/share_save_171_16.png"></a>'
        "</body></html>"
    )
    service._fetch_page_html = lambda url, **kw: (fake_html, url, False)

    url, alt = service._extract_preferred_source_image_data(
        fake_html, "https://www.nuonsoft.com/blog/x/", "https://www.nuonsoft.com/blog/x/"
    )

    assert url is None
    assert alt is None


def test_megaphone_featured_image_beats_recent_episodes_widget(tmp_path: Path):
    """SE Radio (WordPress 'Megaphone' podcast theme) has no og:image; the page's
    own featured image is a square `wp-post-image` rendered right after the nav menu,
    while a `megaphone-items megaphone-posts` widget lists OTHER episodes' square
    thumbnails. Regression: every episode showed the newest episode's thumbnail.

    The fix must (a) strip the recent-episodes widget and (b) accept the square
    featured image despite the headshot/site-chrome heuristics."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    page = (
        "https://se-radio.net/2026/06/se-radio-725-danny-yang-and-sam-goldman/"
    )
    html = (
        "<html><body>"
        '<nav class="navbar"><ul><li><a href="#">Menu</a></li></ul></nav>'
        '<div class="megaphone-section single-layout-5"><div class="entry-media entry-media-rounded">'
        '<img width="300" height="300" class="size-megaphone-single-podcast-5 wp-post-image" '
        'alt="SE Radio Guests Danny Yang and Sam Goldman" '
        'src="https://se-radio.net/wp-content/uploads/2026/06/danny-yang-sam-goldman.png"></div></div>'
        '<div class="row megaphone-items megaphone-posts">'
        '<article class="megaphone-item megaphone-post"><div class="entry-media">'
        '<a href="/2026/06/se-radio-724-jure-leskovec/"><img width="300" height="188" '
        'src="https://se-radio.net/wp-content/uploads/2026/06/jure-leskovec-300x188.png"></a>'
        "</div></article></div>"
        "</body></html>"
    )
    url, alt = service._extract_preferred_source_image_data(html, page, page)
    assert url == "https://se-radio.net/wp-content/uploads/2026/06/danny-yang-sam-goldman.png"
    assert alt == "SE Radio Guests Danny Yang and Sam Goldman"


def test_webcomic_alt_prefers_img_title_over_og_description(tmp_path: Path):
    """The hover-text punchline on the main comic <img title="..."> must win over
    og:description, which on SMBC is just the post title (regression: SMBC)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head>"
        '<meta property="og:description" content="Saturday Morning Breakfast Cereal - Proof" />'
        "</head><body>"
        '<img title="This sort of thing is why I will likely never write fantasy." '
        'src="https://www.smbc-comics.com/comics/1780608554-20260605.png" id="cc-comic" />'
        "</body></html>"
    )
    assert service._extract_webcomic_alt_text(fake_html) == (
        "This sort of thing is why I will likely never write fantasy."
    )


def test_webcomic_alt_falls_back_to_og_description(tmp_path: Path):
    """When the comic <img> carries no title/alt, og:description is still used."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head>"
        '<meta property="og:description" content="the secret hover joke" />'
        "</head><body><p>no comic img here</p></body></html>"
    )
    assert service._extract_webcomic_alt_text(fake_html) == "the secret hover joke"


def test_advertisement_images_rejected(tmp_path: Path):
    """Ad images flag themselves via filename or alt text (regression: SE Radio
    'banner ad' image leaking into feed-content and source lead images)."""
    service = _build_service(tmp_path / "meta.sqlite", [])

    # URL ad-token (".../Cert-ad1.png", "/ads/...") rejected.
    assert not service._is_image_url_acceptable(
        "https://se-radio.net/wp-content/uploads/2026/04/2026-Software-Pro-Cert-ad1.png",
        320, 100,
    )
    assert not service._is_image_url_acceptable(
        "https://cdn.example.com/ads/leaderboard.png", None, None
    )
    # Words containing the "ad" substring are not ads.
    assert service._is_image_url_acceptable(
        "https://cdn.example.com/wp-content/uploads/2026/hero.jpg", None, None
    )
    # alt-flagged ad rejected by the feed-content inline extractor.
    banner_html = (
        '<img src="https://example.com/promo/spring.png" width="320" height="100" '
        'alt="banner ad that says subscribe now">'
    )
    assert service._extract_first_image_url_from_html(
        banner_html, "https://example.com/article", allow_extensionless=True
    ) is None


def test_extreme_aspect_logo_rejected(tmp_path: Path):
    """Wordmark logos with banner aspect ratios (e.g. 600x100, 200x1500) are site
    branding, not article images (regression: SE Radio logo in source scrape)."""
    service = _build_service(tmp_path / "meta.sqlite", [])

    assert not service._is_image_url_acceptable(
        "https://se-radio.net/wp-content/uploads/2024/01/SE-radio-logo-color-600x100-1.png",
        None, None,
    )
    assert not service._is_image_url_acceptable(
        "https://se-radio.net/wp-content/uploads/seradio-20th-site-logo-200x1500-2.png",
        None, None,
    )
    # A logo-named image with a content-like aspect ratio still passes (e.g. an
    # article about a logo, sized 1200x630).
    assert service._is_image_url_acceptable(
        "https://cdn.example.com/articles/imdb-logo-1200x630.jpg", None, None
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


def test_webcomic_panel_wins_over_generic_og_image(tmp_path: Path):
    """ComicControl sites set a single generic site banner as og:image on every
    page with a sane 1200x630 aspect ratio. The main comic panel (id="cc-comic")
    must still win for webcomic feeds (regression: everblue-comic.com)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    fake_html = (
        "<html><head>"
        '<meta property="og:image" content="https://www.everblue-comic.com/files/og-image.jpg"/>'
        '<meta property="og:image:width" content="1200"/>'
        '<meta property="og:image:height" content="630"/>'
        "</head><body>"
        '<img title="At last." '
        'src="https://www.everblue-comic.com/comics/1781177826-Vol1-Ch4-Page-143-144-339.jpg" '
        'id="cc-comic" />'
        "</body></html>"
    )
    service._fetch_page_html = lambda url, **kw: (fake_html, url, False)

    # Without webcomic mode the curated og:image banner wins (existing behaviour).
    assert (
        service._fetch_source_lead_image("https://www.everblue-comic.com/comic/x")
        == "https://www.everblue-comic.com/files/og-image.jpg"
    )
    # In webcomic mode the comic panel wins over the generic banner.
    assert (
        service._fetch_source_lead_image("https://www.everblue-comic.com/comic/x", is_webcomic=True)
        == "https://www.everblue-comic.com/comics/1781177826-Vol1-Ch4-Page-143-144-339.jpg"
    )


def test_backfill_webcomic_prefers_source_panel_over_enclosure(tmp_path: Path):
    """Webcomic feeds carry a small /comicsthumbs/ enclosure but the source page
    has the full-resolution panel + hover text. Backfill must fall through the
    enclosure to the source fetch (regression: everblue-comic.com)."""
    feed_url = "https://www.everblue-comic.com/comic/rss/"
    entry = _FakeEntry(
        feed_url=feed_url,
        entry_id="https://www.everblue-comic.com/comic/p",
        link="https://www.everblue-comic.com/comic/p",
    )
    # RSS enclosure is the small thumbnail variant.
    entry.enclosures = (
        {"href": "https://www.everblue-comic.com/comicsthumbs/x-thumb.jpg", "type": "image/jpeg"},
    )
    service = _build_service(tmp_path / "meta.sqlite", [entry])
    service.store_feed_strategy(feed_url, "webcomic", manual=True)

    full_panel = "https://www.everblue-comic.com/comics/x-full.jpg"
    calls: list[tuple] = []
    service._fetch_source_lead_image = lambda link, **kw: calls.append((link, kw)) or full_panel  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    # Webcomic feeds must NOT fetch the feed XML for media thumbs.
    service._fetch_feed_media_thumbnails = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("webcomic must skip _fetch_feed_media_thumbnails")
    )

    service.fetch_and_store_lead_images_for_feed(feed_url, force_retry_negative=True)

    # The full-resolution source panel wins over the enclosure thumbnail,
    # and is_webcomic=True is propagated to the source fetch.
    assert service._cache[(feed_url, entry.id)] == full_panel
    assert calls and calls[0][1].get("is_webcomic") is True


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


def test_strip_related_post_blocks_removes_sibling_thumbnails(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    html = (
        '<article><img src="https://site.example/post/hero.jpg"></article>'
        '<div class="related-content--wrapper"><div class="related-content">'
        '<img src="https://site.example/other-a/cover.jpg">'
        '<img src="https://site.example/other-b/cover.png">'
        "</div></div>"
    )

    stripped = service._strip_related_post_blocks(html)

    assert "post/hero.jpg" in stripped
    assert "other-a/cover.jpg" not in stripped
    assert "other-b/cover.png" not in stripped


def test_source_image_ignores_related_posts_section(tmp_path: Path):
    # A post with no og:image and no hero of its own must not borrow a sibling
    # post's thumbnail from the "related posts" widget.
    service = _build_service(tmp_path / "meta.sqlite", [])
    html = (
        "<main></main>"
        '<section class="related-posts">'
        '<img src="https://site.example/other/cover.jpg" width="800" height="450">'
        "</section>"
    )

    url = service._extract_preferred_source_image_url(
        html, "https://site.example/post/", "https://site.example/post/"
    )

    assert url is None


def test_inline_from_reader_falls_back_to_feed_content_image(tmp_path: Path):
    # ArtStation-style: the page is a JS SPA with no og:image, but the feed
    # embeds the image inline, so the chunk-backfill fallback should find it.
    entry = _FakeEntry(
        feed_url="https://www.artstation.com/artist.rss",
        entry_id="https://www.artstation.com/artwork/abc",
        link="https://www.artstation.com/artwork/abc",
        content_html=(
            '<p><a href="https://cdn.artstation.com/p/large/art.jpg">'
            '<img src="https://cdn.artstation.com/p/large/art.jpg" /></a></p>'
        ),
    )
    service = _build_service(tmp_path / "meta.sqlite", [entry])

    result = service._inline_from_reader(
        "https://www.artstation.com/artist.rss",
        "https://www.artstation.com/artwork/abc",
    )

    assert result == "https://cdn.artstation.com/p/large/art.jpg"


def test_logo_named_image_accepted_when_post_local(tmp_path: Path):
    """A content hero named '…-logo.png' hosted under the post's own path must
    not be dropped by the logo filter (e.g. andreagrandi's mcp-wire-logo.png)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    post = "https://www.andreagrandi.it/posts/announcing-mcp-wire-0-3-0/"
    img = post + "mcp-wire-logo.png"
    # Without post context the logo filter rejects it; with it, it's accepted.
    assert service._is_image_url_acceptable(img, None, None, allow_extensionless=True) is False
    assert service._is_image_url_acceptable(img, None, None, allow_extensionless=True, source_url=post) is True


def test_site_logo_still_rejected_when_not_post_local(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    post = "https://example.com/posts/some-article/"
    site_logo = "https://example.com/assets/site-logo.png"  # not under the post path
    assert service._is_image_url_acceptable(site_logo, None, None, source_url=post) is False


def test_forge_avatar_urls_rejected(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._is_image_url_acceptable("https://gitea.com/delvh.png", None, None) is False
    assert service._is_image_url_acceptable("https://github.com/octocat.png", None, None) is False
    # Repo/asset paths (more than one segment) are NOT avatars.
    assert service._is_image_url_acceptable(
        "https://github.com/owner/repo/raw/main/hero.png", None, None
    ) is True


def test_og_scrape_manual_keeps_inline_when_source_misses(tmp_path: Path, monkeypatch):
    """An og_scrape-manual feed stores the inline image, then falls through to the
    source fetch. A transient source miss must NOT clobber the good inline image
    with None (the delightlylinux brand-new-post intermittency)."""
    db_path = tmp_path / "meta.sqlite"
    feed = "https://blog.example.com/feed"
    inline_img = "https://blog.example.com/post/cover.jpg?w=300"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="p-og",
        link="https://blog.example.com/post/",
        content_html=f'<p><img src="{inline_img}"></p>',
    )
    service = _build_service(db_path, [entry])
    service.store_feed_strategy(feed, "og_scrape", manual=True)
    # Source page transiently yields nothing.
    monkeypatch.setattr(service, "_fetch_source_lead_image", lambda *a, **k: None)

    service.fetch_and_store_lead_images_for_feed(feed, force_retry_negative=True)

    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE entry_id = ?", ("p-og",)
        ).fetchone()
    assert row is not None
    assert row["image_url"] == inline_img, "transient source miss clobbered the inline image"


# --- inline <svg> thumbnails (PR5) -----------------------------------------

_INLINE_SVG = (
    '<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M1 1H9V9Z" fill="currentColor"></path></svg>'
)


def test_inline_svg_used_as_thumb_when_no_raster(tmp_path: Path):
    feed = "https://analogue.example/feed.xml"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="svg-1",
        link="https://analogue.example/post/",
        content_html=f"<p>Notes</p>{_INLINE_SVG}",
    )
    service = _build_service(tmp_path / "meta.sqlite", [entry])

    thumb = service.extract_inline_thumb_url(entry)

    assert thumb is not None
    assert thumb.startswith("data:image/svg+xml,")
    assert "%3Cpath" in thumb  # path geometry survived sanitization


def test_raster_image_preferred_over_inline_svg(tmp_path: Path):
    feed = "https://analogue.example/feed.xml"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="svg-2",
        link="https://analogue.example/post/",
        content_html=f'<img src="https://cdn.example/hero.jpg">{_INLINE_SVG}',
    )
    service = _build_service(tmp_path / "meta.sqlite", [entry])

    thumb = service.extract_inline_thumb_url(entry)

    assert thumb == "https://cdn.example/hero.jpg"


def test_extract_inline_svg_thumb_url_public_helper(tmp_path: Path):
    feed = "https://analogue.example/feed.xml"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="svg-3",
        link="https://analogue.example/post/",
        content_html=f"<div>{_INLINE_SVG}</div>",
    )
    service = _build_service(tmp_path / "meta.sqlite", [entry])

    assert service.extract_inline_svg_thumb_url(entry).startswith("data:image/svg+xml,")


def test_no_svg_no_thumb(tmp_path: Path):
    feed = "https://analogue.example/feed.xml"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="svg-4",
        link="https://analogue.example/post/",
        content_html="<p>just text, no images</p>",
    )
    service = _build_service(tmp_path / "meta.sqlite", [entry])

    assert service.extract_inline_svg_thumb_url(entry) is None


# --- request-path async persistence (perf: keep opens off the meta-DB writer) ---


def test_persist_lead_image_async_writes_when_changed(tmp_path, monkeypatch):
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    # Run the queued write inline so the test is deterministic (no worker thread).
    monkeypatch.setattr(service, "_enqueue_write", lambda uid, fn: fn())
    service.persist_lead_image_async("https://f/x.xml", "e1", "https://img/a.jpg")
    with _make_conn(db) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE entry_id = ?", ("e1",)
        ).fetchone()
    assert row is not None and row["image_url"] == "https://img/a.jpg"


def test_persist_lead_image_async_skips_unchanged(tmp_path, monkeypatch):
    service = _build_service(tmp_path / "meta.sqlite", [])
    enqueued = []
    monkeypatch.setattr(service, "_enqueue_write", lambda uid, fn: enqueued.append(fn))
    # Seed the in-memory cache as if already persisted.
    service._cache[("https://f/x.xml", "e1")] = "https://img/a.jpg"
    service.persist_lead_image_async("https://f/x.xml", "e1", "https://img/a.jpg")
    assert enqueued == []  # unchanged -> nothing enqueued for the writer
    assert service._cache[("https://f/x.xml", "e1")] == "https://img/a.jpg"


def test_persist_image_alt_async_skips_unchanged(tmp_path, monkeypatch):
    service = _build_service(tmp_path / "meta.sqlite", [])
    enqueued = []
    monkeypatch.setattr(service, "_enqueue_write", lambda uid, fn: enqueued.append(fn))
    key = ("https://f/x.xml", "e1")
    service._alt_cache[key] = "alt"
    service._title_cache[key] = "title"
    service.persist_image_alt_async("https://f/x.xml", "e1", "alt", title_text="title")
    assert enqueued == []  # unchanged -> nothing enqueued


def test_write_worker_drains_queue(tmp_path):
    """The shared worker processes enqueued writes (and survives a failing one)."""
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    boom_ran = threading.Event()
    ok_ran = threading.Event()

    def _boom():
        boom_ran.set()
        raise RuntimeError("write failed")

    def _ok():
        service.store_entry_lead_image("https://f/x.xml", "e9", "https://img/z.jpg")
        ok_ran.set()

    service._enqueue_write("u", _boom)   # failure is logged, worker keeps going
    service._enqueue_write("u", _ok)
    assert ok_ran.wait(timeout=5) and boom_ran.is_set()
    with _make_conn(db) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE entry_id = ?", ("e9",)
        ).fetchone()
    assert row is not None and row["image_url"] == "https://img/z.jpg"


def test_og_image_beats_preload_hint(tmp_path: Path, monkeypatch):
    """A <link rel=preload as=image> is a perf hint (often an above-the-fold widget
    chart, e.g. usafacts.org's answer-page-card) and must NOT override the
    publisher's curated og:image. Regression for wrong lead images on usafacts."""
    import services.lead_images as li_mod
    monkeypatch.setattr(li_mod, "is_safe_outbound_url", lambda *a, **k: True)
    service = _build_service(tmp_path / "m.sqlite", [])
    html = (
        '<html><head>'
        '<link rel="preload" as="image" href="https://cdn.example.com/widget-chart.png">'
        '<meta property="og:image" content="https://cdn.example.com/real-hero.jpg">'
        '</head><body><p>article</p></body></html>'
    )
    service._fetch_page_html = lambda link, **kw: (html, "https://site.test/a", False)
    assert service._fetch_source_lead_image("https://site.test/a") == "https://cdn.example.com/real-hero.jpg"


def test_preload_used_when_no_og_image(tmp_path: Path, monkeypatch):
    """With no og:image, the preload hint is still a valid fallback."""
    import services.lead_images as li_mod
    monkeypatch.setattr(li_mod, "is_safe_outbound_url", lambda *a, **k: True)
    service = _build_service(tmp_path / "m.sqlite", [])
    html = (
        '<html><head>'
        '<link rel="preload" as="image" href="https://cdn.example.com/hero.jpg">'
        '</head><body><p>article</p></body></html>'
    )
    service._fetch_page_html = lambda link, **kw: (html, "https://site.test/a", False)
    assert service._fetch_source_lead_image("https://site.test/a") == "https://cdn.example.com/hero.jpg"
