# Feeds

Subscribing, fetching, deduplicating, combining and removing feeds.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## Feed URL normalization

`normalize_feed_url` (main.py) is applied at add-feed time and in the Duplicate scan (`GET /feeds/duplicates`). It handles:

- Trailing-slash stripping from paths longer than `/`.
- Format-selector query params (`alt=rss`, `type=atom`, `feed=rss2`, etc. — `_FORMAT_SELECTOR_PARAMS`) whose *value* is also on the allowlist (`_FORMAT_SELECTOR_VALUES = {rss, rss2, atom}`, plus anything prefix-matching `json*` since JSON Feed versioning is a moving target) that select serialization without changing content — lets the Blogger Atom and RSS URLs of the same feed collapse to one. Param name **and** value both have to match — a hypothetical `?type=news` category selector is left alone.
- ArtStation subdomain rewrites (`username.artstation.com/rss` → `www.artstation.com/username.rss`) to avoid TLS hostname issues with underscore usernames.
- `_DOMAIN_ALIASES` map — known domain pairs that serve identical content, or renamed domains (`old.reddit.com` → `www.reddit.com`; `tapastic.com` → `tapas.io`). Add new pairs there; the normalization and duplicate-scan logic picks them up automatically.

**Curation migration on consolidation.** Every duplicate-scan tier and the format-Upgrade tier resolve through `POST /feeds/combine` (a user-picked survivor + one or more sources), which calls `purge_orphaned_feed` with `migrate_curation_to` set. That first calls `_migrate_curation` to move the removed feed's manual tags and stars onto the surviving feed — matching each curated source entry to a survivor entry by GUID, else normalized link, else synthesizing it into the survivor (`reader.add_entry`) so nothing is lost. This is unconditional (independent of the opt-in "rescue unread" toggle, which only re-flags read/unread state) and mirrors the offline `scripts/reconcile_duplicate_feeds.py --merge` path. The old bulk `POST /feeds/deduplicate` (auto-apply same-folder pairs, checkbox-picked cross-folder/upgrade choices) was retired 2026-08-10 once every tier moved to `/feeds/combine`'s per-group Compare-then-Combine flow — see below.

**Import-time canonicalization.** `canonical_feed_url` (main.py) composes `normalize_youtube_feed_url` + `normalize_feed_url` and is the single choke point every bulk importer runs each incoming feed URL through *before* it subscribes or keys per-entry tag/star state. This makes a variant URL (old.reddit, `?alt=rss`, trailing slash) attach to an existing subscription instead of spawning a duplicate. It is wired into OPML import, the Inoreader local-file migrator, the shared migration applier `_apply_migration_items` (Miniflux/FreshRSS/Tiny Tiny RSS), the Inoreader JSON upload, and the Inoreader OAuth drip (subscriptions, label, and starred phases). Importers that key both subscription and tagging off `item["feed_url"]` call `_canonicalize_item_feed_urls(items)` once up front so both phases stay in sync. Google Takeout import is exempt: it only applies tags/stars to entries already present in the reader DB (never `add_feed`s), and those URLs are already canonical from the original subscription.

**Canonicalizing the incoming URL is only half of it — the set you compare against has to be canonical too.** `import_opml` canonicalized each `xmlUrl` and then tested it against the *raw* `folder_feeds` URLs. Any subscription whose stored URL was not already canonical therefore never matched, looked new, and was subscribed a second time under the canonical spelling. A trailing slash was enough: re-importing Lectio's own OPML export duplicated **440 of 2,909** foldered feeds — the restore-from-backup path, which is exactly when a user can least afford it. The dedupe set is now built through `canonical_feed_url` as well. Worth noting for any future importer: these duplicates are invisible to a `GROUP BY feed_url` check, because the two rows hold different strings (`…/feed/` and `…/feed`), so verify idempotency by comparing subscription *counts* across a round trip.

The same bug shipped, unnoticed, in every OTHER importer this section says is "wired into" `canonical_feed_url` — the paragraph above describes the intent, not what the code did until 2026-08-24. `_inoreader_drip_step`'s subscriptions phase, `_run_import_loop`, `inoreader_import_json`, and `_apply_migration_items` all canonicalized the incoming URL and then checked it against a raw `{f.url for f in reader.get_feeds()}` set — same gap as OPML, just against `reader.get_feeds()` instead of `folder_feeds`. Caught when a nightly Inoreader drip re-added subscriptions whose stored URL predated canonicalization. Worse than OPML's case: the drip step's label/starred phases and the migration applier's tagging phase also use the (wrongly uncanonicalized) URL to look up the entry to tag/star, via `_api_resolve_entry`'s 3-pass lookup — a miss there doesn't just fail silently, it falls through to `add_entry` synthesis and creates a duplicate *entry* under the new duplicate feed. `_canonical_feed_url_lookup(reader)` (a canonical→stored-URL map, built once per import batch) and `_resolve_feed_url(url, lookup)` are the shared fix: every add-feed pre-check and every entry lookup in these four importers now resolves through the *stored* URL, not just the freshly-canonicalized one.

## Duplicate entry suppression

Two mechanisms prevent duplicate articles from accumulating in the reader DB:

**GUID-churn suppression** (`_suppress_guid_churn`, runs after each refresh): detects entries that reappear with a new GUID but the same URL slug, or the same title + publication date (within 7 days). Checks both read history AND existing unread entries so that multiple copies arriving before any are opened are also caught.

**Change URL resolves, but does not adopt across hosts.** The route probes the pasted URL and uses the feed it resolves to — right for a redirect, wrong for a page. A section page (`music.<site>/c/instruments`) advertises the network-wide feed in its HTML head, so the probe "succeeded" and the subscription silently became the whole network, plus an auto-seeded `feed_url_rewrites` host alias for it. Cross-host resolutions that came from HTML discovery (`direct` absent) now return `needs_confirm` carrying `resolved_url`; confirming re-posts *that* URL with force, because forcing the pasted page instead would subscribe reader to an HTML document. A direct feed URL that 301s to another host keeps resolving silently — that one really is the same feed.

**A plain-text excerpt must decode entities, because whoever renders it escapes again.** `html_sanitize.plain_text_excerpt` backs the email preview and the digest. Tag-stripping alone left the body's own entities as literal text, so `html.escape` in the mail builder escaped the ampersands a second time and the recipient read ``&lt;chrono&gt;, his date &amp; time library``. Tags are stripped **before** entities are decoded, never after: the other order turns an escaped `&lt;script&gt;` into a real tag for the stripper to eat, losing text the reader is meant to see.

**Entities left in a title are decoded at ingest.** feedparser decodes once, so a double-encoded publisher still lands `AT&amp;T` or `Apple&rsquo;s` in the stored title (1,014 of them across 182 feeds), which is what the reader sees and what a plain rule keyword has to match letter for letter. `html_sanitize.decode_title_entities` runs in `_sanitize_entry`. `&lt;`/`&gt;` decode too. A stored title is never rendered raw — the list rows and reader head run it through `sanitize_inline_title`, whose allowlist re-escapes anything that is not the feed's own `<em>`-style formatting (verified in a browser: a title carrying `<script>` or `<img onerror>` renders as visible text, sets no globals and injects no nodes). 41 titles already hold a literal `<` from their publisher — `ReadOnlySpan<T>`, `<chrono> and more` — and have always rendered correctly. Keeping the entities encoded was the worse outcome: the ampersand is escaped in turn, so the reader saw a literal `&lt;details&gt;` on screen. `scripts/decode_entry_title_entities.py` is the backfill for what is already stored.

**Relative URLs in an item's HTML resolve against the ITEM's link, not the feed's.** feedparser's `resolve_relative_uris` only ever uses the document base, and reader's HTTP layer fills `content-location` with the feed URL (`reader/_parser/http.py`), so `<img src="images/x.jpg">` inside an item at `/news/202608/post.html` resolved to `/news/images/x.jpg` — a 404 on every image such a feed carries, and static-site generators that copy a page's markup into the item verbatim produce exactly this. The flag is now off (it governs embedded markup only; entry links and enclosures are still resolved by feedparser) and `_sanitize_entry` redoes it per entry via `html_sanitize.resolve_relative_urls`, *before* sanitizing so the embed host-allowlist still judges absolute URLs. The base is the entry's own link but only when it shares the feed's host — an aggregator links out while its markup stays relative to itself, and rebasing onto the linked domain would invent URLs. Entries stored before this keep the old broken URLs; re-fetching the entry's content repairs one.

**A field typed as HTML can actually be raw Markdown source.** blog.gitea.com's release posts ship literal `## Security`, `**bold**`, `- ` bullets and `[text](url)` links in `summary` — Markdown, not HTML — so with no tags to interpret, a browser collapsed every newline into one dense wall of text. `_sanitize_entry` now detects this per field (`_looks_like_markdown`: no HTML tags at all, but a Markdown-shaped hint — a heading, bold, a bullet, or a link) and runs it through `markdown.markdown()` before the usual resolve-then-sanitize pipeline, the same call `main.markdown_to_article_html` already makes for a whole page declared `text/markdown`. Requiring *both* conditions matters: a false positive would mangle a feed that never used Markdown syntax and happens to contain a stray `**` or `##` in real prose.

**Title normalization strips punctuation from token EDGES only.** `normalize_entry_title_for_dedupe` NFKC-normalizes, folds smart quotes, splits en/em dashes *between* letters (they separate phrases), casefolds, then strips sentence punctuation from each token's edges. Word boundaries never move: `second-best.cat()` stays one token instead of becoming `second best cat`, because splitting compounds was measured as strictly worse (30 → 28 true cross-feed pairs on a 5,588-entry backlog — splitting inflates the token count on whichever side spells the compound out). The strip set deliberately omits `+ # & @ $ *`: `C++` and `C#` both collapse to `c` otherwise, merging unrelated programming posts. Adopting the strip added 6 cross-feed fuzzy pairs at 80%, all of them near-misses already scoring 0.71–0.79 — punctuation was costing genuine duplicates about 0.12 of similarity.

**One title-length floor, `_DEDUP_MIN_TITLE_WORDS`, tunable per rule.** Shared by fuzzy, exact-title, the safe combo and GUID-churn suppression, which each carried their own literal `4`. It is a false-positive guard, not an optimization: titles under 4 words produced 11 same-feed false collisions against 1 real cross-feed duplicate. It also bounds what the % knob can express — two 4-word titles can only score 1.0, 0.6, 0.33 or 0.14, so every threshold from 61% to 100% behaves identically there. `dedup_min_title_words` (3–10) overrides it per rule and now applies to Title mode too, which had no floor at all. The default is **5**, not 4: the 4-word band itself contributed 2 more same-feed false collisions and no true cross-feed ones. Rules created with the shipped default of 4 are raised once at startup, behind an `app_settings` flag so a deliberate 4 set later survives.

