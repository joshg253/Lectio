"""Keeping the files a post links to — guitar-pro's .gp tabs and .pdf sheets.

Two rules shape the whole feature: page extensions are never attachments, and
there is no wildcard. The extension list IS the safeguard that keeps this a
capture of named file types rather than a crawl of every link on the page.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://blog.guitar-pro.com/feed/"
POST = "https://blog.guitar-pro.com/2018/10/free-tablatures/"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


# --- the extension list ------------------------------------------------------


def test_extensions_are_normalized():
    assert main.normalize_attachment_exts(".GP, gp5  pdf") == ["gp", "gp5", "pdf"]


@pytest.mark.parametrize("raw", ["html", "htm", "php", "aspx", "jsp", "cgi", "xhtml"])
def test_page_types_are_never_attachments(raw):
    """Following these would make it a crawler, not a capture."""
    assert main.normalize_attachment_exts(raw) == []


def test_page_types_are_dropped_not_fatal():
    """Typing "pdf html" means the pdf; failing the save over the html helps
    nobody."""
    assert main.normalize_attachment_exts("pdf html gp") == ["pdf", "gp"]


@pytest.mark.parametrize("raw", ["*", "*.*", ".*", "**"])
def test_there_is_no_wildcard(raw):
    assert main.normalize_attachment_exts(raw) == []


def test_junk_is_ignored():
    assert main.normalize_attachment_exts("!! .. -- 'x'") == []


def test_round_trip(configured):
    assert main.set_feed_attachment_exts(FEED, "gp gp5 pdf") == ["gp", "gp5", "pdf"]
    assert main.get_feed_attachment_exts(FEED) == ["gp", "gp5", "pdf"]


def test_off_by_default(configured):
    assert main.get_feed_attachment_exts(FEED) == []


def test_clearing(configured):
    main.set_feed_attachment_exts(FEED, "gp")
    assert main.set_feed_attachment_exts(FEED, "") == []
    assert main.get_feed_attachment_exts(FEED) == []


# --- finding the links -------------------------------------------------------


HTML = """
<a href="/wp-content/uploads/My_Life.gp">tab</a>
<a href="https://assets-wp.guitar-pro.eu/uploads/Lyrics.pdf">sheet</a>
<a href="https://blog.guitar-pro.com/category/lessons/">more lessons</a>
<a href="/index.php">home</a>
<a href="/post.php?download=song.gp">looks like a file</a>
<a href="mailto:hi@example.test">mail</a>
"""


def test_only_the_wanted_extensions_are_found():
    got = main.attachment_links_in_html(HTML, POST, ["gp", "pdf"])
    assert got == [
        "https://blog.guitar-pro.com/wp-content/uploads/My_Life.gp",
        "https://assets-wp.guitar-pro.eu/uploads/Lyrics.pdf",
    ]


def test_a_different_host_is_allowed():
    """The whole reason a same-host rule was rejected: guitar-pro serves tabs
    from assets-wp.guitar-pro.eu while the post lives on blog.guitar-pro.com."""
    got = main.attachment_links_in_html(HTML, POST, ["pdf"])
    assert got == ["https://assets-wp.guitar-pro.eu/uploads/Lyrics.pdf"]


def test_a_query_string_does_not_make_a_page_a_file():
    """/post.php?download=song.gp is a page. Matching on the PATH is what keeps
    this honest."""
    assert "post.php" not in " ".join(main.attachment_links_in_html(HTML, POST, ["gp"]))


def test_nothing_is_found_when_the_feature_is_off():
    assert main.attachment_links_in_html(HTML, POST, []) == []


def test_page_extensions_cannot_be_forced_through_the_finder():
    """Even if a stored value somehow contained one, the finder refuses it."""
    assert main.attachment_links_in_html(HTML, POST, ["php"]) == []


def test_duplicate_links_are_returned_once():
    html = '<a href="/a.pdf">1</a><a href="/a.pdf">2</a>'
    assert main.attachment_links_in_html(html, POST, ["pdf"]) == [
        "https://blog.guitar-pro.com/a.pdf"]


def test_relative_links_resolve_against_the_post():
    """POST ends in a slash, so it is a directory and a bare filename resolves
    inside it."""
    assert main.attachment_links_in_html('<a href="song.gp">x</a>', POST, ["gp"]) == [
        "https://blog.guitar-pro.com/2018/10/free-tablatures/song.gp"]


def test_root_relative_links_resolve_against_the_host():
    assert main.attachment_links_in_html('<a href="/files/song.gp">x</a>', POST, ["gp"]) == [
        "https://blog.guitar-pro.com/files/song.gp"]


# --- prefix patterns ---------------------------------------------------------


def test_a_prefix_pattern_is_kept_as_written():
    """Guitar Pro alone ships .gp/.gp3/.gp4/.gp5/.gpx — listing them by hand is
    tedious and misses the next one."""
    assert main.normalize_attachment_exts("gp*") == ["gp*"]


@pytest.mark.parametrize("ext,expected", [
    ("gp", True), ("gp3", True), ("gp5", True), ("gpx", True),
    ("gtp", False), ("png", False), ("pdf", False),
])
def test_prefix_matching(ext, expected):
    assert main._attachment_ext_matches(ext, ["gp*"]) is expected


def test_a_bare_wildcard_is_still_refused():
    assert main.normalize_attachment_exts("*") == []


def test_a_one_letter_prefix_is_refused():
    """"p*" would take pdf, png, ppt, psd… — a wildcard wearing a hat. The list
    is meant to name a FAMILY of file types."""
    assert main.normalize_attachment_exts("p*") == []
    assert main.normalize_attachment_exts("h*") == []


def test_a_prefix_can_never_reach_a_page_type():
    """Page types are refused at MATCH time as well as on save, so even a broad
    prefix cannot pull in an .html or a .php."""
    assert main._attachment_ext_matches("php", ["ph*"]) is False
    assert main._attachment_ext_matches("html", ["ht*"]) is False


def test_prefix_and_exact_patterns_mix():
    exts = main.normalize_attachment_exts("gp* gtp pdf")
    html = ('<a href="/a.gp5">1</a><a href="/b.gtp">2</a>'
            '<a href="/c.pdf">3</a><a href="/d.zip">4</a><a href="/e.php">5</a>')
    got = main.attachment_links_in_html(html, POST, exts)

    assert [u.split("/")[-1] for u in got] == ["a.gp5", "b.gtp", "c.pdf"]


# --- suggesting what the feed actually links to ------------------------------


def _seed_entry(entry_id: str, html: str) -> None:
    with main.get_reader() as reader:
        reader.add_entry({
            "feed_url": FEED, "id": entry_id, "title": entry_id,
            "link": POST, "content": [{"value": html, "type": "text/html"}],
        })


def test_scan_reports_what_the_feed_links(configured):
    _seed_entry("a", '<a href="/x/song.gp">t</a><a href="/x/sheet.pdf">p</a>')
    _seed_entry("b", '<a href="/y/other.gp">t</a>')
    _seed_entry("c", '<a href="/z/third.gp">t</a><a href="/z/b.pdf">p</a>')

    got = {r["ext"]: r["count"] for r in main.scan_feed_attachment_extensions(FEED)}

    assert got == {"gp": 3, "pdf": 2}


def test_images_are_not_suggested(configured):
    """The archive captures those anyway — offering them is noise."""
    for i in range(3):
        _seed_entry(f"i{i}", '<a href="/a.png">x</a><a href="/b.jpg">y</a>')

    assert main.scan_feed_attachment_extensions(FEED) == []


def test_page_types_are_not_suggested(configured):
    for i in range(3):
        _seed_entry(f"p{i}", '<a href="/a.html">x</a><a href="/b.php">y</a>')

    assert main.scan_feed_attachment_extensions(FEED) == []


def test_bare_domain_links_do_not_look_like_extensions(configured):
    """"https://example.com" leaves "com" looking like a file extension."""
    for i in range(3):
        _seed_entry(f"d{i}", '<a href="https://example.com">x</a>')

    assert main.scan_feed_attachment_extensions(FEED) == []


def test_one_off_path_fragments_are_dropped(configured):
    """A path that merely looks like an extension ("…/bulakhov") appears once;
    anything the feed genuinely publishes recurs."""
    _seed_entry("one", '<a href="/artists/bulakhov">x</a><a href="/a.gp">t</a>')
    _seed_entry("two", '<a href="/b.gp">t</a>')

    got = [r["ext"] for r in main.scan_feed_attachment_extensions(FEED)]

    assert got == ["gp"]


def test_most_common_first(configured):
    _seed_entry("m1", '<a href="/a.pdf">1</a>')
    _seed_entry("m2", '<a href="/b.pdf">2</a>')
    for i in range(4):
        _seed_entry(f"g{i}", '<a href="/c.gp">t</a>')

    assert [r["ext"] for r in main.scan_feed_attachment_extensions(FEED)] == ["gp", "pdf"]


def test_a_feed_with_no_file_links_suggests_nothing(configured):
    _seed_entry("plain", "<p>just words</p>")
    assert main.scan_feed_attachment_extensions(FEED) == []


# --- the content-type filter -------------------------------------------------


def test_only_real_files_are_returned_as_attachments(configured):
    """Decided by STORED content type. A Gravatar and a CDN path with no
    extension are both images with nothing in the URL to say so, and they
    surfaced as "webp attachments"; a share button was stored as 1.1MB of HTML
    and surfaced as an "attachment" that downloaded a .htm."""
    main.ensure_starred_archive_schema()
    rows = [
        ("https://x.test/song.gp", "H1", "application/octet-stream"),
        ("https://x.test/sheet.pdf", "H2", "application/pdf"),
        ("https://gravatar.com/avatar/abc?s=48", "H3", "image/webp"),
        ("https://cdn.test/flexiimages/xyz", "H4", "image/svg+xml"),
        ("https://pinterest.com/pin/create/button/?media=x.jpg", "H5", "text/html"),
        ("https://x.test/ep", "H6", "audio/mpeg"),
    ]
    with main.get_starred_archive_connection() as conn:
        for url, h, ctype in rows:
            conn.execute(
                "INSERT OR REPLACE INTO archived_asset"
                " (asset_hash, data, content_type, byte_size, created_at)"
                " VALUES (?, ?, ?, 1, 0)", (h, b"x", ctype))
            conn.execute(
                "INSERT OR REPLACE INTO archived_asset_link"
                " (feed_url, entry_id, source_url, asset_hash) VALUES (?, ?, ?, ?)",
                (FEED, "e1", url, h))

    got = main.starred_archive_service.get_entry_file_assets(FEED, "e1")

    assert set(got) == {"https://x.test/song.gp", "https://x.test/sheet.pdf"}


# --- links the page hides in a base64 attribute ------------------------------


OBF = ("<span class='obflink' data-o='aHR0cHM6Ly9hc3NldHMtd3AuZ3VpdGFyLXByby5ldS93cC1jb250"
       "ZW50L3VwbG9hZHMvMjAyNi8wNi9Ccnlhbl9BZGFtcy1TdW1tZXJfb2ZfNjkuZ3A='>"
       "bryan_adams-summer_of_69.gp</span>")


def test_a_base64_hidden_link_is_found():
    """guitar-pro ships <span class="obflink" data-o="<base64>"> for its tab
    downloads, so the file the page offers every visitor is reachable by the
    browser but invisible to an href scan."""
    got = main.attachment_links_in_html(OBF, POST, ["gp*"])
    assert got == ["https://assets-wp.guitar-pro.eu/wp-content/uploads/2026/06/"
                   "Bryan_Adams-Summer_of_69.gp"]


def test_a_hidden_link_still_has_to_match_the_extension_list():
    """Widens where links are FOUND, not what counts as a file."""
    assert main.attachment_links_in_html(OBF, POST, ["pdf"]) == []


def test_a_hidden_page_link_is_still_refused():
    # base64 of "https://x.test/page.html"
    html = "<span data-o='aHR0cHM6Ly94LnRlc3QvcGFnZS5odG1s'>x</span>"
    assert main.attachment_links_in_html(html, POST, ["html", "gp"]) == []


def test_non_base64_attributes_are_ignored():
    html = "<span data-o='not-base64-at-all!!'>x</span>"
    assert main.attachment_links_in_html(html, POST, ["gp"]) == []


def test_base64_that_is_not_a_url_is_ignored():
    # base64 of "just some text"
    html = "<span data-o='anVzdCBzb21lIHRleHQ='>x</span>"
    assert main.attachment_links_in_html(html, POST, ["gp"]) == []


def test_plain_links_and_hidden_links_both_appear():
    html = OBF + '<a href="/plain.pdf">p</a>'
    got = main.attachment_links_in_html(html, POST, ["gp*", "pdf"])
    assert len(got) == 2
    assert any(u.endswith(".pdf") for u in got)
    assert any(u.endswith(".gp") for u in got)
