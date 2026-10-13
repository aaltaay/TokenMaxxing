const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

/** Just enough DOM for the renderer scripts. Setting innerHTML throws: nothing is ever rendered as HTML. */
class FakeNode {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.hidden = false;
    this.className = '';
    this.text = '';
  }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  set innerHTML(_) { throw new Error('innerHTML is never used'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.text = ''; this.children = nodes; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { return Promise.all((this.listeners.click || []).map(fn => fn())); }
  focus() { FakeNode.focused += 1; }
  all(test) { return [...(test(this) ? [this] : []), ...this.children.flatMap(c => c.all(test))]; }
  byClass(name) { return this.all(n => n.className.split(' ').includes(name)); }
}

FakeNode.focused = 0;

/** The renderer scripts, in one context the way index.html loads them, with window.hud stubbed. */
function load(scripts, hud, ids) {
  const nodes = Object.fromEntries(ids.map(id => [id, new FakeNode()]));
  const listeners = {};
  const window = {hud: {...hud, on: (event, fn) => { (listeners[event] ||= []).push(fn); }}};
  const context = vm.createContext({window, document: {getElementById: id => nodes[id], createElement: tag => new FakeNode(tag)}});
  for (const file of scripts) vm.runInContext(fs.readFileSync(path.join(__dirname, 'renderer', file), 'utf8'), context);
  const push = (event, value) => { for (const fn of listeners[event] || []) fn(value); };
  return {nodes, push};
}

const settle = () => new Promise(resolve => setImmediate(resolve));
const UPDATE_IDS = ['updateStatus', 'btnUpdate', 'btnUpdateLater', 'btnWhatsNew', 'updateNotes', 'updateNotesTitle', 'updateNotesList'];
const CARD_IDS = ['whatsNew', 'whatsNewTitle', 'whatsNewSubtitle', 'whatsNewBody', 'whatsNewClose', 'whatsNewGotIt'];

const NOTES = {
  loading: false, error: null, more: 2, older_unlisted: false, page_url: 'https://github.com/aaltaay/TokenMaxxing/releases',
  releases: [
    {tag: 'v042', recorded: true, title: 'Show <b>what</b> each update brings', kind: 'feat', pr: 15,
      pr_url: 'https://github.com/aaltaay/TokenMaxxing/pull/15', summary: 'Release notes in the app.', points: ['What\'s new card', 'Notes on Restart to update']},
    {tag: 'v041', recorded: false, title: '', kind: null, pr: null, pr_url: null, summary: '', points: []},
  ],
};

test('the update row draws every phase and sends Restart, Later and Resume back', async () => {
  const sent = [];
  let state = {status: 'current', installed: 'v040', version: '', percent: 0};
  const {nodes, push} = load(['release-notes.js', 'updates.js'], {
    updateStatus: async () => state,
    checkUpdates: async () => { sent.push('check'); return {...state, status: 'checking'}; },
    installUpdate: async () => { sent.push('install'); state = {...state, status: 'installing'}; },
    laterUpdate: async () => { sent.push('later'); return {...state, dismissed: true}; },
    whatsNewStatus: async () => ({schema_version: 1, available: false, card: null}),
  }, UPDATE_IDS);
  await settle();
  const {updateStatus: label, btnUpdate: button, btnUpdateLater: later} = nodes;
  assert.equal(label.textContent, 'v040 · Up to date');
  assert.equal(later.hidden, true);
  await button.click();
  assert.deepEqual(sent, ['check']);
  assert.equal(button.disabled, true);

  push('updateStatus', {...state, status: 'downloading', version: 'v042', percent: 25, retry: 2});
  assert.equal(label.textContent, 'Downloading v042 · 25% · connection dropped, retrying (attempt 2)');
  push('updateStatus', {...state, status: 'failed', failedStage: 'download', version: 'v042', percent: 37});
  assert.equal(button.textContent, 'Resume');
  assert.match(label.textContent, /stopped at 37%/);

  state = {...state, status: 'ready', version: 'v042'};
  push('updateStatus', state);
  assert.equal(label.textContent, 'v042 is ready to install');
  assert.equal(button.textContent, 'Restart to update');
  assert.equal(button.disabled, false);
  assert.equal(later.hidden, false);
  await later.click();
  assert.equal(later.hidden, true);
  await button.click();
  assert.deepEqual(sent, ['check', 'later', 'install']);
  assert.equal(label.textContent, 'Installing v042…');

  push('updateStatus', {status: 'disabled', installed: 'v0.0.0-dev'});
  assert.equal(button.hidden, true);
  assert.match(label.textContent, /Windows installer/);
});

test('the ready row lists what the update brings, as plain text, with only release links', async () => {
  const opened = [];
  let whatsNewOpened = 0;
  const {nodes, push} = load(['release-notes.js', 'updates.js'], {
    updateStatus: async () => ({status: 'downloading', installed: 'v040', version: 'v042', percent: 50, notes: NOTES}),
    whatsNewStatus: async () => ({schema_version: 1, available: true, card: null}),
    openWhatsNew: async () => { whatsNewOpened++; },
    openReleaseLink: async url => { opened.push(url); return true; },
  }, UPDATE_IDS);
  await settle();
  // Downloading: no notes yet, and What's new reopens this version's card.
  assert.equal(nodes.updateNotes.hidden, true);
  assert.equal(nodes.btnWhatsNew.hidden, false);
  await nodes.btnWhatsNew.click();
  assert.equal(whatsNewOpened, 1);

  push('updateStatus', {status: 'ready', installed: 'v040', version: 'v042', notes: {...NOTES, loading: true}});
  assert.equal(nodes.updateNotes.hidden, false);
  assert.equal(nodes.btnWhatsNew.hidden, true);
  assert.equal(nodes.updateNotesList.textContent, 'Loading release notes…');

  push('updateStatus', {status: 'ready', installed: 'v040', version: 'v042', notes: NOTES});
  assert.equal(nodes.updateNotesTitle.textContent, 'What these 2 releases bring');
  const list = nodes.updateNotesList;
  const items = list.byClass('rn-item');
  assert.equal(items.length, 2);
  assert.equal(items[0].byClass('rn-title')[0].textContent, 'Show <b>what</b> each update brings');
  assert.equal(items[0].byClass('rn-kind')[0].textContent, 'New');
  assert.deepEqual(items[0].byClass('rn-points')[0].children.map(li => li.textContent), ['What\'s new card', 'Notes on Restart to update']);
  assert.equal(items[1].byClass('rn-title')[0].textContent, 'No release notes were recorded for this release.');
  assert.match(list.textContent, /…and 2 more releases are on GitHub\./);
  const links = list.byClass('rn-link');
  assert.deepEqual(links.map(l => l.textContent), ['Pull request #15', 'All releases on GitHub']);
  for (const l of links) await l.click();
  assert.deepEqual(opened, ['https://github.com/aaltaay/TokenMaxxing/pull/15', 'https://github.com/aaltaay/TokenMaxxing/releases']);

  push('updateStatus', {status: 'ready', installed: 'v040', version: 'v042', notes: {...NOTES, releases: [], error: 'Release notes could not be loaded: offline'}});
  assert.match(nodes.updateNotesList.textContent, /could not be loaded: offline/);
  push('updateStatus', {status: 'current', installed: 'v042', version: '', notes: null});
  assert.equal(nodes.updateNotes.hidden, true);
  assert.equal(nodes.btnWhatsNew.hidden, false);
  // A What's new view of another version offers nothing to reopen.
  push('whatsNew', {schema_version: 2, available: true});
  assert.equal(nodes.btnWhatsNew.hidden, true);
});

test('the What\'s new card shows what the update brought, never takes focus, and Got it closes it', async () => {
  let view = {schema_version: 1, available: true, card: {mode: 'updated', tag: 'v042', since: 'v040', notes: {...NOTES, loading: true}}};
  let closes = 0;
  const {nodes, push} = load(['release-notes.js', 'whats-new.js'], {
    whatsNewStatus: async () => view,
    closeWhatsNew: async () => { closes++; view = {...view, card: null}; return view; },
    openReleaseLink: async () => true,
  }, CARD_IDS);
  await settle();
  const card = nodes.whatsNew;
  assert.equal(card.hidden, false);
  assert.equal(nodes.whatsNewTitle.textContent, 'What\'s new in v042');
  assert.equal(nodes.whatsNewBody.textContent, 'Loading release notes…');
  push('whatsNew', {...view, card: {...view.card, notes: NOTES}});
  assert.equal(nodes.whatsNewSubtitle.textContent, 'Updated from v040 · 2 releases.');
  assert.deepEqual(nodes.whatsNewBody.byClass('rn-tag').map(t => t.textContent), ['v042', 'v041']);
  assert.equal(FakeNode.focused, 0);

  await nodes.whatsNewGotIt.click();
  assert.equal(closes, 1);
  assert.equal(card.hidden, true);

  push('whatsNew', {schema_version: 1, available: true, card: {mode: 'recent', tag: 'v042', since: null, notes: NOTES}});
  assert.equal(card.hidden, false);
  assert.equal(nodes.whatsNewSubtitle.textContent, 'The latest releases up to this version.');
  await nodes.whatsNewClose.click();
  assert.equal(closes, 2);
  push('whatsNew', {schema_version: 1, available: true, card: {mode: 'updated', tag: 'v042', since: null, notes: NOTES}});
  assert.equal(nodes.whatsNewSubtitle.textContent, 'What this version brings.');
  // A view this version does not read shows no card.
  push('whatsNew', {schema_version: 2, card: {mode: 'updated', tag: 'v042', notes: NOTES}});
  assert.equal(card.hidden, true);
});

test('the card and the panel ship in index.html, loaded after the shared notes list', () => {
  const html = fs.readFileSync(path.join(__dirname, 'renderer', 'index.html'), 'utf8');
  for (const id of [...UPDATE_IDS, ...CARD_IDS]) assert.match(html, new RegExp(`id="${id}"`), id);
  const order = ['app.js', 'release-notes.js', 'updates.js', 'whats-new.js'].map(f => html.indexOf(`<script src="${f}">`));
  assert.ok(order.every((at, i) => at > 0 && (i === 0 || at > order[i - 1])), String(order));
  // Floating, not modal: a section, never a <dialog>, and nothing autofocuses.
  const card = /<section class="whats-new" id="whatsNew"[^>]*hidden>[\s\S]*?<\/section>/.exec(html);
  assert.ok(card, 'the What\'s new card is a hidden <section>');
  assert.doesNotMatch(card[0], /autofocus|<dialog/);
});
