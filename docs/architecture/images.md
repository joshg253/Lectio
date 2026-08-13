# Images

Choosing, rejecting, sizing and serving the image for a post.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## Floated images, and why margins are not kept

The style allowlist (`_ALLOWED_STYLE_VALUES`) keeps `float` and `clear` alongside
the typographic properties. Blogger emits `clear: right; float: right` on every
side-set image, and dropping it turned a post written *around* a right-set cover
into a centred block with all the text pushed below — a visibly different
article, and the way it was reported (2026-08-02).

Floats are admitted where `position`/`z-index`/`width` are not, and the
distinction is not arbitrary: a float stays inside its container, so it cannot
overlay the app's own UI or escape the pane. It is prose layout.

The author's accompanying `margin-*` is **deliberately dropped**. Margins are
free-form lengths, so keeping them would mean matching a value *pattern* rather
than a literal — the one thing this table promises never to do, and the property
that makes it auditable. The gutter comes from `static/style.css` and
`static/reader.css` instead, keyed off the sanitizer's **normalized** output.
That is why the spacing inside `"float: right"` is load-bearing: the stylesheets
select on `[style*="float: right"]`, and an emitter that ever wrote `float:right`
would silently stop matching every rule with nothing failing.

**The narrow-screen override needs `!important`, and this is not stylistic.** The
float survives as an *inline* style — that is how the sanitizer preserves it —
and an inline declaration outranks any stylesheet rule. Without `!important` the
`max-width: 620px` block is inert and a 45%-wide image stays floated on a phone.
It shipped that way and was caught in a browser at 390px, not by a test; the test
now asserts the `!important` specifically, because a plain `float: none` passes a
naive check while doing nothing.

Read Mode already floated WordPress `alignleft`/`alignright` for the same reason,
so the inline-style selectors were folded into those existing rules rather than
added beside them.