**Deduplicate rules: the fuzzy threshold belongs to the rule, not the code.** `fuzzy` matching is Jaccard word overlap on normalized titles (`title_word_similarity`), and the cutoff that separates a syndicated repost from two different articles is corpus-specific — 80% was a guess that suited no feed in particular. It is a `highlight_keywords.dedup_fuzzy_pct` column (50-100, clamped by `_clamp_fuzzy_pct`), read by the after-refresh automation and sent as `fuzzy_pct` by dry-run and Run Now, so one number governs preview, future entries, and the backlog sweep. The mode-comparison panel exists to *find* that number: it buckets pairs by how many of the four modes agreed, opens on the single-mode outliers, and hides the all-modes-agree consensus inside each mode's detail panel — the pairs every mode caught need no review, and burying the disagreements under them was the original mistake.

**Intra-feed and cross-feed cleanup** (`_cleanup_intra_feed_slug_dupes`, runs at startup and after each refresh cycle): two-pass retroactive cleanup for duplicates that slipped through before suppression was in place or before Deduplicate rules ran.
- Pass 1: within each feed, keep the oldest entry per slug and per title+date; mark newer copies read.
- Pass 2: across all feeds, group entries by `normalize_entry_link_for_dedupe`; keep the oldest copy globally and mark the rest read. This handles syndicated posts that appear in multiple subscribed feeds (e.g. a blog post cross-posted to two feeds from the same author).

`normalize_entry_link_for_dedupe` is the single canonical link key, shared by this pass, the render-time list collapse (`build_entry_dedupe_key`), the curation migration on feed removal, and the Saved duplicate scan's "confirmed" tier. It drops the fragment and trailing slash, then **folds the scheme and a leading `www.`**, lowercasing only the host — paths stay case-sensitive. The fold matters because the Saved scan's other confirmed-tier key, the URL slug, is deliberately discarded when it is generic (`/index.html`, blocklisted, or hyphen-free and short); before the fold, an http/https or www/non-www twin with such a URL had no confirmed-tier key at all and fell through to the weaker "possible" tier, where it needed a hand judgment. The result is a comparison key, not a URL — it has no scheme and is never fetched or displayed.

**The saved scan's slug key is host-scoped** (`_saved_dup_host_slug`), unlike the shared `_safe_dedup_entry_slug` it wraps. That helper returns the last path segment and nothing more, which is right for its other callers — per-feed GUID-churn history, and the multi-signal dedup where a lone `slug` is never one of `_SAFE_DEDUP_COMBOS` — but in `/saved/duplicates` a bare slug match *confirms* a duplicate on its own. Host-blind, that made unrelated publishers collide: guitarworld.com and guitarmasterclass.net each have a `pinch-harmonics` article, and the scan offered to delete one of them. A descriptive slug clears every length/hyphen guard precisely *because* it names a topic. Scoping the key to the folded host keeps the tier's real job (one article under a changed path on one site, including across a scheme/www change) and leaves genuine cross-host syndication to the title/body tier, which is far stronger evidence.

These run server-side and affect the underlying DB state, so third-party clients (Capy, etc.) see the clean state after the next sync.

## Duplicate feeds: the scheme (and www) is folded in the comparison, not the URL

`get_feed_duplicates` groups subscriptions by `normalize_feed_url` **with the
scheme and a leading `www.` stripped** (`_dupe_group_key`). Both have to be
folded somewhere other than `normalize_feed_url` itself, because some hosts
really are http-only or www-only and forcing either there would break them —
the comparison has no such obligation, unlike the stored subscription URL.
`_DOMAIN_ALIASES` rewrites a host without touching the scheme, which is what
originally forced the scheme fold: a legacy `http://tapastic.com/…`
subscription normalized to `http://tapas.io/…` while its live twin was
`https://…`, so the two never grouped and a dead feed sat beside a working copy
failing every refresh, unflagged. The www fold followed the same shape:
`deathbulge.com/rss.xml` and `www.deathbulge.com/rss.xml` are the same feed.

The survivor (`keep`) is chosen from the variants that are actually subscribed —
preferring `https`, then the already-canonical spelling, then the shortest.
`keep` used to be the canonical *string*, which is not necessarily one of the
variants; when it was not, `url_folders.get(keep)` came back empty, so every
variant looked cross-folder and was offered for removal against a URL nobody was
subscribed to.

### Five tiers, one Compare-then-Combine UX

`GET /feeds/duplicates` returns five tiers, each rendered in Settings → Feeds
→ Utilities as its own `<details>` section but sharing one frontend format
(originally built for the title tier alone, generalized to the rest
2026-08-10 — `_renderDedupGroups` in `app.js`): a group of candidate feed
URLs, an inline **Compare** (`GET /feeds/compare`, live-fetches each URL and
shows format/entry-count/full-text/dates/GUID-type/sample-title), then an
inline **Combine** (survivor radio + optional "carry over unread state",
`POST /feeds/combine`). Nothing auto-applies anywhere — every merge is an
explicit, post-Compare user decision. The bulk `POST /feeds/deduplicate`
(same-folder auto-apply, checkbox-picked cross-folder/upgrade choices) that
this replaced is gone.

- **`same_folder` / `cross_folder`** — a `_dupe_group_key` match: same feed,
  different scheme/www/format-selector. Two feeds, no checkboxes needed
  (there's only one possible comparison) — Compare is just always available.
- **`query_pairs`** — same host+path, *different* query, deliberately never
  auto-folded: a query param can be a real duplicate (a format selector the
  allowlist doesn't recognize) or genuinely different content (a WordPress
  category/tag feed). YouTube (`channel_id` differs by design — the Watch
  folder sync is bidirectional, so two channel ids are never a duplicate) and
  DeviantArt's `backend.deviantart.com/rss.xml?q=gallery:<user>` (one shared
  endpoint for the whole site) are excluded entirely as noise sources.
- **`title_groups`** — same feed *title* across two genuinely different
  addresses (a Tumblr and a Tapas copy of the same webcomic) — the only tier
  the URL-scheme grouping can't reach at all, since it needs two different
  addresses, not variants of one. `GENERIC_TITLE_GROUP_MAX = 5` floors out
  generic titles ("News" shared by unrelated sites is noise; every genuine
  match measured at 2–3 feeds). The only tier where a group can legitimately
  hold a non-match (an unrelated site sharing a generic title alongside a
  real pair), so it's also the only one that keeps a pick-a-subset checkbox
  gate before Compare — see `_renderDedupGroups`'s `selectable` flag.
