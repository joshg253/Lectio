"""Rules that could be one rule (Settings -> Automation -> Rules): a keyword
was one term until comma lists landed, so multiple single-keyword rules on the
same (type, scope, scope_id, search_in, is_regex) accumulated where one
multi-keyword rule would now do. Suggestion-with-preview, never automatic
(decided 2026-08-24) -- find_mergeable_rule_groups/merge_highlight_rule_group
scan and apply; find_redundant_feed_rules flags a feed rule already covered by
a same-type folder rule, for removal via the existing /highlights/remove.

Verified 2026-08-19 against the live library: 3 groups (2 mark_as_read on
folder 8, 3 highlight on folder 9, 3 highlight global) = 5 rules that collapse
to 3. Two of those three real groups (folder 9, global) mix colors across
their rules -- exercised here as the "mismatched" case that must NOT silently
merge onto one color.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
FEED2 = "https://other.test/feed"


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
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _add_rule(scope, scope_id, keyword, **kw):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn, scope, scope_id, keyword,
            kw.get("color", "yellow"), kw.get("is_regex", False),
            kw.get("type", "highlight"), kw.get("search_in", "title"),
            kw.get("delivery", "immediately"), kw.get("email_to", ""),
            kw.get("batch_time", ""), kw.get("batch_count", 0), kw.get("cc_me", False),
            kw.get("enabled", 1),
        )
        conn.commit()


def _make_folder(name) -> int:
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        conn.execute("INSERT INTO folders (name, parent_id) VALUES (?, ?)", (name, root))
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return int(fid)


def _add_feed_to_folder(feed_url, folder_id):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (folder_id, feed_url),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# find_mergeable_rule_groups
# ---------------------------------------------------------------------------

def test_three_same_identity_rules_form_one_group(env):
    fid = _make_folder("Dev")
    _add_rule("folder", str(fid), "C#", color="blue", search_in="both")
    _add_rule("folder", str(fid), "C++", color="blue", search_in="both")
    _add_rule("folder", str(fid), "Python", color="blue", search_in="both")
    with main.get_meta_connection() as conn:
        groups, mismatched = main.find_mergeable_rule_groups(conn)
    assert mismatched == []
    assert len(groups) == 1
    g = groups[0]
    assert g["type"] == "highlight" and g["scope"] == "folder" and g["scope_id"] == str(fid)
    assert {r["keyword"] for r in g["rules"]} == {"C#", "C++", "Python"}


def test_a_single_rule_is_not_a_group(env):
    _add_rule("global", "", "solo")
    with main.get_meta_connection() as conn:
        groups, mismatched = main.find_mergeable_rule_groups(conn)
    assert groups == [] and mismatched == []


def test_different_scope_ids_never_group(env):
    fid1, fid2 = _make_folder("A"), _make_folder("B")
    _add_rule("folder", str(fid1), "kw1")
    _add_rule("folder", str(fid2), "kw2")
    with main.get_meta_connection() as conn:
        groups, _ = main.find_mergeable_rule_groups(conn)
    assert groups == []


def test_different_regex_flag_never_groups(env):
    _add_rule("global", "", "kw1", is_regex=False)
    _add_rule("global", "", "kw2", is_regex=True)
    with main.get_meta_connection() as conn:
        groups, _ = main.find_mergeable_rule_groups(conn)
    assert groups == []


def test_deduplicate_and_tag_filter_types_are_never_offered(env):
    """deduplicate's keyword is a match-method enum; tag_filter's is a
    +/-tag spec -- joining either as a plain OR-list would corrupt it."""
    fid = _make_folder("Dup")
    _add_rule("folder", str(fid), "slug", type="deduplicate")
    _add_rule("folder", str(fid), "title", type="deduplicate")
    _add_rule("feed", FEED, "+python", type="tag_filter")
    _add_rule("feed", FEED, "-rust", type="tag_filter")
    with main.get_meta_connection() as conn:
        groups, _ = main.find_mergeable_rule_groups(conn)
    assert groups == []


def test_partial_color_agreement_forms_a_group_and_leaves_the_singleton_unreported(env):
    """Raised 2026-08-31 by Josh's real 5-rule example (orange/blue/blue/
    green/orange): rules that already agree on settings should merge even
    when they share an identity with others that don't. C#/C++ (both blue)
    become a group; Python (green, no partner) is not reported anywhere --
    same as a genuinely solo identity group always has been. Merging must
    still never silently pick a color across rules that actually disagree
    (see test_mismatched_delivery_settings_are_flagged_not_grouped)."""
    fid = _make_folder("Mixed")
    _add_rule("folder", str(fid), "C#", color="blue")
    _add_rule("folder", str(fid), "C++", color="blue")
    _add_rule("folder", str(fid), "Python", color="green")
    with main.get_meta_connection() as conn:
        groups, mismatched = main.find_mergeable_rule_groups(conn)
    assert mismatched == []
    assert len(groups) == 1
    assert {r["keyword"] for r in groups[0]["rules"]} == {"C#", "C++"}


def test_two_disjoint_pairs_merge_leaving_no_residual_mismatch(env):
    """Josh's literal report (2026-08-31): 5 rules, orange/blue/blue/green/
    orange. Dillinger and BTBAM (orange) merge, C#/C++ (blue) merge, Python
    (green, solo) is reported nowhere -- every non-singleton settings-value
    already got its own group, so there's no 2+-way disagreement left to
    flag as mismatched."""
    _add_rule("global", "", "Dillinger Escape Plan", color="orange")
    _add_rule("global", "", "C#", color="blue")
    _add_rule("global", "", "C++", color="blue")
    _add_rule("global", "", "Python", color="green")
    _add_rule("global", "", "Between the Buried and Me", color="orange")
    with main.get_meta_connection() as conn:
        groups, mismatched = main.find_mergeable_rule_groups(conn)
    assert mismatched == []
    assert len(groups) == 2
    keyword_sets = [{r["keyword"] for r in g["rules"]} for g in groups]
    assert {"Dillinger Escape Plan", "Between the Buried and Me"} in keyword_sets
    assert {"C#", "C++"} in keyword_sets


def test_mismatched_delivery_settings_are_flagged_not_grouped(env):
    fid = _make_folder("Notif")
    _add_rule("folder", str(fid), "a", delivery="immediately")
    _add_rule("folder", str(fid), "b", delivery="batch", email_to="me@example.test")
    with main.get_meta_connection() as conn:
        groups, mismatched = main.find_mergeable_rule_groups(conn)
    assert groups == []
    assert len(mismatched) == 1


# ---------------------------------------------------------------------------
# merge_highlight_rule_group
# ---------------------------------------------------------------------------

def test_merge_joins_plain_keywords_as_a_comma_list(env):
    fid = _make_folder("Dev")
    _add_rule("folder", str(fid), "C#", color="blue", search_in="both")
    _add_rule("folder", str(fid), "C++", color="blue", search_in="both")
    with main.get_meta_connection() as conn:
        result = main.merge_highlight_rule_group(
            conn, "highlight", "folder", str(fid), "both", False, "blue", "immediately", "", "", 0, False,
        )
        conn.commit()
        rows = conn.execute("SELECT keyword, color FROM highlight_keywords WHERE scope_id = ?", (str(fid),)).fetchall()
    assert result is not None
    assert result["merged_count"] == 2
    assert len(rows) == 1
    assert set(rows[0]["keyword"].split(", ")) == {"C#", "C++"}
    assert rows[0]["color"] == "blue"  # template's setting preserved


def test_merge_joins_regex_keywords_as_alternation(env):
    _add_rule("global", "", "foo.*bar", is_regex=True)
    _add_rule("global", "", "baz+", is_regex=True)
    with main.get_meta_connection() as conn:
        main.merge_highlight_rule_group(
            conn, "highlight", "global", "", "title", True, "yellow", "immediately", "", "", 0, False,
        )
        conn.commit()
        rows = conn.execute("SELECT keyword FROM highlight_keywords WHERE scope = 'global'").fetchall()
    assert len(rows) == 1
    assert rows[0]["keyword"] == "(foo.*bar)|(baz+)"


def test_merge_dedupes_overlapping_individual_keywords(env):
    """One rule is already a comma list; merging must not repeat a term the
    other side already has."""
    _add_rule("global", "", "python, rust")
    _add_rule("global", "", "rust, go")
    with main.get_meta_connection() as conn:
        main.merge_highlight_rule_group(
            conn, "highlight", "global", "", "title", False, "yellow", "immediately", "", "", 0, False,
        )
        conn.commit()
        rows = conn.execute("SELECT keyword FROM highlight_keywords WHERE scope = 'global'").fetchall()
    assert len(rows) == 1
    parts = [p.strip() for p in rows[0]["keyword"].split(",")]
    assert parts == ["python", "rust", "go"]  # order-preserving, no repeat


def test_merge_preserves_min_sort_order(env):
    _add_rule("global", "", "a")
    _add_rule("global", "", "b")
    with main.get_meta_connection() as conn:
        before = {r["keyword"]: r["sort_order"] for r in conn.execute(
            "SELECT keyword, sort_order FROM highlight_keywords WHERE scope = 'global'")}
        conn.execute("UPDATE highlight_keywords SET sort_order = 5 WHERE keyword = 'a'")
        conn.execute("UPDATE highlight_keywords SET sort_order = 9 WHERE keyword = 'b'")
        conn.commit()
        main.merge_highlight_rule_group(
            conn, "highlight", "global", "", "title", False, "yellow", "immediately", "", "", 0, False,
        )
        conn.commit()
        row = conn.execute("SELECT sort_order FROM highlight_keywords WHERE scope = 'global'").fetchone()
    assert row["sort_order"] == 5
    assert before  # sanity: fixture actually ran


def test_merge_refuses_when_settings_mismatch(env):
    """Requesting a merge for one specific settings value only picks up rows
    that actually match it -- if the group's other row uses different
    settings, fewer than 2 rows match the request and nothing merges."""
    fid = _make_folder("Mixed")
    _add_rule("folder", str(fid), "a", color="blue")
    _add_rule("folder", str(fid), "b", color="green")
    with main.get_meta_connection() as conn:
        result = main.merge_highlight_rule_group(
            conn, "highlight", "folder", str(fid), "title", False, "blue", "immediately", "", "", 0, False,
        )
        rows = conn.execute("SELECT COUNT(*) FROM highlight_keywords").fetchone()[0]
    assert result is None
    assert rows == 2  # nothing was touched


def test_merge_refuses_a_stale_group(env):
    """Only one rule matches now (removed since the preview was shown) --
    nothing to merge, and the surviving rule must be untouched."""
    _add_rule("global", "", "a")
    with main.get_meta_connection() as conn:
        result = main.merge_highlight_rule_group(
            conn, "highlight", "global", "", "title", False, "yellow", "immediately", "", "", 0, False,
        )
        rows = conn.execute("SELECT keyword FROM highlight_keywords").fetchall()
    assert result is None
    assert [r["keyword"] for r in rows] == ["a"]


def test_merge_refuses_a_non_mergeable_type(env):
    fid = _make_folder("Dup")
    _add_rule("folder", str(fid), "slug", type="deduplicate")
    _add_rule("folder", str(fid), "title", type="deduplicate")
    with main.get_meta_connection() as conn:
        result = main.merge_highlight_rule_group(
            conn, "deduplicate", "folder", str(fid), "title", False, "yellow", "immediately", "", "", 0, False,
        )
    assert result is None


# ---------------------------------------------------------------------------
# find_redundant_feed_rules
# ---------------------------------------------------------------------------

def test_feed_rule_covered_by_folder_rule_is_flagged(env):
    fid = _make_folder("Deals")
    _add_feed_to_folder(FEED, fid)
    _add_rule("folder", str(fid), "iphone, macbook", type="mark_as_read")
    _add_rule("feed", FEED, "iphone", type="mark_as_read")
    with main.get_meta_connection() as conn:
        redundant = main.find_redundant_feed_rules(conn)
    assert len(redundant) == 1
    assert redundant[0]["feed_url"] == FEED
    assert redundant[0]["covering_folder_id"] == fid


def test_feed_rule_with_an_extra_keyword_is_not_flagged(env):
    """The feed rule matches something the folder rule doesn't -- it is not
    fully redundant, so it must not be flagged."""
    fid = _make_folder("Deals")
    _add_feed_to_folder(FEED, fid)
    _add_rule("folder", str(fid), "iphone", type="mark_as_read")
    _add_rule("feed", FEED, "iphone, ipad", type="mark_as_read")
    with main.get_meta_connection() as conn:
        redundant = main.find_redundant_feed_rules(conn)
    assert redundant == []


def test_regex_feed_rules_are_never_flagged(env):
    fid = _make_folder("Deals")
    _add_feed_to_folder(FEED, fid)
    _add_rule("folder", str(fid), "iphone", type="mark_as_read")
    _add_rule("feed", FEED, "iphone", type="mark_as_read", is_regex=True)
    with main.get_meta_connection() as conn:
        redundant = main.find_redundant_feed_rules(conn)
    assert redundant == []


def test_feed_not_in_the_covering_folder_is_not_flagged(env):
    fid = _make_folder("Deals")
    # FEED is never added to fid.
    _add_rule("folder", str(fid), "iphone", type="mark_as_read")
    _add_rule("feed", FEED, "iphone", type="mark_as_read")
    with main.get_meta_connection() as conn:
        redundant = main.find_redundant_feed_rules(conn)
    assert redundant == []


def test_different_search_in_does_not_cover(env):
    fid = _make_folder("Deals")
    _add_feed_to_folder(FEED, fid)
    _add_rule("folder", str(fid), "iphone", type="mark_as_read", search_in="title")
    _add_rule("feed", FEED, "iphone", type="mark_as_read", search_in="body")
    with main.get_meta_connection() as conn:
        redundant = main.find_redundant_feed_rules(conn)
    assert redundant == []


def test_live_library_shape_is_reproduced(env):
    """The exact 2026-08-19 measurement, re-verified 2026-08-31 after
    settings-subgrouping landed: 8 rules across 3 identities collapse to 3
    groups (folder 8's pair, folder 9's blue pair, global's orange pair).
    Python and PS4 are solo leftovers with no partner to disagree with, so
    neither is reported in either bucket."""
    fid8, fid9 = _make_folder("Deals"), _make_folder("Dev")
    _add_rule("folder", str(fid8), "AirPods|iPhone|MacBook|AppleTV", type="mark_as_read", is_regex=True)
    _add_rule("folder", str(fid8), "Micro ?Center", type="mark_as_read", is_regex=True)
    _add_rule("folder", str(fid9), "C#", color="blue", search_in="both")
    _add_rule("folder", str(fid9), "C++", color="blue", search_in="both")
    _add_rule("folder", str(fid9), "Python", color="green", search_in="both")
    _add_rule("global", "", "Between the Buried and Me", color="orange", search_in="both")
    _add_rule("global", "", "Dillinger Escape Plan", color="orange", search_in="both")
    _add_rule("global", "", "PS4", color="blue", search_in="both")
    with main.get_meta_connection() as conn:
        groups, mismatched = main.find_mergeable_rule_groups(conn)
    assert len(groups) == 3  # folder 8's pair, folder 9's blue pair, global's orange pair
    assert mismatched == []
    assert sum(len(g["rules"]) for g in groups) == 6  # 3 pairs of 2


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _app():
    from fastapi import FastAPI
    app = FastAPI()
    app.get("/highlights/suggestions")(main.get_highlight_suggestions_route)
    app.post("/highlights/merge-group")(main.merge_highlight_group_route)
    return app


def test_suggestions_route_returns_all_three_buckets(env):
    from fastapi.testclient import TestClient

    fid = _make_folder("Deals")
    _add_feed_to_folder(FEED, fid)
    _add_rule("folder", str(fid), "iphone, macbook", type="mark_as_read")
    _add_rule("feed", FEED, "iphone", type="mark_as_read")
    _add_rule("global", "", "a")
    _add_rule("global", "", "b")

    with TestClient(_app()) as client:
        r = client.get("/highlights/suggestions")
    assert r.status_code == 200
    data = r.json()
    assert len(data["mergeable"]) == 1
    assert len(data["redundant"]) == 1
    assert data["mismatched"] == []


def test_merge_group_route_applies_and_persists(env):
    from fastapi.testclient import TestClient

    _add_rule("global", "", "a")
    _add_rule("global", "", "b")
    with TestClient(_app()) as client:
        r = client.post("/highlights/merge-group", data={
            "type": "highlight", "scope": "global", "scope_id": "", "search_in": "title", "is_regex": "0",
            "color": "yellow", "delivery": "immediately", "email_to": "", "batch_time": "",
            "batch_count": "0", "cc_me": "0",
        })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with main.get_meta_connection() as conn:
        rows = conn.execute("SELECT keyword FROM highlight_keywords WHERE scope = 'global'").fetchall()
    assert len(rows) == 1


def test_merge_group_route_409s_on_a_stale_group(env):
    from fastapi.testclient import TestClient

    _add_rule("global", "", "a")
    with TestClient(_app()) as client:
        r = client.post("/highlights/merge-group", data={
            "type": "highlight", "scope": "global", "scope_id": "", "search_in": "title", "is_regex": "0",
            "color": "yellow", "delivery": "immediately", "email_to": "", "batch_time": "",
            "batch_count": "0", "cc_me": "0",
        })
    assert r.status_code == 409
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# find_regex_convertible_rule_groups / merge_regex_convertible_rule_group
#
# Raised 2026-08-31: "Lowe's" (plain) sat in the same Deals folder as the two
# regex rules above (test_live_library_shape_is_reproduced) with nothing
# offering to fold it in, even though a plain keyword is always representable
# as an escaped regex.
# ---------------------------------------------------------------------------

def test_a_plain_and_a_regex_rule_on_the_same_scope_are_flagged(env):
    fid = _make_folder("Deals")
    _add_rule("folder", str(fid), "Lowe's", type="mark_as_read", is_regex=False)
    _add_rule("folder", str(fid), "AirPods|iPhone", type="mark_as_read", is_regex=True)
    with main.get_meta_connection() as conn:
        groups = main.find_regex_convertible_rule_groups(conn)
    assert len(groups) == 1
    assert {r["keyword"] for r in groups[0]["rules"]} == {"Lowe's", "AirPods|iPhone"}


def test_all_regex_or_all_plain_groups_are_excluded(env):
    """A same-is_regex group is find_mergeable_rule_groups' territory, not
    this one -- both-regex or both-plain groups must not double up here."""
    _add_rule("global", "", "a", is_regex=True)
    _add_rule("global", "", "b", is_regex=True)
    with main.get_meta_connection() as conn:
        assert main.find_regex_convertible_rule_groups(conn) == []
    _add_rule("feed", FEED, "c", is_regex=False)
    _add_rule("feed", FEED, "d", is_regex=False)
    with main.get_meta_connection() as conn:
        assert main.find_regex_convertible_rule_groups(conn) == []


def test_mismatched_settings_are_excluded_from_regex_conversion_too(env):
    fid = _make_folder("Mixed")
    _add_rule("folder", str(fid), "a", is_regex=False, color="blue")
    _add_rule("folder", str(fid), "b", is_regex=True, color="green")
    with main.get_meta_connection() as conn:
        groups = main.find_regex_convertible_rule_groups(conn)
    assert groups == []


def test_merge_regex_convert_escapes_the_plain_keyword(env):
    _add_rule("global", "", "Lowe's", is_regex=False)
    _add_rule("global", "", "AirPods|iPhone", is_regex=True)
    with main.get_meta_connection() as conn:
        result = main.merge_regex_convertible_rule_group(conn, "highlight", "global", "", "title")
        conn.commit()
        rows = conn.execute("SELECT keyword, is_regex FROM highlight_keywords WHERE scope = 'global'").fetchall()
    assert result is not None
    assert len(rows) == 1
    assert rows[0]["is_regex"] == 1
    # re.escape leaves the apostrophe alone (not a regex metacharacter, and
    # not escaped by Python's re.escape since 3.7) -- Josh's own reason for
    # not hand-writing this rule as regex was hesitating over exactly that
    # escaping, which turns out to be a non-issue here.
    assert rows[0]["keyword"] == "(Lowe's)|(AirPods|iPhone)"
    import re as _re
    assert _re.search(rows[0]["keyword"], "Lowe's has a sale")
    assert _re.search(rows[0]["keyword"], "New AirPods dropped")
    assert not _re.search(rows[0]["keyword"], "Something else entirely")


def test_merge_regex_convert_refuses_when_settings_mismatch(env):
    fid = _make_folder("Mixed")
    _add_rule("folder", str(fid), "a", is_regex=False, color="blue")
    _add_rule("folder", str(fid), "b", is_regex=True, color="green")
    with main.get_meta_connection() as conn:
        result = main.merge_regex_convertible_rule_group(conn, "highlight", "folder", str(fid), "title")
        rows = conn.execute("SELECT COUNT(*) FROM highlight_keywords").fetchone()[0]
    assert result is None
    assert rows == 2


def test_merge_regex_convert_refuses_a_stale_all_regex_group(env):
    _add_rule("global", "", "a", is_regex=True)
    _add_rule("global", "", "b", is_regex=True)
    with main.get_meta_connection() as conn:
        result = main.merge_regex_convertible_rule_group(conn, "highlight", "global", "", "title")
    assert result is None


def test_suggestions_route_includes_regex_convertible_bucket(env):
    from fastapi.testclient import TestClient

    fid = _make_folder("Deals")
    _add_rule("folder", str(fid), "Lowe's", type="mark_as_read", is_regex=False)
    _add_rule("folder", str(fid), "AirPods|iPhone", type="mark_as_read", is_regex=True)

    app = _app()
    app.post("/highlights/merge-group-regex-convert")(main.merge_highlight_group_regex_convert_route)
    with TestClient(app) as client:
        r = client.get("/highlights/suggestions")
    assert r.status_code == 200
    assert len(r.json()["regex_convertible"]) == 1


def test_merge_group_regex_convert_route_applies_and_persists(env):
    from fastapi.testclient import TestClient

    _add_rule("global", "", "Lowe's", is_regex=False)
    _add_rule("global", "", "AirPods|iPhone", is_regex=True)
    app = _app()
    app.post("/highlights/merge-group-regex-convert")(main.merge_highlight_group_regex_convert_route)
    with TestClient(app) as client:
        r = client.post("/highlights/merge-group-regex-convert", data={
            "type": "highlight", "scope": "global", "scope_id": "", "search_in": "title",
        })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with main.get_meta_connection() as conn:
        rows = conn.execute("SELECT keyword, is_regex FROM highlight_keywords WHERE scope = 'global'").fetchall()
    assert len(rows) == 1 and rows[0]["is_regex"] == 1
