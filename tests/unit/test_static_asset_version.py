"""The ?v= cache-buster must move when a versioned asset moves.

It was a hand-kept list of six filenames, and it went stale exactly as you would
expect: offline-probe.js and js/cleanup.js are both served with
?v={{ static_asset_version }} and neither was in it. Editing them left the
version unchanged, so browsers — and the service worker, which caches /static by
design — kept serving the previous file. A deployed fix ran as the old copy.
"""
from __future__ import annotations

import re
from pathlib import Path

import main

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"


def _versioned_assets() -> set[str]:
    """Every static file a template serves with ?v=<version>."""
    found: set[str] = set()
    for tpl in TEMPLATES.glob("*.html"):
        for m in re.finditer(r'(?:src|href)="/(static/[^"?]+)\?v=\{\{\s*static_asset_version',
                             tpl.read_text()):
            path = m.group(1)
            # Skip runtime-interpolated hrefs (the theme switcher builds
            # static/themes/${window.__lectioTheme}.css in JS); the real files it
            # resolves to are covered on their own.
            if "${" in path:
                continue
            found.add(path)
    return found


def test_every_versioned_asset_is_covered_by_the_hash():
    """The property that actually matters: if a template cache-busts a file, the
    buster has to notice that file changing."""
    covered = {
        str(p.relative_to(ROOT))
        for p in STATIC.rglob("*")
        if p.is_file() and p.suffix in (".css", ".js")
    }

    missing = _versioned_assets() - covered
    assert not missing, f"served with ?v= but not hashed: {sorted(missing)}"


def test_the_version_moves_when_a_versioned_asset_changes():
    probe = STATIC / "offline-probe.js"
    original = probe.read_bytes()
    before = main._static_asset_version()
    try:
        probe.write_bytes(original + b"\n// changed\n")
        after = main._static_asset_version()
    finally:
        probe.write_bytes(original)

    assert before != after
    assert main._static_asset_version() == before      # restored


def test_the_version_is_stable_for_unchanged_files():
    assert main._static_asset_version() == main._static_asset_version()
