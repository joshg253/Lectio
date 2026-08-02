"""Follow-ups to the 2026-08-02 fixes, all from the same reading session.

- Tagging a Whisky Advocate cocktail post neither auto-refetched nor re-fetched by
  hand: the page is titled "Charred Garden Smash" while its URL slug reads
  peated-whisky-cocktail-for-summer, so the different-article guard saw zero
  overlap and refused. One bug, two symptoms — the log confirmed the automatic
  attempt had fired and hit the same refusal.
- A Gunnerkrigg strip showed its image in the article and no thumbnail in the
  list.
- Auto-refetch fired at DeviantArt, which answers this server with 403 every
  time, on every tag.
"""
from __future__ import annotations

import inspect
import re

import main
from services import lead_image_plugins as plugins
from services import saved_articles as sa

# --- the different-article guard consults the body -----------------------


def test_a_descriptive_slug_and_a_specific_title_are_not_a_mismatch():
    """The reported case. The slug describes the article
    (peated-whisky-cocktail-for-summer); the page titles itself after the drink
    (Charred Garden Smash). Zero title overlap is routine, not evidence."""
    url = "https://whiskyadvocate.com/peated-whisky-cocktail-for-summer"
    body = ("<p>This peated whisky cocktail is built for summer: falernum, lime "
            "and pineapple, kissed by light peat smoke.</p>")
    assert sa._page_is_a_different_article(
        url, "Charred Garden Smash", old_title="Peated Whisky Cocktail for Summer",
        new_html=body) is False


def test_a_parked_page_is_still_refused():
    """The failure the guard exists for: the-digital-reader served an unrelated
    marketing page for a 2019 post and the re-fetch destroyed the stored copy.
    The body must not rescue it — a parked page discusses none of the subject."""
    url = "https://the-digital-reader.com/2019/01/22/33-ornament-dingbat-and-other-decorative-fonts/"
    body = "<h1>Empowering Relationships</h1><p>Build better connections today. Contact us.</p>"
    assert sa._page_is_a_different_article(
        url, "Empowering Relationships", old_title="33 Ornament Dingbat Fonts",
        new_html=body) is True


def test_the_body_check_needs_a_real_subject_word_not_boilerplate():
    """Overlap has to come from the page's own text. A body that happens to be
    empty gives the guard nothing, and it must still refuse."""
    url = "https://example.com/2019/01/22/ornament-dingbat-decorative-fonts/"
    assert sa._page_is_a_different_article(
        url, "Empowering Relationships", old_title="Ornament Dingbat Fonts",
        new_html="") is True


def test_visible_text_strips_markup_so_tag_names_cannot_match():
    """Without stripping, a slug word like 'video' or 'article' would match the
    HTML's own tag and attribute names and neuter the guard."""
    assert "div" not in sa._visible_text('<div class="video">hello</div>').split()
    assert "hello" in sa._visible_text('<div class="video">hello</div>')


def test_the_body_scan_is_capped():
    """A cost guard: a slug word absent from the first several thousand
    characters is not what 'this page is about' looks like."""
    src = inspect.getsource(sa._visible_text)
    assert re.search(r"html_text\[:\d+\]", src)


# --- the same file, referred to two ways ---------------------------------


def _gk():
    return next(p for p in [plugins.GunnerkriggPlugin()] if p)


def test_a_cache_buster_does_not_make_it_a_different_image():
    """Gunnerkrigg serves the panel with ?v=<timestamp>. An exact compare called
    the very image this plugin derives 'not preferred' and bypassed it — so the
    article rendered the picture while the list thumbnail came back empty."""
    assert _gk().should_bypass_cached_url(
        entry_link="http://www.gunnerkrigg.com/?p=3289",
        cached_url="https://www.gunnerkrigg.com/comics/00003289.jpg?v=1785135600") is False


