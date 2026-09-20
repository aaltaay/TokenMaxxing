'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { ProviderLinkManager, allowedAuthUrl, extractAuthUrl, INSTALL_URLS } = require('./provider-link');

function child() {
  const value = new EventEmitter();
  value.stdout = new EventEmitter();
  value.stderr = new EventEmitter();
  value.killed = false;
  value.kill = () => { value.killed = true; value.emit('close', null); };
  return value;
}

function setup(options = {}) {
  const calls = [], events = [];
  const manager = new ProviderLinkManager({
    resolveCli: () => ({ command: 'official-cli.exe', args: [] }),
    spawnProcess: (command, args, opts) => {
      const process = child(); calls.push({ command, args, opts, process }); return process;
    },
    onChange: state => events.push(state),
    ...options,
  });
  return { manager, calls, events };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

test('Codex starts only official login without a shell; duplicate clicks share the process', async t => {
  const { manager, calls } = setup(); t.after(() => manager.dispose());
  assert.equal(manager.start('codex').status, 'connecting');
  manager.start('codex'); await flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].args, ['login']);
  assert.equal(calls[0].opts.shell, false);
  assert.equal(calls[0].opts.windowsHide, true);
  calls[0].process.emit('close', 0);
  assert.equal(manager.status('codex').status, 'success');
});

test('Claude probes help then requests subscription sign-in when supported', async t => {
  const { manager, calls } = setup(); t.after(() => manager.dispose());
  manager.start('claude'); await flush();
  assert.deepEqual(calls[0].args, ['auth', 'login', '--help']);
  calls[0].process.stdout.emit('data', 'Usage: claude auth login [options]\n--claudeai subscription\n');
  calls[0].process.emit('close', 0); await flush();
  assert.deepEqual(calls[1].args, ['auth', 'login', '--claudeai']);
});

test('Older documented Claude auth login uses its default subscription flow', async t => {
  const { manager, calls } = setup(); t.after(() => manager.dispose());
  manager.start('claude'); await flush();
  calls[0].process.stdout.emit('data', 'Usage: claude auth login [options]\n');
  calls[0].process.emit('close', 0); await flush();
  assert.deepEqual(calls[1].args, ['auth', 'login']);
});

test('Unknown Claude help refuses to start a possible model session', async t => {
  const { manager, calls } = setup(); t.after(() => manager.dispose());
  manager.start('claude'); await flush();
  calls[0].process.stdout.emit('data', 'Usage: claude [prompt]\n');
  calls[0].process.emit('close', 0); await flush();
  assert.equal(calls.length, 1);
  assert.equal(manager.status('claude').status, 'error');
  assert.equal(manager.status('claude').installUrl, INSTALL_URLS.claude);
});

test('Missing CLI offers the official installation page', async () => {
  const { manager, calls } = setup({ resolveCli: () => null });
  manager.start('claude'); await flush();
  assert.equal(calls.length, 0);
  assert.equal(manager.status('claude').status, 'error');
  assert.equal(manager.status('claude').installUrl, INSTALL_URLS.claude);
  manager.dispose();
});

test('Only complete allowlisted authorization URLs are exposed; raw output never is', async t => {
  const { manager, calls, events } = setup(); t.after(() => manager.dispose());
  manager.start('codex'); await flush();
  const process = calls[0].process;
  process.stdout.emit('data', 'sensitive-output\nhttps://auth.openai.com/oauth/authorize?client_id=client&state=');
  assert.equal(manager.status('codex').authUrl, null);
  process.stdout.emit('data', 'request-state\n');
  assert.equal(manager.status('codex').authUrl,
    'https://auth.openai.com/oauth/authorize?client_id=client&state=request-state');
  assert.equal(JSON.stringify(events).includes('sensitive-output'), false);
  process.emit('close', 0);
  assert.equal(manager.status('codex').authUrl, null);
});

