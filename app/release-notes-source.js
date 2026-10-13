'use strict';

/**
 * Where the app's release notes come from, ported from Nova's
 * releaseNotesSource.mjs: GitHub's Releases API, one page, kept in a cache so
 * the What's new card after an update reads the notes the bell panel already
 * fetched for it, with no second request and nothing to fetch when the
 * connection is down at launch.
 *
 * Persisted state: `<userData>/release-notes-cache.json`, `schema_version` 1:
 * `{schema_version, fetched_at (epoch ms), rows: [{tag_name, body, html_url,
 * published_at, draft, prerelease}]}`. Owner: this module. Invalidation: a
 * request whose range the cache does not cover fetches and replaces it; an
 * unknown schema_version is ignored (and replaced by the next fetch).
 *
 * A failed fetch never throws: the answer says why (`error`), and a cache that
 * holds the release answers with its notes instead.
 */
const fs = require('node:fs');
const path = require('node:path');
const {
  RELEASES_API_URL, RELEASES_PAGE_URL, RELEASES_PER_PAGE, RELEASE_NOTES_MAX_LISTED, selectReleases, tagNumber,
} = require('./release-notes');

const RELEASE_NOTES_CACHE_FILE = 'release-notes-cache.json';
const RELEASE_NOTES_CACHE_SCHEMA_VERSION = 1;
const RELEASE_NOTES_FETCH_TIMEOUT_MS = 20_000;
const RELEASE_NOTES_FETCH_ATTEMPTS = 2;
const ROW_FIELDS = ['tag_name', 'body', 'html_url', 'published_at', 'draft', 'prerelease'];
const ERROR_MAX = 160;

function errorLine(err) {
  const raw = err instanceof Error ? err.message : String(err ?? '');
  const line = raw.split(/\r?\n/).find(l => l.trim())?.trim() || 'unknown error';
  return line.length > ERROR_MAX ? `${line.slice(0, ERROR_MAX - 1)}…` : line;
}

function keep(row) {
  const out = {};
  for (const key of ROW_FIELDS) out[key] = row?.[key] ?? null;
  return out;
}

/** Holds every release in (after, through]: it has `through`, and it reaches back to `after` or is GitHub's whole list. */
function cacheCovers(rows, {after, through}) {
  if (!Array.isArray(rows) || !rows.some(r => r?.tag_name === through)) return false;
  const floor = tagNumber(after);
  if (floor == null || rows.length < RELEASES_PER_PAGE) return true;
  return rows.some(r => {
    const n = tagNumber(r?.tag_name);
    return n != null && n <= floor;
  });
}

/** The notes answer the renderer draws. */
function notesPayload(rows, range, error = null) {
  const picked = rows ? selectReleases(rows, range) : {releases: [], more: 0, older_unlisted: false};
  return {loading: false, error, ...picked, page_url: RELEASES_PAGE_URL};
}

const LOADING_NOTES = Object.freeze({
  loading: true, error: null, releases: [], more: 0, older_unlisted: false, page_url: RELEASES_PAGE_URL,
});

/**
 * @param {{fetch: (url: string, init?: object) => Promise<Response>, dir: () => string,
 *   logger: {info: Function, warn: Function}, now?: () => number, timeoutMs?: number}} deps
 */
function createNotesSource({fetch, dir, logger, now = () => Date.now(), timeoutMs = RELEASE_NOTES_FETCH_TIMEOUT_MS}) {
  const cachePath = () => {
    const folder = dir();
    return folder ? path.join(folder, RELEASE_NOTES_CACHE_FILE) : '';
  };

  function readCache() {
    const file = cachePath();
    if (!file || !fs.existsSync(file)) return null;
    try {
      const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (parsed?.schema_version !== RELEASE_NOTES_CACHE_SCHEMA_VERSION || !Array.isArray(parsed.rows)) {
        logger.warn(`release notes cache ${file} has schema_version ${parsed?.schema_version}; ignoring it`);
        return null;
      }
      return parsed.rows;
    } catch (err) {
      logger.warn(`release notes cache unreadable: ${errorLine(err)}`);
      return null;
    }
  }

  function writeCache(rows) {
    const file = cachePath();
    if (!file) return;
    const body = {schema_version: RELEASE_NOTES_CACHE_SCHEMA_VERSION, fetched_at: now(), rows};
    try {
      fs.mkdirSync(path.dirname(file), {recursive: true});
      fs.writeFileSync(`${file}.tmp`, JSON.stringify(body));
      fs.renameSync(`${file}.tmp`, file);
    } catch (err) {
      logger.warn(`release notes cache not written: ${errorLine(err)}`);
    }
  }

  async function fetchRows() {
    let last = null;
    for (let attempt = 1; attempt <= RELEASE_NOTES_FETCH_ATTEMPTS; attempt += 1) {
      try {
        const res = await fetch(RELEASES_API_URL, {
          headers: {Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
          signal: AbortSignal.timeout(timeoutMs),
        });
        if (!res.ok) throw new Error(`GitHub answered HTTP ${res.status}`);
        const body = await res.json();
        if (!Array.isArray(body)) throw new Error('GitHub did not answer with a list of releases');
        return body.map(keep);
      } catch (err) {
        last = err;
        logger.warn(`release notes fetch ${attempt}/${RELEASE_NOTES_FETCH_ATTEMPTS} failed: ${errorLine(err)}`);
      }
    }
    throw last;
  }

  /**
   * Notes for the releases in (after, through], newest first.
   * @param {{after?: string | null, through: string, limit?: number}} range
   */
  async function load({after = null, through, limit = RELEASE_NOTES_MAX_LISTED}) {
    const range = {after, through, limit};
    const cached = readCache();
    if (cached && cacheCovers(cached, range)) return notesPayload(cached, range);
    try {
      const rows = await fetchRows();
      writeCache(rows);
      return notesPayload(rows, range);
    } catch (err) {
      return notesPayload(cached, range, `Release notes could not be loaded: ${errorLine(err)}`);
    }
  }

  return {load};
}

module.exports = {
  RELEASE_NOTES_CACHE_FILE, RELEASE_NOTES_CACHE_SCHEMA_VERSION, RELEASE_NOTES_FETCH_ATTEMPTS, LOADING_NOTES,
  cacheCovers, notesPayload, createNotesSource,
};
