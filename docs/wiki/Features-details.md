# Features — the detail behind the bullets

> Behavior detail that doesn't belong in the README's feature bullets. The README links here; add new detail here, not there.

Dates are worked out rather than trusted blindly. A publisher that ships no
date — or ships a placeholder like the Unix epoch, which importers and parsers
both like to write — no longer strands a post at "1970": Lectio falls back to the
date in the permalink (`/2019/07/06/` or `/2025-11-22/`), then a month-precision
permalink, then a date in the title, and only then to when it first saw the post.
Posts nothing can date say so instead of quietly showing their arrival time as a
publication date. You can also correct any post's date by hand, including saved
posts whose feed you've since unsubscribed from.

When a saved article's page rots, **Re-fetch from Internet Archive** pulls the
snapshot instead — and because archived copies usually still carry the byline the
publisher has since dropped, that often recovers the date as well as the text.

**Keep the files a post links to.** Some posts are really a wrapper around a
download — guitar-pro's tab posts link `.gp` files and PDF lyric sheets that
vanish with the article, so keeping the text without them keeps the wrong half.
Name the extensions in Feed Properties and they're captured alongside the post
when you star or tag it, served from the same offline archive. Extensions only:
there's deliberately no wildcard, and page types (`html`, `php`, …) are always
ignored — that list is what keeps this a capture of named file types rather than
a crawl of every link on the page. Files on a separate asset domain are fine,
which is the normal case.

**Suggested tags per feed.** A feed with a stable subject rarely tags its own
posts — a guitar blog doesn't tag anything "guitar" — so filing meant typing the
same word every time. Pin tags to a feed in Feed Properties and they're offered
as chips on every post in it, ahead of the feed's own tags and never shown twice
if the publisher happens to ship the same one. They're a suggestion, not an
automatic tag; a tag rule already covers that. Picking a tag from the
autocomplete applies it straight away rather than making you confirm.

Some comic hosts publish a thumbnail where the comic should be. Tapas and
Webtoons feeds carry one image per episode — fine in the post list, wrong in the
article, since the episode itself can be fifty stacked panels. Lectio reads the
episode and shows all of them, keeping the feed's picture as the list thumbnail.

Transparent images keep their transparency and the theme paints behind them
(`--img-backdrop`, white in both themes) — black line art on a dark page was
otherwise invisible.

Scheduled refresh is watchdogged. Feeds are fetched sequentially, so one host
that accepts a connection and then goes silent can stall every feed behind it —
invisibly, since the app keeps serving. Every fetch carries a read deadline, the
scheduler loop cannot be killed by an exception, and a watchdog trips when a pass
stops *advancing* (not merely when it takes a while — a full-library pass runs for
an hour legitimately). Past a longer threshold it exits so the container restarts,
since a thread wedged in a socket read cannot be cancelled. `/healthz` reports the
stall but keeps returning 200: a reader whose refresh is stuck is still readable,
and failing the probe would take the whole app out of the reverse proxy. Tunable
via `LECTIO_FEED_READ_TIMEOUT`, `LECTIO_SCHEDULER_STALL_SECONDS` and
`LECTIO_SCHEDULER_STALL_RESTART_SECONDS` (see `.env.example`).

Pages stay light at large subscription counts: per-feed row sections (the
sidebar folder feed lists, the Settings → Feeds table, and the Stale view)
load as HTML fragments on first open instead of shipping with every page, and
the app script is a cacheable static file rather than inline JS.

**Automation details.** An **auto-read age** (off by default) marks new posts read at fetch once they're older than N days, so a quiet site
that finally updates doesn't flood the unread list; set it for the instance, an account, a folder or a feed, each only able to shorten the
one above. Tag-filter rules also work on tags scraped from article pages: for a feed whose tags exist only on the page, saving the rule
turns on page-tag capture, so matching posts are filtered as they arrive rather than when opened. That capture reaches sites that block
plain fetches (Cloudflare-fronted ones) through the same fetch ladder as opening a post. Tagging a teaser-only post fetches the full page
even when the excerpt plus its "Read X on Y" footer looks long enough.

**IsThereAnyDeal waitlist posts** render one compact line per offer (price, discount, store, voucher) instead of a column of short lines.

**Clean up YouTube playlists** (Settings → Utilities → YouTube, needs the YouTube connection). Pick playlists, nothing pre-selected, and
it removes duplicate videos, keeping the first copy. Two optional extras: remove deleted and private videos (your own private uploads
stay), plus members-only ones Lectio has already identified; and remove videos blocked in a country you give, using the country blocks
YouTube lists for each video, which can miss some rights-holder blocks. Because YouTube sometimes removes both copies when you delete
one, it checks each video afterward and re-adds one that disappeared (it lands at the end of the playlist). Each delete or re-add costs
50 of the day's YouTube API quota, so a big clean-up can stop partway; it says so and names any video it couldn't put back.
