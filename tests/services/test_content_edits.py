"""Replaying pane cleanup operations against stored article HTML.

The browser sends what it removed (path + fingerprint), never the edited DOM,
so these tests are about the matcher: it must find the node the user clicked
even when the stored tree has drifted from the rendered one, and must refuse to
guess when it can't.
"""
from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from services import content_edits

HTML = (
    "<p>First paragraph.</p>"
    '<div class="related alignright"><a href="/x">Related junk</a></div>'
    "<p>Second paragraph.</p>"
)


def _op(html: str, path: list[int], op: str = content_edits.OP_REMOVE) -> dict:
    """Build an op the way the browser would: fingerprint the node the path
    lands on in the *rendered* tree."""
    soup = BeautifulSoup(f"<div>{html}</div>", "html.parser")
    node = content_edits._resolve_by_path(soup.div, path)
    assert node is not None, "test fixture path does not resolve"
    return {"op": op, "path": path, "fp": content_edits.fingerprint(node)}


def test_remove_by_path():
    new_html, applied, unmatched = content_edits.apply_ops(HTML, [_op(HTML, [1])])
    assert applied == 1 and unmatched == []
    assert "Related junk" not in new_html
    assert "First paragraph." in new_html and "Second paragraph." in new_html


def test_isolate_keeps_only_the_selection():
    new_html, applied, unmatched = content_edits.apply_ops(
        HTML, [_op(HTML, [2], content_edits.OP_ISOLATE)]
    )
    assert applied == 1 and unmatched == []
    assert new_html.strip() == "<p>Second paragraph.</p>"


def test_ops_apply_in_order_against_a_mutating_tree():
    """The client derives each path from the DOM as it stands after the
    previous click, so index 1 means different nodes in the two ops."""
    first = _op(HTML, [0])
    after_first = "".join(str(t) for t in BeautifulSoup(f"<div>{HTML}</div>", "html.parser").div.contents[1:])
    second = _op(after_first, [0])
    new_html, applied, unmatched = content_edits.apply_ops(HTML, [first, second])
    assert applied == 2 and unmatched == []
    assert new_html.strip() == "<p>Second paragraph.</p>"


def test_fingerprint_rescues_a_shifted_path():
    """A render-time cleanup removed a node the stored tree still has, so the
    path is off by one. The fingerprint has to find the intended node anyway."""
    rendered = '<div class="related alignright"><a href="/x">Related junk</a></div><p>Second paragraph.</p>'
    op = _op(rendered, [0])  # path [0] in the rendered tree, [1] in the stored one
    new_html, applied, unmatched = content_edits.apply_ops(HTML, [op])
    assert applied == 1 and unmatched == []
    assert "Related junk" not in new_html
    assert "First paragraph." in new_html


def test_unmatched_op_is_reported_not_guessed():
    """A node that only exists in the rendered page (an injected embed) has
    nothing to delete in the stored body — say so rather than deleting
    whatever the path happens to hit."""
    rendered = '<div class="embed-container"><iframe src="https://youtube.com/embed/x"></iframe></div>'
    op = _op(rendered, [0])
    new_html, applied, unmatched = content_edits.apply_ops(HTML, [op])
    assert applied == 0
    assert len(unmatched) == 1 and unmatched[0]["tag"] == "div"
    assert new_html.replace("\n", "") == HTML


def test_ambiguous_fingerprint_is_left_unmatched():
    """Two identical nodes and a path that resolves to neither: refuse."""
    html = "<ul><li>same</li><li>same</li></ul>"
    op = {"op": "remove", "path": [9], "fp": content_edits.fingerprint(
        BeautifulSoup("<li>same</li>", "html.parser").li)}
    _new_html, applied, unmatched = content_edits.apply_ops(html, [op])
    assert applied == 0 and len(unmatched) == 1


def test_proxied_image_fingerprints_as_its_source():
    """The rendered body routes hotlinked images through /api/img; the stored
    body has the original URL. Both must fingerprint the same."""
    stored = '<p><img src="https://cdn.example.com/a/pic.jpg"></p>'
    rendered = '<p><img src="/api/img?u=https%3A%2F%2Fcdn.example.com%2Fa%2Fpic.jpg"></p>'
    op = _op(rendered, [0, 0])
    new_html, applied, unmatched = content_edits.apply_ops(stored, [op])
    assert applied == 1 and unmatched == []
    assert "<img" not in new_html and "<p>" in new_html


def test_removing_everything_is_refused():
    with pytest.raises(content_edits.ContentEditError):
        content_edits.apply_ops("<p>only</p>", [_op("<p>only</p>", [0])])


def test_empty_body_is_refused():
    with pytest.raises(content_edits.ContentEditError):
        content_edits.apply_ops("   ", [{"op": "remove", "path": [0], "fp": {"tag": "p"}}])


