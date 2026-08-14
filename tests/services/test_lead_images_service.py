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


def test_comiccontrol_thumb_is_left_alone(tmp_path: Path):
    # ComicControl feeds ship a small /comicsthumbs/<ts>-<file> image, but the
    # full panel carries a DIFFERENT cache-bust timestamp — so swapping only the
    # directory names a file that does not exist, and the site answers it with a
    # 200 placeholder (atomic-robo: 1.2MB panel at …494, 11KB placeholder at
    # …495). Blind promotion poisoned the cached lead image and with it the
    # thumbnail; promotion is only safe against the panel URL read from the page
    # (see main._promote_comicsthumbs_in_content).
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://www.atomic-robo.com/atomicrobo/rss",
        entry_id="ar-1",
        link="https://www.atomic-robo.com/atomicrobo/17ch1-page-1",
        content_html='<p>x</p><img src="https://www.atomic-robo.com/comicsthumbs/1781025836-ARV1701_01.jpg" />',
    )

    thumb = service.extract_entry_thumbnail_url(entry)

    assert thumb == "https://www.atomic-robo.com/comicsthumbs/1781025836-ARV1701_01.jpg"


def test_standard_ebooks_cover_on_fast_path(tmp_path: Path):
    # SE feed entries carry only a small media:thumbnail (which reader drops) and
    # no inline image, so the posts-list fast path returned nothing. The plugin's
    # deterministic cover URL (network-free) must fill it in.
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://standardebooks.org/rss/new-releases",
        entry_id="https://standardebooks.org/ebooks/walter-white/flight",
        link="https://standardebooks.org/ebooks/walter-white/flight",
    )

    thumb = service.extract_entry_thumbnail_url(entry, fast_only=True)

    assert thumb == "https://standardebooks.org/ebooks/walter-white/flight/downloads/cover.jpg"


def test_promote_known_thumbnail_is_noop_for_other_urls(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    # Substring-but-not-segment must not be rewritten.
    assert (
        service._promote_known_thumbnail("https://x/comicsthumbsfoo/a.jpg")
        == "https://x/comicsthumbsfoo/a.jpg"
    )
    assert service._promote_known_thumbnail(None) is None


def test_inline_thumb_url_keeps_comiccontrol_thumb(tmp_path: Path):
    # Same reasoning as above: a small correct thumbnail beats a big broken one.
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://www.atomic-robo.com/atomicrobo/rss",
        entry_id="ar-2",
        link="https://www.atomic-robo.com/atomicrobo/17ch1-page-1",
        content_html='<a href="x"><img src="https://www.atomic-robo.com/comicsthumbs/1-ARV.jpg"/></a>',
    )
    assert service.extract_inline_thumb_url(entry) == "https://www.atomic-robo.com/comicsthumbs/1-ARV.jpg"


def test_inline_thumb_url_promotes_bare_plaintext_image_url(tmp_path: Path):
    # Escaped-plaintext feeds (e.g. tracker.example news) ship the post image as a
    # bare URL with no <img> tag. Without promotion the inline extractor finds
    # nothing and the feed falls back to og_scrape (which on a login-gated source
    # page grabs the site logo). The bare URL must be surfaced as the thumbnail.
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://tracker.example/feeds.php?feed=feed_news",
        entry_id="https://tracker.example/index.php#news212",
        link="https://tracker.example/index.php#news212",
        content_html=(
            "https://i.ibb.co/Zp25NHbV/2-Sd-RZ3-GBUz.jpg&lt;br&gt;\n"
            "&lt;br&gt;\nAoTM June Round 1 has closed and we have a winner!&lt;br&gt;"
        ),
    )
    assert (
        service.extract_inline_thumb_url(entry)
        == "https://i.ibb.co/Zp25NHbV/2-Sd-RZ3-GBUz.jpg"
    )
    assert (
        service.extract_entry_thumbnail_url(entry)
        == "https://i.ibb.co/Zp25NHbV/2-Sd-RZ3-GBUz.jpg"
    )


def test_bare_url_promotion_skips_non_image_urls(tmp_path: Path):
    # A bare non-image URL (forum link) must not be promoted to an <img>.
    service = _build_service(tmp_path / "meta.sqlite", [])
    entry = _FakeEntry(
        feed_url="https://tracker.example/feeds.php?feed=feed_news",
        entry_id="news-x",
        link="https://tracker.example/index.php#news-x",
        content_html="Voting is open&lt;br&gt;\nhttps://tracker.example/forums.php?threadid=1",
    )
    assert service.extract_inline_thumb_url(entry) is None


def test_podcast_title_branding_image_rejected(tmp_path: Path):
    # og:scrape can fall back to a show-title branding graphic on a post with no
    # real featured image; reject it even on the cached (skip_logo_patterns) path.
    service = _build_service(tmp_path / "meta.sqlite", [])
    url = "https://ii.techdirt.com/s/t/i/podcast-title-small.png"
    assert service._is_image_url_acceptable(url, None, None, skip_logo_patterns=True) is False


def test_gog_chrome_subdomains_rejected(tmp_path: Path):
    # GOG article pages embed the nav "PATRON" badge and template hero images from
    # dedicated chrome subdomains before the article's og:image; they must never be
    # picked as the lead image, while real content on images-N.gog-statics.com stays.
    service = _build_service(tmp_path / "meta.sqlite", [])
    badge = "https://menu-static.gog-statics.com/assets/img/patron_badge.png"
    hero = "https://landing-pages.gog-statics.com/assets/images/hero-image.png"
    content = "https://images-4.gog-statics.com/3dc0df829359442b4369ac4d806f9161ba7d498ea36f168ebdf95558ccf89841.jpg"
    assert service._is_image_url_acceptable(badge, None, None, skip_logo_patterns=True) is False
    assert service._is_image_url_acceptable(hero, None, None, skip_logo_patterns=True) is False
    assert service._is_image_url_acceptable(content, None, None, skip_logo_patterns=True) is True


def test_source_header_banner_rejected_via_srcset_dims(tmp_path: Path):
    # PlayStation Blog's article header banner has no width/height attrs and a bare
    # src with no query, but its srcset carries resize=1900,470 (4:1). It must be
    # rejected so og:image (the real featured image) wins instead.
    service = _build_service(tmp_path / "meta.sqlite", [])
    banner_attrs = {
        "class": "header-image wp-image-420286",
        "fetchpriority": "high",
        "src": "https://blog.playstation.com/tachyon/2026/06/c53068d0.png",
        "srcset": "https://blog.playstation.com/tachyon/2026/06/c53068d0.png?resize=1900%2C470&zoom=1 1900w",
    }
    hero_attrs = {
        "class": "wp-post-image",
        "src": "https://blog.playstation.com/tachyon/2026/06/fac3adf5.png",
        "srcset": "https://blog.playstation.com/tachyon/2026/06/fac3adf5.png?resize=1600%2C900 1600w",
    }
    assert service._is_source_image_tag_acceptable(banner_attrs, banner_attrs["src"]) is False
    assert service._is_source_image_tag_acceptable(hero_attrs, hero_attrs["src"]) is True


