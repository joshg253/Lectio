"""YouTube descriptions keep line-leading hashtags as text instead of rendering them as Markdown headings."""

from __future__ import annotations

import dataclasses

from services.reader_sanitize import _sanitize_entry

_DESC = "Watch [this](https://example.com/x)\n#Tag1 #Tag2"


@dataclasses.dataclass(frozen=True)
class _Entry:
    id: str
    summary: str | None = None
    content: tuple = ()


def test_youtube_entry_hashtag_line_is_not_a_heading():
    out = _sanitize_entry(_Entry(id="yt:video:abc", summary=_DESC))
    assert "<h1" not in out.summary and "<a " not in out.summary and "#Tag1" in out.summary


def test_other_markdown_entries_still_render():
    out = _sanitize_entry(_Entry(id="https://example.com/post", summary=_DESC))
    assert "<h1" in out.summary
