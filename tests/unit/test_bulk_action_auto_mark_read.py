"""Two bulk actions auto-mark-read the posts they touch, raised 2026-08-31:
waiting for the "done" toast just to then right-click -> Mark as read on the
same selection was a second manual step for the common case.

- YT playlist bulk add marks read only what the server reports as "settled"
  (ok_video_ids: newly-added or already-on-the-playlist) — a video that
  failed, or was never reached because the run stopped on quota, is left
  unread on purpose.
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


def test_yt_playlist_poll_marks_read_only_settled_videos():
    idx = APP_JS.index("async function _ytPollBatchAddProgress(playlistTitle, posts, jobId)")
    block = APP_JS[idx:idx + 3000]
    assert "job.ok_video_ids" in block
    assert "okIds.has(p.videoId)" in block
    assert "/entries/read-batch" in block
    assert "applyReadStateToSelection(toMark)" in block


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


def test_bulk_edit_tags_marks_read_only_entries_still_tagged_after_the_edit():
    idx = APP_JS.index("showToastMessage(data.message || 'Tags updated.');")
    block = APP_JS[idx:idx + 3200]
    assert "data.still_tagged" in block
    assert "data.now_untagged" in block
    assert "stillTaggedKeys" in block
    assert "/entries/read-batch" in block
    assert "applyReadStateToSelection(toMarkRead)" in block