def test_sponsor_icon_and_ad_blast_rejected(tmp_path: Path):
    # The Daily WTF post: feed content's only image is the Inedo buildmaster-icon
    # sponsor logo and the source page's first image is an /fblast/ ad blast; the
    # real image is the og:image author thumb, which must survive.
    service = _build_service(tmp_path / "meta.sqlite", [])
    sponsor = "https://thedailywtf.com/images/inedo/buildmaster-icon.png"
    ad_blast = "https://thedailywtf.com/fblast/0816b244af9d4758a39f08bf7cc5aec6"
    author = "https://s3.amazonaws.com/remy.jetpackshark.com/remy-thumb.jpg"
    assert service._is_image_url_acceptable(sponsor, None, None, allow_extensionless=True) is False
    assert service._is_image_url_acceptable(ad_blast, None, None, allow_extensionless=True) is False
    assert service._is_image_url_acceptable(author, None, None, allow_extensionless=True) is True


def test_inline_svg_icon_skipped_for_real_hero(tmp_path: Path):
    # An icon-classed / 20x20 inline <svg> (e.g. PlayStation Blog's download
    # glyph) must not win the inline lead-image slot; a real large hero <svg>
    # after it should, and an icon-only body yields no inline SVG lead image.
    service = _build_service(tmp_path / "meta.sqlite", [])
    icon = '<svg class="icon icon--download" width="20" height="20" viewBox="0 0 20 20"><path d="M8 1z"/></svg>'
    hero = '<svg viewBox="0 0 800 450"><rect width="800" height="450"/></svg>'
    assert service._is_decorative_inline_svg(icon) is True
    assert service._is_decorative_inline_svg(hero) is False
    assert service._extract_inline_svg_data_uri("x " + icon) is None
    assert service._extract_inline_svg_data_uri(icon + hero) is not None


def test_font_awesome_inline_icon_is_decorative(tmp_path: Path):
    """Font Awesome icons defeat both other decorative tests.

    Reported on paizo.com's Pathfinder blog: the nav chevron carries no "icon"
    word in its class, and its width/height are *viewBox units* (320x512), far
    above the pixel floor that catches ordinary UI glyphs — so it read as a
    hero and became the post's thumbnail. `svg-inline--fa` is Font Awesome's
    own marker, added by its JS to every inline icon.
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    chevron = (
        '<svg class="fa-lg svg-inline--fa fa-chevron-left fa-w-10" color="#888" '
        'height="512" viewBox="0 0 320 512" width="320"><path d="M8 1z"/></svg>'
    )
    tags = (
        '<svg class="fa-lg svg-inline--fa fa-tags fa-w-20" height="512" '
        'viewBox="0 0 640 512" width="640"><path d="M8 1z"/></svg>'
    )
    hero = '<svg viewBox="0 0 800 450"><rect width="800" height="450"/></svg>'

    assert service._is_decorative_inline_svg(chevron) is True
    assert service._is_decorative_inline_svg(tags) is True
    # A real inline hero must still win, even when the icons come first.
    assert service._is_decorative_inline_svg(hero) is False
    assert service._extract_inline_svg_data_uri(chevron + tags) is None
    assert service._extract_inline_svg_data_uri(chevron + hero) is not None


def test_plain_fa_prefixed_class_is_not_treated_as_an_icon(tmp_path: Path):
    """Only Font Awesome's real marker counts, not any `fa-` substring.

    Guards the deliberately narrow match: an illustration classed
    `alfa-romeo-art` contains "fa-" but is article art, not a glyph.
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    art = ('<svg class="alfa-romeo-art" viewBox="0 0 800 450">'
           '<rect width="800" height="450"/></svg>')
    assert service._is_decorative_inline_svg(art) is False


def test_banner_aspect_ratio_rejected_from_query_dims(tmp_path: Path):
    # WordPress/Jetpack resize= and fit= query params declare the served size.
    # A banner-shaped ratio (wider than 4:1) is a site-wide promo, not article
    # content — e.g. PlayStation Blog's 1900x470 featured banners.
    service = _build_service(tmp_path / "meta.sqlite", [])
    banner = "https://blog.playstation.com/tachyon/2026/06/c53068d0.png?resize=1900%2C470&zoom=1"
    fit_banner = "https://example.com/x.jpg?fit=1900,470"
    hero = "https://blog.playstation.com/tachyon/2026/06/abc.png?resize=1600%2C900"
    assert service._is_image_url_acceptable(banner, None, None, allow_extensionless=True) is False
    assert service._is_image_url_acceptable(fit_banner, None, None, allow_extensionless=True) is False
    # A normal 16:9 hero at the same CDN must still pass.
    assert service._is_image_url_acceptable(hero, None, None, allow_extensionless=True) is True


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


