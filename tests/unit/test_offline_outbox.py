"""Offline actions: the outbox queue, and the cache-aware save cursor.

There is no JS runtime in this environment, so where a behavior is decided by a
literal in the shipped file — a regex, a status-code map — the test pulls that
literal out of the real file and exercises it here, rather than asserting the
source contains a string. A copy of the pattern in the test would only prove the
copy works.

The rest are wiring contracts: load order, shell membership, cache version. They
are cheap and they catch the failures that are invisible until a device is
offline in a field somewhere, which is the worst possible place to find them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"

SW = (STATIC / "sw.js").read_text()
OUTBOX = (STATIC / "outbox.js").read_text()
PROBE = (STATIC / "offline-probe.js").read_text()
READER = (STATIC / "reader.js").read_text()


def _code_only(js: str) -> str:
    """Strip comments. These files explain themselves at length, and several of
    those explanations name the very thing the code must no longer do — so a
    plain substring check on the whole file tests the prose, not the program."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


# --- The image harvester in the service worker ---------------------------
# Images are now derived from each article the worker caches, so this regex is
# the only thing standing between "saved for offline" and an article of empty
# boxes.


def _img_pattern() -> re.Pattern:
    """The literal regex from sw.js, compiled here. JS and Python agree on every
    construct it uses (\\b, lazy quantifiers, character classes)."""
    m = re.search(r"const re = /(.+?)/gi;", SW)
    assert m, "sw.js no longer defines the image-harvesting regex as expected"
    return re.compile(m.group(1), re.IGNORECASE)


def _harvest(html: str) -> list[str]:
    """Mirror of _imageUrlsIn: match, unescape &amp;, keep same-origin images
    from the two endpoints articles are rewritten to."""
    out: list[str] = []
    for raw in _img_pattern().findall(html):
        src = raw.replace("&amp;", "&")
        if not src.startswith("/"):
            continue
        path = src.split("?")[0]
        if not (path == "/api/img" or path.startswith("/starred-asset/")):
            continue
        if src not in out:
            out.append(src)
    return out


def test_harvest_finds_proxied_and_archived_images():
    html = ('<p>x</p><img src="/api/img?u=https%3A//e.com/a.jpg&amp;w=800">'
            '<img class="lead" src="/starred-asset/abc123.jpg">')
    assert _harvest(html) == ["/api/img?u=https%3A//e.com/a.jpg&w=800",
                              "/starred-asset/abc123.jpg"]


def test_harvest_unescapes_ampersands():
    """src attributes are HTML-escaped and /api/img carries several parameters.
    Caching the literal "&amp;" URL stores a response nothing ever requests
    again — it counts as a success and the image is still missing offline."""
    got = _harvest('<img src="/api/img?u=x&amp;w=800&amp;h=600">')
    assert got == ["/api/img?u=x&w=800&h=600"]
    assert "&amp;" not in got[0]


def test_harvest_skips_cross_origin_and_stray_same_origin():
    """A cross-origin fetch is opaque, so caching one stores something the worker
    cannot tell apart from a failure. A feed's broken relative src resolves
    against our origin and would be cached as a 404."""
    html = ('<img src="https://cdn.example.com/a.jpg">'
            '<img src="/uploads/2019/broken.jpg">'
            '<img src="/static/placeholder.png">')
    assert _harvest(html) == []


def test_harvest_reads_src_not_other_attributes():
    """data-src and srcset are decoys: the browser renders `src`, so that is what
    has to be in the cache."""
    html = '<img data-src="/api/img?u=lazy" srcset="/api/img?u=2x 2x" src="/api/img?u=real">'
    assert _harvest(html) == ["/api/img?u=real"]


def test_harvest_dedupes():
    """Articles from one feed share a logo and an author avatar far more often
    than you would guess; re-fetching each occurrence triples a save."""
    html = '<img src="/api/img?u=logo"><p>a</p><img src="/api/img?u=logo">'
    assert _harvest(html) == ["/api/img?u=logo"]


# --- What the queue is allowed to throw away -----------------------------


def _terminal_codes() -> set[int]:
    m = re.search(r"var TERMINAL = \{(.+?)\};", OUTBOX, re.S)
    assert m, "outbox.js no longer defines TERMINAL as expected"
    return {int(c) for c in re.findall(r"(\d+):", m.group(1))}


