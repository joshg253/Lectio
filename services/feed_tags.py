"""Persistence for feed-provided entry tags (RSS/Atom ``<category>``).

The ``reader`` library discards entry categories at ingest, so Lectio captures
them itself: the sanitizing feed parser (services.reader_sanitize) hands each
parsed feed's raw tag data to :meth:`FeedTagService.record_entry_tags`, which
stores it in the per-user meta-DB table ``entry_feed_tags``. Tags are stored
raw (case-preserving); callers normalize to Lectio tag format at display time.

Besides powering the post-header suggestion chips, this table is the data
foundation for future tag-filtered feed adapters.
"""
from __future__ import annotations

import html as html_module
import logging
import re
from urllib.parse import unquote_plus
import time
from collections.abc import Callable, Iterable
from typing import Any

LOGGER = logging.getLogger(__name__)

# Placeholder/junk taxonomy values that carry no signal (WordPress's default
# "Uncategorized" and friends) — dropped at capture so they never become
# suggestion chips or filter-rule bait. Compared lowercase.
JUNK_TAGS = {
    "uncategorized", "uncategorised", "untagged", "no category",
    "general", "misc", "miscellaneous", "other", "others",
    "blog", "blogs", "post", "posts", "article", "articles",
    "all", "default", "unsorted", "rss", "feed", "home",
}


