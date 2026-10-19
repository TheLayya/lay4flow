// Run with: node tests/test_manage_auth.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/manage.html'), 'utf8');
const scripts = [fs.readFileSync(path.join(root, 'static/js/manage.js'), 'utf8')];
const redirects = [];
const context = vm.createContext({window: {location: {replace: target => redirects.push(target)}}});
for (const script of scripts) vm.runInContext(script, context);
assert.deepEqual(redirects, ['/login#/accounts']);
assert.ok(html.includes('http-equiv="refresh" content="0;url=/login#/accounts"'));
assert.ok(html.includes('href="/login#/accounts"'));
assert.doesNotMatch(html, /<form\b|<iframe\b|tailwind|captcha_method|import\.hello/);
console.log('PASS: retired management entry redirects to the current authenticated account page');
