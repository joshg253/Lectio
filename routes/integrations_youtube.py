"""YouTube OAuth (connect/callback/disconnect) and playlist actions.

The bulk add-batch job state (`_yt_playlist_batch_jobs` and friends) is a
module-level singleton — tests that reach into it or monkeypatch
`get_youtube_oauth_token` for these routes must target this module, not
`main` (see the docstring on the batch job code below for why)."""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Literal, overload
from urllib.parse import quote_plus

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    _MOVE_BATCH_CAP,
    LOGGER,
    SETTING_YT_OAUTH_ACCESS_TOKEN,
    SETTING_YT_OAUTH_REFRESH_TOKEN,
    SETTING_YT_OAUTH_STATE,
    SETTING_YT_OAUTH_TOKEN_EXPIRES_AT,
    _PerUserDict,
    _run_in_user_context,
    delete_setting,
    get_meta_connection,
    get_setting,
    get_youtube_oauth_credentials,
    get_youtube_oauth_token,
    mark_yt_quota_exhausted,
    set_setting,
    youtube_duration_service,
)
from services import tenancy
from services import youtube_oauth as youtube_oauth_service
from services.errors import public_error

router = APIRouter()


def _youtube_oauth_redirect_uri(request: Request) -> str:
    """Callback URL Google redirects back to — MUST exactly match the URI
    registered on the OAuth client in Google Cloud."""
    base = os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/integrations/youtube/oauth/callback"
    return str(request.url_for("youtube_oauth_callback"))


@router.get("/integrations/youtube/oauth/connect")
def youtube_oauth_connect(request: Request):
    """Kick off the YouTube OAuth flow → redirect to Google's consent page."""
    cid, secret = get_youtube_oauth_credentials()
    if not cid or not secret:
        return RedirectResponse(
            url="/?message=" + quote_plus("YouTube OAuth client is not configured (set YOUTUBE_OAUTH_CLIENT_ID/SECRET)."),
            status_code=303,
        )
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_YT_OAUTH_STATE, state)
    url = youtube_oauth_service.authorize_url(cid, _youtube_oauth_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/integrations/youtube/oauth/callback")
def youtube_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them per-user."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"YouTube authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_YT_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("YouTube authorization failed (bad state)."), status_code=303)
    cid, secret = get_youtube_oauth_credentials()
    try:
        data = youtube_oauth_service.exchange_code(cid, secret, code, _youtube_oauth_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(public_error(exc, "YouTube connect")), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_YT_OAUTH_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_YT_OAUTH_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_YT_OAUTH_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        delete_setting(conn, SETTING_YT_OAUTH_STATE)
    return RedirectResponse(url="/?message=" + quote_plus("YouTube account connected."), status_code=303)


@router.post("/integrations/youtube/oauth/disconnect")
def youtube_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_YT_OAUTH_ACCESS_TOKEN,
            SETTING_YT_OAUTH_REFRESH_TOKEN,
            SETTING_YT_OAUTH_TOKEN_EXPIRES_AT,
            SETTING_YT_OAUTH_STATE,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


@router.get("/api/youtube/playlists")
def youtube_playlists_route():
    """List the connected user's playlists for the Add-to-playlist dropdown."""
    token = get_youtube_oauth_token()
    if not token:
        return JSONResponse({"connected": False, "playlists": []})
    try:
        playlists = youtube_oauth_service.list_playlists(token)
    except youtube_oauth_service.QuotaExceeded:
        mark_yt_quota_exhausted()
        return JSONResponse({"connected": True, "error": "quota", "playlists": []}, status_code=429)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"connected": True, "error": public_error(exc, "Loading YouTube playlists"), "playlists": []}, status_code=502)
    return JSONResponse({"connected": True, "playlists": playlists})


