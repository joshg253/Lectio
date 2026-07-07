/*
 * Lectio global audio player.
 *
 * A single persistent <audio> element + control bar lives in index.html,
 * outside the entry-pane swap target, so playback survives navigation. Play
 * triggers injected into entry content (`.podcast-player[data-audio-src]`)
 * load a track into this player instead of rendering an inline <audio> that
 * would be ripped out of the DOM on pane-swap.
 *
 * State (current track, position, speed) is client-side only; no server/DB.
 */
(function () {
  'use strict';

  var SPEEDS = [1, 1.25, 1.5, 1.75, 2, 0.75];
  var SPEED_KEY = 'lectio.player.speed';

  var bar, audio, playBtn, titleEl, seek, curTime, durTime, speedBtn, dlLink, closeBtn;
  var currentSrc = null;
  var currentReturnUrl = null;
  var seeking = false;

  function fmt(secs) {
    if (!isFinite(secs) || secs < 0) return '0:00';
    secs = Math.floor(secs);
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    var s = secs % 60;
    var mm = (h && m < 10) ? ('0' + m) : String(m);
    var ss = s < 10 ? ('0' + s) : String(s);
    return (h ? (h + ':') : '') + mm + ':' + ss;
  }

  function loadSpeed() {
    var v = 1;
    try { v = parseFloat(localStorage.getItem(SPEED_KEY)); } catch (e) {}
    return (v && SPEEDS.indexOf(v) !== -1) ? v : 1;
  }

  function applySpeed(v) {
    audio.playbackRate = v;
    speedBtn.textContent = v + '×';
    try { localStorage.setItem(SPEED_KEY, String(v)); } catch (e) {}
  }

  function cycleSpeed() {
    var cur = audio.playbackRate;
    var idx = SPEEDS.indexOf(cur);
    applySpeed(SPEEDS[(idx + 1) % SPEEDS.length]);
  }

  function setPlayIcon(playing) {
    playBtn.textContent = playing ? '❚❚' : '▶';
    playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    bar.classList.toggle('is-playing', playing);
  }

  function showBar() {
    bar.hidden = false;
    bar.classList.add('is-active');
    document.body.classList.add('has-audio-player');
  }

  function play(src, title, downloadUrl, returnUrl) {
    src = safeMediaUrl(src);
    downloadUrl = safeMediaUrl(downloadUrl);
    if (!src) return;
    if (src !== currentSrc) {
      currentSrc = src;
      audio.src = src;
      titleEl.textContent = title || 'Audio';
      titleEl.title = returnUrl ? 'Open the post this audio is from' : (title || '');
      currentReturnUrl = returnUrl || null;
      titleEl.classList.toggle('is-link', !!returnUrl);
      if (dlLink) {
        if (downloadUrl) { dlLink.href = downloadUrl; dlLink.hidden = false; }
        else { dlLink.hidden = true; }
      }
      seek.value = 0;
      curTime.textContent = '0:00';
      durTime.textContent = '0:00';
    }
    showBar();
    pauseOthers();
    audio.play().catch(function () {});
  }

  // Silence any other <audio>/<video> on the page (e.g. a feed's own inline
  // player) so only the global player is heard.
  function pauseOthers() {
    var media = document.querySelectorAll('audio, video');
    for (var i = 0; i < media.length; i++) {
      var el = media[i];
      if (el === audio) continue;
      try { el.pause(); } catch (e) {}
    }
  }

  // Adopted URLs originate in feed content; only allow http(s) or same-origin
  // relative URLs so e.g. a javascript: href can never reach audio.src/dlLink.
  function safeMediaUrl(src) {
    if (!src) return '';
    try {
      var u = new URL(src, location.href);
      if (u.protocol === 'http:' || u.protocol === 'https:') return src;
    } catch (e) {}
    return '';
  }

  function mediaSrc(el) {
    var src = el.currentSrc || el.getAttribute('src') || '';
    if (!src) {
      var source = el.querySelector('source[src]');
      if (source) src = source.getAttribute('src');
    }
    return src || '';
  }

  // Take over inline <audio> elements that arrive inside feed content (the feed
  // embedded its own player, so the backend didn't inject a trigger). Replace
  // each with a Play trigger that routes into the global player, so playback is
  // unified and survives navigation.
  function adoptInlineAudio(root) {
    root = root || document;
    var list = Array.prototype.slice.call(root.querySelectorAll('audio:not(.gap-audio)'));
    if (root.matches && root.matches('audio:not(.gap-audio)')) list.push(root);
    for (var i = 0; i < list.length; i++) {
      var el = list[i];
      if (el.getAttribute('data-gap-adopted')) continue;
      var src = safeMediaUrl(mediaSrc(el));
      if (!src) continue;
      el.setAttribute('data-gap-adopted', '1');
      try { el.pause(); } catch (e) {}
      var title = '';
      var t = document.querySelector('.entry-pane-title-link, .entry-pane-title');
      if (t) title = (t.textContent || '').trim();

      // Build with DOM APIs — src/title come from feed content, so they must
      // never be interpolated into markup strings.
      var host = document.createElement('div');
      host.className = 'podcast-player';
      host.setAttribute('data-audio-src', src);
      host.setAttribute('data-audio-title', title || 'Audio');
      host.setAttribute('data-audio-download', src);
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'podcast-play-trigger';
      btn.setAttribute('aria-label', 'Play audio in player');
      var icon = document.createElement('span');
      icon.className = 'podcast-play-trigger-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '▶';
      var label = document.createElement('span');
      label.className = 'podcast-play-trigger-label';
      label.textContent = 'Play audio';
      btn.appendChild(icon);
      btn.appendChild(label);
      var dl = document.createElement('a');
      dl.className = 'podcast-download-link';
      dl.href = src;
      dl.setAttribute('download', '');
      dl.textContent = 'Download';
      host.appendChild(btn);
      host.appendChild(dl);
      if (el.parentNode) {
        el.parentNode.replaceChild(host, el);
      }
    }
  }

  function stop() {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    currentSrc = null;
    bar.hidden = true;
    bar.classList.remove('is-active');
    document.body.classList.remove('has-audio-player');
  }

  function togglePlay() {
    if (!currentSrc) return;
    if (audio.paused) audio.play().catch(function () {});
    else audio.pause();
  }

  function init() {
    bar = document.getElementById('global-audio-player');
    if (!bar) return;
    audio = bar.querySelector('.gap-audio');
    playBtn = bar.querySelector('.gap-play');
    titleEl = bar.querySelector('.gap-title');
    seek = bar.querySelector('.gap-seek');
    curTime = bar.querySelector('.gap-cur');
    durTime = bar.querySelector('.gap-dur');
    speedBtn = bar.querySelector('.gap-speed');
    dlLink = bar.querySelector('.gap-download');
    closeBtn = bar.querySelector('.gap-close');

    applySpeed(loadSpeed());

    playBtn.addEventListener('click', togglePlay);
    speedBtn.addEventListener('click', cycleSpeed);
    if (closeBtn) closeBtn.addEventListener('click', stop);

    audio.addEventListener('play', function () { setPlayIcon(true); });
    audio.addEventListener('pause', function () { setPlayIcon(false); });
    audio.addEventListener('ended', function () { setPlayIcon(false); });
    audio.addEventListener('loadedmetadata', function () {
      seek.max = isFinite(audio.duration) ? audio.duration : 0;
      durTime.textContent = fmt(audio.duration);
    });
    audio.addEventListener('timeupdate', function () {
      if (seeking) return;
      seek.value = audio.currentTime;
      curTime.textContent = fmt(audio.currentTime);
    });

    seek.addEventListener('input', function () {
      seeking = true;
      curTime.textContent = fmt(parseFloat(seek.value));
    });
    seek.addEventListener('change', function () {
      audio.currentTime = parseFloat(seek.value);
      seeking = false;
    });

    // Delegated: play triggers injected into entry content.
    document.addEventListener('click', function (ev) {
      var trigger = ev.target.closest && ev.target.closest('.podcast-play-trigger');
      if (!trigger) return;
      var host = trigger.closest('.podcast-player');
      if (!host) return;
      ev.preventDefault();
      play(
        host.getAttribute('data-audio-src'),
        host.getAttribute('data-audio-title'),
        host.getAttribute('data-audio-download'),
        location.href  // the entry currently open — lets us jump back to it
      );
    });

    // Click the now-playing title to return to the post the audio came from.
    // Use the app's in-app pane swap so playback keeps going; only fall back to
    // a full navigation if that helper isn't available.
    titleEl.addEventListener('click', function () {
      if (!currentReturnUrl || currentReturnUrl === location.href) return;
      if (typeof window.loadEntryPaneWithoutFullRefresh === 'function') {
        window.loadEntryPaneWithoutFullRefresh(currentReturnUrl);
      } else {
        location.href = currentReturnUrl;
      }
    });

    // Feeds sometimes embed their own <audio>; adopt those into the global
    // player so playback is unified and there's never a second stream.
    adoptInlineAudio(document);
    var host = document.querySelector('.panes') || document.body;
    new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var added = mutations[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          if (added[j].nodeType === 1) adoptInlineAudio(added[j]);
        }
      }
    }).observe(host, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
