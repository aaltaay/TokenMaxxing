'use strict';

// The app's own version and updates, in the notifications panel. The main
// process owns every decision (updates.js); this only draws its view and sends
// the click back.
(() => {
  const label = document.getElementById('updateStatus');
  const button = document.getElementById('btnUpdate');
  const later = document.getElementById('btnUpdateLater');
  let state = {};

  function message(s) {
    const installed = s.installed || 'This version';
    switch (s.status) {
      case 'disabled': return `${installed} · Updates come with the Windows installer`;
      case 'checking': return 'Checking for updates…';
      case 'current': return `${installed} · Up to date`;
      case 'downloading': return s.retry
        ? `Downloading ${s.version} · ${s.percent}% · connection dropped, retrying (attempt ${s.retry})`
        : `Downloading ${s.version} · ${s.percent}%`;
      case 'ready': return s.error ? `${s.version} is ready · last attempt failed: ${s.error}` : `${s.version} is ready to install`;
      case 'installing': return `Installing ${s.version}…`;
      case 'failed': return s.failedStage === 'download'
        ? `Download of ${s.version} stopped at ${s.percent}%; what arrived is kept`
        : 'Could not check for updates. Check your connection and retry.';
      default: return installed;
    }
  }

  function paint(value) {
    if (!value) return;
    state = value;
    label.textContent = message(state);
    label.title = state.status === 'failed' && state.error ? state.error : '';
    button.hidden = state.status === 'disabled';
    button.disabled = ['checking', 'downloading', 'installing'].includes(state.status);
    button.textContent = state.status === 'ready' ? 'Restart to update'
      : state.status === 'failed' ? (state.failedStage === 'download' ? 'Resume' : 'Retry')
        : 'Check for updates';
    later.hidden = state.status !== 'ready' || Boolean(state.dismissed);
  }

  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      if (state.status === 'ready') {
        await window.hud.installUpdate();
        paint(await window.hud.updateStatus());
      } else paint(await window.hud.checkUpdates());
    } catch { paint({...state, status: 'failed', failedStage: 'check'}); }
  });
  later.addEventListener('click', async () => {
    try { paint(await window.hud.laterUpdate()); } catch {}
  });
  window.hud.on('updateStatus', paint);
  window.hud.updateStatus().then(paint).catch(() => paint({status: 'failed', failedStage: 'check'}));
})();
