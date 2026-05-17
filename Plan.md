# Lectio Plan

This file is the backlog and staging area for future work.

## Up next (ordered)

1. ~~**Email Article**~~ — share button in entry toolbar; sends link + summary via Resend. ✓
2. ~~**Feed Troubleshooter / Advanced Properties**~~ — right-click → "Properties…" panel with feed metadata, health/post counts, and image display controls. Three per-feed flags (show as thumbnail, show in article, caption mode with junk-suppression heuristics) plus strategy comparison (og_scrape / inline / media_rss / youtube) with cached results and a Refresh button. ✓
3. ~~**RSS Auto-Discovery**~~ — when adding a feed URL, auto-detect hidden RSS/Atom links on the page before failing. ✓
4. ~~**Text Highlighting**~~ — keyword-based title highlighting at global, per-folder, and per-feed scope. Keywords and colors managed via a modal (main menu + right-click on folder/feed). Highlights apply client-side on post list titles and the entry pane title. ✓
5. **Automation Engine** — unified rule system that subsumes and extends the current Highlights feature. Design:
   - **Single "Automation" modal** (replaces the Highlights modal) accessible from the main menu and from right-click on folder/feed.
   - **Rule row structure** (adaptive — controls to the right depend on type):
     - *Type* dropdown: `Highlight` | `Mark as Read` | `Email Article`
     - *Pattern* — keyword or regex (the `.*` toggle)
     - *Search in* — `Title` | `Body` | `Both`
     - *Scope* — folder dropdown → feed dropdown (same as current Highlights)
     - *Color* — only shown when type = `Highlight`
   - **Affected feeds** — each rule row (or its detail view) shows the list of feeds in scope, so it's easy to see blast radius before saving.
   - **Feed Properties integration** — the Properties panel for a feed shows which Automation rules (if any) match that feed, so you can audit from the feed side too.
   - *Mark as Read* and *Email Article* rules fire server-side at fetch time; *Highlight* remains client-side.
   - Migration: existing `highlight_keywords` rows become Automation rules with type=`highlight`; add `type`, `search_in` columns to the table.
6. **Page-to-Feed (Scraping)** — for truly feedless pages, generate synthetic feeds via scraping or change-detection (RSSHub, FetchRSS, or built-in scraper).

## Backburner

- **Resurface / GUID-churn suppression** — publishers sometimes change entry GUIDs (CMS migrations, permalink changes, plugin rebuilds), causing batches of already-read articles to reappear as new. Mitigation: when a new entry arrives whose title + approximate date matches a known read entry in the same feed, auto-mark it read. Overlaps with cross-feed dedup (slug/title matching). Distinct from `updated`-timestamp changes, which don't affect read state because the GUID is unchanged.

- **Cross-feed article deduplication** — same article published across multiple feeds (e.g. Sound Publishing network: Kent/Issaquah/Renton Reporter share identical URL slugs). Detection options in rough order of reliability:
  1. URL slug match (path component after last `/`) — works well for same-network syndication.
  2. Exact title match within a time window.
  3. Content hash (more expensive, catches rewrites).
  Could suppress duplicates in the post list (show one, dim/hide others) or auto-mark-read when a sibling is read. Needs a UI to configure which feeds participate and what the dedup scope is (folder, global).


- Per-user vs. shared thumb cache (only relevant if multi-user is added).
- Archive caps for starred entries.
- Multi-user support / auth refactor.
- YunoHost or other packaging.
- PWA features.
