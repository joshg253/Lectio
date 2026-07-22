"""Capture the README/docs screenshots from a running demo Lectio instance.

Driven by :mod:`scripts.refresh_screenshots`, which seeds the demo library and
starts the server first. Uses Playwright (Chromium). The set of shots mirrors the
images referenced from ``README.md``.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - dependency hint
    print(
        "Playwright is not installed. Install the screenshot extra:\n"
        "    uv sync --extra screenshots && uv run playwright install chromium",
        file=sys.stderr,
    )
    raise

_VIEWPORT = {"width": 1440, "height": 900}


def _set_theme(page, theme: str) -> None:
    page.add_init_script(f"window.localStorage.setItem('lectio-theme', {theme!r});")


def _open_first_article(page, base_url: str) -> None:
    """Load a feed's posts and open the first article so the reading pane fills.

    Sidebar feeds live inside collapsed folders whose rows are **lazy** — the
    `<ul data-lazy-feeds>` ships empty and is filled from
    /tree/folder-feeds/{id} on first expand, so on a fresh load there is no
    `.feed-link` in the DOM at all. Expand the first folder, wait for its rows
    to arrive, then navigate to the feed's own list URL and open a post.
    """
    toggle = page.locator(".tree-toggle[data-tree-target]").first
    if toggle.count():
        toggle.click()
        try:
            page.wait_for_selector(".feed-link[href]", timeout=10_000)
        except Exception:  # noqa: BLE001 — fall through to the root list
            pass
    href = page.evaluate(
        "(() => { const el = document.querySelector('.feed-link[href]');"
        " return el ? el.getAttribute('href') : null; })()"
    )
    if href:
        page.goto(base_url.rstrip("/") + href, wait_until="networkidle")
        page.wait_for_timeout(300)
    page.locator(".post-main-link").first.click()
    page.wait_for_timeout(700)


def _shoot(page, out: Path, name: str, *, clip_selector: str | None = None) -> None:
    target = out / name
    if clip_selector:
        page.locator(clip_selector).first.screenshot(path=str(target))
    else:
        page.screenshot(path=str(target), full_page=False)
    print(f"  wrote {target.name}")


def capture_admin(base_url: str, out_dir: Path, admin_user: str, admin_pw: str) -> None:
    """Shoot the multi-user Administration page (with the user-management table).

    Runs against a separate instance booted in ``multi`` mode by the orchestrator.
    Logs in as the bootstrap admin, creates a couple of demo users so the table
    has rows, then captures the Users section (including the Delete action).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport=_VIEWPORT, device_scale_factor=2)  # ty: ignore[invalid-argument-type]
        page = ctx.new_page()
        _set_theme(page, "dark")

        page.goto(base_url + "/login", wait_until="networkidle")
        page.fill("#login-username", admin_user)
        page.fill("#login-password", admin_pw)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")

        # The page is tabbed and opens on Settings; user management lives in the
        # Users panel, which is display:none until its tab is clicked — so the
        # create-user form is in the DOM but not fillable until then.
        def _open_users_tab() -> None:
            page.goto(base_url + "/administration", wait_until="networkidle")
            page.click(".adm-tab[data-panel='panel-users']")
            page.wait_for_selector("#panel-users.active", timeout=10_000)

        for name, pw_ in (("alice", "demo-password"), ("mallory", "demo-password")):
            _open_users_tab()
            page.fill("form[action='/admin/users/create'] input[name='username']", name)
            page.fill("form[action='/admin/users/create'] input[name='password']", pw_)
            page.click("form[action='/admin/users/create'] button[type='submit']")
            page.wait_for_load_state("networkidle")

        _open_users_tab()
        page.wait_for_timeout(400)
        _shoot(page, out_dir, "11administration.png")
        ctx.close()
        browser.close()


def capture(base_url: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def new_page(theme: str):
            ctx = browser.new_context(viewport=_VIEWPORT, device_scale_factor=2)  # ty: ignore[invalid-argument-type]
            pg = ctx.new_page()
            _set_theme(pg, theme)
            return ctx, pg

        # 1 + 2: main reading view, dark and light.
        for theme, name in (("dark", "1dark.png"), ("light", "2light.png")):
            ctx, page = new_page(theme)
            page.goto(base_url, wait_until="networkidle")
            _open_first_article(page, base_url)
            _shoot(page, out_dir, name)
            ctx.close()

        # 3 + 4: Settings modal — Feeds and Automation tabs.
        ctx, page = new_page("dark")
        page.goto(base_url, wait_until="networkidle")
        page.evaluate("window.openSettingsModal('feeds')")
        # Expand the first folder so its feeds are visible in the Feeds tab.
        page.locator(".settings-folder-toggle:not([disabled])").first.click()
        page.wait_for_timeout(500)
        _shoot(page, out_dir, "3settings_feeds.png", clip_selector="#settings-modal")
        page.evaluate("window.openSettingsModal('automation')")
        page.wait_for_timeout(500)
        _shoot(page, out_dir, "4automation.png", clip_selector="#settings-modal")
        ctx.close()

        # 5: Folder Properties modal — opened from the Settings → Feeds folder
        # "settings" glyph (first folder).
        ctx, page = new_page("dark")
        page.goto(base_url, wait_until="networkidle")
        page.evaluate("window.openSettingsModal('feeds')")
        page.wait_for_timeout(400)
        page.locator("[data-settings-folder-id]").first.click()
        page.wait_for_timeout(700)
        _shoot(page, out_dir, "5folderprops.png", clip_selector="#folder-properties-modal")
        ctx.close()

        # 6 + 7: Feed Properties modal (Info + Tuning) — opened from a feed name
        # button in the expanded Settings → Feeds list.
        ctx, page = new_page("dark")
        page.goto(base_url, wait_until="networkidle")
        page.evaluate("window.openSettingsModal('feeds')")
        page.locator(".settings-folder-toggle:not([disabled])").first.click()
        page.wait_for_timeout(400)
        page.locator("[data-feed-properties-url]").first.click()
        page.wait_for_timeout(800)
        _shoot(page, out_dir, "6feedprops.png", clip_selector="#feed-properties-modal")
        page.locator("[data-feed-prop-tab='tuning']").click()
        page.wait_for_timeout(500)
        _shoot(page, out_dir, "7feedtuning.png", clip_selector="#feed-properties-modal")
        # 8: same modal, History tab (synthetic fetch history).
        page.locator("[data-feed-prop-tab='history']").click()
        page.wait_for_timeout(600)
        _shoot(page, out_dir, "8feedhistory.png", clip_selector="#feed-properties-modal")
        ctx.close()

        # 9: Tag filtering — click a sidebar tag, then open the first tagged post.
        ctx, page = new_page("dark")
        page.goto(base_url, wait_until="networkidle")
        page.locator(".tag-link").first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(300)
        page.locator(".post-main-link").first.click()
        page.wait_for_timeout(700)
        _shoot(page, out_dir, "9tags.png")
        ctx.close()

        # 10: Read History view (read_filter=history) — navigate via its menu link.
        ctx, page = new_page("dark")
        page.goto(base_url, wait_until="networkidle")
        href = page.evaluate(
            "(() => { const el = document.querySelector('.filter-history-item[href]');"
            " return el ? el.getAttribute('href') : null; })()"
        )
        if href:
            page.goto(base_url.rstrip("/") + href, wait_until="networkidle")
            page.wait_for_timeout(500)
            _shoot(page, out_dir, "10history.png")
        ctx.close()

        browser.close()
