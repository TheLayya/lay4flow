// Synthetic Chrome APIs exercise the account-login bridge without any network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../account_import_extension/background.js'), 'utf8');
const event = () => ({ addListener(fn) { this.listener = fn; } });
const plain = value => JSON.parse(JSON.stringify(value));

async function fixture() {
  const stored = {}, messages = [], removed = [], tabs = new Map();
  let cookies = [], timer = 0;
  tabs.set(1, { id: 1, url: 'http://manage.test/#/accounts', windowId: 10 });
  const chrome = {
    runtime: { onMessage: event(), lastError: null },
    storage: {
      session: {
        async get() { return { ...stored }; },
        async set(values) { Object.assign(stored, plain(values)); },
        async remove(key) { delete stored[key]; }
      },
      local: { async get() { return {}; }, async set() {} }
    },
    tabs: {
      async create(options) { const tab = { ...options, id: 2, windowId: options.windowId || 20 }; tabs.set(2, tab); return tab; },
      async get(id) { if (!tabs.has(id)) throw Error('missing tab'); return tabs.get(id); },
      async update(id, options) { Object.assign(tabs.get(id), options); return tabs.get(id); },
      async remove(id) { removed.push(['tab', id]); tabs.delete(id); },
      async query() { return []; },
      sendMessage(id, message, done) { messages.push(plain(message)); done?.(); },
      onUpdated: event(), onRemoved: event()
    },
    windows: {
      async create(options) { tabs.set(2, { id: 2, windowId: 20, url: options.url }); return { id: 20, tabs: [tabs.get(2)] }; },
      async update() {},
      async remove(id) { removed.push(['window', id]); tabs.delete(2); }
    },
    cookies: {
      async getAllCookieStores() { return [{ id: '0', tabIds: [1, 2] }]; },
      async getAll() { return cookies; },
      async remove(detail) { removed.push(['cookie', detail.name]); cookies = cookies.filter(c => c.name !== detail.name); },
      onChanged: event()
    },
    alarms: { async create() {}, async clear() {}, onAlarm: event() },
    action: { onClicked: event() }
  };
  const context = vm.createContext({ chrome, URL, console, setTimeout: () => ++timer, clearTimeout() {}, btoa: s => Buffer.from(s).toString('base64') });
  vm.runInContext(source + '\nthis.testApi = {startImport, probe, finishSubmit, activeImports, importsRestored};', context);
  await context.testApi.importsRestored;
  return {
    chrome, messages, removed, stored, api: context.testApi,
    setCookies(value) { cookies = value; },
    async runtime(message, sender) {
      return new Promise(resolve => chrome.runtime.onMessage.listener(message, sender, resolve));
    }
  };
}

const cookie = (name, domain = '.google.com', value = 'synthetic-secret') => ({
  name, value, domain, path: '/', secure: true, httpOnly: true,
  hostOnly: !domain.startsWith('.'), storeId: '0', sameSite: 'lax'
});
const identity = { user_agent: 'Synthetic Chrome', language: 'en-US', languages: ['en-US'] };
const admin = { frameId: 0, tab: { id: 1, windowId: 10, url: 'http://manage.test/#/accounts' }, url: 'http://manage.test/#/accounts' };
const flow = { frameId: 0, tab: { id: 2, url: 'https://flow.google.com/' }, url: 'https://flow.google.com/' };
const receipt = context => ({
  session_id: context, nonce: 'synthetic-nonce', admin_origin: 'http://manage.test',
  complete_url: 'http://manage.test/api/plugin/import/complete',
  return_url: 'http://manage.test/manage#/accounts', expires_at: Date.now() / 1000 + 300,
  login_context: context === 'profile' ? 'profile' : 'isolated'
});

(async () => {
  for (const mode of ['profile', 'isolated']) {
    const f = await fixture();
    f.setCookies([cookie('SID'), cookie('OSID', 'flow.google.com')]);
    let response;
    await f.api.startImport(receipt(mode), admin, value => { response = value; });
    assert.equal(response.type, 'import.accepted');
    const record = f.api.activeImports.get(mode);
    if (mode === 'profile') assert.deepEqual(f.removed, [], 'Existing fingerprint cookies must survive start');
    else assert.ok(f.removed.some(row => row[0] === 'cookie'), 'Isolated login clears its own temporary store');
    f.setCookies([cookie('SID'), cookie('OSID', 'flow.google.com'), cookie('unrelated', 'evil.test')]);
    await f.api.probe(record);
    assert.ok(!f.messages.some(m => m.type === 'import.submit'), 'Wait for actual Flow navigator identity');
    await f.runtime({ type: 'import.flow.identity', identity }, { ...flow, frameId: 1 });
    assert.equal(record.browserIdentity, undefined, 'An iframe cannot set login identity');
    await f.runtime({ type: 'import.flow.identity', identity }, flow);
    await f.api.probe(record);
    const submission = f.messages.find(m => m.type === 'import.submit');
    assert.ok(submission, 'Modern Flow login submits without a Labs session token');
    assert.equal(submission.complete_url, 'http://manage.test/api/plugin/import/complete');
    assert.equal(submission.authorization, `Bearer import:${mode}.synthetic-nonce`);
    assert.deepEqual(submission.payload.browser_identity, identity);
    assert.equal(submission.payload.session_token, undefined);
    assert.ok(!submission.payload.google_cookies.includes('evil.test'));
    assert.ok(!JSON.stringify(f.stored).includes('synthetic-secret'), 'Cookie values never enter extension storage');
    await f.api.finishSubmit(record, { ok: true, status: 200, result: { success: true, email: 'member@example.test', action: 'added' } });
    assert.equal(f.api.activeImports.size, 0);
    assert.deepEqual(Object.keys(f.stored), []);
    assert.ok(f.messages.some(m => m.type === 'import.result'));
    if (mode === 'profile') assert.ok(!f.removed.some(row => row[0] === 'cookie'), 'Current fingerprint login must survive completion');
    else assert.ok(f.removed.some(row => row[0] === 'window'));
  }
  const f = await fixture();
  let rejected;
  await f.api.startImport({ ...receipt('profile'), complete_url: 'https://evil.test/collect' }, admin, r => { rejected = r; });
  assert.equal(rejected.type, 'import.error');
  assert.equal(f.api.activeImports.size, 0);
  console.log('PASS: modern Flow extension login, owned tab identity, fixed callback, profile retention and isolated cleanup');
})().catch(error => { console.error(error); process.exitCode = 1; });
