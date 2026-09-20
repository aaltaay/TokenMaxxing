'use strict';

// Official CLI browser sign-in only. This module does not read credentials,
// print CLI output, submit model requests, or accept commands from the renderer.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const INSTALL_URLS = Object.freeze({
  claude: 'https://code.claude.com/docs/en/setup',
  codex: 'https://learn.chatgpt.com/docs/cli',
});
const AUTH_HOSTS = Object.freeze({
  claude: new Set(['auth.claude.ai', 'claude.ai', 'console.anthropic.com']),
  codex: new Set(['auth.openai.com']),
});

function requireProvider(provider) {
  if (provider !== 'claude' && provider !== 'codex') throw new TypeError('Unknown usage provider.');
}

function fileExists(filename) {
  try { return fs.statSync(filename).isFile(); } catch { return false; }
}

function pathDirectories(env = process.env) {
  return (env.PATH || env.Path || '').split(path.delimiter)
    .map(p => p.replace(/^"|"$/g, '')).filter(p => path.isAbsolute(p));
}

function findNode(directories) {
  const filename = process.platform === 'win32' ? 'node.exe' : 'node';
  const candidates = directories.map(dir => path.join(dir, filename));
  if (path.basename(process.execPath).toLowerCase() === filename) candidates.unshift(process.execPath);
  return candidates.find(fileExists) || null;
}

function discoverCli(provider) {
  requireProvider(provider);
  const home = os.homedir();
  const dirs = pathDirectories();
  const windows = process.platform === 'win32';
  const filename = provider + (windows ? '.exe' : '');
  const candidates = [path.join(home, '.local', 'bin', filename), ...dirs.map(dir => path.join(dir, filename))];
  if (provider === 'claude') {
    candidates.push(path.join(home, '.claude', 'local', filename));
  } else if (windows && process.env.LOCALAPPDATA) {
    const bundled = path.join(process.env.LOCALAPPDATA, 'OpenAI', 'Codex', 'bin');
    try {
      const desktop = fs.readdirSync(bundled, { withFileTypes: true })
        .filter(entry => entry.isDirectory())
        .map(entry => path.join(bundled, entry.name, 'codex.exe'))
        .filter(fileExists)
        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
      candidates.unshift(...desktop);
    } catch { /* Desktop installation is optional. */ }
  }
  const packageName = provider === 'claude' ? '@anthropic-ai/claude-code' : '@openai/codex';
  const packageRoots = dirs.map(dir => path.join(dir, 'node_modules', packageName));
  if (process.env.APPDATA) packageRoots.unshift(path.join(process.env.APPDATA, 'npm', 'node_modules', packageName));
  packageRoots.push(path.join(home, '.npm-global', 'lib', 'node_modules', packageName));
  packageRoots.push(path.join('/usr/local/lib/node_modules', packageName));
  packageRoots.push(path.join('/usr/lib/node_modules', packageName));
  for (const root of packageRoots) candidates.push(path.join(root, 'bin', filename));
  const native = candidates.find(fileExists);
  if (native) return { command: native, args: [] };
  const node = findNode(dirs);
  if (node) {
    const scripts = packageRoots.map(root => path.join(root, provider === 'claude' ? 'cli.js' : 'bin/codex.js'));
    const script = scripts.find(fileExists);
    if (script) return { command: node, args: [script] };
  }
  return null;
}

function allowedAuthUrl(provider, input) {
  requireProvider(provider);
  try {
    const parsed = new URL(input);
    if (parsed.protocol !== 'https:' || !AUTH_HOSTS[provider].has(parsed.hostname) ||
        parsed.username || parsed.password || (parsed.port && parsed.port !== '443') || parsed.hash) return null;
    if (!/^\/(?:oauth\/)?authorize\/?$/.test(parsed.pathname)) return null;
    for (const key of parsed.searchParams.keys()) {
      if (/^(?:access_token|refresh_token|id_token|token|client_secret)$/i.test(key)) return null;
      // Claude's authorization URL can contain the boolean `code=true` flag.
      if (key.toLowerCase() === 'code' && parsed.searchParams.get(key) !== 'true') return null;
    }
    return parsed.href;
  } catch { return null; }
}

function extractAuthUrl(provider, output) {
  const plain = String(output).replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '');
  for (const match of plain.matchAll(/https:\/\/[^\s<>"'`]+/g)) {
    // Only act on complete URLs, not an arbitrary partial stdout chunk.
    if (match.index + match[0].length === plain.length) continue;
    const candidate = allowedAuthUrl(provider, match[0].replace(/[),.;]+$/, ''));
    if (candidate) return candidate;
  }
  return null;
}

function initialState(provider) {
  return { provider, revision: 0, status: 'idle', message: '', startedAt: null, finishedAt: null,
    authUrl: null, installUrl: null };
}

class ProviderLinkManager {
  constructor(options = {}) {
    this.onChange = options.onChange || (() => {});
    this.resolveCli = options.resolveCli || discoverCli;
    this.spawnProcess = options.spawnProcess || spawn;
    this.timeoutMs = options.timeoutMs ?? 5 * 60 * 1000;
    this.probeTimeoutMs = options.probeTimeoutMs ?? 8000;
    this.states = { claude: initialState('claude'), codex: initialState('codex') };
    this.sessions = new Map();
    this.disposed = false;
  }

