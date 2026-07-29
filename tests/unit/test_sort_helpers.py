"""Sort-key normalization, and why one of the keys is opt-in."""
from __future__ import annotations

import main


def test_known_keys_pass_through():
    assert main.normalize_sort_by("post") == "post"
    assert main.normalize_sort_by("received") == "received"


def test_unknown_key_falls_back_to_the_default():
    assert main.normalize_sort_by("bogus") == "post"
    assert main.normalize_sort_by(None) == "post"


def test_starred_sort_requires_opt_in():
    """"starred" (order by when it was starred) exists for Read Mode's Inbox and
    must NOT be globally valid.

    The main index persists whatever normalize_sort_by hands it as the remembered
    sort. With "starred" blessed globally it could be stored there, and the
    regular sort menu has no entry for it — so nothing rendered as active and the
    toolbar displayed "Published newest" while the list was ordered by star date.
    Reported as the Feed view reverting to "Pub new" after switching in and out of
    e-ink mode.
    """
    assert main.normalize_sort_by("starred") == "post"          # falls back
    assert main.normalize_sort_by("starred", allow_starred=True) == "starred"
    assert main.normalize_sort_by("post", allow_starred=True) == "post"
    assert main.normalize_sort_by("bogus", allow_starred=True) == "post"


def test_feeds_and_saved_keep_separate_sort_keys():
    """One shared pair meant picking an order in Saved silently changed Feeds.

    They are different jobs — a publish-date backlog versus a to-do pile — so
    they want different orders. The unprefixed keys stay Feeds' so existing
    installs keep the value they already had.
    """
    assert main.sort_setting_keys(False) == ("sort_by", "sort_dir")
    assert main.sort_setting_keys(True) == ("saved_sort_by", "saved_sort_dir")
