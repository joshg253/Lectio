"""Same-origin redirect guard.

Every ``RedirectResponse`` target built from request data goes through ``local_url`` so the destination can only be a path on this app.
The CodeQL copy of ``py/url-redirection`` (``.github/codeql/queries/``) treats ``local_url``'s return value as sanitized, so calling it
is also what clears the alert; keep the name and module path in sync with ``LectioUrlRedirectSanitizers.qll``.
"""

from __future__ import annotations


def local_url(url: str | None) -> str:
    """Return ``url`` only if it is a same-origin path, else ``/``.

    Rejects off-site absolute URLs and the protocol-relative (``//evil.com``) and backslash (``/\\evil.com``) forms browsers normalize
    to an external authority, plus control characters (header-splitting / whitespace tricks).
    """
    if not url or not url.startswith("/") or url.startswith(("//", "/\\")):
        return "/"
    if any(ord(ch) < 32 for ch in url):
        return "/"
    return url