def test_webcomic_panel_skips_query_loop_sibling_posts(tmp_path: Path):
    """WordPress block-theme single-post pages (e.g. karlkerschl.com) embed
    `wp-block-query` Query Loops listing OTHER posts, each with a `wp-post-image`
    featured thumbnail. The webcomic panel extractor must strip those loops first
    so it returns None here (no own panel) rather than a pinned sibling post's
    thumbnail. Regression: nearly every entry showed one featured post's image."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    page = "https://karlkerschl.com/gotham-academy-short-story-process/"
    html = (
        "<html><body><main>"
        # The post's own content images (NOT featured/wp-post-image).
        '<figure><img class="wp-image-22525" '
        'src="https://karlkerschl.com/wp-content/uploads/2022/09/GA-BTS-01-rough-675x1024.jpg"></figure>'
        # Query Loop of OTHER posts: a pinned "patreon" post + siblings.
        '<div class="wp-block-query is-layout-flow"><ul class="wp-block-post-template">'
        '<li class="wp-block-post post-7884"><figure class="wp-block-post-featured-image">'
        '<a href="https://karlkerschl.com/how-i-built-my-own-patreon-alternative/">'
        '<img width="870" height="532" class="attachment-post-thumbnail wp-post-image" '
        'alt="How I Built My Own Patreon Alternative" '
        'src="https://karlkerschl.com/wp-content/uploads/2021/08/patreon-alternative-feature.jpg"></a>'
        "</figure></li></ul></div>"
        "</main></body></html>"
    )
    # Webcomic panel extraction: no own panel survives stripping -> None.
    assert service._extract_webcomic_panel_image(html, page, page) is None
    # Full preferred-image scan then surfaces the post's own first content image.
    assert service._extract_preferred_source_image_url(html, page, page, is_webcomic=True) == (
        "https://karlkerschl.com/wp-content/uploads/2022/09/GA-BTS-01-rough-675x1024.jpg"
    )
    # The webcomic alt fallback must NOT return the sibling post's featured-image
    # alt text — the post's own content image has no alt, so caption is None.
    assert service._extract_webcomic_alt_text(html) is None


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


def test_source_image_ignores_recommended_videos_widget(tmp_path: Path):
    # c-sharpcorner posts with no image of their own render a "Recommended Videos"
    # widget whose thumbnails belong to OTHER articles — never borrow one.
    service = _build_service(tmp_path / "meta.sqlite", [])
    html = (
        "<main></main>"
        '<div class="videos-section"><ul class="videoList"><li>'
        '<img width="320" height="180" '
        'src="https://www.c-sharpcorner.com/article/other/Media/mqdefault.jpg">'
        "</li></ul></div>"
    )

    url = service._extract_preferred_source_image_url(
        html,
        "https://www.c-sharpcorner.com/article/post/",
        "https://www.c-sharpcorner.com/article/post/",
    )

    assert url is None


def test_twitter_card_and_brand_logo_rejected(tmp_path: Path):
    # Generic share-card / site-wordmark og:image files must not become lead images.
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert not service._is_image_url_acceptable(
        "https://www.c-sharpcorner.com/UploadFile/TwitterCard/twitter_card_logo.png",
        None,
        None,
    )
    assert not service._is_image_url_acceptable(
        "https://www.c-sharpcorner.com/images/csharp-corner-new.png", None, None
    )
    # A real per-article image on the same host stays acceptable.
    assert service._is_image_url_acceptable(
        "https://www.c-sharpcorner.com/article/post/Media/hero.jpg", 800, 450
    )


def test_template_placeholder_url_rejected(tmp_path: Path):
    # An inline JS/mustache <img> template scraped from a source page yields a URL
    # with an unresolved placeholder the browser can't load (→ thumb flicker).
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert not service._is_image_url_acceptable(
        "https://www.c-sharpcorner.com/article/foo/${challenge.MinorCategoryImage}",
        None,
        None,
    )
    assert not service._is_image_url_acceptable(
        "https://example.com/img/{{thumbnail}}.jpg", None, None
    )
    assert service._is_image_url_acceptable(
        "https://example.com/article/hero.jpg", 800, 450
    )


def test_avatar_hint_not_triggered_by_profile_in_artwork_title(tmp_path: Path):
    # A DeviantArt piece titled "…Profile…" carries "profile" as a title word in
    # its filename (preceded by "_"); it must not be mistaken for an author headshot.
    service = _build_service(tmp_path / "meta.sqlite", [])
    art = (
        "/f/xx/dmdcn02.jpg/v1/fill/w_1280,h_854,q_75,strp/"
        "collared_peccary_profile__enclosure__by_artist_dmdcn02-fullview.jpg"
    )
    assert service._AVATAR_HINT_PATTERNS.search(art) is None
    # Real profile/avatar paths still flagged.
    assert service._AVATAR_HINT_PATTERNS.search("/users/profile.jpg") is not None
    assert service._AVATAR_HINT_PATTERNS.search("/user-profile-photo.png") is not None


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


def test_detected_og_scrape_prefers_source_over_a_mid_body_image(tmp_path: Path, monkeypatch):
    """A DETECTED og_scrape feed must scrape the source too, not just a manual one.

    sonarsource.com/blog: most posts carry no body image, so they scrape their
    og:image and the feed detects as og_scrape. The two or three posts that DO
    embed a mid-article screenshot used to short-circuit on that inline image and
    take the screenshot as their hero and thumbnail, while every post around them
    was fine. The publisher's og:image is the lead; a screenshot dropped
    mid-paragraph is not.
    """
    db_path = tmp_path / "meta.sqlite"
    feed = "https://www.sonarsource.com/rss/blog.xml"
    body_img = "https://assets.example.com/a/openinterminal-issue.png"
    og_img = "https://assets.example.com/b/openinterminal-blog-landscape.webp"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="p-sonar",
        link="https://www.sonarsource.com/blog/escape-from-applescript/",
        content_html=f'<p>we are presented with a finding:</p><img src="{body_img}"/><p>more</p>',
    )
    service = _build_service(db_path, [entry])
    # Detected, NOT manually locked — the whole point of the regression.
    service.store_feed_strategy(feed, "og_scrape", manual=False)
    monkeypatch.setattr(service, "_fetch_source_lead_image", lambda *a, **k: og_img)

    service.fetch_and_store_lead_images_for_feed(feed, force_retry_negative=True)

    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE entry_id = ?", ("p-sonar",)
        ).fetchone()
    assert row is not None
    assert row["image_url"] == og_img, "mid-body screenshot won over the publisher's og:image"


def test_detected_og_scrape_keeps_inline_when_source_misses(tmp_path: Path, monkeypatch):
    """Falling through must not cost the inline image when the source yields nothing.

    Same protection the manual case already had — a fresh post whose og:image is
    not generated yet keeps its body image rather than losing its thumbnail.
    """
    db_path = tmp_path / "meta.sqlite"
    feed = "https://www.sonarsource.com/rss/blog.xml"
    body_img = "https://assets.example.com/a/screenshot.png"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="p-fresh",
        link="https://www.sonarsource.com/blog/fresh/",
        content_html=f'<p>x</p><img src="{body_img}"/>',
    )
    service = _build_service(db_path, [entry])
    service.store_feed_strategy(feed, "og_scrape", manual=False)
    monkeypatch.setattr(service, "_fetch_source_lead_image", lambda *a, **k: None)

    service.fetch_and_store_lead_images_for_feed(feed, force_retry_negative=True)

    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE entry_id = ?", ("p-fresh",)
        ).fetchone()
    assert row is not None
    assert row["image_url"] == body_img


def test_detected_inline_feed_still_short_circuits(tmp_path: Path, monkeypatch):
    """The shortcut still holds for feeds whose images really are inline.

    Guards the scope of the fix: only og_scrape (and webcomic) fall through, so
    an inline feed does not start fetching a source page per entry.
    """
    db_path = tmp_path / "meta.sqlite"
    feed = "https://inline.example/feed"
    body_img = "https://inline.example/img/cover.jpg"
    entry = _FakeEntry(
        feed_url=feed,
        entry_id="p-inline",
        link="https://inline.example/post/",
        content_html=f'<img src="{body_img}"/>',
    )
    service = _build_service(db_path, [entry])
    service.store_feed_strategy(feed, "inline", manual=False)

    def _boom(*a, **k):
        raise AssertionError("inline feed must not fetch the source page")

    monkeypatch.setattr(service, "_fetch_source_lead_image", _boom)

    service.fetch_and_store_lead_images_for_feed(feed, force_retry_negative=True)

    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE entry_id = ?", ("p-inline",)
        ).fetchone()
    assert row is not None and row["image_url"] == body_img


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


def test_logo_with_digit_suffix_rejected(tmp_path: Path):
    """logo<digits> is a versioned wordmark (questionablecontent's logo2026.png
    became a lead image via the old [a-zA-Z0-9] lookahead); letter compounds
    like imdblogo stay content."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._is_image_url_acceptable(
        "https://questionablecontent.net/images/logo2026.png", None, None) is False
    assert service._is_image_url_acceptable(
        "https://www.therockcocks.com/the-rock-cocks/images/logo.png", None, None) is False
    # Compound words with letters keep passing (imdblogo precedent).
    assert service._is_image_url_acceptable(
        "https://example.com/media/imdblogo-poster.jpg", None, None) is True


