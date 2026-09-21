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
  run(`renderResets = () => {}; paintOverview = () => {};
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
    paintOverview = () => {};
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
