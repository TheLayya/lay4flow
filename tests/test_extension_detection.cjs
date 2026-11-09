// Management entry paths retain extension detection across hash-only navigation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../account_import_extension/content.js'), 'utf8');
for (const pathname of ['/', '/login', '/manage', '/test', '/vids', '/api-guide', '/vids/accounts']) {
  const listeners = new Map(), replies = [];
  const window = {
    addEventListener(type, listener) { listeners.set(type, listener); },
    postMessage(value, origin) { replies.push({ value, origin }); }
  };
  const location = { pathname, origin: 'http://127.0.0.1:38001' };
  const chrome = { runtime: { id: 'synthetic-extension', onMessage: { addListener() {} } } };
  vm.runInNewContext(source, { window, location, chrome, URL, fetch() { throw Error('No requests during detection'); } });
  replies.length = 0;
  listeners.get('message')({ source: window, origin: location.origin, data: { source: 'lay4flow-manage', type: 'import.hello' } });
  assert.equal(replies.length, 1, pathname);
  assert.equal(replies[0].value.type, 'import.ready');
  assert.equal(replies[0].value.version, '1.8.4');
  listeners.get('message')({ source: window, origin: 'https://evil.invalid', data: { source: 'lay4flow-manage', type: 'import.hello' } });
  assert.equal(replies.length, 1, 'Foreign origin must not get a reply');
}
console.log('PASS extension detects on all SPA entries including /login#/accounts');