@router.post("/api/youtube/playlists/add")
async def youtube_playlist_add_route(request: Request):
    """Add a video to a playlist (or a new one). Body: {video_id, playlist_id?, new_title?}."""
    body = await request.json()
    video_id = (body.get("video_id") or "").strip()
    playlist_id = (body.get("playlist_id") or "").strip()
    new_title = (body.get("new_title") or "").strip()
    if not video_id or (not playlist_id and not new_title):
        return JSONResponse({"ok": False, "error": "video_id and playlist_id or new_title required"}, status_code=400)
    token = get_youtube_oauth_token()
    if not token:
        return JSONResponse({"ok": False, "error": "not_connected"}, status_code=401)
    try:
        if not playlist_id:
            created = youtube_oauth_service.create_playlist(token, new_title)
            playlist_id = created["id"]
        youtube_oauth_service.add_video_to_playlist(token, playlist_id, video_id)
    except youtube_oauth_service.QuotaExceeded:
        mark_yt_quota_exhausted()
        return JSONResponse({"ok": False, "error": "quota"}, status_code=429)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": public_error(exc, "Adding to the YouTube playlist")}, status_code=502)
    return JSONResponse({"ok": True, "playlist_id": playlist_id})


# One YT playlist bulk-add job at a time, per user — same reasoning as
# _refetch_jobs: two interleaved batches would double the YouTube API call
# rate against the same daily quota, and could race each other's duplicate
# check. Progress is polled from here rather than pushed, mirroring the
# refetch-scope status pill — the shortest mechanism the app already has for
# "a request that keeps a background thread busy for longer than a person
# should stare at a spinner for."
_yt_playlist_batch_jobs = _PerUserDict()
_yt_playlist_batch_jobs_lock = threading.Lock()


@overload
def _yt_playlist_batch_job_state(create: Literal[True]) -> dict: ...
@overload
def _yt_playlist_batch_job_state(create: bool = False) -> dict | None: ...
def _yt_playlist_batch_job_state(create: bool = False) -> dict | None:
    with _yt_playlist_batch_jobs_lock:
        job = _yt_playlist_batch_jobs.get("job")
        if job is None and create:
            job = {"running": False}
            _yt_playlist_batch_jobs["job"] = job
        return job


def _yt_playlist_job_update(job: dict, fields: dict) -> None:
    """Merge *fields* into *job* under the shared lock, one atomic update.

    Raised in review 2026-08-31: the status route used to read `job` (via
    `{"ok": True, **job}`) with no lock while this worker mutated it field by
    field with no lock either, so a status poll landing mid-iteration could
    observe a torn snapshot — e.g. `processed` already bumped but `added`
    still one behind. Batching each iteration's changes into one dict and
    merging them in a single locked `.update()` means a poll only ever sees
    a fully-applied iteration, never a partial one."""
    with _yt_playlist_batch_jobs_lock:
        job.update(fields)


