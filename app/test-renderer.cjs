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

test('changing follow mode while a read is pending discards the old selection', async () => {
  const run = renderer();
  run(`
    renderChat = () => {};
    const sessionCalls = [], sessionReplies = [];
    window.hud.call = (cmd,args) => {
      sessionCalls.push(args);
      return new Promise(resolve => sessionReplies.push(resolve));
    };
    state.sessionProvider='claude';
    state.sessionFollow='latest';
    const pendingSession = refreshLocal();
    state.sessionFollow='active';
    state.local=null;
  `);
  await run('refreshLocal()');
  run(`sessionReplies.shift()({chat:{id:'background',used:999}})`);
  await run('pendingSession');
  assert.equal(run('state.local'),null);
  assert.equal(run('sessionCalls[1].follow'),'active');
  run(`sessionReplies.shift()({chat:{id:'open',used:123}})`);
  await run('Promise.resolve()');
  assert.equal(run('state.local.chat.id'),'open');
});

test('auto mode follows whichever of Codex or Claude actually has an open chat', async () => {
  const run = renderer();
  run(`
    renderChat = () => {};
    state.activeChatSupported = true;
    window.hud.call = async (cmd, args) => args.provider === 'claude'
      ? {chat: {id: 'claude-one', used: 10}}
      : {chat: null, reason: 'Open Codex chat could not be detected.'};
  `);
  await run('refreshLocal()');
  assert.equal(run('state.autoDetected'), 'claude');
  assert.equal(run('state.autoActive'), true);
  assert.equal(run('state.local.chat.id'), 'claude-one');
  assert.equal(run('effectiveProvider()'), 'claude');
});

test('auto mode holds the last detected chat instead of blanking when nothing is open', async () => {
  const run = renderer();
  run(`
    renderChat = () => {};
    state.activeChatSupported = false;
    state.autoDetected = 'claude';
    state.local = {chat: {id: 'claude-one', used: 10}};
    window.hud.call = async () => ({chat: null, reason: 'not open'});
  `);
  await run('refreshLocal()');
  assert.equal(run('state.autoActive'), false);
  // Nothing currently open, but the previous chat stays on screen rather
  // than being replaced by an empty state.
  assert.equal(run('state.local.chat.id'), 'claude-one');
});

test('auto mode does not call Codex detection on platforms without the accessibility helper', async () => {
  const run = renderer();
  run(`
    renderChat = () => {};
    state.activeChatSupported = false;
    const calls = [];
    window.hud.call = async (cmd, args) => { calls.push(args.provider); return {chat: null}; };
  `);
  await run('refreshLocal()');
  assert.equal(run('calls.length'), 1);
  assert.equal(run('calls[0]'), 'claude');
});

test('picking a chat while in Auto locks the provider so the pin is unambiguous', () => {
  const run = renderer();
  run(`state.sessionProvider = 'auto'; state.autoDetected = 'claude';`);
  run(`if (state.sessionProvider === 'auto') state.sessionProvider = effectiveProvider();`);
  assert.equal(run('state.sessionProvider'), 'claude');
});

test('missing metrics remain unavailable while actual zero remains zero', () => {
  const run = renderer();
  assert.equal(run('pct(null)'), '—');
  assert.equal(run('money(undefined)'), '—');
  assert.equal(run('compact(null)'), '—');
  assert.equal(run('pct(0)'), '0%');
  assert.equal(run('money(0)'), '$0.00');
  assert.equal(run('compact(0)'), '0');
});

test('session cost notes distinguish estimates, partial history, reported totals and unavailable', () => {
  const run = renderer();
  const notes = (cost) => JSON.parse(run(`JSON.stringify(costNotes(${JSON.stringify(cost)}))`));
  assert.match(notes({cents:33, priced_requests:2, partial:true, basis:'API value', pricing_date:'2026-09-20'})[0], /Partial estimate · 2 priced/);
  assert.match(notes({cents:0, priced_requests:1})[0], /1 recorded requests/);
  assert.match(notes({cents:null, priced_requests:0})[0], /Cost unavailable/);
  assert.match(notes(null)[0], /Cost unavailable/);
  assert.deepEqual(notes({cents:125, reported:true, basis:'Reported by Claude Code.', series:[[1, 2, 0, 3]]}),
    ['Reported by Claude Code.', 'Per-request amounts are estimates at API rates from the session log.']);
});

test('the week is counted in 5-hour sessions from the measured share', () => {
  const run = renderer();
  const figures = (pair, session, week) => JSON.parse(run(`JSON.stringify(budgetFigures(${JSON.stringify(pair)}, ${JSON.stringify(session)}, ${JSON.stringify(week)}))`));
  const later = Date.now() / 1000 + 3600;
  // 11% of the week per full session, 81% of the week gone, session at 9%.
  const roomy = figures({share: 0.11}, {used_percent: 9, resets_at: later}, {used_percent: 81});
  assert.ok(Math.abs(roomy.perWeek - 9.09) < 0.01);
  assert.ok(Math.abs(roomy.sessionsLeft - 19 / 11) < 1e-9);
  assert.ok(Math.abs(roomy.room - 10.01) < 1e-9);
  assert.equal(roomy.stopsAt, null);
  // 5% of the week left cannot carry the other 91% of this session.
  const tight = figures({share: 0.11}, {used_percent: 9, resets_at: later}, {used_percent: 95});
  assert.ok(Math.abs(tight.stopsAt - (9 + 5 / 0.11)) < 1e-9);
  // No current 5-hour reading: nothing is said about this session.
  const idle = figures({share: 0.11}, {used_percent: 40, resets_at: Date.now() / 1000 - 1}, {used_percent: 50});
  assert.equal(idle.room, null);
  assert.equal(idle.stopsAt, null);
});