test('Auth allowlist rejects other hosts, credentials, callbacks, tokens, and provider mismatch', () => {
  for (const url of [
    'http://auth.openai.com/oauth/authorize', 'https://auth.openai.com.evil.test/oauth/authorize',
    'https://user:password@auth.openai.com/oauth/authorize',
    'https://auth.openai.com:8443/oauth/authorize', 'https://auth.openai.com/oauth/callback?code=secret',
    'https://auth.openai.com/oauth/authorize?access_token=secret',
    'https://auth.openai.com/oauth/authorize?code=secret',
    'https://auth.openai.com/oauth/authorize#access_token=secret',
    'https://claude.ai/oauth/authorize',
  ]) assert.equal(allowedAuthUrl('codex', url), null, url);
  assert.equal(allowedAuthUrl('claude', 'https://claude.ai/oauth/authorize?code=true'),
    'https://claude.ai/oauth/authorize?code=true');
  assert.equal(extractAuthUrl('claude', 'Open https://evil.test/oauth/authorize\n'), null);
});

test('Cancellation stops only its own process and ignores late success', async () => {
  const { manager, calls } = setup();
  manager.start('codex'); await flush();
  manager.cancel('codex');
  assert.equal(calls[0].process.killed, true);
  calls[0].process.emit('close', 0);
  assert.equal(manager.status('codex').status, 'idle');
  manager.dispose();
});

test('Cancellation during Claude probe cannot start login afterwards', async () => {
  const { manager, calls } = setup();
  manager.start('claude'); await flush();
  manager.cancel('claude'); await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].process.killed, true);
  assert.equal(manager.status('claude').status, 'idle');
  manager.dispose();
});

test('Timeout ends login with a sanitized error and clears auth URL', async () => {
  const { manager, calls } = setup({ timeoutMs: 25 });
  manager.start('codex'); await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(calls[0].process.killed, true);
  assert.equal(manager.status('codex').status, 'error');
  assert.match(manager.status('codex').message, /timed out/);
  assert.equal(manager.status('codex').authUrl, null);
  manager.dispose();
});

test('Errors and unsuccessful exits never expose raw provider messages', async () => {
  const { manager, calls, events } = setup();
  manager.start('codex'); await flush();
  calls[0].process.stderr.emit('data', 'secret-token-value\n');
  calls[0].process.emit('error', new Error('secret-token-value'));
  assert.equal(manager.status('codex').status, 'error');
  assert.equal(JSON.stringify(events).includes('secret-token-value'), false);
  manager.start('codex'); await flush();
  calls[1].process.emit('close', 1);
  assert.equal(manager.status('codex').status, 'error');
  manager.dispose();
});

test('Status returns copies; dispose cancels processes and prevents new logins', async () => {
  const { manager, calls } = setup();
  manager.start('codex'); await flush();
  const states = manager.status(); states.codex.status = 'success';
  assert.equal(manager.status('codex').status, 'connecting');
  manager.dispose();
  assert.equal(calls[0].process.killed, true);
  assert.throws(() => manager.start('codex'), /closed/);
  assert.throws(() => manager.start('arbitrary-command'), /Unknown usage provider/);
});

test('Every provider state revision increases across events and repeat sign-in attempts', async () => {
  const { manager, calls, events } = setup();
  assert.equal(manager.status('codex').revision, 0);
  assert.equal(manager.start('codex').revision, 1);
  assert.equal(manager.start('codex').revision, 1); // Dedupe is not a new state.
  await flush();
  calls[0].process.stdout.emit('data', 'https://auth.openai.com/oauth/authorize?state=example\n');
  assert.equal(manager.status('codex').revision, 2);
  calls[0].process.emit('close', 0);
  assert.equal(manager.status('codex').revision, 3);
  assert.equal(manager.start('codex').revision, 4);
  await flush();
  assert.equal(manager.cancel('codex').revision, 5);
  calls[1].process.emit('close', 0); // Late events cannot change the revision.
  assert.equal(manager.status('codex').revision, 5);
  assert.equal(manager.status('claude').revision, 0);
  assert.deepEqual(events.map(state => state.revision), [1, 2, 3, 4, 5]);
  manager.dispose();
});
