"""Bulk-archive stars older than N days — the Inbox bankruptcy pass.

The Inbox is starred-minus-archived, and after years of starring under the *old*
meaning (where a star meant "keep this") it opened at 9,979 items. Renaming the
axis to TODO does not reclassify them: anything starred months ago is, by any
honest reading, not something you are still going to get to.

**Why this rather than #5's unstar-tagged sweep.** #5 removed the star from
entries that also carried a tag, on the premise that a tag already kept them. That
premise died when a star came to mean *to-do* and a tag *filing* — starred+tagged
then means "filed, and still to deal with", an ordinary state. Archiving is the
better instrument: it clears the queue while keeping the tag, the offline capture,
and pruning-exemption, and it is reversible per item. Nothing is lost.

This module is the pure decision layer: it takes the current curation and returns
what would change. Every DB write, and the cache invalidation a behind-the-back
write needs, stays with the caller.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

Key = tuple[str, str]

# Offered in the UI. Deliberately coarse: the point is "declare bankruptcy on
# stars I am plainly not getting to", not fine-grained date surgery.
DAY_CHOICES: tuple[int, ...] = (7, 30, 90, 180, 365)
DEFAULT_DAYS = 30


def build_archive_plan(
    starred_at: dict[Key, datetime | None],
    archived: set[Key],
    *,
    days: int,
    now: datetime | None = None,
) -> dict:
    """Decide which stars to archive.

    *starred_at* maps every star row to when it was starred; *archived* is the
    current done-axis membership. An entry already archived is skipped — it has
    no star left to discharge.

    Ages are bucketed for the preview so the number can be sanity-checked before
    9,000 items move. A single total is easy to mistrust and impossible to check.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days)))

    to_archive: list[Key] = []
    for key, when in starred_at.items():
        if key in archived:
            continue
        if when is None:
            # No star date at all: leave it alone rather than guess. A missing
            # timestamp is not evidence of age.
            continue
        if _as_utc(when) < cutoff:
            to_archive.append(key)

    return {
        "days": int(days),
        "cutoff": cutoff.isoformat(),
        "to_archive": sorted(to_archive),
        "totals": {
            "starred": len(starred_at),
            "already_archived": len(archived),
            "to_archive": len(to_archive),
            "remaining": len(starred_at) - len(archived) - len(to_archive),
        },
        "buckets": age_buckets(starred_at, archived, now=now),
    }


def age_buckets(
    starred_at: dict[Key, datetime | None],
    archived: set[Key],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """How the un-archived stars distribute by age, for the preview table."""
    now = now or datetime.now(timezone.utc)
    edges = [(0, 7), (7, 30), (30, 90), (90, 365), (365, None)]
    labels = ["under a week", "1 week – 1 month", "1–3 months", "3–12 months", "over a year"]
    counts = [0] * len(edges)
    for key, when in starred_at.items():
        if key in archived or when is None:
            continue
        age_days = (now - _as_utc(when)).days
        for i, (lo, hi) in enumerate(edges):
            if age_days >= lo and (hi is None or age_days < hi):
                counts[i] += 1
                break
    return [{"label": label, "count": n} for label, n in zip(labels, counts, strict=True)]


def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are stored as UTC (SQLite CURRENT_TIMESTAMP), so treating
    them as local would shift the cutoff by the host's offset."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