def _run_yt_playlist_batch_add(
    video_ids: list[str],
    playlist_id: str,
    new_title: str,
    job: dict,
) -> None:
    """Background worker for POST /api/youtube/playlists/add-batch.

    Mutates *job* in place as it goes (same idiom as _run_refetch_batch) so
    GET .../add-batch/status, polled by the client, can show live progress —
    raised 2026-08-30: the prior synchronous version blocked one HTTP request
    for the whole batch (a playlist-contents fetch plus one YouTube API call
    per video) with no feedback until it finished, which on a 50-video batch
    read as "nothing is happening" for the better part of a minute."""
    token = get_youtube_oauth_token()
    if not token:
        _yt_playlist_job_update(job, {"running": False, "done": True, "error": "not_connected"})
        return
    try:
        if not playlist_id:
            created = youtube_oauth_service.create_playlist(token, new_title)
            playlist_id = created["id"]
            existing: set[str] = set()  # brand-new playlist — nothing to dedupe against
        else:
            existing = youtube_oauth_service.existing_videos_for_batch(token, playlist_id, video_ids)
    except youtube_oauth_service.QuotaExceeded:
        mark_yt_quota_exhausted()
        _yt_playlist_job_update(job, {"running": False, "done": True, "error": "quota"})
        return
    except Exception as exc:  # noqa: BLE001
        _yt_playlist_job_update(job, {"running": False, "done": True, "error": str(exc)})
        return

    _yt_playlist_job_update(job, {"playlist_id": playlist_id, "phase": "adding"})
    added = duplicate = failed = 0
    # Videos that ended up either newly-added or already-on-the-playlist —
    # the two outcomes the client auto-marks read for once the job finishes
    # (raised 2026-08-31: "have to wait until I see the done message before I
    # can rc->mark"). A video that failed, or never got looked at because the
    # run stopped on quota, is deliberately excluded — those are exactly the
    # cases worth noticing unread, not silently marking done.
    ok_video_ids: list[str] = []
    for i, video_id in enumerate(video_ids, 1):
        update: dict = {}
        quota_stopped = False
        if video_id in existing:
            duplicate += 1
            update["duplicate"] = duplicate
            ok_video_ids.append(video_id)
        else:
            try:
                youtube_oauth_service.add_video_to_playlist(token, playlist_id, video_id)
                existing.add(video_id)  # guards against a dupe within this same batch too
                added += 1
                update["added"] = added
                ok_video_ids.append(video_id)
            except youtube_oauth_service.QuotaExceeded:
                # Stop burning calls once quota's gone; report what succeeded so far
                # rather than hiding real progress behind an error.
                mark_yt_quota_exhausted()
                update["error"] = "quota"
                quota_stopped = True
            except Exception as exc:  # noqa: BLE001 — one bad video must not sink the batch
                failed += 1
                update["failed"] = failed
                LOGGER.warning("[yt-playlist-batch] failed to add %s to %s: %s", video_id, playlist_id, exc)
        update["processed"] = i
        update["ok_video_ids"] = list(ok_video_ids)
        _yt_playlist_job_update(job, update)
        if quota_stopped:
            break

    msg = f"Added {added}."
    if duplicate:
        msg += f" {duplicate} already in the playlist."
    if failed:
        msg += f" {failed} failed."
    _yt_playlist_job_update(job, {"running": False, "done": True, "phase": "done", "message": msg})


@router.post("/api/youtube/playlists/add-batch")
async def youtube_playlist_add_batch_route(request: Request):
    """Start a background bulk add of several videos to a playlist (or a new
    one); the post list's multi-selection "Add to YouTube Playlist…" action.

    Body: {video_ids: [str, ...], playlist_id?, new_title?}. Returns
    immediately once the job is running — the client polls
    GET /api/youtube/playlists/add-batch/status for progress, same shape as
    the refetch-scope status pill. See _run_yt_playlist_batch_add for the
    actual work, including the existing-contents check that skips a video
    already in the playlist rather than letting the insert create a silent
    duplicate."""
    body = await request.json()
    video_ids = body.get("video_ids")
    if not isinstance(video_ids, list) or not video_ids:
        return JSONResponse({"ok": False, "error": "video_ids required"}, status_code=400)
    video_ids = [str(v).strip() for v in video_ids if str(v).strip()]
    if len(video_ids) > _MOVE_BATCH_CAP:
        return JSONResponse(
            {"ok": False, "error": f"Too many videos (max {_MOVE_BATCH_CAP} per action)."},
            status_code=400,
        )
    playlist_id = (body.get("playlist_id") or "").strip()
    new_title = (body.get("new_title") or "").strip()
    if not playlist_id and not new_title:
        return JSONResponse({"ok": False, "error": "playlist_id or new_title required"}, status_code=400)
    if not get_youtube_oauth_token():
        return JSONResponse({"ok": False, "error": "not_connected"}, status_code=401)

    job = _yt_playlist_batch_job_state(create=True)
    # A fresh id per batch (not reused across runs), so the status route can
    # tell "this is MY batch's progress" from "a different batch started and
    # finished between my polls" -- raised in review 2026-08-31: with no id,
    # a short batch finishing before the client's first 900ms poll, followed
    # immediately by a second batch, meant that first poll could consume the
    # SECOND batch's status and mark the wrong posts read.
    job_id = secrets.token_hex(8)
    dupe_job = _yt_dupe_jobs.get("job")
    with _yt_playlist_batch_jobs_lock:
        if job.get("running") or (dupe_job and dupe_job.get("running")):
            return JSONResponse({"ok": False, "error": "busy"}, status_code=409)
        job.update(
            {
                "running": True,
                "done": False,
                "error": None,
                "phase": "checking_existing",
                "total": len(video_ids),
                "processed": 0,
                "added": 0,
                "duplicate": 0,
                "failed": 0,
                "message": None,
                "ok_video_ids": [],
                "job_id": job_id,
            }
        )

    uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(uid, _run_yt_playlist_batch_add, video_ids, playlist_id, new_title, job),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "started": True, "total": len(video_ids), "job_id": job_id})


