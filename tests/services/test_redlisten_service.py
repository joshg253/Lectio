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
        svc.check_key("nope")


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
