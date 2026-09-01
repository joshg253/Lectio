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

from services.feed_tags import extract_page_tags, tags_from_mrf_meta, tags_from_url_path  # noqa: E402


def test_page_tags_article_tag_metas():
    html = '''<html><head>
      <meta property="article:tag" content="Windows 11">
      <meta property="article:tag" content="Backup">
      <meta name="article:tag" content="Storage">
    </head><body></body></html>'''
    assert extract_page_tags(html) == ["Windows 11", "Backup", "Storage"]


def test_page_tags_og_article_tag_metas():
    """initialcommit.com: og:article:tag, not bare article:tag — same
    one-tag-per-meta convention (Open Graph's own og: prefix on the
    article: namespace), found live 2026-08-31 surveying untagged feeds
    once the page-fetch escalation fix made the page reachable at all."""
    html = '''<meta property="og:article:section" content="Programming"/>
      <meta property="og:article:tag" content="programming"/>
      <meta property="og:article:tag" content="coding"/>'''
    assert extract_page_tags(html) == ["programming", "coding"]


def test_page_tags_keywords_split_and_dedupe():
    html = '''<meta name="keywords" content="python, AI,  python , machine learning">'''
    assert extract_page_tags(html) == ["python", "AI", "machine learning"]


def test_page_tags_single_quotes_and_parsely():
    html = "<meta name='parsely-tags' content='linux,gaming'>"
    assert extract_page_tags(html) == ["linux", "gaming"]


def test_page_tags_keywords_falls_back_to_space_split_when_long_and_comma_free():
    """sethmlarson.dev: keywords ships space-separated, not comma-separated.
    Left whole this always exceeds the length cap and is silently dropped —
    found live 2026-08-31 surveying untagged feeds."""
    html = ('<meta name="keywords" content="python pypi open source maintainer '
            'urllib3 requests http networking security oss"/>')
    out = extract_page_tags(html)
    assert "python" in out
    assert "urllib3" in out
    assert "oss" in out


def test_page_tags_keywords_short_comma_free_value_stays_one_tag():
    """A short space-free phrase must NOT get shredded into words — only the
    long, comma-free shape (guaranteed junk otherwise) triggers the split."""
    html = '<meta name="keywords" content="machine learning">'
    assert extract_page_tags(html) == ["machine learning"]


def test_page_tags_ignores_other_metas_and_junk():
    html = '''<meta property="og:title" content="Not a tag">
      <meta name="description" content="prose, with, commas">
      <meta property="article:tag" content="">
      <meta property="article:tag" content="''' + ("x" * 80) + '''">'''
    assert extract_page_tags(html) == []


# --- youtube.com is excluded entirely ---------------------------------------
# Its <meta name="keywords"> is fixed, locale-translated UI boilerplate, not
# per-video content — confirmed live 2026-08-31: 63 sampled entries across
# unrelated YouTube channels all produced the byte-identical six "tags"
# ("Video, share, camera phone, video phone, free, upload", German shown
# here). No other tier has anything real to key on for a YouTube page either.

def test_youtube_watch_page_yields_nothing_even_with_a_keywords_meta():
    html = '<meta name="keywords" content="Video, teilen, Kamerahandy, Videohandy, kostenlos, hochladen">'
    assert extract_page_tags(html, "https://www.youtube.com/watch?v=hsm0ahVdb0Q") == []


def test_youtube_short_link_host_also_excluded():
    html = '<meta name="keywords" content="Video, teilen, Kamerahandy, Videohandy, kostenlos, hochladen">'
    assert extract_page_tags(html, "https://youtu.be/hsm0ahVdb0Q") == []


def test_youtube_url_path_fallback_also_suppressed():
    """Without even a page fetch, tags_from_url_path("/shorts/<id>") would
    otherwise yield the equally useless "shorts" for every Short regardless
    of channel or subject."""
    assert extract_page_tags(None, "https://www.youtube.com/shorts/abc123") == []


def test_non_youtube_host_is_unaffected():
    assert extract_page_tags('<a rel="tag" href="/x">Real Tag</a>', "https://example.com/post") == ["Real Tag"]


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


# ── tag_vocabulary: the source for the rule form's autocomplete ────────────


