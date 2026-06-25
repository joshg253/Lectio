import uuid as _uuid


def assert_safe_feed_id(feed_id: "str | _uuid.UUID") -> None:
    """Validate that feed_id is a UUID before it is used in a filesystem path."""
    if isinstance(feed_id, _uuid.UUID):
        return
    try:
        _uuid.UUID(feed_id)
    except ValueError:
        raise ValueError(f"Invalid feed_id: {feed_id!r}")