**Preserving the float in the sanitizer was only half of it.** The FIRST image in
a body is also what `_strip_lead_image_opener` hoists into a full-width hero,
removing it from the flow — so the one image a reader is most likely to be
pointing at was the one still losing its wrap. That function already had the
right rule for images further down (*"an occurrence further down is the author
placing it in the flow, which is content rather than a header"*); a float is that
same placement stated explicitly, and it happens to be at the top. A floated
opener is now left where it is and the separate lead is dropped, or the picture
would appear twice. Only the *article* lead is dropped: the list thumbnail is
resolved on its own path, so the post keeps it. "Don't show the lead image in the
article" still outranks the author's layout — that is an explicit instruction.

**⚠ `lead_image_url` after the dedup is a RENDERING decision, not a fact about
the entry.** Every branch that sets it to `None` means "don't draw this twice" —
the lead is already visible in the body, or the author floated it and it stays in
the flow. `get_entry_detail` used to persist that `None` into `entry_lead_images`,
which is where the **list thumbnail** reads from, so an article opened after the
floated-opener change recorded itself as imageless and lost its thumb. 130 live
entries before it was caught. The resolved value is now captured *before* the
dedup (`_resolved_lead_for_cache`) and persisted instead. Anything added to that
function has the same trap.

## Lead image pipeline

`LeadImageService` (services/lead_images.py) resolves a hero image for each entry using a layered strategy:

1. **Feed-level strategy** (`feed_lead_image_strategy` table) — detected automatically and cached weekly. Values: `og_scrape`, `inline`, `media_rss`, `youtube`, `artwork`, `webcomic`, `none`, `unknown`. Two auto-taggers run at startup: `_auto_tag_artwork_feeds` (matches `artstation.com` URLs → `artwork`) and `_auto_tag_webcomic_feeds` (folder name contains "comic" → `webcomic`). Artwork wins over webcomic when both conditions apply. Manual overrides (`manual=1`) are never overwritten by either tagger.
2. **Plugin fallbacks** — site-specific handlers (e.g. YouTube thumbnail from video ID).
3. **Source-page scraping** — fetches the article URL, checks `og:image` / `twitter:image` meta tags (both `property=` and `name=` attribute order), preload hints, CSS background-image, then scored in-page `<img>` tags. A `<link rel="preload" as="image">` hint is used **only as a fallback when there is no acceptable `og:image`** — it's a perf hint and sites often preload an above-the-fold widget/chart that isn't the lead image (e.g. usafacts.org preloads an `answer-page-card` stats chart, which must not override the curated builder.io `og:image` hero). Body scanner decision order: (a) first valid image in document gets a +10 position bonus; (b) when an `<img>` sits inside a `<picture>` with a `<source type="image/webp">`, the WebP srcset URL is substituted as the candidate. Logo/site-chrome rejection uses `_LOGO_URL_PATTERNS` (word-boundary-aware — compound words like "imdblogo" are not rejected) and `_SITE_CHROME_PATH/DOMAIN_PATTERNS` (`www.blogger.com` is chrome-only — Blogger content images live on `bp.blogspot.com`/`googleusercontent.com`); SVG candidates are always skipped. `_SITE_CHROME_CONTEXT_RE` skips images whose preceding markup carries nav/dropdown/widget class names (menu icons and sidebar/footer widgets are never lead images). Before scoring, `_strip_related_post_blocks` removes whole balanced `div`/`section`/`aside`/`nav`/`ul` containers whose class names a related/recent/more-posts list (e.g. Hugo blogs' `related-content` widget, or a WordPress block-theme `wp-block-query` Query Loop) — the per-image context check only looks ~500 chars back, so a sibling post's thumbnail deep in such a list would otherwise win on pages that lack their own `og:image`/hero. Stripping `wp-block-query` is safe because a block theme renders the post's *own* featured image via `wp-block-post-featured-image` directly under `<article>`, never inside a Query Loop (which only lists other posts) — this also stops a pinned/featured sibling post from being picked on webcomic-strategy WordPress feeds (e.g. karlkerschl.com). The alt-text logo check is suppressed for images with explicit `width`/`height` attrs ≥ minimum dimensions, since publishers who size article images explicitly signal intentional placement. Additional URL/attribute rejections in `_is_image_url_acceptable`: `_SITE_CHROME_PATH_PATTERNS` includes a `/navigation/` asset-directory segment (header/menu icons, e.g. Paizo's `Personal-Account.png`); `_AD_URL_PATTERNS` + `_AD_ALT_PATTERNS` drop advertisement banners (filename `-ad1`/`/ads/`, or alt text "banner ad"/"advertisement"); the placeholder list covers `blank.{jpg,png,webp}` (WordPress.com's `s0.wp.com/i/blank.jpg` 200×200 white box shipped as og:image on image-less posts); and the logo safety-valve that lets a logo-named URL through on large embedded dimensions now also requires a content-like aspect ratio (0.25–4.0), so banner-shaped wordmarks like `logo-color-600x100` are still rejected. Two further refinements: (a) a `logo`-named image hosted **under the post's own URL directory** (passed as `source_url`) is treated as the post's own asset and skips the logo filter — site logos live at the site root or on a CDN, not under a specific post path, so a content hero like andreagrandi's `…/announcing-mcp-wire-0-3-0/mcp-wire-logo.png` is no longer dropped; (b) code-forge avatar URLs (`github.com/<user>.png`, `gitea.com/<user>.png`, gitlab/codeberg) — a single user segment + `.png` on the forge host — are rejected as profile pictures, so an election/announcement post that embeds candidate avatars doesn't pick one as its lead image (repo/asset paths have more segments and are unaffected). `_TRACKER_URL_PATTERNS` also rejects analytics pixels and social share-button sprites — `statcounter` (the `c.statcounter.com` `alt="Web Analytics"` 1×1 GIF that scales to a grey thumbnail on image-less posts) and `addtoany`/`addthis`/`sharethis` (e.g. `static.addtoany.com/buttons/share_save_171_16.png`, `alt="Share"`); because the tracker check runs even under `skip_logo_patterns=True`, the render cache-gate in `extract_entry_thumbnail_url` drops a *stale cached* statcounter/share URL on display without a DB rewrite. `_EMOJI_URL_PATTERNS` rejects emoji image sprites (`s.w.org/images/core/emoji/`, twemoji CDN) as lead images — they're decorative glyphs, not article content (but they survive inline; see below). When a lead image is rejected here, the alt/title that came with it is also suppressed at render via `_TRIVIAL_ALT_TEXTS` ("share", "web analytics", "analytics"), so an entry whose only "image" was a share button or tracking pixel shows neither a thumbnail nor a junk caption.

**Inline body-content rendering** (separate from lead-image selection): images that are rejected as lead images may still be legitimate *inline* content. Emoji sprites are kept in the body but constrained to ~1.2em via CSS (`.entry-content img.wp-smiley/.emoji/.ipsEmoji`) so they read as text-sized glyphs rather than the full-size 72×72 block the general `.entry-content img` rule would otherwise produce (e.g. IP.Board's `ipsEmoji` 🙃, which carries no inline size style of its own). All inline body images are also given `referrerpolicy="no-referrer"` (`add_no_referrer_to_images`, applied late in the entry-content pipeline; skipped for locally-served starred assets) so hotlink-protected hosts that serve a placeholder image on a foreign `Referer` return the real asset. `referrerpolicy` only fixes *fresh* loads, though — a browser that already cached a host's "image was hotlinked" placeholder under the unchanged image URL (these hosts send no `Vary`) keeps serving it. So for a small set of *known* hotlink hosts (`_HOTLINK_IMG_HOSTS`, e.g. nanolx.org), body-image `src` and the lead image are rewritten to the same-origin `/api/img?u=…` proxy (`proxy_hotlink_images`, `_lead_image_display_url`): the new URL isn't in the browser's cache, and the server-side proxy fetch carries no `Referer`, so the real image loads and stays correct. `srcset` is dropped on those imgs so the proxied `src` is the one used. Add a registrable domain to `_HOTLINK_IMG_HOSTS` to cover a new host (matches it and any subdomain).

**Same-origin Referer escalation** (the inverse hotlink case): some hosts do the opposite of the nanolx pattern — they *refuse* an image fetched with no `Referer` (HTTP 403, often a `text/html` body) but serve it 200 once a same-origin `Referer` is present (e.g. `fabiensanglard.net`'s `.webp` files, which `/api/img` would otherwise reject at the `content-type` gate → broken image). So both server-side image proxies (`api_img_proxy` for `/api/img`, `thumbnail_proxy` for `/thumb`) are **honest-first**: the first fetch carries only the honest `User-Agent`, and *only* if it comes back `403`/`503` do they retry once with `Referer: <scheme>://<host>/` (`_same_origin_referer`, the image's own origin root). This mirrors the honest-first WAF→browser-UA escalation in `services/lead_images.py` (`_BROWSER_USER_AGENT`): never preemptive, so hosts happy to serve us still see no `Referer`. The cache key ignores the `Referer` (the bytes are identical), so a hit skips the round trip entirely. The escalation only helps images that actually reach the proxy, though — the browser can't send a foreign site's own origin as `Referer`, so such hosts must be in `_HOTLINK_IMG_HOSTS` to have their `<img src>` rewritten to `/api/img?u=…` in the first place (`fabiensanglard.net` is listed for exactly this — its `.webp` files would otherwise load directly and 403, breaking them in reader/web view while its `.jpg` loads fine). `build_readability_response` (reader/web view) runs the same `proxy_hotlink_images` + `add_no_referrer_to_images` pass as the entry pane, after `_absolutize_article_urls` so host-matching sees absolute `src`.
4. **Inline feed content** — images embedded in `<content>` or `<summary>` elements. The render-triggered chunk backfill (`_do_backfill_entry_list`) does source-page fetches for `og_scrape`/`webcomic`/`unknown` feeds; when that fetch yields nothing it falls back via `_inline_from_reader` to the entry's own inline image rather than caching a blank. This rescues feeds whose pages are JS-only SPAs with no `og:image` (e.g. ArtStation) but which embed the artwork directly in the feed.

At render time, a feed pinned to `inline`/`media_rss` thumb strategy that extracts nothing also falls back to the cached lead image (`list_entries` in main.py) instead of showing a blank — important for feeds whose `thumb_strategy` was auto-detected as `media_rss` but whose reader `Entry` objects carry no `media:*` fields.

**ComicControl thumb→full promotion**: many ComicControl-CMS webcomics (e.g. atomic-robo.com, everblue-comic.com) ship only a small `/comicsthumbs/<file>` image in the RSS enclosure while the full-resolution panel is the same filename under `/comics/<file>` (page `id="cc-comic"`). These feeds may be pinned to `webcomic` strategy (whose source-scrape already stores the `/comics/` URL in the cache) but `_derive_article_lead_image` derives the *article* lead from the inline image, not the cache — so the article showed the small enclosure thumb. `LeadImageService._promote_known_thumbnail` rewrites the `/comicsthumbs/` path segment to `/comics/` (exact-segment lookbehind/lookahead, idempotent) on every thumbnail return, the cached-only read (`get_cached_entry_thumbnail`), and the inline-image path (`extract_inline_thumb_url`); `_apply_feed_content_cleanups` applies the same rewrite to inline body images. So the list thumbnail, the article lead, and the in-body image all show the readable full panel without an extra fetch. **Timestamp-mismatch caveat**: ComicControl filenames carry a cache-bust unix-timestamp prefix (`1782426356-ARV1701_05.jpg`), and the thumb and the full panel are often generated a second apart, so their prefixes differ (`comicsthumbs/…356-…` vs `comics/…355-…`). A naive directory swap keeps the thumb's timestamp, and ComicControl answers that nonexistent timestamp with a **200 HTML page** (not the image), so `/api/img` rejects it (422) and the comic breaks. `_promote_comicsthumbs_in_content` therefore substitutes the resolved full lead image URL (the real `/comics/<ts>-<file>` read from the page, looked up via `get_cached_lead_image_url`) whenever its timestamp-stripped filename (`_comiccontrol_stable_name`) matches the body thumb's; it only falls back to the directory swap when no lead image is cached yet.

Relatedly, `_is_image_url_acceptable` rejects show-title branding graphics (`podcast-title*`, added to `_SITE_CHROME_PATH_PATTERNS`, which is checked even on cached `skip_logo_patterns` reads): og:scrape falls back to one of these on a post with no real featured image — e.g. a WordPress `?preview=true` entry that leaked into the feed — so the article shows no image rather than the site's podcast logo.

The in-memory cache is warmed at startup **per enabled user** (`_for_each_background_user("lead-image cache warm", ...)`): lead images live in each tenant's own `entry_lead_images` table, and the render path consults only the shared in-memory cache (no per-user DB read), so warming bare against the default tenant would leave every other user's thumbnails blank until the rate-limited background backfill caught up after each restart.

For webcomics, the main comic panel is the lead image and takes priority over both the publisher's `og:image` and any RSS enclosure thumbnail. `_fetch_source_lead_image` calls `_extract_webcomic_panel_image` first when `is_webcomic` is set: it strips related/recent/Query-Loop post listings (`_strip_related_post_blocks`) and then returns the `<img>` matched by `_WEBCOMIC_IMG_ID_RE`/`_CLASS_RE` (e.g. ComicControl's `id="cc-comic"`) before the `og:image` early-return — many webcomic CMSes set a single generic site banner as `og:image` on every page with a sane aspect ratio, which would otherwise win. The related-block strip matters because `_CLASS_RE` matches WordPress's `wp-post-image`, so on a block-theme WordPress feed a sibling post's featured thumbnail in a `wp-block-query` loop would otherwise be returned as the panel; when no own panel survives the strip, resolution falls through to the regular scored body scan. For the same reason, backfill (`fetch_and_store_lead_images_for_feed`) treats `webcomic` like `og_scrape`-manual: it falls through the inline/enclosure image (typically a small `/comicsthumbs/` variant with no hover text) to the source-page fetch so the full-resolution panel and its alt/title win, and skips the feed-XML media-thumbnail lookup entirely (the enclosure is the same small thumbnail). `_extract_webcomic_alt_text` then surfaces the hover-text punchline: it checks the WordPress `comic-alt-text` balloon, then the `title`/`alt` attribute of the main comic `<img>` (matched by `_WEBCOMIC_IMG_ID_RE`/`_CLASS_RE`, e.g. SMBC's `id="cc-comic"`), and only then falls back to `og:description` (which on many comic sites is just the post title). At render time, captions that merely restate the entry title are dropped — including auto-generated banner captions that pad the title with a decorative word and/or date (e.g. "Progress Update Banner 2026-06-06" for a post titled "Progress Update 6/06/2026").

**The panel is usually marked on its CONTAINER, not on the `<img>`** — a lesson that cost three feeds. Matching only the img's own id/class meant `<div id="comic">` was invisible, so pbfcomics.com (image carries just `class="lazyload"`) resolved to the 79×30 "Home" nav button on every entry, and mahonoir.com to a 1200×630 OG social card. `_WEBCOMIC_CONTAINER_OPEN_RE` now selects a wrapper first, and **inside such a container the first acceptable image wins with no id/class test on the img at all**: the container is the evidence. Its tokens are an explicit list rather than a fuzzy `comic` substring, so `comic-nav` (previous/next buttons) and PBF's `comic_categories-comic` post class do not match.

The counterpart is `_WEBCOMIC_CHROME_OPEN_RE`, which drops nav/menu/widget/sidebar/gallery containers before either scan. This exists because **`wp-post-image` is a weak signal**: it is WordPress's featured-image class, it was added to `_WEBCOMIC_IMG_CLASS_RE` *for claycomix*, and it also marks PBF's nav items and claycomix's own `pf-summary-widget` sidebar — which was serving *another post's* comic. The class alone is not evidence; where it sits is. The same strip runs in `_extract_webcomic_alt_text`, because a caption has to come from wherever the panel came from — without it, every PBF strip was captioned "Home". Both scans share the balanced-element walker (`_strip_balanced_containers` / `_iter_balanced_containers`), which takes its tag name from the matched text rather than a capture group: these patterns are alternations, and a wrong group index fails silently by stripping nothing.

One site needed more than a pattern. mahonoir.com publishes each page **twice inside one outer `<div id="comic">`** — `#spliced-comic` cut into single panels for phones, first in the document, and `#unspliced-comic` whole. Excluding the spliced container from the match list does not help, because the outer container matches and its first image is the spliced one; the spliced container is removed by the chrome strip instead, so what remains inside `#comic` is the whole page. Finally, an `og:description` equal to `og:site_name` is refused as hover text: PBF ships `og:description="The Perry Bible Fellowship"` on every strip, and using it would caption the whole feed with the site name.

Results are stored in `entry_lead_images (feed_url, entry_id, image_url, image_alt, image_title, fetched_at)`. `image_alt` and `image_title` hold the raw `alt` and `title` HTML attributes from the matching `<img>` tag on the source page, stored separately so the user can choose which to display via the `caption_source` feed preference (`feed_display_prefs.caption_source`: `auto` / `alt` / `title` / `both` / `none`). NULL image_url means "no image found." Negative results are retried after **4 hours** (`_NEGATIVE_RETRY_SECONDS`); positive results are revalidated after 12 hours (`_POSITIVE_REVALIDATE_SECONDS`). An existing non-NULL URL is never overwritten with NULL during revalidation. Likewise on first resolution: an `og_scrape`-**manual** feed stores the inline feed image and then falls through to the authoritative source-page fetch, but a transient source miss must not clobber that inline image with NULL — otherwise a brand-new post (whose `og:image` isn't generated yet at first fetch) loses its thumbnail until the 4-hour negative retry. The NULL negative is only recorded when there was no inline image either.

First-open availability: when `queue_source_fetch` (the lead-**image** fetch) is called for a new entry, it posts a `threading.Event` keyed by `(feed_url, entry_id)`. The entry render path calls `wait_for_source_fetch(..., timeout=0.8)` immediately after queuing so the lead image — which the user sees right away — fills on the very first open for fast sites, capped low enough that slow hosts (Squarespace, WordPress.com) fall through and fill on the next open instead.

The **caption** source-HTML fetch (`queue_source_html_fetch` → `fetch_entry_image_caption`) is, by contrast, fully asynchronous: when the source HTML isn't already cached, the render queues the background fetch (which both primes the HTML cache and persists the alt/title to `entry_lead_images`) and returns immediately — it does **not** call `wait_for_source_html_fetch`. The caption appears on the next open from the persisted value. This was previously a `wait_for_source_html_fetch(..., timeout=3.0)` blocking call, which stalled first-open by up to 3s on og_scrape feeds (e.g. mynorthwest) purely to maybe show a caption that gets persisted for next time anyway; removing the wait is the cache-first/defer fix (the lead image still uses the brief 0.8s wait above since it's the user-visible payload). The narrower `inject_source_images` gallery path keeps a 0.8s `wait_for_source_html_fetch` since it's gated on an opt-in per-feed pref.

**The gallery ranks nothing, so it needs its own filters.** `extract_source_gallery_urls` collects *every* acceptable image in document order rather than picking a winner, which means the lead-image scorer's defences don't apply to it. Two consequences were live on tinyview, whose comic post injected 14 images — 5 real panels, 5 chrome, 4 broken:

- **Plugin verdicts now apply here too.** `LeadImagePlugin.source_score_adjustment` only ever fed the scorer, so `TinyviewPlugin`'s −200 for `assets.tinyview.com` (skeleton animation, wordmark, icons8 buttons) demoted those URLs for the lead image while leaving them first-class gallery entries. Anything scored at or below `_PLUGIN_CHROME_SCORE` is now skipped — a plugin scoring that low is calling a URL chrome, not merely ranking it lower.
- **Duplicate filenames collapse** (`_drop_duplicate_basenames`). A server-rendered app often emits an image twice, at its real location and at a fallback path that 404s; tinyview ships each panel as both `/<comic>/<yyyy>/<mm>/<dd>/<slug>/IMG_*.jpeg` (200) and `/<comic>/IMG_*.jpeg` (404). Which is real can't be known without fetching, but it can be inferred — prefer the URL whose path carries the entry's own slug, since that is the copy filed under this post. Falls back to first-seen order, so sites without the pattern are untouched.

**A plugin verdict is only as good as the paths that honor it.**
`should_skip_source_lookup` was consulted on 3 of the 12 `_fetch_source_lead_image`
call sites, and the storing paths in `fetch_and_store_lead_images_for_feed` were
not among them — so a plugin-owned host could resolve correctly on the render
path and then be overwritten by a background revalidation that scraped the page
anyway. Webtoons episodes went back to the series thumbnail hours after the
plugin was fixed, which is how this surfaced. Storing paths now go through
`_plugin_or_source_lead_image`: the plugin's own answer wins, a plugin that
forbids scraping gets no scrape, and a forbidding plugin with no answer yields
NULL rather than a scraped one. The old code stored NULL outright for any
skip-source host, which blanked panels the plugin could have named.

**Name heuristics must not run against machine-generated filenames.**
`_AD_URL_PATTERNS`'s `[-_]ad[0-9]` exists for `Cert-ad1.png` and cannot tell it
from `-ad27-` inside `ff52deff-c6a8-448d-ad27-a3c3d14c719c.jpg` — two Tapas
panels were rejected as ad slots that way. The wixmp host-trust a few lines
above was added for exactly this class ("ad87 in a UUID") one host at a time;
`_UUID_BASENAME_RE` generalizes it. Only the *filename* half is exempted: the
pattern does two jobs, and `/ads/` still names a directory, so a UUID sitting in
an ads directory is still an ad. Path-, host- and dimension-based checks are
untouched.

**A plugin that suppresses everything is a claim about the feed, not just the page.** `WebtoonsPlugin` skipped source scraping *and* returned no fallback, on the stated belief that "the feed and og:image return the series thumbnail for every episode". Only half was true. An episode page's og:image really is the series thumbnail — byte-identical across episodes — but every Webtoons `<description>` carries that episode's own panel on the same CDN, distinct per episode on all nine subscribed feeds. The result was the series thumbnail on every strip, served from cache rows written before the plugin existed, while the real panel sat unused in the entry body. The plugin now reads the body like `BloggerPlugin` does, keeps `should_skip_source_lookup`, and narrows `should_bypass_cached_url` to URLs whose basename is `thumbnail.*` — so stale thumbnails are re-resolved and a good cached panel is left alone instead of being re-derived on every render.

**Webtoons is the same trade, and the hotlink gate is dodged rather than
forged.** An episode is a vertical strip cut into slices — 50 on a Backchannel
chapter, 8 on MercWorks, 5 on False Knees — and the feed carries exactly one
image. The slices are on the page as `<img class="_images" data-url=…>`; the
class is what bounds them, because the page also embeds a recommendation strip
of other series and a looser scan swept 62 URLs into an episode that has 50.

The page serves them from `webtoon-phinf`, which answers **403 to any request
without a `webtoons.com` Referer** — including a browser loading the image off a
Lectio page. The sibling `swebtoon-phinf` host serves the same paths with no
Referer at all, and is the host the RSS feed itself uses, so
`_webtoons_public_slice_url` rewrites to it and drops the `?type=` resize.
Verified 200 on every slice of three series. That is the difference between
routing around a gate and forging a header we do not have; it also means no
image-proxy change.

**The feed's image and the article's image are different questions, and on
Tapas they have different answers.** `/sa/` is *series art* — one picture per
episode, thumbnail-grade, and all the RSS feed carries. `/c/` is the episode's
actual content, one URL per panel, so a four-panel episode arrives in the feed
as a single image. `_inject_tapas_episode_panels` fetches the episode page and
puts the `/c/` panels in the body, strips the `/sa/` picture out of it, and
drops the separate hero — otherwise the thumbnail renders above the comic it is
a thumbnail of. The **list** thumbnail is untouched, because the caller captured
the resolved lead in `_resolved_lead_for_cache` before the body rewrite; that
split is what lets one entry have a thumbnail-grade image in the list and the
real comic in the article.

The `/c/` URLs are signed and short-lived (`?__token__=exp=…~acl=…`), which
drives two decisions. The page is **re-read** rather than the URLs stored, since
a stored URL is dead within the hour; and `__token__` joins
`_IMG_CACHE_VOLATILE_PARAMS`, so `/api/img` caches the bytes under a
token-stripped key and keeps answering after every token expires. The page fetch
is synchronous on the render path — narrow enough to afford (Tapas links only,
and only when the body has no `/c/` image already) and *not* deferrable the way
a caption is, because deferring would show the wrong picture now.

**Tapas is the same shape on a different host, and needed its own plugin.** An
episode page's og:image is a social *card* (`.png` on `us-a.tapas.io`) while the
panel is a `.jpg` on the same CDN inside `<content:encoded>`. The card is
distinct per episode, so unlike Webtoons nothing in the URL says which is which
— `TapasPlugin` therefore takes the body image and treats a non-`.jpg` cached
value as the card worth re-resolving.

The strategy comparison cache (`feed_strategy_cache`) also stores `image_alt` and `image_title` per strategy so the Tuning tab can display them below each card without a live fetch.

SmartCrop's `min_scale` is a per-feed preference (`feed_display_prefs.smart_min_scale`, NULL = default 0.9), set in Feed Properties next to the thumb fit mode and passed to the `/thumb` proxy as the `ms` query param; it was previously a global app setting. The min_scale is part of the thumb cache key, so changing it regenerates that feed's Smart thumbnails.

Fill mode's `fill_zoom` multiplier (`feed_display_prefs.fill_zoom`, NULL = default 1.0, range 0.5–2.0) scales the cover-crop resize step before the anchor-crop. Values below 1.0 produce a letterbox (image pasted on a black canvas); values above 1.0 crop more aggressively than the default tight fill. Passed to `/thumb` as the `fz` query param and included in the cache key for cover-family modes.

**Direct-load fallback:** `/thumb` fetches the source image *from the server*, so a host that IP-blocks datacenter traffic (e.g. Cloudflare 403, washingtonstatestandard.com) makes `/thumb` 502 and the list thumbnail break — even though the browser's own (residential) IP can fetch the image fine. The list `<img>` carries the raw image URL in `data-direct`; on a `/thumb` error its `onerror` (`window.thumbImgFallback`, defined pre-body so it exists before any load fails) retries once with that direct URL, letting the browser load the image itself. CSS `object-fit:cover` sizes the un-resized image to the tile. This recovers the thumbnail without evading the block server-side (it's the user's own client fetching, exactly as the article view already does). Only `http(s)` direct URLs are retried, and only once (a `data-triedDirect` guard prevents an error loop); if the direct load also fails, the tile collapses to `is-empty` as before. The same helper backs the JS-derived list thumbnail (it sets `data-direct` to the lead-image URL).

## An embed URL wearing an anchor is still an embed

Feeds that lose their oEmbed iframe usually ship the video as a `watch?v=`,
`youtu.be/` or `/shorts/` link, and `_embed_standalone_youtube_links` rebuilds a
player from any of those when the link is a paragraph's sole content.
sonarsource.com ships a fourth shape: the **`/embed/` URL itself**, as a plain
`<a href>` with descriptive text —

```html
<p><a href="https://www.youtube.com/embed/<id>?si=…">Escape from AppleScript</a></p>
```

That is an embed source that happens to be wearing an anchor. It read as "the
video is on the web post but not in the article", which is precisely what this
function exists to undo, and it missed in **two** places at once: the URL matcher
(`_YT_WATCH_URL_RE`) listed only the three link shapes, and `extract_video_id`
could not name a video from an `/embed/` URL either — so even code that reached
past the matcher had nothing to build a player from. Both now accept `/embed/`,
and `youtube-nocookie.com` alongside it, since it is the same URL shape from the
privacy host.

There was a vestigial `if "/embed/" in content_html: pass` at the top of the
function, anticipating this case and doing nothing about it. Removed rather than
left to imply a check that was not happening.

The paragraph-sole rule is unchanged and still the whole scope guard: a link
inside a sentence stays a link. The anchor's *text* was never required to be a
bare URL — the test is whether the anchor is its container's only content — so a
worded link like this one already qualified once the URL shape was recognized.

## A body image that fails has to be able to try again

The article's hero image has always carried an `onerror` that swaps its `src`
for `/api/img?u=…` and only gives up if the proxy fails too. Body images carried
nothing: a failed load left blank space, no second attempt, and no way to tell a
blocked image from one the publisher never shipped.

That asymmetry stays invisible for as long as a post has both a hero and body
images, because the hero is the one people look at. It surfaces when a post's
`og:image` **is** its body image. `_strip_lead_image_opener` then correctly drops
the separate hero — showing the same picture twice above and inside the article
is worse than showing it once — and the only copy left is the body copy, the one
with no fallback. That is why sonarsource.com's blog read as "the posts before
and after this one show their image and this one doesn't": the neighbours were
rendering a hero, this post was rendering a body image.

`add_img_proxy_fallback` closes it, running alongside `add_no_referrer_to_images`
on the entry-pane path. Three properties matter:

- **The direct URL stays the first attempt.** This is a retry, not a rewrite.
  Preemptively routing every body image through the proxy is a different (and
  much larger) change — `proxy_hotlink_images` already does that deliberately,
  for the named hosts in `_HOTLINK_IMG_HOSTS`, where a direct load is *known* to
  fail.
- **It costs nothing on the happy path.** `onerror` never fires for an image
  that loads.
- **It only adds a handler where there is none.** The sanitizer strips
  author-supplied event handlers, so in practice that is every image, but the
  guard means running the pass twice is a no-op rather than a nest of handlers.

Because the retry is same-origin and the server-side fetch carries no `Referer`,
it recovers the same three failures the hero's copy always did: a host that
refuses cross-origin image loads, a URL a client-side blocker drops, and a signed
URL that expired between storage and reading.

## Choosing a lead image: what gets thrown away, and what sneaks through

The selector is a pile of heuristics, and its failures come in two opposite
shapes. Both were live on 2026-08-12 and each one silently produced a *plausible
wrong answer*, which is why they went unnoticed.

**Site chrome that was being kept.** blogs.windows.com made its site icon the
article image. Both available signals missed it: the alt text said `Site Icon`
but `_LOGO_URL_PATTERNS` only allowed `-`/`_` between the words — and that
pattern is matched against alt/title *text* as well as URLs, where words are
separated by spaces. The file was `Windows11Icon.png`, CamelCase, so the
`[-_]icon.png` rule needed a separator that never existed. Separators are now
`[-_\s]`, and the icon-filename rule is separator-optional behind a lookbehind so
`emoticon.png` and `lexicon.png` stay safe.

**Real art that was being thrown away.** Two of these:

- *A title that contains a hint word.* `round` is a shape hint for a cropped
  avatar (`avatar-round.png`) and was matched against the whole path, so Standard
  Ebooks' **"The Third Round"** lost its cover — a perfectly good 1400×2100 JPEG
  — because the word appeared in a *directory segment that is a book title*. The
  hint now applies to the filename only. Same class of false positive the
  `profile` guard in that pattern already documents for DeviantArt.
- *A file honestly named "fallback".* Full Circle Magazine's genuine podcast
  cover art is `covers/podcasts/fallback.webp` — the art used when an episode has
  none of its own — and the placeholder rule reads the name. Declared dimensions
  now override it, since a page sizing an image at 640×360 is asserting intent.
  ⚠ The bar is **hero scale (400×200), not the ordinary minimums (200×100)**:
  WordPress's `blank.jpg` is a 200×200 white box sitting exactly on the floor, so
  an at-or-above rule readmits the canonical placeholder. An existing regression
  test caught that immediately.

**A negative is cached, so a wrong rejection is sticky.** Each of these stored
`image_url = NULL` and stopped re-resolving, so fixing the rule is only half the
job — the poisoned rows have to be cleared for the entries to recover.

## Several images in one container are a row — unless they are a comic page

`.entry-content p:has(> img + img)` lays a container's images out as a wrapping
flex row. It was written for paizo.com's three 250px cover variants, which are
inline by default and sat side by side unstyled; the note said `max-width:100%`
would keep a full-width image one-per-line.

It does not, for two separate reasons, and `width: auto` is the bigger one. It
**discards the `width` attribute**, so the used width becomes the intrinsic width
of whichever `srcset` candidate the browser picked — and `sizes="auto"` inside a
flex container resolves small, so it picked the 300w file. mahonoir.com stacks a
comic page as four 800px panels in one `<p>`; they rendered 300x165, two to a
line, genuinely low-res as well as small.

The second reason is that **a flex item shrinks below its intrinsic width by
default**, which is what the original note missed.

Measured in Chromium rather than reasoned about, because the first attempt at
this fix (adding `flex: 0 0 auto` alone) did not work and the report came back
unchanged:

| | comic panels | paizo covers |
|---|---|---|
| `width: auto` | 300x165, 2/line | 16x21 |
| without it | 700x384, 1/line | 250x250, sharing a line |

The covers collapsing to 16px is the same bug from the other end: with no width
attribute honoured and no loaded image, nothing is left to size them.

So `width: auto` is gone and `flex: 0 0 auto` stays. It survives only for
`.npf_row`, which is what it was there for: Tumblr's row layout sets
`width: 100%` on its figure images, which would force one per line, and a row is
the entire point of an npf_row.

## dresdencodak draws its own list crop, for half its comics

Same split as Penny Arcade — whole comic in the article, a legible crop in the
list — with different naming and, unlike Penny Arcade, a convention the site only
half keeps:

```
article : …/uploads/YYYY/MM/dc_minis_<n>.jpg          (#27 is 1500x4875)
list    : …/uploads/YYYY/MM/dc_minis_<n>_thumbnail.jpg (2500x1000)
```

Every DC Minis strip checked has its `_thumbnail.jpg` (25, 26, 27); no Dark
Science page has one (`ds_185`, `ds_186_silder`, `ds_187_silder` all 404). Since
`thumbnail_from_lead_image` is network-free by contract — it runs on the
posts-list render path — the plugin cannot check, so it derives only for
`dc_minis_` and declines where it does not know.

The reverse derivation is deliberately absent. Stripping `_thumbnail` to reach
the article image works for #26 and #23 and 404s for #25 and #22: those posts
publish only the `_thumbnail`-named file, which is therefore the comic rather
than a crop of it. Instead `source_score_adjustment` demotes a `_thumbnail` URL
while scoring the source page, so the full comic wins the lead on its own merits
when the page offers both, and the crop still wins when it is all there is.

## A comic's prev/next arrows are not its lead image

`main.py` has known these as body chrome for a long time (`_COMIC_NAV_ALT_RE`,
`_COMIC_NAV_SRC_RE`) and strips them from the article. Nothing stopped one being
chosen as the **lead**: dresdencodak's feed opens with

```html
<img alt="Previous" height="30" src=".../prev_002.png">
```

so the 30px arrow won the first-image bonus and became both the hero and the
thumbnail.

The match is anchored to a basename that is *only* the nav word plus an optional
number, because these are ordinary English words: `first-contact.jpg` and
`next-door.png` are comics, `prev_002.png` and `next.gif` are buttons. `main`'s
looser `src` pattern can afford the ambiguity because it only fires alongside
other nav signals; this one stands alone, so it cannot.

## An `<img>` inside a `<script>` is source code, not an image

monstersoupcomic's bookmark widget does

```js
document.write('<a …><img src="'+imgTag+'" …>')
```

and the page scan dutifully produced the lead image
`https://monstersoupcomic.com/'+imgTag+'` — a URL that cannot resolve to
anything. `<script>` blocks are now stripped once in `_fetch_page_html`, rather
than at each of the ten `_IMG_TAG_RE` scan sites, so no future scan can forget.
Safe there because this class reads no JSON-LD (which also lives in `<script>`),
and `og:`/`meta` tags are in `<head>` and untouched.

## An age gate is the one image that is definitely not the post

An adult webcomic serves a content-warning interstitial *instead of* the strip,
so a page scrape picks it up exactly where the comic should be — and on a
webcomic feed whose body ships no image, `_inject_webcomic_panel_into_bodyless_entry`
then puts it in the article. monstersoupcomic.com illustrated both halves at once:
a post about paintbrushes rendered `maturecontentwarning.png`.

Unlike most site chrome this is not a logo or a widget, so none of the existing
rules saw it. It is now matched in `_SITE_CHROME_PATH_PATTERNS`, separator-optional
because these files are named every way going (`maturecontentwarning`,
`mature-content-warning`, `age_gate`, `nsfw-warning`).

The words themselves appear in real comic titles, so the patterns match the
*gate* shapes only: `the-warning-sign-chapter-4.jpg` and
`mature-audiences-episode.jpg` still pass, and a test pins that.

Removing the gate exposed what it had been masking: the same feed's text posts
then resolved to `/images/blog_on.png`, a 99x44 nav button. `<name>_on.png` /
`<name>_off.png` is the rollover convention for a button's two states, and a
site that still writes its menu that way carries no other markup saying so. The
size floor cannot catch it either — the dimensions are neither in the URL nor
declared on the tag, so nothing measures them without fetching the bytes. That
shape is rejected too, anchored to the whole basename so `lights-on.jpg` and
`switched_on_and_off_again.png` are untouched.

## A caption that never changes is the site's, not the post's

Webcomic caption extraction falls back to `og:description`, because that is
where a lot of comics put the hover-text punchline. Plenty of sites put a fixed
blurb there instead. `_extract_webcomic_alt_text` already rejects one that merely
repeats `og:site_name` (pbfcomics ships `og:description="The Perry Bible
Fellowship"` on every strip), but Penny Arcade defeats that twice over: it ships
no `og:site_name` at all, and its description is a real sentence —

> Videogaming-related online strip by Mike Krahulik and Jerry Holkins. Includes
> news and commentary.

Nothing *within* one page marks that as boilerplate. What marks it is that it
does not vary: a punchline belongs to its strip, a tagline belongs to the site.
So the test is across the feed rather than within the page — if another entry
already carries this exact caption, neither of them is a caption.

Self-healing rather than perfect. The first entry cannot know, so it stores the
text; the second recognises the repeat, and `_clear_feed_boilerplate_title` drops
it from every row of that feed at once (and from the in-memory title cache, which
would otherwise keep serving it until a restart). Worst case is a missing caption
on one post instead of a wrong one on every post.

Scoped to a single feed on purpose: two different sites may legitimately share a
sentence, and `image_alt` is never touched — only the title, which is the field
that fell back to `og:description` in the first place.

## A webcomic wants a different image in the list than in the article

Penny Arcade strips are ~1050×438 — three panels side by side, which is three
unreadable smudges at thumbnail size, while panel 1 is legible. The two are
derivable from each other (`…/comics/<hash>.jpg` ↔
`…/comics/panels/<hash>-p1.jpg`), so the `thumbnail_from_lead_image` plugin hook
returns a thumbnail crop from the already-resolved lead image with no extra
fetch, safe on the render path.

Getting that right needed **three** places to agree, and fixing the first two was
not enough:

1. `should_skip_source_lookup` — without it the page scan takes the first `<img>`
   (panel 1) and stores it as the article image, beating the plugin's own
   og:image fallback. The plugin's docstring described behaviour it never reached.
2. `get_cached_entry_thumbnail` — the panel-bypass returned `None` rather than
   falling back, so a cached panel produced *no thumbnail at all*.
3. `_inject_webcomic_panel_into_bodyless_entry` — re-scanned the source page **at
   render time** and injected panel 1, discarding the strip that had just been
   resolved. It now honours the same `should_skip_source_lookup` answer the
   storing path uses, so a plugin-owned host is never re-scanned; hosts with no
   plugin opinion still scan, which is the case that injection was written for
   (mahonoir's enclosure is a share card, so the page really is the only source).

## Image bytes: the dimension cap is not a size cap

`/api/img` downscales a cached image to `LECTIO_IMG_CACHE_MAX_DIM` (3840) on the
longest side. That says nothing about how many bytes it weighs. A 3840x2160 RGBA
**PNG** is exactly at the cap, so `_maybe_downscale_image` returns it untouched —
at 11.6 MB, shipped whole on every article view. Reported as "this image loads
slowly every time", which it did, because it does.

`_maybe_shrink_oversized_image` adds a byte budget (`LECTIO_IMG_TARGET_BYTES`,
default 1.5 MB, 0 disables). Over it, the image is re-encoded to WebP:

- **Lossless** for small, few-colour images — logos, pixel art, diagrams,
  screenshots — where lossy compression visibly damages hard edges and text.
- **Lossy q85** for everything else, on the reasoning that a large full-colour
  image is photographic or painted, which is the case lossy handles best.

**Both gates were measured, and the obvious implementations are wrong.** Trying
lossless first and falling back cost **12.3s** in the request path; lossless alone
on that image is 6s and still 7.9 MB. Its colour count is 42,082 — low enough
that a naive "few colours means line art" test sends painted artwork down exactly
that slow path. So pixels are checked first (cheap, and pixel count is what makes
the encoder slow) and the colour scan only runs where the answer is both fast and
true. Result on the reported image: **11.6 MB → 0.17 MB in 0.54s**, with line art
verified pixel-identical.

Both re-encoders now run via `run_in_threadpool`. `/api/img` is an async route, so
running a multi-hundred-millisecond bitmap decode inline blocks the event loop and
every other request on the worker queues behind one large image. Both settings are
read *before* entering the pool, so the threaded call is pure CPU with no DB access
and no tenancy context to carry.

The budget is an instance setting (**Administration → Image cache**), with the env
var as its default — the same shape as `LECTIO_IMG_CACHE_MAX_DIM` beside it, and
admin-only for the same reason: it decides how every user's images are stored.

## Transparency: `convert("RGB")` paints line art black

`Image.convert("RGB")` keeps whatever RGB sits *under* the alpha channel, and for
line art that is black — so a transparent PNG becomes a solid black rectangle.
Measured on `what-if.xkcd.com/imgs/a/138`: mean luminance **33** the naive way
against **235** composited onto white. Two paths had it, in two disguises:

- **`/thumb`** called `.convert("RGB")` outright, and now composites onto white
  first. White rather than a theme colour because the output is a JPEG cached and
  shared across users and themes, so the background is chosen once — and this
  kind of image (diagrams, logos, line art) is drawn for a light page. The
  zoom<1 letterbox canvas follows the same rule: white when the source had alpha,
  black for photos.
- **The starred archive** tested `"A" in img.mode`, which is `False` for a
  *palette* PNG (mode `"P"`, transparency in `img.info`) — so precisely the
  images this breaks were the ones it flattened. WebP carries alpha, so capture
  keeps it, and normalization moved ahead of the resize since LANCZOS on a
  palette image resamples palette indices rather than colours.

**Neither fix reaches bytes already stored.** A saved entry's images are rewritten
to `/starred-asset/<hash>` at render time, so the body shows the stored copy, not
the live image — only a re-fetch restores the alpha
(`scripts/repair_flattened_archive_images.py`; 143 what-if assets restored
2026-08-04). Candidates are found from the **WebP header** rather than by
decoding: an asset declaring no alpha whose source format *can* carry it is a
suspect, a 32-byte read instead of decoding 25k images (a decode scan did not
finish in ten minutes). A candidate is only rewritten when the re-fetched source
has a *meaningful* alpha channel — xkcd's book covers have a fully opaque alpha
channel and artwork that is simply dark, and an earlier pass "repaired" those into
byte-identical output and reported a fix that never happened.

Cached thumbnails were already black too, so the fix needed a cache bust:
`_THUMB_RENDER_VERSION` joins the cache key (the same idiom as the existing `_p2`
suffix), so old entries are never looked up again and each thumbnail re-renders on
first view — no mass delete of the 59k-row, 431 MB cache and no refetch storm.

At render time the theme paints behind transparent images via `--img-backdrop`,
white in *both* themes: transparent article images are overwhelmingly black line
art drawn for a light page, and an image that changed appearance when you toggled
the theme would be worse than one that never does. Setting the variable to
`transparent` restores the untouched look with no re-fetch — which is the point of
doing it in CSS rather than in the stored bytes. Deliberately not applied in Read
Mode, where `#reader-columns *` forces `background: transparent !important` for
e-ink contrast and transparent art already lands on white.

## Thumbnails must reuse the image proxy's bytes

`/thumb` fetched its source URL directly and never consulted `img_cache`. For an
image behind a short-lived signed URL that is fatal: the article renders fine
(`/api/img` holds the bytes under a token-stripped key — see
[DeviantArt mature images](#deviantart-mature-images-signed-for-minutes-cached-for-good)) while the thumbnail re-requests the dead URL, gets a 401, and is
recorded in the recently-failed set. The result is a post with a working image and
no thumbnail, permanently, plus a failing fetch on every list render. Found on a
deviation whose token expired two days earlier.

The proxy cache is now consulted first, and — importantly — **before** the
recently-failed short-circuit. Ordering it the other way preserves the bug: the
host *is* failing, which is precisely when the cached bytes are the only way to
get a thumbnail.

**A related way to lose a thumbnail: comparing two references to the same file.**
`GunnerkriggPlugin` derives the panel URL from the entry's `?p=` number and
bypasses any *cached* URL that differs, so a stale site banner cannot win. It
compared strings exactly, and lost twice over — the site serves the panel with a
`?v=<timestamp>` cache-buster, and the derived URL inherits the **entry link's**
scheme, which that feed still publishes as `http://` for an image served over
`https://`. So the plugin declared the very image it derives "not preferred" and
suppressed it. The article still rendered the picture, because that path does not
consult the bypass, which is exactly how it presented: a comic post with an image
and no thumbnail. Comparison is now on host+path (`_same_file_key`); the cached
URL is still *served* untouched, cache-buster and all, since rewriting it is the
ComicControl mistake `_promote_known_thumbnail` documents.

## DeviantArt mature images: signed for minutes, cached for good

DeviantArt serves images from wixmp with a signed JWT in the query string. Ordinary deviations are signed permanently; **mature** ones are signed for about **15 minutes**, and every variant (`content.src` and every thumb) shares the expiry — so there is no long-lived variant to prefer, and a stored URL is normally dead by the time the post is read, showing neither image nor thumbnail.

Nothing scheduled can fix that: a nightly re-sign yields images dead a quarter of an hour later. The re-sign therefore happens **on open** — `_resign_expired_deviantart_images`, run in `get_entry_detail` just before the hotlink-proxy rewrite.

What keeps it cheap is the proxy's byte cache, which was already most of the answer: `wixmp.com` is in `_HOTLINK_IMG_HOSTS`, so these images render through `/api/img`, and `_img_cache_key_url` strips `token`/`sig`/`exp` (`_IMG_CACHE_VOLATILE_PARAMS`) from the cache key. Once the bytes are cached under *any* valid token they answer for every later one. So the re-sign fires only when a token has already expired **and** the cache has no copy — one API call per image over its lifetime, not one per view — and a permanently-signed image (21,564 of 21,568 on the live library) never reaches the API at all. The fresh URL is persisted back onto the entry so the list thumbnail starts from it too.

`scripts/refresh_expired_deviantart_images.py` remains as a manual catch-up over the same routine. Note it must use `get_deviantart_user_token()` rather than reading `deviantart_access_token` directly: DA access tokens last an hour, so any batch reading the stored value 401s on almost every run.
