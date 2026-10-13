'use strict';

/**
 * The in-app updater's log, ported from Nova's updateLog.mjs. A packaged app's
 * main-process console goes nowhere, so every update line also goes to
 * `update.log` in the app's logs folder (`%APPDATA%\TOKENMAXXING\logs`), which
 * rolls to `update.log.1` past UPDATE_LOG_MAX_BYTES. Writing never throws: a log
 * that cannot be written must not stop an update. electron-updater gets the same
 * logger, so its own lines land here too.
 */
const fs = require('node:fs');
const path = require('node:path');

const UPDATE_LOG_FILE = 'update.log';
const UPDATE_LOG_MAX_BYTES = 1_000_000;

/** `dir` is asked on every line, so a folder that appears later is used. */
function createUpdateLogger({prefix, dir, now = () => new Date()}) {
  let fileBroken = false;
  const toFile = (level, msg) => {
    if (fileBroken) return;
    try {
      const folder = dir();
      if (!folder) return;
      fs.mkdirSync(folder, {recursive: true});
      const file = path.join(folder, UPDATE_LOG_FILE);
      if (fs.existsSync(file) && fs.statSync(file).size > UPDATE_LOG_MAX_BYTES) fs.renameSync(file, `${file}.1`);
      fs.appendFileSync(file, `${now().toISOString()} ${level} ${msg}\n`);
    } catch (err) {
      fileBroken = true;
      console.warn(prefix, `update log unavailable: ${err instanceof Error ? err.message : String(err)}`);
    }
  };
  const line = (level, write) => (msg) => {
    const text = String(msg);
    write(prefix, text);
    toFile(level, text);
  };
  return {
    info: line('INFO', console.log),
    warn: line('WARN', console.warn),
    error: line('ERROR', console.error),
    debug: () => {},
  };
}

module.exports = {UPDATE_LOG_FILE, UPDATE_LOG_MAX_BYTES, createUpdateLogger};
