// ── Pull-to-refresh (single-pane / mobile only) ────────────────────────
// Attaches PTR gesture to a scrollable element. When in single-pane mode
// the user can pull down from the top to trigger a feed/folder refresh.
// triggerFn is called once the pull passes the threshold and is released.
//
// paneEl: the visible pane element (.pane-folders or .pane-posts) used to
//         anchor the indicator and detect scroll position.
// scrollEl: the element that actually scrolls (.tree for folders, .posts
//           for posts). May differ from paneEl.
function setupPullToRefresh(paneEl, scrollEl, triggerFn) {
  if (!paneEl || !scrollEl) return;
  if (paneEl.dataset.ptrBound === '1') return;
  paneEl.dataset.ptrBound = '1';

  const START_PULL = 14;  // ignore light swipe/scroll jitter before this pull distance
  const THRESHOLD = 78;   // pull distance needed before hold-to-arm starts
  const HOLD_MS = 180;    // short deliberate hold required once threshold is reached
  const MAX_PULL  = 108;  // px cap for visual travel

  let startY = 0;
  let pulling = false;
  let armed   = false;
  let holdReady = false;
  let loading = false;
  let armHoldTimer = null;

  // Folders: indicator is position:absolute inside .pane-folders (overflow:hidden clips it when above top).
  // Posts: indicator is position:fixed, appended to body (unaffected by scroll container).
  const isPostsPane = paneEl.classList.contains('pane-posts');

  const indicator = document.createElement('div');
  indicator.className = 'ptr-indicator';
  indicator.innerHTML = '<div class="ptr-icon"><span class="material-symbols-rounded" aria-hidden="true">refresh</span></div>';
  // Start fully hidden — display:none avoids any transform math leaking through
  indicator.style.display = 'none';

  if (isPostsPane) {
    document.body.appendChild(indicator);
  } else {
    // Ensure parent is position:relative so absolute child is anchored correctly
    if (getComputedStyle(paneEl).position === 'static') paneEl.style.position = 'relative';
    paneEl.insertBefore(indicator, paneEl.firstChild);
  }

  function getTopbarHeight() {
    return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topbar-height')) || 50;
  }

  function showIndicatorAt(travel) {
    travel = Math.min(Math.max(travel, 0), MAX_PULL);
    indicator.style.display = 'flex';
    indicator.style.transition = 'none';
    if (isPostsPane) {
      // Fixed indicator: top=0 in CSS, translate from -56px (hidden) toward topbar-height (visible below topbar)
      const th = getTopbarHeight();
      indicator.style.transform = `translateX(-50%) translateY(calc(-56px + ${travel}px))`;
      // Once travel exceeds (56 - th) the icon clears the topbar top; clamp visual at MAX_PULL
    } else {
      indicator.style.transform = `translateY(calc(-56px + ${travel}px))`;
    }
    indicator.classList.toggle('ptr-armed', armed);
    indicator.classList.toggle('ptr-ready', holdReady);
  }

  function showIndicatorLoading() {
    indicator.style.display = 'flex';
    indicator.style.transition = 'none';
    if (isPostsPane) {
      const th = getTopbarHeight();
      indicator.style.transform = `translateX(-50%) translateY(${th}px)`;
    } else {
      indicator.style.transform = 'translateY(0)';
    }
  }

  function hideIndicator(animate) {
    if (armHoldTimer) {
      clearTimeout(armHoldTimer);
      armHoldTimer = null;
    }
    if (animate) {
      indicator.style.transition = 'transform 200ms ease';
      if (isPostsPane) {
        indicator.style.transform = 'translateX(-50%) translateY(-56px)';
      } else {
        indicator.style.transform = 'translateY(-56px)';
      }
      setTimeout(() => {
        indicator.style.display = 'none';
        indicator.style.transition = 'none';
        indicator.classList.remove('ptr-loading');
        indicator.classList.remove('ptr-armed');
        indicator.classList.remove('ptr-ready');
        loading = false; pulling = false; armed = false; holdReady = false;
      }, 220);
    } else {
      indicator.style.display = 'none';
      indicator.classList.remove('ptr-loading');
      indicator.classList.remove('ptr-armed');
      indicator.classList.remove('ptr-ready');
      loading = false; pulling = false; armed = false; holdReady = false;
    }
  }

  function startArmHoldIfNeeded() {
    if (holdReady || armHoldTimer || !armed || loading || !pulling) return;
    armHoldTimer = setTimeout(() => {
      armHoldTimer = null;
      if (!pulling || !armed || loading) return;
      holdReady = true;
      indicator.classList.add('ptr-ready');
    }, HOLD_MS);
  }

  // Touch target is paneEl so we get events from anywhere in the pane,
  // including children that have their own touch handlers (they don't stopPropagation).
  paneEl.addEventListener('touchstart', (e) => {
    if (!window.isSingleMode()) return;
    if (loading) return;
    // Allow up to 2px tolerance for sub-pixel scroll positions
    if (scrollEl.scrollTop > 2) return;
    if (e.touches.length !== 1) return;
    startY = e.touches[0].clientY;
    pulling = true;
    armed   = false;
    holdReady = false;
    if (armHoldTimer) {
      clearTimeout(armHoldTimer);
      armHoldTimer = null;
    }
  }, { passive: true });

  paneEl.addEventListener('touchmove', (e) => {
    if (!pulling || loading) return;
    const dy = e.touches[0].clientY - startY;
    if (dy <= START_PULL) {
      // Ignore tiny movements; abort if scrolling up
      if (dy < 0) hideIndicator(false);
      return;
    }
    showIndicatorAt(dy);
    const nextArmed = dy >= THRESHOLD;
    if (nextArmed !== armed) {
      armed = nextArmed;
      if (!armed) {
        holdReady = false;
        indicator.classList.remove('ptr-ready');
        if (armHoldTimer) {
          clearTimeout(armHoldTimer);
          armHoldTimer = null;
        }
      }
    }
    if (armed && !holdReady) {
      startArmHoldIfNeeded();
    }
  }, { passive: true });

  paneEl.addEventListener('touchend', (e) => {
    if (!pulling || loading) return;
    if (holdReady) {
      // preventDefault suppresses the synthesized click that would follow
      e.preventDefault();
      loading = true;
      armed   = false;
      pulling = false;
      indicator.classList.add('ptr-loading');
      showIndicatorLoading();
      // Brief pause so the spinner is visible before the async refresh starts.
      setTimeout(() => {
        let ptrError = null;
        Promise.resolve(triggerFn()).catch((error) => {
          ptrError = error;
          console.error('[ptr] refresh failed:', error);
        }).finally(() => {
          // Always hide if we're still in loading state (page navigation clears it naturally).
          if (loading) {
            hideIndicator(false);
            if (ptrError) {
              const msg = ptrError?.message ? ' (' + ptrError.message + ')' : '';
              showToastMessage('Could not refresh view.' + msg);
            }
            // If no error and still loading, trigger did nothing (e.g. no folder selected) — just hide silently.
          }
        });
      }, 250);
    } else {
      hideIndicator(true);
    }
  }); // intentionally NOT passive so preventDefault works

  paneEl.addEventListener('touchcancel', () => hideIndicator(false), { passive: true });
}

