"""Tag aliases: fold one spelling of a tag into another.

Publishers disagree about the same subject, so one topic arrives under several
spellings and filtering on one silently misses the rest. The live library had
c++ (4,965 uses) beside cpp (223), and c# (606) beside csharp (152).
"""

from __future__ import annotations

import pytest

import main
from services import tenancy


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
    main.invalidate_tag_alias_cache()
    try:
        yield
    finally:
        main.invalidate_tag_alias_cache()
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_normalize_applies_the_alias(env):
    main.create_tag_alias("cpp", "c++", rewrite=False)
    assert main.normalize_tag_value("cpp") == "c++"
    assert main.normalize_tag_value("CPP") == "c++"


def test_a_tag_with_no_alias_is_left_alone(env):
    """The other half of the rule: aliasing must be a lookup, not a rewrite that everything passes through.
    Every tag in the library goes through normalize_tag_value, so a bug that remapped unmatched tags would
    corrupt the whole vocabulary rather than fail visibly."""
    main.create_tag_alias("cpp", "c++", rewrite=False)

    assert main.normalize_tag_value("rust") == "rust"
    assert main.normalize_tag_value("Rust") == "rust"          # still case-normalized
    assert main.normalize_tag_value("c++") == "c++"            # the canonical side is not re-aliased
    assert main.normalize_tag_value("cpp-lang") == "cpp-lang"  # not a prefix match on the alias


def test_raw_normalize_does_not(env):
    """The editor needs this: normalizing through the aliased path would return
    'c++' for 'cpp' the moment the alias exists, so an alias could never be
    listed, edited or deleted by the name it was created under."""
    main.create_tag_alias("cpp", "c++", rewrite=False)
    assert main.normalize_tag_value_raw("cpp") == "cpp"


def test_alias_resolves_one_hop_only(env):
    """A chain would loop; a cycle would hang. Creating one is refused, and the
    resolver takes a single step regardless."""
    main.create_tag_alias("cpp", "c++", rewrite=False)
    # Folding the canonical onward would strand #cpp, which resolves one hop.
    out = main.tag_alias_preview("c++", "cplusplus")
    assert out["error"]
    assert "cpp" in out["error"]

    # And the other direction: aliasing onto something that is itself an alias.
    back = main.tag_alias_preview("cplusplus", "cpp")
    assert back["error"]
    assert "c++" in back["error"]


def test_self_alias_refused(env):
    assert main.tag_alias_preview("cpp", "cpp")["error"]
    assert main.tag_alias_preview("", "c++")["error"]


def test_delete_restores_the_original_spelling(env):
    main.create_tag_alias("cpp", "c++", rewrite=False)
    assert main.normalize_tag_value("cpp") == "c++"
    assert main.delete_tag_alias("cpp") is True
    assert main.normalize_tag_value("cpp") == "cpp"


def test_listing_shows_what_was_created(env):
    main.create_tag_alias("csharp", "c#", rewrite=False)
    rows = main.list_tag_aliases()
    assert [(r["alias"], r["canonical"]) for r in rows] == [("csharp", "c#")]


def test_preview_counts_feed_tags_without_writing(env):
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO entry_feed_tags (feed_url, entry_id, tag, first_seen_at) VALUES (?, ?, ?, 0)",
            [("f", "e1", "cpp"), ("f", "e2", "cpp"), ("f", "e3", "c++")],
        )
    out = main.tag_alias_preview("cpp", "c++")
    assert out["feed"] == 2
    assert not out["error"]
    assert main.list_tag_aliases() == []          # preview wrote nothing


def test_create_rewrites_stored_feed_tags(env):
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO entry_feed_tags (feed_url, entry_id, tag, first_seen_at) VALUES (?, ?, ?, 0)",
            [("f", "e1", "cpp"), ("f", "e2", "cpp")],
        )
    main.create_tag_alias("cpp", "c++", rewrite=True)
    with main.get_meta_connection() as conn:
        rows = [r["tag"] for r in conn.execute("SELECT tag FROM entry_feed_tags ORDER BY entry_id")]
    assert rows == ["c++", "c++"]


def test_rewrite_drops_a_row_that_would_collide(env):
    """An entry already carrying the canonical must not end up with it twice —
    (feed_url, entry_id, tag) would collide."""
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO entry_feed_tags (feed_url, entry_id, tag, first_seen_at) VALUES (?, ?, ?, 0)",
            [("f", "e1", "cpp"), ("f", "e1", "c++")],
        )
    main.create_tag_alias("cpp", "c++", rewrite=True)
    with main.get_meta_connection() as conn:
        rows = [r["tag"] for r in conn.execute("SELECT tag FROM entry_feed_tags WHERE entry_id = 'e1'")]
    assert rows == ["c++"]
