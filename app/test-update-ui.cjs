const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('the update row draws every phase and sends Restart, Later and Resume back', async () => {
  const clicks = {};
  const sent = [];
  let listener;
  let state = {status: 'current', installed: 'v040', version: '', percent: 0};
  const node = id => ({addEventListener: (_, fn) => { clicks[id] = fn; }});
  const nodes = {updateStatus: {}, btnUpdate: node('btnUpdate'), btnUpdateLater: node('btnUpdateLater')};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'renderer/updates.js'), 'utf8'), {
    document: {getElementById: id => nodes[id]},
    window: {hud: {
      on: (_, fn) => { listener = fn; },
      updateStatus: async () => state,
      checkUpdates: async () => { sent.push('check'); return {...state, status: 'checking'}; },
      installUpdate: async () => { sent.push('install'); state = {...state, status: 'installing'}; },
      laterUpdate: async () => { sent.push('later'); return {...state, dismissed: true}; },
    }},
  });
  await new Promise(resolve => setImmediate(resolve));
  const label = nodes.updateStatus;
  const button = nodes.btnUpdate;
  const later = nodes.btnUpdateLater;
  assert.equal(label.textContent, 'v040 · Up to date');
  assert.equal(later.hidden, true);
  await clicks.btnUpdate();
  assert.deepEqual(sent, ['check']);
  assert.equal(button.disabled, true);

  listener({...state, status: 'downloading', version: 'v042', percent: 25, retry: 2});
  assert.equal(label.textContent, 'Downloading v042 · 25% · connection dropped, retrying (attempt 2)');
  listener({...state, status: 'failed', failedStage: 'download', version: 'v042', percent: 37});
  assert.equal(button.textContent, 'Resume');
  assert.match(label.textContent, /stopped at 37%/);

  state = {...state, status: 'ready', version: 'v042'};
  listener(state);
  assert.equal(label.textContent, 'v042 is ready to install');
  assert.equal(button.textContent, 'Restart to update');
  assert.equal(button.disabled, false);
  assert.equal(later.hidden, false);
  await clicks.btnUpdateLater();
  assert.equal(later.hidden, true);
  await clicks.btnUpdate();
  assert.deepEqual(sent, ['check', 'later', 'install']);
  assert.equal(label.textContent, 'Installing v042…');

  listener({status: 'disabled', installed: 'v0.0.0-dev'});
  assert.equal(button.hidden, true);
  assert.match(label.textContent, /Windows installer/);
});