@pytest.mark.parametrize("payload", [
    "not json",
    "[]",
    '[{"op": "explode", "path": [0], "fp": {}}]',
    '[{"op": "remove", "path": [], "fp": {}}]',
    '[{"op": "remove", "path": [-1], "fp": {}}]',
    '[{"op": "remove", "path": [0]}]',
])
def test_parse_ops_rejects_malformed_payloads(payload):
    with pytest.raises(content_edits.ContentEditError):
        content_edits.parse_ops(payload)


def test_parse_ops_caps_the_batch():
    ops = [{"op": "remove", "path": [0], "fp": {"tag": "p"}}] * (content_edits.MAX_OPS + 1)
    with pytest.raises(content_edits.ContentEditError):
        content_edits.parse_ops(ops)


def test_every_refusal_carries_a_code_the_route_can_map():
    """The route words these errors itself, keyed by `code`, so nothing derived
    from an exception object reaches a response (CodeQL: py/stack-trace-exposure).
    A code with no entry in that table would silently degrade every message for
    that case to the generic fallback."""
    import main

    codes = set()
    for payload in ("not json", "[]", '[{"op": "explode", "path": [0], "fp": {}}]',
                    '[{"op": "remove", "path": [], "fp": {}}]',
                    '[{"op": "remove", "path": [0]}]'):
        try:
            content_edits.parse_ops(payload)
        except content_edits.ContentEditError as exc:
            codes.add(exc.code)
    for html_in, ops in (("   ", [{"op": "remove", "path": [0], "fp": {"tag": "p"}}]),
                         ("<p>only</p>", [_op("<p>only</p>", [0])])):
        try:
            content_edits.apply_ops(html_in, ops)
        except content_edits.ContentEditError as exc:
            codes.add(exc.code)

    assert codes, "no refusals were raised — the test is not exercising anything"
    unmapped = codes - set(main._CLEANUP_ERROR_MESSAGES)
    assert not unmapped, f"ContentEditError codes with no user-facing wording: {unmapped}"


def test_too_many_ops_is_mapped_too():
    import main
    try:
        content_edits.parse_ops([{"op": "remove", "path": [0], "fp": {}}] * (content_edits.MAX_OPS + 1))
    except content_edits.ContentEditError as exc:
        assert exc.code in main._CLEANUP_ERROR_MESSAGES

# --- text as the last resort -------------------------------------------------
#
# The browser fingerprints the RENDERED body — sanitized (attributes and classes
# stripped), lead image hoisted out (so sibling indices shift), per-feed cleanups
# applied — while ops replay against the STORED body. Structure therefore
# disagrees for nodes that are plainly the same paragraph, which is how
# "remove this boilerplate" reported that nothing could be matched.

_BOILER = ("I am the author. I write essays about technology and philosophy, "
           "and here are a few things I have built.")


def test_a_node_is_found_by_text_when_structure_disagrees():
    stored = f'<p class="stored-only">{_BOILER}</p><p>The actual article.</p>'
    ops = [{"op": "remove", "path": [99],
            "fp": {"tag": "p", "id": "", "cls": ["rendered-only"],
                   "text": _BOILER, "kids": 0, "src": ""}}]

    html, applied, unmatched = content_edits.apply_ops(stored, ops)

    assert (applied, unmatched) == (1, [])
    assert _BOILER not in html
    assert "The actual article." in html


def test_ambiguous_text_is_refused():
    """Deleting the wrong paragraph is worse than declining to delete one."""
    stored = f"<p>{_BOILER}</p><p>{_BOILER}</p>"
    ops = [{"op": "remove", "path": [99],
            "fp": {"tag": "p", "id": "", "cls": ["x"], "text": _BOILER,
                   "kids": 0, "src": ""}}]

    _html, applied, unmatched = content_edits.apply_ops(stored, ops)

    assert applied == 0
    assert len(unmatched) == 1


def test_short_text_is_not_enough_for_the_TEXT_fallback():
    """A word is not a passage. Tested against the fallback directly: the
    structural scorer can still match a short node on its own evidence (exact
    text plus child count), and that behaviour is deliberately unchanged."""
    root = BeautifulSoup('<div><p class="a">Read more</p></div>', "html.parser").div
    target = {"tag": "p", "id": "", "cls": ["b"], "text": "Read more",
              "kids": 0, "src": ""}

    assert content_edits._resolve_by_text(root, target) is None


def test_a_different_tag_is_never_matched_by_text():
    stored = f"<div>{_BOILER}</div>"
    ops = [{"op": "remove", "path": [99],
            "fp": {"tag": "p", "id": "", "cls": [], "text": _BOILER,
                   "kids": 0, "src": ""}}]

    _html, applied, _unmatched = content_edits.apply_ops(stored, ops)

    assert applied == 0