  status(provider) {
    if (provider !== undefined) {
      requireProvider(provider);
      return { ...this.states[provider] };
    }
    return { claude: this.status('claude'), codex: this.status('codex') };
  }

  _update(provider, changes) {
    // IPC action responses and events can arrive in a different order. Every
    // snapshot carries a per-provider revision so consumers can ignore old ones.
    const revision = this.states[provider].revision + 1;
    this.states[provider] = { ...this.states[provider], ...changes, revision };
    const state = this.status(provider);
    try { this.onChange(state); } catch { /* Consumer failures never leak CLI output. */ }
    return state;
  }

  start(provider) {
    requireProvider(provider);
    if (this.disposed) throw new Error('The sign-in manager is closed.');
    if (this.sessions.has(provider)) return this.status(provider);
    const session = { child: null, timer: null, done: false, buffers: { stdout: '', stderr: '' } };
    this.sessions.set(provider, session);
    const state = this._update(provider, { ...initialState(provider), status: 'connecting',
      startedAt: Date.now(), message: 'Opening the official sign-in page…' });
    session.timer = setTimeout(() => this._finish(provider, session, 'error',
      'Sign-in timed out. Try connecting again.', true), this.timeoutMs);
    session.timer.unref?.();
    Promise.resolve().then(() => this._begin(provider, session)).catch(() => {
      this._finish(provider, session, 'error', 'Could not start sign-in. Try again.', true);
    });
    return state;
  }

  _spawn(candidate, args) {
    return this.spawnProcess(candidate.command, [...(candidate.args || []), ...args], {
      shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      cwd: os.homedir(),
    });
  }

  async _begin(provider, session) {
    const candidate = await this.resolveCli(provider);
    if (session.done) return;
    if (!candidate) {
      this._finish(provider, session, 'error', `${provider === 'claude' ? 'Claude Code' : 'Codex'} is not installed.`,
        false, { installUrl: INSTALL_URLS[provider] });
      return;
    }
    let args = ['login'];
    if (provider === 'claude') {
      const help = await this._probe(candidate, session);
      if (session.done) return;
      if (!help || !/usage:\s*claude\s+auth\s+login/i.test(help)) {
        this._finish(provider, session, 'error', 'Update Claude Code to enable browser sign-in.',
          false, { installUrl: INSTALL_URLS.claude });
        return;
      }
      args = ['auth', 'login'];
      if (/--claudeai\b/.test(help)) args.push('--claudeai');
    }
    if (session.done) return;
    const child = this._spawn(candidate, args);
    session.child = child;
    for (const name of ['stdout', 'stderr']) {
      child[name]?.setEncoding?.('utf8');
      child[name]?.on('data', chunk => {
        if (session.done) return;
        session.buffers[name] = (session.buffers[name] + String(chunk)).slice(-16384);
        const authUrl = extractAuthUrl(provider, session.buffers[name]);
        if (authUrl && authUrl !== this.states[provider].authUrl) {
          this._update(provider, { authUrl, message: 'Complete sign-in in your browser.' });
        }
      });
    }
    child.once('error', () => this._finish(provider, session, 'error', 'Could not start sign-in. Try again.', true));
    child.once('close', code => this._finish(provider, session, code === 0 ? 'success' : 'error',
      code === 0 ? 'Sign-in completed. Refreshing usage…' : 'Sign-in did not complete. Try connecting again.'));
  }

  _probe(candidate, session) {
    return new Promise(resolve => {
      let child;
      try { child = this._spawn(candidate, ['auth', 'login', '--help']); }
      catch { resolve(null); return; }
      session.child = child;
      let help = '';
      let ended = false;
      const finish = result => {
        if (ended) return;
        ended = true;
        clearTimeout(timer);
        if (session.child === child) session.child = null;
        resolve(result);
      };
      const timer = setTimeout(() => {
        finish(null);
        try { child.kill(); } catch { /* Process already stopped. */ }
      }, this.probeTimeoutMs);
      timer.unref?.();
      for (const stream of [child.stdout, child.stderr]) {
        stream?.setEncoding?.('utf8');
        stream?.on('data', chunk => { if (help.length < 65536) help += String(chunk).slice(0, 65536 - help.length); });
      }
      child.once('error', () => finish(null));
      child.once('close', code => finish(code === 0 ? help : null));
    });
  }

  _finish(provider, session, status, message, terminate = false, extra = {}) {
    if (session.done) return this.status(provider);
    session.done = true;
    clearTimeout(session.timer);
    this.sessions.delete(provider);
    const child = session.child;
    session.child = null;
    session.buffers = { stdout: '', stderr: '' };
    if (terminate && child) {
      try { child.kill(); } catch { /* Process already stopped. */ }
    }
    return this._update(provider, { status, message, finishedAt: Date.now(), authUrl: null, ...extra });
  }

  cancel(provider) {
    requireProvider(provider);
    const session = this.sessions.get(provider);
    if (!session) return this.status(provider);
    return this._finish(provider, session, 'idle', 'Sign-in canceled.', true);
  }

  dispose() {
    this.disposed = true;
    for (const provider of [...this.sessions.keys()]) this.cancel(provider);
  }
}

module.exports = { ProviderLinkManager, discoverCli, allowedAuthUrl, extractAuthUrl, INSTALL_URLS };
