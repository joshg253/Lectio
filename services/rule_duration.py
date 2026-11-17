"""Rule keyword-box syntax helpers: ``/regex/`` wrapping and duration terms.

Duration terms in a rule's keyword box (``=2-10``, ``<2:00``, ``>=1h``) use the same syntax as "Filter this view" (app.js
``_parseDurationFilter``; keep the two in step). Bare numbers are minutes. ``=0`` ("no duration") is deliberately unsupported here: in a
rule it would match every non-video post."""

from __future__ import annotations

import re

from services import youtube_embeds

_HMS = re.compile(r"^(\d+):(\d{2}):(\d{2})$")
_MS = re.compile(r"^(\d+):(\d{2})$")
_UNIT = re.compile(r"^(\d+(?:\.\d+)?)\s*([hms])$")
_BARE = re.compile(r"^(\d+(?:\.\d+)?)$")
_TERM = re.compile(r"^(<=|>=|<|>|=)(.+)$")


def keyword_syntax_error(rule_type: str, keyword: str) -> str | None:
    """Why *keyword* can't be saved for *rule_type*, or None. A duration term only works as a whole Mark as Read keyword, and a
    comma list is plain text: a /regex/ or duration item inside one would silently be matched as literal text."""
    keyword = (keyword or "").strip()
    if not keyword or rule_type in ("deduplicate", "tag_filter"):
        return None
    if parse_duration_term(keyword):
        if rule_type != "mark_as_read":
            return "duration terms (=2-10) work in Mark as Read rules only; a playlist rule has its own min/max minutes"
        return None
    if "," in keyword:
        items = [t.strip() for t in keyword.split(",") if t.strip()]
        if not (len(keyword) >= 3 and keyword[0] == "/" and keyword[-1] == "/") and any(
            parse_duration_term(t) or (len(t) >= 3 and t[0] == "/" and t[-1] == "/") for t in items
        ):
            return "a /regex/ or duration term can't be one item of a comma list; make it its own rule (or one /regex/ with |)"
    return None


def wrap_regex_keyword(rule_type: str, keyword: str, is_regex: bool) -> tuple[str, bool]:
    """Normalize a legacy ``is_regex`` rule to the slash form: ``(keyword, True)`` -> ``("/keyword/", False)``. Types whose keyword isn't
    a search pattern (deduplicate, tag_filter) and blank keywords are left alone."""
    keyword = (keyword or "").strip()
    if is_regex and keyword and rule_type not in ("deduplicate", "tag_filter"):
        return f"/{keyword}/", False
    return keyword, bool(is_regex)


def _to_seconds(text: str) -> float | None:
    m = _HMS.match(text)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    m = _MS.match(text)
    if m:
        return int(m[1]) * 60 + int(m[2])
    m = _UNIT.match(text)
    if m:
        n = float(m[1])
        return n * {"h": 3600, "m": 60, "s": 1}[m[2]]
    m = _BARE.match(text)
    if m:
        return float(m[1]) * 60
    return None


def parse_duration_term(keyword: str) -> dict | None:
    """Return ``{"op": "range", "min", "max"}`` or ``{"op": "<"|"<="|">"|">=", "seconds"}``, or None if *keyword* isn't a duration."""
    m = _TERM.match((keyword or "").strip())
    if not m:
        return None
    op, value = m[1], m[2].strip()
    if op == "=":
        # First hyphen only: none of the duration shapes use "-" internally.
        rng = re.match(r"^(.+?)-(.+)$", value)
        if not rng:
            return None
        lo, hi = _to_seconds(rng[1].strip()), _to_seconds(rng[2].strip())
        if lo is None or hi is None:
            return None
        return {"op": "range", "min": lo, "max": hi}
    seconds = _to_seconds(value)
    if seconds is None:
        return None
    return {"op": op, "seconds": seconds}


def entry_duration_matches(entry: object, spec: dict, service) -> bool:
    """Match the video a YouTube-feed entry *is* against *spec*; entries of any other feed never match."""
    vid = youtube_embeds.own_video_id(str(getattr(entry, "feed_url", "") or ""), str(getattr(entry, "link", None) or ""))
    return bool(vid) and duration_matches(spec, service.get_cached_duration(vid)[0])


def duration_matches(spec: dict, seconds: float | None) -> bool:
    """False for an unknown duration: never act on a guess."""
    if seconds is None:
        return False
    op = spec["op"]
    if op == "range":
        return spec["min"] <= seconds <= spec["max"]
    if op == "<":
        return seconds < spec["seconds"]
    if op == "<=":
        return seconds <= spec["seconds"]
    if op == ">":
        return seconds > spec["seconds"]
    return seconds >= spec["seconds"]