test('a session climbing in cost says so, and counts only real rebuilds', () => {
  const run = renderer();
  const series = Array.from({length: 30}, (_, i) => [1000 + i * 60, 2 + i, i === 20 ? 1 : 0, 10000 * (i + 1)]);
  const stats = JSON.parse(run(`JSON.stringify(sessionStats({series: ${JSON.stringify(series)}, series_start: 0, last_request_cents: 31}))`));
  assert.equal(stats.early, 6.5);
  assert.equal(stats.climbing, true);
  assert.equal(stats.rebuilds, 1);
  assert.equal(stats.rebuildCents, 22);
  assert.equal(stats.firstContext, 10000);
  // A truncated series has no honest "start of the session" to compare with.
  const tail = JSON.parse(run(`JSON.stringify(sessionStats({series: ${JSON.stringify(series)}, series_start: 500, last_request_cents: 31}))`));
  assert.equal(tail.early, null);
  assert.equal(tail.firstContext, null);
});

test('the priciest requests are explained by what dominated their cost', () => {
  const run = renderer();
  const explain = (d, stats = {firstContext: 20000}) => JSON.parse(run(`JSON.stringify(explainRequest(${JSON.stringify(d)}, ${JSON.stringify(stats)}))`));
  const expired = explain({index: 40, cents: 520, gap: 5400, model: 'claude-opus-5', previous_model: 'claude-opus-5', context: 520000, rebuild: true,
    parts: [['Cache write (1-hour)', 518000, 518], ['Output', 800, 2]]});
  assert.equal(expired.tag, 'Cache expired · 1h 30m idle');
  assert.match(expired.why[0], /longer than the 1-hour cache lasts/);
  const switched = explain({index: 9, cents: 300, gap: 30, model: 'claude-sonnet-5', previous_model: 'claude-opus-5', context: 300000, rebuild: true,
    parts: [['Cache write (5-minute)', 299000, 299], ['Output', 100, 1]]});
  assert.equal(switched.tag, 'Model switch');
  const output = explain({index: 6, cents: 67, gap: 90, model: 'claude-opus-5', previous_model: 'claude-opus-5', context: 110000,
    parts: [['Cache read', 101954, 5.1], ['Cache write (1-hour)', 8067, 8.07], ['Output', 21681, 54.2]]});
  assert.equal(output.tag, 'Long output · 21.7k');
  assert.match(output.why[0], /\$25\/M/);
  const context = explain({index: 200, cents: 30, gap: 20, model: 'm', previous_model: 'm', context: 531000,
    parts: [['Cache read', 528900, 26.4], ['Output', 451, 1.1]]});
  assert.match(context.why[0], /27× the size at the start/);
  const long = explain({index: 3, cents: 247.5, context: 300000, long_context: true,
    parts: [['Uncached input', 100000, 200], ['Cached input', 200000, 40], ['Output', 1000, 7.5]]});
  assert.match(long.why[1], /long-context rate/);
});

test('provider rings cannot be generated from reset countdowns', () => {
  const run = renderer();
  run(`state.resets={rows:[{provider:'codex',kind:'session',elapsed_pct:70,countdown:'4h'}]}`);
  assert.equal(run(`providerBands().find(p=>p.id==='codex').display`),'Unavailable');
  assert.equal(run(`providerBands().find(p=>p.id==='codex').value`),null);
});

test('Codex weekly quota remains a consumption percentage', () => {
  const run = renderer();
  run(`state.providers={providers:[{id:'codex',status:'ok',source:'test',fetched_at:Date.now()/1000,windows:[{id:'primary',label:'Weekly',used_percent:3,window_minutes:10080,resets_at:Date.now()/1000+86400}]}]}`);
  assert.equal(run(`providerBands().find(p=>p.id==='codex').display`),'3% used');
  assert.equal(run(`providerBands().find(p=>p.id==='codex').value`),3);
  assert.match(run(`providerBands().find(p=>p.id==='codex').hint`),/^Weekly/);
});

test('expired or stale provider readings have no consumption arc', () => {
  const run = renderer();
  run(`state.providers={providers:[{id:'codex',status:'ok',fetched_at:Date.now()/1000-HOLD_SECONDS-1,windows:[{used_percent:5,window_minutes:10080}]}]}`);
  assert.equal(run(`providerBands().find(p=>p.id==='codex').value`),null);
  run(`state.providers.providers[0].fetched_at=Date.now()/1000; state.providers.providers[0].windows[0].resets_at=Date.now()/1000-1`);
  assert.equal(run(`providerBands().find(p=>p.id==='codex').value`),null);
});

