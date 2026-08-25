# Images

Choosing, rejecting, sizing and serving the image for a post.

> Split out of `ARCHITECTURE.md` on 2026-08-13. See
> [ARCHITECTURE.md](../../ARCHITECTURE.md) for the index.

## Floated images, and why margins are not kept

`_ALLOWED_STYLE_VALUES` keeps `float`/`clear`. Blogger emits
`clear: right; float: right` on every side-set image, and dropping it turned a
post written *around* a right-set cover into a centred block with the text pushed
below. Floats are admitted where `position`/`z-index`/`width` are not, and the
distinction is not arbitrary: a float stays inside its container, so it cannot
overlay the app's UI or escape the pane. It is prose layout.

The author's `margin-*` is **deliberately dropped** — margins are free-form
lengths, so keeping them means matching a value *pattern* rather than a literal,
the one thing this table promises never to do. The gutter comes from the
stylesheets instead, keyed off the sanitizer's **normalized** output. That is why
the space in `"float: right"` is load-bearing: the rules select on
`[style*="float: right"]`, and an emitter writing `float:right` would silently
stop matching with nothing failing.

**The narrow-screen override needs `!important`.** The float survives as an
*inline* style, which outranks any stylesheet rule, so without it the
`max-width: 620px` block is inert and a 45%-wide image stays floated on a phone.
Caught in a browser at 390px, not by a test; the test now asserts the
`!important` specifically, because a plain `float: none` passes a naive check
while doing nothing.

**Preserving the float was only half of it.** The first body image is what
`_strip_lead_image_opener` hoists into a hero, removing it from the flow — so the
image most likely to be wrapped was the one still losing its wrap. That function
already had the right rule for images further down (*an occurrence further down is
the author placing it in the flow*); a float is that placement stated explicitly,
and it happens to be at the top. A floated opener stays put and the separate lead
is dropped, or the picture appears twice. Only the *article* lead — the list
thumbnail resolves on its own path. "Don't show the lead image in the article"
still outranks the author's layout.

**⚠ `lead_image_url` after the dedup is a RENDERING decision, not a fact.** Every
branch setting it to `None` means "don't draw this twice". `get_entry_detail`
used to persist that `None` into `entry_lead_images`, which is where the **list
thumbnail** reads from — so an article opened after the floated-opener change
recorded itself as imageless and lost its thumb (130 live entries). The resolved
value is now captured *before* the dedup (`_resolved_lead_for_cache`). Anything
added to that function has the same trap.

## Lead image pipeline

`LeadImageService` resolves a hero per entry through four layers.

**1. Feed-level strategy** (`feed_lead_image_strategy`, cached weekly):
`og_scrape`, `inline`, `media_rss`, `youtube`, `artwork`, `webcomic`, `none`,
`unknown`. Two startup auto-taggers: artstation URLs → `artwork`, folder name
containing "comic" → `webcomic`; artwork wins when both apply. Manual overrides
(`manual=1`) are never overwritten.

**2. Plugin fallbacks** — site-specific handlers (e.g. a YouTube thumbnail from
the video id).

**3. Source-page scraping** — `og:image` / `twitter:image` (both attribute
orders), then preload hints, then CSS background, then scored `<img>` tags. A
`<link rel="preload" as="image">` is used **only when there is no acceptable
og:image**: it is a perf hint, and sites preload above-the-fold widgets
(usafacts preloads a stats chart that must not beat the curated hero).

Body scan: the first valid image gets a +10 position bonus, and an `<img>` inside
a `<picture>` with a WebP `<source>` is substituted for the WebP URL.

Rejections, roughly in order of how often they matter:

| rule | what it drops |
|---|---|
| `_LOGO_URL_PATTERNS` | logos, word-boundary aware so "imdblogo" survives |
| `_SITE_CHROME_PATH/DOMAIN_PATTERNS` | `/navigation/` icons, `www.blogger.com` (chrome-only host), `podcast-title*` branding |
| `_SITE_CHROME_CONTEXT_RE` | images whose preceding markup carries nav/dropdown/widget classes |
| `_strip_related_post_blocks` | whole related/recent/Query-Loop containers |
| `_AD_URL/ALT_PATTERNS` | `-ad1`, `/ads/`, alt "advertisement" |
| placeholder list | WordPress.com's 200x200 `blank.jpg` shipped as og:image |
| `_TRACKER_URL_PATTERNS` | statcounter 1x1 pixels, addtoany/addthis share sprites |
| `_EMOJI_URL_PATTERNS` | emoji sprites (kept inline, see below) |
| code-forge avatars | `github.com/<user>.png` — one segment + `.png` on a forge host |

Four refinements worth knowing:

