"""Term syntax for the posts list's "Filter this view" box.

* ``tag:games`` keeps posts whose suggestion chips include ``games``; ``tagged:`` does the same for tags the user applied. A leading ``!``
  on the value negates (``tag:!games``) and ``*`` is a wildcard (``tag:gam*``); a value without ``*`` must match a whole tag.
* Any other word is a substring match on title, link and feed name; ``!word`` excludes it. Double quotes make a phrase
  (``"zelda tears"``, ``!"press release"``), and also let a tag value hold spaces (``tag:"games to play"``).
* Every term must hold (AND), in any order.

static/js/app.js's ``parseFilterTerm`` mirrors this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TAG_RE = re.compile(r"^(tag|tagged):(!?)(.*)$", re.IGNORECASE)


def glob_match(parts: tuple[str, ...], tag: str) -> bool:
    """Whole-tag match of a ``*`` pattern given as its literal parts (``"gam*"`` is ``("gam", "")``).

    Linear in the tag, unlike a ``.*``-joined regex, which backtracks badly with many stars: anyone can create a 32-character tag.
    """
    if len(parts) == 1:
        return tag == parts[0]
    first, last = parts[0], parts[-1]
    if len(tag) < len(first) + len(last) or not tag.startswith(first) or not tag.endswith(last):
        return False
    pos, end = len(first), len(tag) - len(last)
    for mid in parts[1:-1]:
        found = tag.find(mid, pos, end)
        if found < 0:
            return False
        pos = found + len(mid)
    return True


@dataclass(frozen=True)
class TagClause:
    applied: bool
    negate: bool
    parts: tuple[str, ...]

    def matches(self, tags: list[str]) -> bool:
        found = any(glob_match(self.parts, t) for t in tags)
        return not found if self.negate else found


@dataclass(frozen=True)
class ParsedFilter:
    clauses: list[TagClause] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    def text_matches(self, haystack: str) -> bool:
        return all(t in haystack for t in self.include) and not any(t in haystack for t in self.exclude)


def _parts(value: str) -> tuple[str, ...]:
    # Same shape as normalize_tag_value, minus the user's aliases: lowercase, no "#", spaces to hyphens.
    norm = re.sub(r"\s+", "-", value.strip().lower().lstrip("#"))
    return tuple(norm.split("*"))


def _tokens(term: str) -> list[str]:
    """Whitespace-separated tokens with quotes kept; spaces inside quotes do not split. An unclosed quote runs to the end."""
    tokens: list[str] = []
    current: list[str] = []
    in_quote = False
    for ch in term:
        if ch == '"':
            in_quote = not in_quote
        elif ch.isspace() and not in_quote:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def parse_filter_term(term: str) -> ParsedFilter:
    """Split *term* into tag clauses, required text and excluded text (text lowercased).

    A bare ``tag:`` or ``!`` (still being typed) is skipped rather than narrowing the list to nothing.
    """
    clauses: list[TagClause] = []
    include: list[str] = []
    exclude: list[str] = []
    for raw in _tokens(term.lower()):
        m = _TAG_RE.match(raw)
        if m:
            value = m.group(3).replace('"', "")
            if value.strip():
                clauses.append(TagClause(applied=m.group(1).lower() == "tagged", negate=bool(m.group(2)), parts=_parts(value)))
            continue
        negate = raw.startswith("!")
        text = (raw[1:] if negate else raw).replace('"', "")
        if text:
            (exclude if negate else include).append(text)
    return ParsedFilter(clauses, include, exclude)
