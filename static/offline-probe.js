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

  // How many articles one press saves.
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

  /* Every article link this page lists, in display order.
   *
   * The set to save is chosen from these hrefs and nowhere else. The server used
   * to propose article URLs too, built by _reader_href without the active sort,
   * while the real links carry sort=starred — so every cached article sat under
   * a URL nothing navigates to. Reading the hrefs the page actually has makes a
   * mismatch impossible. */
  function listedHrefs() {
    return Array.from(document.querySelectorAll(".rm-item-link"))
      .map((a) => a.getAttribute("href"))
      .filter((h) => h && h.indexOf("/read") === 0);
  }

  let reg = null;
  let cachedResolve = null;

  function workerTarget() {
    return navigator.serviceWorker.controller ||
           (reg && (reg.active || reg.installing || reg.waiting));
  }

  /* Which of these are already stored? Asked of the worker, because the worker
   * owns the cache and is the only thing that can answer truthfully.
   *
   * This replaces a localStorage cursor that counted POSITIONS — press once for
   * items 1-20, again for 21-40. If new articles arrive at the top between
   * presses, the list has shifted underneath the cursor: a few get re-saved and
   * a few are skipped and never offered again. Rare in a backlog folder, routine
   * in the Inbox, where new stars landing at the top is the whole point.
   *
   * Falls back to "nothing is cached" if the worker does not answer, which
   * degrades to re-saving rather than to skipping. Re-saving costs bandwidth;
   * skipping costs an article you thought you had. */
  function alreadyCached(urls) {
    const target = workerTarget();
    if (!target) return Promise.resolve([]);
    return new Promise((resolve) => {
      const timer = setTimeout(() => { cachedResolve = null; resolve([]); }, 5000);
      cachedResolve = (have) => { clearTimeout(timer); resolve(have); };
      target.postMessage({ type: "cached", urls: urls });
    });
  }

  /* Re-read the cache and restate what is left to do.
   *
   * `quiet` keeps the status line as it is and updates only the button. It is
   * used after a save, where the line holds the run's report — how many articles
   * and images landed, how many failed, how much space it took. That report is
   * the whole reason this feature states failure loudly, and overwriting it a
   * moment later with "everything is saved" would hide precisely the runs worth
   * looking at. */
  function refreshReadiness(quiet) {
    const hrefs = listedHrefs();
    if (!hrefs.length) {
      if (!quiet) say("Nothing here to save.");
      btn.disabled = true;
      return;
    }
    alreadyCached(hrefs).then((have) => {
      const todo = hrefs.length - have.length;
      const batch = Math.min(todo, OFFLINE_ARTICLE_COUNT);
      btn.disabled = !todo;
      btn.textContent = todo
        ? (have.length ? "Save " + batch + " more" : "Save " + batch + " for offline")
        : "Saved";
      if (quiet) return;
      if (!todo) { say(have.length + " saved — everything in this list is available offline."); return; }
      say(have.length
        ? have.length + " saved here, " + todo + " to go — save " + batch +
          " more to read without a connection."
        : "Ready — save the next " + batch + " articles to read without a connection.");
    });
  }

  navigator.serviceWorker.register("/sw.js", { scope: "/" }).then((r) => {
    reg = r;
    // A freshly-installed worker is not yet controlling this page, so it cannot
    // be messaged about the cache. Wait for one that can.
    return navigator.serviceWorker.ready;
    // Wrapped, not passed by reference: .then hands the resolved registration
    // to its callback, which would arrive as a truthy `quiet` and suppress the
    // status line this call exists to write.
  }).then(() => refreshReadiness(false)).catch((err) => {
    // The likeliest failure on a locked-down WebView, and the answer we came for.
    say("Couldn't set up offline saving: " + err);
    btn.disabled = true;
  });

  navigator.serviceWorker.addEventListener("message", (ev) => {
    const m = ev.data || {};
    if (m.type === "cached-result") {
      if (cachedResolve) { const f = cachedResolve; cachedResolve = null; f(m.have || []); }
      return;
    }
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
    // Re-ask the cache rather than assuming the batch landed whole: a partial
    // save now resumes exactly where it stopped, with no bookkeeping to drift.
    // Quietly, so the report above survives to be read.
    refreshReadiness(true);
  });

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Saving…";
    say("Working out what to save…");

    const hrefs = listedHrefs();
    const have = new Set(await alreadyCached(hrefs));
    const todo = hrefs.filter((h) => !have.has(h)).slice(0, OFFLINE_ARTICLE_COUNT);
    if (!todo.length) {
      // The list renders a bounded number of rows, so this is the end of what is
      // reachable here — not necessarily the end of the node.
      say("Everything in this list is saved.");
      btn.textContent = "Saved";
      return;
    }
    // This page first: it is the entry point, and without it the saved hyperlink
    // lands nowhere no matter how many articles are cached.
    //
    // Articles only. The worker derives each article's images from the article
    // it just stored, so there is no second list to fall out of step with this
    // one — which is what a server manifest sliced by position always did.
    const urls = [location.pathname + location.search].concat(todo);
    say("Saving " + todo.length + " article" + (todo.length === 1 ? "" : "s") +
        " and their images…");
    const target = workerTarget();
    if (!target) {
      say("Offline saving isn't ready on this page yet — reload and try again.");
      refreshReadiness(true);
      return;
    }
    target.postMessage({ type: "precache", urls: urls });
  });
})();
