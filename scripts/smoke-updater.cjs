// Run with app/node_modules/.bin/electron. Downloads a real installer from a
// loopback feed into an isolated cache; never installs or changes user settings.
const {app} = require('electron');
const {NsisUpdater} = require('../app/node_modules/electron-updater');
const {ElectronHttpExecutor} = require('../app/node_modules/electron-updater/out/electronHttpExecutor');
const {Updates} = require('../app/updates');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const cache = fs.mkdtempSync(path.join(root, 'build', 'updater-smoke-'));
app.setPath('userData', cache);
const config = path.join(cache, 'app-update.yml');
fs.writeFileSync(config, 'updaterCacheDirName: smoke-cache\n');
const server = http.createServer((req, res) => {
  const name = new URL(req.url, 'http://localhost').pathname.slice(1);
  if (!/^(latest\.yml|TokenMaxxing-Setup-[\d.]+-x64\.exe(?:\.blockmap)?)$/.test(name)) {
    res.writeHead(404).end(); return;
  }
  const filename = path.join(root, 'dist', name);
  if (!fs.existsSync(filename)) { res.writeHead(404).end(); return; }
  res.writeHead(200, {'Content-Length': fs.statSync(filename).size});
  fs.createReadStream(filename).pipe(res);
});
const timeout = setTimeout(() => { console.error('Update smoke test timed out'); app.exit(1); }, 120000);
app.whenReady().then(async () => {
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const adapter = {
      version: '2.0.0', name: 'tokenmaxxing-smoke', isPackaged: true,
      appUpdateConfigPath: config, userDataPath: cache, baseCachePath: cache,
      whenReady: () => Promise.resolve(), onQuit: () => {},
      quit: () => { throw new Error('Smoke test must not install'); },
    };
    const updater = new NsisUpdater(null, adapter);
    updater.httpExecutor = new ElectronHttpExecutor();
    updater.disableDifferentialDownload = true;
    updater.disableWebInstaller = true;
    updater.setFeedURL({provider: 'generic', url: `http://127.0.0.1:${server.address().port}`});
    const updates = new Updates({updater, enabled: true, version: adapter.version});
    await updates.check();
    assert.equal(updates.state.status, 'ready');
    assert.equal(updates.state.version, require('../app/package.json').version);
    console.log('PASS: real updater discovered, downloaded and verified the installer; restart remains manual.');
    clearTimeout(timeout);
    server.close();
    app.exit(0);
  } catch (error) { console.error(error); server.close(); app.exit(1); }
});
