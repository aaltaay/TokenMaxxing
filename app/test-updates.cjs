const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {Updates} = require('./updates');

const ON = {updater: true, automatic: true, reason: ''};
const INSTALLER = crypto.randomBytes(2_000);
const SHA = crypto.createHash('sha512').update(INSTALLER).digest('base64');
const URL_HREF = 'https://github.com/aaltaay/TokenMaxxing/releases/download/v042/TokenMaxxing-Setup-v042.exe';

/**
 * A stand-in for electron-updater's NsisUpdater: it finds `latest` (or nothing),
 * plans the download the way the real one does, and on downloadUpdate() reports
 * downloaded only if our hand-off left the right file in its pending cache.
 */
function setup({latest = '3.0.42', gate = ON, stopEngine, fetch, notesSource} = {}) {
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-updates-'));
  const updater = new EventEmitter();
  const calls = {checks: 0, installs: [], restarts: 0, attention: 0};
  updater.checkForUpdates = async () => {
    calls.checks++;
    updater.emit('checking-for-update');
    if (!latest) {
      updater.emit('update-not-available', {});
      return {isUpdateAvailable: false};
    }
    const updateInfo = {version: latest};
    updater.updateInfoAndProvider = {provider: {resolveFiles: () => [{url: new URL(URL_HREF), info: {sha512: SHA, size: INSTALLER.length}}]}};
    updater.emit('update-available', updateInfo);
    return {isUpdateAvailable: true, updateInfo};
  };
  updater.getOrCreateDownloadHelper = async () => ({cacheDir});
  updater.downloadUpdate = async () => {
    const file = path.join(cacheDir, 'pending', 'TokenMaxxing-Setup-v042.exe');
    if (fs.existsSync(file) && fs.readFileSync(file).equals(INSTALLER)) updater.emit('update-downloaded', {version: latest});
    else updater.emit('error', new Error('sha512 checksum mismatch'));
  };
  updater.quitAndInstall = (silent, runAfter) => calls.installs.push([silent, runAfter]);
  const views = [];
  const progress = [];
  const updates = new Updates({
    updater, gate, version: '3.0.40', sleep: async () => {}, notesSource,
    fetch: fetch || (async (_url, init) => {
      const [start, end] = /bytes=(\d+)-(\d+)/.exec(init.headers.Range).slice(1).map(Number);
      const slice = INSTALLER.subarray(start, end + 1);
      return {status: 206, headers: {get: () => `bytes ${start}-${end}/${INSTALLER.length}`},
        arrayBuffer: async () => slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.length)};
    }),
    publish: view => views.push(view),
    setProgress: value => progress.push(value),
    attention: () => calls.attention++,
    stopEngine: stopEngine || (async () => true),
    restartEngine: async () => { calls.restarts++; },
  });
  return {updates, updater, calls, views, progress, cleanup: () => fs.rmSync(cacheDir, {recursive: true, force: true})};
}

test('a source checkout never contacts the update server or installs', async () => {
  const s = setup({gate: {updater: false, automatic: false, reason: 'running from source (not packaged)'}});
  await s.updates.check('manual');
  s.updates.start();
  assert.equal(await s.updates.install(), false);
  assert.equal(s.calls.checks, 0);
  assert.equal(s.updates.view.status, 'disabled');
  assert.equal(s.updates.view.installed, 'v040');
  assert.equal(s.updater.autoInstallOnAppQuit, undefined);
  s.cleanup();
});

test('a newer release downloads in the background, asks once, and installs only on the click', async () => {
  const s = setup();
  assert.equal(s.updater.autoDownload, false);
  assert.equal(s.updater.autoInstallOnAppQuit, false);
  assert.equal(s.updater.allowDowngrade, false);
  const view = await s.updates.check('launch');
  assert.equal(view.status, 'ready');
  assert.equal(view.version, 'v042');
  assert.ok(s.views.some(v => v.status === 'downloading'));
  assert.ok(s.progress.some(v => v > 0 && v <= 1));
  assert.equal(s.progress.at(-1), -1);
  assert.equal(s.calls.attention, 1);
  assert.deepEqual(s.calls.installs, []);
  // Ready stays ready: an automatic check does not run, a manual one does not re-download.
  await s.updates.check('recheck');
  assert.equal(s.calls.checks, 1);
  await s.updates.check('manual');
  assert.equal(s.updates.view.status, 'ready');
  assert.equal(s.calls.attention, 1);
  assert.equal(await s.updates.install(), true);
  assert.equal(await s.updates.install(), false);
  assert.deepEqual(s.calls.installs, [[false, true]]);
  s.cleanup();
});

test('Later quiets the ask for that release but keeps Restart to update', async () => {
  const s = setup();
  await s.updates.check('launch');
  const view = s.updates.later();
  assert.equal(view.dismissed, true);
  assert.equal(view.status, 'ready');
  s.cleanup();
});