- **`upgradable`** — a subscribed URL still carrying a format-selector query
  param. `upgrade_to` is the stripped default (skipped entirely when
  stripping it would leave nothing but a bare domain — WordPress's
  root-level `?feed=rss2` is the failure case: the query *is* the whole
  address there, not decoration on a working default). `alternates` are
  same-family format-selector swaps (`_format_alternate_urls`: a WordPress
  `?feed=rss2` subscription also serves `?feed=atom` and `?feed=rss`
  natively — a real, near-guaranteed-to-exist option, not a guess). Neither
  candidate is ever assumed better: RSS isn't reliably worse than Atom on
  every site (some sites' RSS is the richer feed), so nothing here gets a
  "suggested keep" bias and Compare-then-Combine is mandatory before
  switching.

**`content_identical` gates the "suggested keep" hint.** `same_folder` and
`cross_folder` pairs carry a `content_identical` flag (`_pair_is_content_identical`)
— true only when the two URLs differ by scheme/www alone (provably the same
bytes). A format-selector swap does *not* get the hint: reported 2026-08-10,
freac.org's `?type=rss` carries summaries only while `?type=atom` carries full
text, so the length-based tie-break (`keep`) happened to "suggest" the worse
one. `query_pairs` and `upgradable` never get the hint at all, since content
identity isn't provable from the URL shape for either.

**A broken Compare result can't be picked as the Combine survivor.** If
`/feeds/compare` returns an `error` for a candidate (dead link, unparseable —
an Upgrade-tier guess turning out to be the site's HTML homepage, say), the
frontend drops it from the survivor radio list before rendering the Combine
panel, so a wrong candidate can't repoint a subscription at something that
isn't a feed. It can still be a Combine *source* (a silent no-op — nothing to
migrate from a URL nobody's subscribed to).

**Dismissals.** `POST /feeds/duplicates/dismiss` (and the "Not dupes" button
on every group) records the group's exact feed-URL set in `dedup_dismissed`
(`_dedup_dismiss_key` — order-independent, sorted-and-joined); `get_feed_duplicates`
filters every tier against it before returning. `POST /feeds/combine` also
records a dismissal automatically on every completed call, survivor + all
sources, regardless of whether anything was actually deleted — load-bearing
for the Upgrade tier specifically, where picking the already-subscribed
"current" URL as survivor makes every "source" a candidate nobody was ever
subscribed to, so the purge loop is a structural no-op and, without the
auto-dismiss, the next scan re-detects the identical group (reported
2026-08-10: "I just combined them, then checked again and they are back").
A dismissal naturally stops matching if the underlying feed set changes later
(unsubscribed, URL changed) rather than silently hiding some other, unrelated
group.

## Removing a feed: what goes, and what a surviving capture protects

Feed removal used to clean two marker tables and leave everything keyed on
`(feed_url, entry_id)` behind. Measured on the live library before the fix:
**51,672 dead rows across 551 removed feeds**, almost all of it two derived
caches (24,443 `entry_lead_images`, 27,049 `entry_read_state`).

**It is a per-entry question, not `DELETE ... WHERE feed_url = ?`, because a
capture can outlive its feed.** The Saved view renders those archive orphans and
reads their thumbnail and hand-made corrections from these very tables — 2,547
rows were still being displayed that way. `_purge_dead_entry_meta` therefore
stages the feed's surviving `archived_entry` ids in a temp table and deletes
only what is not among them. A temp table rather than a bound `NOT IN` list: a
feed can hold thousands of captures, past SQLite's variable limit, and chunking
a `NOT IN` is actively wrong — each chunk would delete the ids named in every
other chunk. If the archive cannot be read the function deletes **nothing**,
because leaking rows is recoverable and blanking an orphan's thumbnail is not.

Two tables are deliberately excluded. `read_history` is a log of what you read,
not state owned by a subscription, so unsubscribing must not rewrite it.
`saved_entries` belongs to the star/keep paths.

## Hard-deleting a single entry (tombstones)

The entry context menu's **Delete post…** (`POST /entries/delete`) hard-removes one garbage entry (spam, corrupted post). reader's public `delete_entry` only covers user-added entries, so feed-provided ones go through the storage-level delete — the same API reader's own `entry_dedupe` plugin uses. A tombstone row in the meta DB (`deleted_entries`, keyed feed_url + entry_id) records the deletion, and the refresh service purges any tombstoned entry a refresh re-ingested (`purge_tombstoned_entries`, runs after every update batch, before enhancement) — otherwise the entry would resurrect on every fetch while still inside the publisher's feed window. Tombstones are kept forever (tiny rows; the guid could reappear any time the publisher republishes).

## Feed discovery: which feed a page actually means

Two entry points share one set of rules, and must: `probe_url` previews what the Add dialog shows, while the Add route itself re-discovers through `discover_feed_urls_ex`. Any divergence means the dialog promises one feed and the button subscribes to another — which is exactly what happened when the page-path fix below landed in only one of them.

**Page path before site root.** Multisite WordPress puts a whole blog under a path (`devblogs.microsoft.com/oldnewthing/`) while the domain root serves a firehose of every blog on it. Probing the root first meant subscribing to "The Old New Thing" silently handed back "Microsoft for Developers". The more specific feed is the one the user asked for; a path with no feed of its own still falls through to the root.

**Gone vs refused.** A stale `<link rel="alternate">` is discarded only when positively confirmed dead — 4xx/5xx under the current identity *and* a browser-identity retry, with 405/501 and network errors left alone. Redirects are now followed one guarded hop at a time (re-running the SSRF check per hop, so no probe is ever bounced blind to an internal address): a stale tag is often an `http://` URL whose 301 hid the 404 behind it.

When every advertised link is dead and nothing else answers, what happens next depends on *why*:

- **Gone (404/410)** — report "no feed found", naming the dead address and pointing at Page Feed. Handing the link back produced the worst outcome available: the dialog says it found a feed, the add route then refuses it, and nothing appears in the feed list. The failure toast already offers a "Create page feed" button, so this lands the user where they need to be.
- **Refused (403, 429, 5xx)** — still offered. The server declined to answer a HEAD; that is not proof the feed is absent, and reader's real GET may get through. This is the bot-walled case the last resort exists for.

**Two kinds of known-site rule.** `_SITE_FEED_REWRITES` are pure URL functions (Pinboard, ArtStation, Behance, freeCodeCamp, Tinyview, and the numeric Tapas form) — no network, applied before the fetch. `_SITE_BODY_FEED_EXTRACTORS` are the other shape: the feed address exists only in the page body, so they run *after* the fetch, against HTML discovery already has. Tapas is the case that needed it — it advertises no `<link rel="alternate">` at all (its only alternate is the mobile page) and its canonical link points at the latest *episode*, not the series, so `tapas.io/series/<slug>` is invisible to generic discovery. The series id lives in the markup as `seriesId:` / `data-series-id`, which is what the community userscripts scrape by hand. Extractors run only when nothing was advertised, and their result flows into the same liveness check as any advertised link, so a stale id is caught rather than offered. Tinyview is the same *problem* with a simpler answer: it too advertises nothing, because it renders client-side, but its feed address is entirely predictable (`tinyview.com/<comic>/feed.rss`), so a URL rule suffices and no body scrape is needed. A rewrite is a guess that costs nothing to get wrong — the rewritten URL is fetched and content-type/body checked like any other candidate, so a non-comic path fails discovery exactly as it would have. Both entry points call the same helper on the same slice of the same HTML — see the divergence warning above.

## Fetching a feed: three ways it fails before it ever parses

All three were found on the live library on 2026-08-12, and all three presented
as "this feed is broken" while the site worked fine in a browser.

**A challenge page is a block, not a malformed feed** (`services/bot_challenge.py`).
An anti-bot interstitial served in place of a feed arrives as a 2xx whose body is
HTML, then fails to parse — indistinguishable from a genuinely broken feed unless
you look. poorlydrawnlines.com was logged as *"could not be parsed as a valid
RSS/Atom document"* for months while returning a SiteGround captcha as **HTTP
202**, which is also why no count of blocked feeds could ever be right: anything
keyed on 403 never saw it. The fetch hook now sniffs for vendor markers
(SiteGround, Cloudflare, Imperva, Sucuri, DDoS-Guard, AWS WAF) and raises a
stable `bot challenge: blocked by <vendor>`. Status is deliberately not part of
the test. The detector is narrow on purpose — a site serving its ordinary
homepage at a dead feed URL is *moved/dropped*, a different failure with a
different fix.

⚠ These blocks are keyed on the **client IP**, not the user-agent: the same URL
fetched with the honest UA and with a full browser identity returns a
byte-identical challenge naming our own IP. All 29 of the library's genuine
403s were already flagged for browser identity and still 403. Escalating the UA
does nothing, so detection here exists to *label* the failure, not to get past
it.

**One illegal byte costs a whole feed.** reader parses with a strict SAX parser,
so a single character XML 1.0 forbids makes the entire document not well-formed.
inventwithpython.com shipped a raw `0x0B` mid-sentence and its 2.7MB, 100-entry
feed failed outright at line 19918. The fetch hook strips exactly the C0 controls
XML forbids and keeps the three it allows (tab, LF, CR), so a feed that was
already valid comes through byte-identical.

⚠ **feedparser reads that same feed happily.** Verifying a replacement URL with
feedparser reported "200, 100 entries, looks great" and the feed then refused to
ingest. When checking whether a feed will work, check with *reader*.

**A feed that's merely malformed doesn't have to fail — reader was throwing
away recoveries feedparser already made.** feedparser's loose parser is a
built-in fallback for exactly the "one illegal byte" case above and others
like it (an unescaped `&`, a mismatched tag) — it kept working the whole time.
The loss was downstream: `reader`'s `_process_feed` raises `ParseError` on
*any* `bozo_exception` outside its own two-item survivable whitelist
(`CharacterEncodingOverride`, `NonXMLContentType`), discarding whatever the
loose parser recovered along with it. A folder-by-folder audit of the live
library (2026-08-22) found 13 feeds — one with 368 entries sitting behind a
single bad token — permanently marked dead by this policy alone, with nothing
wrong on the source side. `SanitizingFeedparserParser.__call__`
(`services/reader_sanitize.py`) now calls `_accept_recovered_bozo(url,
result)` before handing off to `_process_feed`: if the loose parser recovered
a real `version` and `entries` despite the bozo flag, it clears the flag and
lets the recovered content through (with a warning logged) instead of raising.
A bozo feed that recovered *nothing* — an HTML error page swapped in for the
XML, a dead FeedBurner redirect — still raises exactly as before; the override
only fires when there's something real to keep.

**Announcing yourself as a crawler invites a fake 404.** Auto-discovery used to
send `Lectio/1.0 (RSS auto-discovery; +…)`, and filters match on that phrase:
chickensoft.games returns a fabricated 404 to any UA containing it while serving
200 to `Lectio/1.0`. Discovery was the only part of Lectio sending it, so it was
the only part that could not read the site. The damage went past a failed lookup
— `probe_url` reported "server denied the request", `refusal_is_forceable()` read
that as the site refusing us, and Add Feed offered **Subscribe anyway**, the
husk-feed path the add-feed code explicitly warns against, while suppressing the
page-feed offer that was correct. Discovery now sends the same honest identity as
every other fetch. Still names the app and links the repo; only the description
of the activity is gone.

## Feed auto-taggers

Three functions run at startup to apply strategy and display defaults without user action:

- `_auto_tag_artwork_feeds()` — matches `artstation.com` and `deviantart.com` feed URLs → `strategy=artwork`.
- `_auto_tag_webcomic_feeds()` — matches feeds in folders whose name contains "comic" → `strategy=webcomic`. Artwork wins if both conditions apply.
- `_auto_tag_github_release_feeds()` — matches `github.com/*/releases.atom` URLs → `strategy=og_scrape` + `show_lead_image_as_thumb=0`. GitHub generates a unique social-preview card per release; thumbnails are suppressed because the card is contextual rather than a post image.

All three skip feeds where `feed_lead_image_strategy.manual=1` (user has explicitly chosen a strategy in Feed Properties). To add a new tagger, follow the same pattern and register it in `lifespan()`.

## Feed-provided tag suggestions (`entry_feed_tags`)

`reader` discards entry categories at ingest — its `Entry` type has no tags
attribute — so Lectio captures them at the only point the raw feedparser result
exists: `SanitizingFeedparserParser.__call__` hands `(entry_id, tags)` pairs to an
**injected sink** (`set_entry_tag_sink` → `FeedTagService.record_entry_tags`),
keeping services free of main/DB imports.

- **Tenancy for free.** Parsing runs synchronously inside `reader.update_feed(s)`,
  always in a user context, so the sink's `get_meta_connection()` resolves the
  right per-user DB at call time. The service itself is tenancy-unaware.
- **Id mapping re-derives, never zips.** `_process_feed` skips unparsable
  entries, so positions do not line up. A sink failure is logged and swallowed —
  tag capture must never fail a feed parse.
- **Shopify's `<s:vendor>` counts as a category.** A storefront collection feed
  (`…/collections/<name>.atom`) files the maker in the Shopify product namespace,
  never in `<category>` — and on a record shop that element is the *artist*, the
  one thing worth tagging. feedparser flattens an unknown namespace using the
  document's own prefix, so the key is `s_vendor` for the usual `xmlns:s`;
  `_shopify_vendor_tags` matches any `<prefix>_vendor` rather than betting on one
  spelling, and takes plain strings only (a structured value belongs to some other
  schema's `vendor`). A bare `vendor` key is ignored — unnamespaced, it could mean
  anything.
- **neowin.net's `<neowin:tags>` counts too — same shape, a hashtag list
  instead of one name.** Raised 2026-08-31 rechecking an earlier "no can do":
  Neowin ships `<neowin:tags>#OpenAI #ChatGPT #Ads</neowin:tags>` instead of
  `<category>`, invisible to feedparser's `.tags`/`.category` the same way the
  Shopify vendor field is. `_prefixed_hashtag_field_tags` mirrors
  `_shopify_vendor_tags`'s `<prefix>_vendor` matching but for `<prefix>_tags`,
  splitting the value on whitespace instead of taking it whole (a leading `#`
  is stripped by the shared `_clean_tag_values`, same as any other tag).
- **Storage.** `entry_feed_tags(feed_url, entry_id, tag, first_seen_at)`, tags
  stored **raw** and normalized only at display, because the raw text is the
  foundation for tag-filtered feed adapters. Replace-per-entry semantics, so
  publisher edits propagate; entries outside the fetch window keep theirs.
- **Synthetic feeds** (dev.to, DeviantArt) emit `<category>` in their generated
  RSS and flow through the same capture — one code path. ⚠ That XML is
  regenerated from their per-user entry tables, so tags must persist in those rows
  or they never reach `<category>`. DA's browse/gallery API omits deviation tags.

### What gets dropped at capture

**Numbers-only tags**, from feed categories and page scraping alike — they are
comment counts, post ids, pagination and bare years. Anything with a non-digit
survives (`80s`, `2020 election`, `Windows 11`). A stray `84` reached lemire.me
this way; 580 stored rows were bare numbers.

**An archive year-list, as a run.** nwcpp.org carries 2000–2026 down every page
and all sixteen landed on one post. Five or more distinct 4-digit years on a page
is a sidebar, not a tag set, so the whole run goes rather than judging any year
alone.

**Subscriber-only stubs are detectable without a marker.** Substack publishes
none, but ships a body containing only a "Read more" link back to the post.
`is_paywall_stub` requires both a body under ~120 characters *and* that its only
link points at the entry's own URL — which keeps it off a genuinely short post,
since a link roundup points elsewhere. On abortretry.fail, 17 of 20 items were
9-character stubs against three real posts of 19k–38k. `hide_paywalled` marks them
read at fetch, mirroring `hide_shorts`: non-destructive, findable under All, and
opt-in because a *partial* feed is all stubs by design.

### Suggestions are never auto-filtered, and that is a decision

Two heuristics were built and both reverted (`1381cbc`, 2026-07-29):

1. **Coverage** — suppress a tag carried by ~every entry. Caught the motivating
   cases exactly (`Popular Deals` on 2,525 slickdeals posts, `VinylDeals` on 576)
   and then hid `Lessons` on a guitarplayer tag feed, where it is precisely the
   right tag. These chips are for **filing**, not for telling entries apart, so
   uniformity is not disqualifying.
2. **Feed-name echo** — uniform, and the tag's tokens are a subset of the feed
   title. Failed identically: a tag feed puts its tag in its own name ("Latest
   from Guitar Player in Lessons"). Matching the URL fails too — `/r/VinylDeals/`
   and `/feeds/tag/lessons` have the same shape.

`VinylDeals` is a place, `Lessons` is a kind of content, and nothing in feed
metadata carries that distinction. ⚠ The asymmetry picks the default: **an
unwanted chip is cheap, because it is ignored; a wanted chip that is hidden is
invisible.** Resist a third heuristic — the first two each looked convincing
against the data that motivated them.

The × records the decision in `suppressed_feed_tags`, compared through
`normalize_tag_value` on **both** sides. Chips render normalized, so the × sends
`popular-deals` while the stored tag is `Popular Deals`; a plain lowercase compare
yields `popular deals` and misses — every **multi-word** tag reappeared after
dismissal while single-word ones stuck, because those normalize to themselves.
Per feed, not global (`Forum` is noise on Slickdeals, a topic elsewhere). It hides
a chip, never a fact: the rows stay and keep feeding the adapters. Undo lives in
Feed Properties → **Hidden tags**, because a mis-clicked × needs a way back.

### The chip row, and the tag-filter rule

Chips render as **[ + tag ▲ ▼ ]**. **+** applies the tag manually. **▲/▼** edit
the **feed-scoped** `tag_filter` rule in place: same sign removes, opposite flips.
The rule is created **disabled** — chips are a tuning surface, the user arms it in
Automation — and deleted when the spec empties. Folder/global rules are never
touched. Only an already-enabled rule applies a chip edit to unread entries
immediately.

**Every captured tag reaches the row; only the first eight show.**
`MAX_FEED_TAG_SUGGESTIONS` was a *fetch* cap of 8, so anything past the eighth did
not exist client-side. Rock Paper Shotgun ships **28 tags per post** and puts `PC`
tenth — so the row offered every platform to drop and no way to name the one to
keep, which is exactly the `+pc` rescue the drop needs. The cap is now 40 and
`FEED_TAG_CHIPS_COLLAPSED` (8) governs display, with `+N more`. Hidden rather than
omitted, for the same reason there is no auto-suppression.

**The `tag_filter` spec** lives in `keyword` as one comma-separated field with
three strengths: `-tag` **drops**; `+tag` (or bare) is **good** — it rescues from a
drop but its absence never cuts; `++tag` **requires** — tagged entries lacking
every required tag are cut. Commas separate, so multi-word tags are typed as-is
(`+windows 11, -rust`). Evaluation is requires → drops, and **untagged entries are
always kept**: a feed that stops tagging must not have its firehose suppressed.
The author rides along as a pseudo-tag (`by-steven-parker`), so author tokens work
in every position. Runs after `update_feed`, since the sink fires during parse.

**Writing a spec needs autocomplete, because the failure is silent.** A spec can
only match what ingest captured, stored lowercase-hyphenated, so typing against an
unseen vocabulary (HackerNoon: 140 distinct tags in 20 items) is guesswork — and a
rule matching nothing looks exactly like one that works.
`GET /rules/tag-vocabulary` resolves the draft's scope through the same
`resolve_rule_feed_urls` the rule will use and returns the vocabulary **normalized
through `normalize_tag_value`**, so a completed suggestion matches by
construction, plus per-tag entry counts — the actual decision (a tag on 9 of 10
posts is a filter; on one it is noise). Counts merge across casing variants.
Loaded lazily on focus, keyed by scope.

The same `attachTagAutocomplete` powers the per-entry input; the rule form differs
only where the grammar genuinely differs — comma separation, and the `-`/`+`/`++`
sign, which is part of the *spec* and survives completion untouched (unlike the
per-entry `#`, which is decoration and is overwritten). It consumes keys with
`stopImmediatePropagation`, not `preventDefault`: the rule form binds Enter-to-save
on the same element, and a listener can only stop one registered after it — so the
autocomplete must be attached first, and is.

**The dry run explains an empty result**, because a bare zero teaches nothing.
`-mac, +pc` reads as "drop Apple, keep PC" and is the natural first spec — but RPS
tags *platform availability*, so all 41 Mac-tagged posts are also tagged `PC` and
the rescue cancels the rule. `_run_tag_filter` counts entries a drop caught that a
good/required tag let through and returns `rescued` + the top `rescued_by` tags. A
**good-only** spec (`+wallpapers`) cuts nothing by construction — good tags rescue
and whitelist nothing — yet reads naturally as "keep these", so `good_only` is
returned and the panel names the two specs with teeth. Diagnostics, not a policy
change: `+android, -iphone` must keep a post tagged both.

**Source-page fallback.** Entries whose feed never delivered `<category>` (aged
out before capture, or a tag-stripping publisher) are tagged from the article page
on open: `extract_page_tags` harvests `article:tag` / `keywords` / `parsely-tags`
from the lead-image service's source-HTML cache — zero extra requests when primed,
and on a miss the tags appear next open, the same deferral as image captions. Only
runs when the entry has no rows, so feed tags stay authoritative.

## FakeFeedz entries get the article's own date

A listing page is a wall of links: titles and hrefs are there, dates usually are
not (chickensoft.games shows none at all on `/blog`, only on each post). Stamping
every scraped entry with the scrape time made a fresh feed look like its whole
backlog was published the second it was added, and made sorting by date
meaningless — ten entries all reading `2026-08-12 23:18:17`.

`_article_published_at` fetches each **new** entry's page and asks it for a date,
trying the publisher's own metadata first (`mine_publish_date`: JSON-LD,
`article:published_time`, `<time datetime=…>`) and the date the page merely
*prints* second — the same order the re-fetch path uses. Cost is one fetch per
new entry: an entry already in `scraped_entries` is never re-fetched, so a steady
feed pays nothing per refresh and only the first scrape pays for its backlog. Any
failure falls back to "now", because a missing date must never cost the entry.

`publish_date.from_visible_text` needed a second tier for this. Its matcher wants
an element whose `class`/`id` says `date`/`posted`/`byline`, which utility-CSS
frameworks never provide — the byline lives in
`<p class="text-[var(--color-muted-foreground)] text-sm font-serif">April 26, 2026</p>`.
The fallback accepts an element whose **entire text is a date and nothing else**,
which is a comparably strong signal without matching the dates scattered through
comment timestamps, related-post rails and copyright footers. `Updated April 26,
2026 by Chris` does not qualify. It runs only after the labelled pass, so a
publisher that marks its date up properly still wins.

## YouTube videos that haven't premiered yet

`services/youtube.py`'s duration lookup (`videos.list`) also requests `part=snippet,liveStreamingDetails` — free on the same 1-quota-unit call as `contentDetails` — and caches `snippet.liveBroadcastContent` (`upcoming`/`live`/`none`) and `liveStreamingDetails.scheduledStartTime` alongside duration in `youtube_video_duration`. A video stays `upcoming` until it actually airs; since its duration is also `NULL` until then, the existing "retry a stale negative" logic (`_NEGATIVE_RETRY_SECONDS`, 6h) already re-polls it whenever its feed gets refreshed.

That's not frequent enough on its own: a feed's own refresh cadence can run far slower than 6h (backoff, a low-traffic channel), which could leave a premiere's status stale for hours after it actually airs — noticeable specifically because the countdown text recomputes live from the cached schedule on every render, so a stale "upcoming" status shows "Premieres soon" indefinitely past the scheduled time rather than flipping to a real duration. `YouTubeDurationService.refresh_upcoming_videos()` is the fix: an hourly, feed-independent sweep (wired into `_daily_maintenance_loop`'s existing 30s-tick thread, tracked with its own `time.monotonic()` interval) that re-polls every video still cached as `upcoming` directly, batched the normal way (up to 50 ids/call). Cheap in practice — rarely more than a handful of upcoming videos across a whole library at once.

**Both this and `fetch_and_store_durations_for_feed` guard against a real data-loss trap**: `get_video_durations_batch` returns `{}` when there's no YouTube API key resolvable for the current tenancy context (a background user with none configured), and the naive `results.get(vid, (None, None, None, None))` fallback would then write blanks over every id in the batch. For a normal (non-upcoming) video this mostly self-heals — a video with a known-positive cached duration is filtered out of `to_fetch` before the call, so it's never at risk — but an `upcoming` video's duration is `NULL` by design, so it never ages out of `to_fetch`/the upcoming-sweep's candidate set even after a successful fetch. Without an explicit `if not results: return` guard before the per-id loop, a second background user with no key hitting the same tick would blank a freshly-fetched `live_broadcast_content`/`scheduledStartTime` right back to null. Both methods bail out early when the batch call returns nothing at all, rather than treating an empty dict as "every id is now null."

The `[MM:SS]` title prefix becomes `[Premieres in Xd]` for an `upcoming` video, computed from the cached `scheduledStartTime` **at render time**, not stored as text — so it self-corrects if YouTube moves the date instead of drifting stale the way a baked-in "Live in N days" string would (`_youtube_premiere_prefix`).

`hide_unpremiered` (per-feed `feed_display_prefs` column + `yt_hide_unpremiered_global`, same shape as `hide_shorts`) is deliberately **not** implemented as mark-as-read like `hide_shorts` is. `hide_shorts` marking read is a permanent, intentional hide; an unpremiered video should come back once it airs, so this is a render-time filter inside `list_entries_for_feeds`'s phase-1 loop (before the sort+limit clip, so a filtered row can't steal a display slot from a real entry) — the entry stays genuinely unread and reappears the moment `liveBroadcastContent` flips away from `upcoming`.

**The "still upcoming" exemption is unconditional, independent of `hide_unpremiered`.** `_youtube_unpremiered_video_id` (feed_url, link) → video id gates four places: `_prune_entries`'s read-cutoff and published-cutoff lanes (alongside starred/tagged/archived), and the three bulk mark-read sweeps — `mark_feeds_as_read` (Mark Folder/Feed as Read), `mark_entries_range_read` (Read above/below), `mark_entries_older_than_read` (Mark older than X). A delayed premiere must not get purged by folder retention or swallowed by a blanket mark-read before it ever airs, whether or not the display toggle is on. Manual single-entry mark-read (opening/clicking one post) is untouched — that's explicit user intent, not a sweep.

## dev.to filtered feeds

Dev.to's RSS (front page and per-tag) is an unfiltered firehose that mixes languages, while its public unauthenticated JSON API (`GET https://dev.to/api/articles`) exposes a per-article `language` label, reaction counts, and a `top=N` ranking window. `services/devto.py` follows the DeviantArt/FakeFeedz synthetic-feed pattern: one polite API request per refresh, client-side filtering (the API ignores `?language=`; we filter on dev.to's *own* `language` field, deliberately not our own detection), then render to `file://` RSS under `DATA_DIR/devto-feeds/` for `reader` to ingest. Per-feed config (tag, top-window days, English-only, min reactions, tags_exclude) lives in the per-user meta table `devto_feeds`; the Add Feed dialog detects dev.to front-page/tag URLs client-side (mirroring `parse_devto_url` — user/org pages are left to their normal small RSS) and reveals the filter fields, and the config is editable later via feed Properties → Tuning (`POST /devto-feeds/{id}/config`). Cover images seed the lead-image cache via the same sink mechanism as DeviantArt; deletion is dispatched in `purge_orphaned_feed` alongside the other rendered-feed types. Filter changes shape what arrives from then on — already-ingested entries are kept.

