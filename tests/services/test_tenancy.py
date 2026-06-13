"""Unit tests for the tenancy resolver (services/tenancy.py)."""
from __future__ import annotations

import pytest

from services import tenancy


@pytest.fixture
def configured(tmp_path):
    """Point the resolver at a throwaway layout for the duration of a test.

    Saves and restores the module-global layout so these tests don't leak a
    (soon-deleted) tmp path into other test files that import main and rely on
    its configuration.
    """
    saved = tenancy._layout
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "lectio_reader.sqlite",
        legacy_meta=tmp_path / "lectio_meta.sqlite3",
        legacy_starred=tmp_path / "lectio_starred_archive.sqlite",
    )
    try:
        yield tmp_path
    finally:
        tenancy._layout = saved


def test_default_user_resolves_to_legacy_paths(configured):
    assert tenancy.reader_db_path() == configured / "lectio_reader.sqlite"
    assert tenancy.meta_db_path() == configured / "lectio_meta.sqlite3"
    assert tenancy.starred_archive_db_path() == configured / "lectio_starred_archive.sqlite"


def test_default_user_is_the_implicit_context(configured):
    # No context set → DEFAULT_USER_ID → legacy paths.
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID
    assert tenancy.meta_db_path(tenancy.DEFAULT_USER_ID) == tenancy.meta_db_path()


def test_named_user_resolves_under_users_dir(configured):
    base = configured / "users" / "alice"
    assert tenancy.reader_db_path("alice") == base / "lectio_reader.sqlite"
    assert tenancy.meta_db_path("alice") == base / "lectio_meta.sqlite3"
    assert tenancy.starred_archive_db_path("alice") == base / "lectio_starred_archive.sqlite"


def test_distinct_users_get_distinct_paths(configured):
    assert tenancy.meta_db_path("alice") != tenancy.meta_db_path("bob")
    assert tenancy.meta_db_path("alice") != tenancy.meta_db_path()


@pytest.mark.parametrize(
    "bad",
    ["../escape", "a/b", "with space", "", "x" * 65, "semi;colon", "dot.dot", "tab\t"],
)
def test_invalid_user_ids_are_rejected(configured, bad):
    assert not tenancy.is_valid_user_id(bad)
    with pytest.raises(ValueError):
        tenancy.meta_db_path(bad)


@pytest.mark.parametrize("good", ["alice", "user_1", "A-B_c", "default", "x" * 64])
def test_valid_user_ids_accepted(configured, good):
    assert tenancy.is_valid_user_id(good)
    # Should not raise.
    tenancy.meta_db_path(good)


def test_user_context_sets_and_restores(configured):
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID
    with tenancy.user_context("alice"):
        assert tenancy.current_user_id() == "alice"
        assert tenancy.meta_db_path() == tenancy.meta_db_path("alice")
        with tenancy.user_context("bob"):
            assert tenancy.current_user_id() == "bob"
        # Inner context restored to alice, not default.
        assert tenancy.current_user_id() == "alice"
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


def test_user_context_restores_on_exception(configured):
    with pytest.raises(RuntimeError):
        with tenancy.user_context("alice"):
            raise RuntimeError("boom")
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


def test_set_reset_current_user_token(configured):
    token = tenancy.set_current_user("alice")
    try:
        assert tenancy.current_user_id() == "alice"
    finally:
        tenancy.reset_current_user(token)
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


def test_set_current_user_rejects_invalid(configured):
    with pytest.raises(ValueError):
        tenancy.set_current_user("../bad")


def test_ensure_user_data_dir_creates_dir(configured):
    path = tenancy.ensure_user_data_dir("alice")
    assert path.is_dir()
    assert path == configured / "users" / "alice"


def test_ensure_user_data_dir_rejects_default(configured):
    with pytest.raises(ValueError):
        tenancy.ensure_user_data_dir(tenancy.DEFAULT_USER_ID)


def test_resolution_requires_configure(monkeypatch):
    monkeypatch.setattr(tenancy, "_layout", None)
    with pytest.raises(RuntimeError):
        tenancy.meta_db_path()