- **Related-block stripping is what makes the context check sufficient.** That
  check only looks ~500 chars back, so a sibling post's thumbnail deep in a list
  would win on pages with no hero of their own. Stripping `wp-block-query` is
  safe because a block theme renders the post's *own* featured image directly
  under `<article>`, never inside a Query Loop.
- **The alt-text logo check is suppressed on explicit large dimensions** —
  a publisher who sizes an image is signalling intentional placement. The logo
  safety valve additionally requires a content-like aspect ratio (0.25–4.0), so
  banner wordmarks like `logo-color-600x100` are still rejected.
- **A `logo`-named image under the post's own URL directory is the post's
  asset.** Site logos live at the root or a CDN, not under a post path.
- **Trackers are rejected even under `skip_logo_patterns=True`**, so the render
  cache-gate drops a *stale cached* statcounter URL without a DB rewrite. When a
  lead is rejected, its alt is suppressed too (`_TRIVIAL_ALT_TEXTS`), so an entry
  whose only image was a share button shows neither thumbnail nor junk caption.

**4. Inline feed content.** The render-triggered chunk backfill does source
fetches for `og_scrape`/`webcomic`/`unknown`; when that yields nothing it falls
back via `_inline_from_reader` rather than caching a blank — which rescues feeds
whose pages are JS-only SPAs with no og:image but which embed the artwork in the
feed (ArtStation). At render time a feed pinned to `inline`/`media_rss` that
extracts nothing also falls back to the cached lead.

### Storage and retry

`entry_lead_images (feed_url, entry_id, image_url, image_alt, image_title,
fetched_at)`. `image_alt`/`image_title` are the raw attributes, stored separately
so `feed_display_prefs.caption_source` (`auto`/`alt`/`title`/`both`/`none`) can
choose. NULL means "no image found": negatives retry after 4h, positives
revalidate after 12h.

**A non-NULL URL is never overwritten with NULL.** On first resolution an
`og_scrape`-manual feed stores the inline image then falls through to the
authoritative source fetch, and a transient miss must not clobber it — a
brand-new post whose og:image is not generated yet would otherwise lose its
thumbnail for four hours.

**First-open availability is asymmetric on purpose.** The lead image gets a brief
`wait_for_source_fetch(timeout=0.8)` — it is the user-visible payload — capped low
enough that slow hosts fall through and fill on the next open. The **caption**
fetch is fully async: it used to block up to 3s purely to maybe show a caption
that gets persisted for next time anyway.

The in-memory cache is warmed at startup **per enabled user**: lead images live in
each tenant's own table while the render path consults only the shared cache, so
warming against the default tenant alone leaves every other user blank until
backfill catches up.

### Webcomics: the panel beats the publisher's own og:image

Many webcomic CMSes set one generic site banner as `og:image` on every page, so
`_fetch_source_lead_image` calls `_extract_webcomic_panel_image` *before* the
og:image early-return. Backfill treats `webcomic` like `og_scrape`-manual — it
falls through the inline enclosure (a small `/comicsthumbs/` variant with no hover
text) to the source fetch, and skips the feed-XML media lookup entirely.

**The panel is usually marked on its CONTAINER, not on the `<img>`** — a lesson
that cost three feeds. Matching only the img's id/class made `<div id="comic">`
invisible, so pbfcomics resolved to a 79x30 "Home" nav button and mahonoir to a
1200x630 social card. `_WEBCOMIC_CONTAINER_OPEN_RE` selects a wrapper first, and
**inside one the first acceptable image wins with no test on the img at all** —
the container is the evidence. Its tokens are an explicit list, not a fuzzy
`comic` substring, so `comic-nav` and PBF's `comic_categories-comic` post class do
not match.

Its counterpart `_WEBCOMIC_CHROME_OPEN_RE` drops nav/menu/widget/sidebar
containers before either scan, because **`wp-post-image` is a weak signal**: it is
WordPress's featured-image class, added for claycomix, and it also marks PBF's nav
items and claycomix's own sidebar widget — which was serving *another post's*
comic. The class is not evidence; where it sits is. The same strip runs in
`_extract_webcomic_alt_text`, or every PBF strip is captioned "Home". Both share
the balanced-element walker, which takes its tag name from the matched text rather
than a capture group — these patterns are alternations, and a wrong group index
fails silently by stripping nothing.

mahonoir needed more: it publishes each page **twice inside one outer
`<div id="comic">`** — `#spliced-comic` (single panels, first in the document) and
`#unspliced-comic`. Excluding the spliced container does not help, because the
outer one matches and its first image is the spliced panel; the chrome strip
removes the spliced container instead.

