const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/api-guide.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'static/js/api-guide.js'), 'utf8');
const ids = [...html.matchAll(/\bid=["']([^"']+)["']/g)].map(match => match[1]);
const businessIds = ['downstream', 'business-models', 'business-parameters', 'business-endpoints',
  'business-chat', 'business-chat-stream', 'business-images', 'business-image-edits',
  'business-videos', 'business-video-query', 'business-video-download', 'business-video-delete',
  'business-video-edit', 'business-billing', 'business-errors'];
assert.equal(ids.length, new Set(ids).size);
for (const id of businessIds) assert(ids.includes(id), 'Missing business page: ' + id);
const embeddedHtml = html.replace(/<header\b[^>]*>[\s\S]*?<\/header>/, '');
assert(embeddedHtml.includes('class="mode-switch"'), 'Mode switch must survive embedded header removal');
assert(embeddedHtml.includes('data-doc-mode="aggregate"'));
assert(embeddedHtml.includes('data-doc-mode="business"'));
assert(html.includes('id="skuRows"'));
assert(html.includes('id="skuFilter"'));
const sectionTags = [...html.matchAll(/<section\b([^>]*)>/g)].map(match => match[1]);
for (const id of businessIds) {
  const tag = sectionTags.find(value => value.includes('id="' + id + '"'));
  assert(tag.includes('data-doc-mode="business"'), 'Business section must select its mode: ' + id);
}
for (const match of html.matchAll(/href="#([^"]+)"/g)) {
  assert(ids.includes(match[1]), 'Missing anchor: ' + match[1]);
}
for (const id of ['images', 'image-edits', 'videos', 'video-edit', 'chat', 'gemini', 'responses']) {
  const section = html.split('id="' + id + '"')[1].split('</section>')[0];
  for (const heading of ['请求参数', '请求示例', '响应']) assert(section.includes(heading));
}

const makeSection = id => ({
  id,
  dataset: {docMode: businessIds.includes(id) ? 'business' : undefined},
  hidden: false,
  closest: () => sections.find(section => section.id === id),
  querySelector: () => ({textContent: id}),
  querySelectorAll: () => [],
  scrollIntoView() {}
});
const sections = ['overview', 'videos', 'video-query', 'images', 'parameters', ...businessIds].map(makeSection);
const navigation = sections.map(section => ({
  href: '#' + section.id,
  hidden: false,
  getAttribute() { return this.href; },
  setAttribute(name, value) { this[name] = value; },
  removeAttribute(name) { delete this[name]; }
}));
const elements = Object.fromEntries(sections.map(section => [section.id, section]));
elements['videos-request'] = {closest: () => sections[1], scrollIntoView() {}};
elements['business-chat-request'] = {closest: () => sections.find(section => section.id === 'business-chat'), scrollIntoView() {}};
elements.pageToc = {replaceChildren() {}, appendChild() {}};
const modeButtons = ['aggregate', 'business'].map(docMode => ({
  dataset: {docMode},
  setAttribute(name, value) { this[name] = value; },
  addEventListener(event, callback) { this[event] = callback; }
}));
const navGroups = ['aggregate', 'business'].map(navMode => ({dataset: {navMode}, hidden: false}));
const schemaLinks = [{href: '/v1/openapi.json'}];
const swaggerLinks = [{href: '/api-docs'}];
const decode = value => value.replaceAll('&quot;', '"').replaceAll('&#39;', "'")
  .replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
