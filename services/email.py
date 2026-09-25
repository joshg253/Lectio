"""Send article share emails via Resend."""

from __future__ import annotations

import html
import re
import textwrap
from urllib.parse import urlparse

# The app's own tokens (static/themes/light.css / dark.css), so a shared article reads like the entry pane it came from. Email clients
# that honor prefers-color-scheme (Apple Mail, iOS, Outlook apps) get the dark theme too; Gmail ignores it and stays light. Web fonts
# load where supported (Apple Mail) and fall back to system sans elsewhere.
_LIGHT = {
    "bg": "#f2efe7",
    "surface": "#fcfbf7",
    "ink": "#22201a",
    "muted": "#6a6659",
    "line": "#ddd6c6",
    "accent": "#22577a",
    "link": "#0f5f8f",
}
_DARK = {
    "bg": "#15191d",
    "surface": "#1d242a",
    "ink": "#e7e0d2",
    "muted": "#a6b0b8",
    "line": "#374049",
    "accent": "#4f89ab",
    "link": "#7fb3d1",
}
_FONTS_LINK = (
    '<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&family=Merriweather:wght@700&display=swap"'
    ' rel="stylesheet">'
)
_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)
_SANS = "'Source Sans 3', 'Segoe UI', -apple-system, Helvetica, Arial, sans-serif"
_SERIF = "Merriweather, Georgia, 'Times New Roman', serif"


def _base_css() -> str:
    L, D = _LIGHT, _DARK
    return f"""
          body {{ margin: 0; padding: 0; background: {L["bg"]}; color: {L["ink"]}; font-family: {_SANS}; -webkit-text-size-adjust: 100%; }}
          .page {{ max-width: 680px; margin: 0 auto; padding: 20px 12px 28px; }}
          .brandbar {{ display: flex; align-items: baseline; justify-content: space-between; padding: 0 6px 12px; }}
          .wordmark {{ font-family: {_SERIF}; font-weight: 700; font-size: 20px; color: {L["accent"]}; text-decoration: none; }}
          .brand-note {{ font-size: 12px; color: {L["muted"]}; }}
          .card {{ background: {L["surface"]}; border: 1px solid {L["line"]}; border-radius: 12px; overflow: hidden; }}
          .hero {{ display: block; width: 100%; height: auto; max-height: 420px; object-fit: cover; background: #ffffff; }}
          .body {{ padding: 22px 24px 6px; }}
          .meta {{ display: block; font-size: 13px; color: {L["muted"]}; margin: 0 0 8px; }}
          .meta .feed {{ color: {L["accent"]}; font-weight: 600; }}
          h1, h2 {{ font-family: {_SANS}; font-weight: 700; color: {L["ink"]}; margin: 0 0 14px; }}
          h1 {{ font-size: 25px; line-height: 1.25; }}
          h2 {{ font-size: 19px; line-height: 1.3; margin-bottom: 8px; }}
          h1 a, h2 a {{ color: {L["ink"]}; text-decoration: none; }}
          .excerpt {{ font-size: 16px; line-height: 1.55; color: {L["ink"]}; margin: 0 0 16px; }}
          .excerpt a {{ color: {L["link"]}; }}
          /* The card clips overflow rather than scrolling it, so a wide article image (feed HTML ships explicit width/height more
             often than not) would lose its right edge instead of shrinking to the column. */
          .excerpt img, .excerpt iframe {{ max-width: 100%; height: auto; }}
          .excerpt img {{ background: #ffffff; border-radius: 6px; }}
          .excerpt blockquote {{ margin: 0 0 16px; padding: 2px 0 2px 14px; border-left: 3px solid {L["line"]}; color: {L["muted"]}; }}
          .cta-row {{ margin: 6px 0 24px; }}
          .cta {{ display: inline-block; padding: 10px 20px; background: {L["accent"]}; color: #ffffff !important; font-size: 14px;
            font-weight: 600; text-decoration: none; border-radius: 999px; }}
          .item {{ padding: 18px 24px; border-top: 1px solid {L["line"]}; }}
          .item:first-child {{ border-top: none; }}
          .item-link {{ font-size: 13px; font-weight: 600; color: {L["link"]}; text-decoration: none; }}
          .footer {{ padding: 14px 6px 0; font-size: 12px; color: {L["muted"]}; text-align: center; }}
          .footer a {{ color: {L["muted"]}; }}
          @media (prefers-color-scheme: dark) {{
            body {{ background: {D["bg"]} !important; color: {D["ink"]} !important; }}
            .card {{ background: {D["surface"]} !important; border-color: {D["line"]} !important; }}
            .wordmark {{ color: {D["accent"]} !important; }}
            .meta .feed {{ color: {D["accent"]} !important; }}
            .brand-note, .meta, .footer, .footer a, .excerpt blockquote {{ color: {D["muted"]} !important; }}
            h1, h2, h1 a, h2 a, .excerpt {{ color: {D["ink"]} !important; }}
            .excerpt a, .item-link {{ color: {D["link"]} !important; }}
            .item {{ border-top-color: {D["line"]} !important; }}
            .cta {{ background: {D["accent"]} !important; }}
          }}"""


