"""Auto-discovery must not announce itself as a feed crawler.

chickensoft.games returns a fabricated HTTP 404 to any user agent containing the
phrase "RSS auto-discovery", while serving 200 to "Lectio/1.0" and to the honest
UA the rest of the app uses. Discovery was the only part of Lectio sending that
phrase, so it was the only part that could not read the site.

The failure did not stop at "no feed found". probe_url reported "HTTP 404 —
server denied the request", refusal_is_forceable() classified that as the site
refusing us, and Add Feed offered "Subscribe anyway" — the husk-feed path the
add-feed code explicitly warns about — instead of offering a page feed. A
self-inflicted wrong remedy.
"""
from __future__ import annotations

from services import feed_discovery
from services.reader_api import _HONEST_USER_AGENT


def test_discovery_uses_the_apps_honest_identity():
    assert feed_discovery._HEADERS["User-Agent"] == "Lectio/1.0 (+https://github.com/joshg253/Lectio)"


def test_the_user_agent_still_identifies_lectio_and_links_the_repo():
    """Dropping the activity description must not turn this into a disguise —
    it still says who we are and where to complain."""
    ua = feed_discovery._HEADERS["User-Agent"]
    assert ua.startswith("Lectio/")
    assert "github.com/joshg253/Lectio" in ua


def test_no_crawler_phrasing_that_filters_match_on():
    ua = feed_discovery._HEADERS["User-Agent"].lower()
    for phrase in ("auto-discovery", "autodiscovery", "crawler", "spider", "bot"):
        assert phrase not in ua, f"{phrase!r} in the discovery UA invites a fake 404"


def test_it_matches_the_identity_used_for_feed_fetches():
    """Discovery and fetching should look like the same client, or a site can
    accept one and refuse the other for no reason the user can see."""
    assert feed_discovery._HEADERS["User-Agent"].split("/")[0] == _HONEST_USER_AGENT.split("/")[0]
    assert "github.com/joshg253/Lectio" in _HONEST_USER_AGENT
