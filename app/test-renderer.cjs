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

test('session cost distinguishes estimates, partial history, zero and unavailable', () => {
  const run = renderer();
  run(`el = (tag, attrs) => attrs; const costRows = []; const costContainer = {append: row => costRows.push(row)};`);
  run(`renderSessionCost(costContainer, {cents:33, priced_requests:2, partial:true, basis:'API value, not billed charges', pricing_date:'2026-09-20'})`);
  assert.equal(run('costRows[0].text'), '≈ $0.33');
  assert.match(run('costRows[1].text'), /Partial estimate/);
  run(`costRows.length=0; renderSessionCost(costContainer, {cents:0, priced_requests:1})`);
  assert.equal(run('costRows[0].text'), '≈ $0.00');
  run(`costRows.length=0; renderSessionCost(costContainer, null)`);
  assert.match(run('costRows[0].text'), /Cost unavailable/);
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
