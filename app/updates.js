'use strict';

class Updates {
  constructor({updater, enabled, version, publish = () => {}}) {
    this.updater = updater;
    this.enabled = enabled;
    this.publish = publish;
    this.state = {status: enabled ? 'idle' : 'disabled', currentVersion: version};
    this.pending = null;
    if (!enabled) return;
    updater.autoDownload = true;
    updater.autoInstallOnAppQuit = false;
    updater.allowPrerelease = false;
    updater.allowDowngrade = false;
    updater.on('checking-for-update', () => this.set({status: 'checking'}));
    updater.on('update-available', info => this.set({status: 'downloading', version: info.version, percent: 0}));
    updater.on('download-progress', info => this.set({status: 'downloading', percent: Math.round(info.percent)}));
    updater.on('update-not-available', () => this.set({status: 'current'}));
    updater.on('update-downloaded', info => this.set({status: 'ready', version: info.version, percent: 100}));
    updater.on('error', () => this.set({status: 'error'}));
  }

  set(patch) {
    this.state = {...this.state, ...patch};
    this.publish(this.state);
  }

  async check() {
    if (!this.enabled || ['ready', 'installing', 'downloading'].includes(this.state.status)) return this.state;
    if (this.pending) return this.pending;
    this.set({status: 'checking', version: null, percent: 0});
    this.pending = (async () => {
      try {
        const result = await Promise.resolve().then(() => this.updater.checkForUpdates());
        // A failed background download rejects separately from the check itself.
        if (result?.downloadPromise) await result.downloadPromise;
      } catch { this.set({status: 'error'}); }
      finally { this.pending = null; }
      return this.state;
    })();
    return this.pending;
  }

  install() {
    if (!this.enabled || this.state.status !== 'ready') return false;
    this.set({status: 'installing'});
    try { this.updater.quitAndInstall(false, true); return true; }
    catch { this.set({status: 'error'}); return false; }
  }

  start() {
    if (!this.enabled || this.timer) return;
    this.check();
    this.timer = setInterval(() => this.check(), 6 * 60 * 60 * 1000);
    this.timer.unref?.();
  }

  stop() { clearInterval(this.timer); this.timer = null; }
}

module.exports = {Updates};