const initializedExamples = [...html.matchAll(/<div\b([^>]*class="example"[^>]*data-path="([^"]+)"[^>]*)>([\s\S]*?)<\/pre>/g)].map(match => {
  const source = match[3].match(/<code([^>]*)>([\s\S]*?)<\/code>/);
  const code = {textContent: decode(source[2])};
  const buttons = [...match[3].matchAll(/<button\b[^>]*data-language="([^"]+)"[^>]*aria-pressed="([^"]+)"[^>]*>/g)].map(item => ({
    dataset: {language: item[1]}, 'aria-pressed': item[2],
    setAttribute(name, value) { this[name] = value; },
    addEventListener(event, callback) { this[event] = callback; }
  }));
  return {
    dataset: Object.fromEntries([...match[1].matchAll(/data-([\w-]+)="([^"]+)"/g)]
      .map(item => [item[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), item[2]])),
    originalBody: code.textContent,
    code,
    buttons,
    hasDataJSON: /\bdata-json\b/.test(source[1]),
    querySelector(selector) {
      if (selector === 'pre code') return code;
      if (selector === '[data-json]') return this.hasDataJSON ? code : null;
      if (selector === '[aria-pressed="true"]') return buttons.find(button => button['aria-pressed'] === 'true');
      return null;
    },
    querySelectorAll() { return buttons; }
  };
});
// Exercise the initialization fallback on a real JSON request example, too.
const fallbackExample = initializedExamples.find(example => example.dataset.method === 'POST');
fallbackExample.hasDataJSON = false;
const timers = [];
let clipboard = '';
let copyButton;
const context = vm.createContext({
  document: {
    querySelectorAll(selector) {
      if (selector === '.doc-section') return sections;
      if (selector === '.doc-nav a') return navigation;
      if (selector === '.mode-switch [data-doc-mode]') return modeButtons;
      if (selector === '[data-nav-mode]') return navGroups;
      if (selector === '[data-doc-schema]') return schemaLinks;
      if (selector === '[data-doc-swagger]') return swaggerLinks;
      if (selector === 'pre code') return initializedExamples.map(example => example.code);
      if (selector === '.example[data-path]') return initializedExamples;
      if (selector === '.copy') return copyButton ? [copyButton] : [];
      return [];
    },
    getElementById: id => elements[id],
    createElement() {
      return {
        style: {},
        select() { clipboard = this.value; },
        remove() {}
      };
    },
    body: {appendChild() {}},
    execCommand: () => true
  },
  window: {location: {origin: 'http://api.example.test', hash: '#videos', search: '?embedded=1'},
    addEventListener(event, callback) { this[event] = callback; }},
  navigator: {},
  fetch() { throw new Error('Documentation must not make network requests'); },
  XMLHttpRequest() { throw new Error('Documentation must not make network requests'); },
  setTimeout: callback => timers.push(callback)
});
for (const target of [context, context.window]) {
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(target, name, {get() { throw new Error('Documentation must not read credentials from storage'); }});
  }
}
Object.defineProperty(context.document, 'cookie', {get() { throw new Error('Documentation must not read cookies'); }});
const coldStart = vm.createContext({
  document: context.document,
  window: {location: {origin: 'http://api.example.test', hash: '#business-images'}, addEventListener() {}},
  navigator: {}, fetch: context.fetch, XMLHttpRequest: context.XMLHttpRequest,
  setTimeout: callback => timers.push(callback)
});
vm.runInContext(script, coldStart);
assert.equal(sections.find(section => !section.hidden).id, 'business-images', 'A cold deep link selects business mode');
assert.equal(modeButtons[1]['aria-pressed'], 'true');
assert.equal(navGroups[0].hidden, true);
vm.runInContext(script, context);
assert.equal(sections.filter(section => !section.hidden)[0].id, 'videos');
assert.equal(navigation[1]['aria-current'], 'page');
context.window.location.hash = '#videos-request';
vm.runInContext('showSection()', context);
assert(!sections[1].hidden);
context.window.location.hash = '#quickstart';
vm.runInContext('showSection()', context);
assert(!sections[0].hidden);
context.window.location.hash = '#contracts';
vm.runInContext('showSection()', context);
assert(!sections[4].hidden);

