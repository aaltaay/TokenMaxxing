const {test, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const n = require('./release-notes');
const {createNotesSource, cacheCovers, RELEASE_NOTES_CACHE_FILE, RELEASE_NOTES_FETCH_ATTEMPTS} = require('./release-notes-source');
const {createWhatsNew, WHATS_NEW_FILE} = require('./whats-new');

const quiet = {info() {}, warn() {}, error() {}};

// v040's real body, as scripts/release_notes.py wrote it.
const V040_BODY = [
  "## What's new in v040", '', '**Release every merge to main and update installed copies in the background**  ', '#14', '',
  'Every change merged into main now ships as a numbered release.', '',
  '<!-- tokenmaxxing-release-notes {"schema_version":1,"tag":"v040","title":"Release every merge to main and update installed copies in the background","kind":null,"scope":null,"pr":14,"summary":"Every change merged into main now ships as a numbered release (v040, v041, \\u2026), and the installed app updates itself:","points":["Background download that survives a dropped connection","Restart to update or Later","Update log"]} -->',
  '', '---', '', 'TOKENMAXXING v040 for Windows.',
].join('\n');

/** A marker the way release_notes.py writes it: compact JSON, `<` and `>` escaped. */
function marker(record) {
  const json = JSON.stringify({schema_version: 1, ...record}).replace(/</g, '\\u003c').replace(/>/g, '\\u003e');
  return `## What's new\n\n<!-- tokenmaxxing-release-notes ${json} -->\n\n---\ninstaller text`;
}

function row(number, record = {title: `Release ${number}`, pr: number}, extra = {}) {
  const tag = `v${String(number).padStart(3, '0')}`;
  return {
    tag_name: tag, body: record ? marker({tag, ...record}) : 'Full changelog', draft: false, prerelease: false,
    html_url: `https://github.com/aaltaay/TokenMaxxing/releases/tag/${tag}`, published_at: '2026-10-13T17:50:19Z', ...extra,
  };
}

const made = [];
const tempDir = () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-notes-'));
  made.push(dir);
  return dir;
};
after(() => { for (const dir of made) fs.rmSync(dir, {recursive: true, force: true}); });

test('the marker scripts/release_notes.py writes reads back, and nothing else is guessed', () => {
  const v040 = n.parseNotesRecord(V040_BODY);
  assert.equal(v040.title, 'Release every merge to main and update installed copies in the background');
  assert.equal(v040.pr, 14);
  assert.equal(v040.kind, null);
  assert.match(v040.summary, /numbered release \(v040, v041, …\)/);
  assert.deepEqual(v040.points, ['Background download that survives a dropped connection', 'Restart to update or Later', 'Update log']);
  // `<` / `>` are escaped in the JSON, so the payload never closes the comment and still reads as text.
  assert.equal(n.parseNotesRecord(marker({title: 'Show <b>bold</b> text', pr: 3})).title, 'Show <b>bold</b> text');
  assert.equal(n.parseNotesRecord(marker({schema_version: 2, title: 'From the future'})), null);
  assert.equal(n.parseNotesRecord('<!-- tokenmaxxing-release-notes {"schema_version":1, oops} -->'), null);
  assert.equal(n.parseNotesRecord('<!-- nova-release-notes {"schema_version":1,"title":"Other app"} -->'), null);
  assert.equal(n.parseNotesRecord('**Full Changelog**: v2.1.0...v2.1.1'), null);
  assert.equal(n.parseNotesRecord(null), null);
  const odd = n.parseNotesRecord(marker({title: 7, pr: -1, points: ['  a  ', '', 3, ...Array(12).fill('p')]}));
  assert.deepEqual([odd.title, odd.pr, odd.points.length, odd.points[0]], ['', null, 8, 'a']);
});

