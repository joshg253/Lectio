"""Save arbitrary web articles into a per-user local "Saved Articles" feed.

Instapaper-style read-later capture for pages that don't come from any
subscribed feed. Articles live as user-added entries (``added_by='user'``,
protected from updates by the reader library) in a synthetic local feed that
is never fetched (``updates_enabled=False``), so the whole existing pipeline —
read state, tags, keyboard flows, the Saved/Starred view, and the starred
archive's offline capture — applies to them with no special-casing.

Saving a URL:
  1. fetches + readability-extracts the page server-side (injected callable,
     so this module stays free of main.py's extraction internals);
  2. adds the entry (id = link = the article URL, published = save time);
  3. stars it (``saved_entries`` row) and enqueues the starred-archive
     capture, which independently persists the source page and its images.

Extraction failure is not fatal: the entry is still created (title falls back
to the URL) and starred, so the archive worker can capture the page later.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote_plus, urldefrag, urlparse

LOGGER = logging.getLogger(__name__)

# "We do not know when this was published." Deliberately a real date rather than
# NULL: entry_effective_date falls back to the received date, so NULL would just
# re-substitute the import/capture time. See the note at its use sites.
UNKNOWN_PUBLISHED = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Statuses that mean the host refused *us*, not that the article is missing.
# Kept distinct from 404/410 because those flag the entry as dead and offer a
# delete — a bot-wall must never be able to trigger that for a live article.
_BLOCKED_STATUSES = frozenset({401, 403, 429, 451, 503})

SAVED_FEED_URL = "lectio:saved"
SAVED_FEED_TITLE = "Saved Articles"


def is_saved_articles_feed(feed_url: str) -> bool:
    return feed_url == SAVED_FEED_URL


def normalize_article_url(url: str) -> str | None:
    """Clean a user-supplied article URL, or None if it isn't http(s).

    The fragment is dropped so a bookmarklet save of ``page#section`` and a
    pasted ``page`` land on the same entry id."""
    url = (url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urldefrag(url)[0]


def ensure_saved_feed(reader) -> bool:
    """Create the local Saved Articles feed if missing. Returns True if created.

    ``lectio:`` isn't a fetchable scheme, so the feed is added with
    ``allow_invalid_url`` and updates disabled — the refresh scheduler and
    reader's updater never touch it; entries only arrive via save_article()."""
    if reader.get_feed(SAVED_FEED_URL, None) is not None:
        return False
    reader.add_feed(SAVED_FEED_URL, allow_invalid_url=True)
    reader.disable_feed_updates(SAVED_FEED_URL)
    reader.set_feed_user_title(SAVED_FEED_URL, SAVED_FEED_TITLE)
    return True


# Duplicated rather than imported from main: services must not import the app
# module. Kept next to its only use so the two cannot drift unnoticed.
_MANUAL_TAG_KEY_PREFIX = "lectio.manual_tag."


# Words too common to count as evidence that two titles are about the same thing.
_TITLE_STOPWORDS = frozenset({
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "at", "by", "with",
    "from", "is", "are", "be", "your", "you", "how", "what", "why", "it", "its",
    "this", "that", "or", "as", "vs", "new", "part", "free",
})


def _title_words(value: str) -> set[str]:
    """Significant lowercase words of a title, site suffix included — a shared
    " - The Digital Reader" must not by itself make two titles look related."""
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    return {w for w in words if len(w) > 2 and w not in _TITLE_STOPWORDS}


# Structural URL vocabulary: file extensions and CMS path furniture. These carry
# no subject, and leaving them in is how informit.com defeated this guard —
# /articles/article.aspx overlapped the site index title "Articles | InformIT",
# so a wrong page read as the right one.
_URL_STRUCTURAL_WORDS = frozenset({
    "article", "articles", "index", "default", "page", "pages", "post", "posts",
    "item", "items", "view", "show", "story", "stories", "content", "detail",
    "details", "print", "amp", "html", "htm", "aspx", "php", "asp",
    "cfm", "jsp", "shtml", "cgi", "www", "web", "site", "blog",
})


