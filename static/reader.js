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

  var FS_KEY = "lectio-reader-fontsize";
  var FS_MIN = 0.9, FS_MAX = 1.9, FS_STEP = 0.1, FS_DEFAULT = 1.2;

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

  function render() {
    cols.style.transform = "translateX(" + (-page * pageWidth()) + "px)";
    if (pageInfo) pageInfo.textContent = (page + 1) + " / " + pages;
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
    // cross-origin URL (the hrefs come from DOM attributes).
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
    else { go(cols.getAttribute("data-next")); }
  }

  function prevPage() {
    if (page > 0) { page--; render(); }
    else { go(cols.getAttribute("data-prev")); }
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
    var next = cols.getAttribute("data-next");
    go(next || cols.getAttribute("data-back") || "/read");
  }
  function postAction(url, params) {
    var body = new URLSearchParams(params);
    fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrfToken(),
        // Ask save-toggle for a JSON reply instead of a redirect to the app.
        "X-Requested-With": "lectio-entry-save-toggle",
      },
      body: body.toString(),
      credentials: "same-origin",
    }).then(afterAction, afterAction);
  }
  var archiveBtn = document.getElementById("reader-archive-btn");
  if (archiveBtn) archiveBtn.addEventListener("click", function (e) {
    e.preventDefault();
    var archived = archiveBtn.getAttribute("aria-pressed") === "true" ? "0" : "1";
    postAction("/entries/archive", {
      feed_url: cols.getAttribute("data-feed"),
      entry_id: cols.getAttribute("data-entry"),
      archived: archived,
    });
  });
  var deleteBtn = document.getElementById("reader-delete-btn");
  if (deleteBtn) deleteBtn.addEventListener("click", function (e) {
    e.preventDefault();
    postAction("/entries/saved", {
      folder_id: "0", saved: "0", select_entry: "0",
      feed_url: cols.getAttribute("data-feed"),
      entry_id: cols.getAttribute("data-entry"),
    });
  });

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
  // Late images/fonts can change article height; re-measure shortly after.
  window.setTimeout(function () { recompute(true); }, 350);
})();
