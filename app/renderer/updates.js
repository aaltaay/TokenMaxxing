'use strict';

// The app's own version and updates, in the notifications panel. The main
// process owns every decision (updates.js); this only draws its view and sends
// the click back. A downloaded update lists what it brings under the ready row,
// and What's new reopens the card for this version (whats-new.js).
(() => {
  const label = document.getElementById('updateStatus');
  const button = document.getElementById('btnUpdate');
  const later = document.getElementById('btnUpdateLater');
  const whatsNew = document.getElementById('btnWhatsNew');
  const notesBox = document.getElementById('updateNotes');
  const notesTitle = document.getElementById('updateNotesTitle');
  const notesList = document.getElementById('updateNotesList');
  let state = {};
  let whatsNewAvailable = false;

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
    paintNotes();
    paintWhatsNew();
  }

  function paintNotes() {
    const notes = state.status === 'ready' && state.notes && typeof state.notes === 'object' ? state.notes : null;
    notesBox.hidden = !notes;
    if (!notes) {
      notesList.replaceChildren();
      return;
    }
    const count = !notes.loading && Array.isArray(notes.releases) ? notes.releases.length : 0;
    // The tags are listed right below; the heading is a kicker, set in capitals.
    notesTitle.textContent = count > 1 ? `What these ${count} releases bring` : 'What this update brings';
    notesList.replaceChildren(window.renderReleaseNotes(notes));
  }

  // The ready row already lists the notes, so What's new steps aside for Restart to update.
  function paintWhatsNew() {
    whatsNew.hidden = !whatsNewAvailable || ['ready', 'installing'].includes(state.status);
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
  whatsNew.addEventListener('click', () => { window.hud.openWhatsNew().catch(() => {}); });
  const takeWhatsNew = view => {
    whatsNewAvailable = Boolean(view && view.schema_version === 1 && view.available);
    paintWhatsNew();
  };
  window.hud.on('whatsNew', takeWhatsNew);
  window.hud.whatsNewStatus().then(takeWhatsNew).catch(() => {});
  window.hud.on('updateStatus', paint);
  window.hud.updateStatus().then(paint).catch(() => paint({status: 'failed', failedStage: 'check'}));
})();
