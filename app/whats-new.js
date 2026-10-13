'use strict';

/**
 * What's new, ported from Nova's whatsNew.mjs: the release notes of the update
 * just installed, shown as a floating card the first time a new version opens,
 * and again on request from the notifications panel.
 *
 * Persisted state: `<userData>/whats-new.json`, `schema_version` 1:
 * `{schema_version, seen_tag, closed_at}`, the release whose notes the user last
 * closed. Owner: this module. Invalidation: closing the card writes the installed
 * tag. No file (a fresh install, or the first version with What's new) shows the
 * installed release's own notes. An unknown schema_version is left alone and
 * shows nothing.
 *
 * The view it publishes: `{schema_version: 1, available, card}`. `available`
 * says this copy is a vNNN release, so it has notes to reopen. `card` is null or
 * `{mode: updated|recent, tag, since, notes}`.
 */
const fs = require('node:fs');
const path = require('node:path');
const {RECENT_RELEASES_LISTED, tagNumber, whatsNewRange} = require('./release-notes');
const {LOADING_NOTES} = require('./release-notes-source');

const WHATS_NEW_FILE = 'whats-new.json';
const WHATS_NEW_SCHEMA_VERSION = 1;
const WHATS_NEW_VIEW_SCHEMA_VERSION = 1;

const noop = () => {};

/**
 * @param {{notesSource: {load: (range: object) => Promise<any>}, dir: () => string, installedTag: string,
 *   logger: {info: Function, warn: Function}, publish?: (view: object) => void, now?: () => Date}} deps
 */
function createWhatsNew({notesSource, dir, installedTag, logger, publish = noop, now = () => new Date()}) {
  const available = tagNumber(installedTag) != null;
  let card = null;
  let shown = 0;
  const file = () => {
    const folder = dir();
    return folder ? path.join(folder, WHATS_NEW_FILE) : '';
  };
  const view = () => ({schema_version: WHATS_NEW_VIEW_SCHEMA_VERSION, available, card});
  const set = value => {
    card = value;
    publish(view());
  };

  /** `{ok: true, seen}` (seen null when never closed), or `{ok: false}` for a file this version does not read. */
  function readSeen() {
    const target = file();
    if (!target || !fs.existsSync(target)) return {ok: true, seen: null};
    try {
      const parsed = JSON.parse(fs.readFileSync(target, 'utf8'));
      if (parsed?.schema_version !== WHATS_NEW_SCHEMA_VERSION) {
        logger.warn(`${target} has schema_version ${parsed?.schema_version}; What's new stays closed`);
        return {ok: false};
      }
      return {ok: true, seen: typeof parsed.seen_tag === 'string' ? parsed.seen_tag : null};
    } catch (err) {
      logger.warn(`${target} unreadable (${err instanceof Error ? err.message : String(err)}); showing this release's notes`);
      return {ok: true, seen: null};
    }
  }

  function writeSeen() {
    const target = file();
    // A file this version does not read is left for the version that wrote it.
    if (!target || !readSeen().ok) return;
    const body = {schema_version: WHATS_NEW_SCHEMA_VERSION, seen_tag: installedTag, closed_at: now().toISOString()};
    try {
      fs.mkdirSync(path.dirname(target), {recursive: true});
      fs.writeFileSync(`${target}.tmp`, JSON.stringify(body));
      fs.renameSync(`${target}.tmp`, target);
    } catch (err) {
      logger.warn(`What's new not recorded as read: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function show(mode, range) {
    shown += 1;
    const ticket = shown;
    const head = {mode, tag: installedTag, since: range.since ?? null};
    set({...head, notes: LOADING_NOTES});
    let notes;
    try {
      notes = await notesSource.load(range);
    } catch (err) {
      notes = {...LOADING_NOTES, loading: false, error: `Release notes could not be loaded: ${err instanceof Error ? err.message : String(err)}`};
    }
    // Closed (or replaced) while the notes loaded: leave it as it is now.
    if (ticket === shown) set({...head, notes});
    return notes;
  }

  return {
    get view() {
      return view();
    },

    /** On launch: the card for an update the user has not read yet. */
    async start() {
      if (!available) return;
      const seen = readSeen();
      if (!seen.ok) return;
      const range = whatsNewRange({installed: installedTag, seen: seen.seen});
      if (!range) return;
      logger.info(`What's new: ${installedTag}${range.since ? ` since ${range.since}` : ''}`);
      await show('updated', range);
    },

    /** From the notifications panel: the installed release and the ones before it. Answers at once, loading. */
    openRecent() {
      if (available) void show('recent', {after: null, through: installedTag, limit: RECENT_RELEASES_LISTED, since: null});
      return view();
    },

    /** Got it / ×: the card is not shown again for this version. */
    close() {
      shown += 1;
      set(null);
      if (available) writeSeen();
      return view();
    },
  };
}

module.exports = {WHATS_NEW_FILE, WHATS_NEW_SCHEMA_VERSION, WHATS_NEW_VIEW_SCHEMA_VERSION, createWhatsNew};
