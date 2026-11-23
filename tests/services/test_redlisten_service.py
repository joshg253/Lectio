"""Tests for the Redlisten Reddit-feed client."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from reader.exceptions import EntryExistsError

from services import redlisten as svc

NOW = 1_700_000_000.0


def _meta():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _reader(*urls):
    reader = MagicMock()
    reader.get_feeds.return_value = [MagicMock(url=u) for u in urls]
    return reader


def _post(sub, pid):
    return {"subreddit": sub, "permalink": f"/r/{sub}/comments/{pid}/t/", "title": pid, "created_utc": 1700000000, "is_self": True}


def _listing(*posts):
    return {"data": {"children": [{"kind": "t3", "data": p} for p in posts]}}


def _resp(status=200, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    r.text = ""
    return r


def _client(*responses):
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.side_effect = list(responses)
    return client


def test_batches_subreddits_into_one_request_and_routes_posts():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss", "https://old.reddit.com/r/rust/.rss")
    client = _client(_resp(body=_listing(_post("Python", "a"), _post("rust", "b"), _post("other", "c"))))
    with patch.object(svc.httpx, "Client", return_value=client):
        added = svc.refresh_reddit_feeds(conn, reader, "rl_live_x", now=10_000.0)
    assert added == 2
    assert client.get.call_count == 1
    assert client.get.call_args.args[0].endswith("/r/python+rust/new")
    routed = {c.args[0]["feed_url"]: c.args[0]["title"] for c in reader.add_entry.call_args_list}
    assert routed == {"https://www.reddit.com/r/Python/.rss": "a", "https://old.reddit.com/r/rust/.rss": "b"}


def test_sweep_is_spaced_by_min_interval():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss")
    client = _client(_resp(body=_listing()), _resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=10_000.0)
        svc.refresh_reddit_feeds(conn, reader, "k", now=10_000.0 + 60)
        assert client.get.call_count == 1
        svc.refresh_reddit_feeds(conn, reader, "k", now=10_000.0 + svc.MIN_SWEEP_INTERVAL_SECONDS + 1)
    assert client.get.call_count == 2


def test_quota_429_blocks_further_sweeps():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss")
    client = _client(_resp(429, {"error": {"status": 429, "code": "quota_exceeded"}}, {"Retry-After": "5"}))
    with patch.object(svc.httpx, "Client", return_value=client):
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=10_000.0) == 0
        later = 10_000.0 + svc.MIN_SWEEP_INTERVAL_SECONDS + 1
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=later) == 0
    assert client.get.call_count == 1  # still blocked: monthly quota backs off hours, not seconds


def test_existing_entries_are_not_counted():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss")
    reader.add_entry.side_effect = EntryExistsError("f", "i")
    client = _client(_resp(body=_listing(_post("Python", "a"))))
    with patch.object(svc.httpx, "Client", return_value=client):
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=10_000.0) == 0


def test_user_feed_uses_submitted_endpoint():
    conn = _meta()
    reader = _reader("https://www.reddit.com/user/spez/submitted/.rss")
    client = _client(_resp(body=_listing(_post("announcements", "a"))))
    with patch.object(svc.httpx, "Client", return_value=client):
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=NOW) == 1
    assert client.get.call_args.args[0].endswith("/user/spez/submitted")


def test_bad_key_raises():
    client = _client(_resp(401))
    with patch.object(svc.httpx, "Client", return_value=client), pytest.raises(svc.RedlistenError):
        svc.check_key(_meta(), "nope")


def test_sends_bearer_header():
    assert svc._headers("rl_live_abc")["Authorization"] == "Bearer rl_live_abc"


def test_key_resolution_user_then_shared_then_env(monkeypatch):
    import main

    store = {}
    monkeypatch.setattr(main, "get_runtime_setting", lambda k, env="": store.get(k, env))
    monkeypatch.setattr(main, "_get_shared_credential", lambda k: store.get("shared:" + k, ""))
    monkeypatch.setattr(main, "_ENV_REDLISTEN_API_KEY", "env-key")
    assert main.get_redlisten_api_key() == "env-key"
    store["shared:" + main.SETTING_SHARED_REDLISTEN_API_KEY] = "shared-key"
    assert main.get_redlisten_api_key() == "shared-key"
    store[main.SETTING_REDLISTEN_API_KEY] = "user-key"
    assert main.get_redlisten_api_key() == "user-key"


def test_user_feeds_are_checked_daily_not_every_sweep():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss", "https://www.reddit.com/user/spez/submitted/.rss")
    client = _client(*[_resp(body=_listing()) for _ in range(6)])
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW)
        assert client.get.call_count == 2  # subreddit batch + user feed
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + svc.MIN_SWEEP_INTERVAL_SECONDS + 1)
        assert client.get.call_count == 3  # subreddit batch only
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + svc.USER_SWEEP_INTERVAL_SECONDS + 1)
    assert client.get.call_count == 5


def test_requests_ask_for_plan_page_size():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss")
    client = _client(_resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=10_000.0)
    assert client.get.call_args.kwargs["params"]["limit"] == svc.PAGE_LIMIT == 50


def test_quota_warning_at_80_percent(caplog):
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss")
    client = _client(_resp(body=_listing(), headers={"x-quota-used": "850", "x-quota-limit": "1000"}))
    with patch.object(svc.httpx, "Client", return_value=client), caplog.at_level("WARNING"):
        svc.refresh_reddit_feeds(conn, reader, "k", now=10_000.0)
    assert "850 of 1000 monthly requests" in caplog.text


def test_feeds_come_from_enabled_reader_subscriptions_only():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss", "https://example.com/feed.xml")
    client = _client(_resp(body=_listing(_post("Python", "a"))))
    with patch.object(svc.httpx, "Client", return_value=client):
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=NOW) == 1
    reader.get_feeds.assert_called_once_with(updates_enabled=True)
    assert client.get.call_args.args[0].endswith("/r/python/new")


def test_forced_sync_is_limited_to_given_feeds_and_cooled_down():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss", "https://www.reddit.com/r/rust/.rss")
    client = _client(_resp(body=_listing()), _resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW, only=["https://www.reddit.com/r/rust/.rss"], force=True)
        assert client.get.call_args.args[0].endswith("/r/rust/new")
        # inside the cooldown: nothing
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + 5, only=["https://www.reddit.com/r/rust/.rss"], force=True)
        assert client.get.call_count == 1
        # a forced sync does not consume the hourly slot
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + 10)
    assert client.get.call_count == 2


def test_multi_subreddit_feed_gets_posts_from_each_and_unsupported_shapes_are_skipped():
    conn = _meta()
    multi = "https://www.reddit.com/r/Python+rust/.rss"
    reader = _reader(multi, "https://www.reddit.com/r/foo/search.rss?q=x", "https://www.reddit.com/user/spez/.rss")
    client = _client(_resp(body=_listing(_post("Python", "a"), _post("rust", "b"), _post("foo", "c"))))
    with patch.object(svc.httpx, "Client", return_value=client):
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=NOW) == 2
    assert client.get.call_count == 1
    assert client.get.call_args.args[0].endswith("/r/python+rust/new")


def test_network_error_is_recorded_not_raised():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss")
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.side_effect = svc.httpx.ConnectError("boom")
    with patch.object(svc.httpx, "Client", return_value=client):
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=NOW) == 0
    assert "network error" in svc.last_error(conn)


def test_last_error_clears_after_a_clean_sweep():
    conn = _meta()
    reader = _reader("https://www.reddit.com/r/Python/.rss")
    client = _client(_resp(401), _resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW)
        assert "invalid" in svc.last_error(conn)
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + svc.MIN_SWEEP_INTERVAL_SECONDS + 1)
    assert svc.last_error(conn) == ""


def test_non_quota_429_backs_off_for_retry_after_only():
    conn = _meta()
    url = "https://www.reddit.com/r/Python/.rss"
    reader = _reader(url)
    client = _client(_resp(429, {"error": {"status": 429}}, {"Retry-After": "120"}), _resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW)
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + 60, only=[url], force=True)  # inside Retry-After: nothing
        assert client.get.call_count == 1
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + 130, only=[url], force=True)  # a quota 429 would still block
    assert client.get.call_count == 2


def test_manual_cooldown_is_per_feed():
    conn = _meta()
    a, b = "https://www.reddit.com/r/Python/.rss", "https://www.reddit.com/r/rust/.rss"
    reader = _reader(a, b)
    client = _client(_resp(body=_listing()), _resp(body=_listing()), _resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW, only=[a], force=True)
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + 5, only=[b], force=True)  # a different feed: not blocked
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + 10, only=[a], force=True)  # same feed inside cooldown: skipped
    assert client.get.call_count == 2


def test_deleted_entries_are_not_re_added():
    conn = _meta()
    conn.execute("CREATE TABLE deleted_entries (feed_url TEXT, entry_id TEXT)")
    url = "https://www.reddit.com/r/Python/.rss"
    conn.execute("INSERT INTO deleted_entries VALUES (?, ?)", (url, "https://www.reddit.com/r/Python/comments/a/t/"))
    reader = _reader(url)
    client = _client(_resp(body=_listing(_post("Python", "a"), _post("Python", "b"))))
    with patch.object(svc.httpx, "Client", return_value=client):
        assert svc.refresh_reddit_feeds(conn, reader, "k", now=NOW) == 1
    assert [c.args[0]["title"] for c in reader.add_entry.call_args_list] == ["b"]


def test_new_entries_hook_runs_for_feeds_that_gained_entries_only():
    conn = _meta()
    a, b = "https://www.reddit.com/r/Python/.rss", "https://www.reddit.com/r/rust/.rss"
    reader = _reader(a, b)
    client = _client(_resp(body=_listing(_post("Python", "a"))))
    seen: list[str] = []
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW, on_new_entries=seen.append)
    assert seen == [a]


def test_a_new_key_does_not_inherit_the_old_keys_backoff():
    conn = _meta()
    url = "https://www.reddit.com/r/Python/.rss"
    reader = _reader(url)
    client = _client(_resp(429, {"error": {"code": "quota_exceeded"}}), _resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "old-key", now=NOW)
        assert svc.refresh_reddit_feeds(conn, reader, "old-key", now=NOW + 7200) == 0  # still paused for the quota
        assert client.get.call_count == 1
        svc.refresh_reddit_feeds(conn, reader, "new-key", now=NOW + 7200)
    assert client.get.call_count == 2


def test_a_failed_profile_check_is_retried_next_sweep_not_tomorrow():
    conn = _meta()
    url = "https://www.reddit.com/user/spez/submitted/.rss"
    reader = _reader(url)
    client = _client(_resp(500), _resp(body=_listing()))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW)
        svc.refresh_reddit_feeds(conn, reader, "k", now=NOW + svc.MIN_SWEEP_INTERVAL_SECONDS + 1)
    assert client.get.call_count == 2


_QUOTA_HEADERS = {"x-quota-used": "120", "x-quota-limit": "1000", "x-items-used": "900", "x-items-limit": "50000"}


def test_sweep_records_usage_from_headers_and_stats_read_it():
    conn = _meta()
    urls = ["https://www.reddit.com/r/Python/.rss"]
    client = _client(_resp(body=_listing(_post("Python", "a")), headers=_QUOTA_HEADERS))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, _reader(*urls), "k", now=NOW)
    stats = svc.get_stats(conn, urls, NOW + 60)
    assert (stats["requests_used"], stats["requests_limit"], stats["items_used"]) == (120, 1000, 900)
    assert client.get.call_count == 1  # reading stats spends nothing


def test_usage_from_a_previous_month_is_not_shown_as_current():
    conn = _meta()
    client = _client(_resp(body=_listing(), headers=_QUOTA_HEADERS))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.refresh_reddit_feeds(conn, _reader("https://www.reddit.com/r/Python/.rss"), "k", now=NOW)
    stats = svc.get_stats(conn, [], NOW + 40 * 86400)
    assert stats["requests_used"] is None
    assert stats["requests_limit"] == 1000  # the plan limit carries over


def test_unlimited_enterprise_headers_are_ignored():
    assert svc._usage_from_headers({"x-quota-limit": "unlimited", "x-quota-used": "5"}) == {"requests_used": 5}


def test_check_key_records_plan_and_usage():
    conn = _meta()
    body = {
        "plan": "free",
        "limits": {"monthly_quota": 1000, "feed_items_quota": 50000, "rpm_limit": 30, "page_size": 50},
        "usage": {"requests": 42, "items": 700, "month": "2023-11"},
    }
    with patch.object(svc.httpx, "Client", return_value=_client(_resp(body=body))):
        stats = svc.check_key(conn, "k", now=NOW)
    assert (stats["plan"], stats["requests_used"], stats["requests_limit"], stats["items_limit"]) == ("free", 42, 1000, 50000)


def test_stats_project_cost_and_warn_when_over_the_free_quota():
    conn = _meta()
    few = [f"https://www.reddit.com/r/s{i}/.rss" for i in range(5)]
    stats = svc.get_stats(conn, few, NOW)
    assert (stats["subreddits"], stats["requests_per_sweep"], stats["projected_requests"]) == (5, 1, 730)
    assert not any("over the" in h for h in stats["hints"])
    many = [f"https://www.reddit.com/r/s{i}/.rss" for i in range(11)]
    stats = svc.get_stats(conn, many, NOW)
    assert stats["projected_requests"] == 1460
    assert any("over the 1000" in h for h in stats["hints"])


def test_stats_count_profiles_and_warn_near_the_quota():
    conn = _meta()
    svc._record_usage(conn, {"requests_used": 850, "requests_limit": 1000}, NOW)
    stats = svc.get_stats(conn, ["https://www.reddit.com/user/spez/submitted/.rss"], NOW)
    assert (stats["profiles"], stats["projected_requests"]) == (1, 30)
    assert any("850 of 1000" in h for h in stats["hints"])


def test_key_change_clears_stored_plan_and_usage():
    conn = _meta()
    svc._record_usage(conn, {"requests_used": 5, "requests_limit": 1000}, NOW, {"plan": "free"})
    with patch.object(svc.httpx, "Client", return_value=_client(_resp(body=_listing()))):
        svc.refresh_reddit_feeds(conn, _reader("https://www.reddit.com/r/Python/.rss"), "key-one", now=NOW)
    svc._record_usage(conn, {"requests_used": 5, "requests_limit": 1000}, NOW, {"plan": "free"})
    with patch.object(svc.httpx, "Client", return_value=_client(_resp(body=_listing()))):
        svc.refresh_reddit_feeds(conn, _reader("https://www.reddit.com/r/Python/.rss"), "key-two", now=NOW + 10_000)
    stats = svc.get_stats(conn, [], NOW + 10_000)
    assert (stats["plan"], stats["requests_used"]) == ("", None)


def test_check_key_is_throttled_and_makes_one_request():
    conn = _meta()
    body = {"plan": "free", "limits": {"monthly_quota": 1000}, "usage": {"requests": 1, "items": 2}}
    client = _client(_resp(body=body))
    with patch.object(svc.httpx, "Client", return_value=client):
        svc.check_key(conn, "k", now=NOW)
        stats = svc.check_key(conn, "k", now=NOW + 10)  # within the cooldown: no second request
    assert client.get.call_count == 1
    assert stats["plan"] == "free"


def test_check_key_429_pauses_further_checks():
    conn = _meta()
    with patch.object(svc.httpx, "Client", return_value=_client(_resp(429, headers={"Retry-After": "600"}))):
        with pytest.raises(svc.RedlistenError):
            svc.check_key(conn, "k", now=NOW)
    client = _client()
    with patch.object(svc.httpx, "Client", return_value=client), pytest.raises(svc.RedlistenError):
        svc.check_key(conn, "k", now=NOW + 400)  # past the cooldown but still paused
    assert client.get.call_count == 0


def test_quota_429_still_records_usage():
    conn = _meta()
    resp = _resp(429, body={"error": {"code": "monthly_quota_exceeded"}}, headers=_QUOTA_HEADERS)
    with patch.object(svc.httpx, "Client", return_value=_client(resp)):
        svc.refresh_reddit_feeds(conn, _reader("https://www.reddit.com/r/Python/.rss"), "k", now=NOW)
    assert svc.get_stats(conn, [], NOW)["requests_used"] == 120


def test_corrupt_stored_usage_is_ignored():
    conn = _meta()
    conn.execute("INSERT INTO app_settings VALUES ('redlisten_usage', 'null')")
    assert svc.get_stats(conn, [], NOW)["requests_used"] is None
    svc._record_usage(conn, {"requests_used": 1}, NOW)
    assert svc.get_stats(conn, [], NOW)["requests_used"] == 1