def _page(title: str, note: str, card_html: str) -> str:
    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="color-scheme" content="light dark">
        <meta name="supported-color-schemes" content="light dark">
        <title>{title}</title>
        {_FONTS_LINK}
        <style>{_base_css()}
        </style>
        </head>
        <body>
        <div class="page">
          <div class="brandbar">
            <a class="wordmark" href="https://github.com/joshg253/Lectio">Lectio</a><span class="brand-note">{note}</span>
          </div>
          <div class="card">
        {card_html}
          </div>
          <div class="footer">Shared via <a href="https://github.com/joshg253/Lectio">Lectio</a></div>
        </div>
        </body>
        </html>
    """)


def _meta_line(feed_title: str, published: str | None, author: str | None) -> str:
    parts = []
    if feed_title:
        parts.append(f'<span class="feed">{html.escape(feed_title)}</span>')
    if published:
        parts.append(html.escape(published))
    if author:
        parts.append(f"by {html.escape(author)}")
    return f'<span class="meta">{" · ".join(parts)}</span>' if parts else ""


def _site_label(link: str) -> str:
    host = urlparse(link or "").netloc.lower().removeprefix("www.")
    return f"Read on {host}" if host else "Read article"


def _build_html(
    title: str,
    feed_title: str,
    link: str,
    excerpt: str,
    excerpt_html: str | None = None,
    *,
    lead_image_url: str | None = None,
    author: str | None = None,
    published: str | None = None,
) -> str:
    safe_title = html.escape(title or "(untitled)")
    safe_link = html.escape(link or "")

    # excerpt_html is pre-sanitized article HTML (the full-text case) and is
    # trusted as-is — the same allowlist output already rendered unescaped in
    # the app's own entry pane. Plain excerpt still needs escaping + manual
    # paragraph splitting, since it is unstructured stripped text.
    if excerpt_html:
        # Inline SVG goes: most mail clients (Gmail included) drop it anyway, and what survives readability is icon furniture —
        # a video-embed facade's play-button and wordmark logos render full-column-width with no size of their own.
        excerpt_html = _SVG_RE.sub("", excerpt_html)
        excerpt_block = f'<div class="excerpt">{excerpt_html}</div>'
    else:
        safe_excerpt = html.escape(excerpt or "")
        excerpt_block = "".join(f'<p class="excerpt">{para}</p>' for para in safe_excerpt.split("\n\n") if para)
    # The lead image as a hero, like the entry pane — unless the body already carries it (the pane hoists it; here it would repeat).
    hero = ""
    if lead_image_url and lead_image_url.startswith(("http://", "https://")) and lead_image_url not in (excerpt_html or ""):
        hero = f'<a href="{safe_link}"><img class="hero" src="{html.escape(lead_image_url)}" alt=""></a>'
    card = (
        f'{hero}<div class="body">{_meta_line(feed_title, published, author)}'
        f'<h1><a href="{safe_link}">{safe_title}</a></h1>{excerpt_block}'
        f'<p class="cta-row"><a class="cta" href="{safe_link}">{html.escape(_site_label(link))} →</a></p></div>'
    )
    return _page(safe_title, "shared article", card)


def _build_digest_html(articles: list[dict]) -> str:
    """Build a digest email HTML body listing multiple articles."""
    items = []
    for art in articles:
        safe_title = html.escape(str(art.get("title") or "(untitled)"))
        safe_link = html.escape(str(art.get("link") or ""))
        safe_excerpt = html.escape(str(art.get("excerpt") or ""))
        excerpt_p = f'<p class="excerpt">{safe_excerpt}</p>' if safe_excerpt else ""
        items.append(
            f'<div class="item">{_meta_line(str(art.get("feed_title") or ""), None, None)}'
            f'<h2><a href="{safe_link}">{safe_title}</a></h2>{excerpt_p}'
            f'<a class="item-link" href="{safe_link}">Read →</a></div>'
        )
    count = len(articles)
    return _page("Lectio digest", f"digest · {count} article{'s' if count != 1 else ''}", "".join(items))


def _build_text(title: str, feed_title: str, link: str, excerpt: str) -> str:
    parts = []
    if feed_title:
        parts.append(f"From: {feed_title}")
    parts.append(title or "(untitled)")
    parts.append(link or "")
    if excerpt:
        parts.append("")
        parts.append(excerpt)
    parts.append("")
    parts.append("Shared via Lectio")
    return "\n".join(parts)


def _build_digest_text(articles: list[dict]) -> str:
    parts = [f"Lectio digest — {len(articles)} article(s)", ""]
    for art in articles:
        if art.get("feed_title"):
            parts.append(f"From: {art['feed_title']}")
        parts.append(art.get("title") or "(untitled)")
        if art.get("link"):
            parts.append(art["link"])
        if art.get("excerpt"):
            parts.append(art["excerpt"])
        parts.append("")
    parts.append("Shared via Lectio")
    return "\n".join(parts)


def send_article_email(
    api_key: str,
    from_addr: str,
    to_addr: str,
    title: str,
    feed_title: str,
    link: str,
    excerpt: str,
    cc_addr: str | None = None,
    reply_to: str | None = None,
    excerpt_html: str | None = None,
    *,
    lead_image_url: str | None = None,
    author: str | None = None,
    published: str | None = None,
) -> tuple[bool, str | None]:
    """Send a share email. Returns (ok, error_message).

    excerpt is always plain text, used for the text part and as the HTML
    part's fallback. excerpt_html, when given, is pre-sanitized article HTML
    (the full-text case) and is what the HTML part actually renders — real
    paragraphs/links/lists instead of one escaped blob.
    """
    import resend

    resend.api_key = api_key
    subject = title or "(untitled)"
    payload: dict = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": _build_html(
            title, feed_title, link, excerpt, excerpt_html, lead_image_url=lead_image_url, author=author, published=published
        ),
        "text": _build_text(title, feed_title, link, excerpt),
    }
    if cc_addr:
        payload["cc"] = [cc_addr]
    if reply_to:
        payload["reply_to"] = reply_to
    try:
        resend.Emails.send(payload)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        return True, None
    except Exception as exc:
        return False, str(exc)


def send_digest_email(
    api_key: str,
    from_addr: str,
    to_addr: str,
    articles: list[dict],
    cc_addr: str | None = None,
) -> tuple[bool, str | None]:
    """Send a digest email bundling multiple articles. Returns (ok, error_message)."""
    import resend

    if not articles:
        return False, "No articles to send"

    resend.api_key = api_key
    count = len(articles)
    subject = f"Lectio digest — {count} article{'s' if count != 1 else ''}"
    payload: dict = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": _build_digest_html(articles),
        "text": _build_digest_text(articles),
    }
    if cc_addr:
        payload["cc"] = [cc_addr]
    try:
        resend.Emails.send(payload)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        return True, None
    except Exception as exc:
        return False, str(exc)
