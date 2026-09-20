'use strict';

(() => {
  const label = document.getElementById('updateStatus');
  const button = document.getElementById('btnUpdate');
  let state = {};
  function paint(value) {
    if (!value) return;
    state = value;
    const messages = {
      disabled: `v${state.currentVersion} · Updates require the Windows installer`,
      idle: `v${state.currentVersion}`,
      checking: 'Checking for updates…',
      current: `v${state.currentVersion} · Up to date`,
      downloading: `Downloading v${state.version} · ${state.percent || 0}%`,
      ready: `v${state.version} is ready`,
      installing: 'Restarting to install the update…',
      error: 'Could not update. Check your connection and retry.',
    };
    label.textContent = messages[state.status] || `v${state.currentVersion}`;
    button.hidden = state.status === 'disabled';
    button.disabled = ['checking', 'downloading', 'installing'].includes(state.status);
    button.textContent = state.status === 'ready' ? 'Restart to update' : state.status === 'error' ? 'Retry update' : 'Check for updates';
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      if (state.status === 'ready') {
        await window.hud.installUpdate();
        paint(await window.hud.updateStatus());
      } else paint(await window.hud.checkUpdates());
    } catch { paint({...state, status: 'error'}); }
  });
  window.hud.on('updateStatus', paint);
  window.hud.updateStatus().then(paint).catch(() => paint({status: 'error'}));
})();
