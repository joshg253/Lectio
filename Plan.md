# Lectio Plan

This file is the backlog and staging area for future work.

## Now
- Deploy to VPS (Traefik + Cloudflare Origin certs at `lectio.${BASE_URL}`).

## Soon
- tons of feeds with "None" for pubdate
- problem feeds: delete refreshes whole page and opens the folder where it lived -- should just remove from list, ~queue background remove
- FIXED? counters not updating at all while using it, (looks like they eventually did but not while I was using the app)
- Stronger saved/archive view state persistence.
- More feed-specific display tweaks.
- Better per-feed preferences.
- Additional topbar or entry actions.
- More robust refresh/restore behavior.

## Later
- Per-user vs shared thumb cache decision.
- Archive caps for starred entries.
- Keyword/author auto-tagging.
- Smart folders.
- Non-RSS monitoring.
- Read-later/share integrations.
- Cloudflare integrations where useful.

## Maybe
- Docker packaging.
- YunoHost packaging.
- Multi-user support after auth refactor.
- Richer plugin system.
- PWA features.
- Cloudflare Tunnel for ingress.