def test_vocabulary_counts_entries_and_orders_by_use(service):
    service.record_entry_tags(FEED, [
        ("e1", ["Python", "AI"]),
        ("e2", ["Python"]),
        ("e3", ["Python", "Rust"]),
    ])
    assert service.tag_vocabulary([FEED]) == [("Python", 3), ("AI", 1), ("Rust", 1)]


def test_vocabulary_merges_casing_variants(service):
    """A publisher that switches "AI" to "ai" must not produce two suggestions
    with half the count each — the filter matches case-insensitively."""
    service.record_entry_tags(FEED, [("e1", ["AI"]), ("e2", ["ai"]), ("e3", ["Ai"])])
    vocab = service.tag_vocabulary([FEED])
    assert len(vocab) == 1
    assert vocab[0][1] == 3


def test_vocabulary_is_scoped_to_the_given_feeds(service):
    other = "https://y.test/feed"
    service.record_entry_tags(FEED, [("e1", ["Python"])])
    service.record_entry_tags(other, [("e1", ["Rust"])])
    assert service.tag_vocabulary([FEED]) == [("Python", 1)]
    assert service.tag_vocabulary([other]) == [("Rust", 1)]
    # None is global scope: every feed, which is what a global rule matches.
    assert sorted(service.tag_vocabulary(None)) == [("Python", 1), ("Rust", 1)]


def test_an_empty_feed_list_is_not_global(service):
    """A 'feeds' rule whose picks resolved to nothing must suggest nothing —
    falling through to every tag in the library would be the opposite of scope."""
    service.record_entry_tags(FEED, [("e1", ["Python"])])
    assert service.tag_vocabulary([]) == []


def test_vocabulary_honors_the_limit(service):
    service.record_entry_tags(FEED, [("e1", [f"t{i}" for i in range(20)])])
    assert len(service.tag_vocabulary([FEED], limit=5)) == 5


def test_page_tags_prefer_link_text_over_prose_title():
    """A publisher links the same taxonomy twice — once as the tag, once as a
    navigation aside — and only the first is a tag. fossforce carries
    title="View all posts in AI" on the tag itself and a sibling reading
    "More posts in AI »", so a post tagged AI and Developer harvested four
    "tags" and not one of them was a tag."""
    html = (
        '<a href="https://x.test/category/ai/" title="View all posts in AI">AI</a>'
        '<a href="https://x.test/category/developer/" title="View all posts in Developer">Developer</a>'
        '<a href="https://x.test/category/ai/">More posts in AI &raquo;</a>'
        '<a href="https://x.test/category/developer/">More posts in Developer &raquo;</a>'
    )
    assert extract_page_tags(html) == ["AI", "Developer"]


def test_prose_anchor_text_falls_back_to_the_slug():
    """When the only link to a taxonomy is the prose one, its slug is the tag."""
    html = '<a href="https://x.test/category/ai/">More posts in AI &raquo;</a>'
    assert extract_page_tags(html) == ["ai"]


def test_a_long_link_text_is_not_a_tag():
    html = ('<a href="https://x.test/tag/linux/">'
            'Read our complete guide to everything Linux on the desktop</a>')
    assert extract_page_tags(html) == ["linux"]


# --- Taxonomy carried in the query string, not the path -----------------

def test_query_string_taxonomy_is_read_as_a_tag():
    """Google Developers Blog's "posted in:" block. The path says /search/,
    so only the parameter name identifies these as taxonomy links."""
    html = """
      <div class="posted-in-section__tags"><ul>
        <li><a href="/search/?technology_categories=AI" class="glue-caption">AI</a></li>
        <li><a href="/search/?content_type_categories=How-To+Guides" class="glue-caption">How-To Guides</a></li>
      </ul></div>
    """
    assert extract_page_tags(html) == ["AI", "How-To Guides"]


def test_query_taxonomy_falls_back_to_the_param_value():
    """No link text to read (the anchor wraps an icon), so the term itself is
    the tag — and '+' is a space, not a literal plus."""
    html = '<a href="/search/?content_type_categories=How-To+Guides"><img src="i.png"></a>'
    assert extract_page_tags(html) == ["How-To Guides"]


def test_search_and_paging_params_are_not_taxonomy():
    """The parameter name must END in a taxonomy word. A free-text search box
    and a paginator sit on the same pages and are not tags."""
    html = (
        '<a href="/search/?q=machine+learning">Search</a>'
        '<a href="/search/?s=tags">Find</a>'
        '<a href="/blog/?page=2">Next</a>'
        '<a href="/blog/?category_count=12">Counts</a>'
    )
    assert extract_page_tags(html) == []