function bindPullToRefresh() {
  // Folders pane: pane = .pane-folders, scroll container = .tree.
  // Use a folder-only trigger (ignore active post item's feed) so PTR always
  // refreshes the current folder scope, not the last-opened feed.
  const ptrFoldersPaneEl = document.querySelector('.pane-folders');
  const ptrFoldersScrollEl = document.querySelector('.pane-folders .tree');
  setupPullToRefresh(ptrFoldersPaneEl, ptrFoldersScrollEl, async () => {
    const folderId = resolveCurrentFolderId();
    console.log('[ptr] folders trigger: folderId=', folderId, 'form=', refreshFolderForm, 'input=', refreshFolderIdInput);
    if (!folderId) throw new Error('no folder_id available');
    if (!refreshFolderIdInput || !refreshFolderForm) throw new Error('refresh form not found');
    refreshFolderIdInput.value = folderId;
    console.log('[ptr] folders: submitting refresh for folder_id=', folderId, 'action=', refreshFolderForm.action, 'method=', refreshFolderForm.method);
    await submitRefreshFormAsync(refreshFolderForm, 0);
  });

  // Posts pane: .posts is the actual scrolling list in practice across
  // layouts; use it for top-detection while keeping touch listeners on pane.
  const ptrPostsPaneEl = document.querySelector('.pane-posts');
  const ptrPostsScrollEl = document.querySelector('.posts') || ptrPostsPaneEl;
  setupPullToRefresh(ptrPostsPaneEl, ptrPostsScrollEl, () => refreshCurrentFeedOrFolder(1));
}

bindPullToRefresh();
// ── End pull-to-refresh ────────────────────────────────────────────────