function assertMode(mode) {
  for (const button of modeButtons) assert.equal(button['aria-pressed'], String(button.dataset.docMode === mode));
  for (const group of navGroups) assert.equal(group.hidden, group.dataset.navMode !== mode);
  for (const link of navigation) {
    const section = elements[link.href.slice(1)];
    assert.equal(link.hidden, (section.dataset.docMode || 'aggregate') !== mode, link.href);
  }
  assert.equal(schemaLinks[0].href, mode === 'business' ? '/compat/v1/openapi.json' : '/v1/openapi.json');
  assert.equal(swaggerLinks[0].href, mode === 'business' ? '/compat/api-docs' : '/api-docs');
  assert.equal(sections.filter(section => !section.hidden).length, 1);
}
assertMode('aggregate');
context.window.location.hash = '#videos';
vm.runInContext('showSection()', context);
modeButtons[1].click();
assert.equal(sections.find(section => !section.hidden).id, 'downstream');
assertMode('business');
context.window.location.hash = '#business-images';
context.window.hashchange();
assertMode('business');
modeButtons[0].click();
assert.equal(sections.find(section => !section.hidden).id, 'videos');
assertMode('aggregate');
modeButtons[1].click();
assert.equal(sections.find(section => !section.hidden).id, 'business-images');
assertMode('business');
for (const id of businessIds) {
  context.window.location.hash = '#' + id;
  context.window.hashchange();
  assert.equal(sections.find(section => !section.hidden).id, id);
  assertMode('business');
}
context.window.location.hash = '#business-chat-request';
context.window.hashchange();
assert.equal(sections.find(section => !section.hidden).id, 'business-chat');
assertMode('business');
context.window.location.hash = '#images';
context.window.hashchange();
assert.equal(sections.find(section => !section.hidden).id, 'images');
assertMode('aggregate');
vm.runInContext('setDocMode("unknown")', context);
assert.equal(sections.find(section => !section.hidden).id, 'images');

for (const example of initializedExamples.filter(example => example.dataset.method === 'POST')) {
  assert.equal(example.requestBody, example.originalBody);
  assert.doesNotThrow(() => JSON.parse(example.requestBody));
  context.example = example;
  vm.runInContext('renderExample(example, "curl")', context);
  assert(example.code.textContent.includes('http://api.example.test' + example.dataset.path));
  if (example.dataset.form === 'true') {
    assert(example.code.textContent.includes('-F '), 'Upload examples need multipart fields');
    assert(!example.code.textContent.includes('Content-Type: application/json'));
    assert(!example.code.textContent.includes('--data-raw'));
  }
  vm.runInContext('renderExample(example, "python")', context);
  if (example.dataset.form === 'true') {
    assert(example.code.textContent.includes('httpx'));
    assert(example.code.textContent.includes('files='));
    assert(example.code.textContent.includes('data='));
    assert(!example.code.textContent.includes('json=body'));
  } else assert(example.code.textContent.includes('json.loads('));
  if (JSON.parse(example.requestBody).stream) {
    assert(example.dataset.stream === 'true', 'SSE examples must identify their response format');
    assert(!example.code.textContent.includes('json.load(response)'), 'SSE is not one JSON response');
    assert(example.code.textContent.includes('[DONE]'));
  }
  if (example.dataset.idempotency) assert(example.code.textContent.includes('Idempotency-Key'));
  assert(!example.code.textContent.includes('undefined'));
  const syntax = spawnSync('python', ['-c', 'import ast,sys; ast.parse(sys.stdin.buffer.read().decode("utf-8"))'],
    {input: example.code.textContent, encoding: 'utf8'});
  assert.equal(syntax.status, 0, example.dataset.path + ': ' + syntax.stderr);
  vm.runInContext('renderExample(example, "json")', context);
  assert.equal(example.code.textContent, example.originalBody);
  assert.equal(example.buttons[0]['aria-pressed'], 'true');
}
const businessExample = initializedExamples.find(example => example.dataset.path === '/compat/v1/chat/completions');
assert(businessExample, 'Business Chat example must initialize without a data-json regression');
assert(JSON.parse(businessExample.requestBody).model.startsWith('flow-'));
for (const example of initializedExamples.filter(example => example.dataset.path.startsWith('/compat/v1/'))) {
  const model = JSON.parse(example.requestBody || example.originalBody).model;
  if (model) assert(!model.endsWith('-standard'), 'Business examples should use the short canonical SKU');
}

