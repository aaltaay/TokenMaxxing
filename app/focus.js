'use strict';
const {spawn} = require('node:child_process');
const path = require('node:path');

// Measures how long one of the user's AI tools is the window in front while
// the user is at the keyboard or mouse. focus.ps1 reports the foreground
// program and the idle time every few seconds; this file turns those readings
// into spans of time per app and hands them to the engine. A window title is
// read only to tell a claude.ai tab or an agent's terminal from any other, then
// dropped: it is never kept, logged or sent anywhere.

/** The apps counted, and exactly the ids the engine accepts. */
const FOCUS_APPS = Object.freeze(['claude', 'chatgpt', 'codex', 'cursor', 'vscode', 'windsurf', 'terminal']);
/** How often focus.ps1 reports. */
const SAMPLE_MS = 5000;
/** Five minutes without keyboard or mouse input means the user is away. */
const IDLE_CUTOFF_MS = 5 * 60000;
/** Longer than this between readings (sleep, a restarted reader) breaks a span. */
const GAP_MS = 15000;
/** How often finished spans and the open one so far go to the engine. */
const FLUSH_MS = 60000;
/** Spans kept while the engine is down; the oldest go first past this. */
const QUEUE_CAP = 2000;
/** A reader silent this long is replaced. */
const SILENT_MS = 30000;
/** Quitting waits at most this long for the last spans to reach the engine. */
const SETTLE_MS = 3000;

const DESKTOP = new Map([
  ['cursor', 'cursor'],
  ['code', 'vscode'], ['code - insiders', 'vscode'], ['vscodium', 'vscode'],
  ['windsurf', 'windsurf'], ['windsurf - next', 'windsurf'],
  ['chatgpt classic', 'chatgpt'],
]);
const BROWSERS = new Set(['chrome', 'msedge', 'firefox', 'brave', 'arc', 'opera', 'vivaldi', 'chromium', 'comet', 'zen',
  'librewolf', 'waterfox', 'floorp', 'thorium', 'dia']);
const TERMINALS = new Set(['windowsterminal', 'openconsole', 'conhost', 'cmd', 'powershell', 'pwsh', 'wezterm-gui', 'alacritty',
  'mintty', 'hyper', 'tabby', 'warp', 'kitty', 'ghostty', 'wsl', 'bash']);
// The Codex desktop app ships as ChatGPT.exe (and Codex.exe) inside the
// OpenAI.Codex package, beside the ChatGPT app's own "ChatGPT Classic.exe";
// only the install path tells them apart. Same rule as active-chat.ps1.
const CODEX_DESKTOP = /[\\/]OpenAI\.Codex_[^\\/]+[\\/]|[\\/]OpenAI[\\/]Codex[\\/]|[\\/]Codex[\\/]Codex\.exe$/i;
// claude.exe and codex.exe are also the command-line agents. When one of those
// owns a console window itself, it lives in one of these places.
const AGENT_CLI = /[\\/]\.local[\\/](?:bin|share)[\\/]|[\\/]node_modules[\\/]|[\\/]\.claude[\\/]/i;
// CLI agents name their terminal tab ("claude", "Gemini - project", "aider"),
// and Claude Code leads its title with a status glyph (✳ idle, ✶ ✻ ✽ ✢ busy).
const AGENT_TITLE = /\b(?:claude|codex|gemini|aider)\b/i;
const CLAUDE_CODE_GLYPH = /^[✳✶✻✽✢]️?\s/;

/**
 * A browser's title is the tab's title plus the browser's name, " - "
 * separated ("Fix the build - Claude - Google Chrome"), and Edge adds
 * "and 3 more pages". The site counts only when its name is a whole segment,
 * so a search for "claude pricing" does not.
 */
function browserSite(title) {
  if (/\bclaude\.ai\b/i.test(title)) return 'claude';
  if (/\bchatgpt\.com\b|\bchat\.openai\.com\b/i.test(title)) return 'chatgpt';
  for (const part of title.split(/\s+[-–—|·]\s+/)) {
    const name = part.replace(/\s+and \d+ more (?:pages?|tabs?)$/i, '').trim().toLowerCase();
    if (name === 'claude') return 'claude';
    if (name === 'chatgpt') return 'chatgpt';
  }
  return null;
}

/** The app id a foreground reading belongs to, or null when it is not counted. */
function classifyFocus(sample) {
  const exe = typeof sample?.exe === 'string' ? sample.exe.trim().toLowerCase().replace(/\.exe$/, '') : '';
  const where = typeof sample?.path === 'string' ? sample.path : '';
  const title = typeof sample?.title === 'string' ? sample.title : '';
  if (!exe) return null;
  if (exe === 'claude') return where && AGENT_CLI.test(where) ? 'terminal' : 'claude';
  if (exe === 'codex') return where && AGENT_CLI.test(where) ? 'terminal' : 'codex';
  if (exe === 'chatgpt') return CODEX_DESKTOP.test(where) ? 'codex' : 'chatgpt';
  if (DESKTOP.has(exe)) return DESKTOP.get(exe);
  if (BROWSERS.has(exe)) return browserSite(title);
  if (TERMINALS.has(exe)) return AGENT_TITLE.test(title) || CLAUDE_CODE_GLYPH.test(title) ? 'terminal' : null;
  return null;
}

/** A span in whole epoch seconds, or null when it rounds to nothing. */
function toSeconds(app, startMs, endMs) {
  const start = Math.round(startMs / 1000);
  const end = Math.round(endMs / 1000);
  return end > start ? {app, start, end} : null;
}

