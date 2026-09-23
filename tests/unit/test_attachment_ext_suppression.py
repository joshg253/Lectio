"""A suggested file type must be dismissible per feed.

The scanner reads the last dot-segment of a link path, so a bare domain leaves its TLD behind — ".il"
(Israel) was offered as a file type. No fixed blocklist fixes this: ".zip" and ".mov" are real TLDs and
real file types at once, so the user has to be able to say "not this one, not here".
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "main.py").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
# change_feed_url_route (and its _feed_url_tables list) moved to routes/feeds.py
# in Stage 8D of the main.py route-by-URL-prefix split; feed_attachment_candidates_route/
# suppress_feed_attachment_candidate_route moved there too in Stage 8E.
ROUTES_FEEDS = (ROOT / "routes" / "feeds.py").read_text()


def test_table_is_created_in_the_shared_schema():
    """ensure_meta_schema runs for every existing tenant at startup; a table added anywhere else is
    missing from already-provisioned users and surfaces as a 'no such table' 500."""
    assert "CREATE TABLE IF NOT EXISTS suppressed_feed_attachment_exts" in MAIN
    schema_start = MAIN.index("def ensure_meta_schema()")
    assert MAIN.index("suppressed_feed_attachment_exts") > schema_start


def test_scan_filters_dismissed_extensions():
    body = MAIN[MAIN.index("def scan_feed_attachment_extensions") :]
    body = body[: body.index("\ndef ", 10)]
    assert "dismissed = suppressed_attachment_exts(feed_url)" in body
    assert "or ext in dismissed" in body


def test_suppress_route_round_trips_and_returns_fresh_lists():
    # feed_attachment_candidates_route/suppress_feed_attachment_candidate_route
    # moved to routes/feeds.py in Stage 8E of the main.py route-by-URL-prefix split.
    assert '@router.post("/feeds/attachment-candidate-suppress")' in ROUTES_FEEDS
    route = ROUTES_FEEDS[ROUTES_FEEDS.index('@router.post("/feeds/attachment-candidate-suppress")') :]
    route = route[: route.index("\n@router.")]
    # Restore has to be reachable, not just dismissal.
    assert 'suppressed: str = Form("1")' in route
    assert '{"0", "false", "no", ""}' in route
    assert "suppressed_attachment_ext_list(feed_url)" in route


def test_candidates_route_reports_what_was_dismissed():
    route = ROUTES_FEEDS[ROUTES_FEEDS.index('@router.get("/feeds/attachment-candidates")') :]
    route = route[: route.index("\n@router.post")]
    assert "suppressed_attachment_ext_list(feed_url)" in route


def test_ui_offers_dismiss_and_restore():
    assert "feed-prop-ext-chip-drop" in APP_JS
    assert "suppressFeedPropAttachmentExt(feedUrl, row.ext, true" in APP_JS
    assert re.search(r"suppressFeedPropAttachmentExt\(feedUrl, ext, false", APP_JS)


def test_dismissals_follow_a_feed_url_rewrite():
    """A feed whose URL is rewritten keeps its rows in every other per-feed table; leaving the suppression
    tables out meant every chip the user had waved off silently came back."""
    table_list = ROUTES_FEEDS[ROUTES_FEEDS.index("_feed_url_tables = [") :]
    table_list = table_list[: table_list.index("]")]
    assert '"suppressed_feed_attachment_exts"' in table_list
    assert '"suppressed_feed_tags"' in table_list, "the table this one mirrors had the same gap"
