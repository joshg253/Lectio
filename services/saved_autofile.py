"""Match unfiled saved articles to the subscribed feed they belong to.

Read-later captures pile up in the synthetic ``lectio:saved`` feed with no feed
of their own, even when the article came from a site already subscribed to —
the common case being an Instapaper library imported from a feed reader, where
almost every article originated in a feed. Filing them onto their real feed is
what makes the Saved view organized, and it collapses cross-feed duplicates for
free, because `_move_entry_to_feed` matches into the target by GUID else
normalized link.

Matching is by **article host**, not feed-URL host: a feed's own URL often lives
on a different host than the articles it publishes (``rss.beehiiv.com`` serving
``joanwestenberg.com`` posts), so the reliable signal is "which subscribed feed
already carries entries whose links are on this host".

The plan this builds is a *proposal*. Nothing here moves anything; the caller
presents it for approval per host. Two guards decide what may be auto-approved:

  ambiguous — more than one *on-host* subscribed feed carries entries on the
              host, so picking one would be a guess. Off-host feeds (aggregators
              and link blogs, which carry links to everywhere) are ranked below
              the site's own feed and don't make a choice ambiguous on their own.
  support   — how many of the target feed's own entries are on that host. A
              single supporting entry usually means the "feed" is a scraped
              one-article URL, not the site's real feed; filing hundreds of
              articles into it would be wrong. `MIN_SUPPORT` is the floor for
              calling a target confident.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from urllib.parse import urlparse

# Below this many supporting entries a target is shown but not pre-approved.
# 3 is deliberately low: it only has to separate "this feed really publishes
# this host" from a one-off scraped URL that happens to share the domain.
MIN_SUPPORT = 3


def article_host(url: str | None) -> str:
    """Comparison host for an article URL: lowercased, no ``www.``, no port."""
    if not url:
        return ""
    host = urlparse(str(url)).netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def build_autofile_plan(
    saved_rows: list[tuple[str, str]],
    feed_entry_links: list[tuple[str, str]],
    *,
    feed_titles: dict[str, str] | None = None,
    feed_hosts: dict[str, set[str]] | None = None,
    feed_sizes: dict[str, int] | None = None,
    min_support: int = MIN_SUPPORT,
    exclude_feeds: frozenset[str] = frozenset(),
) -> list[dict]:
    """Group unfiled saved articles by host and propose a target feed for each.

    *saved_rows* is (entry_id, link) for the articles to file; *feed_entry_links*
    is (feed_url, link) over every entry in real feeds — the evidence for which
    feed owns a host. *feed_hosts* is the hosts each feed *declares* (its own URL
    host and the site link it advertises) and *feed_sizes* how many entries it
    holds. Returns clusters sorted by size, largest first, so the reviewer's
    first few decisions cover most of the backlog.

    Entry links alone are not enough evidence. A feed subscribed but not yet
    fetched has no entries at all, so a feed added specifically to receive a
    backlog would never be offered for it. And a feed that proxies its links
    (FeedBurner rewrites every entry link to feeds.feedburner.com) points its
    evidence at the wrong host entirely — measured here, 696 of 2,881 feeds
    advertise a site on a different host than their feed URL. A declared host
    therefore makes a feed a candidate on its own.
    """
    titles = feed_titles or {}
    sizes = feed_sizes or {}

    # host -> feed_url -> how many of that feed's entries are on the host
    host_feeds: dict[str, Counter] = defaultdict(Counter)
    seen_feeds: set[str] = set()
    for feed_url, link in feed_entry_links:
        if feed_url in exclude_feeds:
            continue
        seen_feeds.add(feed_url)
        host = article_host(link)
        if host:
            host_feeds[host][feed_url] += 1

    # Absent explicit declarations, a feed still declares its own URL's host —
    # otherwise a caller that omits `feed_hosts` silently loses the preference
    # for a site's own feed over an aggregator that links to it.
    declared = feed_hosts if feed_hosts is not None else {
        f: {article_host(f)} for f in seen_feeds
    }
    # A declared host makes the feed a candidate even with no matching entries.
    declares: dict[str, set[str]] = defaultdict(set)
    for feed_url, hosts in declared.items():
        if feed_url in exclude_feeds:
            continue
        for host in hosts:
            if host:
                declares[host].add(feed_url)
                host_feeds[host].setdefault(feed_url, 0)

    by_host: dict[str, list[str]] = defaultdict(list)
    for entry_id, link in saved_rows:
        host = article_host(link)
        if host:
            by_host[host].append(entry_id)

    plan: list[dict] = []
    for host, entry_ids in by_host.items():
        candidates = host_feeds.get(host) or Counter()
        # A feed served from the host itself is the site's own feed. Anything
        # else carrying links to this host is an aggregator or link blog (Hacker
        # News showed up as a candidate for 16 different hosts), and by raw
        # count it can outrank the real thing — one link blog beat a site's own
        # feed 23 posts to 11. On-host candidates therefore rank first, and
        # off-host ones never make a choice "ambiguous" when an on-host feed
        # exists: they aren't competing for the same job.
        host_declarers = declares.get(host, set())
        ranked = sorted(
            candidates.items(),
            key=lambda kv: (kv[0] not in host_declarers, -kv[1], kv[0]),
        )
        on_host = [f for f, _ in ranked if f in host_declarers]
        target, support = (ranked[0] if ranked else (None, 0))
        ambiguous = len(on_host) > 1 if on_host else len(ranked) > 1
        # A feed that declares the host is trustworthy once it is demonstrably a
        # real feed — enough entries of its own — even if none of them happen to
        # be on this host yet. Without the size check a scraped single-article
        # URL sitting in the subscription list (one entry, its own host) would
        # read as the site's feed and collect the whole backlog.
        declares_and_stocked = (
            target in host_declarers and sizes.get(target, 0) >= min_support
        )
        plan.append({
            "host": host,
            "count": len(entry_ids),
            "entry_ids": entry_ids,
            "target_feed_url": target,
            "target_title": titles.get(target or "", target or ""),
            "support": support,
            "ambiguous": ambiguous,
            "declared": target in host_declarers,
            # Auto-approvable only when the choice is unambiguous *and* the
            # target is either evidenced by its own entries on this host or is a
            # stocked feed that declares it.
            "confident": bool(target) and not ambiguous and (
                support >= min_support or declares_and_stocked
            ),
            "candidates": [
                {"feed_url": f, "title": titles.get(f, f), "support": n}
                for f, n in ranked[:5]
            ],
        })

    plan.sort(key=lambda c: (-c["count"], c["host"]))
    return plan


def plan_totals(plan: list[dict]) -> dict:
    """Headline counts for the preview header."""
    return {
        "hosts": len(plan),
        "articles": sum(c["count"] for c in plan),
        "confident_hosts": sum(1 for c in plan if c["confident"]),
        "confident_articles": sum(c["count"] for c in plan if c["confident"]),
        "ambiguous_articles": sum(c["count"] for c in plan if c["ambiguous"]),
        "unmatched_articles": sum(c["count"] for c in plan if not c["target_feed_url"]),
        "low_support_articles": sum(
            c["count"] for c in plan
            if c["target_feed_url"] and not c["ambiguous"] and not c["confident"]
        ),
    }