def test_the_scheme_does_not_make_it_a_different_image():
    """The subtler half: the derived URL inherits the ENTRY LINK's scheme, and
    this feed still publishes http:// links for images served over https."""
    assert _gk().should_bypass_cached_url(
        entry_link="http://www.gunnerkrigg.com/?p=3289",
        cached_url="https://www.gunnerkrigg.com/comics/00003289.jpg") is False


def test_a_genuinely_wrong_image_is_still_bypassed():
    """The plugin's whole job. Site chrome and the wrong strip must both lose."""
    gk = _gk()
    link = "http://www.gunnerkrigg.com/?p=3289"
    assert gk.should_bypass_cached_url(
        entry_link=link, cached_url="https://www.gunnerkrigg.com/images/site_logo.png") is True
    assert gk.should_bypass_cached_url(
        entry_link=link, cached_url="https://www.gunnerkrigg.com/comics/00003288.jpg") is True


def test_the_cached_url_is_compared_not_rewritten():
    """Only the comparison ignores scheme/query. Rewriting the URL itself would
    be the ComicControl mistake documented in _promote_known_thumbnail: a
    reconstructed URL can name a file that does not exist and get a 200
    placeholder back."""
    src = inspect.getsource(plugins.GunnerkriggPlugin.should_bypass_cached_url)
    assert "return" in src and "_same_file_key" in src
    # It returns a bool comparison, never a URL.
    assert "return _same_file_key(cached_url) != _same_file_key(preferred)" in src


def test_same_file_key_is_not_fooled_by_a_different_host():
    assert plugins._same_file_key("https://a.com/x.jpg") != plugins._same_file_key(
        "https://b.com/x.jpg")


# --- auto-refetch stops asking a host that refused ------------------------


def test_a_refusing_host_is_not_re_asked_on_every_tag():
    """DeviantArt answers this server with 403 every time. Tagging a watchlist
    would otherwise be dozens of requests it has already declined — see the
    good-web-citizen posture."""
    main._autofetch_failed_hosts.clear()
    assert main._autofetch_host_in_cooldown("www.deviantart.com") is False
    main._mark_autofetch_host_failed("www.deviantart.com")
    assert main._autofetch_host_in_cooldown("www.deviantart.com") is True
    assert main._autofetch_host_in_cooldown("whiskyadvocate.com") is False
    main._autofetch_failed_hosts.clear()


def test_the_cooldown_expires():
    main._autofetch_failed_hosts.clear()
    main._autofetch_failed_hosts["x.com"] = 0.0      # already elapsed
    assert main._autofetch_host_in_cooldown("x.com") is False
    assert "x.com" not in main._autofetch_failed_hosts


def test_the_cooldown_table_is_bounded():
    """A politeness memo, not a record."""
    main._autofetch_failed_hosts.clear()
    for i in range(600):
        main._mark_autofetch_host_failed(f"h{i}.example.com")
    assert len(main._autofetch_failed_hosts) <= 512
    main._autofetch_failed_hosts.clear()


def test_only_the_automatic_path_consults_the_cooldown():
    """Manual Re-fetch is a person asking on purpose and must never be blocked."""
    assert "_autofetch_host_in_cooldown" in inspect.getsource(main._maybe_autofetch_on_keep)
    assert "_autofetch_host_in_cooldown" not in inspect.getsource(
        main._refresh_captured_article_for_current_user)
    assert "_autofetch_host_in_cooldown" not in inspect.getsource(
        main.refresh_saved_article_content)


def test_a_successful_refetch_does_not_pause_the_host():
    src = inspect.getsource(main._maybe_autofetch_on_keep)
    body = src[src.index("def _work"):]
    ok_branch = body[body.index('result.get("ok")'):body.index("_mark_autofetch_host_failed")]
    assert "return" in ok_branch


# --- the image size budget is an Administration setting ------------------


