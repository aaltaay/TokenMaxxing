'use strict';

const { app, BrowserWindow, ipcMain, shell, nativeTheme, Notification, safeStorage } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const { ProviderLinkManager } = require('./provider-link');
const { ResetAttention } = require('./reset-attention');
const { SmsAlerts } = require('./sms-alerts');
const { Updates } = require('./updates');
const { ActiveChat } = require('./active-chat');
const activeChat = new ActiveChat({script: app.isPackaged
  ? path.join(process.resourcesPath, 'active-chat.ps1') : path.join(__dirname, 'active-chat.ps1')});
let updates = null;
let sms = null;
let smsQueue = Promise.resolve();
function sendSms(body) {
  const request = smsQueue.then(() => sms.send(body));
  smsQueue = request.catch(() => {});
  return request;
}

const REPO_ROOT = path.join(__dirname, '..');
const BRIDGE = path.join(REPO_ROOT, 'hud_bridge.py');
const WIN = { width: 760, height: 960, minWidth: 560, minHeight: 640 };

let win = null;
let bridge = null;
const attention = new ResetAttention({
  getWindow: () => win,
  publish: state => send('reset:alert', state),
  buzz: () => bridge?.call('buzz', {}, 10000).catch(() => {}),
  notify: body => {
    if (Notification.isSupported()) {
      const notification = new Notification({title: 'TOKENMAXXING — Reset alert', body, silent: true});
      notification.on('click', () => { if (attention.active) attention.show(attention.active.events); });
      notification.show();
    }
  },
});
let alertPoll = null;
let alertBusy = false;
let lastQuotaCheck = 0;
async function pollResetAlerts() {
  if (alertBusy || !bridge?.ready) return;
  alertBusy = true;
  try {
    if (Date.now() - lastQuotaCheck >= 60000) {
      await bridge.call('providers', {}, 45000);
      lastQuotaCheck = Date.now();
    }
    const result = await bridge.call('due', {}, 10000);
    attention.show(result.events || []);
    if (result.events?.length && sms) {
      sendSms('TOKENMAXXING\n' + result.events.map(e => e.message).join('\n')).then(status => send('sms:status', status)).catch(() => {});
    }
  } catch { /* Retry on the next tick; never fabricate a reset. */ }
  finally { alertBusy = false; }
}
function previewAlert() {
  attention.show([{id: 'preview', phase: 'preview', message: 'PREVIEW: This is how your 15-minute reminder and confirmed-reset alert will appear.'}]);
}
const providerLinks = new ProviderLinkManager({
  onChange: (state) => {
    send('provider:link', state);
  },
});
const watchedCredentials = [];

// Claude Code rewrites its credential file often. Without this, every write
// forced a fresh quota request and the provider row flickered.
let credentialTimer = null;
function credentialsChanged() {
  if (credentialTimer) return;
  credentialTimer = setTimeout(() => { credentialTimer = null; send('providers:changed', {}); }, 10000);
  credentialTimer.unref?.();
}

function watchExistingConnections() {
  const home = app.getPath('home');
  const credentials = [
    path.join(process.env.CLAUDE_CONFIG_DIR || path.join(home, '.claude'), '.credentials.json'),
    path.join(process.env.CODEX_HOME || path.join(home, '.codex'), 'auth.json'),
  ];
  for (const file of credentials) {
    const changed = (current, previous) => {
      if (current.mtimeMs !== previous.mtimeMs) credentialsChanged();
    };
    fs.watchFile(file, {interval: 2000, persistent: false}, changed);
    watchedCredentials.push([file, changed]);
  }
}
// The engine can come up before the renderer has registered its listeners, so
// its state is remembered here and replayed on demand.
let engineState = { ready: false, hello: null, fatal: null };

// ── python discovery ──────────────────────────────────────────────────────
// stdlib-only engine, so any CPython 3.10+ on PATH will do.

function pythonCandidates() {
  const out = [];
  if (process.env.TOKEN_HUD_PYTHON) out.push({ cmd: process.env.TOKEN_HUD_PYTHON, args: [] });
  if (process.platform === 'win32') {
    out.push({ cmd: 'py', args: ['-3'] }, { cmd: 'python', args: [] }, { cmd: 'python3', args: [] });
  } else {
    out.push({ cmd: 'python3', args: [] }, { cmd: 'python', args: [] });
  }
  return out;
}

