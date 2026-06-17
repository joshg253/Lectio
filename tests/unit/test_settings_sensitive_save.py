"""Regression: re-saving settings must not wipe stored secrets.

Masked secret fields reload blank, so a routine re-save sends "" for them.
That must be treated as "leave unchanged", not a delete.
"""
from __future__ import annotations

from main import _keep_existing_sensitive

_SENSITIVE = {"resend_api_key", "yt_api_key", "instapaper_password", "deviantart_client_secret"}


def test_blank_sensitive_is_kept():
    # Blank value for a secret → skip (keep existing).
    assert _keep_existing_sensitive("deviantart_client_secret", "", _SENSITIVE) is True
    assert _keep_existing_sensitive("yt_api_key", "", _SENSITIVE) is True


def test_masked_placeholder_is_kept():
    assert _keep_existing_sensitive("deviantart_client_secret", "••••••••", _SENSITIVE) is True


def test_real_secret_value_is_written():
    # A real (non-blank, non-masked) value is NOT skipped → it gets saved.
    assert _keep_existing_sensitive("deviantart_client_secret", "abc123", _SENSITIVE) is False


def test_non_sensitive_blank_still_clears():
    # Non-secret fields keep delete-on-blank behavior (e.g. clearing a channel id).
    assert _keep_existing_sensitive("yt_channel_id", "", _SENSITIVE) is False
    assert _keep_existing_sensitive("deviantart_client_id", "", _SENSITIVE) is False