## DeviantArt integration

DeviantArt's legacy `backend.deviantart.com/rss.xml` is behind a CloudFront WAF that 403s datacenter traffic, so Lectio uses the DeviantArt API and renders results to `file://` RSS files like FakeFeedz (services/deviantart.py). Per-user creds live in app-settings.

**Bluesky image recovery** (`services/bluesky.py`): per-profile bsky.app RSS (`/profile/<did>/rss`) is text-only, and content-labeled posts (e.g. adult) also expose no og:image on the web page. The images live in the post record and are served from the public `cdn.bsky.app` CDN, so Lectio fetches them from the public AT Protocol API (`app.bsky.feed.getPosts`) keyed by the post's `at://` URI — which the RSS feed stores as the entry id. `extract_entry_thumbnail_url` uses the first image for the list thumbnail; `get_entry_detail` appends all images to the article body. No auth and no label check at this layer — subscribing to the account is the user's opt-in. Cached in-memory (1h TTL) so list rendering doesn't re-hit the API.

- **Auth** — OAuth2. Public galleries use the *client-credentials* grant; the *authorization_code* grant (PKCE — DeviantArt requires `code_challenge`) connects the user's account for watch-list access. Tokens are stored per-user and auto-refreshed; the token request tries with-secret then without, tolerating both confidential and public clients.
- **Watch feed** (preferred) — one combined feed from `/browse/deviantsyouwatch` (everyone you Watch), instead of one feed per artist. A few paginated calls per refresh keep it under DeviantArt's strict per-user rate limit (`DeviantArtRateLimited` aborts bulk work cleanly; the scheduled refresh is round-robin capped).
- **Add = Watch** — while connected, adding a `deviantart.com/<user>` URL Watches that artist on DeviantArt (it then appears in the Watch feed) rather than creating a per-artist feed.
- **Tags** — browse/gallery responses don't include deviation tags; those need `/deviation/metadata` (up to 50 ids per call). Each feed refresh makes at most one metadata call for never-checked entries (`fetch_and_store_missing_tags`; `tags_fetched_at` is set even on zero-tag results so untagged deviations aren't re-queried forever), stores them on `deviantart_entries.tags`, and the regenerated RSS emits them as `<category>` — from there the standard `entry_feed_tags` capture renders the suggestion chips. The bulk watchlist sync's create path deliberately skips the lookup (adding N artists stays N calls); tags fill in on the first scheduled refresh. A rate-limited lookup is skipped, not fatal — entries land untagged and are retried next cycle.
- **Watch-list sync auto-resume** — `sync_deviantart_watchlist` is add-only and stops cleanly at the rate cap; instead of waiting for a re-click it schedules a background continuation (a daemon `threading.Timer` routed through `_run_in_user_context`) honoring the 429's `Retry-After` (conservative 15-min fallback), capped at 12 rounds per triggering run. A per-user in-process guard keeps the Settings button, the daily maintenance run, and a pending auto-resume from syncing concurrently. Timers don't survive a restart — the daily maintenance sync is the catch-up. The sync also reconciles: subscribed artists no longer on the watch list are reported, never auto-unsubscribed (a curated feed may deliberately outlive a Watch). The reconcile excludes the synthetic combined Watch feed (`source='watch'`, username `deviantsyouwatch`) so it isn't perpetually flagged as unwatched. Failed adds and unwatched artists are persisted structurally in `deviantart_sync_detail` (JSON alongside the `deviantart_sync_status` string) so the Settings → DeviantArt subtab lists them as links to `deviantart.com/<user>` — the user never has to read server logs.
- **Deactivated accounts** — a watched artist who deactivated their DeviantArt account returns `HTTP 400 "Account is inactive."` on gallery fetch (distinct from 404 = deleted/renamed). Such artists can never be added, so the sync would otherwise re-probe and re-fail them on every run. They're parked in the `deviantart_deactivated` table (`_is_da_deactivated_error` detects them), excluded from the sync's `to_add`, and shown as their own "Watched but deactivated" list in the subtab. Daily maintenance (`_deviantart_recheck_deactivated`, capped per run, oldest-checked first) re-probes them: a successful fetch means reactivation → subscribe and un-park; still inactive → bump `last_checked_at`; rate-limited → stop and resume next run.
- **Images** — deviations carry stable (non-expiring) signed `wixmp.com` image URLs. DA feeds are pinned to the `inline` strategy so the article lead image and list thumbnail derive statelessly from the embedded content image (no source-page scrape, nothing to clobber). `wixmp.com` is trusted in `_is_image_url_acceptable` (its long auto-generated filenames/UUIDs otherwise trip the avatar/ad heuristics) and routed through `/api/img`.
- The lead-image cache reads through to its DB table on a miss, so stored images survive restarts (the in-memory cache is seeded once under the default tenancy and otherwise warms lazily).
- The interactive on-open `queue_source_fetch` persists **only a positive result**. A `None` is ambiguous — a transient page-fetch failure is indistinguishable from a genuine "no image" — and this path runs once per opened entry with no retry, so storing `None` would cement a momentary miss as a permanent negative and blank a thumbnail the feed actually has (e.g. a Standard Ebooks cover, which lives in `media:thumbnail` and resolves via the page's `og:image`). Negative-recording is left to the background backfill, which retries on its own schedule.

## Combining feeds moves the entries, not just the curation

`_migrate_curation` used to walk `set(src_tags) | set(src_stars)` — curation,
not entries. A post with no tag, no star and no capture was never visited, so it
was neither matched onto a survivor twin nor synthesized, and it vanished when
`reader.delete_feed` ran. Combining two subscriptions to the same Webtoons comic
silently lost an unread post that way. Dropping uncurated entries is correct for
an *unsubscribe*; for a combine — the user asserting these two feeds are the same
feed — it is not, so the loop now walks every source entry.

Two things make that safe at scale. **Read state is carried, not reset**, or
combining an old feed would dump its entire history into the survivor's unread
count; a survivor twin that is already read is never resurrected as unread by an
unread source copy. And **per-entry meta follows the entry**
(`_rekey_entry_meta`: lead image, captured feed tags, content/title/date/link
overrides, media, read state, history), because a post that arrives with no
thumbnail and its hand-made title correction reverted is a worse outcome than
one that did not move. The re-key omits INTEGER PRIMARY KEY columns — copying
`read_history.id` verbatim collides with the row being copied from, so
`INSERT OR IGNORE` drops the copy and the follow-up `DELETE` loses the row.

## Combining feeds carries the offline captures

`_migrate_curation` moves a removed feed's manual tags and stars onto the survivor, and now its **starred-archive rows** too (`rekey_archive`, which carries the asset links and refuses to clobber a capture the survivor already has). Without that the captures stayed keyed to a feed that was about to be deleted: the articles were fine, but their offline copies became unreachable and the Saved view rendered them as archive-only *orphans* — from the archive row's own stale `link`, which is how it surfaced, as a combined feed's articles still showing their old dead URLs. Measured on the live library 2026-07-25, past combines had stranded **85** of them; `scripts/repair_orphaned_archives.py` re-attached them (64 re-keyed, 3 where the stranded row was the *better* capture and replaced a thinner twin, 18 redundant drops, 14 links refreshed).

Two subtleties the fix has to respect. The migration loop walks *curation*, not entries, so a capture on an entry carrying neither star nor tag is never visited — a sweep after the loop re-keys those, but only when the article exists on the survivor; synthesizing an entry purely to host a capture would put an uncurated row in the survivor, and the orphan view is already that capture's home. And the archived id set is read **once** up front, because the per-entry alternative opens an archive connection for every entry just to learn that most have nothing to move.

## WebSub (PubSubHubbub)

`WebSubService` (`services/websub.py`) implements the WebSub subscriber protocol:

1. **Hub discovery** — on feed add and periodically during refresh, `_discover_hub_url` fetches the feed URL and looks for `rel="hub"` in the HTTP `Link` header or in `<atom:link>` / `<link>` XML elements. A "no hub found" attempt is recorded in `websub_subscriptions.hub_tried_at` so the check is not repeated for 7 days.
2. **Subscription** — `subscribe(feed_url, hub_url)` posts `hub.mode=subscribe` with a random HMAC secret and a 7-day lease request. The row is written as `verified=0` until the hub confirms.
3. **Verification callback** (`GET /websub/callback`) — hub sends `hub.challenge`; `handle_verification` confirms the topic matches, marks the row `verified=1`, and echoes the challenge. FastAPI query-alias params (`hub.mode`, `hub.topic`, `hub.challenge`, `hub.lease_seconds`) map the dot-notation params cleanly.
4. **Push callback** (`POST /websub/callback`) — hub delivers content; `verify_push_signature` checks HMAC-SHA256 (or SHA1 fallback) against the stored secret. On success, for each subscriber `feed_refresh_service.update_feeds([feed_url])` fetches the new entries, then `_run_automation_after_refresh({feed_url})` runs the rules (mark-read, tag-filter, dedup, guid-churn suppression) on them — the same post-refresh step every scheduled/manual refresh performs. Omitting it (the state before this was fixed) meant WebSub-delivered entries bypassed all automation, so rules on prolific push publishers (e.g. realpython.com, which delivers almost entirely via push) effectively never fired.
5. **Lease renewal** — `renew_expiring_subscriptions()` re-subscribes any verified row whose `expires_at` is within 24 hours. Called each refresh cycle.

**Multi-user fan-out:** the callback URL carries only the topic (no user). `websub_subscribers` lists every user subscribed to a feed; both callbacks fan out across those rows rather than acting on a single tenant. `_websub_verify_fanout` confirms the handshake for whichever user(s) have a matching pending subscription; `_process_websub_push` collects every user with a verified subscription for the topic, confirms the push is authentic against *any* of their secrets (a forged push matches none), then refreshes each subscriber under its own tenancy context in a daemon thread. Because the shared callback means the hub only retains the most recent subscriber's secret, validating against any one secret and fanning the refresh out to all subscribers is what lets several users share a single hub subscription.

The service is initialized only when `LECTIO_PUBLIC_URL` is set; all integration points in `main.py` guard on `if websub_service`. On feed removal, `purge_orphaned_feed` sends an active unsubscription request to the hub (best-effort HTTP POST; hubs expire leases anyway if the request is lost).

**Feed removal lifecycle:** `purge_orphaned_feed(reader, conn, feed_url, *, archive_pending, rescue_to)` is the single canonical sequence run whenever a feed leaves the system (confirmed orphaned — no remaining `folder_feeds` rows). Steps in order: (1) force-archive pending saved/starred entries; (2) rescue unread entries into a kept/canonical feed; (3) dispatch the delete via the appropriate path (DeviantArt rendered feed → `deviantart_service.delete_deviantart_feed`; dev.to rendered feed → `devto_service.delete_devto_feed`; scraped/FakeFeedz feed → `scraper_service.delete_scraped_feed`; plain feed → `reader.delete_feed`); (4) WebSub unsubscribe. Callers set `archive_pending=False` when entries survive under a kept URL (dedup, format-upgrade), and pass `rescue_to` to migrate unread state. The helper takes an already-open `reader` and `conn` so callers control the `with` scope and context-manager nesting is never doubled.

**Declined feeds — a genuine unsubscribe must not come back on the next Ino
sync.** `_inoreader_drip_step`'s subscriptions phase and the local-file Ino
importer (`_run_import_loop`) both `reader.add_feed(furl, exist_ok=True)` for
every feed Ino still lists as subscribed that's missing locally — with no
check for *why* it's missing. Bit twice in one day (2026-08-23/24): an initial
full import, then a later recovery re-sync, each re-added the same ~394 feeds
Josh had deliberately unsubscribed from over time. The `declined_feeds
(feed_url TEXT PRIMARY KEY, declined_at TEXT)` table (`main.py`,
`ensure_meta_schema`) closes this — the feed-level equivalent of
`dedup_dismissed` for entries (below). It is written (`INSERT OR REPLACE`,
so a re-decline refreshes the timestamp) only from the three call sites that
represent a genuine user decision: the `/feeds/unsubscribe` route, `bulk_feed_action`'s
`unsubscribe` action, and `delete_folder(feed_action="unsub")`. Both Ino
import paths load the set once per subscriptions-phase run and skip any
`furl` present in it instead of re-adding.

Two call sites deliberately do **not** record a decline: `purge_orphaned_feed`
calls from the dedup/merge/format-upgrade paths (`archive_pending=False`,
`migrate_curation_to` set — the content survives under the surviving feed, so
nothing was declined), and `remove_feed_from_folder`, which is only ever
invoked as the YouTube-sync auto-removal callback when a channel drops off the
subscribed list on YouTube's own side — recording a decline there would stop
Lectio re-adding a channel the user re-subscribes to on YouTube later. The
generic multi-integration importer (`_apply_migration_items`, shared by
Miniflux/FreshRSS/TTRSS) is out of scope for the same reason `declined_feeds`
itself is Ino-specific: those imports are a distinct decision context, not a
resync of a subscription list Lectio itself once held.

**Folder deletion:** `delete_folder(folder_id, feed_action, move_to_folder_id)` deletes a folder and its descendants. When the folder holds feeds the UI prompts for their fate: `feed_action="unsub"` (default) purges feeds that end up orphaned via `purge_orphaned_feed`; `feed_action="move"` reassigns every affected feed to `move_to_folder_id` without unsubscribing. A target of `UNCATEGORIZED_FOLDER_ID` (or the root folder) leaves feeds folderless (Uncategorized). Returns `(deleted_folder_count, unsubscribed_count, moved_count)`. The empty-folder case skips the prompt (simple confirm).

**Push indicator:** `get_push_active_feed_urls()` queries `websub_subscriptions` for `verified=1 AND hub_url IS NOT NULL` in one pass and returns a `set[str]`; the index route threads this into the template context so both the sidebar feed tree and Settings → Feeds can render the ⚡ glyph without per-feed queries.

Storage: **shared** `lectio_websub.sqlite` (not per-user), two tables:
- `websub_subscriptions (feed_url TEXT PK, hub_url, secret, lease_seconds, subscribed_at, expires_at, verified, hub_tried_at)` — one row per feed, one active hub subscription regardless of how many users subscribe to that feed.
- `websub_subscribers (feed_url, user_id, PRIMARY KEY (feed_url, user_id))` — the N-user fan-out list; push and verification callbacks iterate this table.

Startup migration copies legacy per-user `websub_subscriptions` rows idempotently into the shared DB.

## Removing a feed has to clear the record that it was failing

`purge_orphaned_feed` cleaned `kept_feeds`, `feeds_needing_replacement` and
`folder_feeds` but not `feed_failure_state`, so a feed unsubscribed *because* it
was dead counted as failing forever (560 rows from one sweep). Safe to drop: it
is derived state rebuilt on the next fetch, and a feed with no reader row has no
next fetch.

**Ghost is defined against reader, not folders** — unsubscribe-with-keep
deliberately leaves a real feed with no folder row.
`scripts/clear_ghost_failure_state.py` cleared the backlog. Sibling tables still
outlive their feeds (`feed_lead_image_strategy` 1,024 ghost feeds,
`feed_fetch_history` 754, …); untidy rather than wrong, and the API id maps and
fetch history are not obviously safe to drop.

## A tag has to survive an unsubscribe, because a star does

A star is a `saved_entries` row in the meta DB and survives the feed's deletion;
a tag lives in reader's own `entry_tags` and is deleted *with the feed*. So a
tagged-but-unstarred entry came out of an unsubscribe with its capture intact and
its tags gone — which makes it read as carrying no keep signal, and therefore
eligible for `purge_uncurated_orphan_archives.py`.

`_carry_tags_to_orphan_archive` copies them into `orphan_entry_tags` (the table
the UI already writes when you tag an entry whose feed is gone) before the
delete. Gated on a surviving capture — the same test `_purge_dead_entry_meta`
uses — and skipped when `migrate_curation_to` is set, where tags follow the
entries instead. `orphan_entry_tags` is absent from `_DEAD_ENTRY_META_TABLES` so
the sweep afterwards does not undo it.

## Unsubscribing has to be able to actually remove things

Once tags survived, *every* exit from the dialog left the posts in Saved, because
a star or tag preserves the capture. `drop_all_curation` is the fourth choice.
Order is load-bearing:

1. **tags first** — `apply_star_state(False)` consults `entry_has_keep_signal`
   and would refuse to release the capture while a tag remains;
2. then the star;
3. then the archive **synchronously** — `enqueue_removal` only marks
   `pending_removal` for a worker, and the feed is about to disappear.
   `delete_archive` keeps assets another entry references.

`archived_entries` needs no handling: with every capture gone,
`_purge_dead_entry_meta` drops all per-entry meta in one pass. It is the dialog's
only irreversible choice, so it carries a `confirm()` and disables the re-star
checkbox.

## "Re-star" is two operations, not one

Unsubscribing can bring curated posts back to the top of the Inbox, which orders
by `saved_entries.saved_at`. `restar_curated_entries` does two different things:

- **tagged but unstarred** — no row to update, so a date bump does nothing. It
  must be genuinely starred, which is also what enqueues its capture.
- **already starred** — `apply_star_state` is `INSERT OR IGNORE`, so it would
  leave the old timestamp. That row is re-stamped directly.

Deliberately **not** unstar-then-star: unstarring an entry with no other keep
signal enqueues removal of its capture. Runs before the feed is removed, off by
default, and the checkbox resets on every open (the modal is reused).

## A kept post has to say its feed is gone

A **kept feed** still exists in reader but is hidden from the tree; an **orphan
archive**'s feed is gone entirely. Both mean there is no subscription behind the
name, so both render `Feed Name (unsubscribed)`.

The test is membership of `get_all_reader_feed_urls(include_kept=False)`, *not*
"has no folder row" — a feed in no folder is still subscribed via Uncategorized.
Scoped to the feeds on screen, and rendered as a separate `<span>`: the feed name
also appears in search, exports and the tree, and baking a status word into it
would leak everywhere.

## Moved here from saved.md

**A URL can carry a month without carrying a day.** `url_inferred_pubdate` reads
the `/YYYY/MM/DD/` permalink; `url_inferred_pubmonth` reads `/YYYY/MM/` and
resolves it to the first of the month. The day is a placeholder, the month is not
— WordPress generates the permalink from the publish date. It is the last tier in
`recover_publish_dates.py` for that reason, but on blog.guitar-pro.com (67 of the
68 entries it recovered) it is also the *only honest* signal: those pages publish
`dateModified` and nothing else, so mining the page would have dated a 2021 post
to October 2024. A real month beats a precise-looking lie.

## Refresh pacing

- **Refresh backoff & high-fanout pacing** (`FeedRefreshService.update_feeds`) —
  failing feeds get exponential *per-feed* backoff. A coarse *per-domain* backoff
  also exists for hosts that go down, but it is **exempt for high-fanout hosts**
  (>= `_HIGH_FANOUT_DOMAIN_FEEDS` feeds in the batch, e.g. ~700 youtube.com subs):
  reader doesn't reliably expose an HTTP status on its exceptions (real YouTube
  404s arrive with status `None`), so a few dead channels would otherwise look
  like transport failures and lock the whole domain — which starved every
  subscription for days. Per-feed backoff handles the dead ones; the domain guard
  is kept only for small hosts, and there it activates only after several
  consecutive failures, caps at 1h, and clamps stale locks at read so they
  self-heal. Requests to a high-fanout host are also **paced**
  (`_HIGH_FANOUT_PACE_SECONDS`) so a big serial burst isn't throttled into
  spurious 404s (YouTube 404s a ~700-request burst though each feed is fine
  singly) — a polite-client measure, feeds to other hosts interleave at full speed.
