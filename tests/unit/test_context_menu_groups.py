"""Right-click menus are grouped into `.ctx-group` sections; the divider/empty-group logic lives in app.js (`tidyMenuGroups`)."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MENUS = (ROOT / "templates" / "_context_menus.html").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


class _Menus(HTMLParser):
    """Collect, per top-level context menu, its direct children (tag, classes) and every button id inside."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.menu: str | None = None
        self.menu_depth = 0
        self.children: dict[str, list[tuple[str, str]]] = {}
        self.button_ids: dict[str, list[str]] = {}
        self._void = {"br", "hr", "img", "input", "meta", "link"}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.menu is None and "context-menu" in (a.get("class") or "").split() and a.get("id"):
            self.menu, self.menu_depth = a["id"], self.depth
            self.children[self.menu], self.button_ids[self.menu] = [], []
        elif self.menu is not None:
            if self.depth == self.menu_depth + 1:
                self.children[self.menu].append((tag, a.get("class") or ""))
            if tag == "button" and a.get("id"):
                self.button_ids[self.menu].append(a["id"])
        if tag not in self._void:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in self._void:
            return
        self.depth -= 1
        if self.menu is not None and self.depth == self.menu_depth:
            self.menu = None


def _parse() -> _Menus:
    parser = _Menus()
    parser.feed(MENUS)
    return parser


def test_grouped_menus_hold_only_ctx_groups():
    parsed = _parse()
    for menu in ("folder-context-menu", "post-context-menu", "tag-context-menu"):
        assert parsed.children[menu], menu
        assert all(tag == "div" and "ctx-group" in cls.split() for tag, cls in parsed.children[menu]), menu


def test_post_menu_keeps_its_actions_and_puts_delete_in_its_own_last_group():
    parsed = _parse()
    ids = parsed.button_ids["post-context-menu"]
    for needed in ("ctx-post-mark-read", "ctx-post-copy-url", "ctx-post-add-tag", "ctx-post-move-to-feed", "ctx-post-delete"):
        assert needed in ids
    # Destructive items come after everything else.
    assert ids.index("ctx-post-delete") > ids.index("ctx-post-automation") > ids.index("ctx-post-copy-url")


def test_tidy_runs_whenever_a_menu_is_positioned():
    body = APP_JS.split("function positionMenuInViewport(", 1)[1].split("\n    }", 1)[0]
    assert "tidyMenuGroups(menu)" in body
