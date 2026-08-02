/* Offline service worker — reading without a connection, and acting without one.
 *
 * Began as a probe (2026-07-29) asking whether a worker would register at all in
 * the Supernote's WebView. It does, precaching works, and it now carries the
 * offline action queue too (see outbox.js), so this is a feature rather than an
 * experiment.
 *
 * The Supernote's browser is an Android WebView with no download handler at all
 * (no <a download>, no long-press "save link"), so every file-based route to
 * offline reading is closed. A service worker is the only remaining in-browser
 * option, and the reason it can work where a cache alone cannot: it intercepts
 * the NAVIGATION, so the saved hyperlink to /read still resolves with WiFi off.
 *
 * Deliberately network-first. Lectio is a live app; serving a stale Inbox to a
 * device that has WiFi would be a worse bug than not working offline at all.
 * The cache is a fallback, never the primary.
 */
// Bumped when the caching RULES change, not just the code: v1 stored article
// URLs built without the active sort, so its entries were keyed to URLs nothing
// navigates to. Old caches are deleted on activate rather than left to be hit by
// an ignoreSearch match — which is precisely how v1's mis-keying stayed hidden.
// v3: the worker now derives an article's images from the article it just
// cached rather than from a server manifest indexed by position, so a v2 cache
// holds articles whose images were chosen for a different set of articles.
const CACHE = "lectio-offline-v3";

// The reader shell: without these a cached article renders as unstyled HTML and
// its pagination never runs. outbox.js is shell too — an article read offline
// with no outbox is an article you cannot act on.
const SHELL = ["/static/reader.css", "/static/reader.js", "/static/outbox.js"];

// The offline action queue, shared verbatim with the page (it guards every DOM
// touch precisely so it can run here). Background Sync replays from the worker,
// where there is no page and therefore no second implementation to keep in step.
importScripts("/static/outbox.js");

self.addEventListener("sync", (event) => {
  if (event.tag !== "lectio-outbox") return;
  event.waitUntil(self.LectioOutbox.flush());
});

self.addEventListener("install", (event) => {
  // Take over without waiting for every tab to close — on a single-window
  // WebView there is no second tab to wait for, so the default would just
  // strand the worker in "waiting" forever.
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    fetch(req)
      .then((resp) => {
        // Refresh the cached copy opportunistically, but only for the things
        // worth having offline. Caching everything would fill the quota with
        // one-off requests and evict the articles.
        if (resp && resp.ok && _worthCaching(url)) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return resp;
      })
      .catch(async () => {
        // Offline. Exact match only for /read: its query string IS the article's
        // identity, so ignoreSearch there matched the cached BROWSE page for
        // every article and re-rendered the list — a cache miss that looked
        // like a successful navigation going nowhere. ignoreSearch is for
        // /static assets whose ?v= cache-buster moved, and nothing else.
        const c = await caches.open(CACHE);
        const loose = url.pathname.startsWith("/static/")
          ? await c.match(req, { ignoreSearch: true })
          : null;
        return (await c.match(req)) || loose ||
               new Response(
                 "<!DOCTYPE html><meta charset=utf-8><title>Offline</title>" +
                 "<body style='font:16px/1.5 serif;padding:2rem'>" +
                 "<h1>Not saved for offline</h1>" +
                 "<p>This page wasn't cached before the network went away.</p>",
                 { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
               );
      })
  );
});

async function _fetchWithTimeout(url, ms) {
  const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = setTimeout(() => ctrl && ctrl.abort(), ms);
  try {
    return await fetch(url, {
      credentials: "same-origin",
      cache: "reload",
      signal: ctrl ? ctrl.signal : undefined,
    });
  } finally {
    clearTimeout(timer);
  }
}

function _worthCaching(url) {
  return url.pathname === "/read" ||
         url.pathname.startsWith("/static/") ||
         url.pathname === "/api/img";
}

// The two endpoints article images are rewritten to. Same-origin alone is too
// loose: a feed's broken relative src resolves against our origin and would be
// cached as a 404. Mirrors isWarmableImagePath in reader.js.
function _isArticleImage(path) {
  return path === "/api/img" || path.indexOf("/starred-asset/") === 0;
}

/* Pull the cacheable image URLs out of an article's HTML.
 *
 * No DOMParser in a worker, so this is a regex — acceptable because it is not
 * parsing untrusted markup for meaning, only harvesting src attributes, and
 * every candidate is then filtered to two known paths. `&amp;` must be undone:
 * src attributes are HTML-escaped, and /api/img URLs carry several query
 * parameters, so a literal "&amp;" would request a URL nothing else ever asks
 * for — cached, counted as a success, and never hit again.
 */
