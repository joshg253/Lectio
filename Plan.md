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
8. ~~**Settings dialog + Daily Maintenance**~~ ✓
   - Unified Settings dialog (user avatar button in topbar) with tabs: *Profile* (name, email), *Settings* (timezone display pref, maintenance hour), *Contacts* (email recipients list with default), *Email* (Resend API key + from address), *Integrations* (YouTube config + Instapaper credentials).
   - All previously env-only keys (`YOUTUBE_API_KEY`, `YOUTUBE_CHANNEL_ID`, `YOUTUBE_FOLDER_NAME`, `RESEND_API_KEY`, `LECTIO_EMAIL_FROM`) now editable in the DB-backed Settings dialog; env still works as fallback.
   - `LECTIO_MAINTENANCE_HOUR` (default 3 am): daily job runs VACUUM on meta/thumb/archive DBs, prunes old rule run log rows (90-day retention), purges orphaned DB rows, and syncs YouTube subscriptions.
   - Auth (`LECTIO_USERNAME` / `LECTIO_PASSWORD` / `LECTIO_SECRET_KEY`) stays in `.env`.
9. ~~**Instapaper integration**~~ ✓ — "Save to Instapaper" button in the entry toolbar. Credentials stored in Settings → Integrations. Backend POSTs to the Instapaper Simple API (`/api/add`) with credentials in the POST body; frontend shows a toast.
10. ~~**Email Automation**~~ ✓ — server-side Email Article rule execution at every feed refresh.
    - *Dev feeds*: `GET /dev/feeds/email-match.xml` and `email-skip.xml` (debug mode only) generate fresh entries on every request; dev feeds bypass the 60-second manual refresh cooldown. "Flush email batch queue" button in Feed Properties for dev feeds.
    - *Immediately* mode: sends one email per matching new entry (capped at 10/cycle). Entry must have been added within the last 15 minutes to qualify as "new."
    - *Batch digest* mode: entries queued in `email_batch_queue`; flushed when `batch_count` threshold is reached OR during daily maintenance. Digest email groups all queued articles in a single styled email.
    - *Cc me*: adds `profile_email` as Cc; suppressed if profile email already matches the To address.
    - Email rules run after mark_as_read/dedup to avoid emailing articles that were just auto-read.
11. ~~**Feed Properties v2 — Tuning tab**~~ ✓
    - YouTube feeds show only "Hide Shorts" in the Tuning tab; image strategy and display controls are hidden for YouTube feeds.
    - *Hide Shorts*: per-feed toggle; when enabled, entries whose link contains `/shorts/` are auto-marked as read at fetch time.
12. ~~**Polish / Bug fixes**~~ ✓
    - Readability fallback: BeautifulSoup `<main>` extraction when readability returns < 100 chars.
    - Instapaper credentials sent in POST body (Simple API requirement); removed unsupported `tags` field.
    - Highlight rules now apply to article body text (text-node walker), not just titles.
    - Fixed-width HTML tables no longer expand the viewport (`overflow-x: auto` + `max-width: 100%`).
    - Lead image source fetch skipped for entries with a cached miss (was re-fetching on every open).
    - Tag input: Enter key now reliably submits; AJAX save replaces full-page refresh; `+`, `.`, `#` allowed in tag names; invalid chars shown as a toast.
    - Fixed variable shadowing in tag route that caused posts list to filter by the last loop token.
13. **Page-to-Feed / FakeFeedz** — for truly feedless pages, generate synthetic feeds via scraping or change-detection (RSSHub, FetchRSS, or built-in scraper).
14. **Data Export / Takeout** — portable export of all user data beyond OPML and raw SQLite backup. Motivation: RSS services (e.g. Inoreader) often omit disabled feeds, tags, and starred articles from their takeout exports; Lectio should do better.
    - *OPML* — already done (feeds + folder structure).
    - *Tagged entries* — all entries with manual tags: title, link, date, tags (JSON + optional CSV).
    - *Starred entries* — same shape.
    - *Read history* — the 2,000-entry history log.
    - *Automation rules* — highlights, mark-as-read, dedup, email rules.
    - *Settings / contacts* — profile, email config, contacts list.
    - Delivered as a single ZIP download with one JSON file per category. Triggered from the main menu or Settings.

## Backburner

- **Resurface / GUID-churn suppression** — publishers sometimes change entry GUIDs (CMS migrations, permalink changes, plugin rebuilds), causing batches of already-read articles to reappear as new. Mitigation: when a new entry arrives whose title + approximate date matches a known read entry in the same feed, auto-mark it read. Overlaps with cross-feed dedup (slug/title matching). Distinct from `updated`-timestamp changes, which don't affect read state because the GUID is unchanged.


- Per-user vs. shared thumb cache (only relevant if multi-user is added).
- Archive caps for starred entries.
- Multi-user support / auth refactor.
- YunoHost or other packaging.
- PWA features.