- **`bypass_backoff`** (`FeedRefreshService.update_feeds`) — skips the feed- and
  domain-level backoff checks above, but not reader's own `update_after`
  (Retry-After/Cache-Control, a real instruction from the site). Wired only into
  the single-feed manual `/refresh/feed` route: a deliberate click on one feed is
  a single polite request, the same reasoning already used for a never-updated
  feed's first fetch. The scheduler and the bulk `/refresh/folder` route stay on
  the default (respect backoff) — bypassing a whole folder's backoff in one click
  would hit every backed-off feed on it at once, a different blast radius.
  Without this, a feed that recovered *after* its last failed attempt stayed
  reported as failing — and Refresh silently did nothing — for up to the 24h
  backoff cap.

## Outbound proxy escalation

Four tiers, in order, each only reached once the one before it was already
tried and still failed: honest UA → browser identity → outbound proxy →
FlareSolverr → last-resort backend. The first two are wired into
`services/reader_api.py`'s request hooks directly; the last three are
`FeedRefreshService.update_feeds`' `on_fetch_still_blocked` /
`on_bot_challenge_still_blocked` / `on_fetch_still_blocked_via_proxy`
callbacks, each mirroring the one before it one rung further (same-cycle
retry on newly-flagged, same fall-through when already flagged from an
earlier cycle). FlareSolverr's gate is narrower than the other two —
`_is_bot_challenge`, a real challenge page specifically
(`bot_challenge.FeedBlockedError`), not `_is_refusal_or_challenge`'s broader
"any refusal" — so a plain IP-block 403 skips it and falls straight through
to last-resort; spinning up real Chrome to solve a challenge that was never
served is wasted effort, and the failure it wants (IP reputation) needs a
different exit IP, not a browser.

