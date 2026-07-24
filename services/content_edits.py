"""Aardvark-style per-entry content cleanup.

The reading pane can be armed into a "clean up" mode where hovering outlines a
DOM node and a click deletes it (plus isolate / widen / narrow, mirroring the
original Aardvark bookmarklet). This module is the server half: it replays the
recorded operations against the entry's *stored* HTML so the result can be
written back to reader, rather than trusting the browser's serialized DOM.

Why replay instead of accepting the edited HTML:
  - the rendered body is not the stored body. Images are routed through
    ``/api/img`` for hotlink hosts, ``referrerpolicy`` is injected, starred
    assets are rewritten to local copies, and app.js rewrites more on error.
    Posting the DOM back would bake all of that into stored content.
  - the operation list is the durable record of *what the user removed*, which
    is what a later per-feed rule gets promoted from.

Matching is deliberately two-tier because the rendered tree and the stored tree
are not guaranteed identical (render-time cleanups strip nodes, embeds get
injected). Each op carries a structural path *and* a fingerprint:

  1. walk the path (element-child indexes from the content root) and accept the
     node it lands on only if the fingerprint agrees;
  2. otherwise search the whole tree for the best fingerprint match, and accept
     it only if it is unambiguous.

An op that matches neither is reported back as unmatched rather than guessed
at — deleting the wrong node silently is the one outcome worth failing over.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

OP_REMOVE = "remove"
OP_ISOLATE = "isolate"
_VALID_OPS = frozenset({OP_REMOVE, OP_ISOLATE})

# Fingerprint text is compared as a prefix; long enough to be distinctive on a
# paragraph, short enough that a trailing edit elsewhere in the node's subtree
# doesn't break the match.
_TEXT_PREFIX_LEN = 160

# Guardrails on a single request. Generous for hand-driven cleanup (the whole
# point is a handful of clicks) while bounding the replay cost.
MAX_OPS = 200

_WS_RE = re.compile(r"\s+")


class ContentEditError(ValueError):
    """Payload the server refuses to replay (malformed ops, empty content)."""


def parse_ops(raw: str | list) -> list[dict]:
    """Validate the client's op list. Raises ContentEditError on anything
    malformed — a bad path or op name means the browser and server disagree
    about the document, which is exactly when guessing is unsafe."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ContentEditError("ops is not valid JSON") from exc
    if not isinstance(raw, list) or not raw:
        raise ContentEditError("no cleanup operations were sent")
    if len(raw) > MAX_OPS:
        raise ContentEditError(f"too many operations (max {MAX_OPS})")
    ops: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ContentEditError(f"operation {index} is not an object")
        op = item.get("op")
        if op not in _VALID_OPS:
            raise ContentEditError(f"operation {index} has unknown op {op!r}")
        path = item.get("path")
        if not isinstance(path, list) or not path or not all(isinstance(p, int) and p >= 0 for p in path):
            raise ContentEditError(f"operation {index} has an invalid path")
        fingerprint = item.get("fp")
        if not isinstance(fingerprint, dict):
            raise ContentEditError(f"operation {index} has no fingerprint")
        ops.append({"op": op, "path": [int(p) for p in path], "fp": fingerprint})
    return ops


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _WS_RE.sub(" ", value).strip()[:_TEXT_PREFIX_LEN]


def _normalize_src(value: str | None) -> str:
    """Reduce an image/iframe URL to something stable across the render-time
    rewrites: unwrap the ``/api/img?u=`` proxy and keep the last path segment,
    so a proxied render URL fingerprints the same as the stored source URL."""
    if not value:
        return ""
    src = value.strip()
    if src.startswith("/api/img?"):
        query = urlparse(src).query
        for part in query.split("&"):
            if part.startswith("u="):
                src = unquote(part[2:])
                break
    path = urlparse(src).path or src
    return path.rsplit("/", 1)[-1]


def _element_children(node) -> list:
    return [child for child in node.children if getattr(child, "name", None)]


