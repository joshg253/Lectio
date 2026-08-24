"""Batch re-fetch: what it touches, how long it claims to take, and its guards.

Bulk re-fetch spends someone else's bandwidth, so the parts that make it
defensible are the parts worth pinning: the scope is kept articles only, the
pacing is shared with the CLI rather than reimplemented, the estimate accounts
for the per-host delay, and two runs cannot overlap.
"""
from __future__ import annotations

import json
from typing import cast

import pytest
from fastapi import Request

import main
from services import refetch_batch, tenancy

FEED_A = "https://a.test/feed"
FEED_B = "https://b.test/feed"
MTAG = main.MANUAL_TAG_KEY_PREFIX


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        for feed in (FEED_A, FEED_B):
            reader.add_feed(feed, allow_invalid_url=True, exist_ok=True)
            reader.disable_feed_updates(feed)
        for eid in ("starred", "tagged", "plain", "relative"):
            reader.add_entry({"feed_url": FEED_A, "id": eid,
                              "link": f"https://a.test/{eid}"})
        reader.add_entry({"feed_url": FEED_B, "id": "starred",
                          "link": "https://b.test/starred"})
        # No usable link at all — must not reach the fetcher.
        reader.add_entry({"feed_url": FEED_A, "id": "nolink", "link": ""})
        reader.set_tag((FEED_A, "tagged"), f"{MTAG}python")
    with main.get_meta_connection() as conn:
        for feed, eid in ((FEED_A, "starred"), (FEED_B, "starred"), (FEED_A, "nolink")):
            conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                         (feed, eid))
        conn.commit()
    main._refetch_jobs.clear()
    try:
        yield
    finally:
        main._refetch_jobs.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


# ── scope ──
def test_scope_is_kept_articles_only(configured):
    """Starred OR tagged. An unkept feed entry is rewritten by the next refresh
    anyway, so re-fetching it spends a request to change nothing."""
    rows = main._scope_refetchable(None, FEED_A)

    assert {e for _f, e, _l in rows} == {"starred", "tagged"}


def test_scope_skips_entries_with_no_http_link(configured):
    """A kept entry with no usable link would otherwise be handed to the fetcher
    as a URL it cannot resolve."""
    assert "nolink" not in {e for _f, e, _l in main._scope_refetchable(None, FEED_A)}


def test_feed_scope_does_not_leak_into_other_feeds(configured):
    assert {f for f, _e, _l in main._scope_refetchable(None, FEED_A)} == {FEED_A}


# ── pacing and the estimate ──
def test_estimate_counts_the_per_host_delay_not_just_the_global_one():
    """A single-feed scope is one host, so N articles is N * PER_HOST_DELAY. Using
    the global delay alone understated a 15-minute run as under 4 minutes, which is
    the one number a deliberately slow job must not get wrong."""
    rows = [("f", str(i), f"https://one.test/{i}") for i in range(89)]

    assert refetch_batch.estimate_seconds(rows) == pytest.approx(890.0)


def test_spreading_across_hosts_is_what_makes_a_batch_faster():
    same = [("f", str(i), f"https://one.test/{i}") for i in range(20)]
    spread = [("f", str(i), f"https://h{i % 10}.test/{i}") for i in range(20)]

    assert refetch_batch.estimate_seconds(spread) < refetch_batch.estimate_seconds(same)


def test_interleaving_never_puts_two_hits_on_one_host_back_to_back():
    rows = [("f", str(i), "https://one.test/x") for i in range(3)]
    rows += [("f", str(i), "https://two.test/x") for i in range(3)]

    hosts = [refetch_batch.host_of(link) for _f, _e, link in
             refetch_batch.interleave_by_host(rows)]

    assert all(a != b for a, b in zip(hosts, hosts[1:], strict=False))


def test_the_job_and_the_cli_share_one_set_of_delays():
    """Pacing that holds in only one of two entry points is not a guarantee."""
    import scripts.refetch_scope as cli

    assert (cli._GLOBAL_DELAY, cli._PER_HOST_DELAY, cli._HOST_FAILURE_LIMIT) == (
        refetch_batch.GLOBAL_DELAY, refetch_batch.PER_HOST_DELAY,
        refetch_batch.HOST_FAILURE_LIMIT)


