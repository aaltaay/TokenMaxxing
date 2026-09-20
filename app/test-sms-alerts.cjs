const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {SmsAlerts}=require('./sms-alerts');
const config={sid:'AC'+'a'.repeat(32),token:'b'.repeat(32),from:'+15555550100',to:'+15555550101'};
function setup(t,fetcher){
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'sms-test-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const storage={isEncryptionAvailable:()=>true,encryptString:s=>Buffer.from(s),decryptString:b=>b.toString()};
  return new SmsAlerts({filename:path.join(dir,'test.enc'),storage,fetcher});
}
test('Twilio posts only the configured destination and returns no credentials or false delivery claim',async t=>{
  let request;
  const sms=setup(t,async(url,args)=>{request={url,...args};return{ok:true,json:async()=>({status:'queued'})};});
  const saved=sms.save(config);
  assert.equal(saved.configured,true);
  assert.equal('token' in saved,false);
  const result=await sms.send('Reset in 15 minutes');
  assert.equal(new URLSearchParams(request.body).get('To'),config.to);
  assert.equal(request.redirect,'error');
  assert.match(result.last,/queued/);
  assert.doesNotMatch(result.last,/delivered/);
});
test('SMS is never attempted before configuration',async t=>{
  const sms=setup(t,()=>{throw new Error('must not send');});
  assert.equal((await sms.send('test')).configured,false);
});
test('Twilio errors are sanitized and uncertain requests are not retried',async t=>{
  let requests=0;
  const sms=setup(t,async()=>{requests++;throw new Error(config.token);});
  sms.save(config);
  const result=await sms.send('test');
  assert.equal(requests,1);assert.doesNotMatch(result.last,new RegExp(config.token));
  assert.match(result.last,/unconfirmed/);
});
test('invalid numbers and unencrypted credential storage are rejected',t=>{
  const sms=setup(t,()=>{});
  assert.throws(()=>sms.save({...config,to:'7042993472'}));
  sms.storage.isEncryptionAvailable=()=>false;
  assert.throws(()=>sms.save(config),/Secure/);
});