def test_social_badge_basename_rejected(tmp_path: Path):
    """A bare social-platform basename is a header/footer link badge
    (meetingcpp.com/files/meetup.png won the body scan on og:image-less pages)."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._is_image_url_acceptable(
        "https://www.meetingcpp.com/files/meetup.png", None, None) is False
    # A platform name inside a longer basename is not a badge.
    assert service._is_image_url_acceptable(
        "https://example.com/photos/meetup-group-photo.jpg", None, None) is True


def test_plugin_fallback_urls_are_validated(tmp_path: Path):
    """A plugin's own og:image fetch can return the SITE LOGO (age-gated
    therockcocks page, 2026-07-10) — the wrapper must reject it like any
    other candidate instead of caching it."""
    service = _build_service(tmp_path / "meta.sqlite", [])

    class _LogoPlugin:
        def fallback_lead_image_url(self, *, entry_link, content_html, summary):
            return "https://www.therockcocks.com/the-rock-cocks/images/logo.png"

    class _ComicPlugin:
        def fallback_lead_image_url(self, *, entry_link, content_html, summary):
            return "https://www.therockcocks.com/comics/1783297346-RockCocks_1256.png"

    service._plugins = [_LogoPlugin()]
    assert service._plugin_fallback_lead_image_url(
        entry_link="https://www.therockcocks.com/the-rock-cocks/page-1256-nsfw",
        content_html=None, summary=None) is None

    service._plugins = [_LogoPlugin(), _ComicPlugin()]
    assert service._plugin_fallback_lead_image_url(
        entry_link="https://www.therockcocks.com/the-rock-cocks/page-1256-nsfw",
        content_html=None, summary=None) == "https://www.therockcocks.com/comics/1783297346-RockCocks_1256.png"


def test_support_platform_link_context_is_chrome(tmp_path: Path):
    """An image wrapped in a link to a support/social platform is that
    platform's badge (meetingcpp's topbar patreon.png/meetup.png), regardless
    of the image filename."""
    ctx = '<div class="gridcenteritem"> <a href="https://www.patreon.com/bePatron?u=3512102">'
    assert LeadImageService._SITE_CHROME_CONTEXT_RE.search(ctx)
    ctx2 = '<a href="https://www.meetup.com/Meeting-Cpp-online/">'
    assert LeadImageService._SITE_CHROME_CONTEXT_RE.search(ctx2)
    # A plain article link is not chrome context.
    assert not LeadImageService._SITE_CHROME_CONTEXT_RE.search('<a href="https://example.com/story">')


def test_bare_comic_class_wins_the_panel_scan(tmp_path: Path):
    """A bare `comic` class must win the panel scan (qwantz.com).

    Without it the site's decorative first image wins instead. Background in
    Plan.md 8e.
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    page = "http://www.qwantz.com/index.php?comic=4499"
    html = (
        "<html><body>"
        '<img src="/pteranodon.png" class="pteranodon">'
        '<img src="/logo5.png" width=390 height=56 class="logo">'
        '<img src="comics/comic2-5197.png" class="comic" title="alt text here">'
        '<img src="feedrss.gif" class="feedicon">'
        "</body></html>"
    )
    assert service._extract_webcomic_panel_image(html, page, page) == (
        "http://www.qwantz.com/comics/comic2-5197.png"
    )


