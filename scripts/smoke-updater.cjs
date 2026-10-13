// Run with app/node_modules/.bin/electron. The real electron-updater finds the
// packed installer on a loopback feed; update-download.js fetches it in Range
// chunks (one of which the server drops, so a retry is exercised) and hands it
// to electron-updater's cache, which hashes it again before reporting it ready.
// This is the hand-off that depends on electron-updater internals, run against
// the pinned version. It never installs or touches user settings.
const {app, net} = require('electron');
const {NsisUpdater} = require('../app/node_modules/electron-updater');
const {ElectronHttpExecutor} = require('../app/node_modules/electron-updater/out/electronHttpExecutor');
const {Updates} = require('../app/updates');
const {displayTag} = require('../app/update-policy');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
fs.mkdirSync(path.join(root, 'build'), {recursive: true});
const cache = fs.mkdtempSync(path.join(root, 'build', 'updater-smoke-'));
app.setPath('userData', cache);
const config = path.join(cache, 'app-update.yml');
fs.writeFileSync(config, 'updaterCacheDirName: smoke-cache\n');
const feedVersion = /^version:\s*(\S+)/m.exec(fs.readFileSync(path.join(root, 'dist', 'latest.yml'), 'utf8'))[1];

const seen = {ranges: 0, dropped: 0};
const server = http.createServer((req, res) => {
  const name = new URL(req.url, 'http://localhost').pathname.slice(1);
  if (!/^(latest\.yml|TokenMaxxing-Setup-v\d+\.exe(?:\.blockmap)?)$/.test(name)) { res.writeHead(404).end(); return; }
  const filename = path.join(root, 'dist', name);
  if (!fs.existsSync(filename)) { res.writeHead(404).end(); return; }
  const size = fs.statSync(filename).size;
  const range = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range || '');
  if (!range) {
    res.writeHead(200, {'Content-Length': size});
    fs.createReadStream(filename).pipe(res);
    return;
  }
  const start = Number(range[1]);
  const end = Math.min(Number(range[2]), size - 1);
  seen.ranges++;
  // Drop the second chunk once, mid-flight, the way a lossy link does.
  if (start > 0 && !seen.dropped) {
    seen.dropped++;
    res.writeHead(206, {'Content-Range': `bytes ${start}-${end}/${size}`, 'Content-Length': end - start + 1});
    res.write(Buffer.alloc(1024));
    req.socket.destroy();
    return;
  }
  res.writeHead(206, {'Content-Range': `bytes ${start}-${end}/${size}`, 'Content-Length': end - start + 1});
  fs.createReadStream(filename, {start, end}).pipe(res);
});
const timeout = setTimeout(() => { console.error('Update smoke test timed out'); app.exit(1); }, 300000);
app.whenReady().then(async () => {
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const adapter = {
      version: '2.1.1', name: 'tokenmaxxing-smoke', isPackaged: true,
      appUpdateConfigPath: config, userDataPath: cache, baseCachePath: cache,
      whenReady: () => Promise.resolve(), onQuit: () => {},
      quit: () => { throw new Error('Smoke test must not install'); },
    };
    const updater = new NsisUpdater(null, adapter);
    updater.httpExecutor = new ElectronHttpExecutor();
    updater.setFeedURL({provider: 'generic', url: `http://127.0.0.1:${server.address().port}`});
    const updates = new Updates({
      updater, gate: {updater: true, automatic: true, reason: ''}, version: adapter.version,
      fetch: (url, init) => net.fetch(url, init),
    });
    const view = await updates.check('manual');
    assert.equal(view.status, 'ready', `ended ${view.status}: ${view.error}`);
    assert.equal(view.version, displayTag(feedVersion));
    assert.equal(updater.autoInstallOnAppQuit, false);
    assert.equal(seen.dropped, 1, 'the dropped chunk was never requested');
    assert.ok(seen.ranges >= 2, 'the installer was not fetched in Range chunks');
    const pending = path.join(cache, 'smoke-cache', 'pending');
    assert.ok(fs.readdirSync(pending).some(name => name.endsWith('.exe')), 'no installer in the pending cache');
    console.log(`PASS: ${view.version} fetched in ${seen.ranges} Range requests (one dropped and retried), `
      + 'verified by electron-updater, and left for Restart to update.');
    clearTimeout(timeout);
    server.close();
    app.exit(0);
  } catch (error) { console.error(error); server.close(); app.exit(1); }
});
