"""Integration tests for Read Mode (GET /read): the 2-pane browse, the
paginated reader state, prev/next, Archive/Delete controls, and the article
content-resolution chain. Follows the save-article test pattern: mount the route
on a bare app and monkeypatch the service layer so orchestration is exercised
without a DB."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _rec(n: int, *, read: bool = False) -> dict:
    return {
        "feed_url": f"feed{n}", "id": f"e{n}", "title": f"Title {n}",
        "link": f"https://example.com/{n}", "read": read, "feed_title": "Feed",
    }


def _app():
    app = FastAPI()
    app.add_api_route("/read", main.reader_view, methods=["GET"])
    return app


def _patch_read(monkeypatch, *, backlog, archived_keys=frozenset(), article="<p>BODY</p>",
                starred=True, manual_tags=(), all_tag_names=()):
    marks: list[tuple] = []
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: list(backlog))
    monkeypatch.setattr(main, "resolve_reader_article_html", lambda f, e, l: article)
    monkeypatch.setattr(main, "_mark_entry_read_background", lambda *a: marks.append(a[:2]))
    monkeypatch.setattr(main, "get_archived_saved_keys", lambda: set(archived_keys))
    # Archive lives on the star row, so the button only renders for a starred
    # item; tags ride along so Delete's confirm can name them.
    monkeypatch.setattr(main, "_entry_is_starred", lambda f, e: starred)
    monkeypatch.setattr(main, "get_manual_tags_for_entry", lambda f, e: list(manual_tags))
    monkeypatch.setattr(main, "get_all_manual_tag_names", lambda: list(all_tag_names))
    monkeypatch.setattr(main, "_csrf_token_for", lambda req: "tok")  # bare app has no session
    return marks


# --- READ state (an article is selected) -------------------------------------

def test_read_state_prev_next_and_controls(monkeypatch):
    marks = _patch_read(monkeypatch, backlog=[_rec(1), _rec(2), _rec(3)])
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2"})
    body = r.text
    assert r.status_code == 200
    assert "reader-columns" in body and "Title 2" in body and "<p>BODY</p>" in body
    assert "entry_id=e1" in body and "entry_id=e3" in body       # prev / next (nav object)
    assert "__READER_NAV__" in body
    assert "name='csrf-token' content='tok'" in body
    assert "id='reader-archive-btn'" in body and "id='reader-delete-btn'" in body
    assert marks == []                    # rendering alone never marks read



def test_archive_button_shown_for_a_tag_kept_item(monkeypatch):
    """A tag-kept item can be archived now, and this deliberately overturns the
    old assertion that the button was hidden for it.

    Hiding it was correct while the done-flag lived on the star row: an item kept
    only by a tag had nothing to set. With `archived_entries` it does — and this
    is the majority case, not an edge one (16,479 tagged vs 10,002 starred), so
    those were exactly the items with no way to be cleared from the device.
    """
    _patch_read(monkeypatch, backlog=[_rec(2)], starred=False, manual_tags=("keepme",))
    with TestClient(_app()) as client:
        body = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2"}).text
    assert "id='reader-archive-btn'" in body
    assert "id='reader-delete-btn'" in body         # Delete still applies
    assert "data-tags='keepme'" in body             # confirm can name the tag


def test_read_archive_button_reflects_state(monkeypatch):
    _patch_read(monkeypatch, backlog=[_rec(2)], archived_keys={("feed2", "e2")}, starred=True)
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2"})
    assert "aria-pressed='true'" in r.text and "Un-archive" in r.text


def test_opening_an_unread_article_does_not_mark_it_read(monkeypatch):
    """Serving the reader page marks nothing.

    Opening an item is how you decide whether to read it in an e-ink browse
    loop, so marking on render turned every peek into a read. The client posts
    to /entries/read once pagination has settled and the last page has actually
    been reached (static/reader.js).
    """
    marks = _patch_read(monkeypatch, backlog=[_rec(1), _rec(2)])
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed1", "entry_id": "e1"})
    assert r.status_code == 200 and marks == []


def test_read_already_read_not_remarked(monkeypatch):
    marks = _patch_read(monkeypatch, backlog=[_rec(1, read=True), _rec(2)])
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed1", "entry_id": "e1"})
    assert r.status_code == 200 and marks == []


def test_read_entry_not_in_list_renders_standalone(monkeypatch):
    _patch_read(monkeypatch, backlog=[_rec(2), _rec(3)])
    monkeypatch.setattr(
        main, "get_entry_detail",
        lambda f, e: {"feed_url": "feed1", "id": "e1", "title": "Gone",
                      "link": "https://example.com/1", "read": True},
    )
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed1", "entry_id": "e1"})
    assert r.status_code == 200 and "Gone" in r.text
    assert '"prev": ""' in r.text and "entry_id=e2" in r.text     # no prev; next = list head


# --- BROWSE state (no article selected) --------------------------------------

_CANNED_CTX = {
    "folder_nodes": [
        {"label": "All", "glyph": "★", "href": "/read", "count": 3, "active": True},
        {"label": "Tech", "glyph": "▸", "href": "/read?folder_id=5", "count": 2, "active": False},
    ],
    "tag_nodes": [{"label": "#toread", "glyph": "", "href": "/read?tag=toread", "count": 2, "active": False}],
    "archive_node": {"label": "Archive", "glyph": "▤", "href": "/read?archived=1", "count": 1, "active": False},
    "list_items": [{"title": "Item One", "subtitle": "example.com", "read": False, "href": "/read?feed_url=f&entry_id=e"}],
    "selected_label": "All", "search_query": "", "tags_open": False, "static_asset_version": "t",
}


def test_bare_read_renders_two_pane_browse(monkeypatch):
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: [])
    monkeypatch.setattr(main, "_build_read_mode_context", lambda *a, **k: dict(_CANNED_CTX))
    with TestClient(_app()) as client:
        r = client.get("/read")
    body = r.text
    assert r.status_code == 200
    assert "rm-layout" in body and "reader-columns" not in body   # browse, not reader
    assert "Item One" in body and "#toread" in body and "Archive" in body


def test_all_node_empty_glyph_renders_spacer_not_arrow(monkeypatch):
    """'All' is plain navigation — no expand arrow — but it still gets the
    fixed-width glyph spacer so its label aligns with the folder rows."""
    ctx = dict(_CANNED_CTX, folder_nodes=[
        {"label": "All", "glyph": "", "href": "/read?scope=feeds", "count": 3, "active": True},
        {"label": "Tech", "glyph": "▸", "href": "/read?folder_id=5", "count": 2, "active": False},
    ])
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: [])
    monkeypatch.setattr(main, "_build_read_mode_context", lambda *a, **k: dict(ctx))
    with TestClient(_app()) as client:
        body = client.get("/read").text
    # The All row keeps an (empty) spacer; only Tech carries the arrow.
    assert '<span class="rm-glyph" aria-hidden="true"></span>' in body
    assert body.count("▸") == 1


def test_browse_passes_archived_and_scope(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: seen.update(k) or [])
    monkeypatch.setattr(main, "_build_read_mode_context", lambda *a, **k: dict(_CANNED_CTX, **{}))
    with TestClient(_app()) as client:
        r = client.get("/read", params={"archived": "1", "folder_id": "5"})
    assert r.status_code == 200
    assert seen["archived"] is True and seen["star_only"] is True and seen["read_filter"] == "all"


def test_search_reaches_all_saved(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: seen.update(k) or [])
    monkeypatch.setattr(main, "_build_read_mode_context", lambda *a, **k: dict(_CANNED_CTX))
    with TestClient(_app()) as client:
        client.get("/read", params={"q": "python"})
    assert seen["archived"] is None and seen["search_query"] == "python"  # search spans archived too


# --- URL scope helpers -------------------------------------------------------

def test_read_href_helpers_encode_scope():
    assert main._read_browse_href(None, None, False, None) == "/read"
    assert main._read_browse_href(5, None, True, None) == "/read?folder_id=5&archived=1"
    assert main._read_browse_href(None, "toread", False, None) == "/read?tag=toread"
    href = main._reader_href("feed1", "e1", folder_id=5, tag=None, archived=True, q=None)
    assert "feed_url=feed1" in href and "entry_id=e1" in href
    assert "folder_id=5" in href and "archived=1" in href


# --- resolve_reader_article_html: archived -> live -> stored -----------------

def test_article_html_prefers_archived(monkeypatch):
    monkeypatch.setattr(main, "_resolve_archived_readability_html", lambda f, e: "<p>ARCHIVED</p>")
    monkeypatch.setattr(main, "fetch_readability_article",
                        lambda url: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert "ARCHIVED" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")


def test_article_html_falls_back_live_then_stored(monkeypatch):
    monkeypatch.setattr(main, "_resolve_archived_readability_html", lambda f, e: None)
    monkeypatch.setattr(main, "fetch_readability_article", lambda url: ("T", "<p>LIVE</p>"))
    assert "LIVE" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")

    monkeypatch.setattr(main, "fetch_readability_article",
                        lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(main, "get_entry_detail", lambda f, e: {"content_html": "<p>STORED</p>"})
    assert "STORED" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")


# --- Feeds scope --------------------------------------------------------------

_FEEDS_CTX = {
    "folder_nodes": [{"label": "All Feeds", "glyph": "☰", "href": "/read?scope=feeds", "count": 5, "active": True}],
    "tag_nodes": [], "archive_node": None,
    "list_items": [{"title": "Item", "subtitle": "Feed", "read": False, "href": "/read?scope=feeds&feed_url=f&entry_id=e"}],
    "selected_label": "All Feeds", "node_selected": True, "search_query": "",
    "tags_open": False, "scope": "feeds", "exit_href": "/?full=1", "static_asset_version": "t",
}


def test_feeds_scope_uses_unread_not_starred(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: seen.update(k) or [])
    monkeypatch.setattr(main, "_build_feeds_mode_context", lambda *a, **k: dict(_FEEDS_CTX))
    with TestClient(_app()) as client:
        r = client.get("/read", params={"scope": "feeds", "folder_id": "5"})
    assert r.status_code == 200 and "All Feeds" in r.text
    assert seen["star_only"] is False and seen["read_filter"] == "unread" and seen["archived"] is None


def test_feeds_reader_hides_saved_actions(monkeypatch):
    _patch_read(monkeypatch, backlog=[_rec(1), _rec(2)])
    with TestClient(_app()) as client:
        r = client.get("/read", params={"scope": "feeds", "feed_url": "feed1", "entry_id": "e1"})
    body = r.text
    assert r.status_code == 200 and "reader-columns" in body
    assert "id='reader-archive-btn'" not in body and "id='reader-delete-btn'" not in body
    assert "scope=feeds" in body  # prev/next carry the feeds scope


# --- Supernote e-ink auto-detect (home redirect) ------------------------------

def _home_app():
    app = FastAPI()
    app.add_api_route("/", main.home, methods=["GET"])
    return app


_SUPERNOTE_UA = "Mozilla/5.0 (Linux; Android 11; Supernote Nomad Build/RQ2A; wv) Chrome/96 Safari/537.36"


def test_supernote_redirects_to_feeds_read_mode():
    with TestClient(_home_app()) as client:
        r = client.get("/", headers={"User-Agent": _SUPERNOTE_UA}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/read?scope=feeds"


def test_supernote_full_opt_out_sets_cookie(monkeypatch):
    from fastapi.responses import PlainTextResponse
    monkeypatch.setattr(main, "_home_inner", lambda **k: PlainTextResponse("app"))
    with TestClient(_home_app()) as client:
        r = client.get("/", params={"full": "1"}, headers={"User-Agent": _SUPERNOTE_UA}, follow_redirects=False)
    assert r.status_code == 200 and "lectio_full_app" in r.headers.get("set-cookie", "")


def test_non_supernote_not_redirected(monkeypatch):
    from fastapi.responses import PlainTextResponse
    monkeypatch.setattr(main, "_home_inner", lambda **k: PlainTextResponse("app"))
    with TestClient(_home_app()) as client:
        r = client.get("/", headers={"User-Agent": "Mozilla/5.0 Chrome/120"}, follow_redirects=False)
    assert r.status_code == 200


def test_tag_panel_ships_the_whole_vocabulary(monkeypatch):
    """Filing from an e-ink device means tapping, not typing.

    The panel needs every tag name in the library up front so it can render
    them as toggles; only the "+ New" field needs a keyboard.
    """
    _patch_read(
        monkeypatch, backlog=[_rec(2)],
        manual_tags=("humour",), all_tag_names=("comics", "humour", "linux"),
    )
    with TestClient(_app()) as client:
        body = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2"}).text

    assert "id='reader-tag-btn'" in body
    assert "id='reader-tag-panel'" in body
    assert "__READER_TAGS__" in body
    # Vocabulary and current selection both travel as inline JSON — nothing the
    # panel acts on is read back out of the DOM.
    assert '"all": ["comics", "humour", "linux"]' in body
    assert '"current": ["humour"]' in body
    # The count rides on the button so it is visible without opening the panel.
    assert ">#1</button>" in body


def test_tag_panel_absent_in_the_feeds_scope(monkeypatch):
    """Tagging is a Saved-scope action, alongside Archive/Delete."""
    _patch_read(monkeypatch, backlog=[_rec(2)], manual_tags=(), all_tag_names=("x",))
    with TestClient(_app()) as client:
        body = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2",
                                           "scope": "feeds"}).text
    assert "id='reader-tag-btn'" not in body
    assert "id='reader-tag-panel'" not in body


# --- archived-copy plausibility -------------------------------------------
def test_a_failed_extraction_does_not_beat_the_feed_content(monkeypatch):
    """An implausibly thin archived copy is a FAILED extraction, not a short post.

    readability sometimes locks onto a sidebar widget instead of the body:
    illogicalcontraption's 2011 entry archived as a 168-byte "Contact:" block while
    the feed held 9,208 bytes of the actual post. Because the archive won
    unconditionally, the article read as empty online and offline alike — 520 of
    6,000 archived copies on the live library are in this state (8.7%).
    """
    monkeypatch.setattr(main, "_resolve_archived_readability_html",
                        lambda f, e: '<div><h2>Contact:</h2><p>a(at)b.com</p></div>')
    monkeypatch.setattr(main, "get_entry_detail",
                        lambda f, e: {"content_html": "<p>" + ("real prose " * 80) + "</p>"})
    monkeypatch.setattr(main, "_prepend_reader_lead_image", lambda f, e, h: h)

    out = main.resolve_reader_article_html("feed", "entry", "")
    assert "real prose" in out
    assert "Contact:" not in out


def test_a_picture_post_archive_is_plausible(monkeypatch):
    """Images count as substance in their own right. A comic or photo post is
    legitimately almost text-free — the illogicalcontraption entry is 16 images and
    57 characters — so a text-length-only test would call every one of them a
    failed extraction."""
    comic = '<p>(Click for full size)</p><img src="a.jpg"><img src="b.jpg">'
    assert main._archived_copy_is_plausible(comic)
    assert not main._archived_copy_is_plausible('<div><h2>Contact:</h2><p>a(at)b.com</p></div>')


def test_images_outweigh_text_when_choosing_a_copy():
    """Images lead in the comparison because the failure being corrected is a text
    widget beating a picture post — and on that comparison a text-length test came
    down to 57 characters against 47, which is not a margin to trust."""
    widget = "<div><h2>Contact:</h2><p>illogicalcontraption(at)yahoo(dot)com</p></div>"
    pictures = '<p>(Click individual images for full size)</p>' + ('<img src="x.jpg">' * 16)
    assert main._reader_copy_is_richer(pictures, widget)
    assert not main._reader_copy_is_richer(widget, pictures)


def test_a_thin_archive_is_still_used_when_nothing_is_richer(monkeypatch):
    """A genuinely short article must still render: the thin copy is the fallback,
    not a reason to show an error."""
    monkeypatch.setattr(main, "_resolve_archived_readability_html",
                        lambda f, e: "<p>A very short but real post.</p>")
    monkeypatch.setattr(main, "get_entry_detail", lambda f, e: {"content_html": ""})
    monkeypatch.setattr(main, "_prepend_reader_lead_image", lambda f, e, h: h)

    out = main.resolve_reader_article_html("feed", "entry", "")
    assert "A very short but real post." in out


# --- subscriber-only stub detection ---------------------------------------
def test_paywalled_stub_is_recognized():
    """Substack marks paid posts NOWHERE in its feed — no category, no audience
    field. What it ships is a body containing only a "Read more" link back to the
    post. Measured on abortretry.fail: 17 of 20 items were 9-character stubs
    against three real posts of 19k-38k characters.
    """
    stub = '<p> <a href="https://www.abortretry.fail/p/matrox"> Read more </a> </p>'
    assert main.is_paywall_stub(stub, "https://www.abortretry.fail/p/matrox")


def test_a_short_post_with_outbound_links_is_not_a_stub():
    """Requiring the link to point at the ENTRY'S OWN URL is what keeps this off a
    genuinely short post: a two-line link roundup points elsewhere."""
    roundup = '<p>See <a href="https://other.test/x">this</a> today.</p>'
    assert not main.is_paywall_stub(roundup, "https://x.test/a")
    assert not main.is_paywall_stub("<p>Short note, no links.</p>", "https://x.test/a")
    assert not main.is_paywall_stub("<p>" + ("word " * 300) + "</p>", "https://x.test/a")
    assert not main.is_paywall_stub(None, "https://x.test/a")