test('only published vNNN releases are listed, and one without notes says so', () => {
  assert.equal(n.releaseEntry(row(41, undefined, {draft: true})), null);
  assert.equal(n.releaseEntry(row(41, undefined, {prerelease: true})), null);
  assert.equal(n.releaseEntry({tag_name: 'v2.1.1', body: ''}), null);
  const bare = n.releaseEntry(row(39, null));
  assert.deepEqual([bare.recorded, bare.title, bare.summary, bare.points, bare.pr_url], [false, '', '', [], null]);
  const entry = n.releaseEntry(row(42, {title: 'Notes', pr: 15}));
  assert.equal(entry.number, 42);
  assert.equal(entry.pr_url, 'https://github.com/aaltaay/TokenMaxxing/pull/15');
  assert.equal(entry.url, 'https://github.com/aaltaay/TokenMaxxing/releases/tag/v042');
  // A link GitHub sent that is not this repo's release page is replaced, never passed on.
  assert.equal(n.releaseEntry(row(42, {}, {html_url: 'https://evil.example/v042'})).url,
    'https://github.com/aaltaay/TokenMaxxing/releases/tag/v042');
});

test('links open only this repo\'s release and PR pages', () => {
  for (const ok of ['https://github.com/aaltaay/TokenMaxxing/releases', 'https://github.com/aaltaay/TokenMaxxing/releases/tag/v042',
    'https://github.com/aaltaay/TokenMaxxing/pull/14']) assert.equal(n.isReleaseLink(ok), true, ok);
  for (const bad of ['http://github.com/aaltaay/TokenMaxxing/pull/14', 'https://github.com/aaltaay/Nova/pull/14',
    'https://github.com/aaltaay/TokenMaxxing/pull/14/files', 'https://github.com/aaltaay/TokenMaxxing/releases/download/v042/x.exe',
    'https://github.com/aaltaay/TokenMaxxing/pull/14?x=https://evil', 'javascript:alert(1)', '', null]) {
    assert.equal(n.isReleaseLink(bad), false, String(bad));
  }
});

test('the releases between two versions are listed newest first, with the rest counted', () => {
  const rows = [row(40), row(43), row(41), row(42), row(44), {tag_name: 'v2.1.1', body: ''}];
  const picked = n.selectReleases(rows, {after: 'v040', through: 'v043'});
  assert.deepEqual(picked.releases.map(r => r.tag), ['v043', 'v042', 'v041']);
  assert.deepEqual([picked.more, picked.older_unlisted], [0, false]);
  const limited = n.selectReleases(rows, {after: 'v040', through: 'v044', limit: 2});
  assert.deepEqual([limited.releases.map(r => r.tag), limited.more], [['v044', 'v043'], 2]);
  assert.deepEqual(n.selectReleases(rows, {through: 'v041'}).releases.map(r => r.tag), ['v041', 'v040']);
  assert.deepEqual(n.selectReleases(rows, {through: '3.0.41'}).releases, []);
  // A full page that stops short of `after` says older releases are on GitHub.
  const page = Array.from({length: n.RELEASES_PER_PAGE}, (_, i) => row(500 - i));
  assert.equal(n.selectReleases(page, {after: 'v100', through: 'v500'}).older_unlisted, true);
  assert.equal(n.selectReleases(page, {after: 'v450', through: 'v500'}).older_unlisted, false);
});

test('an update lists what it brings; What\'s new lists what the last update brought', () => {
  assert.deepEqual(n.updateRange({installed: 'v040', available: 'v043'}), {after: 'v040', through: 'v043'});
  assert.equal(n.updateRange({installed: 'v043', available: 'v043'}), null);
  assert.equal(n.updateRange({installed: 'v2.1.1', available: 'v043'}), null);
  assert.equal(n.updateRange({installed: 'v040', available: ''}), null);

  assert.deepEqual(n.whatsNewRange({installed: 'v043', seen: 'v040'}), {after: 'v040', through: 'v043', since: 'v040'});
  // Never closed: only the installed release's own notes.
  assert.deepEqual(n.whatsNewRange({installed: 'v043', seen: null}), {after: 'v42', through: 'v043', since: null});
  assert.deepEqual(n.selectReleases([row(41), row(42), row(43)], n.whatsNewRange({installed: 'v043', seen: null})).releases.map(r => r.tag), ['v043']);
  assert.equal(n.whatsNewRange({installed: 'v043', seen: 'v043'}), null);
  assert.equal(n.whatsNewRange({installed: 'v043', seen: 'v050'}), null);
  assert.equal(n.whatsNewRange({installed: 'v0.0.0-dev', seen: null}), null);
});