def test_bare_comic_match_does_not_leak_into_hyphenated_classes(tmp_path: Path):
    """The bare alternative uses lookarounds, not \\b.

    With \\b it would also fire inside "comic-nav", handing the panel bonus to
    navigation buttons — the precise failure the pattern exists to prevent.
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    rx = service._WEBCOMIC_IMG_CLASS_RE
    assert rx.search("comic") and rx.search("comic_image") and rx.search("webcomic")
    assert not rx.search("comic-nav")
    assert not rx.search("comic_nav")
    assert not rx.search("comic-navigation")
    assert not rx.search("comicnav")


# --- social/share icon rejection ---
def test_social_icon_urls_are_rejected():
    """A post with no images of its own leaves the site's share row as the
    best-scoring picture on the page — accu.org shipped /img/bsky.png as an
    article thumbnail."""
    from services.lead_images import LeadImageService

    for url in (
        "https://accu.org/img/bsky.png",
        "https://x.test/icons/social-twitter.svg",
        "https://x.test/img/rss.png",
        "https://x.test/share-facebook-32.png",
        "https://x.test/follow_mastodon.png",
    ):
        assert LeadImageService.is_social_icon_url(url), url


def test_articles_about_social_networks_keep_their_images():
    """Matched on the filename STEM, not anywhere in the URL. An article genuinely
    about Bluesky may carry "bluesky-screenshot-2026.png", and a substring rule
    would throw away the real picture."""
    from services.lead_images import LeadImageService

    for url in (
        "https://x.test/img/bluesky-screenshot-2026.png",
        "https://x.test/photos/twitter-acquisition-analysis.jpg",
        "https://x.test/2026/07/facebook-outage-timeline.png",
        "https://x.test/img/hero-landscape.jpg",
    ):
        assert not LeadImageService.is_social_icon_url(url), url


def test_social_link_anchors_are_stripped_before_scoring():
    """The spelling-independent signal, and the reason a filename list is not
    enough: accu.org's icon row is /img/bsky.png, /img/mastadon.png (their typo),
    /img/facebook.png … so a name list is always one misspelling behind. What they
    share is an enclosing <a> pointing at the network itself.
    """
    import main
    svc = main.lead_image_service
    html = (
        '<a href="https://bsky.app/profile/x"><img src="/img/bsky.png"></a>'
        '<a href="https://mastodon.social/@ACCU"><img src="/img/mastadon.png"></a>'
        '<a href="https://example.com/article"><img src="/img/real-photo.jpg"></a>'
    )
    out = svc._strip_social_link_images(html)
    assert "bsky.png" not in out
    assert "mastadon.png" not in out
    assert "real-photo.jpg" in out          # a content link keeps its image


def test_social_link_detection():
    from services.lead_images import LeadImageService as L

    assert L.is_social_link_href("https://bsky.app/profile/x")
    assert L.is_social_link_href("https://mastodon.social/@ACCU")
    assert L.is_social_link_href("https://fosstodon.org/@someone")   # any instance
    assert L.is_social_link_href("https://www.github.com/accu-org")
    # A link to an article that merely mentions a network is not a social link.
    assert not L.is_social_link_href("https://blog.example.com/2026/07/twitter-analysis")
    assert not L.is_social_link_href("https://example.com/@notprofile/post")


def test_ad_server_urls_are_rejected():
    """Once the social row is stripped from an image-less post, the ad slots are
    the next best-scoring "picture" — accu.org served
    ads.accu.org/www/delivery/avw.php."""
    from services.lead_images import LeadImageService as L

    assert L.is_ad_url("https://ads.accu.org/www/delivery/avw.php?zoneid=2")
    assert L.is_ad_url("https://ad.example.com/x.gif")
    assert L.is_ad_url("https://pagead2.googlesyndication.com/x.png")
    assert L.is_ad_url("https://example.com/adserver/banner.png")
    # Words that merely start with "ad" are not ad servers.
    assert not L.is_ad_url("https://example.com/adventure-photos/hero.jpg")
    assert not L.is_ad_url("https://example.com/img/badges/award.png")


def test_a_platforms_own_media_is_not_a_share_icon():
    """⚠ A social link is NOT enough on its own to call an image chrome.

    A Tumblr-hosted webcomic wraps its comic in a link to its own Tumblr post, so
    host-only matching DELETED the comic — theycantalk.com and every Tumblr
    webcomic like it. The discriminator: a share icon is hosted by the SITE and
    links OUT to the network, while a platform's own media is hosted BY that
    platform.
    """
    import main
    svc = main.lead_image_service

    comic = ('<a href="https://theycantalk.tumblr.com/post/823519750908002304">'
             '<img src="https://64.media.tumblr.com/a/s1280x1920/comic.jpg"></a>')
    assert "comic.jpg" in svc._strip_social_link_images(comic)

    icon = '<a href="https://bsky.app/profile/accuorg"><img src="/img/bsky.png"></a>'
    assert "bsky.png" not in svc._strip_social_link_images(icon)


def test_service_cdn_media_survives():
    """Services that serve media from a separate domain need an alias, or a
    YouTube poster (i.ytimg.com) linking to youtube.com reads as "site icon
    linking out" and gets stripped."""
    import main
    svc = main.lead_image_service

    html = ('<a href="https://www.youtube.com/watch?v=x">'
            '<img src="https://i.ytimg.com/vi/x/hqdefault.jpg"></a>')
    assert "hqdefault.jpg" in svc._strip_social_link_images(html)


def test_ad_slot_names_and_sizes_are_rejected():
    """Publishers upload house ads to the same media directory as everything else,
    so host and path say nothing — decibelmagazine served
    "…-hero-superbanner.gif" out of wp-content."""
    from services.lead_images import LeadImageService as L

    assert L.is_ad_url("https://decibelmagazine.com/wp-content/uploads/x-hero-superbanner.gif")
    assert L.is_ad_url("https://x.test/img/leaderboard-728x90.png")
    assert L.is_ad_url("https://x.test/img/sponsored-post-header.jpg")
    # 728x90 is the IAB leaderboard: a slot fill, never a photograph.
    assert L.is_ad_dimension(728, 90)
    assert not L.is_ad_dimension(1200, 800)


def test_bare_banner_is_not_an_ad():
    """⚠ Deliberately excluded. Sites name hero/header images banner.jpg all the
    time, and an existing og:image test caught exactly that false positive when
    bare "banner" was in the list. The creative that motivated the rule
    ("…-content-banner.jpg") is caught by its 728x90 size instead, which is the
    stronger signal.
    """
    from services.lead_images import LeadImageService as L

    assert not L.is_ad_url("https://cdn.example.com/banner.jpg")
    assert not L.is_ad_url("https://x.test/img/site-banner.png")
    # A section ABOUT sponsors is not a creative: only the filename is matched.
    assert not L.is_ad_url("https://x.test/sponsors/our-partners-hero.jpg")


def test_a_storing_path_honors_a_plugin_that_forbids_the_source_page(tmp_path: Path):
    """`should_skip_source_lookup` was consulted on 3 of 12 `_fetch_source_lead_image`
    call sites, and the backfill's storing paths were not among them — so a
    plugin-owned host got the right image on the render path and had it
    overwritten by a background revalidation that scraped the page anyway.
    That is how Webtoons episodes went back to the series thumbnail hours after
    the plugin was fixed."""
    entry = _FakeEntry(
        feed_url="https://example.test/feed",
        entry_id="e1",
        link="https://plugin-owned.test/episode/1",
        content_html='<img src="https://cdn.test/panel.jpg"/>',
    )
    svc = _build_service(tmp_path / "meta.sqlite3", [entry])

    scraped: list[str] = []

    def _never(entry_link, is_webcomic=False):
        scraped.append(entry_link)
        return "https://cdn.test/social-card.png"

    svc._fetch_source_lead_image = _never  # type: ignore[method-assign]
    svc._plugin_should_skip_source_lookup = (  # type: ignore[method-assign]
        lambda *, entry_link: "plugin-owned.test" in entry_link
    )
    svc._plugin_fallback_lead_image_url = (  # type: ignore[method-assign]
        lambda *, entry_link, content_html, summary: "https://cdn.test/panel.jpg"
    )

    got = svc._plugin_or_source_lead_image(entry, entry.link, is_webcomic=True)

    assert got == "https://cdn.test/panel.jpg"
    assert scraped == [], "the page must not be fetched when a plugin forbids it"


