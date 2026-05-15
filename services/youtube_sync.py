"""YouTube subscription sync: add/remove RSS feeds based on YouTube channel subscriptions.

Requires YOUTUBE_API_KEY and YOUTUBE_CHANNEL_ID (or handle) to be set.
The target channel's subscriptions must be set to Public on YouTube.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

LOGGER = logging.getLogger(__name__)

YT_FEED_PREFIX = "https://www.youtube.com/feeds/videos.xml?channel_id="
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_SUBSCRIPTIONS_URL = "https://www.googleapis.com/youtube/v3/subscriptions"


def resolve_channel_id(api_key: str, identifier: str) -> str | None:
    """Return the channel ID for a handle (@foo), username, or raw channel ID.

    Returns None if the channel cannot be found.
    """
    identifier = identifier.strip().lstrip("/").rstrip("/")
    if not identifier:
        return None

    # Already looks like a channel ID (UCxxxxxxxxxxxxxxxxxxxxxxxx — 24 chars starting UC)
    if identifier.startswith("UC") and len(identifier) >= 22:
        return identifier

    base_params = {"part": "id", "key": api_key, "maxResults": 1}
    with httpx.Client(timeout=15.0) as client:
        # Try forHandle first (@-prefixed or bare handle)
        handle = identifier if identifier.startswith("@") else f"@{identifier}"
        r = client.get(_CHANNELS_URL, params={**base_params, "forHandle": handle})
        if r.status_code == 200:
            items = r.json().get("items", [])
            if items:
                return items[0]["id"]

        # Fallback: forUsername (legacy usernames without @)
        r = client.get(_CHANNELS_URL, params={**base_params, "forUsername": identifier.lstrip("@")})
        if r.status_code == 200:
            items = r.json().get("items", [])
            if items:
                return items[0]["id"]

    return None


def fetch_subscriptions(api_key: str, channel_id: str) -> list[dict]:
    """Return all subscriptions for the given channel as a list of dicts.

    Each dict has: channel_id, title.
    Raises httpx.HTTPStatusError if the API returns an error (e.g. 403 for private subs).
    """
    results: list[dict] = []
    page_token: str | None = None

    with httpx.Client(timeout=15.0) as client:
        while True:
            params: dict = {
                "part": "snippet",
                "channelId": channel_id,
                "maxResults": 50,
                "key": api_key,
                "order": "alphabetical",
            }
            if page_token:
                params["pageToken"] = page_token

            r = client.get(_SUBSCRIPTIONS_URL, params=params)
            r.raise_for_status()
            data = r.json()

            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                resource = snippet.get("resourceId", {})
                ch_id = resource.get("channelId", "")
                title = snippet.get("title", "")
                if ch_id:
                    results.append({"channel_id": ch_id, "title": title})

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(0.05)

    return results


def sync_youtube_folder(
    *,
    api_key: str,
    channel_identifier: str,
    folder_id: int,
    get_folder_feed_urls: Callable[[int], list[str]],
    add_feed: Callable[[str, int], None],
    remove_feed: Callable[[str, int], None],
) -> dict:
    """Sync subscriptions to/from a Lectio folder.

    Returns {"added": N, "removed": N, "total": N, "error": str | None}.
    """
    result: dict = {"added": 0, "removed": 0, "total": 0, "error": None}

    # 1. Resolve channel ID
    channel_id = resolve_channel_id(api_key, channel_identifier)
    if not channel_id:
        result["error"] = f"Could not resolve YouTube channel: {channel_identifier!r}"
        LOGGER.error("YouTube sync: %s", result["error"])
        return result

    # 2. Fetch current subscriptions from YouTube
    try:
        subscriptions = fetch_subscriptions(api_key, channel_id)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 403:
            result["error"] = (
                "YouTube API returned 403 — the channel's subscriptions may be set to Private. "
                "Go to YouTube > Manage all subscriptions > Settings and set them to Public."
            )
        else:
            result["error"] = f"YouTube API error {status}: {exc}"
        LOGGER.error("YouTube sync: %s", result["error"])
        return result
    except Exception as exc:
        result["error"] = f"Failed to fetch subscriptions: {exc}"
        LOGGER.error("YouTube sync: %s", result["error"])
        return result

    result["total"] = len(subscriptions)
    subscribed_ids: set[str] = {s["channel_id"] for s in subscriptions}
    LOGGER.info("YouTube sync: %d subscriptions found for channel %s", len(subscriptions), channel_id)

    # 3. Determine what's currently in the folder (YouTube feeds only)
    current_urls = get_folder_feed_urls(folder_id)
    current_yt: dict[str, str] = {}  # channel_id -> feed_url
    for url in current_urls:
        if url.startswith(YT_FEED_PREFIX):
            ch_id = url[len(YT_FEED_PREFIX):]
            if ch_id:
                current_yt[ch_id] = url

    # 4. Add new
    for sub in subscriptions:
        ch_id = sub["channel_id"]
        if ch_id not in current_yt:
            feed_url = f"{YT_FEED_PREFIX}{ch_id}"
            try:
                add_feed(feed_url, folder_id)
                result["added"] += 1
                LOGGER.info("YouTube sync: added %s (%s)", sub["title"], ch_id)
            except Exception as exc:
                LOGGER.warning("YouTube sync: failed to add %s: %s", ch_id, exc)

    # 5. Remove unsubscribed (only YouTube feeds that are no longer subscribed)
    for ch_id, feed_url in current_yt.items():
        if ch_id not in subscribed_ids:
            try:
                remove_feed(feed_url, folder_id)
                result["removed"] += 1
                LOGGER.info("YouTube sync: removed channel %s", ch_id)
            except Exception as exc:
                LOGGER.warning("YouTube sync: failed to remove %s: %s", ch_id, exc)

    LOGGER.info(
        "YouTube sync complete: +%d -%d, total=%d",
        result["added"], result["removed"], result["total"],
    )
    return result
