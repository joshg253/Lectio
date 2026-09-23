"""Module-level singleton state: thread-safety caches and locks shared across main.py.

Pure relocation target — this module holds the in-process caches, locks, and a
few rebound counters that used to be declared inline, scattered through
main.py. It depends on nothing in main.py (one-directional dependency), so
main.py, and eventually the route modules split out of it, can import these
names without redefining them and without any circular-import concerns.

Every name here is re-exported into main.py's namespace via a single
``from state import (...)`` block, so existing bare-name references throughout
main.py (and any test that does ``monkeypatch.setattr(main, "_login_failures", ...)``)
keep resolving unchanged.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime

from services import tenancy

# --- Instance-level settings cache -----------------------------------------
# Instance-level settings are saved from the Administration page into the
# saving admin's own app_settings, but their consumers run in arbitrary
# contexts: background threads (maintenance hour, image-cache eviction),
# pre-auth requests (login lockout), or other users' requests. The admin-tier
# lookup below makes them resolvable from any context. TTL-cached because
# list_users() is a DB query and get_img_cache_max_dim sits on the /api/img
# hot path; the settings-save route invalidates it so edits apply immediately.
_INSTANCE_SETTING_TTL_SECONDS = 60.0
_instance_setting_cache: dict[str, tuple[float, str]] = {}
_instance_setting_cache_lock = threading.Lock()

# --- DeviantArt watch-list sync serialization -------------------------------
# One watch-list sync per user at a time: the Settings button, the daily
# maintenance run, and a scheduled auto-resume can otherwise overlap and burn
# the same DeviantArt quota adding the same artists.
_da_sync_lock = threading.Lock()
_da_sync_active: set[str] = set()
_DA_SYNC_MAX_AUTO_RESUMES = 12

# --- Refresh-in-progress tracking -------------------------------------------
updating_feeds_lock = threading.Lock()
updating_feeds: set[str] = set()

# --- Scheduler liveness, read by the watchdog and /healthz ------------------
# Written by the scheduler thread, read by the watchdog and by request handlers,
# so every field is touched under this lock. Kept as module state rather than on
# app.state because the watchdog must be able to read it during startup, before
# the first pass has run.
_scheduler_state_lock = threading.Lock()
_scheduler_state: dict[str, object] = {
    # monotonic seconds; None when no pass is in flight
    "pass_started_at": None,
    "last_pass_finished_at": None,
    # Last time the pass advanced at all, and what it was doing. "Advanced" is
    # the honest liveness signal: elapsed time alone can't distinguish a slow
    # 2,500-feed pass from a socket read that will never return.
    "last_progress_at": None,
    "stage": "idle",
    "consecutive_stall_logs": 0,
}

# --- Lead-image / YouTube-duration enhancement in-flight tracking -----------
# Lead-image / YouTube-duration enhancement is network-heavy, so manual refresh
# runs it off the request path. Track in-flight feeds to skip overlapping work.
_enhancement_inflight_lock = threading.Lock()
_enhancement_inflight_feeds: set[str] = set()

# --- Per-user last-seen touch throttle --------------------------------------
_LAST_SEEN_THROTTLE_SECONDS = 300
_last_seen_touch: dict[str, float] = {}
_last_seen_touch_lock = threading.Lock()

# --- Prefetch diagnostic + home-request concurrency cap ---------------------
_home_request_semaphore = threading.Semaphore(int(os.getenv("LECTIO_MAX_CONCURRENT_HOME_REQUESTS", "4")))
_prefetch_header_log_remaining = [10]  # diagnostic: log headers of the first N suspect requests
_prefetch_header_log_lock = threading.Lock()

# --- App settings cache ------------------------------------------------------
# Per-user (app_settings lives in each user's meta DB): user_id -> {key: value}.
# A user absent from the map means "not loaded yet" (was the None sentinel).
_app_settings_cache: dict[str, dict[str, str]] = {}
_app_settings_cache_lock = threading.Lock()

# --- Feed last-post cache -----------------------------------------------------
# Newest-post-per-feed is derived from a full GROUP BY over the reader entries
# table, so cache it briefly (keyed per-user by reader DB path) to keep the
# Settings → Feeds "Stale" view from re-scanning on every home render.
_feed_last_post_cache: dict[str, tuple[float, dict[str, datetime]]] = {}
_feed_last_post_cache_lock = threading.Lock()
FEED_LAST_POST_TTL_SECONDS = 300

# --- Browser-UA / proxy / Tailscale / FlareSolverr feed-flag caches ---------
# Per-user cache of browser-UA-flagged feed URLs, consulted by reader's per-feed
# request hook on every fetch. Refreshed on a short TTL (the set changes only when
# a feed is flagged/unflagged) and invalidated immediately on those writes.
_browser_ua_cache: dict[str, set[str]] = {}
_browser_ua_cache_at: dict[str, float] = {}
_browser_ua_cache_lock = threading.Lock()
_BROWSER_UA_CACHE_TTL = 30.0

# Per-user cache of as-needed-proxy-flagged feed URLs — same shape as the
# browser-UA cache above, consulted by the same request-hook mechanism.
_proxy_feeds_cache: dict[str, set[str]] = {}
_proxy_feeds_cache_at: dict[str, float] = {}
_proxy_feeds_cache_lock = threading.Lock()
_PROXY_FEEDS_CACHE_TTL = 30.0

# Per-user cache of last-resort-flagged feed URLs — same shape as the proxy
# cache above, one rung further out.
_tailscale_feeds_cache: dict[str, set[str]] = {}
_tailscale_feeds_cache_at: dict[str, float] = {}
_tailscale_feeds_cache_lock = threading.Lock()
_TAILSCALE_FEEDS_CACHE_TTL = 30.0

# Per-user cache of FlareSolverr-flagged feed URLs — same shape as the two above.
_flaresolverr_feeds_cache: dict[str, set[str]] = {}
_flaresolverr_feeds_cache_at: dict[str, float] = {}
_flaresolverr_feeds_cache_lock = threading.Lock()
_FLARESOLVERR_FEEDS_CACHE_TTL = 30.0

# A dead proxy backend (e.g. gluetun restarting, or the Tailscale exit node
# blipping) must never be worse than not having one. On a proxy-unreachable
# failure (see services.feed_refresh.FeedRefreshService._is_proxy_unreachable),
# _mark_backend_unreachable skips WHICHEVER backend was actually in play for
# that fetch, for this user, for a cooldown — across every mode, not just
# as_needed — rather than hard-failing every fetch until someone notices.
# Tracked separately per backend: the two have very different reliability
# profiles (a home Tailscale exit blips far more than a dedicated VPN
# container), and marking the wrong one down would block a perfectly fine
# primary proxy over a last-resort hiccup, or vice versa.
_PROXY_DOWN_COOLDOWN_SECONDS = 300.0
_proxy_down_until: dict[str, float] = {}
_proxy_down_lock = threading.Lock()
_TAILSCALE_DOWN_COOLDOWN_SECONDS = 300.0
_tailscale_down_until: dict[str, float] = {}
_tailscale_down_lock = threading.Lock()

# --- Tag alias cache ---------------------------------------------------------
# Per-user alias map, loaded once and dropped whenever an alias changes.
# normalize_tag_value is on every tag path there is (51 call sites, several of
# them per-entry during a refresh), so a meta-DB read per call is not an option.
_tag_alias_cache: dict[str, dict[str, str]] = {}
_tag_alias_lock = threading.Lock()

# --- media:content scan in-progress tracking --------------------------------
# A scan that errored mid-way (e.g. discovered a host feed but couldn't fetch it)
# isn't proof there's no audio — retry well before the long "empty" backoff.
_MEDIA_SCAN_TTL_ERROR = 6 * 3600
_media_scan_in_progress: set[tuple[str, str]] = set()
_media_scan_lock = threading.Lock()

# --- Login rate limiting -----------------------------------------------------
_login_failures: dict[str, list[float]] = {}
_login_failures_lock = threading.Lock()

# --- /thumb fetch failure cache ----------------------------------------------
# Short-lived negative cache for /thumb fetches that failed (timeout, 5xx, blocked).
# A folder full of server-blocked images (e.g. Cloudflare-403 washingtonstatestandard)
# would otherwise re-hit every dead host on every page load, each tying up a worker
# thread. Keyed by the source image URL; brief TTL so transient failures recover.
_THUMB_FETCH_FAIL_CACHE: dict[str, float] = {}
_THUMB_FETCH_FAIL_LOCK = threading.Lock()
_THUMB_FETCH_FAIL_TTL = 10 * 60  # seconds

# --- Auto-refetch host cooldown ----------------------------------------------
# Hosts whose last automatic re-fetch failed, and when. Auto-refetch is a
# side effect of tagging, so a host that refuses us must not be re-asked on every
# tag: DeviantArt answers this server with 403 every time, and a tagging session
# across a watchlist would be dozens of requests it has already declined. Manual
# Re-fetch ignores this entirely — that is a person asking on purpose.
_AUTOFETCH_HOST_COOLDOWN_S = 6 * 3600
_autofetch_failed_hosts: dict[str, float] = {}
_autofetch_hosts_lock = threading.Lock()


# --- _PerUserDict caches -----------------------------------------------------
class _PerUserDict:
    """Dict-like cache partitioned by the current tenancy user, so per-user cached
    data (folder structure, unread counts, tags, settings) never bleeds across
    users. Implements the subset of dict operations the cache sites use; each op
    resolves the current user via tenancy.current_user_id()."""

    __slots__ = ("_by_user",)

    def __init__(self) -> None:
        self._by_user: dict[str, dict] = {}

    def _d(self) -> dict:
        return self._by_user.setdefault(tenancy.current_user_id(), {})

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def __delitem__(self, k):
        del self._d()[k]

    def __contains__(self, k):
        return k in self._d()

    def __bool__(self):
        return bool(self._d())

    def __len__(self):
        return len(self._d())

    def __iter__(self):
        return iter(self._d())

    def get(self, k, default=None):
        return self._d().get(k, default)

    def pop(self, k, *a):
        return self._d().pop(k, *a)

    def setdefault(self, k, default=None):
        return self._d().setdefault(k, default)

    def update(self, *a, **kw):
        self._d().update(*a, **kw)

    def clear(self):
        self._d().clear()

    def items(self):
        return self._d().items()

    def keys(self):
        return self._d().keys()

    def values(self):
        return self._d().values()


# Short in-memory TTL cache for tag counts to avoid repeatedly scanning
# reader entries on every request. Small TTL keeps counts fresh while
# preventing repeated expensive work during rapid navigation.
TAG_COUNTS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_TAG_COUNTS_CACHE_TTL", "300"))
tag_counts_cache_lock = threading.Lock()
tag_counts_cache = _PerUserDict()

# Short in-memory TTL cache for unread counts so the UI doesn't scan the
# entire reader DB on every load. TTL is small to stay responsive to new
# incoming posts.
UNREAD_COUNTS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_UNREAD_COUNTS_CACHE_TTL", "300"))
unread_counts_cache_lock = threading.Lock()
unread_counts_cache = _PerUserDict()

# Feed-title map: hits the reader DB to enumerate every feed. Cache it — feed
# titles barely change between page renders.
FEED_TITLE_MAP_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_FEED_TITLE_MAP_CACHE_TTL", "300"))
feed_title_map_cache_lock = threading.Lock()
feed_title_map_cache = _PerUserDict()

# Cache the meta-DB structure snapshot. Folders / folder_feeds change only on
# explicit user actions (subscribe, unsubscribe, add/delete folder, move feed),
# so we cache the read-side queries indefinitely and invalidate on mutation.
# This collapses ~5 SQL roundtrips per home render to one dict lookup.
_meta_structure_lock = threading.Lock()
_meta_structure_cache = _PerUserDict()

# Cache for problematic-feeds list. Only changes when a refresh succeeds/fails,
# so a TTL is fine — we don't need exact freshness on the home page.
PROBLEMATIC_FEEDS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_PROBLEMATIC_FEEDS_CACHE_TTL", "60"))
# How many failing feeds the list renders. Raised from 50 so the category filter
# can triage the whole failing set (each row is small; the panel is in the
# hidden Feeds tab). Tunable if the row weight ever matters.
PROBLEMATIC_FEEDS_LIMIT = int(os.getenv("LECTIO_FAILING_FEEDS_LIMIT", "500"))
_problematic_feeds_cache_lock = threading.Lock()
_problematic_feeds_cache = _PerUserDict()

_has_manual_tags_cache = _PerUserDict()
_has_manual_tags_lock = threading.Lock()
HAS_MANUAL_TAGS_CACHE_TTL_SECONDS = int(os.getenv("LECTIO_HAS_MANUAL_TAGS_CACHE_TTL", "60"))

_ad_asset_hashes_cache = _PerUserDict()
_ad_asset_hashes_lock = threading.Lock()

# Per-(feed_url, entry_id) autofetch job tracking. Unlike _refetch_jobs' single
# running slot (one bulk run at a time), many of these can be in flight at once
# -- one per recently-kept stub -- and each open pane only cares about its own
# entry's status. The pane polls /entries/autofetch-status to notice once the
# background re-fetch below lands, since the pane already rendered (showing the
# stub) before that thread even started.
_autofetch_jobs = _PerUserDict()
_autofetch_jobs_lock = threading.Lock()
_AUTOFETCH_JOB_STALE_S = 600  # finished job records this old are dropped lazily

# One batch re-fetch at a time, per user. A bulk network job that can be started
# twice is a politeness bug: two runs interleave and each host sees double the rate
# the pacing promises.
_refetch_jobs = _PerUserDict()
_refetch_jobs_lock = threading.Lock()


# --- Unread-counts generation counter + stale-while-revalidate inflight flag -
# Incremented on every invalidation so in-flight background refreshes that
# started before the invalidation don't write stale counts back to the cache.
_unread_counts_generation: int = 0

# Stale-while-revalidate: when the cache is stale we serve the prior value and
# spawn ONE background refresh. Concurrent renders never wait on the scan.
unread_counts_compute_lock = threading.Lock()
unread_counts_refresh_inflight = False


def invalidate_unread_counts_cache() -> None:
    """Bump the generation + clear the cache so folder/feed unread badges recompute."""
    global _unread_counts_generation
    with unread_counts_cache_lock:
        _unread_counts_generation += 1
        unread_counts_cache.clear()


def _bump_unread_counts_generation() -> None:
    """Bump the generation without clearing the cache (stale-while-revalidate reads still work).

    Callers that mark entries read as a side effect of automation (dedup, mark_as_read
    rules, hide-shorts, etc.) use this instead of ``invalidate_unread_counts_cache`` --
    deliberately not clearing, so a badge still renders from cache while it recomputes.
    A plain ``global _unread_counts_generation; _unread_counts_generation += 1`` in a
    function that later moves out of main.py would rebind a *second* copy of the
    counter in the new module -- silently, with no import error -- so every automation
    call site goes through this function instead of touching the global directly.
    """
    global _unread_counts_generation
    _unread_counts_generation += 1


def get_unread_counts_generation() -> int:
    """Current generation. Callers compare a value captured earlier against this
    live read (never cache/import the bare counter -- it's rebound via `global`
    in this module, so a plain `from state import _unread_counts_generation`
    elsewhere would freeze at whatever it was at import time)."""
    return _unread_counts_generation


def try_start_unread_refresh() -> bool:
    """Atomically claim the single stale-while-revalidate background-refresh slot.

    Returns True if the caller just claimed it (and should spawn the refresh thread),
    False if a refresh is already in flight.
    """
    global unread_counts_refresh_inflight
    with unread_counts_compute_lock:
        if unread_counts_refresh_inflight:
            return False
        unread_counts_refresh_inflight = True
        return True


def clear_unread_refresh_inflight() -> None:
    """Release the in-flight slot claimed by try_start_unread_refresh()."""
    global unread_counts_refresh_inflight
    with unread_counts_compute_lock:
        unread_counts_refresh_inflight = False


# --- Scheduled-refresh fairness rotation ------------------------------------
_scheduled_refresh_rotation = 0


def next_refresh_rotation_offset(count: int) -> int:
    """Return this pass's rotation offset into a `count`-user list, and advance
    the counter for next time (round-robin, wrapping on `count`)."""
    global _scheduled_refresh_rotation
    offset = _scheduled_refresh_rotation % count
    _scheduled_refresh_rotation = (_scheduled_refresh_rotation + 1) % count
    return offset


# --- Manual-refresh cooldown -------------------------------------------------
manual_refresh_lock = threading.Lock()
last_manual_refresh_started_at = 0.0


def check_manual_refresh_cooldown(cooldown_seconds: float) -> int:
    """Enforce a cooldown between manual refreshes, atomically checking and
    (if past cooldown) recording now as the new last-started time. Returns 0 if
    this call may proceed, else the seconds remaining until the cooldown clears."""
    global last_manual_refresh_started_at
    with manual_refresh_lock:
        now = time.monotonic()
        elapsed = now - last_manual_refresh_started_at
        if elapsed < cooldown_seconds:
            return int(cooldown_seconds - elapsed)
        last_manual_refresh_started_at = now
        return 0
