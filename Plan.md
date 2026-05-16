# Lectio Plan

This file is the backlog and staging area for future work.

## Bugs
- Feeds with missing `pubdate`: many feeds show "None" — handle gracefully.
- Delete feed reloads the page and re-opens its folder — should remove in-place.

## Up next (ordered)

1. **Email Article** — share button in entry toolbar; sends link + summary via Resend.
2. **Feed Troubleshooter** — right-click or Properties panel showing all candidate image/thumbnail sources, content extraction methods, and raw feed data; lets you pick best options per feed.
3. **RSS Auto-Discovery** — when adding a feed URL, auto-detect hidden RSS/Atom links on the page before failing.
4. **Text Highlighting** — highlight matching keywords in entry titles and bodies (global or per-feed keyword lists).
5. **Rules / Actions Engine** — trigger actions (mark-read, tag, star) based on text match in title/body, per-feed or global; rule manager UI. Subsumes keyword/author auto-tagging and smart folders.
6. **Page-to-Feed (Scraping)** — for truly feedless pages, generate synthetic feeds via scraping or change-detection (RSSHub, FetchRSS, or built-in scraper).

## Backburner
- Per-user vs. shared thumb cache (only relevant if multi-user is added).
- Archive caps for starred entries.
- Multi-user support / auth refactor.
- YunoHost or other packaging.
- PWA features.
