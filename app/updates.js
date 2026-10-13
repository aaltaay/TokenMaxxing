'use strict';

/**
 * In-app updates: electron-updater reading this repo's GitHub Releases, wired
 * the way Nova's autoUpdate.mjs does it. The rules live in update-policy.js and
 * the resumable download in update-download.js; this class is the wiring.
 *
 * Every merge to main becomes a vNNN Release (.github/workflows/release.yml).
 * 15 s after launch, and every 2 hours while the app stays open, it asks GitHub
 * for the latest Release. A newer one downloads in the background, and the
 * notifications panel then offers Restart to update / Later. Nothing installs
 * until that click: not on quit, not on a timer. Restart to update stops the
 * usage engine first and installs nothing unless the engine is confirmed stopped.
 *
 * Once a newer release is found, its release notes (and those of every release
 * between it and this one) load through `notesSource` (release-notes-source.js),
 * so the ready row can say what Restart to update brings. The view carries them
 * as `notes` while they belong to the release on offer.
 *
 * Every failure is logged and swallowed: the app keeps running on the version it has.
 */
const policy = require('./update-policy');
const {updateRange} = require('./release-notes');
const {LOADING_NOTES} = require('./release-notes-source');
const {downloadRelease} = require('./update-download');

const noop = () => {};
const quietLogger = {info: noop, warn: noop, error: noop, debug: noop};

class Updates {
  constructor({updater, gate, version, fetch, logger = quietLogger, publish = noop, attention = noop,
    setProgress = noop, stopEngine = async () => true, restartEngine = async () => {}, now = Date.now, sleep,
    notesSource = null}) {
    this.updater = gate?.updater ? updater || null : null;
    this.gate = this.updater ? gate
      : {updater: false, automatic: false, reason: gate?.reason || 'the updater could not load'};
    this.version = version;
    this.fetch = fetch;
    this.logger = logger;
    this.hooks = {publish, attention, setProgress, stopEngine, restartEngine};
    this.now = now;
    this.sleep = sleep;
    this.notesSource = notesSource;
    this.notes = null; // {through, payload} for the release the notes were loaded for
    this.state = policy.INITIAL_UPDATE_STATE;
    this.pending = null;
    this.lastCheckAt = 0;
    if (!this.updater) return;
    const u = this.updater;
    u.logger = logger;
    u.autoDownload = false; // update-download.js fetches the installer itself
    u.autoInstallOnAppQuit = false; // never install on quit
    u.autoRunAppAfterInstall = true;
    u.allowPrerelease = false;
    u.allowDowngrade = false;
    u.on('checking-for-update', () => this.dispatch({type: 'checking'}));
    u.on('update-available', info => this.dispatch({type: 'available', version: info?.version}));
    u.on('update-not-available', () => this.dispatch({type: 'not-available'}));
    // Only electron-updater's own download (the fallback) reports through this.
    u.on('download-progress', p => this.dispatch({type: 'progress', percent: p?.percent}));
    u.on('update-downloaded', info => {
      this.dispatch({type: 'downloaded', version: info?.version});
      this.prompt();
    });
    u.on('error', err => this.onError(err));
  }

  get view() {
    const view = policy.updateView(this.state, {gate: this.gate, installedVersion: this.version});
    return {...view, notes: this.notes && this.notes.through === view.version ? this.notes.payload : null};
  }

  dispatch(event) {
    this.state = policy.reduceUpdateState(this.state, event);
    this.loadNotes(event);
    this.hooks.setProgress(policy.taskbarProgress(this.state));
    this.hooks.publish(this.view);
  }

  /**
   * Once per release found: the notes of everything it brings, published when they
   * arrive. A load that failed tries again when the release is found again or has
   * just downloaded (the connection works), never on every progress tick.
   */
  loadNotes(event) {
    const range = updateRange({installed: policy.displayTag(this.version), available: policy.displayTag(this.state.version)});
    if (!this.notesSource || !range) return;
    const retry = Boolean(this.notes?.payload?.error) && ['available', 'downloaded'].includes(event?.type);
    if (this.notes?.through === range.through && !retry) return;
    const notes = {through: range.through, payload: LOADING_NOTES};
    this.notes = notes;
    Promise.resolve()
      .then(() => this.notesSource.load(range))
      .catch(err => ({...LOADING_NOTES, loading: false, error: `Release notes could not be loaded: ${policy.errorText(err)}`}))
      .then(payload => {
        if (this.notes !== notes) return; // a newer release replaced it
        notes.payload = payload;
        this.hooks.publish(this.view);
      });
  }

  onError(err) {
    const message = policy.errorText(err);
    if (this.state.phase === 'installing') {
      // The installer did not start: the engine comes back and the download stays offered.
      this.logger.error(`install failed: ${message}`);
      this.dispatch({type: 'install-failed', message});
      void this.recoverEngine();
      return;
    }
    this.logger.error(`update error: ${message}`);
    this.dispatch({type: 'error', message});
  }

