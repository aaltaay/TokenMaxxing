const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {FocusTracker, classifyFocus, FOCUS_APPS, QUEUE_CAP} = require('./focus');

const T0 = 1_760_000_000_000; // a whole second, so span edges are easy to read
const sec = offset => (T0 + offset * 1000) / 1000;

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stdout.setEncoding = () => {};
  child.killed = false;
  child.kill = () => { child.killed = true; };
  return child;
}

/** A tracker on a fake clock and a fake focus.ps1; `read(offset, sample)` delivers one reading at T0 + offset s. */
function harness({send, platform = 'win32'} = {}) {
  let now = T0;
  const children = [];
  const spawned = [];
  const calls = [];
  const tracker = new FocusTracker({
    platform, script: 'focus.ps1', parentPid: 4242, now: () => now,
    spawnProcess: (cmd, args, options) => { spawned.push({cmd, args, options}); const child = fakeChild(); children.push(child); return child; },
    send: send || (args => { calls.push(JSON.parse(JSON.stringify(args))); return Promise.resolve({ok: true}); }),
  });
  const read = (offset, sample) => {
    now = T0 + offset * 1000;
    children.at(-1).stdout.emit('data', `${JSON.stringify(sample)}\n`);
  };
  return {tracker, children, spawned, calls, read, setNow: offset => { now = T0 + offset * 1000; }};
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const active = (exe, title = '', extra = {}) => ({exe, path: null, title, idle_ms: 1000, ...extra});

test('foreground readings map to the engine\'s app ids, and everything else is not counted', () => {
  const codexPackage = 'C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.930.7945.0_x64__2p2nqsd0c76g0\\app\\';
  const cases = [
    // Desktop apps, by the executable names found on this machine.
    [{exe: 'claude', path: 'C:\\Program Files\\WindowsApps\\Claude_2.31226.1.0_x64__pzs8sxrjxfjjc\\app\\claude.exe', title: 'Claude'}, 'claude'],
    [{exe: 'claude', path: 'C:\\Users\\a\\AppData\\Local\\AnthropicClaude\\app-1.0.0\\claude.exe'}, 'claude'],
    [{exe: 'claude', path: null}, 'claude'],
    [{exe: 'ChatGPT', path: `${codexPackage}ChatGPT.exe`, title: 'ChatGPT'}, 'codex'],
    [{exe: 'Codex', path: `${codexPackage}Codex.exe`, title: 'Codex'}, 'codex'],
    [{exe: 'ChatGPT Classic', path: 'C:\\Program Files\\WindowsApps\\OpenAI.ChatGPT-Desktop_1.2026.190.0_x64__2p2nqsd0c76g0\\app\\ChatGPT Classic.exe'}, 'chatgpt'],
    [{exe: 'ChatGPT', path: 'C:\\Users\\a\\AppData\\Local\\Programs\\ChatGPT\\ChatGPT.exe'}, 'chatgpt'],
    [{exe: 'Cursor', path: 'C:\\Users\\a\\AppData\\Local\\Programs\\cursor\\Cursor.exe', title: 'focus.js - TokenMaxxing - Cursor'}, 'cursor'],
    [{exe: 'Code', title: 'main.js - app - Visual Studio Code'}, 'vscode'],
    [{exe: 'Code - Insiders'}, 'vscode'],
    [{exe: 'Windsurf'}, 'windsurf'],
    // A command-line agent that owns a console window itself.
    [{exe: 'claude', path: 'C:\\Users\\a\\.local\\bin\\claude.exe', title: '✳ Fix the build'}, 'terminal'],
    [{exe: 'codex', path: 'C:\\Users\\a\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\vendor\\codex.exe'}, 'terminal'],
    // Browsers count only on Claude or ChatGPT.
    [{exe: 'chrome', title: 'Fix the build - Claude - Google Chrome'}, 'claude'],
    [{exe: 'chrome', title: 'Claude - Google Chrome'}, 'claude'],
    [{exe: 'msedge', title: 'New chat - Claude and 3 more pages - Personal - Microsoft\u200b Edge'}, 'claude'],
    [{exe: 'firefox', title: 'Fix the build - Claude — Mozilla Firefox'}, 'claude'],
    [{exe: 'brave', title: 'https://claude.ai/new - Brave'}, 'claude'],
    [{exe: 'chrome', title: 'ChatGPT - Google Chrome'}, 'chatgpt'],
    [{exe: 'msedge', title: 'chatgpt.com/c/abc - Personal - Microsoft Edge'}, 'chatgpt'],
    [{exe: 'chrome', title: 'claude pricing - Google Search - Google Chrome'}, null],
    [{exe: 'chrome', title: 'YouTube - Google Chrome'}, null],
    [{exe: 'opera', title: ''}, null],
    // Terminals count only while an agent names the window.
    [{exe: 'WindowsTerminal', title: 'claude'}, 'terminal'],
    [{exe: 'WindowsTerminal', title: '✳ Fix the focus tracker'}, 'terminal'],
    [{exe: 'WindowsTerminal', title: 'Gemini - TokenMaxxing'}, 'terminal'],
    [{exe: 'pwsh', title: 'codex'}, 'terminal'],
    [{exe: 'wezterm-gui', title: 'aider --model sonnet'}, 'terminal'],
    [{exe: 'alacritty', title: 'Claude Code'}, 'terminal'],
    [{exe: 'WindowsTerminal', title: 'Windows PowerShell'}, null],
    [{exe: 'cmd', title: 'Command Prompt'}, null],
    [{exe: 'powershell', title: 'C:\\Users\\a\\github'}, null],
    // This app, and anything unknown.
    [{exe: 'TOKENMAXXING', title: 'TOKENMAXXING v051'}, null],
    [{exe: 'electron', title: 'TOKENMAXXING dev'}, null],
    [{exe: 'Notepad', title: 'claude notes.txt - Notepad'}, null],
    [{exe: 'explorer', title: 'Downloads'}, null],
    [{exe: null, title: 'Claude'}, null],
    [{}, null],
    [null, null],
  ];
  for (const [sample, expected] of cases) {
    assert.equal(classifyFocus(sample), expected, JSON.stringify(sample));
    if (expected) assert.ok(FOCUS_APPS.includes(expected));
  }
});

test('a span grows while the same app stays in front and ends on a switch, an idle user or a gap', async () => {
  const h = harness();
  try {
    h.tracker.start();
    h.read(0, active('claude'));
    h.read(6, active('claude'));
    h.read(12, active('Cursor'));                                   // switch: Claude ends halfway, at 9
    h.read(18, active('chrome', 'YouTube - Google Chrome'));        // not counted: Cursor ends at 15
    h.read(24, active('claude', '', {idle_ms: 300000}));            // five minutes idle is away
    h.read(30, active('claude', '', {idle_ms: 299999}));            // back: starts halfway, at 27
    h.read(60, active('claude'));                                   // 30 s gap: the span ends at its last reading
    assert.equal(h.calls.length, 1, 'a minute after starting, the spans go to the engine');
    assert.deepEqual(h.calls[0], {spans: [
      {app: 'claude', start: sec(0), end: sec(9)},
      {app: 'cursor', start: sec(9), end: sec(15)},
      {app: 'claude', start: sec(27), end: sec(30)},
    ]});
    await settle();
    assert.equal(h.tracker.closed.length, 0, 'confirmed spans are not sent again');
    h.read(66, active('claude'));
    h.read(72, active('claude'));
    await h.tracker.flush();
    assert.deepEqual(h.calls[1], {spans: [{app: 'claude', start: sec(60), end: sec(72)}]}, 'the open span so far');
    h.read(78, active('claude'));
    await h.tracker.flush();
    assert.deepEqual(h.calls[2], {spans: [{app: 'claude', start: sec(60), end: sec(78)}]}, 'resent as it grows');
  } finally { await h.tracker.stop(); }
});

test('the open span goes to the engine every minute while it keeps growing', async () => {
  const h = harness();
  try {
    h.tracker.start();
    for (let s = 0; s <= 125; s += 5) { h.read(s, active('WindowsTerminal', 'claude')); await settle(); }
    assert.deepEqual(h.calls.map(call => call.spans), [
      [{app: 'terminal', start: sec(0), end: sec(60)}],
      [{app: 'terminal', start: sec(0), end: sec(120)}],
    ]);
  } finally { await h.tracker.stop(); }
});

test('the watchdog flushes when readings stop and replaces a silent reader', async () => {
  const h = harness();
  try {
    h.tracker.start();
    h.read(0, active('Cursor'));
    h.read(5, active('Cursor'));
    h.setNow(61);
    h.tracker.tick();
    await settle();
    assert.deepEqual(h.calls[0], {spans: [{app: 'cursor', start: sec(0), end: sec(5)}]});
    assert.equal(h.children[0].killed, true, 'silent for over 30 s');
    assert.equal(h.spawned.length, 2);
    h.read(64, active('Cursor'));
    assert.deepEqual(h.tracker.open, {app: 'cursor', start: T0 + 64000, last: T0 + 64000}, 'a gap, so a new span');
  } finally { await h.tracker.stop(); }
});

test('spans wait while the engine is down and go on the next flush', async () => {
  let fail = true;
  const sent = [];
  const h = harness({send: args => { sent.push(JSON.parse(JSON.stringify(args))); return fail ? Promise.reject(new Error('engine not ready')) : Promise.resolve({}); }});
  try {
    h.tracker.start();
    h.read(0, active('claude'));
    h.read(6, active('Code'));
    h.read(12, active('explorer'));
    assert.equal(await h.tracker.flush(), false);
    assert.equal(h.tracker.closed.length, 2, 'kept for the retry');
    h.read(18, active('claude'));
    h.read(24, active('claude'));
    fail = false;
    assert.equal(await h.tracker.flush(), true);
    assert.deepEqual(sent.at(-1), {spans: [
      {app: 'claude', start: sec(0), end: sec(3)},
      {app: 'vscode', start: sec(3), end: sec(9)},
      {app: 'claude', start: sec(15), end: sec(24)},
    ]});
    assert.equal(h.tracker.closed.length, 0);
  } finally { await h.tracker.stop(); }
});

test('the waiting queue is bounded and drops the oldest spans first', async () => {
  const h = harness({send: () => Promise.reject(new Error('engine down'))});
  try {
    h.tracker.start();
    const total = QUEUE_CAP + 150;
    for (let i = 0; i <= total; i += 1) h.read(i * 6, active(i % 2 ? 'Cursor' : 'claude'));
    await settle();
    assert.equal(h.tracker.closed.length, QUEUE_CAP);
    assert.ok(h.tracker.closed[0].start > sec(0), 'the oldest were dropped');
    assert.equal(h.tracker.closed.at(-1).end, sec(total * 6 - 3), 'the newest were kept');
  } finally { h.tracker.send = () => Promise.resolve(); await h.tracker.stop(); }
});

test('window titles and paths never reach the engine or stay in the tracker', async () => {
  const h = harness();
  try {
    h.tracker.start();
    const secret = 'SECRET-7f3a';
    h.read(0, {exe: 'chrome', path: `C:\\${secret}\\chrome.exe`, title: `${secret} plan - Claude - Google Chrome`, idle_ms: 10});
    h.read(5, {exe: 'WindowsTerminal', path: `C:\\${secret}\\wt.exe`, title: `claude ${secret}`, idle_ms: 10});
    h.read(10, {exe: 'Cursor', path: `C:\\${secret}\\Cursor.exe`, title: `${secret}.js - Cursor`, idle_ms: 10});
    h.read(15, {exe: 'Notepad', path: `C:\\${secret}\\notepad.exe`, title: `${secret} - Notepad`, idle_ms: 10});
    const kept = () => JSON.stringify({closed: h.tracker.closed, open: h.tracker.open, buffer: h.tracker.buffer});
    assert.ok(!kept().includes(secret));
    await h.tracker.flush();
    assert.equal(h.calls.length, 1);
    assert.ok(!JSON.stringify(h.calls).includes(secret));
    for (const call of h.calls) {
      assert.deepEqual(Object.keys(call), ['spans']);
      for (const span of call.spans) {
        assert.deepEqual(Object.keys(span), ['app', 'start', 'end']);
        assert.ok(FOCUS_APPS.includes(span.app));
        assert.ok(Number.isInteger(span.start) && Number.isInteger(span.end) && span.end > span.start);
      }
    }
  } finally { await h.tracker.stop(); }
});

test('the reader is a hidden PowerShell tied to this process, and partial or broken lines are handled', async () => {
  const h = harness();
  try {
    h.tracker.start();
    h.tracker.start();
    assert.equal(h.spawned.length, 1, 'one reader at a time');
    const {cmd, args, options} = h.spawned[0];
    assert.match(cmd, /powershell\.exe$/i);
    assert.equal(options.windowsHide, true);
    assert.equal(options.shell, undefined);
    assert.ok(args.includes('-NonInteractive'));
    assert.equal(args[args.indexOf('-File') + 1], 'focus.ps1');
    assert.equal(args[args.indexOf('-ParentId') + 1], '4242');
    h.setNow(0);
    h.children[0].stdout.emit('data', '{"exe":"Cur');
    assert.equal(h.tracker.open, null);
    h.children[0].stdout.emit('data', 'sor","idle_ms":0}\nnot json\n\n');
    assert.equal(h.tracker.open?.app, 'cursor');
    h.read(5, {exe: 'Cursor', idle_ms: null});
    assert.equal(h.tracker.open, null, 'an unknown idle time is not counted');
  } finally { await h.tracker.stop(); }
});

test('stopping ends the open span at its last reading and hands it over, without waiting forever', async () => {
  const h = harness();
  h.tracker.start();
  h.read(0, active('claude'));
  h.read(5, active('claude'));
  assert.equal(h.tracker.pending(), true);
  h.setNow(9);
  assert.equal(await h.tracker.stop(), true);
  assert.equal(h.children[0].killed, true);
  assert.deepEqual(h.calls, [{spans: [{app: 'claude', start: sec(0), end: sec(5)}]}]);
  assert.equal(h.tracker.pending(), false);

  const stuck = harness({send: () => new Promise(() => {})});
  stuck.tracker.start();
  stuck.read(0, active('Code'));
  stuck.read(5, active('Code'));
  assert.equal(await stuck.tracker.stop(20), false, 'gives up rather than holding up quit');
  assert.equal(stuck.tracker.closed.length, 1);
});

test('non-Windows launches never spawn a reader', async () => {
  const tracker = new FocusTracker({platform: 'linux', script: 'focus.ps1', send: () => { throw new Error('must not send'); },
    spawnProcess: () => { throw new Error('must not run'); }});
  tracker.start();
  tracker.tick();
  assert.equal(tracker.pending(), false);
  assert.equal(await tracker.stop(), true);
});
