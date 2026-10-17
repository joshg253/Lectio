"""Unit tests for services.scraper_service link-list anchor resolution."""

from __future__ import annotations

import sqlite3

import pytest
from bs4 import BeautifulSoup

from services import scraper_service


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE scraped_feeds (id TEXT PRIMARY KEY, source_url TEXT, mode TEXT,"
        " selector TEXT, feed_title TEXT, created_at TEXT, last_scraped_at TEXT,"
        " last_content_hash TEXT, content_selector TEXT)"
    )
    conn.execute(
        "CREATE TABLE scraped_entries (id TEXT PRIMARY KEY, scraped_feed_id TEXT,"
        " entry_url TEXT, title TEXT, content TEXT, published_at TEXT,"
        " hidden INTEGER NOT NULL DEFAULT 0, UNIQUE(scraped_feed_id, entry_url))"
    )
    return conn


_PAGE = """
<html><body><div id='home'><div class='wrapper'><div class='content-wrapper'>
  <div>other</div><div>other</div>
  <div><ul>
    <li><a href='transcriptions.php?i=1192'>Diana Ross - Give up</a></li>
    <li><a href='transcriptions.php?i=1193'>Another Song</a></li>
  </ul></div>
</div></div></div></body></html>
"""


def test_link_list_resolves_anchor_inside_container_selector(monkeypatch):
    # A selector that targets the <li> container (not the <a>) must still resolve
    # to the anchor inside it.
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    conn = _make_conn()
    feed = {
        "id": "f1",
        "source_url": "https://basslessons.be/",
        "selector": "#home > div.wrapper > div.content-wrapper > div:nth-child(3) > ul > li:nth-child(1)",
    }
    added = scraper_service._scrape_link_list(conn, feed, initial=False)
    rows = conn.execute("SELECT entry_url, title FROM scraped_entries").fetchall()
    assert added is True
    assert len(rows) == 1
    assert rows[0]["entry_url"] == "https://basslessons.be/transcriptions.php?i=1192"
    assert rows[0]["title"] == "Diana Ross - Give up"