def _url_slug_words(url: str) -> set[str]:
    """Significant words from a URL's path AND its query VALUES.

    The STORED title cannot be the reference: re-fetch exists partly to replace a
    bad capture's title ("Stale Listing Page" -> the real one), so comparing old
    against new refuses exactly the case the feature is for. A URL does not change
    when a site starts serving a parked page over it.

    Query values matter as much as the path. Plenty of CMS URLs are
    ``/articles/article.aspx?p=2432250&WT.rss_a=Working+with+the+PowerShell…`` —
    the path is furniture and the subject is in the query, so reading the path
    alone left nothing to compare against but boilerplate.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return set()
    text = parsed.path
    if parsed.query:
        # Values only: keys are parameter names, never subject.
        for _key, value in parse_qsl(parsed.query, keep_blank_values=False):
            text += " " + unquote_plus(value)
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {
        w for w in words
        if len(w) > 2 and w not in _TITLE_STOPWORDS and w not in _URL_STRUCTURAL_WORDS
    }


_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# A page that is mostly links, in quantity. Calibrated against 1,192 real captured
# articles: the 95th percentile of anchor-text ratio is 0.32 and the 98th is 0.46,
# so 0.40 with a 20-anchor floor sits in the tail rather than the body of the
# distribution. It still catches genuine link roundups, which is why this is never
# used alone — only in conjunction with a title check below.
_LINK_INDEX_MIN_ANCHORS = 20
_LINK_INDEX_MIN_RATIO = 0.40


def looks_like_a_link_index(html: str) -> bool:
    """Is this page a list of links to other articles rather than an article?

    Section indexes, "more in this category" pages and site maps all share the
    shape: many anchors, and most of the text lives inside them.
    """
    if not html:
        return False
    anchors = _ANCHOR_RE.findall(html)
    if len(anchors) < _LINK_INDEX_MIN_ANCHORS:
        return False
    text_len = len(_TAG_RE.sub("", html))
    if text_len < 200:
        return False
    anchor_text = sum(len(_TAG_RE.sub("", a)) for a in anchors)
    return (anchor_text / text_len) >= _LINK_INDEX_MIN_RATIO


def _page_is_a_different_article(source_url: str, new_title: str, *,
                                 old_title: str = "", new_html: str = "") -> bool:
    """True when the page fetched from *source_url* is plainly not what lives there.

    the-digital-reader served a parked "Empowering Relationships" page for a 2019
    post; its slug (33-ornament-dingbat-and-other-decorative-fonts…) shares no word
    with that title, while a real article's title almost always echoes its own slug.

    Deliberately conservative — refusing a legitimate re-fetch is a nuisance, while
    accepting a wrong one destroys the stored copy, which is why this fires only on
    ZERO overlap and stands down whenever the slug carries too little to judge
    (opaque ids like ``?p=1524``, date-only paths, short section URLs).
    """
    if not new_title:
        return False
    slug_words = _url_slug_words(source_url)
    # Drop pure numbers: a date path (/2019/01/22/) is not evidence of subject.
    slug_words = {w for w in slug_words if not w.isdigit()}
    title_words = _title_words(new_title)
    if len(title_words) < 2:
        return False
    if len(slug_words) < 3:
        # The slug gave nothing to judge by — an opaque id like
        # ``article.aspx?p=2438407``. This used to stand down entirely, and that
        # hole is how informit's "Articles | InformIT" section index overwrote two
        # stored articles: the only subject word in the URL lived in a *tracking*
        # parameter (``WT.rss_a=Classes in C#``), below the three-word floor.
        #
        # Fall back to the stored title here, which the slug branch deliberately
        # avoids (re-fetch exists partly to fix a bad title, so old-vs-new would
        # refuse the very case the feature is for). Safe only in conjunction: the
        # fetched body must ALSO be shaped like a link index. A genuine article
        # re-fetched under a corrected title is not 20 anchors of mostly-link text,
        # and a genuine link roundup still echoes its own stored title.
        if old_title and looks_like_a_link_index(new_html):
            return not (_title_words(old_title) & title_words)
        return False
    if slug_words & title_words:
        return False
    # The title alone is too thin a reference. Plenty of URLs describe the
    # article generically while the page titles itself something specific:
    # whiskyadvocate.com/peated-whisky-cocktail-for-summer is headed "Charred
    # Garden Smash" — the drink's name, sharing not one word with its own slug.
    # Judging on the title alone refused that re-fetch outright, and refused the
    # automatic one on tagging too.
    #
    # So consult the fetched BODY before refusing. This does not weaken the
    # guard it was built for: a parked "Empowering Relationships" page does not
    # mention ornaments, dingbats or decorative fonts either, and a section index
    # does not discuss the article it replaced. Zero overlap across BOTH the
    # title and the body is a much stronger signal of a genuinely different page
    # than zero overlap with the title, which is routine.
    return not (slug_words & _title_words(_visible_text(new_html)))


def _visible_text(html_text: str) -> str:
    """Tag-stripped text of a fetched page body, capped.

    The cap is a cost guard, not a correctness one: a slug word that appears
    nowhere in the first several thousand characters of an article is not what
    "this page is about" looks like, and scanning a whole page for every
    re-fetch buys nothing.
    """
    if not html_text:
        return ""
    return re.sub(r"<[^>]+>", " ", html_text[:20000])


def _has_manual_tag(reader, feed_url: str, entry_id: str) -> bool:
    """Does this entry carry any manual (user) tag? Read straight from reader's
    entry_tags, the same way this module already writes reader's own columns."""
    try:
        row = reader._storage.get_db().execute(
            "SELECT 1 FROM entry_tags WHERE feed = ? AND id = ? AND key LIKE ? LIMIT 1",
            (feed_url, entry_id, _MANUAL_TAG_KEY_PREFIX + "%"),
        ).fetchone()
    except Exception:  # noqa: BLE001 — treat an unreadable tag table as "no tags"
        LOGGER.warning("manual-tag probe failed for %s", entry_id, exc_info=True)
        return False
    return row is not None