def test_path_taxonomy_still_wins_its_hyphen_expansion():
    """A PATH slug is a slugification, so hyphens become spaces; a QUERY value
    is the publisher's own display term, so its hyphens are kept (the
    'How-To Guides' case above)."""
    html = '<a href="/tags/pet-supplies/"><img src="i.png"></a>'
    assert extract_page_tags(html) == ["pet supplies"]


# --- tag-classed anchors whose name is only in the link text ---------------
# ArtStation: classed tag block, but each tag links to a search page, so there
# is no title attribute and no /tag/ or /category/ slug to read.

ARTSTATION_TAGS = (
    '<div class="project-sidebar-section tags-section"><h4>Tags</h4>'
    '<ul class="project-tags list-unstyled d-flex flex-wrap">'
    '<li class="project-tag"><a target="_blank" class="project-tag-item badge label-tag"'
    ' href="/search?query=Digital 2D"> #Digital 2D </a></li>'
    '<li class="project-tag"><a target="_blank" class="project-tag-item badge label-tag"'
    ' href="/search?query=Illustration"> #Illustration </a></li>'
    '<li class="project-tag"><a target="_blank" class="project-tag-item badge label-tag"'
    ' href="/search?query=Environmental Concept Art &amp; Design">'
    ' #Environmental Concept Art &amp; Design </a></li>'
    '<li class="project-tag"><a target="_blank" class="project-tag-item badge label-tag"'
    ' href="/search?query=NoAI"> #NoAI </a></li><!----></ul></div>'
)


def test_artstation_tag_block_is_harvested():
    assert extract_page_tags(ARTSTATION_TAGS) == [
        "Digital 2D", "Illustration", "Environmental Concept Art & Design", "NoAI",
    ]


def test_ampersand_tag_is_not_split():
    """The href writes the tag straight into ?query=, so `&amp;` unescapes to a
    bare `&` that URL-parsing would treat as a parameter separator."""
    tags = extract_page_tags(ARTSTATION_TAGS)
    assert "Environmental Concept Art & Design" in tags
    assert "Design" not in tags


def test_anchor_wrapping_markup_is_left_to_the_title_tier():
    html = '<a class="tag-item" href="/x"><img src="a.png"> Something </a>'
    assert extract_page_tags(html) == []


def test_taxonomy_href_anchor_survives_a_decorative_icon():
    """tartanllama.xyz: a /tags/cpp/ link wraps ~400 chars of <svg> markup
    around the visible "C++" label. Found live 2026-08-31 — the anchor was
    entirely invisible to every tier (not just this one) because the old
    120-char _ANCHOR_RE cap could never reach </a>."""
    html = ('<a href="/tags/cpp/" class="relative pr-2 text-lg" data-x="y">'
            '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            'class="inline-block opacity-80 -mr-3.5 size-4">'
            '<path stroke="none" d="M0 0h24v24H0z" fill="none" /><path d="M5 9l14 0" />'
            '<path d="M5 15l14 0" /><path d="M11 4l-4 16" /><path d="M17 4l-4 16" /></svg>'
            '&nbsp;<span>C++</span> </a>')
    assert extract_page_tags(html) == ["C++"]


def test_sentence_length_text_is_not_a_tag():
    """Guards the gottadeal failure: surrounding prose harvested as a tag."""
    long_text = "Posted on 7/29/26 in Woot!, Pet Supplies and a great deal more besides"
    assert extract_page_tags(f'<a class="tag" href="/x">{long_text}</a>') == []


def test_titled_anchor_still_uses_its_title():
    html = '<a class="tag" title="Real Title" href="/x"> ignored text </a>'
    assert extract_page_tags(html) == ["Real Title"]


# --- taxonomy from the entry's own URL path --------------------------------
# Needs no page fetch, which makes it the only tier that works on a site that
# refuses us: gottadeal and realpython 403 even a browser identity, and their
# section is right there in the link.

def test_section_and_subsection_from_path():
    assert tags_from_url_path(
        "https://www.guitarplayer.com/lessons/advice-tips/some-long-headline"
    ) == ["lessons", "advice tips"]