def test_link_list_resolves_all_anchors_for_list_selector(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    conn = _make_conn()
    feed = {
        "id": "f2",
        "source_url": "https://basslessons.be/",
        "selector": "#home > div.wrapper > div.content-wrapper > div:nth-child(3) > ul > li",
    }
    scraper_service._scrape_link_list(conn, feed, initial=False)
    urls = {r["entry_url"] for r in conn.execute("SELECT entry_url FROM scraped_entries").fetchall()}
    assert urls == {
        "https://basslessons.be/transcriptions.php?i=1192",
        "https://basslessons.be/transcriptions.php?i=1193",
    }


def test_link_list_direct_anchor_selector_still_works(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    conn = _make_conn()
    feed = {"id": "f3", "source_url": "https://basslessons.be/", "selector": "ul li a"}
    scraper_service._scrape_link_list(conn, feed, initial=False)
    count = conn.execute("SELECT COUNT(*) FROM scraped_entries").fetchone()[0]
    assert count == 2


_ENTRY_PAGE = (
    '<html><body><nav>site nav junk, ads, related-posts widget</nav><article class="post-body"><p>Real content.</p></article></body></html>'
)


def test_link_list_content_selector_fills_new_entry_body(monkeypatch):
    """A per-feed content_selector (link_list only) is applied to each NEW
    entry's OWN page -- not the listing page -- to fill its body directly,
    bypassing readability/full-page guessing on sites where that grabs the
    whole chromed page instead of the article (texasbluesalley.com, confirmed
    live 2026-09-19)."""

    def fake_fetch(url):
        return _PAGE if url == "https://basslessons.be/" else _ENTRY_PAGE

    monkeypatch.setattr(scraper_service, "_fetch_html", fake_fetch)
    conn = _make_conn()
    feed = {
        "id": "f4",
        "source_url": "https://basslessons.be/",
        "selector": "ul li a",
        "content_selector": "article.post-body",
    }
    scraper_service._scrape_link_list(conn, feed, initial=False)
    rows = conn.execute("SELECT content FROM scraped_entries").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["content"] == '<article class="post-body"><p>Real content.</p></article>'


def test_link_list_no_content_selector_leaves_body_empty(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE if url == "https://basslessons.be/" else _ENTRY_PAGE)
    conn = _make_conn()
    feed = {"id": "f5", "source_url": "https://basslessons.be/", "selector": "ul li a"}
    scraper_service._scrape_link_list(conn, feed, initial=False)
    rows = conn.execute("SELECT content FROM scraped_entries").fetchall()
    assert all(row["content"] == "" for row in rows)


def test_new_entry_extras_handles_fetch_failure_and_bad_selector(monkeypatch):
    def raise_fetch(url):
        raise RuntimeError("boom")

    monkeypatch.setattr(scraper_service, "_fetch_html", raise_fetch)
    assert scraper_service._new_entry_extras("https://x.test/", "article") == (None, "")

    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _ENTRY_PAGE)
    # An invalid selector must not blow up the whole scrape -- just no body.
    published, content = scraper_service._new_entry_extras("https://x.test/", ":::not-a-selector")
    assert content == ""


def test_extract_link_items_dedups_and_resolves_absolute():
    items = scraper_service.extract_link_items(_PAGE, "https://basslessons.be/", "ul > li")
    assert [i["url"] for i in items] == [
        "https://basslessons.be/transcriptions.php?i=1192",
        "https://basslessons.be/transcriptions.php?i=1193",
    ]
    assert items[0]["title"] == "Diana Ross - Give up"


# texasbluesalley.com's video cards wrap the SAME href in two anchors sharing one
# class: a thumbnail-image link (only a hidden duration badge for text, still read
# by get_text() despite display:none) preceding a caption link with the real title.
_DUAL_ANCHOR_PAGE = """
<html><body><div class="thumbnail-item-wrapper">
  <a class="thumbnail-item-link" href="/lessons/open-strings">
    <div class="thumbnail-item-image"><img alt="thumb"/></div>
    <div class="thumbnail-item-duration" style="display: none">16:36</div>
  </a>
  <div class="thumbnail-item-caption">
    <div class="thumbnail-item-title">
      <a class="thumbnail-item-link" href="/lessons/open-strings">Open Strings Unlock Little Wing's Potential</a>
    </div>
  </div>
</div></body></html>
"""


def test_extract_link_items_prefers_real_title_over_duplicate_decoration_anchor():
    items = scraper_service.extract_link_items(_DUAL_ANCHOR_PAGE, "https://texasbluesalley.com/", "a.thumbnail-item-link")
    assert len(items) == 1
    assert items[0]["title"] == "Open Strings Unlock Little Wing's Potential"


def test_link_list_scrape_prefers_real_title_and_heals_duration_titles(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _DUAL_ANCHOR_PAGE if url.endswith("/") else "")
    conn = _make_conn()
    feed = {"id": "f1", "source_url": "https://texasbluesalley.com/", "selector": "a.thumbnail-item-link"}
    scraper_service._scrape_link_list(conn, feed, initial=False)
    row = conn.execute("SELECT title FROM scraped_entries").fetchone()
    assert row["title"] == "Open Strings Unlock Little Wing's Potential"
    conn.execute("UPDATE scraped_entries SET title = '15:40'")
    scraper_service._scrape_link_list(conn, feed, initial=False)
    assert conn.execute("SELECT title FROM scraped_entries").fetchone()["title"] == "Open Strings Unlock Little Wing's Potential"


# The same card with no hidden badge: the thumbnail anchor has NO text, so its title used to fall back
# to the URL, which (longer, has letters) outranked the real caption title.
_IMAGE_ONLY_ANCHOR_PAGE = """
<html><body><div class="thumbnail-item-wrapper">
  <a class="thumbnail-item-link" href="/lessons/raking" title="Essential Techniques: Raking">
    <div class="thumbnail-item-image"><img alt="Lessons - Raking"/></div>
  </a>
  <div class="thumbnail-item-title"><a class="thumbnail-item-link" href="/lessons/raking">Essential Techniques: Raking</a></div>
</div>
<div class="thumbnail-item-wrapper"><a class="thumbnail-item-link" href="/lessons/muting"><img alt="Muting"/></a></div>
<div class="thumbnail-item-wrapper"><a class="thumbnail-item-link" href="/lessons/bare"></a></div>
</body></html>
"""


def test_image_only_anchor_title_uses_title_attribute_then_alt_then_url():
    items = scraper_service.extract_link_items(_IMAGE_ONLY_ANCHOR_PAGE, "https://example.com/", "a.thumbnail-item-link")
    assert [i["title"] for i in items] == ["Essential Techniques: Raking", "Muting", "https://example.com/lessons/bare"]


def test_link_list_scrape_heals_url_titles(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _IMAGE_ONLY_ANCHOR_PAGE if url.endswith("/") else "")
    conn = _make_conn()
    feed = {"id": "f1", "source_url": "https://example.com/", "selector": "a.thumbnail-item-link"}
    scraper_service._scrape_link_list(conn, feed, initial=False)
    conn.execute("UPDATE scraped_entries SET title = entry_url")
    scraper_service._scrape_link_list(conn, feed, initial=False)
    titles = {r["entry_url"]: r["title"] for r in conn.execute("SELECT entry_url, title FROM scraped_entries")}
    assert titles["https://example.com/lessons/raking"] == "Essential Techniques: Raking"
    assert titles["https://example.com/lessons/bare"] == "https://example.com/lessons/bare"


_RANK_PAGE = """
<html><body>
  <nav><div><div><a href='/login'>Login</a></div><div><a href='/a'>A</a></div>
    <div><a href='/b'>B</a></div><div><a href='/c'>C</a></div><div><a href='/d'>D</a></div></div></nav>
  <ul><li><a class='post' href='/p1'>Post 1</a></li>
      <li><a class='post' href='/p2'>Post 2</a></li>
      <li><a class='post' href='/p3'>Post 3</a></li></ul>
</body></html>
"""


def test_suggest_selectors_ranks_content_over_nav_chrome():
    suggestions = scraper_service.suggest_selectors(_RANK_PAGE, "https://x/")
    assert suggestions, "expected at least one suggestion"
    for s in suggestions:
        assert s["count"] >= 2
        assert scraper_service.extract_link_items(_RANK_PAGE, "https://x/", s["selector"])
    # The class-based content list must outrank the more-numerous generic
    # "div > div a" nav chrome, even though the latter matches more links.
    assert "." in suggestions[0]["selector"]


def test_selector_for_clicked_link_reuses_candidate_logic():
    # Clicking a '.post' link derives the same class-based selector the chips do.
    res = scraper_service.selector_for_clicked_link(_RANK_PAGE, "https://x/", "https://x/p2")
    assert res is not None
    assert res["selector"] == "a.post"
    assert res["count"] == 3


def test_selector_for_clicked_link_no_match_returns_none():
    assert scraper_service.selector_for_clicked_link(_RANK_PAGE, "https://x/", "https://x/nope") is None


@pytest.mark.parametrize("href", ["", "   ", "\t\n"])
def test_selector_for_clicked_link_blank_href_returns_none(href):
    # A blank href normalizes to the source URL (no anchor), so no selector.
    assert scraper_service.selector_for_clicked_link(_RANK_PAGE, "https://x/", href) is None


def test_pick_page_feed_selector_fetches(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _RANK_PAGE)
    res = scraper_service.pick_page_feed_selector("https://x/", "https://x/p1")
    assert res == {"selector": "a.post", "count": 3}


def test_preview_page_feed_link_list(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    out = scraper_service.preview_page_feed("https://basslessons.be/", "link_list", "ul > li")
    assert out["mode"] == "link_list"
    assert len(out["items"]) == 2
    assert out["suggestions"]


def test_preview_page_feed_change_detect(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    out = scraper_service.preview_page_feed("https://basslessons.be/", "change_detect", "ul")
    assert out["mode"] == "change_detect"
    assert "Diana Ross" in out["content_preview"]


def test_absolutize_links_resolves_relative_href_and_src_only():
    from bs4 import BeautifulSoup

    region = BeautifulSoup(
        '<div><a href="/woodshed/tag/Hendrix">H</a><img src="img/a.png"><a href="#top">t</a><a href="https://x.test/y">y</a></div>',
        "html.parser",
    ).div
    scraper_service.absolutize_links(region, "https://texasbluesalley.com/woodshed/free-lessons/lesson")
    out = str(region)
    assert 'href="https://texasbluesalley.com/woodshed/tag/Hendrix"' in out
    assert 'src="https://texasbluesalley.com/woodshed/free-lessons/img/a.png"' in out
    assert 'href="#top"' in out and 'href="https://x.test/y"' in out


def test_link_list_scrape_heals_relative_links_in_stored_bodies(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE if url == "https://basslessons.be/" else "")
    conn = _make_conn()
    feed = {"id": "f1", "source_url": "https://basslessons.be/", "selector": "a"}
    scraper_service._scrape_link_list(conn, feed, initial=False)
    conn.execute("UPDATE scraped_entries SET content = '<div><a href=\"/tag/x\">x</a></div>'")
    scraper_service._scrape_link_list(conn, feed, initial=False)
    body = conn.execute("SELECT content FROM scraped_entries LIMIT 1").fetchone()["content"]
    assert 'href="https://basslessons.be/tag/x"' in body


def test_link_list_accepts_complex_has_selector(monkeypatch):
    # soupsieve 3 rejects a complex :has() unless NOSTRICT is passed; a user-typed selector like this
    # was accepted before the upgrade and must keep resolving rather than raising.
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    conn = _make_conn()
    feed = {"id": "f2", "source_url": "https://basslessons.be/", "selector": "ul:has(li a) li:nth-child(2) a"}
    assert scraper_service._scrape_link_list(conn, feed, initial=False) is True
    rows = conn.execute("SELECT entry_url FROM scraped_entries").fetchall()
    assert [r["entry_url"] for r in rows] == ["https://basslessons.be/transcriptions.php?i=1193"]


_UNWRAPPED_POST = """
<div id="c"><div class="tags">Tags</div><h1>Title</h1><div class="meta">Oct 1</div><br/>
First paragraph.<br/><br/>Second with <a href="/x">a link</a>.<div class="share">share</div><div class="comments">c1</div></div>
"""


def test_select_region_slices_siblings_between_start_and_end():
    soup = BeautifulSoup(_UNWRAPPED_POST, "html.parser")
    region = scraper_service.select_region(soup, ".meta >> .share")
    text = region.get_text(" ", strip=True)
    assert "First paragraph." in text and "a link" in text
    assert "share" not in text and "c1" not in text and "Title" not in text


def test_select_region_plain_selector_and_misses():
    soup = BeautifulSoup(_UNWRAPPED_POST, "html.parser")
    assert scraper_service.select_region(soup, "h1").get_text() == "Title"
    assert scraper_service.select_region(soup, ".nope >> .share") is None
    assert scraper_service.select_region(BeautifulSoup(_UNWRAPPED_POST, "html.parser"), ".share >> .nope").get_text(strip=True) == "c1"


def test_new_entry_extras_uses_range_selector(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _UNWRAPPED_POST)
    _, content = scraper_service._new_entry_extras("https://example.com/p", ".meta >> .share")
    assert "First paragraph." in content and "c1" not in content


def test_select_region_range_shows_linked_fullsize_image_inline():
    html = (
        '<h1>T</h1><div class="meta">d</div><a href="p/big.png" target="_blank"><img class="itemleft" src="p/s.png"></a>'
        'text<div class="end"></div>'
    )
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .end")
    img = region.find_all("img")[-1]
    assert img["src"] == "p/big.png" and region.find("a") is None and not img.get("class")


def test_select_region_range_tiles_a_strip_of_linked_images():
    strip = "".join(f'<a href="p/{n}.png"><img src="p/{n}_s.jpg"></a>' for n in range(4))
    html = f'<div class="meta">d</div><center>{strip}</center><div class="end"></div>'
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .end")
    imgs = region.find_all("img")
    assert [i["src"] for i in imgs] == [f"p/{n}.png" for n in range(4)]
    assert {i["width"] for i in imgs} == {"320"}
    assert imgs[0].parent.name == "div" and len({id(i.parent) for i in imgs}) == 1


def test_generated_feed_escapes_ampersands_in_links():
    import xml.dom.minidom

    xml_text = scraper_service._generate_rss_xml(
        "T", "https://e.com/?a=1&b=2", [{"id": "1", "title": "t", "entry_url": "https://e.com/?x=1&y=2", "content": "", "published_at": ""}]
    )
    xml.dom.minidom.parseString(xml_text.encode())
    assert "x=1&amp;y=2" in xml_text


def test_select_region_does_not_tile_images_scattered_through_a_post():
    html = (
        '<div class="meta">d</div><a href="p/a.png"><img src="p/a_s.png"></a>text<br/>more text'
        '<a href="p/b.png"><img src="p/b_s.png"></a> and words<div class="end"></div>'
    )
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .end")
    assert all(i.get("width") is None for i in region.find_all("img"))


def test_select_region_carries_small_leading_image_as_thumb_attribute():
    html = '<div class="meta">d</div><br/><a href="p/big.png"><img class="itemleft" src="p/s.png"></a>Body<div class="end"></div>'
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .end")
    img = region.find("img")
    assert region.find_all("img") == [img] and img["src"] == "p/big.png" and img["data-lectio-thumb"] == "p/s.png"


def test_select_region_end_marker_ignores_an_earlier_match_elsewhere():
    html = '<aside><div class="share">x</div></aside><div><div class="meta"></div>Body<div class="share"></div><div>SPAM</div></div>'
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .share")
    text = region.get_text(" ", strip=True)
    assert text == "Body"


def test_select_region_end_marker_can_be_nested_in_a_sibling():
    html = '<div><div class="meta"></div>Body<section><div class="share">s</div></section><p>SPAM</p></div>'
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .share")
    assert region.get_text(" ", strip=True) == "Body"


def test_select_region_scattered_images_never_sit_next_to_each_other_under_the_wrapper():
    html = (
        '<div class="meta"></div><a href="a.png"><img src="a_s.png"></a>words<a href="b.png"><img src="b_s.png"></a>'
        'more<a href="c.png"><img src="c_s.png"></a><div class="end"></div>'
    )
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .end")
    assert region.find("img", recursive=False) is None
    assert [i.parent.name for i in region.find_all("img")] == ["span"] * 3


def test_select_region_thumb_attribute_is_made_absolute_by_absolutize_links():
    html = '<div class="meta"></div><a href="p/b.png"><img src="p/s.png"></a>t<div class="end"></div>'
    region = scraper_service.select_region(BeautifulSoup(html, "html.parser"), ".meta >> .end")
    scraper_service.absolutize_links(region, "https://e.com/index.php")
    img = region.find("img")
    assert img["src"] == "https://e.com/p/b.png" and img["data-lectio-thumb"] == "https://e.com/p/s.png"