test('nothing installs unless the engine is confirmed stopped, and the engine comes back', async () => {
  const s = setup({stopEngine: async () => false});
  await s.updates.check('launch');
  assert.equal(await s.updates.install(), false);
  assert.deepEqual(s.calls.installs, []);
  assert.equal(s.calls.restarts, 1);
  assert.equal(s.updates.view.status, 'ready');
  assert.match(s.updates.view.error, /did not stop/);
  s.cleanup();
});

test('an installer that fails to start brings the engine back and stays offered', async () => {
  const s = setup();
  await s.updates.check('launch');
  s.updater.quitAndInstall = () => { throw new Error('spawn EACCES'); };
  assert.equal(await s.updates.install(), false);
  assert.equal(s.calls.restarts, 1);
  assert.equal(s.updates.view.status, 'ready');
  s.cleanup();
});

test('up to date, and concurrent checks share one request', async () => {
  const s = setup({latest: null});
  const [a, b] = [s.updates.check('manual'), s.updates.check('manual')];
  assert.equal(a, b);
  assert.equal((await a).status, 'current');
  assert.equal(s.calls.checks, 1);
  s.cleanup();
});

test('a check that throws before electron-updater reports it still fails, and can be retried', async () => {
  const s = setup({latest: null});
  const original = s.updater.checkForUpdates;
  s.updater.checkForUpdates = () => { throw new Error('invalid configuration'); };
  assert.equal((await s.updates.check('launch')).status, 'failed');
  assert.equal(s.updates.pending, null);
  s.updater.checkForUpdates = original;
  assert.equal((await s.updates.check('manual')).status, 'current');
  s.cleanup();
});

test('a download that stops keeps its place and the next check resumes it', async () => {
  let fail = true;
  const s = setup({fetch: async (_url, init) => {
    if (fail) throw new Error('net::ERR_SSL_PROTOCOL_ERROR');
    const [start, end] = /bytes=(\d+)-(\d+)/.exec(init.headers.Range).slice(1).map(Number);
    const slice = INSTALLER.subarray(start, end + 1);
    return {status: 206, headers: {get: () => `bytes ${start}-${end}/${INSTALLER.length}`},
      arrayBuffer: async () => slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.length)};
  }});
  const stopped = await s.updates.check('launch');
  assert.deepEqual([stopped.status, stopped.failedStage], ['failed', 'download']);
  assert.ok(s.views.some(v => v.retry > 0));
  fail = false;
  assert.equal((await s.updates.check('manual')).status, 'ready');
  s.cleanup();
});

test('the ready release carries the notes of everything it brings, loaded once', async () => {
  const loads = [];
  let answer;
  const notesSource = {load: range => {
    loads.push(range);
    return new Promise(resolve => { answer = resolve; });
  }};
  const s = setup({notesSource});
  const view = await s.updates.check('launch');
  assert.equal(view.status, 'ready');
  assert.deepEqual(loads, [{after: 'v040', through: 'v042'}]);
  assert.equal(view.notes.loading, true);
  answer({loading: false, error: null, releases: [{tag: 'v042'}, {tag: 'v041'}], more: 0, older_unlisted: false});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(s.views.at(-1).notes.releases.map(r => r.tag), ['v042', 'v041']);
  // Re-checks and Later do not fetch the notes again.
  await s.updates.check('manual');
  s.updates.later();
  assert.equal(loads.length, 1);
  assert.equal(s.updates.view.notes.releases.length, 2);
  s.cleanup();
});

test('notes that fail to load say so and try again once the release has downloaded', async () => {
  let attempts = 0;
  const failing = setup({notesSource: {load: async () => {
    attempts++;
    if (attempts === 1) throw new Error('offline');
    return {loading: false, error: null, releases: [{tag: 'v042'}], more: 0, older_unlisted: false};
  }}});
  // The first load fails at once, before the download finishes; 'downloaded' loads again.
  const helper = failing.updater.getOrCreateDownloadHelper;
  let seenError = '';
  failing.updater.getOrCreateDownloadHelper = async () => {
    await new Promise(resolve => setImmediate(resolve));
    seenError = failing.updates.view.notes.error;
    return helper();
  };
  await failing.updates.check('launch');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(seenError, /could not be loaded: offline/);
  assert.equal(attempts, 2);
  assert.deepEqual(failing.updates.view.notes.releases, [{tag: 'v042'}]);
  failing.cleanup();
  const loads = [];
  const current = setup({latest: null, notesSource: {load: async range => { loads.push(range); }}});
  // Up to date: no release on offer, so no notes are asked for.
  assert.equal((await current.updates.check('launch')).notes, null);
  assert.deepEqual(loads, []);
  current.cleanup();
});