def test_single_section():
    assert tags_from_url_path("https://gottadeal.com/deals/kohls-55-off-475772") == ["deals"]


def test_slug_only_yields_nothing():
    """The last segment is the article, not a taxonomy — tagging it would give
    every post a unique useless tag."""
    assert tags_from_url_path("https://realpython.com/ollama/") == []
    assert tags_from_url_path("https://example.com/just-a-title") == []


def test_a_dated_permalink_yields_nothing():
    """A date in the path means the segments around it are the site's own
    filing, not a subject: tagging every strip with the comic's name repeats
    the feed itself. Reads the URL's SHAPE — not how often a tag occurs, which
    is the suppression approach reverted twice (see Plan.md)."""
    assert tags_from_url_path("https://lernerpython.com/2026/02/21/uv-version-bump/") == []
    assert tags_from_url_path("https://tinyview.com/they-can-talk/2026/02/25/blizzard") == []
    assert tags_from_url_path("https://example.com/tech/2026/some-post") == []


def test_a_year_inside_a_slug_is_not_a_date_segment():
    assert tags_from_url_path(
        "https://example.com/section/subsection/a-post-2026-roundup"
    ) == ["section", "subsection"]


def test_structure_words_are_dropped():
    assert tags_from_url_path("https://example.com/blog/post/my-title") == []


def test_front_main_routing_segments_are_dropped():
    """netbeans.apache.org: /front/main/blogs/entry/<slug> — found live
    2026-08-31 in the untagged-feed survey. "front"/"main" are this site's
    own URL routing, not a subject, same class as blog/entry/page already
    in the stopword list."""
    assert tags_from_url_path(
        "https://netbeans.apache.org/front/main/blogs/entry/netbeans-status-interview-at-javaone/"
    ) == []


def test_bad_input_is_safe():
    for value in (None, "", "not a url", "https://example.com"):
        assert tags_from_url_path(value) == []


def test_path_tags_join_the_page_tags():
    out = extract_page_tags('<a rel="tag" href="/x">Woot!</a>',
                            "https://gottadeal.com/deals/a-slug-1")
    assert "Woot!" in out and "deals" in out


def test_path_tags_work_with_no_html_at_all():
    assert extract_page_tags(None, "https://www.guitarplayer.com/lessons/advice-tips/x") == [
        "lessons", "advice tips"
    ]


# --- "Posted ... in <a>Category</a>, <a>Category</a>" byline ---------------
# gottadeal.com's real taxonomy: found live 2026-08-31 on a real entry that
# was already reachable (the proxy escalation fix landed first) but still
# yielded only the generic path tag "deals" — its per-article anchors carry
# no rel="tag", no "tag" class, and their href ("/deals/target") doesn't
# match the /tag//category/ URL-shape tier either, since "deals" is the
# site's own top-level section, not a taxonomy word.

def test_posted_in_byline_anchors_are_captured():
    html = ('<font color=#888888>Posted on 8/31/26 in '
            '<a href="/deals/target">Target</a>, '
            '<a href="/deals/household-essentials">Household Essentials</a></font>')
    out = extract_page_tags(html, "https://gottadeal.com/deals/some-slug-476534")
    assert "Target" in out
    assert "Household Essentials" in out


def test_posted_in_single_category():
    html = 'Posted in <a href="/deals/electronics">Electronics</a>'
    assert extract_page_tags(html) == ["Electronics"]


def test_posted_in_requires_an_adjacent_anchor():
    """The cue text alone (no anchor right after "in") must not match —
    otherwise this tier degrades into the free-prose-harvest failure mode
    test_sentence_length_text_is_not_a_tag guards against for the anchor-text
    tiers."""
    assert extract_page_tags("<p>Posted in the comments below by a reader.</p>") == []


def test_posted_in_does_not_reach_past_a_run_of_anchors():
    """A sentence AFTER the anchor run must not get pulled in — only the
    anchors themselves are tags."""
    html = ('Posted in <a href="/deals/target">Target</a> and other places '
            'you might not expect to find a bargain this good.')
    out = extract_page_tags(html)
    assert out == ["Target"]


def test_filed_under_byline_anchors_are_captured():
    """xania.org (Matt Godbolt's blog): "Filed under:" instead of "Posted
    in" — same shape, different cue word. Found live 2026-08-31."""
    html = 'Filed under:\n<a href="/Coding">Coding</a>\n<a href="/AoCO2025">AoCO2025</a>'
    out = extract_page_tags(html)
    assert "Coding" in out
    assert "AoCO2025" in out