Hover text comes from `_extract_webcomic_alt_text`: the WordPress
`comic-alt-text` balloon, then the panel img's `title`/`alt`, then
`og:description`. Captions restating the entry title are dropped at render,
including banner captions padding the title with a decorative word or date.

**ComicControl thumb→full promotion.** These feeds ship only
`/comicsthumbs/<file>` in the enclosure while the full panel is the same filename
under `/comics/<file>`. `_promote_known_thumbnail` rewrites the path segment on
every thumbnail return, the cached read, and the inline path.

⚠ **Timestamp caveat:** the filenames carry a cache-bust unix prefix and the two
files are often generated a second apart, so a naive directory swap keeps the
*thumb's* timestamp — and ComicControl answers a nonexistent timestamp with a
**200 HTML page**, which `/api/img` rejects (422) and the comic breaks.
`_promote_comicsthumbs_in_content` substitutes the resolved full URL whenever its
timestamp-stripped filename matches, falling back to the directory swap only when
no lead is cached.

### Galleries rank nothing, so they need their own filters

`extract_source_gallery_urls` collects *every* acceptable image in document order
rather than picking a winner, so the scorer's defences do not apply. Two were
live on tinyview, whose comic post injected 14 images (5 panels, 5 chrome, 4
broken):

- **Plugin verdicts now apply here too.** `source_score_adjustment` only fed the
  scorer, so a −200 for chrome demoted it for the lead while leaving it a
  first-class gallery entry. Anything at or below `_PLUGIN_CHROME_SCORE` is
  skipped — a plugin scoring that low is calling a URL chrome, not ranking it.
- **Duplicate filenames collapse** (`_drop_duplicate_basenames`). Tinyview ships
  each panel at a dated path (200) and a bare path (404). Which is real cannot be
  known without fetching, but it can be inferred: prefer the URL carrying the
  entry's own slug. Falls back to first-seen order.

### Two traps worth keeping

**A plugin verdict is only as good as the paths that honor it.**
`should_skip_source_lookup` was consulted on 3 of 12 `_fetch_source_lead_image`
call sites, and the *storing* paths were not among them — so a plugin-owned host
resolved correctly on render and was overwritten by a background revalidation
hours later (Webtoons episodes reverting to the series thumbnail). Storing paths
go through `_plugin_or_source_lead_image`: the plugin's answer wins, a forbidding
plugin gets no scrape, and a forbidding plugin with no answer yields NULL rather
than a scraped one. The old code stored NULL outright, blanking panels the plugin
could have named.

**Name heuristics must not run against machine-generated filenames.**
`[-_]ad[0-9]` exists for `Cert-ad1.png` and cannot tell it from `-ad27-` inside a
UUID — two Tapas panels were rejected as ad slots. `_UUID_BASENAME_RE` generalizes
the one-host-at-a-time exemptions. Only the *filename* half is exempt: `/ads/`
still names a directory, so a UUID in an ads directory is still an ad.

## An embed URL wearing an anchor is still an embed

sonarsource ships its video as `<a href="https://www.youtube.com/embed/<id>">`.
`_embed_standalone_youtube_links` knew `watch?v=`, `youtu.be/` and `/shorts/`
only, and `extract_video_id` could not parse `/embed/` either — so nothing
recovered it. Both now accept `/embed/` and `youtube-nocookie.com`.

The paragraph-sole rule is the scope guard and is unchanged: a link inside a
sentence stays a link. The anchor's *text* was never required to be a bare URL.

## A body image that fails has to be able to try again

The hero has always had an `onerror` that retries via `/api/img?u=…`; body images
had none, so a failed load was blank space. Invisible until a post's `og:image`
*is* its body image — `_strip_lead_image_opener` then drops the hero as a
duplicate and the only copy left is the one without a fallback (sonarsource.com).

`add_img_proxy_fallback` adds the same handler. It is a **retry, not a rewrite**:
the direct URL stays the first attempt. Preemptive proxying is
`proxy_hotlink_images`, for hosts where a direct load is known to fail. Only tags
without an `onerror` are touched, so the pass is idempotent.

**Whole-body preemptive proxying is opt-in** (`proxy_body_images` setting,
Settings → Account → Appearance, off by default): every remote `<img src>` in
the article pane routes through `/api/img`, `srcset` is dropped so the browser
cannot pick a direct URL instead, and only the named-host hotlink rewrite above
is skipped as redundant — `add_img_proxy_fallback` still runs. **The fallback
is not just a retry-through-proxy; on an already-proxied image it is the only
thing that hides a genuine failure** (`display:none`), so an image dead at the
source (a wixmp token with no readable `exp` claim that turned out to already
be dead — see "Signed image URLs rot" in Plan.md) rendered as a bare
broken-image icon the first time this shipped, until the fallback was restored
for the proxied case too (2026-08-20). Read Mode has always done this
unconditionally — same job, offline needs the manifest to see same-origin
URLs — via the now-shared `proxy_all_body_images` (renamed from
`proxy_reader_images`, since a second caller no longer makes the old name
accurate). Off by default because proxying every body image (not just heroes
and known-hotlink hosts) raises `/api/img` traffic and cache size sharply, and
losing `srcset` costs high-DPI screens their 2x asset.

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