function _imageUrlsIn(html) {
  const out = [];
  const seen = new Set();
  const re = /<img\b[^>]*?\ssrc\s*=\s*["']([^"']+)["']/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    const src = m[1].replace(/&amp;/g, "&");
    if (src.charAt(0) !== "/") continue;      // cross-origin: opaque, unusable
    const path = src.split("?")[0];
    if (!_isArticleImage(path)) continue;
    if (seen.has(src)) continue;
    seen.add(src);
    out.push(src);
  }
  return out;
}

// Which of these URLs are already stored? This is what makes "Save 20 more"
// mean "the next 20 I do not have" rather than "the next 20 by index" — a
// position-based cursor silently re-saves and skips articles whenever new items
// land at the top of the list, which in the Inbox is the entire point of the
// Inbox.
self.addEventListener("message", (event) => {
  const msg = event.data || {};
  if (msg.type !== "cached") return;
  event.waitUntil((async () => {
    const c = await caches.open(CACHE);
    const have = [];
    for (const u of (msg.urls || [])) {
      if (await c.match(new Request(u))) have.push(u);
    }
    const clients = await self.clients.matchAll({ includeUncontrolled: true });
    clients.forEach((cl) => cl.postMessage({ type: "cached-result", have }));
  })());
});

// Precache on demand, driven by the page (which knows what the Inbox holds).
// Reports per-URL results back so the UI can state what actually happened
// rather than claiming success.
//
// The page sends ARTICLES only. Images are derived here, from each article as
// it is cached — the server used to supply them from a manifest sliced by
// position, which meant the images cached belonged to whichever articles sat at
// those indexes, not to the ones actually saved. Deriving them from the bytes
// just stored makes that mismatch unrepresentable.
self.addEventListener("message", (event) => {
  const msg = event.data || {};
  if (msg.type !== "precache") return;
  const urls = msg.urls || [];
  event.waitUntil((async () => {
    const c = await caches.open(CACHE);
    let ok = 0, failed = 0;
    // Split by kind and keep examples: "10 failed" is not an answer. On a device
    // this decides between "images are flaky" (fine, text still reads) and
    // "articles didn't cache" (the feature does not work) — opposite conclusions
    // from the same number.
    const detail = { articles_ok: 0, articles_failed: 0, images_ok: 0, images_failed: 0 };
    const examples = [];
    // Images found inside the articles, queued behind them: a quota cut-off
    // should cost pictures, not whole articles.
    const imageQueue = [];
    const queued = new Set();
    for (const u of urls) {
      const isArticle = u.indexOf("/read") === 0;
      try {
        // credentials: same-origin — /read is behind auth, and an opaque
        // cross-origin response would cache a login page as the article.
        // A hung request must not stall the whole run, so each one is raced
        // against a timeout; without this the UI sits on "Saving…" forever.
        const resp = await _fetchWithTimeout(u, 20000);
        if (!resp.ok) {
          failed++;
          isArticle ? detail.articles_failed++ : detail.images_failed++;
          if (examples.length < 8) examples.push("HTTP " + resp.status + " " + u.slice(0, 90));
          continue;
        }
        await c.put(new Request(u), resp.clone());
        ok++;
        isArticle ? detail.articles_ok++ : detail.images_ok++;
        if (isArticle) {
          // resp was already consumed by the put above; read the text from a
          // second clone taken before it.
          for (const src of _imageUrlsIn(await resp.clone().text())) {
            if (queued.has(src)) continue;
            queued.add(src);
            imageQueue.push(src);
          }
        }
      } catch (e) {
        failed++;
        isArticle ? detail.articles_failed++ : detail.images_failed++;
        if (examples.length < 8) examples.push(String(e).slice(0, 40) + " " + u.slice(0, 90));
      }
    }
    for (const u of imageQueue) {
      try {
        // Already stored from an earlier batch (articles share images far more
        // often than you would guess — the same feed logo, the same author
        // avatar). Re-fetching them would triple a save's network cost.
        if (await c.match(new Request(u))) { detail.images_ok++; ok++; continue; }
        const resp = await _fetchWithTimeout(u, 20000);
        if (!resp.ok) {
          failed++; detail.images_failed++;
          if (examples.length < 8) examples.push("HTTP " + resp.status + " " + u.slice(0, 90));
          continue;
        }
        await c.put(new Request(u), resp.clone());
        ok++; detail.images_ok++;
      } catch (e) {
        failed++; detail.images_failed++;
        if (examples.length < 8) examples.push(String(e).slice(0, 40) + " " + u.slice(0, 90));
      }
    }
    let quota = null;
    try {
      if (navigator.storage && navigator.storage.estimate) {
        const est = await navigator.storage.estimate();
        quota = { usage: est.usage, quota: est.quota };
      }
    } catch (e) { /* not fatal */ }
    const clients = await self.clients.matchAll({ includeUncontrolled: true });
    clients.forEach((cl) => cl.postMessage({
      type: "precache-done", ok, failed, quota, detail, examples,
    }));
  })());
});
