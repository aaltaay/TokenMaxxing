const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function renderer() {
  const context = vm.createContext({
    Date, console,
    window: {hud: {on() {}, engineStatus: () => Promise.resolve({ready:false})}},
    document: {},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'renderer','app.js'),'utf8'),context);
  return expression => vm.runInContext(expression,context);
}

const DAY = 86400;

test('a weekly window burnt to 87% after one day is flagged too fast, with the rate that would last', () => {
  const run = renderer();
  const p = run(`pace({used: 87, elapsed: ${DAY}, remaining: ${6 * DAY}})`);
  assert.equal(p.status, 'fast');
  assert.ok(p.multiplier > 6 && p.multiplier < 6.2, `multiplier ${p.multiplier}`);
  // 87% in 24h leaves 13% at 3.625%/h: about 3.6 hours, days before the reset.
  assert.ok(p.runsOutIn > 3.5 * 3600 && p.runsOutIn < 3.7 * 3600, `runs out in ${p.runsOutIn}`);
  assert.ok(p.early > 5 * DAY, `early ${p.early}`);
  // 13% over six days is ~2.17% per day.
  assert.equal(run(`rateText(${p.allowedPerHour}, ${7 * DAY})`), '2.2% / day');
  const sentence = run(`paceSentence(pace({used: 87, elapsed: ${DAY}, remaining: ${6 * DAY}}))`);
  assert.match(sentence, /6\.1× the even pace/);
  assert.match(sentence, /runs out in 3h 3\dm/);
  assert.match(sentence, /Keep under 2\.2% \/ day to last/);
});

test('spending at or under the even rate is on pace, and slightly over is only ahead', () => {
  const run = renderer();
  assert.equal(run(`pace({used: 40, elapsed: ${3 * DAY}, remaining: ${4 * DAY}}).status`), 'on-pace');
  assert.equal(run(`pace({used: 50, elapsed: ${3 * DAY}, remaining: ${4 * DAY}}).status`), 'ahead');
  assert.equal(run(`pace({used: 0, elapsed: ${3 * DAY}, remaining: ${4 * DAY}}).status`), 'on-pace');
  assert.equal(run(`pace({used: 0, elapsed: ${3 * DAY}, remaining: ${4 * DAY}}).runsOutIn`), null);
});

test('the first minutes of a window do not judge pace unless usage is already high', () => {
  const run = renderer();
  assert.equal(run(`pace({used: 5, elapsed: 60, remaining: ${5 * 3600 - 60}}).status`), 'early');
  assert.equal(run(`pace({used: 40, elapsed: 60, remaining: ${5 * 3600 - 60}}).status`), 'fast');
});

test('a nearly finished window with most of the quota unused is reported as spare', () => {
  const run = renderer();
  const p = run(`pace({used: 20, elapsed: ${6 * DAY}, remaining: ${DAY}})`);
  assert.equal(p.status, 'spare');
  assert.match(run(`paceSentence(pace({used: 20, elapsed: ${6 * DAY}, remaining: ${DAY}}))`), /80% is still unused/);
});

test('an exhausted window and missing inputs never produce a pace', () => {
  const run = renderer();
  assert.equal(run(`pace({used: 100, elapsed: ${DAY}, remaining: ${6 * DAY}}).status`), 'exhausted');
  assert.equal(run(`pace({used: null, elapsed: ${DAY}, remaining: ${6 * DAY}})`), null);
  assert.equal(run(`pace({used: 50, elapsed: ${DAY}, remaining: -5})`), null);
  assert.equal(run(`windowPace({used_percent: 50, resets_at: null, window_minutes: 300})`), null);
  assert.equal(run(`windowPace({used_percent: 50, resets_at: 1000, window_minutes: 300}, 2000)`), null);
});

test('provider windows and the Cursor cycle feed the same pace model', () => {
  const run = renderer();
  const now = 1_800_000_000;
  const p = run(`windowPace({used_percent: 87, resets_at: ${now + 6 * DAY}, window_minutes: 10080}, ${now})`);
  assert.equal(p.status, 'fast');
  assert.equal(Math.round(p.elapsed), DAY);
  const start = new Date((now - 10 * DAY) * 1000).toISOString();
  const end = new Date((now + 20 * DAY) * 1000).toISOString();
  const c = run(`cursorPace({cycle_start: ${JSON.stringify(start)}, cycle_end: ${JSON.stringify(end)}}, 30, ${now})`);
  assert.equal(c.status, 'on-pace');
  assert.equal(Math.round(c.expected * 10) / 10, 33.3);
  assert.equal(run(`cursorPace({cycle_start: null, cycle_end: ${JSON.stringify(end)}}, 30, ${now})`), null);
});

test('overview alerts list only windows running ahead, worst first, and read the Cursor cycle too', () => {
  const run = renderer();
  const now = Date.now() / 1000;
  run(`
    state.providers = {providers: [
      {id: 'claude', status: 'ok', fetched_at: ${now}, windows: [
        {id: 'five_hour', label: '5-hour', used_percent: 30, resets_at: ${now + 2 * 3600}, window_minutes: 300},
        {id: 'seven_day', label: 'Weekly', used_percent: 87, resets_at: ${now + 6 * DAY}, window_minutes: 10080},
      ]},
      {id: 'codex', status: 'ok', fetched_at: ${now}, windows: [
        {id: 'codex:primary', label: '5-hour', used_percent: 90, resets_at: ${now + 3 * 3600}, window_minutes: 300},
      ]},
    ]};
    state.cycle = {total_pct: 10, cycle_start: ${JSON.stringify(new Date((now - 5 * DAY) * 1000).toISOString())},
                   cycle_end: ${JSON.stringify(new Date((now + 25 * DAY) * 1000).toISOString())}};
  `);
  const alerts = JSON.parse(run('JSON.stringify(paceAlerts().map(a => [a.name, a.pace.status]))'));
  assert.deepEqual(alerts, [['Claude weekly', 'fast'], ['Codex 5-hour', 'fast']]);
});