def fingerprint(node) -> dict:
    """Structural signature of a node, computed identically here and in the
    browser (see static/js/cleanup.js — the two must stay in step)."""
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return {
        "tag": node.name or "",
        "id": (node.get("id") or "").strip(),
        "cls": sorted(str(c) for c in classes),
        "text": _normalize_text(node.get_text(" ", strip=True)),
        "kids": len(_element_children(node)),
        "src": _normalize_src(node.get("src")),
    }


def _score(candidate: dict, target: dict) -> int:
    """How strongly a node matches a fingerprint. Tag disagreement is fatal;
    everything else accumulates. The thresholds below are calibrated so a bare
    ``<p>`` with different text can never reach acceptance."""
    if candidate.get("tag") != target.get("tag"):
        return 0
    score = 1
    target_id = target.get("id") or ""
    if target_id and candidate.get("id") == target_id:
        score += 4
    elif target_id or candidate.get("id"):
        return 0  # an id that doesn't match is a positive signal of difference
    target_cls = target.get("cls") or []
    candidate_cls = candidate.get("cls") or []
    if target_cls and candidate_cls == target_cls:
        score += 3
    elif target_cls and set(target_cls) & set(candidate_cls):
        score += 1
    target_text = target.get("text") or ""
    candidate_text = candidate.get("text") or ""
    if target_text and candidate_text == target_text:
        score += 4
    elif target_text and candidate_text and (
        candidate_text.startswith(target_text[:60]) or target_text.startswith(candidate_text[:60])
    ):
        score += 2
    elif target_text != candidate_text:
        return 0  # text is the strongest signal; a mismatch means a different node
    target_src = target.get("src") or ""
    if target_src and candidate.get("src") == target_src:
        score += 3
    if candidate.get("kids") == target.get("kids"):
        score += 1
    return score


# A path hit only has to confirm it landed somewhere plausible; a blind search
# has to be strong enough to stand on its own.
_PATH_ACCEPT_SCORE = 2
_SEARCH_ACCEPT_SCORE = 6


def _resolve_by_path(root, path: list[int]):
    node = root
    for index in path:
        children = _element_children(node)
        if index >= len(children):
            return None
        node = children[index]
    return node if node is not root else None


def _resolve_by_search(root, target: dict):
    """Best unambiguous fingerprint match anywhere under root, or None."""
    best = None
    best_score = 0
    runner_up = 0
    for node in root.find_all(True):
        score = _score(fingerprint(node), target)
        if score > best_score:
            runner_up = best_score
            best, best_score = node, score
        elif score > runner_up:
            runner_up = score
    if best_score >= _SEARCH_ACCEPT_SCORE and best_score > runner_up:
        return best
    return None


def apply_ops(content_html: str, ops: list[dict]) -> tuple[str, int, list[dict]]:
    """Replay *ops* against *content_html*.

    Returns ``(new_html, applied_count, unmatched)``. Ops are applied in order
    against a tree that mutates as it goes, mirroring the browser: the client
    derives each path from the DOM as it stands at the moment of that click.
    """
    if not isinstance(content_html, str) or not content_html.strip():
        raise ContentEditError("this entry has no HTML body to clean")
    soup = BeautifulSoup(f"<div>{content_html}</div>", "html.parser")
    root = soup.div
    applied = 0
    unmatched: list[dict] = []

    for index, op in enumerate(ops):
        target = op["fp"]
        node = _resolve_by_path(root, op["path"])
        if node is None or _score(fingerprint(node), target) < _PATH_ACCEPT_SCORE:
            node = _resolve_by_search(root, target)
        if node is None:
            unmatched.append({
                "index": index,
                "op": op["op"],
                "tag": target.get("tag", ""),
                "text": (target.get("text") or "")[:80],
            })
            continue
        if op["op"] == OP_REMOVE:
            node.decompose()
        else:  # isolate: the selected subtree becomes the whole body
            node.extract()
            root.clear()
            root.append(node)
        applied += 1

    new_html = root.decode_contents()
    if applied and not new_html.strip():
        raise ContentEditError("that would remove the entire article body")
    return new_html, applied, unmatched