**Settings shape**: `SETTING_PROXY_URL`/`SETTING_FLARESOLVERR_URL`/
`SETTING_TAILSCALE_URL` (all admin-only — a non-admin choosing an arbitrary
proxy/solver target is SSRF-adjacent) + `SETTING_PROXY_MODE`
(off/as_needed/always, per-user-overridable). FlareSolverr and the
last-resort backend both ride the *same* `proxy_mode` rather than their own —
Josh's own framing was to mirror the existing pattern, not invent new mode
controls — and are reachable only via `as_needed`'s per-feed escalation,
never `always` (which would spin up real Chrome, or route every fetch
through a real home IP, on *every* fetch — the whole point of per-feed
escalation is not doing that preemptively). Each tier's actual opt-in gate is
simply being configured at all: `_flag_flaresolverr_feed_on_still_blocked`/
`_flag_tailscale_feed_on_still_blocked` require their own URL non-empty, same
as the primary proxy requiring `get_proxy_url()`.

**FlareSolverr is not a proxy swap — it redirects the whole request**
(`ReaderApi._make_flaresolverr_request_hook`, `_fix_flaresolverr_response`).
Unlike gluetun/Tailscale (transparent to `requests` via `session.proxies`),
FlareSolverr is its own API: `POST http://flaresolverr:8191/v1` with
`{"cmd": "request.get", "url": <feed_url>, "proxy": {"url": ...}}`, returning
real Chrome's rendered `outerHTML`, not the origin's raw bytes. The request
hook rewrites method/url/headers/body outright (not just headers or
`session.proxies`) and stamps a `_lectio_via_flaresolverr` marker; the
response hook (registered *before* `_fix_feed_response`, so its output gets
the same cleanup as any other fetch) only acts on marked requests, unwraps
the JSON envelope, and propagates the *origin's* real status code (not
FlareSolverr's own 200) so refusal detection upstream still works for a site
that's still blocking even through a real browser. Always stacked with the
primary proxy when one is configured (`_resolve_flaresolverr_for_fetch`) —
confirmed empirically that FlareSolverr alone, on the bare VPS IP, failed
outright on a real Cloudflare-protected feed, while stacked with gluetun it
solved the same feed cleanly. `_resolve_proxy_for_fetch` stands down
entirely for a FlareSolverr-active feed, or the request TO FlareSolverr's own
container would get routed through the primary proxy's `session.proxies` by
mistake — the stacking happens inside FlareSolverr's own request body
instead, a completely different mechanism.

