"""Two bulk actions auto-mark-read the posts they touch, raised 2026-08-31:
waiting for the "done" toast just to then right-click -> Mark as read on the
same selection was a second manual step for the common case.

- YT playlist bulk add marks read only what the server reports as "settled"
  (ok_video_ids: newly-added or already-on-the-playlist) — a video that
  failed, or was never reached because the run stopped on quota, is left
  unread on purpose.
- Bulk "Add tag" marks read everything tagged — tagging implies keeping/
  filing it, unconditionally (no partial-failure case: the route either
  tags every entry or reports one shared error).

Source assertions, because this is client-side bulk-action wiring with no JS
test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def test_yt_playlist_poll_marks_read_only_settled_videos():
    idx = APP_JS.index("async function _ytPollBatchAddProgress(playlistTitle, posts)")
    block = APP_JS[idx:idx + 2200]
    assert "job.ok_video_ids" in block
    assert "okIds.has(p.videoId)" in block
    assert "/entries/read-batch" in block
    assert "applyReadStateToSelection(toMark)" in block


def test_yt_bulk_add_passes_posts_to_the_poller():
    """Without threading `posts` through, the poller has nothing to match
    ok_video_ids against and can't mark anything read."""
    assert "await _ytPollBatchAddProgress(choice.title, posts);" in APP_JS


def test_bulk_add_tag_marks_read_everything_tagged():
    idx = APP_JS.index("showToastMessage(data.message || 'Tags added.');")
    block = APP_JS[idx:idx + 1150]
    assert "/entries/read-batch" in block
    assert "applyReadStateToSelection(entries)" in block