def test_a_storing_path_still_scrapes_for_hosts_no_plugin_claims(tmp_path: Path):
    entry = _FakeEntry(
        feed_url="https://example.test/feed", entry_id="e1",
        link="https://ordinary.test/post/1",
    )
    svc = _build_service(tmp_path / "meta.sqlite3", [entry])
    svc._fetch_source_lead_image = (  # type: ignore[method-assign]
        lambda entry_link, is_webcomic=False: "https://cdn.test/og.jpg"
    )
    svc._plugin_should_skip_source_lookup = lambda *, entry_link: False  # type: ignore[method-assign]

    assert svc._plugin_or_source_lead_image(
        entry, entry.link, is_webcomic=False
    ) == "https://cdn.test/og.jpg"


def test_a_forbidding_plugin_with_no_answer_yields_none_not_a_scrape(tmp_path: Path):
    entry = _FakeEntry(
        feed_url="https://example.test/feed", entry_id="e1",
        link="https://plugin-owned.test/episode/1",
    )
    svc = _build_service(tmp_path / "meta.sqlite3", [entry])
    svc._fetch_source_lead_image = (  # type: ignore[method-assign]
        lambda entry_link, is_webcomic=False: "https://cdn.test/social-card.png"
    )
    svc._plugin_should_skip_source_lookup = lambda *, entry_link: True  # type: ignore[method-assign]
    svc._plugin_fallback_lead_image_url = (  # type: ignore[method-assign]
        lambda *, entry_link, content_html, summary: None
    )

    assert svc._plugin_or_source_lead_image(entry, entry.link, is_webcomic=True) is None


def test_a_uuid_filename_is_not_read_as_an_ad_slot(tmp_path: Path):
    """`[-_]ad[0-9]` exists for `Cert-ad1.png`, and it cannot tell that from
    `-ad27-` inside a UUID. Two Tapas panels were rejected that way, and the
    wixmp host-trust was added for the same class one host at a time."""
    svc = _build_service(tmp_path / "meta.sqlite3", [])
    for url in (
        "https://us-a.tapas.io/sa/a3/ff52deff-c6a8-448d-ad27-a3c3d14c719c.jpg",
        "https://us-a.tapas.io/sa/34/811959d7-ad1c-47c0-8ee1-570d77204f48.jpg",
        # The same class in the other name-based filters.
        "https://cdn.test/6566d9f3-5857-4a02-98f2-f1941bb0f8f0.png",
        # Webtoons appends a numeric id straight onto the UUID's last group
        # with no separator, which a trailing-separator-only rule missed.
        "https://swebtoon-phinf.pstatic.net/20251231_227/x_JPEG/"
        "53e3fa05-ad49-4593-b2ac-782469d45a9212398245534840153981.jpg",
    ):
        assert svc._is_image_url_acceptable(url, None, None) is True, url


def test_a_real_ad_slot_name_is_still_rejected(tmp_path: Path):
    """The UUID exemption must not blunt the filter it protects."""
    svc = _build_service(tmp_path / "meta.sqlite3", [])
    for url in (
        "https://example.test/img/Cert-ad1.png",
        "https://example.test/ads/banner.png",
        "https://example.test/advert/x.jpg",
    ):
        assert svc._is_image_url_acceptable(url, None, None) is False, url


def test_a_uuid_name_with_a_suffix_is_still_opaque(tmp_path: Path):
    svc = _build_service(tmp_path / "meta.sqlite3", [])
    assert svc._is_image_url_acceptable(
        "https://cdn.test/ff52deff-c6a8-448d-ad27-a3c3d14c719c-1200x800.jpg", None, None
    ) is True


def test_path_based_rejections_survive_an_opaque_name(tmp_path: Path):
    """Only *name* heuristics are skipped — a UUID sitting in an ads directory
    is still an ad."""
    svc = _build_service(tmp_path / "meta.sqlite3", [])
    assert svc._is_image_url_acceptable(
        "https://example.test/ads/ff52deff-c6a8-448d-ad27-a3c3d14c719c.jpg", None, None
    ) is False


def test_the_on_open_fetch_is_skipped_for_a_plugin_owned_host(tmp_path: Path):
    """`queue_source_fetch` is the on-open path, so it is the one a *user*
    triggers by clicking an entry — and it persisted whatever the page gave it.
    That is how Webtoons episodes kept reacquiring the series thumbnail after
    the backfill's storing paths were fixed."""
    svc = _build_service(tmp_path / "meta.sqlite3", [])
    scraped: list[str] = []
    svc._fetch_source_lead_image = (  # type: ignore[method-assign]
        lambda entry_link, is_webcomic=False: scraped.append(entry_link) or "https://cdn.test/og.png"
    )
    svc._plugin_should_skip_source_lookup = (  # type: ignore[method-assign]
        lambda *, entry_link: "plugin-owned.test" in entry_link
    )

    svc.queue_source_fetch("https://f.test/feed", "e1", "https://plugin-owned.test/episode/1")
    svc.wait_for_source_fetch("https://f.test/feed", "e1", timeout=1.0)

    assert scraped == []
    assert svc.get_cached_lead_image_url("https://f.test/feed", "e1") is None


# --- Tumblr writes the size as a path segment, prefixed with "s" -------------
#
# ".../s1280x1920/..." is a post's image; ".../s64x64u_c1/..." is the BLOG's
# avatar. _URL_DIMENSION_RE cannot see either, because it wants the digits to
# follow a separator and here they follow the "s" — so a 64x64 avatar sailed
# past the size floor and became a comic's lead image. Worse for a webcomic
# feed whose body carries no image: the bodyless-entry injector then put the
# avatar in the article (theycantalk.com, 2026-08-13).


def _acceptable(service, url: str) -> bool:
    return service._is_image_url_acceptable(url, None, None, allow_extensionless=True)


_TUMBLR_AVATAR = (
    "https://64.media.tumblr.com/b05eac3c2a9d8d3e276163870a65a091/"
    "d133bf64a6916e97-a4/s64x64u_c1/7e1cf3f8bd2b36ef72cdbc2bb89d1a41d7140123.jpg"
)
_TUMBLR_POST_IMAGE = (
    "https://64.media.tumblr.com/cb1f43c2950792577e0d14da334592f2/"
    "26be0f55f0c59947-c9/s1280x1920/1924c9bc735644255a1020a493a9fbc5ea649a6f.jpg"
)
_TUMBLR_AVATAR_128 = (
    "https://64.media.tumblr.com/aaaabbbbccccddddeeeeffff00001111/"
    "2222333344445555-66/s128x128u_c1/7777888899990000aaaabbbbccccddddeeeeffff.jpg"
)


