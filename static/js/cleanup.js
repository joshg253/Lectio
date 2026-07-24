/*
 * Aardvark-style article cleanup for the reading pane.
 *
 * Arms `.entry-content` into a mode where hovering outlines the node under the
 * cursor and a click deletes it, plus isolate / widen / narrow — the original
 * Aardvark bookmarklet's verbs. Nothing persists until Save: every edit is a
 * live DOM mutation with a full undo stack behind it.
 *
 * What Save sends is the *operation list*, not the edited HTML — each op is a
 * structural path (element-child indexes from the content root) plus a
 * fingerprint of the node. services/content_edits.py replays it against the
 * stored body. The two fingerprint builders must stay in step; if you change
 * one, change the other.
 *
 * Exposed as window.LectioCleanup so app.js can bind the pane button without
 * this file having to know how the pane is wired.
 */
(function () {
  'use strict';

  var TEXT_PREFIX_LEN = 160;
  var state = null;

  function normalizeText(value) {
    return (value || '').replace(/\s+/g, ' ').trim().slice(0, TEXT_PREFIX_LEN);
  }

  // Mirrors _normalize_src in services/content_edits.py: unwrap the /api/img
  // proxy and keep the last path segment, so a proxied render URL fingerprints
  // the same as the source URL that is actually stored.
  function normalizeSrc(value) {
    if (!value) return '';
    var src = String(value).trim();
    if (src.indexOf('/api/img?') === 0) {
      var q = src.split('?')[1] || '';
      var parts = q.split('&');
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].indexOf('u=') === 0) {
          src = decodeURIComponent(parts[i].slice(2));
          break;
        }
      }
    }
    var path = src;
    try {
      path = new URL(src, window.location.origin).pathname;
    } catch (_e) { /* relative or malformed: fall back to the raw value */ }
    var segments = path.split('/');
    return segments[segments.length - 1];
  }

  function elementChildren(node) {
    var out = [];
    for (var i = 0; i < node.children.length; i++) out.push(node.children[i]);
    return out;
  }

  function fingerprint(node) {
    var classes = [];
    node.classList.forEach(function (c) {
      // Our own mode-only markers are not part of the document.
      if (c.indexOf('cleanup-') !== 0) classes.push(c);
    });
    classes.sort();
    return {
      tag: node.tagName.toLowerCase(),
      id: (node.id || '').trim(),
      cls: classes,
      text: normalizeText(node.textContent),
      kids: node.children.length,
      src: normalizeSrc(node.getAttribute('src')),
    };
  }

  // Path is derived against the DOM *as it stands right now* — the server
  // replays ops in the same order against a tree it mutates identically.
  function pathFor(root, node) {
    var path = [];
    var current = node;
    while (current && current !== root) {
      var parent = current.parentElement;
      if (!parent) return null;
      path.unshift(elementChildren(parent).indexOf(current));
      current = parent;
    }
    return current === root && path.length ? path : null;
  }

  function clearHighlight() {
    if (state && state.selected) state.selected.classList.remove('cleanup-selected');
  }

  function select(node) {
    if (!state || !node || node === state.root || !state.root.contains(node)) return;
    clearHighlight();
    state.selected = node;
    node.classList.add('cleanup-selected');
    updateStatus();
  }

  function describe(node) {
    if (!node) return 'nothing selected';
    var label = node.tagName.toLowerCase();
    if (node.id) label += '#' + node.id;
    var cls = [];
    node.classList.forEach(function (c) { if (c.indexOf('cleanup-') !== 0) cls.push(c); });
    if (cls.length) label += '.' + cls.slice(0, 2).join('.');
    return label;
  }

  function updateStatus() {
    if (!state) return;
    state.statusEl.textContent = describe(state.selected)
      + ' — ' + state.ops.length + (state.ops.length === 1 ? ' edit' : ' edits');
    state.saveBtn.disabled = state.ops.length === 0;
  }

  function recordOp(op, node) {
    var path = pathFor(state.root, node);
    if (!path) return false;
    state.ops.push({ op: op, path: path, fp: fingerprint(node) });
    return true;
  }

  function removeSelected() {
    if (!state || !state.selected) return;
    var node = state.selected;
    if (!recordOp('remove', node)) return;
    state.undo.push({ kind: 'remove', node: node, parent: node.parentElement, next: node.nextSibling });
    clearHighlight();
    state.selected = null;
    node.remove();
    updateStatus();
  }

  function isolateSelected() {
    if (!state || !state.selected) return;
    var node = state.selected;
    if (!recordOp('isolate', node)) return;
    var previousChildren = [];
    for (var i = 0; i < state.root.childNodes.length; i++) previousChildren.push(state.root.childNodes[i]);
    state.undo.push({ kind: 'isolate', children: previousChildren });
    clearHighlight();
    state.selected = null;
    state.root.innerHTML = '';
    state.root.appendChild(node);
    updateStatus();
  }

  function undo() {
    if (!state || !state.undo.length) return;
    var last = state.undo.pop();
    state.ops.pop();
    clearHighlight();
    state.selected = null;
    if (last.kind === 'remove') {
      last.parent.insertBefore(last.node, last.next);
    } else {
      state.root.innerHTML = '';
      for (var i = 0; i < last.children.length; i++) state.root.appendChild(last.children[i]);
    }
    updateStatus();
  }

  function widen() {
    if (!state || !state.selected) return;
    var parent = state.selected.parentElement;
    if (parent && parent !== state.root) select(parent);
    else if (parent === state.root) { /* already at a top-level block */ }
  }

  function narrow() {
    if (!state || !state.selected) return;
    var child = state.selected.children[0];
    if (child) select(child);
  }

  function onMouseMove(event) {
    if (!state) return;
    var node = event.target;
    if (!(node instanceof Element) || !state.root.contains(node) || node === state.root) return;
    select(node);
  }

  // Armed mode is modal: no click anywhere else in the app does its normal
  // thing, or a stray click follows a link and loses the pending edits.
  function onClick(event) {
    if (!state) return;
    var target = event.target;
    if (state.bar.contains(target)) return;
    event.preventDefault();
    event.stopPropagation();
    if (target instanceof Element && target.closest('#entry-cleanup-button')) {
      cancel();
      return;
    }
    if (state.root.contains(target)) removeSelected();
  }

  function onKeyDown(event) {
    if (!state) return;
    var key = event.key;
    if ((event.ctrlKey || event.metaKey) && (key === 'z' || key === 'Z')) {
      event.preventDefault();
      undo();
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    var handled = true;
    switch (key) {
      case 'Escape': cancel(); break;
      case 'w': case 'W': widen(); break;
      case 'n': case 'N': narrow(); break;
      case 'r': case 'R': removeSelected(); break;
      case 'i': case 'I': isolateSelected(); break;
      case 'Enter': save(); break;
      default: handled = false;
    }
    if (handled) {
      event.preventDefault();
      // The pane's own shortcuts (j/k/v/w/…) must not also fire while armed.
      event.stopPropagation();
    }
  }

  function buildBar() {
    var bar = document.createElement('div');
    bar.className = 'cleanup-bar';
    bar.innerHTML =
      '<span class="cleanup-bar-title">Clean up</span>'
      + '<span class="cleanup-bar-status"></span>'
      + '<span class="cleanup-bar-hint">click/R remove · I isolate · W wider · N narrower · Ctrl+Z undo</span>'
      + '<button type="button" class="cleanup-bar-btn cleanup-bar-save" disabled>Save</button>'
      + '<button type="button" class="cleanup-bar-btn cleanup-bar-cancel">Cancel</button>';
    return bar;
  }

  function teardown() {
    if (!state) return;
    clearHighlight();
    state.root.classList.remove('cleanup-active');
    document.removeEventListener('mousemove', state.onMouseMove, true);
    document.removeEventListener('click', state.onClick, true);
    document.removeEventListener('keydown', state.onKeyDown, true);
    state.bar.remove();
    state = null;
  }

  function cancel() {
    // Everything so far was a live DOM mutation; reloading the pane is the
    // cheapest honest way back to the stored article.
    var dirty = state && state.ops.length > 0;
    teardown();
    if (dirty) window.location.reload();
  }

  async function save() {
    if (!state || !state.ops.length) return;
    var payload = new URLSearchParams({
      feed_url: state.feedUrl,
      entry_id: state.entryId,
      ops: JSON.stringify(state.ops),
    });
    state.saveBtn.disabled = true;
    state.saveBtn.textContent = 'Saving…';
    try {
      var resp = await fetch('/entries/content/clean', { method: 'POST', body: payload });
      var data = await resp.json();
      if (!data.ok) {
        window.alert(data.error || 'Could not save the cleanup.');
        state.saveBtn.disabled = false;
        state.saveBtn.textContent = 'Save';
        return;
      }
      if (data.unmatched && data.unmatched.length) {
        window.alert(data.applied + ' of ' + (data.applied + data.unmatched.length)
          + ' edits were saved. The rest matched nothing in the stored article —'
          + ' those elements are added when the page renders, so there is nothing to remove.');
      }
      teardown();
      window.location.reload();
    } catch (_e) {
      window.alert('Could not save the cleanup.');
      if (state) {
        state.saveBtn.disabled = false;
        state.saveBtn.textContent = 'Save';
      }
    }
  }

  function start(options) {
    if (state) { cancel(); return; }
    var root = document.querySelector('.entry-content');
    if (!root) {
      window.alert('This post has no article body to clean up.');
      return;
    }
    var bar = buildBar();
    document.body.appendChild(bar);
    state = {
      root: root,
      bar: bar,
      feedUrl: options.feedUrl,
      entryId: options.entryId,
      selected: null,
      ops: [],
      undo: [],
      statusEl: bar.querySelector('.cleanup-bar-status'),
      saveBtn: bar.querySelector('.cleanup-bar-save'),
      onMouseMove: onMouseMove,
      onClick: onClick,
      onKeyDown: onKeyDown,
    };
    root.classList.add('cleanup-active');
    bar.querySelector('.cleanup-bar-save').addEventListener('click', save);
    bar.querySelector('.cleanup-bar-cancel').addEventListener('click', cancel);
    // Capture phase throughout: the pane binds its own click and key handlers,
    // and while armed a click must delete a node rather than follow a link.
    document.addEventListener('mousemove', onMouseMove, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKeyDown, true);
    updateStatus();
  }

  async function revert(options) {
    if (!window.confirm('Restore this article as the feed served it? Your cleanup will be discarded.')) return;
    try {
      var payload = new URLSearchParams({ feed_url: options.feedUrl, entry_id: options.entryId });
      var resp = await fetch('/entries/content/revert', { method: 'POST', body: payload });
      var data = await resp.json();
      if (!data.ok) {
        window.alert(data.error || 'Could not revert the cleanup.');
        return;
      }
      window.location.reload();
    } catch (_e) {
      window.alert('Could not revert the cleanup.');
    }
  }

  // Delegated so the buttons keep working across pane swaps, which replace the
  // whole .pane-entry subtree and would drop any directly-bound handler.
  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!(target instanceof Element)) return;
    var startBtn = target.closest('#entry-cleanup-button');
    if (startBtn) {
      event.preventDefault();
      start({ feedUrl: startBtn.dataset.feedUrl, entryId: startBtn.dataset.entryId });
      return;
    }
    var revertBtn = target.closest('#entry-cleanup-revert-button');
    if (revertBtn) {
      event.preventDefault();
      revert({ feedUrl: revertBtn.dataset.feedUrl, entryId: revertBtn.dataset.entryId });
    }
  });

  window.LectioCleanup = { start: start, revert: revert, active: function () { return state !== null; } };
})();
