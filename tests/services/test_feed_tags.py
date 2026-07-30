"""FeedTagService: persistence of feed-provided entry tags (entry_feed_tags),
plus the parser-side capture sink in reader_sanitize."""
from __future__ import annotations

import io
import sqlite3
from contextlib import contextmanager

import pytest

from services import reader_sanitize
from services.feed_tags import FeedTagService, extract_feed_entry_tags
from services.reader_sanitize import SanitizingFeedparserParser

FEED = "https://x.test/feed"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        """
        CREATE TABLE entry_feed_tags (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            PRIMARY KEY (feed_url, entry_id, tag)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE suppressed_feed_tags (
            feed_url TEXT NOT NULL,
            tag TEXT NOT NULL,
            suppressed_at REAL NOT NULL,
            PRIMARY KEY (feed_url, tag)
        )
        """
    )
    yield c
    c.close()


@pytest.fixture
def service(conn):
    @contextmanager
    def get_meta_connection():
        yield conn

    return FeedTagService(get_meta_connection=get_meta_connection)


def _rows(conn, entry_id="e1"):
    return [
        r[0]
        for r in conn.execute(
            "SELECT tag FROM entry_feed_tags WHERE feed_url=? AND entry_id=? ORDER BY tag",
            (FEED, entry_id),
        )
    ]


def test_record_and_get_preserves_feed_order(service):
    service.record_entry_tags(FEED, [("e1", ["Python", "AI"]), ("e2", ["Rust"])])
    assert service.get_tags_for_entry(FEED, "e1") == ["Python", "AI"]
    assert service.get_tags_for_entry(FEED, "e2") == ["Rust"]
    assert service.get_tags_for_entry(FEED, "missing") == []


def test_replace_on_reseen_entry(service, conn):
    service.record_entry_tags(FEED, [("e1", ["old-tag", "kept"])])
    service.record_entry_tags(FEED, [("e1", ["kept", "new-tag"])])
    assert _rows(conn) == ["kept", "new-tag"]


def test_absent_entries_keep_rows(service, conn):
    service.record_entry_tags(FEED, [("e1", ["a"]), ("e2", ["b"])])
    service.record_entry_tags(FEED, [("e2", ["b2"])])
    assert _rows(conn, "e1") == ["a"]
    assert _rows(conn, "e2") == ["b2"]


def test_case_duplicate_tags_do_not_raise(service, conn):
    service.record_entry_tags(FEED, [("e1", ["Python", "Python"])])
    assert _rows(conn) == ["Python"]


def test_empty_pairs_and_empty_tags_noop(service, conn):
    service.record_entry_tags(FEED, [])
    service.record_entry_tags(FEED, [("e1", []), ("", ["x"])])
    assert conn.execute("SELECT COUNT(*) FROM entry_feed_tags").fetchone()[0] == 0


def test_delete_for_feed(service, conn):
    service.record_entry_tags(FEED, [("e1", ["a"])])
    service.record_entry_tags("https://other.test/feed", [("e1", ["b"])])
    assert service.delete_for_feed(FEED) == 1
    assert _rows(conn) == []
    assert service.get_tags_for_entry("https://other.test/feed", "e1") == ["b"]


def test_migrate_feed_url_merges_collisions(service):
    new = "https://new.test/feed"
    service.record_entry_tags(FEED, [("e1", ["a", "b"])])
    service.record_entry_tags(new, [("e1", ["a"])])
    service.migrate_feed_url(FEED, new)
    # Order after a merge is best-effort; content is what matters.
    assert sorted(service.get_tags_for_entry(new, "e1")) == ["a", "b"]
    assert service.get_tags_for_entry(FEED, "e1") == []


# --- extract_feed_entry_tags ---


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_extract_terms_labels_category_dedup():
    raw = _Obj(
        tags=[{"term": "Python"}, {"term": None, "label": "AI"}, {"term": "python"}],
        category="Web Dev",
    )
    assert extract_feed_entry_tags(raw) == ["Python", "AI", "Web Dev"]


def test_extract_whitespace_compaction():
    raw = _Obj(tags=[{"term": "  machine \n learning "}], category=None)
    assert extract_feed_entry_tags(raw) == ["machine learning"]


