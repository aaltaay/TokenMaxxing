'use strict';

// What's new: the first launch of a new version opens this card with the notes
// of every release the update brought, and What's new in the notifications
// panel opens it again. It floats over the app, never a modal, and never takes
// focus, so the app stays usable while it is open. Got it (or ×) records the
// version as read; the main process (whats-new.js) owns that and every other
// decision, and this only draws its view.
(() => {
  const VIEW_SCHEMA_VERSION = 1;
  const card = document.getElementById('whatsNew');
  const title = document.getElementById('whatsNewTitle');
  const subtitle = document.getElementById('whatsNewSubtitle');
  const body = document.getElementById('whatsNewBody');

  function subtitleText({mode, since, notes}) {
    if (mode === 'recent') return 'The latest releases up to this version.';
    if (!since) return 'What this version brings.';
    const count = notes && !notes.loading && Array.isArray(notes.releases) ? notes.releases.length : 0;
    return count > 1 ? `Updated from ${since} · ${count} releases.` : `Updated from ${since}.`;
  }

  function paint(view) {
    // A view this version does not read shows no card rather than a guess.
    const current = view && view.schema_version === VIEW_SCHEMA_VERSION && view.card && typeof view.card === 'object'
      ? view.card : null;
    card.hidden = !current;
    if (!current) {
      body.replaceChildren();
      return;
    }
    title.textContent = `What's new in ${current.tag}`;
    subtitle.textContent = subtitleText(current);
    body.replaceChildren(window.renderReleaseNotes(current.notes));
  }

  async function close() {
    card.hidden = true;
    try { paint(await window.hud.closeWhatsNew()); } catch {}
  }

  document.getElementById('whatsNewClose').addEventListener('click', close);
  document.getElementById('whatsNewGotIt').addEventListener('click', close);
  window.hud.on('whatsNew', paint);
  window.hud.whatsNewStatus().then(paint).catch(() => {});
})();
