"""Three follow-ups from the 2026-08-02 reading session.

- Standard Ebooks releases arrived with no cover: the feed is one sentence with
  an epub enclosure and no <img>, the page has a proper og:image, and the feed is
  classified 'inline' — which suppressed the page scrape with no fallback.
- A DeviantArt thumbnail never appeared while the same image rendered fine in the
  article: the thumbnail proxy re-fetched the expired signed URL instead of using
  bytes the image proxy already held.
- Re-fetch showed up on the right-click menu straight after starring a post but
  not after tagging it, even though both are keep signals.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import main
from services import lead_images

ROOT = Path(__file__).resolve().parent.parent.parent


# --- the lead-image strategy hint must not be a dead end -----------------


def test_inline_strategy_falls_back_to_the_source_page():
    """`skip_source` is an optimization, not a verdict. A feed whose entries are
    usually inline can still have one with no image at all, and then the page is
    the only place a cover can come from."""
    src = inspect.getsource(lead_images.LeadImageService.resolve_entry_lead_image_url)
    tail = src[src.rindex("_plugin_fallback_lead_image_url"):]
    assert "skip_source" in tail, "the skip_source path has no fallback after inline fails"
    assert "_fetch_source_lead_image" in tail


class _Entry:
    def __init__(self, link: str) -> None:
        self.link = link
        self.feed_url = "https://standardebooks.org/rss/new-releases"
        self.id = link


def _inline_strategy_service(monkeypatch, *, scraped: str | None):
    """A service classified 'inline' for this feed, with the network stubbed."""
    svc = lead_images.LeadImageService.__new__(lead_images.LeadImageService)
    svc._cache = {}
    svc._debug_bypass_feeds = set()
    svc._none_strategy_feeds = set()
    svc._webcomic_feeds = set()
    monkeypatch.setattr(svc, "get_feed_strategy", lambda f: ("inline", 0.0, False), raising=False)
    monkeypatch.setattr(svc, "_is_feed_webcomic", lambda f: False, raising=False)
    monkeypatch.setattr(svc, "_plugin_fallback_lead_image_url", lambda **k: None, raising=False)
    monkeypatch.setattr(svc, "_plugin_should_skip_source_lookup", lambda **k: False, raising=False)
    monkeypatch.setattr(svc, "_should_bypass_cached_url", lambda **k: False, raising=False)
    calls: list[str] = []

    def _scrape(link, is_webcomic=False):
        calls.append(link)
        return scraped

    monkeypatch.setattr(svc, "_fetch_source_lead_image", _scrape, raising=False)
    return svc, calls


def test_an_imageless_entry_on_an_inline_feed_gets_the_pages_cover(monkeypatch):
    """The Standard Ebooks case end to end: no <img> anywhere in the entry, feed
    classified 'inline', and the page has the cover."""
    cover = "https://standardebooks.org/images/covers/x/hero@2x.jpg"
    svc, calls = _inline_strategy_service(monkeypatch, scraped=cover)
    got = svc.resolve_entry_lead_image_url(
        _Entry("https://standardebooks.org/ebooks/joseph-conrad/suspense"),
        "A young Englishman stumbles upon love and political intrigue.", None,
    )
    assert got == cover
    assert calls, "the source page was never scraped"


def test_an_inline_image_still_wins_and_costs_no_fetch(monkeypatch):
    """The optimization must survive the fix: when the feed DOES carry an image,
    the page is not scraped at all."""
    svc, calls = _inline_strategy_service(monkeypatch, scraped="https://e.com/og.jpg")
    got = svc.resolve_entry_lead_image_url(
        _Entry("https://e.com/post"), '<p>x</p><img src="https://e.com/inline.jpg">', None,
    )
    assert got == "https://e.com/inline.jpg"
    assert calls == [], "inline image found — the page should not have been fetched"


def test_the_fallback_still_respects_the_negative_cache_and_plugins():
    """A recorded 'no image here' result and a plugin's skip both mean don't
    fetch. The fallback must not become a way around either."""
    src = inspect.getsource(lead_images.LeadImageService.resolve_entry_lead_image_url)
    tail = src[src.rindex("_plugin_fallback_lead_image_url"):]
    assert "not cached_negative" in tail
    assert "_plugin_should_skip_source_lookup" in tail


# --- the thumbnail proxy uses the bytes we already have ------------------


def test_the_thumbnail_proxy_consults_the_image_byte_cache():
    """The image proxy's cache keys on the URL with signing params stripped, so
    it answers after the token dies. The thumbnail path did not consult it: it
    re-fetched the dead URL, got a 401, and marked the host failed — a post whose
    image displayed fine and whose thumbnail never appeared, permanently."""
    src = inspect.getsource(main.thumbnail_proxy)
    assert "_img_cache_get" in src
    assert "_img_cache_key_url" in src


def test_a_cached_image_is_thumbnailed_even_when_its_host_is_failing():
    """The recently-failed short-circuit has to come after the cache lookup, or
    the bug survives: the host IS failing (expired token), which is exactly when
    the cached bytes are the only way to get a thumbnail."""
    src = inspect.getsource(main.thumbnail_proxy)
    cache_at = src.index("_img_cache_get")
    guard_at = src.index("_thumb_fetch_recently_failed")
    assert cache_at < guard_at
    assert "raw is None and _thumb_fetch_recently_failed" in src


def test_the_network_fetch_is_skipped_when_the_cache_answered():
    src = inspect.getsource(main.thumbnail_proxy)
    assert re.search(r"if raw is None:\s*\n\s*try:", src)


# --- kept state reaches the row you right-click --------------------------


def _app_js() -> str:
    return (ROOT / "static" / "js" / "app.js").read_text()


def test_tagging_syncs_the_list_rows_kept_flag():
    """The menu reads data-post-kept off the LIST ROW at open time. Tagging only
    re-rendered the entry pane, so the row stayed stale and Re-fetch was hidden
    on a post that had just been tagged."""
    js = _app_js()
    assert "function applyPostItemKeptState" in js
    assert js.count("syncKeptFromTagResponse(entryTagsForm, data)") == 2, \
        "both the add and the remove handler must sync"


def test_removing_a_tag_from_a_starred_post_leaves_it_kept():
    """Kept is starred OR tagged. Clearing the last tag off a still-starred post
    must not un-keep it and take Re-fetch away."""
    js = _app_js()
    fn = js[js.index("function applyPostItemKeptState"):]
    fn = fn[:fn.index("\n    }")]
    assert "data-post-saved" in fn


def test_kept_comes_from_the_servers_reply_not_the_typed_text():
    """The server normalizes and caps; what was typed is not what was stored."""
    js = _app_js()
    fn = js[js.index("function syncKeptFromTagResponse"):]
    fn = fn[:fn.index("\n    }")]
    assert "data?.tags" in fn


# --- auto-refetch on keep is deliberately narrow -------------------------


def test_auto_refetch_only_fires_for_a_thin_stored_copy():
    """Re-fetching a good copy can only make it worse — the live page may now be
    a paywall, a 404, or a readability miss. Overwriting at the moment the reader
    marked it worth keeping is the failure this avoids."""
    src = inspect.getsource(main._maybe_autofetch_on_keep)
    assert "_archived_copy_is_plausible" in src
    # ...and bails out when it IS plausible, rather than merely mentioning it.
    body = src[src.index('"""', src.index('"""') + 3) + 3:]
    assert re.search(r"if _archived_copy_is_plausible\(stored\):\s*\n\s*return", body)


def test_auto_refetch_is_wired_to_the_routes_not_the_tag_service():
    """set_manual_tags_for_entry is also driven by the feed auto-taggers, at
    ingest, across everything a refresh just delivered — hooking it would turn
    one refresh into a burst of outbound requests at one host."""
    assert "_maybe_autofetch_on_keep" not in inspect.getsource(main.set_manual_tags_for_entry)
    assert "_maybe_autofetch_on_keep" in inspect.getsource(main.set_entry_manual_tags)
    assert "_maybe_autofetch_on_keep" in inspect.getsource(main.toggle_entry_saved)


def test_auto_refetch_skips_lectio_captures():
    """Those were fetched from the page already; there is no feed teaser to
    improve on."""
    src = inspect.getsource(main._maybe_autofetch_on_keep)
    assert "is_saved_articles_feed" in src


def test_auto_refetch_does_not_fire_on_unstar_or_on_clearing_tags():
    star = inspect.getsource(main.toggle_entry_saved)
    assert re.search(r"if saved:\s*\n\s*_maybe_autofetch_on_keep", star)
    tags = inspect.getsource(main.set_entry_manual_tags)
    assert re.search(r"if tags:\s*\n\s*_maybe_autofetch_on_keep", tags)


def test_auto_refetch_runs_under_the_tenancy_helper():
    """A bare thread loses the tenancy user and would fetch as the default one."""
    src = inspect.getsource(main._maybe_autofetch_on_keep)
    assert "_run_in_user_context" in src
    assert "tenancy.current_user_id()" in src
