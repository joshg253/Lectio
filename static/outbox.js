/* Offline action outbox — apply locally, sync on reconnect.
 *
 * Offline *reading* works on the Supernote. Offline *acting* did not: Archive,
 * Delete, tagging and mark-read are ordinary POSTs, so with no connection they
 * simply failed and the tap was lost. This queues them instead.
 *
 * Why IndexedDB and not the Cache API: these are mutations. They have to survive
 * the browser being killed, which on that device is how reading sessions usually
 * end — there is no "quit", you just close the lid.
 *
 * Why every action is enqueued FIRST and only then sent, even when online: the
 * failure that actually loses work is not "offline", which the page can see, but
 * the connection dying *mid-POST* — and on Read Mode every action is immediately
 * followed by a navigation, which cancels the request in flight. Post-first-then-
 * queue-on-failure never learns about those. Enqueue-first does, and it costs
 * nothing because the four routes this drives are all idempotent set-state
 * operations (archived=0/1, discard, the full tag set, read=1): replaying one is
 * a no-op, so a record retried after a half-finished flush is harmless.
 *
 * Written against `self` with every DOM touch guarded, so the same file runs in
 * the page AND under importScripts() in the service worker. Background Sync
 * replays the queue from there, with no page open — one implementation, not two
 * that can drift.
 */