test('a held reading keeps its arc and says it is being held', () => {
  const run = renderer();
  run(`state.providers={providers:[{id:'claude',status:'ok',source:'test',warning:'Claude limited quota requests. Retrying in 5 min.',fetched_at:Date.now()/1000-240,windows:[{id:'five_hour',label:'5-hour',used_percent:64,window_minutes:300,resets_at:Date.now()/1000+3600}]}]}`);
  assert.equal(run(`providerBands().find(p=>p.id==='claude').display`),'64% used');
  assert.match(run(`providerBands().find(p=>p.id==='claude').hint`),/holding last reading/);
  assert.match(run(`providerBands().find(p=>p.id==='claude').tip`),/Retrying in 5 min/);
});

test('a failed refresh keeps the previous snapshot on screen', async () => {
  const run = renderer();
  run(`state.providers={providers:[{id:'claude',status:'ok',source:'test',fetched_at:Date.now()/1000,windows:[{id:'five_hour',label:'5-hour',used_percent:64,window_minutes:300,resets_at:Date.now()/1000+3600}]}]}`);
  run(`renderResets = () => {}; paintStatus = () => {};
    window.hud.call=async()=>{throw new Error('engine busy')}`);
  await run(`refreshProviders(true)`);
  assert.equal(run(`state.providers.providers[0].windows[0].used_percent`),64);
  assert.match(run(`state.providersError`),/Provider refresh failed/);
});

test('an expired session preserves each provider’s current weekly quota', () => {
  for (const id of ['claude', 'codex']) {
    const run = renderer();
    run(`state.providers={providers:[{id:'${id}',status:'ok',source:'test',fetched_at:Date.now()/1000,windows:[
      {id:'${id}:primary',label:'5-hour',used_percent:95,window_minutes:300,resets_at:Date.now()/1000-1},
      {id:'${id}:secondary',label:'Weekly',used_percent:12,window_minutes:10080,resets_at:Date.now()/1000+86400}
    ]}]}`);
    assert.equal(run(`providerBands().find(p=>p.id==='${id}').display`), '12% used');
    assert.equal(run(`providerBands().find(p=>p.id==='${id}').value`), 12);
    assert.match(run(`providerBands().find(p=>p.id==='${id}').hint`), /^Weekly/);
    assert.doesNotMatch(run(`providerBands().find(p=>p.id==='${id}').tip`), /95%/);
  }
});

test('an explicitly expired window is excluded even before its timestamp passes', () => {
  const run = renderer();
  run(`state.providers={providers:[{id:'codex',status:'ok',source:'test',fetched_at:Date.now()/1000,windows:[
    {id:'codex:primary',label:'Weekly',status:'expired',used_percent:95,window_minutes:10080,resets_at:Date.now()/1000+86400},
    {id:'codex:secondary',label:'5-hour',used_percent:8,window_minutes:300,resets_at:Date.now()/1000+1000}
  ]}]}`);
  assert.equal(run(`providerBands().find(p=>p.id==='codex').value`), 8);
});

test('core Codex quota wins over an earlier weekly model-specific bucket', () => {
  const run = renderer();
  run(`state.providers={providers:[{id:'codex',status:'ok',source:'test',fetched_at:Date.now()/1000,windows:[
    {id:'codex_other:secondary',label:'Other model · Weekly',used_percent:88,window_minutes:10080,resets_at:Date.now()/1000+86400},
    {id:'codex:primary',label:'5-hour',used_percent:7,window_minutes:300,resets_at:Date.now()/1000+1000}
  ]}]}`);
  assert.equal(run(`providerBands().find(p=>p.id==='codex').value`), 7);
  assert.match(run(`providerBands().find(p=>p.id==='codex').hint`), /^5-hour/);
  run(`state.providers.providers[0].windows.push({id:'codex:secondary',label:'Weekly',used_percent:3,window_minutes:10080,resets_at:Date.now()/1000+86400})`);
  assert.equal(run(`providerBands().find(p=>p.id==='codex').value`), 3);
  assert.match(run(`providerBands().find(p=>p.id==='codex').hint`), /^Weekly/);
});

test('a forced refresh during polling is queued and replaces the old account snapshot', async () => {
  const run = renderer();
  run(`
    renderResets = () => {};
    paintStatus = () => {};
    const refreshCalls = [];
    const completions = [];
    window.hud.call = (command, args) => {
      refreshCalls.push({command, force:args.force});
      return new Promise(resolve => completions.push(resolve));
    };
    const initialPoll = refreshProviders(false);
  `);
  await run('refreshProviders(true)');
  assert.equal(run('refreshCalls.length'), 1);
  run('completions.shift()({generation:1,providers:[]})');
  await run('initialPoll');
  assert.equal(run('JSON.stringify(refreshCalls)'), JSON.stringify([
    {command:'providers',force:false}, {command:'providers',force:true},
  ]));
  run('completions.shift()({generation:2,providers:[]})');
  await run('Promise.resolve()');
  assert.equal(run('state.providers.generation'), 2);
  assert.equal(run('state.providersFetching'), false);
  assert.equal(run('state.providersRefreshPending'), false);
});

