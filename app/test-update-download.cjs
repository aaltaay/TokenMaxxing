// The resumable installer download, ported with Nova's tests: a link that drops
// a TLS record every few dozen MB must still deliver the installer, keep what
// arrived across failures, never hand over a file that fails the release
// checksum, and leave it exactly where electron-updater's cache check accepts it.
const {test, beforeEach, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const d = require('./update-download');

const CHUNK = 1000;
const FILE_NAME = 'TokenMaxxing-Setup-v042.exe';
const URL_HREF = `https://github.com/aaltaay/TokenMaxxing/releases/download/v042/${FILE_NAME}`;
const logger = {info() {}, warn() {}};
const sha512 = buf => crypto.createHash('sha512').update(buf).digest('base64');
const makeTarget = (body, overrides = {}) => ({
  url: URL_HREF, fileName: FILE_NAME, sha512: sha512(body), size: body.length,
  isAdminRightsRequired: false, version: '3.0.42', ...overrides,
});

/** A Range-honouring server; `failAt` maps a range start to how many times it errors first. */
function fakeServer(body, failAt = {}) {
  const asked = [];
  const fetch = async (_url, init) => {
    const range = init.headers.Range;
    asked.push(range);
    const [start, end] = /bytes=(\d+)-(\d+)/.exec(range).slice(1).map(Number);
    if ((failAt[start] ?? 0) > 0) {
      failAt[start] -= 1;
      throw new Error('net::ERR_SSL_PROTOCOL_ERROR');
    }
    const slice = body.subarray(start, end + 1);
    return {
      status: 206,
      headers: {get: k => (k.toLowerCase() === 'content-range' ? `bytes ${start}-${end}/${body.length}` : null)},
      arrayBuffer: async () => slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.length),
    };
  };
  return {fetch, asked};
}

let cacheDir = '';
beforeEach(() => { cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-update-dl-')); });
afterEach(() => fs.rmSync(cacheDir, {recursive: true, force: true}));
const run = (target, fetch, extra = {}) =>
  d.downloadInstaller({target, cacheDir, fetch, logger, sleep: async () => {}, chunkBytes: CHUNK, ...extra});
const body = crypto.randomBytes(4_500);

test('pure helpers plan chunks, read Content-Range and bound the retries', () => {
  assert.deepEqual(d.nextRange(0, 2500, 1000), {start: 0, end: 999});
  assert.deepEqual(d.nextRange(2000, 2500, 1000), {start: 2000, end: 2499});
  assert.equal(d.nextRange(2500, 2500, 1000), null);
  assert.deepEqual(d.parseContentRange('bytes 0-99/1000'), {start: 0, end: 99, total: 1000});
  assert.equal(d.parseContentRange('bytes */1000'), null);
  assert.equal(d.retryDelay(1), d.UPDATE_CHUNK_RETRY_DELAYS_MS[0]);
  assert.equal(d.retryDelay(d.UPDATE_CHUNK_RETRY_DELAYS_MS.length + 1), null);
});

test('a sharing lock is waited out, any other error is not', async () => {
  const busy = Object.assign(new Error('EBUSY'), {code: 'EBUSY'});
  let calls = 0;
  const sleep = async () => {};
  assert.equal(await d.retryWhileBusy(async () => { if (++calls < 3) throw busy; return 'done'; }, sleep), 'done');
  const missing = Object.assign(new Error('ENOENT'), {code: 'ENOENT'});
  await assert.rejects(d.retryWhileBusy(async () => { throw missing; }, sleep), missing);
  await assert.rejects(d.retryWhileBusy(async () => { throw busy; }, sleep, 4), busy);
});

test('the target is the .exe electron-updater would fetch, and only a checkable one', () => {
  const files = [
    {url: new URL('https://x/y/app.blockmap'), info: {sha512: 'b', size: 1}},
    {url: new URL(URL_HREF), info: {sha512: 'abc', size: 86_765_137}},
  ];
  assert.deepEqual(d.installerTarget(files, '3.0.42'), {
    url: URL_HREF, fileName: FILE_NAME, sha512: 'abc', size: 86_765_137, isAdminRightsRequired: false, version: '3.0.42',
  });
  assert.equal(d.installerTarget([{url: new URL(URL_HREF), info: {size: 10}}], '1'), null);
  assert.equal(d.installerTarget([{url: new URL(URL_HREF), info: {sha512: 'a'}}], '1'), null);
  assert.equal(d.installerTarget(undefined, '1'), null);
});

test('rides out dropped chunks and leaves a verified installer in the pending cache', async () => {
  const {fetch} = fakeServer(body, {1000: 2, 3000: 1});
  const retries = [];
  let last = 0;
  const file = await run(makeTarget(body), fetch, {onRetry: r => retries.push(r.attempt), onProgress: v => { last = v; }});
  assert.equal(file, path.join(cacheDir, 'pending', FILE_NAME));
  assert.ok(fs.readFileSync(file).equals(body));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(cacheDir, 'pending', 'update-info.json'), 'utf8')),
    {fileName: FILE_NAME, sha512: sha512(body), isAdminRightsRequired: false});
  assert.deepEqual(retries, [1, 2, 1]);
  assert.equal(last, 100);
  assert.deepEqual(fs.readdirSync(path.join(cacheDir, d.PARTIAL_DIR_NAME)), []);
});

