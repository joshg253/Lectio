# Lectio Plan

This file is the backlog and staging area for future work.

## Bugs
- Feeds with missing `pubdate`: many feeds show "None" — handle gracefully.
- Delete feed reloads the page and re-opens its folder — should remove in-place.

## Up next (ordered)

1. **Email Article** — share button in entry toolbar; sends link + summary via Resend.
2. **Feed Troubleshooter / Advanced Properties** — right-click → "Properties…" panel with three sections: (1) feed metadata and detected feed type; (2) raw content preview (what `<content>`/`<summary>` actually contain); (3) image strategy: try all candidate strategies against a sample entry, pick the best, save as override. Three independent per-feed image flags: *use as thumbnail* (post list), *show at top of article body*, and *show image caption* (the alt/title text rendered below the image — useful for some feeds, noise for others). Also the foundation for the Add Feed discovery flow.
3. **RSS Auto-Discovery** — when adding a feed URL, auto-detect hidden RSS/Atom links on the page before failing.
4. **Text Highlighting** — highlight matching keywords in entry titles and bodies (global or per-feed keyword lists).
5. **Rules / Actions Engine** — trigger actions (mark-read, tag, star) based on text match in title/body, per-feed or global; rule manager UI. Subsumes keyword/author auto-tagging and smart folders.
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
