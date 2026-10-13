'use strict';

/**
 * In-app update rules, ported from Nova's updatePolicy.mjs. Pure: no Electron,
 * no electron-updater, no I/O. updates.js feeds every electron-updater event
 * through reduceUpdateState() and asks these functions what to do.
 *
 * Every merge to main is a vNNN Release. The installed app checks 15 s after
 * launch and every 2 hours while open, downloads a newer installer in the
 * background, then offers Restart to update. It never installs on its own: not
 * on quit, not on a timer.
 */

/** `0` / `false` / `no` / `off` in this environment variable stops automatic checks. */
const UPDATE_CHECK_ENV = 'TOKENMAXXING_UPDATE_CHECK';
/** Let the app and its engine finish starting before touching the network. */
const UPDATE_FIRST_CHECK_DELAY_MS = 15_000;
/** While the app stays open, ask GitHub again this often. */
const UPDATE_RECHECK_INTERVAL_MS = 2 * 60 * 60_000;
/** How often the open app looks whether a re-check is due. */
const UPDATE_RECHECK_TICK_MS = 10 * 60_000;

const OFF_VALUES = new Set(['0', 'false', 'no', 'off']);
const BUSY_PHASES = new Set(['checking', 'downloading', 'installing']);
const ERROR_TEXT_MAX = 160;
// Packed releases are 3.0.N for vNNN (scripts/release_version.py owns the mapping).
const RELEASE_SEMVER = /^3\.0\.(\d+)$/;

/**
 * Can this process update itself, and may it check without being asked?
 * `updater`: a packaged Windows (NSIS) install; a source checkout never updates.
 * `automatic`: the launch check and re-checks run. Check for updates needs only `updater`.
 */
function updateGate({isPackaged, platform, setting} = {}) {
  if (!isPackaged) return {updater: false, automatic: false, reason: 'running from source (not packaged)'};
  if (platform !== 'win32') return {updater: false, automatic: false, reason: `installer updates are Windows-only (${platform})`};
  if (OFF_VALUES.has(String(setting ?? '').trim().toLowerCase())) {
    return {updater: true, automatic: false, reason: `${UPDATE_CHECK_ENV} is off`};
  }
  return {updater: true, automatic: true, reason: ''};
}

/** `3.0.42` -> `v042`; any other version is shown as `v<version>`. */
function displayTag(version) {
  const text = String(version ?? '').trim();
  if (!text) return '';
  const release = RELEASE_SEMVER.exec(text);
  return release ? `v${release[1].padStart(3, '0')}` : `v${text.replace(/^v/, '')}`;
}

function errorText(err) {
  const raw = err instanceof Error ? err.message : String(err ?? '');
  const line = (raw.split(/\r?\n/).find(l => l.trim()) || 'unknown error').trim();
  return line.length > ERROR_TEXT_MAX ? `${line.slice(0, ERROR_TEXT_MAX - 1)}…` : line;
}

function clampPercent(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : 0;
}

/**
 * phase: idle | checking | downloading | ready | current | failed | installing.
 * `failedStage` says which step a failed phase failed in: `check` or `download`
 * (whose part stays on disk, so the next check resumes it). `retry` is the chunk
 * retry in progress while downloading. `promptedVersion` is the release the app
 * has asked about this session; `dismissedVersion` the one answered with Later.
 */
const INITIAL_UPDATE_STATE = Object.freeze({
  phase: 'idle', version: '', percent: 0, error: '', failedStage: '', retry: 0,
  promptedVersion: '', dismissedVersion: '',
});