test('expired Claude login is explicitly unavailable', () => {
  const run = renderer();
  run(`state.providers={providers:[{id:'claude',status:'unavailable',fetched_at:null,reason:'Sign-in expired',windows:[]}]}`);
  assert.equal(run(`providerBands().find(p=>p.id==='claude').display`),'Unavailable');
  assert.equal(run(`providerBands().find(p=>p.id==='claude').hint`),'Sign-in expired');
});

test('a slow first overview read says how far it has got, and a failed one says why', async () => {
  const run = renderer();
  run(`
    el = (tag, attrs = {}, children = []) => ({text: [attrs.text, ...[].concat(children).filter(Boolean).map(c => c.text)].filter(Boolean).join(' ')});
    let overviewRenders = 0;
    renderOverview = () => { overviewRenders += 1; };
    window.hud.call = async (cmd) => cmd === 'overview_progress' ? {read: 1.5e9, total: 6e9} : null;
    state.overviewLoading.month = true;
  `);
  assert.equal(run(`overviewReading().text`), 'Reading your agent logs…');
  await run(`watchOverviewRead()`);
  assert.equal(run(`overviewRenders`), 1);
  assert.equal(run(`overviewReading().text`),
    'Reading your agent logs… 25% of 6.0 GB. The first read takes a minute or two; after that, seconds.');
  // An earlier attempt's error waits while a new read is under way.
  run(`state.overviewError = 'overview timed out'`);
  assert.match(run(`overviewReading().text`), /^Reading your agent logs… 25%/);
  run(`state.overviewLoading.month = false`);
  assert.equal(run(`overviewReading().text`), 'overview timed out');
});

// ── the Overview, drawn into a small fake DOM ────────────────────────────

class FakeText {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}

/** Just enough of an element for the renderer: attributes, classes, style
    properties, children, listeners (not bubbling), and simple selectors. */
class FakeElement {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = new Map();
    this.listeners = {};
    this.hidden = false;
    const props = {};
    this.style = {props, setProperty: (k, v) => { props[k] = String(v); }, getPropertyValue: (k) => props[k] ?? ''};
    const classes = () => (this.attrs.get('class') || '').split(/\s+/).filter(Boolean);
    const set = (list) => this.attrs.set('class', list.join(' '));
    this.classList = {
      add: (...c) => set([...new Set([...classes(), ...c])]),
      remove: (...c) => set(classes().filter(x => !c.includes(x))),
      contains: (c) => classes().includes(c),
      toggle: (c, on) => {
        const want = on === undefined ? !classes().includes(c) : Boolean(on);
        if (want) this.classList.add(c); else this.classList.remove(c);
        return want;
      },
    };
  }
  get className() { return this.attrs.get('class') || ''; }
  set className(v) { this.attrs.set('class', String(v)); }
  get id() { return this.attrs.get('id') || ''; }
  set id(v) { this.attrs.set('id', String(v)); }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  hasAttribute(k) { return this.attrs.has(k); }
  removeAttribute(k) { this.attrs.delete(k); }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get textContent() { return this.childNodes.map(n => n.textContent).join(''); }
  set textContent(v) { this.replaceChildren(); if (String(v)) this.append(String(v)); }
  set innerHTML(_) { throw new Error('innerHTML is never used'); }
  adopt(raw) {
    const node = typeof raw === 'object' ? raw : new FakeText(raw);
    if (node.parentNode) node.parentNode.childNodes = node.parentNode.childNodes.filter(c => c !== node);
    node.parentNode = this;
    return node;
  }
  append(...nodes) { for (const raw of nodes) this.childNodes.push(this.adopt(raw)); }
  prepend(...nodes) { this.childNodes.unshift(...nodes.map(raw => this.adopt(raw))); }
  replaceChildren(...nodes) { for (const c of this.childNodes) c.parentNode = null; this.childNodes = []; this.append(...nodes); }
  replaceWith(raw) {
    const parent = this.parentNode;
    if (!parent) return;
    const i = parent.childNodes.indexOf(this);
    const node = parent.adopt(raw);
    parent.childNodes.splice(parent.childNodes.indexOf(this) >= 0 ? parent.childNodes.indexOf(this) : i, 1, node);
    this.parentNode = null;
  }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === this.ownerDocument.body; }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  matches(selector) {
    return selector.split(',').some((part) => {
      const m = part.trim().match(/^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i);
      if (!m) throw new Error(`selector not supported here: ${part}`);
      const [, tag, cls, attrs] = m;
      if (tag && tag.toUpperCase() !== this.tagName) return false;
      if (cls && !cls.split('.').filter(Boolean).every(c => this.classList.contains(c))) return false;
      for (const [, name, value] of attrs.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
        if (!this.hasAttribute(name) || (value !== undefined && this.getAttribute(name) !== value)) return false;
      }
      return true;
    });
  }
  closest(selector) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (n.matches(selector)) return n; return null; }
  querySelectorAll(selector) {
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c.matches(selector)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) { for (const fn of this.listeners[type] || []) fn({target: this, preventDefault() {}, ...event}); }
  getBoundingClientRect() { return {left: 10, top: 10, right: 110, bottom: 30, width: 100, height: 20}; }
  focus() { this.ownerDocument.activeElement = this; }
}