test('the cache answers only for ranges it covers', () => {
  const rows = [row(43), row(42), row(41)];
  assert.equal(cacheCovers(rows, {after: 'v041', through: 'v043'}), true);
  assert.equal(cacheCovers(rows, {after: 'v043', through: 'v044'}), false);
  const page = Array.from({length: 100}, (_, i) => row(300 - i));
  assert.equal(cacheCovers(page, {after: 'v250', through: 'v300'}), true);
  assert.equal(cacheCovers(page, {after: 'v100', through: 'v300'}), false);
  assert.equal(cacheCovers(null, {through: 'v043'}), false);
});

function source({dir = tempDir(), responses}) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({url, init});
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return {ok: next.status === 200, status: next.status, json: async () => next.body};
  };
  return {dir, calls, notes: createNotesSource({fetch, dir: () => dir, logger: quiet, now: () => 1234})};
}

test('notes are fetched once, cached in userData, and read back from the cache', async () => {
  const s = source({responses: [{status: 200, body: [row(42), row(41), {...row(40), assets: ['dropped']}]}]});
  const first = await s.notes.load({after: 'v040', through: 'v042'});
  assert.deepEqual([first.loading, first.error, first.releases.map(r => r.tag)], [false, null, ['v042', 'v041']]);
  assert.equal(first.page_url, 'https://github.com/aaltaay/TokenMaxxing/releases');
  assert.equal(s.calls.length, 1);
  assert.match(s.calls[0].url, /^https:\/\/api\.github\.com\/repos\/aaltaay\/TokenMaxxing\/releases\?per_page=100$/);
  const cache = JSON.parse(fs.readFileSync(path.join(s.dir, RELEASE_NOTES_CACHE_FILE), 'utf8'));
  assert.deepEqual([cache.schema_version, cache.fetched_at, cache.rows.length], [1, 1234, 3]);
  assert.equal(cache.rows[2].assets, undefined);
  const again = await s.notes.load({after: 'v041', through: 'v042'});
  assert.deepEqual(again.releases.map(r => r.tag), ['v042']);
  assert.equal(s.calls.length, 1);
});

test('a cache of an unknown version is ignored, and a range it misses fetches again', async () => {
  const dir = tempDir();
  fs.writeFileSync(path.join(dir, RELEASE_NOTES_CACHE_FILE), JSON.stringify({schema_version: 9, rows: [row(42)]}));
  const s = source({dir, responses: [{status: 200, body: [row(42)]}, {status: 200, body: [row(43), row(42)]}]});
  await s.notes.load({after: 'v041', through: 'v042'});
  assert.equal(s.calls.length, 1);
  const newer = await s.notes.load({after: 'v042', through: 'v043'});
  assert.equal(s.calls.length, 2);
  assert.deepEqual(newer.releases.map(r => r.tag), ['v043']);
});

test('a failed fetch says why and never throws; the cache still answers what it holds', async () => {
  const offline = source({responses: Array(RELEASE_NOTES_FETCH_ATTEMPTS).fill(new Error('net::ERR_INTERNET_DISCONNECTED'))});
  const none = await offline.notes.load({after: 'v041', through: 'v042'});
  assert.deepEqual([none.loading, none.releases], [false, []]);
  assert.match(none.error, /could not be loaded: net::ERR_INTERNET_DISCONNECTED/);
  assert.equal(offline.calls.length, RELEASE_NOTES_FETCH_ATTEMPTS);

  const dir = tempDir();
  fs.writeFileSync(path.join(dir, RELEASE_NOTES_CACHE_FILE), JSON.stringify({schema_version: 1, rows: [row(42)]}));
  const limited = source({dir, responses: [{status: 403, body: {}}, {status: 200, body: {message: 'not a list'}}]});
  const partial = await limited.notes.load({after: 'v041', through: 'v043'});
  assert.match(partial.error, /did not answer with a list/);
  assert.deepEqual(partial.releases.map(r => r.tag), ['v042']);
});

