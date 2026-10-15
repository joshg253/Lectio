"""Which of an entry's content elements is the article.

A feed can carry several content elements and lead with a lesser one (a literal ``"null"`` text/plain stub, an escaped empty
paragraph, an author blurb), so reading ``entry.content[0]`` can render or store the stub instead of the post. Every place that
needs an entry's body goes through `entry_body` / `richest_content` rather than indexing the list.
"""

from __future__ import annotations

import html
import re


def visible_word_count(value: str | None) -> int:
    """Words a reader would actually SEE, with escaped markup counted as markup.

    ``&lt;p&gt;&lt;br&gt;&lt;/p&gt;`` is a paragraph and a line break spelled as text; unescaping before stripping tags scores it 0,
    which is what it looks like on screen.
    """
    if not value:
        return 0
    return len(re.sub(r"<[^>]+>", " ", html.unescape(value)).split())


def richest_content(entry, chosen):
    """Pick the content element with the most visible text.

    reader's ``get_content`` returns the FIRST html element, and a feed may put a lesser one first. pcgamer does both: on some entries
    an escaped ``&lt;p&gt;&lt;br&gt;&lt;/p&gt;`` (29 characters, zero words) precedes the 5,364-character article, on others a 77-word
    author blurb precedes the 464-word piece.

    "Skip it only when it is empty" was tried first and is not enough — the blurb is not empty, it is just not the article. Among
    several representations of one entry the fullest is the one a reader wants, so the choice is by visible word count, html
    preferred, with the caller's own pick as the tie-break.
    """
    candidates = [c for c in (getattr(entry, "content", None) or []) if visible_word_count(getattr(c, "value", None))]
    if not candidates:
        return chosen  # nothing has text; leave the caller's choice alone
    html_ones = [c for c in candidates if getattr(c, "is_html", False)]
    best = max(html_ones or candidates, key=lambda c: visible_word_count(c.value))
    chosen_words = visible_word_count(getattr(chosen, "value", None)) if chosen is not None else 0
    return chosen if chosen_words >= visible_word_count(best.value) else best


def entry_body(entry, *, prefer_summary: bool = False) -> str:
    """The entry's body text: the richest content element, falling back to the summary ("" when there is neither)."""
    content = getattr(entry, "content", None) or []
    summary = getattr(entry, "summary", None) or ""
    if prefer_summary and summary:
        return summary
    if content:
        # One element has nothing to rank against; skip the per-element word counts (a regex over the whole article each).
        chosen = content[0] if len(content) == 1 else richest_content(entry, content[0])
        value = getattr(chosen, "value", None) or ""
        if value:
            return value
    return summary
