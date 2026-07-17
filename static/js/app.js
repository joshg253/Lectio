// Lectio app script. Extracted from templates/index.html so browsers cache
// it across navigations (it was ~580KB of inline JS re-shipped with every
// full page render). Template-derived values arrive via the window.* config
// object in the document <head>; this file must stay Jinja-free.
    // Only web-ish schemes may reach an href/src. Entry and feed URLs are
    // feed-controlled: a `javascript:` URL assigned to an anchor's href would
    // run in our origin the moment the user clicks it. The server already
    // empties unsafe links (services/html_sanitize.safe_link_url); this is
    // defense in depth for values read back out of the DOM, and mirrors that
    // allowlist (SAFE_LINK_SCHEMES; relative resolves same-origin). The two
    // lists are intentionally independent — this guard must hold even if the
    // server's is wrong — so a Python test asserts they never drift apart.
    const _SAFE_URL_PROTOCOLS = ['http:', 'https:', 'mailto:', 'tel:'];
    function safeHttpUrl(value) {
      if (!value) return '';
      try {
        const parsed = new URL(value, window.location.origin);
        return _SAFE_URL_PROTOCOLS.includes(parsed.protocol) ? value : '';
      } catch (e) {
        return '';
      }
    }

    const themeToggle = null; // replaced by two-button picker in Settings
    const themeStylesheet = document.getElementById('theme-stylesheet');
    let appUnreadCount = null;
    let unreadSinceLastFocus = false;
    let lastFocusedUnreadCount = 0;

    function measureAndSetTileHeight() {
      // Find a 2-line title tile and measure its height to set thumbnail height dynamically
      const items = Array.from(document.querySelectorAll('.post-item'));
      
      for (const item of items) {
        const title = item.querySelector('.post-title');
        if (!title) continue;
        
        const titleStyle = window.getComputedStyle(title);
        const lineHeight = parseFloat(titleStyle.lineHeight);
        const titleRect = title.getBoundingClientRect();
        const titleLines = Math.round(titleRect.height / lineHeight);
        
        // Found a 2-line tile, measure its card height
        if (titleLines === 2) {
          const card = item.querySelector('.post-item-card');
          const cardRect = card.getBoundingClientRect();
          const tileHeight = Math.ceil(cardRect.height);
          
          // Set CSS custom property
          document.documentElement.style.setProperty('--post-tile-height', `${tileHeight}px`);
          return;
        }
      }
    }

    const localTimeFormatterShort = new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
    });
    const localTimeFormatterShortWithYear = new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
    const localTimeFormatterLong = new Intl.DateTimeFormat(undefined, {
      weekday: 'short',
      month: 'long',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });

    function formatRelativeDate(date) {
      const now = new Date();
      const diffMs = now - date;
      const diffSecs = Math.floor(diffMs / 1000);
      const diffMins = Math.floor(diffSecs / 60);
      const diffHours = Math.floor(diffMins / 60);
      const diffDays = Math.floor(diffHours / 24);

      if (diffSecs < 60) {
        return 'now';
      }
      if (diffMins < 60) {
        return `${diffMins}m`;
      }
      if (diffHours < 24) {
        return `${diffHours}h`;
      }
      if (diffDays < 7) {
        return `${diffDays}d`;
      }
      
      const diffWeeks = Math.floor(diffDays / 7);
      if (diffWeeks < 4) {
        return `${diffWeeks}w`;
      }
      
      // Fall back to absolute date for older items
      const isCurrentYear = date.getFullYear() === now.getFullYear();
      return isCurrentYear
        ? localTimeFormatterShort.format(date)
        : localTimeFormatterShortWithYear.format(date);
    }

    function applyRelativeTimestamps(root = document) {
      for (const node of root.querySelectorAll('.js-local-time-relative')) {
        const rawRead = (node.getAttribute('data-read-iso') || '').trim();
        const readDisplay = (node.getAttribute('data-read-display') || '').trim();
        let raw = '';
        let mainDisplay = '';

        if ((window.CURRENT_SORT_BY || 'post') === 'received') {
          raw = (node.getAttribute('data-received-iso') || '').trim();
          mainDisplay = (node.getAttribute('data-received-display') || '').trim();
        } else {
          raw = (node.getAttribute('data-post-iso') || '').trim();
          mainDisplay = (node.getAttribute('data-post-display') || '').trim();
        }

        if (rawRead && readDisplay && window.location.search.includes('read_filter=history')) {
          raw = rawRead;
          mainDisplay = readDisplay;
        }

        if (!raw) {
          if (mainDisplay) node.title = mainDisplay;
          continue;
        }

        const date = new Date(raw);
        if (Number.isNaN(date.getTime())) continue;

        const relative = formatRelativeDate(date);
        node.textContent = relative;
        if (node instanceof HTMLTimeElement) node.dateTime = date.toISOString();

        // Tooltip shows the absolute formatted version of the same date being displayed
        node.title = mainDisplay || localTimeFormatterShortWithYear.format(date);
      }
    }

    function applyAbsoluteTimestamps(root = document) {
      for (const node of root.querySelectorAll('.js-local-time-absolute')) {
        const postIso = (node.getAttribute('data-post-iso') || '').trim();
        const receivedIso = (node.getAttribute('data-received-iso') || '').trim();
        const postDisplay = (node.getAttribute('data-post-display') || '').trim();
        const receivedDisplay = (node.getAttribute('data-received-display') || '').trim();

        if (!postIso) {
          continue;
        }
        
        const postDate = new Date(postIso);
        if (Number.isNaN(postDate.getTime())) {
          continue;
        }
        
        // Format the post date
        const useLong = node.getAttribute('data-time-format') === 'long';
        const now = new Date();
        const isCurrentYear = postDate.getFullYear() === now.getFullYear();
        const formatted = useLong
          ? localTimeFormatterLong.format(postDate)
          : isCurrentYear
            ? localTimeFormatterShort.format(postDate)
            : localTimeFormatterShortWithYear.format(postDate);
        
        node.textContent = formatted;
        if (node instanceof HTMLTimeElement) node.dateTime = postDate.toISOString();
        
        // Tooltip shows received date if available
        if (receivedDisplay) {
          node.title = useLong
            ? `Received: ${receivedDisplay}`
            : `Article: ${postDisplay || formatted} · Received: ${receivedDisplay}`;
        }
      }
    }

    function applyLocalTimestamps(root = document) {
      for (const node of root.querySelectorAll('.js-local-time')) {
        // pick which ISO to display based on CURRENT_SORT_BY or read history
        const rawRead = (node.getAttribute('data-read-iso') || '').trim();
        const readDisplay = (node.getAttribute('data-read-display') || '').trim();
        let raw = '';
        let altDisplay = '';
        let titlePrefix = '';
        if ((window.CURRENT_SORT_BY || 'post') === 'received') {
          raw = (node.getAttribute('data-received-iso') || '').trim();
          altDisplay = (node.getAttribute('data-post-display') || '').trim();
          titlePrefix = 'Post';
        } else {
          raw = (node.getAttribute('data-post-iso') || '').trim();
          altDisplay = (node.getAttribute('data-received-display') || '').trim();
          titlePrefix = 'Received';
        }
        // If showing history view with a read time, prefer that
        const inHistory = (node.getAttribute('data-read-display') || '').trim();
        if (inHistory && window.location.search.includes('read_filter=history')) {
          raw = rawRead;
          altDisplay = (node.getAttribute('data-post-display') || '').trim();
          titlePrefix = 'Read';
        }

        const formattedRaw = raw;
        if (!formattedRaw) {
          // nothing to show, skip formatting but set title if alt exists
          if (altDisplay) node.title = `${titlePrefix} ${altDisplay}`;
          continue;
        }
        const date = new Date(formattedRaw);
        if (!raw) {
          continue;
        }
        if (Number.isNaN(date.getTime())) {
          continue;
        }
        const now = new Date();
        const isCurrentYear = date.getFullYear() === now.getFullYear();
        const formatted = isCurrentYear
          ? localTimeFormatterShort.format(date)
          : localTimeFormatterShortWithYear.format(date);
        node.textContent = formatted;
        if (node instanceof HTMLTimeElement) node.dateTime = date.toISOString();
        // Tooltip should show the alternate date (if available)
        if (altDisplay) {
          node.title = `${titlePrefix} ${altDisplay}`;
        } else if (titlePrefix) {
          node.title = `${titlePrefix} ${formatted}`;
        }
      }
    }

    function getUnreadCountFromRootBadge() {
      // Aggregate unread lives on the feeds "All" row (tabs carry no counter).
      const rootCountNode = document.querySelector('.feeds-all-item .count');
      if (!rootCountNode) {
        return 0;
      }
      const raw = (rootCountNode.textContent || '').replace(/[^0-9]/g, '');
      const value = Number.parseInt(raw, 10);
      return Number.isFinite(value) ? value : 0;
    }

    function getUnreadCountFallback() {
      return Array.from(document.querySelectorAll('.post-item'))
        .filter((item) => item.getAttribute('data-post-read') === '0')
        .length;
    }

    function getUnreadCountForFavicon() {
      if (Number.isFinite(appUnreadCount)) {
        return Math.max(0, appUnreadCount);
      }
      const fromRoot = getUnreadCountFromRootBadge();
      if (fromRoot > 0) {
        return fromRoot;
      }
      return getUnreadCountFallback();
    }

    function syncUnreadAttentionState(unreadCount) {
      const isFocused = document.visibilityState === 'visible' && document.hasFocus();
      if (isFocused) {
        unreadSinceLastFocus = false;
        lastFocusedUnreadCount = unreadCount;
        return;
      }

      if (unreadCount > lastFocusedUnreadCount) {
        unreadSinceLastFocus = true;
      }
    }

    function drawFavicon(size, showAttentionDot) {
      const canvas = document.createElement('canvas');
      canvas.width = size;
      canvas.height = size;
      const ctx = canvas.getContext('2d');
      if (!ctx) {
        return '';
      }

      // Base tile with Lectio "L" glyph.
      ctx.fillStyle = '#1d3f66';
      ctx.fillRect(0, 0, size, size);
      ctx.fillStyle = '#f4f7fb';
      ctx.font = `800 ${Math.round(size * 0.92)}px Merriweather, Georgia, serif`;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText('L', Math.round(size * 0.02), Math.round(size * 0.54));

      if (showAttentionDot) {
        const radius = size * 0.22;
        const cx = size - radius + 0.5;
        const cy = radius - 0.5;
        ctx.fillStyle = '#d5332a';
        ctx.beginPath();
        ctx.arc(cx, cy, radius, 0, Math.PI * 2);
        ctx.fill();
        // Intentionally no stroke for a cleaner, simpler indicator.
      }

      return canvas.toDataURL('image/png');
    }

    function updateDynamicFavicon() {
      const unreadCount = getUnreadCountForFavicon();
      syncUnreadAttentionState(unreadCount);

      const icon16 = drawFavicon(16, unreadSinceLastFocus);
      const icon32 = drawFavicon(32, unreadSinceLastFocus);
      if (!icon16 || !icon32) {
        return;
      }

      let icon16Link = document.querySelector('link[data-dynamic-favicon="16"]');
      if (!icon16Link) {
        icon16Link = document.createElement('link');
        icon16Link.setAttribute('rel', 'icon');
        icon16Link.setAttribute('type', 'image/png');
        icon16Link.setAttribute('sizes', '16x16');
        icon16Link.setAttribute('data-dynamic-favicon', '16');
        document.head.appendChild(icon16Link);
      }
      icon16Link.setAttribute('href', icon16);

      let icon32Link = document.querySelector('link[data-dynamic-favicon="32"]');
      if (!icon32Link) {
        icon32Link = document.createElement('link');
        icon32Link.setAttribute('rel', 'icon');
        icon32Link.setAttribute('type', 'image/png');
        icon32Link.setAttribute('sizes', '32x32');
        icon32Link.setAttribute('data-dynamic-favicon', '32');
        document.head.appendChild(icon32Link);
      }
      icon32Link.setAttribute('href', icon32);
    }

    function applyTheme(theme) {
      window.localStorage.setItem('lectio-theme', theme);
      window.__lectioTheme = theme;
      document.documentElement.setAttribute('data-theme', theme);

      if (themeStylesheet) {
        // Must carry the asset-version query: /static is served immutable for a year,
        // so an unversioned href serves a stale cached theme (e.g. a dark.css from
        // before the math-invert rule), silently undoing theme CSS changes.
        themeStylesheet.href = `/static/themes/${theme}.css?v=${window.STATIC_ASSET_VERSION}`;
      }

      const btnDark = document.getElementById('theme-btn-dark');
      const btnLight = document.getElementById('theme-btn-light');
      if (btnDark) { btnDark.classList.toggle('sett-theme-btn--active', theme === 'dark'); btnDark.setAttribute('aria-pressed', String(theme === 'dark')); }
      if (btnLight) { btnLight.classList.toggle('sett-theme-btn--active', theme === 'light'); btnLight.setAttribute('aria-pressed', String(theme === 'light')); }
      // Mirror onto the avatar-menu theme row (Dark / Light / eInk).
      document.querySelectorAll('[data-menu-theme]').forEach((b) => {
        const on = b.getAttribute('data-menu-theme') === theme;
        b.classList.toggle('menu-theme-btn--active', on);
        b.setAttribute('aria-pressed', String(on));
      });

      updateDynamicFavicon();
    }

    function updateHamburgerFlyoutDirection() {
      for (const menu of document.querySelectorAll('.hamburger-menu')) {
        const summary = menu.querySelector('summary');
        const popover = menu.querySelector('.menu-popover');
        if (!summary || !popover) {
          continue;
        }

        const rect = summary.getBoundingClientRect();
        const summaryMid = rect.left + rect.width / 2;
        const side = summaryMid > window.innerWidth / 2 ? 'left' : 'right';

        popover.classList.remove('anchor-left', 'anchor-right');
        popover.classList.add(side === 'left' ? 'anchor-right' : 'anchor-left');

        popover.classList.remove('flyout-left', 'flyout-right');
        popover.classList.add(side === 'left' ? 'flyout-left' : 'flyout-right');

        const arrowGlyph = side === 'left' ? 'chevron_left' : 'chevron_right';
        for (const arrow of popover.querySelectorAll('.flyout-arrow')) {
          arrow.textContent = arrowGlyph;
        }
      }
    }

    applyTheme(window.__lectioTheme || 'dark');
    document.getElementById('theme-btn-dark')?.addEventListener('click', () => applyTheme('dark'));
    document.getElementById('theme-btn-light')?.addEventListener('click', () => applyTheme('light'));
    document.querySelectorAll('[data-menu-theme]').forEach((b) =>
      b.addEventListener('click', () => applyTheme(b.getAttribute('data-menu-theme'))));

    updateHamburgerFlyoutDirection();
    window.addEventListener('resize', updateHamburgerFlyoutDirection);

    // Position toolbar dropdowns using fixed positioning to escape .pane overflow:hidden

    appUnreadCount = getUnreadCountFromRootBadge();
    lastFocusedUnreadCount = getUnreadCountForFavicon();
    updateDynamicFavicon();

    window.addEventListener('focus', () => {
      unreadSinceLastFocus = false;
      lastFocusedUnreadCount = getUnreadCountForFavicon();
      updateDynamicFavicon();
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        unreadSinceLastFocus = false;
        lastFocusedUnreadCount = getUnreadCountForFavicon();
      } else {
        lastFocusedUnreadCount = getUnreadCountForFavicon();
      }
      updateDynamicFavicon();
    });

    document.addEventListener('click', (event) => {
      const trigger = event.target instanceof Element && event.target.closest('[data-toggle-panel]');
      if (!trigger) return;

      const panelId = trigger.getAttribute('data-toggle-panel');
      if (!panelId) return;

      const panel = document.getElementById(panelId);
      if (!panel) return;

      if (panel.classList.contains('action-modal')) {
        panel.removeAttribute('hidden');
      } else {
        const willShow = panel.hasAttribute('hidden');

        for (const otherPanel of document.querySelectorAll('.hidden-panel')) {
          otherPanel.setAttribute('hidden', '');
        }

        if (willShow) {
          panel.removeAttribute('hidden');
        }
      }

      const burger = trigger.closest('.hamburger-menu');
      if (burger) {
        burger.removeAttribute('open');
      }
    });

    function closeAddModal(modalId) {
      const modal = document.getElementById(modalId);
      if (modal) {
        modal.setAttribute('hidden', '');
      }
    }

    for (const btn of document.querySelectorAll('[data-close-modal]')) {
      btn.addEventListener('click', () => {
        closeAddModal(btn.getAttribute('data-close-modal'));
      });
    }

    for (const modal of document.querySelectorAll('#add-feed-modal, #save-article-modal, #global-note-modal, #email-article-modal, #settings-modal')) {
      // Use mousedown/mouseup pairing so that CSS-resize drag releases (where
      // mousedown was on the panel and mouseup lands on the backdrop) don't
      // accidentally dismiss the modal.  Only closes when BOTH events hit the
      // backdrop itself.
      let _backdropDown = false;
      modal.addEventListener('mousedown', (event) => {
        _backdropDown = event.target === modal;
      });
      modal.addEventListener('mouseup', (event) => {
        if (_backdropDown && event.target === modal) {
          modal.setAttribute('hidden', '');
        }
        _backdropDown = false;
      });
    }

    function formatBytes(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    async function loadStatsData() {
      const statIds = ['stat-feed-count', 'stat-folder-count', 'stat-entry-total', 'stat-entry-unread', 'stat-entry-read', 'stat-archive-summary', 'stat-thumb-summary', 'stat-img-cache-summary', 'stat-reader-db-size', 'stat-meta-db-size'];
      for (const id of statIds) {
        const el = document.getElementById(id);
        if (el) el.textContent = '…';
      }
      try {
        const resp = await fetch('/stats');
        if (!resp.ok) throw new Error('stats request failed');
        const data = await resp.json();
        document.getElementById('stat-feed-count').textContent = data.feed_count.toLocaleString();
        document.getElementById('stat-folder-count').textContent = data.folder_count.toLocaleString();
        document.getElementById('stat-entry-total').textContent = data.entry_total.toLocaleString();
        document.getElementById('stat-entry-unread').textContent = data.entry_unread.toLocaleString();
        document.getElementById('stat-entry-read').textContent = data.entry_read.toLocaleString();
        const archivedComplete = data.starred_archive_complete || 0;
        let archiveSummary = `${archivedComplete.toLocaleString()} (${formatBytes(data.starred_archive_db_bytes || 0)})`;
        const archiveExtras = [];
        if (data.starred_archive_pending) archiveExtras.push(`${data.starred_archive_pending.toLocaleString()} pending`);
        if (data.starred_archive_in_progress) archiveExtras.push(`${data.starred_archive_in_progress.toLocaleString()} in progress`);
        if (data.starred_archive_failed) archiveExtras.push(`${data.starred_archive_failed.toLocaleString()} failed`);
        if (data.starred_archive_pending_removal) archiveExtras.push(`${data.starred_archive_pending_removal.toLocaleString()} pending removal`);
        if (archiveExtras.length) archiveSummary += ` · ${archiveExtras.join(', ')}`;
        document.getElementById('stat-archive-summary').textContent = archiveSummary;
        const thumbCount = (data.thumb_count || 0).toLocaleString();
        document.getElementById('stat-thumb-summary').textContent = `${thumbCount} (${formatBytes(data.thumb_db_bytes || 0)})`;
        const imgCacheCount = (data.img_cache_count || 0).toLocaleString();
        document.getElementById('stat-img-cache-summary').textContent = `${imgCacheCount} (${formatBytes(data.img_cache_db_bytes || 0)})`;
        document.getElementById('stat-reader-db-size').textContent = formatBytes(data.reader_db_bytes);
        document.getElementById('stat-meta-db-size').textContent = formatBytes(data.meta_db_bytes);
      } catch (_e) {
        for (const id of statIds) {
          const el = document.getElementById(id);
          if (el) el.textContent = 'Error';
        }
      }
    }

    // Deferred sidebar feed lists: only the selected folder ships its rows
    // inline — every other folder has an empty <ul data-lazy-feeds="<id>">
    // whose rows are fetched on first expand (inlining all rows costs
    // megabytes at thousands of feeds). On window because callers live in
    // different top-level blocks.
    window._ensureTreeFeedsLoaded = async function (ul) {
      if (!ul) return;
      const fid = ul.getAttribute('data-lazy-feeds');
      if (!fid) return;
      ul.removeAttribute('data-lazy-feeds');
      ul.innerHTML = '<li class="tree-feed-item"><span class="feed-label">Loading…</span></li>';
      try {
        const resp = await fetch(`/tree/folder-feeds/${encodeURIComponent(fid)}`, { credentials: 'same-origin' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        ul.innerHTML = await resp.text();
        window._refreshUnreadFoldersOnly?.();
      } catch (err) {
        ul.setAttribute('data-lazy-feeds', fid); // retry on next expand
        ul.innerHTML = '<li class="tree-feed-item"><span class="feed-label">Failed to load — collapse and expand to retry.</span></li>';
        console.error('tree feeds load failed:', err);
      }
    };

    for (const toggle of document.querySelectorAll('.tree-toggle')) {
      const targetId = toggle.getAttribute('data-tree-target');
      if (!targetId) {
        continue;
      }

      const target = document.getElementById(targetId);
      if (!target) {
        continue;
      }

      if (target.querySelector('.feed-link.active')) {
        target.removeAttribute('hidden');
        toggle.classList.add('expanded');
        toggle.setAttribute('aria-expanded', 'true');
      }

      toggle.addEventListener('click', () => {
        const willExpand = target.hasAttribute('hidden');
        if (willExpand) {
          void window._ensureTreeFeedsLoaded(target);
          target.removeAttribute('hidden');
          toggle.classList.add('expanded');
          toggle.setAttribute('aria-expanded', 'true');
        } else {
          target.setAttribute('hidden', '');
          toggle.classList.remove('expanded');
          toggle.setAttribute('aria-expanded', 'false');
        }
      });
    }

    for (const folderLink of document.querySelectorAll('.child-item, .root-item')) {
      folderLink.addEventListener('click', (event) => {
        event.stopPropagation();
      });
    }

    const opmlInput = document.getElementById('opml-file-input');
    const opmlForm = document.getElementById('opml-import-form');
    opmlInput?.addEventListener('change', () => {
      if (opmlInput.files && opmlInput.files.length > 0) {
        opmlForm?.submit();
      }
    });

    const takeoutInput = document.getElementById('takeout-file-input');
    const takeoutForm = document.getElementById('takeout-import-form');
    takeoutInput?.addEventListener('change', () => {
      if (takeoutInput.files && takeoutInput.files.length > 0) {
        takeoutForm?.submit();
      }
    });

    const instapaperInput = document.getElementById('instapaper-file-input');
    const instapaperForm = document.getElementById('instapaper-import-form');
    instapaperInput?.addEventListener('change', () => {
      if (instapaperInput.files && instapaperInput.files.length > 0) {
        instapaperForm?.submit();
      }
    });

    document.getElementById('dedup-feeds-btn')?.addEventListener('click', async () => {
      const resp = await fetch('/feeds/duplicates');
      const data = await resp.json();
      const sameFolder = data.same_folder || [];
      const crossFolder = data.cross_folder || [];
      const upgradable = data.upgradable || [];
      const results = document.getElementById('dedup-inline-results');
      const intro = results.querySelector('.dedup-modal-intro');
      const list = results.querySelector('.dedup-modal-list');
      const crossSection = results.querySelector('.dedup-cross-section');
      const crossList = results.querySelector('.dedup-cross-list');
      const upgradeSection = results.querySelector('.dedup-upgrade-section');
      const upgradeList = results.querySelector('.dedup-upgrade-list');
      const okBtn = document.getElementById('dedup-modal-ok');

      results.hidden = false;
      const hasAnything = sameFolder.length > 0 || crossFolder.length > 0 || upgradable.length > 0;

      if (!hasAnything) {
        intro.textContent = 'No duplicate or upgradable feeds found.';
        list.innerHTML = '';
        crossSection.hidden = true;
        upgradeSection.hidden = true;
        okBtn.hidden = true;
      } else {
        okBtn.hidden = false;

        // Same-folder section
        if (sameFolder.length > 0) {
          intro.textContent = `Found ${sameFolder.length} duplicate(s) in the same folder — the slash variant will be removed automatically:`;
          const byFolder = {};
          sameFolder.forEach(d => {
            if (!byFolder[d.folder_id]) byFolder[d.folder_id] = { name: d.folder_name, items: [] };
            byFolder[d.folder_id].items.push(d);
          });
          list.innerHTML = Object.values(byFolder).map(({ name, items }) =>
            `<div class="dedup-folder-group"><span class="dedup-folder-label">${name}</span>` +
            items.map(d =>
              `<div class="dedup-pair">` +
              `<div class="dedup-pair-row"><span class="dedup-tag keep-tag">keep</span><span class="dedup-url">${d.keep}</span></div>` +
              `<div class="dedup-pair-row"><span class="dedup-tag remove-tag">remove</span><span class="dedup-url">${d.remove}</span></div>` +
              `</div>`
            ).join('') + `</div>`
          ).join('');
        } else {
          intro.textContent = '';
          list.innerHTML = '';
        }

        // Cross-folder section
        if (crossFolder.length > 0) {
          crossSection.hidden = false;
          const crossIntro = crossSection.querySelector('.dedup-cross-intro');
          crossIntro.textContent = sameFolder.length > 0
            ? `Also found across different folders — choose which folder(s) to keep each feed in:`
            : `Found ${crossFolder.length} duplicate feed(s) across different folders — choose which folder(s) to keep each feed in:`;
          crossList.innerHTML = crossFolder.map((d, i) => {
            const checkboxes = d.all_folders.map(f =>
              `<label class="dedup-folder-check"><input type="checkbox" name="cross_${i}_folder" value="${f.id}" checked> ${f.name}</label>`
            ).join('');
            return `<div class="dedup-cross-pair" data-index="${i}" data-keep="${d.keep}" data-remove="${d.remove}">` +
              `<div class="dedup-pair">` +
              `<div class="dedup-pair-row"><span class="dedup-tag keep-tag">keep</span><span class="dedup-url">${d.keep}</span></div>` +
              `<div class="dedup-pair-row"><span class="dedup-tag remove-tag">remove</span><span class="dedup-url">${d.remove}</span></div>` +
              `</div>` +
              `<div class="dedup-folder-checks">${checkboxes}</div>` +
              `</div>`;
          }).join('');
        } else {
          crossSection.hidden = true;
        }

        // Upgrade section
        if (upgradable.length > 0) {
          upgradeSection.hidden = false;
          const upgradeIntro = upgradeSection.querySelector('.dedup-upgrade-intro');
          upgradeIntro.textContent = `Found ${upgradable.length} feed(s) using an RSS format URL — check to upgrade to Atom:`;
          upgradeList.innerHTML = upgradable.map((d, i) =>
            `<label class="dedup-upgrade-item"><input type="checkbox" class="dedup-upgrade-check" data-current="${d.current}" data-upgrade-to="${d.upgrade_to}" checked>` +
            `<div class="dedup-pair">` +
            `<div class="dedup-pair-row"><span class="dedup-tag remove-tag">rss</span><span class="dedup-url">${d.current}</span></div>` +
            `<div class="dedup-pair-row"><span class="dedup-tag keep-tag">atom</span><span class="dedup-url">${d.upgrade_to}</span></div>` +
            `</div></label>`
          ).join('');
        } else {
          upgradeSection.hidden = true;
        }

        // Update OK button label and rescue checkbox visibility
        const hasDedup = sameFolder.length > 0 || crossFolder.length > 0;
        const hasUpgrade = upgradable.length > 0;
        okBtn.textContent = hasDedup && hasUpgrade ? 'Remove duplicates & upgrade' : hasUpgrade ? 'Upgrade feeds' : 'Remove duplicates';
        const rescueLabel = document.getElementById('dedup-rescue-label');
        if (rescueLabel) rescueLabel.hidden = !hasDedup;
      }
    });

    document.getElementById('dedup-modal-ok')?.addEventListener('click', async () => {
      const results = document.getElementById('dedup-inline-results');
      // Collect cross-folder choices
      const crossChoices = [];
      for (const pair of results.querySelectorAll('.dedup-cross-pair')) {
        const keep = pair.dataset.keep;
        const remove = pair.dataset.remove;
        const folderIds = [...pair.querySelectorAll('input[type="checkbox"]:checked')].map(cb => parseInt(cb.value));
        crossChoices.push({ keep, remove, folder_ids: folderIds });
      }
      // Collect upgrade choices (only checked ones)
      const upgradeChoices = [];
      for (const cb of results.querySelectorAll('.dedup-upgrade-check:checked')) {
        upgradeChoices.push({ current: cb.dataset.current, upgrade_to: cb.dataset.upgradeTo });
      }
      const rescueUnread = document.getElementById('dedup-rescue-unread')?.checked ?? false;
      results.hidden = true;
      const dedupeResp = await fetch('/feeds/deduplicate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cross_folder_choices: crossChoices, upgrade_choices: upgradeChoices, rescue_unread: rescueUnread }),
      });
      const dedupeData = await dedupeResp.json();
      const parts = [];
      if (dedupeData.count > 0) parts.push(`Removed ${dedupeData.count} duplicate feed(s)`);
      if (dedupeData.rescued_count > 0) parts.push(`rescued ${dedupeData.rescued_count} unread post(s)`);
      if (dedupeData.upgraded_count > 0) parts.push(`upgraded ${dedupeData.upgraded_count} feed(s) to Atom`);
      alert(parts.length ? parts.join(', ') + '.' : 'Nothing to do.');
      window.location.reload();
    });

    const _mfEscape = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // ── Saved Articles duplicate scan ──────────────────────────────────────
    const SAVED_DEDUP_GROUP_CAP = 200;

    const savedDedupGroupHtml = (g, preselect) => {
      const rows = g.entries.map((e, i) => {
        const keeper = preselect && i === 0;
        const badges =
          `<span class="saved-dedup-badge${e.read ? '' : ' saved-dedup-badge--unread'}">${e.read ? 'read' : 'unread'}</span>` +
          (e.has_content ? '' : '<span class="saved-dedup-badge">no content</span>');
        const date = e.published ? `<span class="saved-dedup-date">${_mfEscape(String(e.published).slice(0, 10))}</span>` : '';
        return `<label class="dedup-pair-row saved-dedup-row">` +
          `<input type="checkbox" class="saved-dedup-check" data-entry-id="${_mfEscape(e.entry_id)}"` +
          ` data-has-content="${e.has_content ? '1' : '0'}"${preselect && !keeper ? ' checked' : ''}>` +
          `<span class="dedup-tag${keeper ? ' keep-tag' : ''}">${keeper ? 'keep' : ''}</span>` +
          `<span class="saved-dedup-main"><span class="saved-dedup-title">${_mfEscape(e.title || e.link)}</span> <span class="saved-dedup-row-badges">${badges}</span>${date}` +
          `<br><a class="dedup-url" href="${_mfEscape(e.link)}" target="_blank" rel="noopener noreferrer">${_mfEscape(e.link)}</a></span>` +
          `</label>`;
      }).join('');
      const reasons = (g.reasons || []).join(', ');
      return `<div class="dedup-pair saved-dedup-group">` +
        `<div class="saved-dedup-group-head">` +
        `<span class="saved-dedup-reasons">${_mfEscape(reasons)}</span>` +
        `<span class="saved-dedup-group-btns">` +
        `<button type="button" class="saved-dedup-check-urls-btn" title="Probe each copy's URL — dead links are flagged and the kept copy switches to a live one">Check URLs</button>` +
        `<button type="button" class="saved-dedup-compare-btn" title="Show the stored text of each copy side by side">Compare</button>` +
        `</span></div>` +
        rows + `</div>`;
    };

    const _sdHost = (link) => { try { return new URL(link).hostname; } catch (_e) { return link; } };

    const _sdUrlBadge = (r) => {
      const b = document.createElement('span');
      b.className = 'saved-dedup-badge saved-dedup-url-badge';
      if (r.dead) {
        b.classList.add('saved-dedup-badge--dead');
        b.textContent = `dead (${r.status})`;
      } else if (r.alive) {
        b.classList.add('saved-dedup-badge--alive');
        b.textContent = r.status === 200 ? 'alive' : `alive (${r.status})`;
      } else {
        b.textContent = r.status ? `HTTP ${r.status}` : (r.error || 'unreachable');
        b.title = 'Inconclusive — bot-wall or network hiccup, not proof the page is gone';
      }
      return b;
    };

    // Flip the group's selection to keep a live copy — only when every
    // currently-kept row turned out dead, and never at the cost of deleting
    // the only copy with stored content.
    const _sdFlipKeeper = (group, byId) => {
      if (!group.closest('.saved-dedup-confirmed-list')) return;
      const info = [...group.querySelectorAll('.saved-dedup-row')].map(row => {
        const cb = row.querySelector('.saved-dedup-check');
        return { row, cb, r: byId.get(cb.dataset.entryId), hasContent: cb.dataset.hasContent === '1' };
      });
      const kept = info.filter(i => !i.cb.checked);
      if (!kept.length || !kept.every(i => i.r && i.r.dead)) return;
      const candidate = info.find(i => i.r && i.r.alive);  // rows are in keep-priority order
      if (!candidate) return;
      if (!candidate.hasContent && info.some(i => i.hasContent)) return;  // human call
      for (const i of info) {
        i.cb.checked = i !== candidate && !!(i.r && (i.r.alive || i.r.dead));
        const tag = i.row.querySelector('.dedup-tag');
        tag.textContent = i === candidate ? 'keep' : '';
        tag.classList.toggle('keep-tag', i === candidate);
      }
    };

    const _sdCheckGroupUrls = async (group) => {
      const ids = [...group.querySelectorAll('.saved-dedup-check')].map(cb => cb.dataset.entryId);
      const resp = await fetch('/saved/duplicates/check-urls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entry_ids: ids }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const byId = new Map((data.results || []).map(r => [r.entry_id, r]));
      for (const row of group.querySelectorAll('.saved-dedup-row')) {
        const cb = row.querySelector('.saved-dedup-check');
        const r = byId.get(cb.dataset.entryId);
        row.querySelector('.saved-dedup-url-badge')?.remove();
        if (r) row.querySelector('.saved-dedup-row-badges').appendChild(_sdUrlBadge(r));
      }
      // Two copies redirecting to the same final URL = proof of a dupe.
      const finals = (data.results || []).filter(r => r.alive && r.final_url).map(r => r.final_url.replace(/\/$/, ''));
      if (finals.length > 1 && new Set(finals).size < finals.length && !group.querySelector('.saved-dedup-same-dest')) {
        const s = document.createElement('span');
        s.className = 'saved-dedup-badge saved-dedup-badge--alive saved-dedup-same-dest';
        s.textContent = 'same destination';
        s.title = 'These URLs redirect to the same page';
        group.querySelector('.saved-dedup-reasons').after(s);
      }
      _sdFlipKeeper(group, byId);
    };

    document.getElementById('saved-dedup-results')?.addEventListener('click', async (ev) => {
      const checkBtn = ev.target.closest('.saved-dedup-check-urls-btn');
      if (checkBtn) {
        const group = checkBtn.closest('.saved-dedup-group');
        checkBtn.disabled = true;
        checkBtn.textContent = 'Checking…';
        try {
          await _sdCheckGroupUrls(group);
          checkBtn.textContent = 'Re-check';
        } catch (err) {
          checkBtn.textContent = 'Check URLs';
          alert('URL check failed: ' + err);
        }
        checkBtn.disabled = false;
        return;
      }
      const btn = ev.target.closest('.saved-dedup-compare-btn');
      if (!btn) return;
      const group = btn.closest('.saved-dedup-group');
      const existing = group.querySelector('.saved-dedup-compare');
      if (existing) { existing.remove(); btn.textContent = 'Compare'; return; }
      const ids = [...group.querySelectorAll('.saved-dedup-check')].map(cb => cb.dataset.entryId);
      btn.disabled = true;
      let data;
      try {
        const resp = await fetch('/saved/duplicates/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entry_ids: ids }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        data = await resp.json();
      } catch (err) {
        btn.disabled = false;
        alert('Compare failed: ' + err);
        return;
      }
      btn.disabled = false;
      btn.textContent = 'Hide';
      const pane = document.createElement('div');
      pane.className = 'saved-dedup-compare';
      pane.innerHTML = (data.previews || []).map(p => {
        const truncated = p.chars > p.text.length;
        return `<div class="saved-dedup-compare-col">` +
          `<div class="saved-dedup-compare-head">${_mfEscape(_sdHost(p.link))}` +
          `${p.published ? ' · ' + _mfEscape(String(p.published).slice(0, 10)) : ''} · ${p.words} words</div>` +
          `<div class="saved-dedup-compare-text">${p.text ? _mfEscape(p.text) + (truncated ? '…' : '') : '<em>no stored content</em>'}</div>` +
          `</div>`;
      }).join('');
      group.appendChild(pane);
    });

    const savedDedupListHtml = (groups, preselect) =>
      groups.slice(0, SAVED_DEDUP_GROUP_CAP).map(g => savedDedupGroupHtml(g, preselect)).join('') +
      (groups.length > SAVED_DEDUP_GROUP_CAP
        ? `<p class="muted">Showing the first ${SAVED_DEDUP_GROUP_CAP} of ${groups.length} groups — re-run the scan after deleting these.</p>`
        : '');

    document.getElementById('saved-dedup-btn')?.addEventListener('click', async () => {
      const results = document.getElementById('saved-dedup-results');
      const intro = results.querySelector('.saved-dedup-intro');
      const confirmedList = results.querySelector('.saved-dedup-confirmed-list');
      const possibleSection = results.querySelector('.saved-dedup-possible-section');
      const possibleList = results.querySelector('.saved-dedup-possible-list');
      const okBtn = document.getElementById('saved-dedup-ok');
      let data;
      try {
        const resp = await fetch('/saved/duplicates');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        data = await resp.json();
      } catch (err) {
        alert('Saved duplicate scan failed: ' + err);
        return;
      }
      const confirmed = data.confirmed || [];
      const possible = data.possible || [];
      results.hidden = false;
      const checkAllBtn = document.getElementById('saved-dedup-checkall');

      if (confirmed.length === 0 && possible.length === 0) {
        intro.textContent = `No duplicate saved articles found (${data.scanned} scanned).`;
        confirmedList.innerHTML = '';
        possibleSection.hidden = true;
        okBtn.hidden = true;
        if (checkAllBtn) checkAllBtn.hidden = true;
        return;
      }
      okBtn.hidden = false;
      if (checkAllBtn) checkAllBtn.hidden = false;
      if (confirmed.length > 0) {
        intro.textContent = `Found ${confirmed.length} duplicate group(s) among ${data.scanned} saved articles — extra copies are preselected, keeping the copy with content, preferring https, then oldest:`;
        confirmedList.innerHTML = savedDedupListHtml(confirmed, true);
      } else {
        intro.textContent = `No confirmed duplicates among ${data.scanned} saved articles.`;
        confirmedList.innerHTML = '';
      }
      if (possible.length > 0) {
        possibleSection.hidden = false;
        possibleSection.querySelector('.saved-dedup-possible-intro').textContent =
          `${possible.length} possible duplicate group(s) — same title or same extracted content under different URLs:`;
        possibleList.innerHTML = savedDedupListHtml(possible, false);
      } else {
        possibleSection.hidden = true;
      }
    });

    let _sdCheckAllRunning = false;
    let _sdCheckAllStop = false;
    document.getElementById('saved-dedup-checkall')?.addEventListener('click', async (ev) => {
      const btn = ev.currentTarget;
      if (_sdCheckAllRunning) { _sdCheckAllStop = true; return; }
      const groups = [...document.getElementById('saved-dedup-results').querySelectorAll('.saved-dedup-group')];
      _sdCheckAllRunning = true;
      _sdCheckAllStop = false;
      let failures = 0;
      for (let i = 0; i < groups.length; i++) {
        if (_sdCheckAllStop) break;
        btn.textContent = `Checking ${i + 1}/${groups.length}… (click to stop)`;
        groups[i].scrollIntoView({ block: 'nearest' });
        try {
          await _sdCheckGroupUrls(groups[i]);
        } catch (_err) {
          if (++failures >= 3) { alert('URL checking keeps failing — stopped.'); break; }
        }
        // Pacing between groups — stay a polite client even over 200+ groups.
        await new Promise(r => setTimeout(r, 400));
      }
      _sdCheckAllRunning = false;
      btn.textContent = 'Check all URLs';
    });

    document.getElementById('saved-dedup-ok')?.addEventListener('click', async () => {
      const results = document.getElementById('saved-dedup-results');
      const ids = [...results.querySelectorAll('.saved-dedup-check:checked')].map(cb => cb.dataset.entryId);
      if (ids.length === 0) { alert('Nothing selected.'); return; }
      if (!confirm(`Permanently delete ${ids.length} saved article(s)? This cannot be undone.`)) return;
      let data;
      try {
        const resp = await fetch('/saved/deduplicate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ entry_ids: ids }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        data = await resp.json();
      } catch (err) {
        alert('Delete failed: ' + err);
        return;
      }
      alert(data.errors
        ? `Deleted ${data.deleted} — ${data.errors} failed, see server logs.`
        : `Deleted ${data.deleted} duplicate saved article(s).`);
      results.hidden = true;
      window.location.reload();
    });

    document.getElementById('multi-folder-btn')?.addEventListener('click', async () => {
      const results = document.getElementById('multi-folder-results');
      const intro = results?.querySelector('.multi-folder-intro');
      const list = results?.querySelector('.multi-folder-list');
      const okBtn = document.getElementById('multi-folder-ok');
      if (!results || !intro || !list || !okBtn) {
        console.error('Multi-folder UI elements are missing');
        return;
      }

      let feeds;
      try {
        const resp = await fetch('/feeds/multi-folder');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        feeds = data?.feeds || [];
      } catch (err) {
        console.error('Failed to scan for multi-folder feeds', err);
        results.hidden = false;
        intro.textContent = 'Could not scan for multi-folder feeds — please try again.';
        list.innerHTML = '';
        okBtn.hidden = true;
        return;
      }

      results.hidden = false;
      if (feeds.length === 0) {
        intro.textContent = 'No feeds are in more than one folder.';
        list.innerHTML = '';
        okBtn.hidden = true;
        return;
      }
      okBtn.hidden = false;
      intro.textContent = `Found ${feeds.length} feed(s) in more than one folder — choose the single folder to keep each in:`;
      list.innerHTML = feeds.map((f, i) => {
        const radios = f.folders.map((fld, j) =>
          `<label class="dedup-folder-check"><input type="radio" name="mf_${i}" value="${fld.id}"${j === 0 ? ' checked' : ''}> ${_mfEscape(fld.name)}</label>`
        ).join('');
        return `<div class="dedup-cross-pair" data-index="${i}" data-feed-url="${_mfEscape(f.feed_url)}">` +
          `<div class="dedup-pair"><div class="dedup-pair-row"><span class="dedup-url">${_mfEscape(f.title)}</span></div></div>` +
          `<div class="dedup-folder-checks">${radios}</div>` +
          `</div>`;
      }).join('');
    });

    document.getElementById('multi-folder-ok')?.addEventListener('click', async () => {
      const results = document.getElementById('multi-folder-results');
      const intro = results?.querySelector('.multi-folder-intro');
      if (!results) return;
      const choices = [];
      for (const pair of results.querySelectorAll('.dedup-cross-pair')) {
        const feedUrl = pair.dataset.feedUrl;
        const checked = pair.querySelector('input[type="radio"]:checked');
        if (feedUrl && checked) {
          choices.push({ feed_url: feedUrl, folder_id: parseInt(checked.value) });
        }
      }
      let respData;
      try {
        const resp = await fetch('/feeds/multi-folder/resolve', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ choices }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        respData = await resp.json();
      } catch (err) {
        // Keep the UI visible so the user can retry rather than losing their picks.
        console.error('Failed to resolve multi-folder feeds', err);
        if (intro) intro.textContent = 'Could not apply changes — please try again.';
        return;
      }
      results.hidden = true;
      alert(respData.resolved > 0 ? `Collapsed ${respData.resolved} feed(s) to a single folder.` : 'Nothing to do.');
      window.location.reload();
    });

    const toast = document.getElementById('toast-message');
    let toastFadeTimeoutId = null;
    let toastRemoveTimeoutId = null;

    function scheduleToastFade(nextToast, visibleMs) {
      if (toastFadeTimeoutId) {
        window.clearTimeout(toastFadeTimeoutId);
      }
      if (toastRemoveTimeoutId) {
        window.clearTimeout(toastRemoveTimeoutId);
      }

      toastFadeTimeoutId = window.setTimeout(() => {
        nextToast.classList.add('fade-out');
        toastRemoveTimeoutId = window.setTimeout(() => nextToast.remove(), 500);
      }, visibleMs);
    }

    function showToastMessage(message) {
      if (!message) {
        return;
      }

      const existingToast = document.getElementById('toast-message');
      if (existingToast) {
        existingToast.remove();
      }

      const nextToast = document.createElement('div');
      nextToast.id = 'toast-message';
      nextToast.className = 'toast-message';
      nextToast.textContent = message;
      document.body.appendChild(nextToast);

      scheduleToastFade(nextToast, 3800);
    }

    async function copyTextToClipboard(text) {
      if (!text) {
        return false;
      }

      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch (_clipboardError) {
        // Fall back to legacy copy path.
      }

      try {
        const scratch = document.createElement('textarea');
        scratch.value = text;
        scratch.setAttribute('readonly', '');
        scratch.style.position = 'fixed';
        scratch.style.opacity = '0';
        scratch.style.pointerEvents = 'none';
        document.body.appendChild(scratch);
        scratch.focus();
        scratch.select();
        scratch.setSelectionRange(0, scratch.value.length);
        const copied = document.execCommand('copy');
        scratch.remove();
        return copied;
      } catch (_legacyCopyError) {
        return false;
      }
    }

    if (toast) {
      scheduleToastFade(toast, 3200);
    }

    const earlyGlobalNoteForm = document.getElementById('global-note-form');
    const earlyGlobalNoteModal = document.getElementById('global-note-modal');
    if (earlyGlobalNoteForm instanceof HTMLFormElement) {
      earlyGlobalNoteForm.addEventListener('submit', async (event) => {
        event.preventDefault();

        const formData = new FormData(earlyGlobalNoteForm);
        const noteTextField = document.getElementById('global-note-text');
        const previousNoteText = noteTextField instanceof HTMLTextAreaElement ? noteTextField.value : '';

        // Treat note save as an account-level scratchpad action, independent of
        // current folder/feed/view state: close immediately and save in background.
        if (earlyGlobalNoteModal) {
          earlyGlobalNoteModal.setAttribute('hidden', '');
        }

        try {
          const response = await fetch(earlyGlobalNoteForm.action, {
            method: 'POST',
            body: formData,
            credentials: 'same-origin',
            headers: {
              'X-Requested-With': 'lectio-global-note-save',
            },
          });
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
          }

          const data = await response.json();
          if (!data || data.ok !== true) {
            throw new Error('Failed to save note.');
          }
          showToastMessage(data.message || 'Note saved.');
        } catch (_error) {
          if (noteTextField instanceof HTMLTextAreaElement) {
            noteTextField.value = previousNoteText;
          }
          showToastMessage('Could not save note.');
          if (earlyGlobalNoteModal) {
            earlyGlobalNoteModal.removeAttribute('hidden');
          }
        }
      });
    }

    document.addEventListener('click', async (event) => {
      const ipBtn = event.target instanceof Element && event.target.closest('[data-instapaper-feed-url]');
      if (ipBtn) {
        ipBtn.disabled = true;
        try {
          const fd = new FormData();
          fd.append('feed_url', ipBtn.getAttribute('data-instapaper-feed-url') || '');
          fd.append('entry_id', ipBtn.getAttribute('data-instapaper-entry-id') || '');
          const resp = await fetch('/entries/instapaper', { method: 'POST', body: fd, credentials: 'same-origin' });
          const data = await resp.json();
          showToastMessage(data.ok ? 'Saved to Instapaper' : (data.error || 'Instapaper save failed'));
        } catch (e) {
          showToastMessage('Instapaper save failed');
        } finally {
          ipBtn.disabled = false;
        }
        return;
      }
      const pinBtn = event.target instanceof Element && event.target.closest('[data-pinterest-feed-url]');
      if (pinBtn) {
        event.preventDefault();
        await openPinterestPicker(pinBtn);
        return;
      }
      const quireBtn = event.target instanceof Element && event.target.closest('[data-quire-feed-url]');
      if (quireBtn) {
        event.preventDefault();
        // Plain click adds straight to the default project; right-click opens the
        // picker to choose a different one (see the contextmenu handler below).
        await _quireQuickAdd(quireBtn);
        return;
      }
      const redditBtn = event.target instanceof Element && event.target.closest('[data-reddit-feed-url]');
      if (redditBtn) {
        event.preventDefault();
        await openRedditSubmitModal(redditBtn);
        return;
      }
    });

    // Right-click the Quire button to choose a destination project instead of
    // adding to the default one. Capture phase so it beats the share-menu close.
    document.addEventListener('contextmenu', async (event) => {
      const quireBtn = event.target instanceof Element && event.target.closest('[data-quire-feed-url]');
      if (!quireBtn) return;
      event.preventDefault();
      await openQuirePicker(quireBtn);
    }, true);

    // Position a share-target popup (already appended to <body>) just below the
    // entry share button, clamped into the viewport. The share-menu item that
    // triggered it is hidden by the capture-phase close by the time we run, so its
    // own rect is empty — anchor to the still-visible share button instead, and
    // measure the menu's own width rather than assume a fixed one.
    function positionShareTargetMenu(menu, fallbackBtn) {
      const anchor = document.getElementById('entry-share-btn') || fallbackBtn;
      const r = anchor.getBoundingClientRect();
      const width = menu.offsetWidth || 200;
      const maxLeft = window.scrollX + window.innerWidth - width - 8;
      const left = Math.max(window.scrollX + 8, Math.min(Math.round(r.left + window.scrollX), maxLeft));
      menu.style.top = `${Math.round(r.bottom + window.scrollY + 4)}px`;
      menu.style.left = `${left}px`;
    }

    // --- Pinterest "Pin to board" picker -----------------------------------
    let _pinterestBoardsCache = null;   // null = not fetched; {connected, boards, error}
    async function _pinFetchBoards() {
      if (_pinterestBoardsCache) return _pinterestBoardsCache;
      try {
        const r = await fetch('/api/pinterest/boards', { credentials: 'same-origin' });
        _pinterestBoardsCache = (await r.json().catch(() => ({}))) || { connected: false, boards: [] };
      } catch (e) {
        _pinterestBoardsCache = { connected: false, boards: [], error: 'network' };
      }
      return _pinterestBoardsCache;
    }

    function _closePinMenu() {
      document.querySelector('.lectio-pin-menu')?.remove();
      document.removeEventListener('click', _pinOutsideClick, true);
    }
    function _pinOutsideClick(e) {
      if (!(e.target instanceof Element) || !e.target.closest('.lectio-pin-menu, [data-pinterest-feed-url]')) _closePinMenu();
    }

    async function _doPin(feedUrl, entryId, boardId, boardName) {
      const r = await fetch('/api/pinterest/pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ feed_url: feedUrl, entry_id: entryId, board_id: boardId }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      showToastMessage(`Pinned to ${boardName}.`);
    }

    async function openPinterestPicker(btn) {
      _closePinMenu();
      const feedUrl = btn.getAttribute('data-pinterest-feed-url') || '';
      const entryId = btn.getAttribute('data-pinterest-entry-id') || '';
      const menu = document.createElement('div');
      menu.className = 'lectio-pin-menu';
      menu.innerHTML = '<div class="lectio-pin-loading">Loading boards…</div>';
      document.body.appendChild(menu);
      positionShareTargetMenu(menu, btn);
      setTimeout(() => document.addEventListener('click', _pinOutsideClick, true), 0);

      const data = await _pinFetchBoards();
      if (!data.connected) {
        menu.innerHTML = '<a class="lectio-pin-item" href="/integrations/pinterest/oauth/connect">Connect Pinterest…</a>';
        return;
      }
      if (data.error) { menu.innerHTML = `<div class="lectio-pin-loading">Error: ${data.error}</div>`; return; }
      const boards = data.boards || [];
      if (!boards.length) { menu.innerHTML = '<div class="lectio-pin-loading">No boards found.</div>'; return; }
      menu.innerHTML = '';
      boards.forEach(b => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'lectio-pin-item';
        item.textContent = b.name || '(untitled)';
        item.addEventListener('click', async () => {
          _closePinMenu();
          try { await _doPin(feedUrl, entryId, b.id, b.name || 'board'); }
          catch (e) { showToastMessage(e.message || 'Pin failed'); }
        });
        menu.appendChild(item);
      });
    }

    // --- Quire "Add to project" picker --------------------------------------
    let _quireProjectsCache = null;   // null = not fetched; {ok, projects, error}
    async function _fetchQuireProjects() {
      if (_quireProjectsCache) return _quireProjectsCache;
      try {
        const r = await fetch('/api/quire/projects', { credentials: 'same-origin' });
        _quireProjectsCache = (await r.json().catch(() => ({}))) || { ok: false, projects: [] };
      } catch (e) {
        _quireProjectsCache = { ok: false, projects: [], error: 'network' };
      }
      return _quireProjectsCache;
    }

    function _closeQuireMenu() {
      document.querySelector('.lectio-quire-menu')?.remove();
      document.removeEventListener('click', _quireOutsideClick, true);
    }
    function _quireOutsideClick(e) {
      if (!(e.target instanceof Element) || !e.target.closest('.lectio-quire-menu, [data-quire-feed-url]')) _closeQuireMenu();
    }

    async function _doQuireAdd(feedUrl, entryId, projectOid, projectName) {
      const fd = new FormData();
      fd.append('feed_url', feedUrl);
      fd.append('entry_id', entryId);
      fd.append('project_oid', projectOid);
      const r = await fetch('/entries/quire', { method: 'POST', body: fd, credentials: 'same-origin' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      showToastMessage(`Added to ${projectName}.`);
    }

    // One-click: add to the configured default project (no picker). The button is
    // only active when a default project is set, so an empty oid lets the server
    // fall back to it.
    async function _quireQuickAdd(btn) {
      hideShareMenu();
      _closeQuireMenu();
      const feedUrl = btn.getAttribute('data-quire-feed-url') || '';
      const entryId = btn.getAttribute('data-quire-entry-id') || '';
      const projectName = window.QUIRE_PROJECT_NAME || 'Quire';
      try { await _doQuireAdd(feedUrl, entryId, '', projectName); }
      catch (e) { showToastMessage(e.message || 'Quire add failed'); }
    }

    async function openQuirePicker(btn) {
      hideShareMenu();
      _closeQuireMenu();
      const feedUrl = btn.getAttribute('data-quire-feed-url') || '';
      const entryId = btn.getAttribute('data-quire-entry-id') || '';
      const menu = document.createElement('div');
      menu.className = 'lectio-quire-menu';
      menu.innerHTML = '<div class="lectio-quire-loading">Loading projects…</div>';
      document.body.appendChild(menu);
      positionShareTargetMenu(menu, btn);
      setTimeout(() => document.addEventListener('click', _quireOutsideClick, true), 0);

      const data = await _fetchQuireProjects();
      if (!data.ok) {
        menu.innerHTML = `<div class="lectio-quire-loading">${data.error || 'Failed to load projects.'}</div>`;
        return;
      }
      const projects = data.projects || [];
      if (!projects.length) { menu.innerHTML = '<div class="lectio-quire-loading">No projects found.</div>'; return; }
      menu.innerHTML = '';
      projects.forEach(p => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'lectio-quire-item';
        item.textContent = p.name || '(untitled)';
        item.addEventListener('click', async () => {
          _closeQuireMenu();
          try { await _doQuireAdd(feedUrl, entryId, p.oid, p.name || 'Quire'); }
          catch (e) { showToastMessage(e.message || 'Quire add failed'); }
        });
        menu.appendChild(item);
      });
    }

    // --- Reddit "Submit to subreddit" modal ----------------------------------
    async function openRedditSubmitModal(btn) {
      hideShareMenu();
      const feedUrl = btn.getAttribute('data-reddit-feed-url') || '';
      const entryId = btn.getAttribute('data-reddit-entry-id') || '';
      const prefillTitle = btn.getAttribute('data-reddit-title') || '';
      const prefillUrl = btn.getAttribute('data-reddit-url') || '';

      // Pre-fill subreddit from feed URL if it's a reddit feed.
      let prefillSub = '';
      const subMatch = feedUrl.match(/\/r\/([A-Za-z0-9_]+)/);
      if (subMatch) prefillSub = 'r/' + subMatch[1];

      const modal = document.getElementById('reddit-submit-modal');
      if (!modal) return;
      const subInput = document.getElementById('reddit-submit-sub');
      const titleInput = document.getElementById('reddit-submit-title');
      const urlInput = document.getElementById('reddit-submit-url');
      const errEl = document.getElementById('reddit-submit-error');
      if (subInput) subInput.value = prefillSub;
      if (titleInput) titleInput.value = prefillTitle;
      if (urlInput) urlInput.value = prefillUrl;
      if (errEl) errEl.textContent = '';
      modal.dataset.feedUrl = feedUrl;
      modal.dataset.entryId = entryId;
      modal.removeAttribute('hidden');
      if (subInput) subInput.focus();
    }

    document.getElementById('reddit-submit-confirm')?.addEventListener('click', async () => {
      const modal = document.getElementById('reddit-submit-modal');
      if (!modal) return;
      const subInput = document.getElementById('reddit-submit-sub');
      const titleInput = document.getElementById('reddit-submit-title');
      const urlInput = document.getElementById('reddit-submit-url');
      const errEl = document.getElementById('reddit-submit-error');
      const subreddit = (subInput?.value || '').trim();
      const title = (titleInput?.value || '').trim();
      const url = (urlInput?.value || '').trim();
      if (!subreddit) { if (errEl) errEl.textContent = 'Subreddit is required.'; return; }
      if (!url) { if (errEl) errEl.textContent = 'URL is required.'; return; }
      const btn = document.getElementById('reddit-submit-confirm');
      if (btn) btn.disabled = true;
      try {
        const r = await fetch('/api/reddit/submit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ subreddit, title, url,
            feed_url: modal.dataset.feedUrl || '',
            entry_id: modal.dataset.entryId || '' }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
        modal.setAttribute('hidden', '');
        showToastMessage(`Submitted to r/${subreddit.replace(/^r\//, '')}.`);
      } catch (e) {
        if (errEl) errEl.textContent = e.message || 'Submit failed.';
      } finally {
        if (btn) btn.disabled = false;
      }
    });
    document.getElementById('reddit-submit-cancel')?.addEventListener('click', () => {
      document.getElementById('reddit-submit-modal')?.setAttribute('hidden', '');
    });

    document.addEventListener('click', (event) => {
      const btn = event.target instanceof Element && event.target.closest('[data-open-email-modal]');
      if (!btn) return;
      const modal = document.getElementById('email-article-modal');
      const feedUrlInput = document.getElementById('email-article-feed-url');
      const entryIdInput = document.getElementById('email-article-entry-id');
      if (feedUrlInput instanceof HTMLInputElement) feedUrlInput.value = btn.getAttribute('data-email-feed-url') || '';
      if (entryIdInput instanceof HTMLInputElement) entryIdInput.value = btn.getAttribute('data-email-entry-id') || '';
      const contactSel = document.getElementById('email-article-contact-select');
      if (contactSel instanceof HTMLSelectElement) contactSel.value = '';
      const ccMe = document.getElementById('email-article-cc-me');
      if (ccMe instanceof HTMLInputElement) ccMe.checked = false;
      if (modal) modal.removeAttribute('hidden');
    });

    // Picking a saved contact fills the free-text To field (which still accepts
    // any typed address).
    const emailContactSel = document.getElementById('email-article-contact-select');
    if (emailContactSel instanceof HTMLSelectElement) {
      emailContactSel.addEventListener('change', () => {
        const toInput = document.getElementById('email-article-to');
        if (emailContactSel.value && toInput instanceof HTMLInputElement) {
          toInput.value = emailContactSel.value;
        }
      });
    }

    const emailArticleForm = document.getElementById('email-article-form');
    const emailArticleModal = document.getElementById('email-article-modal');
    if (emailArticleForm && emailArticleModal) {
      emailArticleForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const formData = new FormData(emailArticleForm);
        const sendBtn = document.getElementById('email-article-send');
        if (sendBtn) sendBtn.disabled = true;
        emailArticleModal.setAttribute('hidden', '');
        try {
          const response = await fetch('/entries/email', {
            method: 'POST',
            body: formData,
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'lectio-email-article' },
          });
          const data = await response.json();
          if (data && data.ok) {
            showToastMessage(data.message || 'Article sent.');
          } else {
            showToastMessage(data?.error || 'Could not send email.');
            emailArticleModal.removeAttribute('hidden');
          }
        } catch (_err) {
          showToastMessage('Could not send email.');
          emailArticleModal.removeAttribute('hidden');
        } finally {
          if (sendBtn) sendBtn.disabled = false;
        }
      });
    }

    const unreadFoldersOnlyToggle = document.getElementById('folders-unread-toggle');
    const UNREAD_FOLDERS_ONLY_KEY = 'lectio-unread-folders-only';

    function applyUnreadFoldersOnly(onlyUnread) {
      // Feeds tree only: the Saved sublist has no unread counts (badges are
      // total saved) and always shows exactly the folders that hold saves.
      for (const group of document.querySelectorAll('.feeds-tree-children .tree-folder-group')) {
        // The "All" row is navigation, not a folder — never filter it out.
        if (group.classList.contains('feeds-all-group')) { group.hidden = false; continue; }
        const unreadCount = Number(group.getAttribute('data-unread-count') || '0');
        group.hidden = onlyUnread && unreadCount <= 0;
      }
      for (const feed of document.querySelectorAll('.feeds-tree-children .tree-feed-item')) {
        const unreadCount = Number(feed.getAttribute('data-unread-count') || '0');
        feed.hidden = onlyUnread && unreadCount <= 0;
      }

      if (unreadFoldersOnlyToggle) {
        unreadFoldersOnlyToggle.classList.toggle('active', onlyUnread);
        unreadFoldersOnlyToggle.setAttribute('aria-pressed', onlyUnread ? 'true' : 'false');
      }
    }
    // Lazy-injected tree rows arrive after the load-time pass, so the loader
    // re-applies the persisted unread-only state to them via this hook.
    window._refreshUnreadFoldersOnly = () => {
      applyUnreadFoldersOnly(window.localStorage.getItem(UNREAD_FOLDERS_ONLY_KEY) === '1');
    };

    {
      const savedUnreadOnly = window.localStorage.getItem(UNREAD_FOLDERS_ONLY_KEY) === '1';
      applyUnreadFoldersOnly(savedUnreadOnly);
      if (unreadFoldersOnlyToggle) {
        unreadFoldersOnlyToggle.addEventListener('click', () => {
          const enabled = unreadFoldersOnlyToggle.getAttribute('aria-pressed') !== 'true';
          window.localStorage.setItem(UNREAD_FOLDERS_ONLY_KEY, enabled ? '1' : '0');
          applyUnreadFoldersOnly(enabled);
        });
      }
    }

    const tagsTreeBlock = document.getElementById('tags-tree-block');
    const tagsHeaderBtn = document.getElementById('tags-header-btn');
    const TAGS_COLLAPSED_KEY = 'lectio-tags-collapsed';
    let problematicFeedsViewedThisSession = false;

    function applyTagsCollapsed(collapsed) {
      if (!tagsTreeBlock) return;
      tagsTreeBlock.classList.toggle('is-collapsed', collapsed);
    }

    if (tagsTreeBlock) {
      const savedTagsCollapsed = window.localStorage.getItem(TAGS_COLLAPSED_KEY) === '1';
      applyTagsCollapsed(savedTagsCollapsed);
      if (tagsHeaderBtn) {
        tagsHeaderBtn.addEventListener('click', () => {
          const nextCollapsed = !tagsTreeBlock.classList.contains('is-collapsed');
          window.localStorage.setItem(TAGS_COLLAPSED_KEY, nextCollapsed ? '1' : '0');
          applyTagsCollapsed(nextCollapsed);
        });
      }
    }

    async function markProblematicFeedsViewed() {
      if (problematicFeedsViewedThisSession) return;
      problematicFeedsViewedThisSession = true;
      try {
        await fetch('/settings/problematic-feeds/viewed', {
          method: 'POST',
          headers: { 'X-Requested-With': 'lectio-problematic-feeds-viewed' },
          credentials: 'same-origin',
        });
      } catch (_err) {
        // ignore
      }
    }

    // (markProblematicFeedsViewed is now called when the Feeds tab opens in Settings)

    const panes = document.querySelector('.panes');
    const rootStyle = document.documentElement.style;
    const RESIZER_SIZE = 8;
    const MIN_LEFT = 220;
    const MIN_MIDDLE = 280;
    const MIN_RIGHT = 300;
    const PANE_LEFT_KEY = 'lectio-pane-left';
    const PANE_MIDDLE_KEY = 'lectio-pane-middle';

    let activeResizer = null;

    function pxVar(name, fallback) {
      const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return Number.parseFloat(raw || fallback);
    }

    function clampPaneWidths(leftWidth, middleWidth, totalWidth) {
      const maxLeft = totalWidth - RESIZER_SIZE * 2 - MIN_MIDDLE - MIN_RIGHT;
      const nextLeft = Math.max(MIN_LEFT, Math.min(maxLeft, leftWidth));
      const maxMiddle = totalWidth - nextLeft - RESIZER_SIZE * 2 - MIN_RIGHT;
      const nextMiddle = Math.max(MIN_MIDDLE, Math.min(maxMiddle, middleWidth));
      return { left: nextLeft, middle: nextMiddle };
    }

    function persistPaneWidths() {
      if (!panes) {
        return;
      }
      window.localStorage.setItem(PANE_LEFT_KEY, String(Math.round(pxVar('--pane-left', '280'))));
      window.localStorage.setItem(PANE_MIDDLE_KEY, String(Math.round(pxVar('--pane-middle', '420'))));
    }

    function restorePaneWidths() {
      if (!panes) {
        return;
      }
      const savedLeft = Number.parseFloat(window.localStorage.getItem(PANE_LEFT_KEY) || '');
      const savedMiddle = Number.parseFloat(window.localStorage.getItem(PANE_MIDDLE_KEY) || '');
      if (!Number.isFinite(savedLeft) || !Number.isFinite(savedMiddle)) {
        return;
      }

      const total = panes.getBoundingClientRect().width;
      const clamped = clampPaneWidths(savedLeft, savedMiddle, total);
      rootStyle.setProperty('--pane-left', `${clamped.left}px`);
      rootStyle.setProperty('--pane-middle', `${clamped.middle}px`);
    }

    restorePaneWidths();

    window.addEventListener('resize', () => {
      if (!panes) {
        return;
      }
      const total = panes.getBoundingClientRect().width;
      const leftWidth = pxVar('--pane-left', '280');
      const middleWidth = pxVar('--pane-middle', '420');
      const clamped = clampPaneWidths(leftWidth, middleWidth, total);
      rootStyle.setProperty('--pane-left', `${clamped.left}px`);
      rootStyle.setProperty('--pane-middle', `${clamped.middle}px`);
      persistPaneWidths();
    });

    for (const resizer of document.querySelectorAll('.pane-resizer')) {
      resizer.addEventListener('mousedown', (event) => {
        if (!panes) return;
        event.preventDefault();
        activeResizer = resizer.getAttribute('data-resizer');
        document.body.classList.add('resizing');
      });
      // Touch/pen support
      resizer.addEventListener('touchstart', (event) => {
        if (!panes) return;
        if (event.touches.length !== 1) return;
        event.preventDefault();
        activeResizer = resizer.getAttribute('data-resizer');
        document.body.classList.add('resizing');
      }, { passive: false });
    }

    function handleResize(clientX) {
      if (!panes || !activeResizer) return;
      const rect = panes.getBoundingClientRect();
      const total = rect.width;
      const leftWidth = pxVar('--pane-left', '280');
      const middleWidth = pxVar('--pane-middle', '420');
      if (activeResizer === 'left-middle') {
        const desiredLeft = clientX - rect.left;
        const clamped = clampPaneWidths(desiredLeft, middleWidth, total);
        rootStyle.setProperty('--pane-left', `${clamped.left}px`);
        rootStyle.setProperty('--pane-middle', `${clamped.middle}px`);
        return;
      }
      if (activeResizer === 'middle-right') {
        const desiredMiddle = clientX - rect.left - leftWidth - RESIZER_SIZE;
        const clamped = clampPaneWidths(leftWidth, desiredMiddle, total);
        rootStyle.setProperty('--pane-left', `${clamped.left}px`);
        rootStyle.setProperty('--pane-middle', `${clamped.middle}px`);
      }
    }

    window.addEventListener('mousemove', (event) => {
      if (!activeResizer) return;
      handleResize(event.clientX);
    });

    window.addEventListener('touchmove', (event) => {
      if (!activeResizer) return;
      if (event.touches.length !== 1) return;
      handleResize(event.touches[0].clientX);
    }, { passive: false });

    function endResize() {
      const wasResizing = Boolean(activeResizer);
      activeResizer = null;
      document.body.classList.remove('resizing');
      if (wasResizing) persistPaneWidths();
    }
    window.addEventListener('mouseup', endResize);
    window.addEventListener('touchend', endResize);

    const youtubeSyncLastAt = window.YT_SYNC_LAST_AT || '';
    const youtubeSyncLastResult = window.YT_SYNC_LAST_RESULT || '';

    const contextMenu = document.getElementById('folder-context-menu');
    const rootContextMenu = document.getElementById('root-context-menu');
    const postContextMenu = document.getElementById('post-context-menu');
    const tagContextMenu = document.getElementById('tag-context-menu');
    let contextTagName = null;
    const treeNav = document.querySelector('.tree');
    const refreshButton = document.getElementById('ctx-refresh');
    const markReadButton = document.getElementById('ctx-mark-read');
    const rootRefreshButton = document.getElementById('ctx-root-refresh');
    const rootAddFolderButton = document.getElementById('ctx-root-add-folder');
    const rootAddFeedButton = document.getElementById('ctx-root-add-feed');
    const addToFolderWrap = document.getElementById('ctx-add-to-folder-wrap');
    const addToFolderButton = document.getElementById('ctx-add-to-folder');
    const folderSubmenu = document.getElementById('ctx-folder-submenu');
    const addFeedButton = document.getElementById('ctx-add-feed');
    const feedPropertiesButton = document.getElementById('ctx-feed-properties');
    const folderPropertiesButton = document.getElementById('ctx-folder-properties');
    const highlightsButton = document.getElementById('ctx-highlights');
    const unsubscribeFeedButton = document.getElementById('ctx-unsubscribe-feed');
    const renameFolderButton = document.getElementById('ctx-rename-folder');
    const deleteFolderButton = document.getElementById('ctx-delete-folder');
    const youtubeSyncButton = document.getElementById('ctx-youtube-sync');
    const disableFeedButton = document.getElementById('ctx-disable-feed');
    const addFeedForm = document.getElementById('context-add-feed-form');
    const addFolderForm = document.getElementById('context-add-folder-form');
    const renameFolderForm = document.getElementById('context-rename-folder-form');
    const deleteFolderForm = document.getElementById('context-delete-folder-form');
    const youtubeSyncForm = document.getElementById('context-youtube-sync-form');
    const youtubeSyncFolderIdInput = document.getElementById('context-youtube-sync-folder-id');
    const disableFeedForm = document.getElementById('context-disable-feed-form');
    const enableFeedForm = document.getElementById('context-enable-feed-form');
    const disableFeedFolderIdInput = document.getElementById('context-disable-folder-id');
    const disableFeedUrlInput = document.getElementById('context-disable-feed-url');
    const enableFeedFolderIdInput = document.getElementById('context-enable-folder-id');
    const enableFeedUrlInput = document.getElementById('context-enable-feed-url');
    const refreshFolderForm = document.getElementById('context-refresh-folder-form');
    const markReadFolderForm = document.getElementById('context-mark-read-folder-form');
    const refreshFeedForm = document.getElementById('context-refresh-feed-form');
    const markReadFeedForm = document.getElementById('context-mark-read-feed-form');
    const moveFeedForm = document.getElementById('context-move-feed-form');
    const unsubscribeFeedForm = document.getElementById('context-unsubscribe-feed-form');
    const contextFolderIdInput = document.getElementById('context-folder-id');
    const contextFolderNameInput = document.getElementById('context-folder-name');
    const contextFeedUrlInput = document.getElementById('context-feed-url');
    const renameFolderIdInput = document.getElementById('context-rename-folder-id');
    const renameFolderNameInput = document.getElementById('context-rename-folder-name');
    const deleteFolderIdInput = document.getElementById('context-delete-folder-id');
    const refreshFolderIdInput = document.getElementById('context-refresh-folder-id');
    const refreshFolderListFeedUrlInput = document.getElementById('context-refresh-folder-list-feed-url');
    const refreshFolderFeedUrlInput = document.getElementById('context-refresh-folder-feed-url');
    const refreshFolderEntryIdInput = document.getElementById('context-refresh-folder-entry-id');
    const refreshFolderSortByInput = document.getElementById('context-refresh-folder-sort-by');
    const refreshFolderSortDirInput = document.getElementById('context-refresh-folder-sort-dir');
    const refreshFolderReadFilterInput = document.getElementById('context-refresh-folder-read-filter');
    const refreshFolderStarOnlyInput = document.getElementById('context-refresh-folder-star-only');
    const refreshFolderResumeReadFilterInput = document.getElementById('context-refresh-folder-resume-read-filter');
    const refreshFolderTagInput = document.getElementById('context-refresh-folder-tag');
    const markReadFolderIdInput = document.getElementById('context-mark-read-folder-id');
    const refreshFeedFolderIdInput = document.getElementById('context-refresh-feed-folder-id');
    const refreshFeedUrlInput = document.getElementById('context-refresh-feed-url');
    const refreshListFeedUrlInput = document.getElementById('context-refresh-list-feed-url');
    const refreshFeedEntryIdInput = document.getElementById('context-refresh-feed-entry-id');
    const refreshFeedSortByInput = document.getElementById('context-refresh-feed-sort-by');
    const refreshFeedSortDirInput = document.getElementById('context-refresh-feed-sort-dir');
    const refreshFeedReadFilterInput = document.getElementById('context-refresh-feed-read-filter');
    const refreshFeedStarOnlyInput = document.getElementById('context-refresh-feed-star-only');
    const refreshFeedResumeReadFilterInput = document.getElementById('context-refresh-feed-resume-read-filter');
    const refreshFeedTagInput = document.getElementById('context-refresh-feed-tag');
    const markReadFeedFolderIdInput = document.getElementById('context-mark-read-feed-folder-id');
    const markReadFeedUrlInput = document.getElementById('context-mark-read-feed-url');
    const markReadListFeedUrlInput = document.getElementById('context-mark-read-list-feed-url');
    const moveFeedUrlInput = document.getElementById('context-move-feed-url');
    const moveFeedFromFolderIdInput = document.getElementById('context-move-feed-from-folder-id');
    const moveFeedToFolderIdInput = document.getElementById('context-move-feed-to-folder-id');
    const unsubscribeFolderIdInput = document.getElementById('context-unsubscribe-folder-id');
    const unsubscribeFeedUrlInput = document.getElementById('context-unsubscribe-feed-url');
    const postMarkReadButton = document.getElementById('ctx-post-mark-read');
    const postMarkFeedReadButton = document.getElementById('ctx-post-mark-feed-read');
    const postMarkAboveReadButton = document.getElementById('ctx-post-mark-above-read');
    const postMarkBelowReadButton = document.getElementById('ctx-post-mark-below-read');
    const postCopyUrlButton = document.getElementById('ctx-post-copy-url');
    const postAutomationButton = document.getElementById('ctx-post-automation');
    const postMoveToFeedButton = document.getElementById('ctx-post-move-to-feed');
    const postMoveVisibleButton = document.getElementById('ctx-post-move-visible');
    const postDeleteButton = document.getElementById('ctx-post-delete');
    const postEditDateButton = document.getElementById('ctx-post-edit-date');
    const postEditTitleButton = document.getElementById('ctx-post-edit-title');
    const postClearImgCacheButton = document.getElementById('ctx-post-clear-img-cache');
    const postReadForm = document.getElementById('context-post-read-form');
    const postRangeReadForm = document.getElementById('context-post-range-read-form');
    const postReadFeedUrlInput = document.getElementById('context-post-read-feed-url');
    const postReadEntryIdInput = document.getElementById('context-post-read-entry-id');
    const postReadValueInput = document.getElementById('context-post-read-value');
    const postRangeFeedUrlInput = document.getElementById('context-post-range-feed-url');
    const postRangeEntryIdInput = document.getElementById('context-post-range-entry-id');
    const postRangeDirectionInput = document.getElementById('context-post-range-direction');
    const actionInputModal = document.getElementById('action-input-modal');
    const actionModalTitle = document.getElementById('action-modal-title');
    const actionModalLabel = document.getElementById('action-modal-label');
    const actionModalForm = document.getElementById('action-modal-form');
    const actionModalInput = document.getElementById('action-modal-input');
    const actionModalCancel = document.getElementById('action-modal-cancel');
    const actionModalSubmit = document.getElementById('action-modal-submit');
    const globalNoteModal = document.getElementById('global-note-modal');
    const globalNoteForm = document.getElementById('global-note-form');
    const feedPropertiesModal = document.getElementById('feed-properties-modal');
    const feedPropertiesClose = document.getElementById('feed-properties-close');
    const feedPropUserTitle = document.getElementById('feed-prop-user-title');
    const feedPropResetTitleBtn = document.getElementById('feed-prop-reset-title-btn');
    const feedPropRealTitle = document.getElementById('feed-prop-real-title');
    const feedPropWebsite = document.getElementById('feed-prop-website');
    const feedPropWebsiteOpen = document.getElementById('feed-prop-website-open');
    const feedPropXml = document.getElementById('feed-prop-xml');
    const feedPropXmlOpen = document.getElementById('feed-prop-xml-open');
    const feedPropChangeUrlBtn = document.getElementById('feed-prop-change-url-btn');
    const feedPropChangeUrlWrap = document.getElementById('feed-prop-change-url-wrap');
    const feedPropChangeUrlInput = document.getElementById('feed-prop-change-url-input');
    const feedPropChangeUrlSave = document.getElementById('feed-prop-change-url-save');
    const feedPropChangeUrlCancel = document.getElementById('feed-prop-change-url-cancel');
    const feedPropChangeUrlStatus = document.getElementById('feed-prop-change-url-status');
    const feedPropUpdatesLabel = document.getElementById('feed-prop-updates-label');
    const feedPropDisableBtn = document.getElementById('feed-prop-disable-btn');
    const feedPropDisableStatus = document.getElementById('feed-prop-disable-status');
    const feedPropHealth = document.getElementById('feed-prop-health');
    const feedPropHealthDetail = document.getElementById('feed-prop-health-detail');
    const feedPropTotal = document.getElementById('feed-prop-total');
    const feedPropUnread = document.getElementById('feed-prop-unread');
    const feedPropAdded = document.getElementById('feed-prop-added');
    const feedPropUpdated = document.getElementById('feed-prop-updated');
    const feedPropReceived = document.getElementById('feed-prop-received');
    const feedPropLastPost = document.getElementById('feed-prop-last-post');
    const feedPropFolderSelect = document.getElementById('feed-prop-folder-select');
    const feedPropFolderStatus = document.getElementById('feed-prop-folder-status');
    const feedPropStrategy = document.getElementById('feed-prop-strategy');
    const feedPropStrategyHint = document.getElementById('feed-prop-strategy-hint');
    const feedPropShowThumb = document.getElementById('feed-prop-show-thumb');
    const feedPropShowInArticle = document.getElementById('feed-prop-show-in-article');
    const feedPropInjectSourceImages = document.getElementById('feed-prop-inject-source-images');
    const feedPropPresetBtns = document.querySelectorAll('.feed-prop-preset-btn');
    const feedPropCaptionTitle = document.getElementById('feed-prop-caption-title');
    const feedPropCaptionAlt = document.getElementById('feed-prop-caption-alt');
    const feedPropCaptionAutoBtn = document.getElementById('feed-prop-caption-auto-btn');
    const feedPropStratCaptions = document.getElementById('feed-prop-strat-captions');
    const feedPropCaptionAltPreview   = document.getElementById('feed-prop-caption-alt-preview');
    const feedPropCaptionTitlePreview = document.getElementById('feed-prop-caption-title-preview');
    let _lastStratCacheRows = [];
    const feedPropRefreshBtn = document.getElementById('feed-prop-refresh-btn');
    const STRATEGY_LABELS = {
      inline: 'Feed content', og_scrape: 'Source page', webcomic: 'Webcomic',
      artwork: 'Artwork', media_rss: 'Media RSS', enclosure: 'Enclosure', youtube: 'YouTube',
    };
    const feedPropStratGrid = document.getElementById('feed-prop-strat-grid');
    const feedPropStratEmpty = document.getElementById('feed-prop-strat-empty');
    const feedPropUnsubscribeBtn = document.getElementById('feed-prop-unsubscribe-btn');
    // Sentinel folder id for a feed that belongs to no folder (an Uncategorized
    // orphan). Real folder ids are >= 1; the Uncategorized virtual folder is -1.
    const ORPHAN_FOLDER_ID = 0;
    const feedPropReparseBtn = document.getElementById('feed-prop-reparse-btn');
    const feedPropReparseStatus = document.getElementById('feed-prop-reparse-status');
    const feedPropBrowserUaRow = document.getElementById('feed-prop-browser-ua-row');
    const feedPropBrowserUaReset = document.getElementById('feed-prop-browser-ua-reset');
    const feedPropCooldownLabel = document.getElementById('feed-prop-cooldown-label');
    const feedPropCooldown = document.getElementById('feed-prop-cooldown');
    const feedPropTabInfo = document.getElementById('feed-prop-tab-info');
    const feedPropTabTuning = document.getElementById('feed-prop-tab-tuning');
    const feedPropTabHistory = document.getElementById('feed-prop-tab-history');
    const feedPropTabAutomations = document.getElementById('feed-prop-tab-automations');
    const feedPropYtSection = document.getElementById('feed-prop-yt-section');
    const feedPropImgSection = document.getElementById('feed-prop-img-section');
    const feedPropDevSection = document.getElementById('feed-prop-dev-section');
    const feedPropHideShorts = document.getElementById('feed-prop-hide-shorts');
    const feedPropFlushBatchBtn = document.getElementById('feed-prop-flush-batch-btn');
    const feedPropFlushBatchStatus = document.getElementById('feed-prop-flush-batch-status');
    document.querySelectorAll('[data-feed-prop-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.getAttribute('data-feed-prop-tab');
        document.querySelectorAll('[data-feed-prop-tab]').forEach(b => {
          b.classList.toggle('hl-tab-btn--active', b === btn);
          b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
        });
        if (feedPropTabInfo) feedPropTabInfo.hidden = tab !== 'info';
        if (feedPropTabTuning) feedPropTabTuning.hidden = tab !== 'tuning';
        if (feedPropTabHistory) feedPropTabHistory.hidden = tab !== 'history';
        if (feedPropTabAutomations) feedPropTabAutomations.hidden = tab !== 'automations';
      });
    });
    let entryArticle = document.querySelector('.entry');
    let entryPaneTitle = document.querySelector('.entry-pane-title');
    let entryBody = document.getElementById('entry-body');
    let entryReadabilityButton = document.getElementById('entry-readability-button');
    let entrySourceButton = document.getElementById('entry-source-button');
    let entrySourceFrame = document.getElementById('entry-source-frame');
    let entryReadabilityContainer = document.getElementById('entry-readability-container');
    let entrySourceFallback = document.getElementById('entry-source-fallback');
    let entrySourceOpenExternal = document.getElementById('entry-source-open-external');
    let entrySourceDismiss = document.getElementById('entry-source-dismiss');
    let entrySourceModeIndicator = document.getElementById('entry-source-mode-indicator');
    let entryPaneObserver = null;

    let contextFolderId = null;
    let contextFeedUrl = null;
    let contextFolderName = '';
    let contextFolderDepth = 0;
    let contextTargetType = 'folder';
    let contextPostFeedUrl = null;
    let contextPostEntryId = null;
    let contextPostRead = false;
    let contextPostLink = '';
    let contextPostTitle = '';
    let contextPostFolderId = null;
    let actionModalSubmitHandler = null;
    let sourceViewActive = false;
    let sourceLoadTimeoutId = null;
    let sourceFrameLoaded = false;
    let sourceHealthCheckId = null;
    let sourceViewMode = null;
    let sourceViewUrl = '';
    let sourceFallbackAttempted = false;
    let sourceReadabilityAttempted = false;
    let sourceDirectLoaded = false;
    let frameCheckRequestToken = 0;
    let entryPaneRequestToken = 0;
    let scopePaneRequestToken = 0;
    let activeScopeUrl = window.location.href;
    let _prevPaneEntryOverflow = null;

    function normalizeScopeUrl(rawUrl) {
      const u = new URL(rawUrl, window.location.origin);
      u.searchParams.delete('chunk');
      u.searchParams.delete('chunk_delta');
      return u.toString();
    }

    function refreshEntryPaneRefs() {
      entryArticle = document.querySelector('.entry');
      entryPaneTitle = document.querySelector('.entry-pane-title');
      entryBody = document.getElementById('entry-body');
      entryReadabilityButton = document.getElementById('entry-readability-button');
      entrySourceButton = document.getElementById('entry-source-button');
      entrySourceFrame = document.getElementById('entry-source-frame');
      entryReadabilityContainer = document.getElementById('entry-readability-container');
      entrySourceFallback = document.getElementById('entry-source-fallback');
      entrySourceOpenExternal = document.getElementById('entry-source-open-external');
      entrySourceDismiss = document.getElementById('entry-source-dismiss');
      entrySourceModeIndicator = document.getElementById('entry-source-mode-indicator');

      // Rebind a mutation observer to the (possibly replaced) entry pane so
      // activating the source view moves us into the entry single-pane level.
      try {
        if (entryPaneObserver) {
          try { entryPaneObserver.disconnect(); } catch (e) {}
          entryPaneObserver = null;
        }
        const newEntryPane = document.querySelector('.pane-entry');
        if (newEntryPane) {
          entryPaneObserver = new MutationObserver(() => {
            try { if (window.isSingleMode && window.isSingleMode()) setSinglePaneLevel(2); } catch (e) {}
          });
          entryPaneObserver.observe(newEntryPane, { childList: true, subtree: true });
        }
      } catch (e) {
        // ignore
      }
    }

    // Ensure the entry source iframe fills the available entry article area.
    function ensureSourceFrameFills() {
      try {
        if (!entrySourceFrame) return;
        // Only run when web view is actively showing the iframe.
        if (!sourceViewActive || sourceViewMode === 'readability') return;
        // Anchor sizing to the pane container (more reliable than article height).
        const paneEntry = document.querySelector('.pane-entry');
        if (!paneEntry) return;
        const header = paneEntry.querySelector('.entry-pane-header');
        const paneRect = paneEntry.getBoundingClientRect();
        const headerHeight = header ? header.getBoundingClientRect().height : 0;
        const topOffset = Math.max(0, headerHeight);
        // Prefer flex-based sizing: make the iframe a flex child that grows to
        // fill remaining space in the `.entry` column. This is more reliable
        // across browsers and avoids absolute positioning quirks.
        try { entrySourceFrame.style.removeProperty('position'); } catch (e) {}
        entrySourceFrame.style.left = '';
        entrySourceFrame.style.right = '';
        entrySourceFrame.style.top = '';
        entrySourceFrame.style.bottom = '';
        entrySourceFrame.style.width = '100%';
        entrySourceFrame.style.flex = '1 1 auto';
        entrySourceFrame.style.minHeight = '0';
        entrySourceFrame.style.alignSelf = 'stretch';
        entrySourceFrame.style.height = 'auto';
      } catch (e) {
        // ignore
      }
    }

    // Wire resize/visualViewport events to keep iframe sized correctly.
    window.addEventListener('resize', () => { window.setTimeout(ensureSourceFrameFills, 40); }, { passive: true });
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', () => { window.setTimeout(ensureSourceFrameFills, 40); }, { passive: true });
      window.visualViewport.addEventListener('scroll', () => { window.setTimeout(ensureSourceFrameFills, 40); }, { passive: true });
    }

    function markActivePostByUrl(url) {
      const parsedUrl = new URL(url, window.location.origin);
      const targetFeedUrl = parsedUrl.searchParams.get('feed_url');
      const targetEntryId = parsedUrl.searchParams.get('entry_id');
      if (!targetFeedUrl || !targetEntryId) {
        return;
      }

      for (const postItem of document.querySelectorAll('.post-item')) {
        const isTarget =
          postItem.getAttribute('data-post-feed-url') === targetFeedUrl
          && postItem.getAttribute('data-post-entry-id') === targetEntryId;
        postItem.classList.toggle('active', isTarget);
        if (isTarget) {
          const wasUnread = postItem.getAttribute('data-post-read') === '0';
          postItem.classList.add('is-read');
          postItem.setAttribute('data-post-read', '1');
          const readInput = postItem.querySelector('.post-read-toggle-form input[name="read"]');
          if (readInput) {
            readInput.value = '0';
          }
          const readButton = postItem.querySelector('.post-read-toggle');
          if (readButton) {
            readButton.title = 'Mark as Unread';
            readButton.setAttribute('aria-label', 'Mark as Unread');
          }
          if (wasUnread) {
            const fallbackBase = getUnreadCountFallback();
            const current = Number.isFinite(appUnreadCount) ? appUnreadCount : fallbackBase;
            appUnreadCount = Math.max(0, current - 1);
            updateDynamicFavicon();
            const postFeedUrl = postItem.getAttribute('data-post-feed-url') || '';
            if (postFeedUrl) adjustSidebarUnreadCount(postFeedUrl, -1, postItem.getAttribute('data-post-saved') === '1');
          }
        }
      }
    }

    function updateScopeActiveState(url) {
      const nextUrl = new URL(url, window.location.origin);
      let nextFolderId = nextUrl.searchParams.get('folder_id');
      let nextFeedUrl = nextUrl.searchParams.get('list_feed_url');
      const nextTag = nextUrl.searchParams.get('tag');
      const nextStarOnly = nextUrl.searchParams.get('star_only') === '1';
      const nextHome = nextUrl.searchParams.get('home') === '1'
        || (nextStarOnly && nextUrl.searchParams.get('saved_home') === '1');
      const nextSavedHome = nextStarOnly && nextHome;
      const nextReadFilter = nextUrl.searchParams.get('read_filter') || 'unread';
      const nextResumeReadFilter = nextUrl.searchParams.get('resume_read_filter') || nextReadFilter;
      const nextSortBy = nextUrl.searchParams.get('sort_by') || 'post';
      const nextSortDir = nextUrl.searchParams.get('sort_dir') || 'desc';

      // Bare URLs are ambiguous in SPA/popstate flows. Prefer preserving
      // current visible selection to avoid active-state flicker/clearing.
      // Scope tabs are excluded: they're mode indicators that always carry the
      // root folder id, so falling back to one would light up the "All" row.
      let nextHomeEffective = nextHome;
      if (!nextFolderId && !nextFeedUrl && !nextTag) {
        const activeFeed = document.querySelector('.feed-link.active[data-feed-url]');
        if (activeFeed instanceof HTMLElement) {
          nextFeedUrl = activeFeed.getAttribute('data-feed-url') || null;
          nextFolderId = activeFeed.getAttribute('data-folder-id') || nextFolderId;
        } else {
          const activeFolder = document.querySelector('.tree-item.active[data-folder-id]:not(.scope-tab)');
          if (activeFolder instanceof HTMLElement) {
            nextFolderId = activeFolder.getAttribute('data-folder-id') || nextFolderId;
          }
        }
      }

      if (!nextFolderId && !nextFeedUrl && !nextTag) {
        // Still nothing: a truly bare URL is the scope-tab landing (the server
        // treats bare / as home=1) — keep every folder row inactive rather than
        // defaulting to root, which used to auto-select "All".
        nextHomeEffective = true;
        const rootTree = document.querySelector('.tree[data-root-folder-id]');
        if (rootTree instanceof HTMLElement) {
          nextFolderId = rootTree.getAttribute('data-root-folder-id') || null;
        }
      }

      const foldersStarToggle = document.getElementById('folders-star-toggle');
      if (foldersStarToggle) {
        const resumeFilter = (nextReadFilter === 'history' || nextStarOnly) ? nextResumeReadFilter : nextReadFilter;
        const params = new URLSearchParams();
        params.set('folder_id', nextFolderId || '1');
        if (nextFeedUrl) {
          params.set('list_feed_url', nextFeedUrl);
        }
        if (nextTag) {
          params.set('tag', nextTag);
        }
        params.set('sort_by', nextSortBy);
        params.set('sort_dir', nextSortDir);
        params.set('read_filter', nextStarOnly ? resumeFilter : 'all');
        params.set('star_only', nextStarOnly ? '0' : '1');
        params.set('resume_read_filter', resumeFilter);

        foldersStarToggle.setAttribute('href', `/?${params.toString()}`);
        foldersStarToggle.classList.toggle('active', nextStarOnly);
      }

      for (const folderLink of document.querySelectorAll('.tree-item[data-folder-id]')) {
        // Scope tabs are mode indicators: active by feeds/saved mode, not folder.
        if (folderLink.classList.contains('scope-tab')) {
          const savedTab = folderLink.classList.contains('saved-item');
          folderLink.classList.toggle('active', savedTab ? nextStarOnly : !nextStarOnly);
          continue;
        }
        let isMatch = false;
        if (!nextFeedUrl && folderLink.getAttribute('data-folder-id') === nextFolderId) {
          // Saved "All" row: the whole-backlog view (root + star, not landing).
          if (folderLink.classList.contains('saved-all-item')) {
            isMatch = nextStarOnly && !nextHomeEffective;
          }
          // Feeds "All" row: every feed (root, feeds mode, not landing).
          else if (folderLink.classList.contains('feeds-all-item')) {
            isMatch = !nextStarOnly && !nextHomeEffective;
          }
          // Saved-mode folder rows vs feeds-tree folder rows: same folder ids,
          // so activate only the copy that belongs to the current mode.
          else if (folderLink.classList.contains('saved-folder-item')) {
            isMatch = nextStarOnly;
          }
          else {
            isMatch = !nextStarOnly && !nextHomeEffective;
          }
        }
        folderLink.classList.toggle('active', isMatch);
      }

      // Saved mode and Feeds mode are mutually exclusive tree blocks; the
      // pane-swap path doesn't re-render the tree, so toggle them here.
      document.querySelector('.saved-tree-children')?.toggleAttribute('hidden', !nextStarOnly);
      document.querySelector('.feeds-tree-children')?.toggleAttribute('hidden', nextStarOnly);
      // saved-mode drives the pinned layout (All Feeds stuck above Tags while
      // the saved sublist scrolls).
      document.querySelector('nav.tree')?.classList.toggle('saved-mode', nextStarOnly);

      for (const feedLink of document.querySelectorAll('.feed-link[data-feed-url]')) {
        const isMatch = Boolean(nextFeedUrl) && feedLink.getAttribute('data-feed-url') === nextFeedUrl;
        feedLink.classList.toggle('active', isMatch);
        if (isMatch) {
          // Expand the folder that contains this feed so the user sees the
          // selected feed in the tree (e.g. when navigating from a post-list
          // feed-name link or the entry pane's feed link).
          const ul = feedLink.closest('ul.tree-feed-list');
          if (ul && ul.id) {
            ul.removeAttribute('hidden');
            const toggle = document.querySelector(`.tree-toggle[data-tree-target="${CSS.escape(ul.id)}"]`);
            if (toggle) {
              toggle.classList.add('expanded');
              toggle.setAttribute('aria-expanded', 'true');
            }
          }
        }
      }

      // Selected feed inside a folder whose deferred rows haven't loaded yet:
      // fetch them so the tree can highlight and reveal it (matches the
      // pre-lazy behavior where every folder's rows were always in the DOM).
      if (nextFeedUrl && nextFolderId
          && !document.querySelector(`.feed-link[data-feed-url="${CSS.escape(nextFeedUrl)}"]`)) {
        const lazyUl = document.getElementById(`folder-feeds-${nextFolderId}`);
        if (lazyUl && lazyUl.hasAttribute('data-lazy-feeds')) {
          const wantedFeedUrl = nextFeedUrl;
          void window._ensureTreeFeedsLoaded?.(lazyUl).then(() => {
            for (const feedLink of lazyUl.querySelectorAll('.feed-link[data-feed-url]')) {
              if (feedLink.getAttribute('data-feed-url') !== wantedFeedUrl) continue;
              feedLink.classList.add('active');
              lazyUl.removeAttribute('hidden');
              const toggle = document.querySelector(`.tree-toggle[data-tree-target="${CSS.escape(lazyUl.id)}"]`);
              if (toggle) {
                toggle.classList.add('expanded');
                toggle.setAttribute('aria-expanded', 'true');
              }
            }
          });
        }
      }

      for (const tagLink of document.querySelectorAll('.tag-link, .entry-tag-link')) {
        const href = tagLink.getAttribute('href') || '';
        const tagValue = new URL(href, window.location.origin).searchParams.get('tag');
        tagLink.classList.toggle('active', Boolean(nextTag) && tagValue === nextTag);
      }

      // Sidebar tree-filter state: hide sidebar feeds with 0 unread when the
      // posts filter is set to unread. CSS drives the actual hide; we just
      // toggle the body attribute so SPA nav doesn't lose the state.
      const treeFilter = nextReadFilter === 'unread' && !nextStarOnly ? 'unread' : 'all';
      document.body.setAttribute('data-tree-filter', treeFilter);
    }

    function applyCurrentScopeStateToScopeLink(targetUrl, linkEl) {
      try {
        if (!linkEl || !targetUrl) {
          return;
        }
        if (!linkEl.matches('.tree-item, .feed-link, .entry-feed-link')) {
          return;
        }

        const currentParams = new URL(window.location.href, window.location.origin).searchParams;

        // Mode switches (Saved Articles row ↔ All Feeds row, or any link whose
        // star_only differs from the current view) own their read/star state:
        // each mode keeps a separate remembered filter, so stamping the
        // CURRENT filter across the switch would leak one mode's choice into
        // the other (the bug where entering Saved inherited Feeds' Unread).
        // Only sort carries across.
        const targetStar2 = targetUrl.searchParams.get('star_only') === '1';
        const currentStar2 = currentParams.get('star_only') === '1';
        if (linkEl.matches('.saved-item, .root-item') || targetStar2 !== currentStar2) {
          for (const key of ['sort_by', 'sort_dir']) {
            if (currentParams.has(key)) {
              targetUrl.searchParams.set(key, currentParams.get(key) || '');
            }
          }
          return;
        }

        // Leaving a tag view: clicking a tag forces read_filter=all, so when the
        // user then picks a folder/feed, restore the filter that was active
        // before the tag (carried in resume_read_filter) — mirroring how exiting
        // History returns to the prior filter. Drop the tag from the target.
        if (currentParams.get('tag')) {
          const resume = currentParams.get('resume_read_filter') || 'unread';
          targetUrl.searchParams.set('read_filter', resume);
          targetUrl.searchParams.set('resume_read_filter', resume);
          for (const key of ['star_only', 'sort_by', 'sort_dir']) {
            if (currentParams.has(key)) {
              targetUrl.searchParams.set(key, currentParams.get(key) || '');
            }
          }
          targetUrl.searchParams.delete('tag');
          return;
        }

        const currentReadFilter = currentParams.get('read_filter') || '';
        const shouldKeepTargetReadFilter =
          currentReadFilter === 'history' && linkEl.matches('.tree-item, .feed-link');
        const keysToSync = ['resume_read_filter', 'star_only', 'sort_by', 'sort_dir'];
        if (!shouldKeepTargetReadFilter) {
          keysToSync.unshift('read_filter');
        }
        for (const key of keysToSync) {
          if (currentParams.has(key)) {
            targetUrl.searchParams.set(key, currentParams.get(key) || '');
          }
        }
      } catch (e) {
        // ignore
      }
    }

    

    async function loadScopePanesWithoutFullRefresh(url, pushHistory = true) {
      const token = ++scopePaneRequestToken;

      // Determine if we should inform the server of the user's persisted read_filter
      // via header rather than mutating visible URLs. Also prefer requesting
      // a small chunk in single-pane mode by adding chunk=1 to the request URL.
      let headerReadFilter = null;
      try {
        const u = new URL(url, window.location.origin);
        if (!u.searchParams.has('read_filter')) {
          const savedMode = u.searchParams.get('star_only') === '1';
          const pref = window.localStorage.getItem(savedMode ? 'lectio-read-filter-saved' : 'lectio-read-filter');
          if (pref) {
            // don't rewrite the visible URL; instead send as header
            headerReadFilter = pref;
          }
        }
        try {
          if (window.isSingleMode && window.isSingleMode()) {
            if (!u.searchParams.has('chunk')) {
              u.searchParams.set('chunk', '1');
              url = u.toString();
            }
          }
        } catch (e) {
          // ignore
        }
      } catch (e) {
        // ignore malformed urls
      }

      try {
        const headers = {
          'X-Requested-With': 'lectio-scope-panes',
        };
        if (headerReadFilter) headers['X-Lectio-Read-Filter'] = headerReadFilter;
        const response = await fetch(url, {
          headers,
          credentials: 'same-origin',
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const htmlText = await response.text();
        if (token !== scopePaneRequestToken) {
          return;
        }

        const parser = new DOMParser();
        const doc = parser.parseFromString(htmlText, 'text/html');
        const nextPostsPane = doc.querySelector('.pane-posts');
        const nextEntryPane = doc.querySelector('.pane-entry');
        const currentPostsPane = document.querySelector('.pane-posts');
        const currentEntryPane = document.querySelector('.pane-entry');
        if (!nextPostsPane || !nextEntryPane || !currentPostsPane || !currentEntryPane) {
          window.location.href = url;
          return;
        }

        // If this request asked for a specific `chunk` and the caller doesn't
        // want a history navigation (incremental load), append the new
        // `.post-item` elements into the existing posts pane instead of
        // replacing the whole pane. This preserves scroll and shows new items.
        let maybeUrl;
        try {
          maybeUrl = new URL(url, window.location.origin);
        } catch (e) {
          maybeUrl = null;
        }

        if (maybeUrl && maybeUrl.searchParams.has('chunk') && maybeUrl.searchParams.has('chunk_delta') && pushHistory === false) {
          const appended = Array.from(nextPostsPane.querySelectorAll('.post-item'));
          if (appended.length === 0) {
            // nothing new; avoid replacing pane which would scroll to top
            updateScopeActiveState(url);
            return;
          }

          // .posts is the actual item container and the element setupPostChunks
          // queries. Append there so getPostItems() finds new items and the
          // chunk counter advances correctly. In single-pane mode .pane-posts
          // scrolls, so preserve scroll on the right element.
          const postsInnerEl = currentPostsPane.querySelector('.posts') || currentPostsPane;
          const scrollingEl = (window.isSingleMode && window.isSingleMode()) ? currentPostsPane : postsInnerEl;
          const prevScroll = scrollingEl.scrollTop;

          // Avoid appending duplicates: only append items whose entry-id
          // isn't already present in the current posts pane.
          const existingIds = new Set(
            Array.from(currentPostsPane.querySelectorAll('.post-item')).map((n) => n.getAttribute('data-post-entry-id'))
          );

          for (const item of appended) {
            const id = item.getAttribute && item.getAttribute('data-post-entry-id');
            if (id && existingIds.has(id)) {
              // skip duplicates
              continue;
            }
            // Move the node from the parsed doc into the live .posts container
            postsInnerEl.appendChild(item);
          }

          // Move/replace the sentinel so it remains at the end
          try {
            const nextSentinel = nextPostsPane.querySelector('#posts-chunk-sentinel');
            const existingSentinel = postsInnerEl.querySelector('#posts-chunk-sentinel');
            if (nextSentinel) {
              if (existingSentinel) existingSentinel.remove();
              postsInnerEl.appendChild(nextSentinel);
            }
          } catch (e) {
            // ignore
          }

          // Apply timestamps and bind interactions for newly appended items
          applyLocalTimestamps(currentPostsPane);
          applyRelativeTimestamps(currentPostsPane);
          applyAbsoluteTimestamps(currentPostsPane);
          measureAndSetTileHeight();
          bindPostListInteractions();

          // Unhide the next chunk of items (keep client chunking behavior)
          try {
            const chunkSize = Number.parseInt(postsInnerEl.getAttribute('data-chunk-size') || '10', 10) || 10;
            const currentlyVisible = postsInnerEl.querySelectorAll('.post-item:not(.post-item-hidden)').length;
            const items = Array.from(postsInnerEl.querySelectorAll('.post-item'));
            const nextVisible = Math.min(items.length, currentlyVisible + chunkSize);
            for (let i = currentlyVisible; i < nextVisible; i++) {
              items[i].classList.remove('post-item-hidden');
            }
          } catch (e) {
            // ignore
          }

          // restore scroll position so the viewport doesn't jump
          scrollingEl.scrollTop = prevScroll;
          updateScopeActiveState(url);
          return;
        }

        // default behavior: replace whole panes
        currentPostsPane.replaceWith(nextPostsPane);
        currentEntryPane.replaceWith(nextEntryPane);
        activeScopeUrl = normalizeScopeUrl(url);
        // Update client-side sort state from server-rendered hidden input if present
        try {
          const sortInput = doc.querySelector('input[name="sort_by"]');
          if (sortInput && sortInput.value) {
            window.CURRENT_SORT_BY = sortInput.value;
          }
        } catch (e) {
          // ignore
        }
        applyLocalTimestamps(nextPostsPane);
        applyRelativeTimestamps(nextPostsPane);
        applyAbsoluteTimestamps(nextEntryPane);
        applyLocalTimestamps(nextEntryPane);
        measureAndSetTileHeight();
        refreshEntryPaneRefs();
        bindEntryPaneInteractions();
        bindEntryTagInteractions();
        bindPostListInteractions();
        if (typeof applyHighlights === 'function') applyHighlights();
        if (typeof window.bindSwipeGestures === 'function') {
          window.bindSwipeGestures();
        }
        setupPostChunks();
        if (typeof window.bindSinglePanePullToRefresh === 'function') {
          window.bindSinglePanePullToRefresh();
        }
        centerActivePostInView();
        updateScopeActiveState(url);

        if (pushHistory) {
          history.pushState({ lectioScopePane: true, lectioPaneLevel: (window.isSingleMode && window.isSingleMode()) ? 1 : 0 }, '', url);
        }
      } catch (_error) {
        window.location.href = url;
        throw _error;
      }
    }

    // --- YouTube "Add to playlist" embed enhancement ---------------------
    let _ytPlaylistsCache = null;        // null = not fetched; {connected, playlists, error}
    let _ytPlaylistsPromise = null;
    let _ytFolderName = '';              // populated from settings; used for empty-folder sync detection
    let _ytAccountFeaturesEnabled = !!window.YT_EMBED_ACCOUNT_FEATURES; // yt_embed_account_features setting; bootstrapped so embeds enhance on first load

    async function _ytFetchPlaylists(force = false) {
      if (!force && _ytPlaylistsCache) return _ytPlaylistsCache;
      if (_ytPlaylistsPromise) return _ytPlaylistsPromise;
      _ytPlaylistsPromise = (async () => {
        try {
          const r = await fetch('/api/youtube/playlists', { credentials: 'same-origin' });
          const d = await r.json().catch(() => ({}));
          _ytPlaylistsCache = d || { connected: false, playlists: [] };
        } catch {
          _ytPlaylistsCache = { connected: false, playlists: [], error: 'network' };
        }
        _ytPlaylistsPromise = null;
        return _ytPlaylistsCache;
      })();
      return _ytPlaylistsPromise;
    }

    function _ytVideoIdFromIframe(iframe) {
      const src = iframe.getAttribute('src') || '';
      const m = src.match(/youtube(?:-nocookie)?\.com\/embed\/([A-Za-z0-9_-]{11})/);
      return m ? m[1] : '';
    }

    async function _ytAddToPlaylist(videoId, { playlistId = '', newTitle = '' } = {}) {
      const r = await fetch('/api/youtube/playlists/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ video_id: videoId, playlist_id: playlistId, new_title: newTitle }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) {
        const err = d.error || `HTTP ${r.status}`;
        if (err === 'quota') throw new Error('Daily YouTube quota reached — try again tomorrow or add it on youtube.com.');
        if (err === 'not_connected') throw new Error('YouTube account not connected.');
        throw new Error(err);
      }
      // A new playlist changes the list — invalidate the cache.
      if (newTitle) _ytPlaylistsCache = null;
      return d;
    }

    async function _ytOpenPicker(videoId, menu, statusEl) {
      menu.innerHTML = '<div class="lectio-yt-pl-loading">Loading playlists…</div>';
      const data = await _ytFetchPlaylists();
      if (!data.connected) {
        menu.innerHTML = '<a class="lectio-yt-pl-item" href="/integrations/youtube/oauth/connect">Connect YouTube account…</a>';
        return;
      }
      if (data.error === 'quota') {
        menu.innerHTML = '<div class="lectio-yt-pl-loading">Daily quota reached.</div>';
        return;
      }
      menu.innerHTML = '';
      (data.playlists || []).forEach(pl => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'lectio-yt-pl-item';
        item.textContent = pl.title + (pl.count != null ? ` (${pl.count})` : '');
        item.addEventListener('click', async () => {
          statusEl.textContent = 'Adding…';
          menu.hidden = true;
          try {
            await _ytAddToPlaylist(videoId, { playlistId: pl.id });
            statusEl.textContent = `Added to ${pl.title}.`;
          } catch (e) { statusEl.textContent = e.message; }
        });
        menu.appendChild(item);
      });
      const newItem = document.createElement('button');
      newItem.type = 'button';
      newItem.className = 'lectio-yt-pl-item lectio-yt-pl-new';
      newItem.textContent = 'New playlist…';
      newItem.addEventListener('click', async () => {
        const title = window.prompt('New playlist name:');
        if (!title) return;
        statusEl.textContent = 'Creating…';
        menu.hidden = true;
        try {
          await _ytAddToPlaylist(videoId, { newTitle: title.trim() });
          statusEl.textContent = `Added to ${title.trim()}.`;
        } catch (e) { statusEl.textContent = e.message; }
      });
      menu.appendChild(newItem);
    }

    function enhanceYoutubeEmbeds(root) {
      if (!root) return;
      const iframes = root.querySelectorAll('iframe[src*="/embed/"]');
      iframes.forEach(iframe => {
        const videoId = _ytVideoIdFromIframe(iframe);
        if (!videoId) return;
        // Avoid double-injecting if the pane is re-enhanced.
        if (iframe.dataset.ytEnhanced === '1') return;
        iframe.dataset.ytEnhanced = '1';

        const bar = document.createElement('div');
        bar.className = 'lectio-yt-actions';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'lectio-yt-pl-btn';
        btn.textContent = '+ Add to playlist ▾';
        const menu = document.createElement('div');
        menu.className = 'lectio-yt-pl-menu';
        menu.hidden = true;
        const status = document.createElement('span');
        status.className = 'lectio-yt-pl-status';

        // Position the (fixed) menu next to the button so it escapes the
        // article's overflow:auto clipping; flip up if it'd run off-screen.
        const positionMenu = () => {
          const r = btn.getBoundingClientRect();
          menu.style.left = `${Math.round(r.left)}px`;
          const spaceBelow = window.innerHeight - r.bottom;
          const wantUp = spaceBelow < 260 && r.top > spaceBelow;
          if (wantUp) {
            menu.style.top = 'auto';
            menu.style.bottom = `${Math.round(window.innerHeight - r.top + 4)}px`;
            menu.style.maxHeight = `${Math.round(r.top - 12)}px`;
          } else {
            menu.style.bottom = 'auto';
            menu.style.top = `${Math.round(r.bottom + 4)}px`;
            menu.style.maxHeight = `${Math.round(spaceBelow - 12)}px`;
          }
        };
        btn.addEventListener('click', async () => {
          if (!menu.hidden) { menu.hidden = true; return; }
          menu.hidden = false;
          positionMenu();
          await _ytOpenPicker(videoId, menu, status);
          positionMenu();  // re-place after content sets its height
        });
        // Close on outside click; close on scroll/resize (fixed menu would drift).
        document.addEventListener('click', (e) => {
          if (!bar.contains(e.target)) menu.hidden = true;
        });
        window.addEventListener('resize', () => { menu.hidden = true; });
        document.addEventListener('scroll', () => { if (!menu.hidden) menu.hidden = true; }, true);

        bar.appendChild(btn);
        bar.appendChild(menu);
        bar.appendChild(status);
        // Place the control directly after the iframe (or its wrapper <p>).
        const anchor = iframe.closest('p.lectio-embed') || iframe;
        anchor.insertAdjacentElement('afterend', bar);
      });
    }

    async function loadEntryPaneWithoutFullRefresh(url, pushHistory = true) {
      const token = ++entryPaneRequestToken;
      let currentUrlHasEntry = false;
      let paneSwapped = false;

      try {
        const currentUrl = new URL(window.location.href, window.location.origin);
        currentUrlHasEntry = currentUrl.searchParams.has('feed_url') && currentUrl.searchParams.has('entry_id');
      } catch (_e) {
        currentUrlHasEntry = false;
      }

      try {
        const headers = { 'X-Requested-With': 'lectio-entry-pane' };
        const requestUrl = new URL(url, window.location.origin);
        requestUrl.pathname = '/entries/pane';
        try {
          if (!requestUrl.searchParams.has('read_filter')) {
            const savedMode = requestUrl.searchParams.get('star_only') === '1';
            const pref = window.localStorage.getItem(savedMode ? 'lectio-read-filter-saved' : 'lectio-read-filter');
            if (pref) headers['X-Lectio-Read-Filter'] = pref;
          }
        } catch (e) {
          // ignore
        }

        // One retry after a short pause before the full-reload fallback —
        // rides out server restarts (deploys) and transient network blips.
        let response = null;
        for (let attempt = 0; attempt < 2; attempt++) {
          try {
            response = await fetch(requestUrl.toString(), { headers, credentials: 'same-origin' });
            if (response.ok) break;
          } catch (fetchError) {
            response = null;
            if (attempt === 1) throw fetchError;
          }
          if (attempt === 0) await new Promise((r) => setTimeout(r, 1500));
        }
        if (!response || !response.ok) {
          throw new Error(`HTTP ${response ? response.status : 'failed'}`);
        }

        const htmlText = await response.text();
        if (token !== entryPaneRequestToken) {
          return;
        }

        const parser = new DOMParser();
        const doc = parser.parseFromString(htmlText, 'text/html');
        const nextPane = doc.querySelector('.pane-entry');
        const currentPane = document.querySelector('.pane-entry');
        if (!nextPane || !currentPane) {
          window.location.href = url;
          return;
        }

        currentPane.replaceWith(nextPane);
        paneSwapped = true;
        // Each post-swap step runs isolated: one throwing binder must not
        // skip the rest (an unbound post list makes the NEXT click a raw
        // link navigation — the "random full refresh while browsing" bug).
        for (const step of [
          () => applyLocalTimestamps(nextPane),
          () => applyRelativeTimestamps(nextPane),
          () => applyAbsoluteTimestamps(nextPane),
          () => measureAndSetTileHeight(),
          () => refreshEntryPaneRefs(),
          () => bindEntryPaneInteractions(),
          () => bindEntryTagInteractions(),
          () => bindPostListInteractions(),
          () => { if (_ytAccountFeaturesEnabled) enhanceYoutubeEmbeds(nextPane); },
          () => { if (typeof window.bindSwipeGestures === 'function') window.bindSwipeGestures(); },
          () => { if (typeof applyHighlights === 'function') applyHighlights(); },
          () => markActivePostByUrl(url),
          () => syncActivePostThumbnailFromEntryPane(),
          () => centerActivePostInView(),
        ]) {
          try {
            step();
          } catch (stepError) {
            console.error('[lectio] entry-pane post-swap step failed (continuing):', stepError);
          }
        }
        // If running in single-pane mode, ensure we switch to the entry pane
        try {
          if (window.isSingleMode && window.isSingleMode()) {
            if (window.setSinglePaneLevel) {
              window.setSinglePaneLevel(2);
            }
          }
        } catch (e) {
          // ignore
        }
        if (pushHistory) {
          const isSinglePaneMode = Boolean(window.isSingleMode && window.isSingleMode());
          const nextState = { lectioEntryPane: true, lectioPaneLevel: isSinglePaneMode ? 2 : 0 };
          // In 1-pane mode, keep only one "entry" history state so browser back
          // returns to posts list instead of stepping through prior entry swipes.
          if (isSinglePaneMode && currentUrlHasEntry) {
            history.replaceState(nextState, '', url);
          } else {
            history.pushState(nextState, '', url);
          }
        }
      } catch (_error) {
        // Full-page fallback ONLY when the pane never made it in (fetch/parse
        // failed). Once the swap has happened the content is on screen — an
        // exception in the post-swap enhancement/binder pipeline must not
        // throw it away with a hard reload (this was the random "app
        // refreshes while browsing articles" bug: entry-specific content
        // tripping one binder nuked the whole session).
        if (!paneSwapped) {
          window.location.href = url;
          throw _error;
        }
        console.error('[lectio] entry-pane post-swap enhancement failed (pane content is fine):', _error);
      }
    }

    function syncActivePostThumbnailFromEntryPane() {
      const title = document.querySelector('.entry-pane-title');
      if (!(title instanceof HTMLElement)) {
        return;
      }

      const feedUrl = title.getAttribute('data-post-feed-url') || '';
      const entryId = title.getAttribute('data-post-entry-id') || '';
      if (!feedUrl || !entryId) {
        return;
      }

      const postItem = document.querySelector(
        `.post-item[data-post-feed-url="${CSS.escape(feedUrl)}"][data-post-entry-id="${CSS.escape(entryId)}"]`
      );
      if (!(postItem instanceof HTMLElement)) {
        return;
      }

      const thumbnail = postItem.querySelector('.post-thumbnail');
      if (!(thumbnail instanceof HTMLElement)) {
        return;
      }

      const leadImageUrl = (title.getAttribute('data-post-lead-image-url') || '').trim();
      const existingImage = thumbnail.querySelector('.post-thumbnail-image');

      if (!leadImageUrl) {
        // If there is no lead image for the entry (for example we suppressed
        // the video thumbnail when injecting a YouTube player), do not
        // remove or clear the thumbnail shown in the posts list — keep the
        // list thumbnail generated from `post.thumbnail_url` intact.
        return;
      }

      // When the feed uses a per-entry inline thumbnail strategy, the lead image
      // (typically og_scrape) must not overwrite the inline thumbnail in the list.
      if (postItem.dataset.thumbStrategy === 'inline') return;

      // /thumb requires an absolute URL. When the server proxied the image through
      // /api/img (CORP-restricted domains), leadImageUrl is a relative path like
      // "/api/img?u=...". Passing that to /thumb?url= would produce a 400.
      // Leave the existing list thumbnail in place instead.
      if (!leadImageUrl.startsWith('http://') && !leadImageUrl.startsWith('https://')) {
        return;
      }

      const _thumbCrop = postItem.dataset.thumbCrop || 'cover';
      const _smartMs = postItem.dataset.smartMs || '';
      const _fillZoom = postItem.dataset.fillZoom || '';
      const thumbedLeadUrl = '/thumb?url=' + encodeURIComponent(leadImageUrl) + '&crop=' + encodeURIComponent(_thumbCrop)
        + (_thumbCrop === 'smart' && _smartMs ? '&ms=' + encodeURIComponent(_smartMs) : '')
        + (_thumbCrop !== 'smart' && _thumbCrop !== 'contain' && _fillZoom && parseFloat(_fillZoom) !== 1.0 ? '&fz=' + encodeURIComponent(_fillZoom) : '');
      if (existingImage instanceof HTMLImageElement) {
        if (existingImage.getAttribute('src') !== thumbedLeadUrl) {
          const prevSrc = existingImage.getAttribute('src');
          existingImage.onerror = () => {
            // /thumb failed (remote 4xx/5xx or network). Restore the previous
            // thumbnail rather than leaving a broken-image icon in the list.
            if (prevSrc) {
              existingImage.setAttribute('src', prevSrc);
              existingImage.style.display = '';
            } else {
              thumbnail.classList.add('is-empty');
              existingImage.style.display = 'none';
            }
          };
          existingImage.setAttribute('src', thumbedLeadUrl);
          if (_thumbCrop === 'smart') thumbnail.style.backgroundImage = `url('${setThumbCropParam(thumbedLeadUrl, 'cover')}')`;
        }
        existingImage.style.display = '';
      } else {
        const image = document.createElement('img');
        image.className = 'post-thumbnail-image';
        image.alt = '';
        image.dataset.direct = leadImageUrl;  // /thumb fail → load the image direct
        image.src = thumbedLeadUrl;
        image.onerror = () => window.thumbImgFallback(image);
        thumbnail.insertBefore(image, thumbnail.firstChild);
      }

      thumbnail.classList.remove('is-empty');
    }

    function setSourceModeIndicator(mode) {
      if (!entrySourceModeIndicator) {
        return;
      }

      if (!mode) {
        entrySourceModeIndicator.setAttribute('hidden', '');
        entrySourceModeIndicator.textContent = '';
        return;
      }

      const labels = {
        source: 'Direct',
        'source-proxy': 'Proxy',
        readability: 'Readability',
      };
      entrySourceModeIndicator.textContent = labels[mode] || mode;
      entrySourceModeIndicator.removeAttribute('hidden');
    }

    function scheduleSourceLoadTimeout(timeoutMs) {
      if (sourceLoadTimeoutId) {
        window.clearTimeout(sourceLoadTimeoutId);
      }
      sourceLoadTimeoutId = window.setTimeout(() => {
        if (!sourceViewActive || sourceFrameLoaded) {
          return;
        }

        if (sourceViewMode === 'source' && !sourceFallbackAttempted) {
          fallbackToProxiedSource();
          return;
        }

        setEntrySourceFallbackVisible(true);
      }, timeoutMs);
    }

    function fallbackToProxiedSource() {
      if (!sourceViewActive || sourceViewMode !== 'source' || sourceFallbackAttempted || sourceDirectLoaded || !sourceViewUrl || !entrySourceFrame) {
        return;
      }

      sourceFallbackAttempted = true;
      sourceViewMode = 'source-proxy';
      setSourceModeIndicator(sourceViewMode);
      sourceFrameLoaded = false;
      fetchAndInjectProxy(sourceViewUrl);
    }

    // Fetch the proxied page via JS (with session credentials) and inject it into the
    // iframe via srcdoc. This avoids cookie/Sec-Fetch-Dest issues that arise when
    // setting iframe.src directly to a same-origin URL.
    function fetchAndInjectProxy(entryUrl) {
      scheduleSourceLoadTimeout(12000);
      const proxyUrl = `/entries/source?url=${encodeURIComponent(entryUrl)}`;
      fetch(proxyUrl, { credentials: 'same-origin' })
        .then((r) => (r.ok ? r.text() : Promise.reject(`HTTP ${r.status}`)))
        .then((html) => {
          if (!sourceViewActive || sourceViewMode !== 'source-proxy' || !entrySourceFrame) return;
          entrySourceFrame.removeAttribute('src');
          entrySourceFrame.srcdoc = html;
        })
        .catch(() => {
          if (!sourceViewActive) return;
          setEntrySourceFallbackVisible(true);
        });
    }

    function setEntrySourceFallbackVisible(visible) {
      if (!entrySourceFallback) {
        return;
      }
      if (visible) {
        entrySourceFallback.removeAttribute('hidden');
      } else {
        entrySourceFallback.setAttribute('hidden', '');
      }
    }

    function showContextMenu(event) {
      if (!contextMenu) {
        return;
      }
      hideRootContextMenu();
      hidePostContextMenu();
      positionMenuInViewport(contextMenu, event.clientX, event.clientY);
      contextMenu.removeAttribute('hidden');
    }

    function showRootContextMenu(event) {
      if (!rootContextMenu) {
        return;
      }
      hideContextMenu();
      hidePostContextMenu();
      positionMenuInViewport(rootContextMenu, event.clientX, event.clientY);
      rootContextMenu.removeAttribute('hidden');
    }

    function showPostContextMenu(event) {
      if (!postContextMenu) {
        return;
      }
      hideContextMenu();
      hideRootContextMenu();
      positionMenuInViewport(postContextMenu, event.clientX, event.clientY);
      postContextMenu.removeAttribute('hidden');
    }

    function positionMenuInViewport(menu, x, y) {
      const viewportPadding = 8;
      menu.style.left = '0px';
      menu.style.top = '0px';
      menu.removeAttribute('hidden');
      const rect = menu.getBoundingClientRect();
      const maxLeft = window.innerWidth - rect.width - viewportPadding;
      const maxTop = window.innerHeight - rect.height - viewportPadding;
      const clampedLeft = Math.max(viewportPadding, Math.min(x, maxLeft));
      const clampedTop = Math.max(viewportPadding, Math.min(y, maxTop));
      menu.style.left = `${clampedLeft}px`;
      menu.style.top = `${clampedTop}px`;
      menu.setAttribute('hidden', '');
    }

    async function copyTextToClipboard(text) {
      if (!text) {
        return false;
      }

      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch (_error) {
        // Fall back to execCommand path below.
      }

      try {
        const helper = document.createElement('textarea');
        helper.value = text;
        helper.setAttribute('readonly', '');
        helper.style.position = 'fixed';
        helper.style.opacity = '0';
        document.body.appendChild(helper);
        helper.select();
        const copied = document.execCommand('copy');
        helper.remove();
        return copied;
      } catch (_error) {
        return false;
      }
    }

    function hideFolderSubmenu() {
      if (!folderSubmenu) {
        return;
      }
      folderSubmenu.setAttribute('hidden', '');
      folderSubmenu.style.removeProperty('left');
      folderSubmenu.style.removeProperty('top');
    }

    function positionFolderSubmenuInViewport() {
      if (!folderSubmenu || !addToFolderButton) {
        return;
      }
      const viewportPadding = 8;
      const submenuRect = folderSubmenu.getBoundingClientRect();
      const buttonRect = addToFolderButton.getBoundingClientRect();

      let left = buttonRect.right + 6;
      if (left + submenuRect.width > window.innerWidth - viewportPadding) {
        left = buttonRect.left - submenuRect.width - 6;
      }
      left = Math.max(viewportPadding, Math.min(left, window.innerWidth - submenuRect.width - viewportPadding));

      let top = buttonRect.top - 4;
      top = Math.max(viewportPadding, Math.min(top, window.innerHeight - submenuRect.height - viewportPadding));

      folderSubmenu.style.left = `${left}px`;
      folderSubmenu.style.top = `${top}px`;
    }

    function showFolderSubmenu() {
      if (!folderSubmenu) {
        return;
      }
      folderSubmenu.removeAttribute('hidden');
      positionFolderSubmenuInViewport();
    }

    function updateFolderSubmenuOptions() {
      if (!folderSubmenu || !contextFolderId) {
        return;
      }

      let hasVisibleOption = false;
      for (const option of folderSubmenu.querySelectorAll('.context-submenu-item')) {
        const optionFolderId = option.getAttribute('data-target-folder-id');
        const isCurrentFolder = optionFolderId === contextFolderId;
        option.hidden = isCurrentFolder;
        if (!isCurrentFolder) {
          hasVisibleOption = true;
        }
      }

      if (addToFolderButton) {
        addToFolderButton.disabled = !hasVisibleOption;
      }
    }

    function hideContextMenu() {
      if (!contextMenu) {
        return;
      }
      contextMenu.setAttribute('hidden', '');
      hideFolderSubmenu();
    }

    function hidePostContextMenu() {
      if (!postContextMenu) {
        return;
      }
      postContextMenu.setAttribute('hidden', '');
    }

    function hideRootContextMenu() {
      if (!rootContextMenu) {
        return;
      }
      rootContextMenu.setAttribute('hidden', '');
    }

    function showTagContextMenu(event) {
      if (!tagContextMenu) {
        return;
      }
      hideContextMenu();
      hideRootContextMenu();
      hidePostContextMenu();
      positionMenuInViewport(tagContextMenu, event.clientX, event.clientY);
      tagContextMenu.removeAttribute('hidden');
    }

    function hideTagContextMenu() {
      if (!tagContextMenu) {
        return;
      }
      tagContextMenu.setAttribute('hidden', '');
    }

    function setMenuItemVisible(item, visible) {
      if (!item) {
        return;
      }
      if (visible) {
        item.removeAttribute('hidden');
        item.style.removeProperty('display');
      } else {
        item.setAttribute('hidden', '');
        item.style.display = 'none';
      }
    }

    function hideAllContextMenus() {
      hideContextMenu();
      hidePostContextMenu();
      hideRootContextMenu();
      hideTagContextMenu();
      hideShareMenu();
      _closeQuireMenu();
    }

    function hideShareMenu() {
      const menu = document.getElementById('entry-share-menu');
      if (menu) menu.setAttribute('hidden', '');
      document.getElementById('entry-share-btn')?.setAttribute('aria-expanded', 'false');
    }

    // Anchor the (fixed) share menu to its button so it escapes the entry pane's
    // overflow:hidden; right-align to the button and flip up only if it'd run off
    // the bottom of the viewport.
    function positionShareMenu(menu, btn) {
      const r = btn.getBoundingClientRect();
      menu.style.right = `${Math.round(window.innerWidth - r.right)}px`;
      menu.style.left = 'auto';
      const spaceBelow = window.innerHeight - r.bottom;
      const wantUp = spaceBelow < 260 && r.top > spaceBelow;
      if (wantUp) {
        menu.style.top = 'auto';
        menu.style.bottom = `${Math.round(window.innerHeight - r.top + 6)}px`;
        menu.style.maxHeight = `${Math.round(r.top - 12)}px`;
      } else {
        menu.style.bottom = 'auto';
        menu.style.top = `${Math.round(r.bottom + 6)}px`;
        menu.style.maxHeight = `${Math.round(spaceBelow - 12)}px`;
      }
    }

    document.addEventListener('click', (event) => {
      if (!(event.target instanceof Element)) return;
      const shareBtn = event.target.closest('#entry-share-btn');
      if (shareBtn) {
        event.stopPropagation();
        const menu = document.getElementById('entry-share-menu');
        if (!menu) return;
        const isOpen = !menu.hasAttribute('hidden');
        if (isOpen) {
          hideShareMenu();
        } else {
          _closePinMenu();
          menu.removeAttribute('hidden');
          positionShareMenu(menu, shareBtn);
          shareBtn.setAttribute('aria-expanded', 'true');
        }
        return;
      }
      // Close share menu on any click outside
      const menu = document.getElementById('entry-share-menu');
      if (menu && !menu.hasAttribute('hidden') && !event.target.closest('.entry-share-wrap')) {
        hideShareMenu();
      }
    }, true);

    // Close share menu when an active item is clicked (before action handlers fire)
    document.addEventListener('click', (event) => {
      if (!(event.target instanceof Element)) return;
      const item = event.target.closest('.share-menu-item:not(:disabled)');
      if (item && item.closest('.entry-share-menu')) {
        hideShareMenu();
      }
    }, true);

    function closeFeedPropertiesModal() {
      feedPropertiesModal?.setAttribute('hidden', '');
    }

    function setFeedPropText(node, value) {
      if (!node) {
        return;
      }
      node.textContent = value && String(value).trim() ? String(value) : '-';
    }

    async function openFeedPropertiesModal(feedUrl) {
      if (!feedUrl || !feedPropertiesModal) {
        return;
      }

      if (feedPropUserTitle) { feedPropUserTitle.value = ''; feedPropUserTitle.placeholder = 'Loading...'; feedPropUserTitle.dataset.feedUrl = feedUrl; }
      if (feedPropResetTitleBtn) feedPropResetTitleBtn.hidden = true;
      setFeedPropText(feedPropRealTitle, '-');
      setFeedPropText(feedPropWebsite, '-');
      if (feedPropWebsiteOpen) feedPropWebsiteOpen.hidden = true;
      setFeedPropText(feedPropXml, feedUrl);
      if (feedPropXmlOpen) { feedPropXmlOpen.href = safeHttpUrl(feedUrl) || '#'; feedPropXmlOpen.hidden = !feedUrl; }
      setFeedPropText(feedPropHealth, '-');
      setFeedPropText(feedPropHealthDetail, '-');
      setFeedPropText(feedPropTotal, '-');
      setFeedPropText(feedPropUnread, '-');
      setFeedPropText(feedPropAdded, '-');
      setFeedPropText(feedPropUpdated, '-');
      setFeedPropText(feedPropReceived, '-');
      setFeedPropText(feedPropLastPost, '-');
      if (feedPropFolderSelect) { feedPropFolderSelect.value = '-1'; feedPropFolderSelect.dataset.currentFolderId = '-1'; feedPropFolderSelect.dataset.feedUrl = feedUrl; }
      if (feedPropFolderStatus) feedPropFolderStatus.textContent = '';
      if (feedPropStrategy) feedPropStrategy.value = 'auto';
      if (feedPropStrategyHint) feedPropStrategyHint.textContent = '';
      setActivePreset('');
      if (feedPropShowInArticle) feedPropShowInArticle.checked = true;
      if (feedPropHideShorts) feedPropHideShorts.checked = false;
      const _thumbSourceSelect = document.getElementById('feed-prop-thumb-source');
      if (_thumbSourceSelect) { _thumbSourceSelect.value = ''; _thumbSourceSelect.dataset.feedUrl = feedUrl; delete _thumbSourceSelect.dataset.savedThumbUrl; }
      const _thumbCustomRow = document.getElementById('feed-prop-thumb-custom-row');
      if (_thumbCustomRow) _thumbCustomRow.style.display = 'none';
      const _thumbPreview = document.getElementById('feed-prop-thumb-preview');
      if (_thumbPreview) _thumbPreview.style.display = 'none';
      const _thumbInput = document.getElementById('feed-prop-thumbnail-url');
      if (_thumbInput) _thumbInput.value = '';
      // YT-ness is knowable from the URL alone, so set the tuning section now —
      // don't wait on (or depend on) the /feeds/properties fetch, which on a hiccup
      // would otherwise leave a YouTube feed showing the full (non-YT) tuning page.
      const _isYoutubeFeedUrl = feedUrl.includes('youtube.com/feeds/videos.xml');
      if (feedPropYtSection) feedPropYtSection.hidden = !_isYoutubeFeedUrl;
      if (feedPropImgSection) feedPropImgSection.hidden = _isYoutubeFeedUrl;
      if (feedPropDevSection) feedPropDevSection.hidden = true;
      if (feedPropFlushBatchStatus) feedPropFlushBatchStatus.textContent = '';
      if (feedPropUnsubscribeBtn) {
        feedPropUnsubscribeBtn.setAttribute('hidden', '');
        // Re-enable: the success path leaves it disabled, so without this a
        // second open in the same page session has a dead (no-click) button.
        feedPropUnsubscribeBtn.disabled = false;
      }
      const activePost = document.querySelector('.post-item.active');
      const activeEntryId = (activePost?.getAttribute('data-post-feed-url') === feedUrl)
        ? (activePost?.getAttribute('data-post-entry-id') || '')
        : '';
      if (feedPropRefreshBtn) {
        feedPropRefreshBtn.dataset.feedUrl = feedUrl;
        feedPropRefreshBtn.dataset.entryId = activeEntryId;
      }
      renderStrategyGrid([]);
      // Reset to Info tab
      document.querySelectorAll('[data-feed-prop-tab]').forEach(b => {
        const isInfo = b.getAttribute('data-feed-prop-tab') === 'info';
        b.classList.toggle('hl-tab-btn--active', isInfo);
        b.setAttribute('aria-selected', isInfo ? 'true' : 'false');
      });
      if (feedPropTabInfo) feedPropTabInfo.hidden = false;
      if (feedPropTabTuning) feedPropTabTuning.hidden = true;
      if (feedPropTabHistory) feedPropTabHistory.hidden = true;
      if (feedPropTabAutomations) feedPropTabAutomations.hidden = true;
      feedPropertiesModal.removeAttribute('hidden');

      try {
        const response = await fetch(`/feeds/properties?feed_url=${encodeURIComponent(feedUrl)}`);
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();
        if (!data.found) {
          setFeedPropText(feedPropRealTitle, feedUrl);
          setFeedPropText(feedPropHealth, 'error');
          setFeedPropText(feedPropHealthDetail, data.error || 'Feed not found.');
          return;
        }

        if (feedPropUserTitle) {
          feedPropUserTitle.value = data.user_title || '';
          feedPropUserTitle.placeholder = data.real_title || data.feed_url || feedUrl;
          feedPropUserTitle.dataset.feedUrl = feedUrl;
        }
        if (feedPropResetTitleBtn) feedPropResetTitleBtn.hidden = !data.user_title;
        setFeedPropText(feedPropRealTitle, data.real_title || data.feed_url || feedUrl);
        setFeedPropText(feedPropWebsite, data.website || '-');
        if (feedPropWebsiteOpen) {
          const ws = (data.website || '').trim();
          feedPropWebsiteOpen.href = ws || '#';
          feedPropWebsiteOpen.hidden = !ws;
        }
        setFeedPropText(feedPropXml, data.feed_url || feedUrl);
        if (feedPropXmlOpen) {
          const xu = (data.feed_url || feedUrl || '').trim();
          feedPropXmlOpen.href = safeHttpUrl(xu) || '#';
          feedPropXmlOpen.hidden = !xu;
        }
        if (feedPropChangeUrlWrap) feedPropChangeUrlWrap.hidden = true;
        if (feedPropChangeUrlInput) feedPropChangeUrlInput.value = '';
        if (feedPropChangeUrlStatus) feedPropChangeUrlStatus.textContent = '';
        {
          const updatesEnabled = data.updates_enabled !== false;
          if (feedPropUpdatesLabel) feedPropUpdatesLabel.textContent = updatesEnabled ? 'Active' : 'Disabled';
          if (feedPropDisableBtn) {
            feedPropDisableBtn.textContent = updatesEnabled ? 'Disable feed' : 'Enable feed';
            feedPropDisableBtn.dataset.updatesEnabled = updatesEnabled ? '1' : '0';
          }
          if (feedPropDisableStatus) feedPropDisableStatus.textContent = '';
        }
        setFeedPropText(feedPropHealth, data.health || '-');
        setFeedPropText(feedPropHealthDetail, data.health_detail || '-');
        setFeedPropText(feedPropTotal, data.total_posts);
        setFeedPropText(feedPropUnread, data.unread_posts);
        setFeedPropText(feedPropAdded, data.added);
        setFeedPropText(feedPropUpdated, data.last_updated);
        if (feedPropCooldown && feedPropCooldownLabel) {
          if (data.backoff_active) {
            let cooldownText = `until ${data.backoff_retry_at || '?'}`;
            if (data.backoff_domain_driven) {
              cooldownText += ` — domain-wide (${data.backoff_domain})`;
            }
            if (data.backoff_feed_failures > 0) cooldownText += `, ${data.backoff_feed_failures} feed failure${data.backoff_feed_failures !== 1 ? 's' : ''}`;
            if (data.backoff_domain_failures > 0 && data.backoff_domain_driven) cooldownText += `, ${data.backoff_domain_failures} domain failure${data.backoff_domain_failures !== 1 ? 's' : ''}`;
            setFeedPropText(feedPropCooldown, cooldownText);
            feedPropCooldown.removeAttribute('hidden');
            feedPropCooldownLabel.removeAttribute('hidden');
          } else {
            feedPropCooldown.setAttribute('hidden', '');
            feedPropCooldownLabel.setAttribute('hidden', '');
          }
        }
        setFeedPropText(feedPropReceived, data.last_received);
        setFeedPropText(feedPropLastPost, data.last_post);
        if (feedPropFolderSelect) {
          // Root membership is stored folderless, so treat "no folder id" (and
          // any stray root id) as Uncategorized (-1). Feeds live in one folder.
          const fids = Array.isArray(data.folder_ids) ? data.folder_ids : [];
          const hasOption = (id) => !!feedPropFolderSelect.querySelector(`option[value="${CSS.escape(String(id))}"]`);
          const currentFolderId = fids.find((id) => hasOption(id)) ?? -1;
          feedPropFolderSelect.value = String(currentFolderId);
          feedPropFolderSelect.dataset.feedUrl = feedUrl;
          feedPropFolderSelect.dataset.currentFolderId = String(currentFolderId);
        }
        if (feedPropFolderStatus) feedPropFolderStatus.textContent = '';

        // dev.to filtered feeds: show + fill the filter-config section
        const devtoSection = document.getElementById('feed-prop-devto-section');
        if (devtoSection) {
          devtoSection.hidden = !data.devto;
          if (data.devto) {
            document.getElementById('feed-prop-devto-tag').value = data.devto.tag || '';
            document.getElementById('feed-prop-devto-top').value = data.devto.top_days || '';
            document.getElementById('feed-prop-devto-minreact').value = data.devto.min_reactions || '';
            document.getElementById('feed-prop-devto-exclude').value = data.devto.tags_exclude || '';
            document.getElementById('feed-prop-devto-english').checked = !!data.devto.english_only;
            document.getElementById('feed-prop-devto-save').dataset.devtoFeedId = data.devto_feed_id || '';
            document.getElementById('feed-prop-devto-status').textContent = '';
          }
        }

        // Toggle YouTube vs standard tuning sections
        const isYoutubeFeed = !!data.is_youtube_feed;
        const isDevFeed = feedUrl.includes('/dev/feeds/');
        if (feedPropYtSection) feedPropYtSection.hidden = !isYoutubeFeed;
        if (feedPropImgSection) feedPropImgSection.hidden = isYoutubeFeed;
        if (feedPropDevSection) feedPropDevSection.hidden = !isDevFeed;
        if (feedPropBrowserUaRow) feedPropBrowserUaRow.hidden = !data.browser_ua;
        const feedPropBrowserUaForceRow = document.getElementById('feed-prop-browser-ua-force-row');
        if (feedPropBrowserUaForceRow) feedPropBrowserUaForceRow.hidden = !!data.browser_ua;

        setFeedHistory(data.fetch_history || []);
        renderFeedAutomations(data.automations);

        if (feedPropHideShorts) {
          feedPropHideShorts.checked = !!data.hide_shorts;
          feedPropHideShorts.dataset.feedUrl = feedUrl;
        }
        if (feedPropFlushBatchBtn) feedPropFlushBatchBtn.dataset.feedUrl = feedUrl;
        if (feedPropFlushBatchStatus) feedPropFlushBatchStatus.textContent = '';

        if (!isYoutubeFeed) {
          if (feedPropStrategy) {
            const rawStrategy = data.image_strategy || 'auto';
            const isPreset = PRESET_STRATEGIES.has(rawStrategy);
            setActivePreset(isPreset ? rawStrategy : '');
            feedPropStrategy.value = isPreset ? 'auto' : rawStrategy;
            feedPropStrategy.dataset.feedUrl = feedUrl;
            feedPropPresetBtns.forEach(btn => btn.dataset.feedUrl = feedUrl);
          }
          if (feedPropStrategyHint) {
            const detected = data.image_strategy_detected;
            const rawStrategy = data.image_strategy || 'auto';
            const isAuto = rawStrategy === 'auto' || PRESET_STRATEGIES.has(rawStrategy);
            feedPropStrategyHint.textContent = (isAuto && detected && detected !== 'unknown')
              ? `detected: ${detected}` : '';
          }

          if (feedPropShowInArticle) {
            feedPropShowInArticle.checked = data.show_lead_image_in_article !== false;
            feedPropShowInArticle.dataset.feedUrl = feedUrl;
          }
          if (feedPropInjectSourceImages) {
            feedPropInjectSourceImages.checked = !!data.inject_source_images;
            feedPropInjectSourceImages.dataset.feedUrl = feedUrl;
          }
          if (feedPropCaptionTitle && feedPropCaptionAlt && feedPropCaptionAutoBtn) {
            const src = data.caption_source || 'auto';
            feedPropCaptionTitle.checked = src === 'title' || src === 'both';
            feedPropCaptionAlt.checked   = src === 'alt'   || src === 'both';
            feedPropCaptionAutoBtn.classList.toggle('active', src === 'auto');
            feedPropCaptionTitle.dataset.feedUrl = feedUrl;
            feedPropCaptionAlt.dataset.feedUrl   = feedUrl;
            feedPropCaptionAutoBtn.dataset.feedUrl = feedUrl;
          }
          const feedPropThumbUrlSave = document.getElementById('feed-prop-thumbnail-url-save');
          if (feedPropThumbUrlSave) feedPropThumbUrlSave.dataset.feedUrl = feedUrl;
          const thumbSourceSelect = document.getElementById('feed-prop-thumb-source');
          if (thumbSourceSelect) {
            thumbSourceSelect.dataset.feedUrl = feedUrl;
            thumbSourceSelect.dataset.savedThumbUrl = data.feed_thumbnail_url || '';
          }
          if (feedPropRefreshBtn) feedPropRefreshBtn.dataset.feedUrl = feedUrl;

          // Thumb crop position + mode
          const _cropVal = data.thumb_crop || 'cover';
          document.querySelectorAll('.feed-prop-crop-pos-btn, .feed-prop-crop-mode-btn').forEach(btn => {
            btn.dataset.feedUrl = feedUrl;
          });
          updateCropUI(_cropVal);
          const smartMsInput = document.getElementById('feed-prop-smart-min-scale');
          if (smartMsInput) {
            smartMsInput.dataset.feedUrl = feedUrl;
            smartMsInput.value = data.smart_min_scale != null ? String(data.smart_min_scale) : '';
          }
          const fillZoomInput = document.getElementById('feed-prop-fill-zoom');
          if (fillZoomInput) {
            fillZoomInput.dataset.feedUrl = feedUrl;
            fillZoomInput.value = data.fill_zoom != null ? String(data.fill_zoom) : '';
          }

          const _showThumb = data.show_lead_image_as_thumb !== false;
          if (activeEntryId) {
            doStrategyRefresh(feedUrl, activeEntryId);
            updateThumbSourceSelect(data.strategy_cache || [], data.feed_thumbnail_url || null, _showThumb, data.thumb_strategy || null);
          } else {
            renderStrategyGrid(data.strategy_cache || []);
            updateThumbSourceSelect(data.strategy_cache || [], data.feed_thumbnail_url || null, _showThumb, data.thumb_strategy || null);
          }
        }

        if (feedPropUnsubscribeBtn) {
          // Show Unsubscribe even for a feed in no folder (an "Uncategorized"
          // orphan): folderIds is empty, but the backend still purges the feed
          // from reader once it's used in no folder. Pass folder_id 0 in that case.
          const folderIds = data.folder_ids || [];
          feedPropUnsubscribeBtn.dataset.feedUrl = feedUrl;
          feedPropUnsubscribeBtn.dataset.folderIds = folderIds.join(',');
          feedPropUnsubscribeBtn.removeAttribute('hidden');
        }
      } catch (error) {
        if (feedPropUserTitle) feedPropUserTitle.placeholder = feedUrl;
        setFeedPropText(feedPropHealth, 'error');
        setFeedPropText(feedPropHealthDetail, `Could not load properties: ${error}`);
      }
    }

    async function saveFeedUserTitle(feedUrl, userTitle) {
      const fd = new FormData();
      fd.append('feed_url', feedUrl);
      fd.append('user_title', userTitle);
      try {
        await fetch('/feeds/set-user-title', { method: 'POST', body: fd, credentials: 'same-origin' });
        const displayTitle = userTitle || feedPropUserTitle?.placeholder || feedUrl;
        document.querySelectorAll(`.feed-link[data-feed-url="${CSS.escape(feedUrl)}"]`).forEach(link => {
          const nameSpan = link.querySelector('.feed-label > span:last-of-type');
          if (nameSpan) nameSpan.textContent = displayTitle;
        });
        if (feedPropResetTitleBtn) feedPropResetTitleBtn.hidden = !userTitle;
      } catch (_err) {}
    }

    if (feedPropUserTitle) {
      feedPropUserTitle.addEventListener('focus', () => {
        if (!feedPropUserTitle.value) {
          feedPropUserTitle.value = feedPropUserTitle.placeholder;
          feedPropUserTitle.select();
        }
      });
      feedPropUserTitle.addEventListener('blur', () => {
        const feedUrl = feedPropUserTitle.dataset.feedUrl;
        if (!feedUrl) return;
        const trimmed = feedPropUserTitle.value.trim();
        // If user focused and blurred without changing, clear so it stays as "no override".
        if (trimmed === feedPropUserTitle.placeholder) {
          feedPropUserTitle.value = '';
          return;
        }
        saveFeedUserTitle(feedUrl, trimmed);
      });
    }
    if (feedPropResetTitleBtn) {
      feedPropResetTitleBtn.addEventListener('click', () => {
        const feedUrl = feedPropUserTitle?.dataset.feedUrl;
        if (feedPropUserTitle) feedPropUserTitle.value = '';
        if (feedUrl) saveFeedUserTitle(feedUrl, '');
      });
    }

    const SELECTABLE_STRATEGIES = new Set(['inline', 'og_scrape', 'media_rss', 'enclosure']);
    const PRESET_STRATEGIES = new Set(['webcomic', 'artwork']);
    function setActivePreset(presetName) {
      feedPropPresetBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.preset === presetName));
    }

    // Crop helpers
    function setThumbCropParam(src, cropVal) {
      try {
        const u = new URL(src, location.origin);
        u.searchParams.set('crop', cropVal);
        return u.pathname + u.search;
      } catch (_) { return src; }
    }
    function setThumbMsParam(src, ms) {
      try {
        const u = new URL(src, location.origin);
        if (ms) u.searchParams.set('ms', ms);
        else u.searchParams.delete('ms');
        return u.pathname + u.search;
      } catch (_) { return src; }
    }
    function setThumbFzParam(src, fz) {
      try {
        const u = new URL(src, location.origin);
        if (fz && parseFloat(fz) !== 1.0) u.searchParams.set('fz', fz);
        else u.searchParams.delete('fz');
        return u.pathname + u.search;
      } catch (_) { return src; }
    }
    function parseCropValue(v) {
      if (!v || v === 'cover') return { mode: 'cover', pos: 'center' };
      if (v === 'left') return { mode: 'cover', pos: 'left' };  // backward compat
      if (v === 'contain') return { mode: 'contain', pos: 'center' };
      if (v === 'smart') return { mode: 'smart', pos: 'center' };
      if (v.startsWith('cover-')) return { mode: 'cover', pos: v.slice(6) };
      return { mode: 'cover', pos: 'center' };
    }
    function buildCropValue(mode, pos) {
      if (mode === 'contain') return 'contain';
      if (mode === 'smart') return 'smart';
      return pos === 'center' ? 'cover' : `cover-${pos}`;
    }
    function getActiveCropValue() {
      const mode = document.querySelector('.feed-prop-crop-mode-btn.active')?.dataset.mode || 'cover';
      const pos = document.querySelector('.feed-prop-crop-pos-btn.active')?.dataset.pos || 'center';
      return buildCropValue(mode, pos);
    }
    function updateCropUI(cropVal) {
      const { mode, pos } = parseCropValue(cropVal);
      document.querySelectorAll('.feed-prop-crop-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
      document.querySelectorAll('.feed-prop-crop-pos-btn').forEach(b => b.classList.toggle('active', b.dataset.pos === pos));
      const posSection = document.getElementById('feed-prop-crop-pos-section');
      if (posSection) posSection.style.opacity = mode === 'cover' ? '' : '0.4';
      const msSection = document.getElementById('feed-prop-smart-ms-section');
      if (msSection) msSection.style.display = mode === 'smart' ? '' : 'none';
      const fzSection = document.getElementById('feed-prop-fill-zoom-section');
      if (fzSection) fzSection.style.display = mode === 'cover' ? '' : 'none';
    }
    function applyCropToThumbnails(feedUrl, cropVal) {
      const previewTile = document.getElementById('feed-prop-thumb-preview-tile');
      if (previewTile) {
        previewTile.className = previewTile.className.replace(/\bcrop-\S+/g, '').trim() + ' crop-' + cropVal;
        const previewImg = document.getElementById('feed-prop-thumb-preview-img');
        if (previewImg instanceof HTMLImageElement) {
          if (cropVal === 'smart') {
            previewTile.style.backgroundImage = `url('${setThumbCropParam(previewImg.src, 'cover')}')`;
          } else {
            previewTile.style.backgroundImage = '';
          }
          previewImg.src = setThumbCropParam(previewImg.src, cropVal);
        }
      }
      document.querySelectorAll(`.post-item[data-post-feed-url="${CSS.escape(feedUrl)}"]`).forEach(postItem => {
        postItem.dataset.thumbCrop = cropVal;
        const thumb = postItem.querySelector('.post-thumbnail');
        if (thumb instanceof HTMLElement) {
          thumb.className = thumb.className.replace(/\bcrop-\S+/g, '').trim() + ' crop-' + cropVal;
          const img = postItem.querySelector('.post-thumbnail-image');
          if (img instanceof HTMLImageElement) {
            if (cropVal === 'smart') {
              thumb.style.backgroundImage = `url('${setThumbCropParam(img.src, 'cover')}')`;
            } else {
              thumb.style.backgroundImage = '';
            }
            img.src = setThumbCropParam(img.src, cropVal);
          }
        }
      });
    }

    function applyThumbSourceUI() {
      const select = document.getElementById('feed-prop-thumb-source');
      const customRow = document.getElementById('feed-prop-thumb-custom-row');
      const preview = document.getElementById('feed-prop-thumb-preview');
      const previewImg = document.getElementById('feed-prop-thumb-preview-img');
      if (!select) return;

      const val = select.value;
      if (customRow) customRow.style.display = val === '__custom__' ? '' : 'none';

      let previewUrl = null;
      if (val === '__custom__') {
        previewUrl = document.getElementById('feed-prop-thumbnail-url')?.value?.trim() || null;
      } else if (val.startsWith('__strat__')) {
        previewUrl = val.slice(9);
      } else if (val === '' || val === '__per_entry_inline__' || val === '__per_entry_media_rss__') {
        // For per-entry modes, show first strategy cache entry as a representative preview
        previewUrl = select.dataset.autoPreviewUrl || null;
      }

      const previewTile = document.getElementById('feed-prop-thumb-preview-tile');
      if (preview && previewImg) {
        if (previewUrl) {
          const _crop = getActiveCropValue();
          const _ms = _crop === 'smart' ? (document.getElementById('feed-prop-smart-min-scale')?.value?.trim() || '') : '';
          const _fz = (_crop !== 'smart' && _crop !== 'contain') ? (document.getElementById('feed-prop-fill-zoom')?.value?.trim() || '') : '';
          previewImg.src = `/thumb?url=${encodeURIComponent(previewUrl)}&crop=${encodeURIComponent(_crop)}` + (_ms ? `&ms=${encodeURIComponent(_ms)}` : '') + (_fz && parseFloat(_fz) !== 1.0 ? `&fz=${encodeURIComponent(_fz)}` : '');
          preview.style.display = '';
          // Apply current crop to preview tile
          if (previewTile) {
            previewTile.className = previewTile.className.replace(/\bcrop-\S+/g, '').trim() + ' crop-' + _crop;
            if (_crop === 'smart') {
              previewTile.style.backgroundImage = `url('/thumb?url=${encodeURIComponent(previewUrl)}&crop=cover')`;
            } else {
              previewTile.style.backgroundImage = '';
            }
          }
        } else {
          preview.style.display = 'none';
        }
      }
    }

    function updateThumbSourceSelect(strategyCache, currentThumbUrl, showThumb, thumbStrategy) {
      const select = document.getElementById('feed-prop-thumb-source');
      const group = document.getElementById('feed-prop-thumb-strategies-group');
      if (!select || !group) return;

      // Rebuild strategy options
      group.innerHTML = '';
      (strategyCache || []).forEach(row => {
        if (!row.image_url) return;
        const opt = document.createElement('option');
        opt.value = '__strat__' + row.image_url;
        opt.textContent = row.strategy;
        group.appendChild(opt);
      });
      // autoPreviewUrl: use the active image-extraction strategy's image, not just the first one
      const _activeImageStrat = feedPropStrategy?.value || 'auto';
      const _autoRow = (strategyCache || []).find(r => r.strategy === _activeImageStrat && r.image_url)
                    || (strategyCache || []).find(r => r.image_url);
      select.dataset.autoPreviewUrl = _autoRow?.image_url || '';
      select.dataset.thumbStrategy = thumbStrategy || '';

      // Set current selection: showThumb=false → Disabled, otherwise use thumbUrl/strategy
      if (!showThumb) {
        select.value = '__disabled__';
      } else if (!currentThumbUrl && thumbStrategy === 'inline') {
        select.value = '__per_entry_inline__';
      } else if (!currentThumbUrl && thumbStrategy === 'media_rss') {
        select.value = '__per_entry_media_rss__';
      } else if (!currentThumbUrl) {
        select.value = '';
      } else if (currentThumbUrl === '__favicon__') {
        select.value = '__favicon__';
      } else {
        const matchOpt = [...select.options].find(o => o.value === '__strat__' + currentThumbUrl);
        const input = document.getElementById('feed-prop-thumbnail-url');
        if (matchOpt) {
          select.value = matchOpt.value;
          if (input) input.value = currentThumbUrl;
        } else {
          select.value = '__custom__';
          if (input) input.value = currentThumbUrl;
        }
      }
      applyThumbSourceUI();
    }

    function updateCaptionPreviews(cacheRows) {
      const active = feedPropStrategy?.value || 'auto';
      const row = (cacheRows || []).find(r => r.strategy === active)
               || (cacheRows || [])[0];
      if (feedPropCaptionAltPreview)   feedPropCaptionAltPreview.textContent   = row?.image_alt   || '';
      if (feedPropCaptionTitlePreview) feedPropCaptionTitlePreview.textContent = row?.image_title || '';
    }

    let feedHistoryRows = [];
    let feedHistoryFilter = 'all';

    function setFeedHistory(rows) {
      feedHistoryRows = Array.isArray(rows) ? rows : [];
      feedHistoryFilter = 'all';
      document.querySelectorAll('[data-history-filter]').forEach(b => {
        b.classList.toggle('feed-prop-history-filter-btn--active', b.getAttribute('data-history-filter') === 'all');
      });
      renderFeedHistory();
    }

    function renderFeedHistory() {
      const list = document.getElementById('feed-prop-history-list');
      const empty = document.getElementById('feed-prop-history-empty');
      if (!list) return;
      const rows = feedHistoryRows.filter(r => {
        if (feedHistoryFilter === 'error') return r.status === 'error';
        if (feedHistoryFilter === 'new') return (r.new_entries || 0) > 0;
        return true;
      });
      list.replaceChildren();
      rows.forEach(r => {
        const li = document.createElement('li');
        const isError = r.status === 'error';
        li.className = 'feed-prop-history-item' + (isError ? ' feed-prop-history-item--error' : '');

        const head = document.createElement('span');
        head.className = 'feed-prop-history-head';
        const badge = document.createElement('span');
        badge.className = 'feed-prop-history-badge ' + (isError ? 'feed-prop-history-badge--error' : 'feed-prop-history-badge--ok');
        badge.textContent = isError ? 'Error' : 'OK';
        head.appendChild(badge);
        const when = document.createElement('span');
        when.className = 'feed-prop-history-when';
        when.textContent = r.fetched_at || '';
        head.appendChild(when);
        if (typeof r.http_status === 'number') {
          const http = document.createElement('span');
          http.className = 'feed-prop-history-meta';
          http.textContent = `HTTP ${r.http_status}`;
          head.appendChild(http);
        }
        if (!isError && typeof r.new_entries === 'number') {
          const ne = document.createElement('span');
          ne.className = 'feed-prop-history-meta';
          ne.textContent = r.new_entries > 0 ? `+${r.new_entries} new` : 'no change';
          head.appendChild(ne);
        }
        if (typeof r.duration_ms === 'number') {
          const dur = document.createElement('span');
          dur.className = 'feed-prop-history-meta';
          dur.textContent = `${r.duration_ms} ms`;
          head.appendChild(dur);
        }
        li.appendChild(head);
        if (isError && r.error) {
          const err = document.createElement('span');
          err.className = 'feed-prop-history-error muted';
          err.textContent = r.error;
          li.appendChild(err);
        }
        list.appendChild(li);
      });
      if (empty) empty.hidden = rows.length > 0;
    }

    document.querySelectorAll('[data-history-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        feedHistoryFilter = btn.getAttribute('data-history-filter') || 'all';
        document.querySelectorAll('[data-history-filter]').forEach(b => {
          b.classList.toggle('feed-prop-history-filter-btn--active', b === btn);
        });
        renderFeedHistory();
      });
    });
    function renderFeedAutomations(automations) {
      const rulesList = document.getElementById('feed-prop-automations-rules');
      const rulesEmpty = document.getElementById('feed-prop-automations-rules-empty');
      const runsList = document.getElementById('feed-prop-automations-runs');
      const runsEmpty = document.getElementById('feed-prop-automations-runs-empty');
      const rules = (automations && automations.rules) || [];
      const runs = (automations && automations.recent_runs) || [];

      if (rulesList) {
        rulesList.replaceChildren();
        rules.forEach(rule => {
          const li = document.createElement('li');
          li.className = 'feed-prop-automation-item' + (rule.enabled ? '' : ' feed-prop-automation-item--off');
          const head = document.createElement('span');
          head.className = 'feed-prop-automation-head';
          const label = document.createElement('strong');
          label.textContent = rule.type_label || 'Rule';
          head.appendChild(label);
          if (rule.keyword) {
            const kw = document.createElement('code');
            kw.className = 'feed-prop-automation-keyword';
            kw.textContent = rule.keyword;
            head.appendChild(kw);
          }
          const scope = document.createElement('span');
          scope.className = 'feed-prop-automation-scope';
          scope.textContent = rule.scope_label || '';
          head.appendChild(scope);
          if (!rule.enabled) {
            const off = document.createElement('span');
            off.className = 'feed-prop-automation-off';
            off.textContent = 'disabled';
            head.appendChild(off);
          }
          li.appendChild(head);
          if (rule.detail) {
            const detail = document.createElement('span');
            detail.className = 'feed-prop-automation-detail muted';
            detail.textContent = rule.detail;
            li.appendChild(detail);
          }
          rulesList.appendChild(li);
        });
      }
      if (rulesEmpty) rulesEmpty.hidden = rules.length > 0;

      if (runsList) {
        runsList.replaceChildren();
        runs.forEach(run => {
          const li = document.createElement('li');
          li.className = 'feed-prop-automation-item';
          const head = document.createElement('span');
          head.className = 'feed-prop-automation-head';
          const label = document.createElement('strong');
          label.textContent = run.type_label || 'Rule';
          head.appendChild(label);
          if (run.keyword) {
            const kw = document.createElement('code');
            kw.className = 'feed-prop-automation-keyword';
            kw.textContent = run.keyword;
            head.appendChild(kw);
          }
          const count = document.createElement('span');
          count.className = 'feed-prop-automation-scope';
          count.textContent = `${run.affected} ${run.affected === 1 ? 'entry' : 'entries'}`;
          head.appendChild(count);
          li.appendChild(head);
          const detail = document.createElement('span');
          detail.className = 'feed-prop-automation-detail muted';
          detail.textContent = (run.run_at || '') + (run.trigger ? ` · ${run.trigger}` : '');
          li.appendChild(detail);
          runsList.appendChild(li);
        });
      }
      if (runsEmpty) runsEmpty.hidden = runs.length > 0;
    }

    function renderStrategyGrid(cacheRows) {
      if (!feedPropStratGrid || !feedPropStratEmpty) return;
      if (!cacheRows || cacheRows.length === 0) {
        feedPropStratEmpty.hidden = false;
        feedPropStratGrid.hidden = true;
        feedPropStratGrid.innerHTML = '';
        return;
      }
      const currentStrategy = feedPropStrategy?.value || 'auto';
      const visibleRows = cacheRows.filter(r => !['youtube', 'webcomic', 'artwork'].includes(r.strategy));
      feedPropStratEmpty.hidden = true;
      feedPropStratGrid.hidden = false;
      feedPropStratGrid.style.gridTemplateColumns = `repeat(${visibleRows.length || 1}, minmax(0, 1fr))`;
      feedPropStratGrid.innerHTML = visibleRows.map(row => {
        const label = row.strategy || '';
        const isActive = label === currentStrategy;
        const isSelectable = SELECTABLE_STRATEGIES.has(label);
        const activeClass = isActive ? ' active' : '';
        const title = isSelectable
          ? `title="Click to use ${label} strategy"`
          : `title="${label}: auto-detected only"`;
        const friendlyLabel = STRATEGY_LABELS[label] || label;
        if (row.error) {
          return `<div class="feed-prop-strat-card feed-prop-strat-card--error${activeClass}" data-strategy="${label}" ${title}>
            <div class="feed-prop-strat-label">${friendlyLabel}</div>
            <div class="feed-prop-strat-error">${row.error}</div>
          </div>`;
        }
        const useAsThumbBtn = row.image_url
          ? `<button class="feed-prop-use-as-thumb" data-url="${row.image_url}" title="Use as thumbnail">📌</button>`
          : '';
        const img = row.image_url
          ? `<img class="feed-prop-strat-thumb" src="/thumb?url=${encodeURIComponent(row.image_url)}" alt="" loading="lazy" data-full-url="${row.image_url.replace(/&/g,'&amp;').replace(/"/g,'&quot;')}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'feed-prop-strat-thumb feed-prop-strat-thumb-empty',textContent:'no image'}))" />`
          : `<div class="feed-prop-strat-thumb feed-prop-strat-thumb-empty">no image</div>`;
        return `<div class="feed-prop-strat-card${activeClass}" data-strategy="${label}" ${title}>
          ${img}
          ${useAsThumbBtn}
          <div class="feed-prop-strat-label">${friendlyLabel}</div>
        </div>`;
      }).join('');
      feedPropStratGrid.querySelectorAll('.feed-prop-strat-thumb[data-full-url]').forEach(img => {
        const card = img.closest('.feed-prop-strat-card');
        const fullUrl = img.getAttribute('data-full-url');
        if (!fullUrl) return;
        const full = new Image();
        full.addEventListener('load', () => {
          if (full.naturalWidth > 0) card.dataset.dims = `${full.naturalWidth}×${full.naturalHeight}`;
        });
        full.addEventListener('error', () => {
          if (img.complete && img.naturalWidth > 0)
            card.dataset.dims = `${img.naturalWidth}×${img.naturalHeight}`;
        });
        full.src = fullUrl;
      });

      // Title/alt text cells aligned under each card column.
      if (feedPropStratCaptions) {
        const escHtml = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        const hasAny = visibleRows.some(r => r.image_title || r.image_alt);
        if (!hasAny) {
          feedPropStratCaptions.hidden = true;
          feedPropStratCaptions.innerHTML = '';
        } else {
          feedPropStratCaptions.hidden = false;
          feedPropStratCaptions.style.gridTemplateColumns = feedPropStratGrid.style.gridTemplateColumns;
          feedPropStratCaptions.innerHTML = visibleRows.map(r => {
            const titleHtml = r.image_title
              ? `<div class="feed-prop-strat-cap-title"><span class="feed-prop-strat-cap-attr">Title:</span> ${escHtml(r.image_title)}</div>` : '';
            const altHtml = r.image_alt
              ? `<div class="feed-prop-strat-cap-alt"><span class="feed-prop-strat-cap-attr">Alt:</span> ${escHtml(r.image_alt)}</div>` : '';
            return `<div class="feed-prop-strat-cap-cell">${titleHtml}${altHtml}</div>`;
          }).join('');
        }
      }
      _lastStratCacheRows = cacheRows;
      updateCaptionPreviews(cacheRows);
    }

    async function doStrategyRefresh(feedUrl, entryId) {
      if (!feedPropRefreshBtn) return;
      feedPropRefreshBtn.disabled = true;
      feedPropRefreshBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">refresh</span> Refreshing…';
      try {
        const body = new URLSearchParams({ feed_url: feedUrl });
        if (entryId) body.set('entry_id', entryId);
        const resp = await fetch('/feeds/strategy-refresh', { method: 'POST', body });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderStrategyGrid(data.strategy_cache || []);
        const _ts = document.getElementById('feed-prop-thumb-source');
        const currentThumbUrl = _ts?.dataset.savedThumbUrl || null;
        const _showThumb = _ts?.value !== '__disabled__';
        const _preVal = _ts?.value;  // preserve any user-initiated change in flight
        updateThumbSourceSelect(data.strategy_cache || [], currentThumbUrl, _showThumb, _ts?.dataset.thumbStrategy || null);
        // If updateThumbSourceSelect overwrote a pending user selection, restore it
        if (_ts && _preVal !== undefined && _ts.value !== _preVal
            && [..._ts.options].some(o => o.value === _preVal)) {
          _ts.value = _preVal;
          applyThumbSourceUI();
        }
      } catch (e) {
        if (feedPropStratEmpty) {
          feedPropStratEmpty.textContent = `Refresh failed: ${e.message}`;
          feedPropStratEmpty.hidden = false;
          if (feedPropStratGrid) feedPropStratGrid.hidden = true;
        }
      } finally {
        feedPropRefreshBtn.disabled = false;
        feedPropRefreshBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">refresh</span> Refresh';
      }
    }

    async function openFolderPropertiesModal(folderId) {
      const modal = document.getElementById('folder-properties-modal');
      if (!modal || !folderId) return;

      const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = (val !== null && val !== undefined) ? String(val) : '-';
      };

      setVal('folder-prop-path', 'Loading...');
      setVal('folder-prop-feed-count', '-');
      setVal('folder-prop-total', '-');
      const topWrap = document.getElementById('folder-prop-top-feeds-wrap');
      if (topWrap) topWrap.hidden = true;
      modal.removeAttribute('hidden');

      try {
        const response = await fetch(`/folders/properties?folder_id=${encodeURIComponent(folderId)}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!data.found) {
          setVal('folder-prop-path', data.error || 'Folder not found.');
          return;
        }
        setVal('folder-prop-path', data.path || data.name || '-');
        setVal('folder-prop-feed-count', data.feed_count);
        const cadenceSel = document.getElementById('folder-prop-cadence');
        if (cadenceSel) {
          cadenceSel.dataset.folderId = String(folderId);
          const cadence = data.cadence_minutes || 0;
          // Select closest option
          const opts = Array.from(cadenceSel.options);
          const match = opts.find(o => parseInt(o.value) === cadence);
          cadenceSel.value = match ? String(cadence) : '0';
        }
        const cadenceStatus = document.getElementById('folder-prop-cadence-status');
        if (cadenceStatus) cadenceStatus.textContent = '';

        const total = data.total_articles ?? 0;
        const unread = data.unread_articles ?? 0;
        const totalEl = document.getElementById('folder-prop-total');
        if (totalEl) {
          totalEl.textContent = '';
          totalEl.appendChild(document.createTextNode(String(total)));
          if (unread > 0) {
            const badge = document.createElement('span');
            badge.className = 'folder-prop-unread-badge';
            badge.textContent = ` (${unread.toLocaleString()} unread)`;
            totalEl.appendChild(badge);
          }
        }

        const tbody = document.getElementById('folder-prop-top-feeds-body');
        if (tbody && data.top_feeds && data.top_feeds.length > 0) {
          tbody.innerHTML = '';
          for (const f of data.top_feeds) {
            const tr = document.createElement('tr');
            const tdTitle = document.createElement('td');
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'folder-prop-feed-link';
            btn.textContent = f.title;
            btn.dataset.feedUrl = f.feed_url;
            btn.addEventListener('click', () => {
              modal.setAttribute('hidden', '');
              openFeedPropertiesModal(f.feed_url);
            });
            tdTitle.appendChild(btn);
            const tdAvg = document.createElement('td');
            tdAvg.textContent = f.avg_per_week;
            const tdTotal = document.createElement('td');
            tdTotal.textContent = f.total;
            tr.appendChild(tdTitle);
            tr.appendChild(tdAvg);
            tr.appendChild(tdTotal);
            tbody.appendChild(tr);
          }
          if (topWrap) topWrap.hidden = false;
        }
      } catch (error) {
        setVal('folder-prop-path', `Could not load: ${error}`);
      }
    }

    function closeActionInputModal() {
      if (!actionInputModal) {
        return;
      }
      actionInputModal.setAttribute('hidden', '');
      actionModalSubmitHandler = null;
      if (actionModalForm) {
        actionModalForm.reset();
      }
      if (actionModalInput) {
        actionModalInput.type = 'text';
      }
    }

    function openActionInputModal(config) {
      if (
        !actionInputModal ||
        !actionModalTitle ||
        !actionModalLabel ||
        !actionModalInput ||
        !actionModalSubmit ||
        !config ||
        !config.onSubmit
      ) {
        return;
      }

      actionModalTitle.textContent = config.title;
      actionModalLabel.textContent = config.label;
      actionModalInput.type = config.inputType || 'text';
      actionModalInput.placeholder = config.placeholder || '';
      actionModalSubmit.textContent = config.submitLabel || 'Save';
      actionModalInput.value = config.initialValue || '';
      actionModalSubmitHandler = config.onSubmit;
      actionInputModal.removeAttribute('hidden');
      window.setTimeout(() => actionModalInput.focus(), 0);
    }

    actionModalForm?.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!actionModalInput || !actionModalSubmitHandler) {
        return;
      }

      const value = actionModalInput.value.trim();
      if (!value) {
        actionModalInput.focus();
        return;
      }

      const submitHandler = actionModalSubmitHandler;
      closeActionInputModal();
      submitHandler(value);
    });

    actionModalCancel?.addEventListener('click', () => {
      closeActionInputModal();
    });

    actionInputModal?.addEventListener('click', (event) => {
      if (event.target === actionInputModal) {
        closeActionInputModal();
      }
    });

    feedPropertiesClose?.addEventListener('click', () => {
      closeFeedPropertiesModal();
    });

    // Change a feed's folder straight from the Info tab. Reuses moveFeedRequest
    // (hoisted below) so the sidebar/Settings surfaces update in place. A target
    // of -1 (Uncategorized) is handled server-side as folderless.
    feedPropFolderSelect?.addEventListener('change', async () => {
      const feedUrl = feedPropFolderSelect.dataset.feedUrl;
      const fromId = parseInt(feedPropFolderSelect.dataset.currentFolderId ?? '-1', 10);
      const toId = parseInt(feedPropFolderSelect.value, 10);
      if (!feedUrl || fromId === toId) return;
      const toName = feedPropFolderSelect.options[feedPropFolderSelect.selectedIndex]?.textContent?.trim() || 'folder';
      if (feedPropFolderStatus) feedPropFolderStatus.textContent = 'Moving…';
      try {
        await moveFeedRequest(feedUrl, fromId, toId, toName);
        feedPropFolderSelect.dataset.currentFolderId = String(toId);
        if (feedPropFolderStatus) feedPropFolderStatus.textContent = '';
      } catch (e) {
        if (feedPropFolderStatus) feedPropFolderStatus.textContent = 'Move failed.';
        feedPropFolderSelect.value = String(fromId);
      }
    });

    let _feedPropCloseOnClick = false;
    feedPropertiesModal?.addEventListener('pointerdown', (event) => {
      _feedPropCloseOnClick = event.target === feedPropertiesModal;
    });
    feedPropertiesModal?.addEventListener('click', (event) => {
      if (_feedPropCloseOnClick && event.target === feedPropertiesModal) {
        closeFeedPropertiesModal();
      }
      _feedPropCloseOnClick = false;
    });

    // Single source of truth for unsubscribing one feed from one folder. Every
    // entry point (sidebar context menu, Feed Properties modal, Settings feed
    // row, problematic-feeds panels) calls this so the request + UI cleanup stay
    // identical and can't drift. POSTs, then strips the feed from all surfaces
    // (sidebar tree, Settings list, problem panels). Throws on a failed request
    // so callers can surface their own error UI. `function` decl is hoisted, so
    // callers defined earlier/later in this scope can all use it.
    async function moveFeedRequest(feedUrl, fromFolderId, toFolderId, toFolderName) {
      const form = document.getElementById('context-move-feed-form');
      const body = new URLSearchParams();
      body.set('feed_url', feedUrl);
      body.set('from_folder_id', fromFolderId);
      body.set('to_folder_id', toFolderId);
      for (const n of ['current_folder_id', 'current_list_feed_url', 'sort_by', 'sort_dir', 'read_filter', 'star_only', 'resume_read_filter']) {
        const inp = form?.querySelector(`[name="${n}"]`);
        if (inp) body.set(n, inp.value);
      }
      const resp = await fetch('/feeds/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-move' },
        credentials: 'same-origin', body: body.toString(),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json().catch(() => ({ ok: true }));
      if (data.ok === false) { window.alert(data.message || 'Move failed.'); return; }

      // Relocate the feed node from the source folder to the target (if that
      // folder's list is rendered), else just drop it from the source — no reload.
      const fu = CSS.escape(feedUrl), sfid = CSS.escape(String(fromFolderId));
      const srcLi = document.querySelector(`.tree-feed-item > .feed-link[data-feed-url="${fu}"][data-folder-id="${sfid}"]`)?.closest('.tree-feed-item');
      const unread = srcLi ? (parseInt(srcLi.querySelector('.count')?.textContent || '0', 10) || 0) : 0;
      const targetUl = document.getElementById(`folder-feeds-${toFolderId}`);
      const srcFolderEl = document.querySelector(`.tree-item[data-folder-id="${sfid}"]:not(.root-item):not(.saved-folder-item)`);
      const tgtFolderEl = document.querySelector(`.tree-item[data-folder-id="${CSS.escape(String(toFolderId))}"]:not(.root-item):not(.saved-folder-item)`);
      if (srcLi) {
        const link = srcLi.querySelector('.feed-link');
        if (targetUl && link) {
          link.setAttribute('data-folder-id', String(toFolderId));
          if (link.href) link.href = link.href.replace(/([?&]folder_id=)[^&]*/, `$1${toFolderId}`);
          targetUl.appendChild(srcLi);
          if (tgtFolderEl && unread) adjustCountBadge(tgtFolderEl, unread);
        } else {
          srcLi.remove();
        }
        if (srcFolderEl && unread) adjustCountBadge(srcFolderEl, -unread);
      }
      // Settings tree: drop the source row (reappears under the target on reload).
      document.querySelectorAll(`.settings-feed-row[data-folder-feeds="${sfid}"][data-feed-url="${fu}"]`).forEach(r => r.remove());

      if (typeof showToastMessage === 'function') showToastMessage(`Moved to "${toFolderName}".`);
    }

    async function unsubscribeFeedRequest(feedUrl, folderId, opts = {}) {
      const body = new URLSearchParams({ folder_id: folderId, feed_url: feedUrl });
      if (opts.migrateCurationTo) body.set('migrate_curation_to', opts.migrateCurationTo);
      const resp = await fetch('/feeds/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-unsubscribe' },
        credentials: 'same-origin',
        body: body.toString(),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const fu = CSS.escape(feedUrl);
      const fid = CSS.escape(String(folderId));
      // An orphan (Uncategorized) feed renders under the virtual folder's
      // sentinel id (e.g. -1), not the folder_id 0 we unsubscribe with, so match
      // by feed-url alone in that case — otherwise the sidebar item lingers until
      // a full refresh.
      const feedFolderSel = folderId > 0 ? `[data-folder-id="${fid}"]` : '';
      document.querySelectorAll(
        `.tree-feed-item > .feed-link[data-feed-url="${fu}"]${feedFolderSel}`
      ).forEach(link => link.closest('.tree-feed-item')?.remove());
      let removedSettingsRows = 0;
      document.querySelectorAll(
        `.settings-feed-row [data-settings-feed-unsubscribe][data-feed-url="${fu}"]${feedFolderSel}`
      ).forEach(btn => { if (btn.closest('.settings-feed-row')) { btn.closest('.settings-feed-row').remove(); removedSettingsRows++; } });
      // Keep the Settings folder row consistent: decrement its feed count and,
      // if it's now empty, disable/collapse its expand toggle.
      if (removedSettingsRows) {
        const folderRow = document.querySelector(`.settings-folder-row[data-folder-row="${fid}"]`);
        const countCell = folderRow?.querySelector('.settings-folder-feeds');
        if (countCell) {
          const n = Math.max(0, (parseInt(countCell.textContent, 10) || 0) - removedSettingsRows);
          countCell.textContent = String(n);
          if (n === 0) {
            const toggle = folderRow.querySelector('[data-folder-toggle]');
            toggle?.setAttribute('disabled', '');
            toggle?.setAttribute('aria-expanded', 'false');
          }
        }
      }
      document.querySelectorAll(`.problem-feed-item[data-feed-url="${fu}"]`).forEach(el => el.remove());

      if (opts.title) showToastMessage(`Unsubscribed from "${opts.title}".`);

      // Refresh the post list so entries from the now-removed feed disappear.
      // If the list is scoped to this very feed, open its folder instead; for a
      // folder/All-Feeds/tag view, just reload the current scope in place.
      // Prefer a soft (SPA) refresh; fall back to full navigation only when the
      // feed itself was the active scope.
      if (opts.refresh !== false) {
        const cur = new URL(window.location.href, window.location.origin);
        const viewingThisFeed = cur.searchParams.get('list_feed_url') === feedUrl;
        if (viewingThisFeed) {
          cur.searchParams.delete('list_feed_url');
          cur.searchParams.delete('feed_url');
          cur.searchParams.delete('entry_id');
          // folderId 0 == an orphan feed with no folder; drop the scope to the
          // default view rather than navigating to a non-existent folder_id=0.
          if (folderId > 0) cur.searchParams.set('folder_id', String(folderId));
          else cur.searchParams.delete('folder_id');
        }
        const target = cur.pathname + cur.search;
        if (typeof loadScopePanesWithoutFullRefresh === 'function') {
          // pushHistory only when we changed scope (left the dead feed view).
          loadScopePanesWithoutFullRefresh(target, viewingThisFeed);
        } else if (viewingThisFeed) {
          window.location.assign(target);
        }
      }
    }

    // Confirm + (if the feed carries curation) offer to migrate its tags/stars to
    // another feed before unsubscribing, so curation isn't silently lost. Resolves
    // true if the feed was unsubscribed, false if cancelled/failed.
    async function unsubscribeFeedInteractive(feedUrl, folderId, opts = {}) {
      const title = opts.title || feedUrl;
      const modal = document.getElementById('unsub-migrate-modal');
      const bodyEl = document.getElementById('unsub-migrate-body');
      const curationEl = document.getElementById('unsub-migrate-curation');
      const noteEl = document.getElementById('unsub-migrate-curation-note');
      const targetSel = document.getElementById('unsub-migrate-target');
      const targetList = document.getElementById('unsub-migrate-candidates');
      const confirmBtn = document.getElementById('unsub-migrate-confirm');
      const moveRadio = document.getElementById('unsub-migrate-move');
      const movePanel = document.getElementById('unsub-migrate-move-panel');
      const itemsEl = document.getElementById('unsub-migrate-items');
      if (!modal) { // fallback: plain confirm
        if (!window.confirm(`Unsubscribe from "${title}"?`)) return false;
        try { await unsubscribeFeedRequest(feedUrl, folderId, opts); return true; }
        catch (err) { window.alert(`Unsubscribe failed: ${err}`); return false; }
      }

      let counts = { tagged: 0, stars: 0, candidates: [] };
      try {
        const r = await fetch('/feeds/curation-count?feed_url=' + encodeURIComponent(feedUrl), { credentials: 'same-origin' });
        if (r.ok) counts = await r.json();
      } catch (_) { /* proceed with zero counts */ }

      bodyEl.textContent = `Unsubscribe from "${title}"? This removes the feed and its articles from Lectio.`;
      const hasCuration = (counts.tagged || 0) + (counts.stars || 0) > 0;
      const candidates = counts.candidates || [];
      if (hasCuration) {
        const parts = [];
        if (counts.stars) parts.push(`${counts.stars} starred`);
        if (counts.tagged) parts.push(`${counts.tagged} tagged`);
        noteEl.textContent = `This feed has ${parts.join(' and ')} item${(counts.stars + counts.tagged) === 1 ? '' : 's'}. Move them onto another feed so they aren't lost?`;
        // Filterable typeahead: datalist option label -> feed url. Titles can
        // repeat, so disambiguate duplicate labels with a short host hint.
        targetList.innerHTML = '';
        targetSel.value = '';
        const labelToUrl = new Map();
        const labelSeen = new Map();
        candidates.forEach(c => {
          let label = c.title || c.url;
          if (labelSeen.has(label)) {
            let host = c.url; try { host = new URL(c.url).host; } catch (_) {}
            label = `${label} (${host})`;
          }
          // Still collide? append a counter so every option is selectable.
          let uniq = label, n = 2;
          while (labelToUrl.has(uniq)) { uniq = `${label} #${n++}`; }
          labelSeen.set(c.title || c.url, true);
          labelToUrl.set(uniq, c.url);
          const o = document.createElement('option'); o.value = uniq; targetList.appendChild(o);
        });
        targetSel._labelToUrl = labelToUrl;
        // "Just unsubscribe" is the default so the dialog opens clean (no list);
        // picking "Move items" reveals the picker + item list.
        const skipRadio = document.getElementById('unsub-migrate-skip');
        if (skipRadio) skipRadio.checked = true;
        moveRadio.checked = false;
        moveRadio.disabled = candidates.length === 0;
        curationEl.hidden = false;
        if (itemsEl) { itemsEl.innerHTML = ''; itemsEl._loaded = false; }
      } else {
        curationEl.hidden = true;
      }

      modal.removeAttribute('hidden');

      return new Promise((resolve) => {
        function currentChoice() {
          return modal.querySelector('input[name="unsub-migrate-choice"]:checked')?.value;
        }
        function checkedIds() {
          return Array.from(itemsEl?.querySelectorAll('.unsub-migrate-item-check:checked') || [])
            .map(c => c.value).filter(Boolean);
        }
        const selectAllCb = document.getElementById('unsub-migrate-select-all');
        function syncSelectAllState() {
          if (!selectAllCb) return;
          const boxes = Array.from(itemsEl?.querySelectorAll('.unsub-migrate-item-check') || []);
          const checked = boxes.filter(b => b.checked).length;
          selectAllCb.checked = boxes.length > 0 && checked === boxes.length;
          selectAllCb.indeterminate = checked > 0 && checked < boxes.length;
        }
        function onSelectAllToggle() {
          const boxes = itemsEl?.querySelectorAll('.unsub-migrate-item-check') || [];
          boxes.forEach(b => { b.checked = selectAllCb.checked; });
          syncConfirmState();
        }
        const moveOnlyBtn = document.getElementById('unsub-migrate-move-only');
        function syncConfirmState() {
          const moving = hasCuration && currentChoice() === 'move';
          if (movePanel) movePanel.hidden = !moving;
          if (moveOnlyBtn) moveOnlyBtn.hidden = !moving;
          confirmBtn.textContent = moving ? 'Move & Unsubscribe' : 'Unsubscribe';
          if (!moving) { confirmBtn.disabled = false; return; }
          const targetOk = Boolean(targetSel._labelToUrl && targetSel._labelToUrl.get(targetSel.value));
          // Enabled once at least one item is checked and a target is picked;
          // before the list loads, itemsEl._loaded is false and we hold off.
          const ready = targetOk && itemsEl._loaded && checkedIds().length > 0;
          confirmBtn.disabled = !ready;
          if (moveOnlyBtn) moveOnlyBtn.disabled = !ready;
        }
        async function refreshCurationNote() {
          try {
            const rc = await fetch('/feeds/curation-count?feed_url=' + encodeURIComponent(feedUrl), { credentials: 'same-origin' });
            const dc = await rc.json();
            const parts = [];
            if (dc.stars) parts.push(`${dc.stars} starred`);
            if (dc.tagged) parts.push(`${dc.tagged} tagged`);
            if (parts.length) {
              noteEl.textContent = `This feed has ${parts.join(' and ')} item${(dc.stars + dc.tagged) === 1 ? '' : 's'}. Move them onto another feed so they aren't lost?`;
            } else {
              noteEl.textContent = 'All curated items have been moved — nothing left to lose.';
            }
          } catch (_) { /* stale note is harmless */ }
        }
        // Move the checked chunk to the picked feed and keep the dialog open,
        // so successive chunks can go to different feeds before unsubscribing.
        async function onMoveOnly() {
          const targetUrl = targetSel._labelToUrl && targetSel._labelToUrl.get(targetSel.value);
          const ids = checkedIds();
          if (!targetUrl || !ids.length) return;
          moveOnlyBtn.disabled = true;
          moveOnlyBtn.textContent = 'Moving…';
          try {
            const resp = await fetch('/entries/move-to-feed-batch', {
              method: 'POST',
              body: new URLSearchParams({
                entries: JSON.stringify(ids.map(id => [feedUrl, id])),
                target_url: targetUrl,
              }),
            });
            const data = await resp.json();
            showToastMessage(data.ok ? (data.message || 'Moved.') : (data.error || 'Move failed.'));
            if (data.ok) {
              targetSel.value = '';
              itemsEl._loaded = false;
              await loadItems();   // remaining items, all checked for the next chunk
              await refreshCurationNote();
              if (!itemsEl.querySelector('.unsub-migrate-item-check')) {
                // Everything's been moved — flip to plain unsubscribe.
                const skipRb = document.getElementById('unsub-migrate-skip');
                if (skipRb) { skipRb.checked = true; }
              }
            }
          } catch (_) {
            showToastMessage('Move failed — network error.');
          } finally {
            moveOnlyBtn.textContent = 'Move selected';
            syncConfirmState();
          }
        }
        async function loadItems() {
          if (!itemsEl || itemsEl._loaded) return;
          itemsEl.textContent = 'Loading…';
          try {
            const r = await fetch('/feeds/curation-items?feed_url=' + encodeURIComponent(feedUrl), { credentials: 'same-origin' });
            const data = r.ok ? await r.json() : { items: [] };
            const list = data.items || [];
            itemsEl.innerHTML = '';
            if (!list.length) { itemsEl.textContent = 'No curated items found.'; itemsEl._loaded = true; syncConfirmState(); return; }
            const ul = document.createElement('ul');
            ul.className = 'unsub-migrate-item-list';
            list.forEach(it => {
              const li = document.createElement('li');
              const lbl = document.createElement('label');
              // All checked by default: the common case is "move everything";
              // uncheck to leave an item behind (its star gets archived).
              const cb = document.createElement('input');
              cb.type = 'checkbox';
              cb.className = 'unsub-migrate-item-check';
              cb.checked = true;
              cb.value = it.id || '';
              cb.addEventListener('change', () => { syncSelectAllState(); syncConfirmState(); });
              lbl.appendChild(cb);
              lbl.appendChild(document.createTextNode(' ' + (it.starred ? '★ ' : '') + (it.title || it.link || '(untitled)')));
              li.appendChild(lbl);
              if (it.link) {
                const a = document.createElement('a');
                a.href = it.link; a.target = '_blank'; a.rel = 'noopener noreferrer';
                a.textContent = ' ↗'; a.title = 'Open article';
                li.appendChild(a);
              }
              if (it.tags && it.tags.length) {
                const tagSpan = document.createElement('span');
                tagSpan.className = 'unsub-migrate-item-tags';
                tagSpan.textContent = ' ' + it.tags.map(t => '#' + t).join(' ');
                li.appendChild(tagSpan);
              }
              ul.appendChild(li);
            });
            itemsEl.appendChild(ul);
            itemsEl._loaded = true;
            syncSelectAllState();
          } catch (_) {
            itemsEl.textContent = 'Could not load items.';
          }
          syncConfirmState();
        }
        function onChoiceChange() {
          syncConfirmState();
          if (hasCuration && currentChoice() === 'move') loadItems();
        }
        function onTargetInput() { syncConfirmState(); }
        function cleanup() {
          confirmBtn.removeEventListener('click', onConfirm);
          moveOnlyBtn?.removeEventListener('click', onMoveOnly);
          targetSel.removeEventListener('input', onTargetInput);
          selectAllCb?.removeEventListener('change', onSelectAllToggle);
          if (moveOnlyBtn) { moveOnlyBtn.hidden = true; moveOnlyBtn.disabled = false; }
          modal.querySelectorAll('input[name="unsub-migrate-choice"]').forEach(rb => rb.removeEventListener('change', onChoiceChange));
          modal.querySelectorAll('[data-close-modal="unsub-migrate-modal"]').forEach(b => b.removeEventListener('click', onCancel));
          confirmBtn.disabled = false;
          confirmBtn.textContent = 'Unsubscribe';
          modal.setAttribute('hidden', '');
        }
        async function onConfirm() {
          let migrateTo = null;
          let moveSubset = null;
          if (hasCuration && currentChoice() === 'move') {
            migrateTo = (targetSel._labelToUrl && targetSel._labelToUrl.get(targetSel.value)) || null;
            if (!migrateTo) { window.alert('Pick a feed to move the items to, or choose "Just unsubscribe".'); return; }
            const ids = checkedIds();
            const total = itemsEl?.querySelectorAll('.unsub-migrate-item-check').length || 0;
            if (!ids.length) { window.alert('Select at least one item to move, or choose "Just unsubscribe".'); return; }
            // Everything checked → the whole-feed migration path (also carries
            // read-state slugs). A subset → batch-move just those first, then
            // plain unsubscribe (unmoved stars get archived as usual).
            if (ids.length < total) moveSubset = ids;
          }
          confirmBtn.disabled = true;
          if (moveSubset) {
            try {
              const resp = await fetch('/entries/move-to-feed-batch', {
                method: 'POST',
                body: new URLSearchParams({
                  entries: JSON.stringify(moveSubset.map(id => [feedUrl, id])),
                  target_url: migrateTo,
                }),
              });
              const data = await resp.json();
              if (!data.ok) { window.alert(data.error || 'Move failed.'); confirmBtn.disabled = false; return; }
              migrateTo = null; // subset moved by hand; don't run the whole-feed migration
            } catch (_) {
              window.alert('Move failed — network error.');
              confirmBtn.disabled = false;
              return;
            }
          }
          cleanup();
          try { await unsubscribeFeedRequest(feedUrl, folderId, { ...opts, migrateCurationTo: migrateTo }); resolve(true); }
          catch (err) { window.alert(`Unsubscribe failed: ${err}`); resolve(false); }
        }
        function onCancel() { cleanup(); resolve(false); }
        confirmBtn.addEventListener('click', onConfirm);
        moveOnlyBtn?.addEventListener('click', onMoveOnly);
        targetSel.addEventListener('input', onTargetInput);
        selectAllCb?.addEventListener('change', onSelectAllToggle);
        modal.querySelectorAll('input[name="unsub-migrate-choice"]').forEach(rb => rb.addEventListener('change', onChoiceChange));
        modal.querySelectorAll('[data-close-modal="unsub-migrate-modal"]').forEach(b => b.addEventListener('click', onCancel));
        if (selectAllCb) { selectAllCb.checked = true; selectAllCb.indeterminate = false; }
        syncConfirmState();
      });
    }
    window.unsubscribeFeedInteractive = unsubscribeFeedInteractive;

    feedPropUnsubscribeBtn?.addEventListener('click', async () => {
      const feedUrl = feedPropUnsubscribeBtn.dataset.feedUrl;
      // ORPHAN_FOLDER_ID (0) means "feed is in no folder" (an Uncategorized
      // orphan). Real folder ids are always >= 1 and the Uncategorized virtual
      // folder is -1, so 0 is a safe not-a-folder sentinel: the backend still
      // purges the feed instead of the button silently no-op'ing.
      const parsedFolderId = parseInt((feedPropUnsubscribeBtn.dataset.folderIds || '').split(',')[0], 10);
      const folderId = Number.isNaN(parsedFolderId) ? ORPHAN_FOLDER_ID : parsedFolderId;
      if (!feedUrl) return;
      const title = feedPropRealTitle?.textContent || feedUrl;
      feedPropUnsubscribeBtn.disabled = true;
      const done = await unsubscribeFeedInteractive(feedUrl, folderId, { title });
      if (done) closeFeedPropertiesModal();
      else feedPropUnsubscribeBtn.disabled = false;
    });

    // Pause / Resume updates toggle
    feedPropDisableBtn?.addEventListener('click', async () => {
      const feedUrl = feedPropXml?.textContent?.trim();
      if (!feedUrl) return;
      const currentlyEnabled = feedPropDisableBtn.dataset.updatesEnabled === '1';
      const wantEnabled = !currentlyEnabled;  // Disable→pause; Enable→resume
      feedPropDisableBtn.disabled = true;
      if (feedPropDisableStatus) feedPropDisableStatus.textContent = wantEnabled ? 'Enabling…' : 'Disabling…';
      try {
        const body = new URLSearchParams({ feed_url: feedUrl, enabled: wantEnabled ? '1' : '0' });
        const resp = await fetch('/feeds/toggle-updates', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || 'toggle failed');
        if (feedPropUpdatesLabel) feedPropUpdatesLabel.textContent = wantEnabled ? 'Active' : 'Disabled';
        feedPropDisableBtn.textContent = wantEnabled ? 'Disable feed' : 'Enable feed';
        feedPropDisableBtn.dataset.updatesEnabled = wantEnabled ? '1' : '0';
        if (feedPropDisableStatus) feedPropDisableStatus.textContent = '';
      } catch (err) {
        if (feedPropDisableStatus) feedPropDisableStatus.textContent = `Error: ${err.message}`;
      } finally {
        feedPropDisableBtn.disabled = false;
      }
    });

    // Change feed URL
    feedPropChangeUrlBtn?.addEventListener('click', () => {
      if (!feedPropChangeUrlWrap || !feedPropChangeUrlInput) return;
      feedPropChangeUrlInput.value = feedPropXml?.textContent || '';
      feedPropChangeUrlWrap.hidden = false;
      feedPropChangeUrlBtn.hidden = true;
      feedPropChangeUrlStatus.textContent = '';
      feedPropChangeUrlInput.focus();
      feedPropChangeUrlInput.select();
    });
    feedPropChangeUrlCancel?.addEventListener('click', () => {
      if (feedPropChangeUrlWrap) feedPropChangeUrlWrap.hidden = true;
      if (feedPropChangeUrlBtn) feedPropChangeUrlBtn.hidden = false;
    });
    feedPropChangeUrlSave?.addEventListener('click', async () => {
      const oldUrl = feedPropXml?.textContent?.trim();
      const newUrl = feedPropChangeUrlInput?.value?.trim();
      if (!oldUrl || !newUrl || newUrl === oldUrl) {
        if (feedPropChangeUrlWrap) feedPropChangeUrlWrap.hidden = true;
        if (feedPropChangeUrlBtn) feedPropChangeUrlBtn.hidden = false;
        return;
      }
      feedPropChangeUrlSave.disabled = true;
      feedPropChangeUrlStatus.textContent = 'Saving…';
      try {
        const body = new URLSearchParams({ old_url: oldUrl, new_url: newUrl });
        const resp = await fetch('/feeds/change-url', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || 'Change failed');
        feedPropChangeUrlStatus.textContent = '';
        if (feedPropChangeUrlWrap) feedPropChangeUrlWrap.hidden = true;
        if (feedPropChangeUrlBtn) feedPropChangeUrlBtn.hidden = false;
        // Reload properties for the new URL
        window.location.assign(`/?${new URLSearchParams({ ...Object.fromEntries(new URLSearchParams(window.location.search)), list_feed_url: newUrl })}`);
      } catch (err) {
        feedPropChangeUrlStatus.textContent = `Error: ${err.message}`;
      } finally {
        feedPropChangeUrlSave.disabled = false;
      }
    });

    feedPropReparseBtn?.addEventListener('click', async () => {
      const feedUrl = feedPropXml?.textContent?.trim();
      if (!feedUrl) return;
      feedPropReparseBtn.disabled = true;
      if (feedPropReparseStatus) feedPropReparseStatus.textContent = 'Re-parsing…';
      try {
        const body = new URLSearchParams({ feed_url: feedUrl });
        const resp = await fetch('/feeds/reparse', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || 'Re-parse failed');
        if (feedPropReparseStatus) {
          feedPropReparseStatus.textContent = json.modified ? `Updated ${json.modified} post(s)` : 'No changes';
          setTimeout(() => { feedPropReparseStatus.textContent = ''; }, 4000);
        }
      } catch (err) {
        if (feedPropReparseStatus) feedPropReparseStatus.textContent = `Error: ${err.message}`;
      } finally {
        feedPropReparseBtn.disabled = false;
      }
    });

    feedPropBrowserUaReset?.addEventListener('click', async () => {
      const feedUrl = feedPropXml?.textContent?.trim();
      if (!feedUrl) return;
      feedPropBrowserUaReset.disabled = true;
      try {
        const body = new URLSearchParams({ feed_url: feedUrl, enabled: '0' });
        const resp = await fetch('/feeds/browser-ua', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
        const json = await resp.json();
        if (json.ok) {
          if (feedPropBrowserUaRow) feedPropBrowserUaRow.hidden = true;
          const forceRow = document.getElementById('feed-prop-browser-ua-force-row');
          if (forceRow) forceRow.hidden = false;
        }
      } catch { /* leave row visible on error */ } finally {
        feedPropBrowserUaReset.disabled = false;
      }
    });

    document.getElementById('feed-prop-browser-ua-force')?.addEventListener('click', async () => {
      const feedUrl = feedPropXml?.textContent?.trim();
      if (!feedUrl) return;
      const btn = document.getElementById('feed-prop-browser-ua-force');
      if (btn) btn.disabled = true;
      try {
        const body = new URLSearchParams({ feed_url: feedUrl, enabled: '1' });
        const resp = await fetch('/feeds/browser-ua', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
        const json = await resp.json();
        if (json.ok) {
          const forceRow = document.getElementById('feed-prop-browser-ua-force-row');
          if (forceRow) forceRow.hidden = true;
          if (feedPropBrowserUaRow) feedPropBrowserUaRow.hidden = false;
          showToastMessage('Browser identity enabled — feed will retry on next refresh.');
        }
      } catch { showToastMessage('Failed to update feed identity.'); } finally {
        if (btn) btn.disabled = false;
      }
    });

    feedPropStrategy?.addEventListener('change', async () => {
      const feedUrl = feedPropStrategy.dataset.feedUrl;
      const strategy = feedPropStrategy.value;
      if (!feedUrl) return;
      setActivePreset('');
      try {
        const body = new URLSearchParams({ feed_url: feedUrl, strategy });
        const resp = await fetch('/feeds/strategy', { method: 'POST', body });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (feedPropStrategyHint) {
          feedPropStrategyHint.textContent = strategy === 'auto' ? '' : 'saved';
          if (strategy !== 'auto') setTimeout(() => { feedPropStrategyHint.textContent = ''; }, 1500);
        }
      } catch (e) {
        if (feedPropStrategyHint) feedPropStrategyHint.textContent = 'error saving';
      }
    });

    feedPropPresetBtns.forEach(btn => {
      btn.addEventListener('click', async () => {
        const feedUrl = btn.dataset.feedUrl;
        if (!feedUrl) return;
        const preset = btn.dataset.preset;
        const isActive = btn.classList.contains('active');
        const strategy = isActive ? 'auto' : preset;
        setActivePreset(isActive ? '' : preset);
        if (feedPropStrategy) feedPropStrategy.value = 'auto';
        if (feedPropStrategyHint) feedPropStrategyHint.textContent = '';
        try {
          const body = new URLSearchParams({ feed_url: feedUrl, strategy });
          await fetch('/feeds/strategy', { method: 'POST', body });
        } catch (e) {
          if (feedPropStrategyHint) feedPropStrategyHint.textContent = 'error saving';
        }
      });
    });

    async function saveDisplayPref(feedUrl, key, value) {
      const body = new URLSearchParams({ feed_url: feedUrl, key, value: String(value) });
      const resp = await fetch('/feeds/display-prefs', { method: 'POST', body });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      try { return await resp.json(); } catch (e) { return {}; }
    }

    document.querySelectorAll('.feed-prop-crop-pos-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const feedUrl = btn.dataset.feedUrl;
        if (!feedUrl) return;
        const mode = document.querySelector('.feed-prop-crop-mode-btn.active')?.dataset.mode || 'cover';
        if (mode !== 'cover') return;
        const cropVal = buildCropValue(mode, btn.dataset.pos);
        document.querySelectorAll('.feed-prop-crop-pos-btn').forEach(b => b.classList.toggle('active', b === btn));
        applyCropToThumbnails(feedUrl, cropVal);
        try { await fetch('/feeds/thumb-crop', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, crop: cropVal }), credentials: 'same-origin' }); } catch (_e) {}
      });
    });
    document.querySelectorAll('.feed-prop-crop-mode-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const feedUrl = btn.dataset.feedUrl;
        if (!feedUrl) return;
        const mode = btn.dataset.mode;
        const pos = document.querySelector('.feed-prop-crop-pos-btn.active')?.dataset.pos || 'center';
        const cropVal = buildCropValue(mode, pos);
        document.querySelectorAll('.feed-prop-crop-mode-btn').forEach(b => b.classList.toggle('active', b === btn));
        const posSection = document.getElementById('feed-prop-crop-pos-section');
        if (posSection) posSection.style.opacity = mode === 'cover' ? '' : '0.4';
        const msSection = document.getElementById('feed-prop-smart-ms-section');
        if (msSection) msSection.style.display = mode === 'smart' ? '' : 'none';
        const fzSection = document.getElementById('feed-prop-fill-zoom-section');
        if (fzSection) fzSection.style.display = mode === 'cover' ? '' : 'none';
        applyCropToThumbnails(feedUrl, cropVal);
        try { await fetch('/feeds/thumb-crop', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, crop: cropVal }), credentials: 'same-origin' }); } catch (_e) {}
      });
    });

    document.getElementById('feed-prop-smart-min-scale')?.addEventListener('change', async () => {
      const input = document.getElementById('feed-prop-smart-min-scale');
      const feedUrl = input?.dataset.feedUrl;
      if (!feedUrl) return;
      let ms = input.value.trim();
      if (ms) {
        const parsed = parseFloat(ms);
        if (Number.isNaN(parsed)) { input.value = ''; ms = ''; }
        else { ms = String(Math.min(1.0, Math.max(0.5, parsed))); input.value = ms; }
      }
      try {
        await fetch('/feeds/smart-min-scale', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, min_scale: ms }), credentials: 'same-origin' });
      } catch (_e) { return; }
      // Re-render this feed's smart-cropped thumbnails with the new sensitivity.
      const previewImg = document.getElementById('feed-prop-thumb-preview-img');
      if (previewImg instanceof HTMLImageElement && previewImg.src) {
        previewImg.src = setThumbMsParam(previewImg.src, ms);
      }
      document.querySelectorAll(`.post-item[data-post-feed-url="${CSS.escape(feedUrl)}"]`).forEach(postItem => {
        postItem.dataset.smartMs = ms;
        if ((postItem.dataset.thumbCrop || 'cover') !== 'smart') return;
        const img = postItem.querySelector('.post-thumbnail-image');
        if (img instanceof HTMLImageElement && img.src) img.src = setThumbMsParam(img.src, ms);
      });
    });

    document.getElementById('feed-prop-fill-zoom')?.addEventListener('change', async () => {
      const input = document.getElementById('feed-prop-fill-zoom');
      const feedUrl = input?.dataset.feedUrl;
      if (!feedUrl) return;
      let fz = input.value.trim();
      if (fz) {
        const parsed = parseFloat(fz);
        if (Number.isNaN(parsed)) { input.value = ''; fz = ''; }
        else { fz = String(Math.min(2.0, Math.max(0.5, parsed))); input.value = fz; }
      }
      try {
        await fetch('/feeds/fill-zoom', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, zoom: fz }), credentials: 'same-origin' });
      } catch (_e) { return; }
      // Re-render this feed's fill-cropped thumbnails with the new zoom.
      const previewImg = document.getElementById('feed-prop-thumb-preview-img');
      if (previewImg instanceof HTMLImageElement && previewImg.src) {
        previewImg.src = setThumbFzParam(previewImg.src, fz);
      }
      document.querySelectorAll(`.post-item[data-post-feed-url="${CSS.escape(feedUrl)}"]`).forEach(postItem => {
        postItem.dataset.fillZoom = fz;
        const crop = postItem.dataset.thumbCrop || 'cover';
        if (crop === 'smart' || crop === 'contain') return;
        const img = postItem.querySelector('.post-thumbnail-image');
        if (img instanceof HTMLImageElement && img.src) img.src = setThumbFzParam(img.src, fz);
      });
    });

    // feedPropShowThumb listener removed — now handled by thumb-source dropdown

    feedPropShowInArticle?.addEventListener('change', async () => {
      const feedUrl = feedPropShowInArticle.dataset.feedUrl;
      if (!feedUrl) return;
      try { await saveDisplayPref(feedUrl, 'show_lead_image_in_article', feedPropShowInArticle.checked ? 1 : 0); }
      catch (e) { feedPropShowInArticle.checked = !feedPropShowInArticle.checked; }
    });


    async function saveCaptionSource(feedUrl) {
      if (!feedUrl) return;
      const titleOn = feedPropCaptionTitle?.checked;
      const altOn   = feedPropCaptionAlt?.checked;
      const source  = titleOn && altOn ? 'both' : titleOn ? 'title' : altOn ? 'alt' : 'none';
      feedPropCaptionAutoBtn?.classList.remove('active');
      await fetch('/feeds/caption-source', {
        method: 'POST',
        body: new URLSearchParams({ feed_url: feedUrl, source }),
        credentials: 'same-origin',
      });
    }
    feedPropCaptionTitle?.addEventListener('change', () => saveCaptionSource(feedPropCaptionTitle.dataset.feedUrl));
    feedPropCaptionAlt?.addEventListener('change',   () => saveCaptionSource(feedPropCaptionAlt.dataset.feedUrl));
    feedPropCaptionAutoBtn?.addEventListener('click', async () => {
      const feedUrl = feedPropCaptionAutoBtn.dataset.feedUrl;
      if (!feedUrl) return;
      if (feedPropCaptionTitle) feedPropCaptionTitle.checked = false;
      if (feedPropCaptionAlt)   feedPropCaptionAlt.checked   = false;
      feedPropCaptionAutoBtn.classList.add('active');
      await fetch('/feeds/caption-source', {
        method: 'POST',
        body: new URLSearchParams({ feed_url: feedUrl, source: 'auto' }),
        credentials: 'same-origin',
      });
    });

    document.getElementById('feed-prop-thumbnail-url-save')?.addEventListener('click', async () => {
      const btn = document.getElementById('feed-prop-thumbnail-url-save');
      const input = document.getElementById('feed-prop-thumbnail-url');
      const feedUrl = btn?.dataset.feedUrl || document.getElementById('feed-prop-thumb-source')?.dataset.feedUrl;
      if (!feedUrl || !input) return;
      try {
        const newUrl = input.value.trim();
        const body = new URLSearchParams({ feed_url: feedUrl, thumbnail_url: newUrl });
        const resp = await fetch('/feeds/thumbnail-url', { method: 'POST', body, credentials: 'same-origin' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        // A pinned URL implies thumbnails on (server re-enables the show flag);
        // clear any per-entry-inline strategy override like the dropdown branches do.
        await fetch('/feeds/thumb-strategy', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, strategy: '' }), credentials: 'same-origin' });
        btn.textContent = 'Saved';
        const _ts2 = document.getElementById('feed-prop-thumb-source');
        if (_ts2) { _ts2.dataset.savedThumbUrl = newUrl; _ts2.dataset.thumbStrategy = ''; }
        applyThumbSourceUI();
        setTimeout(() => { btn.textContent = 'Save'; }, 1500);
      } catch (e) {
        btn.textContent = 'Error';
        setTimeout(() => { btn.textContent = 'Save'; }, 1500);
      }
    });

    document.getElementById('feed-prop-thumb-source')?.addEventListener('change', async () => {
      const select = document.getElementById('feed-prop-thumb-source');
      const feedUrl = select.dataset.feedUrl;
      applyThumbSourceUI();
      if (!feedUrl) return;
      const val = select.value;
      if (val === '__custom__') return; // wait for Save button
      if (val === '__disabled__') {
        await Promise.all([
          fetch('/feeds/thumbnail-url', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, thumbnail_url: '' }), credentials: 'same-origin' }),
          saveDisplayPref(feedUrl, 'show_lead_image_as_thumb', 0),
          fetch('/feeds/thumb-strategy', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, strategy: '' }), credentials: 'same-origin' }),
        ]);
        select.dataset.thumbStrategy = '';
        return;
      }
      // Re-enable thumbnails if they were disabled
      await saveDisplayPref(feedUrl, 'show_lead_image_as_thumb', 1);
      if (val === '__per_entry_inline__' || val === '__per_entry_media_rss__') {
        // Per-entry strategy: clear pinned URL, set thumb_strategy
        const strategy = val === '__per_entry_media_rss__' ? 'media_rss' : 'inline';
        select.dataset.thumbStrategy = strategy;  // update immediately so doStrategyRefresh sees it
        await Promise.all([
          fetch('/feeds/thumbnail-url', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, thumbnail_url: '' }), credentials: 'same-origin' }),
          fetch('/feeds/thumb-strategy', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, strategy }), credentials: 'same-origin' }),
        ]);
        select.dataset.savedThumbUrl = '';
        return;
      }
      // Any other selection clears thumb_strategy
      select.dataset.thumbStrategy = '';  // update immediately so doStrategyRefresh sees it
      await fetch('/feeds/thumb-strategy', { method: 'POST', body: new URLSearchParams({ feed_url: feedUrl, strategy: '' }), credentials: 'same-origin' });
      let url = '';
      if (val === '__favicon__') url = '__favicon__';
      else if (val.startsWith('__strat__')) url = val.slice(9);
      const body = new URLSearchParams({ feed_url: feedUrl, thumbnail_url: url });
      await fetch('/feeds/thumbnail-url', { method: 'POST', body, credentials: 'same-origin' });
      select.dataset.savedThumbUrl = url;
    });

    feedPropRefreshBtn?.addEventListener('click', () => {
      const feedUrl = feedPropRefreshBtn.dataset.feedUrl;
      if (!feedUrl) return;
      doStrategyRefresh(feedUrl, feedPropRefreshBtn.dataset.entryId || '');
    });

    feedPropHideShorts?.addEventListener('change', async () => {
      const feedUrl = feedPropHideShorts.dataset.feedUrl;
      if (!feedUrl) return;
      try {
        const r = await saveDisplayPref(feedUrl, 'hide_shorts', feedPropHideShorts.checked ? 1 : 0);
        // Enabling clears the existing Shorts backlog server-side; reflect it.
        if (r && r.marked_read > 0) {
          try { await _refreshSidebarCounts(); } catch (e) { console.error('hide-shorts: sidebar refresh failed', e); }
          try { await refreshCurrentFeedOrFolder(); } catch (e) { console.error('hide-shorts: view refresh failed', e); }
        }
      }
      catch (e) { feedPropHideShorts.checked = !feedPropHideShorts.checked; }
    });

    document.getElementById('feed-prop-devto-save')?.addEventListener('click', async () => {
      const saveBtn = document.getElementById('feed-prop-devto-save');
      const statusEl = document.getElementById('feed-prop-devto-status');
      const feedId = saveBtn.dataset.devtoFeedId;
      if (!feedId) return;
      saveBtn.disabled = true;
      statusEl.textContent = 'Saving…';
      try {
        const resp = await fetch(`/devto-feeds/${encodeURIComponent(feedId)}/config`, {
          method: 'POST',
          body: new URLSearchParams({
            devto_tag: document.getElementById('feed-prop-devto-tag').value.trim(),
            devto_top_days: document.getElementById('feed-prop-devto-top').value.trim(),
            devto_english_only: document.getElementById('feed-prop-devto-english').checked ? '1' : '0',
            devto_min_reactions: document.getElementById('feed-prop-devto-minreact').value.trim(),
            devto_tags_exclude: document.getElementById('feed-prop-devto-exclude').value.trim(),
          }),
        });
        const json = await resp.json();
        statusEl.textContent = json.ok ? 'Saved — feed refetched.' : (json.error || 'Save failed.');
      } catch (e) {
        statusEl.textContent = 'Save failed.';
      } finally {
        saveBtn.disabled = false;
      }
    });

    feedPropInjectSourceImages?.addEventListener('change', async () => {
      const feedUrl = feedPropInjectSourceImages.dataset.feedUrl;
      if (!feedUrl) return;
      try { await saveDisplayPref(feedUrl, 'inject_source_images', feedPropInjectSourceImages.checked ? 1 : 0); }
      catch (e) { feedPropInjectSourceImages.checked = !feedPropInjectSourceImages.checked; }
    });

    feedPropFlushBatchBtn?.addEventListener('click', async () => {
      if (!feedPropFlushBatchStatus) return;
      feedPropFlushBatchBtn.disabled = true;
      feedPropFlushBatchStatus.textContent = 'Flushing…';
      try {
        const resp = await fetch('/dev/flush-email-batch', { method: 'POST' });
        const json = await resp.json();
        feedPropFlushBatchStatus.textContent = json.ok ? 'Done.' : `Error: ${json.error}`;
      } catch (e) {
        feedPropFlushBatchStatus.textContent = `Error: ${e.message}`;
      } finally {
        feedPropFlushBatchBtn.disabled = false;
      }
    });

    feedPropStratGrid?.addEventListener('click', (e) => {
      // Handle "Use as thumbnail" pin button
      const useBtn = e.target.closest('.feed-prop-use-as-thumb');
      if (useBtn) {
        e.stopPropagation();
        const url = useBtn.dataset.url;
        const select = document.getElementById('feed-prop-thumb-source');
        const feedUrl = select?.dataset.feedUrl;
        if (!url || !feedUrl) return;
        // Find or create __strat__ option matching this URL
        const matchOpt = select ? [...select.options].find(o => o.value === '__strat__' + url) : null;
        if (matchOpt) {
          select.value = matchOpt.value;
        } else {
          select.value = '__custom__';
          const input = document.getElementById('feed-prop-thumbnail-url');
          if (input) input.value = url;
        }
        applyThumbSourceUI();
        const body = new URLSearchParams({ feed_url: feedUrl, thumbnail_url: url });
        fetch('/feeds/thumbnail-url', { method: 'POST', body, credentials: 'same-origin' });
        return;
      }

      const card = e.target.closest('.feed-prop-strat-card[data-strategy]');
      if (!card || !feedPropStrategy) return;
      const strategy = card.dataset.strategy;
      if (!SELECTABLE_STRATEGIES.has(strategy)) return;
      feedPropStrategy.value = strategy;
      feedPropStrategy.dispatchEvent(new Event('change'));
      feedPropStratGrid.querySelectorAll('.feed-prop-strat-card').forEach(c => {
        c.classList.toggle('active', c.dataset.strategy === strategy);
      });
      updateCaptionPreviews(_lastStratCacheRows);
    });


    contextMenu?.addEventListener('click', (event) => {
      event.stopPropagation();
    });
    rootContextMenu?.addEventListener('click', (event) => {
      event.stopPropagation();
    });
    postContextMenu?.addEventListener('click', (event) => {
      event.stopPropagation();
    });

    for (const folderLink of document.querySelectorAll('.tree-item')) {
      folderLink.addEventListener('contextmenu', (event) => {
        if (!contextMenu) {
          return;
        }

        event.preventDefault();
        event.stopPropagation();

        contextFolderId = folderLink.getAttribute('data-folder-id');
        contextFeedUrl = null;
        contextFolderName = folderLink.getAttribute('data-folder-name') || 'folder';
        contextFolderDepth = Number(folderLink.getAttribute('data-folder-depth') || '0');
        contextTargetType = 'folder';
        const isRootFolder = folderLink.classList.contains('root-item');

        if (isRootFolder) {
          showRootContextMenu(event);
          return;
        }

        // The virtual "Uncategorized" folder is derived, not a real folder row:
        // expose only whole-folder actions, hide anything that would rename,
        // delete, or otherwise mutate a folder that doesn't exist in the DB.
        if (folderLink.hasAttribute('data-virtual')) {
          setMenuItemVisible(refreshButton, true);
          setMenuItemVisible(markReadButton, true);
          setMenuItemVisible(addToFolderWrap, false);
          setMenuItemVisible(addFeedButton, false);
          setMenuItemVisible(feedPropertiesButton, false);
          setMenuItemVisible(folderPropertiesButton, false);
          setMenuItemVisible(highlightsButton, false);
          setMenuItemVisible(unsubscribeFeedButton, false);
          setMenuItemVisible(renameFolderButton, false);
          setMenuItemVisible(deleteFolderButton, false);
          setMenuItemVisible(youtubeSyncButton, false);
          setMenuItemVisible(disableFeedButton, false);
          hideFolderSubmenu();
          showContextMenu(event);
          return;
        }

        setMenuItemVisible(refreshButton, true);
        setMenuItemVisible(markReadButton, true);
        setMenuItemVisible(addToFolderWrap, false);
        setMenuItemVisible(addFeedButton, true);
        setMenuItemVisible(feedPropertiesButton, false);
        setMenuItemVisible(folderPropertiesButton, true);
        setMenuItemVisible(highlightsButton, true);
        setMenuItemVisible(unsubscribeFeedButton, false);
        setMenuItemVisible(renameFolderButton, true);
        setMenuItemVisible(deleteFolderButton, true);
        setMenuItemVisible(disableFeedButton, false);
        // Detect the YouTube folder by CONTENT (any feed in it is a YouTube feed),
        // so renaming the folder doesn't break "Sync Subscriptions". Fall back to an
        // exact match against the configured folder name (_ytFolderName, loaded from
        // Settings) to handle an as-yet-empty folder before the first sync.
        const _ytFid = folderLink.getAttribute('data-folder-id');
        const _hasYtFeed = !!(_ytFid && document.querySelector(
          `.feed-link[data-folder-id="${CSS.escape(_ytFid)}"][data-feed-url*="youtube.com/feeds/videos.xml"]`));
        const isYouTubeFolder = _hasYtFeed || (!!_ytFolderName && contextFolderName === _ytFolderName);
        setMenuItemVisible(youtubeSyncButton, isYouTubeFolder);
        if (isYouTubeFolder && youtubeSyncButton) {
          youtubeSyncButton.title = youtubeSyncLastAt
            ? `Last sync: ${youtubeSyncLastAt}\n${youtubeSyncLastResult}`
            : 'No sync run yet';
        }
        hideFolderSubmenu();

        showContextMenu(event);
      });

      // Double-click a folder row → open its Properties. Skip the root folder
      // and the derived virtual "Uncategorized" folder, which have no editable
      // properties.
      folderLink.addEventListener('dblclick', (event) => {
        if (folderLink.classList.contains('root-item') || folderLink.hasAttribute('data-virtual')) {
          return;
        }
        const fid = folderLink.getAttribute('data-folder-id');
        if (!fid) {
          return;
        }
        event.preventDefault();
        hideAllContextMenus();
        openFolderPropertiesModal(Number(fid));
      });
    }

    // Delegated on the tree nav (not per feed link): sidebar feed rows are
    // lazy-injected on folder expand, so per-element bindings would miss them.
    // stopPropagation at the nav keeps the document-level tree-background
    // context menu from firing, matching the old per-element behavior.
    {
      const treeNav = document.querySelector('nav.tree');
      treeNav?.addEventListener('contextmenu', (event) => {
        const feedLink = event.target.closest('.feed-link');
        if (!feedLink || !contextMenu) {
          return;
        }

        event.preventDefault();
        event.stopPropagation();

        contextFolderId = feedLink.getAttribute('data-folder-id');
        contextFeedUrl = feedLink.getAttribute('data-feed-url');
        contextFolderName = feedLink.textContent || 'feed';
        contextFolderDepth = 1;
        contextTargetType = 'feed';

        setMenuItemVisible(refreshButton, true);
        setMenuItemVisible(markReadButton, true);
        setMenuItemVisible(addToFolderWrap, true);
        setMenuItemVisible(addFeedButton, false);
        setMenuItemVisible(feedPropertiesButton, true);
        setMenuItemVisible(folderPropertiesButton, false);
        setMenuItemVisible(highlightsButton, true);
        setMenuItemVisible(unsubscribeFeedButton, true);
        setMenuItemVisible(renameFolderButton, false);
        setMenuItemVisible(deleteFolderButton, false);
        setMenuItemVisible(youtubeSyncButton, false);
        setMenuItemVisible(disableFeedButton, true);
        updateFolderSubmenuOptions();
        hideFolderSubmenu();

        showContextMenu(event);
      });

      // Double-click a feed row → open its Properties.
      treeNav?.addEventListener('dblclick', (event) => {
        const feedLink = event.target.closest('.feed-link');
        if (!feedLink) {
          return;
        }
        const feedUrl = feedLink.getAttribute('data-feed-url');
        if (!feedUrl) {
          return;
        }
        event.preventDefault();
        hideAllContextMenus();
        openFeedPropertiesModal(feedUrl);
      });
    }

    // Capture-phase delegation for feed links in the posts list and entry pane.
    // Fires before the post-item contextmenu handler so those links get the
    // feed context menu (with Properties) instead of the post context menu.
    document.addEventListener('contextmenu', (event) => {
      const feedLinkTarget = event.target.closest('.post-feed-link, .entry-feed-link');
      if (!feedLinkTarget || !contextMenu) return;
      event.preventDefault();
      event.stopPropagation();
      contextFeedUrl = feedLinkTarget.getAttribute('data-feed-url');
      contextFolderId = feedLinkTarget.getAttribute('data-folder-id');
      contextFolderName = feedLinkTarget.textContent?.trim() || 'feed';
      contextFolderDepth = 1;
      contextTargetType = 'feed';
      setMenuItemVisible(refreshButton, true);
      setMenuItemVisible(markReadButton, true);
      setMenuItemVisible(addToFolderWrap, true);
      setMenuItemVisible(addFeedButton, false);
      setMenuItemVisible(feedPropertiesButton, true);
      setMenuItemVisible(folderPropertiesButton, false);
      setMenuItemVisible(highlightsButton, true);
      setMenuItemVisible(unsubscribeFeedButton, true);
      setMenuItemVisible(renameFolderButton, false);
      setMenuItemVisible(deleteFolderButton, false);
      setMenuItemVisible(youtubeSyncButton, false);
      setMenuItemVisible(disableFeedButton, true);
      updateFolderSubmenuOptions();
      hideFolderSubmenu();
      showContextMenu(event);
    }, true);

    // Right-click a tag (sidebar list or article-pane chip) to manage it.
    // Capture phase so it wins over the tag links' own navigation handlers.
    document.addEventListener('contextmenu', (event) => {
      if (!(event.target instanceof Element) || !tagContextMenu) {
        return;
      }
      const tagTarget = event.target.closest('.tag-link, .entry-tag-link');
      if (!tagTarget) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      // Sidebar links carry the tag in data via the href; chips render #tag text.
      const params = new URLSearchParams((tagTarget.getAttribute('href') || '').split('?')[1] || '');
      contextTagName = (params.get('tag') || tagTarget.textContent || '').trim().replace(/^#+/, '').toLowerCase();
      if (!contextTagName) {
        return;
      }
      showTagContextMenu(event);
    }, true);

    document.getElementById('ctx-tag-delete')?.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const tagName = contextTagName;
      hideAllContextMenus();
      if (!tagName) {
        return;
      }
      if (!window.confirm(`Delete the tag "#${tagName}" from every post?\n\nThis removes the tag everywhere it appears. This cannot be undone.`)) {
        return;
      }
      try {
        const body = new URLSearchParams({ tag: tagName });
        const resp = await fetch('/tags/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-sidebar' },
          credentials: 'same-origin',
          body: body.toString(),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (!data.ok) {
          showToastMessage(data.error || 'Failed to delete tag.');
          return;
        }
        // Drop the tag's filter from the URL if it was active, then reload so
        // the sidebar counts and any tagged-view list refresh consistently.
        const urlParams = new URLSearchParams(window.location.search);
        if ((urlParams.get('tag') || '').toLowerCase() === tagName) {
          urlParams.delete('tag');
        }
        window.location.assign('/?' + urlParams.toString());
      } catch {
        showToastMessage('Failed to delete tag.');
      }
    });

    // Tag rename
    const tagRenameModal = document.getElementById('tag-rename-modal');
    const tagRenameInput = document.getElementById('tag-rename-input');
    const tagRenameOldLabel = document.getElementById('tag-rename-old');
    const tagRenameError = document.getElementById('tag-rename-error');

    document.getElementById('ctx-tag-rename')?.addEventListener('click', () => {
      const tagName = contextTagName;
      hideAllContextMenus();
      if (!tagName || !tagRenameModal || !tagRenameInput) return;
      if (tagRenameOldLabel) tagRenameOldLabel.textContent = `#${tagName}`;
      if (tagRenameError) { tagRenameError.textContent = ''; tagRenameError.hidden = true; }
      tagRenameInput.value = tagName;
      tagRenameModal.hidden = false;
      tagRenameInput.focus();
      tagRenameInput.select();
    });

    async function doTagRename(oldTag, newTag, force) {
      const body = new URLSearchParams({ old_tag: oldTag, new_tag: newTag });
      if (force) body.set('force', '1');
      const resp = await fetch('/tags/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-sidebar' },
        credentials: 'same-origin',
        body: body.toString(),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return resp.json();
    }

    document.getElementById('tag-rename-confirm')?.addEventListener('click', async () => {
      if (!tagRenameModal || !tagRenameInput) return;
      const oldTag = contextTagName;
      const newTag = tagRenameInput.value.trim().toLowerCase().replace(/^#+/, '');
      if (!newTag) {
        if (tagRenameError) { tagRenameError.textContent = 'Enter a name.'; tagRenameError.hidden = false; }
        return;
      }
      if (newTag === oldTag) {
        tagRenameModal.hidden = true;
        return;
      }
      try {
        let data = await doTagRename(oldTag, newTag, false);
        if (!data.ok && data.exists) {
          if (!window.confirm(`Tag "#${newTag}" already exists.\n\nRenaming will combine both tags into "#${newTag}". Continue?`)) return;
          data = await doTagRename(oldTag, newTag, true);
        }
        if (!data.ok) {
          if (tagRenameError) { tagRenameError.textContent = data.error || 'Rename failed.'; tagRenameError.hidden = false; }
          return;
        }
        tagRenameModal.hidden = true;
        const urlParams = new URLSearchParams(window.location.search);
        if ((urlParams.get('tag') || '').toLowerCase() === oldTag) {
          urlParams.set('tag', newTag);
        }
        window.location.assign('/?' + urlParams.toString());
      } catch {
        if (tagRenameError) { tagRenameError.textContent = 'Rename failed.'; tagRenameError.hidden = false; }
      }
    });

    tagRenameInput?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') document.getElementById('tag-rename-confirm')?.click();
      if (e.key === 'Escape') { if (tagRenameModal) tagRenameModal.hidden = true; }
    });

    refreshButton?.addEventListener('click', () => {
      if (contextTargetType === 'feed') {
        if (
          !contextFolderId ||
          !contextFeedUrl ||
          !refreshFeedFolderIdInput ||
          !refreshFeedUrlInput ||
          !refreshListFeedUrlInput ||
          !refreshFeedForm
        ) {
          return;
        }

        refreshFeedFolderIdInput.value = contextFolderId;
        refreshFeedUrlInput.value = contextFeedUrl;
        refreshListFeedUrlInput.value = contextFeedUrl;
        hideContextMenu();
        refreshFeedForm.submit();
        return;
      }

      if (!contextFolderId || !refreshFolderIdInput || !refreshFolderForm) {
        return;
      }

      refreshFolderIdInput.value = contextFolderId;
      hideContextMenu();
      refreshFolderForm.submit();
    });

    function bindEntryPaneInteractions() {
      const entrySaveForm = document.querySelector('.entry-save-toggle-form');
      if (entrySaveForm && !entrySaveForm.dataset.boundAsyncSubmit) {
        entrySaveForm.dataset.boundAsyncSubmit = '1';
        entrySaveForm.addEventListener('submit', async (event) => {
          event.preventDefault();

          const savedInput = entrySaveForm.querySelector('input[name="saved"]');
          const feedUrlInput = entrySaveForm.querySelector('input[name="feed_url"]');
          const entryIdInput = entrySaveForm.querySelector('input[name="entry_id"]');
          if (!(savedInput instanceof HTMLInputElement) || !(feedUrlInput instanceof HTMLInputElement) || !(entryIdInput instanceof HTMLInputElement)) {
            entrySaveForm.submit();
            return;
          }

          const nextIsSaved = savedInput.value === '1';

          // Capture the body before the optimistic update — the update flips
          // savedInput.value, so FormData collected after would send the wrong value.
          const formData = new FormData(entrySaveForm);
          const body = new URLSearchParams();
          for (const [key, value] of formData.entries()) {
            body.append(key, String(value));
          }

          applyEntryPaneSavedState(nextIsSaved);

          const linkedPostItem = document.querySelector(`.post-item[data-post-feed-url="${CSS.escape(feedUrlInput.value)}"][data-post-entry-id="${CSS.escape(entryIdInput.value)}"]`);
          if (linkedPostItem instanceof HTMLElement) {
            applyPostItemSavedState(linkedPostItem, nextIsSaved);
          }
          // (Un)starring an unread post moves it in/out of the Saved backlog.
          const starredUnread = linkedPostItem instanceof HTMLElement
            && linkedPostItem.getAttribute('data-post-read') === '0';
          if (starredUnread) adjustSavedUnreadBadge(nextIsSaved ? +1 : -1);
          const paneStarFolderId = (linkedPostItem instanceof HTMLElement
            ? linkedPostItem.getAttribute('data-post-folder-id')
            : document.querySelector('.entry-pane-title')?.getAttribute('data-post-folder-id'));
          adjustSavedFolderBadge(paneStarFolderId, nextIsSaved ? +1 : -1);

          try {
            const response = await fetch(entrySaveForm.action, {
              method: 'POST',
              credentials: 'same-origin',
              headers: {
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'X-Requested-With': 'lectio-entry-save-toggle',
              },
              body: body.toString(),
            });

            if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
            }
          } catch (_error) {
            applyEntryPaneSavedState(!nextIsSaved);
            if (linkedPostItem instanceof HTMLElement) {
              applyPostItemSavedState(linkedPostItem, !nextIsSaved);
            }
            if (starredUnread) adjustSavedUnreadBadge(nextIsSaved ? -1 : +1);
            adjustSavedFolderBadge(paneStarFolderId, nextIsSaved ? -1 : +1);
            entrySaveForm.submit();
          }
        });
      }

      if (entryPaneTitle && !entryPaneTitle.dataset.boundContextMenu) {
        entryPaneTitle.dataset.boundContextMenu = '1';
        entryPaneTitle.addEventListener('contextmenu', (event) => {
          if (!postContextMenu) {
            return;
          }

          event.preventDefault();
          event.stopPropagation();
          contextPostFeedUrl = entryPaneTitle.getAttribute('data-post-feed-url');
          contextPostEntryId = entryPaneTitle.getAttribute('data-post-entry-id');
          contextPostRead = entryPaneTitle.getAttribute('data-post-read') === '1';
          contextPostLink = entryPaneTitle.getAttribute('data-post-link') || '';
          contextPostTitle = entryPaneTitle.getAttribute('data-post-title') || '';
          contextPostFolderId = entryPaneTitle.getAttribute('data-post-folder-id') || null;
          if (postMarkReadButton) {
            postMarkReadButton.textContent = contextPostRead ? 'Mark as unread' : 'Mark as read';
          }
          setMenuItemVisible(postCopyUrlButton, Boolean(contextPostLink));
          setMenuItemVisible(postMarkFeedReadButton, Boolean(contextPostFeedUrl));
          setMenuItemVisible(postAutomationButton, Boolean(contextPostFeedUrl));
          setMenuItemVisible(postMoveToFeedButton, Boolean(contextPostFeedUrl && contextPostEntryId));
          setMenuItemVisible(postDeleteButton, Boolean(contextPostFeedUrl && contextPostEntryId));
          setMenuItemVisible(postEditDateButton, Boolean(contextPostFeedUrl && contextPostEntryId));
          setMenuItemVisible(postEditTitleButton, Boolean(contextPostFeedUrl && contextPostEntryId));
          setMenuItemVisible(postMoveVisibleButton, false);
          setMenuItemVisible(postMarkAboveReadButton, false);
          setMenuItemVisible(postMarkBelowReadButton, false);
          showPostContextMenu(event);
        });
      }

      if (entryReadabilityButton && !entryReadabilityButton.dataset.boundClick) {
        entryReadabilityButton.dataset.boundClick = '1';
        entryReadabilityButton.addEventListener('click', async (event) => {
          event.preventDefault();
          const sourceUrl = entryReadabilityButton.getAttribute('data-source-url');
          if (!sourceUrl || !entryBody) {
            return;
          }
          if (entryReadabilityButton.classList.contains('active')) {
            deactivateSourceView();
            return;
          }
          // Deactivate any active source view first.
          if (sourceViewActive) deactivateSourceView();

          const feedUrl = entryReadabilityButton.getAttribute('data-feed-url') || '';
          const entryId = entryReadabilityButton.getAttribute('data-entry-id') || '';
          const archiveQuery = (feedUrl && entryId)
            ? `&feed_url=${encodeURIComponent(feedUrl)}&entry_id=${encodeURIComponent(entryId)}`
            : '';
          const readabilityUrl = `/entries/readability?url=${encodeURIComponent(sourceUrl)}${archiveQuery}`;

          // Switch UI to readability mode.
          sourceViewMode = 'readability';
          sourceViewActive = true;
          sourceViewUrl = sourceUrl;
          setSourceModeIndicator('readability');
          entryReadabilityButton.classList.add('active');
          entrySourceButton?.classList.remove('active');
          if (entrySourceOpenExternal) entrySourceOpenExternal.href = safeHttpUrl(sourceUrl);
          entryBody.setAttribute('hidden', '');
          if (entryReadabilityContainer) {
            entryReadabilityContainer.innerHTML = '<p class="entry-readability-loading">Loading reader view…</p>';
            entryReadabilityContainer.removeAttribute('hidden');
          }
          try { if (window.isSingleMode && window.isSingleMode()) setSinglePaneLevel(2); } catch (e) {}
          entryArticle?.classList.add('source-active');

          try {
            const resp = await fetch(readabilityUrl, { credentials: 'same-origin' });
            const html = await resp.text();
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            const article = doc.querySelector('article');
            const titleText = doc.querySelector('h1')?.textContent || '';
            if (!entryReadabilityContainer || !sourceViewActive || sourceViewMode !== 'readability') return;
            entryReadabilityContainer.innerHTML = '';

            // Normalize an image URL for duplicate comparison: drop the query
            // string and collapse CDN size-variant path segments (Blogger/
            // Googleusercontent /sNNN/ and /wNNN-hNNN.../ before the filename) so
            // the same image at different sizes compares equal.
            const normImageKey = (u) => {
              try {
                const url = new URL(u, window.location.href);
                const path = url.pathname.replace(
                  /\/(?:s\d+|w\d+-h\d+)(?:-[a-z0-9-]+)?(\/[^/]+)$/i, '$1');
                return url.host + path;
              } catch (e) {
                return (u || '').split('?')[0];
              }
            };

            // Prepend lead image if present in the entry pane title metadata —
            // but not when the article body already contains that same image
            // (any CDN size variant), which would render it twice.
            const leadImageUrl = entryPaneTitle?.getAttribute('data-post-lead-image-url') || '';
            let leadInArticle = false;
            if (leadImageUrl && article) {
              const leadKey = normImageKey(leadImageUrl);
              leadInArticle = Array.from(article.querySelectorAll('img')).some(
                (im) => normImageKey(im.getAttribute('src') || '') === leadKey);
            }
            if (leadImageUrl && !leadInArticle) {
              const img = document.createElement('img');
              img.src = leadImageUrl;
              img.className = 'entry-readability-lead-image';
              img.alt = titleText;
              img.loading = 'lazy';
              img.onerror = function() {
                const _s = this.getAttribute('src');
                if (!_s || _s.startsWith('/api/img?')) {
                  this.style.display = 'none';
                } else {
                  this.setAttribute('src', '/api/img?u=' + encodeURIComponent(this.src));
                }
              };
              entryReadabilityContainer.appendChild(img);
            }

            if (article) {
              const contentDiv = document.createElement('div');
              contentDiv.className = 'entry-readability-content';
              contentDiv.innerHTML = article.innerHTML;
              entryReadabilityContainer.appendChild(contentDiv);
            } else {
              entryReadabilityContainer.innerHTML = '<p class="muted">Could not extract readable content.</p>';
            }
          } catch (err) {
            if (entryReadabilityContainer && sourceViewMode === 'readability') {
              entryReadabilityContainer.innerHTML = '<p class="muted">Failed to load reader view.</p>';
            }
          }
        });
      }

      if (entrySourceButton && !entrySourceButton.dataset.boundClick) {
        entrySourceButton.dataset.boundClick = '1';
        entrySourceButton.addEventListener('click', (event) => {
          event.preventDefault();
          const sourceUrl = entrySourceButton.getAttribute('data-source-url');
          if (!sourceUrl || !entryBody || !entrySourceFrame) {
            return;
          }
          if (entrySourceButton.classList.contains('active')) {
            deactivateSourceView();
          } else {
            frameCheckRequestToken += 1;
            const requestToken = frameCheckRequestToken;
            const frameCheckController = new AbortController();
            const frameCheckTimeoutId = window.setTimeout(() => {
              frameCheckController.abort();
            }, 2200);

            fetch(`/entries/frame-check?url=${encodeURIComponent(sourceUrl)}`, { signal: frameCheckController.signal })
              .then((response) => (response.ok ? response.json() : null))
              .then((frameCheck) => {
                if (requestToken !== frameCheckRequestToken) {
                  return;
                }

                if (frameCheck && frameCheck.blocked) {
                  const proxiedUrl = `/entries/source?url=${encodeURIComponent(sourceUrl)}`;
                  activateSourceView(proxiedUrl, entrySourceButton, 'source-proxy');
                } else {
                  activateSourceView(sourceUrl, entrySourceButton, 'source');
                }
              })
              .catch(() => {
                if (requestToken !== frameCheckRequestToken) {
                  return;
                }
                activateSourceView(sourceUrl, entrySourceButton, 'source');
              })
              .finally(() => {
                window.clearTimeout(frameCheckTimeoutId);
              });
          }
        });
      }

      if (entrySourceFrame && !entrySourceFrame.dataset.boundLoad) {
        entrySourceFrame.dataset.boundLoad = '1';
        entrySourceFrame.addEventListener('load', () => {
          sourceFrameLoaded = true;

          if (sourceViewMode === 'source' && !sourceFallbackAttempted) {
            try {
              const loadedUrl = entrySourceFrame.contentWindow?.location?.href || '';
              if (loadedUrl === 'about:blank') {
                if (sourceDirectLoaded) {
                  // Page had already loaded successfully. This about:blank is from
                  // a subsequent JS-driven navigation (e.g. the site's own scripts
                  // or a redirect chain) — not a framing block. Leave it alone.
                  return;
                }
                // First load ended at about:blank — site blocked framing.
                sourceFrameLoaded = false;
                fallbackToProxiedSource();
                return;
              }
              sourceDirectLoaded = true;
            } catch (_error) {
              // Cross-origin frame — content is there but inaccessible. Mark as
              // directly loaded so the health check doesn't force a proxy switch.
              sourceDirectLoaded = true;
            }
          }

          // Proxy bar ("Open original ↗") inside the srcdoc handles bad-rendering content.
          setEntrySourceFallbackVisible(false);
          if (sourceLoadTimeoutId) {
            window.clearTimeout(sourceLoadTimeoutId);
            sourceLoadTimeoutId = null;
          }
          // Ensure iframe height is adjusted after content loads.
          window.setTimeout(ensureSourceFrameFills, 20);
        });
      }

      if (entrySourceFrame && !entrySourceFrame.dataset.boundError) {
        entrySourceFrame.dataset.boundError = '1';
        entrySourceFrame.addEventListener('error', () => {
          if (sourceDirectLoaded && sourceViewMode === 'source') {
            return;
          }

          sourceFrameLoaded = false;
          if (sourceViewMode === 'source' && !sourceFallbackAttempted) {
            fallbackToProxiedSource();
          } else {
            setEntrySourceFallbackVisible(true);
          }
        });
      }

      if (entrySourceDismiss && !entrySourceDismiss.dataset.boundClick) {
        entrySourceDismiss.dataset.boundClick = '1';
        entrySourceDismiss.addEventListener('click', () => {
          setEntrySourceFallbackVisible(false);
        });
      }

      const entryBody = document.getElementById('entry-body');
      if (entryBody && entryBody.dataset.leadImagePending === '1' && !entryBody.dataset.leadImagePolling) {
        entryBody.dataset.leadImagePolling = '1';
        const pendingFeedUrl = entryBody.dataset.leadImageFeedUrl;
        const pendingEntryId = entryBody.dataset.leadImageEntryId;
        const pollIntervals = [2000, 3000, 4000, 5000, 6000, 8000, 10000, 12000];
        let pollIndex = 0;

        function schedulePoll() {
          if (pollIndex >= pollIntervals.length) return;
          const delay = pollIntervals[pollIndex++];
          setTimeout(async () => {
            if (document.getElementById('entry-body') !== entryBody) return;
            try {
              const params = new URLSearchParams({ feed_url: pendingFeedUrl, entry_id: pendingEntryId });
              const resp = await fetch('/entries/lead-image?' + params.toString(), { credentials: 'same-origin' });
              if (!resp.ok) return;
              const data = await resp.json();
              if (data.status === 'ready' && data.url) {
                const img = document.createElement('img');
                img.className = 'entry-lead-image entry-lead-image-popped';
                img.src = data.url;
                img.alt = '';
                img.loading = 'lazy';
                img.onerror = function() {
                  const _s = this.getAttribute('src');
                  if (!_s || _s.startsWith('/api/img?') || _s.startsWith('data:')) {
                    this.style.display = 'none';
                  } else {
                    this.setAttribute('src', '/api/img?u=' + encodeURIComponent(this.src));
                  }
                };
                entryBody.prepend(img);
                entryBody.removeAttribute('data-lead-image-pending');
                const muted = entryBody.querySelector('.muted');
                if (muted) muted.remove();
                return;
              }
              if (data.status === 'pending') schedulePoll();
            } catch (_e) {
              schedulePoll();
            }
          }, delay);
        }
        schedulePoll();
      }

    }

    bindEntryPaneInteractions();
    try { if (_ytAccountFeaturesEnabled) enhanceYoutubeEmbeds(document.querySelector('.pane-entry')); } catch (e) {}

    function bindPostListInteractions() {
      for (const postMainLink of document.querySelectorAll('.post-main-link')) {
        if (postMainLink.dataset.boundClick) {
          continue;
        }
        postMainLink.dataset.boundClick = '1';
        postMainLink.addEventListener('click', (event) => {
          if (event.defaultPrevented) {
            return;
          }
          event.preventDefault();
          loadEntryPaneWithoutFullRefresh(postMainLink.href);
        });
        postMainLink.addEventListener('auxclick', (event) => {
          if (event.button !== 1) return;
          event.preventDefault();
          const url = postMainLink.dataset.articleUrl;
          if (url) window.open(url, '_blank', 'noopener');
        });
      }

      for (const postItem of document.querySelectorAll('.post-item')) {
        if (!postItem.dataset.boundTileClick) {
          postItem.dataset.boundTileClick = '1';
          postItem.addEventListener('click', (event) => {
            if (event.target.closest('.post-save-toggle-form, .post-read-toggle-form')) {
              return;
            }
            if (event.target.closest('.post-feed')) {
              event.preventDefault();
              const feedUrl = postItem.getAttribute('data-post-feed-url');
              const feedLink = feedUrl
                ? document.querySelector(`.feed-link[data-feed-url="${CSS.escape(feedUrl)}"]`)
                : null;
              if (feedLink) {
                loadScopePanesWithoutFullRefresh(feedLink.href);
              } else if (feedUrl) {
                const params = new URLSearchParams(window.location.search);
                params.set('list_feed_url', feedUrl);
                params.delete('tag');
                params.delete('feed_url');
                params.delete('entry_id');
                loadScopePanesWithoutFullRefresh('/?' + params.toString());
              }
              return;
            }
            if (event.target.closest('.post-main-link')) {
              return;
            }
            const link = postItem.querySelector('.post-main-link');
            if (link) {
              loadEntryPaneWithoutFullRefresh(link.href);
            }
          });
        }

        if (!postItem.dataset.boundContextMenu) {
          postItem.dataset.boundContextMenu = '1';
          postItem.addEventListener('contextmenu', (event) => {
            if (!postContextMenu) {
              return;
            }
            event.preventDefault();
            event.stopPropagation();
            contextPostFeedUrl = postItem.getAttribute('data-post-feed-url');
            contextPostEntryId = postItem.getAttribute('data-post-entry-id');
            contextPostRead = postItem.getAttribute('data-post-read') === '1';
            contextPostLink = postItem.getAttribute('data-post-link') || '';
            contextPostTitle = postItem.getAttribute('data-post-title') || '';
            contextPostFolderId = postItem.getAttribute('data-post-folder-id') || null;
            if (postMarkReadButton) {
              postMarkReadButton.textContent = contextPostRead ? 'Mark as unread' : 'Mark as read';
            }
            setMenuItemVisible(postCopyUrlButton, Boolean(contextPostLink));
            setMenuItemVisible(postMarkFeedReadButton, Boolean(contextPostFeedUrl));
            setMenuItemVisible(postAutomationButton, Boolean(contextPostFeedUrl));
            setMenuItemVisible(postMoveToFeedButton, Boolean(contextPostFeedUrl && contextPostEntryId));
            setMenuItemVisible(postDeleteButton, Boolean(contextPostFeedUrl && contextPostEntryId));
            setMenuItemVisible(postEditDateButton, Boolean(contextPostFeedUrl && contextPostEntryId));
            setMenuItemVisible(postEditTitleButton, Boolean(contextPostFeedUrl && contextPostEntryId));
            setMenuItemVisible(postMoveVisibleButton, true);
            setMenuItemVisible(postMarkAboveReadButton, true);
            setMenuItemVisible(postMarkBelowReadButton, true);
            setMenuItemVisible(postClearImgCacheButton, true);
            showPostContextMenu(event);
          });
        }
        // attach swipe for post items (desktop and touch)
        if (!postItem.dataset.boundSwipe) {
          postItem.dataset.boundSwipe = '1';
          (function(pi){
            let sx=0, sy=0, dx=0, dy=0, touching=false; const threshold=40;
            pi.addEventListener('touchstart', (ev)=>{ const t=ev.changedTouches[0]; sx=t.clientX; sy=t.clientY; touching=true; dx=0; dy=0; }, { passive: true });
            pi.addEventListener('touchmove', (ev)=>{ if(!touching) return; const t=ev.changedTouches[0]; dx=t.clientX-sx; dy=t.clientY-sy; }, { passive: true });
            pi.addEventListener('touchend', (ev)=>{ if(!touching) return; touching=false; if(Math.abs(dx) < Math.abs(dy)) return; if(Math.abs(dx) < threshold) return; if(dx>0){ const btn=pi.querySelector('.post-read-toggle'); if(btn) btn.click(); } else { const btn=pi.querySelector('.post-save-toggle'); if(btn) btn.click(); } }, { passive: true });
          })(postItem);
        }
      }

      for (const toggleButton of document.querySelectorAll('.post-read-toggle')) {
        if (toggleButton.dataset.boundStopBubble) {
          continue;
        }
        toggleButton.dataset.boundStopBubble = '1';
        toggleButton.addEventListener('click', (event) => {
          event.stopPropagation();
        });
        toggleButton.addEventListener('mousedown', (event) => {
          event.stopPropagation();
        });
      }

      for (const toggleButton of document.querySelectorAll('.post-save-toggle')) {
        if (toggleButton.dataset.boundStopBubble) {
          continue;
        }
        toggleButton.dataset.boundStopBubble = '1';
        toggleButton.addEventListener('click', (event) => {
          event.stopPropagation();
        });
        toggleButton.addEventListener('mousedown', (event) => {
          event.stopPropagation();
        });
      }

      for (const readForm of document.querySelectorAll('.post-read-toggle-form')) {
        if (readForm.dataset.boundAsyncSubmit) {
          continue;
        }
        readForm.dataset.boundAsyncSubmit = '1';
        readForm.addEventListener('submit', async (event) => {
          event.preventDefault();
          event.stopPropagation();

          const postItem = readForm.closest('.post-item');
          const readInput = readForm.querySelector('input[name="read"]');
          if (!postItem || !(readInput instanceof HTMLInputElement)) {
            readForm.submit();
            return;
          }

          const wasUnread = postItem.getAttribute('data-post-read') === '0';
          const nextIsRead = readInput.value === '1';

          try {
            const formData = new FormData(readForm);
            const body = new URLSearchParams();
            for (const [key, value] of formData.entries()) {
              body.append(key, String(value));
            }

            const response = await fetch(readForm.action, {
              method: 'POST',
              credentials: 'same-origin',
              headers: {
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'X-Requested-With': 'lectio-post-read-toggle',
              },
              body: body.toString(),
            });

            if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
            }

            const changed = applyPostItemReadState(postItem, nextIsRead);
            if (changed) {
              const paneTitle = document.querySelector('.entry-pane-title');
              if (paneTitle
                  && paneTitle.getAttribute('data-post-feed-url') === postItem.getAttribute('data-post-feed-url')
                  && paneTitle.getAttribute('data-post-entry-id') === postItem.getAttribute('data-post-entry-id')) {
                applyEntryPaneReadState(nextIsRead);
              }
              const feedUrl = postItem.getAttribute('data-post-feed-url') || '';
              const postIsSaved = postItem.getAttribute('data-post-saved') === '1';
              if (nextIsRead && wasUnread) {
                const fallbackBase = getUnreadCountFallback();
                const current = Number.isFinite(appUnreadCount) ? appUnreadCount : fallbackBase;
                appUnreadCount = Math.max(0, current - 1);
                updateDynamicFavicon();
                if (feedUrl) adjustSidebarUnreadCount(feedUrl, -1, postIsSaved);
              } else if (!nextIsRead && !wasUnread) {
                const fallbackBase = getUnreadCountFallback();
                const current = Number.isFinite(appUnreadCount) ? appUnreadCount : fallbackBase;
                appUnreadCount = Math.max(0, current + 1);
                updateDynamicFavicon();
                if (feedUrl) adjustSidebarUnreadCount(feedUrl, +1, postIsSaved);
              }
            }
          } catch (_error) {
            readForm.submit();
          }
        });
      }

      for (const saveForm of document.querySelectorAll('.post-save-toggle-form')) {
        if (saveForm.dataset.boundAsyncSubmit) {
          continue;
        }
        saveForm.dataset.boundAsyncSubmit = '1';
        saveForm.addEventListener('submit', async (event) => {
          event.preventDefault();
          event.stopPropagation();

          const postItem = saveForm.closest('.post-item');
          const savedInput = saveForm.querySelector('input[name="saved"]');
          if (!postItem || !(savedInput instanceof HTMLInputElement)) {
            saveForm.submit();
            return;
          }

          const nextIsSaved = savedInput.value === '1';

          // Capture the body before the optimistic update — the update flips
          // savedInput.value, so FormData collected after would send the wrong value.
          const formData = new FormData(saveForm);
          const body = new URLSearchParams();
          for (const [key, value] of formData.entries()) {
            body.append(key, String(value));
          }

          applyPostItemSavedState(postItem, nextIsSaved);
          // (Un)starring an unread post moves it in/out of the Saved backlog.
          const starredUnread = postItem.getAttribute('data-post-read') === '0';
          if (starredUnread) adjustSavedUnreadBadge(nextIsSaved ? +1 : -1);
          const starFolderId = postItem.getAttribute('data-post-folder-id');
          adjustSavedFolderBadge(starFolderId, nextIsSaved ? +1 : -1);

          try {
            const response = await fetch(saveForm.action, {
              method: 'POST',
              credentials: 'same-origin',
              headers: {
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'X-Requested-With': 'lectio-post-save-toggle',
              },
              body: body.toString(),
            });

            if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
            }
          } catch (_error) {
            applyPostItemSavedState(postItem, !nextIsSaved);
            if (starredUnread) adjustSavedUnreadBadge(nextIsSaved ? -1 : +1);
            adjustSavedFolderBadge(starFolderId, nextIsSaved ? -1 : +1);
            saveForm.submit();
          }
        });
      }

      for (const entryReadForm of document.querySelectorAll('.entry-read-toggle-form')) {
        if (entryReadForm.dataset.boundAsyncSubmit) {
          continue;
        }
        entryReadForm.dataset.boundAsyncSubmit = '1';
        entryReadForm.addEventListener('submit', async (event) => {
          event.preventDefault();
          event.stopPropagation();

          const readInput = entryReadForm.querySelector('input[name="read"]');
          const feedInput = entryReadForm.querySelector('input[name="feed_url"]');
          const entryInput = entryReadForm.querySelector('input[name="entry_id"]');
          if (
            !(readInput instanceof HTMLInputElement)
            || !(feedInput instanceof HTMLInputElement)
            || !(entryInput instanceof HTMLInputElement)
          ) {
            entryReadForm.submit();
            return;
          }

          const nextIsRead = readInput.value === '1';
          const entryTitle = document.querySelector('.entry-pane-title');
          const wasRead = entryTitle?.getAttribute('data-post-read') === '1';

          try {
            const formData = new FormData(entryReadForm);
            const body = new URLSearchParams();
            for (const [key, value] of formData.entries()) {
              body.append(key, String(value));
            }

            const response = await fetch(entryReadForm.action, {
              method: 'POST',
              credentials: 'same-origin',
              headers: {
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'X-Requested-With': 'lectio-post-read-toggle',
              },
              body: body.toString(),
            });

            if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
            }

            applyEntryPaneReadState(nextIsRead);

            const matchingPostItem = document.querySelector(
              `.post-item[data-post-feed-url="${CSS.escape(feedInput.value)}"][data-post-entry-id="${CSS.escape(entryInput.value)}"]`
            );
            if (matchingPostItem) {
              applyPostItemReadState(matchingPostItem, nextIsRead);
            }

            const paneIsSaved = matchingPostItem
              ? matchingPostItem.getAttribute('data-post-saved') === '1'
              : document.querySelector('.entry-save-indicator')?.textContent.trim() === 'star';
            if (nextIsRead && !wasRead) {
              const fallbackBase = getUnreadCountFallback();
              const current = Number.isFinite(appUnreadCount) ? appUnreadCount : fallbackBase;
              appUnreadCount = Math.max(0, current - 1);
              updateDynamicFavicon();
              adjustSidebarUnreadCount(feedInput.value, -1, paneIsSaved);
            } else if (!nextIsRead && wasRead) {
              const fallbackBase = getUnreadCountFallback();
              const current = Number.isFinite(appUnreadCount) ? appUnreadCount : fallbackBase;
              appUnreadCount = Math.max(0, current + 1);
              updateDynamicFavicon();
              adjustSidebarUnreadCount(feedInput.value, +1, paneIsSaved);
            }
          } catch (_error) {
            entryReadForm.submit();
          }
        });
      }
    }

    bindPostListInteractions();

    document.addEventListener('submit', (event) => {
      const form = event.target instanceof HTMLFormElement && event.target.closest('.mark-read-action-form');
      if (!form) return;
      event.preventDefault();
      event.stopPropagation();
      const feedUrlInput = form.querySelector('input[name="feed_url"]');
      const feedUrlFilter = feedUrlInput instanceof HTMLInputElement ? feedUrlInput.value : null;
      const maxAgeDaysInput = form.querySelector('input[name="max_age_days"]');
      const maxAgeDays = maxAgeDaysInput instanceof HTMLInputElement ? Number(maxAgeDaysInput.value) : null;
      const markReadMenuDetails = form.closest('details.mark-read-menu');
      if (markReadMenuDetails) markReadMenuDetails.removeAttribute('open');
      submitMarkReadAsync(form, feedUrlFilter || null, maxAgeDays);
    }, true);

    postMarkReadButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (
        !contextPostFeedUrl ||
        !contextPostEntryId ||
        !postReadFeedUrlInput ||
        !postReadEntryIdInput ||
        !postReadValueInput ||
        !postReadForm
      ) {
        return;
      }

      postReadFeedUrlInput.value = contextPostFeedUrl;
      postReadEntryIdInput.value = contextPostEntryId;
      postReadValueInput.value = contextPostRead ? '0' : '1';
      hideAllContextMenus();
      postReadForm.submit();
    });

    postMarkFeedReadButton?.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (
        !contextPostFeedUrl ||
        !markReadFeedFolderIdInput ||
        !markReadFeedUrlInput ||
        !markReadListFeedUrlInput ||
        !markReadFeedForm
      ) {
        return;
      }

      const feedUrl = contextPostFeedUrl;
      const folderId = document.querySelector(`.feed-link[data-feed-url="${CSS.escape(feedUrl)}"]`)?.getAttribute('data-folder-id')
        || markReadFeedFolderIdInput.value
        || window.SELECTED_FOLDER_ID || '';
      markReadFeedFolderIdInput.value = folderId;
      markReadFeedUrlInput.value = feedUrl;
      markReadListFeedUrlInput.value = feedUrl;
      hideAllContextMenus();
      submitMarkReadAsync(markReadFeedForm, feedUrl);
    });

    function adjustCountBadge(containerEl, delta) {
      if (containerEl.classList.contains('scope-tab')) return;  // tabs carry no counter
      let badge = containerEl.querySelector(':scope > .count');
      const current = badge ? Number(badge.textContent) : 0;
      const next = Math.max(0, current + delta);
      if (next === 0) {
        if (badge) badge.remove();
      } else {
        if (!badge) {
          badge = document.createElement('span');
          badge.className = 'count';
          containerEl.appendChild(badge);
        }
        badge.textContent = String(next);
      }
    }

    // Adjust the Saved Articles row's unread badge (unread starred entries).
    function adjustSavedUnreadBadge(delta) {
      const savedItem = document.querySelector('.saved-item.tree-item');
      if (savedItem) adjustCountBadge(savedItem, delta);
    }

    // Saved-sublist folder badges are TOTAL saved per folder — they move on
    // star/unstar (any read state), never on read toggles. A folder emptied
    // to zero hides its row (the sublist only shows folders holding saves);
    // a folder's FIRST save has no row to update until the next full render.
    function adjustSavedFolderBadge(folderId, delta) {
      if (folderId === null || folderId === undefined || folderId === '') return;
      const el = document.querySelector(`.saved-folder-item[data-folder-id="${CSS.escape(String(folderId))}"]`);
      if (!el) return;
      adjustCountBadge(el, delta);
      const group = el.closest('.tree-folder-group');
      if (group) group.hidden = !el.querySelector(':scope > .count');
    }

    function adjustSidebarUnreadCount(feedUrl, delta, isSaved = false) {
      if (isSaved) adjustSavedUnreadBadge(delta);
      const feedLink = document.querySelector(`.feed-link[data-feed-url="${CSS.escape(feedUrl)}"]`);
      if (feedLink) adjustCountBadge(feedLink, delta);
      // The feed's tree row only exists once its folder has been expanded
      // (rows lazy-load), so for the folder badge fall back to the post-list /
      // entry-pane feed links, which carry the feed's own folder id.
      const folderId = feedLink?.getAttribute('data-folder-id')
        || document.querySelector(
          `.post-feed-link[data-feed-url="${CSS.escape(feedUrl)}"], .entry-feed-link[data-feed-url="${CSS.escape(feedUrl)}"]`
        )?.getAttribute('data-folder-id');
      if (folderId) {
        const folderItem = document.querySelector(`.tree-item[data-folder-id="${CSS.escape(folderId)}"]:not(.root-item):not(.saved-folder-item)`);
        if (folderItem) {
          adjustCountBadge(folderItem, delta);
          const folderGroup = folderItem.closest('.tree-folder-group');
          if (folderGroup) {
            const cur = Number(folderGroup.getAttribute('data-unread-count') || '0');
            folderGroup.setAttribute('data-unread-count', String(Math.max(0, cur + delta)));
          }
        }
      }
      // The aggregate unread badge lives on the feeds "All" row (the Feeds
      // scope tab itself carries no counter).
      const feedsAll = document.querySelector('.feeds-all-item');
      if (feedsAll) adjustCountBadge(feedsAll, delta);
    }

    // Zero out sidebar count badges for a folder (or all folders when isRoot=true).
    // Called after submitMarkReadAsync so off-screen unread posts don't leave stale counts.
    function clearFolderBadges(folderId, isRoot) {
      const feedLinks = isRoot
        ? document.querySelectorAll('.feed-link')
        : document.querySelectorAll(`.feed-link[data-folder-id="${CSS.escape(folderId)}"]`);
      let remaining = 0;
      for (const feedLink of feedLinks) {
        const badge = feedLink.querySelector(':scope > .count');
        if (badge) {
          remaining += Number(badge.textContent) || 0;
          badge.remove();
        }
        feedLink.closest('.tree-feed-item')?.setAttribute('data-unread-count', '0');
      }
      if (isRoot) {
        // Saved-sublist badges are TOTAL saved (not unread) — mark-read must not clear them.
        document.querySelectorAll('.tree-item:not(.root-item):not(.saved-folder-item) > .count').forEach(b => b.remove());
        document.querySelectorAll('.tree-folder-group').forEach(g => g.setAttribute('data-unread-count', '0'));
        const rootItem = document.querySelector('.root-item.tree-item');
        rootItem?.querySelector(':scope > .count')?.remove();
      } else {
        const folderItem = document.querySelector(`.tree-item[data-folder-id="${CSS.escape(folderId)}"]:not(.root-item):not(.saved-folder-item)`);
        if (folderItem) {
          // Prefer the folder's own badge for the "All" decrement: an
          // unexpanded folder has no feed rows in the DOM, so `remaining`
          // (summed from them) undercounts.
          const folderBadge = folderItem.querySelector(':scope > .count');
          const folderCount = folderBadge ? (Number(folderBadge.textContent) || 0) : 0;
          remaining = Math.max(remaining, folderCount);
          folderBadge?.remove();
          folderItem.closest('.tree-folder-group')?.setAttribute('data-unread-count', '0');
        }
        if (remaining > 0) {
          const feedsAll = document.querySelector('.feeds-all-item');
          if (feedsAll) adjustCountBadge(feedsAll, -remaining);
        }
      }
    }

    function applyPostItemReadState(postItem, isRead) {
      if (!postItem) {
        return false;
      }
      const wasRead = postItem.getAttribute('data-post-read') === '1';
      if (wasRead === isRead) {
        return false;
      }

      postItem.setAttribute('data-post-read', isRead ? '1' : '0');
      postItem.classList.toggle('is-read', isRead);

      const readInput = postItem.querySelector('.post-read-toggle-form input[name="read"]');
      if (readInput) {
        readInput.value = isRead ? '0' : '1';
      }

      const readButton = postItem.querySelector('.post-read-toggle');
      if (readButton) {
        readButton.title = isRead ? 'Mark as Unread' : 'Mark as Read';
        readButton.setAttribute('aria-label', isRead ? 'Mark as Unread' : 'Mark as Read');
      }

      return true;
    }

    function applyBulkReadState(feedUrlFilter, maxAgeDays) {
      const cutoff = (maxAgeDays != null && Number.isFinite(maxAgeDays))
        ? new Date(Date.now() - maxAgeDays * 86400 * 1000)
        : null;
      const postItems = document.querySelectorAll('.posts .post-item');
      const deltaByFeed = {};
      for (const item of postItems) {
        const itemFeedUrl = item.getAttribute('data-post-feed-url') || '';
        if (feedUrlFilter instanceof Set ? !feedUrlFilter.has(itemFeedUrl) : (feedUrlFilter && itemFeedUrl !== feedUrlFilter)) continue;
        if (item.getAttribute('data-post-read') === '1') continue;
        if (cutoff !== null) {
          // Mirror the server logic: skip entries with no date, skip entries newer than cutoff.
          // Timestamps live on the child <time> element, not on post-item itself.
          const timeEl = item.querySelector('time[data-post-iso]');
          const isoPost = (timeEl?.getAttribute('data-post-iso') || '').trim();
          const isoReceived = (timeEl?.getAttribute('data-received-iso') || '').trim();
          const dateStr = isoPost || isoReceived;
          if (!dateStr) continue;
          const entryDate = new Date(dateStr);
          if (isNaN(entryDate.getTime()) || entryDate >= cutoff) continue;
        }
        const changed = applyPostItemReadState(item, true);
        if (changed) {
          deltaByFeed[itemFeedUrl] = (deltaByFeed[itemFeedUrl] || 0) + 1;
        }
      }
      let totalChanged = 0;
      for (const [fu, delta] of Object.entries(deltaByFeed)) {
        adjustSidebarUnreadCount(fu, -delta);
        totalChanged += delta;
      }
      if (totalChanged > 0) {
        const fallbackBase = getUnreadCountFallback();
        const current = Number.isFinite(appUnreadCount) ? appUnreadCount : fallbackBase;
        appUnreadCount = Math.max(0, current - totalChanged);
        updateDynamicFavicon();
      }
    }

    async function submitMarkReadAsync(form, feedUrlFilter, maxAgeDays) {
      // Optimistic: dim only the posts that will actually be marked on the server.
      applyBulkReadState(feedUrlFilter || null, maxAgeDays ?? null);

      const formData = new FormData(form);
      const body = new URLSearchParams();
      for (const [key, value] of formData.entries()) {
        body.append(key, String(value));
      }
      const csrfMeta = document.querySelector('meta[name="csrf-token"]');
      const csrfToken = csrfMeta ? csrfMeta.content : '';
      const headers = {
        'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
        'X-Requested-With': 'lectio-mark-read',
      };
      if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
      try {
        await fetch(form.action, {
          method: 'POST',
          credentials: 'same-origin',
          headers,
          body: body.toString(),
        });
      } catch (_err) {
        // Network error — UI already updated optimistically, silently ignore.
      }
      // After any bulk mark-read, refresh sidebar counts from the server so that
      // off-screen entries (not in the current post list) are reflected correctly.
      _refreshSidebarCounts();
    }

    // Set a badge to an absolute value (adjustCountBadge handles creation/removal).
    function setCountBadge(containerEl, value) {
      const badge = containerEl.querySelector(':scope > .count');
      const current = badge ? (Number(badge.textContent) || 0) : 0;
      if (value !== current) adjustCountBadge(containerEl, value - current);
    }

    async function _refreshSidebarCounts() {
      try {
        const resp = await fetch('/api/unread-counts', { credentials: 'same-origin' });
        if (!resp.ok) return;
        const data = await resp.json();
        const feedCounts = data.feeds || {};
        // Feed rows present in the DOM (unexpanded folders have none — their
        // rows arrive with fresh counts when the folder loads on expand).
        for (const feedLink of document.querySelectorAll('.feed-link[data-feed-url]')) {
          const feedUrl = feedLink.getAttribute('data-feed-url');
          if (!feedUrl) continue;
          const serverCount = feedCounts[feedUrl] ?? 0;
          setCountBadge(feedLink, serverCount);
          feedLink.closest('.tree-feed-item')?.setAttribute('data-unread-count', String(serverCount));
        }
        // Folder badges come straight from the server — they can never be
        // derived from feed rows, which don't exist for unexpanded folders.
        const folderCounts = data.folders || {};
        for (const folderLink of document.querySelectorAll('.feeds-tree-children .tree-item.child-item[data-folder-id]:not(.feeds-all-item)')) {
          const fid = folderLink.getAttribute('data-folder-id');
          if (!fid || !(fid in folderCounts)) continue;
          const serverCount = folderCounts[fid] ?? 0;
          setCountBadge(folderLink, serverCount);
          folderLink.closest('.tree-folder-group')?.setAttribute('data-unread-count', String(serverCount));
        }
        const feedsAll = document.querySelector('.feeds-all-item');
        if (feedsAll && typeof data.total === 'number') {
          setCountBadge(feedsAll, data.total);
          feedsAll.closest('.tree-folder-group')?.setAttribute('data-unread-count', String(data.total));
        }
        // Re-evaluate the "Unread only" folder filter against the fresh counts:
        // a folder emptied by mark-all-read then refilled by a background feed
        // refresh must reappear (and a newly-emptied one hide) without a reload.
        if (typeof applyUnreadFoldersOnly === 'function'
            && window.localStorage.getItem(UNREAD_FOLDERS_ONLY_KEY) === '1') {
          applyUnreadFoldersOnly(true);
        }
      } catch (_e) { }
    }

    // Poll sidebar counts on a slow cadence so items arriving via the
    // background scheduled refresh surface in the tree (and un-hide folders
    // the "Unread only" filter had collapsed) without a manual reload. Gated
    // on tab visibility so a backgrounded tab doesn't poll; also catches up
    // immediately when the tab is refocused.
    (function () {
      const SIDEBAR_POLL_MS = 90000;
      let _pollTimer = null;
      function _start() {
        if (_pollTimer !== null) return;
        _pollTimer = window.setInterval(() => {
          if (document.visibilityState === 'visible') _refreshSidebarCounts();
        }, SIDEBAR_POLL_MS);
      }
      function _stop() {
        if (_pollTimer !== null) { window.clearInterval(_pollTimer); _pollTimer = null; }
      }
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') { _refreshSidebarCounts(); _start(); }
        else { _stop(); }
      });
      if (document.visibilityState === 'visible') _start();
    }());

    function applyPostItemSavedState(postItem, isSaved) {
      if (!postItem) {
        return false;
      }

      const wasSaved = postItem.getAttribute('data-post-saved') === '1';
      if (wasSaved === isSaved) {
        return false;
      }

      postItem.setAttribute('data-post-saved', isSaved ? '1' : '0');

      const savedInput = postItem.querySelector('.post-save-toggle-form input[name="saved"]');
      if (savedInput) {
        savedInput.value = isSaved ? '0' : '1';
      }

      const saveButton = postItem.querySelector('.post-save-toggle');
      const saveIndicator = postItem.querySelector('.post-save-indicator');
      if (saveButton) {
        const label = isSaved ? 'Remove from saved' : 'Save for later';
        saveButton.title = label;
        saveButton.setAttribute('aria-label', label);
      }
      if (saveIndicator) {
        saveIndicator.textContent = isSaved ? 'star' : 'star_outline';
      }

      return true;
    }

    function applyEntryPaneSavedState(isSaved) {
      const entrySaveForm = document.querySelector('.entry-save-toggle-form');
      const entrySaveInput = entrySaveForm?.querySelector('input[name="saved"]');
      const entrySaveButton = entrySaveForm?.querySelector('.entry-save-toggle');
      const entrySaveIndicator = entrySaveForm?.querySelector('.entry-save-indicator');

      if (entrySaveInput instanceof HTMLInputElement) {
        entrySaveInput.value = isSaved ? '0' : '1';
      }
      if (entrySaveButton instanceof HTMLElement) {
        const label = isSaved ? 'Remove from saved' : 'Save for later';
        entrySaveButton.title = label;
        entrySaveButton.setAttribute('aria-label', label);
      }
      if (entrySaveIndicator instanceof HTMLElement) {
        entrySaveIndicator.textContent = isSaved ? 'star' : 'star_outline';
      }
    }

    function applyEntryPaneReadState(isRead) {
      const entryReadForm = document.querySelector('.entry-read-toggle-form');
      const entryReadInput = entryReadForm?.querySelector('input[name="read"]');
      const entryReadButton = entryReadForm?.querySelector('.entry-read-toggle');
      const entryTitle = document.querySelector('.entry-pane-title');

      if (entryReadInput instanceof HTMLInputElement) {
        entryReadInput.value = isRead ? '0' : '1';
      }
      if (entryReadButton instanceof HTMLElement) {
        const label = isRead ? 'Mark as Unread' : 'Mark as Read';
        entryReadButton.title = label;
        entryReadButton.setAttribute('aria-label', label);
        entryReadButton.setAttribute('data-read-state', isRead ? '1' : '0');
      }
      if (entryTitle instanceof HTMLElement) {
        entryTitle.setAttribute('data-post-read', isRead ? '1' : '0');
      }
    }

    function applyRangeReadStateInList(direction) {
      const postsInList = Array.from(document.querySelectorAll('.posts .post-item'));
      const anchorIndex = postsInList.findIndex(
        (item) => item.getAttribute('data-post-feed-url') === contextPostFeedUrl
          && item.getAttribute('data-post-entry-id') === contextPostEntryId
      );
      if (anchorIndex < 0) {
        return;
      }

      const targets = direction === 'above'
        ? postsInList.slice(0, anchorIndex)
        : postsInList.slice(anchorIndex + 1);

      let changedUnreadToRead = 0;
      for (const postItem of targets) {
        const wasUnread = postItem.getAttribute('data-post-read') === '0';
        const changed = applyPostItemReadState(postItem, true);
        if (changed && wasUnread) {
          changedUnreadToRead += 1;
        }
      }

      if (changedUnreadToRead > 0) {
        const fallbackBase = getUnreadCountFallback();
        const current = Number.isFinite(appUnreadCount) ? appUnreadCount : fallbackBase;
        appUnreadCount = Math.max(0, current - changedUnreadToRead);
        updateDynamicFavicon();
        if (contextPostFeedUrl) adjustSidebarUnreadCount(contextPostFeedUrl, -changedUnreadToRead);
      }
    }

    async function markRangeReadWithoutRefresh(direction) {
      if (
        !contextPostFeedUrl
        || !contextPostEntryId
        || !postRangeFeedUrlInput
        || !postRangeEntryIdInput
        || !postRangeDirectionInput
        || !postRangeReadForm
      ) {
        return;
      }

      postRangeFeedUrlInput.value = contextPostFeedUrl;
      postRangeEntryIdInput.value = contextPostEntryId;
      postRangeDirectionInput.value = direction;
      hideAllContextMenus();

      try {
        const formData = new FormData(postRangeReadForm);
        const body = new URLSearchParams();
        for (const [key, value] of formData.entries()) {
          body.append(key, String(value));
        }

        const response = await fetch(postRangeReadForm.action, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
            'X-Requested-With': 'lectio-post-range-read',
          },
          body: body.toString(),
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        let result = null;
        try {
          result = await response.json();
        } catch (_parseError) {
          result = null;
        }

        applyRangeReadStateInList(direction);
        if (result && typeof result.message === 'string' && result.message.trim()) {
          showToastMessage(result.message);
        }
      } catch (_error) {
        showToastMessage('Could not mark posts in that range.');
      }
    }

    postMarkAboveReadButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      markRangeReadWithoutRefresh('above');
    });

    postMarkBelowReadButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      markRangeReadWithoutRefresh('below');
    });

    postCopyUrlButton?.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!contextPostLink) {
        hideAllContextMenus();
        return;
      }

      const copied = await copyTextToClipboard(contextPostLink);
      hideAllContextMenus();
      if (!copied) {
        window.alert('Could not copy URL to clipboard.');
      }
    });

    postAutomationButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const feedUrl = contextPostFeedUrl;
      const folderId = contextPostFolderId;
      const title = contextPostTitle;
      hideAllContextMenus();
      if (!feedUrl) return;
      window.openHighlightsModal?.({
        scope: 'feed',
        scope_id: feedUrl,
        folder_id: folderId,
        keyword: title || '',
        search_in: 'title',
      });
    });

    // Shared driver for the move-to-feed modal. `entries` is a list of
    // {feedUrl, entryId}; one entry uses the single endpoint, more uses the
    // batch endpoint. `bodyText` describes what's being moved.
    async function openMoveToFeedModal(entries, bodyText, onDone) {
      if (!entries.length) return;
      const modal = document.getElementById('move-entry-modal');
      const bodyEl = document.getElementById('move-entry-body');
      const targetSel = document.getElementById('move-entry-target');
      const targetList = document.getElementById('move-entry-candidates');
      const confirmBtn = document.getElementById('move-entry-confirm');
      if (!modal || !targetSel || !confirmBtn) return;

      bodyEl.textContent = bodyText;
      targetSel.value = '';
      targetList.innerHTML = '';
      confirmBtn.disabled = true;
      confirmBtn.textContent = 'Move';

      // Candidates: every other subscribed feed (same endpoint the unsubscribe
      // migration picker uses, so it doesn't depend on what's in the DOM).
      let candidates = [];
      try {
        const r = await fetch(`/feeds/curation-count?feed_url=${encodeURIComponent(entries[0].feedUrl)}`);
        const d = await r.json();
        candidates = d.candidates || [];
      } catch (_) { /* picker just stays empty */ }
      const labelToUrl = new Map();
      const labelSeen = new Map();
      candidates.forEach(c => {
        let label = c.title || c.url;
        if (labelSeen.has(label)) {
          let host = c.url; try { host = new URL(c.url).host; } catch (_) {}
          label = `${label} (${host})`;
        }
        let uniq = label, n = 2;
        while (labelToUrl.has(uniq)) { uniq = `${label} #${n++}`; }
        labelSeen.set(c.title || c.url, true);
        labelToUrl.set(uniq, c.url);
        const o = document.createElement('option'); o.value = uniq; targetList.appendChild(o);
      });
      targetSel.oninput = () => { confirmBtn.disabled = !labelToUrl.has(targetSel.value); };

      confirmBtn.onclick = async () => {
        const targetUrl = labelToUrl.get(targetSel.value);
        if (!targetUrl) return;
        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Moving…';
        const body = entries.length === 1
          ? new URLSearchParams({ feed_url: entries[0].feedUrl, entry_id: entries[0].entryId, target_url: targetUrl })
          : new URLSearchParams({
              entries: JSON.stringify(entries.map(e => [e.feedUrl, e.entryId])),
              target_url: targetUrl,
            });
        const endpoint = entries.length === 1 ? '/entries/move-to-feed' : '/entries/move-to-feed-batch';
        try {
          const resp = await fetch(endpoint, { method: 'POST', body });
          const data = await resp.json();
          if (data.ok) {
            modal.setAttribute('hidden', '');
            showToastMessage(data.message || 'Moved.');
            // Reload the list panes so star/tag badges and read state reflect the move.
            loadScopePanesWithoutFullRefresh(window.location.href, false);
            onDone?.(data);
          } else {
            showToastMessage(data.error || 'Move failed.');
            confirmBtn.disabled = false;
            confirmBtn.textContent = 'Move';
          }
        } catch (_) {
          showToastMessage('Move failed — network error.');
          confirmBtn.disabled = false;
          confirmBtn.textContent = 'Move';
        }
      };

      modal.removeAttribute('hidden');
      targetSel.focus();
    }

    postMoveToFeedButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const feedUrl = contextPostFeedUrl;
      const entryId = contextPostEntryId;
      const title = contextPostTitle;
      hideAllContextMenus();
      if (!feedUrl || !entryId) return;
      openMoveToFeedModal(
        [{ feedUrl, entryId }],
        title ? `Move “${title}” to another feed.` : 'Move this entry to another feed.'
      );
    });

    postEditDateButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const feedUrl = contextPostFeedUrl;
      const entryId = contextPostEntryId;
      hideAllContextMenus();
      if (!feedUrl || !entryId) return;
      openActionInputModal({
        title: 'Edit published date',
        label: 'Published',
        inputType: 'date',
        submitLabel: 'Save',
        onSubmit: async (value) => {
          try {
            const body = new URLSearchParams({ feed_url: feedUrl, entry_id: entryId, published: value });
            const resp = await fetch('/entries/set-date', { method: 'POST', body });
            const data = await resp.json();
            if (!data.ok) {
              window.alert(data.error || 'Could not save the date.');
              return;
            }
            // Sorting and the rendered timestamp both derive from the stored
            // date server-side; a reload is the simple way to reflect both.
            window.location.reload();
          } catch (_) {
            window.alert('Could not save the date.');
          }
        },
      });
    });

    postEditTitleButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const feedUrl = contextPostFeedUrl;
      const entryId = contextPostEntryId;
      const currentTitle = contextPostTitle;
      hideAllContextMenus();
      if (!feedUrl || !entryId) return;
      openActionInputModal({
        title: 'Edit title',
        label: 'Title',
        initialValue: currentTitle,
        submitLabel: 'Save',
        onSubmit: async (value) => {
          try {
            const body = new URLSearchParams({ feed_url: feedUrl, entry_id: entryId, title: value });
            const resp = await fetch('/entries/set-title', { method: 'POST', body });
            const data = await resp.json();
            if (!data.ok) {
              window.alert(data.error || 'Could not save the title.');
              return;
            }
            // The list row and pane header both render the stored title
            // server-side; a reload is the simple way to reflect both.
            window.location.reload();
          } catch (_) {
            window.alert('Could not save the title.');
          }
        },
      });
    });

    postDeleteButton?.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const feedUrl = contextPostFeedUrl;
      const entryId = contextPostEntryId;
      const title = contextPostTitle;
      hideAllContextMenus();
      if (!feedUrl || !entryId) return;
      const label = title ? `“${title}”` : 'this post';
      if (!window.confirm(`Permanently delete ${label} from this feed? A tombstone stops the next refresh from re-adding it.`)) return;
      try {
        const body = new URLSearchParams({ feed_url: feedUrl, entry_id: entryId });
        const resp = await fetch('/entries/delete', { method: 'POST', body });
        const data = await resp.json();
        if (!data.ok) {
          window.alert(data.error || 'Delete failed.');
          return;
        }
        const row = document.querySelector(
          `.posts .post-item[data-post-entry-id="${window.CSS && CSS.escape ? CSS.escape(entryId) : entryId}"]`
        );
        row?.remove();
      } catch (_) {
        window.alert('Delete failed.');
      }
    });

    postMoveVisibleButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      hideAllContextMenus();
      // "Visible" = every post currently rendered in the list — i.e. whatever
      // survives the active filters (tag, search, unread, star).
      const entries = Array.from(document.querySelectorAll('.posts .post-item'))
        .map(el => ({
          feedUrl: el.getAttribute('data-post-feed-url') || '',
          entryId: el.getAttribute('data-post-entry-id') || '',
        }))
        .filter(e => e.feedUrl && e.entryId);
      if (!entries.length) return;
      openMoveToFeedModal(
        entries,
        `Move the ${entries.length} visible post${entries.length === 1 ? '' : 's'} to another feed. ` +
        'Posts already in the target feed are skipped.'
      );
    });

    postClearImgCacheButton?.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      hideAllContextMenus();
      if (!contextPostFeedUrl || !contextPostEntryId) return;
      const body = new URLSearchParams({ feed_url: contextPostFeedUrl, entry_id: contextPostEntryId });
      try {
        const resp = await fetch('/debug/clear-entry-lead-image-cache', { method: 'POST', body });
        const data = await resp.json();
        if (data.ok) {
          // Reset the thumbnail on the post-list item to fallback icon.
          const postItem = document.querySelector(`.post-item[data-entry-id="${CSS.escape(contextPostEntryId)}"]`);
          if (postItem) {
            const thumb = postItem.querySelector('.post-thumbnail img');
            if (thumb) {
              thumb.src = '';
              thumb.closest('.post-thumbnail')?.classList.add('is-empty');
            }
          }
          showToast('Image cache cleared');
        }
      } catch (e) {
        // ignore
      }
    });

    markReadButton?.addEventListener('click', () => {
      if (contextTargetType === 'feed') {
        if (
          !contextFolderId ||
          !contextFeedUrl ||
          !markReadFeedFolderIdInput ||
          !markReadFeedUrlInput ||
          !markReadListFeedUrlInput ||
          !markReadFeedForm
        ) {
          return;
        }

        markReadFeedFolderIdInput.value = contextFolderId;
        markReadFeedUrlInput.value = contextFeedUrl;
        markReadListFeedUrlInput.value = contextFeedUrl;
        hideContextMenu();
        submitMarkReadAsync(markReadFeedForm, contextFeedUrl);
        // Zero any remaining feed badge (covers off-screen unread posts)
        const _feedLink = contextFeedUrl
          ? document.querySelector(`.feed-link[data-feed-url="${CSS.escape(contextFeedUrl)}"]`)
          : null;
        if (_feedLink) {
          const _feedBadge = _feedLink.querySelector(':scope > .count');
          const _feedRemaining = _feedBadge ? (Number(_feedBadge.textContent) || 0) : 0;
          if (_feedBadge) _feedBadge.remove();
          _feedLink.closest('.tree-feed-item')?.setAttribute('data-unread-count', '0');
          if (_feedRemaining > 0) {
            const _feedFolderId = _feedLink.getAttribute('data-folder-id');
            if (_feedFolderId) {
              const _folderEl = document.querySelector(`.tree-item[data-folder-id="${CSS.escape(_feedFolderId)}"]:not(.root-item):not(.saved-folder-item)`);
              if (_folderEl) {
                adjustCountBadge(_folderEl, -_feedRemaining);
                const _fg = _folderEl.closest('.tree-folder-group');
                if (_fg) _fg.setAttribute('data-unread-count', String(Math.max(0, Number(_fg.getAttribute('data-unread-count') || '0') - _feedRemaining)));
              }
            }
            const _rootEl = document.querySelector('.feeds-all-item');
            if (_rootEl) adjustCountBadge(_rootEl, -_feedRemaining);
          }
        }
        return;
      }

      if (!contextFolderId || !markReadFolderIdInput || !markReadFolderForm) {
        return;
      }

      markReadFolderIdInput.value = contextFolderId;
      hideContextMenu();
      const isRootFolder = !!document.querySelector(`.root-item[data-folder-id="${CSS.escape(contextFolderId)}"]`);
      const folderFeedFilter = isRootFolder ? null : new Set(
        Array.from(document.querySelectorAll(`.feed-link[data-folder-id="${CSS.escape(contextFolderId)}"]`))
          .map(el => el.getAttribute('data-feed-url')).filter(Boolean)
      );
      submitMarkReadAsync(markReadFolderForm, folderFeedFilter);
      // Zero any remaining sidebar counts (covers off-screen unread posts)
      clearFolderBadges(contextFolderId, isRootFolder);
    });

    let _folderSubmenuCloseTimer = null;

    function scheduleFolderSubmenuClose() {
      _folderSubmenuCloseTimer = setTimeout(() => {
        _folderSubmenuCloseTimer = null;
        hideFolderSubmenu();
      }, 120);
    }

    function cancelFolderSubmenuClose() {
      if (_folderSubmenuCloseTimer !== null) {
        clearTimeout(_folderSubmenuCloseTimer);
        _folderSubmenuCloseTimer = null;
      }
    }

    const hasCoarsePointer = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;

    if (!hasCoarsePointer) {
      addToFolderButton?.addEventListener('mouseenter', () => {
        cancelFolderSubmenuClose();
        showFolderSubmenu();
      });

      folderSubmenu?.addEventListener('mouseenter', () => {
        cancelFolderSubmenuClose();
      });
    }

    if (!hasCoarsePointer) {
      addToFolderWrap?.addEventListener('mouseleave', () => {
        scheduleFolderSubmenuClose();
      });

      folderSubmenu?.addEventListener('mouseleave', () => {
        scheduleFolderSubmenuClose();
      });
    }

    addToFolderButton?.addEventListener('click', (event) => {
      event.stopPropagation();
      event.preventDefault();
      if (!folderSubmenu) {
        return;
      }
      if (hasCoarsePointer) {
        cancelFolderSubmenuClose();
        showFolderSubmenu();
        return;
      }
      if (folderSubmenu.hasAttribute('hidden')) {
        showFolderSubmenu();
      } else {
        hideFolderSubmenu();
      }
    });

    window.addEventListener('resize', () => {
      if (folderSubmenu && !folderSubmenu.hasAttribute('hidden')) {
        positionFolderSubmenuInViewport();
      }
    });

    for (const folderOption of document.querySelectorAll('.context-submenu-item')) {
      folderOption.addEventListener('click', () => {
        const targetFolderId = folderOption.getAttribute('data-target-folder-id');
        if (
          contextTargetType !== 'feed' ||
          !contextFolderId ||
          !contextFeedUrl ||
          !targetFolderId ||
          !moveFeedUrlInput ||
          !moveFeedFromFolderIdInput ||
          !moveFeedToFolderIdInput ||
          !moveFeedForm
        ) {
          return;
        }

        const toFolderName = folderOption.textContent.trim();
        const feedUrl = contextFeedUrl;
        const fromFolderId = contextFolderId;
        hideContextMenu();
        moveFeedRequest(feedUrl, fromFolderId, targetFolderId, toFolderName)
          .catch((err) => window.alert(`Move failed: ${err}`));
      });
    }

    unsubscribeFeedButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (contextTargetType !== 'feed' || !contextFolderId || !contextFeedUrl) {
        return;
      }

      const title = contextFolderName.trim() || contextFeedUrl;
      hideAllContextMenus();

      const feedUrl = contextFeedUrl;
      const folderId = contextFolderId;
      const feedLi = document.querySelector(
        `.tree-feed-item > .feed-link[data-feed-url="${CSS.escape(feedUrl)}"][data-folder-id="${CSS.escape(String(folderId))}"]`
      )?.closest('.tree-feed-item');

      unsubscribeFeedInteractive(feedUrl, folderId, { title }).then((done) => {
        if (done && feedLi) feedLi.style.opacity = '0.4';
      });
    });

    feedPropertiesButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (contextTargetType !== 'feed' || !contextFeedUrl) {
        return;
      }

      hideAllContextMenus();
      openFeedPropertiesModal(contextFeedUrl);
    });

    folderPropertiesButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (contextTargetType !== 'folder' || !contextFolderId) {
        return;
      }

      hideAllContextMenus();
      openFolderPropertiesModal(Number(contextFolderId));
    });

    // ── Highlights modal ────────────────────────────────────────────────────
    {
      if (!Array.isArray(window.HIGHLIGHT_RULES)) window.HIGHLIGHT_RULES = [];
      const hlRules = window.HIGHLIGHT_RULES;

      // Build lookup maps from the sidebar DOM
      // Folders use .tree-item (not .folder-link); exclude root-item so "All Feeds" stays distinct
      function hlScopeData() {
        const folderNames = {};
        const feedTitles = {};
        for (const el of document.querySelectorAll('.tree-item[data-folder-id][data-folder-name]:not(.root-item):not([data-virtual])')) {
          folderNames[el.getAttribute('data-folder-id')] = el.getAttribute('data-folder-name');
        }
        for (const el of document.querySelectorAll('.feed-link[data-feed-url][data-folder-id]')) {
          const url = el.getAttribute('data-feed-url');
          const titleSpan = el.querySelector('.feed-label > span:last-child');
          feedTitles[url] = (titleSpan || el).textContent.trim();
        }
        return { folderNames, feedTitles };
      }

      const _hlCmpModes = ['slug', 'title', 'both', 'fuzzy'];
      const _hlCmpModeLabels = { slug: 'URL Slug', title: 'Title', both: 'Slug+Title', fuzzy: 'Fuzzy' };

      function hlBuildModeComparisonWrap(results, activeMode) {
        const modes = _hlCmpModes, modeLabels = _hlCmpModeLabels;
        function makePairEl(p) {
          const pairKey = (p.keep.link || p.keep.title || '') + '||' + (p.mark.link || p.mark.title || '');
          const isFalse = hlFalseMatches.has(pairKey);
          const pairEl = document.createElement('div');
          pairEl.className = 'hl-cmp-pair' + (isFalse ? ' hl-cmp-pair--false' : '');
          const modeWrap = document.createElement('div');
          modeWrap.className = 'hl-cmp-entry-modes';
          for (const m of p.modes) {
            const b = document.createElement('span');
            b.className = 'hl-cmp-mode-badge hl-cmp-mode-badge--' + m;
            b.textContent = modeLabels[m];
            modeWrap.appendChild(b);
          }
          pairEl.appendChild(modeWrap);
          for (const [side, label, cls] of [[p.keep, '✓ keep', 'hl-rule-dryrun-keep-label'], [p.mark, '✗ mark read', 'hl-rule-dryrun-markread-label']]) {
            const row = document.createElement('div');
            row.className = 'hl-cmp-pair-item ' + (side === p.keep ? 'hl-cmp-pair-keep' : 'hl-cmp-pair-mark');
            const lbl = document.createElement('span');
            lbl.className = 'hl-cmp-pair-label ' + cls;
            lbl.textContent = label;
            const titleEl = document.createElement('a');
            titleEl.className = 'hl-rule-dryrun-item-title';
            titleEl.href = side.link || '#'; titleEl.target = '_blank'; titleEl.rel = 'noopener';
            titleEl.textContent = side.title || side.link || '(no title)';
            const feedEl = document.createElement('span');
            feedEl.className = 'hl-rule-dryrun-item-feed';
            feedEl.appendChild(hlMakeFeedLink(side.feed_title, side.feed_url));
            row.appendChild(lbl); row.appendChild(titleEl); row.appendChild(feedEl);
            pairEl.appendChild(row);
          }
          const actionsEl = document.createElement('div');
          actionsEl.className = 'hl-cmp-pair-actions';
          const falseBtn = document.createElement('button');
          falseBtn.type = 'button';
          falseBtn.className = 'hl-cmp-pair-false-btn' + (isFalse ? ' active' : '');
          falseBtn.textContent = isFalse ? 'False match ✓' : 'False match?';
          falseBtn.title = isFalse ? 'Unmark as false match' : 'Mark as false match';
          falseBtn.addEventListener('click', async () => {
            await hlToggleFalseMatch(pairKey);
            const nowFalse = hlFalseMatches.has(pairKey);
            pairEl.classList.toggle('hl-cmp-pair--false', nowFalse);
            falseBtn.classList.toggle('active', nowFalse);
            falseBtn.textContent = nowFalse ? 'False match ✓' : 'False match?';
            falseBtn.title = nowFalse ? 'Unmark as false match' : 'Mark as false match';
          });
          actionsEl.appendChild(falseBtn);
          if (p.keep.feed_url && p.mark.feed_url && p.keep.feed_url !== p.mark.feed_url) {
            const pairCmpBtn = document.createElement('button');
            pairCmpBtn.type = 'button';
            pairCmpBtn.className = 'hl-cmp-pair-cmp-btn';
            pairCmpBtn.textContent = 'Compare feeds';
            const resultEl = document.createElement('div');
            resultEl.className = 'hl-cmp-pair-result';
            resultEl.hidden = true;
            pairCmpBtn.addEventListener('click', async () => {
              if (!resultEl.hidden) { resultEl.hidden = true; pairCmpBtn.textContent = 'Compare feeds'; return; }
              pairCmpBtn.disabled = true; pairCmpBtn.textContent = 'Comparing…';
              try {
                const feedUrlsParam = [p.keep.feed_url, p.mark.feed_url].join(',');
                const results = await Promise.all(_hlCmpModes.map(m => {
                  const qs = new URLSearchParams({ type: 'deduplicate', scope: 'global', keyword: m, feed_urls: feedUrlsParam, dedup_window_hours: 168 });
                  return fetch('/rules/dry-run?' + qs, { credentials: 'same-origin' }).then(r => r.json());
                }));
                resultEl.innerHTML = '';
                resultEl.appendChild(hlBuildModeComparisonWrap(results, null));
                resultEl.hidden = false;
                pairCmpBtn.textContent = 'Hide compare';
              } catch { pairCmpBtn.textContent = 'Compare feeds'; }
              pairCmpBtn.disabled = false;
            });
            actionsEl.appendChild(pairCmpBtn);
            pairEl.appendChild(actionsEl);
            pairEl.appendChild(resultEl);
          } else {
            pairEl.appendChild(actionsEl);
          }
          return pairEl;
        }
        function makeBucketSection(label, cls, pairs, open) {
          if (pairs.length === 0) return null;
          const sec = document.createElement('details');
          sec.className = 'hl-cmp-bucket ' + cls;
          sec.open = open;
          const sum = document.createElement('summary');
          sum.className = 'hl-cmp-bucket-summary';
          sum.textContent = label + ' (' + pairs.length + ')';
          sec.appendChild(sum);
          for (const p of pairs) sec.appendChild(makePairEl(p));
          return sec;
        }
        const wrap = document.createElement('div');
        wrap.className = 'hl-compare-wrap';
        const tbl = document.createElement('div');
        tbl.className = 'hl-compare-table';
        modes.forEach((m, mi) => {
          const d = results[mi];
          const rowEl = document.createElement('div');
          rowEl.className = 'hl-compare-row' + (m === activeMode ? ' hl-compare-row--active' : '');
          const modeSpan = document.createElement('span');
          modeSpan.className = 'hl-compare-mode';
          const badge = document.createElement('span');
          badge.className = 'hl-cmp-mode-badge hl-cmp-mode-badge--' + m;
          badge.textContent = modeLabels[m];
          modeSpan.appendChild(badge);
          const grpSpan = document.createElement('span');
          grpSpan.className = 'hl-compare-groups';
          grpSpan.textContent = (d.groups ? d.groups.length : '—') + ' groups';
          const markSpan = document.createElement('span');
          markSpan.className = 'hl-compare-mark';
          markSpan.textContent = (d.total_would_mark_read !== undefined ? d.total_would_mark_read : '—') + ' mark read';
          const detailPanel = document.createElement('div');
          detailPanel.className = 'hl-rule-dryrun-panel hl-compare-detail-panel';
          detailPanel.dataset.comparePanel = '1';
          detailPanel.hidden = true;
          const expandBtn = document.createElement('button');
          expandBtn.type = 'button';
          expandBtn.className = 'hl-compare-expand';
          expandBtn.textContent = 'Details';
          expandBtn.addEventListener('click', () => {
            if (detailPanel.hidden) {
              detailPanel.hidden = false; expandBtn.textContent = 'Hide';
              if (!detailPanel.dataset.rendered) {
                hlRenderDryRunResult(detailPanel, { ...d, type: 'deduplicate' }, null);
                detailPanel.dataset.rendered = '1';
              }
            } else { detailPanel.hidden = true; expandBtn.textContent = 'Details'; }
          });
          rowEl.appendChild(modeSpan); rowEl.appendChild(grpSpan);
          rowEl.appendChild(markSpan); rowEl.appendChild(expandBtn);
          tbl.appendChild(rowEl);
          tbl.appendChild(detailPanel);
        });
        wrap.appendChild(tbl);
        const pairMap = new Map();
        modes.forEach((m, mi) => {
          const d = results[mi];
          if (!d.groups) return;
          for (const g of d.groups) {
            const keepKey = g.keep.link || g.keep.title || '';
            for (const e of g.mark_read) {
              const markKey = e.link || e.title || '';
              const pairKey = keepKey + '||' + markKey;
              if (!markKey) continue;
              if (!pairMap.has(pairKey)) pairMap.set(pairKey, { keep: g.keep, mark: e, modes: new Set() });
              pairMap.get(pairKey).modes.add(m);
            }
          }
        });
        const allPairs = [...pairMap.values()].map(p => ({ ...p, modes: [...p.modes] }))
          .sort((a, b) => b.modes.length - a.modes.length || (a.mark.title || '').localeCompare(b.mark.title || ''));
        const totalPairs = allPairs.length;
        if (totalPairs > 0) {
          const ovHdr = document.createElement('div');
          ovHdr.className = 'hl-compare-overlap-hdr';
          ovHdr.textContent = totalPairs + ' duplicate pair' + (totalPairs === 1 ? '' : 's') + ' across all modes';
          wrap.appendChild(ovHdr);
          for (const [minCount, label, cls, open] of [
            [4, 'All 4 modes agree', 'hl-cmp-bucket--4', true],
            [3, '3 modes agree',     'hl-cmp-bucket--3', true],
            [2, '2 modes agree',     'hl-cmp-bucket--2', true],
          ]) {
            const sec = makeBucketSection(label, cls, allPairs.filter(p => p.modes.length === minCount), open);
            if (sec) wrap.appendChild(sec);
          }
          const singlePairs = allPairs.filter(p => p.modes.length === 1);
          if (singlePairs.length > 0) {
            const outerSec = document.createElement('details');
            outerSec.className = 'hl-cmp-bucket hl-cmp-bucket--1';
            outerSec.open = false;
            const outerSum = document.createElement('summary');
            outerSum.className = 'hl-cmp-bucket-summary';
            outerSum.textContent = 'Single mode only — review for false positives (' + singlePairs.length + ')';
            outerSec.appendChild(outerSum);
            for (const m of modes) {
              const mPairs = singlePairs.filter(p => p.modes[0] === m);
              if (mPairs.length === 0) continue;
              const subSec = document.createElement('details');
              subSec.className = 'hl-cmp-bucket hl-cmp-bucket--1-sub';
              subSec.open = false;
              const subSum = document.createElement('summary');
              subSum.className = 'hl-cmp-bucket-summary hl-cmp-bucket-summary--sub';
              const subBadge = document.createElement('span');
              subBadge.className = 'hl-cmp-mode-badge hl-cmp-mode-badge--' + m;
              subBadge.textContent = modeLabels[m];
              subSum.appendChild(subBadge);
              subSum.appendChild(document.createTextNode(' only (' + mPairs.length + ')'));
              subSec.appendChild(subSum);
              for (const p of mPairs) subSec.appendChild(makePairEl(p));
              outerSec.appendChild(subSec);
            }
            wrap.appendChild(outerSec);
          }
        }
        return wrap;
      }

      function hlMakeFeedLink(feedTitle, feedUrl) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'hl-feed-link';
        btn.textContent = feedTitle || feedUrl || '(unknown feed)';
        btn.title = feedUrl || '';
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          if (feedUrl) openFeedPropertiesModal(feedUrl);
        });
        return btn;
      }

      function hlScopeLabel(scope, scopeId) {
        if (scope === 'global') return 'All Feeds';
        const { folderNames, feedTitles } = hlScopeData();
        if (scope === 'folder') return folderNames[scopeId] || `Folder ${scopeId}`;
        if (scope === 'feed') return feedTitles[scopeId] || scopeId;
        if (scope === 'feeds') {
          const urls = String(scopeId || '').split('\n').map(s => s.trim()).filter(Boolean);
          if (urls.length <= 2) return urls.map(u => feedTitles[u] || u).join(', ');
          return `${urls.length} feeds`;
        }
        return scope;
      }

      let hlFalseMatches = new Set();
      async function hlLoadFalseMatches() {
        try {
          const resp = await fetch('/dedup/false-matches', { credentials: 'same-origin' });
          const data = await resp.json();
          hlFalseMatches = new Set((data.pairs || []).map(p => p.keep_link + '||' + p.mark_link));
        } catch { /* non-fatal */ }
      }
      async function hlToggleFalseMatch(key) {
        const [keep_link, mark_link] = key.split('||');
        try {
          const resp = await fetch('/dedup/false-match', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keep_link, mark_link }),
          });
          const data = await resp.json();
          if (data.active) hlFalseMatches.add(key);
          else hlFalseMatches.delete(key);
        } catch { /* non-fatal */ }
      }

      let hlActiveDryRun = null;
      let hlActiveHistPanel = null;
      let hlDragSrcIdx = null;

      // Tab switching (Automation modal only)
      document.querySelectorAll('[data-hl-tab]').forEach(btn => {
        btn.addEventListener('click', () => {
          const tab = btn.dataset.hlTab;
          document.querySelectorAll('[data-hl-tab]').forEach(b => { b.classList.toggle('hl-tab-btn--active', b === btn); b.setAttribute('aria-selected', b === btn); });
          document.getElementById('hl-tab-rules').hidden = tab !== 'rules';
          document.getElementById('hl-tab-history').hidden = tab !== 'history';
          if (tab === 'history') hlLoadHistory();
        });
      });

      // Shared "show matched articles" expansion for a run-log row. Used by both
      // the global History tab and the per-rule history panel. `h` is a
      // /automation/history row; the button + lazy-loaded entry list are
      // appended to `item`.
      function hlAttachHistoryExpansion(item, h) {
        if (!h.entries_affected) return;
        const n = h.entries_affected;
        const expandBtn = document.createElement('button');
        expandBtn.type = 'button';
        expandBtn.className = 'hl-hist-expand-btn';
        expandBtn.title = 'Show matched articles';
        expandBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">expand_more</span>';
        let entryList = null;
        expandBtn.addEventListener('click', async () => {
          if (entryList) {
            const collapsed = entryList.classList.toggle('is-collapsed');
            expandBtn.querySelector('.material-symbols-rounded').textContent = collapsed ? 'expand_more' : 'expand_less';
            return;
          }
          entryList = document.createElement('div');
          entryList.className = 'hl-hist-entry-list';
          entryList.textContent = 'Loading…';
          item.appendChild(entryList);
          expandBtn.querySelector('.material-symbols-rounded').textContent = 'expand_less';
          try {
            const er = await fetch(`/automation/history/${h.id}/entries`, { credentials: 'same-origin' });
            const ed = await er.json();
            entryList.textContent = '';
            const entries = ed.entries || [];
            if (entries.length === 0) {
              entryList.textContent = 'No detail stored for this run.';
            } else {
              entries.forEach(e => {
                const row = document.createElement('div');
                row.className = 'hl-hist-entry-row';
                if (e.role === 'kept') row.classList.add('hl-hist-entry-row--kept');
                const feedEl = document.createElement('span');
                feedEl.className = 'hl-hist-entry-feed';
                feedEl.textContent = e.feed_title || e.feed_url || '';
                const titleEl = e.link
                  ? document.createElement('a')
                  : document.createElement('span');
                titleEl.className = 'hl-hist-entry-title';
                titleEl.textContent = e.title || '(untitled)';
                if (e.link) { titleEl.href = e.link; titleEl.target = '_blank'; titleEl.rel = 'noopener noreferrer'; }
                row.appendChild(feedEl);
                row.appendChild(titleEl);
                if (e.role === 'kept') {
                  const keptChip = document.createElement('span');
                  keptChip.className = 'hl-hist-entry-kept-chip';
                  keptChip.textContent = 'kept';
                  keptChip.title = 'Surviving copy — duplicates were matched against this entry';
                  row.appendChild(keptChip);
                }
                entryList.appendChild(row);
              });
              const markedShown = entries.filter(e => e.role !== 'kept').length;
              if (n > markedShown) {
                const moreEl = document.createElement('p');
                moreEl.className = 'hl-hist-entry-more';
                moreEl.textContent = `… and ${n - markedShown} more`;
                entryList.appendChild(moreEl);
              }
            }
          } catch { entryList.textContent = 'Failed to load matches.'; }
        });
        item.appendChild(expandBtn);
      }

      async function hlLoadHistory() {
        const listEl = document.getElementById('hl-history-list');
        if (!listEl) return;
        listEl.innerHTML = '<p class="hl-empty">Loading…</p>';
        try {
          const resp = await fetch('/automation/history', { credentials: 'same-origin' });
          const data = await resp.json();
          listEl.innerHTML = '';
          if (!data.history || data.history.length === 0) {
            listEl.innerHTML = '<p class="hl-empty">No history yet — rules log entries when Run Now is used.</p>';
            return;
          }
          const { folderNames, feedTitles } = hlScopeData();
          const typeLabels = { mark_as_read: 'Mark Read', deduplicate: 'Dedup', email_article: 'Email', webhook: 'Webhook', youtube_playlist: 'YT Playlist', instapaper: 'Instapaper', quire: 'Quire', highlight: 'Highlight', tag_filter: 'Tag Filter' };
          const methodLabels = { safe: 'Safe', slug: 'URL Slug', title: 'Title', both: 'Slug+Title', fuzzy: 'Fuzzy' };
          for (const row of data.history) {
            const el = document.createElement('div');
            el.className = 'hl-history-row';
            const typeChip = document.createElement('span');
            typeChip.className = `hl-rule-type-chip hl-rule-type-chip--${row.rule_type}`;
            typeChip.textContent = typeLabels[row.rule_type] || row.rule_type;
            el.appendChild(typeChip);
            if (row.rule_type === 'deduplicate') {
              const mBadge = document.createElement('span');
              mBadge.className = 'hl-rule-badge';
              mBadge.textContent = methodLabels[row.keyword] || row.keyword;
              el.appendChild(mBadge);
            } else if (row.keyword) {
              const kBadge = document.createElement('span');
              kBadge.className = 'hl-rule-badge';
              kBadge.textContent = row.keyword;
              el.appendChild(kBadge);
            }
            const scopeEl = document.createElement('span');
            scopeEl.className = 'hl-rule-scope';
            scopeEl.textContent = row.scope === 'global' ? 'All Feeds' : row.scope === 'folder' ? (folderNames[row.scope_id] || row.scope_id) : (feedTitles[row.scope_id] || row.scope_id);
            el.appendChild(scopeEl);
            const countEl = document.createElement('span');
            countEl.className = 'hl-history-count';
            countEl.textContent = row.entries_affected + ' marked read';
            el.appendChild(countEl);
            const tsEl = document.createElement('span');
            tsEl.className = 'hl-history-ts';
            tsEl.textContent = new Date(row.run_at).toLocaleString();
            el.appendChild(tsEl);
            hlAttachHistoryExpansion(el, row);
            listEl.appendChild(el);
          }
        } catch { listEl.innerHTML = '<p class="hl-empty">Failed to load history.</p>'; }
      }

      async function hlSaveOrder() {
        const order = hlRules.map(r => ({ scope: r.scope, scope_id: r.scope_id, keyword: r.keyword }));
        try {
          await fetch('/highlights/reorder', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ order }) });
        } catch { /* non-fatal */ }
      }

      function hlRenderRules() {
        const listEl = document.getElementById('hl-rules-list');
        if (!listEl) return;
        listEl.querySelectorAll('.hl-rule-row, .hl-empty, .hl-rule-dryrun-panel, .hl-rule-hist-panel, .hl-section-label').forEach(el => el.remove());
        hlActiveDryRun = null;
        hlActiveHistPanel = null;
        if (hlRules.length === 0) {
          const empty = document.createElement('p');
          empty.className = 'hl-empty';
          empty.textContent = 'No automation rules yet. Click "+ Add Rule" to create one.';
          listEl.appendChild(empty);
          return;
        }
        const TYPE_ORDER = ['highlight', 'mark_as_read', 'tag_filter', 'deduplicate', 'email_article', 'webhook', 'youtube_playlist', 'instapaper', 'quire'];
        const TYPE_LABELS = { highlight: 'Highlight', mark_as_read: 'Mark as Read', tag_filter: 'Tag Filter', deduplicate: 'Deduplicate', email_article: 'Email Article', webhook: 'Webhook', youtube_playlist: 'Add to YT Playlist', instapaper: 'Save to Instapaper', quire: 'Add to Quire' };
        for (const sectionType of TYPE_ORDER) {
          const sectionRules = hlRules.map((r, i) => ({ r, i })).filter(({ r }) => (r.type || 'highlight') === sectionType);
          if (sectionRules.length === 0) continue;
          const labelEl = document.createElement('div');
          labelEl.className = 'hl-section-label';
          labelEl.textContent = TYPE_LABELS[sectionType] || sectionType;
          listEl.appendChild(labelEl);
          for (const { r: rule, i } of sectionRules) {
          const enabled = rule.enabled !== 0;
          const row = document.createElement('div');
          row.className = 'hl-rule-row' + (enabled ? '' : ' hl-rule-row--disabled');
          row.dataset.ruleIdx = i;

          // Drag handle (mouse + touch)
          const handle = document.createElement('span');
          handle.className = 'hl-rule-handle';
          handle.title = 'Drag to reorder';
          handle.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">drag_indicator</span>';
          handle.addEventListener('mousedown', () => row.setAttribute('draggable', 'true'));
          row.addEventListener('dragstart', e => {
            hlDragSrcIdx = i;
            e.dataTransfer.effectAllowed = 'move';
            row.classList.add('hl-rule-dragging');
          });
          row.addEventListener('dragend', () => {
            row.removeAttribute('draggable');
            row.classList.remove('hl-rule-dragging');
            listEl.querySelectorAll('.hl-rule-drag-over').forEach(el => el.classList.remove('hl-rule-drag-over'));
            hlDragSrcIdx = null;
          });
          row.addEventListener('dragover', e => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            listEl.querySelectorAll('.hl-rule-drag-over').forEach(el => el.classList.remove('hl-rule-drag-over'));
            row.classList.add('hl-rule-drag-over');
          });
          row.addEventListener('drop', e => {
            e.preventDefault();
            if (hlDragSrcIdx === null || hlDragSrcIdx === i) return;
            if ((hlRules[hlDragSrcIdx].type || 'highlight') !== sectionType) return;
            const moved = hlRules.splice(hlDragSrcIdx, 1)[0];
            hlRules.splice(i, 0, moved);
            hlSaveOrder();
            hlRenderRules();
          });
          handle.addEventListener('touchstart', e => {
            e.preventDefault();
            hlDragSrcIdx = i;
            row.classList.add('hl-rule-dragging');
          }, { passive: false });
          handle.addEventListener('touchmove', e => {
            e.preventDefault();
            const touch = e.touches[0];
            const target = document.elementFromPoint(touch.clientX, touch.clientY);
            const targetRow = target && target.closest('.hl-rule-row');
            listEl.querySelectorAll('.hl-rule-drag-over').forEach(el => el.classList.remove('hl-rule-drag-over'));
            if (targetRow && targetRow !== row) targetRow.classList.add('hl-rule-drag-over');
          }, { passive: false });
          handle.addEventListener('touchend', e => {
            e.preventDefault();
            row.classList.remove('hl-rule-dragging');
            const overRow = listEl.querySelector('.hl-rule-drag-over');
            listEl.querySelectorAll('.hl-rule-drag-over').forEach(el => el.classList.remove('hl-rule-drag-over'));
            if (overRow && hlDragSrcIdx !== null) {
              const destIdx = parseInt(overRow.dataset.ruleIdx, 10);
              if (!isNaN(destIdx) && destIdx !== hlDragSrcIdx &&
                  (hlRules[destIdx]?.type || 'highlight') === (hlRules[hlDragSrcIdx]?.type || 'highlight')) {
                const moved = hlRules.splice(hlDragSrcIdx, 1)[0];
                hlRules.splice(destIdx > hlDragSrcIdx ? destIdx - 1 : destIdx, 0, moved);
                hlSaveOrder();
                hlRenderRules();
              }
            }
            hlDragSrcIdx = null;
          }, { passive: false });
          row.appendChild(handle);

          // Toggle enable/disable
          const toggle = document.createElement('button');
          toggle.type = 'button';
          toggle.className = 'hl-rule-toggle';
          toggle.setAttribute('aria-label', enabled ? 'Disable rule' : 'Enable rule');
          toggle.innerHTML = `<span class="material-symbols-rounded" aria-hidden="true">${enabled ? 'toggle_on' : 'toggle_off'}</span>`;
          toggle.addEventListener('click', async () => {
            const newEnabled = rule.enabled === 0 ? 1 : 0;
            const body = new URLSearchParams({ scope: rule.scope, scope_id: rule.scope_id, keyword: rule.keyword, enabled: newEnabled });
            try {
              const resp = await fetch('/highlights/toggle', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
              if (!resp.ok) throw new Error('failed');
              rule.enabled = newEnabled;
              hlRenderRules();
              applyHighlights();
            } catch { window.alert('Failed to toggle rule'); }
          });
          row.appendChild(toggle);

          const ruleType = rule.type || 'highlight';

          if (ruleType === 'deduplicate') {
            const methodBadge = document.createElement('span');
            methodBadge.className = 'hl-rule-badge';
            const methodLabels = { safe: 'Safe', slug: 'URL Slug', title: 'Title', both: 'Slug + Title', fuzzy: 'Fuzzy Title' };
            methodBadge.textContent = methodLabels[rule.keyword] || rule.keyword;
            row.appendChild(methodBadge);
            if (!['slug', 'safe'].includes(rule.keyword)) { // title, both, fuzzy show window
              const winBadge = document.createElement('span');
              winBadge.className = 'hl-rule-search-in-badge';
              winBadge.textContent = Math.round((rule.dedup_window_hours || 168) / 24) + 'd';
              row.appendChild(winBadge);
            }
            if (rule.exclude_scope_ids) {
              const exclIds = rule.exclude_scope_ids.split(',').map(s => s.trim()).filter(Boolean);
              if (exclIds.length > 0) {
                const folderNames = exclIds.map(fid => {
                  const el = document.querySelector(`.tree-item[data-folder-id="${fid}"][data-folder-name]`);
                  return el ? el.getAttribute('data-folder-name') : '#' + fid;
                });
                const exclBadge = document.createElement('span');
                exclBadge.className = 'hl-rule-badge hl-rule-excl-badge';
                exclBadge.textContent = '−' + folderNames.join(', ');
                row.appendChild(exclBadge);
              }
            }
          } else {
            const badge = document.createElement('span');
            badge.className = ruleType === 'highlight'
              ? `hl-rule-badge highlight-mark-${rule.color}`
              : 'hl-rule-badge';
            badge.textContent = (ruleType === 'youtube_playlist' && !rule.keyword) ? 'all videos' : rule.keyword;
            row.appendChild(badge);

            if (rule.is_regex) {
              const rb = document.createElement('span');
              rb.className = 'hl-rule-regex-badge';
              rb.textContent = '.*';
              row.appendChild(rb);
            }

            const searchIn = rule.search_in || 'title';
            if (searchIn !== 'title') {
              const sinBadge = document.createElement('span');
              sinBadge.className = 'hl-rule-search-in-badge';
              sinBadge.textContent = searchIn === 'body' ? 'body' : 'title+body';
              row.appendChild(sinBadge);
            }

            if (ruleType === 'email_article') {
              if ((rule.delivery || 'immediately') === 'batch') {
                const delivBadge = document.createElement('span');
                delivBadge.className = 'hl-rule-delivery-badge';
                delivBadge.textContent = 'batch';
                row.appendChild(delivBadge);
              }
              if (rule.email_to) {
                const contacts = window.EMAIL_CONTACTS || [];
                const contact = contacts.find(c => c.address === rule.email_to);
                const toSpan = document.createElement('span');
                toSpan.className = 'hl-rule-email-to';
                let toLabel;
                if (rule.email_to === window.PROFILE_EMAIL) toLabel = 'Me';
                else if (contact) toLabel = contact.label;
                else toLabel = rule.email_to;
                toSpan.textContent = '→ ' + toLabel;
                row.appendChild(toSpan);
              }
              if (rule.cc_me) {
                const ccBadge = document.createElement('span');
                ccBadge.className = 'hl-rule-cc-badge';
                ccBadge.textContent = 'Cc me';
                row.appendChild(ccBadge);
              }
            }

            if (ruleType === 'webhook') {
              const fmtBadge = document.createElement('span');
              fmtBadge.className = 'hl-rule-delivery-badge';
              fmtBadge.textContent = (rule.webhook_format === 'ifttt') ? 'IFTTT' : (rule.webhook_batch ? 'JSON (batch)' : 'JSON');
              row.appendChild(fmtBadge);
              if (rule.webhook_url) {
                const toSpan = document.createElement('span');
                toSpan.className = 'hl-rule-email-to';
                let host = rule.webhook_url;
                try { host = new URL(rule.webhook_url).host; } catch {}
                toSpan.textContent = '→ ' + host;
                toSpan.title = rule.webhook_url;
                row.appendChild(toSpan);
              }
            }

            if (ruleType === 'youtube_playlist') {
              const toSpan = document.createElement('span');
              toSpan.className = 'hl-rule-email-to';
              toSpan.textContent = '▶ ' + (rule.yt_playlist_title || rule.yt_playlist_id || 'playlist');
              row.appendChild(toSpan);
              if (rule.yt_include_shorts) {
                const b = document.createElement('span');
                b.className = 'hl-rule-search-in-badge';
                b.textContent = '+Shorts';
                row.appendChild(b);
              }
              if (rule.yt_mark_read) {
                const b = document.createElement('span');
                b.className = 'hl-rule-delivery-badge';
                b.textContent = 'mark read';
                row.appendChild(b);
              }
              if (rule.yt_min_minutes || rule.yt_max_minutes) {
                const b = document.createElement('span');
                b.className = 'hl-rule-search-in-badge';
                const lo = rule.yt_min_minutes || 0, hi = rule.yt_max_minutes || 0;
                b.textContent = hi ? (lo ? `${lo}–${hi}m` : `≤${hi}m`) : `≥${lo}m`;
                row.appendChild(b);
              }
            }
          }

          const scopeEl = document.createElement('span');
          scopeEl.className = 'hl-rule-scope';
          scopeEl.textContent = hlScopeLabel(rule.scope, rule.scope_id);
          row.appendChild(scopeEl);

          // Test (dry-run) button
          const testBtn = document.createElement('button');
          testBtn.type = 'button';
          testBtn.className = 'hl-rule-test';
          testBtn.setAttribute('aria-label', 'Test rule (dry run)');
          testBtn.title = 'Test (dry run) — preview matching articles without acting';
          testBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">play_circle</span>';
          testBtn.addEventListener('click', async () => {
            const ruleKey = rule.scope + '|' + rule.scope_id + '|' + rule.keyword;
            // Toggle off if already showing for this rule
            if (hlActiveDryRun && hlActiveDryRun.dataset.ruleKey === ruleKey) {
              hlActiveDryRun.remove(); hlActiveDryRun = null;
              testBtn.classList.remove('active');
              return;
            }
            if (hlActiveDryRun) { hlActiveDryRun.remove(); hlActiveDryRun = null; }
            if (hlActiveHistPanel) { hlActiveHistPanel.remove(); hlActiveHistPanel = null; }
            // Also clear active class from all test/hist buttons
            document.querySelectorAll('.hl-rule-test.active, .hl-rule-hist.active').forEach(b => b.classList.remove('active'));
            testBtn.classList.add('active');
            const panel = document.createElement('div');
            panel.className = 'hl-rule-dryrun-panel hl-rule-dryrun-panel--loading';
            panel.dataset.ruleKey = ruleKey;
            panel.textContent = 'Running test…';
            row.insertAdjacentElement('afterend', panel);
            hlActiveDryRun = panel;
            try {
              const qs = new URLSearchParams({
                type: rule.type || 'highlight',
                scope: rule.scope, scope_id: rule.scope_id || '',
                keyword: rule.keyword || '', is_regex: rule.is_regex || 0,
                search_in: rule.search_in || 'title',
                dedup_window_hours: rule.dedup_window_hours || 168,
                exclude_scope_ids: rule.exclude_scope_ids || '',
                yt_include_shorts: rule.yt_include_shorts ? 1 : 0,
                yt_min_minutes: rule.yt_min_minutes || 0,
                yt_max_minutes: rule.yt_max_minutes || 0,
              });
              const resp = await fetch('/rules/dry-run?' + qs.toString(), { credentials: 'same-origin' });
              const data = await resp.json();
              panel.classList.remove('hl-rule-dryrun-panel--loading');
              hlRenderDryRunResult(panel, data, rule);
            } catch { panel.textContent = 'Failed to run test'; panel.classList.remove('hl-rule-dryrun-panel--loading'); }
          });
          row.appendChild(testBtn);

          // Run Now button — only for actionable server-side rule types
          if (ruleType === 'mark_as_read' || ruleType === 'deduplicate' || ruleType === 'tag_filter') {
            const runBtn = document.createElement('button');
            runBtn.type = 'button';
            runBtn.className = 'hl-rule-run';
            runBtn.setAttribute('aria-label', 'Run rule now on unread entries');
            runBtn.title = 'Run now — apply this rule to existing unread entries';
            runBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">bolt</span>';
            runBtn.addEventListener('click', async () => {
              const scopeLabel = hlScopeLabel(rule.scope, rule.scope_id);
              const ruleLabel = ruleType === 'deduplicate'
                ? `Deduplicate (${rule.keyword}) in ${scopeLabel}`
                : (ruleType === 'tag_filter'
                  ? `Tag filter (${rule.keyword}) in ${scopeLabel}`
                  : `"${rule.keyword}" mark-as-read in ${scopeLabel}`);
              if (!window.confirm(`Run rule now on all matching unread entries?\n\n${ruleLabel}\n\nThis cannot be undone.`)) return;
              runBtn.disabled = true;
              runBtn.style.opacity = '0.5';
              try {
                const body = new URLSearchParams({
                  type: rule.type || 'highlight',
                  scope: rule.scope, scope_id: rule.scope_id || '',
                  keyword: rule.keyword || '', is_regex: rule.is_regex || 0,
                  search_in: rule.search_in || 'title',
                  dedup_window_hours: rule.dedup_window_hours || 168,
                  exclude_scope_ids: rule.exclude_scope_ids || '',
                });
                const resp = await fetch('/rules/run-now', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
                const data = await resp.json();
                if (!resp.ok || data.error) throw new Error(data.error || 'failed');
                const n = data.count;
                if (n === 0) {
                  showToastMessage('No matching unread entries found.');
                  runBtn.disabled = false;
                  runBtn.style.opacity = '';
                } else {
                  showToastMessage(`Marked ${n} entr${n === 1 ? 'y' : 'ies'} as read.`);
                  window.location.reload();
                }
              } catch (err) {
                window.alert('Run failed: ' + (err.message || err));
                runBtn.disabled = false;
                runBtn.style.opacity = '';
              }
            });
            row.appendChild(runBtn);

            // History button — shows recent runs for this specific rule
            const histBtn = document.createElement('button');
            histBtn.type = 'button';
            histBtn.className = 'hl-rule-hist';
            histBtn.setAttribute('aria-label', 'Show run history for this rule');
            histBtn.title = 'Run history — what this rule has done';
            histBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">history</span>';
            histBtn.addEventListener('click', async () => {
              const ruleKey = rule.scope + '|' + rule.scope_id + '|' + rule.keyword;
              if (hlActiveHistPanel && hlActiveHistPanel.dataset.ruleKey === ruleKey) {
                hlActiveHistPanel.remove(); hlActiveHistPanel = null;
                histBtn.classList.remove('active');
                return;
              }
              if (hlActiveHistPanel) { hlActiveHistPanel.remove(); hlActiveHistPanel = null; }
              document.querySelectorAll('.hl-rule-hist.active').forEach(b => b.classList.remove('active'));
              histBtn.classList.add('active');
              const panel = document.createElement('div');
              panel.className = 'hl-rule-hist-panel hl-rule-hist-panel--loading';
              panel.dataset.ruleKey = ruleKey;
              panel.textContent = 'Loading…';
              row.insertAdjacentElement('afterend', panel);
              hlActiveHistPanel = panel;
              try {
                const qs = new URLSearchParams({
                  scope: rule.scope, scope_id: rule.scope_id || '',
                  keyword: rule.keyword || '', limit: 20,
                });
                const resp = await fetch('/automation/history?' + qs.toString(), { credentials: 'same-origin' });
                const data = await resp.json();
                panel.classList.remove('hl-rule-hist-panel--loading');
                panel.textContent = '';
                const rows = data.history || [];
                if (rows.length === 0) {
                  panel.textContent = 'No runs recorded yet.';
                } else {
                  rows.forEach(h => {
                    const item = document.createElement('div');
                    item.className = 'hl-rule-hist-item';
                    const timeEl = document.createElement('span');
                    timeEl.className = 'hl-rule-hist-time';
                    const d = new Date(h.run_at);
                    timeEl.textContent = formatRelativeDate(d);
                    timeEl.title = localTimeFormatterLong.format(d);
                    const countEl = document.createElement('span');
                    countEl.className = 'hl-rule-hist-count';
                    const n = h.entries_affected;
                    countEl.textContent = n === 0 ? 'no matches' : `${n} matched`;
                    const trigBadge = document.createElement('span');
                    trigBadge.className = `hl-rule-hist-trigger hl-rule-hist-trigger--${h.trigger}`;
                    trigBadge.textContent = h.trigger;
                    item.appendChild(timeEl);
                    item.appendChild(countEl);
                    item.appendChild(trigBadge);
                    hlAttachHistoryExpansion(item, h);
                    panel.appendChild(item);
                  });
                }
              } catch { panel.textContent = 'Failed to load history'; panel.classList.remove('hl-rule-hist-panel--loading'); }
            });
            row.appendChild(histBtn);
          }

          const edit = document.createElement('button');
          edit.type = 'button';
          edit.className = 'hl-rule-edit';
          edit.setAttribute('aria-label', `Edit rule for "${rule.keyword}"`);
          edit.title = 'Edit rule';
          edit.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">edit</span>';
          edit.addEventListener('click', () => {
            const origScope = rule.scope, origScopeId = rule.scope_id, origKeyword = rule.keyword;
            const origEnabled = rule.enabled;
            const draft = hlBuildDraft(rule, 'Save', async (updated) => {
              const removeBody = new URLSearchParams({ scope: origScope, scope_id: origScopeId, keyword: origKeyword });
              const addBody = hlRuleToParams({ ...updated, enabled: origEnabled });
              try {
                const [r1, r2] = await Promise.all([
                  fetch('/highlights/remove', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: removeBody.toString() }),
                  fetch('/highlights/add', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: addBody.toString() }),
                ]);
                if (!r1.ok || !r2.ok) throw new Error('failed');
                const idx = hlRules.findIndex(r => r.scope === origScope && r.scope_id === origScopeId && r.keyword === origKeyword);
                if (idx >= 0) hlRules[idx] = { ...updated, enabled: origEnabled };
                hlHideDraft();
                hlRenderRules();
                applyHighlights();
              } catch { window.alert('Failed to update rule'); }
            });
            hlInsertDraft(draft, row);
          });
          row.appendChild(edit);

          // Duplicate: open a NEW add-draft prefilled from this rule so you can
          // quickly make a similar rule (e.g. point it at a different feed). The
          // copy starts disabled; change the scope/feed/keyword before saving (an
          // unchanged copy would just overwrite the original, same primary key).
          const dup = document.createElement('button');
          dup.type = 'button';
          dup.className = 'hl-rule-edit hl-rule-duplicate';
          dup.setAttribute('aria-label', `Duplicate rule for "${rule.keyword}"`);
          dup.title = 'Duplicate rule';
          dup.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">content_copy</span>';
          dup.addEventListener('click', () => {
            // folder_id helps the draft preselect the folder for a feed-scoped rule.
            const prefill = { ...rule };
            if (rule.scope === 'feed') {
              const fl = document.querySelector(`.feed-link[data-feed-url="${CSS.escape(rule.scope_id)}"]`);
              if (fl) prefill.folder_id = fl.getAttribute('data-folder-id') || '';
            }
            hlMakeAddDraft(prefill, row);
          });
          row.appendChild(dup);

          const del = document.createElement('button');
          del.type = 'button';
          del.className = 'hl-rule-delete';
          del.setAttribute('aria-label', `Delete rule for "${rule.keyword}"`);
          del.title = 'Delete rule';
          del.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">delete</span>';
          del.addEventListener('click', async () => {
            const body = new URLSearchParams({ scope: rule.scope, scope_id: rule.scope_id, keyword: rule.keyword });
            try {
              const resp = await fetch('/highlights/remove', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
              if (!resp.ok) throw new Error('failed');
              const idx = hlRules.findIndex(r => r.scope === rule.scope && r.scope_id === rule.scope_id && r.keyword === rule.keyword);
              if (idx >= 0) hlRules.splice(idx, 1);
              hlRenderRules();
              applyHighlights();
            } catch { window.alert('Failed to delete rule'); }
          });
          row.appendChild(del);
          listEl.appendChild(row);
          } // end sectionRules loop
        } // end TYPE_ORDER loop
      }

      function hlRenderDedupGroups(panel, groups, maxShown) {
        const shown = groups.slice(0, maxShown);
        shown.forEach(g => {
          const gDiv = document.createElement('div');
          gDiv.className = 'hl-rule-dryrun-group';
          const matchLabel = { slug: 'slug', title: 'title', 'slug+title': 'slug+title', fuzzy: g.matched_value, safe: g.matched_value }[g.match_by] || g.match_by;
          const matchByEl = document.createElement('div');
          matchByEl.className = 'hl-rule-dryrun-match-by';
          matchByEl.textContent = 'matched on ' + matchLabel;
          gDiv.appendChild(matchByEl);
          const keepEl = document.createElement('div');
          keepEl.className = 'hl-rule-dryrun-item hl-rule-dryrun-keep-item';
          const keepTitle = document.createElement('a');
          keepTitle.className = 'hl-rule-dryrun-item-title';
          keepTitle.href = g.keep.link || '#'; keepTitle.target = '_blank'; keepTitle.rel = 'noopener';
          keepTitle.textContent = g.keep.title || g.keep.link || '(no title)';
          const keepFeed = document.createElement('span');
          keepFeed.className = 'hl-rule-dryrun-item-feed hl-rule-dryrun-keep-label';
          keepFeed.appendChild(document.createTextNode('✓ keep · '));
          keepFeed.appendChild(hlMakeFeedLink(g.keep.feed_title, g.keep.feed_url));
          keepEl.appendChild(keepTitle); keepEl.appendChild(keepFeed); gDiv.appendChild(keepEl);
          for (const dup of g.mark_read) {
            const dupKey = (g.keep.link || g.keep.title || '') + '||' + (dup.link || dup.title || '');
            const dupIsFalse = hlFalseMatches.has(dupKey);
            const dupEl = document.createElement('div');
            dupEl.className = 'hl-rule-dryrun-item' + (dupIsFalse ? ' hl-cmp-pair--false' : '');
            const dupTitle = document.createElement('a');
            dupTitle.className = 'hl-rule-dryrun-item-title';
            dupTitle.href = dup.link || '#'; dupTitle.target = '_blank'; dupTitle.rel = 'noopener';
            dupTitle.textContent = dup.title || dup.link || '(no title)';
            const dupFeed = document.createElement('span');
            dupFeed.className = 'hl-rule-dryrun-item-feed hl-rule-dryrun-markread-label';
            dupFeed.appendChild(document.createTextNode('✗ mark read · '));
            dupFeed.appendChild(hlMakeFeedLink(dup.feed_title, dup.feed_url));
            const dupFalseBtn = document.createElement('button');
            dupFalseBtn.type = 'button';
            dupFalseBtn.className = 'hl-cmp-pair-false-btn' + (dupIsFalse ? ' active' : '');
            dupFalseBtn.textContent = dupIsFalse ? 'False match ✓' : 'False match?';
            dupFalseBtn.title = dupIsFalse ? 'Unmark as false match' : 'Mark as false match';
            dupFalseBtn.addEventListener('click', async () => {
              await hlToggleFalseMatch(dupKey);
              const nowFalse = hlFalseMatches.has(dupKey);
              dupEl.classList.toggle('hl-cmp-pair--false', nowFalse);
              dupFalseBtn.classList.toggle('active', nowFalse);
              dupFalseBtn.textContent = nowFalse ? 'False match ✓' : 'False match?';
              dupFalseBtn.title = nowFalse ? 'Unmark as false match' : 'Mark as false match';
            });
            dupEl.appendChild(dupTitle); dupEl.appendChild(dupFeed); dupEl.appendChild(dupFalseBtn); gDiv.appendChild(dupEl);
          }
          panel.appendChild(gDiv);
        });
      }

      function hlRenderDryRunResult(panel, data, rule) {
        panel.innerHTML = '';
        if (data.error) { panel.textContent = 'Error: ' + data.error; return; }

        if (data.type === 'deduplicate') {
          const groups = data.groups || [];
          const PAGE = 30;
          let shownCount = PAGE;

          const summary = document.createElement('div');
          summary.className = 'hl-rule-dryrun-summary';
          const updateSummary = () => {
            if (groups.length === 0) {
              summary.textContent = 'No duplicates found' + (data.message ? ' — ' + data.message : '') + ' (' + (data.total_entries_scanned || 0) + ' scanned)';
            } else {
              summary.textContent = groups.length + ' duplicate group' + (groups.length === 1 ? '' : 's') + ' · ' + data.total_would_mark_read + ' would be marked read · ' + (data.total_entries_scanned || 0) + ' scanned';
            }
          };
          updateSummary();
          panel.appendChild(summary);

          // Compare modes button (only for top-level dedup panels, not inside a compare sub-panel)
          if (rule && !panel.dataset.comparePanel) {
            const cmpBtn = document.createElement('button');
            cmpBtn.type = 'button';
            cmpBtn.className = 'hl-dryrun-compare-btn';
            cmpBtn.textContent = 'Compare all modes';
            cmpBtn.addEventListener('click', async () => {
              cmpBtn.disabled = true; cmpBtn.textContent = 'Comparing…';
              const modes = ['slug', 'title', 'both', 'fuzzy'];
              const modeLabels = { slug: 'URL Slug', title: 'Title', both: 'Slug+Title', fuzzy: 'Fuzzy' };
              try {
                const results = await Promise.all(modes.map(m => {
                  const qs = new URLSearchParams({ type: 'deduplicate', scope: rule.scope, scope_id: rule.scope_id || '', keyword: m, dedup_window_hours: rule.dedup_window_hours || 168, exclude_scope_ids: rule.exclude_scope_ids || '' });
                  return fetch('/rules/dry-run?' + qs, { credentials: 'same-origin' }).then(r => r.json());
                }));

                cmpBtn.replaceWith(hlBuildModeComparisonWrap(results, rule.keyword));
              } catch (err) { cmpBtn.textContent = 'Compare failed'; cmpBtn.disabled = false; }
            });
            panel.appendChild(cmpBtn);

            // Compare specific feeds button
            const cmpFeedsBtn = document.createElement('button');
            cmpFeedsBtn.type = 'button';
            cmpFeedsBtn.className = 'hl-dryrun-compare-btn hl-dryrun-compare-feeds-btn';
            cmpFeedsBtn.textContent = 'Compare specific feeds…';
            cmpFeedsBtn.addEventListener('click', () => {
              cmpFeedsBtn.remove();
              const { feedTitles } = hlScopeData();
              const feedOptions = Object.entries(feedTitles)
                .sort((a, b) => a[1].localeCompare(b[1]))
                .map(([url, title]) => `<option value="${url.replace(/"/g, '&quot;')}">${title.replace(/</g, '&lt;')}</option>`)
                .join('');
              const form = document.createElement('div');
              form.className = 'hl-cmp-feeds-form';
              const selA = document.createElement('select');
              selA.className = 'hl-cmp-feeds-sel';
              selA.innerHTML = `<option value="">Feed A…</option>${feedOptions}`;
              const selB = document.createElement('select');
              selB.className = 'hl-cmp-feeds-sel';
              selB.innerHTML = `<option value="">Feed B…</option>${feedOptions}`;
              const runBtn = document.createElement('button');
              runBtn.type = 'button';
              runBtn.className = 'hl-dryrun-compare-btn';
              runBtn.textContent = 'Compare';
              runBtn.addEventListener('click', async () => {
                const urlA = selA.value, urlB = selB.value;
                if (!urlA || !urlB) { window.alert('Select two feeds to compare.'); return; }
                if (urlA === urlB) { window.alert('Select two different feeds.'); return; }
                runBtn.disabled = true; runBtn.textContent = 'Comparing…';
                try {
                  const feedUrlsParam = [urlA, urlB].join(',');
                  const windowHours = rule ? (rule.dedup_window_hours || 168) : 168;
                  const results = await Promise.all(_hlCmpModes.map(m => {
                    const qs = new URLSearchParams({ type: 'deduplicate', scope: 'global', keyword: m, feed_urls: feedUrlsParam, dedup_window_hours: windowHours });
                    return fetch('/rules/dry-run?' + qs, { credentials: 'same-origin' }).then(r => r.json());
                  }));
                  form.replaceWith(hlBuildModeComparisonWrap(results, null));
                } catch { runBtn.disabled = false; runBtn.textContent = 'Compare'; window.alert('Compare failed.'); }
              });
              form.appendChild(selA); form.appendChild(selB); form.appendChild(runBtn);
              panel.insertBefore(form, groupsEl);
            });
            panel.appendChild(cmpFeedsBtn);
          }

          const groupsEl = document.createElement('div');
          groupsEl.className = 'hl-dryrun-groups-wrap';
          panel.appendChild(groupsEl);

          const renderPage = () => {
            groupsEl.innerHTML = '';
            hlRenderDedupGroups(groupsEl, groups, shownCount);
            if (groups.length > shownCount) {
              const moreBtn = document.createElement('button');
              moreBtn.type = 'button';
              moreBtn.className = 'hl-dryrun-more-btn';
              moreBtn.textContent = 'Show ' + Math.min(PAGE, groups.length - shownCount) + ' more (' + (groups.length - shownCount) + ' remaining)';
              moreBtn.addEventListener('click', () => { shownCount = Math.min(shownCount + PAGE, groups.length); renderPage(); });
              groupsEl.appendChild(moreBtn);
            }
          };
          renderPage();

        } else {
          const matches = data.matches || [];
          const summary = document.createElement('div');
          summary.className = 'hl-rule-dryrun-summary';
          if (matches.length === 0) {
            summary.textContent = 'No matches in last ' + (data.total_scanned || 0) + ' entries (read + unread)';
          } else {
            summary.textContent = data.total_matches + ' match' + (data.total_matches === 1 ? '' : 'es') + (data.truncated ? ' (showing first 20)' : '') + ' in last ' + (data.total_scanned || 0) + ' entries (read + unread)';
          }
          panel.appendChild(summary);
          for (const m of matches) {
            const item = document.createElement('div');
            item.className = 'hl-rule-dryrun-item' + (m.read ? ' hl-rule-dryrun-item--read' : '');
            const titleEl = document.createElement('a');
            titleEl.className = 'hl-rule-dryrun-item-title';
            titleEl.href = m.link || '#'; titleEl.target = '_blank'; titleEl.rel = 'noopener';
            titleEl.textContent = m.title || m.link || '(no title)';
            const feedEl = document.createElement('span');
            feedEl.className = 'hl-rule-dryrun-item-feed';
            feedEl.textContent = (m.read ? '✓ ' : '') + (m.feed_title || m.feed_url);
            item.appendChild(titleEl); item.appendChild(feedEl);
            if (m.published) {
              const dateEl = document.createElement('span');
              dateEl.className = 'hl-rule-dryrun-item-date';
              // published is an ISO-8601 string from the server. Show a short date in
              // the row, full date+time on hover; fall back to the raw value if it
              // doesn't parse (no "Invalid Date").
              const parsed = new Date(m.published);
              if (isNaN(parsed.getTime())) {
                dateEl.textContent = m.published;
              } else {
                dateEl.textContent = parsed.toLocaleDateString([], { month: 'short', day: 'numeric' });
                dateEl.title = parsed.toLocaleString([], { dateStyle: 'full', timeStyle: 'short' });
              }
              item.appendChild(dateEl);
            }
            panel.appendChild(item);
          }
        }
      }

      // Dynamic draft-row management
      let hlActiveDraft = null;
      const addRuleBtn = document.getElementById('hl-add-rule-btn');

      function hlHideDraft() {
        if (hlActiveDraft) { hlActiveDraft.remove(); hlActiveDraft = null; }
        addRuleBtn?.removeAttribute('hidden');
      }

      function hlInsertDraft(draftEl, afterEl) {
        hlHideDraft();
        hlActiveDraft = draftEl;
        addRuleBtn?.setAttribute('hidden', '');
        const listEl = document.getElementById('hl-rules-list');
        if (afterEl) afterEl.insertAdjacentElement('afterend', draftEl);
        else listEl?.appendChild(draftEl);
        draftEl.querySelector('.hl-draft-pattern')?.focus();
        window.setTimeout(() => draftEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 80);
      }

      function hlBuildDraft(prefill, saveLabel, onSave) {
        const draft = document.createElement('div');
        draft.className = 'hl-draft-row';

        // Type
        const typeSel = document.createElement('select');
        typeSel.className = 'hl-draft-select hl-type-select';
        for (const [val, label] of [['highlight','Highlight'],['mark_as_read','Mark as Read'],['tag_filter','Tag Filter'],['email_article','Email Article'],['webhook','Webhook'],['youtube_playlist','Add to YT Playlist'],['instapaper','Save to Instapaper'],['quire','Add to Quire'],['deduplicate','Deduplicate']]) {
          if (val === 'email_article' && !window.EMAIL_CONFIGURED) continue;
          // YouTube playlist rule needs a connected account; hide the option until
          // connected (unless editing an existing rule of this type).
          if (val === 'youtube_playlist' && !window.YT_OAUTH_CONNECTED && prefill.type !== 'youtube_playlist') continue;
          // Instapaper rule needs configured credentials.
          if (val === 'instapaper' && !window.INSTAPAPER_CONFIGURED && prefill.type !== 'instapaper') continue;
          // Quire rule needs a connected account + chosen destination project.
          if (val === 'quire' && !window.QUIRE_CONFIGURED && prefill.type !== 'quire') continue;
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = label;
          typeSel.appendChild(opt);
        }
        typeSel.value = prefill.type || 'highlight';
        draft.appendChild(typeSel);

        // Pattern
        const patInput = document.createElement('textarea');
        patInput.className = 'hl-draft-pattern';
        patInput.placeholder = 'keyword or pattern';
        patInput.maxLength = 1000;
        patInput.autocomplete = 'off';
        patInput.rows = 2;
        patInput.value = prefill.keyword || '';
        draft.appendChild(patInput);

        // Regex toggle
        const regexBtn = document.createElement('button');
        regexBtn.type = 'button';
        const regexOn = !!prefill.is_regex;
        regexBtn.className = 'hl-regex-toggle' + (regexOn ? ' active' : '');
        regexBtn.setAttribute('aria-pressed', String(regexOn));
        regexBtn.title = 'Enable regex matching';
        regexBtn.textContent = '.*';
        regexBtn.addEventListener('click', () => {
          const on = regexBtn.getAttribute('aria-pressed') !== 'true';
          regexBtn.setAttribute('aria-pressed', String(on));
          regexBtn.classList.toggle('active', on);
        });
        draft.appendChild(regexBtn);

        // Search in
        const searchInSel = document.createElement('select');
        searchInSel.className = 'hl-draft-select hl-search-in-select';
        for (const [val, label] of [['title','Title'],['body','Body'],['both','Title + Body']]) {
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = label;
          searchInSel.appendChild(opt);
        }
        searchInSel.value = prefill.search_in || 'title';
        draft.appendChild(searchInSel);

        // Scope: folder
        const folderSel = document.createElement('select');
        folderSel.className = 'hl-draft-select hl-folder-select';
        const allFolderOpt = document.createElement('option');
        allFolderOpt.value = '';
        allFolderOpt.textContent = 'All Feeds';
        folderSel.appendChild(allFolderOpt);
        for (const el of document.querySelectorAll('.tree-item[data-folder-id][data-folder-name]:not(.root-item):not([data-virtual])')) {
          const opt = document.createElement('option');
          opt.value = el.getAttribute('data-folder-id');
          opt.textContent = el.getAttribute('data-folder-name');
          folderSel.appendChild(opt);
        }
        if (prefill.scope === 'folder') {
          folderSel.value = prefill.scope_id || '';
        } else if (prefill.scope === 'feed' || prefill.scope === 'feeds') {
          // Prefer the explicit folder_id passed in the prefill (from context menu data
          // attributes), falling back to a DOM lookup so editing existing rules still works.
          // For a multi-feed rule, locate the folder of the first selected feed.
          const firstFeed = prefill.scope === 'feeds'
            ? String(prefill.scope_id || '').split('\n').map(s => s.trim()).filter(Boolean)[0]
            : prefill.scope_id;
          if (prefill.folder_id) {
            folderSel.value = String(prefill.folder_id);
          } else {
            for (const el of document.querySelectorAll('.feed-link[data-feed-url]')) {
              if (el.getAttribute('data-feed-url') === firstFeed) {
                folderSel.value = el.getAttribute('data-folder-id') || '';
                break;
              }
            }
          }
        }
        draft.appendChild(folderSel);

        // Scope: feed(s) — a searchable token picker (chips + find-as-you-type),
        // so it scales to thousands of feeds in one folder. No selection = whole
        // folder; exactly one = "feed" scope; two or more = "feeds". Selection lives
        // in selectedFeedUrls (decoupled from the DOM) so it survives type changes.
        const selectedFeedUrls = new Set(
          prefill.scope === 'feed' ? [prefill.scope_id]
          : prefill.scope === 'feeds' ? String(prefill.scope_id || '').split('\n').map(s => s.trim()).filter(Boolean)
          : []
        );
        let folderFeeds = [];                  // [{url,title}] for the current folder
        const feedTitleByUrl = new Map();
        function loadFolderFeeds(folderId) {
          folderFeeds = [];
          feedTitleByUrl.clear();
          if (!folderId) return;
          for (const el of document.querySelectorAll(`.feed-link[data-folder-id="${CSS.escape(folderId)}"]`)) {
            const url = el.getAttribute('data-feed-url');
            if (url && !feedTitleByUrl.has(url)) {
              const title = el.textContent.trim();
              folderFeeds.push({ url, title });
              feedTitleByUrl.set(url, title);
            }
          }
          folderFeeds.sort((a, b) => a.title.localeCompare(b.title));
        }
        function getSelectedFeeds() { return [...selectedFeedUrls]; }

        const feedPick = document.createElement('div');
        feedPick.className = 'hl-feed-pick';
        const feedChips = document.createElement('div');
        feedChips.className = 'hl-feed-chips';
        const feedSearchWrap = document.createElement('div');
        feedSearchWrap.className = 'hl-token-input-wrap hl-feed-search-wrap';
        const feedSearch = document.createElement('input');
        feedSearch.type = 'text';
        feedSearch.className = 'hl-token-input-field';
        feedSearch.placeholder = 'Type to add specific feeds…';
        feedSearch.setAttribute('autocomplete', 'off');
        const feedDrop = document.createElement('div');
        feedDrop.className = 'hl-token-input-dropdown';
        feedDrop.hidden = true;
        feedSearchWrap.appendChild(feedSearch);
        feedSearchWrap.appendChild(feedDrop);
        feedPick.appendChild(feedChips);
        feedPick.appendChild(feedSearchWrap);

        function renderFeedChips() {
          feedChips.innerHTML = '';
          if (!selectedFeedUrls.size) {
            const none = document.createElement('span');
            none.className = 'hl-feed-chips-empty';
            none.textContent = folderSel.value ? 'All feeds in folder' : 'All feeds';
            feedChips.appendChild(none);
            return;
          }
          for (const url of selectedFeedUrls) {
            const tag = document.createElement('span');
            tag.className = 'hl-folder-tag';
            tag.textContent = feedTitleByUrl.get(url) || url;
            const x = document.createElement('button');
            x.type = 'button'; x.className = 'hl-folder-tag-remove'; x.textContent = '×';
            x.setAttribute('aria-label', 'Remove');
            x.addEventListener('click', e => { e.stopPropagation(); selectedFeedUrls.delete(url); renderFeedChips(); renderFeedDrop(); });
            tag.appendChild(x);
            feedChips.appendChild(tag);
          }
        }
        function renderFeedDrop() {
          const q = feedSearch.value.trim().toLowerCase();
          feedDrop.innerHTML = '';
          if (!folderSel.value) { feedDrop.hidden = true; return; }
          const avail = folderFeeds.filter(f => !selectedFeedUrls.has(f.url) && (!q || f.title.toLowerCase().includes(q)));
          if (!avail.length) { feedDrop.hidden = true; return; }
          for (const f of avail.slice(0, 50)) {
            const opt = document.createElement('div');
            opt.className = 'hl-token-input-option';
            opt.textContent = f.title;
            opt.addEventListener('mousedown', e => {
              e.preventDefault();
              selectedFeedUrls.add(f.url);
              feedSearch.value = '';
              renderFeedChips(); renderFeedDrop(); feedSearch.focus();
            });
            feedDrop.appendChild(opt);
          }
          if (avail.length > 50) {
            const more = document.createElement('div');
            more.className = 'hl-token-input-option hl-token-input-more';
            more.textContent = `…and ${avail.length - 50} more — keep typing to narrow`;
            feedDrop.appendChild(more);
          }
          feedDrop.hidden = false;
        }
        feedSearch.addEventListener('focus', renderFeedDrop);
        feedSearch.addEventListener('input', renderFeedDrop);
        feedSearch.addEventListener('blur', () => { feedDrop.hidden = true; });

        loadFolderFeeds(folderSel.value);
        renderFeedChips();
        // Changing folder clears the (now out-of-scope) feed selection.
        folderSel.addEventListener('change', () => {
          selectedFeedUrls.clear();
          loadFolderFeeds(folderSel.value);
          renderFeedChips(); renderFeedDrop();
        });
        draft.appendChild(feedPick);

        // Match method (Deduplicate only)
        const matchMethodSel = document.createElement('select');
        matchMethodSel.className = 'hl-draft-select hl-match-method-select';
        for (const [val, label] of [['safe','Safe (recommended)'],['slug','URL Slug'],['title','Title'],['both','Slug + Title'],['fuzzy','Fuzzy Title']]) {
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = label;
          matchMethodSel.appendChild(opt);
        }
        matchMethodSel.value = prefill.keyword || 'slug';
        draft.appendChild(matchMethodSel);

        // Window hours (Deduplicate, title/both only)
        const windowHoursWrap = document.createElement('label');
        windowHoursWrap.className = 'hl-draft-field-label hl-draft-window-wrap';
        windowHoursWrap.textContent = 'within ';
        const windowHoursInput = document.createElement('input');
        windowHoursInput.type = 'number';
        windowHoursInput.className = 'hl-draft-batch-count hl-draft-window-hours';
        windowHoursInput.min = '1';
        windowHoursInput.max = '365';
        windowHoursInput.value = Math.round((prefill.dedup_window_hours || 168) / 24);
        windowHoursInput.title = 'Match articles published within this many days of each other';
        windowHoursWrap.appendChild(windowHoursInput);
        const windowHrsSuffix = document.createTextNode(' days');
        windowHoursWrap.appendChild(windowHrsSuffix);
        draft.appendChild(windowHoursWrap);

        // Exclude folders token-input sub-row (Deduplicate + global scope only)
        const excludeRow = document.createElement('div');
        excludeRow.className = 'hl-draft-form-subrow hl-draft-exclude-row';
        const excludeRowLabel = document.createElement('span');
        excludeRowLabel.className = 'hl-draft-field-label';
        excludeRowLabel.textContent = 'Exclude:';
        excludeRow.appendChild(excludeRowLabel);

        const allFoldersList = [];
        for (const el of document.querySelectorAll('.tree-item[data-folder-id][data-folder-name]:not(.root-item):not([data-virtual])')) {
          allFoldersList.push({ id: el.getAttribute('data-folder-id'), name: el.getAttribute('data-folder-name') });
        }
        const selectedExcludeIds = new Set((prefill.exclude_scope_ids || '').split(',').map(s => s.trim()).filter(Boolean));
        function getExcludeIds() { return [...selectedExcludeIds].join(','); }

        const tokenWrap = document.createElement('div');
        tokenWrap.className = 'hl-token-input-wrap';

        const tokenSearch = document.createElement('input');
        tokenSearch.type = 'text';
        tokenSearch.className = 'hl-token-input-field';
        tokenSearch.placeholder = 'Add folder…';
        tokenSearch.setAttribute('autocomplete', 'off');

        const tokenDropdown = document.createElement('div');
        tokenDropdown.className = 'hl-token-input-dropdown';
        tokenDropdown.hidden = true;

        function renderExcludeDropdown() {
          const q = tokenSearch.value.toLowerCase();
          tokenDropdown.innerHTML = '';
          const available = allFoldersList.filter(f => !selectedExcludeIds.has(f.id) && f.name.toLowerCase().includes(q));
          if (available.length === 0) { tokenDropdown.hidden = true; return; }
          for (const folder of available) {
            const opt = document.createElement('div');
            opt.className = 'hl-token-input-option';
            opt.textContent = folder.name;
            opt.addEventListener('mousedown', e => {
              e.preventDefault();
              selectedExcludeIds.add(folder.id);
              tokenSearch.value = '';
              renderExcludeTags();
              renderExcludeDropdown();
              tokenSearch.focus();
            });
            tokenDropdown.appendChild(opt);
          }
          tokenDropdown.hidden = false;
        }

        function renderExcludeTags() {
          tokenWrap.querySelectorAll('.hl-folder-tag').forEach(t => t.remove());
          for (const fid of selectedExcludeIds) {
            const folder = allFoldersList.find(f => f.id === fid);
            const tag = document.createElement('span');
            tag.className = 'hl-folder-tag';
            tag.textContent = (folder ? folder.name : '#' + fid);
            const x = document.createElement('button');
            x.type = 'button';
            x.className = 'hl-folder-tag-remove';
            x.textContent = '×';
            x.setAttribute('aria-label', 'Remove');
            x.addEventListener('click', e => {
              e.stopPropagation();
              selectedExcludeIds.delete(fid);
              renderExcludeTags();
              renderExcludeDropdown();
            });
            tag.appendChild(x);
            tokenWrap.insertBefore(tag, tokenSearch);
          }
        }

        tokenSearch.addEventListener('focus', renderExcludeDropdown);
        tokenSearch.addEventListener('input', renderExcludeDropdown);
        tokenSearch.addEventListener('blur', () => { tokenDropdown.hidden = true; tokenSearch.value = ''; });
        tokenWrap.addEventListener('click', () => tokenSearch.focus());

        tokenWrap.appendChild(tokenSearch);
        tokenWrap.appendChild(tokenDropdown);
        renderExcludeTags();
        excludeRow.appendChild(tokenWrap);
        draft.appendChild(excludeRow);

        // Color (Highlight only)
        const colorSel = document.createElement('select');
        colorSel.className = 'hl-draft-select hl-color-select';
        for (const [val, label] of [['yellow','Yellow'],['green','Green'],['blue','Blue'],['pink','Pink'],['orange','Orange']]) {
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = label;
          colorSel.appendChild(opt);
        }
        colorSel.value = prefill.color || 'yellow';
        draft.appendChild(colorSel);

        // Delivery (Email Article only)
        const deliverySel = document.createElement('select');
        deliverySel.className = 'hl-draft-select hl-delivery-select';
        for (const [val, label] of [['immediately','Send immediately'],['batch','Batch digest']]) {
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = label;
          deliverySel.appendChild(opt);
        }
        deliverySel.value = prefill.delivery || 'immediately';
        draft.appendChild(deliverySel);

        // Save + Cancel (in main row)
        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.className = 'hl-draft-save';
        saveBtn.textContent = saveLabel;
        draft.appendChild(saveBtn);

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'hl-draft-cancel';
        cancelBtn.setAttribute('aria-label', 'Cancel');
        cancelBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">close</span>';
        draft.appendChild(cancelBtn);

        // ── Email row: To address ─────────────────────────────────────
        // ── To: label + contact select (in main row, Email Article only) ─
        const toLabel = document.createElement('span');
        toLabel.className = 'hl-draft-field-label';
        toLabel.textContent = 'To:';
        draft.appendChild(toLabel);

        const contactSel = document.createElement('select');
        contactSel.className = 'hl-draft-select hl-contact-select';

        function populateContacts() {
          while (contactSel.options.length) contactSel.remove(0);
          const placeholder = document.createElement('option');
          placeholder.value = '';
          placeholder.textContent = '— select address —';
          contactSel.appendChild(placeholder);
          if (window.PROFILE_EMAIL) {
            const meOpt = document.createElement('option');
            meOpt.value = window.PROFILE_EMAIL;
            meOpt.textContent = 'Me (' + window.PROFILE_EMAIL + ')';
            contactSel.appendChild(meOpt);
          }
          for (const c of (window.EMAIL_CONTACTS || [])) {
            // Skip contacts that duplicate the profile email (already shown as "Me")
            if (window.PROFILE_EMAIL && c.address.toLowerCase() === window.PROFILE_EMAIL.toLowerCase()) continue;
            const opt = document.createElement('option');
            opt.value = c.address;
            opt.textContent = c.label + ' (' + c.address + ')';
            contactSel.appendChild(opt);
          }
          const addOpt = document.createElement('option');
          addOpt.value = '__add__';
          addOpt.textContent = '＋ Add address…';
          contactSel.appendChild(addOpt);
          contactSel.value = prefill.email_to || '';
        }
        populateContacts();
        draft.appendChild(contactSel);

        // ── Cc me checkbox (in main row, Email Article + profile email) ─
        const ccMeWrap = document.createElement('label');
        ccMeWrap.className = 'hl-draft-cc-wrap';
        const ccMeCheck = document.createElement('input');
        ccMeCheck.type = 'checkbox';
        ccMeCheck.checked = !!prefill.cc_me;
        ccMeWrap.appendChild(ccMeCheck);
        const ccMeText = document.createTextNode(' Cc me');
        ccMeWrap.appendChild(ccMeText);
        draft.appendChild(ccMeWrap);

        // ── Add-contact form sub-row (pops out below when __add__ selected)
        const contactFormRow = document.createElement('div');
        contactFormRow.className = 'hl-draft-form-subrow';
        contactFormRow.style.display = 'none';

        const cfLabelInput = document.createElement('input');
        cfLabelInput.type = 'text';
        cfLabelInput.className = 'hl-draft-pattern';
        cfLabelInput.placeholder = 'Label (e.g. Kindle)';
        cfLabelInput.maxLength = 80;
        contactFormRow.appendChild(cfLabelInput);

        const cfAddrInput = document.createElement('input');
        cfAddrInput.type = 'email';
        cfAddrInput.className = 'hl-draft-pattern';
        cfAddrInput.placeholder = 'email@example.com';
        cfAddrInput.maxLength = 200;
        contactFormRow.appendChild(cfAddrInput);

        const cfSaveBtn = document.createElement('button');
        cfSaveBtn.type = 'button';
        cfSaveBtn.className = 'hl-draft-save';
        cfSaveBtn.textContent = 'Add';
        contactFormRow.appendChild(cfSaveBtn);

        const cfCancelBtn = document.createElement('button');
        cfCancelBtn.type = 'button';
        cfCancelBtn.className = 'hl-draft-cancel';
        cfCancelBtn.setAttribute('aria-label', 'Cancel');
        cfCancelBtn.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">close</span>';
        contactFormRow.appendChild(cfCancelBtn);

        draft.appendChild(contactFormRow);

        contactSel.addEventListener('change', () => {
          if (contactSel.value === '__add__') {
            contactSel.value = '';
            contactFormRow.style.display = '';
            cfLabelInput.focus();
          }
        });
        cfCancelBtn.addEventListener('click', () => {
          contactFormRow.style.display = 'none';
          cfLabelInput.value = '';
          cfAddrInput.value = '';
        });
        cfSaveBtn.addEventListener('click', async () => {
          const lbl = cfLabelInput.value.trim();
          const addr = cfAddrInput.value.trim();
          if (!lbl || !addr) { (lbl ? cfAddrInput : cfLabelInput).focus(); return; }
          try {
            const body = new URLSearchParams({ label: lbl, address: addr });
            const resp = await fetch('/email-contacts/add', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
            if (!resp.ok) throw new Error('failed');
            const data = await resp.json();
            if (!window.EMAIL_CONTACTS) window.EMAIL_CONTACTS = [];
            window.EMAIL_CONTACTS.push(data.contact);
            window.EMAIL_CONTACTS.sort((a, b) => a.label.localeCompare(b.label));
            populateContacts();
            contactSel.value = data.contact.address;
            contactFormRow.style.display = 'none';
            cfLabelInput.value = '';
            cfAddrInput.value = '';
          } catch { window.alert('Failed to add contact'); }
        });

        // ── Batch sub-row: time + count ───────────────────────────────
        const batchRow = document.createElement('div');
        batchRow.className = 'hl-draft-form-subrow';

        const btAtLabel = document.createElement('span');
        btAtLabel.className = 'hl-draft-field-label';
        btAtLabel.textContent = 'At:';
        batchRow.appendChild(btAtLabel);

        const batchTimeInput = document.createElement('input');
        batchTimeInput.type = 'time';
        batchTimeInput.className = 'hl-draft-batch-time';
        batchTimeInput.value = prefill.batch_time || '';
        batchTimeInput.title = 'Send digest at this time each day (leave blank to disable)';
        batchRow.appendChild(batchTimeInput);

        const btOrLabel = document.createElement('span');
        btOrLabel.className = 'hl-draft-field-label';
        btOrLabel.textContent = 'or after';
        batchRow.appendChild(btOrLabel);

        const batchCountInput = document.createElement('input');
        batchCountInput.type = 'number';
        batchCountInput.className = 'hl-draft-batch-count';
        batchCountInput.min = '1';
        batchCountInput.max = '500';
        batchCountInput.placeholder = 'N';
        batchCountInput.value = prefill.batch_count && prefill.batch_count > 0 ? prefill.batch_count : '';
        batchCountInput.title = 'Send when N articles have accumulated (leave blank to disable)';
        batchRow.appendChild(batchCountInput);

        const btArticlesLabel = document.createElement('span');
        btArticlesLabel.className = 'hl-draft-field-label';
        btArticlesLabel.textContent = 'articles';
        batchRow.appendChild(btArticlesLabel);

        draft.appendChild(batchRow);

        // ── Webhook sub-row: URL + format (Webhook only) ───────────────
        const webhookRow = document.createElement('div');
        webhookRow.className = 'hl-draft-form-subrow hl-draft-webhook-row';

        const whUrlLabel = document.createElement('span');
        whUrlLabel.className = 'hl-draft-field-label';
        whUrlLabel.textContent = 'POST to:';
        webhookRow.appendChild(whUrlLabel);

        const webhookUrlInput = document.createElement('input');
        webhookUrlInput.type = 'url';
        webhookUrlInput.className = 'hl-draft-pattern hl-draft-webhook-url';
        webhookUrlInput.placeholder = 'https://maker.ifttt.com/trigger/…  or  https://hooks.zapier.com/…';
        webhookUrlInput.maxLength = 2000;
        webhookUrlInput.autocomplete = 'off';
        webhookUrlInput.value = prefill.webhook_url || '';
        webhookRow.appendChild(webhookUrlInput);

        const whFmtLabel = document.createElement('span');
        whFmtLabel.className = 'hl-draft-field-label';
        whFmtLabel.textContent = 'as';
        webhookRow.appendChild(whFmtLabel);

        const webhookFormatSel = document.createElement('select');
        webhookFormatSel.className = 'hl-draft-select hl-webhook-format-select';
        for (const [val, label] of [['generic','Generic JSON'],['ifttt','IFTTT (value1/2/3)']]) {
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = label;
          webhookFormatSel.appendChild(opt);
        }
        webhookFormatSel.value = prefill.webhook_format || 'generic';
        webhookRow.appendChild(webhookFormatSel);

        const whBatchLabel = document.createElement('label');
        whBatchLabel.className = 'hl-draft-field-label';
        whBatchLabel.style.display = 'flex';
        whBatchLabel.style.alignItems = 'center';
        whBatchLabel.style.gap = '0.25rem';
        const webhookBatchCheck = document.createElement('input');
        webhookBatchCheck.type = 'checkbox';
        webhookBatchCheck.checked = !!prefill.webhook_batch;
        whBatchLabel.appendChild(webhookBatchCheck);
        whBatchLabel.appendChild(document.createTextNode('Batch'));
        whBatchLabel.title = 'Send all matches in one request instead of one request per entry. Not available for IFTTT format.';
        webhookRow.appendChild(whBatchLabel);
        const toggleBatch = () => { whBatchLabel.style.display = webhookFormatSel.value === 'ifttt' ? 'none' : 'flex'; };
        webhookFormatSel.addEventListener('change', toggleBatch);
        toggleBatch();

        const webhookTestBtn = document.createElement('button');
        webhookTestBtn.type = 'button';
        webhookTestBtn.className = 'feed-prop-inline-btn';
        webhookTestBtn.textContent = 'Send test';
        webhookTestBtn.title = 'POST a sample payload to this URL now';
        const webhookTestStatus = document.createElement('span');
        webhookTestStatus.className = 'feed-prop-strategy-hint';
        webhookTestBtn.addEventListener('click', async () => {
          const url = webhookUrlInput.value.trim();
          if (!url) { webhookUrlInput.focus(); return; }
          webhookTestBtn.disabled = true;
          webhookTestStatus.textContent = 'Sending…';
          try {
            const body = new URLSearchParams({ webhook_url: url, webhook_format: webhookFormatSel.value || 'generic' });
            const resp = await fetch('/rules/webhook-test', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
            const json = await resp.json();
            webhookTestStatus.textContent = json.ok ? '✓ Sent' : `Error: ${json.error || 'failed'}`;
          } catch (err) {
            webhookTestStatus.textContent = `Error: ${err.message}`;
          } finally {
            webhookTestBtn.disabled = false;
            setTimeout(() => { webhookTestStatus.textContent = ''; }, 5000);
          }
        });
        webhookRow.appendChild(webhookTestBtn);
        webhookRow.appendChild(webhookTestStatus);

        draft.appendChild(webhookRow);

        // ── YouTube playlist sub-row: playlist + options (youtube_playlist only) ─
        const ytRow = document.createElement('div');
        ytRow.className = 'hl-draft-form-subrow hl-draft-yt-row';

        const ytPlLabel = document.createElement('span');
        ytPlLabel.className = 'hl-draft-field-label';
        ytPlLabel.textContent = 'Playlist:';
        ytRow.appendChild(ytPlLabel);

        const ytPlSel = document.createElement('select');
        ytPlSel.className = 'hl-draft-select hl-yt-playlist-select';
        const ytPlLoading = document.createElement('option');
        ytPlLoading.value = '';
        ytPlLoading.textContent = 'Loading…';
        ytPlSel.appendChild(ytPlLoading);
        ytRow.appendChild(ytPlSel);

        // Populate playlists from the shared endpoint (cache on window across drafts).
        (async () => {
          try {
            if (!window._ytPlaylistsForRules) {
              const r = await fetch('/api/youtube/playlists', { credentials: 'same-origin' });
              window._ytPlaylistsForRules = (await r.json().catch(() => ({}))).playlists || [];
            }
            ytPlSel.innerHTML = '';
            const ph = document.createElement('option');
            ph.value = ''; ph.textContent = '— select playlist —';
            ytPlSel.appendChild(ph);
            for (const pl of window._ytPlaylistsForRules) {
              const opt = document.createElement('option');
              opt.value = pl.id;
              opt.textContent = pl.title + (pl.count != null ? ` (${pl.count})` : '');
              opt.dataset.title = pl.title;
              ytPlSel.appendChild(opt);
            }
            // If the prefilled playlist is no longer in the list (renamed/deleted),
            // keep a stub so editing doesn't silently drop it.
            if (prefill.yt_playlist_id && !window._ytPlaylistsForRules.some(p => p.id === prefill.yt_playlist_id)) {
              const opt = document.createElement('option');
              opt.value = prefill.yt_playlist_id;
              opt.textContent = (prefill.yt_playlist_title || prefill.yt_playlist_id) + ' (unavailable)';
              opt.dataset.title = prefill.yt_playlist_title || '';
              ytPlSel.appendChild(opt);
            }
            ytPlSel.value = prefill.yt_playlist_id || '';
          } catch {
            ytPlSel.innerHTML = '<option value="">(failed to load playlists)</option>';
          }
        })();

        const ytShortsWrap = document.createElement('label');
        ytShortsWrap.className = 'hl-draft-cc-wrap';
        const ytShortsCheck = document.createElement('input');
        ytShortsCheck.type = 'checkbox';
        ytShortsCheck.checked = !!prefill.yt_include_shorts;
        ytShortsWrap.appendChild(ytShortsCheck);
        ytShortsWrap.appendChild(document.createTextNode(' Include Shorts'));
        ytRow.appendChild(ytShortsWrap);

        const ytMarkWrap = document.createElement('label');
        ytMarkWrap.className = 'hl-draft-cc-wrap';
        const ytMarkCheck = document.createElement('input');
        ytMarkCheck.type = 'checkbox';
        ytMarkCheck.checked = prefill.yt_mark_read !== undefined ? !!prefill.yt_mark_read : true;
        ytMarkWrap.appendChild(ytMarkCheck);
        ytMarkWrap.appendChild(document.createTextNode(' Mark read after add'));
        ytRow.appendChild(ytMarkWrap);

        // Duration filter (minutes; blank/0 = no limit). Uses the cached video
        // length, so it needs the YouTube API key that also powers durations.
        const ytDurWrap = document.createElement('span');
        ytDurWrap.className = 'hl-draft-field-label';
        ytDurWrap.appendChild(document.createTextNode('Length'));
        const ytMinInput = document.createElement('input');
        ytMinInput.type = 'number'; ytMinInput.min = '0'; ytMinInput.placeholder = 'min';
        ytMinInput.className = 'hl-draft-batch-count';
        ytMinInput.title = 'Only add videos at least this many minutes long (blank = no minimum)';
        ytMinInput.value = prefill.yt_min_minutes ? prefill.yt_min_minutes : '';
        const ytMaxInput = document.createElement('input');
        ytMaxInput.type = 'number'; ytMaxInput.min = '0'; ytMaxInput.placeholder = 'max';
        ytMaxInput.className = 'hl-draft-batch-count';
        ytMaxInput.title = 'Only add videos at most this many minutes long (blank = no maximum)';
        ytMaxInput.value = prefill.yt_max_minutes ? prefill.yt_max_minutes : '';
        ytDurWrap.appendChild(document.createTextNode(' '));
        ytDurWrap.appendChild(ytMinInput);
        ytDurWrap.appendChild(document.createTextNode('–'));
        ytDurWrap.appendChild(ytMaxInput);
        ytDurWrap.appendChild(document.createTextNode(' min'));
        ytRow.appendChild(ytDurWrap);

        draft.appendChild(ytRow);

        // ── Sync visibility ───────────────────────────────────────────
        function syncTypeControls() {
          const t = typeSel.value;
          const isEmail = t === 'email_article';
          const isDedup = t === 'deduplicate';
          const isWebhook = t === 'webhook';
          const isYtPlaylist = t === 'youtube_playlist';
          const isTagFilter = t === 'tag_filter';
          const isBatch = isEmail && deliverySel.value === 'batch';
          const dedupNeedsWindow = isDedup && !['slug', 'safe'].includes(matchMethodSel.value);
          const isGlobalDedup = isDedup && !folderSel.value;
          patInput.style.display = isDedup ? 'none' : '';
          regexBtn.style.display = (isDedup || isTagFilter) ? 'none' : '';
          searchInSel.style.display = (isDedup || isTagFilter) ? 'none' : '';
          colorSel.style.display = t === 'highlight' ? '' : 'none';
          deliverySel.style.display = isEmail ? '' : 'none';
          toLabel.style.display = isEmail ? '' : 'none';
          contactSel.style.display = isEmail ? '' : 'none';
          ccMeWrap.style.display = (isEmail && window.PROFILE_EMAIL) ? '' : 'none';
          batchRow.style.display = isBatch ? '' : 'none';
          if (!isEmail) contactFormRow.style.display = 'none';
          matchMethodSel.style.display = isDedup ? '' : 'none';
          windowHoursWrap.style.display = dedupNeedsWindow ? '' : 'none';
          excludeRow.style.display = isGlobalDedup ? '' : 'none';
          webhookRow.style.display = isWebhook ? '' : 'none';
          ytRow.style.display = isYtPlaylist ? '' : 'none';
          // For the playlist rule the keyword is an OPTIONAL filter (empty = all
          // new videos in scope); reflect that in the placeholder.
          patInput.placeholder = isYtPlaylist ? 'optional keyword filter (blank = all videos)'
            : (t === 'instapaper' ? 'optional keyword filter (blank = save all)'
            : (isTagFilter ? 'comma-separated feed tags: -drops, +good (rescues from drops), ++required' : 'keyword or pattern'));
          // The feed picker shows for every type, including deduplicate — dedup can
          // run across an explicit set of selected feeds (>=2). Selection is decoupled
          // (selectedFeedUrls), so a type change keeps the chosen feed(s) automatically.
        }
        syncTypeControls();
        typeSel.addEventListener('change', syncTypeControls);
        deliverySel.addEventListener('change', syncTypeControls);
        matchMethodSel.addEventListener('change', syncTypeControls);
        folderSel.addEventListener('change', syncTypeControls);

        cancelBtn.addEventListener('click', hlHideDraft);
        patInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); saveBtn.click(); } });

        saveBtn.addEventListener('click', async () => {
          const ruleType = typeSel.value || 'highlight';
          const folderId = folderSel.value || '';

          if (ruleType === 'deduplicate') {
            const matchMethod = matchMethodSel.value || 'slug';
            const windowHours = (parseInt(windowHoursInput.value || '7', 10) || 7) * 24;
            const dedupFeeds = getSelectedFeeds();
            // 2+ selected feeds → dedupe across just those; else folder/global.
            // A single selected feed can't cross-dedupe — guide the user.
            if (dedupFeeds.length === 1) { window.alert('Select at least two feeds to deduplicate between them (or none for the whole folder).'); return; }
            let scope, scopeId;
            if (dedupFeeds.length >= 2) { scope = 'feeds'; scopeId = dedupFeeds.join('\n'); }
            else if (folderId)         { scope = 'folder'; scopeId = folderId; }
            else                       { scope = 'global'; scopeId = ''; }
            const excludeIds = getExcludeIds();
            await onSave({ scope, scope_id: scopeId, keyword: matchMethod, color: 'yellow',
                           is_regex: 0, type: 'deduplicate', search_in: 'title',
                           delivery: 'immediately', email_to: '', batch_time: '', batch_count: 0,
                           cc_me: 0, dedup_window_hours: windowHours, exclude_scope_ids: excludeIds });
            return;
          }

          const pattern = patInput.value.trim();
          // youtube_playlist, instapaper, and quire allow an empty keyword (all in scope).
          if (!pattern && ruleType !== 'youtube_playlist' && ruleType !== 'instapaper' && ruleType !== 'quire') { patInput.focus(); return; }
          if (ruleType === 'email_article' && !contactSel.value) { contactSel.focus(); return; }
          const webhookUrl = webhookUrlInput.value.trim();
          if (ruleType === 'webhook' && !webhookUrl) { webhookUrlInput.focus(); return; }
          if (ruleType === 'youtube_playlist' && !ytPlSel.value) { ytPlSel.focus(); return; }
          const isRegex = regexBtn.getAttribute('aria-pressed') === 'true' ? 1 : 0;
          const selectedFeeds = getSelectedFeeds();
          const color    = colorSel.value || 'yellow';
          const searchIn = searchInSel.value || 'title';
          const delivery = deliverySel.value || 'immediately';
          const emailTo  = contactSel.value || '';
          const batchTime  = batchTimeInput.value || '';
          const batchCount = parseInt(batchCountInput.value || '0', 10) || 0;
          const ccMe = ccMeCheck.checked ? 1 : 0;
          const webhookFormat = webhookFormatSel.value || 'generic';
          const webhookBatch = (webhookFormat !== 'ifttt' && webhookBatchCheck.checked) ? 1 : 0;
          let scope, scopeId;
          if (!folderId)                  { scope = 'global'; scopeId = ''; }
          else if (selectedFeeds.length === 0) { scope = 'folder'; scopeId = folderId; }
          else if (selectedFeeds.length === 1) { scope = 'feed';   scopeId = selectedFeeds[0]; }
          else                            { scope = 'feeds';  scopeId = selectedFeeds.join('\n'); }
          const ytPlOpt = ytPlSel.options[ytPlSel.selectedIndex];
          await onSave({ scope, scope_id: scopeId, keyword: pattern, color, is_regex: isRegex,
                         type: ruleType, search_in: searchIn, delivery,
                         email_to: emailTo, batch_time: batchTime, batch_count: batchCount, cc_me: ccMe,
                         dedup_window_hours: 24, webhook_url: webhookUrl, webhook_format: webhookFormat, webhook_batch: webhookBatch,
                         yt_playlist_id: ytPlSel.value || '',
                         yt_playlist_title: (ytPlOpt && ytPlOpt.dataset.title) || '',
                         yt_include_shorts: ytShortsCheck.checked ? 1 : 0,
                         yt_mark_read: ytMarkCheck.checked ? 1 : 0,
                         yt_min_minutes: parseInt(ytMinInput.value || '0', 10) || 0,
                         yt_max_minutes: parseInt(ytMaxInput.value || '0', 10) || 0 });
        });

        return draft;
      }

      function hlRuleToParams(r) {
        return new URLSearchParams({
          scope: r.scope, scope_id: r.scope_id, keyword: r.keyword,
          color: r.color || 'yellow', is_regex: r.is_regex || 0,
          type: r.type || 'highlight', search_in: r.search_in || 'title',
          delivery: r.delivery || 'immediately',
          email_to: r.email_to || '', batch_time: r.batch_time || '',
          batch_count: r.batch_count || 0, cc_me: r.cc_me || 0,
          enabled: r.enabled !== undefined ? r.enabled : 0,
          dedup_window_hours: r.dedup_window_hours || 168,
          exclude_scope_ids: r.exclude_scope_ids || '',
          webhook_url: r.webhook_url || '', webhook_format: r.webhook_format || 'generic', webhook_batch: r.webhook_batch ? 1 : 0,
          yt_playlist_id: r.yt_playlist_id || '', yt_playlist_title: r.yt_playlist_title || '',
          yt_include_shorts: r.yt_include_shorts ? 1 : 0,
          yt_mark_read: (r.yt_mark_read !== undefined ? r.yt_mark_read : 1) ? 1 : 0,
          yt_min_minutes: r.yt_min_minutes || 0, yt_max_minutes: r.yt_max_minutes || 0,
        });
      }

      function hlMakeAddDraft(prefill, afterEl = null) {
        const draft = hlBuildDraft(prefill || {}, 'Add', async (newRule) => {
          try {
            const params = hlRuleToParams({ ...newRule, enabled: 0 });
            const resp = await fetch('/highlights/add', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: params.toString() });
            if (!resp.ok) throw new Error('failed');
            const idx = hlRules.findIndex(r => r.scope === newRule.scope && r.scope_id === newRule.scope_id && r.keyword === newRule.keyword);
            if (idx < 0) hlRules.push({ ...newRule, enabled: 0 });
            else Object.assign(hlRules[idx], { ...newRule, enabled: 0 });
            hlHideDraft();
            hlRenderRules();
            applyHighlights();
          } catch { window.alert('Failed to add rule'); }
        });
        hlInsertDraft(draft, afterEl);
      }

      addRuleBtn?.addEventListener('click', () => hlMakeAddDraft({}));

      window._hlLoadFalseMatches = hlLoadFalseMatches;
      window._hlRenderRules = hlRenderRules;
      window._hlHideDraft = hlHideDraft;
      window._hlMakeAddDraft = hlMakeAddDraft;

      function openHighlightsModal(prefill) {
        window._hlHideDraft?.();
        window.openSettingsModal?.('automation');
        if (prefill) window._hlMakeAddDraft?.(prefill);
      }
      window.openHighlightsModal = openHighlightsModal;

      highlightsButton?.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        hideAllContextMenus();
        let prefill = null;
        if (contextFeedUrl) prefill = { scope: 'feed', scope_id: contextFeedUrl, folder_id: contextFolderId };
        else if (contextFolderId) prefill = { scope: 'folder', scope_id: contextFolderId };
        openHighlightsModal(prefill);
      });
    }

    // ── Profile modal ────────────────────────────────────────────────────────
    {
      // ── Settings modal ────────────────────────────────────────────────────
      const settingsModal = document.getElementById('settings-modal');
      let settingsData = null;
      let settContacts = [];

      function openSettingsModal(tab) {
        if (!settingsModal) return;
        settingsModal.removeAttribute('hidden');
        settSwitchTab(tab || 'account');
        if (tab !== 'automation' && tab !== 'stats') loadSettingsData();
      }
      window.openSettingsModal = openSettingsModal;

      // Lazy Settings→Feeds panels: the folders table and stale list are the
      // heaviest fragments in the app (a row per feed — megabytes at thousands
      // of feeds), fetched on first open instead of shipping with every page.
      // Row interactions are delegated, so injected content needs no rebinding.
      window._loadLazySettingsPanel = async function (container) {
        if (!container || container.dataset.loaded) return;
        container.dataset.loaded = '1';
        try {
          const resp = await fetch(container.dataset.lazySrc, { credentials: 'same-origin' });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          container.innerHTML = await resp.text();
        } catch (err) {
          delete container.dataset.loaded;
          container.innerHTML = '<p class="muted" style="padding:0.5rem 0">Failed to load — reopen this tab to retry.</p>';
          console.error('lazy settings panel load failed:', err);
        }
      };

      function settSwitchTab(tabName) {
        document.querySelectorAll('[data-settings-tab]').forEach(btn => {
          const active = btn.getAttribute('data-settings-tab') === tabName;
          btn.classList.toggle('hl-tab-btn--active', active);
          btn.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        document.querySelectorAll('.settings-tab-panel').forEach(panel => {
          const isActive = panel.id === `settings-tab-${tabName}`;
          panel.hidden = !isActive;
          panel.style.display = isActive ? 'flex' : 'none';
        });
        if (tabName === 'feeds') void window._loadLazySettingsPanel(document.getElementById('settings-folders-lazy'));
        if (tabName === 'stats') void loadStatsData();
        if (tabName === 'automation') {
          window._hlLoadFalseMatches?.();
          window._hlRenderRules?.();
        }
      }

      document.querySelectorAll('[data-settings-tab]').forEach(btn => {
        btn.addEventListener('click', () => {
          const tab = btn.getAttribute('data-settings-tab');
          settSwitchTab(tab);
          if (tab === 'feeds') void markProblematicFeedsViewed();
        });
      });
      // Force hidden panels to display:none regardless of stylesheet specificity
      document.querySelectorAll('.settings-tab-panel[hidden]').forEach(p => { p.style.display = 'none'; });

      // Populate timezone datalist from IANA list
      (function() {
        const dl = document.getElementById('sett-tz-list');
        if (!dl) return;
        const zones = (typeof Intl?.supportedValuesOf === 'function')
          ? Intl.supportedValuesOf('timeZone')
          : [];
        const frag = document.createDocumentFragment();
        zones.forEach(z => { const o = document.createElement('option'); o.value = z; frag.appendChild(o); });
        dl.appendChild(frag);
      })();

      // Render the DeviantArt watch-list sync detail (failed / no-longer-watched
      // artists) as links to their DeviantArt profiles, so the user never has to
      // dig through server logs to see which adds failed.
      function renderDaSyncDetail(detail, connected, deactivated) {
        const wrap = document.getElementById('sett-da-sync-detail');
        if (!wrap) return;
        const failed = (detail && detail.failed) || [];
        const unwatched = (detail && detail.unwatched) || [];
        deactivated = deactivated || [];
        const fill = (listId, wrapId, items, withError) => {
          const ul = document.getElementById(listId);
          const box = document.getElementById(wrapId);
          if (!ul || !box) return;
          ul.textContent = '';
          items.forEach(it => {
            const user = (it && it.username) || '';
            if (!user) return;
            const li = document.createElement('li');
            const a = document.createElement('a');
            a.href = 'https://www.deviantart.com/' + encodeURIComponent(user);
            a.target = '_blank';
            a.rel = 'noopener';
            a.textContent = user;
            li.appendChild(a);
            if (withError && it.error) {
              const span = document.createElement('span');
              span.className = 'settings-da-artist-error';
              span.textContent = ' — ' + it.error;
              li.appendChild(span);
            }
            ul.appendChild(li);
          });
          box.hidden = items.length === 0;
        };
        fill('sett-da-failed-list', 'sett-da-failed-wrap', failed, true);
        fill('sett-da-deactivated-list', 'sett-da-deactivated-wrap', deactivated, false);
        fill('sett-da-unwatched-list', 'sett-da-unwatched-wrap', unwatched, false);
        wrap.hidden = !connected || (failed.length === 0 && deactivated.length === 0 && unwatched.length === 0);
      }

      async function loadSettingsData() {
        try {
          const resp = await fetch('/settings/all', { credentials: 'same-origin' });
          settingsData = await resp.json();
          settPopulate(settingsData);
        } catch { /* silently fail on load */ }
      }

      // Minimal MD5 for Gravatar hashing
      function _md5(str) {
        function safeAdd(x, y) { const lsw = (x & 0xffff) + (y & 0xffff); return (((x >> 16) + (y >> 16) + (lsw >> 16)) << 16) | (lsw & 0xffff); }
        function bitRotateLeft(num, cnt) { return (num << cnt) | (num >>> (32 - cnt)); }
        function md5cmn(q, a, b, x, s, t) { return safeAdd(bitRotateLeft(safeAdd(safeAdd(a, q), safeAdd(x, t)), s), b); }
        function md5ff(a, b, c, d, x, s, t) { return md5cmn((b & c) | (~b & d), a, b, x, s, t); }
        function md5gg(a, b, c, d, x, s, t) { return md5cmn((b & d) | (c & ~d), a, b, x, s, t); }
        function md5hh(a, b, c, d, x, s, t) { return md5cmn(b ^ c ^ d, a, b, x, s, t); }
        function md5ii(a, b, c, d, x, s, t) { return md5cmn(c ^ (b | ~d), a, b, x, s, t); }
        function utf8Encode(s) { return unescape(encodeURIComponent(s)); }
        const utf = utf8Encode(str);
        const n = utf.length;
        const ws = [];
        for (let i = 0; i < n; i++) ws[i >> 2] |= utf.charCodeAt(i) << ((i % 4) * 8);
        ws[n >> 2] |= 0x80 << ((n % 4) * 8);
        ws[(((n + 8) >> 6) << 4) + 14] = n * 8;
        let a = 1732584193, b = -271733879, c = -1732584194, d = 271733878;
        for (let i = 0; i < ws.length; i += 16) {
          const [oa, ob, oc, od] = [a, b, c, d];
          a = md5ff(a,b,c,d,ws[i+0],7,-680876936); d = md5ff(d,a,b,c,ws[i+1],12,-389564586); c = md5ff(c,d,a,b,ws[i+2],17,606105819); b = md5ff(b,c,d,a,ws[i+3],22,-1044525330);
          a = md5ff(a,b,c,d,ws[i+4],7,-176418897); d = md5ff(d,a,b,c,ws[i+5],12,1200080426); c = md5ff(c,d,a,b,ws[i+6],17,-1473231341); b = md5ff(b,c,d,a,ws[i+7],22,-45705983);
          a = md5ff(a,b,c,d,ws[i+8],7,1770035416); d = md5ff(d,a,b,c,ws[i+9],12,-1958414417); c = md5ff(c,d,a,b,ws[i+10],17,-42063); b = md5ff(b,c,d,a,ws[i+11],22,-1990404162);
          a = md5ff(a,b,c,d,ws[i+12],7,1804603682); d = md5ff(d,a,b,c,ws[i+13],12,-40341101); c = md5ff(c,d,a,b,ws[i+14],17,-1502002290); b = md5ff(b,c,d,a,ws[i+15],22,1236535329);
          a = md5gg(a,b,c,d,ws[i+1],5,-165796510); d = md5gg(d,a,b,c,ws[i+6],9,-1069501632); c = md5gg(c,d,a,b,ws[i+11],14,643717713); b = md5gg(b,c,d,a,ws[i+0],20,-373897302);
          a = md5gg(a,b,c,d,ws[i+5],5,-701558691); d = md5gg(d,a,b,c,ws[i+10],9,38016083); c = md5gg(c,d,a,b,ws[i+15],14,-660478335); b = md5gg(b,c,d,a,ws[i+4],20,-405537848);
          a = md5gg(a,b,c,d,ws[i+9],5,568446438); d = md5gg(d,a,b,c,ws[i+14],9,-1019803690); c = md5gg(c,d,a,b,ws[i+3],14,-187363961); b = md5gg(b,c,d,a,ws[i+8],20,1163531501);
          a = md5gg(a,b,c,d,ws[i+13],5,-1444681467); d = md5gg(d,a,b,c,ws[i+2],9,-51403784); c = md5gg(c,d,a,b,ws[i+7],14,1735328473); b = md5gg(b,c,d,a,ws[i+12],20,-1926607734);
          a = md5hh(a,b,c,d,ws[i+5],4,-378558); d = md5hh(d,a,b,c,ws[i+8],11,-2022574463); c = md5hh(c,d,a,b,ws[i+11],16,1839030562); b = md5hh(b,c,d,a,ws[i+14],23,-35309556);
          a = md5hh(a,b,c,d,ws[i+1],4,-1530992060); d = md5hh(d,a,b,c,ws[i+4],11,1272893353); c = md5hh(c,d,a,b,ws[i+7],16,-155497632); b = md5hh(b,c,d,a,ws[i+10],23,-1094730640);
          a = md5hh(a,b,c,d,ws[i+13],4,681279174); d = md5hh(d,a,b,c,ws[i+0],11,-358537222); c = md5hh(c,d,a,b,ws[i+3],16,-722521979); b = md5hh(b,c,d,a,ws[i+6],23,76029189);
          a = md5hh(a,b,c,d,ws[i+9],4,-640364487); d = md5hh(d,a,b,c,ws[i+12],11,-421815835); c = md5hh(c,d,a,b,ws[i+15],16,530742520); b = md5hh(b,c,d,a,ws[i+2],23,-995338651);
          a = md5ii(a,b,c,d,ws[i+0],6,-198630844); d = md5ii(d,a,b,c,ws[i+7],10,1126891415); c = md5ii(c,d,a,b,ws[i+14],15,-1416354905); b = md5ii(b,c,d,a,ws[i+5],21,-57434055);
          a = md5ii(a,b,c,d,ws[i+12],6,1700485571); d = md5ii(d,a,b,c,ws[i+3],10,-1894986606); c = md5ii(c,d,a,b,ws[i+10],15,-1051523); b = md5ii(b,c,d,a,ws[i+1],21,-2054922799);
          a = md5ii(a,b,c,d,ws[i+8],6,1873313359); d = md5ii(d,a,b,c,ws[i+15],10,-30611744); c = md5ii(c,d,a,b,ws[i+6],15,-1560198380); b = md5ii(b,c,d,a,ws[i+13],21,1309151649);
          a = md5ii(a,b,c,d,ws[i+4],6,-145523070); d = md5ii(d,a,b,c,ws[i+11],10,-1120210379); c = md5ii(c,d,a,b,ws[i+2],15,718787259); b = md5ii(b,c,d,a,ws[i+9],21,-343485551);
          a = safeAdd(a, oa); b = safeAdd(b, ob); c = safeAdd(c, oc); d = safeAdd(d, od);
        }
        return [a, b, c, d].map(n => (n < 0 ? n + 0x100000000 : n).toString(16).padStart(8, '0').match(/../g).reverse().join('')).join('');
      }

      function updateAvatarFromEmail(email) {
        const hash = email ? _md5(email.trim().toLowerCase()) : '';
        const settImg = document.getElementById('sett-avatar-img');
        const menuImg = document.getElementById('menu-avatar-img');
        if (settImg) settImg.src = `https://www.gravatar.com/avatar/${hash}?d=identicon&s=128`;
        if (menuImg) menuImg.src = `https://www.gravatar.com/avatar/${hash}?d=identicon&s=64`;
      }

      function updateSettAvatar() {
        updateAvatarFromEmail(document.getElementById('sett-profile-email')?.value || '');
      }

      document.getElementById('sett-profile-email')?.addEventListener('input', updateSettAvatar);

      function settPopulate(d) {
        const v = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
        v('sett-profile-name', d.profile_name);
        v('sett-profile-email', d.profile_email);
        // Show the explicit tz if set; otherwise leave blank and show the
        // server default greyed (placeholder) so the user sees what's in effect.
        const tzEl = document.getElementById('sett-tz-display');
        if (tzEl) {
          tzEl.value = d.tz_display || '';
          tzEl.placeholder = d.tz_default ? `${d.tz_default} (server default)` : 'Server default';
        }
        v('sett-maint-hour', d.maintenance_hour);
        const maintLast = document.getElementById('sett-maint-last');
        if (maintLast) {
          if (d.maintenance_last_ran_at) {
            maintLast.textContent = `Last ran: ${d.maintenance_last_ran_at}`;
            maintLast.hidden = false;
          } else {
            maintLast.hidden = true;
          }
        }
        // Sensitive fields: show placeholder if set, blank if not
        const secretField = (id, isSet, masked) => {
          const el = document.getElementById(id);
          if (!el) return;
          el.value = '';
          el.placeholder = isSet ? masked : el.placeholder;
        };
        secretField('sett-resend-key', d.resend_api_key_set, d.resend_api_key_masked);
        v('sett-email-from', d.email_from);
        secretField('sett-yt-key', d.yt_api_key_set, d.yt_api_key_masked);
        v('sett-yt-channel', d.yt_channel_id);
        v('sett-yt-folder', d.yt_folder_name);
        _ytFolderName = d.yt_folder_name || '';
        _ytAccountFeaturesEnabled = !!d.yt_embed_account_features;
        { const el = document.getElementById('sett-yt-account-features'); if (el) el.checked = !!d.yt_embed_account_features; }
        { const el = document.getElementById('sett-yt-hide-shorts'); if (el) el.checked = !!d.yt_hide_shorts_global; }
        {
          const el = document.getElementById('sett-yt-quota');
          const q = d.yt_quota;
          if (el && q && d.yt_oauth_connected) {
            const pct = q.cap ? Math.min(100, Math.round((q.spent / q.cap) * 100)) : 0;
            const word = q.state === 'exhausted' ? ' — quota used up; resets midnight Pacific'
              : (q.state === 'low' ? ' — running low' : '');
            el.className = 'settings-yt-quota settings-yt-quota--' + q.state;
            el.textContent = `Quota today: ${q.spent.toLocaleString()} / ${q.cap.toLocaleString()} (${q.remaining.toLocaleString()} left, ${pct}%)${word}`;
            el.hidden = false;
            const qhint = document.getElementById('sett-yt-quota-hint');
            if (qhint) qhint.hidden = false;
          } else if (el) {
            el.hidden = true;
          }
        }
        // "On Star" destinations: show each row only when its integration is ready.
        {
          const ipRow = document.getElementById('sett-star-ip-row');
          const ytRow = document.getElementById('sett-star-yt-row');
          const emRow = document.getElementById('sett-star-email-row');
          const none = document.getElementById('sett-star-none');
          const quireRow = document.getElementById('sett-star-quire-row');
          const ipOk = !!window.INSTAPAPER_CONFIGURED, ytOk = !!d.yt_oauth_connected, emOk = !!window.EMAIL_CONFIGURED;
          const quireOk = !!d.quire_connected && !!d.quire_project_oid;
          const redditOk = !!d.reddit_connected;
          if (ipRow) { ipRow.hidden = !ipOk; const c = document.getElementById('sett-star-ip'); if (c) c.checked = !!d.star_send_instapaper; }
          if (quireRow) { quireRow.hidden = !quireOk; const c = document.getElementById('sett-star-quire'); if (c) c.checked = !!d.star_send_quire; }
          const redditRow = document.getElementById('sett-star-reddit-row');
          if (redditRow) { redditRow.hidden = !redditOk; const i = document.getElementById('sett-star-reddit-sub'); if (i) i.value = d.star_send_reddit_subreddit || ''; }
          if (emRow) { emRow.hidden = !emOk; const i = document.getElementById('sett-star-email'); if (i) i.value = d.star_send_email || ''; }
          if (ytRow) {
            ytRow.hidden = !ytOk;
            const sel = document.getElementById('sett-star-yt-playlist');
            if (sel && ytOk) {
              (async () => {
                try {
                  if (!window._ytPlaylistsForRules) {
                    const r = await fetch('/api/youtube/playlists', { credentials: 'same-origin' });
                    window._ytPlaylistsForRules = (await r.json().catch(() => ({}))).playlists || [];
                  }
                  sel.innerHTML = '<option value="">— off —</option>';
                  for (const pl of window._ytPlaylistsForRules) {
                    const o = document.createElement('option'); o.value = pl.id; o.textContent = pl.title; o.dataset.title = pl.title;
                    sel.appendChild(o);
                  }
                  if (d.star_send_yt_playlist && !window._ytPlaylistsForRules.some(p => p.id === d.star_send_yt_playlist)) {
                    const o = document.createElement('option'); o.value = d.star_send_yt_playlist;
                    o.textContent = (d.star_send_yt_playlist_title || d.star_send_yt_playlist) + ' (unavailable)';
                    o.dataset.title = d.star_send_yt_playlist_title || ''; sel.appendChild(o);
                  }
                  sel.value = d.star_send_yt_playlist || '';
                } catch { /* leave default */ }
              })();
            }
          }
          if (none) none.hidden = (ipOk || ytOk || emOk || quireOk || redditOk);
        }
        v('sett-yt-oauth-client-id', d.yt_oauth_client_id);
        secretField('sett-yt-oauth-client-secret', d.yt_oauth_client_secret_set, d.yt_oauth_client_secret_masked);
        v('sett-pinterest-client-id', d.pinterest_oauth_client_id);
        secretField('sett-pinterest-client-secret', d.pinterest_oauth_client_secret_set, d.pinterest_oauth_client_secret_masked);
        // YouTube account (OAuth) connection status for Add-to-playlist.
        {
          const ytConfigured = !!d.yt_oauth_configured;
          const ytConnected = !!d.yt_oauth_connected;
          const row = document.getElementById('sett-yt-oauth-row');
          const status = document.getElementById('sett-yt-oauth-status');
          const connect = document.getElementById('sett-yt-oauth-connect');
          const disconnect = document.getElementById('sett-yt-oauth-disconnect');
          if (row) row.hidden = !ytConfigured;
          if (status) status.textContent = ytConnected ? 'Connected.' : 'Not connected.';
          if (connect) connect.hidden = ytConnected;
          if (disconnect) disconnect.hidden = !ytConnected;
        }
        // Pinterest account (OAuth) connection status for the per-article Pin button.
        {
          const piConfigured = !!d.pinterest_oauth_configured;
          const piConnected = !!d.pinterest_oauth_connected;
          window.PINTEREST_CONNECTED = piConnected;
          const row = document.getElementById('sett-pinterest-oauth-row');
          const status = document.getElementById('sett-pinterest-oauth-status');
          const connect = document.getElementById('sett-pinterest-oauth-connect');
          const disconnect = document.getElementById('sett-pinterest-oauth-disconnect');
          if (row) row.hidden = !piConfigured;
          if (status) status.textContent = piConnected ? 'Connected.' : 'Not connected.';
          if (connect) connect.hidden = piConnected;
          if (disconnect) disconnect.hidden = !piConnected;
        }
        // Third-party RSS reader migrations.
        v('sett-miniflux-url', d.miniflux_import_url);
        secretField('sett-miniflux-token', d.miniflux_import_token_set, d.miniflux_import_token_masked);
        v('sett-freshrss-url', d.freshrss_url);
        v('sett-freshrss-username', d.freshrss_username);
        secretField('sett-freshrss-password', d.freshrss_password_set, d.freshrss_password_masked);
        v('sett-ttrss-url', d.ttrss_url);
        v('sett-ttrss-username', d.ttrss_username);
        secretField('sett-ttrss-password', d.ttrss_password_set, d.ttrss_password_masked);
        // Inoreader migration.
        v('sett-inoreader-client-id', d.inoreader_client_id);
        secretField('sett-inoreader-client-secret', d.inoreader_client_secret_set, d.inoreader_client_secret_masked);
        {
          const inoConfigured = !!d.inoreader_configured;
          const inoConnected = !!d.inoreader_connected;
          const status = document.getElementById('sett-inoreader-status');
          const connect = document.getElementById('sett-inoreader-connect');
          const disconnect = document.getElementById('sett-inoreader-disconnect');
          if (status) status.textContent = inoConnected ? 'Connected.' : inoConfigured ? 'Not connected.' : '';
          if (connect) connect.hidden = inoConnected || !inoConfigured;
          if (disconnect) disconnect.hidden = !inoConnected;
          // Show/hide API controls in Import/Export → Inoreader panel.
          const apiControls = document.getElementById('mig-ino-api-controls');
          const apiNotConn = document.getElementById('mig-ino-api-not-connected');
          if (apiControls) apiControls.hidden = !inoConnected;
          if (apiNotConn) apiNotConn.hidden = inoConnected;
          _migInoRefreshStatus();
        }
        v('sett-ip-user', d.instapaper_username);
        secretField('sett-ip-pass', d.instapaper_password_set, d.instapaper_password_masked);
        v('sett-da-id', d.deviantart_client_id);
        secretField('sett-da-secret', d.deviantart_client_secret_set, d.deviantart_client_secret_masked);
        v('sett-da-folder', d.deviantart_folder_name);
        // DeviantArt account connection status + action buttons.
        const daConnected = !!d.deviantart_connected;
        const daStatus = document.getElementById('sett-da-status');
        if (daStatus) daStatus.textContent = daConnected
          ? `Connected as ${d.deviantart_username || 'DeviantArt'}.`
          : 'Not connected.';
        const daConnect = document.getElementById('sett-da-connect');
        if (daConnect) daConnect.hidden = daConnected;
        ['sett-da-watchfeed', 'sett-da-sync', 'sett-da-disconnect'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.hidden = !daConnected;
        });
        const daActionStatus = document.getElementById('sett-da-action-status');
        if (daActionStatus && d.deviantart_sync_status) daActionStatus.textContent = d.deviantart_sync_status;
        renderDaSyncDetail(d.deviantart_sync_detail, daConnected, d.deviantart_deactivated);
        // Quire integration: creds, connection status, project picker, usage meter.
        v('sett-quire-id', d.quire_client_id);
        secretField('sett-quire-secret', d.quire_client_secret_set, d.quire_client_secret_masked);
        {
          const connected = !!d.quire_connected;
          window.QUIRE_CONFIGURED = connected && !!d.quire_project_oid;
          window.QUIRE_PROJECT_NAME = d.quire_project_name || '';
          const status = document.getElementById('sett-quire-status');
          if (status) status.textContent = connected
            ? `Connected as ${d.quire_username || 'Quire'}.`
            : 'Not connected.';
          const connect = document.getElementById('sett-quire-connect');
          if (connect) connect.hidden = connected;
          const disconnect = document.getElementById('sett-quire-disconnect');
          if (disconnect) disconnect.hidden = !connected;
          const projRow = document.getElementById('sett-quire-project-row');
          if (projRow) projRow.hidden = !connected;
          const sel = document.getElementById('sett-quire-project');
          if (sel && connected) {
            (async () => {
              try {
                const r = await fetch('/api/quire/projects', { credentials: 'same-origin' });
                const projects = (await r.json().catch(() => ({}))).projects || [];
                sel.innerHTML = '<option value="">— pick a project —</option>';
                for (const p of projects) {
                  const o = document.createElement('option'); o.value = p.oid; o.textContent = p.name; o.dataset.name = p.name;
                  sel.appendChild(o);
                }
                if (d.quire_project_oid && !projects.some(p => p.oid === d.quire_project_oid)) {
                  const o = document.createElement('option'); o.value = d.quire_project_oid;
                  o.textContent = (d.quire_project_name || d.quire_project_oid) + ' (unavailable)';
                  o.dataset.name = d.quire_project_name || ''; sel.appendChild(o);
                }
                sel.value = d.quire_project_oid || '';
              } catch { /* leave default */ }
            })();
          }
          // Usage meter (per-minute / per-hour).
          const el = document.getElementById('sett-quire-usage');
          const hint = document.getElementById('sett-quire-usage-hint');
          const q = d.quire_usage;
          if (el && q && connected) {
            const word = q.state === 'blocked' ? ' — rate limit reached'
              : (q.state === 'low' ? ' — running low' : '');
            const planTag = d.quire_plan ? ` [${d.quire_plan} plan]` : '';
            el.className = 'settings-yt-quota settings-yt-quota--' + (q.state === 'blocked' ? 'exhausted' : q.state);
            el.textContent = `Quire calls: ${q.minute_used}/${q.minute_cap} this minute, ${q.hour_used}/${q.hour_cap} this hour${planTag}${word}`;
            el.hidden = false;
            if (hint) hint.hidden = false;
          } else { if (el) el.hidden = true; if (hint) hint.hidden = true; }
        }
        // Reddit integration: creds + connection status.
        v('sett-reddit-client-id', d.reddit_client_id);
        secretField('sett-reddit-client-secret', d.reddit_client_secret_set, d.reddit_client_secret_masked);
        {
          const rdConfigured = !!d.reddit_configured;
          const rdConnected = !!d.reddit_connected;
          window.REDDIT_CONNECTED = rdConnected;
          const row = document.getElementById('sett-reddit-oauth-row');
          const status = document.getElementById('sett-reddit-oauth-status');
          const connect = document.getElementById('sett-reddit-oauth-connect');
          const disconnect = document.getElementById('sett-reddit-oauth-disconnect');
          if (row) row.hidden = !rdConfigured;
          if (status) status.textContent = rdConnected
            ? `Connected as /u/${d.reddit_username || 'Reddit'}.`
            : rdConfigured ? 'Not connected.' : '';
          if (connect) connect.hidden = rdConnected || !rdConfigured;
          if (disconnect) disconnect.hidden = !rdConnected;
        }
        // Populate OAuth callback URL hints with the actual public URL.
        {
          const base = (d.public_url || window.location.origin).replace(/\/$/, '');
          const cbMap = {
            'sett-yt-callback-url':        base + '/youtube/oauth/callback',
            'sett-da-callback-url':        base + '/deviantart/callback',
            'sett-quire-callback-url':     base + '/quire/callback',
            'sett-pinterest-callback-url': base + '/integrations/pinterest/oauth/callback',
            'sett-inoreader-callback-url': base + '/inoreader/oauth/callback',
            'sett-reddit-callback-url':    base + '/integrations/reddit/oauth/callback',
          };
          for (const [id, url] of Object.entries(cbMap)) {
            const el = document.getElementById(id);
            if (el) el.textContent = url;
          }
        }
        settContacts = d.contacts || [];
        settDefaultAddress = d.email_to_default || '';
        settRenderContacts();
        updateSettAvatar();
      }

      let settDefaultAddress = '';

      function settRenderContacts() {
        const list = document.getElementById('sett-contacts-list');
        if (!list) return;
        list.innerHTML = '';
        const meEmail = settingsData?.profile_email || window.PROFILE_EMAIL || '';
        const meEmailLower = meEmail.toLowerCase();
        const currentDefault = settDefaultAddress || settingsData?.email_to_default || '';

        function makeSetDefaultBtn(address) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'sett-contact-set-default' + (address.toLowerCase() === currentDefault.toLowerCase() ? ' sett-contact-set-default--active' : '');
          btn.title = address.toLowerCase() === currentDefault.toLowerCase() ? 'Default' : 'Set as default';
          btn.textContent = address.toLowerCase() === currentDefault.toLowerCase() ? '★' : '☆';
          btn.addEventListener('click', async () => {
            settDefaultAddress = address;
            settRenderContacts();
            await settSave('default');
          });
          return btn;
        }

        // Me row (always first, derived from profile email)
        const meRow = document.createElement('div');
        meRow.className = 'sett-contact-row sett-contact-row--me';
        const meLbl = document.createElement('span');
        meLbl.className = 'sett-contact-label';
        meLbl.textContent = meEmail ? `Me <${meEmail}>` : 'Me (set your Profile email first)';
        meRow.appendChild(meLbl);
        if (meEmail) meRow.appendChild(makeSetDefaultBtn(meEmail));
        list.appendChild(meRow);

        settContacts.forEach((c, idx) => {
          if (meEmailLower && c.address.toLowerCase() === meEmailLower) return;
          const row = document.createElement('div');
          row.className = 'sett-contact-row';
          const lbl = document.createElement('span');
          lbl.className = 'sett-contact-label';
          lbl.textContent = c.label ? `${c.label} <${c.address}>` : c.address;
          row.appendChild(lbl);
          row.appendChild(makeSetDefaultBtn(c.address));
          const del = document.createElement('button');
          del.type = 'button';
          del.className = 'sett-contact-del';
          del.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">close</span>';
          del.addEventListener('click', async () => {
            settContacts.splice(idx, 1);
            settRenderContacts();
            await settSave('contacts');
          });
          row.appendChild(del);
          list.appendChild(row);
        });
      }

      document.getElementById('sett-contact-email')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); document.getElementById('sett-contact-save')?.click(); }
      });
      document.getElementById('sett-contact-save')?.addEventListener('click', async () => {
        const label = document.getElementById('sett-contact-label')?.value.trim() || '';
        const address = document.getElementById('sett-contact-email')?.value.trim() || '';
        if (!address) { document.getElementById('sett-contact-email')?.focus(); return; }
        if (label.toLowerCase() === 'me') {
          showToastMessage('"Me" is reserved for your Profile email — choose a different label.');
          return;
        }
        settContacts.push({ label, address });
        settRenderContacts();
        const li = document.getElementById('sett-contact-label');
        const ei = document.getElementById('sett-contact-email');
        if (li) li.value = '';
        if (ei) ei.value = '';
        li?.focus();
        await settSave('contacts');
      });

      async function settSave(scope) {
        const g = id => document.getElementById(id)?.value?.trim() || '';
        let payload = {};
        if (scope === 'account') {
          // Per-user only. Instance config (maintenance) lives on the Admin page.
          payload = {
            profile_name: g('sett-profile-name'),
            profile_email: g('sett-profile-email'),
            tz_display: g('sett-tz-display'),
          };
        } else if (scope === 'profile') {
          payload = { profile_name: g('sett-profile-name'), profile_email: g('sett-profile-email') };
        } else if (scope === 'app') {
          payload = { tz_display: g('sett-tz-display') };
        } else if (scope === 'contacts') {
          payload = { email_contacts: JSON.stringify(settContacts) };
        } else if (scope === 'default') {
          payload = { email_to: settDefaultAddress };
        } else if (scope === 'integrations') {
          // Per-user only. Email (Resend key + From) is instance config on the Admin page.
          payload = {
            yt_api_key: g('sett-yt-key'),
            yt_channel_id: g('sett-yt-channel'),
            yt_folder_name: g('sett-yt-folder'),
            yt_embed_account_features: (document.getElementById('sett-yt-account-features')?.checked ? '1' : '0'),
            yt_hide_shorts_global: (document.getElementById('sett-yt-hide-shorts')?.checked ? '1' : '0'),
            instapaper_username: g('sett-ip-user'),
            instapaper_password: g('sett-ip-pass'),
            yt_oauth_client_id: g('sett-yt-oauth-client-id'),
            yt_oauth_client_secret: g('sett-yt-oauth-client-secret'),
            pinterest_oauth_client_id: g('sett-pinterest-client-id'),
            pinterest_oauth_client_secret: g('sett-pinterest-client-secret'),
            deviantart_client_id: g('sett-da-id'),
            deviantart_client_secret: g('sett-da-secret'),
            deviantart_folder_name: g('sett-da-folder'),
            quire_client_id: g('sett-quire-id'),
            quire_client_secret: g('sett-quire-secret'),
            quire_project_oid: (document.getElementById('sett-quire-project')?.value || ''),
            quire_project_name: (() => { const s = document.getElementById('sett-quire-project'); const o = s && s.options[s.selectedIndex]; return (o && o.dataset.name) || ''; })(),
            star_send_quire: (document.getElementById('sett-star-quire')?.checked ? '1' : '0'),
            star_send_instapaper: (document.getElementById('sett-star-ip')?.checked ? '1' : '0'),
            star_send_yt_playlist: (document.getElementById('sett-star-yt-playlist')?.value || ''),
            star_send_yt_playlist_title: (() => { const s = document.getElementById('sett-star-yt-playlist'); const o = s && s.options[s.selectedIndex]; return (o && o.dataset.title) || ''; })(),
            star_send_email: (document.getElementById('sett-star-email')?.value || '').trim(),
            star_send_reddit_subreddit: (document.getElementById('sett-star-reddit-sub')?.value || '').trim(),
            reddit_client_id: g('sett-reddit-client-id'),
            reddit_client_secret: g('sett-reddit-client-secret'),
          };
        }
        try {
          const resp = await fetch('/settings/all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(payload),
          });
          if (!resp.ok) throw new Error('failed');
          // Update client-side globals after save
          if (scope === 'profile' || scope === 'account') {
            window.PROFILE_NAME = payload.profile_name;
            window.PROFILE_EMAIL = payload.profile_email;
            settRenderContacts();
            updateSettAvatar();
          }
          if (scope === 'contacts') {
            window.EMAIL_CONTACTS = [...settContacts];
          }
          showToastMessage('Settings saved.');
        } catch { showToastMessage('Failed to save settings.'); }
      }

      document.querySelectorAll('[data-settings-save]').forEach(btn => {
        btn.addEventListener('click', () => settSave(btn.getAttribute('data-settings-save')));
      });

      // Contacts tab save is triggered by the Contacts save button
      document.getElementById('settings-tab-contacts')?.addEventListener('click', e => {
        if (e.target.closest('[data-settings-save="contacts"]')) settSave('contacts');
      });

      // DeviantArt: verify saved credentials by requesting an app token.
      document.getElementById('sett-da-verify')?.addEventListener('click', async () => {
        const status = document.getElementById('sett-da-verify-status');
        if (status) status.textContent = 'Checking…';
        try {
          const resp = await fetch('/settings/deviantart/verify', { method: 'POST', credentials: 'same-origin' });
          const d = await resp.json();
          if (status) status.textContent = d.message || (d.ok ? 'OK' : 'Failed');
        } catch (err) {
          if (status) status.textContent = `Error: ${err.message || err}`;
        }
      });

      const _daAction = async (endpoint, busyText, fmt) => {
        const status = document.getElementById('sett-da-action-status');
        if (status) status.textContent = busyText;
        try {
          const resp = await fetch(endpoint, { method: 'POST', credentials: 'same-origin' });
          const d = await resp.json();
          if (status) status.textContent = d.error ? `Error: ${d.error}` : fmt(d);
        } catch (err) {
          if (status) status.textContent = `Error: ${err.message || err}`;
        }
      };
      document.getElementById('sett-da-watchfeed')?.addEventListener('click', () =>
        _daAction('/deviantart/add-watch-feed', 'Adding…', d => d.message || 'Added.'));
      document.getElementById('sett-da-sync')?.addEventListener('click', () =>
        _daAction('/deviantart/sync-watchlist', 'Syncing…', d => d.message || 'Sync started.'));
      document.getElementById('sett-da-disconnect')?.addEventListener('click', async () => {
        await fetch('/deviantart/disconnect', { method: 'POST', credentials: 'same-origin' });
        loadSettingsData();
      });
      document.getElementById('sett-yt-oauth-disconnect')?.addEventListener('click', async () => {
        await fetch('/integrations/youtube/oauth/disconnect', { method: 'POST', credentials: 'same-origin' });
        loadSettingsData();
      });
      document.getElementById('sett-pinterest-oauth-disconnect')?.addEventListener('click', async () => {
        await fetch('/integrations/pinterest/oauth/disconnect', { method: 'POST', credentials: 'same-origin' });
        loadSettingsData();
      });
      document.getElementById('sett-quire-disconnect')?.addEventListener('click', async () => {
        await fetch('/quire/disconnect', { method: 'POST', credentials: 'same-origin' });
        loadSettingsData();
      });
      document.getElementById('sett-reddit-oauth-disconnect')?.addEventListener('click', async () => {
        await fetch('/integrations/reddit/oauth/disconnect', { method: 'POST', credentials: 'same-origin' });
        loadSettingsData();
      });

      // Inoreader integration handlers.
      // Inoreader OAuth (in Integrations tab).
      document.getElementById('sett-inoreader-disconnect')?.addEventListener('click', async () => {
        await fetch('/integrations/inoreader/oauth/disconnect', { method: 'POST', credentials: 'same-origin' });
        loadSettingsData();
      });

      // Inoreader migrator (in Import/Export tab).
      async function _migInoRefreshStatus() {
        try {
          const r = await fetch('/integrations/inoreader/import/status', { credentials: 'same-origin' });
          const d = await r.json().catch(() => ({}));
          const progressEl = document.getElementById('mig-ino-progress');
          const localStatus = document.getElementById('mig-ino-local-status');
          const cancelBtn = document.getElementById('mig-ino-local-cancel');
          const apiStatus = document.getElementById('mig-ino-api-status');
          const apiStart = document.getElementById('mig-ino-api-start');
          const apiRun = document.getElementById('mig-ino-api-run');
          const apiReset = document.getElementById('mig-ino-api-reset');

          const phase = d.phase;
          const isLocal = phase === 'local_files';
          const isApi = phase && !isLocal;
          const rl = d.z1_remaining != null ? ` (quota remaining: ${d.z1_remaining})` : '';

          if (!phase) {
            if (progressEl) { progressEl.textContent = ''; progressEl.hidden = true; }
            if (localStatus) localStatus.textContent = '';
            if (cancelBtn) cancelBtn.hidden = true;
            if (apiStart) apiStart.hidden = false;
            if (apiRun) apiRun.hidden = true;
            if (apiReset) apiReset.hidden = true;
            return;
          }

          if (isLocal) {
            const total = d.files_total || 0;
            const done = d.files_done || 0;
            const pct = total ? Math.round(done / total * 100) : 0;
            const summary = `${d.subs_added || 0} feeds added, ${d.items_tagged || 0} tagged, ${d.items_starred || 0} starred`;
            const cur = d.current_file ? ` — ${d.current_file}` : '';
            if (d.done) {
              if (localStatus) localStatus.textContent = `Done ✓ — ${summary}`;
              if (progressEl) { progressEl.textContent = ''; progressEl.hidden = true; }
              if (cancelBtn) cancelBtn.hidden = false;
            } else if (d.error) {
              if (localStatus) localStatus.textContent = `Error: ${d.error}`;
              if (progressEl) { progressEl.textContent = ''; progressEl.hidden = true; }
              if (cancelBtn) cancelBtn.hidden = false;
            } else {
              if (localStatus) localStatus.textContent = `${pct}% (${done}/${total} files)`;
              if (progressEl) { progressEl.textContent = `${summary}${cur}`; progressEl.hidden = false; }
              if (cancelBtn) cancelBtn.hidden = false;
              // Poll while running.
              setTimeout(_migInoRefreshStatus, 3000);
            }
          }

          if (isApi) {
            const phaseLabels = { subscriptions: 'Subscribing feeds…', labels_list: 'Fetching label list…', labels_items: 'Importing labels…', starred: 'Importing starred…' };
            const summary = `${d.subs_added || 0} feeds, ${d.items_tagged || 0} tagged, ${d.items_starred || 0} starred${rl}`;
            if (d.done) {
              if (apiStatus) apiStatus.textContent = `Done ✓ — ${summary}`;
              if (apiStart) apiStart.hidden = true;
              if (apiRun) apiRun.hidden = true;
              if (apiReset) apiReset.hidden = false;
            } else if (d.error) {
              if (apiStatus) apiStatus.textContent = `Error: ${d.error}`;
              if (apiStart) apiStart.hidden = true;
              if (apiRun) apiRun.hidden = false;
              if (apiReset) apiReset.hidden = false;
            } else {
              if (apiStatus) apiStatus.textContent = `${phaseLabels[phase] || phase} — ${summary}`;
              if (apiStart) apiStart.hidden = true;
              if (apiRun) apiRun.hidden = false;
              if (apiReset) apiReset.hidden = false;
            }
          }
        } catch { /* ignore */ }
      }

      // Inoreader creds save button (in Import/Export → Inoreader panel).
      document.getElementById('mig-ino-creds-save')?.addEventListener('click', async () => {
        const cid = document.getElementById('sett-inoreader-client-id')?.value.trim() || '';
        const secret = document.getElementById('sett-inoreader-client-secret')?.value.trim() || '';
        const st = document.getElementById('sett-inoreader-status');
        const payload = { inoreader_client_id: cid };
        if (secret && !secret.startsWith('•')) payload.inoreader_client_secret = secret;
        try {
          const resp = await fetch('/settings/all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(payload),
          });
          if (st) st.textContent = resp.ok ? 'Saved.' : 'Save failed.';
          if (resp.ok) setTimeout(loadSettingsData, 300);
        } catch (err) {
          if (st) st.textContent = `Error: ${err.message || err}`;
        }
      });

      // File picker for Inoreader import.
      document.getElementById('mig-ino-file-pick')?.addEventListener('click', () => {
        document.getElementById('mig-ino-file-input')?.click();
      });
      document.getElementById('mig-ino-file-input')?.addEventListener('change', e => {
        const files = e.target.files;
        const label = document.getElementById('mig-ino-file-label');
        const uploadBtn = document.getElementById('mig-ino-upload-btn');
        if (!files || !files.length) {
          if (label) label.textContent = 'No files selected';
          if (uploadBtn) uploadBtn.hidden = true;
          return;
        }
        const names = Array.from(files).map(f => f.name).join(', ');
        if (label) label.textContent = files.length === 1 ? names : `${files.length} files: ${names}`;
        if (uploadBtn) uploadBtn.hidden = false;
      });

      // Upload & import button.
      document.getElementById('mig-ino-upload-btn')?.addEventListener('click', async () => {
        const input = document.getElementById('mig-ino-file-input');
        const st = document.getElementById('mig-ino-local-status');
        const uploadBtn = document.getElementById('mig-ino-upload-btn');
        if (!input?.files?.length) return;
        if (st) st.textContent = 'Uploading…';
        if (uploadBtn) uploadBtn.disabled = true;
        const fd = new FormData();
        for (const f of input.files) fd.append('files', f);
        try {
          const r = await fetch('/integrations/inoreader/import/upload', {
            method: 'POST',
            credentials: 'same-origin',
            body: fd,
          });
          const d = await r.json().catch(() => ({}));
          if (d.ok) {
            if (st) st.textContent = `Started — ${d.files} JSON file(s) queued.`;
            if (uploadBtn) { uploadBtn.hidden = true; uploadBtn.disabled = false; }
            setTimeout(_migInoRefreshStatus, 1500);
          } else {
            if (st) st.textContent = `Error: ${d.error || 'unknown'}`;
            if (uploadBtn) uploadBtn.disabled = false;
          }
        } catch (err) {
          if (st) st.textContent = `Error: ${err.message || err}`;
          if (uploadBtn) uploadBtn.disabled = false;
        }
      });

      // Reset button for local import.
      document.getElementById('mig-ino-local-cancel')?.addEventListener('click', async () => {
        await fetch('/integrations/inoreader/import/reset', { method: 'POST', credentials: 'same-origin' });
        document.getElementById('mig-ino-file-input').value = '';
        document.getElementById('mig-ino-file-label').textContent = 'No files selected';
        document.getElementById('mig-ino-upload-btn').hidden = true;
        _migInoRefreshStatus();
      });

      // API import controls.
      document.getElementById('mig-ino-api-start')?.addEventListener('click', async () => {
        try {
          const since = document.getElementById('mig-ino-api-since')?.value || '';
          const fd = new FormData();
          if (since) fd.append('since', since);
          await fetch('/integrations/inoreader/import/start', { method: 'POST', credentials: 'same-origin', body: fd });
          setTimeout(_migInoRefreshStatus, 1500);
        } catch { /* ignore */ }
      });
      document.getElementById('mig-ino-api-run')?.addEventListener('click', async () => {
        await fetch('/integrations/inoreader/import/run', { method: 'POST', credentials: 'same-origin' });
        setTimeout(_migInoRefreshStatus, 2000);
      });
      document.getElementById('mig-ino-api-reset')?.addEventListener('click', async () => {
        await fetch('/integrations/inoreader/import/reset', { method: 'POST', credentials: 'same-origin' });
        _migInoRefreshStatus();
      });

      // -----------------------------------------------------------------------
      // Generic migrator helper — wires up save/test/import/reset for
      // Miniflux, FreshRSS, and tt-rss panels.
      // -----------------------------------------------------------------------
      function _setupMigrator({ prefix, apiPrefix, credFields, sensitiveFields }) {
        const $ = id => document.getElementById(id);

        // Save credentials via /settings/all.
        $(`mig-${prefix}-save`)?.addEventListener('click', async () => {
          const st = $(`mig-${prefix}-creds-status`);
          const payload = {};
          for (const [settingKey, elId] of Object.entries(credFields)) {
            const val = $(elId)?.value?.trim() || '';
            if (val && !(sensitiveFields.includes(elId) && val.startsWith('•'))) {
              payload[settingKey] = val;
            } else if (!sensitiveFields.includes(elId)) {
              payload[settingKey] = val;
            }
          }
          try {
            const resp = await fetch('/settings/all', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              credentials: 'same-origin', body: JSON.stringify(payload),
            });
            if (st) st.textContent = resp.ok ? 'Saved.' : 'Save failed.';
            if (resp.ok) setTimeout(loadSettingsData, 300);
          } catch (err) {
            if (st) st.textContent = `Error: ${err.message || err}`;
          }
        });

        // Test connection.
        $(`mig-${prefix}-test`)?.addEventListener('click', async () => {
          const st = $(`mig-${prefix}-creds-status`);
          if (st) st.textContent = 'Testing…';
          const body = {};
          for (const [, elId] of Object.entries(credFields)) {
            const key = elId.replace(`sett-${prefix}-`, '');
            body[key] = $(elId)?.value?.trim() || '';
          }
          try {
            const resp = await fetch(`/integrations/${apiPrefix}/import/test`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              credentials: 'same-origin', body: JSON.stringify(body),
            });
            const d = await resp.json().catch(() => ({}));
            if (st) st.textContent = d.ok ? `Connected${d.username ? ` — ${d.username}` : (d.version ? ` (v${d.version})` : '')}` : `Error: ${d.error || resp.status}`;
          } catch (err) {
            if (st) st.textContent = `Error: ${err.message || err}`;
          }
        });

        // Poll import status.
        async function refreshStatus() {
          try {
            const r = await fetch(`/integrations/${apiPrefix}/import/status`, { credentials: 'same-origin' });
            const d = await r.json().catch(() => ({}));
            const s = d.state;
            const statusEl = $(`mig-${prefix}-status`);
            const startBtn = $(`mig-${prefix}-start`);
            const resetBtn = $(`mig-${prefix}-reset`);
            if (!s) {
              if (statusEl) statusEl.textContent = '';
              if (startBtn) startBtn.hidden = false;
              if (resetBtn) resetBtn.hidden = true;
              return;
            }
            const summary = `${s.subs_added || 0} feeds, ${s.items_starred || 0} starred, ${s.items_tagged || 0} tagged`;
            if (s.done) {
              if (statusEl) statusEl.textContent = `Done ✓ — ${summary}`;
              if (startBtn) startBtn.hidden = true;
              if (resetBtn) resetBtn.hidden = false;
            } else if (s.error) {
              if (statusEl) statusEl.textContent = `Error: ${s.error}`;
              if (startBtn) startBtn.hidden = true;
              if (resetBtn) resetBtn.hidden = false;
            } else {
              if (statusEl) statusEl.textContent = `Running… ${summary}`;
              if (startBtn) startBtn.hidden = true;
              if (resetBtn) resetBtn.hidden = false;
              setTimeout(refreshStatus, 3000);
            }
          } catch { /* ignore */ }
        }

        $(`mig-${prefix}-start`)?.addEventListener('click', async () => {
          try {
            await fetch(`/integrations/${apiPrefix}/import/start`, { method: 'POST', credentials: 'same-origin' });
            setTimeout(refreshStatus, 1500);
          } catch { /* ignore */ }
        });
        $(`mig-${prefix}-reset`)?.addEventListener('click', async () => {
          await fetch(`/integrations/${apiPrefix}/import/reset`, { method: 'POST', credentials: 'same-origin' });
          refreshStatus();
        });

        refreshStatus();
      }

      _setupMigrator({
        prefix: 'miniflux', apiPrefix: 'miniflux',
        credFields: { miniflux_import_url: 'sett-miniflux-url', miniflux_import_token: 'sett-miniflux-token' },
        sensitiveFields: ['sett-miniflux-token'],
      });
      _setupMigrator({
        prefix: 'freshrss', apiPrefix: 'freshrss',
        credFields: { freshrss_url: 'sett-freshrss-url', freshrss_username: 'sett-freshrss-username', freshrss_password: 'sett-freshrss-password' },
        sensitiveFields: ['sett-freshrss-password'],
      });
      _setupMigrator({
        prefix: 'ttrss', apiPrefix: 'ttrss',
        credFields: { ttrss_url: 'sett-ttrss-url', ttrss_username: 'sett-ttrss-username', ttrss_password: 'sett-ttrss-password' },
        sensitiveFields: ['sett-ttrss-password'],
      });

      document.getElementById('sett-maint-run-now')?.addEventListener('click', async () => {
        try {
          await fetch('/settings/maintenance/run-now', { method: 'POST', credentials: 'same-origin' });
          showToastMessage('Maintenance started in background.');
        } catch { showToastMessage('Failed to start maintenance.'); }
      });

      document.getElementById('menu-settings-btn')?.addEventListener('click', () => openSettingsModal('account'));
      document.getElementById('menu-profile-btn')?.addEventListener('click', () => openSettingsModal('account'));

      document.getElementById('sett-api-token-copy')?.addEventListener('click', async () => {
        const tokenEl = document.getElementById('sett-api-token');
        const token = (tokenEl?.value ?? tokenEl?.textContent ?? '').trim();
        const ok = await copyTextToClipboard(token);
        showToastMessage(ok ? 'API token copied.' : 'Could not copy token.');
      });
    }

    // ── Highlight engine (applies <mark> to post titles) ────────────────────
    function applyHighlights() {
      const rules = window.HIGHLIGHT_RULES || [];

      // Flat-text highlight for single-line elements (titles) — replaces all content.
      function highlightEl(el, matchRules) {
        if (!el) return;
        el.querySelectorAll('.highlight-mark').forEach(m => m.replaceWith(document.createTextNode(m.textContent)));
        el.normalize();
        if (matchRules.length === 0) return;
        const text = el.textContent;
        if (!text) return;
        const ranges = [];
        for (const rule of matchRules) {
          let re;
          try {
            re = rule.is_regex
              ? new RegExp(rule.keyword, 'gi')
              : new RegExp(rule.keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
          } catch { continue; }
          let m;
          while ((m = re.exec(text)) !== null) {
            if (m[0].length === 0) { re.lastIndex++; continue; }
            if (!ranges.some(r => m.index < r.end && m.index + m[0].length > r.start)) {
              ranges.push({ start: m.index, end: m.index + m[0].length, color: rule.color });
            }
          }
        }
        if (ranges.length === 0) return;
        ranges.sort((a, b) => a.start - b.start);
        const frag = document.createDocumentFragment();
        let pos = 0;
        for (const r of ranges) {
          if (r.start > pos) frag.appendChild(document.createTextNode(text.slice(pos, r.start)));
          const mark = document.createElement('mark');
          mark.className = `highlight-mark highlight-mark-${r.color}`;
          mark.textContent = text.slice(r.start, r.end);
          frag.appendChild(mark);
          pos = r.end;
        }
        if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
        el.textContent = '';
        el.appendChild(frag);
      }

      // Structure-preserving highlight for rich HTML bodies — walks text nodes.
      function highlightBody(el, matchRules) {
        if (!el || matchRules.length === 0) return;
        // Clear previous highlights
        el.querySelectorAll('.highlight-mark').forEach(m => m.replaceWith(document.createTextNode(m.textContent)));
        el.normalize();
        const patterns = [];
        for (const rule of matchRules) {
          try {
            patterns.push({
              re: rule.is_regex
                ? new RegExp(rule.keyword, 'gi')
                : new RegExp(rule.keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'),
              color: rule.color,
            });
          } catch { /* invalid regex */ }
        }
        if (patterns.length === 0) return;

        function walkNode(node) {
          if (node.nodeType === Node.TEXT_NODE) {
            const text = node.nodeValue;
            if (!text) return;
            const matches = [];
            for (const { re, color } of patterns) {
              re.lastIndex = 0;
              let m;
              while ((m = re.exec(text)) !== null) {
                if (m[0].length === 0) { re.lastIndex++; continue; }
                matches.push({ start: m.index, end: m.index + m[0].length, color });
              }
            }
            if (matches.length === 0) return;
            matches.sort((a, b) => a.start - b.start);
            // Deduplicate overlapping ranges
            const deduped = [];
            for (const m of matches) {
              if (deduped.length > 0 && m.start < deduped[deduped.length - 1].end) continue;
              deduped.push(m);
            }
            const frag = document.createDocumentFragment();
            let pos = 0;
            for (const r of deduped) {
              if (r.start > pos) frag.appendChild(document.createTextNode(text.slice(pos, r.start)));
              const mark = document.createElement('mark');
              mark.className = `highlight-mark highlight-mark-${r.color}`;
              mark.textContent = text.slice(r.start, r.end);
              frag.appendChild(mark);
              pos = r.end;
            }
            if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
            node.parentNode.replaceChild(frag, node);
            return;
          }
          if (node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName.toLowerCase();
            if (tag === 'mark' || tag === 'script' || tag === 'style') return;
          }
          for (const child of [...node.childNodes]) walkNode(child);
        }
        walkNode(el);
      }

      const activeRules = rules.filter(r => r.enabled !== 0 && (r.type || 'highlight') === 'highlight');

      for (const postItem of document.querySelectorAll('.post-item')) {
        const feedUrl = postItem.getAttribute('data-post-feed-url') || '';
        const folderIdStr = postItem.querySelector('.post-feed-link')?.getAttribute('data-folder-id') || '';
        const matchRules = activeRules.filter(r =>
          r.scope === 'global' ||
          (r.scope === 'folder' && r.scope_id === folderIdStr) ||
          (r.scope === 'feed' && r.scope_id === feedUrl)
        );
        const titleRules = matchRules.filter(r => (r.search_in || 'title') !== 'body');
        highlightEl(postItem.querySelector('.post-title'), titleRules);
      }

      const entryTitleEl = document.querySelector('.entry-pane-title-link');
      const entryBodyEl = document.getElementById('entry-body');
      if (entryTitleEl || entryBodyEl) {
        const feedUrl = document.querySelector('.entry-pane-title')?.getAttribute('data-post-feed-url') || '';
        const folderIdStr = document.querySelector('.entry-feed-link')?.getAttribute('data-folder-id') || '';
        const matchRules = activeRules.filter(r =>
          r.scope === 'global' ||
          (r.scope === 'folder' && r.scope_id === folderIdStr) ||
          (r.scope === 'feed' && r.scope_id === feedUrl)
        );
        const titleRules = matchRules.filter(r => (r.search_in || 'title') !== 'body');
        const bodyRules = matchRules.filter(r => { const s = r.search_in || 'title'; return s === 'body' || s === 'both'; });
        highlightEl(entryTitleEl, titleRules);
        highlightBody(entryBodyEl, bodyRules);
      }
    }

    applyHighlights();

    const folderPropertiesModal = document.getElementById('folder-properties-modal');
    document.getElementById('folder-properties-close')?.addEventListener('click', () => {
      folderPropertiesModal?.setAttribute('hidden', '');
    });
    document.getElementById('folder-prop-cadence')?.addEventListener('change', async (e) => {
      const sel = e.target;
      const folderId = sel.dataset.folderId;
      if (!folderId) return;
      const status = document.getElementById('folder-prop-cadence-status');
      if (status) status.textContent = 'Saving…';
      try {
        const body = new URLSearchParams({ folder_id: folderId, cadence_minutes: sel.value });
        const resp = await fetch('/folders/cadence', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || 'save failed');
        if (status) status.textContent = '';
        // Keep the inline Feeds-tab cadence dropdown in sync.
        const inlineSel = document.querySelector(`.settings-folder-cadence-select[data-folder-id="${folderId}"]`);
        if (inlineSel) { inlineSel.value = sel.value; inlineSel.dataset.prevValue = sel.value; }
      } catch (err) {
        if (status) status.textContent = `Error: ${err.message}`;
      }
    });

    let _folderPropCloseOnClick = false;
    folderPropertiesModal?.addEventListener('pointerdown', (event) => {
      _folderPropCloseOnClick = event.target === folderPropertiesModal;
    });
    folderPropertiesModal?.addEventListener('click', (event) => {
      if (_folderPropCloseOnClick && event.target === folderPropertiesModal) folderPropertiesModal.setAttribute('hidden', '');
      _folderPropCloseOnClick = false;
    });

    // Feeds tab: keep the Deactivated tab's count badge in sync.
    function bumpDeactivatedBadge(delta) {
      const btn = document.querySelector('#settings-tab-feeds [data-feeds-view="deactivated"]');
      if (!btn) return;
      let badge = btn.querySelector('.feeds-tab-count');
      const next = (badge ? parseInt(badge.textContent, 10) || 0 : 0) + delta;
      if (next <= 0) { if (badge) badge.remove(); return; }
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'feeds-tab-count';
        btn.appendChild(document.createTextNode(' '));
        btn.appendChild(badge);
      }
      badge.textContent = String(next);
    }

    // Feeds tab: disable/enable a feed in place (row stays, greyed when disabled).
    function toggleFeedDisabled(btn, url, folderId, disable) {
      if (!url) return;
      const row = btn.closest('.settings-feed-row');
      btn.disabled = true;
      const endpoint = disable ? '/feeds/disable' : '/feeds/enable';
      const body = new URLSearchParams({ folder_id: folderId || '', feed_url: url });
      fetch(endpoint, {
        method: 'POST',
        headers: { 'X-Requested-With': 'lectio-feeds-tree', 'Content-Type': 'application/x-www-form-urlencoded' },
        credentials: 'same-origin',
        body: body.toString(),
      })
        .then(resp => { if (!resp.ok) throw new Error(`HTTP ${resp.status}`); })
        .then(() => {
          btn.disabled = false;
          if (row) {
            row.classList.toggle('settings-feed-row--disabled', disable);
            const nameCell = row.querySelector('.settings-feed-name');
            let tag = row.querySelector('.settings-feed-disabled-tag');
            if (disable && !tag && nameCell) {
              tag = document.createElement('span');
              tag.className = 'settings-feed-disabled-tag';
              tag.textContent = 'disabled';
              nameCell.appendChild(tag);
            } else if (!disable && tag) {
              tag.remove();
            }
          }
          const icon = btn.querySelector('.material-symbols-rounded');
          if (disable) {
            btn.removeAttribute('data-feed-disable-url');
            btn.dataset.feedEnableUrl = url;
            btn.title = 'Enable feed';
            btn.setAttribute('aria-label', 'Enable feed');
            if (icon) icon.textContent = 'visibility';
          } else {
            btn.removeAttribute('data-feed-enable-url');
            btn.dataset.feedDisableUrl = url;
            btn.title = 'Disable feed';
            btn.setAttribute('aria-label', 'Disable feed');
            if (icon) icon.textContent = 'visibility_off';
          }
          bumpDeactivatedBadge(disable ? 1 : -1);
          showToastMessage(disable ? 'Feed disabled.' : 'Feed enabled.');
        })
        .catch(err => {
          btn.disabled = false;
          showToastMessage(`${disable ? 'Disable' : 'Enable'} failed: ${err.message || err}`);
        });
    }

    // Feeds tab: folder properties buttons + Fix URL Titles button.
    document.getElementById('settings-tab-feeds')?.addEventListener('click', (e) => {
      const toggleBtn = e.target.closest('[data-folder-toggle]');
      if (toggleBtn) {
        const fid = toggleBtn.dataset.folderToggle;
        const expanded = toggleBtn.getAttribute('aria-expanded') === 'true';
        toggleBtn.setAttribute('aria-expanded', String(!expanded));
        toggleBtn.title = expanded ? 'Show feeds' : 'Hide feeds';
        document.querySelectorAll(`#settings-tab-feeds [data-folder-feeds="${CSS.escape(fid)}"]`).forEach(tr => { tr.hidden = expanded; });
        return;
      }

      const folderAutoBtn = e.target.closest('[data-folder-automation-id]');
      if (folderAutoBtn) { window.openHighlightsModal?.({ scope: 'folder', scope_id: folderAutoBtn.dataset.folderAutomationId }); return; }

      const feedPropsBtn = e.target.closest('[data-feed-properties-url]');
      if (feedPropsBtn) { openFeedPropertiesModal(feedPropsBtn.dataset.feedPropertiesUrl); return; }

      const feedAutoBtn = e.target.closest('[data-feed-automation-url]');
      if (feedAutoBtn) { window.openHighlightsModal?.({ scope: 'feed', scope_id: feedAutoBtn.dataset.feedAutomationUrl, folder_id: feedAutoBtn.dataset.folderId }); return; }

      const feedDisableBtn = e.target.closest('[data-feed-disable-url]');
      if (feedDisableBtn) {
        const url = feedDisableBtn.dataset.feedDisableUrl;
        const folderId = feedDisableBtn.dataset.folderId;
        if (!url) return;
        toggleFeedDisabled(feedDisableBtn, url, folderId, true);
        return;
      }

      const feedEnableBtn = e.target.closest('[data-feed-enable-url]');
      if (feedEnableBtn) {
        toggleFeedDisabled(feedEnableBtn, feedEnableBtn.dataset.feedEnableUrl, feedEnableBtn.dataset.folderId, false);
        return;
      }

      const folderBtn = e.target.closest('[data-settings-folder-id]');
      if (folderBtn) { openFolderPropertiesModal(Number(folderBtn.dataset.settingsFolderId)); return; }

      if (e.target.closest('#backfill-hide-shorts-btn')) {
        const btn = document.getElementById('backfill-hide-shorts-btn');
        btn.disabled = true;
        btn.textContent = 'Cleaning…';
        fetch('/feeds/backfill-hide-shorts', { method: 'POST', credentials: 'same-origin' })
          .then(r => r.json())
          .then(d => {
            btn.disabled = false;
            btn.textContent = 'Clean up Shorts';
            showToastMessage(d.marked > 0 ? `Marked ${d.marked} Short${d.marked !== 1 ? 's' : ''} as read.` : 'No unread Shorts found.');
          })
          .catch(() => {
            btn.disabled = false;
            btn.textContent = 'Clean up Shorts';
            showToastMessage('Error running Shorts cleanup.');
          });
        return;
      }

      if (e.target.closest('#fix-url-titles-btn')) {
        const btn = document.getElementById('fix-url-titles-btn');
        btn.disabled = true;
        btn.textContent = 'Scanning…';
        fetch('/feeds/fix-url-titles', { method: 'POST', credentials: 'same-origin' })
          .then(r => r.json())
          .then(d => {
            btn.disabled = false;
            btn.textContent = 'Fix URL titles';
            if (d.queued > 0) {
              showToastMessage(`Queued ${d.queued} feed${d.queued !== 1 ? 's' : ''} for title refresh.`);
            } else {
              showToastMessage('No feeds with URL-style titles found.');
            }
          })
          .catch(() => {
            btn.disabled = false;
            btn.textContent = 'Fix URL titles';
            showToastMessage('Error running title fix.');
          });
        return;
      }
    });

    // Feeds tab: inline per-folder refresh cadence dropdowns.
    document.getElementById('settings-tab-feeds')?.addEventListener('change', async (e) => {
      const sel = e.target.closest('.settings-folder-cadence-select');
      if (!sel) return;
      const folderId = sel.dataset.folderId;
      if (!folderId) return;
      sel.disabled = true;
      try {
        const body = new URLSearchParams({ folder_id: folderId, cadence_minutes: sel.value });
        const resp = await fetch('/folders/cadence', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, credentials: 'same-origin', body: body.toString() });
        const json = await resp.json();
        if (!json.ok) throw new Error(json.error || 'save failed');
        sel.dataset.prevValue = sel.value;
        // Keep the folder-properties modal cadence select in sync if it's showing this folder.
        const modalSel = document.getElementById('folder-prop-cadence');
        if (modalSel && modalSel.dataset.folderId === String(folderId)) modalSel.value = sel.value;
        showToastMessage('Refresh frequency updated.');
      } catch (err) {
        showToastMessage(`Error: ${err.message || err}`);
        if (sel.dataset.prevValue !== undefined) sel.value = sel.dataset.prevValue;
      } finally {
        sel.disabled = false;
      }
    });

    // Feeds tab: live filter for the Folders list (folders + their feeds).
    const folderSearchInput = document.getElementById('feeds-folder-search-input');
    if (folderSearchInput) {
      let _folderSearchTimer = null;
      const applyFolderFilter = () => {
        const q = folderSearchInput.value.trim().toLowerCase();
        const table = document.querySelector('#settings-tab-feeds [data-feeds-panel="folders"] .settings-folders-table');
        if (!table) return;
        let anyVisible = false;
        table.querySelectorAll('.settings-folder-row').forEach(folderRow => {
          const fid = folderRow.dataset.folderRow;
          const feedRows = table.querySelectorAll(`.settings-feed-row[data-folder-feeds="${CSS.escape(fid)}"]`);
          const toggle = folderRow.querySelector('[data-folder-toggle]');
          if (!q) {
            folderRow.hidden = false;
            feedRows.forEach(fr => { fr.hidden = true; });
            if (toggle) toggle.setAttribute('aria-expanded', 'false');
            anyVisible = true;
            return;
          }
          // Match on folder name OR feed name OR feed URL (per the "Filter
          // folders and feeds…" copy). A folder-name match reveals the folder
          // with all its feeds; otherwise show only the feeds that match.
          const nameEl = folderRow.querySelector('.settings-folder-name-text');
          const folderMatch = !!nameEl && nameEl.textContent.toLowerCase().includes(q);
          let anyFeedMatch = false;
          feedRows.forEach(fr => {
            const t = fr.querySelector('.settings-feed-name-text');
            const url = (t && t.getAttribute('data-feed-properties-url')) || '';
            const feedMatch = folderMatch
              || (!!t && t.textContent.toLowerCase().includes(q))
              || url.toLowerCase().includes(q);
            fr.hidden = !feedMatch;
            if (feedMatch) anyFeedMatch = true;
          });
          const showFolder = folderMatch || anyFeedMatch;
          folderRow.hidden = !showFolder;
          if (showFolder) anyVisible = true;
          if (toggle) toggle.setAttribute('aria-expanded', String(anyFeedMatch));
        });
        const panel = table.parentElement;
        let empty = panel.querySelector('.feeds-folder-search-empty');
        if (q && !anyVisible) {
          if (!empty) {
            empty = document.createElement('p');
            empty.className = 'muted feeds-folder-search-empty';
            panel.appendChild(empty);
          }
          empty.textContent = `No folders or feeds match “${folderSearchInput.value.trim()}”.`;
          empty.hidden = false;
        } else if (empty) {
          empty.hidden = true;
        }
      };
      folderSearchInput.addEventListener('input', () => {
        if (_folderSearchTimer) clearTimeout(_folderSearchTimer);
        _folderSearchTimer = setTimeout(applyFolderFilter, 200);
      });
    }

    // Feeds tab: view switching (Folders / Failing / Deactivated / Duplicates).
    const feedsTab = document.getElementById('settings-tab-feeds');
    feedsTab?.querySelectorAll('[data-feeds-view]').forEach(btn => {
      btn.addEventListener('click', () => {
        const view = btn.getAttribute('data-feeds-view');
        feedsTab.querySelectorAll('[data-feeds-view]').forEach(b => {
          const active = b.getAttribute('data-feeds-view') === view;
          b.classList.toggle('feeds-tab-btn--active', active);
          b.setAttribute('aria-selected', String(active));
        });
        feedsTab.querySelectorAll('[data-feeds-panel]').forEach(panel => {
          panel.hidden = panel.getAttribute('data-feeds-panel') !== view;
        });
        if (view === 'stale') void window._loadLazySettingsPanel?.(document.getElementById('settings-stale-lazy'));
        if (view === 'failing') void markProblematicFeedsViewed();
      });
    });

    // Integrations subtabs (YouTube / DeviantArt / Instapaper) — same toggle pattern.
    const intTab = document.getElementById('settings-tab-integrations');
    intTab?.querySelectorAll('[data-int-view]').forEach(btn => {
      btn.addEventListener('click', () => {
        const view = btn.getAttribute('data-int-view');
        intTab.querySelectorAll('[data-int-view]').forEach(b => {
          const active = b.getAttribute('data-int-view') === view;
          b.classList.toggle('feeds-tab-btn--active', active);
          b.setAttribute('aria-selected', String(active));
        });
        intTab.querySelectorAll('[data-int-panel]').forEach(panel => {
          panel.hidden = panel.getAttribute('data-int-panel') !== view;
        });
      });
    });

    // Platform-migrator subtabs in Import/Export tab.
    const ieTab = document.getElementById('settings-tab-importexport');
    ieTab?.querySelectorAll('[data-migrator-view]').forEach(btn => {
      btn.addEventListener('click', () => {
        const view = btn.getAttribute('data-migrator-view');
        ieTab.querySelectorAll('[data-migrator-view]').forEach(b => {
          const active = b.getAttribute('data-migrator-view') === view;
          b.classList.toggle('feeds-tab-btn--active', active);
          b.setAttribute('aria-selected', String(active));
        });
        ieTab.querySelectorAll('[data-migrator-panel]').forEach(panel => {
          panel.hidden = panel.getAttribute('data-migrator-panel') !== view;
        });
      });
    });

    function problemFeedAckRequest(feedUrl, endpoint, headerVal) {
      const body = new URLSearchParams({ feed_url: feedUrl });
      return fetch(endpoint, {
        method: 'POST',
        headers: {
          'X-Requested-With': headerVal,
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        credentials: 'same-origin',
        body: body.toString(),
      });
    }

    function updateProblemTabCount(panel, delta) {
      const viewKey = panel === 'acked' ? 'ok' : panel;
      const btn = document.querySelector(`#settings-tab-feeds [data-feeds-view="${viewKey}"]`);
      if (!btn) return;
      let countEl = btn.querySelector('.feeds-tab-count');
      const current = countEl ? parseInt(countEl.textContent, 10) || 0 : 0;
      const next = current + delta;
      if (next <= 0) {
        if (countEl) countEl.remove();
      } else {
        if (!countEl) {
          countEl = document.createElement('span');
          countEl.className = 'feeds-tab-count';
          btn.appendChild(document.createTextNode(' '));
          btn.appendChild(countEl);
        }
        countEl.textContent = String(next);
      }
    }

    function ensureEmptyMessage(panel, emptyText) {
      const list = panel.querySelector('.problem-feed-list');
      if (list && !list.querySelector('.problem-feed-item')) {
        const p = document.createElement('p');
        p.className = 'muted';
        p.textContent = emptyText;
        list.parentNode.replaceChild(p, list);
      } else if (!list && !panel.querySelector('.muted')) {
        const p = document.createElement('p');
        p.className = 'muted';
        p.textContent = emptyText;
        panel.insertBefore(p, panel.firstChild);
      }
    }

    // Problematic-feeds modal: wire feed-title button to Feed Properties (stacks
    // on top of the problem-feeds modal) and trash icon to the unsubscribe form.
    document.addEventListener('click', (event) => {
      const propsTrigger = event.target.closest('[data-problem-feed-properties]');
      if (propsTrigger) {
        event.preventDefault();
        const url = propsTrigger.getAttribute('data-feed-url');
        if (url) openFeedPropertiesModal(url);
        return;
      }

      const ackTrigger = event.target.closest('[data-problem-feed-ack]');
      if (ackTrigger) {
        event.preventDefault();
        const url = ackTrigger.getAttribute('data-feed-url');
        if (!url) return;
        const modal = document.getElementById('problematic-feeds-modal');
        const failingPanel = modal?.querySelector('[data-problem-panel="failing"]');
        const ackedPanel = modal?.querySelector('[data-problem-panel="acked"]');
        const li = ackTrigger.closest('.problem-feed-item');
        ackTrigger.disabled = true;
        problemFeedAckRequest(url, '/settings/problematic-feeds/acknowledge', 'lectio-problem-feed-ack')
          .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); })
          .then(() => {
            if (li && ackedPanel) {
              // Swap ack button for unack button.
              ackTrigger.setAttribute('data-problem-feed-unack', '');
              ackTrigger.removeAttribute('data-problem-feed-ack');
              ackTrigger.title = 'Move back to Failing';
              ackTrigger.setAttribute('aria-label', 'Move back to Failing');
              const icon = ackTrigger.querySelector('.material-symbols-rounded');
              if (icon) icon.textContent = 'undo';
              ackTrigger.disabled = false;
              // Reveal the "Checked OK" divider on first ack.
              const divider = ackedPanel.previousElementSibling;
              if (divider?.classList.contains('problem-feed-section-divider')) divider.removeAttribute('hidden');
              // Move the item into the acked panel list, creating a list if needed.
              let ackedList = ackedPanel.querySelector('.problem-feed-list');
              if (!ackedList) {
                ackedList = document.createElement('ul');
                ackedList.className = 'problem-feed-list';
                ackedPanel.appendChild(ackedList);
              }
              ackedList.appendChild(li);
              ensureEmptyMessage(failingPanel, 'No failing feeds right now.');
              if (modal) updateProblemTabCount('failing', -1);
            }
          })
          .catch(() => { ackTrigger.disabled = false; });
        return;
      }

      const unackTrigger = event.target.closest('[data-problem-feed-unack]');
      if (unackTrigger) {
        event.preventDefault();
        const url = unackTrigger.getAttribute('data-feed-url');
        if (!url) return;
        const modal = document.getElementById('problematic-feeds-modal');
        const failingPanel = modal?.querySelector('[data-problem-panel="failing"]');
        const ackedPanel = modal?.querySelector('[data-problem-panel="acked"]');
        const li = unackTrigger.closest('.problem-feed-item');
        unackTrigger.disabled = true;
        problemFeedAckRequest(url, '/settings/problematic-feeds/unacknowledge', 'lectio-problem-feed-unack')
          .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); })
          .then(() => {
            if (li && failingPanel) {
              // Swap unack button back to ack button.
              unackTrigger.setAttribute('data-problem-feed-ack', '');
              unackTrigger.removeAttribute('data-problem-feed-unack');
              unackTrigger.title = 'Mark as manually checked OK (e.g. bot-blocked)';
              unackTrigger.setAttribute('aria-label', 'Mark as checked OK');
              const icon = unackTrigger.querySelector('.material-symbols-rounded');
              if (icon) icon.textContent = 'check_circle';
              unackTrigger.disabled = false;
              // Move item back to failing panel.
              let failingList = failingPanel.querySelector('.problem-feed-list');
              if (!failingList) {
                failingList = document.createElement('ul');
                failingList.className = 'problem-feed-list';
                const emptyMsg = failingPanel.querySelector('.muted');
                if (emptyMsg) emptyMsg.replaceWith(failingList);
                else failingPanel.insertBefore(failingList, failingPanel.firstChild);
              }
              failingList.appendChild(li);
              // Hide "Checked OK" divider when acked list becomes empty.
              if (!ackedPanel.querySelector('.problem-feed-item')) {
                const divider = ackedPanel.previousElementSibling;
                if (divider?.classList.contains('problem-feed-section-divider')) divider.setAttribute('hidden', '');
              }
              if (modal) updateProblemTabCount('failing', 1);
            }
          })
          .catch(() => { unackTrigger.disabled = false; });
        return;
      }

      const unsubTrigger = event.target.closest('[data-problem-feed-unsubscribe]');
      if (unsubTrigger) {
        event.preventDefault();
        const url = unsubTrigger.getAttribute('data-feed-url');
        const folderId = unsubTrigger.getAttribute('data-folder-id');
        if (!url || !folderId) return;

        const li = unsubTrigger.closest('.problem-feed-item');
        // Capture the panel before the helper removes the row, so we can update
        // its count/empty-state afterward.
        const isAcked = li?.closest('[data-problem-panel="acked"]');
        const panelKey = isAcked ? 'acked' : 'failing';
        const emptyText = isAcked ? 'No feeds marked as checked OK.' : 'No failing feeds right now.';
        unsubTrigger.disabled = true;

        unsubscribeFeedInteractive(url, folderId, { title: url }).then((done) => {
          if (!done) { unsubTrigger.disabled = false; return; }
          if (li) li.style.opacity = '0.4';
          const modal = document.getElementById('problematic-feeds-modal');
          if (modal) {
            updateProblemTabCount(panelKey, -1);
            const panel = modal.querySelector(`[data-problem-panel="${panelKey}"]`);
            if (panel) ensureEmptyMessage(panel, emptyText);
          }
        });
      }

      // Settings → Feeds → Folders: trashcan on each feed row.
      const settingsUnsub = event.target.closest('[data-settings-feed-unsubscribe]');
      if (settingsUnsub) {
        event.preventDefault();
        const url = settingsUnsub.getAttribute('data-feed-url');
        const folderId = settingsUnsub.getAttribute('data-folder-id');
        const title = settingsUnsub.getAttribute('data-feed-title') || url;
        if (!url || !folderId) return;

        const tr = settingsUnsub.closest('.settings-feed-row');
        settingsUnsub.disabled = true;
        unsubscribeFeedInteractive(url, folderId, { title }).then((done) => {
          if (done) { if (tr) tr.style.opacity = '0.4'; }
          else settingsUnsub.disabled = false;
        });
      }
    });

    /**
     * Build a DocumentFragment with .afd-chip spans for one /feeds/compare result.
     * Returns a fragment containing a .afd-feed-meta div (and, if applicable,
     * an .afd-feed-title-row div).  On error, wraps the error in a .afd-feed-meta
     * with an .afd-chip--bad span.
     * Shared by the Add-Feed picker (compareFeedsPicker) and the Settings compare panel.
     */
    function buildCompareChips(res) {
      const frag = document.createDocumentFragment();
      function chip(text, cls) {
        const s = document.createElement('span');
        s.textContent = text;
        s.className = cls ? 'afd-chip afd-chip--' + cls : 'afd-chip';
        return s;
      }
      if (res.error) {
        const errMeta = document.createElement('div');
        errMeta.className = 'afd-feed-meta';
        const err = document.createElement('span');
        err.className = 'afd-chip afd-chip--bad';
        err.textContent = res.error;
        errMeta.append(err);
        frag.append(errMeta);
        return frag;
      }
      const meta = document.createElement('div');
      meta.className = 'afd-feed-meta';
      const tags = [
        chip(res.format),
        chip(res.full_text ? '✓ Full text' : 'Summaries', res.full_text ? 'good' : null),
        chip(res.image_count > 0 ? '✓ images' : '✗ no images', res.image_count > 0 ? 'good' : 'bad'),
        chip(res.date_field === 'published' ? '✓ pub dates'
           : res.date_field === 'modified_only' ? '⚠ modified-only' : '✗ no dates',
             res.date_field === 'published' ? 'good' : 'bad'),
        chip(res.guid_type === 'url' ? '✓ URL IDs'
           : res.guid_type === 'string' ? 'string IDs' : '✗ no IDs',
             res.guid_type === 'url' ? 'good' : res.guid_type === 'string' ? null : 'bad'),
      ];
      tags.forEach(t => meta.append(t));
      frag.append(meta);
      if (res.sample_title || res.latest_date) {
        const titleRow = document.createElement('div');
        titleRow.className = 'afd-feed-meta afd-feed-title-row';
        if (res.sample_title) {
          const t = res.sample_title.length > 60 ? res.sample_title.slice(0, 60) + '…' : res.sample_title;
          titleRow.append(chip('"' + t + '"', 'title'));
        }
        if (res.latest_date) titleRow.append(chip(res.latest_date));
        frag.append(titleRow);
      }
      return frag;
    }

    // Settings → Feeds → Folders: feed comparison via checkboxes.
    (function () {
      const feedsTab = document.getElementById('settings-tab-feeds');
      if (!feedsTab) return;
      const toolbar = document.getElementById('sfc-toolbar');
      const compareBtn = document.getElementById('sfc-compare-btn');
      const combineBtn = document.getElementById('sfc-combine-btn');
      const countEl = document.getElementById('sfc-count');
      const panel = document.getElementById('sfc-panel');
      if (!toolbar || !compareBtn || !countEl || !panel) return;

      const MAX_COMPARE = 6;

      // Snapshot the currently-checked feeds as {url, name}, deduped by URL.
      // URL is read from the row (single source of truth: data-feed-url), not a
      // separate checkbox attribute, so the two can't drift.
      function getSelectedFeeds() {
        const byUrl = new Map();
        feedsTab.querySelectorAll('.sfc-check:checked').forEach(cb => {
          const r = cb.closest('.settings-feed-row');
          const u = r?.dataset.feedUrl;
          if (!u || byUrl.has(u)) return;
          const name = r.querySelector('.settings-feed-name-text')?.textContent?.trim() || u;
          byUrl.set(u, name);
        });
        return [...byUrl.entries()].map(([url, name]) => ({ url, name }));
      }

      function updateToolbar() {
        const n = getSelectedFeeds().length;
        toolbar.hidden = n === 0;
        compareBtn.disabled = n < 2;
        if (combineBtn) combineBtn.disabled = n < 2;
        if (n === 0) {
          countEl.textContent = '';
        } else if (n > MAX_COMPARE) {
          countEl.textContent = `${n} selected — max ${MAX_COMPARE}`;
          compareBtn.disabled = true;
        } else {
          countEl.textContent = `${n} selected`;
        }
      }

      // Listen for checkbox changes anywhere in the feeds tab (covers dynamically
      // shown rows after folder expand).
      function syncFolderCheck(folderId) {
        const boxes = [...feedsTab.querySelectorAll(`.settings-feed-row[data-folder-feeds="${folderId}"] .sfc-check`)];
        const folderCb = feedsTab.querySelector(`.sfc-check-all[data-folder-check="${folderId}"]`);
        if (!folderCb) return;
        const checked = boxes.filter(b => b.checked).length;
        folderCb.checked = boxes.length > 0 && checked === boxes.length;
        folderCb.indeterminate = checked > 0 && checked < boxes.length;
      }

      feedsTab.addEventListener('change', (e) => {
        if (e.target.classList.contains('sfc-check-all')) {
          // Folder-level select-all: (un)check every feed in that folder, even
          // when the folder is collapsed (the checkboxes exist in the DOM).
          const fid = e.target.dataset.folderCheck;
          feedsTab.querySelectorAll(`.settings-feed-row[data-folder-feeds="${fid}"] .sfc-check`)
            .forEach(cb => { cb.checked = e.target.checked; });
          e.target.indeterminate = false;
          updateToolbar();
          panel.hidden = true;
        } else if (e.target.classList.contains('sfc-check')) {
          updateToolbar();
          const row = e.target.closest('.settings-feed-row');
          if (row) syncFolderCheck(row.dataset.folderFeeds);
          // Collapse the panel when selection changes so it does not show stale results.
          panel.hidden = true;
        }
      });

      // ---- Bulk actions on the selected feeds ----
      const moveSelect = document.getElementById('sfc-move-folder');
      const _ACTION_LABEL = { move: 'moved', disable: 'disabled', enable: 'enabled',
                              unsubscribe: 'unsubscribed', refresh: 'refreshed', 'mark-read': 'marked read' };
      function selectedUrls() { return getSelectedFeeds().map(f => f.url); }
      function removeRowsFor(urls) {
        const set = new Set(urls);
        const affectedFolders = new Set();
        feedsTab.querySelectorAll('.settings-feed-row').forEach(r => {
          if (set.has(r.dataset.feedUrl)) {
            if (r.dataset.folderFeeds) affectedFolders.add(r.dataset.folderFeeds);
            r.remove();
          }
        });
        // Keep each affected folder's select-all checkbox (checked/indeterminate)
        // consistent with the rows that remain.
        affectedFolders.forEach(fid => syncFolderCheck(fid));
        updateToolbar();
      }
      function setRowsDisabled(urls, disabled) {
        const set = new Set(urls);
        feedsTab.querySelectorAll('.settings-feed-row').forEach(r => {
          if (set.has(r.dataset.feedUrl)) r.classList.toggle('settings-feed-row--disabled', disabled);
        });
      }
      async function bulkAction(action, extra) {
        const urls = selectedUrls();
        if (!urls.length) return null;
        const body = new URLSearchParams();
        body.set('action', action);
        body.set('feed_urls', urls.join('\n'));
        for (const [k, v] of Object.entries(extra || {})) body.set(k, v);
        const controls = toolbar.querySelectorAll('button, select');
        controls.forEach(c => c.disabled = true);
        try {
          const resp = await fetch('/feeds/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-bulk' },
            credentials: 'same-origin', body: body.toString(),
          });
          const data = await resp.json().catch(() => ({}));
          if (!data.ok) { alert(data.error || 'Action failed.'); return null; }
          countEl.textContent = `${data.count} ${_ACTION_LABEL[action] || action}`;
          return data;
        } catch (e) { alert('Action failed: ' + e); return null; }
        finally {
          controls.forEach(c => c.disabled = false);
          compareBtn.disabled = getSelectedFeeds().length < 2;
        }
      }
      moveSelect?.addEventListener('change', async () => {
        if (!moveSelect.value) return;
        const urls = selectedUrls();
        const data = await bulkAction('move', { to_folder_id: moveSelect.value });
        moveSelect.value = '';
        if (data) removeRowsFor(urls);
      });
      document.getElementById('sfc-disable-btn')?.addEventListener('click', async () => {
        const urls = selectedUrls();
        if (await bulkAction('disable')) setRowsDisabled(urls, true);
      });
      document.getElementById('sfc-enable-btn')?.addEventListener('click', async () => {
        const urls = selectedUrls();
        if (await bulkAction('enable')) setRowsDisabled(urls, false);
      });
      document.getElementById('sfc-mark-read-btn')?.addEventListener('click', () => bulkAction('mark-read'));
      document.getElementById('sfc-refresh-btn')?.addEventListener('click', () => bulkAction('refresh'));
      document.getElementById('sfc-unsub-btn')?.addEventListener('click', async () => {
        const urls = selectedUrls();
        if (!urls.length) return;
        if (!confirm(`Unsubscribe ${urls.length} feed${urls.length === 1 ? '' : 's'}? This permanently deletes them.`)) return;
        if (await bulkAction('unsubscribe')) removeRowsFor(urls);
      });

      // Combine: pick one selected feed as survivor; migrate the others' tags/
      // stars (and optionally unread) onto it, then unsubscribe them.
      combineBtn?.addEventListener('click', () => {
        const selected = getSelectedFeeds();
        if (selected.length < 2) return;
        panel.innerHTML = '';
        const heading = document.createElement('p');
        heading.className = 'sfc-panel-heading';
        heading.textContent = 'Combine feeds — keep which one?';
        panel.append(heading);

        const list = document.createElement('div');
        list.className = 'sfc-combine-list';
        selected.forEach((f, i) => {
          const lbl = document.createElement('label');
          lbl.className = 'sfc-combine-item';
          const rb = document.createElement('input');
          rb.type = 'radio'; rb.name = 'sfc-combine-survivor'; rb.value = f.url;
          if (i === 0) rb.checked = true;
          const sp = document.createElement('span');
          sp.textContent = f.name; sp.title = f.url;
          lbl.append(rb, sp); list.append(lbl);
        });
        panel.append(list);

        const opts = document.createElement('label');
        opts.className = 'sfc-combine-item';
        const unreadCb = document.createElement('input');
        unreadCb.type = 'checkbox'; unreadCb.id = 'sfc-combine-unread';
        opts.append(unreadCb, document.createTextNode(' Also carry over unread state'));
        panel.append(opts);

        const actions = document.createElement('div');
        actions.className = 'sfc-combine-actions';
        const go = document.createElement('button');
        go.type = 'button'; go.className = 'sfc-action-btn'; go.textContent = `Combine ${selected.length} feeds`;
        actions.append(go); panel.append(actions);
        panel.hidden = false;

        go.addEventListener('click', async () => {
          const survivor = panel.querySelector('input[name="sfc-combine-survivor"]:checked')?.value;
          if (!survivor) return;
          const sources = selected.map(f => f.url).filter(u => u !== survivor);
          const survivorName = selected.find(f => f.url === survivor)?.name || survivor;
          if (!confirm(`Combine ${sources.length} feed${sources.length === 1 ? '' : 's'} into "${survivorName}"? Their stars, tags${unreadCb.checked ? ', and unread items' : ''} move over, then they're unsubscribed.`)) return;
          go.disabled = true; go.textContent = 'Combining…';
          const body = new URLSearchParams();
          body.set('survivor_url', survivor);
          sources.forEach(u => body.append('source_url', u));
          if (unreadCb.checked) body.set('move_unread', '1');
          try {
            const r = await fetch('/feeds/combine', {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-combine' },
              credentials: 'same-origin', body: body.toString(),
            });
            const data = await r.json().catch(() => ({}));
            if (!data.ok) { alert(data.message || 'Combine failed.'); go.disabled = false; go.textContent = `Combine ${selected.length} feeds`; return; }
            removeRowsFor(sources);
            panel.hidden = true; panel.innerHTML = '';
            if (typeof showToastMessage === 'function') showToastMessage(data.message);
          } catch (e) { alert('Combine failed: ' + e); go.disabled = false; }
        });
      });

      compareBtn.addEventListener('click', async () => {
        // Snapshot the selection NOW, before the await. Rendering reads only from
        // this snapshot, so changing checkboxes mid-request can't desync the
        // results (old URL set) from the names shown.
        const selected = getSelectedFeeds();
        if (selected.length < 2 || selected.length > MAX_COMPARE) return;
        const titleByUrl = Object.fromEntries(selected.map(f => [f.url, f.name]));
        const urls = selected.map(f => f.url);
        compareBtn.disabled = true;
        compareBtn.textContent = 'Comparing…';
        panel.hidden = true;
        try {
          const qs = urls.map(u => 'url=' + encodeURIComponent(u)).join('&');
          const r = await fetch('/feeds/compare?' + qs);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const results = await r.json();

          // Build panel content (names come from the click-time snapshot).
          panel.innerHTML = '';
          const heading = document.createElement('p');
          heading.className = 'sfc-panel-heading';
          heading.textContent = 'Feed comparison';
          panel.append(heading);

          results.forEach(res => {
            const card = document.createElement('div');
            card.className = 'sfc-card';
            const nameEl = document.createElement('div');
            nameEl.className = 'sfc-card-name';
            nameEl.textContent = titleByUrl[res.url] || res.url;
            nameEl.title = res.url;
            card.append(nameEl);
            card.append(buildCompareChips(res));
            panel.append(card);
          });

          panel.hidden = false;
        } catch (err) {
          panel.innerHTML = '';
          const msg = document.createElement('p');
          msg.className = 'sfc-panel-error';
          msg.textContent = `Compare failed: ${err.message || err}`;
          panel.append(msg);
          panel.hidden = false;
        } finally {
          compareBtn.disabled = false;
          compareBtn.textContent = 'Compare selected';
          updateToolbar();
        }
      });
    })();

    rootAddFolderButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!contextFolderNameInput || !addFolderForm) {
        return;
      }
      hideAllContextMenus();
      openActionInputModal({
        title: 'Add Folder',
        label: 'Folder name',
        placeholder: 'New folder name',
        submitLabel: 'Add Folder',
        onSubmit: (folderName) => {
          contextFolderNameInput.value = folderName;
          addFolderForm.submit();
        },
      });
    });

    rootRefreshButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!contextFolderId || !refreshFolderIdInput || !refreshFolderForm) {
        return;
      }

      refreshFolderIdInput.value = contextFolderId;
      hideAllContextMenus();
      refreshFolderForm.submit();
    });

    rootAddFeedButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      hideAllContextMenus();
      openAddFeedDialog({ folderId: contextFolderId });
    });

    addFeedButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      hideAllContextMenus();
      openAddFeedDialog({ folderId: contextFolderId });
    });

    renameFolderButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!contextFolderId || !renameFolderIdInput || !renameFolderNameInput || !renameFolderForm || contextFolderDepth === 0) {
        return;
      }
      const folderId = contextFolderId;
      const currentName = contextFolderName;
      hideAllContextMenus();
      openActionInputModal({
        title: 'Rename Folder',
        label: 'Folder name',
        placeholder: 'New folder name',
        submitLabel: 'Rename',
        initialValue: currentName,
        onSubmit: (newName) => {
          renameFolderIdInput.value = folderId;
          renameFolderNameInput.value = newName;
          renameFolderForm.submit();
        },
      });
    });

    const deleteFolderModal = document.getElementById('delete-folder-modal');
    const deleteFolderMessage = document.getElementById('delete-folder-message');
    const deleteFolderTargetSelect = document.getElementById('delete-folder-target');
    const deleteFolderConfirm = document.getElementById('delete-folder-confirm');
    const deleteFolderFeedActionInput = document.getElementById('context-delete-folder-feed-action');
    const deleteFolderMoveToInput = document.getElementById('context-delete-folder-move-to');

    // Enable the target <select> only when the "move" radio is chosen.
    if (deleteFolderModal) {
      for (const radio of deleteFolderModal.querySelectorAll('input[name="delete-folder-action"]')) {
        radio.addEventListener('change', () => {
          if (deleteFolderTargetSelect) {
            deleteFolderTargetSelect.disabled =
              deleteFolderModal.querySelector('input[name="delete-folder-action"]:checked')?.value !== 'move';
          }
        });
      }
    }

    function submitFolderDeletion(feedAction, moveToFolderId) {
      if (!deleteFolderIdInput || !deleteFolderForm) {
        return;
      }
      deleteFolderIdInput.value = contextFolderId;
      if (deleteFolderFeedActionInput) {
        deleteFolderFeedActionInput.value = feedAction;
      }
      if (deleteFolderMoveToInput) {
        deleteFolderMoveToInput.value = feedAction === 'move' ? moveToFolderId : '';
      }
      hideAllContextMenus();
      deleteFolderForm.submit();
    }

    deleteFolderButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!contextFolderId || !deleteFolderIdInput || !deleteFolderForm || contextFolderDepth === 0) {
        return;
      }

      // Count feeds that live in this folder's subtree.
      const feedCount = document.querySelectorAll(
        `#folder-feeds-${contextFolderId} .tree-feed-item`
      ).length;

      // Empty folder: keep the simple confirm.
      if (feedCount === 0 || !deleteFolderModal) {
        const confirmed = window.confirm(`Delete folder "${contextFolderName}"?`);
        if (!confirmed) {
          hideContextMenu();
          return;
        }
        submitFolderDeletion('unsub', '');
        return;
      }

      // Non-empty: ask what to do with the feeds.
      if (deleteFolderMessage) {
        deleteFolderMessage.textContent =
          `"${contextFolderName}" contains ${feedCount} feed(s). What should happen to them?`;
      }
      // Reset to defaults and hide the folder being deleted from the move target.
      const unsubRadio = deleteFolderModal.querySelector('input[name="delete-folder-action"][value="unsub"]');
      if (unsubRadio) unsubRadio.checked = true;
      if (deleteFolderTargetSelect) {
        deleteFolderTargetSelect.disabled = true;
        for (const opt of deleteFolderTargetSelect.options) {
          opt.hidden = opt.value === String(contextFolderId);
        }
      }
      hideAllContextMenus();
      deleteFolderModal.removeAttribute('hidden');
    });

    deleteFolderConfirm?.addEventListener('click', () => {
      const action = deleteFolderModal?.querySelector('input[name="delete-folder-action"]:checked')?.value || 'unsub';
      const moveTo = deleteFolderTargetSelect?.value || '-1';
      deleteFolderModal?.setAttribute('hidden', '');
      submitFolderDeletion(action, moveTo);
    });

    youtubeSyncButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!contextFolderId || !youtubeSyncFolderIdInput || !youtubeSyncForm) {
        return;
      }
      youtubeSyncFolderIdInput.value = contextFolderId;
      hideAllContextMenus();
      youtubeSyncForm.submit();
    });

    disableFeedButton?.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!contextFeedUrl || !disableFeedFolderIdInput || !disableFeedUrlInput || !disableFeedForm) {
        return;
      }
      disableFeedFolderIdInput.value = contextFolderId || '';
      disableFeedUrlInput.value = contextFeedUrl;
      hideAllContextMenus();
      disableFeedForm.submit();
    });

    let postsContainer = null;
    let postsChunkSentinel = null;
    let postsChunkLoading = false;

    function refreshPostChunkRefs() {
      postsContainer = document.querySelector('.posts');
      postsChunkSentinel = document.getElementById('posts-chunk-sentinel');
    }

    function setupPostChunks() {
      refreshPostChunkRefs();
      if (!postsContainer) {
        return;
      }
      if (postsContainer.dataset.chunkBound === '1') {
        return;
      }
      postsContainer.dataset.chunkBound = '1';

      // In single-pane mode .pane-posts scrolls; in multi-pane .posts scrolls.
      const scrollEl = (window.isSingleMode && window.isSingleMode())
        ? (document.querySelector('.pane-posts') || postsContainer)
        : postsContainer;

      const chunkSize = Number.parseInt(postsContainer.getAttribute('data-chunk-size') || '10', 10) || 10;
      let visibleCount = chunkSize;

      function getPostItems() {
        return Array.from(postsContainer.querySelectorAll('.post-item'));
      }

      function applyVisibleWindow() {
        const items = getPostItems();
        const boundedVisibleCount = Math.min(Math.max(visibleCount, chunkSize), items.length || chunkSize);
        visibleCount = boundedVisibleCount;
        items.forEach((item, index) => {
          if (index < visibleCount) {
            item.classList.remove('post-item-hidden');
          } else {
            item.classList.add('post-item-hidden');
          }
        });
      }

      function revealNextChunk() {
        const items = getPostItems();
        // An empty list has no further chunks: chunk 1 is served pre-filtered,
        // so zero items means either a scope-tab landing (no posts by design —
        // chunk-fetching the bare URL here used to resurrect the all-feeds
        // load) or a genuinely empty view.
        if (items.length === 0) return;
        // If we've already revealed all items currently loaded, request
        // the next server-side chunk (by bumping the `chunk` query param).
        if (visibleCount >= items.length) {
          if (postsChunkLoading) return;
          try {
            const url = new URL(normalizeScopeUrl(activeScopeUrl || window.location.href), window.location.origin);
            const next = Math.max(1, Math.floor(items.length / chunkSize) + 1);
            url.searchParams.set('chunk', String(next));
            // Ask the server to return only the delta items for this chunk
            // so the client can append a fixed-size batch instead of a
            // cumulative list which may contain many items.
            url.searchParams.set('chunk_delta', '1');
            // Ensure the current list filter is included when requesting more.
            if (!url.searchParams.has('read_filter')) {
              const currentReadFilter = document.querySelector('.posts-search-form input[name="read_filter"]')?.value;
              if (currentReadFilter) url.searchParams.set('read_filter', currentReadFilter);
            }
            if (!url.searchParams.has('resume_read_filter')) {
              const currentResumeReadFilter = document.querySelector('.posts-search-form input[name="resume_read_filter"]')?.value;
              if (currentResumeReadFilter) url.searchParams.set('resume_read_filter', currentResumeReadFilter);
            }
            if (!url.searchParams.has('star_only')) {
              const currentStarOnly = document.querySelector('.posts-search-form input[name="star_only"]')?.value;
              if (currentStarOnly) url.searchParams.set('star_only', currentStarOnly);
            }
            if (!url.searchParams.has('q')) {
              const currentQuery = document.querySelector('#topbar-search-input')?.value?.trim();
              if (currentQuery) url.searchParams.set('q', currentQuery);
            }
            postsChunkLoading = true;
            // Avoid pushing a history entry for incremental loads
            return loadScopePanesWithoutFullRefresh(url.toString(), false).finally(() => {
              postsChunkLoading = false;
            });
          } catch (e) {
            // fallback to doing nothing
            return;
          }
        }

        visibleCount = Math.min(visibleCount + chunkSize, items.length);
        applyVisibleWindow();
      }

      async function ensureViewportFilled() {
        // Keep revealing chunks until the list can scroll, capped to avoid loops.
        for (let i = 0; i < 6; i++) {
          if (!postsContainer) {
            return;
          }

          const remaining = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
          if (remaining > 24) {
            return;
          }

          const beforeVisible = postsContainer.querySelectorAll('.post-item:not(.post-item-hidden)').length;
          const beforeTotal = postsContainer.querySelectorAll('.post-item').length;
          await Promise.resolve(revealNextChunk());
          await new Promise((resolve) => window.requestAnimationFrame(resolve));

          const afterVisible = postsContainer.querySelectorAll('.post-item:not(.post-item-hidden)').length;
          const afterTotal = postsContainer.querySelectorAll('.post-item').length;
          if (afterVisible === beforeVisible && afterTotal === beforeTotal) {
            return;
          }
        }
      }

      function maybeRevealOnScroll() {
        if (!postsContainer) {
          return;
        }

        const remaining = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
        if (remaining < 180) {
          revealNextChunk();
        }
      }

      scrollEl.addEventListener('scroll', maybeRevealOnScroll);
      postsContainer._revealNextChunk = revealNextChunk;

      // If items are removed from the top (e.g., unread view interactions), keep the window filled.
      const mutationObserver = new MutationObserver(() => {
        applyVisibleWindow();
        ensureViewportFilled();
      });
      mutationObserver.observe(postsContainer, { childList: true, subtree: false });

      window.addEventListener('resize', ensureViewportFilled, { passive: true });

      applyVisibleWindow();
      ensureViewportFilled();
    }

    setupPostChunks();

    function centerActivePostInView() {
      refreshPostChunkRefs();
      const activePostItem = postsContainer?.querySelector('.post-item.active');
      if (!postsContainer || !activePostItem) {
        return;
      }
      window.requestAnimationFrame(() => {
        const activeTop = activePostItem.offsetTop;
        const activeBottom = activeTop + activePostItem.offsetHeight;
        const viewTop = postsContainer.scrollTop;
        const viewBottom = viewTop + postsContainer.clientHeight;
        if (activeTop >= viewTop + 8 && activeBottom <= viewBottom - 8) {
          return;
        }

        const centeredTop = activeTop - Math.max(0, (postsContainer.clientHeight - activePostItem.offsetHeight) / 2);
        postsContainer.scrollTop = Math.max(0, centeredTop);
      });
    }

    centerActivePostInView();

    function activateSourceView(iframeUrl, triggerButton, forcedMode = null) {
      const sourceUrl = triggerButton.getAttribute('data-source-url');
      sourceViewMode = forcedMode || (triggerButton === entrySourceButton ? 'source' : 'readability');
      setSourceModeIndicator(sourceViewMode);
      sourceViewUrl = sourceUrl || '';
      sourceFallbackAttempted = sourceViewMode !== 'source';
      sourceReadabilityAttempted = sourceViewMode === 'readability';
      sourceDirectLoaded = false;
      sourceFrameLoaded = false;
      if (sourceViewMode === 'source-proxy') {
        // Use fetch+srcdoc so session cookies are sent and the browser's
        // iframe-level Sec-Fetch-Dest filtering doesn't block the same-origin URL.
        entrySourceFrame.removeAttribute('src');
        entrySourceFrame.srcdoc = '';
        fetchAndInjectProxy(sourceViewUrl);
      } else if (entrySourceFrame.src !== iframeUrl) {
        entrySourceFrame.src = safeHttpUrl(iframeUrl);
      } else {
        sourceFrameLoaded = true;
      }
      if (entrySourceOpenExternal) {
        entrySourceOpenExternal.href = safeHttpUrl(sourceUrl);
      }
      entryBody.setAttribute('hidden', '');
      if (entryReadabilityContainer) {
        entryReadabilityContainer.setAttribute('hidden', '');
        entryReadabilityContainer.innerHTML = '';
      }
      entrySourceFrame.removeAttribute('hidden');
      // When activating source view in single-mode ensure we switch to the
      // entry single-pane level so the iframe becomes visible.
      try { if (window.isSingleMode && window.isSingleMode()) setSinglePaneLevel(2); } catch (e) {}
      // Make sure iframe sits above other content and the article has a
      // minimum height so absolute positioning has space to render.
      try {
        entrySourceFrame.style.zIndex = '3';
        entrySourceFrame.style.display = 'block';
        // Allow the entry article to flex-grow to fill the pane.
        entryArticle.style.minHeight = '0';
        entryArticle.style.flex = '1 1 auto';
        entryArticle.style.height = '100%';
      } catch (e) {}
      // Make the pane container itself non-scrolling so the iframe becomes the
      // single scrollable area and avoids double scrollbars.
      try {
        const paneEntry = document.querySelector('.pane-entry');
        if (paneEntry) {
          _prevPaneEntryOverflow = paneEntry.style.overflow || getComputedStyle(paneEntry).overflow;
          paneEntry.style.overflow = 'hidden';
        }
        if (entrySourceFrame) entrySourceFrame.style.overflow = 'auto';
      } catch (e) {}
      entryArticle?.classList.add('source-active');
      entryReadabilityButton?.classList.remove('active');
      entrySourceButton?.classList.remove('active');
      triggerButton.classList.add('active');
      setEntrySourceFallbackVisible(false);
      scheduleSourceLoadTimeout(3000);
      // Force a layout reflow to ensure iframe fills the entry area.
      window.setTimeout(ensureSourceFrameFills, 40);
      requestAnimationFrame(ensureSourceFrameFills);
      sourceViewActive = true;
      // Kick off a health check: if iframe remains empty or inaccessible,
      // attempt proxied source or show fallback.
      if (sourceHealthCheckId) { window.clearTimeout(sourceHealthCheckId); sourceHealthCheckId = null; }
      sourceHealthCheckId = window.setTimeout(() => {
        try {
          if (!sourceViewActive) return;
          // Even if load event fired, a blocked iframe may appear blank.
          // If we can read the document and it's empty, fall back to proxy.
          if (sourceFrameLoaded && sourceViewMode === 'source' && !sourceFallbackAttempted) {
            try {
              const doc = entrySourceFrame.contentDocument;
              if (doc && doc.body && doc.body.innerText.trim().length < 5 &&
                  !doc.body.querySelector('img,video,canvas,svg,iframe')) {
                sourceFrameLoaded = false;
                fallbackToProxiedSource();
                return;
              }
            } catch (_e) { /* cross-origin: assume loaded fine */ }
            return;
          }
          // If already loaded fine, nothing to do.
          if (sourceFrameLoaded) return;
          // If source mode, try proxied source as a fallback.
          if (sourceViewMode === 'source' && !sourceFallbackAttempted && sourceViewUrl) {
            fallbackToProxiedSource();
            return;
          }
          // In proxy mode the frame is still loading — let the load timeout handle it.
          if (sourceViewMode === 'source-proxy') return;
          // Otherwise show fallback UI.
          setEntrySourceFallbackVisible(true);
        } catch (e) {
          // ignore
        }
      }, 900);
    }

    function deactivateSourceView() {
      frameCheckRequestToken += 1;
      // Hide readability container
      try {
        if (entryReadabilityContainer) {
          entryReadabilityContainer.setAttribute('hidden', '');
          entryReadabilityContainer.innerHTML = '';
        }
      } catch (e) {}
      // Hide iframe via attribute first
      try { entrySourceFrame.setAttribute('hidden', ''); } catch (e) {}
      // Remove any inline styles we applied so the iframe does not stay visible
      try {
        if (entrySourceFrame) {
          entrySourceFrame.style.removeProperty('display');
          entrySourceFrame.style.removeProperty('position');
          entrySourceFrame.style.removeProperty('left');
          entrySourceFrame.style.removeProperty('right');
          entrySourceFrame.style.removeProperty('top');
          entrySourceFrame.style.removeProperty('bottom');
          entrySourceFrame.style.removeProperty('width');
          entrySourceFrame.style.removeProperty('height');
          entrySourceFrame.style.removeProperty('flex');
          entrySourceFrame.style.removeProperty('align-self');
          entrySourceFrame.style.removeProperty('z-index');
          entrySourceFrame.style.removeProperty('overflow');
        }
      } catch (e) {}

      // Restore article layout to normal
      try {
        if (entryArticle) {
          entryArticle.style.removeProperty('min-height');
          entryArticle.style.removeProperty('flex');
          entryArticle.style.removeProperty('height');
          entryArticle.style.removeProperty('position');
        }
      } catch (e) {}

      entryBody?.removeAttribute('hidden');
      entryArticle?.classList.remove('source-active');
      entryReadabilityButton?.classList.remove('active');
      entrySourceButton?.classList.remove('active');
      setEntrySourceFallbackVisible(false);
      if (sourceLoadTimeoutId) {
        window.clearTimeout(sourceLoadTimeoutId);
        sourceLoadTimeoutId = null;
      }
      if (sourceHealthCheckId) { window.clearTimeout(sourceHealthCheckId); sourceHealthCheckId = null; }

      // Restore pane overflow so outer pane can scroll again.
      try {
        const paneEntry = document.querySelector('.pane-entry');
        if (paneEntry) {
          if (_prevPaneEntryOverflow !== null) {
            paneEntry.style.overflow = _prevPaneEntryOverflow;
          } else {
            paneEntry.style.removeProperty('overflow');
          }
        }
      } catch (e) {}

      sourceFrameLoaded = false;
      sourceViewActive = false;
      sourceViewMode = null;
      setSourceModeIndicator(null);
      sourceViewUrl = '';
      sourceFallbackAttempted = false;
      sourceReadabilityAttempted = false;
      sourceDirectLoaded = false;
    }

    document.addEventListener('click', (event) => {
      if (event.defaultPrevented || event.button !== 0) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }

      const link = event.target.closest('a[href]');
      if (!link) {
        return;
      }
      if (link.target && link.target !== '_self') {
        return;
      }
      if (link.hasAttribute('download')) {
        return;
      }
      if (!link.matches('.tree-item, .feed-link, .tag-link, .posts-toolbar a, .entry-feed-link, .entry-tag-link, .menu-popover > a.menu-item')) {
        return;
      }

      const targetUrl = new URL(link.href, window.location.origin);
      if (targetUrl.origin !== window.location.origin || targetUrl.pathname !== '/') {
        return;
      }

      applyCurrentScopeStateToScopeLink(targetUrl, link);

      const activeQuery = (document.getElementById('topbar-search-input')?.value || '').trim();
      if (activeQuery && !targetUrl.searchParams.has('q')) {
        targetUrl.searchParams.set('q', activeQuery);
      }

      event.preventDefault();
      link.closest('.hamburger-menu')?.removeAttribute('open');
      loadScopePanesWithoutFullRefresh(targetUrl.toString()).then(() => {
        try { if (window.isSingleMode && window.isSingleMode()) setSinglePaneLevel(1); } catch(e) {}
      }).catch(() => {});
    });

    const topbarSearchForm = document.querySelector('.posts-search-form');
    topbarSearchForm?.addEventListener('submit', (event) => {
      event.preventDefault();

      const targetUrl = new URL(topbarSearchForm.getAttribute('action') || '/', window.location.origin);
      const formData = new FormData(topbarSearchForm);
      for (const [key, value] of formData.entries()) {
        const valueText = String(value);
        if (key === 'q') {
          const queryText = valueText.trim();
          if (queryText) {
            targetUrl.searchParams.set('q', queryText);
          } else {
            targetUrl.searchParams.delete('q');
          }
          continue;
        }
        targetUrl.searchParams.set(key, valueText);
      }
      // Search always spans All; clearing it restores the pre-search filter
      // (carried in the form's resume_read_filter).
      if (targetUrl.searchParams.get('q')) {
        targetUrl.searchParams.set('read_filter', 'all');
      } else {
        const resume = String(formData.get('resume_read_filter') || 'all');
        targetUrl.searchParams.set('read_filter', resume);
      }

      loadScopePanesWithoutFullRefresh(targetUrl.toString())
        .then(() => {
          if (window.isSingleMode && window.isSingleMode()) {
            setSinglePaneLevel(1);
          }
        })
        .catch(() => {
          window.location.href = targetUrl.toString();
        });
    });

    window.addEventListener('popstate', (event) => {
      const state = event.state || {};
      const currentUrl = window.location.href;
      const currentParams = new URL(currentUrl).searchParams;
      if (window.isSingleMode && window.isSingleMode()) {
        let paneLevel = Number.isInteger(state.lectioPaneLevel) ? state.lectioPaneLevel : null;
        if (paneLevel === null) {
          try {
            const popUrl = new URL(currentUrl, window.location.origin);
            const hasEntry = popUrl.searchParams.has('feed_url') && popUrl.searchParams.has('entry_id');
            const hasScope = popUrl.searchParams.has('folder_id')
              || popUrl.searchParams.has('list_feed_url')
              || popUrl.searchParams.has('tag')
              || popUrl.searchParams.has('q');
            paneLevel = hasEntry ? 2 : (hasScope ? 1 : 0);
          } catch (_e) {
            paneLevel = 0;
          }
        }
        setSinglePaneLevel(paneLevel);
      }

      if (!currentParams.has('feed_url') && !currentParams.has('entry_id')) {
        const hasNoScopeQuery = currentParams.toString() === '';
        const normalizedCurrent = normalizeScopeUrl(currentUrl);
        const normalizedActive = normalizeScopeUrl(activeScopeUrl || currentUrl);
        const isSameScopeAsCurrentDom = normalizedCurrent === normalizedActive;
        const scopeStateNoReload = Boolean(state.lectioScopePane) && isSameScopeAsCurrentDom;
        if (hasNoScopeQuery || scopeStateNoReload) {
          updateScopeActiveState(currentUrl);
          return;
        }
      }

      if (currentParams.has('feed_url') && currentParams.has('entry_id')) {
        loadEntryPaneWithoutFullRefresh(currentUrl, false);
        return;
      }
      loadScopePanesWithoutFullRefresh(currentUrl, false);
    });

    let entryTagAddBtn = document.getElementById('entry-tag-add-btn');
    let entryTagsForm = document.querySelector('.entry-tags-form');
    let entryTagsInput = document.getElementById('entry-tags-input');
    let entryTagsMergedInput = document.getElementById('entry-tags-input-merged');

    function refreshEntryTagRefs() {
      entryTagAddBtn = document.getElementById('entry-tag-add-btn');
      entryTagsForm = document.querySelector('.entry-tags-form');
      entryTagsInput = document.getElementById('entry-tags-input');
      entryTagsMergedInput = document.getElementById('entry-tags-input-merged');
    }

    function normalizeTagToken(token) {
      return token.trim().replace(/^#+/, '').toLowerCase();
    }

    function tokenizeTags(value) {
      return value
        .split(/[\s,]+/)
        .map((token) => normalizeTagToken(token))
        .filter(Boolean);
    }

    function setEntryTagsExpandedState(expanded) {
      if (!entryTagAddBtn) {
        return;
      }
      entryTagAddBtn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }

    function bindEntryTagInteractions() {
      refreshEntryTagRefs();
      setEntryTagsExpandedState(Boolean(entryTagsForm && !entryTagsForm.hasAttribute('hidden')));

      if (entryTagsForm && !entryTagsForm.dataset.boundSubmit) {
        entryTagsForm.dataset.boundSubmit = '1';
        entryTagsForm.addEventListener('submit', async (event) => {
          event.preventDefault();

          if (!(entryTagsInput instanceof HTMLInputElement) || !(entryTagsMergedInput instanceof HTMLInputElement)) {
            return;
          }

          const existingTokens = tokenizeTags(entryTagsMergedInput.value);
          const rawInput = entryTagsInput.value.trim();
          const addedTokens = tokenizeTags(rawInput);

          if (addedTokens.length === 0 && !rawInput) {
            entryTagsInput.focus();
            return;
          }

          // Validate against server's TAG_VALUE_PATTERN: letters, digits, _ - + . #
          const TAG_VALID_RE = /^[A-Za-z0-9_.#+][A-Za-z0-9_.#+-]{0,31}$/;
          const invalidTokens = addedTokens.filter(t => !TAG_VALID_RE.test(t));
          if (invalidTokens.length > 0 || (rawInput && addedTokens.length === 0)) {
            showToastMessage('Tags may only contain letters, numbers, and: - _ + . #');
            entryTagsInput.select();
            return;
          }

          const merged = [];
          const seen = new Set();
          for (const token of [...existingTokens, ...addedTokens]) {
            if (seen.has(token)) continue;
            seen.add(token);
            merged.push(token);
            if (merged.length >= 12) break;
          }

          entryTagsMergedInput.value = merged.join(' ');

          const body = new URLSearchParams(new FormData(entryTagsForm));
          try {
            const resp = await fetch('/entries/tags', {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-ajax' },
              credentials: 'same-origin',
              body: body.toString(),
            });
            const data = await resp.json();
            if (data.ok) {
              entryTagsInput.value = '';
              loadEntryPaneWithoutFullRefresh(window.location.href, false);
            } else {
              showToastMessage(data.error || 'Failed to save tags.');
            }
          } catch {
            showToastMessage('Failed to save tags.');
          }
        });
      }

      if (entryTagsInput && !entryTagsInput.dataset.boundKeydown) {
        entryTagsInput.dataset.boundKeydown = '1';
        entryTagsInput.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          // Explicitly click Apply so the submit handler reliably fires and merges tokens.
          entryTagsForm?.querySelector('button[type="submit"]')?.click();
        });
      }

      if (entryTagAddBtn && !entryTagAddBtn.dataset.boundClick) {
        entryTagAddBtn.dataset.boundClick = '1';
        entryTagAddBtn.addEventListener('click', () => {
          if (entryTagsForm?.hasAttribute('hidden')) {
            entryTagsForm.removeAttribute('hidden');
            setEntryTagsExpandedState(true);
            entryTagsInput?.focus();
          } else {
            entryTagsForm?.setAttribute('hidden', '');
            setEntryTagsExpandedState(false);
          }
        });
      }

      // Feed-tag chips [ + tag ▲ ▼ ]: the leading + applies the tag as one of
      // Our Tags via the tags form's submit pipeline.
      for (const suggestionButton of document.querySelectorAll('[data-tag-suggestion]')) {
        if (suggestionButton.dataset.boundClick) {
          continue;
        }
        suggestionButton.dataset.boundClick = '1';
        suggestionButton.addEventListener('click', () => {
          const inputId = suggestionButton.getAttribute('data-tag-input');
          const suggestedTag = suggestionButton.getAttribute('data-tag-suggestion');
          const targetInput = inputId ? document.getElementById(inputId) : null;
          const normalizedSuggestion = (suggestedTag || '').trim().replace(/^#+/, '').toLowerCase();
          if (!normalizedSuggestion || !(targetInput instanceof HTMLInputElement)) {
            return;
          }
          // Merge with anything already typed — the pane re-render on success
          // would drop it otherwise.
          const typedTokens = targetInput.value
            .split(/\s+/)
            .map((token) => token.trim().replace(/^#+/, '').toLowerCase())
            .filter(Boolean);
          if (!typedTokens.includes(normalizedSuggestion)) {
            typedTokens.push(normalizedSuggestion);
          }
          targetInput.value = typedTokens.join(' ');
          suggestionButton.disabled = true;
          entryTagsForm?.querySelector('button[type="submit"]')?.click();
        });
      }

      // ▲/▼ toggle ±tag on this feed's Tag Filter rule. The rule is created
      // DISABLED so it can be tuned while browsing; it only marks entries
      // read once armed in Automation (then chip edits apply immediately).
      for (const signButton of document.querySelectorAll('[data-tag-filter-sign]')) {
        if (signButton.dataset.boundClick) {
          continue;
        }
        signButton.dataset.boundClick = '1';
        signButton.addEventListener('click', async () => {
          const tag = signButton.getAttribute('data-tag-filter-tag');
          const sign = signButton.getAttribute('data-tag-filter-sign');
          const feedUrlInput = entryTagsForm?.querySelector('input[name="feed_url"]');
          const feedUrl = feedUrlInput instanceof HTMLInputElement ? feedUrlInput.value : '';
          if (!tag || !sign || !feedUrl) {
            return;
          }
          signButton.disabled = true;
          try {
            const body = new URLSearchParams({ feed_url: feedUrl, tag, sign });
            const resp = await fetch('/rules/tag-filter/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              credentials: 'same-origin',
              body: body.toString(),
            });
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error || 'failed');
            const nowActive = (data.active || {})[tag] === sign;
            const verb = sign === '-' ? 'dropping' : 'good tag';
            const n = data.applied_count || 0;
            const armed = data.enabled
              ? (n ? ` — ${n} marked read` : '')
              : ' (rule off — enable it in Automation)';
            showToastMessage(nowActive
              ? `Filter: ${verb} #${tag} on this feed${armed}`
              : `Removed ${sign}${tag} from this feed's filter`);
            // Re-render so chip states and the unread list reflect the rule.
            loadEntryPaneWithoutFullRefresh(window.location.href, false);
          } catch (err) {
            showToastMessage('Filter update failed: ' + (err.message || err));
            signButton.disabled = false;
          }
        });
      }

      // Per-post tag removal — the × on each tag chip submits the reduced set
      // in replace mode (append_mode=0) so the one tag drops off this post.
      for (const removeBtn of document.querySelectorAll('[data-tag-remove]')) {
        if (removeBtn.dataset.boundClick) {
          continue;
        }
        removeBtn.dataset.boundClick = '1';
        removeBtn.addEventListener('click', async (event) => {
          event.preventDefault();
          event.stopPropagation();

          if (!(entryTagsForm instanceof HTMLFormElement) || !(entryTagsMergedInput instanceof HTMLInputElement)) {
            return;
          }
          const tagToRemove = normalizeTagToken(removeBtn.getAttribute('data-tag-remove') || '');
          if (!tagToRemove) {
            return;
          }

          const reduced = tokenizeTags(entryTagsMergedInput.value).filter((token) => token !== tagToRemove);
          const body = new URLSearchParams(new FormData(entryTagsForm));
          body.set('append_mode', '0');
          body.set('tags_text', reduced.join(' '));

          removeBtn.disabled = true;
          try {
            const resp = await fetch('/entries/tags', {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'lectio-ajax' },
              credentials: 'same-origin',
              body: body.toString(),
            });
            const data = await resp.json();
            if (data.ok) {
              loadEntryPaneWithoutFullRefresh(window.location.href, false);
            } else {
              removeBtn.disabled = false;
              showToastMessage(data.error || 'Failed to remove tag.');
            }
          } catch {
            removeBtn.disabled = false;
            showToastMessage('Failed to remove tag.');
          }
        });
      }

      maybeInjectFeedTagChips();
    }

    // Late chip delivery: when a pane renders without feed-tag chips (backlog
    // entry — the open queued a background page fetch), ask the server for the
    // harvested tags and inject the [ + tag ▲ ▼ ] chips into the open pane.
    async function maybeInjectFeedTagChips() {
      const row = document.querySelector('.entry-tags-row');
      const form = document.getElementById('entry-tags-form');
      if (!row || !(form instanceof HTMLFormElement)) return;
      if (row.querySelector('.entry-tag-suggestions')) return;  // already has chips
      if (form.dataset.feedTagsRequested) return;
      const feedUrl = form.querySelector('input[name="feed_url"]')?.value || '';
      const entryId = form.querySelector('input[name="entry_id"]')?.value || '';
      if (!feedUrl || !entryId) return;
      form.dataset.feedTagsRequested = '1';
      try {
        const qs = new URLSearchParams({ feed_url: feedUrl, entry_id: entryId });
        const resp = await fetch('/entries/feed-tags?' + qs.toString(), { credentials: 'same-origin' });
        const data = await resp.json();
        if (!resp.ok || !data.ok || !(data.tags || []).length) return;
        // The user may have navigated on while we waited — only inject if the
        // pane still shows the same entry and still has no chips.
        const nowForm = document.getElementById('entry-tags-form');
        const nowRow = document.querySelector('.entry-tags-row');
        if (!nowForm || !nowRow || nowRow.querySelector('.entry-tag-suggestions')) return;
        if ((nowForm.querySelector('input[name="entry_id"]')?.value || '') !== entryId) return;
        const manual = new Set(data.manual_tags || []);
        const signs = data.signs || {};
        const wrap = document.createElement('span');
        wrap.className = 'entry-tag-suggestions';
        wrap.setAttribute('aria-label', 'Feed tags — filter this feed');
        for (const tag of data.tags) {
          const chip = document.createElement('span');
          chip.className = 'entry-tag-chip suggestion feed-tag-filter-chip';
          if (!manual.has(tag)) {
            const add = document.createElement('button');
            add.type = 'button';
            add.className = 'feed-tag-filter-sign add-tag';
            add.setAttribute('data-tag-suggestion', tag);
            add.setAttribute('data-tag-input', 'entry-tags-input');
            add.title = `Add #${tag} to this post`;
            add.setAttribute('aria-label', add.title);
            add.textContent = '+';
            chip.appendChild(add);
          }
          const name = document.createElement('span');
          name.className = 'feed-tag-filter-name';
          name.textContent = tag;
          chip.appendChild(name);
          for (const [sign, cls, glyph, verb] of [['+', 'include', '▲', 'good tag — rescues'], ['-', 'exclude', '▼', 'drops']]) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `feed-tag-filter-sign ${cls}` + (signs[tag] === sign ? ' active' : '');
            btn.setAttribute('data-tag-filter-tag', tag);
            btn.setAttribute('data-tag-filter-sign', sign);
            btn.title = `Feed filter: ${verb} #${tag} posts`;
            btn.setAttribute('aria-label', btn.title);
            btn.setAttribute('aria-pressed', signs[tag] === sign ? 'true' : 'false');
            btn.textContent = glyph;
            chip.appendChild(btn);
          }
          wrap.appendChild(chip);
        }
        nowForm.insertAdjacentElement('beforebegin', wrap);
        bindEntryTagInteractions();  // boundClick markers keep this idempotent
      } catch {
        // chips are progressive enhancement — never disturb the pane
      }
    }

    function isTypingTarget(target) {
      if (!(target instanceof HTMLElement)) {
        return false;
      }
      if (target.isContentEditable) {
        return true;
      }
      const tag = target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        return true;
      }
      return false;
    }

    function getVisiblePostItems() {
      return Array.from(document.querySelectorAll('.posts .post-item:not(.post-item-hidden)'));
    }

    function getActivePostItem() {
      const visiblePosts = getVisiblePostItems();
      if (visiblePosts.length === 0) {
        return null;
      }
      const activePost = document.querySelector('.posts .post-item.active:not(.post-item-hidden)');
      return activePost || visiblePosts[0];
    }

    function openPostItemInPane(postItem) {
      if (!postItem) {
        return;
      }
      const postLink = postItem.querySelector('.post-main-link');
      if (postLink && postLink.href) {
        loadEntryPaneWithoutFullRefresh(postLink.href);
      }
    }

    function moveActivePostBy(delta) {
      const visiblePosts = getVisiblePostItems();
      if (visiblePosts.length === 0) {
        return;
      }

      const activePost = document.querySelector('.posts .post-item.active:not(.post-item-hidden)');
      let nextIndex = activePost ? visiblePosts.indexOf(activePost) + delta : 0;

      if (delta > 0 && nextIndex >= visiblePosts.length) {
        // Past the end of the visible chunk — reveal or fetch the next chunk,
        // then navigate to the first newly visible post and scroll it to the top.
        const postsPane = document.querySelector('.posts');
        const revealFn = postsPane && postsPane._revealNextChunk;
        if (!revealFn) return;
        const beforeCount = visiblePosts.length;
        const result = revealFn();
        const selectFirstNew = () => {
          const newVisible = getVisiblePostItems();
          const target = newVisible[beforeCount];
          if (!target || target === activePost) return;
          if (postsPane instanceof HTMLElement) {
            const itemRect = target.getBoundingClientRect();
            const paneRect = postsPane.getBoundingClientRect();
            postsPane.scrollTop += itemRect.top - paneRect.top - 8;
          }
          openPostItemInPane(target);
        };
        if (result && typeof result.then === 'function') {
          result.then(() => window.requestAnimationFrame(selectFirstNew)).catch(() => {});
        } else {
          window.requestAnimationFrame(selectFirstNew);
        }
        return;
      }

      nextIndex = Math.max(0, Math.min(visiblePosts.length - 1, nextIndex));
      openPostItemInPane(visiblePosts[nextIndex]);
    }

    function setActivePostSelection(postItem) {
      if (!postItem) {
        return;
      }

      const visiblePosts = getVisiblePostItems();
      for (const item of visiblePosts) {
        item.classList.toggle('active', item === postItem);
      }

      const postsPane = document.querySelector('.posts');
      if (!(postsPane instanceof HTMLElement)) {
        return;
      }

      const itemTop = postItem.offsetTop;
      const itemBottom = itemTop + postItem.offsetHeight;
      const viewTop = postsPane.scrollTop;
      const viewBottom = viewTop + postsPane.clientHeight;
      if (itemTop < viewTop + 8 || itemBottom > viewBottom - 8) {
        const centeredTop = itemTop - Math.max(0, (postsPane.clientHeight - postItem.offsetHeight) / 2);
        postsPane.scrollTop = Math.max(0, centeredTop);
      }
    }

    function moveActivePostSelectionBy(delta) {
      const visiblePosts = getVisiblePostItems();
      if (visiblePosts.length === 0) {
        return;
      }

      const activePost = document.querySelector('.posts .post-item.active:not(.post-item-hidden)');
      let nextIndex = activePost ? visiblePosts.indexOf(activePost) + delta : 0;
      nextIndex = Math.max(0, Math.min(visiblePosts.length - 1, nextIndex));
      setActivePostSelection(visiblePosts[nextIndex]);
    }

    function setContextFromPostItem(postItem) {
      if (!postItem) {
        return false;
      }
      contextPostFeedUrl = postItem.getAttribute('data-post-feed-url') || null;
      contextPostEntryId = postItem.getAttribute('data-post-entry-id') || null;
      contextPostRead = postItem.getAttribute('data-post-read') === '1';
      contextPostLink = postItem.getAttribute('data-post-link') || '';
      return Boolean(contextPostFeedUrl && contextPostEntryId);
    }

    function toggleActivePostRead() {
      const postItem = getActivePostItem();
      if (!postItem) {
        return;
      }
      const form = postItem.querySelector('.post-read-toggle-form');
      if (form instanceof HTMLFormElement) {
        form.requestSubmit();
      }
    }

    function toggleActivePostSaved() {
      const postItem = getActivePostItem();
      if (!postItem) {
        return;
      }
      const form = postItem.querySelector('.post-save-toggle-form');
      if (form instanceof HTMLFormElement) {
        form.requestSubmit();
      }
    }

    function openActivePostInNewTab() {
      const postItem = getActivePostItem();
      if (!postItem || !setContextFromPostItem(postItem) || !contextPostLink) {
        return;
      }
      window.open(contextPostLink, '_blank', 'noopener');
    }

    function toggleEntryReadability() {
      if (entryReadabilityButton instanceof HTMLElement) {
        entryReadabilityButton.click();
      }
    }

    function toggleEntryWebView() {
      if (entrySourceButton instanceof HTMLElement) {
        entrySourceButton.click();
      }
    }

    // ── Unified Add Feed dialog ──────────────────────────────────────────────
    (function () {
      const modal    = document.getElementById('add-feed-modal');
      if (!modal) return;
      const urlInput      = document.getElementById('afd-url');
      const spinner       = document.getElementById('afd-spinner');
      const msgEl         = document.getElementById('afd-msg');
      const pickEl        = document.getElementById('afd-pick');
      const pfSection     = document.getElementById('afd-pf');
      const folderSelect  = document.getElementById('afd-folder');
      const newFolderRow  = document.getElementById('afd-new-folder');
      const newFolderName = document.getElementById('afd-new-folder-name');
      const newFolderBtn  = document.getElementById('afd-new-folder-btn');
      const submitBtn     = document.getElementById('afd-submit');
      const feedForm      = document.getElementById('afd-feed-form');
      const pfForm        = document.getElementById('afd-pf-form');

      let checkTimer = null, selectedFeedUrl = null, isPageFeedMode = false, isChecking = false, isDevToMode = false;
      const devtoSection = document.getElementById('afd-devto');

      // Mirror of the server's parse_devto_url: front-page/tag/feed URLs only
      // (user pages keep their normal small RSS). Returns {tag} or null.
      function devtoParse(url) {
        let u;
        try { u = new URL(url); } catch (_) { return null; }
        if (u.hostname !== 'dev.to' && u.hostname !== 'www.dev.to') return null;
        const parts = u.pathname.split('/').filter(Boolean);
        if (!parts.length) return { tag: null };
        if (parts[0] === 't' && parts.length >= 2) return { tag: parts[1].toLowerCase() };
        if (parts[0] === 'feed') {
          if (parts.length === 1) return { tag: null };
          if (parts.length >= 3 && parts[1] === 'tag') return { tag: parts[2].toLowerCase() };
        }
        return null;
      }

      function reset() {
        urlInput.value = '';
        msgEl.hidden = true; msgEl.className = 'afd-msg'; msgEl.textContent = '';
        pickEl.hidden = true; pickEl.innerHTML = '';
        pfSection.hidden = true;
        pfSection.removeAttribute('open');
        document.getElementById('afd-pf-title').value = '';
        document.getElementById('afd-pf-selector').value = '';
        document.getElementById('afd-pf-backfill').checked = false;
        const _sug = document.getElementById('afd-pf-suggestions');
        const _prev = document.getElementById('afd-pf-preview');
        if (_sug) { _sug.hidden = true; document.getElementById('afd-pf-suggestions-chips').innerHTML = ''; }
        if (_prev) { _prev.hidden = true; _prev.innerHTML = ''; }
        modal.querySelector('input[name="afd-mode"][value="link_list"]').checked = true;
        devtoSection.hidden = true;
        document.getElementById('afd-devto-tag').value = '';
        document.getElementById('afd-devto-top').value = '';
        document.getElementById('afd-devto-minreact').value = '';
        document.getElementById('afd-devto-exclude').value = '';
        document.getElementById('afd-devto-english').checked = true;
        submitBtn.disabled = true; submitBtn.textContent = 'Add Feed';
        selectedFeedUrl = null; isPageFeedMode = false; isDevToMode = false;
        spinner.hidden = true; isChecking = false;
        newFolderRow.hidden = true; newFolderName.value = '';
      }

      function setMsg(text, type) {
        msgEl.textContent = text;
        msgEl.className = 'afd-msg' + (type ? ' afd-msg--' + type : '');
        msgEl.hidden = !text;
      }

      // Schemeless paste (www.example.com, example.com/feed) — assume https.
      // Anything that doesn't yet look like a hostname stays pending silently
      // (the user is still typing).
      function normalizeInput(url) {
        if (!url) return '';
        if (url.includes('://')) return url;
        if (/^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[/:?#]|$)/i.test(url)) return 'https://' + url;
        return '';
      }

      function schedule(rawUrl) {
        clearTimeout(checkTimer);
        const url = normalizeInput(rawUrl);
        if (!url) {
          setMsg(''); submitBtn.disabled = true;
          pickEl.hidden = true; pfSection.hidden = true; selectedFeedUrl = null;
          return;
        }
        checkTimer = setTimeout(() => run(url), 700);
      }

      async function run(url) {
        if (isChecking) return;
        // dev.to front-page/tag URLs get the filtered API-backed adapter — show
        // its config instead of probing (the raw RSS is an unfiltered firehose).
        const devto = devtoParse(url);
        if (devto) {
          setMsg('dev.to detected — this will create a filtered feed via the dev.to API.', 'ok');
          pickEl.hidden = true; pfSection.hidden = true; isPageFeedMode = false;
          devtoSection.hidden = false; isDevToMode = true; selectedFeedUrl = url;
          if (devto.tag) document.getElementById('afd-devto-tag').value = devto.tag;
          submitBtn.textContent = 'Add dev.to Feed'; submitBtn.disabled = false;
          return;
        }
        devtoSection.hidden = true; isDevToMode = false;
        isChecking = true; spinner.hidden = false; submitBtn.disabled = true;
        setMsg(''); pickEl.hidden = true; pickEl.innerHTML = ''; pfSection.hidden = true;
        selectedFeedUrl = null; isPageFeedMode = false;
        try {
          const r = await fetch('/feeds/discover?url=' + encodeURIComponent(url));
          apply(url, await r.json());
        } catch (_) { setMsg('Network error — could not reach server.', 'error'); }
        finally { isChecking = false; spinner.hidden = true; }
      }

      function apply(rawUrl, d) {
        if (d.status === 'feed' || d.status === 'feeds') {
          selectedFeedUrl = d.feeds[0].url;
          if (d.direct) {
            // Direct feed URL: enable the button and let the user pick their
            // folder and click Add Feed. (Previously this auto-submitted on
            // validation using whatever folder was selected — usually the
            // default root — ignoring the user's folder choice and button click.)
            setMsg('Direct feed URL.', 'ok');
            submitBtn.textContent = 'Add Feed'; submitBtn.disabled = false;
          } else {
            if (d.feeds.length > 1) setMsg(d.feeds.length + ' feeds found — pick one:');
            renderPicker(d.feeds);
            submitBtn.textContent = 'Add Feed';
          }
        } else if (d.status === 'none' || d.status === 'blocked') {
          setMsg(d.message, 'warn');
          pfSection.hidden = false; isPageFeedMode = true; selectedFeedUrl = rawUrl;
          pfSyncPickBtn();
          submitBtn.textContent = 'Create Page Feed'; submitBtn.disabled = false;
        } else {
          setMsg(d.message || 'Could not check URL.', 'error');
        }
      }

      function renderPicker(feeds) {
        pickEl.innerHTML = ''; pickEl.hidden = false;
        feeds.forEach((f, i) => {
          const lbl = document.createElement('label');
          lbl.className = 'afd-pick-item';
          const rb = document.createElement('input');
          rb.type = 'radio'; rb.name = 'afd-pick'; rb.value = f.url;
          if (i === 0) { rb.checked = true; selectedFeedUrl = f.url; submitBtn.disabled = false; }
          rb.addEventListener('change', () => { selectedFeedUrl = f.url; submitBtn.disabled = false; });
          const sp = document.createElement('span');
          sp.textContent = f.title || f.url; sp.title = f.url;
          lbl.append(rb, sp); pickEl.append(lbl);
        });
        if (feeds.length > 1) {
          const btn = document.createElement('button');
          btn.type = 'button'; btn.className = 'afd-compare-btn'; btn.textContent = 'Compare feeds';
          btn.addEventListener('click', () => compareFeedsPicker(feeds, btn));
          pickEl.append(btn);
        }
      }

      async function compareFeedsPicker(feeds, btn) {
        btn.disabled = true; btn.textContent = 'Comparing…';
        const params = feeds.map(f => 'url=' + encodeURIComponent(f.url)).join('&');
        try {
          const r = await fetch('/feeds/compare?' + params);
          const results = await r.json();
          results.forEach(res => {
            const rb = pickEl.querySelector('input[value="' + CSS.escape(res.url) + '"]');
            const item = rb?.closest('.afd-pick-item');
            if (!item) return;
            // Remove any existing chip rows before re-rendering.
            item.querySelectorAll('.afd-feed-meta, .afd-feed-title-row').forEach(el => el.remove());
            item.append(buildCompareChips(res));
          });
          btn.remove();
        } catch (_) { btn.disabled = false; btn.textContent = 'Compare feeds'; }
      }

      folderSelect.addEventListener('change', () => {
        newFolderRow.hidden = folderSelect.value !== '__new__';
        if (folderSelect.value === '__new__') newFolderName.focus();
      });

      newFolderBtn.addEventListener('click', async () => {
        const name = newFolderName.value.trim();
        if (!name) return;
        newFolderBtn.disabled = true;
        try {
          const r = await fetch('/api/folders', { method: 'POST', body: new URLSearchParams({ name }) });
          const d = await r.json();
          if (d.ok) {
            const opt = new Option(d.name, d.id);
            folderSelect.insertBefore(opt, folderSelect.querySelector('option[value="__new__"]'));
            folderSelect.value = d.id;
            newFolderRow.hidden = true; newFolderName.value = '';
          }
        } finally { newFolderBtn.disabled = false; }
      });

      newFolderName.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); newFolderBtn.click(); } });

      submitBtn.addEventListener('click', () => {
        const fid = folderSelect.value === '__new__' ? null : folderSelect.value;
        if (!fid || !selectedFeedUrl) return;
        if (isPageFeedMode) {
          document.getElementById('afd-pff-url').value    = selectedFeedUrl;
          document.getElementById('afd-pff-folder').value = fid;
          document.getElementById('afd-pff-mode').value   = modal.querySelector('input[name="afd-mode"]:checked')?.value || 'link_list';
          document.getElementById('afd-pff-title').value  = document.getElementById('afd-pf-title').value;
          document.getElementById('afd-pff-selector').value = document.getElementById('afd-pf-selector').value;
          document.getElementById('afd-pff-backfill').value = document.getElementById('afd-pf-backfill').checked ? '1' : '';
          pfForm.submit();
        } else {
          document.getElementById('afd-ff-url').value    = selectedFeedUrl;
          document.getElementById('afd-ff-folder').value = fid;
          document.getElementById('afd-ff-devto-tag').value      = isDevToMode ? document.getElementById('afd-devto-tag').value.trim() : '';
          document.getElementById('afd-ff-devto-top').value      = isDevToMode ? document.getElementById('afd-devto-top').value.trim() : '';
          document.getElementById('afd-ff-devto-english').value  = isDevToMode ? (document.getElementById('afd-devto-english').checked ? '1' : '0') : '';
          document.getElementById('afd-ff-devto-minreact').value = isDevToMode ? document.getElementById('afd-devto-minreact').value.trim() : '';
          document.getElementById('afd-ff-devto-exclude').value  = isDevToMode ? document.getElementById('afd-devto-exclude').value.trim() : '';
          feedForm.submit();
        }
      });

      urlInput.addEventListener('input', () => schedule(urlInput.value.trim()));
      urlInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); clearTimeout(checkTimer); run(urlInput.value.trim()); } });

      window.openAddFeedDialog = function (opts = {}) {
        reset();
        if (opts.folderId) folderSelect.value = String(opts.folderId);
        if (opts.url) { urlInput.value = opts.url; run(opts.url); }
        modal.removeAttribute('hidden');
        urlInput.focus();
      };
      // Legacy shims so any remaining callers don't break
      window.openAddFeedModal = () => window.openAddFeedDialog();
      window.openFakeFeedzModal = url => window.openAddFeedDialog({ url });

      // --- Page Feed live preview + selector suggestions ---
      const pfPreviewBtn = document.getElementById('afd-pf-preview-btn');
      const pfSelectorInput = document.getElementById('afd-pf-selector');
      const pfSuggestions = document.getElementById('afd-pf-suggestions');
      const pfSuggestChips = document.getElementById('afd-pf-suggestions-chips');
      const pfPreviewBox = document.getElementById('afd-pf-preview');

      function pfRenderSuggestions(suggestions) {
        pfSuggestChips.innerHTML = '';
        if (!suggestions || !suggestions.length) { pfSuggestions.hidden = true; return; }
        suggestions.forEach(s => {
          const chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'afd-pf-chip';
          chip.textContent = `${s.count} · ${s.selector}`;
          chip.title = (s.samples || []).join('\n');
          chip.addEventListener('click', () => { pfSelectorInput.value = s.selector; pfRunPreview(); });
          pfSuggestChips.appendChild(chip);
        });
        pfSuggestions.hidden = false;
      }

      async function pfRunPreview() {
        if (!selectedFeedUrl) return;
        const mode = modal.querySelector('input[name="afd-mode"]:checked')?.value || 'link_list';
        pfPreviewBox.hidden = false;
        pfPreviewBox.textContent = 'Loading preview…';
        pfPreviewBtn.disabled = true;
        try {
          const body = new URLSearchParams({ source_url: selectedFeedUrl, mode, selector: pfSelectorInput.value.trim() });
          const r = await fetch('/scraped-feeds/preview', { method: 'POST', body, credentials: 'same-origin' });
          const d = await r.json();
          if (!r.ok) { pfPreviewBox.textContent = d.error || 'Preview failed.'; return; }
          pfRenderSuggestions(d.suggestions);
          if (mode === 'link_list') {
            const items = d.items || [];
            if (!items.length) {
              pfPreviewBox.textContent = pfSelectorInput.value.trim()
                ? 'No links matched this selector — try a suggestion above.'
                : 'Pick a suggested selector above, or enter one, then Preview.';
              return;
            }
            pfPreviewBox.innerHTML = '';
            const head = document.createElement('div');
            head.className = 'afd-pf-preview-head';
            head.textContent = `${items.length} item${items.length === 1 ? '' : 's'} will be added:`;
            pfPreviewBox.appendChild(head);
            const ul = document.createElement('ul');
            ul.className = 'afd-pf-preview-list';
            items.slice(0, 20).forEach(it => {
              const li = document.createElement('li');
              li.textContent = it.title;
              li.title = it.url;
              ul.appendChild(li);
            });
            pfPreviewBox.appendChild(ul);
          } else {
            pfPreviewBox.textContent = d.content_preview
              ? 'Watching this content: ' + d.content_preview
              : 'No content matched — the whole page will be watched.';
          }
        } catch (_) {
          pfPreviewBox.textContent = 'Network error — could not preview.';
        } finally { pfPreviewBtn.disabled = false; }
      }

      pfPreviewBtn?.addEventListener('click', pfRunPreview);
      pfSelectorInput?.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); pfRunPreview(); } });

      // Point-and-click selector picker: load the source page (sanitized, same-
      // origin proxy) in an iframe; clicking a link posts its href back, which we
      // resolve to a selector server-side and feed into the existing preview.
      const pfPickBtn = document.getElementById('afd-pf-pick-btn');
      const pfPicker = document.getElementById('afd-pf-picker');
      const pfPickerFrame = document.getElementById('afd-pf-picker-frame');
      const pfPickerClose = document.getElementById('afd-pf-picker-close');
      const pfPickerHeadText = pfPicker?.querySelector('.afd-pf-picker-head span');

      function pfClosePicker() { if (pfPicker) pfPicker.hidden = true; if (pfPickerFrame) pfPickerFrame.src = 'about:blank'; }

      // The picker only makes sense for link_list mode (it derives a link selector).
      function pfSyncPickBtn() {
        const mode = modal.querySelector('input[name="afd-mode"]:checked')?.value || 'link_list';
        if (pfPickBtn) pfPickBtn.hidden = (mode !== 'link_list');
        if (mode !== 'link_list') pfClosePicker();
      }
      modal.querySelectorAll('input[name="afd-mode"]').forEach(r => r.addEventListener('change', pfSyncPickBtn));

      pfPickBtn?.addEventListener('click', () => {
        if (!selectedFeedUrl) return;
        if (!pfPicker.hidden) { pfClosePicker(); return; }
        if (pfPickerHeadText) pfPickerHeadText.textContent = 'Click a headline link in the page to build a selector.';
        pfPicker.hidden = false;
        pfPickerFrame.src = '/scraped-feeds/picker-frame?url=' + encodeURIComponent(selectedFeedUrl);
      });
      pfPickerClose?.addEventListener('click', pfClosePicker);

      window.addEventListener('message', async (e) => {
        // The picker iframe is same-origin (served by /scraped-feeds/picker-frame),
        // so only trust messages from it — ignore spoofed cross-origin messages.
        if (e.origin !== window.location.origin) return;
        if (!pfPickerFrame || e.source !== pfPickerFrame.contentWindow) return;
        const d = e.data;
        if (!d || d.type !== 'lectio-pick' || !d.href || !selectedFeedUrl) return;
        if (pfPickerHeadText) pfPickerHeadText.textContent = 'Deriving selector…';
        try {
          const body = new URLSearchParams({ source_url: selectedFeedUrl, href: d.href });
          const r = await fetch('/scraped-feeds/pick', { method: 'POST', body, credentials: 'same-origin' });
          const res = await r.json();
          if (!r.ok || !res.selector) {
            if (pfPickerHeadText) pfPickerHeadText.textContent = res.error || 'No selector for that link — try another.';
            return;
          }
          pfSelectorInput.value = res.selector;
          pfClosePicker();
          pfRunPreview();
        } catch (_) {
          if (pfPickerHeadText) pfPickerHeadText.textContent = 'Network error — could not derive a selector.';
        }
      });
      // Auto-load suggestions the first time the Page Feed section is opened.
      pfSection?.addEventListener('toggle', () => {
        if (pfSection.open && isPageFeedMode && pfSuggestions.hidden && !pfSuggestChips.children.length) {
          pfRunPreview();
        }
      });

      document.getElementById('menu-add-feed-btn')?.addEventListener('click', () => {
        window.openAddFeedDialog();
        document.getElementById('side-menu')?.removeAttribute('data-open');
      });

      // Save Article dialog: read-later capture of an arbitrary page URL.
      (function () {
        const svaModal = document.getElementById('save-article-modal');
        const svaUrl = document.getElementById('sva-url');
        const svaMsg = document.getElementById('sva-msg');
        const svaSpinner = document.getElementById('sva-spinner');
        const svaSubmit = document.getElementById('sva-submit');
        if (!svaModal || !svaUrl || !svaSubmit) return;

        const validUrl = () => /^https?:\/\/\S+\.\S+/i.test(svaUrl.value.trim());
        svaUrl.addEventListener('input', () => { svaSubmit.disabled = !validUrl(); });
        svaUrl.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && !svaSubmit.disabled) svaSubmit.click();
        });

        document.getElementById('menu-save-article-btn')?.addEventListener('click', (e) => {
          e.target.closest('.hamburger-menu')?.removeAttribute('open');
          svaMsg.hidden = true;
          svaMsg.textContent = '';
          svaUrl.value = '';
          svaSubmit.disabled = true;
          svaSubmit.textContent = 'Save Article';
          svaModal.removeAttribute('hidden');
          svaUrl.focus();
        });

        svaSubmit.addEventListener('click', async () => {
          const url = svaUrl.value.trim();
          if (!url) return;
          svaSubmit.disabled = true;
          svaSpinner.hidden = false;
          svaMsg.hidden = true;
          try {
            const response = await fetch('/articles/save', {
              method: 'POST',
              headers: { 'X-Requested-With': 'lectio-save-article' },
              body: new URLSearchParams({ url }),
            });
            const result = await response.json();
            if (!result.ok) throw new Error(result.error || 'Could not save the article.');
            const message = result.duplicate ? 'Article already saved.' : 'Article saved.';
            window.location.assign(
              '/?list_feed_url=' + encodeURIComponent(result.feed_url)
              + '&feed_url=' + encodeURIComponent(result.feed_url)
              + '&entry_id=' + encodeURIComponent(result.entry_id)
              + '&message=' + encodeURIComponent(message)
            );
          } catch (err) {
            svaMsg.textContent = err.message || 'Could not save the article.';
            svaMsg.hidden = false;
            svaSubmit.disabled = !validUrl();
            svaSpinner.hidden = true;
          }
        });
      }());
      document.getElementById('no-feeds-add-btn')?.addEventListener('click', () => {
        window.openAddFeedDialog();
      });

      // Quick-subscription deep link: /?subscribe=<feed> (RSSHub-Radar pointed at
      // Lectio via the Feedbin/Nextcloud News address override) auto-opens the
      // Add Feed dialog pre-filled with the feed URL.
      if (window.SUBSCRIBE_URL) {
        window.openAddFeedDialog({ url: window.SUBSCRIBE_URL });
      }
    }());

    // "Create page feed" button inside the no-RSS toast.
    const toastPageFeedBtn = document.getElementById('toast-page-feed-btn');
    if (toastPageFeedBtn) {
      toastPageFeedBtn.addEventListener('click', () => {
        openAddFeedDialog({ url: window.NO_RSS_URL });
      });
    }

    async function submitRefreshFormAsync(form) {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        redirect: 'follow',
      });

      if (!response.ok) {
        throw new Error(`Refresh failed (${response.status})`);
      }

      const finalUrl = response.url || window.location.href;
      try {
        const finalParams = new URL(finalUrl, window.location.origin).searchParams;
        const message = finalParams.get('message');
        if (message && typeof showToastMessage === 'function') {
          showToastMessage(message);
        }
      } catch (_e) {
        // ignore URL parse issues
      }

      if (typeof loadScopePanesWithoutFullRefresh === 'function') {
        await loadScopePanesWithoutFullRefresh(finalUrl, false);
        return;
      }

      window.location.href = finalUrl;
    }

    async function refreshCurrentFeedOrFolder() {
      const searchParams = new URLSearchParams(window.location.search);
      let folderId = searchParams.get('folder_id') || contextFolderId || null;
      const scopeListFeedUrl = searchParams.get('list_feed_url') || null;
      const entryFeedUrl = searchParams.get('feed_url') || null;
      const isEntryScope = searchParams.has('entry_id');
      let feedUrl = scopeListFeedUrl || (isEntryScope ? entryFeedUrl : null);
      const entryId = searchParams.get('entry_id') || '';
      const sortBy = searchParams.get('sort_by') || 'post';
      const sortDir = searchParams.get('sort_dir') || 'desc';
      const readFilter = searchParams.get('read_filter') || 'unread';
      const starOnly = searchParams.get('star_only') || '0';
      const resumeReadFilter = searchParams.get('resume_read_filter') || readFilter;
      const tag = searchParams.get('tag') || '';

      if (!folderId) {
        const rootTree = document.querySelector('.tree[data-root-folder-id]');
        if (rootTree instanceof HTMLElement) {
          folderId = rootTree.getAttribute('data-root-folder-id') || null;
        }
      }

      try {
        if (window.isSingleMode && window.isSingleMode()) {
          const currentPaneLevel = Number.parseInt(document.body.getAttribute('data-single-pane-level') || '0', 10);
          window.sessionStorage.setItem('lectio-single-pane-restore-level', String(Number.isInteger(currentPaneLevel) ? currentPaneLevel : 0));
        }
      } catch (_e) {
        // ignore storage failures
      }

      if (feedUrl && folderId && refreshFeedFolderIdInput && refreshFeedUrlInput && refreshListFeedUrlInput && refreshFeedForm) {
        refreshFeedFolderIdInput.value = folderId;
        refreshFeedUrlInput.value = feedUrl;
        refreshListFeedUrlInput.value = feedUrl;
        if (refreshFeedEntryIdInput) refreshFeedEntryIdInput.value = entryId;
        if (refreshFeedSortByInput) refreshFeedSortByInput.value = sortBy;
        if (refreshFeedSortDirInput) refreshFeedSortDirInput.value = sortDir;
        if (refreshFeedReadFilterInput) refreshFeedReadFilterInput.value = readFilter;
        if (refreshFeedStarOnlyInput) refreshFeedStarOnlyInput.value = starOnly;
        if (refreshFeedResumeReadFilterInput) refreshFeedResumeReadFilterInput.value = resumeReadFilter;
        if (refreshFeedTagInput) refreshFeedTagInput.value = tag;
        await submitRefreshFormAsync(refreshFeedForm);
      } else if (folderId && refreshFolderIdInput && refreshFolderForm) {
        refreshFolderIdInput.value = folderId;
        if (refreshFolderListFeedUrlInput) refreshFolderListFeedUrlInput.value = '';
        if (refreshFolderFeedUrlInput) refreshFolderFeedUrlInput.value = '';
        if (refreshFolderEntryIdInput) refreshFolderEntryIdInput.value = '';
        if (refreshFolderSortByInput) refreshFolderSortByInput.value = sortBy;
        if (refreshFolderSortDirInput) refreshFolderSortDirInput.value = sortDir;
        if (refreshFolderReadFilterInput) refreshFolderReadFilterInput.value = readFilter;
        if (refreshFolderStarOnlyInput) refreshFolderStarOnlyInput.value = starOnly;
        if (refreshFolderResumeReadFilterInput) refreshFolderResumeReadFilterInput.value = resumeReadFilter;
        if (refreshFolderTagInput) refreshFolderTagInput.value = tag;
        await submitRefreshFormAsync(refreshFolderForm);
      }
    }

    window.refreshCurrentFeedOrFolder = refreshCurrentFeedOrFolder;

    function toggleEntryTagsPanel() {
      if (entryTagsForm instanceof HTMLFormElement) {
        if (entryTagsForm.hasAttribute('hidden')) {
          entryTagsForm.removeAttribute('hidden');
          setEntryTagsExpandedState(true);
          entryTagsInput?.focus();
        } else {
          entryTagsForm.setAttribute('hidden', '');
          setEntryTagsExpandedState(false);
        }
      }
    }

    function openSearchRow() {
      const row = document.getElementById('toolbar-search-row');
      const btn = document.getElementById('toolbar-search-btn');
      if (!row) return;
      row.classList.remove('toolbar-search-hidden');
      btn?.classList.add('active');
      btn?.setAttribute('aria-expanded', 'true');
    }

    function closeSearchRow() {
      const row = document.getElementById('toolbar-search-row');
      const btn = document.getElementById('toolbar-search-btn');
      if (!row) return;
      row.classList.add('toolbar-search-hidden');
      btn?.classList.remove('active');
      btn?.setAttribute('aria-expanded', 'false');
    }

    function focusSearchInput() {
      const searchInput = document.getElementById('topbar-search-input');
      if (!(searchInput instanceof HTMLInputElement)) {
        return;
      }
      openSearchRow();
      searchInput.focus();
      searchInput.select();
    }

    document.getElementById('toolbar-search-btn')?.addEventListener('click', () => {
      const row = document.getElementById('toolbar-search-row');
      if (row?.classList.contains('toolbar-search-hidden')) {
        openSearchRow();
        document.getElementById('topbar-search-input')?.focus();
      } else {
        closeSearchRow();
      }
    });

    function shouldHandleGlobalShortcut(event) {
      if (event.defaultPrevented) {
        return false;
      }
      if (isTypingTarget(event.target)) {
        return false;
      }
      if (event.altKey || event.metaKey) {
        return false;
      }
      return true;
    }

    applyLocalTimestamps();
    applyRelativeTimestamps();
    applyAbsoluteTimestamps();
    measureAndSetTileHeight();
    bindEntryTagInteractions();

    // --- Debug panel ---
    const debugConfirmModal = document.getElementById('debug-confirm-modal');
    const debugConfirmMessage = document.getElementById('debug-confirm-message');
    const debugConfirmOk = document.getElementById('debug-confirm-ok');
    let _debugConfirmCallback = null;

    function showDebugConfirm(message, onConfirm) {
      if (!debugConfirmModal || !debugConfirmMessage || !debugConfirmOk) return;
      debugConfirmMessage.textContent = message;
      _debugConfirmCallback = onConfirm;
      debugConfirmModal.removeAttribute('hidden');
    }

    debugConfirmOk?.addEventListener('click', async () => {
      if (!debugConfirmModal) return;
      debugConfirmModal.setAttribute('hidden', '');
      if (typeof _debugConfirmCallback === 'function') {
        await _debugConfirmCallback();
        _debugConfirmCallback = null;
      }
    });

    async function debugClearCache(feedUrl) {
      if (!feedUrl) return;
      const body = new URLSearchParams();
      body.append('feed_url', feedUrl);
      try {
        const resp = await fetch('/debug/clear-lead-image-cache', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
          body: body.toString(),
        });
        const data = await resp.json();
        if (data.ok) {
          // Reset all visible thumbnails for this feed in the post list.
          document.querySelectorAll(`.post-item[data-feed-url="${CSS.escape(feedUrl)}"] .post-thumbnail img`).forEach(img => {
            img.src = '';
            img.closest('.post-thumbnail')?.classList.add('is-empty');
          });
          showToastMessage(`Cleared ${data.deleted} cached image${data.deleted === 1 ? '' : 's'}.`);
        } else {
          showToastMessage('Cache clear failed: ' + (data.error || 'unknown error'));
        }
      } catch (e) {
        showToastMessage('Cache clear request failed.');
      }
    }

    // --- Bypass-cache toggle ---
    function _debugGetCurrentFeedUrl() {
      const params = new URLSearchParams(window.location.search);
      return params.get('list_feed_url') || params.get('feed_url') || null;
    }

    async function _debugRefreshBypassBtn() {
      const btn = document.getElementById('debug-bypass-cache-btn');
      if (!btn) return;
      const feedUrl = _debugGetCurrentFeedUrl();
      if (!feedUrl) { btn.classList.remove('active'); return; }
      try {
        const resp = await fetch('/debug/feed-bypass-state?feed_url=' + encodeURIComponent(feedUrl), { credentials: 'same-origin' });
        const data = await resp.json();
        btn.classList.toggle('active', !!data.bypassed);
      } catch (e) { /* ignore */ }
    }

    document.getElementById('debug-bypass-cache-btn')?.addEventListener('click', async () => {
      const feedUrl = _debugGetCurrentFeedUrl();
      if (!feedUrl) { showToastMessage('No feed selected.'); return; }
      const body = new URLSearchParams();
      body.append('feed_url', feedUrl);
      try {
        const resp = await fetch('/debug/toggle-feed-bypass', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
          body: body.toString(),
        });
        const data = await resp.json();
        const btn = document.getElementById('debug-bypass-cache-btn');
        btn?.classList.toggle('active', !!data.bypassed);
        showToastMessage(data.bypassed ? 'Cache bypass ON — images will be re-fetched.' : 'Cache bypass OFF.');
      } catch (e) {
        showToastMessage('Bypass toggle failed.');
      }
    });

    window.addEventListener('popstate', _debugRefreshBypassBtn);
    _debugRefreshBypassBtn();
    // --- End bypass-cache toggle ---

    document.getElementById('ctx-clear-cache')?.addEventListener('click', () => {
      hideAllContextMenus();
      if (contextTargetType === 'feed' && contextFeedUrl) {
        showDebugConfirm(`Clear image cache for:\n${contextFeedUrl}?`, () => debugClearCache(contextFeedUrl));
      }
    });
    // --- End debug panel ---

    window.addEventListener('click', hideAllContextMenus);
    window.addEventListener('scroll', (event) => {
      const target = event.target;
      if (target instanceof Element && target.closest('.context-menu, .context-submenu')) {
        return;
      }
      hideAllContextMenus();
    }, true);
    window.addEventListener('resize', hideAllContextMenus);
    window.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') {
        return;
      }

      // Close any open action modal (priority order)
      const openModal = document.querySelector(
        '#global-note-modal:not([hidden]), #add-feed-modal:not([hidden]), #action-input-modal:not([hidden]), #feed-properties-modal:not([hidden]), #settings-modal:not([hidden]), #move-entry-modal:not([hidden])'
      );
      if (openModal) {
        openModal.setAttribute('hidden', '');
        closeActionInputModal();
        closeFeedPropertiesModal();
        event.preventDefault();
        return;
      }

      // Close tags panel if open
      if (entryTagsForm instanceof HTMLFormElement && !entryTagsForm.hasAttribute('hidden')) {
        entryTagsForm.setAttribute('hidden', '');
        setEntryTagsExpandedState(false);
        event.preventDefault();
        return;
      }

      // Close search row if focused and empty, or just blur if has content
      const searchInput = document.getElementById('topbar-search-input');
      if (searchInput instanceof HTMLInputElement && document.activeElement === searchInput) {
        if (!searchInput.value.trim()) {
          closeSearchRow();
        }
        searchInput.blur();
        event.preventDefault();
        return;
      }

      // Fall-through: close context menus
      hideAllContextMenus();
    });

    window.addEventListener('keydown', (event) => {
      if (!shouldHandleGlobalShortcut(event)) {
        return;
      }

      const key = (event.key || '').toLowerCase();

      if (event.shiftKey || event.ctrlKey) {
        return;
      }

      if (key === '/') {
        event.preventDefault();
        focusSearchInput();
        return;
      }

      if (key === 'j') {
        event.preventDefault();
        moveActivePostBy(1);
        return;
      }

      if (key === 'k') {
        event.preventDefault();
        moveActivePostBy(-1);
        return;
      }

      if (key === 'n') {
        event.preventDefault();
        moveActivePostSelectionBy(1);
        return;
      }

      if (key === 'p') {
        event.preventDefault();
        moveActivePostSelectionBy(-1);
        return;
      }

      if (key === 'm') {
        event.preventDefault();
        toggleActivePostRead();
        return;
      }

      if (key === 'f' || key === 's') {
        event.preventDefault();
        toggleActivePostSaved();
        return;
      }

      if (key === 'o' || key === 'b') {
        event.preventDefault();
        openActivePostInNewTab();
        return;
      }

      if (key === 'w') {
        event.preventDefault();
        toggleEntryReadability();
        return;
      }

      if (key === 'v') {
        event.preventDefault();
        toggleEntryWebView();
        return;
      }

      if (key === 'a') {
        event.preventDefault();
        openAddFeedModal();
        return;
      }

      if (key === 'r') {
        event.preventDefault();
        refreshCurrentFeedOrFolder();
        return;
      }

      if (key === 't') {
        event.preventDefault();
        toggleEntryTagsPanel();
        return;
      }

      if (key === 'd') {
        event.preventDefault();
        document.getElementById('folders-collapse-btn')?.click();
        return;
      }

      // e = email (not yet implemented — placeholder)
    });
