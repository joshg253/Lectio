"""Settings surface: profile/email-bcc, the `/settings/all` bulk get/save
(every user- and instance-level setting the Settings modal exposes), the
manual maintenance trigger, auto-refresh, the Global Note, the lazy
Settings -> Feeds panels (folders/stale/failing/fetch-tiers), and the
problematic-feeds triage actions (viewed/acknowledge/unacknowledge/mark-dead/
unmark-dead) -- 14 routes.

Stage 7 of the main.py route-by-URL-prefix split (Plan.md). No ordering
constraint: nothing here touches `_run_automation_after_refresh` or anything
else from the late `services.automation_rules` import, so this module is
imported alongside the plain `routes.compat_*`/`routes.tags`-style modules.

The two route clusters were not contiguous in main.py: `/settings/email-bcc`
through `/settings/maintenance/run-now` sat together, while
`/settings/auto-refresh` through the `/settings/problematic-feeds/*` group
sat ~5,600 lines away, with `/tree/folder-feeds/{folder_id}` (a sidebar
fragment route, not a settings concern despite living in the same block)
sandwiched between `/settings/feeds/panel/{panel_name}` and
`/settings/problematic-feeds/acknowledge` -- it stays in main.py. No third
outlier turned up from grepping the literal path strings across the whole
file.

Only one helper was defined immediately next to a moved route:
`_keep_existing_sensitive` (the masked-secret-field guard `save_all_settings`
uses so a routine re-save of `/settings/all` never blanks a stored secret
just because the masked "••••" placeholder came back). It stays in main.py
and is imported back rather than moving, same "exercised directly as
`main.<name>` by a dedicated test file" reason Stage 3/5/6 kept their own
adjacent helpers -- `tests/unit/test_settings_sensitive_save.py` calls it
directly. Everything else these routes call (`get_meta_connection`,
`set_setting`, the whole family of `get_*`/`SETTING_*` settings accessors,
`FeedInFolder`, `_disambiguate_feed_titles`, `_run_youtube_sync`, and so on)
is pre-existing main.py-resident, widely-shared infrastructure with callers
far outside this cluster (checked across `routes/*.py`, `scripts/*.py`, and
`tests/`, not just main.py) -- none of it moved, all of it is imported back.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import cast
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from main import (
    _ENV_INOREADER_CLIENT_ID,
    _ENV_INOREADER_CLIENT_SECRET,
    AUTO_REFRESH_OPTION_MINUTES,
    AUTO_REFRESH_SETTING_KEY,
    EMAIL_BCC_SETTING_KEY,
    EMAIL_TO_SETTING_KEY,
    GLOBAL_NOTE_SETTING_KEY,
    LECTIO_PUBLIC_URL,
    PROBLEMATIC_FEEDS_LAST_VIEWED_AT_SETTING_KEY,
    PROFILE_EMAIL_SETTING_KEY,
    PROFILE_NAME_SETTING_KEY,
    SETTING_DEFAULT_AUTO_REFRESH_MINUTES,
    SETTING_DEVIANTART_ACCESS_TOKEN,
    SETTING_DEVIANTART_CLIENT_ID,
    SETTING_DEVIANTART_CLIENT_SECRET,
    SETTING_DEVIANTART_FOLDER_NAME,
    SETTING_DEVIANTART_SYNC_STATUS,
    SETTING_DEVIANTART_UNWATCHED_DIRTY,
    SETTING_DEVIANTART_USERNAME,
    SETTING_EMAIL_FROM,
    SETTING_FETCH_HISTORY_MAX_AGE_DAYS,
    SETTING_FLARESOLVERR_URL,
    SETTING_FRESHRSS_PASSWORD,
    SETTING_FRESHRSS_URL,
    SETTING_FRESHRSS_USERNAME,
    SETTING_HIDE_LOCKED_COMICS_GLOBAL,
    SETTING_IMG_CACHE_DAYS,
    SETTING_IMG_CACHE_MAX_DIM,
    SETTING_IMG_TARGET_BYTES,
    SETTING_INOREADER_CLIENT_ID,
    SETTING_INOREADER_CLIENT_SECRET,
    SETTING_INOREADER_EXPORT_DIR,
    SETTING_INSTAPAPER_PASSWORD,
    SETTING_INSTAPAPER_USERNAME,
    SETTING_LOGIN_MAX_FAILURES,
    SETTING_LOGIN_WINDOW_SECONDS,
    SETTING_MAINTENANCE_HOUR,
    SETTING_MINIFLUX_IMPORT_TOKEN,
    SETTING_MINIFLUX_IMPORT_URL,
    SETTING_PINTEREST_OAUTH_CLIENT_ID,
    SETTING_PINTEREST_OAUTH_CLIENT_SECRET,
    SETTING_PINTEREST_OAUTH_REFRESH_TOKEN,
    SETTING_PORTRAIT_IMG_MAX_WIDTH,
    SETTING_PROXY_BODY_IMAGES,
    SETTING_PROXY_MODE,
    SETTING_PROXY_URL,
    SETTING_QUIRE_CLIENT_ID,
    SETTING_QUIRE_CLIENT_SECRET,
    SETTING_QUIRE_PLAN,
    SETTING_QUIRE_PROJECT_NAME,
    SETTING_QUIRE_PROJECT_OID,
    SETTING_QUIRE_RATE_CAP_HOUR,
    SETTING_QUIRE_RATE_CAP_MIN,
    SETTING_QUIRE_USERNAME,
    SETTING_REDDIT_CLIENT_ID,
    SETTING_REDDIT_CLIENT_SECRET,
    SETTING_REDDIT_USERNAME,
    SETTING_RESEND_API_KEY,
    SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID,
    SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
    SETTING_SHARED_REDDIT_CLIENT_ID,
    SETTING_SHARED_REDDIT_CLIENT_SECRET,
    SETTING_SHARED_YT_OAUTH_CLIENT_ID,
    SETTING_SHARED_YT_OAUTH_CLIENT_SECRET,
    SETTING_STAR_SEND_EMAIL,
    SETTING_STAR_SEND_INSTAPAPER,
    SETTING_STAR_SEND_QUIRE,
    SETTING_STAR_SEND_REDDIT_SUBREDDIT,
    SETTING_STAR_SEND_YT_PLAYLIST,
    SETTING_STAR_SEND_YT_PLAYLIST_TITLE,
    SETTING_TAILSCALE_URL,
    SETTING_TOMBSTONE_SWEEP_DAYS,
    SETTING_TTRSS_PASSWORD,
    SETTING_TTRSS_URL,
    SETTING_TTRSS_USERNAME,
    SETTING_TZ_DISPLAY,
    SETTING_YT_API_KEY,
    SETTING_YT_CHANNEL_ID,
    SETTING_YT_EMBED_ACCOUNT_FEATURES,
    SETTING_YT_FOLDER_NAME,
    SETTING_YT_HIDE_MEMBERS_ONLY_GLOBAL,
    SETTING_YT_HIDE_SHORTS_GLOBAL,
    SETTING_YT_HIDE_UNPREMIERED_GLOBAL,
    SETTING_YT_OAUTH_CLIENT_ID,
    SETTING_YT_OAUTH_CLIENT_SECRET,
    SETTING_YT_OAUTH_REFRESH_TOKEN,
    SETTING_YT_QUOTA_CAP,
    UNCATEGORIZED_FOLDER_ID,
    UNCATEGORIZED_FOLDER_NAME,
    FeedInFolder,
    _current_web_user,
    _da_deactivated_list,
    _deviantart_folder_name,
    _disambiguate_feed_titles,
    _is_web_admin,
    _keep_existing_sensitive,
    _load_da_sync_detail,
    _run_daily_maintenance,
    _run_in_user_context,
    _run_youtube_sync,
    app,
    build_read_filter_query,
    build_resume_read_filter_query,
    build_sort_query,
    build_star_only_query,
    delete_setting,
    detect_quire_plan_and_caps,
    disable_feed,
    enable_feed,
    feed_discovery,
    format_datetime_for_ui,
    get_all_reader_feed_urls,
    get_deviantart_credentials,
    get_disabled_feed_urls,
    get_email_contacts,
    get_feed_last_post_dates,
    get_feed_title_map,
    get_feeds_needing_replacement,
    get_fetch_history_max_age_days,
    get_inoreader_credentials,
    get_instance_default_auto_refresh,
    get_login_max_failures,
    get_login_window_seconds,
    get_meta_connection,
    get_meta_structure_snapshot,
    get_pinterest_oauth_credentials,
    get_portrait_img_max_width,
    get_problematic_feeds_cached,
    get_proxy_mode,
    get_push_active_feed_urls,
    get_quire_credentials,
    get_quire_usage_status,
    get_reddit_credentials,
    get_resend_api_key,
    get_resend_from,
    get_root_folder_id,
    get_runtime_setting,
    get_setting,
    get_tombstone_sweep_days,
    get_youtube_oauth_credentials,
    get_yt_api_key,
    get_yt_channel_id,
    get_yt_folder_name,
    get_yt_quota_status,
    hide_locked_comics_global,
    inoreader_connected,
    invalidate_instance_setting_cache,
    invalidate_problematic_feeds_cache,
    is_async_action_request,
    is_quire_configured,
    is_quire_connected,
    normalize_auto_refresh_minutes,
    normalize_read_filter,
    normalize_search_query,
    normalize_tag_value,
    page_fetcher,
    proxy_body_images_enabled,
    quire_project_oid,
    reddit_connected,
    saved_articles_service,
    set_setting,
    templates,
    tenancy,
    youtube_embed_account_features_enabled,
    youtube_hide_members_only_global,
    youtube_hide_shorts_global,
    youtube_hide_unpremiered_global,
    youtube_quota_cap,
)

router = APIRouter()


@router.post("/settings/email-bcc")
def save_email_bcc_route(address: str = Form("")):
    with get_meta_connection() as conn:
        set_setting(conn, EMAIL_BCC_SETTING_KEY, address.strip())
    return JSONResponse({"ok": True})


@router.post("/settings/profile")
def save_profile_route(name: str = Form(""), email: str = Form("")):
    with get_meta_connection() as conn:
        set_setting(conn, PROFILE_NAME_SETTING_KEY, name.strip())
        set_setting(conn, PROFILE_EMAIL_SETTING_KEY, email.strip())
    return JSONResponse({"ok": True})


@router.get("/settings/all")
def get_all_settings():
    """Return all user-configurable settings. Sensitive values are masked if set."""

    def _masked(val: str) -> str:
        return "••••••••" if val else ""

    with get_meta_connection() as conn:
        profile_name = get_setting(conn, PROFILE_NAME_SETTING_KEY) or ""
        profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
        maint_last = get_setting(conn, "maintenance_last_ran_at") or ""
        email_to_default = get_setting(conn, EMAIL_TO_SETTING_KEY) or ""
        # Contacts live in the email_contacts table; filter out the profile email
        # since it's already represented by the synthetic "Me" row in the UI.
        all_contacts = get_email_contacts(conn)
        profile_lower = profile_email.lower()
        contacts = [{"label": c["label"], "address": c["address"]} for c in all_contacts if c["address"].lower() != profile_lower]

    yt_api_key = get_yt_api_key()
    resend_key = get_resend_api_key()
    instapaper_pw = get_runtime_setting(SETTING_INSTAPAPER_PASSWORD)
    da_cid, da_secret = get_deviantart_credentials()
    quire_cid, quire_secret = get_quire_credentials()
    # Lazily detect the Quire plan once (sets the meter caps) if a project is chosen
    # but no plan has been recorded yet — e.g. projects picked before plan detection
    # existed, so users see correct caps without re-picking. Best-effort, ~one call.
    if is_quire_configured() and not get_runtime_setting(SETTING_QUIRE_PLAN):
        detect_quire_plan_and_caps()

    return JSONResponse(
        {
            "profile_name": profile_name,
            "profile_email": profile_email,
            "tz_display": get_runtime_setting(SETTING_TZ_DISPLAY),
            "portrait_img_max_width": get_portrait_img_max_width(),
            "proxy_body_images": proxy_body_images_enabled(),
            # Raw own row ("" = inherit the instance default) vs. the resolved value
            # actually in effect for this user right now — the UI shows the former
            # as the select's value and the latter as a hint when it's "inherit".
            "proxy_mode_own": get_runtime_setting(SETTING_PROXY_MODE, ""),
            "proxy_mode_effective": get_proxy_mode(),
            "tz_default": os.environ.get("TZ") or "UTC",
            "maintenance_hour": get_runtime_setting(SETTING_MAINTENANCE_HOUR),
            "maintenance_last_ran_at": maint_last,
            "yt_api_key_set": bool(yt_api_key),
            "yt_api_key_masked": _masked(yt_api_key),
            "yt_channel_id": get_yt_channel_id(),
            "yt_folder_name": get_yt_folder_name(),
            "yt_embed_account_features": youtube_embed_account_features_enabled(),
            "yt_hide_shorts_global": youtube_hide_shorts_global(),
            "yt_hide_unpremiered_global": youtube_hide_unpremiered_global(),
            "yt_hide_members_only_global": youtube_hide_members_only_global(),
            "hide_locked_comics_global": hide_locked_comics_global(),
            "yt_quota": get_yt_quota_status(),
            "yt_quota_cap": youtube_quota_cap(),
            "star_send_instapaper": get_runtime_setting(SETTING_STAR_SEND_INSTAPAPER, "0") == "1",
            "star_send_yt_playlist": get_runtime_setting(SETTING_STAR_SEND_YT_PLAYLIST) or "",
            "star_send_yt_playlist_title": get_runtime_setting(SETTING_STAR_SEND_YT_PLAYLIST_TITLE) or "",
            "star_send_email": get_runtime_setting(SETTING_STAR_SEND_EMAIL) or "",
            "yt_oauth_client_id": get_runtime_setting(SETTING_YT_OAUTH_CLIENT_ID, ""),
            "yt_oauth_client_secret_set": bool(get_runtime_setting(SETTING_YT_OAUTH_CLIENT_SECRET)),
            "yt_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_YT_OAUTH_CLIENT_SECRET, "")),
            "yt_oauth_configured": all(get_youtube_oauth_credentials()),
            "yt_oauth_connected": bool(get_runtime_setting(SETTING_YT_OAUTH_REFRESH_TOKEN)),
            "shared_yt_oauth_client_id": get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_ID, ""),
            "shared_yt_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET)),
            "shared_yt_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET, "")),
            "pinterest_oauth_client_id": get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_ID, ""),
            "pinterest_oauth_client_secret_set": bool(get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_SECRET)),
            "pinterest_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_PINTEREST_OAUTH_CLIENT_SECRET, "")),
            "pinterest_oauth_configured": all(get_pinterest_oauth_credentials()),
            "pinterest_oauth_connected": bool(get_runtime_setting(SETTING_PINTEREST_OAUTH_REFRESH_TOKEN)),
            "shared_pinterest_oauth_client_id": get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID, ""),
            # Miniflux / FreshRSS / tt-rss migrations
            "miniflux_import_url": get_runtime_setting(SETTING_MINIFLUX_IMPORT_URL, ""),
            "miniflux_import_token_set": bool(get_runtime_setting(SETTING_MINIFLUX_IMPORT_TOKEN)),
            "miniflux_import_token_masked": _masked(get_runtime_setting(SETTING_MINIFLUX_IMPORT_TOKEN, "")),
            "freshrss_url": get_runtime_setting(SETTING_FRESHRSS_URL, ""),
            "freshrss_username": get_runtime_setting(SETTING_FRESHRSS_USERNAME, ""),
            "freshrss_password_set": bool(get_runtime_setting(SETTING_FRESHRSS_PASSWORD)),
            "freshrss_password_masked": _masked(get_runtime_setting(SETTING_FRESHRSS_PASSWORD, "")),
            "ttrss_url": get_runtime_setting(SETTING_TTRSS_URL, ""),
            "ttrss_username": get_runtime_setting(SETTING_TTRSS_USERNAME, ""),
            "ttrss_password_set": bool(get_runtime_setting(SETTING_TTRSS_PASSWORD)),
            "ttrss_password_masked": _masked(get_runtime_setting(SETTING_TTRSS_PASSWORD, "")),
            # Inoreader migration
            "inoreader_client_id": get_runtime_setting(SETTING_INOREADER_CLIENT_ID, _ENV_INOREADER_CLIENT_ID),
            "inoreader_client_secret_set": bool(get_runtime_setting(SETTING_INOREADER_CLIENT_SECRET, _ENV_INOREADER_CLIENT_SECRET)),
            "inoreader_client_secret_masked": _masked(get_runtime_setting(SETTING_INOREADER_CLIENT_SECRET, _ENV_INOREADER_CLIENT_SECRET)),
            "inoreader_configured": bool(all(get_inoreader_credentials())),
            "inoreader_connected": inoreader_connected(),
            "inoreader_export_dir": get_runtime_setting(SETTING_INOREADER_EXPORT_DIR, ""),
            "shared_pinterest_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET)),
            "shared_pinterest_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET, "")),
            # Reddit OAuth
            "reddit_client_id": get_runtime_setting(SETTING_REDDIT_CLIENT_ID, ""),
            "reddit_client_secret_set": bool(get_runtime_setting(SETTING_REDDIT_CLIENT_SECRET)),
            "reddit_client_secret_masked": _masked(get_runtime_setting(SETTING_REDDIT_CLIENT_SECRET, "")),
            "reddit_configured": all(get_reddit_credentials()),
            "reddit_connected": reddit_connected(),
            "reddit_username": get_runtime_setting(SETTING_REDDIT_USERNAME, ""),
            "shared_reddit_client_id": get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_ID, ""),
            "shared_reddit_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET)),
            "shared_reddit_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET, "")),
            "star_send_reddit_subreddit": get_runtime_setting(SETTING_STAR_SEND_REDDIT_SUBREDDIT, ""),
            "resend_api_key_set": bool(resend_key),
            "resend_api_key_masked": _masked(resend_key),
            "email_from": get_resend_from(),
            "instapaper_username": get_runtime_setting(SETTING_INSTAPAPER_USERNAME),
            "instapaper_password_set": bool(instapaper_pw),
            "instapaper_password_masked": _masked(instapaper_pw),
            "deviantart_client_id": da_cid,
            "deviantart_client_secret_set": bool(da_secret),
            "deviantart_client_secret_masked": _masked(da_secret),
            "deviantart_connected": bool(get_runtime_setting(SETTING_DEVIANTART_ACCESS_TOKEN)),
            "deviantart_username": get_runtime_setting(SETTING_DEVIANTART_USERNAME),
            "deviantart_sync_status": get_runtime_setting(SETTING_DEVIANTART_SYNC_STATUS),
            "deviantart_sync_detail": _load_da_sync_detail(),
            "deviantart_unwatched_dirty": get_runtime_setting(SETTING_DEVIANTART_UNWATCHED_DIRTY) == "1",
            "deviantart_deactivated": _da_deactivated_list(),
            "deviantart_folder_name": _deviantart_folder_name(),
            "quire_client_id": quire_cid,
            "quire_client_secret_set": bool(quire_secret),
            "quire_client_secret_masked": _masked(quire_secret),
            "quire_connected": is_quire_connected(),
            "quire_username": get_runtime_setting(SETTING_QUIRE_USERNAME),
            "quire_project_oid": quire_project_oid(),
            "quire_project_name": get_runtime_setting(SETTING_QUIRE_PROJECT_NAME),
            "quire_usage": get_quire_usage_status(),
            "quire_plan": get_runtime_setting(SETTING_QUIRE_PLAN),
            "star_send_quire": get_runtime_setting(SETTING_STAR_SEND_QUIRE, "0") == "1",
            "contacts": contacts,
            "email_to_default": email_to_default,
            "public_url": LECTIO_PUBLIC_URL,
            "fetch_history_max_age_days": get_fetch_history_max_age_days(),
            "tombstone_sweep_days": get_tombstone_sweep_days(),
            "login_max_failures": get_login_max_failures(),
            "login_window_seconds": get_login_window_seconds(),
            "instance_auto_refresh": get_instance_default_auto_refresh(),
        }
    )


@router.post("/settings/all")
async def save_all_settings(request: Request):
    """Save any subset of user-configurable settings. Empty string clears a value."""
    import json as _json

    body = await request.json()

    _SENSITIVE = {
        SETTING_RESEND_API_KEY,
        SETTING_YT_API_KEY,
        SETTING_INSTAPAPER_PASSWORD,
        SETTING_DEVIANTART_CLIENT_SECRET,
        SETTING_QUIRE_CLIENT_SECRET,
        SETTING_YT_OAUTH_CLIENT_SECRET,
        SETTING_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_YT_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_INOREADER_CLIENT_SECRET,
        SETTING_REDDIT_CLIENT_SECRET,
        SETTING_SHARED_REDDIT_CLIENT_SECRET,
        SETTING_MINIFLUX_IMPORT_TOKEN,
        SETTING_FRESHRSS_PASSWORD,
        SETTING_TTRSS_PASSWORD,
    }
    _ALLOWED = {
        PROFILE_NAME_SETTING_KEY,
        PROFILE_EMAIL_SETTING_KEY,
        SETTING_TZ_DISPLAY,
        SETTING_PORTRAIT_IMG_MAX_WIDTH,
        SETTING_PROXY_BODY_IMAGES,
        SETTING_PROXY_URL,
        SETTING_PROXY_MODE,
        SETTING_TAILSCALE_URL,
        SETTING_FLARESOLVERR_URL,
        SETTING_MAINTENANCE_HOUR,
        SETTING_IMG_CACHE_DAYS,
        SETTING_IMG_CACHE_MAX_DIM,
        SETTING_IMG_TARGET_BYTES,
        SETTING_YT_API_KEY,
        SETTING_YT_CHANNEL_ID,
        SETTING_YT_FOLDER_NAME,
        SETTING_YT_EMBED_ACCOUNT_FEATURES,
        SETTING_YT_HIDE_SHORTS_GLOBAL,
        SETTING_YT_HIDE_UNPREMIERED_GLOBAL,
        SETTING_YT_HIDE_MEMBERS_ONLY_GLOBAL,
        SETTING_YT_QUOTA_CAP,
        SETTING_HIDE_LOCKED_COMICS_GLOBAL,
        SETTING_YT_OAUTH_CLIENT_ID,
        SETTING_YT_OAUTH_CLIENT_SECRET,
        SETTING_STAR_SEND_INSTAPAPER,
        SETTING_STAR_SEND_YT_PLAYLIST,
        SETTING_STAR_SEND_YT_PLAYLIST_TITLE,
        SETTING_STAR_SEND_EMAIL,
        SETTING_RESEND_API_KEY,
        SETTING_EMAIL_FROM,
        SETTING_INSTAPAPER_USERNAME,
        SETTING_INSTAPAPER_PASSWORD,
        SETTING_DEVIANTART_CLIENT_ID,
        SETTING_DEVIANTART_CLIENT_SECRET,
        SETTING_DEVIANTART_FOLDER_NAME,
        SETTING_QUIRE_CLIENT_ID,
        SETTING_QUIRE_CLIENT_SECRET,
        SETTING_QUIRE_PROJECT_OID,
        SETTING_QUIRE_PROJECT_NAME,
        SETTING_STAR_SEND_QUIRE,
        SETTING_QUIRE_RATE_CAP_MIN,
        SETTING_QUIRE_RATE_CAP_HOUR,
        SETTING_PINTEREST_OAUTH_CLIENT_ID,
        SETTING_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_YT_OAUTH_CLIENT_ID,
        SETTING_SHARED_YT_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID,
        SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_INOREADER_CLIENT_ID,
        SETTING_INOREADER_CLIENT_SECRET,
        SETTING_INOREADER_EXPORT_DIR,
        SETTING_MINIFLUX_IMPORT_URL,
        SETTING_MINIFLUX_IMPORT_TOKEN,
        SETTING_FRESHRSS_URL,
        SETTING_FRESHRSS_USERNAME,
        SETTING_FRESHRSS_PASSWORD,
        SETTING_TTRSS_URL,
        SETTING_TTRSS_USERNAME,
        SETTING_TTRSS_PASSWORD,
        SETTING_REDDIT_CLIENT_ID,
        SETTING_REDDIT_CLIENT_SECRET,
        SETTING_SHARED_REDDIT_CLIENT_ID,
        SETTING_SHARED_REDDIT_CLIENT_SECRET,
        SETTING_STAR_SEND_REDDIT_SUBREDDIT,
        SETTING_FETCH_HISTORY_MAX_AGE_DAYS,
        SETTING_TOMBSTONE_SWEEP_DAYS,
        SETTING_LOGIN_MAX_FAILURES,
        SETTING_LOGIN_WINDOW_SECONDS,
        SETTING_DEFAULT_AUTO_REFRESH_MINUTES,
        "email_contacts",
        EMAIL_TO_SETTING_KEY,
    }
    # Instance-level config — only admins may change it (in multi mode). Non-admin
    # requests silently drop these keys, even if the client sends them.
    _ADMIN_ONLY = {
        SETTING_RESEND_API_KEY,
        SETTING_EMAIL_FROM,
        SETTING_PROXY_URL,
        SETTING_TAILSCALE_URL,
        SETTING_FLARESOLVERR_URL,
        SETTING_MAINTENANCE_HOUR,
        SETTING_IMG_CACHE_DAYS,
        SETTING_IMG_CACHE_MAX_DIM,
        SETTING_IMG_TARGET_BYTES,
        SETTING_SHARED_YT_OAUTH_CLIENT_ID,
        SETTING_SHARED_YT_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID,
        SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
        SETTING_SHARED_REDDIT_CLIENT_ID,
        SETTING_SHARED_REDDIT_CLIENT_SECRET,
        SETTING_FETCH_HISTORY_MAX_AGE_DAYS,
        SETTING_TOMBSTONE_SWEEP_DAYS,
        SETTING_LOGIN_MAX_FAILURES,
        SETTING_LOGIN_WINDOW_SECONDS,
        SETTING_DEFAULT_AUTO_REFRESH_MINUTES,
        SETTING_INOREADER_EXPORT_DIR,
    }
    is_admin = _is_web_admin(_current_web_user(request))

    # Detect a YouTube fill-in so we can kick off an immediate sync (rather than
    # waiting for daily maintenance) when it goes from unconfigured -> configured.
    yt_configured_before = bool(get_yt_api_key() and get_yt_channel_id())
    quire_project_before = quire_project_oid()

    with get_meta_connection() as conn:
        for key, value in body.items():
            if key not in _ALLOWED:
                continue
            if key in _ADMIN_ONLY and not is_admin:
                continue
            if key == "email_contacts":
                # Contacts are stored in the email_contacts table, not app_settings.
                # The payload is a JSON array of {label, address} objects.
                try:
                    incoming = _json.loads(str(value)) if isinstance(value, str) else list(value)
                except Exception:
                    incoming = []
                profile_email_lower = (get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or "").lower()
                # Clear all existing contacts, then re-insert the full list.
                # Keep entries whose address matches profile_email out — they're synthetic "Me".
                conn.execute("DELETE FROM email_contacts")
                for c in incoming:
                    addr = str(c.get("address") or "").strip()
                    label = str(c.get("label") or "").strip()
                    if not addr or addr.lower() == profile_email_lower:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO email_contacts (label, address) VALUES (?, ?)",
                        (label, addr),
                    )
                continue
            str_val = str(value).strip() if value is not None else ""
            if _keep_existing_sensitive(key, str_val, _SENSITIVE):
                continue
            if str_val:
                set_setting(conn, key, str_val)
            else:
                delete_setting(conn, key)
    # Instance-level values may have changed — drop the TTL'd admin-tier cache
    # so background threads (maintenance loop, image eviction) see them now.
    invalidate_instance_setting_cache()

    # Newly-configured YouTube → sync now, in the configuring user's context.
    if not yt_configured_before and get_yt_api_key() and get_yt_channel_id():
        threading.Thread(
            target=_run_in_user_context,
            args=(tenancy.current_user_id(), _run_youtube_sync),
            daemon=True,
            name="youtube-sync-on-config",
        ).start()

    # Quire destination project changed → detect its org's plan and align the
    # rate-meter caps (Free/Pro/Premium) to it, in the configuring user's context.
    if quire_project_oid() and quire_project_oid() != quire_project_before:
        threading.Thread(
            target=_run_in_user_context,
            args=(tenancy.current_user_id(), detect_quire_plan_and_caps),
            daemon=True,
            name="quire-plan-detect",
        ).start()

    return JSONResponse({"ok": True})


@router.post("/settings/maintenance/run-now")
def run_maintenance_now(request: Request):
    """Trigger daily maintenance immediately. Admin-only."""
    if not _is_web_admin(_current_web_user(request)):
        return JSONResponse({"ok": False, "error": "Admins only."}, status_code=403)
    threading.Thread(target=_run_daily_maintenance, daemon=True, name="maintenance-manual").start()
    return JSONResponse({"ok": True, "message": "Maintenance started in background."})


@router.post("/settings/auto-refresh")
def update_auto_refresh_setting(
    refresh_minutes: int = Form(...),
    folder_id: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    if refresh_minutes not in AUTO_REFRESH_OPTION_MINUTES:
        message = "Invalid auto-refresh interval."
    else:
        normalized_minutes = normalize_auto_refresh_minutes(refresh_minutes)
        with get_meta_connection() as conn:
            set_setting(conn, AUTO_REFRESH_SETTING_KEY, str(normalized_minutes))
        app.state.auto_refresh_minutes = normalized_minutes
        app.state.last_scheduled_refresh_started_at = time.monotonic()
        if normalized_minutes <= 0:
            message = "Auto-refresh disabled."
        else:
            message = f"Auto-refresh set to {normalized_minutes // 60}h."

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    return RedirectResponse(
        url=(f"/?folder_id={folder_id}{list_feed_query}{tag_query}&message={quote_plus(message)}"),
        status_code=303,
    )


@router.get("/settings/global-note")
def get_global_note_setting():
    """Return the current Global Note so the modal can pull the latest value on
    open — an edit made in another browser/tab shows without a full page reload."""
    with get_meta_connection() as conn:
        note_text = get_setting(conn, GLOBAL_NOTE_SETTING_KEY) or ""
    return JSONResponse({"ok": True, "note_text": note_text})


@router.post("/settings/global-note")
def update_global_note_setting(
    request: Request,
    note_text: str = Form(default=""),
    folder_id: int | None = Form(default=None),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    q: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_query = normalize_search_query(q)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    trimmed_note = note_text.strip()

    with get_meta_connection() as conn:
        set_setting(conn, GLOBAL_NOTE_SETTING_KEY, trimmed_note)

    if is_async_action_request(request, "lectio-global-note-save"):
        return JSONResponse(
            {
                "ok": True,
                "message": "Note saved.",
                "note_text": trimmed_note,
            }
        )

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    q_query = f"&q={quote_plus(normalized_query)}" if normalized_query else ""
    target_folder_id = folder_id
    if target_folder_id is None:
        with get_meta_connection() as conn:
            target_folder_id = get_root_folder_id(conn)
    return RedirectResponse(
        url=(
            f"/?folder_id={target_folder_id}{list_feed_query}{tag_query}"
            f"{build_sort_query(sort_by, sort_dir)}"
            f"{build_read_filter_query(read_filter)}"
            f"{star_only_query}{resume_read_filter_query}{q_query}"
            f"&message={quote_plus('Note saved.')}"
        ),
        status_code=303,
    )


@router.post("/settings/problematic-feeds/viewed")
def mark_problematic_feeds_viewed(request: Request):
    viewed_at = time.time()
    with get_meta_connection() as conn:
        set_setting(conn, PROBLEMATIC_FEEDS_LAST_VIEWED_AT_SETTING_KEY, str(viewed_at))

    if is_async_action_request(request, "lectio-problematic-feeds-viewed"):
        return JSONResponse({"ok": True, "viewed_at": viewed_at})

    return RedirectResponse(url="/", status_code=303)


@router.get("/settings/feeds/panel/{panel_name}")
def settings_feeds_panel_fragment(request: Request, panel_name: str) -> Response:
    """HTML fragments for the heavyweight Settings → Feeds panels.

    The folders table, the stale list, and the failing-feeds list each render
    a row per feed — megabytes of (mostly hidden) markup at thousands of
    feeds — so index.html ships lazy containers instead and the client
    fetches these on first open of the Feeds tab / Stale view / Failing view.
    Markup matches what index.html used to inline; all row interactions are
    event-delegated, so injection needs no JS hooks.
    """
    if panel_name not in {"folders", "stale", "failing", "fetch-tiers"}:
        return Response(status_code=404)
    with get_meta_connection() as conn:
        snapshot = get_meta_structure_snapshot(conn)
        raw_folder_rows = cast(list[dict], snapshot["raw_folder_rows"])
        direct_feed_urls_by_folder = cast(dict[int, list[str]], snapshot["direct_feed_urls_by_folder"])
        all_feed_urls = cast(set[str], snapshot["all_feed_urls"])
        root_id = cast(int, snapshot["root_id"])
        disabled_feed_urls = get_disabled_feed_urls(conn)
        problematic_feeds = get_problematic_feeds_cached(conn)

    # Same virtual "Uncategorized" derivation as the home route: reader feeds
    # in no folder, minus the local Saved Articles feed for display purposes.
    all_reader_feed_urls = get_all_reader_feed_urls()
    uncategorized_feed_urls = all_reader_feed_urls - all_feed_urls
    _uncat_display_urls = uncategorized_feed_urls - {saved_articles_service.SAVED_FEED_URL}
    direct_feed_urls_by_folder = dict(direct_feed_urls_by_folder)
    direct_feed_urls_by_folder[UNCATEGORIZED_FOLDER_ID] = sorted(_uncat_display_urls)
    feed_title_map = get_feed_title_map()
    folder_rows: list[dict] = [dict(row) for row in raw_folder_rows]
    if uncategorized_feed_urls:
        folder_rows.append(
            {
                "id": UNCATEGORIZED_FOLDER_ID,
                "name": UNCATEGORIZED_FOLDER_NAME,
                "cadence_minutes": None,
                "depth": 1,
                "path": UNCATEGORIZED_FOLDER_NAME,
                "feed_count": len(_uncat_display_urls),
                "virtual": True,
            }
        )

    if panel_name == "stale":
        # Every active feed ranked by how long ago its newest post is, oldest
        # first. Feeds that have never posted sort to the very top so they
        # surface for pruning.
        feed_to_folder: dict[str, int] = {}
        for row in folder_rows:
            for url in direct_feed_urls_by_folder.get(int(row["id"]), []):
                feed_to_folder[url] = int(row["id"])
        folder_name_by_id = {int(row["id"]): str(row["name"]) for row in folder_rows}
        last_post_dates = get_feed_last_post_dates()
        _now_utc = datetime.now(timezone.utc)
        stale_feeds = []
        for url in all_reader_feed_urls:
            if url in disabled_feed_urls:
                continue
            last_dt = last_post_dates.get(url)
            fid = feed_to_folder.get(url)
            stale_feeds.append(
                {
                    "feed_url": url,
                    "feed_title": feed_title_map.get(url, url),
                    "folder_id": fid,
                    "folder_name": folder_name_by_id.get(fid, UNCATEGORIZED_FOLDER_NAME) if fid is not None else UNCATEGORIZED_FOLDER_NAME,
                    "last_post": format_datetime_for_ui(last_dt) if last_dt else None,
                    "last_post_sort": last_dt.timestamp() if last_dt else 0.0,
                    "days_since": (_now_utc - last_dt).days if last_dt else None,
                }
            )
        stale_feeds.sort(key=lambda x: cast(float, x["last_post_sort"]))
        html = templates.env.get_template("_settings_feeds_stale.html").render(
            {
                "stale_feeds": stale_feeds,
                # Unsubscribe fallback target for unfoldered feeds. The inline
                # panel used the currently-selected folder; a fragment has no
                # selection, so fall back to the root ("All Feeds") folder.
                "selected_folder_id": root_id,
            }
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    if panel_name == "fetch-tiers":
        # How many feeds are routing through each outbound-fetch escalation
        # tier right now — added 2026-08-31 so Josh can see how much his paid
        # VPN and home IP (Tailscale) are actually being exposed to, rather
        # than that being invisible until a feed's Properties happened to be
        # opened. Same three tables the escalation callbacks in
        # services/feed_refresh.py write to (see add_proxy_feed /
        # add_tailscale_feed / add_flaresolverr_feed); each row's own
        # ``reason`` and ``flagged_at`` come from whichever escalation attempt
        # first flagged it.
        def _tier_rows(table: str) -> list[dict]:
            rows = conn.execute(
                f"SELECT feed_url, reason, flagged_at FROM {table} ORDER BY flagged_at DESC"  # noqa: S608 -- table is one of 3 literals below, never user input
            ).fetchall()
            out = []
            for row in rows:
                url = str(row["feed_url"])
                out.append(
                    {
                        "feed_url": url,
                        "feed_title": feed_title_map.get(url, url),
                        "reason": row["reason"],
                        "flagged_at": row["flagged_at"],
                    }
                )
            return out

        with get_meta_connection() as conn:
            proxy_rows = _tier_rows("proxy_feeds")
            tailscale_rows = _tier_rows("tailscale_feeds")
            flaresolverr_rows = _tier_rows("flaresolverr_feeds")
        # The page-fetch ladder (services/page_fetch.py — tag/lead-image
        # scraping and the saved-article re-fetch path) is a second,
        # independent consumer of the same proxy/FlareSolverr backends, with
        # its own in-memory per-host memory instead of these three tables.
        # Surfaced here too, or it would be invisible next to the feed ladder
        # this panel already exists to make visible — see Plan.md.
        page_fetch_rows = sorted(
            page_fetcher._state.snapshot(),
            key=lambda row: (not row["blocked"], row["host"]),
        )
        html = templates.env.get_template("_settings_feeds_fetch_tiers.html").render(
            {
                "proxy_rows": proxy_rows,
                "tailscale_rows": tailscale_rows,
                "flaresolverr_rows": flaresolverr_rows,
                "page_fetch_rows": page_fetch_rows,
                "selected_folder_id": root_id,
            }
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    if panel_name == "failing":
        # Same feed_title/needs_replacement/can_suggest_migration augmentation
        # _home_inner applies before splitting problematic_feeds into the
        # three category lists — up to LECTIO_FAILING_FEEDS_LIMIT (default
        # 500) rows of markup, so this is the one served on demand instead of
        # inline with the page.
        needs_replacement_urls = get_feeds_needing_replacement()
        for pf in problematic_feeds:
            pf_url = cast(str, pf["feed_url"])
            pf["feed_title"] = feed_title_map.get(pf_url, pf_url)
            pf["needs_replacement"] = pf_url in needs_replacement_urls
            pf["can_suggest_migration"] = feed_discovery.is_known_dead_end_host(pf_url)
        needs_replacement_feeds = [pf for pf in problematic_feeds if pf["needs_replacement"]]
        active_problem_feeds = [pf for pf in problematic_feeds if not pf["needs_replacement"]]
        failing_feeds = [pf for pf in active_problem_feeds if not pf.get("acknowledged_at")]
        acked_feeds = [pf for pf in active_problem_feeds if pf.get("acknowledged_at")]
        html = templates.env.get_template("_settings_feeds_failing.html").render(
            {
                "failing_feeds": failing_feeds,
                "acked_feeds": acked_feeds,
                "needs_replacement_feeds": needs_replacement_feeds,
                "selected_folder_id": root_id,
            }
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    error_feed_urls: set[str] = {cast(str, pf["feed_url"]) for pf in problematic_feeds if not pf.get("acknowledged_at")}
    # Like the sidebar's feeds_by_folder but including disabled feeds
    # (flagged), so the settings table can show them greyed out.
    settings_feeds_by_folder: dict[int, list[FeedInFolder]] = {}
    folder_failing_counts: dict[int, int] = {}
    for row in folder_rows:
        folder_row_id = int(row["id"])
        urls = direct_feed_urls_by_folder.get(folder_row_id, [])
        folder_feeds = [
            FeedInFolder(
                url=url,
                title=feed_title_map.get(url, url),
                icon_url=None,  # settings table shows no favicons
                unread_count=0,  # settings table shows no unread badges
                has_error=url in error_feed_urls,
                disabled=url in disabled_feed_urls,
            )
            for url in urls
        ]
        # Active feeds first (alphabetical), disabled greyed at the bottom.
        folder_feeds.sort(key=lambda f: (f.disabled, f.title.casefold()))
        _disambiguate_feed_titles(folder_feeds)
        settings_feeds_by_folder[folder_row_id] = folder_feeds
        # Failing counts consider active feeds only, matching the sidebar.
        failing = sum(1 for f in folder_feeds if f.has_error and not f.disabled)
        if failing:
            folder_failing_counts[folder_row_id] = failing
    html = templates.env.get_template("_settings_feeds_folders.html").render(
        {
            "folder_rows": folder_rows,
            "settings_feeds_by_folder": settings_feeds_by_folder,
            "folder_failing_counts": folder_failing_counts,
            "push_feed_urls": get_push_active_feed_urls(),
        }
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.post("/settings/problematic-feeds/acknowledge")
def acknowledge_problematic_feed(request: Request, feed_url: str = Form(...)):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE feed_failure_state SET acknowledged_at = ? WHERE feed_url = ?",
            (time.time(), feed_url),
        )
    invalidate_problematic_feeds_cache()
    if is_async_action_request(request, "lectio-problem-feed-ack"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=303)


@router.post("/settings/problematic-feeds/unacknowledge")
def unacknowledge_problematic_feed(request: Request, feed_url: str = Form(...)):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE feed_failure_state SET acknowledged_at = NULL WHERE feed_url = ?",
            (feed_url,),
        )
    invalidate_problematic_feeds_cache()
    if is_async_action_request(request, "lectio-problem-feed-unack"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=303)


@router.post("/settings/problematic-feeds/mark-dead")
def mark_feed_needs_replacement(request: Request, feed_url: str = Form(...)):
    """Triage a dead feed as 'needs a replacement': disable updates (stop hitting
    it) and move it to the Needs-replacement worklist. The feed + its entries and
    curation are kept, so a Change-URL swap can reattach a working source."""
    feed_url = feed_url.strip()
    if not feed_url:
        return JSONResponse({"ok": False, "error": "Missing feed URL."}, status_code=400)
    disable_feed(feed_url)  # stop fetching
    with get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO feeds_needing_replacement (feed_url) VALUES (?)",
            (feed_url,),
        )
    invalidate_problematic_feeds_cache()
    if is_async_action_request(request, "lectio-problem-feed-mark-dead"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=303)


@router.post("/settings/problematic-feeds/unmark-dead")
def unmark_feed_needs_replacement(request: Request, feed_url: str = Form(...)):
    """Undo a needs-replacement flag: re-enable updates and drop it from the
    worklist (back to the Failing list on its next failed fetch)."""
    feed_url = feed_url.strip()
    if not feed_url:
        return JSONResponse({"ok": False, "error": "Missing feed URL."}, status_code=400)
    enable_feed(feed_url)  # resume fetching
    with get_meta_connection() as conn:
        conn.execute("DELETE FROM feeds_needing_replacement WHERE feed_url = ?", (feed_url,))
    invalidate_problematic_feeds_cache()
    if is_async_action_request(request, "lectio-problem-feed-unmark-dead"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=303)