@router.get("/api/youtube/playlists/add-batch/status")
def youtube_playlist_add_batch_status_route(job_id: str | None = Query(default=None)):
    """Progress of the running bulk-add job, polled by the client for a live
    toast — see _run_yt_playlist_batch_add for what each field means.

    *job_id*, when passed, must match the current job's own id or the
    response reports not-running rather than handing back a DIFFERENT
    batch's progress — see the id-generation comment on the POST route for
    why. Omitted entirely, the caller gets whatever job is currently
    tracked (back-compat for any caller that hasn't been given an id yet)."""
    job = _yt_playlist_batch_job_state()
    if job is None:
        return JSONResponse({"ok": True, "running": False})
    # Snapshot under the lock rather than unpacking the live dict directly —
    # the worker mutates it via _yt_playlist_job_update, also under this
    # lock, so a poll landing between two of its updates used to risk a torn
    # read (some fields already bumped, others not yet). _yt_playlist_batch_job_state()
    # already released the lock by the time we get `job` back, so re-acquiring
    # it here for the copy alone doesn't deadlock (the lock isn't reentrant).
    with _yt_playlist_batch_jobs_lock:
        snapshot = dict(job)
    if job_id and snapshot.get("job_id") != job_id:
        return JSONResponse({"ok": True, "running": False, "stale": True})
    return JSONResponse({"ok": True, **snapshot})


# Playlist duplicate cleaner (Settings -> Utilities). One job at a time per user, polled like the bulk add above.
_yt_dupe_jobs = _PerUserDict()
_yt_dupe_jobs_lock = threading.Lock()
_YT_DUPE_MAX_PLAYLISTS = 200


def _yt_dupe_update(job: dict, fields: dict) -> None:
    with _yt_dupe_jobs_lock:
        job.update(fields)


def _is_gone(exc: Exception) -> bool:
    return "HTTP 404" in str(exc)


def _yt_dupe_bump(job: dict, **counts: int) -> None:
    """Add to the job's running totals (the worker is their only writer), so a poll and an early stop both see real numbers."""
    _yt_dupe_update(job, {key: job[key] + n for key, n in counts.items()})


def _delete_entries(playlist_id: str, entries: list[dict], job: dict, counter: str) -> None:
    """Delete each entry by its playlistItem id, counting successes under *counter* and other failures under "failed".

    A 404 means the entry already went with its twin (YouTube removes every copy of a duplicated video at times), which the
    caller's presence check then covers. Quota and rate-limit errors propagate."""
    for entry in entries:
        token = get_youtube_oauth_token()  # per call: a long run outlives the access token's cached lifetime
        try:
            youtube_oauth_service.delete_playlist_item(token, entry["item_id"])
            _yt_dupe_bump(job, **{counter: 1})
        except youtube_oauth_service.QuotaExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_gone(exc):
                continue
            _yt_dupe_bump(job, failed=1)
            LOGGER.warning("[yt-dupes] failed to delete %s from %s: %s", entry["item_id"], playlist_id, exc)


