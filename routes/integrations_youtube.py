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
)
from services import tenancy
from services import youtube_oauth as youtube_oauth_service

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
        return RedirectResponse(url="/?message=" + quote_plus(f"YouTube connect failed: {exc}"), status_code=303)
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
        return JSONResponse({"connected": True, "error": str(exc), "playlists": []}, status_code=502)
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
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
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
            existing = youtube_oauth_service.list_playlist_video_ids(token, playlist_id)
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
    with _yt_playlist_batch_jobs_lock:
        if job.get("running"):
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
