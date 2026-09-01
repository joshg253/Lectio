"""Two bulk actions auto-mark-read the posts they touch, raised 2026-08-31:
waiting for the "done" toast just to then right-click -> Mark as read on the
same selection was a second manual step for the common case.

- YT playlist bulk add marks read only what the server reports as "settled"
  (ok_video_ids: newly-added or already-on-the-playlist) — a video that
  failed, or was never reached because the run stopped on quota, is left
  unread on purpose. The finished-job handling (_ytHandleFinishedBatchJob) is
  shared between the live poller and a page-load resume path
  (_ytResumeBatchJobOnLoad, also raised 2026-08-31): switching folders is a
  real page reload in this app, which kills whatever poll loop was watching a
  batch, so a batch that kept adding (or finished) after the reload used to
  never get its auto-mark-read step run at all -- every successfully-added
  video sat unread with no way to notice short of re-selecting and letting
  the dedup-skip path re-mark it. The resume path re-fetches the tracked job
  on load and, if it's running or finished-but-unconsumed, rebuilds `posts`
  from the current DOM's post-item video-id attributes and picks up where the
  original poller would have left off.
- Bulk "Edit tags" (formerly "Add tag", 2026-08-31) marks read every entry
  that still has a tag after the edit — tagging implies keeping/filing it,
  but since a bulk edit can now REMOVE a tag too, an entry that lost its last
  tag is not a keep action and must not be marked read.

Source assertions, because this is client-side bulk-action wiring with no JS
test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def test_finished_job_handler_marks_read_only_settled_videos():
    idx = APP_JS.index("async function _ytHandleFinishedBatchJob(job, posts, playlistTitle)")
    block = APP_JS[idx:idx + 1200]
    assert "job.ok_video_ids" in block
    assert "okIds.has(p.videoId)" in block
    assert "/entries/read-batch" in block
    assert "applyReadStateToSelection(toMark)" in block


def test_poller_delegates_to_the_shared_finished_job_handler_and_marks_it_consumed():
    idx = APP_JS.index("async function _ytPollBatchAddProgress(playlistTitle, posts, jobId)")
    block = APP_JS[idx:idx + 2500]
    assert "await _ytHandleFinishedBatchJob(job, posts, playlistTitle);" in block
    assert "_ytMarkBatchJobConsumed(job.job_id);" in block


def test_yt_bulk_add_passes_posts_and_job_id_to_the_poller():
    """Without threading `posts` through, the poller has nothing to match
    ok_video_ids against and can't mark anything read. job_id (raised in
    review 2026-08-31) lets the poller recognize a status response that
    belongs to a DIFFERENT, later batch rather than its own."""
    assert "await _ytPollBatchAddProgress(choice.title, posts, d.job_id);" in APP_JS


def test_poller_ignores_a_status_response_for_a_different_job():
    idx = APP_JS.index("async function _ytPollBatchAddProgress(playlistTitle, posts, jobId)")
    block = APP_JS[idx:idx + 1300]
    assert "job_id=${encodeURIComponent(jobId)}" in block
    assert "if (job.stale) return;" in block


def test_resume_on_load_rebuilds_posts_from_the_dom_and_reuses_the_poller():
    idx = APP_JS.index("async function _ytResumeBatchJobOnLoad()")
    block = APP_JS[idx:idx + 1800]
    assert "add-batch/status" in block
    # Nothing to resume: no job, or a finished job with nothing to mark and
    # already handled once (job_id recorded consumed).
    assert "if (!job || !job.ok || !job.job_id) return;" in block
    assert "_ytBatchJobAlreadyConsumed(job.job_id)" in block
    assert ".post-item[data-post-video-id]" in block
    assert "data-post-feed-url" in block
    assert "data-post-entry-id" in block
    # A still-running job resumes the SAME live poller a fresh start would use.
    assert "await _ytPollBatchAddProgress('your playlist', posts, job.job_id);" in block
    # An already-finished-but-unconsumed job goes straight to the shared handler.
    assert "await _ytHandleFinishedBatchJob(job, posts, 'your playlist');" in block
    assert "_ytMarkBatchJobConsumed(job.job_id);" in block


def test_resume_on_load_is_called_once_on_page_init_gated_on_the_yt_feature_flag():
    assert "if (_ytAccountFeaturesEnabled) _ytResumeBatchJobOnLoad();" in APP_JS


def test_consumed_tracking_is_per_job_id_via_local_storage():
    idx = APP_JS.index("function _ytMarkBatchJobConsumed(jobId)")
    block = APP_JS[idx:idx + 700]
    assert "localStorage.setItem('lectio-yt-batch-consumed', jobId)" in block
    assert "localStorage.getItem('lectio-yt-batch-consumed') === jobId" in block


def test_bulk_edit_tags_marks_read_only_entries_still_tagged_after_the_edit():
    idx = APP_JS.index("showToastMessage(data.message || 'Tags updated.');")
    block = APP_JS[idx:idx + 3200]
    assert "data.still_tagged" in block
    assert "data.now_untagged" in block
    assert "stillTaggedKeys" in block
    assert "/entries/read-batch" in block
    assert "applyReadStateToSelection(toMarkRead)" in block