def _restore_if_missing(playlist_id: str, video_id: str, job: dict) -> str:
    """Re-add *video_id* if the playlist no longer has it. Returns "ok", "quota" or "failed"; never raises.

    A video that could not be put back is recorded on the job (and logged) so the summary can name it: it is the one outcome of
    this utility that loses something."""
    try:
        token = get_youtube_oauth_token()
        if not youtube_oauth_service.video_in_playlist(token, playlist_id, video_id):
            youtube_oauth_service.add_video_to_playlist(token, playlist_id, video_id)
            _yt_dupe_bump(job, restored=1)
        return "ok"
    except youtube_oauth_service.QuotaExceeded:
        outcome = "quota"
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[yt-dupes] could not check or restore %s in %s: %s", video_id, playlist_id, exc)
        outcome = "failed"
    _yt_dupe_bump(job, failed=1)
    _yt_dupe_update(job, {"unrestored": [*job["unrestored"], video_id]})
    LOGGER.warning("[yt-dupes] %s may now be missing from %s", video_id, playlist_id)
    return outcome


def _clean_one_playlist(playlist_id: str, job: dict, remove_unavailable: bool, region: str) -> None:
    """Clean one playlist, adding to the job's counters as it goes.

    YouTube's own web UI removes every copy of a duplicated video when one is deleted, and the API has been seen to do the same.
    So each video is handled on its own: delete its extra copies, then confirm the video is still there and add it back if not
    (a filtered lookup, so listing lag can't create a new duplicate). A quota stop therefore loses at most the video in hand,
    and that one gets a last restore attempt.

    With *remove_unavailable*, entries for deleted or private videos (confirmed against videos.list, so the owner's own private
    uploads stay) and, by cached verdict only (no new page fetches), members-only videos are removed, every copy of them. With
    *region*, videos blocked in that country are removed too."""
    token = get_youtube_oauth_token()
    items = youtube_oauth_service.list_playlist_items(token, playlist_id)
    gone: list[dict] = []
    if remove_unavailable:
        candidates = youtube_oauth_service.unavailable_items(items)
        visible = youtube_oauth_service.existing_video_ids(token, {i["video_id"] for i in candidates}) if candidates else set()
        members = {i["video_id"] for i in items if youtube_duration_service.get_cached_members_only(i["video_id"]) is True}
        gone = youtube_oauth_service.unavailable_items(items, members, visible)
    if region:
        blocked = youtube_oauth_service.blocked_in_region(token, {i["video_id"] for i in items}, region)
        seen = {i["item_id"] for i in gone}
        gone += [i for i in items if i["video_id"] in blocked and i["item_id"] not in seen]
    gone_ids = {i["item_id"] for i in gone}
    extras = youtube_oauth_service.duplicate_items([i for i in items if i["item_id"] not in gone_ids])

    _delete_entries(playlist_id, gone, job, "unavailable")
    by_video: dict[str, list[dict]] = {}
    for extra in extras:
        by_video.setdefault(extra["video_id"], []).append(extra)
    for video_id, copies in by_video.items():
        try:
            _delete_entries(playlist_id, copies, job, "removed")
        except youtube_oauth_service.QuotaExceeded:
            _restore_if_missing(playlist_id, video_id, job)  # best effort before stopping
            raise
        if _restore_if_missing(playlist_id, video_id, job) == "quota":
            raise youtube_oauth_service.QuotaExceeded("quota used up while restoring")


def _dupe_summary(job: dict, remove_unavailable: bool, region: str, error: str | None) -> str:
    n = job["removed"]
    msg = f"Removed {n} duplicate{'' if n == 1 else 's'}."
    if remove_unavailable or region:
        msg += f" Removed {job['unavailable']} unavailable video{'' if job['unavailable'] == 1 else 's'}."
    if job["restored"]:
        msg += f" Re-added {job['restored']} that YouTube removed along with their copy."
    if job["failed"]:
        msg += f" {job['failed']} failed."
    if job["unrestored"]:
        msg += f" Check these videos, which may be missing from a playlist: {', '.join(job['unrestored'][:10])}."
    if error == "quota":
        msg += " Stopped: YouTube API quota is used up for today."
    elif error == "rate":
        msg += " Stopped: YouTube is rate-limiting requests; try again in a few minutes."
    elif error == "failed":
        msg += " Stopped early because of an unexpected error (see the log)."
    return msg