def test_tumblr_avatar_crop_is_rejected(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert _acceptable(service, _TUMBLR_AVATAR) is False


def test_tumblr_128_avatar_crop_is_rejected(tmp_path: Path):
    """norasuko-art's five, the same shape one size up."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert _acceptable(service, _TUMBLR_AVATAR_128) is False


def test_a_real_tumblr_post_image_still_passes(tmp_path: Path):
    """Scope guard: 486 legitimate leads on the live library use this shape."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert _acceptable(service, _TUMBLR_POST_IMAGE) is True


def test_the_size_segment_must_be_a_whole_segment(tmp_path: Path):
    """It cannot fire inside an ordinary filename."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._PATH_SIZE_SEGMENT_RE.search("/images/photo-s64x64.jpg") is None
    assert service._PATH_SIZE_SEGMENT_RE.search("/a/s64x64u_c1/b.jpg") is not None


# --- a caption repeated across a feed is a tagline, not a punchline ----------
#
# Webcomic caption extraction falls back to og:description so hover-text
# punchlines survive. Penny Arcade puts a fixed site blurb there instead, on
# every strip, and ships no og:site_name for the existing pbfcomics guard to
# compare against. Nothing in a single page marks it as boilerplate — what marks
# it is that it does not vary.

_PA_TAGLINE = ("Videogaming-related online strip by Mike Krahulik and Jerry Holkins. "
               "Includes news and commentary.")


def _stored_title(db_path: Path, entry_id: str) -> str | None:
    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT image_title FROM entry_lead_images WHERE entry_id = ?", (entry_id,)
        ).fetchone()
    return row["image_title"] if row else None


def test_the_first_entry_keeps_the_caption(tmp_path: Path):
    """It cannot know yet — one occurrence is indistinguishable from a punchline."""
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    service.store_entry_image_alt("f", "e1", None, title_text=_PA_TAGLINE)
    assert _stored_title(db, "e1") == _PA_TAGLINE


def test_a_repeat_clears_it_from_the_whole_feed(tmp_path: Path):
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    service.store_entry_image_alt("f", "e1", None, title_text=_PA_TAGLINE)
    service.store_entry_image_alt("f", "e2", None, title_text=_PA_TAGLINE)

    assert _stored_title(db, "e2") is None, "second entry stored the tagline"
    assert _stored_title(db, "e1") is None, "first entry was not cleaned up"


def test_the_in_memory_title_cache_is_cleared_too(tmp_path: Path):
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    service.store_entry_image_alt("f", "e1", None, title_text=_PA_TAGLINE)
    service.store_entry_image_alt("f", "e2", None, title_text=_PA_TAGLINE)
    assert service._title_cache[("f", "e1")] is None


def test_a_genuine_per_post_caption_survives(tmp_path: Path):
    """The whole point: xkcd-style hover text differs per strip and must stay."""
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    service.store_entry_image_alt("f", "e1", None, title_text="A punchline")
    service.store_entry_image_alt("f", "e2", None, title_text="A different punchline")
    assert _stored_title(db, "e1") == "A punchline"
    assert _stored_title(db, "e2") == "A different punchline"


def test_another_feeds_identical_caption_is_not_affected(tmp_path: Path):
    """Scope is one feed — two sites may legitimately share a sentence."""
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    service.store_entry_image_alt("feed-a", "e1", None, title_text=_PA_TAGLINE)
    service.store_entry_image_alt("feed-b", "e1", None, title_text=_PA_TAGLINE)
    with _make_conn(db) as conn:
        rows = conn.execute(
            "SELECT feed_url, image_title FROM entry_lead_images ORDER BY feed_url"
        ).fetchall()
    assert [r["image_title"] for r in rows] == [_PA_TAGLINE, _PA_TAGLINE]


def test_alt_text_is_untouched_by_the_guard(tmp_path: Path):
    db = tmp_path / "meta.sqlite"
    service = _build_service(db, [])
    service.store_entry_image_alt("f", "e1", "alt one", title_text=_PA_TAGLINE)
    service.store_entry_image_alt("f", "e2", "alt two", title_text=_PA_TAGLINE)
    with _make_conn(db) as conn:
        rows = conn.execute(
            "SELECT entry_id, image_alt FROM entry_lead_images ORDER BY entry_id"
        ).fetchall()
    assert [r["image_alt"] for r in rows] == ["alt one", "alt two"]


# --- an age gate is the one image guaranteed not to be the post ---------------
#
# An adult webcomic serves a content-warning interstitial INSTEAD of the strip,
# so a page scrape picks it up as though it were the comic. monstersoupcomic.com
# captioned a post about paintbrushes with its maturecontentwarning.png.


def test_age_gate_images_are_rejected(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    for url in (
        "https://monstersoupcomic.com/wp-content/uploads/2025/08/maturecontentwarning.png",
        "https://x.test/img/mature-content-warning.png",
        "https://x.test/img/age_gate.jpg",
        "https://x.test/img/age-verification.png",
        "https://x.test/img/nsfw-warning.png",
    ):
        assert _acceptable(service, url) is False, url


def test_a_comic_whose_title_contains_those_words_survives(tmp_path: Path):
    """Scope guard: the words appear in real titles, so only the gate shapes match."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    for url in (
        "https://x.test/comics/the-warning-sign-chapter-4.jpg",
        "https://x.test/comics/mature-audiences-episode.jpg",
        "https://x.test/comics/a-warning-from-space.png",
    ):
        assert _acceptable(service, url) is True, url


# --- "<name>_on.png" / "_off.png" is a nav button's two states ---------------
#
# monstersoupcomic.com/images/blog_on.png (99x44) became the lead on its text
# posts once the age-gate graphic stopped winning. The size floor cannot help:
# the dimensions are neither in the URL nor declared on the tag, so nothing
# measures it without fetching the bytes.