**A third shape, found the same day but not fixed until 2026-08-24:
site chrome kept because the check that would have caught it never ran.**
joanwestenberg's own byline avatar became the lead image
(`/p/nobody-wants-your-newsletter-you`) — two independent bugs, found together:

1. **`_extract_first_image_url_from_html` (the feed-body scanner) never
   checked alt/class/id text for avatar hints.** `_is_source_image_tag_acceptable`
   already did — `combined_hint_text` includes `alt`/`title`/`aria-label`/
   `data-testid`/`class`/`id`, checked against `_AVATAR_HINT_PATTERNS` — but the
   feed-body scanner only called it `if source_url`, and every one of its six
   call sites passes no `source_url` (that parameter is for a separately
   *fetched source page*, which a feed-body scan by definition doesn't have).
   So the check was live code with no reachable caller — dead by construction,
   not by regression. The avatar's alt was literally `"JA Westenberg's
   avatar"`; the URL (a wixmp/Cloudinary-style fetch URL) had no such wording
   anywhere in it, so the URL-only checks (`_looks_like_avatar_url` et al.)
   had nothing to catch. Fixed by extracting the hint-text check into
   `_has_avatar_hint(attrs, resolved_url="")` and calling it unconditionally,
   early, in the feed-body scanner too — deliberately *not* also enabling
   `_is_source_image_tag_acceptable`'s other checks (dimension floors, the
   square-at-small-scale heuristic, banner-shape) for the feed-body path,
   since those are a bigger, untested behavior change for other feeds whose
   only available image is genuinely small; the avatar-hint text is the one
   piece of evidence that's unambiguous no matter which path found the tag.