function whatsNew({installedTag = 'v043', seen, notes} = {}) {
  const dir = tempDir();
  if (seen !== undefined) fs.writeFileSync(path.join(dir, WHATS_NEW_FILE), typeof seen === 'string' ? seen : JSON.stringify(seen));
  const loads = [];
  const views = [];
  const notesSource = {load: async range => {
    loads.push(range);
    return notes ? notes(range) : {loading: false, error: null, releases: [{tag: range.through}], more: 0, older_unlisted: false};
  }};
  const card = createWhatsNew({notesSource, dir: () => dir, installedTag, logger: quiet,
    publish: view => views.push(view), now: () => new Date('2026-10-13T12:00:00Z')});
  return {card, dir, loads, views, seenFile: () => JSON.parse(fs.readFileSync(path.join(dir, WHATS_NEW_FILE), 'utf8'))};
}

test('the first launch of a new version shows every release since the one last read, until Got it', async () => {
  const w = whatsNew({seen: {schema_version: 1, seen_tag: 'v040'}});
  await w.card.start();
  assert.deepEqual(w.loads, [{after: 'v040', through: 'v043', since: 'v040'}]);
  assert.equal(w.views[0].card.notes.loading, true);
  const {card} = w.card.view;
  assert.deepEqual([card.mode, card.tag, card.since, card.notes.loading], ['updated', 'v043', 'v040', false]);
  assert.equal(w.card.view.schema_version, 1);
  assert.equal(w.card.view.available, true);
  assert.equal(w.card.close().card, null);
  assert.deepEqual(w.seenFile(), {schema_version: 1, seen_tag: 'v043', closed_at: '2026-10-13T12:00:00.000Z'});
  // Read: the next launch of the same version shows nothing.
  const next = whatsNew({seen: w.seenFile()});
  await next.card.start();
  assert.equal(next.card.view.card, null);
  assert.deepEqual(next.loads, []);
});

test('no seen file shows the installed release\'s own notes; a file of another version shows nothing', async () => {
  const fresh = whatsNew();
  await fresh.card.start();
  assert.deepEqual(fresh.loads, [{after: 'v42', through: 'v043', since: null}]);
  const corrupt = whatsNew({seen: '{not json'});
  await corrupt.card.start();
  assert.equal(corrupt.card.view.card.mode, 'updated');
  const future = whatsNew({seen: {schema_version: 2, seen_tag: 'v040'}});
  await future.card.start();
  assert.equal(future.card.view.card, null);
  future.card.openRecent();
  future.card.close();
  // Left for the version that wrote it.
  assert.deepEqual(future.seenFile(), {schema_version: 2, seen_tag: 'v040'});
});

test('a source checkout has no What\'s new and writes nothing', async () => {
  const dev = whatsNew({installedTag: 'v2.1.1'});
  await dev.card.start();
  assert.deepEqual([dev.card.view.available, dev.card.view.card, dev.loads.length], [false, null, 0]);
  assert.equal(dev.card.openRecent().card, null);
  dev.card.close();
  assert.equal(fs.existsSync(path.join(dev.dir, WHATS_NEW_FILE)), false);
});

test('What\'s new reopens from the panel with recent releases, and a close while loading stays closed', async () => {
  let release;
  const w = whatsNew({seen: {schema_version: 1, seen_tag: 'v043'},
    notes: () => new Promise(resolve => { release = resolve; })});
  const opened = w.card.openRecent();
  assert.deepEqual([opened.card.mode, opened.card.since, opened.card.notes.loading], ['recent', null, true]);
  assert.deepEqual(w.loads, [{after: null, through: 'v043', limit: n.RECENT_RELEASES_LISTED, since: null}]);
  w.card.close();
  release({loading: false, error: null, releases: [], more: 0, older_unlisted: false});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(w.card.view.card, null);
  assert.equal(w.views.at(-1).card, null);
});