def test_a_403_is_never_dropped():
    """The one that matters. On this app a 403 is an expired session or a CSRF
    token from a page cached hours ago — neither is a verdict on the action.
    Dropping there loses the work exactly when the reader can least notice."""
    assert 403 not in _terminal_codes()


def test_gone_entries_do_not_wedge_the_queue():
    """Retrying forever would strand every later action behind a record that can
    never apply — the silent never-draining queue the status line exists to
    prevent."""
    assert {404, 409, 410} <= _terminal_codes()


def test_server_errors_are_retried_not_dropped():
    assert not any(c >= 500 for c in _terminal_codes())


# --- Wiring --------------------------------------------------------------


def test_outbox_loads_before_reader_js_on_the_article_page():
    """reader.js calls window.LectioOutbox at click time, so order is not
    strictly required — but a queue that is not yet defined when a page-load
    flush should have run means actions sit unsent until the next navigation."""
    src = (ROOT / "main.py").read_text()
    doc = src[src.index("f\"<script src='/static/outbox.js"):]
    assert doc.index("/static/outbox.js") < doc.index("/static/reader.js")


def test_read_mode_loads_the_outbox():
    """The list is the page most likely to be open when the connection returns,
    so it is the page that most needs to flush on load."""
    tpl = (TEMPLATES / "read_mode.html").read_text()
    assert "/static/outbox.js?v={{ static_asset_version }}" in tpl


def test_both_surfaces_show_the_queue_depth():
    """A queue nobody can see is how work gets lost without anyone noticing."""
    assert "data-outbox-depth" in (TEMPLATES / "read_mode.html").read_text()
    assert "data-outbox-depth" in (ROOT / "main.py").read_text()


def test_the_outbox_is_part_of_the_offline_shell():
    """An article read offline with no outbox is an article you cannot act on."""
    m = re.search(r"const SHELL = \[(.+?)\];", SW, re.S)
    assert m and "/static/outbox.js" in m.group(1)


def test_the_cache_version_moved_with_the_caching_rules():
    """v2 cached images chosen by manifest POSITION, so its articles hold images
    belonging to a different set of articles. Old caches are deleted on activate;
    leaving the name alone would keep serving them."""
    assert 'const CACHE = "lectio-offline-v3"' in SW


def test_the_worker_never_intercepts_a_mutation():
    """POSTs must reach the network untouched — the outbox owns their durability,
    and a worker answering one from cache would fabricate a success."""
    assert 'if (req.method !== "GET") return;' in SW


def test_the_save_cursor_no_longer_counts_positions():
    """The positional cursor re-saved and skipped articles whenever new items
    landed at the top of the list, which in the Inbox is the entire point of the
    Inbox. Replaced by asking the worker what it already holds."""
    code = _code_only(PROBE)
    assert "localStorage" not in code
    assert "lectio-offline-offset" not in code
    assert '{ type: "cached", urls: urls }' in code


def test_the_probe_sends_articles_only():
    """Images are derived by the worker from the articles it stored. If the page
    also proposed images, the two lists could disagree again — which is the bug
    the manifest had."""
    code = _code_only(PROBE)
    assert "manifest" not in code
    assert "imageUrls" not in code


def test_archive_and_delete_go_through_the_queue():
    """Both are immediately followed by a navigation, which cancels an in-flight
    POST. Enqueue-first is the only ordering that survives that."""
    for route in ("/entries/archive", "/entries/discard"):
        block = READER[READER.index(route) - 400:READER.index(route) + 200]
        assert "postAction(" in block
    assert "submit(url, params, requestedWith).then(afterAction, afterAction)" in READER


def test_mark_read_goes_through_the_queue():
    """Otherwise a session read with WiFi off silently un-does itself."""
    assert 'submit("/entries/read"' in READER


def test_tagging_posts_directly_and_queues_only_on_failure():
    """Tagging is the one action whose REPLY matters: the server normalizes the
    name and enforces the cap, and the panel re-renders from what came back."""
    block = READER[READER.index("function applyTags"):READER.index("function toggleTag")]
    assert 'post("/entries/tags", params' in block
    assert "window.LectioOutbox.submit" in block


def test_the_manifest_route_is_gone():
    """Its only caller stopped using it; a route that renders every article with
    BeautifulSoup to guess at images is not worth keeping warm."""
    src = (ROOT / "main.py").read_text()
    assert "/read/offline/manifest" not in src
    assert "read_offline_manifest" not in src