**Three more bugs found only by running it against the real container, none
visible from reading the code:**

- **The unwrapped bytes need their own Content-Type set, not left to
  `_fix_feed_response`'s sniffing.** FlareSolverr's own HTTP response is
  `application/json`; `_fix_flaresolverr_response` swaps the *body* for plain
  feed bytes but did nothing to the header at first. `_fix_feed_response`'s
  own HTML→RSS override only fires when it sees `"html"` in the *current*
  Content-Type (the case it was built for — a feed mistakenly served as
  `text/html`), which never matches `"application/json"`. Left alone, reader
  routed the now-XML body to its JSON-feed parser instead of feedparser and
  failed with an empty-document JSON error. Fixed by setting
  `response.headers["Content-Type"]` explicitly in the unwrap step itself
  (`application/rss+xml` when the `<pre>` unwrap matched, `text/html`
  otherwise).

- **The primary proxy's own URL scheme breaks Chrome's `--proxy-server`.**
  `SETTING_PROXY_URL` is `socks5h://...` on purpose — the trailing `h` tells
  pysocks/requests to resolve DNS *through* the proxy rather than locally,
  which the primary-proxy path needs. FlareSolverr passes the same string
  straight to Chrome's `--proxy-server` flag, which only understands plain
  `socks5://`. Left as `socks5h://`, FlareSolverr's own proxy configuration
  silently failed and Chrome fell through to a bare connection-error page
  instead of the real site — no exception, just wrong content.
  `_normalize_proxy_scheme_for_flaresolverr` strips the `h` specifically for
  the value embedded in FlareSolverr's request body; `SETTING_PROXY_URL`
  itself, and everything else that reads `get_proxy_url()`, is untouched.

