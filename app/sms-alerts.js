'use strict';
const fs = require('node:fs');
const path = require('node:path');

class SmsAlerts {
  constructor({filename, storage, fetcher = fetch}) {
    Object.assign(this, {filename, storage, fetcher});
    this.last = 'SMS not configured';
    this.busy = false;
  }
  read() {
    try { return JSON.parse(this.storage.decryptString(fs.readFileSync(this.filename))); }
    catch { return null; }
  }
  status() {
    const config = this.read();
    const last = config && this.last === 'SMS not configured' ? 'SMS configured. Automatic reset messages are enabled while alerts are on.' : this.last;
    return {configured: !!config, to: config?.to || '', from: config?.from || '', last};
  }
  save(input) {
    const config = Object.fromEntries(['sid','token','from','to'].map(k => [k, String(input[k] || '').trim()]));
    if (!/^AC[0-9a-f]{32}$/i.test(config.sid) || !/^[0-9a-f]{32}$/i.test(config.token)) throw new Error('Enter your Twilio Account SID and Auth Token.');
    if (!/^\+[1-9]\d{7,14}$/.test(config.to) || !/^\+[1-9]\d{7,14}$/.test(config.from)) throw new Error('Use phone numbers with country code, such as +17042993472.');
    if (!this.storage.isEncryptionAvailable() || this.storage.getSelectedStorageBackend?.() === 'basic_text') throw new Error('Secure credential storage is unavailable.');
    fs.mkdirSync(path.dirname(this.filename), {recursive:true});
    fs.writeFileSync(this.filename + '.tmp', this.storage.encryptString(JSON.stringify(config)));
    fs.renameSync(this.filename + '.tmp', this.filename);
    this.last = 'Configured. Send a test SMS to verify delivery.';
    return this.status();
  }
  async send(body) {
    const config = this.read();
    if (!config) return this.status();
    if (this.busy) throw new Error('An SMS request is already in progress.');
    this.busy = true;
    try {
      const response = await this.fetcher(`https://api.twilio.com/2010-04-01/Accounts/${config.sid}/Messages.json`, {
        method:'POST', redirect:'error', signal:AbortSignal.timeout(20000),
        headers:{Authorization:'Basic '+Buffer.from(`${config.sid}:${config.token}`).toString('base64'), 'Content-Type':'application/x-www-form-urlencoded'},
        body:new URLSearchParams({To:config.to, From:config.from, Body:body.slice(0,1500)}).toString(),
      });
      const result = await response.json();
      if (!response.ok) {
        const code = Number.isInteger(result.code) ? ` (code ${result.code})` : '';
        throw new Error(`Twilio rejected the SMS${code}. Check your Twilio console.`);
      }
      const status = ['queued','accepted','sending','sent','delivered','failed','undelivered'].includes(result.status) ? result.status : 'accepted';
      this.last = `SMS ${status} by Twilio at ${new Date().toLocaleTimeString()}.`;
      return this.status();
    } catch (error) {
      this.last = error.message?.startsWith('Twilio rejected') ? error.message : 'SMS delivery is unconfirmed. Check Twilio before retrying.';
      return this.status();
    } finally { this.busy = false; }
  }
}
module.exports = {SmsAlerts};