function resolvePython() {
  for (const candidate of pythonCandidates()) {
    const probe = spawnSync(candidate.cmd, [...candidate.args, '-c', 'import sys;print(sys.version_info[:2])'], {
      encoding: 'utf8',
      timeout: 8000,
      windowsHide: true,
    });
    if (probe.status === 0 && /\(3, (1[0-9]|[2-9][0-9])\)/.test(probe.stdout || '')) return candidate;
  }
  return null;
}

// ── bridge process ────────────────────────────────────────────────────────
// One long-lived process speaking newline-delimited JSON. Requests are
// correlated by id, so a slow cycle fetch and a fast reset tick can overlap.

class Bridge {
  constructor(python) {
    this.python = python;
    this.seq = 0;
    this.pending = new Map();
    this.buffer = '';
    this.ready = false;
    this.child = spawn(python.cmd, python.bridgeArgs || [...python.args, '-u', BRIDGE], {
      cwd: app.isPackaged ? process.resourcesPath : REPO_ROOT,
      env: {...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8'},
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk) => this.onData(chunk));
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', (text) => process.stderr.write(`[engine] ${text}`));
    this.child.on('error', error => fatal(`Usage engine could not start: ${error.message}`));
    this.child.on('exit', (code) => this.onExit(code));
  }

  onData(chunk) {
    this.buffer += chunk;
    let nl;
    while ((nl = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (!line) continue;
      let frame;
      try {
        frame = JSON.parse(line);
      } catch {
        process.stderr.write(`[engine] unparsable frame: ${line}\n`);
        continue;
      }
      if (frame.event === 'ready') {
        this.ready = true;
        this.hello = frame.data;
        engineState = { ready: true, hello: frame.data, fatal: null };
        send('engine:ready', frame.data);
        continue;
      }
      const slot = this.pending.get(frame.id);
      if (!slot) continue;
      this.pending.delete(frame.id);
      clearTimeout(slot.timer);
      if (frame.ok) slot.resolve(frame.data);
      else slot.reject(Object.assign(new Error(frame.error?.message || 'engine error'), { kind: frame.error?.kind }));
    }
  }

  onExit(code) {
    this.ready = false;
    for (const slot of this.pending.values()) {
      clearTimeout(slot.timer);
      slot.reject(new Error(`engine exited (${code})`));
    }
    this.pending.clear();
    if (bridge === this) {
      engineState = { ready: false, hello: null, fatal: null };
      send('engine:down', { code });
    }
  }

  call(cmd, args = {}, timeoutMs = 120000) {
    if (!this.child || this.child.killed) return Promise.reject(new Error('engine not running'));
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${cmd} timed out`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(`${JSON.stringify({ id, cmd, args })}\n`);
    });
  }

  stop() {
    if (this.child && !this.child.killed) {
      try {
        this.child.stdin.end();
      } catch {}
      this.child.kill();
    }
  }
}

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function fatal(message) {
  engineState = { ready: false, hello: null, fatal: { message } };
  send('engine:fatal', { message });
}

// ── window ────────────────────────────────────────────────────────────────

function createWindow() {
  win = new BrowserWindow({
    ...WIN,
    show: process.platform === 'win32',
    backgroundColor: '#0c0c0f',
    title: 'TOKENMAXXING',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'hidden',
    titleBarOverlay:
      process.platform === 'darwin'
        ? undefined
        : { color: '#00000000', symbolColor: '#e9e9ee', height: 44 },
    trafficLightPosition: { x: 18, y: 18 },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      backgroundThrottling: false,
    },
  });

  win.setAlwaysOnTop(true, 'floating');
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => win.show());
  win.webContents.on('did-finish-load', () => {
    if (process.argv.includes('--preview-alert')) previewAlert();
  });
  if (process.argv.includes('--dev')) win.webContents.openDevTools({ mode: 'detach' });
  win.on('closed', () => {
    win = null;
  });
}

function startBridge() {
  if (app.isPackaged) {
    const engine = path.join(process.resourcesPath, 'engine', 'tokenmaxxing-engine.exe');
    if (!fs.existsSync(engine)) { fatal('The bundled usage engine is missing. Reinstall TOKENMAXXING.'); return; }
    bridge = new Bridge({cmd: engine, bridgeArgs: []});
    return;
  }
  if (!fs.existsSync(BRIDGE)) {
    fatal(`hud_bridge.py not found at ${BRIDGE}`);
    return;
  }
  const python = resolvePython();
  if (!python) {
    fatal('No Python 3.10+ found. Install Python, or point TOKEN_HUD_PYTHON at the interpreter.');
    return;
  }
  bridge = new Bridge(python);
}

// ── ipc ───────────────────────────────────────────────────────────────────

ipcMain.handle('engine:call', async (_event, cmd, args) => {
  if (!bridge) throw new Error('engine not started');
  if (cmd === 'sessions') args = {...args, active_title: activeChat.currentTitle(args?.provider === 'claude' ? 'claude' : 'codex')};
  // The cycle fetch pages the dashboard and can legitimately run long.
  const timeout = cmd === 'cycle' ? 180000 : cmd === 'providers' ? 45000 : 30000;
  return bridge.call(cmd, args || {}, timeout);
});

// The renderer asks for this on load; whoever wins the race, it still boots.
ipcMain.handle('engine:status', () => engineState);
ipcMain.handle('updates:status', () => updates?.state);
ipcMain.handle('updates:check', () => { updates?.check(); return updates?.state; });
ipcMain.handle('updates:install', () => updates?.install() || false);
ipcMain.handle('reset:preview', previewAlert);
ipcMain.handle('reset:status', () => attention.active);
ipcMain.handle('reset:dismiss', () => attention.dismiss());
ipcMain.handle('sms:status', () => sms.status());
ipcMain.handle('sms:save', (_event, config) => sms.save(config));
ipcMain.handle('sms:test', () => sendSms('TOKENMAXXING test: SMS alerts are connected. You will receive reminders 15 minutes before reported resets and again after a confirmed reset.'));

ipcMain.handle('engine:restart', async () => {
  if (bridge) bridge.stop();
  startBridge();
  return true;
});

ipcMain.handle('provider:connect', (_event, provider) => providerLinks.start(provider));
ipcMain.handle('provider:link-status', (_event, provider) => providerLinks.status(provider));
ipcMain.handle('provider:cancel', (_event, provider) => providerLinks.cancel(provider));
ipcMain.handle('provider:open-login', async (_event, provider) => {
  const state = providerLinks.status(provider);
  if (state.authUrl) await shell.openExternal(state.authUrl);
});
ipcMain.handle('provider:open-install', async (_event, provider) => {
  const state = providerLinks.status(provider);
  if (state.installUrl) await shell.openExternal(state.installUrl);
});

ipcMain.handle('win:always-on-top', (_event, value) => {
  if (!win) return false;
  win.setAlwaysOnTop(Boolean(value), 'floating');
  return win.isAlwaysOnTop();
});

ipcMain.handle('win:state', () => ({
  alwaysOnTop: win ? win.isAlwaysOnTop() : false,
  platform: process.platform,
  openSmsSetup: process.argv.includes('--sms-setup'),
  openSessions: process.argv.includes('--sessions'),
}));

ipcMain.handle('shell:open-path', (_event, target) => shell.openPath(target));
ipcMain.handle('shell:show-item', (_event, target) => shell.showItemInFolder(target));
ipcMain.handle('shell:open-external', (_event, url) => {
  if (/^https:\/\//.test(url)) return shell.openExternal(url);
  return null;
});

// Flash the window for a reset buzz; the audible part stays in Python
// (winsound on Windows, Tk bell elsewhere) so one implementation owns it.
ipcMain.handle('win:attention', () => {
  if (!win) return false;
  win.flashFrame(true);
  setTimeout(() => win && !win.isDestroyed() && win.flashFrame(false), 4000);
  return true;
});

// ── lifecycle ─────────────────────────────────────────────────────────────

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });

  nativeTheme.themeSource = 'dark';

  app.whenReady().then(() => {
    const enabled = app.isPackaged && process.platform === 'win32';
    updates = new Updates({
      updater: enabled ? require('electron-updater').autoUpdater : null,
      enabled, version: app.getVersion(), publish: state => send('updates:status', state),
    });
    sms = new SmsAlerts({filename: path.join(app.getPath('userData'), 'twilio-credentials.enc'), storage: safeStorage});
    createWindow();
    startBridge();
    activeChat.start();
    updates.start();
    watchExistingConnections();
    alertPoll = setInterval(pollResetAlerts, 5000);
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    activeChat.stop();
    updates?.stop();
    clearInterval(alertPoll);
    attention.dismiss();
    providerLinks.dispose();
    for (const [file, listener] of watchedCredentials) fs.unwatchFile(file, listener);
    if (bridge) bridge.stop();
  });
}
