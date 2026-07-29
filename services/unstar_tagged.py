"""Unstar entries that also carry a manual tag.

After the tag-as-keep flip a tag *is* a keep signal — tagged entries are
archived and protected from pruning independently of the star — so a star on an
already-tagged entry is redundant. It only clutters Saved, which is meant to be
the read-later queue rather than a second copy of the filing system.

Nothing is lost by unstarring a tagged entry (verified, don't re-derive):
pruning protects starred and manually-tagged entries *independently*, and the
unstar route only enqueues archive removal when the entry has no manual tags, so
a tagged entry keeps its offline capture either way.

This module is the pure decision layer: it takes the current curation and
returns what would change. All DB access, and the cache invalidation a
behind-the-back write requires, stays with the caller.
"""
from __future__ import annotations

from collections import Counter

Key = tuple[str, str]


def build_unstar_plan(
    starred: set[Key],
    tags_by_key: dict[Key, list[str]],
    *,
    keep_tags: set[str] | None = None,
) -> dict:
    """Decide which starred+tagged entries to unstar.

    *starred* is every (feed_url, entry_id) holding a star row; *tags_by_key*
    maps an entry to its manual tags; *keep_tags* is the opt-out set.

    There is no *archived* parameter any more. It existed to warn that deleting
    a star row would discard that entry's `archived_at`, back when the done-flag
    was a column on `saved_entries`. Archived is now its own table, so unstarring
    cannot touch it and there is nothing to warn about.

    **An entry is kept if it carries *any* kept tag.** That is the whole reason
    this is tag-selectable rather than a blanket sweep: the distribution is
    topical filing today (`misc`, `linux-stuff`, `c++`), but `games-to-play` and
    `books` are plausibly aspirational queues worth keeping starred, and an
    entry tagged both `python` and `books` must survive an opt-out on `books`.
    Anything else silently drops entries the user asked to protect.
    """
    keep = {t.strip().lower() for t in (keep_tags or set()) if t and t.strip()}

    affected = {k for k in starred if tags_by_key.get(k)}
    protected = {k for k in affected if any(t.lower() in keep for t in tags_by_key[k])}
    to_unstar = affected - protected

    per_tag: Counter[str] = Counter()
    for key in affected:
        for tag in set(tags_by_key[key]):
            per_tag[tag] += 1

    return {
        "affected": sorted(affected),
        "to_unstar": sorted(to_unstar),
        "protected": sorted(protected),
        "per_tag": [
            {"tag": tag, "count": n, "kept": tag.lower() in keep}
            for tag, n in sorted(per_tag.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "totals": {
            "starred": len(starred),
            "affected": len(affected),
            "to_unstar": len(to_unstar),
            "protected": len(protected),
            "distinct_tags": len(per_tag),
        },
    }


# Tag names that suggest a reading queue rather than topical filing. A star on
# one of these is not redundant — it *is* the queue — so blanket-unstarring
# would gut the read-later backlog. Measured 2026-07-21 and again 2026-07-22:
# zero matches on the live set, every affected tag being topical. Kept as a
# guard because tag vocabularies drift, and this cleanup is meant to be re-run.
_QUEUE_WORDS = ("read", "todo", "later", "queue", "inbox", "pending", "unread", "backlog")


def queue_like_tags(tags: set[str]) -> list[str]:
    """Tags that look like a reading queue, for the caller to surface as a
    suggested opt-out rather than silently excluding them."""
    return sorted(t for t in tags if any(w in t.lower() for w in _QUEUE_WORDS))
