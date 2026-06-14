"""Tests for scripts/migrate_to_multiuser.py against synthetic databases."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import migrate_to_multiuser as mig  # noqa: E402
from services import tenancy  # noqa: E402
from services.users import UserStore  # noqa: E402


def _make_db(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
    conn.execute("INSERT INTO probe (v) VALUES (?)", (marker,))
    conn.commit()
    conn.close()


@pytest.fixture
def data_dir(tmp_path):
    """A fake data dir: the three legacy DBs + reader FTS sidecar, plus an auth DB
    holding the 'joshg253' account (so a stable user_id exists to migrate into)."""
    _make_db(tmp_path / "lectio_reader.sqlite", "reader-data")
    _make_db(tmp_path / "lectio_reader.sqlite.search", "search-data")
    _make_db(tmp_path / "lectio_meta.sqlite3", "meta-data")
    _make_db(tmp_path / "lectio_starred_archive.sqlite", "starred-data")
    store = UserStore(tmp_path / "lectio_auth.sqlite")
    user_id = store.create("joshg253", "pw", is_admin=True)
    saved = tenancy._layout
    yield tmp_path, user_id
    tenancy._layout = saved


def _probe(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT v FROM probe").fetchone()[0]
    finally:
        conn.close()


def test_plan_resolves_to_user_id_paths(data_dir):
    dd, user_id = data_dir
    items = mig.plan_migration(dd, "joshg253")
    udir = dd / "users" / user_id
    labels = {i.label: i for i in items}
    assert labels["meta"].dest == udir / "lectio_meta.sqlite3"
    assert labels["reader"].dest == udir / "lectio_reader.sqlite"
    # Destination is keyed by the stable user_id, not the (mutable) username.
    assert "joshg253" not in str(udir)
    csrc, cdest = labels["reader"].companions[0]
    assert csrc.name == "lectio_reader.sqlite.search"
    assert cdest == udir / "lectio_reader.sqlite.search"


def test_unknown_username_errors(data_dir):
    dd, _ = data_dir
    with pytest.raises(ValueError, match="no account"):
        mig.plan_migration(dd, "nobody")


def test_missing_auth_db_errors(tmp_path):
    _make_db(tmp_path / "lectio_reader.sqlite", "x")
    saved = tenancy._layout
    try:
        with pytest.raises(ValueError, match="auth DB not found"):
            mig.plan_migration(tmp_path, "joshg253")
    finally:
        tenancy._layout = saved


def test_invalid_username_rejected(data_dir):
    dd, _ = data_dir
    with pytest.raises(ValueError, match="invalid username"):
        mig.plan_migration(dd, "../evil")


def test_dry_run_writes_nothing(data_dir):
    dd, user_id = data_dir
    assert mig.migrate(dd, "joshg253", apply=False) == 0
    assert not (dd / "users" / user_id).exists()


def test_apply_copies_dbs_and_sidecar(data_dir):
    dd, user_id = data_dir
    assert mig.migrate(dd, "joshg253", apply=True) == 0
    udir = dd / "users" / user_id
    assert _probe(udir / "lectio_reader.sqlite") == "reader-data"
    assert _probe(udir / "lectio_reader.sqlite.search") == "search-data"
    assert _probe(udir / "lectio_meta.sqlite3") == "meta-data"
    assert _probe(udir / "lectio_starred_archive.sqlite") == "starred-data"
    # Originals kept by default (reversible).
    assert (dd / "lectio_reader.sqlite").exists()


def test_apply_refuses_existing_dest_without_force(data_dir):
    dd, _ = data_dir
    assert mig.migrate(dd, "joshg253", apply=True) == 0
    assert mig.migrate(dd, "joshg253", apply=True) == 3  # refuse clobber
    assert mig.migrate(dd, "joshg253", apply=True, force=True) == 0  # --force proceeds


def test_remove_source_deletes_originals_after_copy(data_dir):
    dd, user_id = data_dir
    assert mig.migrate(dd, "joshg253", apply=True, remove_source=True) == 0
    assert not (dd / "lectio_reader.sqlite").exists()
    assert not (dd / "lectio_meta.sqlite3").exists()
    assert _probe(dd / "users" / user_id / "lectio_meta.sqlite3") == "meta-data"


def test_survives_username_rename(data_dir):
    """The whole point: migrate, rename the username, data still resolves by id."""
    dd, user_id = data_dir
    assert mig.migrate(dd, "joshg253", apply=True) == 0
    store = UserStore(dd / "lectio_auth.sqlite")
    store.rename_user(user_id, "joshua")
    # Same user_id → same directory, regardless of the new username.
    assert (dd / "users" / user_id / "lectio_meta.sqlite3").exists()
    assert store.get_by_id(user_id)["username"] == "joshua"
