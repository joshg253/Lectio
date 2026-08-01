/* Save articles to read without a connection.
 *
 * Began as a probe (2026-07-29) asking whether a service worker would register
 * at all in the Supernote's WebView. It does, and precaching works, so the copy
 * here reads as a feature's rather than an experiment's — it no longer tells the
 * reader to go and test it. It still reports failure loudly, and still states
 * articles separately from images, because the two are not equally important:
 * images failing is a degraded read, articles failing is no feature.
 *
 * That device's browser has no launcher, so offline reading starts from a saved
 * hyperlink — which is why the article page itself has to be cached, not just
 * the pieces it is built from.
 */
(function () {
  "use strict";

  // Articles saved for offline, and the number of articles the manifest is asked
  // to supply images for. These MUST agree: they are two halves of one set.
  const OFFLINE_ARTICLE_COUNT = 20;

  const statusEl = document.getElementById("rm-offline-status");
  const btn = document.getElementById("rm-offline-btn");
  if (!statusEl || !btn) return;

  const say = (msg) => { statusEl.textContent = msg; };

  // Feature report first, so even a total failure leaves a useful answer on
  // screen. This is the whole point of running it on the device.
  const missing = [];
  if (!("serviceWorker" in navigator)) missing.push("serviceWorker");
  if (!("caches" in window)) missing.push("caches");
  if (!("indexedDB" in window)) missing.push("indexedDB");
  if (missing.length) {
    say("This browser can't save articles offline (no " + missing.join(", ") + ").");
    btn.disabled = true;
    return;
  }

  let reg = null;
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).then((r) => {
    reg = r;
    say("Ready — save the next " + OFFLINE_ARTICLE_COUNT + " articles to read without a connection.");
  }).catch((err) => {
    // The likeliest failure on a locked-down WebView, and the answer we came for.
    say("Couldn't set up offline saving: " + err);
    btn.disabled = true;
  });

  navigator.serviceWorker.addEventListener("message", (ev) => {
    const m = ev.data || {};
    if (m.type !== "precache-done") return;
    const d = m.detail || {};
    // Articles first and stated separately: they are the pass/fail criterion.
    // Images failing is a degraded experience; articles failing is no feature.
    let line = "Articles " + (d.articles_ok || 0) + " saved";
    if (d.articles_failed) line += " / " + d.articles_failed + " FAILED";
    line += " · images " + (d.images_ok || 0) + " saved";
    if (d.images_failed) line += " / " + d.images_failed + " failed";
    if (m.quota && m.quota.quota) {
      line += " · " + (m.quota.usage / 1048576).toFixed(1) + " MB used";
    }
    line += ". Ready to read offline.";
    say(line);
    if (m.examples && m.examples.length) {
      const pre = document.getElementById("rm-offline-detail");
      if (pre) { pre.hidden = false; pre.textContent = m.examples.join("\n"); }
    }
    btn.disabled = false;
    btn.textContent = "Save 20 for offline";
  });

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Saving…";
    say("Working out what to save…");
    let data;
    try {
      const qs = new URLSearchParams(location.search);
      const params = new URLSearchParams({ n: String(OFFLINE_ARTICLE_COUNT) });
      for (const k of ["folder_id", "tag", "kept"]) {
        if (qs.get(k)) params.set(k, qs.get(k));
      }
      const resp = await fetch("/read/offline/manifest?" + params.toString(),
                               { credentials: "same-origin" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      data = await resp.json();
    } catch (err) {
      say("Couldn't fetch the article list: " + err);
      btn.disabled = false;
      btn.textContent = "Save 20 for offline";
      return;
    }
    // Article URLs come from the DOM, not the server. The server built them
    // from _reader_href without the active sort, while the real links carry
    // sort=starred — so every cached article sat under a URL nothing navigates
    // to. Reading the hrefs the page actually has makes a mismatch impossible.
    // Capped to the SAME count the manifest was asked for. The list renders up to
    // 150 rows, so taking every href cached 150 articles while asking for images
    // from only the first 20 — the rest went offline with no pictures at all,
    // which is exactly what "images weren't included" looked like on the device.
    const domHrefs = Array.from(document.querySelectorAll(".rm-item-link"))
      .map((a) => a.getAttribute("href"))
      .filter((h) => h && h.indexOf("/read") === 0)
      .slice(0, OFFLINE_ARTICLE_COUNT);
    // Keep only the server's image URLs; its article guesses are superseded.
    const imageUrls = (data.urls || []).filter((u) => u.indexOf("/read") !== 0);
    // This page first: it is the entry point, and without it the saved hyperlink
    // lands nowhere no matter how many articles are cached.
    const urls = [location.pathname + location.search]
      .concat(domHrefs)
      .concat(imageUrls);
    // Articles and images, not "files": one is what you asked for, the other
    // is what it needs. 54 files means nothing to the person who tapped Save.
    const _articles = urls.filter((u) => u.indexOf("/read") === 0).length;
    const _images = urls.length - _articles;
    say("Saving " + _articles + " article" + (_articles === 1 ? "" : "s") +
        (_images ? " and " + _images + " image" + (_images === 1 ? "" : "s") : "") + "\u2026");
    const target = navigator.serviceWorker.controller ||
                   (reg && (reg.active || reg.installing || reg.waiting));
    if (!target) {
      say("Offline saving isn't ready on this page yet — reload and try again.");
      btn.disabled = false;
      btn.textContent = "Save 20 for offline";
      return;
    }
    target.postMessage({ type: "precache", urls: urls });
  });
})();