/** app.js in a context with a fake DOM, controllable timers and a stubbed window.hud. */
function rendererWithDom(hud = {}) {
  const doc = {activeElement: null, title: 'TOKENMAXXING', listeners: {}};
  doc.createElement = (tag) => new FakeElement(doc, tag);
  doc.createElementNS = (_ns, tag) => new FakeElement(doc, tag);
  doc.body = new FakeElement(doc, 'body');
  for (const id of ['overviewBody', 'tooltip', 'brandVersion', 'brandSub']) {
    const node = doc.createElement('div');
    node.id = id;
    doc.body.append(node);
  }
  doc.getElementById = (id) => doc.body.querySelector(`[id="${id}"]`);
  doc.addEventListener = (type, fn) => { (doc.listeners[type] ||= []).push(fn); };
  const timers = [];
  const context = vm.createContext({
    Date, console,
    setTimeout: (fn, ms) => timers.push({fn, ms}),
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    setInterval: () => 0,
    clearInterval: () => {},
    localStorage: {store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = String(v); }},
    window: {innerWidth: 760, innerHeight: 900, addEventListener() {},
      hud: {on() {}, engineStatus: () => Promise.resolve({ready: false}), ...hud}},
    document: doc,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'renderer', 'app.js'), 'utf8'), context);
  const run = (expression) => vm.runInContext(expression, context);
  run.doc = doc;
  run.timers = timers;
  run.flush = () => { for (const t of timers.splice(0)) t.fn?.(); };
  run.body = () => doc.getElementById('overviewBody');
  run.explained = () => run.body().querySelectorAll('[data-explain]');
  run.find = (selector) => run.body().querySelectorAll(selector);
  return run;
}

const pad = (n) => String(n).padStart(2, '0');
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const startOf = (date) => new Date(`${date}T00:00`).getTime() / 1000;

/** An engine reply for the last `days` days, today last. Every new key can be left out. */
function payload({days = 30, focus = true, backfill = undefined, cursor = true, db = true} = {}) {
  const dates = [];
  for (let back = days - 1; back >= 0; back--) {
    const d = new Date();
    d.setHours(12, 0, 0, 0);
    d.setDate(d.getDate() - back);
    dates.push(isoDay(d));
  }
  const since = startOf(dates[0]);
  const tracked = startOf(dates[days - 3]) + 15 * 3600;
  return {
    since,
    providers: {
      claude: {cents: 1234567, tokens: {input: 1e6, output: 2e6, cache_read: 3e8, cache_write: 1e7}, agent_hours: 40, sessions: 12, subagents: 30, unpriced_requests: 3},
      codex: {cents: 23456, tokens: {input: 5e5, output: 4e5, cache_read: 2e7, cache_write: 0}, agent_hours: 10, sessions: 4, subagents: 1, unpriced_requests: 0},
    },
    cursor_agent_hours: 5, wall_hours: 30, peak: {agents: 6, at: since + 3 * 86400}, prompts: 400, sittings: 25,
    first_seen: since + 3600, human_hours: 60, think_hours: 8, longest_sitting: {start: since + 7200, hours: 6.5},
    desk_days: dates.map((date, i) => ({date, hours: 2 + (i % 3)})),
    tool_calls: 9000, tools: [['Shell', 4000], ['Read', 2500], ['Edit', 1500], ['Other', 1000]],
    heat: Array.from({length: 7}, (_, d) => Array.from({length: 24}, (_, h) => (d + h) % 5 ? 0 : 30)),
    hours: Array.from({length: 24}, (_, hour) => ({hour, cursor: 0, claude: 0, codex: 0, you: 0})),
    days: dates.map((date) => ({date, cursor: 0.2, claude: 1.5, codex: 0.4, cents: {cursor: 100, claude: 4000, codex: 700}})),
    plan_prices: {claude: 200, codex: 20},
    ...(cursor ? {cursor: {cents: 50000, tokens: {input: 1e5, output: 1e5, cache_read: 1e6, cache_write: 1e5}, requests: 812, complete: true}} : {}),
    ...(focus ? {focus: {hours: 9, idle_cutoff_s: 300, tracked_since: tracked,
      apps: [{id: 'claude', label: 'Claude', hours: 5}, {id: 'cursor', label: 'Cursor', hours: 3}, {id: 'terminal', label: 'Windows Terminal', hours: 1}],
      days: dates.slice(-3).map((date) => ({date, hours: 3, apps: {claude: 5 / 3, cursor: 1, terminal: 1 / 3}}))}} : {}),
    ...(backfill === undefined ? {} : {backfill}),
    ...(db ? {db: {path: 'C:\\Users\\me\\AppData\\Roaming\\TOKENMAXXING\\ledger.sqlite', bytes: 48e6}} : {}),
    _dates: dates,
  };
}

function showOverview(run, range, o) {
  run(`state.overviewRange = ${JSON.stringify(range)}; state.overviews[${JSON.stringify(range)}] = ${JSON.stringify({...o, range})}; renderOverview()`);
}