- **reader's `lazy_init_funcs` fires `_add_response_hook` twice per
  retriever**, registering two copies of every hook for the same fetch — a
  pre-existing quirk of the reader library, not something this feature
  introduced. Invisible until now: the browser-UA and primary-proxy hooks
  only mutate headers/`session.proxies` off the *original* request each time,
  so running twice is a harmless no-op. FlareSolverr's hook is not — it
  mutates `request.url` itself, so the second copy read the *already-
  redirected* request and asked FlareSolverr to fetch its own endpoint
  instead of the real feed (visible as `solution.url` in the JSON response
  being FlareSolverr's own URL, and a "Method not allowed" body). Fixed with
  a self-guard: the request hook no-ops if `_lectio_via_flaresolverr` is
  already set, and the response hook clears that marker immediately after its
  first successful unwrap — the second copy of it then also correctly no-ops
  rather than trying to `json.loads()` the already-unwrapped plain XML bytes
  the first copy left behind.

**The `<pre>` unwrap is empirical, not a documented contract.** A feed is
always served as XML, which Chrome can't render — its `outerHTML` for that
response is its own "view source" viewer, the whole document HTML-entity-escaped
inside one `<pre>`. Verified against a real Cloudflare-protected feed; not
something FlareSolverr's docs promise, just what real Chrome always does with
an XML response. If no `<pre>` is found (the origin
served genuine HTML — still a block page, a login wall, whatever),
the content passes through as-is so `_fix_feed_response`'s own HTML/challenge
detection gets a real look at it rather than guessing in the unwrap step.

**Per-backend unreachable cooldown** (`_active_backend_for_fetch`,
`_mark_backend_unreachable`, `_resolve_proxy_for_fetch`): the primary proxy
and last-resort backend have very different reliability profiles — a
dedicated VPN container is far steadier than a home Tailscale exit node,
which blips for hours or days. A shared cooldown would pause a perfectly
fine primary proxy over a last-resort hiccup (or vice versa), so each is
tracked separately (`_proxy_down_until`/`_tailscale_down_until`).
`_active_backend_for_fetch` centralizes the precedence logic (tailscale
outranks flaresolverr outranks the primary proxy — each is only ever flagged
after the one before it was tried and found wanting) so the resolver(s) and
the unreachable-marker agree on which backend a given fetch actually used,
without the mark step needing the fetch to report it explicitly.
FlareSolverr has no cooldown of its own: `_mark_backend_unreachable` only
ever fires on `socks.ProxyError` (see
`FeedRefreshService._is_proxy_unreachable`), and a FlareSolverr fetch never
touches pysocks at all — it's a plain HTTP POST to FlareSolverr's own
container, which handles any proxy stacking internally. A FlareSolverr
failure just flows into the normal per-feed failure bookkeeping instead
(scoped out deliberately, not an oversight — the containers "run
persistently and idle," per the infra cheatsheet, so downtime is a smaller
risk here than plain slowness, which the existing per-feed backoff already
absorbs).

**Browserless (headless Chrome) was evaluated and NOT shipped** (2026-08-30):
live-tested against two real Cloudflare-protected feeds. Plain `/content`
got stuck on the "Just a moment…" interstitial indefinitely regardless of
wait time (`waitForTimeout` up to 6s, `networkidle2`) — its default Chromium
isn't stealth-patched, and Cloudflare's managed challenge detects it as
automation regardless of exit IP. Stacking it with the primary proxy
(`--proxy-server` launch flag) softened one site's response from a hard
"Sorry, you have been blocked" to the same JS interstitial, but never
cleared it. **FlareSolverr is a different tool solving the same problem
properly** — same "real Chrome" idea, but purpose-built and apparently
stealth-capable where Browserless's stock image is not: stacked with the
primary proxy, it solved both of the same two feeds Browserless couldn't,
confirmed live. Browserless stays unshipped; no evidence found that it adds
anything FlareSolverr doesn't already cover, and it would add its own
complexity for no benefit (its `/content` also returns browser-rendered
HTML rather than raw bytes, the same unwrap problem FlareSolverr already
has to solve).

**Settings → Feeds → Fetch Tiers** (added 2026-08-31) makes the three
escalation tables (`proxy_feeds`, `tailscale_feeds`, `flaresolverr_feeds`)
visible as one page instead of only discoverable a feed at a time via its own
Properties — Josh's ask was specifically to see how much the paid VPN and the
home IP are actually being exposed. Read-only: same lazy-panel pattern as
Stale/Failing (`/settings/feeds/panel/fetch-tiers`, `_settings_feeds_fetch_tiers.html`),
one section per tier showing each flagged feed's title, its own `reason`
(whichever escalation attempt first flagged it), and `flagged_at`.

### Page fetches: a second, deliberately different ladder (`services/page_fetch.py`)

Everything above is about *feeds*. Two other things fetch a single *page* URL
on demand — the tag/lead-image scraper (`_fetch_page_html`) and the
saved-article re-fetch path (`fetch_readability_article`/
`fetch_full_page_article`) — and until 2026-08-31 neither could get past a
browser-identity retry, so a site whose feed is reachable but whose article
pages are Cloudflare-walled (gottadeal.com's tags, three tamriel-rebuilt.org
re-fetches) couldn't be helped.

Not a rewrite of the ladder above: the feed ladder is a flag-and-retry loop
over persisted per-`feed_url` state that an hourly cron escalates through
across cycles. A page fetch is synchronous, single-URL, and resolved (or not)
inside one call, so `PageFetcher.fetch()` runs the whole honest → browser →
proxy → FlareSolverr ladder itself. The one genuinely shared piece — the
FlareSolverr wire protocol — is factored into `services/flaresolverr.py`,
imported by both `reader_api.py` (feeds) and `page_fetch.py` (pages).

- **Host-keyed, in-memory state**, not a new meta-DB table: `HostEscalationState`
  tracks the cheapest tier known to work per `(user, host)` and a 6h cooldown
  once every tier fails — the direct, tier-aware replacement for
  `_fetch_page_html`'s old `_waf_block_until`. The cooldown compares the tier
  available when a host was given up on against the deepest tier available
  now, so a newly-configured proxy/FlareSolverr immediately lifts an old
  block with no invalidation hook. In-memory because losing it on restart
  costs one extra round trip, not a cron cycle — not worth a schema migration.
- **FlareSolverr gated on an actual `bot_challenge` marker**, not any
  refusal — same reasoning as the feed ladder's `_is_bot_challenge`. A plain
  403 (tamriel-rebuilt.org) stops at the proxy tier; a real Cloudflare page
  reaches FlareSolverr.
- **`socks5h://` needs no Chrome-style rewrite on this path** — httpx 0.28 (+
  the `socksio` dependency) accepts it natively for the proxy tier.
  `flaresolverr.normalize_proxy_scheme` is still needed for FlareSolverr's own
  POST body, which Chrome's `--proxy-server` flag does require unprefixed.
- **FlareSolverr is reachable in `proxy_mode="always"` here**, unlike feeds —
  the ladder is reactive by construction (only reached after shallower tiers
  already failed on this URL), so the feed ladder's "don't spend Chrome on
  every fetch" reasoning doesn't apply.
- **No Tailscale tier.** It's the feed ladder's audited, persisted last
  resort; an in-memory, host-keyed background fetch has no business reaching
  the home IP on its own judgment. Revisit only if proxy+FlareSolverr both
  fail somewhere Tailscale would have helped.
- **`max_tier` differs by call site**: `fetch_readability_article`/
  `fetch_full_page_article` default to `"proxy"` (no FlareSolverr) because
  they also run on the synchronous reader-view render path, where a ~55s
  solve would hang a page behind an uncancelable spinner.
  `_refresh_captured_article_for_current_user` (always backgrounded) opts
  into `"flaresolverr"`. The tag/lead-image path always gets the full ladder.
- **`ignore_cooldown=True` on `/articles/refresh-content`** — a deliberate
  click isn't the polite background traffic the cooldown paces. The
  auto-refetch thread and batch worker leave it `False`; they already have
  their own host cooldown/pacing.
- **FlareSolverr's endpoint URL is exempt from `url_guard`; the fetch target
  never is.** `flaresolverr.solve()` asserts the target passes
  `is_safe_outbound_url` before ever reaching the solver, but doesn't check
  the endpoint itself (admin-only config, would refuse any real Docker
  deployment) — same trust basis the feed hook already relies on.
- **Settings → Feeds → Fetch Tiers gained a fourth section** for
  `HostEscalationState.snapshot()`, shown separately from the three feed
  tables above (different key, different lifetime).


## Suggesting a replacement for a feed on a known dead-end host

FeedBurner has stopped 404ing dead feeds; it now serves the origin site's own
homepage HTML back at the feed URL (right content-type-looking header, wrong
content, no redirect). Probing the feed URL itself is useless — the page's own
`<link rel="alternate">` just points back at the same dead FeedBurner address.

`feed_discovery.suggest_feed_migration` instead reads the page's `<link
rel="canonical">` (FeedBurner's passthrough leaves it untouched) to recover the
real origin domain, then runs the same `probe_url` discovery Add Feed and
Change URL already use to find that origin's real, currently-live feed. It
never applies anything — the Failing Feeds panel's "Suggest fix" button
(`GET /feeds/suggest-migration`, gated by `is_known_dead_end_host` so it only
appears on FeedBurner rows) opens Feed Properties with the candidate pre-filled
into the existing Change URL field, so the same verified, user-confirmed flow
applies it.

**Live-checked 2026-08-25, not every FeedBurner failure is this recoverable**:
of ~12 currently-failing `feeds.feedburner.com` subscriptions, roughly a
quarter had no `rel="canonical"` at all (an expired, parked domain; a
JS-rendered SPA with no server-side link tags) — those still need the manual
"risky replacement" judgment call the Failing Feeds panel already supports.
`blogs.technet.com` and `powershell.com/cs/blogs/*`, the other two host
migrations flagged in Plan.md's 2026-08-12 sweep, were **not** built into this:
zero feeds on either host are currently failing, and guessing at a path-mapping
with no live example to verify against risks exactly the "a discovered feed is
not a replacement" trap this same feature is designed to avoid. Add a resolver
for them if/when a real 404'd example reappears.


## Tag aliases

Publishers disagree about the same subject, so one topic arrives under several spellings and filtering on one silently misses the rest. The live library carried `c++` (4,965 uses) beside `cpp` (223), and `c#` (606) beside `csharp` (152).

An alias is applied inside `normalize_tag_value`, which every tag path already runs through — manual tagging, captured feed tags, filter rules, imports — so one row covers all of them. That function is hot (51 call sites, several per entry during a refresh), so the map is cached per user and dropped on write rather than read from the meta DB per call.

`normalize_tag_value_raw` is the same normalization **without** the alias. The editor needs it: once `cpp -> c++` exists, normalizing "cpp" through the aliased path returns "c++", so an alias could never be listed, edited or deleted by the name it was created under.

**Creating one rewrites what is already stored**, in both places tags live: reader entry tags (via the existing rename, which merges rather than colliding) and `entry_feed_tags` rows. A row whose entry already carries the canonical is deleted first, since `(feed_url, entry_id, tag)` is the primary key. Removing the alias afterwards does **not** unwind that — nothing records which entries moved, deliberately: it is a rename, not a filter.

**Chains are refused in both directions.** `_apply_tag_alias` takes exactly one hop, so `a -> b -> c` would leave `a` resolving to a tag that holds nothing. Creating an alias whose canonical is itself an alias is refused, and so is aliasing a tag that other aliases already point at.

Counts in the inventory keep feed-provided and manual tags apart because they live in different stores and a rewrite touches both; one combined number would hide which half is which. Manual counts come from a single grouped query against reader's `entry_tags`, not `get_entry_counts` per tag — there are 33,511 distinct tags.


## Bulk "Edit tags" — add and remove in one pass

`POST /entries/tags-batch` (multi-select context menu, and the single-post
tag icon — same modal, same route) used to only ever append: renamed from
"Add tag" to "Edit tags" 2026-08-31 after Josh found the append-only version
a footgun for editing several posts' tags at once — no way to also drop one
meant falling back to doing each post by hand.

Input now uses the same `+/-tag` convention as the rule editor's tag_filter
spec (`parse_tag_filter_spec`): a leading `-` removes, bare/`+` adds.
`parse_manual_tag_edit_tokens` splits on whitespace/comma (not the filter
spec's comma-only split — multi-word manual tags aren't the concern here the
way a typed filter phrase is) into `(add_tokens, remove_set)`.
`apply_manual_tag_edits` applies them against **each entry's own existing
tags** — a mixed selection has no single "current" state, so removing a tag
one post doesn't have is simply a no-op for that post, never an error. A tag
both added and removed in the same edit ends up removed: the leading `-` is
the more specific, deliberate keystroke.

**Success no longer implies "every entry now has a tag."** The append-only
route could safely mark every touched entry's tag indicator (and its "kept"
state) true unconditionally; a route that can also remove cannot. The
response carries `still_tagged`/`now_untagged` — `[feed_url, entry_id]` pairs
per outcome — so the client updates each post's indicator correctly and only
auto-marks-read the ones that still have a tag (tagging implies keeping/
filing it; losing your last tag is not a keep action).

**The chip picker exists because a multi-word tag isn't what it looks like
typed back.** `normalize_tag_value_raw` collapses internal whitespace to
hyphens, so a tag shown/typed as "science + math" is stored as
`science-+-math` — retyping `-science+math` to remove it silently targets a
tag that was never there. Raised live 2026-08-31 on exactly this case: the
removal appeared to do nothing (the real, differently-spelled tag was
untouched), yet the next open of Edit Tags no longer offered it either (the
attempted removal WAS a real edit — of a normalized string that happened to
match nothing). `GET /entries/manual-tags-batch` (one endpoint for a
selection of any size, replacing a single-entry-only `/entries/manual-tags`)
returns `counts[tag]` = how many of the selected entries carry it; the client
renders one chip per tag, dimmed when `counts[tag] < total`, and a click
toggles `-tagname` in the input by the tag's own stored spelling — so removal
never depends on retyping it by hand.

**A bulk edit has to reach the entry pane, not just the post list.** The pane
renders its own tag chips server-side at load time; the post-list state sync
above (`applyPostItemHasTagsState`) touches list rows, not the open pane.
Raised in the same live report: removing a tag from a post whose pane was
open left the pane showing the stale chip even though the server-side removal
had actually succeeded. The bulk-edit success handler now checks whether the
open pane's `(feed_url, entry_id)` is among the edited entries and, if so,
calls `loadEntryPaneWithoutFullRefresh` — the same in-place refresh the
single-entry tag form already used.