test('keeps what arrived when a chunk exhausts its retries, and resumes from there', async () => {
  const first = fakeServer(body, {2000: d.UPDATE_CHUNK_RETRY_DELAYS_MS.length + 1});
  await assert.rejects(run(makeTarget(body), first.fetch), /gave up on bytes 2000-2999; 2000 kept/);
  const second = fakeServer(body);
  const file = await run(makeTarget(body), second.fetch);
  assert.deepEqual(second.asked, ['bytes=2000-2999', 'bytes=3000-3999', 'bytes=4000-4499']);
  assert.ok(fs.readFileSync(file).equals(body));
});

test('does not fetch again an installer already handed off (Later, then a restart)', async () => {
  const file = await run(makeTarget(body), fakeServer(body).fetch);
  const second = fakeServer(body);
  assert.equal(await run(makeTarget(body), second.fetch), file);
  assert.deepEqual(second.asked, []);
});

test('starts over when a newer release replaces the one partly downloaded', async () => {
  await assert.rejects(run(makeTarget(body), fakeServer(body, {2000: 99}).fetch));
  const newer = crypto.randomBytes(3_000);
  const second = fakeServer(newer);
  const file = await run(makeTarget(newer, {version: '3.0.43'}), second.fetch);
  assert.equal(second.asked[0], 'bytes=0-999');
  assert.ok(fs.readFileSync(file).equals(newer));
});

test('never hands over bytes that fail the release checksum', async () => {
  const tampered = Buffer.from(body);
  tampered[10] ^= 0xff;
  await assert.rejects(run(makeTarget(body), fakeServer(tampered).fetch), /does not match the release checksum/);
  assert.equal(fs.existsSync(path.join(cacheDir, 'pending')), false);
  assert.deepEqual(fs.readdirSync(path.join(cacheDir, d.PARTIAL_DIR_NAME)), []);
});

test('a server whose file changed size is fatal, a 5xx is retried', async () => {
  let calls = 0;
  const changed = async () => ({status: 206, headers: {get: () => 'bytes 0-999/9999'}, arrayBuffer: async () => new ArrayBuffer(1000)});
  await assert.rejects(run(makeTarget(body), changed), /server file changed/);
  const flaky = fakeServer(body);
  const fetch = async (url, init) => (++calls === 1 ? {status: 503, headers: {get: () => null}} : flaky.fetch(url, init));
  assert.ok(fs.readFileSync(await run(makeTarget(body), fetch)).equals(body));
});

test('downloadRelease plans from electron-updater and lets it verify the hand-off', async () => {
  const calls = [];
  const updater = {
    updateInfoAndProvider: {provider: {resolveFiles: () => [{url: new URL(URL_HREF), info: {sha512: sha512(body), size: body.length}}]}},
    getOrCreateDownloadHelper: async () => ({cacheDir}),
    downloadUpdate: async () => { calls.push('downloadUpdate'); },
  };
  await d.downloadRelease({updater, info: {version: '3.0.42'}, fetch: fakeServer(body).fetch, logger, sleep: async () => {}});
  assert.ok(fs.readFileSync(path.join(cacheDir, 'pending', FILE_NAME)).equals(body));
  assert.deepEqual(calls, ['downloadUpdate']);
  // Nothing to plan from: electron-updater downloads it itself.
  await d.downloadRelease({updater: {downloadUpdate: async () => { calls.push('fallback'); }}, info: {}, fetch: null, logger});
  assert.deepEqual(calls, ['downloadUpdate', 'fallback']);
});
