const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('update controls work without a ready Python engine and expose download and restart states', async () => {
  let listener, click, installs = 0;
  let state = {status: 'idle', currentVersion: '2.1.0'};
  const label = {};
  const button = {addEventListener: (_, fn) => { click = fn; }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'renderer/updates.js'), 'utf8'), {
    document: {getElementById: id => id === 'updateStatus' ? label : button},
    window: {hud: {
      on: (_, fn) => { listener = fn; },
      updateStatus: async () => state,
      checkUpdates: async () => ({...state, status: 'checking'}),
      installUpdate: async () => { installs++; state = {...state, status: 'installing'}; },
    }},
  });
  await Promise.resolve();
  assert.equal(label.textContent, 'v2.1.0');
  await click();
  assert.equal(button.disabled, true);
  listener({...state, status: 'downloading', version: '2.2.0', percent: 25});
  assert.match(label.textContent, /25%/);
  listener({...state, status: 'ready', version: '2.2.0'});
  assert.equal(button.textContent, 'Restart to update');
  assert.equal(button.disabled, false);
  await click();
  assert.equal(installs, 1);
  assert.equal(button.disabled, true);
  listener({...state, status: 'error'});
  assert.equal(button.textContent, 'Retry update');
  assert.equal(button.disabled, false);
  listener({...state, status: 'disabled'});
  assert.equal(button.hidden, true);
});
