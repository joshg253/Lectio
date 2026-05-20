# Lectio Plan

This file is the backlog and staging area for future work.

## Up next (ordered)

1. ~~**Email Article**~~ — share button in entry toolbar; sends link + summary via Resend. ✓
2. ~~**Feed Troubleshooter / Advanced Properties**~~ — right-click → "Properties…" panel with feed metadata, health/post counts, and image display controls. Three per-feed flags (show as thumbnail, show in article, caption mode with junk-suppression heuristics) plus strategy comparison (og_scrape / inline / media_rss / youtube) with cached results and a Refresh button. ✓
3. ~~**RSS Auto-Discovery**~~ — when adding a feed URL, auto-detect hidden RSS/Atom links on the page before failing. ✓
4. ~~**Text Highlighting**~~ — keyword-based title highlighting at global, per-folder, and per-feed scope. Keywords and colors managed via a modal (main menu + right-click on folder/feed). Highlights apply client-side on post list titles and the entry pane title. ✓
5. ~~**Automation Engine**~~ ✓ — unified rule system. UI, storage, and server-side execution all done:
   - *Highlight* rules: client-side title highlighting, scoped global/folder/feed.
   - *Mark as Read* rules: fire at fetch time for each refreshed feed in scope; logged to `rule_run_log` with `trigger='auto'`.
   - *Deduplicate* rules: fire once per rule per refresh cycle when ≥1 in-scope feed was refreshed; logged to `rule_run_log`.
   - Email Article rules: UI + manual send done. Server-side automation (batch digest, rate limiting, Cc application) pending — see below.
6. ~~**Profile**~~ ✓ UI — name + email in `app_settings`. Server-side Cc application on email rules pending.
7. ~~**Deduplicate Rule**~~ ✓ — UI, dry-run, and server-side execution at fetch time all done.
8. **Settings dialog + Daily Maintenance** ← *in progress*
   - Replace hamburger menu + Profile modal with a unified Settings dialog (user avatar button in topbar).
   - Tabs: *Profile* (name, email), *Settings* (timezone display pref, maintenance hour), *Contacts* (email recipients list with default), *Email* (Resend API key + from address), *Integrations* (YouTube config + Instapaper credentials).
   - Move `YOUTUBE_API_KEY`, `YOUTUBE_CHANNEL_ID`, `YOUTUBE_FOLDER_NAME`, `RESEND_API_KEY`, `LECTIO_EMAIL_FROM` from `.env` into DB-backed settings (env still works as fallback). Remove `LECTIO_EMAIL_TO` (replaced by Rules).
   - `YOUTUBE_SYNC_HOUR` → `LECTIO_MAINTENANCE_HOUR` (default 3 am): single daily job that runs VACUUM on meta/thumb/archive DBs, prunes old rule run log rows (90-day retention), purges orphaned DB rows (strategy cache, display prefs, failure state for removed feeds), and syncs YouTube subscriptions.
   - Auth (`LECTIO_USERNAME` / `LECTIO_PASSWORD` / `LECTIO_SECRET_KEY`) stays in `.env`; change-password UI deferred to multi-user work.
9. **Instapaper integration** — "Save to Instapaper" button in the entry toolbar (next to email). Credentials (username + password) stored in Integrations settings. Backend endpoint does a `POST https://www.instapaper.com/api/add` with Basic Auth; frontend shows a toast.
10. **Email Automation** — server-side Email Article rule execution at fetch time. Pending items:
    - *Immediately* mode: fire at fetch time; cap per-run (e.g. 10/run); per-recipient cooldown (e.g. 1 article/5 min from same feed).
    - *Batch digest* mode: queue table; flush on N articles OR daily maintenance window.
    - Global daily counter in `app_settings`; hard-stop at ~80/day.
    - *Cc me* checkbox: apply `profile_email` as Cc on every send when checked.
11. **Feed Properties v2 — Tuning tab** ✓ (tab added; advanced content-pull and YouTube-strategy scoping still pending)
    - Add a way to pull in content when none of the 4 strategies produce what's expected (e.g. manual URL override, custom scrape rule, or fallback chain config).
    - Auto-hide or disable the "YouTube" strategy option when the feed is not in the YouTube folder (or is not a youtube.com feed).
12. **Page-to-Feed (Scraping)** — for truly feedless pages, generate synthetic feeds via scraping or change-detection (RSSHub, FetchRSS, or built-in scraper).

## Backburner

- **Resurface / GUID-churn suppression** — publishers sometimes change entry GUIDs (CMS migrations, permalink changes, plugin rebuilds), causing batches of already-read articles to reappear as new. Mitigation: when a new entry arrives whose title + approximate date matches a known read entry in the same feed, auto-mark it read. Overlaps with cross-feed dedup (slug/title matching). Distinct from `updated`-timestamp changes, which don't affect read state because the GUID is unchanged.


- Per-user vs. shared thumb cache (only relevant if multi-user is added).
- Archive caps for starred entries.
- Multi-user support / auth refactor.
- YunoHost or other packaging.
- PWA features.
