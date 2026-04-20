(function(){
  const SINGLE_THRESHOLD = 720;
  const MEDIUM_THRESHOLD = 1100;
  const WIDE_LEFT_COLLAPSED_KEY = 'lectio-wide-left-collapsed';
  let singleMode = window.innerWidth <= SINGLE_THRESHOLD;
  let layoutMode = 'wide';
  const bodyEl = document.body;
  const mediumPaneBackdrop = document.getElementById('medium-pane-backdrop');

  function getWideLeftCollapsedPreference() {
    return window.localStorage.getItem(WIDE_LEFT_COLLAPSED_KEY) === '1';
  }

  function updateCollapseBtnLabel() {
    if (!folderCollapseBtn) return;
    let label;
    if (layoutMode === 'wide') {
      const collapsed = bodyEl.getAttribute('data-left-collapsed') === '1';
      const floating = bodyEl.hasAttribute('data-left-floating');
      label = collapsed && !floating ? 'Pin folders pane' : 'Collapse folders pane';
    } else if (layoutMode === 'medium') {
      const open = bodyEl.getAttribute('data-medium-left-open') === '1';
      label = open ? 'Collapse folders pane' : 'Open folders pane';
    } else {
      return;
    }
    folderCollapseBtn.title = label;
    folderCollapseBtn.setAttribute('aria-label', label);
  }

  function setWideLeftCollapsed(collapsed, persist = true) {
    bodyEl.setAttribute('data-left-collapsed', collapsed ? '1' : '0');
    if (persist) {
      window.localStorage.setItem(WIDE_LEFT_COLLAPSED_KEY, collapsed ? '1' : '0');
    }
    updateCollapseBtnLabel();
  }

  function setMediumLeftOpen(isOpen) {
    bodyEl.setAttribute('data-medium-left-open', isOpen ? '1' : '0');
    if (mediumPaneBackdrop) {
      if (isOpen) {
        mediumPaneBackdrop.removeAttribute('hidden');
      } else {
        mediumPaneBackdrop.setAttribute('hidden', '');
      }
    }
  }

  function updateSingleMode() {
    const width = window.innerWidth;
    if (width <= SINGLE_THRESHOLD) {
      layoutMode = 'narrow';
    } else if (width <= MEDIUM_THRESHOLD) {
      layoutMode = 'medium';
    } else {
      layoutMode = 'wide';
    }

    bodyEl.setAttribute('data-layout-mode', layoutMode);
    singleMode = layoutMode === 'narrow';
    bodyEl.setAttribute('data-single-mode', singleMode ? '1' : '0');

    if (!singleMode) {
      bodyEl.removeAttribute('data-single-pane-level');
    } else if (!bodyEl.getAttribute('data-single-pane-level')) {
      setSinglePaneLevel(0);
    }

    if (layoutMode === 'wide') {
      setMediumLeftOpen(false);
      setWideLeftCollapsed(getWideLeftCollapsedPreference(), false);
    } else {
      setWideLeftCollapsed(false, false);
      setMediumLeftOpen(false);
      bodyEl.removeAttribute('data-left-floating');
    }
    updateVisualViewportInset();
  }

  function updateVisualViewportInset() {
    try {
      const vv = window.visualViewport;
      let inset = 0;
      if (vv) {
        inset = Math.max(0, window.innerHeight - vv.height - (vv.offsetTop || 0));
      } else {
        inset = 0;
      }
      document.documentElement.style.setProperty('--vv-bottom-inset', inset + 'px');
    } catch (e) {
      // ignore
    }
  }

  // listen for visual viewport changes to update bottom inset
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', updateVisualViewportInset, { passive: true });
    window.visualViewport.addEventListener('scroll', updateVisualViewportInset, { passive: true });
  }
  window.addEventListener('resize', updateVisualViewportInset, { passive: true });

  // Set dynamic --vh for accurate mobile viewport height
  function setVh() {
    try {
      const vh = window.innerHeight * 0.01;
      document.documentElement.style.setProperty('--vh', `${vh}px`);
    } catch (e) {
      // ignore
    }
  }
  setVh();
  window.addEventListener('resize', setVh, { passive: true });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', setVh, { passive: true });
    window.visualViewport.addEventListener('scroll', setVh, { passive: true });
  }

  function setSinglePaneLevel(level) {
    bodyEl.setAttribute('data-single-pane-level', String(level));
  }

  function rememberNextSinglePaneLevel(level) {
    try {
      window.sessionStorage.setItem('lectio-next-single-pane-level', String(level));
    } catch (e) {
      // ignore
    }
  }

  function restoreRememberedSinglePaneLevel() {
    try {
      const rememberedLevel = window.sessionStorage.getItem('lectio-next-single-pane-level');
      if (!rememberedLevel) {
        return;
      }
      window.sessionStorage.removeItem('lectio-next-single-pane-level');
      if (window.isSingleMode && window.isSingleMode()) {
        setSinglePaneLevel(Number.parseInt(rememberedLevel, 10) || 0);
      }
    } catch (e) {
      // ignore
    }
  }

  // Topbar hide/show on scroll in single-pane mode
  let lastScrollTop = 0;
  let scrollTimeoutId = null;
  function bindSinglePaneScrollBehavior() {
    function onScrollPane(e) {
      const pane = e.target;
      const st = pane.scrollTop;
      const topbar = document.querySelector('.topbar');
      if (!topbar) return;
      if (st > lastScrollTop + 10) {
        topbar.classList.add('topbar-hidden');
      } else if (st < lastScrollTop - 10) {
        topbar.classList.remove('topbar-hidden');
      }
      lastScrollTop = st;
      if (scrollTimeoutId) clearTimeout(scrollTimeoutId);
      // show topbar when scroll stops briefly
      scrollTimeoutId = window.setTimeout(() => topbar.classList.remove('topbar-hidden'), 1200);
    }

    const postsPane = document.querySelector('.pane-posts');
    const entryPane = document.querySelector('.pane-entry');
    if (postsPane) {
      postsPane.addEventListener('scroll', onScrollPane, { passive: true });
    }
    if (entryPane) {
      entryPane.addEventListener('scroll', onScrollPane, { passive: true });
    }
  }

  // Swipe gestures: post list items and entry pane navigation
  function bindSwipeGestures() {
    // post list swipe for toggles
    function attachPostSwipe(postItem) {
      let sx = 0, sy = 0, dx = 0, dy = 0, touching = false;
      const threshold = 40;
      postItem.addEventListener('touchstart', (ev) => {
        const t = ev.changedTouches[0]; sx = t.clientX; sy = t.clientY; touching = true; dx = 0; dy = 0;
      }, { passive: true });
      postItem.addEventListener('touchmove', (ev) => {
        if (!touching) return; const t = ev.changedTouches[0]; dx = t.clientX - sx; dy = t.clientY - sy; }, { passive: true });
      postItem.addEventListener('touchend', (ev) => {
        if (!touching) return; touching = false; if (Math.abs(dx) < Math.abs(dy)) return; if (Math.abs(dx) < threshold) return;
        // Right swipe -> toggle read/unread, Left swipe -> toggle saved
        if (dx > 0) {
          const btn = postItem.querySelector('.post-read-toggle'); if (btn) btn.click();
        } else {
          const btn = postItem.querySelector('.post-save-toggle'); if (btn) btn.click();
        }
      }, { passive: true });
    }

    for (const postItem of document.querySelectorAll('.post-item')) {
      attachPostSwipe(postItem);
    }

    // entry pane swipe for next/previous
    const entryPaneEl = document.querySelector('.pane-entry');
    if (entryPaneEl) {
      let sx=0, sy=0, dx=0, dy=0, touching=false;
      entryPaneEl.addEventListener('touchstart', (ev)=>{ const t=ev.changedTouches[0]; sx=t.clientX; sy=t.clientY; touching=true; dx=0; dy=0; }, { passive: true });
      entryPaneEl.addEventListener('touchmove', (ev)=>{ if(!touching) return; const t=ev.changedTouches[0]; dx=t.clientX-sx; dy=t.clientY-sy; }, { passive: true });
      entryPaneEl.addEventListener('touchend', (ev)=>{
        if(!touching) return; touching=false; if(Math.abs(dx) < Math.abs(dy)) return; const threshold=60; if(Math.abs(dx) < threshold) return;
        if (dx < 0) { // swipe left -> next
          navigateEntry(1);
        } else { // swipe right -> prev
          navigateEntry(-1);
        }
      }, { passive: true });
    }
  }

  function navigateEntry(direction) {
    // find active post in posts pane
    const posts = Array.from(document.querySelectorAll('.post-item')).filter(p=>!p.classList.contains('post-item-hidden'));
    const active = document.querySelector('.post-item.active');
    if (!active) return;
    const idx = posts.indexOf(active);
    if (idx === -1) return;
    const nextIdx = idx + direction;
    if (nextIdx < 0 || nextIdx >= posts.length) return;
    const target = posts[nextIdx];
    const link = target.querySelector('.post-main-link');
    if (link) loadEntryPaneWithoutFullRefresh(link.href);
  }


  // Expose for debugging
  window.setSinglePaneLevel = setSinglePaneLevel;
  window.isSingleMode = () => singleMode;

  window.addEventListener('resize', () => updateSingleMode());

  // Intercept internal app navigation links and perform SPA loads.
  // Do not intercept external links, modified clicks, or the brand/logo
  // (which must remain a bare `/`).
  document.addEventListener('click', function(e){
    const a = e.target.closest('a');
    if (!a) return;
    // ignore if already prevented or not a primary button click or modified
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    // ignore links that open in new contexts
    if (a.target && a.target.toLowerCase() !== '_self') return;
    // only intercept internal same-origin links
    let href;
    try {
      href = a.href;
      const u = new URL(href, window.location.origin);
      if (u.origin !== window.location.origin) return;
    } catch (err) {
      return;
    }
    // Don't intercept the brand/logo link
    if (a.classList && a.classList.contains('topbar-brand')) return;
    // Only intercept app navigation (folders/feeds/tags/tree links)
    if (!(a.matches('.feed-link, .tag-link, .tree-item') || a.closest('.tree'))) return;

    // For folder rows, only navigate when the visible folder name text is tapped.
    if (a.matches('.root-item, .child-item')) {
      const targetEl = e.target instanceof Element ? e.target : null;
      if (!targetEl || !targetEl.closest('.name')) {
        e.preventDefault();
        return;
      }
    }

    // If this is a filter pill click (All/Unread/Star), persist the
    // `lectio-read-filter` immediately so the SPA loader can include it
    // in headers for the server before performing the fetch. This avoids
    // a race between capture-phase listeners.
    try {
      if (a.matches && a.matches('.filter-pill')) {
        const u2 = new URL(href, window.location.origin);
        const rf = u2.searchParams.get('read_filter') || u2.searchParams.get('resume_read_filter');
        if (rf) {
          window.localStorage.setItem('lectio-read-filter', rf);
          try { document.cookie = `lectio_read_filter=${encodeURIComponent(rf)}; path=/; max-age=${60*60*24*365}`; } catch(_) {}
        }
      }
    } catch (err) {}

    e.preventDefault();
    // Preserve explicit URL state (including read_filter) so filter pills
    // and list content stay in sync after scope switches.
    let navUrl = href;
    try {
      const u2 = new URL(href, window.location.origin);
      applyCurrentScopeStateToScopeLink(u2, a);
      const searchInput = document.getElementById('topbar-search-input');
      const activeQuery = (searchInput instanceof HTMLInputElement ? searchInput.value : '').trim();
      if (activeQuery && !u2.searchParams.has('q')) {
        u2.searchParams.set('q', activeQuery);
      }
      navUrl = u2.toString();
    } catch (err) {
      // fallback to original href
    }
    if (window.loadScopePanesWithoutFullRefresh) {
      loadScopePanesWithoutFullRefresh(navUrl).then(()=> {
        try {
          if (window.isSingleMode && window.isSingleMode()) {
            setSinglePaneLevel(1);
          }
        } catch(e){}
        if (layoutMode === 'wide') {
          bodyEl.removeAttribute('data-left-floating');
        } else if (layoutMode === 'medium') {
          setMediumLeftOpen(false);
          mediumFlyoutClickOpen = false;
        }
      }).catch((_err)=> {});
    } else {
      window.location.href = navUrl;
    }
  }, true);

  // Persist read_filter when user clicks filter pills (All / Unread / Star)
  document.addEventListener('click', function(e) {
    const pill = e.target.closest('.filter-pill');
    if (!pill) return;
    try {
      const u = new URL(pill.href, window.location.origin);
      const rf = u.searchParams.get('read_filter') || u.searchParams.get('resume_read_filter');
      if (rf) {
        window.localStorage.setItem('lectio-read-filter', rf);
        try { document.cookie = `lectio_read_filter=${encodeURIComponent(rf)}; path=/; max-age=${60*60*24*365}`; } catch(_){}
      }
    } catch (err) {
      // ignore
    }
  }, true);

  // Entry pane mutation observer is bound in `refreshEntryPaneRefs()`

  if (mediumPaneBackdrop) {
    mediumPaneBackdrop.addEventListener('click', () => {
      mediumFlyoutClickOpen = false;
      setMediumLeftOpen(false);
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') {
      return;
    }
    if (layoutMode === 'medium' && bodyEl.getAttribute('data-medium-left-open') === '1') {
      mediumFlyoutClickOpen = false;
      setMediumLeftOpen(false);
    }
    if (appMenuDetails?.open) {
      appMenuDetails.removeAttribute('open');
    }
  });

  // Folders pane in-pane collapse/expand + hover floater
  const folderCollapseBtn = document.getElementById('folders-collapse-btn');
  const folderPaneEl = document.querySelector('.pane-folders');
  let mediumFlyoutManualClose = false;
  let mediumFlyoutClickOpen = false;
  updateCollapseBtnLabel();

  if (folderCollapseBtn) {
    folderCollapseBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (layoutMode === 'wide') {
        const collapsed = bodyEl.getAttribute('data-left-collapsed') === '1';
        if (collapsed) {
          setWideLeftCollapsed(false, true);
          bodyEl.removeAttribute('data-left-floating');
        } else {
          setWideLeftCollapsed(true, true);
        }
      } else if (layoutMode === 'medium') {
        const isOpen = bodyEl.getAttribute('data-medium-left-open') === '1';
        if (isOpen) {
          mediumFlyoutManualClose = true;
          mediumFlyoutClickOpen = false;
          setMediumLeftOpen(false);
        } else {
          mediumFlyoutManualClose = false;
          mediumFlyoutClickOpen = true;
          setMediumLeftOpen(true);
        }
      }
    });
  }

  if (folderPaneEl) {
    folderPaneEl.addEventListener('mouseenter', () => {
      if (layoutMode === 'wide' && bodyEl.getAttribute('data-left-collapsed') === '1') {
        bodyEl.setAttribute('data-left-floating', '1');
        updateCollapseBtnLabel();
      } else if (layoutMode === 'medium' && !mediumFlyoutManualClose) {
        bodyEl.setAttribute('data-medium-left-open', '1');
        mediumFlyoutClickOpen = false;
      }
    });
    folderPaneEl.addEventListener('mouseleave', () => {
      if (layoutMode === 'wide') {
        bodyEl.removeAttribute('data-left-floating');
        updateCollapseBtnLabel();
      } else if (layoutMode === 'medium') {
        if (!mediumFlyoutClickOpen) {
          setMediumLeftOpen(false);
        }
        mediumFlyoutManualClose = false;
      }
    });
    // Tap on strip to open flyout (touch / pointer click in medium mode)
    folderPaneEl.addEventListener('click', () => {
      if (layoutMode === 'medium' && bodyEl.getAttribute('data-medium-left-open') !== '1') {
        setMediumLeftOpen(true);
        mediumFlyoutClickOpen = true;
      }
    });
  }

  document.addEventListener('click', (event) => {
    if (layoutMode === 'wide'
      && bodyEl.getAttribute('data-left-collapsed') === '1'
      && bodyEl.getAttribute('data-left-floating') === '1'
      && folderPaneEl
      && !folderPaneEl.contains(event.target)) {
      bodyEl.removeAttribute('data-left-floating');
    }
  }, true);

  // Close hamburger menu when clicking outside it
  const appMenuDetails = document.querySelector('.hamburger-menu.topbar-menu');
  if (appMenuDetails) {
    document.addEventListener('click', (e) => {
      if (appMenuDetails.open && !appMenuDetails.contains(e.target)) {
        appMenuDetails.removeAttribute('open');
      }
    }, true);
  }

  // init
  updateSingleMode();
  restoreRememberedSinglePaneLevel();
  // Ensure entry pane refs and observer are bound for initial load
  try { refreshEntryPaneRefs(); } catch (e) {}
  // (no URL mutation) rely on SPA interception + headers to persist filter
  // bind single-pane niceties
  bindSinglePaneScrollBehavior();
  bindSwipeGestures();

  // Leave the brand/logo as a normal bare link to `/` (no interception).
})();