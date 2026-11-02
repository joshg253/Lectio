"""The webcomic "hide locked strips" toggle lives in Settings → Account, not under Integrations → YouTube, and is saved by the Account
scope (the server allowlist is flat, so only the template and the JS payload decide where it lives)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODAL = (ROOT / "templates" / "_settings_modal.html").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
CHECKBOX = 'id="sett-hide-locked-comics"'
ACCOUNT_SAVE = 'data-settings-save="account"'


def test_checkbox_is_in_the_account_panel_before_its_save_button():
    account = MODAL.index('id="settings-tab-account"')
    save = MODAL.index(ACCOUNT_SAVE)
    assert MODAL.count(CHECKBOX) == 1
    assert account < MODAL.index(CHECKBOX) < save


def test_payload_key_is_set_from_the_checkbox_in_the_account_scope_only():
    payload_line = re.compile(r"^\s+hide_locked_comics_global: \(document\.getElementById\('sett-hide-locked-comics'\)", re.M)
    matches = list(payload_line.finditer(APP_JS))
    assert len(matches) == 1
    account = APP_JS.index("if (scope === 'account') {")
    profile = APP_JS.index("} else if (scope === 'profile') {")
    assert account < matches[0].start() < profile