  /** Ask for attention once per version per session; the notifications panel holds the buttons. */
  prompt() {
    if (!policy.needsPrompt(this.state)) return;
    this.dispatch({type: 'prompted', version: this.state.version});
    this.logger.info(`${policy.displayTag(this.state.version)} is ready; offering Restart to update`);
    this.hooks.attention();
  }

  /**
   * One check at a time; a second call shares the first. Resolves once the check,
   * and any download it starts, has ended.
   * @param {'launch' | 'recheck' | 'manual'} origin
   */
  check(origin = 'manual') {
    if (!this.updater) return Promise.resolve(this.view);
    if (this.pending) return this.pending;
    const action = origin === 'manual' ? policy.manualCheckAction(this.state, this.gate)
      : policy.shouldAutoCheck(this.state, this.gate) ? 'check' : 'skip';
    if (action !== 'check') return Promise.resolve(this.view);
    this.lastCheckAt = this.now();
    if (origin === 'recheck') this.logger.info('re-checking while the app stays open');
    this.pending = this.run().then(() => {
      this.pending = null;
      return this.view;
    });
    return this.pending;
  }

  async run() {
    let result;
    try {
      result = await Promise.resolve().then(() => this.updater.checkForUpdates());
    } catch (err) {
      this.logger.warn(`check failed: ${policy.errorText(err)}`);
      // electron-updater usually emitted 'error' already; a throw before it did still counts.
      if (this.state.phase !== 'failed') this.dispatch({type: 'error', message: err});
      return;
    }
    if (result?.isUpdateAvailable) await this.download(result.updateInfo);
  }

  async download(info) {
    // 'update-available' moved the phase to downloading, unless this release is already downloaded.
    if (this.state.phase !== 'downloading') return;
    this.logger.info(`downloading ${policy.displayTag(this.state.version)}`);
    try {
      await downloadRelease({
        updater: this.updater, info, fetch: this.fetch, logger: this.logger, sleep: this.sleep,
        onProgress: percent => this.dispatch({type: 'progress', percent}),
        onRetry: ({attempt}) => this.dispatch({type: 'retrying', attempt}),
      });
    } catch (err) {
      this.logger.error(`download failed: ${policy.errorText(err)}`);
      this.dispatch({type: 'error', message: err});
    }
  }

  /** Later: stop asking about this release this session. Restart to update stays in the panel. */
  later() {
    if (this.state.version) {
      this.logger.info(`${policy.displayTag(this.state.version)} postponed`);
      this.dispatch({type: 'dismissed', version: this.state.version});
    }
    return this.view;
  }

  async install() {
    if (!this.updater || this.state.phase !== 'ready') return false;
    const tag = policy.displayTag(this.state.version);
    this.dispatch({type: 'installing'});
    let stopped = false;
    try {
      stopped = await this.hooks.stopEngine();
    } catch (err) {
      this.logger.error(`engine stop before install failed: ${policy.errorText(err)}`);
    }
    if (!stopped) {
      // The installer cannot replace files a running engine holds, so nothing installs.
      this.logger.error(`${tag} not installed: the usage engine did not stop`);
      this.dispatch({type: 'install-failed', message: 'The usage engine did not stop, so nothing was installed. Try again.'});
      await this.recoverEngine();
      return false;
    }
    this.logger.info(`installing ${tag}`);
    try {
      // The one-click installer shows its own progress, replaces this install in
      // place and opens the new version, which starts its own engine. Settings
      // live in userData and survive.
      this.updater.quitAndInstall(false, true);
      return true;
    } catch (err) {
      this.logger.error(`install failed: ${policy.errorText(err)}`);
      this.dispatch({type: 'install-failed', message: err});
      await this.recoverEngine();
      return false;
    }
  }

  async recoverEngine() {
    try {
      await this.hooks.restartEngine();
    } catch (err) {
      this.logger.error(`engine restart after a failed install: ${policy.errorText(err)}`);
    }
  }

  start() {
    if (!this.updater) {
      this.logger.info(`in-app updates off: ${this.gate.reason}`);
      return;
    }
    if (this.timer) return;
    if (!this.gate.automatic) {
      this.logger.info(`automatic checks off: ${this.gate.reason}`);
      return;
    }
    this.firstCheck = setTimeout(() => void this.check('launch'), policy.UPDATE_FIRST_CHECK_DELAY_MS);
    this.timer = setInterval(() => {
      if (policy.shouldRecheck(this.state, this.gate, {now: this.now(), lastCheckAt: this.lastCheckAt})) {
        void this.check('recheck');
      }
    }, policy.UPDATE_RECHECK_TICK_MS);
    this.firstCheck.unref?.();
    this.timer.unref?.();
  }

  stop() {
    clearTimeout(this.firstCheck);
    clearInterval(this.timer);
    this.firstCheck = null;
    this.timer = null;
  }
}

module.exports = {Updates};