# --- itemprop="keywords" anchors --------------------------------------------
# refp.se: real per-article tags via schema.org microdata, no rel="tag", no
# "tag" class, href "/articles/tagged/x" doesn't match the /tag//tags/
# taxonomy-href tier either ("tagged" isn't "tag"/"tags"). Found live
# 2026-08-31.

def test_itemprop_keywords_anchors_are_captured():
    html = ('<a itemProp="keywords" href="/articles/tagged/developer-life">#developer-life</a>'
            '<a itemProp="keywords" href="/articles/tagged/blogging">#blogging</a>')
    out = extract_page_tags(html)
    assert "developer-life" in out
    assert "blogging" in out


# --- aria-label="... tagged with X" -----------------------------------------
# labnol.org (Digital Inspiration): tag chips carry no rel="tag", no "tag"
# class, and their href (e.g. "/google-calendar") has no /tag//category/
# shape either — the accessibility label is the only signal. Found live
# 2026-08-31 in the same untagged-feed survey as the og:article:tag fix.

def test_aria_label_tagged_with_is_captured():
    html = ('<a href="/google-calendar" aria-label="View all posts tagged with Google Calendar">'
            '#google calendar</a>')
    assert extract_page_tags(html) == ["Google Calendar"]


def test_aria_label_tagged_with_single_quotes():
    html = "<a href='/x' aria-label='View all posts tagged with Mail Merge for Gmail'>text</a>"
    assert extract_page_tags(html) == ["Mail Merge for Gmail"]


def test_aria_label_scoped_to_the_attribute_not_page_prose():
    """"tagged with" appearing elsewhere on the page (not inside an
    aria-label) must not become a tag."""
    html = "<p>This post is tagged with enthusiasm and a healthy dose of sarcasm.</p>"
    assert extract_page_tags(html) == []


def test_aria_label_multiple_chips_all_captured():
    html = ('<a aria-label="View all posts tagged with Google Calendar">a</a>'
            '<a aria-label="View all posts tagged with Mail Merge for Gmail">b</a>')
    out = extract_page_tags(html)
    assert "Google Calendar" in out
    assert "Mail Merge for Gmail" in out


# --- the same taxonomy stated twice ----------------------------------------

def test_meta_and_path_forms_collapse_to_one_chip():
    """A page can state one taxonomy two ways — a meta tag "Advice & Tips" and
    the URL path "advice-tips". Deduping on the lowercased string keeps both,
    so the reader sees the same tag twice and dismisses it twice."""
    out = extract_page_tags(
        '<meta property="mrf:tags" content="category:Advice &amp; Tips">',
        "https://www.guitarplayer.com/lessons/advice-tips/a-headline",
    )
    assert out.count("Advice & Tips") == 1
    assert "advice tips" not in out
    assert "lessons" in out or "Lessons" in out


# --- Future plc mrf:tags ---------------------------------------------------

def test_mrf_meta_taxonomy():
    html = ('<meta property="mrf:tags" content="region:GB;articleType:Deals;'
            'channel:Music tech;control:serversidehawk;freeform:Joe Bonamassa">')
    out = tags_from_mrf_meta(html)
    assert out == ["Deals", "Music tech", "Joe Bonamassa"]


def test_mrf_entities_do_not_split_the_pairs():
    """Pairs are ';'-separated and values carry entities that also end in ';',
    so a naive split yields 'Advice &amp' plus a stray 'Tips'."""
    assert tags_from_mrf_meta(
        '<meta property="mrf:tags" content="category:Advice &amp; Tips;channel:X">'
    ) == ["Advice & Tips", "X"]


def test_mrf_absent_is_cheap_and_safe():
    assert tags_from_mrf_meta("<html><body>no meta here</body></html>") == []
    assert tags_from_mrf_meta(None) == []


# --- Shopify product feeds: <s:vendor> is the maker (for a record shop, the artist)


