'use strict';

/**
 * Release notes the app shows, ported from Nova's releaseNotes.mjs. Pure: no
 * Electron, no I/O.
 *
 * Every vNNN GitHub Release ends with one hidden line scripts/release_notes.py
 * writes from the PR that made it:
 *
 *   <!-- tokenmaxxing-release-notes {"schema_version":1,"tag":"v042","title":...} -->
 *
 * This reads it out of GitHub's Releases API rows and picks the releases between
 * two versions: what a downloaded update brings (the bell panel's ready row) and
 * what the last update brought (the What's new card). A release without the line,
 * or with a schema_version this version does not read, is listed with
 * `recorded: false`, never an invented summary.
 */

const RELEASES_REPO = 'aaltaay/TokenMaxxing';
/** One page is enough: a copy more than 100 releases behind is told the rest are on GitHub. */
const RELEASES_PER_PAGE = 100;
const RELEASES_API_URL = `https://api.github.com/repos/${RELEASES_REPO}/releases?per_page=${RELEASES_PER_PAGE}`;
const RELEASES_PAGE_URL = `https://github.com/${RELEASES_REPO}/releases`;
const RELEASE_NOTES_SCHEMA_VERSION = 1;
/** The ready row and What's new list at most this many releases; the rest are counted. */
const RELEASE_NOTES_MAX_LISTED = 30;
/** What's new reopened from the bell: the installed release and the ones just before it. */
const RECENT_RELEASES_LISTED = 10;
const TEXT_MAX = 600;
const POINTS_MAX = 8;

const MARKER_RE = /<!--\s*tokenmaxxing-release-notes\s+(\{[\s\S]*?\})\s*-->/;
const TAG_RE = /^v(\d+)$/;
const RELEASE_LINK_RE = new RegExp(
  `^https://github\\.com/${RELEASES_REPO.replace('/', '\\/')}/(?:releases(?:/tag/v\\d+)?|pull/\\d+)$`,
);

/** `v042` -> 42; anything else (`v2.1.1`, `3.0.42`) -> null. */
function tagNumber(tag) {
  const match = TAG_RE.exec(String(tag ?? '').trim());
  return match ? Number(match[1]) : null;
}

/** Only this repo's release and PR pages open from the notes. */
function isReleaseLink(url) {
  return RELEASE_LINK_RE.test(String(url ?? ''));
}

function text(value, max = TEXT_MAX) {
  if (typeof value !== 'string') return '';
  const clean = value.replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean;
}

/** The record in a release body, or null when it has none or it is not one this version reads. */
function parseNotesRecord(body) {
  const match = MARKER_RE.exec(String(body ?? ''));
  if (!match) return null;
  let raw;
  try {
    raw = JSON.parse(match[1]);
  } catch {
    return null;
  }
  if (!raw || raw.schema_version !== RELEASE_NOTES_SCHEMA_VERSION) return null;
  const pr = Number(raw.pr);
  return {
    title: text(raw.title, 200),
    kind: text(raw.kind, 20) || null,
    scope: text(raw.scope, 40) || null,
    pr: Number.isInteger(pr) && pr > 0 ? pr : null,
    summary: text(raw.summary),
    points: Array.isArray(raw.points) ? raw.points.map(p => text(p, 200)).filter(Boolean).slice(0, POINTS_MAX) : [],
  };
}

/** One GitHub Releases API row as the app lists it, or null for anything that is not a published vNNN release. */
function releaseEntry(row) {
  if (!row || row.draft || row.prerelease) return null;
  const tag = String(row.tag_name ?? '');
  const number = tagNumber(tag);
  if (number == null) return null;
  const record = parseNotesRecord(row.body);
  return {
    tag,
    number,
    recorded: Boolean(record),
    title: record?.title || '',
    kind: record?.kind ?? null,
    scope: record?.scope ?? null,
    pr: record?.pr ?? null,
    pr_url: record?.pr ? `https://github.com/${RELEASES_REPO}/pull/${record.pr}` : null,
    summary: record?.summary || '',
    points: record?.points ?? [],
    published_at: typeof row.published_at === 'string' ? row.published_at : null,
    url: isReleaseLink(row.html_url) ? row.html_url : `${RELEASES_PAGE_URL}/tag/${tag}`,
  };
}

/**
 * Releases after `after` up to and including `through` (tags), newest first.
 * `after` null lists everything up to `through`. `more` counts the matches past
 * `limit`; `older_unlisted` says the page ended before reaching `after`, so
 * GitHub holds releases this list could not show.
 */
function selectReleases(rows, {after = null, through, limit = RELEASE_NOTES_MAX_LISTED} = {}) {
  const top = tagNumber(through);
  const floor = tagNumber(after);
  const entries = (Array.isArray(rows) ? rows : []).map(releaseEntry).filter(Boolean);
  const matching = entries
    .filter(e => top != null && e.number <= top && (floor == null || e.number > floor))
    .sort((a, b) => b.number - a.number);
  const oldest = entries.reduce((min, e) => Math.min(min, e.number), Infinity);
  const pageFull = Array.isArray(rows) && rows.length >= RELEASES_PER_PAGE;
  return {
    releases: matching.slice(0, limit),
    more: Math.max(0, matching.length - limit),
    older_unlisted: floor != null && pageFull && oldest > floor + 1,
  };
}

/** What a downloaded update brings: every release after the installed one, or null when it is not newer. */
function updateRange({installed, available}) {
  const from = tagNumber(installed);
  const to = tagNumber(available);
  if (from == null || to == null || to <= from) return null;
  return {after: installed, through: available};
}

/**
 * Which releases the What's new card covers on this launch, or null for none.
 * `seen`: the release whose notes the user last closed (null: never, e.g. the
 * first launch with What's new), so only the installed release is shown.
 */
function whatsNewRange({installed, seen}) {
  const now = tagNumber(installed);
  if (now == null) return null;
  const before = tagNumber(seen);
  if (before == null) return {after: `v${now - 1}`, through: installed, since: null};
  if (before >= now) return null;
  return {after: seen, through: installed, since: seen};
}

module.exports = {
  RELEASES_REPO, RELEASES_PER_PAGE, RELEASES_API_URL, RELEASES_PAGE_URL, RELEASE_NOTES_SCHEMA_VERSION,
  RELEASE_NOTES_MAX_LISTED, RECENT_RELEASES_LISTED,
  tagNumber, isReleaseLink, parseNotesRecord, releaseEntry, selectReleases, updateRange, whatsNewRange,
};
