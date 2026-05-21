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
    - *Dev feeds*: `GET /dev/feeds/email-match.{xml,atom,json}` and `email-skip.{xml,atom,json}` (debug mode only, `LECTIO_DEBUG=1`) generate fresh entries on every request; dev feeds bypass the 60-second manual refresh cooldown. "Flush email batch queue" button in Feed Properties for dev feeds. `/dev/feeds/` path is exempt from auth so the reader fetcher can access it.
    - *Immediately* mode: sends one email per matching new entry (capped at 10/cycle). Entry must have been added within the last 15 minutes to qualify as "new."
    - *Batch digest* mode: entries queued in `email_batch_queue`; flushed when `batch_count` threshold is reached OR when the current local time matches the rule's `batch_time` (HH:MM, checked every 30 s by `_daily_maintenance_loop`). Daily maintenance is a final safety-net flush.
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
13. ~~**Page-to-Feed / FakeFeedz**~~ ✓ — built-in page scraper for feedless pages. Two modes: *New links* (link_list) seeds existing links as hidden on first subscribe, surfaces only newly-appeared links thereafter; *Content changes* (change_detect) hashes the page and creates an entry on each change. Optional CSS selector narrows the watched region. Feeds stored as `file://` RSS 2.0 XML under `DATA_DIR/scraped-feeds/`; reader treats them identically to remote feeds. Delete removes DB rows, XML file, and reader subscription. Toast shown when RSS auto-discovery fails, with direct shortcut to FakeFeedz modal pre-filled with the URL.
14. ~~**Data Export / Takeout**~~ ✓ — portable ZIP export of all user data; importable on any Lectio instance.
    - *OPML* — already done (feeds + folder structure), included in ZIP as `opml.xml`.
    - *Tagged entries* — all entries with manual tags: feed_url, entry_id, title, link, published, tags array.
    - *Starred entries* — same shape plus saved_at.
    - *Read history* — full history log (feed_url, entry_id, title, link, read_at).
    - *Automation rules* — all highlight_keywords rows.
    - *Contacts* — email contacts list.
    - *Settings* — app_settings (sensitive credentials excluded from export).
    - ZIP: `lectio-takeout-YYYYMMDD.zip` via `GET /takeout/export` (main menu → Takeout → Export ZIP).
    - **Import**: `POST /takeout/import` (main menu → Takeout → Import ZIP). Merges non-destructively: rules INSERT OR IGNORE (primary key scope+keyword), contacts by address, history appends, tagged/starred entries re-applied to any matching reader entry. Future-version ZIPs are rejected with an error.

## Backburner

- **Resurface / GUID-churn suppression** — publishers sometimes change entry GUIDs (CMS migrations, permalink changes, plugin rebuilds), causing batches of already-read articles to reappear as new. Mitigation: when a new entry arrives whose title + approximate date matches a known read entry in the same feed, auto-mark it read. Overlaps with cross-feed dedup (slug/title matching). Distinct from `updated`-timestamp changes, which don't affect read state because the GUID is unchanged.


- Per-user vs. shared thumb cache (only relevant if multi-user is added).
- Archive caps for starred entries.
- Multi-user support / auth refactor.
- YunoHost or other packaging.
- PWA features.
