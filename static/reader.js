/* Lectio e-ink reader view — paginator.
 *
 * Splits the article into screen-width CSS columns (see reader.css) and turns
 * "pages" by translating the column container horizontally. No scrolling: taps
 * on the left third go back a page, the right two-thirds go forward; arrow /
 * page / space keys do the same. At the first/last page, turning past the edge
 * navigates to the previous/next article (data-prev / data-next hrefs). A-/A+
 * adjusts body size and re-paginates; the size persists in localStorage.
 * Intentionally tiny and dependency-free for a slow e-ink browser. */
(function () {
  "use strict";

  var cols = document.getElementById("reader-columns");
  var viewport = document.getElementById("reader-viewport");
  var pageInfo = document.getElementById("reader-pageinfo");
  if (!cols || !viewport) return;

  // Prev/next/back targets come from a server-rendered inline object, not DOM
  // attributes, so no navigation URL is ever read from the DOM.
  var NAV = window.__READER_NAV__ || {};

  var FS_KEY = "lectio-reader-fontsize";
  var FS_MIN = 0.9, FS_MAX = 1.9, FS_STEP = 0.1, FS_DEFAULT = 1.2;

  // When the page count becomes trustworthy. Late images and fonts change the
  // article's height, and on a cold load a long article can measure as a single
  // page until they land — which would trip "last page reached" and mark an
  // unread article read the moment it was opened. Observed, not hypothetical.
  //
  // A fixed delay can't fix this: it is a race against however long the images
  // take. So the count is only trusted once the document is complete and every
  // image has resolved, polled up to SETTLE_MAX_TRIES. The cap matters too — an
  // image that never loads must not block mark-read forever.
  var SETTLE_MS = 350;         // first check, and the cosmetic re-measure
  var SETTLE_POLL_MS = 250;    // re-check interval while still loading
  var SETTLE_MAX_TRIES = 20;   // ~5s ceiling, then trust what we have

  var page = 0;   // 0-indexed current page
  var pages = 1;  // total pages

  function pageWidth() { return window.innerWidth; }

  function currentFs() {
    var v = parseFloat(window.localStorage.getItem(FS_KEY));
    if (!isFinite(v)) v = FS_DEFAULT;
    return Math.min(FS_MAX, Math.max(FS_MIN, v));
  }

  function applyFs(fs) {
    document.documentElement.style.setProperty("--reader-fs", fs.toFixed(2) + "rem");
    try { window.localStorage.setItem(FS_KEY, fs.toFixed(2)); } catch (e) { /* private mode */ }
  }

  // Mark-read is earned by reaching the last page, not by opening the article:
  // the server no longer marks on render, because in a browse loop opening an
  // item is how you decide whether to read it.
  var readMarked = false;
  var paginationSettled = false;

  function markReadIfFinished() {
    // Held until pagination settles (SETTLE_MS): a long article can measure as
    // one page before its images load, and marking off that first measurement
    // would reintroduce exactly the peek-marks-read bug from the other side.
    if (readMarked || !paginationSettled) return;
    if (page < pages - 1) return;
    var feed = cols.getAttribute("data-feed");
    var entry = cols.getAttribute("data-entry");
    if (!feed || !entry) return;
    readMarked = true;
    // Fire-and-forget: nothing on screen depends on the reply, and a failure
    // should not interrupt reading. The header asks for the JSON reply and the
    // read-history append, matching what the server used to do on render.
    post("/entries/read", {
      folder_id: "0", feed_url: feed, entry_id: entry, read: "1", select_entry: "0",
    }, "lectio-entry-read-toggle");
  }

  function render() {
    cols.style.transform = "translateX(" + (-page * pageWidth()) + "px)";
    if (pageInfo) pageInfo.textContent = (page + 1) + " / " + pages;
    markReadIfFinished();
  }

  function recompute(keepRatio) {
    var ratio = pages > 1 ? page / (pages - 1) : 0;
    // Reading scrollWidth forces the layout to settle before we measure.
    var total = cols.scrollWidth;
    pages = Math.max(1, Math.round(total / pageWidth()));
    page = keepRatio ? Math.round(ratio * (pages - 1)) : 0;
    if (page > pages - 1) page = pages - 1;
    if (page < 0) page = 0;
    render();
  }

  function go(href) {
    // Only follow app-generated same-origin paths; never a javascript:/data:/
    // cross-origin URL.
    if (!href) return;
    try {
      var u = new URL(href, window.location.origin);
      if (u.origin === window.location.origin) {
        window.location.assign(u.pathname + u.search);
      }
    } catch (e) { /* malformed href — ignore */ }
  }

  function nextPage() {
    if (page < pages - 1) { page++; render(); }
    else { go(NAV.next); }
  }

  function prevPage() {
    if (page > 0) { page--; render(); }
    else { go(NAV.prev); }
  }

  function changeFs(delta) {
    applyFs(Math.min(FS_MAX, Math.max(FS_MIN, currentFs() + delta)));
    window.requestAnimationFrame(function () { recompute(true); });
  }

  // Tap zones. Links inside the article keep working (ignored here).
  viewport.addEventListener("click", function (ev) {
    if (ev.target && ev.target.closest && ev.target.closest("a")) return;
    if (ev.clientX < window.innerWidth * 0.3) prevPage();
    else nextPage();
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    switch (ev.key) {
      case "ArrowRight":
      case "ArrowDown":
      case "PageDown":
      case " ":
        ev.preventDefault(); nextPage(); break;
      case "ArrowLeft":
      case "ArrowUp":
      case "PageUp":
        ev.preventDefault(); prevPage(); break;
      case "+":
      case "=":
        ev.preventDefault(); changeFs(FS_STEP); break;
      case "-":
      case "_":
        ev.preventDefault(); changeFs(-FS_STEP); break;
    }
  });

  var plus = document.getElementById("reader-fs-plus");
  var minus = document.getElementById("reader-fs-minus");
  if (plus) plus.addEventListener("click", function (e) { e.preventDefault(); changeFs(FS_STEP); });
  if (minus) minus.addEventListener("click", function (e) { e.preventDefault(); changeFs(-FS_STEP); });

  // Swipe to turn pages (touch). Horizontal drag past a threshold pages the
  // article; the tap zones and keys still work for non-touch.
  var sx = 0, sy = 0, tracking = false;
  viewport.addEventListener("touchstart", function (ev) {
    var t = ev.changedTouches[0]; sx = t.clientX; sy = t.clientY; tracking = true;
  }, { passive: true });
  viewport.addEventListener("touchend", function (ev) {
    if (!tracking) return;
    tracking = false;
    var t = ev.changedTouches[0];
    var dx = t.clientX - sx, dy = t.clientY - sy;
    if (Math.abs(dx) < 45 || Math.abs(dx) < Math.abs(dy)) return; // not a horizontal swipe
    if (dx < 0) nextPage(); else prevPage();
  }, { passive: true });

  // Archive / Delete(unsave) — POST then advance to the next article (or back
  // to the list when there is none). CSRF from the page meta.
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }
  function afterAction() {
    go(NAV.next || NAV.back || "/read");
  }
  // `requestedWith` is required, not defaulted: it names the async-action mode
  // the target route checks, and routes disagree about which token they accept.
  // Without the right one the endpoint answers with a redirect back into the
  // main app instead of JSON, so each call site declares its own.
  function post(url, params, requestedWith) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrfToken(),
        "X-Requested-With": requestedWith,
      },
      body: new URLSearchParams(params).toString(),
      credentials: "same-origin",
    });
  }
  function postAction(url, params, requestedWith) {
    post(url, params, requestedWith).then(afterAction, afterAction);
  }
  var archiveBtn = document.getElementById("reader-archive-btn");
  if (archiveBtn) archiveBtn.addEventListener("click", function (e) {
    e.preventDefault();
    var archived = archiveBtn.getAttribute("aria-pressed") === "true" ? "0" : "1";
    postAction("/entries/archive", {
      feed_url: cols.getAttribute("data-feed"),
      entry_id: cols.getAttribute("data-entry"),
      archived: archived,
    }, "lectio-entry-save-toggle");
  });
  // Delete removes the item from Kept entirely: star AND tags, plus marking it
  // read. One POST to /entries/discard rather than the old clear-tags-then-
  // unstar chain — the ordering that made the chain safe (tags first, or the
  // offline capture is stranded) is server-side knowledge and now lives there.
  var deleteBtn = document.getElementById("reader-delete-btn");
  if (deleteBtn) deleteBtn.addEventListener("click", function (e) {
    e.preventDefault();
    var feed = cols.getAttribute("data-feed");
    var entry = cols.getAttribute("data-entry");
    var tags = (deleteBtn.getAttribute("data-tags") || "").split(",").filter(Boolean);
    // Losing tags is not recoverable, so name them before doing it. A plain
    // unstar stays unconfirmed — that one is cheap to undo.
    if (tags.length && !window.confirm(
      "Remove this from Saved?\n\nIt will lose its star and these tags: "
      + tags.map(function (t) { return "#" + t; }).join(" ")
    )) return;
    postAction("/entries/discard", { feed_url: feed, entry_id: entry }, "lectio-ajax");
  });

  // Warm the next article's images, to cut the e-ink refresh flash on advance.
  //
  // Only the *images* are prefetched, not the page. The reader page is served
  // Cache-Control: no-store, so a <link rel=prefetch> would fetch the next
  // article and immediately throw it away — cost with no benefit. Images come
  // from /api/img and /starred-asset/ with real max-age, so they survive in the
  // HTTP cache and are what actually makes an e-ink advance repaint slowly.
  //
  // This is only safe because the server no longer marks an entry read when its
  // reader page is served: fetching the next article's HTML to find its images
  // would otherwise have marked it read without it ever being seen.
  var PREFETCH_MAX_IMAGES = 12;   // a lesson-length article can carry 50+
  var PREFETCH_DELAY_MS = 1200;   // breathing room after the settle, see below

  // The image proxy is a fixed path with the source in the query string; the
  // starred archive serves per-asset paths under a prefix.
  function isWarmableImagePath(path) {
    return path === "/api/img" || path.indexOf("/starred-asset/") === 0;
  }

  function prefetchNextImages() {
    if (!NAV.next) return;
    var u;
    try {
      u = new URL(NAV.next, window.location.origin);
      if (u.origin !== window.location.origin) return;
    } catch (e) { return; }
    fetch(u.pathname + u.search, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html) return;
        // DOMParser builds a detached document: it runs no scripts and loads no
        // resources, so this only reads attributes. The warming below is what
        // actually fetches, and only for same-origin URLs.
        var doc = new DOMParser().parseFromString(html, "text/html");
        var imgs = doc.querySelectorAll("#reader-article img");
        var n = Math.min(imgs.length, PREFETCH_MAX_IMAGES);
        for (var i = 0; i < n; i++) {
          var src = imgs[i].getAttribute("src");
          if (!src) continue;
          try {
            var iu = new URL(src, window.location.origin);
            if (iu.origin !== window.location.origin) continue;
            // Warm only the two endpoints article images are rewritten to.
            // Same-origin alone is too loose: a feed's broken relative src
            // resolves against our origin and would be prefetched into a 404,
            // and anything else same-origin (a /static/ placeholder) is already
            // cached or not worth the request. These two are also the only ones
            // with a real max-age, which is the whole reason this works.
            if (!isWarmableImagePath(iu.pathname)) continue;
            new Image().src = iu.pathname + iu.search;
          } catch (e) { /* malformed src — skip */ }
        }
      })
      .catch(function () { /* prefetch is best-effort; never disturb reading */ });
  }

  // --- Tags ---------------------------------------------------------------
  // Filing from the device, without a keyboard: every tag in the library is a
  // tap target, tap toggles it on or off. Each tap applies immediately (the
  // whole desired set is sent, so add and remove are one code path), which means
  // closing the panel half way still saved what you tapped.
  var TAGS = window.__READER_TAGS__ || { all: [], current: [], max: 12 };
  var tagPanel = document.getElementById("reader-tag-panel");
  var tagBtn = document.getElementById("reader-tag-btn");
  var tagList = document.getElementById("reader-tag-list");
  var current = (TAGS.current || []).slice();

  function syncTagChrome() {
    if (tagBtn) tagBtn.textContent = "#" + (current.length || "");
    // Delete names the tags it is about to destroy, so keep it truthful as the
    // set changes underneath it.
    if (deleteBtn) deleteBtn.setAttribute("data-tags", current.join(","));
  }

  function renderTags() {
    if (!tagList) return;
    // Applied tags first, then the rest alphabetically: what is on this entry is
    // what you are most likely to be toggling off.
    var all = (TAGS.all || []).slice();
    current.forEach(function (t) { if (all.indexOf(t) < 0) all.push(t); });
    all.sort(function (a, b) {
      var ia = current.indexOf(a) < 0 ? 1 : 0, ib = current.indexOf(b) < 0 ? 1 : 0;
      return ia !== ib ? ia - ib : a.localeCompare(b);
    });
    tagList.textContent = "";
    all.forEach(function (name) {
      var on = current.indexOf(name) >= 0;
      var b = document.createElement("button");
      b.type = "button";
      b.className = "reader-tag" + (on ? " on" : "");
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.textContent = (on ? "✓ " : "") + name;
      b.addEventListener("click", function () { toggleTag(name); });
      tagList.appendChild(b);
    });
    syncTagChrome();
  }

  function applyTags(next) {
    // Full desired set with append_mode=0, so a removal is the same request as
    // an addition. The server normalizes and caps, and its reply is the truth.
    return post("/entries/tags", {
      folder_id: "0", feed_url: TAGS.feed_url, entry_id: TAGS.entry_id,
      tags_text: next.map(function (t) { return "#" + t; }).join(" "),
      append_mode: "0", select_entry: "0",
    }, "lectio-ajax")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.tags) {
          current = data.tags.slice();
          (TAGS.all = TAGS.all || []);
          current.forEach(function (t) {
            if (TAGS.all.indexOf(t) < 0) TAGS.all.push(t);
          });
        }
        renderTags();
      }, function () { renderTags(); });
  }

  function toggleTag(name) {
    var i = current.indexOf(name);
    if (i >= 0) current.splice(i, 1);
    else if (current.length < (TAGS.max || 12)) current.push(name);
    else return;                       // at the cap; ignore rather than silently drop
    renderTags();                      // optimistic: e-ink should react on tap
    applyTags(current);
  }

  if (tagBtn) tagBtn.addEventListener("click", function (e) {
    e.preventDefault();
    var open = tagPanel && tagPanel.hasAttribute("hidden");
    if (!tagPanel) return;
    if (open) { renderTags(); tagPanel.removeAttribute("hidden"); }
    else { tagPanel.setAttribute("hidden", ""); }
    tagBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  var tagDone = document.getElementById("reader-tag-done");
  if (tagDone) tagDone.addEventListener("click", function () {
    tagPanel.setAttribute("hidden", "");
    if (tagBtn) tagBtn.setAttribute("aria-expanded", "false");
  });
  var tagNewBtn = document.getElementById("reader-tag-new");
  var tagForm = document.getElementById("reader-tag-newform");
  var tagInput = document.getElementById("reader-tag-input");
  // The keyboard stays out of the way until asked for — it is the slow path.
  if (tagNewBtn) tagNewBtn.addEventListener("click", function () {
    if (!tagForm) return;
    var show = tagForm.hasAttribute("hidden");
    if (show) { tagForm.removeAttribute("hidden"); if (tagInput) tagInput.focus(); }
    else tagForm.setAttribute("hidden", "");
  });
  if (tagForm) tagForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var raw = (tagInput && tagInput.value || "").trim();
    if (!raw) return;
    if (tagInput) tagInput.value = "";
    // Send it as typed and let the server normalize; its reply re-renders us.
    applyTags(current.concat([raw.replace(/^#/, "")]));
  });
  syncTagChrome();

  var reflowTimer = null;
  window.addEventListener("resize", function () {
    if (reflowTimer) window.clearTimeout(reflowTimer);
    reflowTimer = window.setTimeout(function () { recompute(true); }, 150);
  });

  // Init: set persisted size, then paginate once layout/images have settled.
  applyFs(currentFs());
  function init() { recompute(false); }
  if (document.readyState === "complete") init();
  else window.addEventListener("load", init);
  // Late images/fonts can change article height; re-measure shortly after. This
  // is also the point the page count is trusted, so mark-read opens for business
  // — but only once there is nothing left to load that could change it.
  function imagesResolved() {
    var imgs = cols.querySelectorAll("img");
    for (var i = 0; i < imgs.length; i++) {
      // .complete covers loaded *and* errored, which is what we want: a broken
      // image is settled, it just has no height to contribute.
      if (!imgs[i].complete) return false;
    }
    return true;
  }

  var settleTries = 0;
  function trySettle() {
    recompute(true);   // cosmetic re-measure happens on every pass
    if ((document.readyState !== "complete" || !imagesResolved())
        && settleTries++ < SETTLE_MAX_TRIES) {
      window.setTimeout(trySettle, SETTLE_POLL_MS);
      return;
    }
    paginationSettled = true;
    markReadIfFinished();
    // Prefetch last, and only once the current article has settled — warming
    // the next one must never compete with rendering the one being read. Hung
    // off the settle rather than a fixed delay, because settling can take up to
    // the SETTLE_MAX_TRIES ceiling on a slow load.
    window.setTimeout(prefetchNextImages, PREFETCH_DELAY_MS);
  }
  window.setTimeout(trySettle, SETTLE_MS);
})();
