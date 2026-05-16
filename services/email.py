"""Send article share emails via Resend."""

from __future__ import annotations

import html
import textwrap


def _build_html(title: str, feed_title: str, link: str, excerpt: str) -> str:
    safe_title = html.escape(title or "(untitled)")
    safe_feed = html.escape(feed_title or "")
    safe_link = html.escape(link or "")
    safe_excerpt = html.escape(excerpt or "")

    excerpt_block = (
        f'<p class="excerpt">{safe_excerpt}</p>' if safe_excerpt else ""
    )
    feed_line = (
        f'<span class="meta">from <strong>{safe_feed}</strong></span>' if safe_feed else ""
    )

    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
          body {{
            margin: 0; padding: 0;
            background: #f5f4f0;
            font-family: Georgia, 'Times New Roman', serif;
            color: #1a1a1a;
          }}
          .wrapper {{
            max-width: 600px;
            margin: 32px auto;
            background: #ffffff;
            border-radius: 6px;
            overflow: hidden;
            box-shadow: 0 1px 4px rgba(0,0,0,.10);
          }}
          .header {{
            background: #1a1a1a;
            padding: 18px 28px;
            display: flex;
            align-items: center;
            gap: 10px;
          }}
          .wordmark {{
            color: #f5f4f0;
            font-family: Georgia, serif;
            font-size: 22px;
            font-weight: normal;
            letter-spacing: .04em;
            margin: 0;
          }}
          .tagline {{
            color: #888;
            font-family: -apple-system, sans-serif;
            font-size: 12px;
            margin: 2px 0 0;
          }}
          .body {{
            padding: 28px 28px 8px;
          }}
          .meta {{
            font-family: -apple-system, sans-serif;
            font-size: 12px;
            color: #888;
            margin-bottom: 10px;
            display: block;
          }}
          h1 {{
            margin: 0 0 14px;
            font-size: 22px;
            font-weight: normal;
            line-height: 1.35;
            color: #111;
          }}
          h1 a {{
            color: #111;
            text-decoration: none;
            border-bottom: 1px solid #ccc;
          }}
          h1 a:hover {{
            border-bottom-color: #111;
          }}
          .excerpt {{
            font-size: 15px;
            line-height: 1.65;
            color: #333;
            margin: 0 0 20px;
          }}
          .cta {{
            display: inline-block;
            margin: 4px 0 28px;
            padding: 9px 18px;
            background: #1a1a1a;
            color: #f5f4f0 !important;
            font-family: -apple-system, sans-serif;
            font-size: 13px;
            text-decoration: none;
            border-radius: 4px;
          }}
          .footer {{
            border-top: 1px solid #eee;
            padding: 14px 28px;
            font-family: -apple-system, sans-serif;
            font-size: 11px;
            color: #aaa;
          }}
        </style>
        </head>
        <body>
        <div class="wrapper">
          <div class="header">
            <div>
              <p class="wordmark">Lectio</p>
              <p class="tagline">shared article</p>
            </div>
          </div>
          <div class="body">
            {feed_line}
            <h1><a href="{safe_link}">{safe_title}</a></h1>
            {excerpt_block}
            <a class="cta" href="{safe_link}">Read article →</a>
          </div>
          <div class="footer">
            Shared via <a href="https://github.com/lectio/lectio" style="color:#aaa">Lectio</a>
          </div>
        </div>
        </body>
        </html>
    """)


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


def send_article_email(
    api_key: str,
    from_addr: str,
    to_addr: str,
    title: str,
    feed_title: str,
    link: str,
    excerpt: str,
) -> tuple[bool, str | None]:
    """Send a share email. Returns (ok, error_message)."""
    import resend

    resend.api_key = api_key
    subject = title or "(untitled)"
    try:
        resend.Emails.send({
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "html": _build_html(title, feed_title, link, excerpt),
            "text": _build_text(title, feed_title, link, excerpt),
        })
        return True, None
    except Exception as exc:
        return False, str(exc)