# --- parser capture sink ---


def _feed_xml(items: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0">'
        f"<channel><title>T</title>{items}</channel></rss>"
    ).encode()


@pytest.fixture
def sink():
    captured = []
    reader_sanitize.set_entry_tag_sink(
        lambda url, pairs: captured.append((url, pairs))
    )
    yield captured
    reader_sanitize.set_entry_tag_sink(None)


def test_parser_captures_category_tags(sink):
    raw = _feed_xml(
        "<item><guid>e1</guid><link>https://x.test/1</link>"
        "<category>Python</category><category>AI</category></item>"
        "<item><guid>e2</guid><link>https://x.test/2</link></item>"
    )
    SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == [(FEED, [("e1", ["Python", "AI"])])]


def test_parser_guidless_rss_maps_by_link(sink):
    raw = _feed_xml(
        "<item><link>https://x.test/1</link><category>tagged</category></item>"
    )
    SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == [(FEED, [("https://x.test/1", ["tagged"])])]


def test_parser_skipped_entry_produces_no_pair(sink):
    # An item with neither guid nor link is rejected by reader's _process_entry
    # (skipped with a warning); its tags must not reach the sink even though
    # the raw feedparser result still contains the entry.
    import warnings

    raw = _feed_xml(
        "<item><title>no id</title><category>Orphan</category></item>"
        "<item><guid>e1</guid><link>https://x.test/1</link>"
        "<category>Kept</category></item>"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == [(FEED, [("e1", ["Kept"])])]


def test_parser_no_tags_no_sink_call(sink):
    raw = _feed_xml("<item><guid>e1</guid><link>https://x.test/1</link></item>")
    SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == []


def test_raising_sink_does_not_break_parse():
    def boom(url, pairs):
        raise RuntimeError("sink failure")

    reader_sanitize.set_entry_tag_sink(boom)
    try:
        raw = _feed_xml(
            "<item><guid>e1</guid><link>https://x.test/1</link>"
            "<category>Python</category></item>"
        )
        _feed, entries = SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
        assert len(entries) == 1
    finally:
        reader_sanitize.set_entry_tag_sink(None)


def test_no_sink_registered_is_fine():
    reader_sanitize.set_entry_tag_sink(None)
    raw = _feed_xml(
        "<item><guid>e1</guid><link>https://x.test/1</link>"
        "<category>Python</category></item>"
    )
    _feed, entries = SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert len(entries) == 1


# --- extract_page_tags (source-page fallback) ---

from services.feed_tags import extract_page_tags  # noqa: E402


def test_page_tags_article_tag_metas():
    html = '''<html><head>
      <meta property="article:tag" content="Windows 11">
      <meta property="article:tag" content="Backup">
      <meta name="article:tag" content="Storage">
    </head><body></body></html>'''
    assert extract_page_tags(html) == ["Windows 11", "Backup", "Storage"]


def test_page_tags_keywords_split_and_dedupe():
    html = '''<meta name="keywords" content="python, AI,  python , machine learning">'''
    assert extract_page_tags(html) == ["python", "AI", "machine learning"]


def test_page_tags_single_quotes_and_parsely():
    html = "<meta name='parsely-tags' content='linux,gaming'>"
    assert extract_page_tags(html) == ["linux", "gaming"]


def test_page_tags_ignores_other_metas_and_junk():
    html = '''<meta property="og:title" content="Not a tag">
      <meta name="description" content="prose, with, commas">
      <meta property="article:tag" content="">
      <meta property="article:tag" content="''' + ("x" * 80) + '''">'''
    assert extract_page_tags(html) == []


def test_page_tags_empty_input_and_cap():
    assert extract_page_tags(None) == []
    assert extract_page_tags("") == []
    many = "".join(f'<meta property="article:tag" content="t{i}">' for i in range(40))
    assert len(extract_page_tags(many)) == 15


def test_page_tags_rel_tag_anchors():
    html = '<a href="/tag/linux/" rel="tag">Linux</a> <a rel="nofollow tag" href="/x">Self-Hosting</a>'
    assert extract_page_tags(html) == ["Linux", "Self-Hosting"]


def test_page_tags_tag_classed_anchors_title_or_slug():
    """Valnet style: tags-link anchors, some wrapping an image or a junk span, so
    the title attribute wins and the slug beats one-character text.

    ⚠ This deliberately overturns the old assertion that a plain /tag/ link
    *without* a tag class is nav noise to be ignored. Hugo (and most static
    generators) mark taxonomy links only by URL shape — no class, no rel="tag" —
    so requiring a class found nothing at all on those sites, which is how
    krshrimali.github.io's category and tags were both missed. A link to
    /tag/<slug>/ on an article page is overwhelmingly that article's tag; the junk
    filter and the 15-tag cap bound the cost of the occasional sidebar link.
    """
    html = (
        '<a class="tags-link image" href="/category/windows/" title="Windows"><img src="x"></a>'
        '<a class="tags-link" href="/tag/windows-tips/"><span>x</span></a>'
        '<a href="/tag/unrelated-nav-link/">Nav</a>'
    )
    assert extract_page_tags(html) == ["Windows", "windows tips", "Nav"]


def test_junk_tags_dropped_at_capture():
    raw = _Obj(tags=[{"term": "Uncategorized"}, {"term": "Wildfire"}], category=None)
    assert extract_feed_entry_tags(raw) == ["Wildfire"]
    html = '<meta name="keywords" content="uncategorized, General, linux">'
    assert extract_page_tags(html) == ["linux"]


class _Tag:
    def __init__(self, term): self.term, self.label, self.scheme = term, None, None


class _Entry:
    def __init__(self, tags=None, category=None):
        if tags is not None:
            self.tags = tags
        if category is not None:
            self.category = category


def test_html_entities_in_tags_are_decoded():
    """Feeds ship C&#43;&#43; for C++, &amp; for &, &#xF1; for ñ. Undecoded,
    each is a distinct tag from its readable twin and matches nothing a user
    would ever type. 99 such rows were live when this was found."""
    out = extract_feed_entry_tags(_Entry(tags=[
        _Tag("C&#43;&#43;"), _Tag("bricks &amp; minifigs"), _Tag("Bu&#xF1;uel"),
    ]))
    assert out == ["C++", "bricks & minifigs", "Buñuel"]


def test_semicolon_packed_category_splits_into_separate_tags():
    """andrewlock.net emits one <category> holding every tag, so the post
    showed a single unusable 'ASP.NET Core;Security;CSRF;CORS'."""
    out = extract_feed_entry_tags(_Entry(tags=[_Tag("ASP.NET Core;Security;CSRF;CORS")]))
    assert out == ["ASP.NET Core", "Security", "CSRF", "CORS"]


def test_entities_are_decoded_before_splitting():
    """The ordering is the whole subtlety, and getting it backwards is worse
    than not splitting at all.

    Of 124 live tags containing a ';', only 25 still did once decoded — the
    other 99 were entity terminators. Splitting the raw text turns C&#43;&#43;
    into ['C&', '43', '&', '43', ''] instead of 'C++'.
    """
    assert extract_feed_entry_tags(_Entry(tags=[_Tag("C&#43;&#43;")])) == ["C++"]
    # A tag that genuinely packs values AND carries an entity gets both right.
    out = extract_feed_entry_tags(_Entry(tags=[_Tag("C&#43;&#43;;Security")]))
    assert out == ["C++", "Security"]


def test_commas_are_not_split():
    """Deliberate: the comma-bearing tags on live data are one junk forum value
    ('9,41,44,… Forum'), which splitting would explode into 19 numeric tags."""
    out = extract_feed_entry_tags(_Entry(tags=[_Tag("Machine Learning, AI")]))
    assert out == ["Machine Learning, AI"]


def test_category_attribute_is_split_too():
    out = extract_feed_entry_tags(_Entry(category="Security;CORS"))
    assert out == ["Security", "CORS"]


# --- page tag extraction: taxonomy URLs ---
def test_taxonomy_url_anchors_are_tags_whatever_their_class():
    """A link to /tags/<slug>/ IS a tag link. Hugo marks them only by URL shape —
    krshrimali.github.io puts its category at the top and its tags in the footer
    with no tag class and no rel="tag", so the class/rel tiers found nothing.

    Plural matters: the old slug fallback matched /tag/ and /category/ only, which
    misses Hugo's /tags/ and /categories/ entirely.
    """
    out = extract_page_tags(
        '<a href="https://x.test/categories/personal/">personal</a>'
        '<a href="https://x.test/tags/motivation/">#motivation</a>'
    )
    assert out == ["personal", "motivation"]


def test_unquoted_attributes_are_parsed():
    """Minified Hugo output emits `href=https://…` with no quotes, so a
    quotes-only attribute pattern matched nothing and every anchor tier silently
    found zero tags on those pages."""
    out = extract_page_tags("<a href=https://x.test/tags/rust/>rust</a>")
    assert out == ["rust"]


def test_taxonomy_index_links_are_not_tags():
    """The nav links to the /tags and /categories listing pages carry no slug,
    which is exactly what keeps them out."""
    assert extract_page_tags('<a href="/tags">Tags</a><a href="/categories">All</a>') == []


def test_anchor_text_beats_the_slug():
    """The publisher's own casing and punctuation — "Pet Supplies", "Woot!" —
    rather than "pet-supplies". This is the gottadeal case, where the harvested
    tag had been the surrounding sentence ("in XXX, YYY") instead of the anchors.
    """
    out = extract_page_tags(
        '<p>Posted on 7/29/26 in <a href="/category/woot/">Woot!</a>, '
        '<a href="/category/pet-supplies/">Pet Supplies</a></p>'
    )
    assert out == ["Woot!", "Pet Supplies"]


def test_a_leading_hash_is_stripped():
    """Display chrome, not part of the name — and Lectio uses "#" as its own tag
    marker everywhere. Stripping it also folds a term linked once as a category
    and once as a hash-prefixed tag."""
    out = extract_page_tags(
        '<a href="/categories/python/">python</a><a href="/tags/python/">#python</a>'
    )
    assert out == ["python"]


# --- manual per-(feed, tag) dismissal ---
def test_dismissed_tags_are_scoped_to_their_feed(service):
    """Per feed, not global: "Forum" is noise on Slickdeals and might be a real
    topic elsewhere."""
    a, b = "https://slickdeals.net/rss", "https://example.test/other"
    service.set_tag_suppressed(a, "Forum", True)

    assert service.suppressed_tags(a) == {"forum"}
    assert service.suppressed_tags(b) == set()


def test_dismissal_is_case_insensitive(service):
    """A publisher changing "ILLUSTRATION" to "Illustration" must not quietly
    resurrect a chip that was already dismissed."""
    feed = "https://example.test/f"
    service.set_tag_suppressed(feed, "ILLUSTRATION", True)

    assert "illustration" in service.suppressed_tags(feed)


def test_restore_removes_it_whatever_the_casing(service):
    """A mis-clicked × must have a way back, and the stored row may differ in case
    from whatever the caller is looking at now."""
    feed = "https://example.test/f"
    service.set_tag_suppressed(feed, "Popular Deals", True)
    service.set_tag_suppressed(feed, "popular deals", False)

    assert service.suppressed_tags(feed) == set()


def test_dismissal_does_not_delete_the_stored_tag(service):
    """This hides a chip; it does not forget a fact. The rows still feed the
    tag-filtered feed adapters."""
    feed = "https://example.test/f"
    service.record_entry_tags(feed, [("e1", ["Popular Deals", "keepme"])])
    service.set_tag_suppressed(feed, "Popular Deals", True)

    assert service.get_tags_for_entry(feed, "e1") == ["Popular Deals", "keepme"]


def test_blank_tags_are_ignored(service):
    feed = "https://example.test/f"
    service.set_tag_suppressed(feed, "   ", True)
    assert service.suppressed_tags(feed) == set()


def test_suppressed_tag_list_keeps_original_casing(service):
    """The undo list shows the user what they dismissed, not a lowercased version."""
    feed = "https://example.test/f"
    service.set_tag_suppressed(feed, "Popular Deals", True)

    assert service.suppressed_tag_list(feed) == ["Popular Deals"]


def test_dismissing_a_multi_word_tag_works(monkeypatch, service):
    """The chips are rendered NORMALIZED (lowercased, spaces to hyphens), so the ×
    sends "popular-deals" while the stored feed tag is "Popular Deals".

    A plain lowercase compare gives "popular deals" and misses — so every
    multi-word tag came back after being dismissed, while single-word ones like
    "python" stuck because they normalize to themselves. Reported as "I removed
    #popular-deals from a bunch of these now, keep seeing it" alongside "other tags
    I've removed elsewhere seem to stay gone", which is what identified it.
    """
    import main

    feed = "https://slickdeals.net/rss"
    service.record_entry_tags(feed, [("e1", ["Popular Deals", "Nintendo Switch"])])
    monkeypatch.setattr(main, "feed_tag_service", service)

    # As the chip sends it: normalized, not the feed's raw casing.
    service.set_tag_suppressed(feed, "popular-deals", True)

    assert main.get_feed_tag_suggestions(feed, "e1") == ["Nintendo Switch"]


def test_suggestions_drop_dismissed_tags(monkeypatch, service):
    """End to end through the suggestion path: the chip disappears, the row stays.

    This is the replacement for two reverted heuristics. Automatic suppression hid
    tags the user wanted — "Lessons" on a guitar-lesson feed reads as boilerplate
    to every frequency- or name-based rule yet is the correct filing tag — so the
    judgment belongs to the person filing.
    """
    import main

    feed = "https://slickdeals.net/rss"
    service.record_entry_tags(feed, [("e1", ["Popular Deals", "Nintendo Switch"])])
    monkeypatch.setattr(main, "feed_tag_service", service)

    assert main.get_feed_tag_suggestions(feed, "e1") == ["Popular Deals", "Nintendo Switch"]

    service.set_tag_suppressed(feed, "Popular Deals", True)
    assert main.get_feed_tag_suggestions(feed, "e1") == ["Nintendo Switch"]

    service.set_tag_suppressed(feed, "Popular Deals", False)
    assert main.get_feed_tag_suggestions(feed, "e1") == ["Popular Deals", "Nintendo Switch"]


def test_numbers_only_tags_are_dropped_from_both_sources():
    """A numbers-only tag carries nothing — a comment count, a post id, pagination,
    a bare year. Josh's call: "trying to think where a numbers-only tag would be
    useful … definitely mixed are useful".

    A stray "84" was harvested from lemire.me this way, and 580 stored rows were
    bare numbers.
    """
    class _Tag:
        def __init__(self, term): self.term, self.label, self.scheme = term, None, None

    class _Entry:
        def __init__(self, tags): self.tags = tags

    assert extract_page_tags('<a href="/tag/84/">84</a><a href="/tag/rust/">rust</a>') == ["rust"]
    assert extract_feed_entry_tags(_Entry([_Tag("2014"), _Tag("666")])) == []


def test_mixed_tags_containing_digits_survive():
    """Anything with a non-digit is real vocabulary and must be kept."""
    class _Tag:
        def __init__(self, term): self.term, self.label, self.scheme = term, None, None

    class _Entry:
        def __init__(self, tags): self.tags = tags

    out = extract_feed_entry_tags(_Entry([
        _Tag("80s"), _Tag("3d"), _Tag("2.5 Admins"), _Tag("2020 election"),
        _Tag("Windows 11"), _Tag("Doom (1993)"),
    ]))
    assert out == ["80s", "3d", "2.5 Admins", "2020 election", "Windows 11", "Doom (1993)"]


def test_an_archive_year_list_is_not_a_set_of_tags():
    """nwcpp.org carries 2000-2026 down the side of every page, and all sixteen
    were harvested onto one post. A real post might carry a year or two; nothing
    carries fifteen — so the whole run is dropped rather than any single year being
    judged."""
    page = "".join(f'<a href="/tags/{y}/">{y}</a>' for y in range(2000, 2016))
    page += '<a href="/tags/cplusplus/">cplusplus</a>'
    assert extract_page_tags(page) == ["cplusplus"]


def test_one_or_two_years_on_a_page_survive():
    """A music or photo post legitimately tags a year, and that is not an archive
    list."""
    page = '<a href="/tags/1985/">1985</a><a href="/tags/synthpop/">synthpop</a>'
    # (the bare-numeric page rule still drops "1985" itself; the point here is that
    # the archive-run rule does not fire and take the real tag with it)
    assert "synthpop" in extract_page_tags(page)
