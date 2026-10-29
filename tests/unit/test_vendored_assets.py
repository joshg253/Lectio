"""Guards for vendored static assets that are only referenced from JS at runtime."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_hls_js_path_in_app_js_exists():
    """hls.js is lazy-loaded by a hardcoded path in app.js; a bump that renames the directory but misses app.js would 404 silently."""
    refs = re.findall(r"/static/(vendor/hls\.js-[\d.]+/hls\.min\.js)", (ROOT / "static/js/app.js").read_text())
    assert len(refs) == 1
    assert (ROOT / "static" / refs[0]).is_file()
    assert len(list((ROOT / "static/vendor").glob("hls.js-*"))) == 1
