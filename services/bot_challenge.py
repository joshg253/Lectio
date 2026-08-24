"""Recognize an anti-bot challenge served in place of a feed.

A blocked feed and a broken feed need opposite responses — one is worth an email
to the site, the other a Change URL or an unsubscribe — but they arrive looking
the same: a 2xx response whose body is HTML, which then fails to parse. Poorly
Drawn Lines was filed as *"could not be parsed as a valid RSS/Atom document"*
for months while actually returning:

    HTTP 202, text/html, 173 bytes
    <meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=%2Ffeed&y=ipr:<our-ip>:...">

Note the `202` — not 403 — which is why it never showed up in a count of blocked
feeds, and why "how many feeds are bot-walled" was unanswerable.

These challenges are keyed on the *client IP*, not the user-agent: the same URL
fetched with Lectio's honest UA and with a full browser identity gets the byte-
identical challenge. So detecting one is not a prelude to working around it —
escalating the UA does nothing. It exists to label the failure honestly.
"""
from __future__ import annotations

# Vendor markers, matched case-insensitively against the first bytes of the body.
# Deliberately specific strings rather than "looks like HTML": a site legitimately
# serving its homepage at a dead feed URL is a *different* failure (moved/dropped),
# and calling that "blocked" would send someone chasing an imaginary block.
_MARKERS: tuple[tuple[str, str], ...] = (
    ("/.well-known/sgcaptcha", "SiteGround captcha"),
    ("cf-browser-verification", "Cloudflare challenge"),
    ("challenge-platform", "Cloudflare challenge"),
    ("cf_chl_", "Cloudflare challenge"),
    ("just a moment...", "Cloudflare challenge"),
    ("attention required! | cloudflare", "Cloudflare block"),
    ("_incapsula_resource", "Imperva/Incapsula challenge"),
    ("sucuri_cloudproxy", "Sucuri firewall"),
    ("ddos-guard", "DDoS-Guard challenge"),
    ("awswaf", "AWS WAF challenge"),
    ("/cdn-cgi/challenge", "Cloudflare challenge"),
)

# Only the head of the body is examined: every marker above appears in the
# document head or an early script/meta tag, and a challenge page is tiny.
_SNIFF_BYTES = 4096

# Header name -> (expected value substring, label). Checked before the body
# markers above, and the only thing that can catch a challenge at all when
# the response body is empty (kcls.org's AWS WAF challenge: HTTP 202,
# 0-byte body, nothing for the body-sniffer to see — found 2026-08-23 after
# it surfaced as a raw AttributeError crash in reader's own parser instead of
# a labeled block). Header names are matched case-insensitively; requests'
# CaseInsensitiveDict already does that for the lookup itself.
_HEADER_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("x-amzn-waf-action", "challenge", "AWS WAF challenge"),
)


def detect_challenge_headers(headers) -> str | None:
    """Return a vendor label if the response *headers* alone signal a challenge.

    Call this even when the body is empty — that is exactly the case it
    exists for. ``headers`` is anything supporting ``.get()`` case-
    insensitively (a ``requests``/``httpx`` response's own headers object).
    """
    if not headers:
        return None
    for header, value, label in _HEADER_MARKERS:
        try:
            got = headers.get(header)
        except Exception:  # noqa: BLE001 — an odd headers object is not a challenge
            return None
        if got and value in got.lower():
            return label
    return None


def detect_challenge(content_type: str | None, body: bytes | None) -> str | None:
    """Return a vendor label if *body* is an anti-bot challenge, else None.

    Status is deliberately NOT part of the test. These are served as 200, 202,
    403 and 503 depending on vendor and mood, so keying on it is how the 202 case
    stayed invisible.
    """
    if not body:
        return None
    ct = (content_type or "").lower()
    # A challenge is always an HTML document. An XML body that happens to contain
    # a marker string is a feed talking about Cloudflare, not a challenge.
    if ct and "html" not in ct and "text/plain" not in ct:
        return None
    try:
        head = body[:_SNIFF_BYTES].decode("utf-8", "ignore").lower()
    except Exception:  # noqa: BLE001 — undecodable bytes are not a challenge page
        return None
    if "<" not in head:
        return None
    for marker, label in _MARKERS:
        if marker in head:
            return label
    return None


class FeedBlockedError(Exception):
    """Raised instead of letting a challenge page fail as a parse error.

    The message is the value that lands in ``feed_failure_state.last_error``, so
    it is kept stable and greppable — the failing-feeds triage buckets on
    ``bot challenge:``.
    """

    def __init__(self, label: str, url: str = "") -> None:
        self.label = label
        self.url = url
        super().__init__(f"bot challenge: blocked by {label} (the site served a "
                         f"challenge page instead of the feed)")
