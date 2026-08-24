import os as _os
import uuid as _uuid

# `reader` ships its own vendored feedparser (reader._vendor.feedparser, 6.0.11)
# and uses it unless this variable is set. Two reasons Lectio opts out:
#
# 1. The vendored copy's sgml.py does a bare `import sgmllib`, which was only
#    ever satisfied transitively by feedparser 6.0.12's sgmllib3k dependency.
#    feedparser 6.0.13 replaced that with a self-contained feedparser.sgml and
#    dropped sgmllib3k, so on 6.0.14 the vendored copy no longer imports at all.
# 2. main.py's _parse_month_first_pubdate is registered on the standalone
#    feedparser. While reader used the vendored copy the handler never applied
#    to ingest — only to Lectio's own direct feedparser.parse calls.
#
# So the whole process parses with one feedparser, the installed one. This must
# be set before anything imports `reader`, which is why importers that reach
# reader ahead of the services package (main.py, tests/conftest.py) import this
# package first. `setdefault`, so an operator can still force the vendored copy.
_os.environ.setdefault("READER_NO_VENDORED_FEEDPARSER", "1")


def assert_safe_feed_id(feed_id: "str | _uuid.UUID") -> None:
    """Validate that feed_id is a UUID before it is used in a filesystem path."""
    if isinstance(feed_id, _uuid.UUID):
        return
    try:
        _uuid.UUID(feed_id)
    except ValueError:
        raise ValueError(f"Invalid feed_id: {feed_id!r}") from None
