'use strict';
const {spawn} = require('node:child_process');
const path = require('node:path');

class ActiveChat {
  constructor({script, platform = process.platform, spawnProcess = spawn, now = Date.now}) {
    this.script = script;
    this.platform = platform;
    this.spawnProcess = spawnProcess;
    this.now = now;
    this.title = null;
    this.claudeTitle = null;
    this.observedAt = 0;
  }
  /** The open chat's title for a provider, or null once the reading is stale. */
  currentTitle(provider = 'codex') {
    if (this.now() - this.observedAt >= 4000) return null;
    return provider === 'claude' ? this.claudeTitle : this.title;
  }
  start() {
    if (this.platform !== 'win32' || this.child) return;
    this.buffer = '';
    this.observedAt = this.now();
    const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    const child = this.spawnProcess(executable, ['-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass', '-File', this.script],
      {windowsHide: true, stdio: ['ignore', 'pipe', 'ignore']});
    this.child = child;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      if (this.child !== child) return;
      this.buffer += chunk;
      if (this.buffer.length > 65536) { this.buffer = ''; this.title = null; this.claudeTitle = null; return; }
      let index;
      while ((index = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, index).trim();
        this.buffer = this.buffer.slice(index + 1);
        try {
          const value = JSON.parse(line);
          const text = (v) => typeof v === 'string' && v.length <= 1000 ? v : null;
          this.title = text(value.title);
          this.claudeTitle = text(value.claude);
          this.observedAt = this.now();
        } catch { this.title = null; this.claudeTitle = null; }
      }
    });
    const clear = () => { if (this.child === child) { this.title = null; this.claudeTitle = null; this.child = null; } };
    child.on('error', clear);
    child.on('exit', clear);
    if (!this.watchdog) {
      this.watchdog = setInterval(() => {
        if (this.child && this.now() - this.observedAt > 15000) { this.child.kill(); this.child = null; this.title = null; this.claudeTitle = null; }
        if (!this.child) this.start();
      }, 5000);
      this.watchdog.unref?.();
    }
  }
  stop() {
    clearInterval(this.watchdog);
    this.watchdog = null;
    this.child?.kill();
    this.child = null;
    this.title = null;
    this.claudeTitle = null;
  }
}
module.exports = {ActiveChat};
