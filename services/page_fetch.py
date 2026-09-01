"""Shared proxy/FlareSolverr escalation ladder for single-URL page fetches.

Unlike the feed-refresh escalation ladder (services/feed_refresh.py +
services/reader_api.py), which is a flag-and-retry loop over an unattended
hourly cron with per-feed-url state persisted to survive restarts, this is a
synchronous, demand-driven, single-URL fetcher: the ladder runs start to
finish inside one call. It backs the tag/lead-image page fetch
(services/lead_images.py) and the saved-article re-fetch path (main.py's
fetch_readability_article/fetch_full_page_article). See Plan.md and
docs/architecture/feeds.md for the design rationale, including why this does
NOT reuse the feed-refresh ladder, does NOT add a Tailscale tier, and keeps
its escalation memory in-memory rather than in a new DB table.

Tiers, in order: honest identity -> browser identity -> outbound proxy ->
FlareSolverr (a real headless-Chrome challenge solve). Each deeper tier is
only attempted if the shallower ones were refused (or errored) AND the
backend is actually configured for the current user — an empty proxy_url or
flaresolverr_url in FetchBackends means "not available", not "try it anyway."
The FlareSolverr tier additionally requires bot_challenge to have recognized
an actual challenge marker (or a host this ladder has solved for before) —
spending a real browser solve on a plain rate-limit would waste the one
shared FlareSolverr container for no benefit.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx

from services import bot_challenge, flaresolverr, tenancy, url_guard

FetchTier = Literal["honest", "browser", "proxy", "flaresolverr"]

_TIER_RANK: dict[FetchTier, int] = {"honest": 0, "browser": 1, "proxy": 2, "flaresolverr": 3}
_TIER_ORDER: tuple[FetchTier, ...] = ("honest", "browser", "proxy", "flaresolverr")

# Same refusal set services/feed_discovery.py uses for its own honest->browser
# escalation — a 415/429 is as much a "try again differently" signal as a 403.
DEFAULT_REFUSAL_STATUSES = frozenset({403, 415, 429, 503})

# Browser identity used ONLY after an honest fetch is refused, never
# preemptively. Page-shaped Accept (vs the feed-shaped one in reader_api.py) —
# some WAFs sniff Sec-Fetch-*/Accept-Language, not just the UA.
_DEFAULT_BROWSER_HEADERS: Mapping[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

_JS_COOKIE_CHALLENGE_RE = re.compile(r'document\.cookie\s*=\s*["\']([^"\'=]+=[^"\']+)["\']', re.IGNORECASE)


@dataclass(frozen=True)
class FetchBackends:
    """Outbound backends available to the CURRENT user right now. An empty
    string means "not available" — already resolved from settings AND any
    backend-down cooldown by the caller (main.py's _page_fetch_backends);
    PageFetcher itself never looks at settings or backend health, only at
    whether these strings are non-empty."""

    mode: str
    proxy_url: str
    flaresolverr_url: str


@dataclass(frozen=True)
class PageFetchResult:
    html: str
    final_url: str
    status: int
    tier: FetchTier
    headers: Mapping[str, str]
    challenge: str | None
    response: httpx.Response | None


class PageFetchError(Exception):
    """Every tier refused, errored, or was unavailable. Carries status_code
    and response (a real or synthesized httpx.Response) so existing
    duck-typed classification of a failed fetch (e.g.
    services/saved_articles.py's dead-vs-blocked check) keeps working."""

    def __init__(
        self,
        url: str,
        *,
        status_code: int | None,
        tier: FetchTier,
        challenge: str | None,
        response: httpx.Response | None,
        message: str | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.tier = tier
        self.challenge = challenge
        self.response = response
        super().__init__(message or f"page fetch failed for {url!r} (deepest tier tried: {tier}, status: {status_code})")


@dataclass(frozen=True)
class _Attempt:
    tier: FetchTier
    status: int | None
    html: str | None
    final_url: str
    headers: Mapping[str, str]
    response: httpx.Response | None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300


def _pick_better(old: _Attempt | None, new: _Attempt) -> _Attempt:
    """Best-response-wins: the deepest tier that actually produced a status
    is what's worth reporting — "even FlareSolverr got 403" is more
    diagnostic than "the honest fetch got 403", even when the number is the
    same, because it means no cheaper tier would ever have helped either. A
    bare transport error (no status at all) never overwrites a real one —
    that tier just failed to add information."""
    if old is None:
        return new
    if new.status is None:
        return old
    return new


@dataclass
class _HostState:
    learned_tier: FetchTier | None = None
    blocked_until: float = 0.0
    blocked_after_tier: FetchTier = "honest"
    challenge: str | None = None


class HostEscalationState:
    """Per-(user, host) memory of what it takes to fetch a page from this
    host: the cheapest tier known to work, and — if every available tier was
    just exhausted — a cooldown before trying this host again.

    Deliberately in-memory, not a new meta-DB table (see Plan.md): these are
    demand-driven single-URL fetches, not an unattended cron with 24h
    backoff, so losing this memory on a restart costs at most one extra
    honest+browser round trip on the next request to that host. Bounded LRU
    so an unbounded stream of distinct hosts can't grow this without limit.

    The cooldown is tier-aware, not a flat timer: a host given up on when
    only the browser tier was reachable gets an immediate fresh attempt the
    moment a proxy or FlareSolverr URL becomes configured, with no cache
    invalidation needed — see is_blocked's rank comparison.
    """

    def __init__(self, *, cooldown_seconds: float = 6 * 3600, max_hosts: int = 2048) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._max_hosts = max_hosts
        self._lock = threading.Lock()
        self._by_key: OrderedDict[tuple[str, str], _HostState] = OrderedDict()

    def _get(self, uid: str, host: str) -> _HostState | None:
        with self._lock:
            return self._by_key.get((uid, host))

    def learned_tier(self, uid: str, host: str) -> FetchTier | None:
        st = self._get(uid, host)
        return st.learned_tier if st else None

    def challenge_for(self, uid: str, host: str) -> str | None:
        st = self._get(uid, host)
        return st.challenge if st else None

    def is_blocked(self, uid: str, host: str, *, best_available: FetchTier, now: float) -> bool:
        st = self._get(uid, host)
        if st is None or st.blocked_until <= now:
            return False
        # A deeper tier than what was available when we gave up is now
        # configured — worth a fresh attempt rather than honoring the cooldown.
        return _TIER_RANK[best_available] <= _TIER_RANK[st.blocked_after_tier]

    def record_success(self, uid: str, host: str, tier: FetchTier) -> None:
        with self._lock:
            key = (uid, host)
            st = self._by_key.get(key) or _HostState()
            st.learned_tier = tier
            st.blocked_until = 0.0
            self._by_key[key] = st
            self._by_key.move_to_end(key)
            self._evict_locked()

    def record_block(self, uid: str, host: str, *, deepest_available: FetchTier, challenge: str | None, now: float) -> None:
        with self._lock:
            key = (uid, host)
            st = self._by_key.get(key) or _HostState()
            st.blocked_until = now + self._cooldown_seconds
            st.blocked_after_tier = deepest_available
            st.challenge = challenge
            self._by_key[key] = st
            self._by_key.move_to_end(key)
            self._evict_locked()

    def snapshot(self) -> list[dict]:
        """For a settings-panel view of what this ladder has learned — see
        the 'Page fetches' section of the Fetch Tiers panel."""
        with self._lock:
            now = time.monotonic()
            return [
                {
                    "user_id": uid,
                    "host": host,
                    "learned_tier": st.learned_tier,
                    "blocked": st.blocked_until > now,
                    "blocked_after_tier": st.blocked_after_tier,
                    "challenge": st.challenge,
                }
                for (uid, host), st in self._by_key.items()
            ]

    def clear(self) -> None:
        """Drop all learned/blocked state. For test isolation — a shared
        PageFetcher (like main.py's process-global one) must not let one
        test's cooldown or learned tier leak into another reusing the same
        host name."""
        with self._lock:
            self._by_key.clear()

    def _evict_locked(self) -> None:
        while len(self._by_key) > self._max_hosts:
            self._by_key.popitem(last=False)


def _deepest_available_tier(backends: FetchBackends, max_tier: FetchTier) -> FetchTier:
    candidates: list[FetchTier] = ["honest", "browser"]
    if backends.proxy_url:
        candidates.append("proxy")
    if backends.flaresolverr_url:
        candidates.append("flaresolverr")
    capped = [t for t in candidates if _TIER_RANK[t] <= _TIER_RANK[max_tier]]
    return max(capped, key=lambda t: _TIER_RANK[t])


def _tier_order(backends: FetchBackends, max_tier: FetchTier) -> list[FetchTier]:
    order: list[FetchTier] = []
    for tier in _TIER_ORDER:
        if _TIER_RANK[tier] > _TIER_RANK[max_tier]:
            break
        if tier == "proxy" and not backends.proxy_url:
            continue
        if tier == "flaresolverr" and not backends.flaresolverr_url:
            continue
        order.append(tier)
    return order


class PageFetcher:
    """Fetches a single URL's HTML, escalating through honest -> browser ->
    proxy -> FlareSolverr identity as needed. See the module docstring for
    the design and Plan.md for the decisions behind it."""

    def __init__(
        self,
        *,
        backends: Callable[[], FetchBackends],
        honest_user_agent: str,
        user_id: Callable[[], str] = tenancy.current_user_id,
        browser_headers: Mapping[str, str] | None = None,
        state: HostEscalationState | None = None,
        clock: Callable[[], float] = time.monotonic,
        flaresolverr_max_timeout_ms: int = 55_000,
    ) -> None:
        self._backends = backends
        self._user_id = user_id
        self._honest_user_agent = honest_user_agent
        self._browser_headers = dict(browser_headers) if browser_headers is not None else dict(_DEFAULT_BROWSER_HEADERS)
        self._state = state if state is not None else HostEscalationState()
        self._clock = clock
        self._flaresolverr_max_timeout_ms = flaresolverr_max_timeout_ms
        # One shared FlareSolverr container, one Chrome instance behind it —
        # bounded to one in-flight solve at a time. On contention, skip the
        # tier rather than queue: page fetches are demand-driven, and a caller
        # waiting behind someone else's 55s solve is worse than falling back.
        self._flaresolverr_semaphore = threading.Semaphore(1)

    def fetch(
        self,
        url: str,
        *,
        timeout: float = 15.0,
        refusal_statuses: frozenset[int] = DEFAULT_REFUSAL_STATUSES,
        max_tier: FetchTier = "flaresolverr",
        ignore_cooldown: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> PageFetchResult:
        if not url_guard.is_safe_outbound_url(url):
            raise url_guard.UnsafeURLError(url)

        uid = self._user_id()
        host = urlparse(url).netloc.lower()
        backends = self._backends()
        deepest_available = _deepest_available_tier(backends, max_tier)
        now = self._clock()

        if not ignore_cooldown and self._state.is_blocked(uid, host, best_available=deepest_available, now=now):
            raise PageFetchError(
                url,
                status_code=None,
                tier=deepest_available,
                challenge=self._state.challenge_for(uid, host),
                response=None,
                message=f"page fetch skipped for {url!r} — {host} is in cooldown",
            )

        tiers: list[FetchTier] = _tier_order(backends, max_tier)
        learned = None if ignore_cooldown else self._state.learned_tier(uid, host)
        if learned == "flaresolverr" and "flaresolverr" in tiers:
            tiers = [t for t in tiers if t == "flaresolverr"] + [t for t in tiers if t != "flaresolverr"]

        honest_headers = {"User-Agent": self._honest_user_agent, **(headers or {})}
        browser_headers = {**self._browser_headers, **(headers or {})}

        challenge_seen: str | None = None
        best: _Attempt | None = None

        for tier in tiers:
            if tier == "flaresolverr" and not (challenge_seen or learned == "flaresolverr"):
                continue  # no evidence a real browser solve would help here
            attempt = self._attempt(
                tier,
                url,
                honest_headers=honest_headers,
                browser_headers=browser_headers,
                proxy_url=backends.proxy_url,
                flaresolverr_url=backends.flaresolverr_url,
                timeout=timeout,
            )
            best = _pick_better(best, attempt)
            if attempt.status is not None and attempt.headers:
                c = bot_challenge.detect_challenge_headers(attempt.headers)
                if not c and attempt.html:
                    c = bot_challenge.detect_challenge(
                        attempt.headers.get("content-type") or attempt.headers.get("Content-Type"),
                        attempt.html.encode("utf-8", errors="replace"),
                    )
                if c:
                    challenge_seen = c
            if attempt.status is not None and attempt.status not in refusal_statuses:
                # Stopped escalating (matches the pre-existing readability/
                # lead-image behavior this generalizes): any non-refusal
                # status, success or not, is the answer to report.
                if attempt.ok:
                    self._state.record_success(uid, host, tier)
                    return PageFetchResult(
                        html=attempt.html or "",
                        final_url=attempt.final_url,
                        status=attempt.status,
                        tier=tier,
                        headers=attempt.headers,
                        challenge=challenge_seen,
                        response=attempt.response,
                    )
                break

        self._state.record_block(uid, host, deepest_available=deepest_available, challenge=challenge_seen, now=now)
        raise PageFetchError(
            url,
            status_code=best.status if best else None,
            tier=best.tier if best else deepest_available,
            challenge=challenge_seen,
            response=best.response if best else None,
        )

    def _attempt(
        self,
        tier: FetchTier,
        url: str,
        *,
        honest_headers: Mapping[str, str],
        browser_headers: Mapping[str, str],
        proxy_url: str,
        flaresolverr_url: str,
        timeout: float,
    ) -> _Attempt:
        if tier == "flaresolverr":
            return self._attempt_flaresolverr(url, flaresolverr_url=flaresolverr_url, proxy_url=proxy_url)
        headers = honest_headers if tier == "honest" else browser_headers
        proxy = proxy_url if tier == "proxy" else None
        return self._attempt_httpx(tier, url, headers=headers, proxy=proxy, timeout=timeout)

    def _attempt_httpx(self, tier: FetchTier, url: str, *, headers: Mapping[str, str], proxy: str | None, timeout: float) -> _Attempt:
        client_kwargs: dict[str, object] = {"timeout": timeout, "headers": dict(headers)}
        if proxy:
            client_kwargs["proxy"] = proxy
        try:
            with url_guard.build_client(**client_kwargs) as client:
                response = url_guard.safe_get(client, url)
                if response.status_code == 409:
                    retried = self._retry_js_cookie_challenge(client, url, response)
                    if retried is not None:
                        response = retried
            return _attempt_from_response(tier, response)
        except httpx.RemoteProtocolError as exc:
            if tier in ("honest", "browser"):
                fallback = self._urllib_fallback(url, headers.get("User-Agent", self._honest_user_agent))
                if fallback is not None:
                    html_text, final_url, resp_headers = fallback
                    return _Attempt(
                        tier=tier, status=200, html=html_text, final_url=final_url,
                        headers=resp_headers, response=httpx.Response(status_code=200), error=None,
                    )
            return _Attempt(tier=tier, status=None, html=None, final_url=url, headers={}, response=None, error=exc)
        except httpx.HTTPError as exc:
            return _Attempt(tier=tier, status=None, html=None, final_url=url, headers={}, response=None, error=exc)

    def _retry_js_cookie_challenge(self, client: httpx.Client, url: str, response: httpx.Response) -> httpx.Response | None:
        """A 409 whose body sets a cookie via JS (e.g. BlueHost humans_XXXXX) is
        solvable without a full browser — set the cookie ourselves and retry
        once. Returns None (leave the 409 standing) if the body doesn't match."""
        m = _JS_COOKIE_CHALLENGE_RE.search(response.text)
        if not m:
            return None
        cookie_str = m.group(1)
        if "=" not in cookie_str:
            return None
        cname, cval = cookie_str.split("=", 1)
        domain = urlparse(url).netloc.removeprefix("www.")
        client.cookies.set(cname.strip(), cval.strip(), domain=domain)
        return url_guard.safe_get(client, url)

    def _urllib_fallback(self, url: str, user_agent: str) -> tuple[str, str, dict] | None:
        """TLS-fingerprint workaround for hosts (e.g. Tumblr) that drop httpx's
        connection outright but accept stdlib urllib's. No proxied counterpart —
        only tried at the honest/browser rungs, same as the pre-existing
        lead_images behavior this generalizes."""
        if not url_guard.is_safe_outbound_url(url):
            return None
        try:
            import urllib.error as _uerr
            import urllib.request as _ureq

            class _SafeRedirectHandler(_ureq.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    if not url_guard.is_safe_outbound_url(newurl):
                        raise _uerr.HTTPError(newurl, code, "Blocked unsafe redirect target", headers, fp)
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            opener = _ureq.build_opener(_SafeRedirectHandler)
            req = _ureq.Request(url, headers={"User-Agent": user_agent})
            with opener.open(req, timeout=10) as resp:
                html_text = resp.read().decode("utf-8", errors="replace")
                final_url = resp.url
                resp_headers = dict(resp.headers.items())
            return html_text, final_url, resp_headers
        except Exception:
            return None

    def _attempt_flaresolverr(self, url: str, *, flaresolverr_url: str, proxy_url: str) -> _Attempt:
        acquire_timeout = self._flaresolverr_max_timeout_ms / 1000.0
        if not self._flaresolverr_semaphore.acquire(timeout=acquire_timeout):
            return _Attempt(
                tier="flaresolverr", status=None, html=None, final_url=url, headers={}, response=None,
                error=TimeoutError("flaresolverr: busy, timed out waiting for the shared solve slot"),
            )
        try:
            stack_proxy = flaresolverr.normalize_proxy_scheme(proxy_url) if proxy_url else None
            solution = flaresolverr.solve(
                flaresolverr_url,
                url,
                proxy_url=stack_proxy,
                timeout=acquire_timeout + 5.0,
                max_timeout_ms=self._flaresolverr_max_timeout_ms,
            )
        except (bot_challenge.FeedBlockedError, httpx.HTTPError) as exc:
            return _Attempt(tier="flaresolverr", status=None, html=None, final_url=url, headers={}, response=None, error=exc)
        finally:
            self._flaresolverr_semaphore.release()
        status = solution.status if solution.status is not None else 502
        return _Attempt(
            tier="flaresolverr", status=status, html=solution.html, final_url=solution.url,
            headers={}, response=httpx.Response(status_code=status), error=None,
        )


def _attempt_from_response(tier: FetchTier, response: httpx.Response) -> _Attempt:
    try:
        html_text = response.text
    except Exception:
        html_text = ""
    return _Attempt(
        tier=tier, status=response.status_code, html=html_text, final_url=str(response.url),
        headers=response.headers, response=response, error=None,
    )
