/* Offline probe service worker — EXPERIMENT (2026-07-29), not committed.
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
const CACHE = "lectio-offline-v2";

// The reader shell: without these a cached article renders as unstyled HTML and
// its pagination never runs.
const SHELL = ["/static/reader.css", "/static/reader.js"];

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

// Precache on demand, driven by the page (which knows what the Inbox holds).
// Reports per-URL results back so the UI can state what actually happened
// rather than claiming success.
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
      } catch (e) {
        failed++;
        isArticle ? detail.articles_failed++ : detail.images_failed++;
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