def _run_yt_dupe_cleanup(playlist_ids: list[str], remove_unavailable: bool, region: str, job: dict) -> None:
    error = None
    try:
        if not get_youtube_oauth_token():
            error = "not_connected"
            return
        for i, playlist_id in enumerate(playlist_ids, 1):
            try:
                _clean_one_playlist(playlist_id, job, remove_unavailable, region)
            except youtube_oauth_service.RateLimited:
                error = "rate"  # transient: don't mark the day's quota exhausted
                break
            except youtube_oauth_service.QuotaExceeded:
                mark_yt_quota_exhausted()
                error = "quota"
                break
            except Exception as exc:  # noqa: BLE001 -- one bad playlist must not sink the rest
                _yt_dupe_bump(job, failed=1)
                LOGGER.warning("[yt-dupes] playlist %s failed: %s", playlist_id, exc)
            _yt_dupe_update(job, {"processed": i})
    except Exception:  # noqa: BLE001
        LOGGER.exception("[yt-dupes] cleanup worker failed")
        error = "failed"
    finally:
        # Always release the job: a stuck "running" would answer every later start with 409 until the app restarts.
        msg = _dupe_summary(job, remove_unavailable, region, error) if error != "not_connected" else None
        if msg:
            LOGGER.info("[yt-dupes] %s", msg)
        _yt_dupe_update(job, {"running": False, "done": True, "error": error, "message": msg})


@router.post("/api/youtube/playlists/dedupe")
async def youtube_playlists_dedupe_route(request: Request):
    """Start removing duplicate videos from the chosen playlists.

    Body: {playlist_ids: [str, ...], remove_unavailable?: bool, region?: "US"}. *region* also removes videos blocked there."""
    body = await request.json()
    ids = body.get("playlist_ids")
    if not isinstance(ids, list):
        return JSONResponse({"ok": False, "error": "playlist_ids required"}, status_code=400)
    playlist_ids = list(dict.fromkeys(str(p).strip() for p in ids if str(p).strip()))
    if not playlist_ids:
        return JSONResponse({"ok": False, "error": "playlist_ids required"}, status_code=400)
    if len(playlist_ids) > _YT_DUPE_MAX_PLAYLISTS:
        return JSONResponse({"ok": False, "error": f"Too many playlists (max {_YT_DUPE_MAX_PLAYLISTS})."}, status_code=400)
    if not get_youtube_oauth_token():
        return JSONResponse({"ok": False, "error": "not_connected"}, status_code=401)
    remove_unavailable = bool(body.get("remove_unavailable"))
    region = str(body.get("region") or "").strip().upper()
    if region and not (len(region) == 2 and region.isalpha()):
        return JSONResponse({"ok": False, "error": "region must be a two-letter country code"}, status_code=400)
    job_id = secrets.token_hex(8)
    with _yt_dupe_jobs_lock:
        job: dict | None = _yt_dupe_jobs.get("job")
        if job is None:
            job = {"running": False}
            _yt_dupe_jobs["job"] = job
        batch = _yt_playlist_batch_job_state()
        if job.get("running") or (batch and batch.get("running")):
            return JSONResponse({"ok": False, "error": "busy"}, status_code=409)
        job.update(
            {
                "running": True,
                "done": False,
                "error": None,
                "unrestored": [],
                "total": len(playlist_ids),
                "processed": 0,
                "removed": 0,
                "unavailable": 0,
                "restored": 0,
                "failed": 0,
                "message": None,
                "job_id": job_id,
            }
        )
    uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(uid, _run_yt_dupe_cleanup, playlist_ids, remove_unavailable, region, job), daemon=True
    ).start()
    return JSONResponse({"ok": True, "started": True, "total": len(playlist_ids), "job_id": job_id})


@router.get("/api/youtube/playlists/dedupe/status")
def youtube_playlists_dedupe_status_route(job_id: str | None = Query(default=None)):
    with _yt_dupe_jobs_lock:
        job = _yt_dupe_jobs.get("job")
        snapshot = dict(job) if job else None
    if snapshot is None or (job_id and snapshot.get("job_id") != job_id):
        return JSONResponse({"ok": True, "running": False, "stale": snapshot is not None})
    return JSONResponse({"ok": True, **snapshot})