def test_the_size_budget_is_configurable_in_administration():
    """It was env-only; related image-cache settings already live in the admin
    screen and this belongs beside them."""
    assert main.SETTING_IMG_TARGET_BYTES == "img_target_bytes"
    tpl = (main.BASE_DIR / "templates" / "administration.html").read_text()
    assert 'id="adm-img-target-bytes"' in tpl
    assert "img_target_bytes: $('adm-img-target-bytes')" in tpl


def test_the_size_budget_is_admin_only_like_its_neighbours():
    """Instance-level config: a non-admin tenant must not be able to change how
    every user's images are stored."""
    src = inspect.getsource(main.save_settings) if hasattr(main, "save_settings") else ""
    if not src:
        import pathlib
        src = pathlib.Path(main.__file__).read_text()
    admin_only = src[src.index("_ADMIN_ONLY = {"):]
    admin_only = admin_only[:admin_only.index("}")]
    assert "SETTING_IMG_TARGET_BYTES" in admin_only


def test_the_env_value_is_the_fallback_not_the_authority():
    src = inspect.getsource(main.get_img_target_bytes)
    assert "get_instance_setting" in src
    assert "_ENV_IMG_TARGET_BYTES" in src


# --- loading spinners are not lead images --------------------------------


def test_a_loading_spinner_is_rejected():
    """commandlinefu shipped /images/tag-loader.gif as the thumbnail for a shell
    one-liner: the post has no picture of its own, so the spinner was the
    best-scoring image on the page. `spinner` was already listed but only as a
    whole filename, so every `*-loader.gif` walked past it."""
    svc = main.lead_image_service
    for url in ("https://www.commandlinefu.com/images/tag-loader.gif",
                "https://x.com/img/ajax-loader.gif",
                "https://x.com/i/spinner.svg",
                "https://x.com/assets/loading.gif",
                "https://x.com/assets/preloader.png",
                "https://x.com/i/loader.gif?v=3"):
        assert svc._is_image_url_acceptable(url, None, None) is False, url


def test_a_real_photo_that_merely_contains_the_word_is_kept():
    """The discriminator is position, not presence: a loading indicator is named
    for what it is and the word sits against the extension, while a photograph is
    named for its subject and carries on afterwards. A bare substring rule
    rejected `front-loader-review.jpg`, which is a picture of a tractor."""
    svc = main.lead_image_service
    for url in ("https://x.com/2026/front-loader-review.jpg",
                "https://x.com/photos/downloading-vinyl.jpg",
                "https://x.com/img/uploader-guide.png",
                "https://x.com/a/busy-street-market.jpg",
                "https://x.com/media/cover-art.jpg"):
        assert svc._is_image_url_acceptable(url, None, None) is True, url


# --- re-fetch is available everywhere ------------------------------------


def test_the_refetch_menu_gate_is_link_only():
    """It used to require the post to be kept, so repairing a truncated article
    meant tagging it first — filing something you may not want filed just to read
    it. The pin that makes a re-fetch stick is applied whether or not anything
    keeps the entry, so the gate was guarding an already-handled hazard."""
    js = (main.BASE_DIR / "static" / "js" / "app.js").read_text()
    fn = js[js.index("const postCanRefetch ="):]
    fn = fn[:fn.index(";")]
    assert "contextPostLink" in fn, "a link is required — it is what gets fetched"
    for stale in ("contextPostSaved", "contextPostKept", "contextPostCaptured", "SAVED_FEED_URL"):
        assert stale not in fn, f"{stale} should no longer gate re-fetch"


def test_the_rules_list_keeps_its_scroll_position():
    """Toggling a rule half way down the list rebuilt the whole list and threw
    you back to the top. The rules list has no overflow of its own — the settings
    panel is the scroller — so the fix has to walk up to the real one."""
    js = (main.BASE_DIR / "static" / "js" / "app.js").read_text()
    assert "function hlScrollParent" in js
    fn = js[js.index("function hlRenderRules"):]
    assert "const keepScrollTop" in fn[:1200]
    assert fn.count("restoreScroll()") >= 2, "the empty-list early return needs it too"
