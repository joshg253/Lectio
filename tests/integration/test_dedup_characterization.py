"""Characterization tests for the deduplicate rule's two engines — the preview (`_dry_run_dedup`) and Run Now (`_run_now_dedup`) —
pinned before they are consolidated into services/dedup.py (Plan.md "Dedup routes consolidation"). One corpus exercises every match
method's edges: cross-feed vs same-feed, the title word floor, the time window, equal-timestamp keeper tie-breaks, three-way groups,
fuzzy thresholds, safe-mode signal combos (and a shared GUID on its own), and read entries (previewed, never acted on).

Expected values were captured from the pre-consolidation code and reviewed by hand; a change here is a behavior change.
"""

from __future__ import annotations

import datetime as dt

import pytest

import main  # must sort before routes.automation — see routes/__init__.py
from routes import automation as automation_routes
from services import automation_rules, tenancy

A = "https://a.test/feed"
B = "https://b.test/feed"
C = "https://c.test/feed"
SCOPE_ID = main._FEEDS_SCOPE_SEP.join([A, B, C])
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
BODY = "<p>" + "Identical syndicated body text shared word for word across both feeds. " * 3 + "</p>"


def _h(hours: float) -> dt.datetime:
    return T0 + dt.timedelta(hours=hours)


# (feed, id, title, link, published, summary, read)
CORPUS = [
    # Same slug + same title in A and B, B an hour later: slug/title/both all group it.
    (A, "slug-a", "The quick brown fox jumps high", "https://a.test/2024/quick-brown-fox", _h(0), "a", False),
    (B, "slug-b", "The quick brown fox jumps high", "https://b.test/news/quick-brown-fox", _h(1), "b", False),
    # ... and a third copy in C, two hours later: one keeper, two marks.
    (C, "slug-c", "The quick brown fox jumps high", "https://c.test/p/quick-brown-fox", _h(2), "c", False),
    # Same title, different slugs: title only.
    (A, "title-a", "Five words make this title", "https://a.test/2024/one-slug", _h(10), "a", False),
    (B, "title-b", "Five words make this title", "https://b.test/2024/other-slug", _h(12), "b", False),
    # Same title ten days apart: outside the 168h window.
    (A, "far-a", "Old story told again much later", "https://a.test/2024/far-one", _h(20), "a", False),
    (C, "far-c", "Old story told again much later", "https://c.test/2024/far-two", _h(20 + 240), "c", False),
    # Under the five-word floor.
    (A, "short-a", "Short title", "https://a.test/2024/short-one", _h(30), "a", False),
    (B, "short-b", "Short title", "https://b.test/2024/short-two", _h(31), "b", False),
    # Same slug twice in ONE feed: not a cross-feed duplicate.
    (A, "same-1", "Only in feed a first copy", "https://a.test/x/same-feed-slug", _h(40), "a", False),
    (A, "same-2", "Only in feed a second copy", "https://a.test/y/same-feed-slug", _h(41), "a", False),
    # Equal timestamps: keeper falls to feed URL, then link.
    (B, "tie-b", "Wire story printed by sister papers", "https://b.test/2024/wire-story", _h(50), "b", False),
    (A, "tie-a", "Wire story printed by sister papers", "https://a.test/2024/wire-story", _h(50), "a", False),
    # Fuzzy: 9 shared words of 11 distinct (0.82).
    (A, "fz-a", "one two three four five six seven eight nine ten", "https://a.test/2024/fz-a", _h(60), "a", False),
    (B, "fz-b", "one two three four five six seven eight nine eleven", "https://b.test/2024/fz-b", _h(61), "b", False),
    # Safe: identical title + identical body, different slugs ({title, body}).
    (A, "safe-a", "Syndicated piece about reading feeds", "https://a.test/2024/safe-one", _h(70), BODY, False),
    (C, "safe-c", "Syndicated piece about reading feeds", "https://c.test/2024/safe-two", _h(71), BODY, False),
    # Shared GUID across feeds: accepted by safe on its own.
    (B, "shared-guid-123", "Deal posted in one search", "https://b.test/deal?x=1", _h(80), "b", False),
    (C, "shared-guid-123", "Different wording of that deal", "https://c.test/deal?y=2", _h(81), "c", False),
    # Read older copy: previewed, but Run Now (unread only) has no pair.
    (A, "rd-a", "Already read original copy here", "https://a.test/2024/read-pair", _h(90), "a", True),
    (B, "rd-b", "Already read original copy here", "https://b.test/2024/read-pair", _h(91), "b", False),
]


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    reader = main.get_reader()
    for feed in (A, B, C):
        reader.add_feed(feed, allow_invalid_url=True)
    for feed, eid, title, link, published, summary, read in CORPUS:
        reader.add_entry({"feed_url": feed, "id": eid, "title": title, "link": link, "summary": summary, "published": published})
        if read:
            reader.mark_entry_as_read((feed, eid))
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _preview(method: str) -> dict:
    with main.get_meta_connection() as conn:
        return automation_routes._dry_run_dedup(conn, "feeds", SCOPE_ID, method, 168)