class FocusTracker {
  constructor({script, send, platform = process.platform, spawnProcess = spawn, now = Date.now, parentPid = process.pid}) {
    this.script = script;
    this.send = send;
    this.platform = platform;
    this.spawnProcess = spawnProcess;
    this.now = now;
    this.parentPid = parentPid;
    this.child = null;
    this.watchdog = null;
    this.closed = [];     // finished spans the engine has not confirmed yet
    this.open = null;     // {app, start, last} in ms: the span still growing
    this.lastSampleAt = 0;
    this.lastFlushAt = 0;
    this.flushing = null;
  }

  start() {
    if (this.platform !== 'win32' || this.child) return;
    this.buffer = '';
    this.heardAt = this.now();
    if (!this.lastFlushAt) this.lastFlushAt = this.now();
    const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    let child;
    try {
      child = this.spawnProcess(executable, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', this.script,
        '-ParentId', String(this.parentPid)], {windowsHide: true, stdio: ['ignore', 'pipe', 'ignore']});
    } catch { child = null; }
    if (child) {
      this.child = child;
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', chunk => {
        if (this.child !== child) return;
        this.buffer += chunk;
        if (this.buffer.length > 65536) { this.buffer = ''; return; }
        let index;
        while ((index = this.buffer.indexOf('\n')) >= 0) {
          const line = this.buffer.slice(0, index).trim();
          this.buffer = this.buffer.slice(index + 1);
          if (!line) continue;
          let sample;
          try { sample = JSON.parse(line); } catch { continue; }
          this.heardAt = this.now();
          this.observe(sample);
        }
      });
      const gone = () => { if (this.child === child) this.child = null; };
      child.on('error', gone);
      child.on('exit', gone);
    }
    if (!this.watchdog) {
      this.watchdog = setInterval(() => this.tick(), SAMPLE_MS);
      this.watchdog.unref?.();
    }
  }

  /** Keeps a reader running and the engine up to date, even while no readings arrive. */
  tick() {
    if (this.child && this.now() - this.heardAt > SILENT_MS) { this.child.kill(); this.child = null; }
    if (!this.child) this.start();
    if (this.now() - this.lastFlushAt >= FLUSH_MS) void this.flush();
  }

  /**
   * One reading from focus.ps1. Readings arrive every few seconds, so a switch
   * happened somewhere between two of them; the midpoint splits that time
   * between the app before and the app after, and one reading on its own is
   * worth one interval rather than nothing.
   */
  observe(sample) {
    const at = this.now();
    const app = classifyFocus(sample);
    const idle = Number(sample?.idle_ms);
    const counted = Boolean(app) && sample?.idle_ms !== null && Number.isFinite(idle) && idle >= 0 && idle < IDLE_CUTOFF_MS;
    const previous = this.lastSampleAt;
    const near = previous > 0 && at >= previous && at - previous <= GAP_MS;
    const between = Math.round((previous + at) / 2);
    if (this.open && !(counted && near && app === this.open.app)) this.close(near ? between : this.open.last);
    if (counted) {
      if (this.open) this.open.last = at;
      else this.open = {app, start: near ? between : at, last: at};
    }
    this.lastSampleAt = at;
    if (at - this.lastFlushAt >= FLUSH_MS) void this.flush();
  }

  close(endMs) {
    const span = toSeconds(this.open.app, this.open.start, endMs);
    this.open = null;
    if (!span) return;
    this.closed.push(span);
    if (this.closed.length > QUEUE_CAP) this.closed.splice(0, this.closed.length - QUEUE_CAP);
  }

  /** True while something is waiting to reach the engine. */
  pending() {
    return Boolean(this.open) || this.closed.length > 0;
  }

  /**
   * Hands the engine every finished span plus the open one so far. The engine
   * merges overlapping spans of one app, so resending the growing span each
   * minute is safe. Anything not confirmed waits for the next try.
   */
  flush() {
    if (this.flushing) return this.flushing;
    this.lastFlushAt = this.now();
    const batch = this.closed.slice();
    const spans = [...batch];
    const growing = this.open && toSeconds(this.open.app, this.open.start, this.open.last);
    if (growing) spans.push(growing);
    if (!spans.length) return Promise.resolve(true);
    let request;
    try { request = Promise.resolve(this.send({spans})); } catch (error) { request = Promise.reject(error); }
    this.flushing = request
      .then(() => {
        const sent = new Set(batch);
        this.closed = this.closed.filter(span => !sent.has(span));
        return true;
      }, () => false)
      .finally(() => { this.flushing = null; });
    return this.flushing;
  }

  /** Flushes after any flush already under way, giving up after `timeoutMs`. */
  settle(timeoutMs = SETTLE_MS) {
    const flushed = (this.flushing || Promise.resolve()).then(() => this.flush());
    let timer;
    const late = new Promise(resolve => { timer = setTimeout(() => resolve(false), timeoutMs); timer.unref?.(); });
    return Promise.race([flushed, late]).finally(() => clearTimeout(timer));
  }

  /** Stops reading, ends the open span at its last reading and hands over what is left. */
  stop(timeoutMs = SETTLE_MS) {
    clearInterval(this.watchdog);
    this.watchdog = null;
    const child = this.child;
    this.child = null;
    child?.kill();
    if (this.open) this.close(this.open.last);
    this.lastSampleAt = 0;
    return this.closed.length ? this.settle(timeoutMs) : Promise.resolve(true);
  }
}

module.exports = {FocusTracker, classifyFocus, FOCUS_APPS, SAMPLE_MS, IDLE_CUTOFF_MS, GAP_MS, FLUSH_MS, QUEUE_CAP};
