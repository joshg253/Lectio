"""The webcomic "hide locked strips" toggle lives in Settings → Account, not under Integrations → YouTube, and is saved by the Account
scope (the server allowlist is flat, so only the template and the JS payload decide where it lives)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODAL = (ROOT / "templates" / "_settings_modal.html").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
CHECKBOX = 'id="sett-hide-locked-comics"'
KEY = "hide_locked_comics_global:"


def test_checkbox_is_in_the_account_tab_only():
    account = MODAL.index('id="settings-tab-account"')
    contacts = MODAL.index('id="settings-tab-contacts"')
    assert MODAL.count(CHECKBOX) == 1
    assert account < MODAL.index(CHECKBOX) < contacts


def test_payload_key_is_saved_by_the_account_scope_only():
    account = APP_JS.index("if (scope === 'account') {")
    profile = APP_JS.index("} else if (scope === 'profile') {")
    assert APP_JS.count(KEY) == 1
    assert account < APP_JS.index(KEY) < profile
