const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const p = require('./update-policy');
const {createUpdateLogger, UPDATE_LOG_FILE} = require('./update-log');

const ON = {updater: true, automatic: true, reason: ''};
const from = (state, ...events) => events.reduce(p.reduceUpdateState, state);
const run = (...events) => from(p.INITIAL_UPDATE_STATE, ...events);

test('only a packaged Windows install updates, and the env switch stops automatic checks only', () => {
  assert.deepEqual(p.updateGate({isPackaged: false, platform: 'win32'}).updater, false);
  assert.deepEqual(p.updateGate({isPackaged: true, platform: 'darwin'}).updater, false);
  assert.deepEqual(p.updateGate({isPackaged: true, platform: 'win32'}), ON);
  const off = p.updateGate({isPackaged: true, platform: 'win32', setting: ' Off '});
  assert.equal(off.updater, true);
  assert.equal(off.automatic, false);
  assert.match(off.reason, /TOKENMAXXING_UPDATE_CHECK/);
});

test('packed releases read as their vNNN tag', () => {
  assert.equal(p.displayTag('3.0.42'), 'v042');
  assert.equal(p.displayTag('3.0.1234'), 'v1234');
  assert.equal(p.displayTag('2.1.1'), 'v2.1.1');
  assert.equal(p.displayTag('0.0.0-dev'), 'v0.0.0-dev');
  assert.equal(p.displayTag(''), '');
});

test('a found release downloads, and a downloaded one survives every re-check', () => {
  let s = run({type: 'checking'}, {type: 'available', version: '3.0.42'}, {type: 'progress', percent: 37.9});
  assert.equal(s.phase, 'downloading');
  assert.equal(p.taskbarProgress(s), 0.379);
  s = p.reduceUpdateState(s, {type: 'downloaded', version: '3.0.42'});
  assert.equal(s.phase, 'ready');
  assert.equal(p.taskbarProgress(s), -1);
  for (const event of [{type: 'checking'}, {type: 'not-available'}, {type: 'available', version: '3.0.42'}]) {
    assert.equal(p.reduceUpdateState(s, event), s);
  }
  const failed = p.reduceUpdateState(s, {type: 'error', message: 'offline'});
  assert.equal(failed.phase, 'ready');
  assert.equal(failed.error, 'offline');
  // A newer release found by Check for updates replaces it.
  assert.equal(p.reduceUpdateState(s, {type: 'available', version: '3.0.43'}).phase, 'downloading');
});

test('a stopped download keeps its percent and resumes from it', () => {
  let s = run({type: 'available', version: '3.0.42'}, {type: 'progress', percent: 37}, {type: 'retrying', attempt: 2});
  assert.equal(s.retry, 2);
  s = p.reduceUpdateState(s, {type: 'error', message: new Error('net::ERR_SSL_PROTOCOL_ERROR\nstack')});
  assert.deepEqual([s.phase, s.failedStage, s.percent, s.retry, s.error], ['failed', 'download', 37, 0, 'net::ERR_SSL_PROTOCOL_ERROR']);
  s = from(s, {type: 'checking'}, {type: 'available', version: '3.0.42'});
  assert.deepEqual([s.phase, s.percent], ['downloading', 37]);
  const check = run({type: 'checking'}, {type: 'error', message: 'DNS'});
  assert.deepEqual([check.phase, check.failedStage, check.percent], ['failed', 'check', 0]);
});

test('install only from ready, and a failed install offers the same download again', () => {
  assert.equal(run({type: 'installing'}).phase, 'idle');
  let s = run({type: 'available', version: '3.0.42'}, {type: 'downloaded'}, {type: 'installing'});
  assert.equal(s.phase, 'installing');
  s = p.reduceUpdateState(s, {type: 'install-failed', message: 'engine did not stop'});
  assert.deepEqual([s.phase, s.version, s.error], ['ready', '3.0.42', 'engine did not stop']);
});

test('automatic checks wait for nothing in hand or in flight, and re-check every two hours', () => {
  const idle = p.INITIAL_UPDATE_STATE;
  assert.equal(p.shouldAutoCheck(idle, ON), true);
  assert.equal(p.shouldAutoCheck(idle, {...ON, automatic: false}), false);
  for (const phase of ['checking', 'downloading', 'installing', 'ready']) assert.equal(p.shouldAutoCheck({...idle, phase}, ON), false);
  const now = 10 * p.UPDATE_RECHECK_INTERVAL_MS;
  assert.equal(p.shouldRecheck(idle, ON, {now, lastCheckAt: now - p.UPDATE_RECHECK_INTERVAL_MS + 1}), false);
  assert.equal(p.shouldRecheck(idle, ON, {now, lastCheckAt: now - p.UPDATE_RECHECK_INTERVAL_MS}), true);
  assert.equal(p.manualCheckAction({...idle, phase: 'ready'}, {...ON, automatic: false}), 'check');
  assert.equal(p.manualCheckAction({...idle, phase: 'downloading'}, ON), 'busy');
  assert.equal(p.manualCheckAction(idle, {updater: false}), 'unavailable');
});

test('a ready release asks once per version per session, never after Later', () => {
  let s = run({type: 'available', version: '3.0.42'}, {type: 'downloaded'});
  assert.equal(p.needsPrompt(s), true);
  assert.equal(p.needsPrompt(p.reduceUpdateState(s, {type: 'prompted', version: '3.0.42'})), false);
  s = p.reduceUpdateState(s, {type: 'dismissed', version: '3.0.42'});
  assert.equal(p.needsPrompt(s), false);
  assert.equal(p.updateView(s, {gate: ON, installedVersion: '3.0.40'}).dismissed, true);
  assert.equal(p.needsPrompt(from(s, {type: 'available', version: '3.0.43'}, {type: 'downloaded'})), true);
});

test('the view names versions by tag and says when this copy cannot update', () => {
  const s = run({type: 'available', version: '3.0.42'}, {type: 'progress', percent: 41.7});
  assert.deepEqual(p.updateView(s, {gate: ON, installedVersion: '3.0.40'}), {
    status: 'downloading', installed: 'v040', version: 'v042', percent: 41, retry: 0, error: '',
    failedStage: '', dismissed: false, automatic: true, reason: '',
  });
  assert.equal(p.updateView(s, {gate: {updater: false}, installedVersion: '0.0.0-dev'}).status, 'disabled');
});

test('the update log rolls over and never throws', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-update-log-'));
  const quiet = {log: console.log, warn: console.warn, error: console.error};
  console.log = console.warn = console.error = () => {};
  try {
    const logger = createUpdateLogger({prefix: '[t]', dir: () => dir, now: () => new Date('2026-10-13T12:00:00Z')});
    logger.info('checking');
    logger.error('broken');
    const file = path.join(dir, UPDATE_LOG_FILE);
    assert.equal(fs.readFileSync(file, 'utf8'), '2026-10-13T12:00:00.000Z INFO checking\n2026-10-13T12:00:00.000Z ERROR broken\n');
    fs.writeFileSync(file, 'x'.repeat(1_000_001));
    logger.warn('after');
    assert.equal(fs.statSync(`${file}.1`).size, 1_000_001);
    assert.match(fs.readFileSync(file, 'utf8'), /WARN after/);
    const broken = createUpdateLogger({prefix: '[t]', dir: () => path.join(file, 'not-a-dir')});
    assert.doesNotThrow(() => broken.info('lost'));
  } finally {
    Object.assign(console, quiet);
    fs.rmSync(dir, {recursive: true, force: true});
  }
});
