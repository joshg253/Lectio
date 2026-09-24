"""Duplicate detection: the deduplicate rule's matching engine (shared by its preview and Run Now) and the refresh-time GUID-churn /
intra-feed duplicate suppression.

The rule's two entry points differ only in what they read and what they do with the result: the preview scans read and unread
entries under one global cap and returns groups; Run Now scans unread only, per-feed capped, and marks each group's non-keepers read.
Both go through `collect_records` + `find_groups`, so a group the preview shows is exactly a group Run Now would act on given the same
entries (tests/integration/test_dedup_characterization.py).
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)

# One floor for every title-based dedup signal (fuzzy, exact title, the safe
# combo, GUID-churn suppression). Below it, "New post" / "Weekly update" collide
# across unrelated feeds: measured on a 5,588-entry backlog, titles under 4 words
# produced 11 same-feed false collisions against 1 real cross-feed duplicate, and
# the 4-word band itself added 2 more false collisions and no true ones. Hence 5.
_DEDUP_MIN_TITLE_WORDS = 5

MATCH_METHODS = ("slug", "title", "both", "fuzzy", "safe")


# Title normalization for every dedup comparison. Punctuation is stripped from
# each token's EDGES only, so word boundaries never move: "second-best.cat()"
# stays one token rather than becoming "second best cat". Splitting hyphens was
# measured as strictly worse (30 -> 28 true cross-feed pairs) because it inflates
# the token count on whichever side spells the compound out.
_TITLE_QUOTE_FOLD = {ord(c): r for c, r in [("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2032", "'")]}
# En/em/figure dashes BETWEEN letters separate phrases ("Title\u2014Subtitle"); a plain
# hyphen-minus joins a compound and is left alone.
_TITLE_DASH_BETWEEN = re.compile(r"(?<=\w)[\u2010\u2011\u2012\u2013\u2014\u2015\u2212](?=\w)")
# Deliberately excludes + # & @ $ * — C++, C#, AT&T and #hashtag are the title,
# and folding them all to "c" merges unrelated programming posts.
_TITLE_EDGE_PUNCT = "\"'!?.,:;()[]{}<>\u00ab\u00bb\u2026\u00b7|\\`-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"


def normalize_entry_title_for_dedupe(title: str | None) -> str:
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", str(title)).translate(_TITLE_QUOTE_FOLD).casefold()
    s = _TITLE_DASH_BETWEEN.sub(" ", s)
    return " ".join(w for w in (t.strip(_TITLE_EDGE_PUNCT) for t in s.split()) if w)


def title_word_similarity(t1: str, t2: str) -> float:
    """Jaccard similarity on word sets. Returns 0.0–1.0."""
    w1 = set(t1.split())
    w2 = set(t2.split())
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


# ── Safe multi-signal dedup ───────────────────────────────────────────────────

_SAFE_DEDUP_FUZZY_THRESH = 0.80
_SAFE_DEDUP_BODY_FUZZY_THRESH = 0.75
_SAFE_DEDUP_BODY_CHARS = 400
_SAFE_DEDUP_MIN_BODY_CHARS = 30
_SAFE_DEDUP_MIN_SLUG_LEN = 4
_SAFE_DEDUP_MIN_TITLE_WORDS = _DEDUP_MIN_TITLE_WORDS
_SAFE_DEDUP_MIN_SLUG_NO_HYPHEN = 16

# Reddit truncates the title-slug in its permalinks to a fixed length, so two
# different posts collide on the last path segment
# (…/comments/<id>/amazon_..._harry_potter_and_the/ for both Sorcerer's Stone
# and Prisoner of Azkaban). The unique thing id sits earlier in the path — use
# it so different posts don't false-match and genuine cross-feed reposts of the
# same thread still do.
_REDDIT_THING_ID_RE = re.compile(r"(?:reddit\.com/(?:r/[^/]+/)?comments/|redd\.it/)([a-z0-9]{4,})", re.IGNORECASE)

_SAFE_DEDUP_SLUG_EXTS = frozenset({".php", ".html", ".htm", ".asp", ".aspx", ".cgi", ".pl", ".jsp", ".cfm", ".shtml"})
_SAFE_DEDUP_SLUG_BLOCKLIST = frozenset(
    {
        "watch",
        "shorts",
        "video",
        "videos",
        "post",
        "posts",
        "article",
        "articles",
        "page",
        "pages",
        "index",
        "home",
        "feed",
        "rss",
        "atom",
        "news",
        "story",
        "entry",
        "item",
        "read",
        "view",
        "show",
        "detail",
        "details",
        "content",
        "about",
        "contact",
        "search",
        "archive",
        "archives",
        "category",
        "categories",
        "tag",
        "tags",
        "author",
        "user",
        "profile",
        "default",
        "main",
        "welcome",
        "latest",
        "recent",
        "popular",
        "trending",
        "featured",
        "top",
        "new",
        "forum",
        "forums",
        "thread",
        "threads",
        "topic",
        "topics",
        "blog",
        "comic",
        "comics",
        "gallery",
        "photo",
        "photos",
        "image",
        "images",
        "release",
        "releases",
        "pre-release",
        "download",
        "downloads",
        "changelog",
        "update",
        "updates",
        "p",
        "s",
        "t",
        "r",
        "q",
        "m",
        "n",
        "a",
        "e",
    }
)
_SAFE_DEDUP_UNICODE_TRANS = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "…": "...",
        " ": " ",
    }
)
_SAFE_DEDUP_TAG_RE = re.compile(r"<[^>]+>")
# A tag left unclosed because a fixed-size window truncated the HTML mid-tag.
_SAFE_DEDUP_UNCLOSED_TAG_RE = re.compile(r"<[^>]*$")

_SAFE_DEDUP_COMBOS: frozenset[frozenset] = frozenset(
    {
        frozenset({"slug", "title", "body"}),
        frozenset({"slug", "fuzzy_near", "body"}),
        frozenset({"title", "body"}),
        frozenset({"slug", "title", "body_fuzzy"}),
        frozenset({"slug", "fuzzy_near", "body_fuzzy"}),
        frozenset({"title", "body_fuzzy"}),
        frozenset({"slug", "body"}),
        frozenset({"fuzzy_near", "body_fuzzy"}),
        frozenset({"fuzzy_near", "body"}),
        frozenset({"body_fuzzy"}),
        frozenset({"slug", "title", "body", "body_fuzzy"}),
        frozenset({"slug", "fuzzy_near", "body", "body_fuzzy"}),
        frozenset({"title", "body", "body_fuzzy"}),
        frozenset({"slug", "body", "body_fuzzy"}),
        frozenset({"fuzzy_near", "body", "body_fuzzy"}),
    }
)


def _safe_dedup_entry_slug(url: str | None) -> str | None:
    if not url:
        return None
    # Reddit's truncated permalink slug collides across posts — key on the
    # unique thing id instead (see _REDDIT_THING_ID_RE).
    reddit = _REDDIT_THING_ID_RE.search(url)
    if reddit:
        return f"reddit:{reddit.group(1).lower()}"
    path = url.split("#")[0].split("?")[0].rstrip("/")
    slug = path.rsplit("/", 1)[-1].lower()
    for ext in _SAFE_DEDUP_SLUG_EXTS:
        if slug.endswith(ext):
            slug = slug[: -len(ext)]
            break
    if len(slug) < _SAFE_DEDUP_MIN_SLUG_LEN or slug in _SAFE_DEDUP_SLUG_BLOCKLIST:
        return None
    if "-" not in slug and len(slug) < _SAFE_DEDUP_MIN_SLUG_NO_HYPHEN:
        return None
    return slug


def _safe_dedup_norm_title(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFC", t).translate(_SAFE_DEDUP_UNICODE_TRANS)
    return " ".join(t.strip().lower().split())


def _safe_dedup_norm_body(entry) -> str:
    raw = ""
    if entry.content:
        raw = entry.content[0].value or ""
    if not raw:
        raw = entry.summary or ""
    text = _SAFE_DEDUP_TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return " ".join(text.split())[:_SAFE_DEDUP_BODY_CHARS].lower()


def _safe_dedup_find_pairs(records: list[dict]) -> dict[tuple[str, str], list[str]]:
    """Run the multi-signal safe-dedup algorithm. Returns {(keep_link, mark_link): [modes]}."""

    def _mk_pair(a: dict, b: dict) -> tuple[str, str]:
        # Tie-break equal timestamps by link so the pair key is canonical
        # regardless of iteration order — otherwise the same duplicate can be
        # emitted as both (A,B) and (B,A) and BOTH copies get marked read.
        ta = a["published_ts"] or 0.0
        tb = b["published_ts"] or 0.0
        if ta != tb:
            return (a["link"], b["link"]) if ta < tb else (b["link"], a["link"])
        return (a["link"], b["link"]) if a["link"] <= b["link"] else (b["link"], a["link"])

    def _index_to_pairs(idx: dict) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for entries in idx.values():
            if len({e["feed_url"] for e in entries}) < 2:
                continue
            _ordered = sorted(entries, key=dedup_order_key)
            for i, a in enumerate(_ordered):
                for b in _ordered[i + 1 :]:
                    if a["feed_url"] != b["feed_url"] and a["link"] != b["link"]:
                        pairs.add(_mk_pair(a, b))
        return pairs

    slug_idx: dict = defaultdict(list)
    title_idx: dict = defaultdict(list)
    body_idx: dict = defaultdict(list)
    guid_idx: dict = defaultdict(list)
    by_feed: dict = defaultdict(list)

    for r in records:
        # Identical GUID in two different feeds is the publisher's own statement
        # that it's the same item (e.g. slickdeals search feeds all carry
        # guid=thread URL while varying the link) — sufficient evidence alone.
        # Length guard keeps degenerate ids ("1", "42") from cross-matching.
        if len(r["entry_id"]) >= 8:
            guid_idx[r["entry_id"]].append(r)
        if r["slug"]:
            slug_idx[r["slug"]].append(r)
        if r["ntitle"] and len(r["ntitle"].split()) >= _SAFE_DEDUP_MIN_TITLE_WORDS:
            title_idx[r["ntitle"]].append(r)
        if r["body"] and len(r["body"]) >= _SAFE_DEDUP_MIN_BODY_CHARS:
            body_idx[r["body"]].append(r)
        if r["ntitle"] and len(r["ntitle"].split()) >= _SAFE_DEDUP_MIN_TITLE_WORDS:
            by_feed[r["feed_url"]].append(r)

    slug_pairs = _index_to_pairs(slug_idx)
    title_pairs = _index_to_pairs(title_idx)
    body_pairs = _index_to_pairs(body_idx)
    guid_pairs = _index_to_pairs(guid_idx)

    link_feed: dict[str, str] = {r["link"]: r["feed_url"] for r in records if r["link"]}
    cand_pairs: set[tuple[str, str]] = set()
    for pk in slug_pairs | title_pairs:
        fu_a = link_feed.get(pk[0])
        fu_b = link_feed.get(pk[1])
        if fu_a and fu_b and fu_a != fu_b:
            cand_pairs.add((min(fu_a, fu_b), max(fu_a, fu_b)))

    fuzzy_pairs: set[tuple[str, str]] = set()
    body_fuzzy_pairs: set[tuple[str, str]] = set()

    for fu_i, fu_j in cand_pairs:
        for a in by_feed.get(fu_i, []):
            for b in by_feed.get(fu_j, []):
                if a["link"] == b["link"]:
                    continue
                sim_t = title_word_similarity(a["ntitle"], b["ntitle"])
                if _SAFE_DEDUP_FUZZY_THRESH <= sim_t < 1.0:
                    fuzzy_pairs.add(_mk_pair(a, b))
                if len(a["body"]) >= _SAFE_DEDUP_MIN_BODY_CHARS and len(b["body"]) >= _SAFE_DEDUP_MIN_BODY_CHARS:
                    sim_b = title_word_similarity(a["body"], b["body"])
                    if sim_b >= _SAFE_DEDUP_BODY_FUZZY_THRESH:
                        body_fuzzy_pairs.add(_mk_pair(a, b))

    all_pairs = slug_pairs | title_pairs | fuzzy_pairs | body_pairs | body_fuzzy_pairs | guid_pairs
    pair_modes: dict[tuple[str, str], list[str]] = {}
    for pk in all_pairs:
        modes: list[str] = []
        if pk in guid_pairs:
            modes.append("guid")
        if pk in slug_pairs:
            modes.append("slug")
        if pk in title_pairs:
            modes.append("title")
        if pk in fuzzy_pairs:
            modes.append("fuzzy_near")
        if pk in body_pairs:
            modes.append("body")
        if pk in body_fuzzy_pairs:
            modes.append("body_fuzzy")
        # A shared GUID is accepted on its own; everything else needs a
        # corroborated combo from _SAFE_DEDUP_COMBOS.
        if "guid" in modes or frozenset(modes) in _SAFE_DEDUP_COMBOS:
            pair_modes[pk] = modes
    return pair_modes


def entry_url_slug(url: str | None) -> str | None:
    """Extract the last non-empty path segment from a URL (before query/fragment)."""
    if not url:
        return None
    path = url.split("#")[0].split("?")[0].rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    return slug.lower() if slug else None


def dedup_order_key(info: dict) -> tuple:
    """Ordering for duplicate copies: oldest first, then a STABLE tie-break.

    Sister papers on one wire publish the same story in the same second — 164 of
    190 cross-paper pairs among three local Reporter feeds shared a timestamp to
    the second. With date alone the winner fell out of set-iteration order over
    the feed URLs, so which copy was kept changed between runs, and so did which
    one got marked read. Feed URL then link makes it repeatable.
    """
    return (info.get("published_ts") or 0.0, str(info.get("feed_url") or ""), str(info.get("link") or ""))


# ── The deduplicate rule's engine ─────────────────────────────────────────────


def collect_records(
    reader,
    feed_urls: Iterable[str],
    *,
    per_feed_limit: int,
    read: bool | None,
    feed_title: Callable[[Any], str],
    effective_date: Callable[[Any], datetime | None],
    max_total: int | None = None,
    safe_fields: bool = False,
) -> list[dict]:
    """One record per entry in scope. *read* None scans read and unread (the preview), False unread only (Run Now). *safe_fields*
    adds the slug / normalized title / body the safe method compares."""
    feed_titles = {str(f.url): feed_title(f) for f in reader.get_feeds()}
    records: list[dict] = []
    for feed_url in feed_urls:
        if max_total is not None and len(records) >= max_total:
            break
        kwargs: dict = {"feed": feed_url, "limit": per_feed_limit}
        if read is not None:
            kwargs["read"] = read
        try:
            for entry in reader.get_entries(**kwargs):
                if max_total is not None and len(records) >= max_total:
                    break
                published = effective_date(entry)
                entry_feed = str(entry.feed_url or "")
                rec = {
                    "feed_url": entry_feed,
                    "entry_id": str(entry.id),
                    "title": str(entry.title or ""),
                    "link": str(entry.link or ""),
                    "feed_title": feed_titles.get(entry_feed, entry_feed),
                    "published": published.isoformat() if published else None,
                    "published_ts": published.timestamp() if published else 0.0,
                    # The preview deliberately scans read entries too — a folder whose duplicates were already marked would otherwise
                    # preview as a bare zero, with nothing to tune a threshold against. The rule only ever acts on UNREAD, so the
                    # preview reports how many of its marks are actionable (unread_actionable).
                    "read": bool(getattr(entry, "read", False)),
                }
                if safe_fields:
                    rec["slug"] = _safe_dedup_entry_slug(entry.link)
                    rec["ntitle"] = _safe_dedup_norm_title(entry.title)
                    rec["body"] = _safe_dedup_norm_body(entry)
                records.append(rec)
        except Exception:
            LOGGER.exception("dedup: error reading feed %s", feed_url)
    return records


def _within_window(ordered: list[dict], window_secs: float) -> bool:
    oldest_ts = ordered[0]["published_ts"] or 0.0
    newest_ts = ordered[-1]["published_ts"] or 0.0
    return not (oldest_ts > 0 and newest_ts > 0 and (newest_ts - oldest_ts) > window_secs)


def _keyed_groups(index: dict, match_by: str, window_secs: float | None) -> list[dict]:
    groups: list[dict] = []
    for key, entries in index.items():
        if len({e["feed_url"] for e in entries}) < 2:
            continue
        ordered = sorted(entries, key=dedup_order_key)
        if window_secs is not None and not _within_window(ordered, window_secs):
            continue
        matched_value = key[1] if isinstance(key, tuple) else key
        groups.append({"match_by": match_by, "matched_value": matched_value, "keep": ordered[0], "mark_read": ordered[1:]})
    return groups


def _fuzzy_groups(records: list[dict], window_secs: float, threshold: float, min_title_words: int) -> list[dict]:
    by_feed: dict[str, list[dict]] = {}
    norm: dict[int, str] = {}
    for r in records:
        n = normalize_entry_title_for_dedupe(r["title"])
        if r["title"] and n and len(n.split()) >= min_title_words:
            norm[id(r)] = n
            by_feed.setdefault(r["feed_url"], []).append(r)
    groups: list[dict] = []
    seen_mark_links: set[str] = set()
    feed_list = sorted(by_feed)
    for i, feed_i in enumerate(feed_list):
        for feed_j in feed_list[i + 1 :]:
            for ei in by_feed[feed_i]:
                for ej in by_feed[feed_j]:
                    ts_i = ei["published_ts"] or 0.0
                    ts_j = ej["published_ts"] or 0.0
                    if window_secs > 0 and abs(ts_i - ts_j) > window_secs:
                        continue
                    sim = title_word_similarity(norm[id(ei)], norm[id(ej)])
                    if sim < threshold:
                        continue
                    keep, newer = (ei, ej) if dedup_order_key(ei) <= dedup_order_key(ej) else (ej, ei)
                    # A copy already marked by an earlier pair stays paired with that pair's keeper.
                    if newer["link"] in seen_mark_links:
                        continue
                    seen_mark_links.add(newer["link"])
                    groups.append(
                        {"match_by": "fuzzy", "matched_value": f"{round(sim * 100)}% similar", "keep": keep, "mark_read": [newer]}
                    )
    return groups


def _safe_groups(records: list[dict], false_matches: set[str]) -> list[dict]:
    pair_modes = _safe_dedup_find_pairs(records)
    link_to_rec = {r["link"]: r for r in records if r["link"]}
    by_keep: dict[str, dict] = {}
    seen_mark: set[str] = set()
    # Most signals first, so a copy matched by several keepers is paired with the best-evidenced one.
    for (keep_link, mark_link), modes in sorted(pair_modes.items(), key=lambda kv: -len(kv[1])):
        if keep_link + "||" + mark_link in false_matches:
            continue
        keep_rec = link_to_rec.get(keep_link)
        mark_rec = link_to_rec.get(mark_link)
        if not keep_rec or not mark_rec:
            continue
        if keep_link not in by_keep:
            by_keep[keep_link] = {"match_by": "safe", "matched_value": "+".join(modes), "keep": keep_rec, "mark_read": []}
        if mark_link not in seen_mark:
            by_keep[keep_link]["mark_read"].append(mark_rec)
            seen_mark.add(mark_link)
    return [g for g in by_keep.values() if g["mark_read"]]


def find_groups(
    records: list[dict],
    method: str,
    *,
    window_hours: int,
    fuzzy_threshold: float = 0.80,
    min_title_words: int = _DEDUP_MIN_TITLE_WORDS,
    false_matches: set[str] | None = None,
) -> list[dict]:
    """Duplicate groups among *records*: ``{"match_by", "matched_value", "keep": record, "mark_read": [record, ...]}``. The keeper is
    the oldest copy (dedup_order_key); a group always spans at least two feeds. *false_matches* ("keep||mark" link pairs the user
    rejected) applies to the safe method only."""
    window_secs = window_hours * 3600
    if method == "safe":
        return _safe_groups(records, false_matches or set())
    if method == "fuzzy":
        return _fuzzy_groups(records, window_secs, fuzzy_threshold, min_title_words)
    index: dict = {}
    for r in records:
        link, title = r["link"], r["title"]
        if method == "slug" and link:
            slug = entry_url_slug(link)
            if slug and len(slug) >= 4:
                index.setdefault(slug, []).append(r)
        elif method == "title" and title:
            n = normalize_entry_title_for_dedupe(title)
            if n and len(n.split()) >= min_title_words:
                index.setdefault(n, []).append(r)
        elif method == "both" and link and title:
            slug = entry_url_slug(link)
            n = normalize_entry_title_for_dedupe(title)
            if slug and n:
                index.setdefault((slug, n), []).append(r)
    if method == "slug":
        return _keyed_groups(index, "slug", None)
    if method == "title":
        return _keyed_groups(index, "title", window_secs)
    if method == "both":
        return _keyed_groups(index, "slug+title", window_secs)
    return []


def unread_actionable(groups: list[dict]) -> int:
    """How many marks Run Now would make from these (preview) groups. It loads UNREAD entries only, so a group is reproduced there only
    by its unread members — and one of them becomes the keeper. A pair whose older copy is already read simply does not form."""
    return sum(max(0, sum(1 for e in [g["keep"], *g["mark_read"]] if not e.get("read")) - 1) for g in groups)


def marks_from_groups(groups: list[dict]) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], dict]]:
    """(marked, kept) for Run Now, each keyed (feed_url, entry_id) -> record. A marked record carries ``matched_link``: the keeper it
    was grouped with (the first group that claimed it). Kept excludes anything also marked."""
    marked: dict[tuple[str, str], dict] = {}
    kept: dict[tuple[str, str], dict] = {}
    for g in groups:
        keep = g["keep"]
        kept[(keep["feed_url"], keep["entry_id"])] = keep
        for e in g["mark_read"]:
            marked.setdefault((e["feed_url"], e["entry_id"]), {**e, "matched_link": keep["link"]})
    return marked, {k: v for k, v in kept.items() if k not in marked}


# ── Refresh-time suppression ─────────────────────────────────────────────────

_CHURN_TITLE_MIN_WORDS = _DEDUP_MIN_TITLE_WORDS  # avoids "New post" / "Update" false positives
_CHURN_TITLE_DATE_DAYS = 7  # published dates must be within this many days of each other


def _suppress_guid_churn(reader, conn, feed_url: str) -> int:
    """Auto-mark newly-seen unread entries as read when another entry in the same
    feed already has the same URL slug or the same title + publication date —
    indicating the publisher re-issued the same article with a new GUID and/or
    URL (CMS migration, permalink rebuild, etc.).

    Two matching strategies:
    - Slug match: URL path slug is identical (near-zero false-positive rate).
    - Title+date match: normalised title is identical AND published dates are
      within _CHURN_TITLE_DATE_DAYS.  Requires at least _CHURN_TITLE_MIN_WORDS
      words to guard against short/generic titles like "Update" or "Episode 12".
      Title-only matching is deliberately not used because some feeds reuse
      titles across unrelated entries (weekly digests, daily roundups).

    Compares new entries against both READ history and EXISTING UNREAD entries so
    that duplicate re-publications are caught even when the originals were never read.

    Returns the number of entries suppressed.
    """
    recent_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=90)

    all_unread = list(reader.get_entries(feed=feed_url, read=False))
    new_unread = [e for e in all_unread if getattr(e, "added", None) and e.added >= recent_cutoff]
    if not new_unread:
        return 0

    old_unread = [e for e in all_unread if e not in new_unread]

    # Build slug set and title→[pub_dates] map from read entries (last 6 months).
    history_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=180)
    known_slugs: set[str] = set()
    known_title_dates: dict[str, list[datetime]] = {}
    for entry in reader.get_entries(feed=feed_url, read=True, limit=5000):
        pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
        if pub and pub < history_cutoff:
            break
        if entry.link:
            slug = _safe_dedup_entry_slug(entry.link)
            if slug:
                known_slugs.add(slug)
        if entry.title and pub:
            norm = normalize_entry_title_for_dedupe(entry.title)
            if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS:
                known_title_dates.setdefault(norm, []).append(pub)

    # Also index old unread entries — catches dupes that arrived before the user read any copy.
    for entry in old_unread:
        if entry.link:
            slug = _safe_dedup_entry_slug(entry.link)
            if slug:
                known_slugs.add(slug)
        pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
        if entry.title and pub:
            norm = normalize_entry_title_for_dedupe(entry.title)
            if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS:
                known_title_dates.setdefault(norm, []).append(pub)

    to_suppress = []
    for entry in new_unread:
        # Slug match (highest confidence).
        if entry.link:
            slug = _safe_dedup_entry_slug(entry.link)
            if slug and slug in known_slugs:
                to_suppress.append(entry)
                continue

        # Title+date match (handles feeds that change both GUID and URL).
        if entry.title and known_title_dates:
            norm = normalize_entry_title_for_dedupe(entry.title)
            if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS and norm in known_title_dates:
                entry_pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
                if entry_pub:
                    threshold = _CHURN_TITLE_DATE_DAYS * 86400
                    for known_pub in known_title_dates[norm]:
                        if abs((entry_pub - known_pub).total_seconds()) <= threshold:
                            to_suppress.append(entry)
                            break

    if not to_suppress:
        return 0

    when = datetime.now().isoformat()
    for entry in to_suppress:
        reader.mark_entry_as_read((str(entry.feed_url), str(entry.id)))
    conn.executemany(
        "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)"
        " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at=excluded.read_at",
        [(str(e.feed_url), str(e.id), when) for e in to_suppress],
    )
    return len(to_suppress)


def _cleanup_intra_feed_slug_dupes(
    reader,
    conn,
    *,
    is_saved_feed: Callable[[str], bool],
    normalize_link: Callable[[str], str | None],
) -> int:
    """Retroactive scan: suppress duplicate unread entries that the web UI already
    hides at render time but that mobile GReader clients still see as separate items.

    Two passes:

    Pass 1 — per-feed: slug or title+date duplicates within the same feed.
      Keeps the oldest copy, marks the rest read.

    Pass 2 — cross-feed: entries in different feeds with the same canonical link URL.
      Same article syndicated to two feeds (e.g. blog + planet aggregator).
      Keeps the oldest copy across feeds, marks the rest read.

    Saved Articles (lectio:saved) are excluded from both passes: a saved copy
    shares its link with the source feed's entry, and suppressing it here would
    silently mark the saved copy read and bury it in the read-later backlog.
    Duplicates *within* Saved are handled by the user-driven /saved/duplicates
    scan instead.

    Returns total entries suppressed.
    """
    when = datetime.now().isoformat()
    suppressed_ids: set[tuple[str, str]] = set()
    all_to_suppress: list = []
    threshold = _CHURN_TITLE_DATE_DAYS * 86400

    # ── Pass 1: per-feed slug + title+date dedup ──────────────────────────────
    for feed in reader.get_feeds():
        feed_url = str(feed.url)
        if is_saved_feed(feed_url):
            continue
        try:
            slug_entries: dict[str, list] = {}
            title_entries: dict[str, list] = {}
            for entry in reader.get_entries(feed=feed_url, read=False):
                pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
                pub_ts = pub.timestamp() if pub else 0.0
                if entry.link:
                    slug = _safe_dedup_entry_slug(entry.link)
                    if slug:
                        slug_entries.setdefault(slug, []).append((pub_ts, entry))
                if entry.title and pub:
                    norm = normalize_entry_title_for_dedupe(entry.title)
                    if len(norm.split()) >= _CHURN_TITLE_MIN_WORDS:
                        title_entries.setdefault(norm, []).append((pub_ts, entry))

            for _slug, items in slug_entries.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda x: x[0])
                for _, entry in items[1:]:
                    eid = (str(entry.feed_url), str(entry.id))
                    if eid not in suppressed_ids:
                        suppressed_ids.add(eid)
                        all_to_suppress.append(entry)

            for _norm, items in title_entries.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda x: x[0])
                oldest_ts = items[0][0]
                for pub_ts, entry in items[1:]:
                    if abs(pub_ts - oldest_ts) <= threshold:
                        eid = (str(entry.feed_url), str(entry.id))
                        if eid not in suppressed_ids:
                            suppressed_ids.add(eid)
                            all_to_suppress.append(entry)
        except Exception:
            LOGGER.exception("[guid-churn-cleanup] error on feed %s", feed_url)

    # ── Pass 2: cross-feed identical-link dedup ───────────────────────────────
    # Mirrors the build_entry_dedupe_key logic the web UI uses at render time.
    # An identical canonical link in two different feeds is a near-certain
    # syndication duplicate (blog + planet, RSS + Atom of same feed, etc.).
    try:
        link_entries: dict[str, list] = {}
        for entry in reader.get_entries(read=False):
            if is_saved_feed(str(entry.feed_url)):
                continue
            if not entry.link:
                continue
            canon = normalize_link(entry.link)
            if not canon:
                continue
            pub = getattr(entry, "published", None) or getattr(entry, "updated", None) or getattr(entry, "added", None)
            pub_ts = pub.timestamp() if pub else 0.0
            link_entries.setdefault(canon, []).append((pub_ts, entry))

        for _canon, items in link_entries.items():
            # Only act when entries come from at least two different feeds.
            if len({str(e.feed_url) for _, e in items}) < 2:
                continue
            items.sort(key=lambda x: x[0])
            for _, entry in items[1:]:
                eid = (str(entry.feed_url), str(entry.id))
                if eid not in suppressed_ids:
                    suppressed_ids.add(eid)
                    all_to_suppress.append(entry)
    except Exception:
        LOGGER.exception("[guid-churn-cleanup] error during cross-feed link dedup")

    if not all_to_suppress:
        return 0

    for entry in all_to_suppress:
        reader.mark_entry_as_read((str(entry.feed_url), str(entry.id)))
    conn.executemany(
        "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)"
        " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at=excluded.read_at",
        [(str(e.feed_url), str(e.id), when) for e in all_to_suppress],
    )
    return len(all_to_suppress)
