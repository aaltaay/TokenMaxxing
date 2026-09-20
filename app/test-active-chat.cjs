const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {ActiveChat} = require('./active-chat');

test('title changes track the open chat; stale or malformed readings are unavailable', () => {
  let now = 100, killed = false;
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stdout.setEncoding = () => {};
  child.kill = () => { killed = true; };
  const tracker = new ActiveChat({platform:'win32',script:'active-chat.ps1',now:()=>now,
    spawnProcess:(_cmd,args,options)=>{
      assert.equal(options.windowsHide,true);
      assert.equal(options.shell,undefined);
      assert.ok(args.includes('-NonInteractive'));
      return child;
    }});
  try {
    tracker.start();
    child.stdout.emit('data','{"title":"First');
    assert.equal(tracker.currentTitle(),null);
    child.stdout.emit('data',' chat"}\n');
    assert.equal(tracker.currentTitle(),'First chat');
    child.stdout.emit('data','{"title":"Second chat"}\n');
    assert.equal(tracker.currentTitle(),'Second chat');
    now += 4001;
    assert.equal(tracker.currentTitle(),null);
    child.stdout.emit('data','{"title":null}\n');
    assert.equal(tracker.currentTitle(),null);
    child.stdout.emit('data','invalid\n');
    assert.equal(tracker.currentTitle(),null);
    child.emit('exit',1);
    assert.equal(tracker.currentTitle(),null);
  } finally { tracker.stop(); }
});

test('non-Windows launches do not spawn an accessibility reader', () => {
  const tracker = new ActiveChat({platform:'linux',spawnProcess:()=>{throw new Error('must not run');}});
  tracker.start();
  assert.equal(tracker.currentTitle(),null);
  tracker.stop();
});