test('the Overview offers 7 and 30 days only, and a saved Today or This cycle opens on 30 days', () => {
  const run = renderer();
  assert.deepEqual(JSON.parse(run(`JSON.stringify(OV_RANGES.map(([id]) => id))`)), ['week', 'month']);
  assert.equal(run(`state.overviewRange`), 'month');
  for (const saved of ['day', 'cycle', null, 'nonsense']) assert.equal(run(`savedRange(${JSON.stringify(saved)})`), 'month');
  assert.equal(run(`savedRange('week')`), 'week');
  assert.equal(run(`rangeSince('week')`), run(`midnight(6)`));
  assert.equal(run(`rangeSince('month')`), run(`midnight(29)`));
  assert.ok(Math.abs(run(`monthShare('week')`) - 7 / 30) < 1e-12);
  assert.equal(run(`monthShare('month')`), 1);
  // The Today chart and the cycle branches are gone.
  assert.equal(run(`typeof overviewDeskToday`), 'undefined');
});

test('switching ranges paints the last payload for it at once, and never blanks', async () => {
  const replies = [];
  const run = rendererWithDom({call: (cmd, args) => new Promise(resolve => replies.push({cmd, args, resolve}))});
  showOverview(run, 'month', payload({days: 30}));
  run(`state.overviews.week = ${JSON.stringify({...payload({days: 7}), range: 'week'})}`);
  run(`setOverviewRange('week')`);
  // Painted from memory before the engine answers, and not dimmed.
  assert.match(run.body().textContent, /Last 7 days/);
  assert.equal(run.find('.is-stale').length, 0);
  assert.equal(replies.length, 1);
  assert.equal(replies[0].args.since, run(`midnight(6)`));
  assert.equal(run(`localStorage.getItem('overviewRange')`), 'week');
  replies[0].resolve(payload({days: 7}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(run(`state.overviewLoading.week`), false);
  // A range never read yet keeps the other one up, dimmed, until it arrives.
  run(`delete state.overviews.month`);
  run(`setOverviewRange('month')`);
  assert.match(run.body().textContent, /Last 7 days/);
  assert.equal(run.find('.is-stale').length, 1);
  assert.match(run.body().textContent, /Updating…/);
});

test('the subscription multiple compares like with like for 7 days', () => {
  const run = rendererWithDom();
  run(`state.cycle = {plan: 'Ultra', email: 'me@example.com'}`);
  showOverview(run, 'week', payload({days: 7}));
  // Value: Cursor $500 + Claude $12,345.67 + Codex $234.56; plans $200 + $200 + $20 a month, 7/30 of that.
  const paid = (200 + 200 + 20) * 7 / 30;
  const roi = run.find('.ov-roi')[0];
  assert.match(roi.textContent, new RegExp(`for \\$${Math.round(paid)} of subscriptions over 7 days`));
  assert.match(roi.textContent, new RegExp(`${((50000 + 1234567 + 23456) / 100 / paid).toFixed(1)}×`));
  assert.match(roi.getAttribute('data-explain'), /7\/30 of each monthly price/);
  assert.match(roi.getAttribute('data-explain'), /Cursor \$200\/mo \+ Claude \$200\/mo \+ Codex \$20\/mo/);
  showOverview(run, 'month', payload({days: 30}));
  assert.match(run.find('.ov-roi')[0].textContent, /for \$420\/mo in subscriptions/);
});

test('your time is measured where the tracker ran and estimated from prompts before, drawn differently', () => {
  const run = rendererWithDom();
  const o = payload({days: 30});
  showOverview(run, 'month', o);
  const days = JSON.parse(run(`JSON.stringify(yourDays(state.overviews.month))`));
  assert.deepEqual(days.slice(-3).map(d => d.kind), ['measured', 'measured', 'measured']);
  assert.ok(days[27].began, 'the first measured day says when measuring began');
  assert.equal(days[26].kind, 'estimated');
  const you = JSON.parse(run(`JSON.stringify(yourTime(state.overviews.month))`));
  const estimate = o.desk_days.slice(0, 27).reduce((s, d) => s + d.hours, 0);
  assert.equal(you.measured, 9);
  assert.equal(you.estimated, estimate);
  assert.equal(you.total, 9 + estimate);
  assert.equal(run(`yourBasis(yourTime(state.overviews.month))`), 'measured 3 d, estimated 27 d');
  // Estimated days are outlined columns that say so; measured days stack by app.
  const plot = run.find('.ov-plot')[0];
  const cols = plot.querySelectorAll('.ov-pcol');
  assert.equal(cols.length, 30);
  assert.ok(cols[0].classList.contains('is-estimated'));
  assert.match(cols[0].getAttribute('data-explain'), /estimated from your prompts/);
  assert.ok(cols[29].classList.contains('is-measured'));
  assert.equal(cols[29].querySelectorAll('i').length, 3);
  assert.match(cols[29].getAttribute('data-explain'), /Measured: Claude 1 h 40 min, Cursor 1 h, Terminal 20 min\./);
  // The per-app breakdown, with the estimate on a row of its own.
  const rows = run.find('.ov-arow').map(r => r.textContent);
  assert.deepEqual(rows.map(r => r.replace(/\d.*$/, '').trim()), ['Claude', 'Cursor', 'Terminal', 'Estimated from your prompts']);
  assert.match(run.body().textContent, /Time in your AI tools/);
  assert.doesNotMatch(run.body().textContent, /at the computer/);
  // The leverage ratio says which hours it used.
  const lever = run.find('.ov-lev')[0];
  assert.match(lever.getAttribute('data-explain'), /measured for 3 days and estimated from your prompts for 27 days/);
});

test('with no focus data every day is the prompt estimate, labelled as one', () => {
  const run = rendererWithDom();
  const o = payload({days: 7, focus: false});
  showOverview(run, 'week', o);
  const you = JSON.parse(run(`JSON.stringify(yourTime(state.overviews.week))`));
  assert.equal(you.measuredDays, 0);
  assert.equal(you.total, o.desk_days.reduce((s, d) => s + d.hours, 0));
  assert.equal(run.find('.ov-basis')[0].textContent, 'estimated from your prompts');
  assert.ok(run.find('.ov-basis')[0].classList.contains('is-est'));
  assert.ok(run.find('.ov-plot')[0].querySelectorAll('.ov-pcol').every(c => c.classList.contains('is-estimated')));
});

test('days the history build has not reached are pending, and totals say so far until it is done', () => {
  const run = rendererWithDom();
  const o = payload({days: 30});
  const from = startOf(o._dates[20]);
  showOverview(run, 'month', {...o, backfill: {done: false, read_bytes: 2.688e9, total_bytes: 6.4e9, complete_from: from}});
  const strip = run.find('.ov-build')[0];
  assert.equal(strip.textContent, 'Building your history: 42% of 6.4 GB. One time only.');
  const agentCols = run.find('.ov-plot')[1].querySelectorAll('.ov-pcol');
  assert.ok(agentCols.slice(0, 20).every(c => c.classList.contains('is-pending')));
  assert.ok(agentCols.slice(20).every(c => !c.classList.contains('is-pending')));
  assert.match(agentCols[0].getAttribute('data-explain'), /not read yet/);
  assert.equal(agentCols[0].querySelectorAll('.ov-stack').length, 0);
  assert.ok(run.find('.ov-sofar').length >= 3);
  // Only the byte count moved: the strip alone is redrawn, everything else stays put.
  const hero = run.find('.ov-hero')[0];
  run(`state.overviews.month.backfill.read_bytes = 3.2e9; renderOverview()`);
  assert.equal(run.find('.ov-hero')[0], hero);
  assert.equal(run.find('.ov-build')[0].textContent, 'Building your history: 50% of 6.4 GB. One time only.');
  // Done: no strip, no hatching, no "so far".
  run(`state.overviews.month.backfill = {done: true, read_bytes: 6.4e9, total_bytes: 6.4e9, complete_from: ${o.since}}; renderOverview()`);
  assert.equal(run.find('.ov-build').length, 0);
  assert.equal(run.find('.is-pending').length, 0);
  assert.equal(run.find('.ov-sofar').length, 0);
});

test('explanations appear quickly on hover or focus, point back with aria-describedby, and hide', () => {
  const run = rendererWithDom();
  run(`const fig = el('span', {text: '42', explain: 'Forty-two\\nWorked out **carefully**.'}); document.body.append(fig);
       const btn = el('button', {text: 'Go', explain: 'Go\\nDoes it.'}); document.body.append(btn);`);
  const fig = run('fig');
  assert.equal(fig.getAttribute('tabindex'), '0');
  assert.equal(run(`btn.hasAttribute('tabindex')`), false);
  const tip = run.doc.getElementById('tooltip');
  run(`explainShow(fig)`);
  assert.equal(fig.getAttribute('aria-describedby'), 'tooltip');
  assert.equal(tip.classList.contains('is-on'), false, 'waits for the short pause');
  assert.equal(run.timers.at(-1).ms, 150);
  run.flush();
  assert.ok(tip.classList.contains('is-on') && tip.classList.contains('is-explain'));
  assert.equal(tip.textContent, 'Forty-twoWorked out carefully.');
  assert.match(tip.style.left, /^\d+px$/);
  // Moving straight on to the next figure needs no second pause.
  run(`explainShow(btn)`);
  assert.equal(tip.textContent, 'GoDoes it.');
  assert.equal(fig.hasAttribute('aria-describedby'), false);
  assert.equal(run(`btn.getAttribute('aria-describedby')`), 'tooltip');
  run(`explainHide()`);
  assert.equal(tip.classList.contains('is-on'), false);
  assert.equal(run(`btn.hasAttribute('aria-describedby')`), false);
});

test('every figure on the Overview explains itself, and none uses a native title', () => {
  const run = rendererWithDom();
  run(`state.cycle = {plan: 'Ultra', conversations: [{last_at: Date.now() / 1000, subagent: false}]}`);
  showOverview(run, 'month', payload({days: 30, backfill: {done: false, read_bytes: 1, total_bytes: 10, complete_from: null}}));
  const all = run.body().querySelectorAll('div, span, b, i, button, h2');
  assert.equal(all.filter(n => n.hasAttribute('title')).length, 0);
  for (const selector of ['.ov-pc', '.ov-value', '.ov-roi', '.ov-who', '.ov-lev', '.ov-lane', '.ov-peak', '.ov-arow', '.ov-tile', '.ov-trow', '.ov-where', '.ov-build']) {
    const nodes = run.find(selector);
    assert.ok(nodes.length, `${selector} is drawn`);
    for (const node of nodes) assert.ok(node.getAttribute('data-explain'), `${selector} explains itself`);
  }
  const cells = run.find('.ov-heat')[0].children.filter(c => c.tagName === 'I');
  assert.equal(cells.length, 168);
  assert.ok(cells.every(c => c.getAttribute('data-explain')));
  assert.ok(run.find('.ov-mixl')[0].children.every(c => c.getAttribute('data-explain')));
  assert.ok(run.find('.ov-pcol').every(c => c.getAttribute('data-explain')));
  assert.match(run.find('.ov-where')[0].getAttribute('data-explain'), /ledger\.sqlite[\s\S]*Nothing in it leaves this computer/);
  // A chart is one Tab stop; the arrow keys step through its days.
  const cols = run.find('.ov-plot')[1].querySelectorAll('.ov-pcol');
  assert.deepEqual([cols[0].getAttribute('tabindex'), cols[1].getAttribute('tabindex')], ['0', '-1']);
  run.find('.ov-plot')[1].dispatch('keydown', {key: 'ArrowRight', target: cols[0]});
  assert.equal(run.doc.activeElement, cols[1]);
  assert.equal(cols[1].getAttribute('tabindex'), '0');
});

test('an older engine without the new keys still draws the Overview', () => {
  const run = rendererWithDom();
  const o = payload({days: 30, focus: false, cursor: false, db: false});
  for (const day of o.days) delete day.cents;
  showOverview(run, 'month', o);
  assert.match(run.body().textContent, /Your time vs agent time/);
  assert.equal(run.find('.ov-build').length, 0);
  assert.equal(run.find('.ov-where').length, 0);
  assert.equal(run.find('.is-pending').length, 0);
  assert.doesNotMatch(run.find('.ov-plot')[1].querySelectorAll('.ov-pcol')[0].getAttribute('data-explain'), /Worth/);
});

test('the title carries the version, and the badge hides without one', async () => {
  const run = rendererWithDom({appVersion: async () => ({version: '0.51.0', tag: 'v051'})});
  await run(`paintVersion()`);
  assert.equal(run.doc.title, 'TOKENMAXXING v051');
  assert.equal(run.doc.getElementById('brandVersion').textContent, 'v051');
  assert.equal(run.doc.getElementById('brandVersion').hidden, false);
  const old = rendererWithDom();
  old.doc.getElementById('brandVersion').hidden = true;
  await old(`paintVersion()`);
  assert.equal(old.doc.title, 'TOKENMAXXING');
  assert.equal(old.doc.getElementById('brandVersion').hidden, true);
});

test('the subtitle names every connected provider’s plan and one email', () => {
  const run = renderer();
  run(`state.cycle = {plan: 'Ultra', email: 'me@cursor.example'};
       state.providers = {providers: [
         {id: 'claude', status: 'ok', plan: 'Max 20x', email: 'me@claude.example', windows: []},
         {id: 'codex', status: 'ok', plan: 'plus', windows: []}]}`);
  assert.equal(run(`brandSubtitle()`), 'Cursor Ultra · Claude Max 20x · Codex Plus · me@cursor.example');
  // No plan: just the name. Signed out: left off. No Cursor: Claude's email.
  run(`state.providers.providers[1].plan = undefined; state.providers.providers[0].needs_sign_in = true`);
  assert.equal(run(`brandSubtitle()`), 'Cursor Ultra · Codex · me@cursor.example');
  run(`state.cycle = null; state.providers.providers[0].needs_sign_in = false; state.providers.providers[1].status = 'unavailable'`);
  assert.equal(run(`brandSubtitle()`), 'Claude Max 20x · me@claude.example');
  run(`state.providers.providers[0].plan = 'Claude Pro'`);
  assert.equal(run(`brandSubtitle()`), 'Claude Pro · me@claude.example');
});

test('Cursor’s row prefers the local ledger once it covers the range', () => {
  const run = renderer();
  const since = run(`midnight(29)`);
  run(`state.cycle = {plan: 'Ultra', range_totals: {month: {start: ${since}, cents: 777, input: 1, output: 2, cache_read: 3, cache_write: 4}}}`);
  const row = (o) => JSON.parse(run(`JSON.stringify(cursorRow(${JSON.stringify(o)}))`));
  const ledger = row({range: 'month', since, cursor: {cents: 50000, tokens: {input: 9}, requests: 812, complete: true}});
  assert.equal(ledger.cents, 50000);
  assert.equal(ledger.source, 'ledger');
  assert.equal(ledger.price, 200);
  const partial = row({range: 'month', since, cursor: {cents: 100, tokens: {}, requests: 3, complete: false}});
  assert.equal(partial.cents, 777);
  assert.equal(partial.source, 'dashboard');
  assert.equal(row({range: 'month', since}).cents, 777);
  assert.equal(row({range: 'week', since: run(`midnight(6)`)}), null);
});
