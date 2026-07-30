"""Batch re-fetch: what it touches, how long it claims to take, and its guards.

Bulk re-fetch spends someone else's bandwidth, so the parts that make it
defensible are the parts worth pinning: the scope is kept articles only, the
pacing is shared with the CLI rather than reimplemented, the estimate accounts
for the per-host delay, and two runs cannot overlap.
"""
from __future__ import annotations

import json

import pytest

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


def test_a_second_run_is_refused_while_one_is_going(configured):
    """Two overlapping runs would each honor the pacing and together double the
    rate every host sees — the politeness guarantee only holds one job at a time."""
    import asyncio

    job = main._refetch_job_state(create=True)
    job.update({"running": True, "done": 0, "total": 5})

    class _Req:
        async def json(self):
            return {"list_feed_url": FEED_A}

    resp = asyncio.run(main.start_refetch_scope(_Req()))

    assert resp.status_code == 409


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
    assert job["running"] is False


def test_one_exploding_entry_does_not_end_the_run(configured, monkeypatch):
    def _boom(feed_url, entry_id, _mode):
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
    assert job["running"] is False