# ── the route ──
def _preview(**kw):
    return json.loads(bytes(main.preview_refetch_scope(**kw).body))


def test_preview_reports_count_hosts_and_runtime(configured):
    body = _preview(folder_id=None, list_feed_url=FEED_A)

    assert body["count"] == 2
    assert body["hosts"] == 1
    assert body["estimate_seconds"] == int(2 * refetch_batch.PER_HOST_DELAY)


def test_status_is_idle_before_anything_runs(configured):
    assert json.loads(bytes(main.refetch_scope_status().body))["idle"] is True


class _Req:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _start(payload):
    import asyncio
    resp = asyncio.run(main.start_refetch_scope(cast(Request, _Req(payload))))
    return resp.status_code, json.loads(bytes(resp.body))


def test_a_second_scope_is_queued_not_refused(configured):
    """Only one batch may run at a time — two overlapping runs would each honor the
    pacing and together double the rate every host sees. But that is a reason to
    serialize them, not to make the user wait at the keyboard and come back."""
    job = main._refetch_job_state(create=True)
    job.update({"running": True, "done": 0, "total": 5, "queue": []})

    status, body = _start({"list_feed_url": FEED_A})

    assert status == 200
    assert (body["queued"], body["position"]) == (True, 1)
    assert [q["list_feed_url"] for q in job["queue"]] == [FEED_A]


def test_queueing_the_same_scope_twice_is_refused(configured):
    """Running a scope twice back to back re-fetches every article again for
    nothing, at someone else's expense."""
    job = main._refetch_job_state(create=True)
    job.update({"running": True, "queue": []})
    _start({"list_feed_url": FEED_A})

    status, body = _start({"list_feed_url": FEED_A})

    assert status == 409
    assert len(job["queue"]) == 1


def test_an_empty_scope_is_rejected_before_it_can_be_queued(configured):
    status, _body = _start({"list_feed_url": "https://nothing.test/feed"})

    assert status == 400


def test_the_queue_is_visible_in_the_status(configured):
    """The complaint that started this: a background job you cannot see."""
    job = main._refetch_job_state(create=True)
    job.update({"running": True, "done": 2, "total": 5, "scope": "A", "queue": []})
    _start({"list_feed_url": FEED_A})

    body = json.loads(bytes(main.refetch_scope_status().body))

    assert body["running"] is True
    assert (body["done"], body["total"], body["scope"]) == (2, 5, "A")
    assert [q["count"] for q in body["queue"]] == [2]
    assert "list_feed_url" not in body["queue"][0]   # label/count only, not internals


def test_the_status_payload_never_exposes_the_cancel_flag(configured):
    """It is internal control state, not progress; leaking it invites a client to
    treat it as something it can set."""
    job = main._refetch_job_state(create=True)
    job.update({"running": True, "cancel": False, "done": 1, "total": 5})

    assert "cancel" not in json.loads(bytes(main.refetch_scope_status().body))


# ── the run loop ──
def _run(results, rows=None, job=None):
    """Drive _run_refetch_batch with canned per-entry results and no sleeping."""
    rows = rows or [("f", str(i), f"https://h{i}.test/x") for i in range(len(results))]
    job = job if job is not None else {"running": True, "cancel": False}
    job.setdefault("done", 0)
    for k in ("ok", "archive", "refused", "dead", "failed", "skipped"):
        job.setdefault(k, 0)
    calls = iter(results)
    main._run_refetch_batch(rows, job)
    return job, calls


def test_batch_does_not_bump_saved_at(configured, monkeypatch):
    """A single deliberate re-fetch surfaces the article at the top of the
    Inbox on purpose (bump_received defaults to True for a capture). A bulk
    backfill across dozens of old articles isn't that — it used to dump the
    whole Inbox's star order onto whatever finished last in the batch."""
    seen_kwargs = []

    def _fake(feed_url, entry_id, mode, **kwargs):
        seen_kwargs.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", _fake)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)

    _run([None] * 3)

    assert seen_kwargs == [{"bump_received": False}] * 3


