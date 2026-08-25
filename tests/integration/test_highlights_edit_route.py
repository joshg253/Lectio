"""POST /highlights/edit -- the atomic counterpart to the client's old
remove-then-add flow for editing a rule.

A rule's identity is (scope, scope_id, keyword). Editing used to be two
requests against /highlights/remove and /highlights/add; when the identity
was unchanged, sending both raced the add against the remove and destroyed
the rule (both responses OK, so the UI reported success) -- Josh lost a
Deals dedup rule to this 2026-08-20. This route does both in one transaction,
so there is nothing left to race regardless of caller discipline.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed"


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


def _app():
    app = FastAPI()
    app.post("/highlights/add")(main.add_highlight_route)
    app.post("/highlights/edit")(main.edit_highlight_route)
    app.post("/highlights/remove")(main.remove_highlight_route)
    return app


def _rules():
    with main.get_meta_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT scope, scope_id, keyword, color, dedup_window_hours FROM highlight_keywords"
        ).fetchall()]


def test_same_identity_edit_updates_in_place(env):
    """The exact case that used to be destroyed: identity unchanged, only a
    field (color) changes."""
    with TestClient(_app()) as client:
        client.post("/highlights/add", data={"scope": "global", "scope_id": "", "keyword": "python", "color": "yellow"})
        r = client.post("/highlights/edit", data={
            "old_scope": "global", "old_scope_id": "", "old_keyword": "python",
            "scope": "global", "scope_id": "", "keyword": "python", "color": "blue",
        })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    rules = _rules()
    assert len(rules) == 1
    assert rules[0]["color"] == "blue"


def test_changed_identity_moves_the_rule(env):
    """Keyword changes -> the old row is gone and only the new one remains
    (not two rows, and not zero)."""
    with TestClient(_app()) as client:
        client.post("/highlights/add", data={"scope": "global", "scope_id": "", "keyword": "python", "color": "yellow"})
        r = client.post("/highlights/edit", data={
            "old_scope": "global", "old_scope_id": "", "old_keyword": "python",
            "scope": "global", "scope_id": "", "keyword": "rust", "color": "yellow",
        })
    assert r.status_code == 200
    rules = _rules()
    assert len(rules) == 1
    assert rules[0]["keyword"] == "rust"


def test_changed_scope_moves_the_rule(env):
    with TestClient(_app()) as client:
        client.post("/highlights/add", data={"scope": "feed", "scope_id": FEED, "keyword": "python", "color": "yellow"})
        r = client.post("/highlights/edit", data={
            "old_scope": "feed", "old_scope_id": FEED, "old_keyword": "python",
            "scope": "global", "scope_id": "", "keyword": "python", "color": "yellow",
        })
    assert r.status_code == 200
    rules = _rules()
    assert len(rules) == 1
    assert rules[0]["scope"] == "global"


def test_invalid_new_rule_is_rejected_and_old_rule_survives(env):
    """A validation failure on the NEW fields must not have already deleted
    the old identity -- both happen inside one transaction, so a 400 must
    leave the original rule exactly as it was."""
    with TestClient(_app()) as client:
        client.post("/highlights/add", data={"scope": "global", "scope_id": "", "keyword": "python", "color": "yellow"})
        r = client.post("/highlights/edit", data={
            "old_scope": "global", "old_scope_id": "", "old_keyword": "python",
            "scope": "bogus-scope", "scope_id": "", "keyword": "rust", "color": "yellow",
        })
    assert r.status_code == 400
    rules = _rules()
    assert len(rules) == 1
    assert rules[0]["keyword"] == "python"


def test_dedup_rule_survives_a_no_op_edit(env):
    """The exact bug report: a deduplicate rule's match method IS the
    keyword, so every edit (even one that changes nothing else) hits the
    same-identity path. Confirms the rule is not destroyed."""
    scope_id = f"{FEED}\n{FEED}2"
    with TestClient(_app()) as client:
        client.post("/highlights/add", data={
            "scope": "feeds", "scope_id": scope_id, "keyword": "title",
            "type": "deduplicate", "dedup_window_hours": "168",
        })
        r = client.post("/highlights/edit", data={
            "old_scope": "feeds", "old_scope_id": scope_id, "old_keyword": "title",
            "scope": "feeds", "scope_id": scope_id, "keyword": "title",
            "type": "deduplicate", "dedup_window_hours": "72",
        })
    assert r.status_code == 200
    rules = _rules()
    assert len(rules) == 1
    assert rules[0]["dedup_window_hours"] == 72


def test_response_shape_matches_add(env):
    """The client swaps this response straight into its rule list, so the
    shape must match what /highlights/add already returns."""
    with TestClient(_app()) as client:
        add_resp = client.post("/highlights/add", data={"scope": "global", "scope_id": "", "keyword": "python", "color": "yellow"})
        edit_resp = client.post("/highlights/edit", data={
            "old_scope": "global", "old_scope_id": "", "old_keyword": "python",
            "scope": "global", "scope_id": "", "keyword": "python", "color": "blue",
        })
    assert set(add_resp.json().keys()) == set(edit_resp.json().keys())
