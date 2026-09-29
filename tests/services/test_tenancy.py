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
    tenancy.configure(data_dir=tmp_path)
    # conftest binds every test to a user; these tests start from the unbound state a bare thread sees.
    token = tenancy._current_user.set(None)
    try:
        yield tmp_path
    finally:
        tenancy._current_user.reset(token)
        tenancy._layout = saved


def test_unbound_resolution_raises(configured):
    assert tenancy.bound_user_id() is None
    with pytest.raises(tenancy.TenancyUnboundError):
        tenancy.current_user_id()
    with pytest.raises(tenancy.TenancyUnboundError):
        tenancy.meta_db_path()
    with pytest.raises(tenancy.TenancyUnboundError):
        tenancy.reader_db_path()
    with pytest.raises(tenancy.TenancyUnboundError):
        tenancy.starred_archive_db_path()


def test_current_user_resolves_under_users_dir(configured):
    with tenancy.user_context("alice"):
        base = configured / "users" / "alice"
        assert tenancy.reader_db_path() == base / "lectio_reader.sqlite"
        assert tenancy.meta_db_path() == base / "lectio_meta.sqlite3"
        assert tenancy.starred_archive_db_path() == base / "lectio_starred_archive.sqlite"


def test_named_user_resolves_under_users_dir(configured):
    base = configured / "users" / "alice"
    assert tenancy.reader_db_path("alice") == base / "lectio_reader.sqlite"
    assert tenancy.meta_db_path("alice") == base / "lectio_meta.sqlite3"
    assert tenancy.starred_archive_db_path("alice") == base / "lectio_starred_archive.sqlite"


def test_distinct_users_get_distinct_paths(configured):
    assert tenancy.meta_db_path("alice") != tenancy.meta_db_path("bob")
    with tenancy.user_context("carol"):
        assert tenancy.meta_db_path("alice") != tenancy.meta_db_path()


@pytest.mark.parametrize(
    "bad",
    ["../escape", "a/b", "with space", "", "x" * 65, "semi;colon", "dot.dot", "tab\t"],
)
def test_invalid_user_ids_are_rejected(configured, bad):
    assert not tenancy.is_valid_user_id(bad)
    with pytest.raises(ValueError):
        tenancy.meta_db_path(bad)


@pytest.mark.parametrize("good", ["alice", "user_1", "A-B_c", "x" * 64])
def test_valid_user_ids_accepted(configured, good):
    assert tenancy.is_valid_user_id(good)
    # Should not raise.
    tenancy.meta_db_path(good)


def test_user_context_sets_and_restores(configured):
    assert tenancy.bound_user_id() is None
    with tenancy.user_context("alice"):
        assert tenancy.current_user_id() == "alice"
        assert tenancy.meta_db_path() == tenancy.meta_db_path("alice")
        with tenancy.user_context("bob"):
            assert tenancy.current_user_id() == "bob"
        # Inner context restored to alice.
        assert tenancy.current_user_id() == "alice"
    assert tenancy.bound_user_id() is None


def test_user_context_restores_on_exception(configured):
    with pytest.raises(RuntimeError):
        with tenancy.user_context("alice"):
            raise RuntimeError("boom")
    assert tenancy.bound_user_id() is None


def test_set_reset_current_user_token(configured):
    token = tenancy.set_current_user("alice")
    try:
        assert tenancy.current_user_id() == "alice"
    finally:
        tenancy.reset_current_user(token)
    assert tenancy.bound_user_id() is None


def test_set_current_user_rejects_invalid(configured):
    with pytest.raises(ValueError):
        tenancy.set_current_user("../bad")


def test_ensure_user_data_dir_creates_dir(configured):
    path = tenancy.ensure_user_data_dir("alice")
    assert path.is_dir()
    assert path == configured / "users" / "alice"


def test_resolution_requires_configure(monkeypatch):
    monkeypatch.setattr(tenancy, "_layout", None)
    with pytest.raises(RuntimeError):
        tenancy.meta_db_path("alice")