(function () {
  "use strict";

  var DB_NAME = "lectio-outbox";
  var DB_VERSION = 1;
  var STORE = "actions";

  // 4xx codes that mean "this action will never apply": the entry is gone, or
  // the request was malformed. Retrying those forever would wedge the queue
  // behind a record that can never drain — the silent-queue failure the whole
  // status line exists to prevent.
  //
  // 403 is deliberately NOT here. On this app a 403 is an expired session or a
  // stale CSRF token from a page that was cached hours ago, neither of which is
  // a verdict on the action. Dropping there would lose the work at exactly the
  // moment the reader is least able to notice.
  var TERMINAL = { 400: 1, 404: 1, 409: 1, 410: 1, 422: 1 };

  var dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      if (!("indexedDB" in self)) { reject(new Error("no indexedDB")); return; }
      var req = self.indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          var store = db.createObjectStore(STORE, { keyPath: "id" });
          // Oldest-first replay: two actions on the same entry must land in the
          // order they were tapped, or an archive-then-unarchive replays as an
          // unarchive-then-archive and the item comes back.
          store.createIndex("ts", "ts");
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error || new Error("open failed")); };
    });
    return dbPromise;
  }

  function tx(mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, mode);
        var out = fn(t.objectStore(STORE));
        t.oncomplete = function () { resolve(out && out.value); };
        t.onerror = function () { reject(t.error || new Error("tx failed")); };
        t.onabort = function () { reject(t.error || new Error("tx aborted")); };
      });
    });
  }

  function uuid() {
    if (self.crypto && self.crypto.randomUUID) return self.crypto.randomUUID();
    // Chrome 96 predates randomUUID. Not a security token — it only has to be
    // unique within one device's queue.
    return "a" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  function enqueue(action) {
    var rec = {
      id: uuid(),
      ts: Date.now(),
      url: action.url,
      params: action.params || {},
      requestedWith: action.requestedWith || "lectio-ajax",
      // Captured at enqueue time, not at send time, because the send may happen
      // in the service worker (Background Sync), which has no document and so no
      // <meta name=csrf-token> to read. The token is session-scoped and stable,
      // so storing it is no less accurate than reading it later.
      csrf: csrfToken(),
    };
    return tx("readwrite", function (store) { store.put(rec); }).then(function () {
      return rec;
    });
  }

  function all() {
    return tx("readonly", function (store) {
      var out = { value: [] };
      var req = store.index("ts").openCursor();
      req.onsuccess = function () {
        var cur = req.result;
        if (!cur) return;
        out.value.push(cur.value);
        cur.continue();
      };
      return out;
    });
  }

  function remove(id) {
    return tx("readwrite", function (store) { store.delete(id); });
  }

  function depth() {
    return tx("readonly", function (store) {
      var out = { value: 0 };
      var req = store.count();
      req.onsuccess = function () { out.value = req.result; };
      return out;
    }).catch(function () { return 0; });
  }

  function csrfToken() {
    var m = self.document && self.document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  function send(rec) {
    return fetch(rec.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": rec.csrf || csrfToken(),
        "X-Requested-With": rec.requestedWith,
      },
      body: new URLSearchParams(rec.params).toString(),
      credentials: "same-origin",
      // The queue is the durability mechanism; the HTTP cache must not answer
      // for a mutation.
      cache: "no-store",
    });
  }

  var flushing = null;

  /* Replay the queue oldest-first. Serial, not parallel: ordering is the point,
   * and a device on a weak WiFi does not benefit from twenty concurrent POSTs.
   * Stops at the first record that neither succeeded nor died definitively —
   * pushing past it would reorder the rest. */
  function flush() {
    if (flushing) return flushing;
    flushing = all().then(function (recs) {
      var sent = 0, dropped = 0;
      return recs.reduce(function (chain, rec) {
        return chain.then(function (stop) {
          if (stop) return true;
          return send(rec).then(function (resp) {
            if (resp.ok) { sent++; return remove(rec.id).then(function () { return false; }); }
            if (TERMINAL[resp.status]) {
              // Logged rather than silently swallowed: a surprising loss should
              // at least be explicable afterwards.
              dropped++;
              if (self.console) {
                console.warn("[outbox] dropping", rec.url, "HTTP " + resp.status, rec.params);
              }
              return remove(rec.id).then(function () { return false; });
            }
            return true;                       // retry later, keep the order
          }, function () { return true; });    // offline — stop, try next time
        });
      }, Promise.resolve(false)).then(function () {
        return { sent: sent, dropped: dropped };
      });
    }).catch(function () {
      return { sent: 0, dropped: 0 };
    }).then(function (r) {
      flushing = null;
      notify();
      return r;
    });
    return flushing;
  }

  /* Queue an action, then kick a flush without waiting for it.
   *
   * The returned promise resolves once the record is DURABLE, not once it is
   * sent — and callers must await that much before navigating. Read Mode's
   * actions are all followed immediately by a page change, and an IndexedDB
   * transaction still open at unload is aborted: not waiting would lose the very
   * record that exists to stop the action being lost. Waiting costs one local
   * write. The flush is deliberately left dangling; if the navigation kills it,
   * the record is still there and the next page load picks it up. */
  function submit(url, params, requestedWith) {
    return enqueue({ url: url, params: params, requestedWith: requestedWith })
      .then(function () {
        notify();
        flush();
        return { queued: true };
      }, function () {
        // No IndexedDB (private mode, or a WebView with storage disabled).
        // Degrade to the old behavior rather than dropping the action.
        return send({ url: url, params: params, requestedWith: requestedWith });
      });
  }

  // --- Status line ---------------------------------------------------------
  // A queue nobody can see is how work gets lost without anyone noticing, so
  // depth is rendered wherever an element asks for it.
  var listeners = [];

  function notify() {
    depth().then(function (n) {
      listeners.forEach(function (fn) { try { fn(n); } catch (e) { /* ignore */ } });
      var els = self.document
        ? self.document.querySelectorAll("[data-outbox-depth]") : [];
      Array.prototype.forEach.call(els, function (el) {
        el.textContent = n
          ? n + " change" + (n === 1 ? "" : "s") + " waiting to sync"
          : "";
        el.hidden = !n;
      });
    });
  }

  function onDepth(fn) { listeners.push(fn); notify(); }

  self.LectioOutbox = {
    submit: submit,
    flush: flush,
    depth: depth,
    onDepth: onDepth,
    _enqueue: enqueue,
    _all: all,
  };

  if (self.document) {
    // Flush on load, and again whenever the network comes back. Background Sync
    // is registered too where it exists, but it cannot be the only path: the
    // device this is for runs Chrome 96 in an Android WebView, where it is
    // absent, and a sync feature that only works on modern browsers is no use on
    // the browser it was built for.
    self.addEventListener("online", function () { flush(); });
    if (self.document.readyState === "loading") {
      self.document.addEventListener("DOMContentLoaded", function () { flush(); });
    } else {
      flush();
    }
    if ("serviceWorker" in self.navigator && "SyncManager" in self) {
      self.navigator.serviceWorker.ready.then(function (reg) {
        return reg.sync.register("lectio-outbox");
      }).catch(function () { /* not supported here; load+online carry it */ });
    }
  }
})();
