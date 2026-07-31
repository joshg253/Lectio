"""The `kept` flag both templates read to decide whether Re-fetch is offered.

Kept = starred OR tagged. The row builder has computed this correctly since
tag-as-keep shipped, but the templates went on deriving it themselves from
`manual_tags` — which the list builder only populates under a tag filter. So a
tagged-but-unstarred post in a plain Saved view rendered `data-post-kept="0"` and
lost its Re-fetch item, which is how an opensource.com article with a `python`
tag and no star had no way to pull its (empty) body.

These are text assertions on the templates because that is exactly where the bug
was: server and template disagreed about which field carried the answer, and no
amount of testing the builder would have caught it.
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"


def _kept_attr(name: str) -> str:
    html = (TEMPLATES / name).read_text()
    m = re.search(r'data-post-kept="([^"]*)"', html)
    assert m, f"{name} no longer renders data-post-kept"
    return m.group(1)


def test_the_post_list_reads_the_kept_field():
    assert _kept_attr("index.html") == "{{ 1 if post.kept else 0 }}"


def test_the_article_pane_reads_the_kept_field():
    assert _kept_attr("_entry_pane.html") == "{{ 1 if selected_entry.kept else 0 }}"


def test_neither_template_derives_keptness_from_manual_tags():
    """`manual_tags` is populated only under a tag filter in the list builder, so
    deriving keptness from it silently drops every tagged post everywhere else."""
    for name in ("index.html", "_entry_pane.html"):
        assert "manual_tags" not in _kept_attr(name)
