"""assume_https_if_schemeless: the schemeless-paste-assumes-https logic shared
by Add Feed's discovery route and Change URL (previously duplicated one-liners
in each — see Plan.md "Centralize schemeless-URL normalization")."""
from __future__ import annotations

import pytest

from main import assume_https_if_schemeless


@pytest.mark.parametrize("raw, expected", [
    ("www.example.com", "https://www.example.com"),
    ("example.com/feed", "https://example.com/feed"),
    ("https://example.com/feed", "https://example.com/feed"),
    ("http://example.com/feed", "http://example.com/feed"),
    ("", ""),
])
def test_schemeless_input_gets_https(raw, expected):
    assert assume_https_if_schemeless(raw) == expected