const skuRows = {children: [], replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); }};
const skuFilter = {value: 'all', addEventListener(event, callback) { this[event] = callback; }};
const skuCount = {};
const skuFixture = [
  {id: 'flow-veo-3-1-fast-8s-720p', type: 'video', locked_spec: {quality: 'fast', durationSeconds: 8, resolution: '720p', priority: 'standard'}, allowed_modes: ['text_to_video']},
  {id: 'flow-veo-3-1-fast-8s-720p-relaxed', type: 'video', locked_spec: {quality: 'fast', durationSeconds: 8, resolution: '720p', priority: 'relaxed'}, allowed_modes: ['text_to_video']}
];
const skuContext = vm.createContext({
  FALLBACK_MEDIA_SKUS: skuFixture,
  document: {
    getElementById: id => ({skuRows, skuFilter, skuCount})[id],
    createElement: () => ({children: [], textContent: '', appendChild(child) { this.children.push(child); }})
  }
});
vm.runInContext(script.slice(script.indexOf('const skuRowsElement'), script.indexOf("modeButtons.forEach(button => button.addEventListener")), skuContext);
assert.equal(skuRows.children.length, 2);
assert.equal(skuRows.children[0].children[0].children[0].textContent, skuFixture[0].id);
assert.equal(skuRows.children[0].children[2].textContent, 'FAST · 8 秒 · 720P');
assert.equal(skuRows.children[1].children[2].textContent, 'FAST · 8 秒 · 720P · Relaxed');
assert.equal(skuFixture[0].locked_spec.priority, 'standard', 'Display changes should preserve locked priority data');
assert.equal(skuFixture[1].locked_spec.priority, 'relaxed');
skuFilter.value = 'image';
skuFilter.change();
assert.equal(skuRows.children.length, 0);

const body = JSON.stringify({prompt: "a cat's window", stream: false});
const code = {textContent: body};
const languages = ['json', 'curl', 'python'].map(language => ({
  dataset: {language},
  setAttribute(name, value) { this[name] = value; }
}));
const example = {
  dataset: {path: '/v1/videos', method: 'POST', idempotency: 'true'},
  requestBody: body,
  querySelector: () => code,
  querySelectorAll: () => languages
};
context.example = example;
vm.runInContext('renderExample(example, "curl")', context);
assert(code.textContent.includes('http://api.example.test/v1/videos'));
assert(code.textContent.includes('Idempotency-Key'));
assert(code.textContent.includes("'\\''"));
vm.runInContext('renderExample(example, "python")', context);
assert(code.textContent.includes('json.loads('));
assert(code.textContent.includes('uuid.uuid4()'));
vm.runInContext('renderExample(example, "json")', context);
assert.equal(code.textContent, body);
assert.equal(languages[0]['aria-pressed'], 'true');
example.dataset = {path: '/v1/videos/video_example/content', method: 'GET', download: 'output.mp4'};
vm.runInContext('renderExample(example, "python")', context);
assert(code.textContent.includes('response.read()'));
assert(!code.textContent.includes('json.load(response)'));
example.dataset = {path: '/compat/v1/videos/video_example', method: 'DELETE'};
vm.runInContext('renderExample(example, "curl")', context);
assert(code.textContent.includes('-X DELETE'));
assert(!code.textContent.includes('--data-raw'));
vm.runInContext('renderExample(example, "python")', context);
assert(code.textContent.includes('method="DELETE"') || code.textContent.includes('method=\'DELETE\''));

copyButton = {
  textContent: '复制',
  closest: () => example,
  addEventListener(event, callback) { this.callback = callback; }
};
vm.runInContext(script.slice(script.indexOf("document.querySelectorAll('.copy')"), script.indexOf('const skuRowsElement')), context);
const embeddedHeader = {removed: false, remove() { this.removed = true; }};
context.document.querySelector = selector => selector === 'header' ? embeddedHeader : undefined;
const embeddedScript = html.match(/<script>(if\(window\.location[\s\S]*?)<\/script>/)?.[1];
assert(embeddedScript, 'Embedded pages should hide only the header');
vm.runInContext(embeddedScript, context);
assert(embeddedHeader.removed);
modeButtons[1].click();
assert.equal(sections.find(section => !section.hidden).id, 'business-chat');
assertMode('business');
(async () => {
  await copyButton.callback();
  assert.equal(clipboard, code.textContent);
  assert.equal(copyButton.textContent, '已复制');
  timers.pop()();
  assert.equal(copyButton.textContent, '复制');
  copyButton.dataset = {copyText: 'http://api.example.test/compat'};
  copyButton.closest = () => { throw new Error('Standalone copy should not look for an example'); };
  await copyButton.callback();
  assert.equal(clipboard, 'http://api.example.test/compat');
  timers.pop()();
  console.log('API guide modes, separate navigation, examples and HTTP copy passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