function reduceUpdateState(state, event) {
  const ready = state.phase === 'ready';
  switch (event?.type) {
    // A downloaded installer survives any re-check.
    case 'checking':
      return ready ? state : {...state, phase: 'checking', error: '', failedStage: '', retry: 0};
    case 'available': {
      const version = String(event.version || '');
      if (ready && (!version || version === state.version)) return state;
      // The same version's stopped download keeps its percent: the part on disk resumes.
      const percent = version === state.version ? state.percent : 0;
      return {...state, phase: 'downloading', version, percent, error: '', failedStage: '', retry: 0};
    }
    case 'not-available':
      return ready ? state : {...state, phase: 'current', percent: 0, error: ''};
    case 'progress':
      return state.phase === 'downloading' ? {...state, percent: clampPercent(event.percent), retry: 0} : state;
    case 'retrying':
      return state.phase === 'downloading' ? {...state, retry: Math.max(1, Number(event.attempt) || 1)} : state;
    case 'downloaded':
      return {...state, phase: 'ready', version: String(event.version || state.version), percent: 100, error: ''};
    case 'error': {
      if (ready) return {...state, error: errorText(event.message)};
      const download = state.phase === 'downloading';
      return {
        ...state, phase: 'failed', failedStage: download ? 'download' : 'check',
        percent: download ? state.percent : 0, retry: 0, error: errorText(event.message),
      };
    }
    case 'prompted':
      return {...state, promptedVersion: String(event.version || '')};
    case 'dismissed':
      return {...state, dismissedVersion: String(event.version || '')};
    case 'installing':
      return ready ? {...state, phase: 'installing', error: ''} : state;
    case 'install-failed':
      // The downloaded installer is still cached; Restart to update can run again.
      return {...state, phase: 'ready', error: errorText(event.message)};
    default:
      return state;
  }
}

/** An automatic check: only when allowed and nothing is already downloaded or in flight. */
function shouldAutoCheck(state, gate) {
  if (!gate?.updater || !gate.automatic) return false;
  return state.phase !== 'ready' && !BUSY_PHASES.has(state.phase);
}

/** A re-check while the app stays open: as an automatic check allows, once per interval. */
function shouldRecheck(state, gate, {now, lastCheckAt} = {}) {
  return shouldAutoCheck(state, gate) && now - (Number(lastCheckAt) || 0) >= UPDATE_RECHECK_INTERVAL_MS;
}

/** Check for updates runs unless busy; it also looks past a downloaded release for a newer one. */
function manualCheckAction(state, gate) {
  if (!gate?.updater) return 'unavailable';
  return BUSY_PHASES.has(state.phase) ? 'busy' : 'check';
}

/** Ask for attention once per version per session, never after Later. */
function needsPrompt(state) {
  const {version} = state;
  return state.phase === 'ready' && Boolean(version)
    && state.promptedVersion !== version && state.dismissedVersion !== version;
}

/** Taskbar progress: a fraction while downloading, -1 (cleared) otherwise. */
function taskbarProgress(state) {
  return state.phase === 'downloading' ? clampPercent(state.percent) / 100 : -1;
}

/** What the renderer draws. `status` is the phase, or `disabled` when this copy cannot update. */
function updateView(state, {gate, installedVersion}) {
  return {
    status: gate?.updater ? state.phase : 'disabled',
    installed: displayTag(installedVersion),
    version: displayTag(state.version),
    percent: Math.floor(clampPercent(state.percent)),
    retry: state.phase === 'downloading' ? state.retry : 0,
    error: state.error,
    failedStage: state.failedStage,
    dismissed: Boolean(state.version) && state.dismissedVersion === state.version,
    automatic: Boolean(gate?.automatic),
    reason: gate?.reason || '',
  };
}

module.exports = {
  UPDATE_CHECK_ENV, UPDATE_FIRST_CHECK_DELAY_MS, UPDATE_RECHECK_INTERVAL_MS, UPDATE_RECHECK_TICK_MS,
  INITIAL_UPDATE_STATE, updateGate, displayTag, errorText, clampPercent, reduceUpdateState,
  shouldAutoCheck, shouldRecheck, manualCheckAction, needsPrompt, taskbarProgress, updateView,
};