def _run(method: str) -> dict:
    with main.get_meta_connection() as conn:
        return automation_rules._run_now_dedup(conn, "feeds", SCOPE_ID, method, 168)


def _groups(res: dict) -> list[tuple[str, str, str, tuple[str, ...]]]:
    return sorted(
        (g["match_by"], g["matched_value"], g["keep"]["link"], tuple(sorted(e["link"] for e in g["mark_read"]))) for g in res["groups"]
    )


def _marks(res: dict) -> list[tuple[str, str]]:
    return sorted((e["link"], e["matched_link"]) for e in res["entries"])


def _kept(res: dict) -> list[str]:
    return sorted(e["link"] for e in res["kept"])


def _unread_links() -> set[str]:
    return {str(e.link) for e in main.get_reader().get_entries(read=False)}


FOX_A, FOX_B, FOX_C = "https://a.test/2024/quick-brown-fox", "https://b.test/news/quick-brown-fox", "https://c.test/p/quick-brown-fox"
WIRE_A, WIRE_B = "https://a.test/2024/wire-story", "https://b.test/2024/wire-story"
READ_A, READ_B = "https://a.test/2024/read-pair", "https://b.test/2024/read-pair"
TITLE_A, TITLE_B = "https://a.test/2024/one-slug", "https://b.test/2024/other-slug"
SAFE_A, SAFE_C = "https://a.test/2024/safe-one", "https://c.test/2024/safe-two"
DEAL_B, DEAL_C = "https://b.test/deal?x=1", "https://c.test/deal?y=2"
FZ_A, FZ_B = "https://a.test/2024/fz-a", "https://b.test/2024/fz-b"

EXPECTED_PREVIEW = {
    "slug": (
        [
            ("slug", "deal", DEAL_B, (DEAL_C,)),
            ("slug", "quick-brown-fox", FOX_A, (FOX_B, FOX_C)),
            ("slug", "read-pair", READ_A, (READ_B,)),
            ("slug", "wire-story", WIRE_A, (WIRE_B,)),
        ],
        5,
        4,
    ),
    "title": (
        [
            ("title", "already read original copy here", READ_A, (READ_B,)),
            ("title", "five words make this title", TITLE_A, (TITLE_B,)),
            ("title", "syndicated piece about reading feeds", SAFE_A, (SAFE_C,)),
            ("title", "the quick brown fox jumps high", FOX_A, (FOX_B, FOX_C)),
            ("title", "wire story printed by sister papers", WIRE_A, (WIRE_B,)),
        ],
        6,
        5,
    ),
    "both": (
        [
            ("slug+title", "already read original copy here", READ_A, (READ_B,)),
            ("slug+title", "the quick brown fox jumps high", FOX_A, (FOX_B, FOX_C)),
            ("slug+title", "wire story printed by sister papers", WIRE_A, (WIRE_B,)),
        ],
        4,
        3,
    ),
    # Pairwise: the B-C fox pair is dropped because C is already marked by A-C.
    "fuzzy": (
        [
            ("fuzzy", "100% similar", TITLE_A, (TITLE_B,)),
            ("fuzzy", "100% similar", FOX_A, (FOX_B,)),
            ("fuzzy", "100% similar", FOX_A, (FOX_C,)),
            ("fuzzy", "100% similar", READ_A, (READ_B,)),
            ("fuzzy", "100% similar", SAFE_A, (SAFE_C,)),
            ("fuzzy", "100% similar", WIRE_A, (WIRE_B,)),
            ("fuzzy", "82% similar", FZ_A, (FZ_B,)),
        ],
        7,
        6,
    ),
    # Safe preview reports the unread-actionable count like every other method (it didn't before services/dedup.py).
    "safe": (
        [
            ("safe", "guid", DEAL_B, (DEAL_C,)),
            ("safe", "title+body+body_fuzzy", SAFE_A, (SAFE_C,)),
        ],
        2,
        2,
    ),
}

