# Lectio Plan

This file is the backlog and staging area for future work.

## Recently Completed

- **DeviantArt artwork strategy** — added `deviantart.com` to `_ARTWORK_FEED_DOMAINS`; existing DeviantArt gallery feeds auto-tagged `artwork` at next startup.
- **GUID-churn suppression** — `_suppress_guid_churn` runs after every feed refresh; auto-marks newly-seen unread entries as read when a read entry in the same feed already has the same URL slug (publisher re-issued the same article with a new GUID). URL-slug matching only — title-only matching skipped due to false-positive risk on repetitive feeds.


## Up next

- **Better tuning / live preview** — full entry preview pane with swappable strategy and display settings side by side. Goal: see exactly what an entry looks like under different combinations (strategy × show-in-article × caption mode) without saving. Probably a modal or split-pane triggered from Feed Properties Tuning tab.
- **Performance investigation** — systematic baseline before enabling multi-user. Capture per-request breakdown (DB time, enrich time, refresh contention) under realistic load (concurrent page loads + active refresh cycle). Identify whether bottleneck is SQLite write contention, thumbnail enrichment, or network.
- **FRB remaining gaps**:
  - *Persistent failure alerting* — feeds with ~30+ consecutive failures should surface a prominent warning in Feed Properties (and ideally the feed list) so the user can decide what to do. Auto-disable is too blunt; the feed might be temporarily blocked or the user may still want it. Alert first, let the user disable.
  - *Adaptive polling / feed TTL hints* — honor `ttl`, `skipHours`, `skipDays`, `sy:updatePeriod` from feed XML as scheduling hints. Feeds that rarely update should be polled less frequently.
- **Resurface / GUID-churn suppression** — ~~moved to backburner~~ done: URL-slug matching implemented. Title+date matching (for feeds that change both GUID and URL) remains as a possible follow-up if needed.

## Backburner

- **Resurface / GUID-churn suppression** — publishers sometimes change entry GUIDs (CMS migrations, permalink changes, plugin rebuilds), causing batches of already-read articles to reappear as new. Mitigation: when a new entry arrives whose title + approximate date matches a known read entry in the same feed, auto-mark it read. Overlaps with cross-feed dedup (slug/title matching). Distinct from `updated`-timestamp changes, which don't affect read state because the GUID is unchanged.
- Per-user vs. shared thumb cache (only relevant if multi-user is added).
- Archive caps for starred entries.
- Multi-user support / auth refactor — performance investigation first.
- YunoHost or other packaging.
- PWA features.
