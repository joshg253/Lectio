/* Offline probe UI — EXPERIMENT (2026-07-29), not committed.
 *
 * Answers, on the actual device, the questions a capability table cannot:
 *   1. does a service worker REGISTER in this WebView at all;
 *   2. does precaching succeed, and how much quota is there;
 *   3. does the saved hyperlink still open with WiFi off — the real test, and
 *      the one that matters, because the Supernote browser has no launcher.
 *
 * Reports failure loudly. A probe that quietly claims success is worse than no
 * probe, since the next step is a multi-day build resting on its answer.
 */
(function () {
  "use strict";

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
    say("Not supported here: " + missing.join(", ") + ". Offline reading in this browser is out.");
    btn.disabled = true;
    return;
  }

  let reg = null;
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).then((r) => {
    reg = r;
    say("Worker registered (scope " + r.scope + "). Tap to save the next 20 for offline.");
  }).catch((err) => {
    // The likeliest failure on a locked-down WebView, and the answer we came for.
    say("Worker registration FAILED: " + err + " — offline reading in this browser is out.");
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
    line += ". Now turn WiFi off and reopen this page.";
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
    say("Fetching the list…");
    let data;
    try {
      const qs = new URLSearchParams(location.search);
      const params = new URLSearchParams({ n: "20" });
      for (const k of ["folder_id", "tag", "kept"]) {
        if (qs.get(k)) params.set(k, qs.get(k));
      }
      const resp = await fetch("/read/offline/manifest?" + params.toString(),
                               { credentials: "same-origin" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      data = await resp.json();
    } catch (err) {
      say("Could not get the list: " + err);
      btn.disabled = false;
      btn.textContent = "Save 20 for offline";
      return;
    }
    // Article URLs come from the DOM, not the server. The server built them
    // from _reader_href without the active sort, while the real links carry
    // sort=starred — so every cached article sat under a URL nothing navigates
    // to. Reading the hrefs the page actually has makes a mismatch impossible.
    const domHrefs = Array.from(document.querySelectorAll(".rm-item-link"))
      .map((a) => a.getAttribute("href"))
      .filter((h) => h && h.indexOf("/read") === 0);
    // Keep only the server's image URLs; its article guesses are superseded.
    const imageUrls = (data.urls || []).filter((u) => u.indexOf("/read") !== 0);
    // This page first: it is the entry point, and without it the saved hyperlink
    // lands nowhere no matter how many articles are cached.
    const urls = [location.pathname + location.search]
      .concat(domHrefs)
      .concat(imageUrls);
    say("Saving " + urls.length + " file(s)…");
    const target = navigator.serviceWorker.controller ||
                   (reg && (reg.active || reg.installing || reg.waiting));
    if (!target) {
      say("No active worker to save with — registration succeeded but nothing is controlling this page.");
      btn.disabled = false;
      btn.textContent = "Save 20 for offline";
      return;
    }
    target.postMessage({ type: "precache", urls: urls });
  });
})();
