const {test} = require('node:test');
const assert = require('node:assert/strict');
const {ResetAttention} = require('./reset-attention');

test('alerts restore, foreground, pulse, sound, then restore the original pin preference', () => {
  const calls=[]; let state;
  const win={isDestroyed:()=>false,isAlwaysOnTop:()=>false,isMinimized:()=>true};
  for(const method of ['restore','setAlwaysOnTop','show','moveTop','focus','flashFrame']) win[method]=(...args)=>calls.push([method,...args]);
  const alert = new ResetAttention({getWindow:()=>win,publish:value=>state=value,buzz:()=>calls.push(['buzz']),notify:()=>calls.push(['notify'])});
  alert.show([{id:'one',message:'Confirmed reset'}]);
  assert.equal(state.pulsing,true);
  for(const name of ['restore','show','moveTop','focus','buzz','notify']) assert(calls.some(c=>c[0]===name));
  alert.show([{id:'one',message:'Confirmed reset'},{id:'two',message:'Another reset'}]);
  assert.equal(state.events.length,2);
  alert.dismiss();
  assert.equal(state,null);
  assert.deepEqual(calls.at(-1),['setAlwaysOnTop',false,'floating']);
});

test('sound and pulse stop automatically while notice remains until dismissed', async () => {
  let state; let buzzes=0;
  const win={isDestroyed:()=>false,isAlwaysOnTop:()=>true,isMinimized:()=>false,
    setAlwaysOnTop(){},show(){},moveTop(){},focus(){},flashFrame(){}};
  const alert=new ResetAttention({getWindow:()=>win,publish:s=>state=s,buzz:()=>buzzes++,notify(){},durationMs:30,repeatMs:10});
  alert.show([{id:'test',message:'Preview'}]);
  await new Promise(r=>setTimeout(r,65));
  assert.equal(state.pulsing,false);
  const prior=buzzes;
  await new Promise(r=>setTimeout(r,25));
  assert.equal(buzzes,prior);
  alert.dismiss();
});
