"""Reformat IsThereAnyDeal waitlist-feed bodies into one compact line per game.

The feed ships a stack of unstyled divs per game (title link, "Historical low" line, then price link, discount, store and optional
voucher each on its own line), which renders as a long column of short lines. This folds each offer into one line; anything that
doesn't look like that markup is returned untouched.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

_LOW_RE = re.compile(r"Historical low:\s*([^\s]+)")
_PERCENT_RE = re.compile(r"-?\d+%")
_PRICE_RE = re.compile(r"[\d.,]+")


def is_itad_feed(feed_url: str) -> bool:
    parsed = urlparse(feed_url or "")
    return (parsed.hostname or "").endswith("isthereanydeal.com") and parsed.path.startswith("/feeds/")


def _squash(text: str) -> str:
    return " ".join(text.split())


def _amount(price: str) -> float | None:
    match = _PRICE_RE.search(price.replace(",", ""))
    try:
        return float(match.group(0)) if match else None
    except ValueError:
        return None


def _link(tag: Tag) -> str:
    href = html.escape(str(tag.get("href") or ""), quote=True)
    return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{html.escape(_squash(tag.get_text()))}</a>'


def _offer_line(offer: Tag, low: str) -> str | None:
    price_link = offer.find("a")
    if price_link is None:
        return None
    price = _squash(price_link.get_text())
    spans = [_squash(s.get_text()) for s in offer.find_all("span")]
    percent = next((s for s in spans if _PERCENT_RE.fullmatch(s)), "")
    # Text after the "on" span is the store; a "with voucher" span is followed by its code.
    store, voucher = "", ""
    seen_on = seen_voucher = False
    for node in price_link.next_siblings:
        if isinstance(node, Tag):
            label = _squash(node.get_text())
            if label == "on":
                seen_on = True
            elif label == "with voucher":
                seen_voucher = True
            continue
        text = _squash(str(node))
        if not text:
            continue
        if seen_voucher:
            voucher = text
        elif seen_on:
            store = text
    parts = [f"<strong>{_link(price_link)}</strong>"]
    if percent:
        parts.append(html.escape(percent.replace("-", "−")))
    if store:
        parts.append("on " + html.escape(store))
    if voucher:
        parts.append(f"with voucher <code>{html.escape(voucher)}</code>")
    line = " · ".join(parts)
    now, floor = _amount(price), _amount(low)
    if now is not None and floor is not None and now <= floor:
        line += " · <strong>historical low</strong>"
    return line


def reformat(body: str) -> str:
    """The compact list, or *body* unchanged when it doesn't parse as game blocks."""
    if not body or "Historical low" not in body:
        return body
    soup = BeautifulSoup(body, "html.parser")
    # A game block is the div whose own direct child is the /game/ info link; the feed may or may not wrap them in one outer div.
    games = [d for d in soup.find_all("div") if d.find("a", href=re.compile(r"/game/"), recursive=False)]
    if not games:
        return body
    items: list[str] = []
    for game in games:
        title_link = game.find("a", href=re.compile(r"/game/"), recursive=False)
        low_match = _LOW_RE.search(_squash(game.get_text()))
        low = low_match.group(1) if low_match else ""
        offers = [
            line
            for line in (_offer_line(o, low) for o in game.find_all("div") if o.find("a", recursive=False) and not o.find("div"))
            if line
        ]
        if title_link is None or not offers:
            return body
        low_note = f" <small>(low {html.escape(low)})</small>" if low else ""
        items.append(f"<li>{_link(title_link)}{low_note}<br>" + "<br>".join(offers) + "</li>")
    return "<ul>" + "".join(items) + "</ul>"