def replace_entry_content(
    reader,
    conn: sqlite3.Connection,
    entry_id: str,
    title: str,
    article_html: str,
    feed_url: str = SAVED_FEED_URL,
    *,
    bump_received: bool = True,
    pin_content: bool = False,
) -> None:
    """Replace a captured article's stored content with a fresh extraction.

    reader has no public setter for entry content (EntryData is ingest-owned),
    so this writes the column directly in reader's own JSON shape. The title
    is only updated when the user hasn't pinned one via Edit title
    (entry_title_overrides).

    *feed_url* defaults to the saved feed but must be passed for an article that
    has been filed onto a real feed: auto-filing moves the entry out of
    ``lectio:saved`` while leaving it a Lectio capture, and re-fetching such an
    article has to update it where it now lives.

    *bump_received* surfaces the re-pulled article at the top of the backlog by
    moving its **Received** date (and saved_at) to now. It used to move
    ``published`` instead, which was wrong twice over: Pub means the date the
    article was published, and a re-fetch does not republish it; and under a
    Pub-oldest sort the bump buried the article at the far end of the list
    rather than surfacing it. Measured on the live library 2026-07-25: 105
    entries had lost their real publish dates this way. Received is the honest
    home for "when this content arrived", and needs no new per-entry field.

    Both received columns move together: ``first_updated`` backs ``Entry.added``
    (what the UI shows and the render-path sort reads) and ``recent_sort`` backs
    the list's SQL fast path. *pin_content* writes an ``entry_content_overrides``
    row so a later feed refresh can't clobber the re-fetched content with the
    feed's own thinner copy — set for feed entries, which reader re-ingests (a
    capture's feed never refreshes)."""
    now = datetime.now(timezone.utc)
    stored_received = now.strftime("%Y-%m-%d %H:%M:%S")  # reader's naive-UTC format
    content_json = json.dumps([{"value": article_html, "type": "text/html", "language": None}])
    db = reader._storage.get_db()
    if bump_received:
        db.execute(
            "UPDATE entries SET content = ?, first_updated = ?, recent_sort = ?"
            " WHERE feed = ? AND id = ?",
            (content_json, stored_received, stored_received, feed_url, entry_id),
        )
    else:
        db.execute(
            "UPDATE entries SET content = ? WHERE feed = ? AND id = ?",
            (content_json, feed_url, entry_id),
        )
    title_pinned = False
    try:
        title_pinned = conn.execute(
            "SELECT 1 FROM entry_title_overrides WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        pass
    if title and not title_pinned:
        db.execute(
            "UPDATE entries SET title = ? WHERE feed = ? AND id = ?",
            (title, feed_url, entry_id),
        )
    db.commit()
    if pin_content:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO entry_content_overrides (feed_url, entry_id, content) "
                "VALUES (?, ?, ?)", (feed_url, entry_id, content_json),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            LOGGER.warning("save-article: content pin failed for %s: %s", entry_id, exc)
    if not bump_received:
        return
    # The saved_at bump is cosmetic ordering — never let a transient lock
    # (e.g. the archive worker writing at the same instant) fail the save
    # after the content is already committed.
    for attempt in (1, 2):
        try:
            conn.execute(
                "UPDATE saved_entries SET saved_at = CURRENT_TIMESTAMP WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )
            conn.commit()
            break
        except sqlite3.OperationalError as exc:
            if attempt == 2:
                LOGGER.warning("save-article: saved_at bump failed for %s: %s", entry_id, exc)
            else:
                time.sleep(0.5)


def read_entry_content_json(reader, feed_url: str, entry_id: str) -> str | None:
    """Return reader's raw ``entries.content`` JSON for an entry, or None.

    The counterpart to replace_entry_content: cleanup edits snapshot this before
    overwriting so the pristine body can be restored verbatim, rather than being
    re-derived (which would lose whatever the feed no longer serves)."""
    row = reader._storage.get_db().execute(
        "SELECT content FROM entries WHERE feed = ? AND id = ?", (feed_url, entry_id),
    ).fetchone()
    return row[0] if row else None  # index access: reader's row_factory is not ours to assume


def restore_entry_content(reader, feed_url: str, entry_id: str, content_json: str) -> None:
    """Write a previously snapshotted content JSON back onto an entry."""
    db = reader._storage.get_db()
    db.execute(
        "UPDATE entries SET content = ? WHERE feed = ? AND id = ?",
        (content_json, feed_url, entry_id),
    )
    db.commit()


def refresh_captured_article(
    reader,
    conn: sqlite3.Connection,
    feed_url: str,
    entry_id: str,
    *,
    extract: Callable[[str], tuple[str, str]],
    enqueue_archive: Callable[[str, str], None] | None = None,
    is_boilerplate_extraction: Callable[[str, str, str], bool] | None = None,
    bump_received: bool | None = None,
) -> dict:
    """Re-fetch and re-extract a Lectio capture in place, wherever it lives.

    *bump_received*, when given, overrides the default is_capture-based
    surfacing decision below — None keeps today's behavior (a capture bumps,
    a feed entry doesn't). A batch re-fetch passes False explicitly: bulk-
    backfilling old articles isn't "look, something changed" the way a
    deliberate single re-fetch is, and bumping 50 saved_at's at once dumped
    the whole Inbox's order onto whatever finished last. Kept as a real
    parameter (not a special case) so a future "Now / Original / Pub date"
    choice on the re-fetch button has something to plug straight into.

    Replaces ``save_article(refresh_content=True)`` as the re-fetch path, and
    fixes two things that one gets wrong:

    **It fetches the entry's current ``link``, not its id.** A capture's id is
    the URL it was *first* saved from and is immutable — it keys the
    ``saved_entries`` star row, manual tags and archive rows. So when Edit URL
    repoints a dead or moved article, only ``link`` moves, and re-fetching by id
    would keep hitting the dead address forever while reporting success.

    **It works off ``lectio:saved`` too.** Auto-filing moves a saved article
    onto the feed that actually publishes it, where it stays a capture
    (``added_by='user'``) but no longer matches the saved-feed gate. Re-saving
    such an article instead of updating it in place would resurrect the
    Uncategorized duplicate that filing removed.

    Also enriches an **ordinary feed entry**: a feed whose content is text-only or
    truncated (paizo, guitarplayer) leaves an article missing its images.
    Re-fetching pulls the full source and *pins* it (entry_content_overrides), so
    the feed re-serving its thinner copy can't clobber it. A feed entry keeps its
    chronological position (no date bump).

    **Available on any entry with a link** (2026-08-02). It used to require the
    entry to be a capture, starred or tagged, on the reasoning that a plain feed
    entry's content is the publisher's — but the practical effect was that fixing
    a truncated post meant first tagging it, which files something you may not
    want filed just to read it properly. The safety that gate was really standing
    in for is the **pin**, and the pin is applied to every non-capture entry
    regardless of whether anything keeps it (``pin_content=not is_capture``
    below), so the refresh cannot silently undo the re-fetch either way.

    The real protections are unconditional and stay: the mismatch guard refuses a
    page that is plainly a different article, and the pre-replacement snapshot in
    ``entry_content_edits`` makes any re-fetch one click to Revert.
    """
    result: dict = {
        "ok": False,
        "error": None,
        "refreshed": False,
        "extracted": False,
        "dead": False,
        "feed_url": feed_url,
        "entry_id": entry_id,
        "title": None,
        "source_url": None,
    }

    entry = reader.get_entry((feed_url, entry_id), None)
    if entry is None:
        result["error"] = "Entry not found."
        return result
    is_capture = str(getattr(entry, "added_by", "") or "") == "user"
    is_starred = conn.execute(
        "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ? LIMIT 1",
        (feed_url, entry_id),
    ).fetchone() is not None
    # Kept-ness no longer gates the re-fetch itself — see the docstring. It still
    # decides whether the result is worth ARCHIVING: the offline capture exists to
    # preserve things you are keeping, and enqueuing one for an entry nothing keeps
    # would leave a capture with no keep signal holding it, which the unstar path
    # then has to clean up.
    is_tagged = _has_manual_tag(reader, feed_url, entry_id)
    is_kept = is_capture or is_starred or is_tagged

    result["title"] = entry.title or entry_id
    source_url = normalize_article_url(str(getattr(entry, "link", "") or "") or entry_id)
    if not source_url:
        result["error"] = "This article has no usable source URL to re-fetch."
        return result
    result["source_url"] = source_url

    try:
        new_title, article_html = extract(source_url)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("refresh-capture: extraction failed for %s: %s", source_url, exc)
        # Duck-type an httpx HTTPStatusError's status without importing httpx
        # here. A 404/410 means the article is gone at the source, so re-fetch
        # will never work — say so, and flag it so the caller can offer to
        # delete rather than leave the user retrying a dead URL.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            result["error"] = f"The source article is gone (HTTP {status}) — nothing to re-fetch."
            result["dead"] = True
        elif status in _BLOCKED_STATUSES:
            # Refused, not absent. Say which, and point at the way through:
            # the extension posts the page your own browser already loaded, so
            # a host that blocks this server (Medium blocks datacenter IPs
            # outright, browser identity or not) is no obstacle.
            result["error"] = (
                f"{urlparse(source_url).netloc or 'The site'} blocked the fetch (HTTP {status}). "
                "Open it in your browser and save it with the Lectio extension instead."
            )
        elif status is not None:
            result["error"] = f"Could not fetch the article (HTTP {status})."
        else:
            result["error"] = "Could not fetch the article."
        return result
    if not article_html:
        result["error"] = "Nothing could be extracted from the page."
        return result

    # ⚠ A 200 does not mean the article is still there. the-digital-reader served
    # a parked "Empowering Relationships" page for a 2019 post, and the re-fetch
    # replaced the stored article AND its title with it — destroying the only copy,
    # since the archive was rewritten too. Refuse when the fetched page is plainly
    # a different article.
    #
    # The URL's own slug is the reference, NOT the stored title: re-fetch exists
    # partly to replace a bad capture's title, so comparing old against new would
    # refuse the very case the feature is for. A slug does not change when a site
    # starts serving a parked page over it.
    if _page_is_a_different_article(source_url, new_title,
                                    old_title=str(getattr(entry, "title", "") or ""),
                                    new_html=article_html):
        result["error"] = (
            "The page now at that URL looks like a different article "
            f"(\u201c{new_title[:60]}\u201d) — the stored copy was left alone. "
            "Use Edit URL if the article moved."
        )
        result["mismatch"] = True
        return result

    # ⚠ A page can be the RIGHT article and still extract to the wrong thing.
    # commandlinefu.com's pages made readability lock onto the site's standing
    # blurb ("is the place to record those command-line gems…"), which replaced
    # the actual command. The guard above cannot see it: the title stayed
    # correct, and the boilerplate is LONGER than the command it replaced, so
    # neither title nor length separates them.
    #
    # What does separate them: site chrome extracts IDENTICALLY for every post
    # on that feed. If this exact text is already stored against a DIFFERENT
    # entry of the same feed, it is not this article's content. Measured on the
    # live archive before this existed: 1,109 entries across 58 feeds had been
    # overwritten with a sibling's extraction.
    if is_boilerplate_extraction is not None:
        try:
            if is_boilerplate_extraction(feed_url, entry_id, article_html):
                result["error"] = (
                    "That page extracted to the same text as other posts in this "
                    "feed — the site's boilerplate, not the article. The stored "
                    "copy was left alone. Try Re-fetch full page."
                )
                result["boilerplate"] = True
                return result
        except Exception:  # noqa: BLE001 — a guard must never break the feature
            LOGGER.debug("boilerplate check failed for %s", entry_id, exc_info=True)

    # Snapshot the body BEFORE replacing it, so a bad re-fetch is one click to
    # undo. Two entries were destroyed this way before this existed — a parked
    # page returning 200, and a URL whose subject lived only in its query string —
    # and each needed a backup dive to recover. The guard above catches what it
    # can recognize; this covers what it cannot.
    #
    # INSERT OR IGNORE keeps the FIRST original, matching the cleanup feature that
    # shares this table: reverting means "as the feed served it", not "as the last
    # re-fetch left it". Reusing the row also means the existing Revert control
    # lights up with no further wiring.
    try:
        _original = read_entry_content_json(reader, feed_url, entry_id)
        if _original is not None:
            conn.execute(
                "INSERT OR IGNORE INTO entry_content_edits"
                " (feed_url, entry_id, original_content, ops, edited_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (feed_url, entry_id, _original, "[]",
                 datetime.now(timezone.utc).isoformat()),
            )
    except Exception:  # noqa: BLE001 — never block a re-fetch on the snapshot
        LOGGER.warning("refresh-capture: could not snapshot %s before replacing", entry_id,
                       exc_info=True)

    try:
        # A feed entry keeps its date and gets its content pinned against the
        # next refresh; a capture bumps to the top and needs no pin (its feed
        # never refreshes) — unless the caller overrode the bump decision.
        replace_entry_content(
            reader, conn, entry_id, new_title, article_html, feed_url=feed_url,
            bump_received=is_capture if bump_received is None else bump_received,
            pin_content=not is_capture,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("refresh-capture: content replace failed for %s: %s", entry_id, exc)
        result["error"] = "Could not store the re-fetched content."
        return result

    if enqueue_archive is not None and is_kept:
        try:
            enqueue_archive(feed_url, entry_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("refresh-capture: archive enqueue failed for %s: %s", entry_id, exc)

    result["ok"] = True
    result["refreshed"] = True
    result["extracted"] = True
    result["title"] = new_title or result["title"]
    return result


def _merge_save_into_entry(
    reader,
    conn: sqlite3.Connection,
    clean_url: str,
    target: tuple[str, str],
    *,
    extract: Callable[[str], tuple[str, str]],
    enqueue_archive: Callable[[str, str], None] | None,
    result: dict,
) -> dict:
    """Fold a save into an article that already exists in another feed.

    The survivor is the entry we found: a feed-provided post keeps updating and
    carries the publisher's tags (``entry_feed_tags``), neither of which a
    capture can offer. What the capture *does* offer is a body the server often
    cannot fetch at all — so the merge keeps whichever body is longer, pinned so
    the feed's own thinner copy can't overwrite it on the next refresh.

    Everything else is the resurface behavior a save already implies: star it,
    un-archive it, mark it unread so it lands back in the Inbox.
    """
    target_feed, target_id = target
    result["feed_url"], result["entry_id"] = target_feed, target_id
    result["merged"] = True

    entry = reader.get_entry((target_feed, target_id), None)
    if entry is None:  # vanished between lookup and merge
        result["error"] = "The matching article disappeared; try saving again."
        return result
    result["title"] = entry.title or clean_url

    try:
        new_title, article_html = extract(clean_url)
    except Exception as exc:  # noqa: BLE001 — the star/resurface below still applies
        LOGGER.warning("save-article: merge extraction failed for %s: %s", clean_url, exc)
        new_title, article_html = "", ""

    if article_html:
        current = (entry.content[0].value if getattr(entry, "content", None) else "") or entry.summary or ""
        if len(article_html) > len(current):
            try:
                replace_entry_content(
                    reader, conn, target_id, "", article_html, feed_url=target_feed,
                    bump_received=False, pin_content=True,
                )
                result["extracted"] = True
                result["title"] = new_title or result["title"]
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("save-article: merge content write failed for %s: %s", target_id, exc)

    conn.execute(
        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
        (target_feed, target_id),
    )
    try:
        conn.execute(
            "DELETE FROM archived_entries WHERE feed_url = ? AND entry_id = ?",
            (target_feed, target_id),
        )
        conn.execute(
            "DELETE FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
            (target_feed, target_id),
        )
        conn.commit()
        reader.mark_entry_as_unread((target_feed, target_id))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("save-article: merge resurface failed for %s: %s", target_id, exc)
    conn.commit()

    if enqueue_archive is not None:
        try:
            enqueue_archive(target_feed, target_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("save-article: merge archive enqueue failed for %s: %s", target_id, exc)

    result["ok"] = True
    return result


def save_article(
    reader,
    conn: sqlite3.Connection,
    url: str,
    *,
    extract: Callable[[str], tuple[str, str]],
    enqueue_archive: Callable[[str, str], None] | None = None,
    refresh_content: bool = False,
    find_existing_entry: Callable[[str], tuple[str, str] | None] | None = None,
) -> dict:
    """Save *url* as a starred entry in the Saved Articles feed.

    Returns ``{"ok", "error", "duplicate", "extracted", "feed_url",
    "entry_id", "title"}``. Deliberately does NOT fire the on-star
    destination fan-out: saving *into* Lectio shouldn't re-send the article
    to external read-later services.

    *refresh_content*: a re-save of an existing article re-runs extraction
    and REPLACES the stored content, bumping the entry to the top of the
    backlog (published = now, saved_at = now). Set when the save carries a
    browser-captured DOM — the user deliberately re-captured the page (e.g.
    after cleaning it up in-browser); URL-only re-saves stay light no-ops.

    *find_existing_entry*: resolves an article URL to an entry that already
    exists in some other feed, so saving a page you are subscribed to **merges
    into that post** instead of adding a second copy. Injected because the
    canonical-link matching lives in main. Without it, behavior is unchanged.

    Merging matters because the copies were never equivalent: a feed entry
    carries the publisher's tags and keeps updating, while an extension capture
    carries the body the server cannot fetch. Split across two entries you get
    an article with tags and no text beside one with text and no tags — which is
    exactly what happened to a Medium post (2026-07-26).
    """
    result: dict = {
        "ok": False,
        "error": None,
        "duplicate": False,
        "extracted": False,
        "feed_url": SAVED_FEED_URL,
        "entry_id": None,
        "title": None,
    }
    clean_url = normalize_article_url(url)
    if not clean_url:
        result["error"] = "Enter a valid http(s) article URL."
        return result
    result["entry_id"] = clean_url

    created = ensure_saved_feed(reader)

    # Already subscribed to this article somewhere? Enrich that post instead of
    # creating a rival copy. Only when there is no saved entry yet — an existing
    # saved copy is this article's home and keeps its history.
    if find_existing_entry is not None and reader.get_entry((SAVED_FEED_URL, clean_url), None) is None:
        target = None
        try:
            target = find_existing_entry(clean_url)
        except Exception:  # noqa: BLE001 — a failed lookup must not block the save
            LOGGER.exception("save-article: existing-entry lookup failed for %s", clean_url)
        if target:
            return _merge_save_into_entry(
                reader, conn, clean_url, target, extract=extract,
                enqueue_archive=enqueue_archive, result=result,
            )

    existing = reader.get_entry((SAVED_FEED_URL, clean_url), None)
    if existing is not None:
        result["duplicate"] = True
        result["title"] = existing.title or clean_url
        # An explicit re-save means "put this back in my Inbox to read." Without
        # this, saving an article that a past Instapaper import had archived (or
        # that the user archived earlier) silently leaves it in Archive marked
        # read — so a fresh save appears to do nothing. Un-archive and mark it
        # unread so it resurfaces.
        try:
            cur = conn.execute(
                "DELETE FROM archived_entries WHERE feed_url = ? AND entry_id = ?",
                (SAVED_FEED_URL, clean_url),
            )
            # entry_read_state is a read-state OVERRIDE re-applied on refresh, so
            # marking unread in reader alone doesn't stick — the override flips it
            # back to read. Clear it so the resurfaced-unread state survives.
            conn.execute(
                "DELETE FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
                (SAVED_FEED_URL, clean_url),
            )
            conn.commit()
            reader.mark_entry_as_unread((SAVED_FEED_URL, clean_url))
            result["resurfaced"] = bool(cur.rowcount) or bool(existing.read)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("save-article: resurface failed for %s: %s", clean_url, exc)
        if refresh_content:
            try:
                new_title, article_html = extract(clean_url)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("save-article: refresh extraction failed for %s: %s", clean_url, exc)
            else:
                if article_html:
                    try:
                        replace_entry_content(reader, conn, clean_url, new_title, article_html)
                    except Exception as exc:  # noqa: BLE001 — refresh is best-effort on a duplicate
                        LOGGER.warning("save-article: content refresh failed for %s: %s", clean_url, exc)
                    else:
                        result["extracted"] = True
                        result["refreshed"] = True
                        result["title"] = new_title or result["title"]
    else:
        title, article_html = clean_url, ""
        try:
            title, article_html = extract(clean_url)
            result["extracted"] = True
        except Exception as exc:  # noqa: BLE001
            # Save the bookmark anyway; the starred-archive worker retries the
            # page independently, so content can still arrive offline.
            LOGGER.warning("save-article: extraction failed for %s: %s", clean_url, exc)
        entry: dict = {
            "feed_url": SAVED_FEED_URL,
            "id": clean_url,
            "link": clean_url,
            "title": title,
            # NOT now(). "When I saved this" is not "when this was published",
            # and storing the capture time as the article's date fabricates a
            # date the UI then presents as fact. The save time is already
            # recorded where it belongs, in saved_entries.saved_at.
            #
            # The Unix epoch is used rather than omitting the field, because
            # entry_effective_date falls back published → updated → added, so
            # leaving it unset would silently substitute the capture time anyway
            # — the same lie by a longer route. 1970 is visibly wrong, sorts to
            # the end, and is trivially searchable.
            "published": UNKNOWN_PUBLISHED,
        }
        if article_html:
            entry["content"] = [{"value": article_html}]
        try:
            reader.add_entry(entry)
        except Exception:  # noqa: BLE001
            LOGGER.exception("save-article: add_entry failed for %s", clean_url)
            result["error"] = "Could not save the article."
            return result
        result["title"] = title

    conn.execute(
        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
        (SAVED_FEED_URL, clean_url),
    )
    conn.commit()

    if enqueue_archive is not None:
        try:
            enqueue_archive(SAVED_FEED_URL, clean_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("save-article: archive enqueue failed for %s: %s", clean_url, exc)

    result["ok"] = True
    result["created_feed"] = created
    return result
