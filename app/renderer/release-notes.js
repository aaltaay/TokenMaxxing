'use strict';

// Release notes, newest first: what each release changed, in the words its PR
// wrote (scripts/release_notes.py). Shared by the notifications panel's ready
// row (what an update brings) and the What's new card (what it brought).
// Plain text only: nothing from GitHub is ever rendered as HTML, and its links
// open through the main process, which allows only this repo's release and PR
// pages.
(() => {
  const KIND_LABELS = {
    feat: 'New', fix: 'Fix', perf: 'Faster', refactor: 'Internal', chore: 'Maintenance',
    docs: 'Docs', build: 'Build', ci: 'Build', test: 'Tests', revert: 'Revert',
  };

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text != null) n.textContent = text;
    return n;
  }

  function link(label, url) {
    const button = node('button', 'rn-link', label);
    button.type = 'button';
    button.addEventListener('click', () => { window.hud.openReleaseLink(url).catch(() => {}); });
    return button;
  }

  function release(entry) {
    const item = node('li', 'rn-item');
    const head = node('div', 'rn-head');
    head.append(node('span', 'rn-tag', entry.tag));
    if (entry.kind) {
      const kind = node('span', 'rn-kind', KIND_LABELS[entry.kind] || entry.kind);
      kind.dataset.kind = entry.kind;
      head.append(kind);
    }
    const title = entry.recorded ? entry.title : 'No release notes were recorded for this release.';
    if (title) head.append(node('span', entry.recorded ? 'rn-title' : 'rn-title is-missing', title));
    item.append(head);
    if (entry.summary) item.append(node('p', 'rn-summary', entry.summary));
    const points = Array.isArray(entry.points) ? entry.points : [];
    if (points.length) {
      const list = node('ul', 'rn-points');
      for (const point of points) list.append(node('li', '', point));
      item.append(list);
    }
    if (entry.pr_url) item.append(link(`Pull request #${entry.pr}`, entry.pr_url));
    return item;
  }

  window.renderReleaseNotes = function renderReleaseNotes(notes) {
    const root = node('div', 'rn');
    if (!notes || typeof notes !== 'object' || notes.loading) {
      root.append(node('p', 'rn-status', 'Loading release notes…'));
      return root;
    }
    const releases = Array.isArray(notes.releases) ? notes.releases : [];
    if (notes.error) root.append(node('p', 'rn-status is-error', notes.error));
    else if (!releases.length) root.append(node('p', 'rn-status', 'No release notes were found for these versions.'));
    if (releases.length) {
      const list = node('ol', 'rn-list');
      for (const entry of releases) list.append(release(entry));
      root.append(list);
    }
    const more = Number(notes.more) || 0;
    if (more > 0 || notes.older_unlisted) {
      const lead = more > 0 ? `…and ${more} more ${more === 1 ? 'release' : 'releases'}` : 'Older releases';
      root.append(node('p', 'rn-status', `${lead} are on GitHub.`));
    }
    if (notes.page_url) root.append(link('All releases on GitHub', notes.page_url));
    return root;
  };
})();