2. **The avatar's `srcset` mangled the URL before either check ran.**
   `_parse_srcset_urls_descending` did `srcset.split(",")` — correct for an
   ordinary srcset, wrong for a CDN URL that embeds its own comma-separated
   transform parameters ahead of the real path (Substack/Cloudinary "image
   fetch" URLs: `.../fetch/f_auto,q_auto,fl_progressive:steep/https%3A%2F%2F
   ...png 2x`). The naive split cut the URL at an internal comma, leaving a
   fragment (`fl_progressive:steep/https%3A%2F%2Fsubstack-post-media...`)
   that doesn't start with a scheme, so `urljoin` resolved it as *relative* —
   producing a URL under the post's own path that 404s. That was the reported
   symptom (a thumbnail that flickered and failed): the list rendered the
   mangled URL, the browser failed it, and the fallback swapped in. Fixed by
   scanning each srcset candidate as one whitespace-delimited token (a URL
   never contains whitespace, however many commas it has) with only the
   *descriptor* comma-terminated — the HTML standard's actual "parse a srcset
   attribute" algorithm, not a plain split. Host-agnostic: covers any CDN with
   this URL shape, not just Substack's.

Either bug alone would have produced a wrong-but-plausible image; together
they compounded (a mangled URL AND an unfiltered avatar). Fixing #1 without
#2 would still store a 404ing URL, just now correctly rejected before it got
that far — so both were needed to actually resolve the case.

## Several images in one container are a row — unless they are a comic page

`.entry-content p:has(> img + img)` lays a container's images out as a wrapping
flex row (written for paizo's three 250px covers). Two properties broke
full-width images, measured in Chromium:

| | comic panels | paizo covers |
|---|---|---|
| with `width: auto` | 300x165, 2/line | 16x21 |
| without | 700x384, 1/line | 250x250, sharing |

`width: auto` **discards the `width` attribute**, so the used width comes from
whichever `srcset` candidate is chosen — and `sizes="auto"` in a flex container
picks a small one. `flex: 0 0 auto` is the other half: a flex item shrinks below
its intrinsic width by default. `width: auto` now survives only for `.npf_row`,
whose `width: 100%` would otherwise force one image per line.

A top-level `<img>` also gets `margin-bottom: 1em` — images inside a `<p>` take
spacing from the paragraph, a bare one has none.

## dresdencodak draws its own list crop, for half its comics

`dc_minis_<n>.jpg` is the article image (#27 is 1500x4875, a sliver as a
thumbnail); `dc_minis_<n>_thumbnail.jpg` is a 2500x1000 crop. Derived, not
fetched — `thumbnail_from_lead_image` runs on the render path and is network-free
by contract.

Restricted to `dc_minis_`: every DC Minis strip has the crop, no Dark Science
page does (`ds_185`, `ds_186_silder`, `ds_187_silder` all 404). The **reverse**
derivation is absent on purpose — stripping `_thumbnail` 404s for #25 and #22,
whose only published file is the `_thumbnail` one, so that *is* their comic.
Demoting the crop while scoring the page was tried and is inert: those pages
carry no other candidate.

## A comic's prev/next arrows are not its lead image

dresdencodak's feed opens with `<img alt="Previous" height="30" src=".../prev_002.png">`,
so the arrow won the first-image bonus and became hero and thumbnail. `main.py`
already stripped these from the *body* (`_COMIC_NAV_ALT_RE`); nothing stopped one
being chosen as the lead.

Matched on a basename that is only the nav word plus an optional number, because
these are ordinary English: `first-contact.jpg` is a comic, `prev_002.png` is a
button.

## An `<img>` inside a `<script>` is source code, not an image

monstersoupcomic's bookmark widget does `document.write('<img src="'+imgTag+'">')`,
and the scan produced the lead `https://monstersoupcomic.com/'+imgTag+'`.
`<script>` blocks are stripped once in `_fetch_page_html` rather than at each of
the ten `_IMG_TAG_RE` scan sites. Safe there: this class reads no JSON-LD, and
`og:`/`meta` live in `<head>`.

## An age gate is the one image that is definitely not the post

An adult webcomic serves a content-warning interstitial *instead of* the strip,
so a scrape finds it where the comic should be — and on a feed whose body has no
image, `_inject_webcomic_panel_into_bodyless_entry` puts it in the article
(monstersoupcomic captioned a post about paintbrushes with it).

Matched separator-optional (`maturecontentwarning`, `age_gate`, `nsfw-warning`)
but only in gate shapes: `the-warning-sign-chapter-4.jpg` still passes.

Removing it exposed the next layer — `/images/blog_on.png`, a 99x44 nav button.
`<name>_on.png` / `_off.png` is the rollover convention, and the size floor
cannot catch it: the dimensions are neither in the URL nor on the tag.

## A caption that never changes is the site's, not the post's

Webcomic captions fall back to `og:description`, where many sites put a fixed
blurb. The existing guard rejects one equal to `og:site_name`; Penny Arcade ships
no `og:site_name` and a real sentence, so nothing in a single page marks it as
boilerplate.

What marks it is that it does not vary. The test is **across the feed**: if
another entry already carries this exact caption, neither is a caption. The first
entry cannot know and stores it; the second recognises the repeat and
`_clear_feed_boilerplate_title` drops it from every row and the in-memory cache.
Scoped to one feed; `image_alt` is untouched. The live sweep cleared 530 rows
across 139 captions — mostly taglines, plus stock-photo alt text reused across
opensource.com articles.

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

## A thumbnail URL is often the full-size one with a smaller number in it

Some hosts render a single asset at any width from a query parameter, and a feed
links the small one because that is what its own list view uses — leaving the
subscription permanently at thumbnail resolution when the large render is one
number away and costs no extra request.

`feed_display_prefs.image_size_rule` holds `"<param>=<value>"` (e.g. `"w=1600"`)
and `upgrade_image_size_param` applies it in `_lead_image_display_url`, before
proxying, so `/api/img` caches the size actually shown.

The rule is stored per feed rather than hardcoded, for two reasons. Which
parameter carries the width is a fact about one host, and so is the ceiling: a
host that has one commonly ignores
an over-large value and returns its **default thumbnail** instead of an error or
a clamp, so a rule set too high makes images smaller, not larger — the value has
to be probed per host and remembered next to that feed. A URL without the named
parameter is returned unchanged, so a rule on the wrong feed is inert.

## An image-less feed body is missing placement, not just pictures

A feed that ships complete prose and no `<img>` (paizo's blog) hides two things:
the images, and where they went. `inject_source_images` originally appended every
source image as one gallery at the end, which recovers the first and discards the
second.

`_source_article_body` prefers the source article itself, readability-extracted
from the same cached page the gallery already fetched: on a paizo post that is 7
images interleaved through 1,329 words, at the author's own break points, instead
of 7 images in a pile.

**Render-time, never stored.** A feed entry's body is overwritten by the next
refresh — the reason re-fetch refuses feed-provided entries at all — so
substituting on render is the only version of this that survives.

**The fetch is synchronous**, unlike the gallery's prime-and-retry. Deferring to
a later open shows a body still missing its pictures, which is the case
`fetch_source_html_now` exists for. The async path was tried and failed in
practice: a paizo page is ~1MB against a 0.8s wait, and the cache is per-process,
so the first open after any deploy produced *no images at all* — the gallery
fallback reads the same cache. Only the first open of an entry pays.

**A site that marks its content should name it.** `content_selectors` slices the
page to the article before extraction, and the slice then goes through the
*whole-body* path rather than readability — the slice is already only the
article, and re-running readability over a run of sibling section wrappers kept
the single highest-scoring one, emptying two of three test posts. Readability
cannot separate a "Back to Blog" link, a tag row, a sharing widget or a
related-posts rail from the piece, because by its measure they are the same
stuff. Unmatched selectors fall back to whole-page extraction, so a markup
change loses the chrome-trimming rather than the body.

Two guards make it decline, falling back to the gallery: an extraction with no
`<img>`, and one under 50 words. Both describe a page readability could not make
sense of, and the feed's own text is better than a thin substitute. It is also
what keeps a webcomic feed — whose page is one image and no prose — on the
gallery path it already relies on.

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

DeviantArt serves images from wixmp with a signed JWT in the query string. Ordinary deviations are *usually* signed with no `exp` claim at all (which is not the same as permanent — see below); **mature** ones are signed for about **15 minutes** with a readable `exp`, and every variant (`content.src` and every thumb) shares the expiry — so there is no long-lived variant to prefer, and a stored URL is normally dead by the time the post is read, showing neither image nor thumbnail.

Nothing scheduled can fix that: a nightly re-sign yields images dead a quarter of an hour later. The re-sign therefore happens **on open** — `_resign_expired_deviantart_images`, run in `get_entry_detail` just before the hotlink-proxy rewrite.

What keeps it cheap is the proxy's byte cache, which was already most of the answer: `wixmp.com` is in `_HOTLINK_IMG_HOSTS`, so these images render through `/api/img`, and `_img_cache_key_url` strips `token`/`sig`/`exp` (`_IMG_CACHE_VOLATILE_PARAMS`) from the cache key. Once the bytes are cached under *any* valid token they answer for every later one. So the re-sign fires only when a token has already expired **and** the cache has no copy — one API call per image over its lifetime, not one per view. The fresh URL is persisted back onto the entry so the list thumbnail starts from it too.

**"No `exp` claim" does not mean permanent — found 2026-08-20.** A live entry (a GIF) had a JWT with no `exp` field, was trusted as permanently valid on that basis alone, and was already dead (wixmp answered `400 image is invalid` directly, no proxy involved). A feed-wide check found **22,597 of 22,884 stored wixmp tokens carry no `exp` claim** — the code had been trusting all of them blind. Calling the DeviantArt API for every one of those on first open would burn through its rate limit for no reason, since the large majority genuinely are fine — so `_wixmp_url_is_live` interposes a plain HEAD at the image host itself (SSRF-guarded like any outbound fetch, no DA API budget spent) before trusting a no-`exp` token; only a URL that fails that check falls through to a real re-sign.

`scripts/refresh_expired_deviantart_images.py` remains as a manual catch-up over the same routine. Note it must use `get_deviantart_user_token()` rather than reading `deviantart_access_token` directly: DA access tokens last an hour, so any batch reading the stored value 401s on almost every run.

## Pinning a list thumbnail before its signed URL dies

The re-sign above closes the loop for the *article* view, but only because
someone opened the article — that is what makes `/api/img`'s cache hold a
copy. Measured 2026-08-18: of 22,903 stored wixmp lead-image URLs, only 583
(2.5%) had ever had bytes land in that cache. The other 97.5% are entries
nobody has opened, so their **list** thumbnail — which reads the stored URL
straight out of `entry_lead_images` and never goes through `get_entry_detail`
— has nothing to fall back on once the token expires. A feed's thumbnails
stayed broken until each entry was opened once, by hand.

The fix pins bytes proactively, during the enhance pass, while the token is
still fresh — rather than waiting for a page view to populate the cache.
`services.lead_images.LeadImageService.store_entry_lead_image` is the single
choke point every lead-image write goes through (enhance pass, DeviantArt/
dev.to API seeding, the tuning-strategy preview route), so a sink hooked
there (`set_thumb_pin_sink`, wired in `main.py` at import time — the same
shape as `set_page_tag_sink`) sees every candidate exactly once, without
`services/lead_images.py` importing anything from `main` (the fetch/cache
code has to live in `main.py`, where `httpx`/`url_guard`/`img_cache` already
live; services must not import main).

**Detection is host-agnostic.** `_url_is_signed` reuses
`_IMG_CACHE_VOLATILE_PARAMS` — the same query-param set `_img_cache_key_url`
already strips — rather than a DeviantArt-specific check, so any future
signing CDN (S3 presigned URLs, Tapas's `__token__`) gets the same treatment
for free.

**A separate cache namespace, not the general `/api/img` one.** `/api/img`
and `/thumb` share one cache key formula (`sha256` of the URL with signing
params stripped), because both want the *same* bytes at *full* resolution.
Pinning could have reused that key — `/thumb`'s existing fallback already
checks it — but the pinned copy is deliberately smaller (~30 KB target,
`_ENTRY_THUMB_MAX_DIM` = 400px, vs ~121 KB average in the general cache), to
keep a backfill over tens of thousands of entries cheap. Storing that
downscaled copy under the *shared* key would mean `/api/img` serves the
low-res thumbnail as the full article image too. So entry pins get their own
prefix (`entrythumb:`, keyed by `(feed_url, entry_id)` via
`_entry_thumb_cache_key` — sha1, not the URL, for the same reason
`_feed_thumb_cache_key` isn't: re-pinning replaces the copy in place and an
expiring source URL can't orphan it) and their own serving route
(`/api/entry-thumb?feed_url=&entry_id=`), exempted from `_evict_img_cache`'s
TTL sweep exactly like the per-feed pin. `list_entries_for_feeds` substitutes
that stable URL for the raw signed one whenever a pin already exists for the
entry being rendered (`_url_is_signed(_thumb) and
has_pinned_entry_thumbnail(...)`) — a cheap indexed lookup per rendered row,
not a bulk pre-fetch, because unlike the feed tree a post list is already
page-bounded.

**Pin once, not every refresh.** The sink is a no-op once a pin exists
(`has_pinned_entry_thumbnail` gates the fetch) — the enhance pass calls
`store_entry_lead_image` for an entry every refresh even when nothing
changed (DeviantArt's wixmp token differs on every fetch even though the
underlying image doesn't), so without that gate every DeviantArt entry would
re-fetch its own thumbnail on every pass. This assumes an entry's image
never changes after publish, true for DeviantArt deviations (the actual
driving case); a feed that legitimately swaps an entry's image later would
need its stale pin dropped by hand.

**Go-forward only.** Pinning happens when a lead image is *stored*, so it
does nothing for the 22,320 already-expired URLs sitting in
`entry_lead_images` today — those still need the article-view re-sign (or a
one-time backfill script, not yet built) to recover. What it does prevent is
the number growing further: every DeviantArt entry ingested from now on gets
pinned before its token can die unread.

## Rendering an entry's markup

> Moved from `tenancy.md`'s security list on 2026-08-13.

- **Presentational formatting is preserved, by enumerated allowlist.** Bold and
  italic always survived (`b`/`strong`/`i`/`em` are allowed tags), but *centering*
  did not: `style` was stripped wholesale, `align` was granted only to `img`/`td`/
  `th`/`tr`, and `<center>` fell through the unknown-tag unwrap. Since feed CSS is
  never loaded, nothing could restore the author's intent afterwards. Now
  `<center>` is allowed, `align` extends to block elements (`p`/`div`/`figure`/
  `figcaption`/`h1`–`h6`/`table` — the same "value-constrained, no scripting
  surface" reasoning already applied to table cells, which had simply never been
  extended), and `_sanitize_style_attr` keeps a **fixed table of property →
  literal values** (`text-align`, `font-style`, `font-weight`,
  `text-decoration`, `text-transform`, `font-variant`).
  **Nothing free-form is ever kept**, so there is no place for `url(…)`,
  `expression(…)`, `-moz-binding`, or an escaped payload to survive — an unlisted
  property *or* an unlisted value is dropped rather than cleaned-and-kept, and the
  declarations are re-emitted from the table so the output string is ours.
  Layout and positioning (`position`, `z-index`, `width`, `display`, `opacity`)
  are deliberately excluded: without any scripting they still let feed content
  escape the pane or overlay the app's own UI. The normalized output spacing
  (`text-align: center`) is load-bearing — `style.css` keys its centering rules
  off that exact string.
- **JS-dependent chrome is stripped at render** (`_strip_js_dependent_chrome`).
  Share widgets and lazy "related posts" carousels only become anything once the
  source page's own JavaScript runs; we don't run it, so they arrive as rows of
  dead icons and empty bullets (paizo.com ends every post with a
  `div.sharing_widget` of href-less anchors plus four
  `<li class="blog-item loading">` holding a dice spinner). The safety rule is
  narrow and load-bearing: **only elements with no text *and* no `<img>` are
  removed**, so a real related-posts block (which has headlines) and a real
  gallery (which has images) can never match on class name alone.
- **Sphinx/dvisvgm math sizing** — blogs like eli.thegreenplace.net emit formulas
  as `<object type="image/svg+xml">` / `<img>` whose *true* rendered height rides on
  an inline `style="height: Npx"` (the SVGs' intrinsic dimensions are in `pt`, which
  renders tiny) plus a `valign-mN` baseline class. Since the allowlist strips inline
  `style`, `_promote_math_height` lifts that px height onto a real `height` attribute
  (already allowlisted) before the strip; CSS then honors the per-glyph height and
  `valign-*` baseline instead of flattening every formula to one size. `_MATH_SCALE`
  (default 1.0) is the single knob to enlarge all math (requires re-ingest to apply).
- **Reader-view embed re-injection** — `python-readability`'s `.summary()` strips
  *every* `<iframe>` during extraction (and sometimes keeps the lead image twice),
  so allowlisted players would vanish from Reader view. `build_readability_response`
  pulls the allowlisted embeds out of the raw page (`_reinject_readability_embeds`,
  reusing `_embed_host_allowed`) and appends any the extracted article is missing
  *before* the sanitizer runs — so the re-injected iframes still get sandboxed by
  `_sanitize_iframe`. `_dedupe_readability_images` then drops repeated `<img>` tags
  sharing an `src`. Responsive CSS sizes the iframes (16/9, Spotify fixed-height).
  Because Reader view is served from Lectio's own origin, relative `src`/`href`
  URLs would resolve against Lectio and 404: `Document(url=source_url)` lets
  readability absolutize the summary, and `_absolutize_article_urls` then runs a
  final `make_links_absolute` pass over the article (covering the BS4 content
  fallback, which returns its element verbatim) — fixes pages that use
  page-relative image paths with no `<base>` tag (e.g. fabiensanglard.net).
- **Feed-side YouTube recovery** — the embed `<iframe>` is stripped at ingest but
  the raw feed still carries it, so the media scan (`extract_youtube_embeds`,
  re-parsing the raw feed with sanitize off) caches the video ids and
  `_inject_recovered_youtube_embeds` refills the empty placeholder it left behind:
  WordPress' `<figure class="...is-provider-youtube">` **or** ArtStation's
  `<div class="video-wrapper media-asset...">` (matched by `_YT_EMBED_PLACEHOLDER_RE`).
  The id scan recognizes both the standard and privacy host (`youtube-nocookie.com`).
- **Source-page embed recovery (feed pane)** — entries ingested *before*
  `services.reader_sanitize` stopped stripping `<iframe>` at feed-parse time lost
  their players, and (unlike the placeholders above) leave *nothing* to refill —
  no `figure`, no `video-wrapper` — and the raw feed item has often scrolled out
  of the window, so the feed-side scan can't help. `_inject_recovered_source_embeds` (called from `get_entry_detail`
  after the cleanups, skipped for native YouTube feeds) handles this: when the
  stored body has no `<iframe>` and the entry has a source link, it reads the
  lead-image **source-HTML cache** (shared with the lead-image scraper, so it's
  often already warm; on a miss it queues `queue_source_html_fetch` and leaves the
  body unchanged — never blocking the render on a network GET — so the embed fills
  in on a later open), then `_extract_source_embed_iframes` pulls the allowlisted
  players (`_embed_host_allowed`) — YouTube rebuilt via `_youtube_embed_html`
  (honors the host preference), the rest sanitized in place (Bandcamp/SoundCloud
  esig/track signatures preserved verbatim). `_place_recovered_embeds` then puts
  each one **in context** rather than dumping them at the bottom: (1) replace a
  bare body link that points at the same media (so the player takes the place of
  the link the feed showed instead — matched by video id for YouTube, by the
  embed's fallback `<a href>` for Bandcamp/SoundCloud), (2) fill empty `<p></p>`
  placeholders that follow a heading (the stripped embed slots, e.g. theobelisk's
  `<h3>title</h3><p></p>`) in document order, (3) append leftovers. Mirrors the
  Reader-view recovery but for the normal entry pane.
- **Bandcamp single-track embeds** — Bandcamp's `.../tracks=<ids>/esig=<sig>/`
  player form is domain-locked: Bandcamp validates the Referer against the
  publisher's site and serves "Sorry, this track or album is not available."
  anywhere else (confirmed by headless test — the same iframe plays from the
  publisher domain but not from Lectio). `_strip_bandcamp_track_signature` drops
  the `tracks`/`esig` path segments so the embed falls back to the plain
  `album=<id>` player, which embeds on any site and streams the same pre-order/
  premiere album. Applied to feed-native and source-recovered embeds in
  `get_entry_detail`, and to both reader-view render paths.