_SHOPIFY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:s="http://jadedpixel.com/-/spec/shopify">
  <title>Store</title>
  <entry>
    <id>https://store.test/products/1</id>
    <title>Pearl Jam - Live 1995 | Vinyl</title>
    <s:type>Vinyl</s:type>
    <s:vendor>Pearl Jam</s:vendor>
    <s:price>16.99</s:price>
    <summary type="html">&lt;p&gt;&lt;strong&gt;Vendor: &lt;/strong&gt;Pearl Jam&lt;/p&gt;</summary>
  </entry>
</feed>
"""


def _first_entry(xml: str):
    import feedparser
    return feedparser.parse(xml.encode()).entries[0]


def test_shopify_vendor_becomes_a_tag():
    assert extract_feed_entry_tags(_first_entry(_SHOPIFY_ATOM)) == ["Pearl Jam"]


def test_vendor_is_found_under_any_namespace_prefix():
    """feedparser flattens an unknown namespace with the document's own prefix,
    so the key depends on what the store wrote in xmlns:…"""
    xml = _SHOPIFY_ATOM.replace("xmlns:s=", "xmlns:shop=").replace("<s:", "<shop:").replace("</s:", "</shop:")
    assert extract_feed_entry_tags(_first_entry(xml)) == ["Pearl Jam"]


def test_a_bare_vendor_key_is_not_harvested():
    """Only a namespaced `<prefix>_vendor`. A bare `vendor` element belongs to
    whatever schema defined it and may mean something else entirely."""
    assert extract_feed_entry_tags({"vendor": "Some Corp"}) == []


def test_vendor_folds_into_an_identical_category():
    """Case-insensitive dedupe: a store that also files the artist as a category
    must not produce the same chip twice."""
    import feedparser
    entry = feedparser.FeedParserDict(
        tags=[feedparser.FeedParserDict(term="Pearl Jam", label=None, scheme=None)],
        s_vendor="pearl jam")
    assert extract_feed_entry_tags(entry) == ["Pearl Jam"]


def test_structured_or_oversized_vendor_values_are_ignored():
    assert extract_feed_entry_tags({"s_vendor": {"name": "x"}}) == []
    assert extract_feed_entry_tags({"s_vendor": "x" * 61}) == []
    assert extract_feed_entry_tags({"s_vendor": "   "}) == []


# --- neowin.net: <neowin:tags>#OpenAI #ChatGPT #Ads</neowin:tags> is invisible to
# feedparser's .tags/.category, same shape as the Shopify vendor field above —
# raised 2026-08-31, checking whether "no can do" still held for it.


_NEOWIN_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:neowin="https://www.neowin.net/">
  <channel>
    <title>Neowin</title>
    <item>
      <title>ChatGPT Ads hits $1 billion annual revenue run rate</title>
      <link>https://www.neowin.net/news/chatgpt-ads-hits-1-billion/</link>
      <guid>https://www.neowin.net/news/chatgpt-ads-hits-1-billion/</guid>
      <neowin:tags>#OpenAI #ChatGPT #Ads</neowin:tags>
    </item>
  </channel>
</rss>
"""


def test_neowin_style_hashtag_field_becomes_tags():
    assert extract_feed_entry_tags(_first_entry(_NEOWIN_RSS)) == ["OpenAI", "ChatGPT", "Ads"]


def test_hashtag_field_is_found_under_any_namespace_prefix():
    xml = _NEOWIN_RSS.replace("xmlns:neowin=", "xmlns:pub=").replace("<neowin:", "<pub:").replace("</neowin:", "</pub:")
    assert extract_feed_entry_tags(_first_entry(xml)) == ["OpenAI", "ChatGPT", "Ads"]


def test_a_bare_tags_key_is_not_harvested_twice():
    """A bare `tags` key is feedparser's OWN structured .tags list, already
    handled above — this extractor only matches a namespaced `<prefix>_tags`
    string, so it must not also fire on that."""
    assert extract_feed_entry_tags({"tags": "#should-not-double-count"}) == []


def test_hashtag_field_without_hash_prefix_still_works():
    """Not every publisher prefixes with #; splitting on whitespace alone
    still separates the terms, and _clean_tag_values' leading-# strip is a
    no-op when there's nothing to strip."""
    assert extract_feed_entry_tags({"pub_tags": "OpenAI ChatGPT Ads"}) == ["OpenAI", "ChatGPT", "Ads"]


def test_oversized_hashtag_tokens_are_ignored():
    assert extract_feed_entry_tags({"pub_tags": "#" + "x" * 61}) == []