EXPECTED_RUN = {
    "slug": (
        [(WIRE_B, WIRE_A), (FOX_B, FOX_A), (DEAL_C, DEAL_B), (FOX_C, FOX_A)],
        [FOX_A, WIRE_A, DEAL_B],
    ),
    "title": (
        [(TITLE_B, TITLE_A), (WIRE_B, WIRE_A), (FOX_B, FOX_A), (SAFE_C, SAFE_A), (FOX_C, FOX_A)],
        [TITLE_A, FOX_A, SAFE_A, WIRE_A],
    ),
    "both": (
        [(WIRE_B, WIRE_A), (FOX_B, FOX_A), (FOX_C, FOX_A)],
        [FOX_A, WIRE_A],
    ),
    # FOX_C pairs with the kept FOX_A, as in the preview. (Before services/dedup.py it pointed at FOX_B, itself marked read, because
    # the B-C pair was processed last and overwrote it.)
    "fuzzy": (
        [(FZ_B, FZ_A), (TITLE_B, TITLE_A), (WIRE_B, WIRE_A), (FOX_B, FOX_A), (SAFE_C, SAFE_A), (FOX_C, FOX_A)],
        [FZ_A, TITLE_A, FOX_A, SAFE_A, WIRE_A],
    ),
    "safe": (
        [(SAFE_C, SAFE_A), (DEAL_C, DEAL_B)],
        [SAFE_A, DEAL_B],
    ),
}

METHODS = sorted(EXPECTED_PREVIEW)


@pytest.mark.parametrize("method", METHODS)
def test_preview(env, method):
    groups, would, unread_would = EXPECTED_PREVIEW[method]
    res = _preview(method)
    assert _groups(res) == sorted(groups)
    assert res["total_entries_scanned"] == len(CORPUS)
    assert res["total_would_mark_read"] == would
    assert res.get("total_unread_would_mark_read") == unread_would


@pytest.mark.parametrize("method", METHODS)
def test_preview_marks_nothing(env, method):
    before = _unread_links()
    _preview(method)
    assert _unread_links() == before


@pytest.mark.parametrize("method", METHODS)
def test_run_now(env, method):
    marks, kept = EXPECTED_RUN[method]
    before = _unread_links()
    res = _run(method)
    assert res["count"] == len(marks)
    assert _marks(res) == sorted(marks)
    assert _kept(res) == sorted(kept)
    assert before - _unread_links() == {link for link, _ in marks}


@pytest.mark.parametrize("method", METHODS)
def test_run_now_marks_what_the_preview_calls_actionable(env, method):
    """Every mark Run Now makes is one the preview showed, and never a read entry's group."""
    previewed = {link for _m, _v, _keep, marks in _groups(_preview(method)) for link in marks}
    ran = {link for link, _ in _marks(_run(method))}
    assert ran <= previewed
    assert READ_B not in ran


@pytest.mark.parametrize("method", METHODS)
def test_run_now_on_all_unread_matches_the_preview_exactly(env, method):
    main.get_reader().mark_entry_as_unread((A, "rd-a"))  # nothing read: both engines see the same corpus
    previewed = {link for _m, _v, _keep, marks in _groups(_preview(method)) for link in marks}
    ran = {link for link, _ in _marks(_run(method))}
    assert ran == previewed


def test_false_match_suppresses_a_safe_pair(env):
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO dedup_false_matches (keep_link, mark_link, added_at) VALUES (?, ?, '')", (SAFE_A, SAFE_C))
    assert _groups(_preview("safe")) == [("safe", "guid", DEAL_B, (DEAL_C,))]
    assert _marks(_run("safe")) == [(DEAL_C, DEAL_B)]