def _clean_tag_values(values: list[str], cap: int | None = None) -> list[str]:
    """Decode entities, whitespace-compact, drop junk placeholders, dedupe
    case-insensitively (order preserved), optionally cap.

    Entity decoding is not cosmetic: feeds ship ``C&#43;&#43;`` for ``C++``,
    ``bricks &amp; minifigs``, ``Bu&#xF1;uel``. Undecoded, each is a distinct
    tag from its readable twin and matches nothing a user would type.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = " ".join(html_module.unescape(value).strip().split())
        # A leading "#" is display chrome, not part of the name: themes render
        # tag links as "#motivation", and Lectio uses "#" as its own tag marker
        # everywhere. Stripping it here also folds the duplicate when a page
        # links the same term once as a category ("personal") and once as a
        # hash-prefixed tag ("#personal").
        compact = compact.lstrip("#").strip()
        # Numbers-only tags carry nothing: comment counts, post ids, pagination,
        # bare years. Josh's call — "trying to think where a numbers-only tag would
        # be useful … definitely mixed are useful" — so anything with a non-digit
        # survives: "80s", "3d", "2.5 Admins", "2020 election", "Fallout 4".
        if compact.isdigit():
            continue
        if not compact or len(compact) > 60:
            continue
        lowered = compact.lower()
        if lowered in seen or lowered in JUNK_TAGS:
            continue
        seen.add(lowered)
        cleaned.append(compact)
        if cap is not None and len(cleaned) >= cap:
            break
    return cleaned


def _split_multi_value_term(value: str) -> list[str]:
    """Split a single <category> that packs several tags into one term.

    Some generators emit ``<category>ASP.NET Core;Security;CSRF;CORS</category>``
    rather than one element each (andrewlock.net), which lands as a single
    unusable tag.

    **Entities are decoded first, and that ordering is the whole subtlety.**
    Measured on the live library: 124 stored tags contain a ``;``, but only 25
    still do once decoded — the other 99 are entity terminators. Splitting raw
    text would turn ``C&#43;&#43;`` into three fragments instead of ``C++``.

    Only ``;`` splits. Commas look tempting and are not: the comma-bearing tags
    here are a single junk value (a forum feed's ``9,41,44,… Forum``), which
    splitting would explode into 19 numeric tags.
    """
    decoded = html_module.unescape(value)
    if ";" not in decoded:
        return [value]
    return [part for part in decoded.split(";") if part.strip()]


def extract_feed_entry_tags(raw_entry: object) -> list[str]:
    """Pull tag strings out of a raw feedparser entry (.tags + .category),
    whitespace-compacted and deduped case-insensitively, order preserved."""
    values: list[str] = []

    raw_tags: list[Any] | None = getattr(raw_entry, "tags", None)
    if raw_tags:
        for raw_tag in raw_tags:
            term = getattr(raw_tag, "term", None)
            label = getattr(raw_tag, "label", None)
            scheme = getattr(raw_tag, "scheme", None)
            if isinstance(raw_tag, dict):
                term = term or raw_tag.get("term")
                label = label or raw_tag.get("label")
                scheme = scheme or raw_tag.get("scheme")
            tag_value = term or label or scheme
            if tag_value:
                values.extend(_split_multi_value_term(str(tag_value)))

    category = getattr(raw_entry, "category", None)
    if category:
        values.extend(_split_multi_value_term(str(category)))

    return _clean_tag_values(values)


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_ATTR_RE = re.compile(
    r'\b(property|name|content)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.IGNORECASE
)
_PAGE_TAG_KEYS = {"article:tag", "parsely-tags", "keywords", "news_keywords", "sailthru.tags"}
_MAX_PAGE_TAGS = 15
# Distinct 4-digit years on one page that mark an archive list rather than tags.
_ARCHIVE_YEAR_RUN = 5
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.{0,120}?)</a>", re.IGNORECASE | re.DOTALL)
# /tag/x, /tags/x, /category/x, /categories/x — trailing slash optional. The
# capture is the slug, used when the anchor has no text of its own.
_TAXONOMY_HREF_RE = re.compile(r"/(?:tags?|categor(?:y|ies))/([^/?#]+)", re.IGNORECASE)
# Taxonomy carried in the QUERY STRING instead of the path. The Google
# Developers Blog links its "posted in:" block as
# /search/?technology_categories=AI and ?content_type_categories=How-To+Guides,
# so the path says "search" and only the parameter NAME identifies a taxonomy.
#
# The name must END in a taxonomy word so a compound like
# ``technology_categories`` matches while a free-text search (``?q=``, ``?s=``)
# and a paginator (``?page=``) cannot. Anchored on both sides for that reason:
# a substring test would take ``?category_count=12``.
#
# The name prefix is length-bounded rather than a bare ``*?``: a lazy star in
# front of an alternation backtracks quadratically on a long run of word
# characters that never completes the match, and hrefs are attacker-supplied
# page content (a data: URI is arbitrarily long). 40 is far past any real
# parameter name.
_TAXONOMY_QUERY_RE = re.compile(
    r"[?&]([A-Za-z0-9_-]{0,40}?(?:tags?|categor(?:y|ies)|topics?))=([^&#]+)", re.IGNORECASE
)


def _taxonomy_slug_from_href(href: str) -> str | None:
    """The taxonomy term an href encodes, or None if it encodes none.

    Returns the raw slug/term for use when the anchor has no readable text of
    its own; the caller still prefers the link text (see the tier below).
    """
    if not href:
        return None
    if path_m := _TAXONOMY_HREF_RE.search(href):
        return path_m.group(1).replace("-", " ")
    if query_m := _TAXONOMY_QUERY_RE.search(href):
        # "How-To+Guides" -> "How-To Guides". Hyphens are NOT expanded here the
        # way a path slug's are: a query value is the publisher's display term
        # already, so "How-To Guides" is the name they use, not a slugification
        # of "How To Guides".
        return unquote_plus(query_m.group(2)).strip() or None
    return None
# The unquoted alternative is not optional politeness: minified Hugo output emits
# `href=https://host/tags/x/` with no quotes at all, so a quotes-only pattern
# matched nothing on those pages and every anchor tier below silently found zero.
_ANCHOR_ATTR_RE = re.compile(
    r'\b(rel|class|href|title)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s"\'>]+))', re.IGNORECASE
)
_INNER_TAG_RE = re.compile(r"<[^>]+>")


# A tag is a label, not a sentence. Publishers link the same taxonomy twice —
# once as the tag itself ("AI") and once as a navigation aside ("More posts in
# AI »", title="View all posts in AI") — and only the first is a tag.
_TAG_PROSE_MARKERS = ("»", "«", "…", "→")
_TAG_TEXT_MAX_CHARS = 30


def _looks_like_a_tag(text: str) -> bool:
    """Whether an anchor's text reads as a tag rather than a phrase."""
    text = (text or "").strip()
    if len(text) < 2 or len(text) > _TAG_TEXT_MAX_CHARS:
        return False
    if any(marker in text for marker in _TAG_PROSE_MARKERS):
        return False
    lowered = text.lower()
    return not any(
        lowered.startswith(prefix)
        for prefix in ("more posts", "view all", "all posts", "see all", "browse ")
    )


def extract_page_tags(html: str | None) -> list[str]:
    """Harvest article tags from a source page — the fallback for entries
    whose feed never delivered <category> data (aged out of the feed window,
    or a publisher that strips tags from RSS). Two tiers:

    - meta tags: article:tag (one per meta) + comma-joined keyword variants;
    - tag anchors: the rel="tag" microformat, plus tag-classed anchors
      (class contains "tag") linking to /tag/ or /category/ paths — how
      Valnet sites (MakeUseOf, How-To-Geek) mark their article tag block.
    """
    if not html:
        return []
    # Generous cap: tag blocks often sit at the BOTTOM of article pages
    # (Valnet's footer tag links live past 300KB on ad-heavy pages), and a
    # regex scan of a few MB is milliseconds. The cap only guards degenerate
    # multi-MB pages (live blogs).
    html = html[:5_000_000]
    values: list[str] = []
    for meta in _META_TAG_RE.findall(html):
        attrs: dict[str, str] = {}
        for m in _META_ATTR_RE.finditer(meta):
            attrs[m.group(1).lower()] = m.group(3) if m.group(3) is not None else m.group(4)
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if key not in _PAGE_TAG_KEYS or not content:
            continue
        if key == "article:tag":
            values.append(content)
        else:
            values.extend(part.strip() for part in content.split(","))

    # rel="tag" microformat anchors: tag name is the (short) link text.
    for m in _ANCHOR_RE.finditer(html):
        attrs = {}
        for am in _ANCHOR_ATTR_RE.finditer(m.group(1)):
            attrs[am.group(1).lower()] = next(
                (g for g in (am.group(3), am.group(4), am.group(5)) if g is not None), ""
            )
        if "tag" not in (attrs.get("rel") or "").lower().split():
            continue
        text = _INNER_TAG_RE.sub(" ", m.group(2)).strip()
        if text:
            values.append(text)

    # Tag-classed anchors (open tag only — the anchor body may wrap an image):
    # tag name from the title attribute (any linked path — How-To-Geek's tag
    # block links bare section slugs like /gpu/), else the /tag//category/
    # URL slug when there's no title.
    for open_tag in re.findall(r"<a\b[^>]*>", html, re.IGNORECASE):
        attrs = {}
        for am in _ANCHOR_ATTR_RE.finditer(open_tag):
            attrs[am.group(1).lower()] = next(
                (g for g in (am.group(3), am.group(4), am.group(5)) if g is not None), ""
            )
        classes = (attrs.get("class") or "").lower()
        href = attrs.get("href") or ""
        if "tag" not in classes or not href:
            continue
        value = (attrs.get("title") or "").strip()
        if not value and (slug_m := re.search(r"/(?:tag|category)/([^/?#]+)", href)):
            value = slug_m.group(1).replace("-", " ")
        if value:
            values.append(value)

    # Tag-classed anchors whose NAME is only in the link text. ArtStation marks
    # its tag block with classes (project-tag-item, label-tag) but links each
    # tag to a search page — /search?query=Digital 2D — so there is no title
    # attribute and no /tag/ or /category/ slug for the tier above to read.
    #
    # The text is used rather than the href because the href is ambiguous:
    # ArtStation writes the tag "Environmental Concept Art &amp; Design"
    # straight into the query string, so unescaping it yields a bare "&" that
    # URL-parsing would split into two tags. The link text carries the same
    # value with no such trap.
    #
    # Guarded to keep this from becoming "any tag-classed anchor's text":
    # the body must be plain (an anchor wrapping an image or markup is the case
    # the title-attribute tier exists for) and short enough to be a tag rather
    # than a sentence — the failure mode already on record for gottadeal, where
    # surrounding prose was harvested as "in XXX, YYY".
    for m in _ANCHOR_RE.finditer(html):
        attrs = {}
        for am in _ANCHOR_ATTR_RE.finditer(m.group(1)):
            attrs[am.group(1).lower()] = next(
                (g for g in (am.group(3), am.group(4), am.group(5)) if g is not None), ""
            )
        classes = (attrs.get("class") or "").lower()
        if "tag" not in classes or attrs.get("title"):
            continue          # titled anchors are the tier above's job
        body = m.group(2)
        if "<" in body:
            continue          # wraps markup — not a plain tag label
        text = html_module.unescape(body).strip().lstrip("#").strip()
        if text and len(text) <= 60:
            values.append(text)

    # Taxonomy-URL anchors: a link to /tags/<slug>/ or /categories/<slug>/ IS a
    # tag link, whatever its class. Hugo (and most static generators) mark them
    # only by URL shape — krshrimali.github.io puts its category at the top and
    # its tags in the footer with no tag class and no rel="tag", so the tiers
    # above found nothing at all.
    #
    # Plural matters: the older slug fallback matched /tag/ and /category/ only,
    # which misses Hugo's /tags/ and /categories/ entirely.
    #
    # A slug is REQUIRED, which is what keeps the nav links to the /tags and
    # /categories index pages out.
    for m in _ANCHOR_RE.finditer(html):
        attrs = {}
        for am in _ANCHOR_ATTR_RE.finditer(m.group(1)):
            attrs[am.group(1).lower()] = next(
                (g for g in (am.group(3), am.group(4), am.group(5)) if g is not None), ""
            )
        href = attrs.get("href") or ""
        slug = _taxonomy_slug_from_href(href)
        if slug is None:
            continue
        # The anchor's own TEXT first, then the slug, then the title.
        #
        # Title was first and it is prose on every WordPress site: fossforce's
        # category links carry title="View all posts in AI" beside a sibling
        # reading "More posts in AI »", so a post tagged AI and Developer
        # harvested four "tags" and not one of them was a tag. The link text is
        # what the publisher shows a reader ("AI"), and it keeps the casing and
        # punctuation a slug loses ("Pet Supplies", "Woot!").
        #
        # Slug before title for the rest: Valnet wraps its tag anchors around an
        # image or a one-character span, so there is no text to read, and
        # "pet-supplies" still beats a sentence.
        text = " ".join(_INNER_TAG_RE.sub(" ", m.group(2)).split())
        if not _looks_like_a_tag(text):
            # Prose, not a tag: the same category is often linked twice, once as
            # the tag and once as "More posts in AI »". Its slug is the tag.
            text = slug or text
        if len(text) < 2:
            text = (attrs.get("title") or "").strip()
        if len(text) >= 2:
            values.append(text)

    # An archive/sidebar year list is not a set of tags. nwcpp.org's page carries
    # 2000-2026 down the side, and all sixteen were harvested onto one post. A real
    # post might carry a year or two; nothing carries fifteen — so the whole run is
    # dropped rather than any single year being judged.
    years = {v for v in values if v.isdigit() and len(v) == 4 and 1900 <= int(v) <= 2100}
    if len(years) >= _ARCHIVE_YEAR_RUN:
        values = [v for v in values if v not in years]
    return _clean_tag_values(values, cap=_MAX_PAGE_TAGS)


class FeedTagService:
    """CRUD for the per-user ``entry_feed_tags`` meta-DB table.

    ``get_meta_connection`` resolves the current tenant at call time, so the
    service itself stays tenancy-unaware (same pattern as LeadImageService).
    """

    def __init__(self, *, get_meta_connection: Callable) -> None:
        self._get_meta_connection = get_meta_connection

    def record_entry_tags(
        self, feed_url: str, pairs: list[tuple[str, list[str]]]
    ) -> None:
        """Persist tags for entries of ``feed_url``.

        ``pairs`` is ``[(entry_id, tags), ...]``. Replace-per-entry semantics:
        an entry seen again with different tags gets its rows replaced, so
        publisher tag edits propagate; entries absent from ``pairs`` keep
        their existing rows.
        """
        if not pairs:
            return
        now = time.time()
        with self._get_meta_connection() as conn:
            for entry_id, tags in pairs:
                if not entry_id or not tags:
                    continue
                conn.execute(
                    "DELETE FROM entry_feed_tags WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
                # INSERT OR IGNORE guards case-collisions within one entry's tags.
                conn.executemany(
                    "INSERT OR IGNORE INTO entry_feed_tags"
                    " (feed_url, entry_id, tag, first_seen_at) VALUES (?, ?, ?, ?)",
                    [(feed_url, entry_id, tag, now) for tag in tags],
                )

    def get_tags_for_entry(self, feed_url: str, entry_id: str) -> list[str]:
        with self._get_meta_connection() as conn:
            # rowid order = insertion order = the feed's own tag order
            # (replace-per-entry rewrites all of an entry's rows together).
            rows = conn.execute(
                "SELECT tag FROM entry_feed_tags"
                " WHERE feed_url = ? AND entry_id = ? ORDER BY rowid",
                (feed_url, entry_id),
            ).fetchall()
        return [row[0] for row in rows]

    def tag_vocabulary(
        self, feed_urls: Iterable[str] | None, *, limit: int = 400
    ) -> list[tuple[str, int]]:
        """The tags actually present in a scope, most-used first.

        Feeds the rule form's autocomplete: a tag_filter rule can only match
        what ingest captured, so suggesting from this table is suggesting from
        the only vocabulary that can ever hit. Counts come along because they
        are the whole decision — a tag on 9 of 10 entries is a filter worth
        writing, one on a single entry is noise.

        ``feed_urls=None`` means every feed (a global-scope rule). Raw stored
        casing is returned; the caller normalizes, because normalization is
        what collapses "Steam Deck" and "steam deck" into one suggestion and
        that merge has to also merge their counts.
        """
        urls = None if feed_urls is None else [u for u in feed_urls if u]
        if urls is not None and not urls:
            return []
        sql = (
            "SELECT tag, COUNT(*) AS n FROM entry_feed_tags"
            "{where} GROUP BY LOWER(tag) ORDER BY n DESC, LOWER(tag) LIMIT ?"
        )
        params: list = []
        where = ""
        if urls is not None:
            # One bound parameter per feed. SQLite allows 32k of them since
            # 3.32, so a folder would need thousands of feeds to reach the
            # limit; global scope (the only unbounded case) passes None and
            # takes the unfiltered branch instead.
            where = " WHERE feed_url IN (%s)" % ",".join("?" * len(urls))
            params.extend(urls)
        params.append(max(1, limit))
        try:
            with self._get_meta_connection() as conn:
                rows = conn.execute(sql.format(where=where), params).fetchall()
        except Exception:
            LOGGER.warning("tag vocabulary lookup failed", exc_info=True)
            return []
        return [(str(r[0]), int(r[1])) for r in rows]

    def suppressed_tags(self, feed_url: str) -> set[str]:
        """Tags the user has dismissed for this feed. Compared case-insensitively,
        because a publisher changing "ILLUSTRATION" to "Illustration" must not
        quietly resurrect a chip that was already dismissed."""
        try:
            with self._get_meta_connection() as conn:
                rows = conn.execute(
                    "SELECT tag FROM suppressed_feed_tags WHERE feed_url = ?", (feed_url,)
                ).fetchall()
        except Exception:
            LOGGER.warning("suppressed tag lookup failed for %s", feed_url, exc_info=True)
            return set()
        return {str(r[0]).strip().lower() for r in rows}

    def set_tag_suppressed(self, feed_url: str, tag: str, suppressed: bool) -> None:
        """Dismiss (or restore) one suggestion chip for one feed."""
        clean = (tag or "").strip()
        if not clean:
            return
        with self._get_meta_connection() as conn:
            if suppressed:
                conn.execute(
                    "INSERT OR REPLACE INTO suppressed_feed_tags (feed_url, tag, suppressed_at)"
                    " VALUES (?, ?, ?)",
                    (feed_url, clean, time.time()),
                )
            else:
                # Delete case-insensitively: the stored row may differ in case from
                # whatever the caller is looking at now.
                conn.execute(
                    "DELETE FROM suppressed_feed_tags WHERE feed_url = ?"
                    " AND LOWER(tag) = LOWER(?)",
                    (feed_url, clean),
                )

    def suppressed_tag_list(self, feed_url: str) -> list[str]:
        """Dismissed tags as stored (original casing), for the Feed Properties
        undo list — there has to be a way back from a mis-click."""
        try:
            with self._get_meta_connection() as conn:
                rows = conn.execute(
                    "SELECT tag FROM suppressed_feed_tags WHERE feed_url = ? ORDER BY tag",
                    (feed_url,),
                ).fetchall()
        except Exception:
            return []
        return [str(r[0]) for r in rows]

    def delete_for_feed(self, feed_url: str) -> int:
        with self._get_meta_connection() as conn:
            return conn.execute(
                "DELETE FROM entry_feed_tags WHERE feed_url = ?", (feed_url,)
            ).rowcount

    def migrate_feed_url(self, old_url: str, new_url: str) -> None:
        with self._get_meta_connection() as conn:
            # OR IGNORE + sweep: if the new URL already has rows for the same
            # (entry_id, tag), keep them and drop the old-URL leftovers.
            conn.execute(
                "UPDATE OR IGNORE entry_feed_tags SET feed_url = ? WHERE feed_url = ?",
                (new_url, old_url),
            )
            conn.execute(
                "DELETE FROM entry_feed_tags WHERE feed_url = ?", (old_url,)
            )