def test_outcomes_are_counted_apart(configured, monkeypatch):
    """A refusal is not a failure: the guard did its job and the stored copy was
    deliberately left alone. Lumping them together would read as breakage."""
    outcomes = [{"ok": True}, {"ok": True, "from_archive": True},
                {"ok": False, "mismatch": True}, {"ok": False, "dead": True},
                {"ok": False}]
    seq = iter(outcomes)
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user",
                        lambda *a, **k: next(seq))
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)

    job, _ = _run(outcomes)

    assert (job["ok"], job["archive"], job["refused"], job["dead"], job["failed"]) == (
        1, 1, 1, 1, 1)


def test_one_exploding_entry_does_not_end_the_run(configured, monkeypatch):
    def _boom(feed_url, entry_id, _mode, **_kw):
        if entry_id == "1":
            raise RuntimeError("network went sideways")
        return {"ok": True}

    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", _boom)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)

    job, _ = _run([None] * 3)

    assert job["done"] == 3
    assert (job["ok"], job["failed"]) == (2, 1)


def test_a_host_that_keeps_failing_is_dropped(configured, monkeypatch):
    """Hammering a site that has failed four times running is exactly the
    behaviour a polite client must not have."""
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user",
                        lambda *a, **k: {"ok": False})
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    rows = [("f", str(i), "https://flaky.test/x") for i in range(10)]

    job, _ = _run([], rows=rows)

    assert job["failed"] == refetch_batch.HOST_FAILURE_LIMIT
    assert job["skipped"] == 10 - refetch_batch.HOST_FAILURE_LIMIT
    assert job["done"] == 10


def test_cancel_stops_the_run_partway(configured, monkeypatch):
    job = {"running": True, "cancel": False}

    def _one(*_a, **_k):
        job["cancel"] = True          # cancelled while the first entry is in flight
        return {"ok": True}

    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", _one)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    rows = [("f", str(i), f"https://h{i}.test/x") for i in range(5)]

    _run([], rows=rows, job=job)

    assert job["done"] == 1


def test_a_queued_scope_can_be_dropped_without_stopping_the_run(configured):
    import asyncio

    job = main._refetch_job_state(create=True)
    job.update({"running": True, "queue": []})
    _start({"list_feed_url": FEED_A})

    asyncio.run(main.cancel_refetch_scope(cast(Request, _Req({"queued_index": 0}))))

    assert job["queue"] == []
    assert job["running"] is True          # the batch in flight is untouched


def test_cancel_all_empties_the_queue_too(configured):
    import asyncio

    job = main._refetch_job_state(create=True)
    job.update({"running": True, "queue": []})
    _start({"list_feed_url": FEED_A})

    asyncio.run(main.cancel_refetch_scope(cast(Request, _Req({"all": True}))))

    assert job["queue"] == []
    assert job["cancel"] is True


def test_the_worker_drains_the_queue_and_stays_running_throughout(configured, monkeypatch):
    """`running` must not blink off between scopes — the status pill reads it, and
    a pill that vanishes mid-queue is the same invisibility this fixed."""
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    seen_running = []
    real = main._run_refetch_batch

    def _spy(rows, job):
        seen_running.append(job["running"])
        real(rows, job)

    monkeypatch.setattr(main, "_run_refetch_batch", _spy)

    job = main._refetch_job_state(create=True)
    main._refetch_begin(job, "A", [("f", "x", "https://a.test/x")], 10)
    job["queue"] = [{"folder_id": None, "list_feed_url": FEED_A,
                     "label": "B", "count": 2, "estimate_seconds": 20}]

    main._refetch_worker([("f", "x", "https://a.test/x")], job)

    assert seen_running == [True, True]     # never observed as stopped mid-queue
    assert job["running"] is False          # ... but off once the queue drained
    assert [h["scope"] for h in job["history"]] == ["A", "B"]


def test_a_queued_scope_is_resolved_when_it_starts_not_when_it_is_queued(configured,
                                                                        monkeypatch):
    """An hour in a queue is long enough for what is kept in the scope to change,
    so re-fetching the list captured at queue time would act on stale membership."""
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    resolved = []
    monkeypatch.setattr(main, "_scope_refetchable",
                        lambda fid, feed: resolved.append((fid, feed)) or [])

    job = main._refetch_job_state(create=True)
    main._refetch_begin(job, "A", [], 0)
    job["queue"] = [{"folder_id": None, "list_feed_url": FEED_A,
                     "label": "B", "count": 99, "estimate_seconds": 20}]

    main._refetch_worker([], job)

    assert resolved == [(None, FEED_A)]