def test_rollover_nav_sprites_are_rejected(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    for url in ("http://monstersoupcomic.com/images/blog_on.png",
                "http://x.test/images/home_off.gif",
                "http://x.test/nav/about_on.jpg"):
        assert _acceptable(service, url) is False, url


def test_a_filename_merely_ending_in_those_letters_survives(tmp_path: Path):
    """Anchored to the whole basename, so real titles are unaffected."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    for url in ("http://x.test/comics/lights-on.jpg",
                "http://x.test/comics/the_one.png",
                "http://x.test/comics/switched_on_and_off_again.png",
                "http://x.test/comics/2026-08-13-showdown.jpg"):
        assert _acceptable(service, url) is True, url


# --- <img> written by JavaScript is source code, not an image ---------------
#
# monstersoupcomic's bookmark widget does
#   document.write('<a …><img src="'+imgTag+'" …>')
# and the page scan produced the lead image
# "https://monstersoupcomic.com/'+imgTag+'" — a URL that cannot resolve to
# anything. Stripped once at fetch time rather than at each of the ten
# _IMG_TAG_RE scan sites, so no future scan can forget.


def test_script_written_img_tags_are_not_candidates(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    page = (
        '<p><img src="/real.jpg"></p>'
        "<script>document.write('<img src=\"'+imgTag+'\">')</script>"
        '<img src="/also-real.png">'
    )
    out = service._strip_script_blocks(page)
    assert "imgTag" not in out
    assert "/real.jpg" in out and "/also-real.png" in out


def test_stripping_scripts_is_a_passthrough_without_any(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._strip_script_blocks("<p>hi</p>") == "<p>hi</p>"


def test_meta_tags_survive_script_stripping(tmp_path: Path):
    """og:image lives in <head> and must be untouched."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    page = '<head><meta property="og:image" content="/hero.jpg"></head><script>var x=1</script>'
    out = service._strip_script_blocks(page)
    assert 'content="/hero.jpg"' in out


# --- a comic's prev/next arrows must not become its lead ---------------------
#
# dresdencodak's feed opens with <img alt="Previous" height="30"
# src=".../prev_002.png">, so the 30px arrow won the first-image bonus and
# became both the hero and the thumbnail. main.py already strips these from the
# article body (_COMIC_NAV_ALT_RE / _COMIC_NAV_SRC_RE); nothing stopped one
# being chosen as the lead.


def test_comic_nav_arrows_are_rejected(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    for url in ("https://dresdencodak.com/wp-content/uploads/2019/03/prev_002.png",
                "https://dresdencodak.com/wp-content/uploads/2019/03/first_001.png",
                "https://x.test/img/next.gif",
                "https://x.test/img/previous-1.png",
                "https://x.test/img/last.png"):
        assert _acceptable(service, url) is False, url


def test_comics_named_after_those_words_survive(tmp_path: Path):
    """These are ordinary English, so the match is anchored to a bare basename."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    for url in ("https://x.test/comics/first-contact.jpg",
                "https://x.test/comics/next-door.png",
                "https://x.test/comics/the-last-stand.jpg",
                "https://x.test/comics/back-to-school.png",
                "https://dresdencodak.com/wp-content/uploads/2026/08/dc_minis_27.jpg"):
        assert _acceptable(service, url) is True, url


# --- dresdencodak: the site draws its own list crop --------------------------
#
# The comic is a tall column (#27 is 1500x4875), a sliver at thumbnail size,
# while ".../dc_minis_27_thumbnail.jpg" is a 2500x1000 landscape crop drawn for
# exactly this. Same split as Penny Arcade's strip/panel, different naming — and
# only half kept by the site, which is what bounds the rule.

_DC_FULL = "https://dresdencodak.com/wp-content/uploads/2026/08/dc_minis_27.jpg"
_DC_THUMB = "https://dresdencodak.com/wp-content/uploads/2026/08/dc_minis_27_thumbnail.jpg"
_DC_LINK = "https://dresdencodak.com/2026/08/09/dc-minis-27-birthday-blues/"


def test_dc_minis_thumbnail_is_derived(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._plugin_thumbnail_variant(entry_link=_DC_LINK, lead_url=_DC_FULL) == _DC_THUMB


def test_a_lead_that_is_already_the_crop_is_kept(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._plugin_thumbnail_variant(entry_link=_DC_LINK, lead_url=_DC_THUMB) == _DC_THUMB


def test_dark_science_derives_nothing(tmp_path: Path):
    """ds_185/186/187 have no _thumbnail.jpg — deriving blind would 404 them all.

    thumbnail_from_lead_image is network-free by contract, so the plugin cannot
    check; it declines where it does not know.
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    for lead in ("https://dresdencodak.com/wp-content/uploads/2026/06/ds_187_silder.jpg",
                 "https://dresdencodak.com/wp-content/uploads/2026/06/ds_185.jpg"):
        assert service._plugin_thumbnail_variant(entry_link=_DC_LINK, lead_url=lead) is None


def test_another_hosts_dc_minis_name_is_not_claimed(tmp_path: Path):
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._plugin_thumbnail_variant(
        entry_link="https://example.test/x/", lead_url="https://example.test/img/dc_minis_9.jpg"
    ) is None


def test_the_site_crop_is_not_demoted_while_scoring(tmp_path: Path):
    """Demoting it is inert, so it is not done.

    Measured against the live site with the penalty patched in: DC Minis #26
    resolves to dc_minis_26_thumbnail.jpg either way, because the crop is the
    only image that page carries. Pinned so the nudge is not added back.
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    src = "https://dresdencodak.com/2026/08/09/dc-minis-27-birthday-blues/"
    assert service._plugin_source_score_adjustment(
        source_url=src, attrs={}, resolved_url=_DC_THUMB) == 0


def test_penny_arcade_panel_derivation_is_unaffected(tmp_path: Path):
    """Two plugins now answer thumbnail_from_lead_image; neither may shadow the other."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    assert service._plugin_thumbnail_variant(
        entry_link="https://www.penny-arcade.com/comic/2026/07/27/x",
        lead_url="https://assets.penny-arcade.com/comics/2026-x.jpg",
    ) == "https://assets.penny-arcade.com/comics/panels/2026-x-p1.jpg"


def test_script_end_tags_with_attributes_are_still_stripped(tmp_path: Path):
    """An HTML end tag may carry attributes; parsers ignore them, a regex cannot.

    With `</script\\s*>` the block did not match at all, so the script survived
    and its document.write('<img …>') was scanned anyway (CodeQL py/bad-tag-filter).
    """
    service = _build_service(tmp_path / "meta.sqlite", [])
    for page in ('<script>x<img src="/bad.png"></script foo>',
                 '<script>x<img src="/bad.png"></script\t\n bar>',
                 '<script>x<img src="/bad.png"></SCRIPT >'):
        assert "/bad.png" not in service._strip_script_blocks(page), page


def test_a_non_end_tag_is_not_eaten(tmp_path: Path):
    """`</scriptfoo>` is not a script end tag, so the strip must not run to it."""
    service = _build_service(tmp_path / "meta.sqlite", [])
    page = '<script>x</scriptfoo><img src="/real.png">'
    assert "/real.png" in service._strip_script_blocks(page)
