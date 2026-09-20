const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {Updates} = require('./updates');

function setup(enabled = true) {
  const updater = new EventEmitter();
  let checks = 0, installs = 0;
  updater.checkForUpdates = async () => { checks++; updater.emit('update-not-available'); };
  updater.quitAndInstall = () => { installs++; };
  const updates = new Updates({updater, enabled, version: '2.1.0'});
  return {updates, updater, checks: () => checks, installs: () => installs};
}

test('development builds never contact the update server or install', async () => {
  const s = setup(false);
  await s.updates.check();
  s.updates.start();
  assert.equal(s.updates.install(), false);
  assert.equal(s.checks(), 0);
  assert.equal(s.updates.state.status, 'disabled');
});

test('only a downloaded update can install, once, with explicit restart', async () => {
  const s = setup();
  assert.equal(s.updater.autoInstallOnAppQuit, false);
  assert.equal(s.updater.allowDowngrade, false);
  assert.equal(s.updates.install(), false);
  s.updater.emit('update-available', {version: '2.2.0'});
  s.updater.emit('download-progress', {percent: 45.6});
  assert.equal(s.updates.state.percent, 46);
  assert.equal(s.updates.install(), false);
  s.updater.emit('update-downloaded', {version: '2.2.0'});
  await s.updates.check();
  assert.equal(s.checks(), 0);
  assert.equal(s.updates.install(), true);
  assert.equal(s.updates.install(), false);
  assert.equal(s.installs(), 1);
});

test('concurrent checks are coalesced and failed checks can retry', async () => {
  const s = setup();
  let reject;
  s.updater.checkForUpdates = () => new Promise((_, fail) => { reject = fail; });
  const first = s.updates.check();
  const pending = s.updates.pending;
  const second = s.updates.check();
  assert.equal(s.updates.pending, pending);
  await Promise.resolve();
  reject(new Error('offline'));
  await Promise.all([first, second]);
  assert.equal(s.updates.state.status, 'error');
  s.updater.checkForUpdates = async () => s.updater.emit('update-not-available');
  await s.updates.check();
  assert.equal(s.updates.state.status, 'current');
});

test('a synchronous updater failure does not leave a stuck check', async () => {
  const s = setup();
  s.updater.checkForUpdates = () => { throw new Error('invalid configuration'); };
  await s.updates.check();
  assert.equal(s.updates.pending, null);
  assert.equal(s.updates.state.status, 'error');
});

test('a separate download rejection is handled and retryable', async () => {
  const s = setup();
  s.updater.checkForUpdates = async () => {
    s.updater.emit('update-available', {version: '2.2.0'});
    return {downloadPromise: Promise.reject(new Error('checksum mismatch'))};
  };
  await s.updates.check();
  assert.equal(s.updates.state.status, 'error');
  assert.equal(s.updates.install(), false);
});
