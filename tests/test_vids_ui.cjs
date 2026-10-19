const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/vids.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'static/js/vids.js'), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(item => item[1]);
assert.equal(ids.length, new Set(ids).size);
for (const forbidden of ['apiKey', 'tokenId', 'accountSelect', 'idempotencyKey', 'connect', 'loadAccounts', 'sceneSelect', 'loadScenes', 'insertImage', 'imageInput', 'verifyInsert']) assert(!ids.includes(forbidden), 'No engineering controls in the product flow: ' + forbidden);
assert(!html.includes('读取可编辑图片') && !html.includes('使用已保存图片'));
assert(!script.includes('/vids/document/'));
assert(html.includes('/static/js/vids.js?v=failure-help-20261012'));
assert(html.includes('id="resultMessage" role="status" aria-live="polite"'));
for (const removed of ['sourceSelect', 'loadSources', 'videoInput', 'generatedVideoPanel', 'sourcePanel']) assert(!ids.includes(removed), 'Uploaded video modes do not read saved video sources: ' + removed);
assert(!ids.includes('newTask') && !script.includes('newTask'), 'Continuing work never requires a separate new-task action');
assert(!html.includes('vids-music'));
assert(html.includes('<summary>请求详情</summary>'));
assert(!script.includes('innerHTML'));
const prefix = '/api/admin/vids/v1';
const receipt = key => 'vids_' + require('node:crypto').createHash('sha256').update(key).digest('hex');
const json = (data, status = 200) => ({ status, ok: status >= 200 && status < 300, headers: { get: () => 'application/json' }, json: async () => data });
const binary = (type, data = 'synthetic media') => ({ status: 200, ok: true, headers: { get: name => name === 'content-type' ? type : null }, blob: async () => new Blob([data], { type }) });
const defaultStatus = { configured: true, available: { image: 1, video: 1, speech: 1 } };
function element(tag = 'div') {
  const classes = new Set();
  return { tagName: tag, value: '', textContent: '', children: [], events: {}, dataset: {}, hidden: false, disabled: false,
    classList: { add: value => classes.add(value), contains: value => classes.has(value), toggle: (value, active) => { if (active) classes.add(value); else classes.delete(value); } },
    setAttribute(name, value) { this[name] = value; }, addEventListener(name, fn) { this.events[name] = fn; }, appendChild(child) { this.children.push(child); },
    style: {}, replaceChildren() { this.children = []; }, append(...children) { this.children.push(...children); }, async click() { if (!this.disabled && !this.hidden) return this.events.click?.(); },
    input(value) { this.value = value; this.events.input?.(); }, async change(value) { this.value = value; await this.events.change?.({ target: this }); } };
}
function loadPage({ status = defaultStatus, crypto = true, embedded = false } = {}) {
  const nodes = Object.fromEntries(ids.map(id => [id, element()]));
  const kinds = ['image', 'video', 'speech'].map(kind => { const e = element('button'); e.dataset.kind = kind; return e; });
  const details = element('details'), events = {}, windowEvents = {}, requests = [], blobs = [], revoked = [], posts = [];
  let responder = async () => json(status);
  class TestURL extends URL {}
  TestURL.createObjectURL = blob => { const value = 'blob:synthetic-' + (blobs.length + 1); blobs.push({ value, blob }); return value; };
  TestURL.revokeObjectURL = value => revoked.push(value);
  const location = { origin: 'http://localhost:38000', search: embedded ? '?embedded=1' : '', href: '/vids' };
  const document = { body: { ...element('body'), scrollHeight: 550 }, getElementById: id => nodes[id], createElement: tag => element(tag),
    addEventListener: (name, fn) => { events[name] = fn; }, querySelector: () => details,
    querySelectorAll: selector => selector === '[data-kind]' ? kinds : [] };
  const window = { location, crypto: crypto ? webcrypto : {}, parent: { postMessage: (message, target) => posts.push({ message, target }) }, addEventListener: (name, fn) => { windowEvents[name] = fn; } };
  class FileReader { readAsDataURL(file) { this.result = `data:${file.type};base64,${file.base64}`; this.onload?.(); } }
  const context = vm.createContext({ document, window, URL: TestURL, URLSearchParams, TextEncoder, Blob, Uint8Array, atob, Date, Math, FileReader, TypeError,
    requestAnimationFrame: callback => callback(), fetch: async (url, options = {}) => { requests.push({ url, options }); return responder(url, options); },
    setInterval: () => { throw new Error('No automatic polling'); } });
  for (const target of [context, window]) for (const name of ['localStorage', 'sessionStorage']) Object.defineProperty(target, name, { get() { throw new Error('Requests remain in memory, without stored credentials'); } });
  Object.defineProperty(document, 'cookie', { get() { throw new Error('HttpOnly session cookies are not read in JavaScript'); } });
  vm.runInContext(script, context, { filename: 'vids.js' }); events.DOMContentLoaded();
  return { nodes, kinds, requests, location, document, windowEvents, blobs, revoked, posts, details,
    respond: fn => { responder = fn; }, ready: async () => { for (let wait = 0; wait < 30 && nodes.generate.disabled && nodes.availability.textContent.includes('读取'); wait++) await new Promise(resolve => setTimeout(resolve, 1)); },
    async choose(kind) { await kinds.find(button => button.dataset.kind === kind).click(); },
    async prepare(kind = 'image') { await this.ready(); await this.choose(kind); nodes.prompt.input('A synthetic scene'); } };
}
async function main() {
  const empty = loadPage({ status: { configured: false, available: {} }, embedded: true }); await empty.ready();
  assert.equal(empty.requests.length, 1, 'Startup only reads local pool status');
  assert.equal(empty.requests[0].url, '/api/admin/vids/status');
  assert.equal(empty.requests[0].options.credentials, 'same-origin');
  assert(empty.document.body.classList.contains('embedded'));
  assert(empty.nodes.emptyTitle.textContent.includes('尚未接入'));
  empty.nodes.prompt.input('synthetic'); await empty.nodes.generate.click(); assert.equal(empty.requests.length, 1, 'No usable account means no paid request');
  assert(empty.posts.some(post => post.message.type === 'lay4flow.vids.resize' && post.target === empty.location.origin && post.message.height < 650));

  for (const kind of ['image', 'video', 'speech']) {
    const restorable = loadPage({ status: { configured: true, available: { image: 0, video: 0, speech: 0 }, recoverable: { [kind]: 1 } } }); await restorable.prepare(kind);
    assert.equal(restorable.nodes.generate.disabled, false); assert.equal(restorable.nodes.availability.textContent, '生成前将恢复会话');
    assert.equal(restorable.nodes.accountState.hidden, false); assert(restorable.nodes.accountState.textContent.includes('恢复成功后才生成'));
    assert.equal(restorable.nodes.emptyTitle.textContent, '生成前将恢复会话'); assert.equal(restorable.requests.length, 1, 'Recoverable status never starts the Profile on page load');
    restorable.respond(async (url, opts) => kind === 'speech' ? binary('audio/wav') : kind === 'image' ? json({ data: [{ b64_json: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') }] }) : json({ id: receipt(opts.headers['Idempotency-Key']), status: 'unknown' }, 202));
    await restorable.nodes.generate.click(); assert.equal(restorable.requests[1].options.method, 'POST'); assert.equal(restorable.requests[1].url, prefix + (kind === 'speech' ? '/audio/speech' : kind === 'image' ? '/images/generations' : '/videos'));
  }
  for (const recoverable of [undefined, { image: 0 }, { image: -1 }, { image: '1' }, { image: 1.5 }]) {
    const unavailable = loadPage({ status: { configured: true, available: {}, recoverable } }); await unavailable.prepare();
    assert.equal(unavailable.nodes.generate.disabled, true); await unavailable.nodes.generate.click(); assert.equal(unavailable.requests.length, 1);
  }
  const missingBinding = loadPage({ status: { configured: false, available: {}, recoverable: { image: 1 } } }); await missingBinding.prepare(); assert.equal(missingBinding.nodes.generate.disabled, true, 'Unbound accounts are not recoverable');

  const cachedQuota = loadPage({ status: { ...defaultStatus, configured_count: 2, quota: { image: { remaining: 0, covered: 1, queried_at: '2026-10-11T14:00:00Z', stale: false }, video: { remaining: 75, covered: 2, queried_at: '2026-10-11T14:00:00Z', stale: false } } } }); await cachedQuota.ready();
  assert.equal(cachedQuota.nodes.imageQuotaRemaining.textContent, '0（部分）'); assert.equal(cachedQuota.nodes.videoQuotaRemaining.textContent, '75');
  assert(cachedQuota.nodes.imageQuotaUpdated.textContent.includes('上次读取')); assert.equal(cachedQuota.requests.length, 1, 'Quota summary comes from local status without upstream refresh');
  cachedQuota.respond(() => { throw new Error('Synthetic status outage'); }); await cachedQuota.nodes.refreshStatus.click();
  assert.equal(cachedQuota.nodes.videoQuotaRemaining.textContent, '75', 'Status failures retain cached balances'); assert(cachedQuota.nodes.videoQuotaUpdated.textContent.includes('已过期'));

  const image = loadPage(); await image.prepare();
  assert.equal(image.nodes.accountState.hidden, true, 'Ready state has no large configuration banner');
  image.respond(async () => json({ data: [{ b64_json: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') }] }));
  await image.nodes.generate.click(); const first = image.requests.at(-1);
  assert.equal(first.url, prefix + '/images/generations');
  assert.deepEqual(JSON.parse(first.options.body), { model: 'vids-image', prompt: 'A synthetic scene', n: 1, size: '1024x1024', response_format: 'b64_json' });
  assert(!first.options.headers.Authorization, 'The product uses the admin session, without API-key input');
  assert.equal(first.options.credentials, 'same-origin'); assert.equal(first.options.redirect, 'error');
  assert.equal(image.nodes.media.children[0].tagName, 'img'); assert(image.nodes.media.children[1].download.endsWith('.jpg'));
  assert.equal(image.nodes.generate.disabled, false, 'Completed content permits an independent next request');
  assert(image.nodes.requestDetails.textContent.includes(first.options.headers['Idempotency-Key']));
  await image.nodes.generate.click(); assert.notEqual(image.requests.at(-1).options.headers['Idempotency-Key'], first.options.headers['Idempotency-Key']);
  assert.equal(image.nodes.sessionHistory.children.length, 3, 'Both independent requests keep original keys in memory history');
  assert.deepEqual(image.revoked, ['blob:synthetic-1']); image.windowEvents.pagehide(); assert.equal(image.revoked.length, 2);

  const rejectedUpload = loadPage(); await rejectedUpload.prepare('video');
  rejectedUpload.respond(async (url, options) => json({ id: receipt(options.headers['Idempotency-Key']), kind: 'video', status: 'failed', error: 'upstream_http_400', error_operation: 'generate' }));
  await rejectedUpload.nodes.generate.click();
  assert(rejectedUpload.nodes.requestDetails.textContent.includes('upstream_http_400'));
  assert(rejectedUpload.nodes.requestDetails.textContent.includes('generate'));
  assert(rejectedUpload.nodes.resultMessage.textContent.includes('上游拒绝了生成提交'));
  const rejectedId = receipt(rejectedUpload.requests[1].options.headers['Idempotency-Key']);
  rejectedUpload.respond(async () => json({ id: rejectedId, kind: 'video', status: 'failed', error: 'upstream_http_400', error_operation: 'generate', upstream_error: { status: 'INVALID_ARGUMENT', reason: 'REQUEST_REFUSED', message: 'private', metadata: { key: 'secret' } } }));
  rejectedUpload.nodes.historyReceipt.value = rejectedId; await rejectedUpload.nodes.queryHistory.click();
  assert(rejectedUpload.nodes.resultMessage.textContent.includes('可能违反其条款（REQUEST_REFUSED）'));
  assert(rejectedUpload.nodes.resultMessage.textContent.includes('无法确定是提示词还是素材内容'));
  assert(rejectedUpload.nodes.resultMessage.textContent.includes('不会自动重发'));
  assert(rejectedUpload.nodes.requestDetails.textContent.includes('INVALID_ARGUMENT'));
  assert(!rejectedUpload.nodes.requestDetails.textContent.includes('private') && !rejectedUpload.nodes.requestDetails.textContent.includes('secret'));
  assert.equal(rejectedUpload.requests.filter(item => item.options.method === 'POST').length, 1, 'Reading refusal details never starts another generation');
  rejectedUpload.respond(async () => json({ id: rejectedId, kind: 'video', status: 'failed', error: 'upstream_http_400', error_operation: 'upload-finalize' }));
  rejectedUpload.nodes.historyReceipt.value = rejectedId; await rejectedUpload.nodes.queryHistory.click();
  assert(rejectedUpload.nodes.resultMessage.textContent.includes('上游拒绝了素材上传'));
  const importedRefusal = loadPage(); await importedRefusal.prepare('image');
  importedRefusal.respond(async () => json({ id: rejectedId, kind: 'video', status: 'failed', error: 'upstream_http_400', error_operation: 'generate', upstream_error: { status: 'INVALID_ARGUMENT', reason: 'REQUEST_REFUSED' } }));
  importedRefusal.nodes.historyReceipt.value = rejectedId; await importedRefusal.nodes.queryHistory.click();
  assert(importedRefusal.nodes.resultMessage.textContent.includes('可能违反其条款（REQUEST_REFUSED）'), 'Imported video receipts keep their real model regardless of the editor selection');
  assert(importedRefusal.nodes.sessionHistory.children[1].textContent.startsWith('视频生成'));
  assert.equal(importedRefusal.requests.filter(item => item.options.method === 'POST').length, 0, 'Importing a refusal remains read only');

  const outputsFor = id => Array.from({ length: 4 }, (_, index) => ({ index, metadata: { width: 1024, height: 1024 }, content_url: `${prefix}/vids/generations/${id}/content${index ? `?index=${index}` : ''}` }));
  const multiple = loadPage(); await multiple.prepare();
  assert.deepEqual(multiple.nodes.imageCount.children.map(option => option.value), ['1', '2', '3', '4']);
  await multiple.nodes.imageCount.change('4');
  assert.equal(multiple.requests.length, 1, 'Changing image count never generates or uploads');
  assert(multiple.nodes.modelBadge.textContent.includes('4 张')); assert(multiple.nodes.costHint.textContent.includes('请求 4 张'));
  assert(!multiple.nodes.costHint.textContent.includes('消耗 4'), 'Requested count is not an unverified quota estimate');
  multiple.respond(async () => json({ data: Array.from({ length: 4 }, () => ({ b64_json: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') })) }));
  await multiple.nodes.generate.click();
  assert.equal(JSON.parse(multiple.requests[1].options.body).n, 4); assert.equal(multiple.requests.length, 2, 'Four Base64 outputs use one generation POST');
  assert.equal(multiple.nodes.media.children.filter(item => item.tagName === 'img').length, 4);
  assert.equal(multiple.nodes.media.children.filter(item => item.tagName === 'a').length, 4);
  assert.equal(new Set(multiple.nodes.media.children.filter(item => item.tagName === 'a').map(item => item.download)).size, 4);
  await multiple.nodes.sessionHistory.change('0'); assert.equal(multiple.revoked.length, 4, 'Selecting saved results releases every previous Blob URL');
  await multiple.nodes.sessionHistory.change('0'); await multiple.nodes.readContent.click();
  assert.equal(multiple.requests.length, 2, 'In-page Base64 result recovery does not invent a download or regenerate');
  assert.equal(multiple.nodes.media.children.filter(item => item.tagName === 'img').length, 4);
  multiple.windowEvents.pagehide(); assert.equal(multiple.revoked.length, 8);

  const multipleUrls = loadPage(); await multipleUrls.prepare(); await multipleUrls.nodes.imageCount.change('4');
  multipleUrls.respond(async (url, options) => options.method === 'POST' ? json({ data: outputsFor(receipt(options.headers['Idempotency-Key'])).map(item => ({ url: item.content_url })) }) : binary('image/jpeg'));
  await multipleUrls.nodes.generate.click();
  assert.equal(multipleUrls.requests.filter(item => item.options.method === 'POST').length, 1);
  assert.equal(multipleUrls.requests.filter(item => item.url.includes('/content')).length, 4, 'URL generation previews all four safe local outputs');
  assert.equal(multipleUrls.nodes.media.children.filter(item => item.tagName === 'img').length, 4);
  assert.equal(multipleUrls.requests.at(-1).url, outputsFor(receipt(multipleUrls.requests[1].options.headers['Idempotency-Key']))[3].content_url);

  const multipleRecovery = loadPage(); await multipleRecovery.prepare(); await multipleRecovery.nodes.imageCount.change('4');
  multipleRecovery.respond(async (url, options) => json({ id: receipt(options.headers['Idempotency-Key']), status: 'unknown' }, 202));
  await multipleRecovery.nodes.generate.click(); const multipleId = receipt(multipleRecovery.requests[1].options.headers['Idempotency-Key']);
  multipleRecovery.respond(async () => json({ id: multipleId, kind: 'image', status: 'received' })); await multipleRecovery.nodes.checkResult.click();
  multipleRecovery.respond(async (url, options) => options.method === 'POST' ? json({ id: multipleId, kind: 'image', status: 'completed', outputs: outputsFor(multipleId) }) : binary('image/jpeg'));
  const beforeRecovery = multipleRecovery.requests.length; await multipleRecovery.nodes.recover.click();
  const recoveryRequests = multipleRecovery.requests.slice(beforeRecovery);
  assert.equal(recoveryRequests.filter(item => item.options.method === 'POST').length, 1);
  assert.equal(recoveryRequests.filter(item => !item.options.method).length, 4, 'Recovery uses one download POST and four content GETs');
  assert.equal(multipleRecovery.requests.filter(item => item.url === `${prefix}/images/generations` && item.options.method === 'POST').length, 1, 'Recovery never repeats image generation');
  assert.equal(multipleRecovery.nodes.media.children.filter(item => item.tagName === 'img').length, 4);
  multipleRecovery.windowEvents.pagehide(); assert.equal(multipleRecovery.revoked.length, 4);

  for (const query of ['?index=1&index=2', '?index=1&key=secret', '?index=4', '?index=-1', '?index=01', '?index=%31', '?index=1#secret']) {
    const unsafe = loadPage(); await unsafe.prepare(); await unsafe.nodes.imageCount.change('4');
    unsafe.respond(async (url, options) => {
      const outputs = outputsFor(receipt(options.headers['Idempotency-Key'])); outputs[3].content_url = `${prefix}/vids/generations/${receipt(options.headers['Idempotency-Key'])}/content${query}`;
      return json({ data: outputs.map(item => ({ url: item.content_url })) });
    });
    await unsafe.nodes.generate.click(); assert.equal(unsafe.requests.length, 2, 'Unsafe multi-output query is rejected before any content GET');
    assert.equal(unsafe.nodes.media.children.length, 0);
  }
  const shortImages = loadPage(); await shortImages.prepare(); await shortImages.nodes.imageCount.change('4');
  shortImages.respond(async () => json({ data: [{ b64_json: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') }] }));
  await shortImages.nodes.generate.click(); assert.equal(shortImages.nodes.media.children.length, 0, 'An incomplete set never appears as a complete four-output result');

  const video = loadPage(); await video.prepare('video'); await video.nodes.videoSeconds.change('4');
  video.respond(async (url, opts) => {
    if (url.endsWith('/videos')) { const id = receipt(opts.headers['Idempotency-Key']); return json({ id, status: 'completed', vids_status: 'completed', content_url: `${prefix}/videos/${id}/content` }); }
    return binary('video/mp4');
  });
  await video.nodes.generate.click();
  assert.deepEqual(JSON.parse(video.requests[1].options.body), { model: 'vids-video', prompt: 'A synthetic scene', seconds: 4, size: '1280x720' });
  assert.equal(video.requests.length, 3, 'Completed video automatically reads local content once');
  assert.equal(video.nodes.media.children[0].tagName, 'video');

  const speech = loadPage({ crypto: false }); await speech.prepare('speech'); speech.respond(async () => binary('audio/wav'));
  const client = fs.readFileSync(path.join(root, 'src/services/vids/client.py'), 'utf8');
  const presetNames = [...client.match(/PRESET_VOICES = \(([\s\S]*?)\n\)/)[1].matchAll(/"([^"]+)"/g)].map(match => match[1]);
  const voiceOptions = [...client.match(/PRESET_VOICE_OPTIONS = \(([\s\S]*?)\n\)/)[1].matchAll(/\{"id": "([^"]+)", "name": "([^"]+)", "description": "([^"]+)"\}/g)].map(match => ({ id: match[1], name: match[2], description: match[3] }));
  assert.equal(presetNames.length, 30); assert.equal(voiceOptions.length, 30);
  assert.deepEqual(speech.nodes.speechVoice.children.map(option => option.value).sort(), presetNames.sort());
  const sortVoices = choices => choices.sort((left, right) => left.id.localeCompare(right.id));
  assert.deepEqual(sortVoices(speech.nodes.speechVoice.children.map(option => ({ id: option.value, label: option.textContent }))), sortVoices(voiceOptions.map(voice => ({ id: voice.id, label: `${voice.name} · ${voice.description}` }))));
  assert.equal(speech.nodes.speechVoice.value, 'Puck');
  assert.equal(speech.nodes.speechVoiceHint.textContent, 'Neo · 欢快、音调适中');
  assert(speech.nodes.modelHint.textContent.includes('当前语音渠道未支持中文；可用英文等官方支持语言。'));
  assert.equal(speech.nodes.speechVoice.children[0].textContent, 'Nyla · 柔和、音调较高');
  await speech.nodes.generate.click(); assert.deepEqual(JSON.parse(speech.requests[1].options.body), { model: 'vids-speech', input: 'A synthetic scene', voice: 'Puck', response_format: 'wav' });
  assert.equal(speech.nodes.media.children[0].tagName, 'audio');
  const selectedSpeech = loadPage(); await selectedSpeech.prepare('speech');
  await selectedSpeech.nodes.speechVoice.change('Kore');
  assert.equal(selectedSpeech.requests.length, 1, 'Choosing a voice never generates');
  assert.equal(selectedSpeech.nodes.speechVoiceHint.textContent, 'Lito · 坚定、音调适中');
  await selectedSpeech.choose('image'); await selectedSpeech.choose('speech');
  assert.equal(selectedSpeech.nodes.speechVoice.value, 'Kore', 'Changing models preserves the selected voice');
  selectedSpeech.respond(async () => binary('audio/wav')); await selectedSpeech.nodes.generate.click();
  assert.deepEqual(JSON.parse(selectedSpeech.requests[1].options.body), { model: 'vids-speech', input: 'A synthetic scene', voice: 'Kore', response_format: 'wav' });
  assert.equal(selectedSpeech.requests.filter(row => row.options.method === 'POST').length, 1, 'Selected speech submits one request');
  const rejectedSpeech = loadPage(); await rejectedSpeech.prepare('speech'); await rejectedSpeech.nodes.speechVoice.change('Kore');
  rejectedSpeech.respond(async () => json({ error: { message: 'Synthetic validation error', code: 'invalid_request' } }, 400));
  await rejectedSpeech.nodes.generate.click(); const rejectedSpeechPost = rejectedSpeech.requests[1];
  await rejectedSpeech.nodes.speechVoice.change('Zephyr');
  assert.equal(rejectedSpeech.requests.length, 2, 'Changing voice after failure stays local');
  await rejectedSpeech.nodes.retry.click();
  assert.equal(rejectedSpeech.requests.at(-1).options.body, rejectedSpeechPost.options.body, 'Retry preserves the original selected voice');
  assert.equal(rejectedSpeech.requests.at(-1).options.headers['Idempotency-Key'], rejectedSpeechPost.options.headers['Idempotency-Key']);
  assert.equal(rejectedSpeech.nodes.speechVoice.value, 'Zephyr', 'Retry does not replace the next-request voice');
  assert.equal(rejectedSpeech.nodes.speechVoiceHint.textContent, 'Sani · 嘹亮、音调较高');

  const speechFailure = loadPage(); await speechFailure.prepare('speech'); await speechFailure.nodes.speechVoice.change('Algenib');
  speechFailure.respond(async (url, options) => json({ error: { message: 'Speech failed', code: 'speech_failed' }, vids_receipt: { id: receipt(options.headers['Idempotency-Key']), kind: 'speech', status: 'failed', error: 'upstream_http_400', error_operation: 'generate', content_url: null } }, 502));
  await speechFailure.nodes.generate.click();
  const failedSpeechPost = speechFailure.requests[1], failedSpeechId = receipt(failedSpeechPost.options.headers['Idempotency-Key']);
  assert.equal(JSON.parse(speechFailure.nodes.requestDetails.textContent).error, 'upstream_http_400');
  assert.equal(JSON.parse(speechFailure.nodes.requestDetails.textContent).error_operation, 'generate');
  assert.equal(JSON.parse(speechFailure.nodes.requestDetails.textContent).receipt_id, failedSpeechId);
  assert(speechFailure.nodes.resultMessage.textContent.includes('上游拒绝了生成提交'));
  assert.equal(speechFailure.requests.length, 2, 'Speech error diagnostics never automatically query or resend');
  assert.equal(speechFailure.nodes.retry.hidden, false, 'Known speech failures remain retryable');
  const speechReceipt = id => ({ id, kind: 'speech', status: 'failed', error: 'upstream_http_400', error_operation: 'generate', content_url: null });
  for (const unsafe of [
    id => ({ ...speechReceipt(id), id: 'vids_' + '0'.repeat(64) }),
    id => ({ ...speechReceipt(id), kind: 'video' }),
    id => ({ ...speechReceipt(id), status: 'untrusted-status' }),
    id => ({ ...speechReceipt(id), error_operation: 'untrusted-operation' }),
    id => ({ ...speechReceipt(id), error: 'https://private.example/token-secret' }),
    id => ({ ...speechReceipt(id), content_url: 'https://private.example/token-secret' }),
    id => ({ ...speechReceipt(id), upstream_response: 'token-secret' })
  ]) {
    const unsafeSpeechFailure = loadPage(); await unsafeSpeechFailure.prepare('speech');
    unsafeSpeechFailure.respond(async (url, options) => json({ error: { message: 'Synthetic invalid receipt', code: 'invalid_error' }, vids_receipt: unsafe(receipt(options.headers['Idempotency-Key'])) }, 502));
    await unsafeSpeechFailure.nodes.generate.click();
    const detail = JSON.parse(unsafeSpeechFailure.nodes.requestDetails.textContent);
    assert.equal(detail.receipt_id, receipt(unsafeSpeechFailure.requests[1].options.headers['Idempotency-Key']));
    assert.equal(detail.status, 'unknown'); assert.equal(detail.error_operation, null);
    assert(!unsafeSpeechFailure.nodes.requestDetails.textContent.includes('token-secret'));
    assert.equal(unsafeSpeechFailure.requests.length, 2, 'Unsafe diagnostic receipts never make additional requests');
  }
  speechFailure.respond(async () => json({ error: { message: 'Synthetic invalid receipt', code: 'invalid_error' }, vids_receipt: { ...speechReceipt('vids_' + '0'.repeat(64)), upstream_response: 'token-secret' } }, 502));
  await speechFailure.nodes.retry.click();
  const retryDetail = JSON.parse(speechFailure.nodes.requestDetails.textContent);
  assert.equal(retryDetail.error, 'upstream_http_400'); assert.equal(retryDetail.error_operation, 'generate'); assert.equal(retryDetail.receipt_id, failedSpeechId);
  assert.equal(speechFailure.requests[2].options.body, failedSpeechPost.options.body);
  assert.equal(speechFailure.requests[2].options.headers['Idempotency-Key'], failedSpeechPost.options.headers['Idempotency-Key']);
  assert.equal(speechFailure.requests.length, 3, 'Retry happens only on explicit click with the original request');

  for (const status of ['generating', 'received', 'unknown', 'no_result']) {
    const pendingSpeech = loadPage(); await pendingSpeech.prepare('speech');
    pendingSpeech.respond(async (url, options) => json({ error: { message: 'Generic speech result error', code: status === 'no_result' ? 'speech_failed' : 'speech_not_ready' }, vids_receipt: { id: receipt(options.headers['Idempotency-Key']), kind: 'speech', status, error: status === 'no_result' ? 'no_result' : null, content_url: null } }, status === 'no_result' ? 502 : 409));
    await pendingSpeech.nodes.generate.click(); assert.equal(JSON.parse(pendingSpeech.nodes.requestDetails.textContent).status, status);
    assert.equal(pendingSpeech.requests.length, 2, 'Pending receipts stay on the original task without polling');
    assert.equal(pendingSpeech.nodes.retry.hidden, true);
  }
  const structuredSpeechFailure = loadPage(); await structuredSpeechFailure.prepare('speech');
  structuredSpeechFailure.respond(async (url, options) => json({ error: { message: 'Speech failed', code: 'speech_failed' }, vids_receipt: { ...speechReceipt(receipt(options.headers['Idempotency-Key'])), upstream_error: { status: 'INVALID_ARGUMENT', reason: 'INPUT_REJECTED', message: 'token-secret', metadata: { key: 'token-secret' } } } }, 502));
  await structuredSpeechFailure.nodes.generate.click();
  assert.deepEqual(JSON.parse(structuredSpeechFailure.nodes.requestDetails.textContent).upstream_error, { status: 'INVALID_ARGUMENT', reason: 'INPUT_REJECTED' });
  assert(!structuredSpeechFailure.nodes.requestDetails.textContent.includes('token-secret'));
  assert(!structuredSpeechFailure.nodes.resultMessage.textContent.includes('不支持这段文本的语言'), 'Other INVALID_ARGUMENT reasons are not mislabeled as unsupported language');
  assert.equal(structuredSpeechFailure.requests.length, 2);
  const languageSpeechFailure = loadPage(); await languageSpeechFailure.prepare('speech'); await languageSpeechFailure.nodes.speechVoice.change('Algenib');
  languageSpeechFailure.nodes.prompt.input('合成中文测试');
  assert.equal(languageSpeechFailure.nodes.generate.disabled, false, 'The UI does not reject CJK scripts before the upstream language check');
  languageSpeechFailure.respond(async (url, options) => json({ error: { message: 'Speech failed', code: 'speech_failed' }, vids_receipt: { ...speechReceipt(receipt(options.headers['Idempotency-Key'])), upstream_error: { status: 'INVALID_ARGUMENT', reason: 'LANGUAGE_NOT_SUPPORTED' } } }, 502));
  await languageSpeechFailure.nodes.generate.click();
  assert.equal(languageSpeechFailure.nodes.resultMessage.textContent, '当前 Vids 语音不支持这段文本的语言，请改用英文等受支持语言。原任务已保留，不会自动重发。');
  assert.equal(languageSpeechFailure.requests.filter(row => row.options.method === 'POST').length, 1);
  assert.equal(languageSpeechFailure.requests.filter(row => row.url.startsWith(prefix)).length, 1, 'Language errors do not trigger receipt queries, translations or retries');
  assert.deepEqual(JSON.parse(languageSpeechFailure.requests[1].options.body), { model: 'vids-speech', input: '合成中文测试', voice: 'Algenib', response_format: 'wav' });
  assert.deepEqual(JSON.parse(languageSpeechFailure.nodes.requestDetails.textContent).upstream_error, { status: 'INVALID_ARGUMENT', reason: 'LANGUAGE_NOT_SUPPORTED' });
  const invalidStructuredSpeech = loadPage(); await invalidStructuredSpeech.prepare('speech');
  invalidStructuredSpeech.respond(async (url, options) => json({ error: { message: 'Speech failed', code: 'speech_failed' }, vids_receipt: { ...speechReceipt(receipt(options.headers['Idempotency-Key'])), upstream_error: { status: 'https://private.example/token-secret', reason: 'token-secret' } } }, 502));
  await invalidStructuredSpeech.nodes.generate.click();
  assert.equal(JSON.parse(invalidStructuredSpeech.nodes.requestDetails.textContent).upstream_error, null);
  assert.equal(JSON.parse(invalidStructuredSpeech.nodes.requestDetails.textContent).error, 'upstream_http_400', 'Bad optional summaries cannot erase a safe failure code');
  assert(!invalidStructuredSpeech.nodes.requestDetails.textContent.includes('token-secret'));

  const actionable = (page, patterns, label) => {
    const message = page.nodes.resultMessage.textContent;
    for (const pattern of patterns) assert.match(message, pattern, `${label}: ${message}`);
  };
  const receiptHintCases = [
    ['failed', 'upstream_http_400', 'upload-start', null, [/上传/, /格式|MP4/, /大小|体积|MiB/, /时长/, /更换|重新选择/]],
    ['failed', 'upstream_http_400', 'upload-finalize', null, [/上传/, /格式|MP4/, /大小|体积|MiB/, /时长/, /更换|重新选择/]],
    ['failed', 'upstream_http_400', 'generate', null, [/生成/, /提示词/, /参数|规格/, /检查|调整|修改/]],
    ['failed', 'upstream_http_401', 'generate', { status: 'UNAUTHENTICATED' }, [/账号|账户/, /管理|Vids 账号/, /登录|授权|会话/, /同一|原账号|原账户/]],
    ['failed', 'upstream_http_403', 'generate', { status: 'PERMISSION_DENIED' }, [/账号|账户/, /管理|Vids 账号/, /权限|登录|授权|会话/, /同一|原账号|原账户/]],
    ['failed', 'upstream_http_403', 'generate', { status: 'RESOURCE_EXHAUSTED' }, [/额度/, /Vids/, /检查|读取|查询/]],
    ['failed', 'upstream_http_429', 'generate', { status: 'RESOURCE_EXHAUSTED' }, [/额度/, /Vids/, /检查|读取|查询/]],
    ['download_failed', 'content_download_failed', null, null, [/接受|生成/, /下载/, /恢复下载/, /不.*重新生成|不会.*重新生成/]],
    ['received', null, null, null, [/接受/, /下载/, /恢复下载/, /不.*重新生成|不会.*重新生成/]],
    ['no_result', 'no_result', null, null, [/没有|未返回|无/, /成品|媒体|结果/, /提示词/, /素材|原视频|图片/, /检查|修改|调整/]],
    ['specification_mismatch', 'upstream_video_specification', null, null, [/尺寸|分辨率|比例/, /时长/, /参数|规格/]],
    ['unknown', 'acceptance_unknown', null, null, [/检查结果/, /原任务|原收据/, /重复|重发|再次生成/]],
  ];
  for (const [status, error, operation, upstream, patterns] of receiptHintCases) {
    const outcome = loadPage(); await outcome.prepare('video');
    outcome.respond(async (url, options) => json({ id: receipt(options.headers['Idempotency-Key']), kind: 'video', status, error, ...(operation ? { error_operation: operation } : {}), ...(upstream ? { upstream_error: upstream } : {}) }));
    await outcome.nodes.generate.click();
    actionable(outcome, patterns, `${status}/${error}/${operation}`);
    assert.equal(JSON.parse(outcome.nodes.requestDetails.textContent).error, error);
    assert.equal(outcome.requests.filter(row => row.options.method === 'POST').length, 1, 'An actionable hint never auto retries');
    if (['received', 'download_failed'].includes(status)) assert.equal(outcome.nodes.recover.hidden, false);
  }
  for (const kind of ['image', 'video', 'speech']) {
    const refusalHint = loadPage(); await refusalHint.prepare(kind);
    refusalHint.respond(async (url, options) => json({ error: { message: 'Synthetic refusal', code: kind === 'speech' ? 'speech_failed' : 'image_failed' }, vids_receipt: { id: receipt(options.headers['Idempotency-Key']), kind, status: 'failed', error: 'upstream_http_400', error_operation: 'generate', content_url: null, upstream_error: { status: 'INVALID_ARGUMENT', reason: 'REQUEST_REFUSED' } } }, 502));
    await refusalHint.nodes.generate.click();
    actionable(refusalHint, [/REQUEST_REFUSED/, /条款|拒绝/, /触发.*未|具体.*未|没有.*具体/, /提示词/, /素材|内容/, /修改|更换|调整/], `${kind} refusal`);
    assert(!/账号.*封禁|永久禁止|必然违规/.test(refusalHint.nodes.resultMessage.textContent), 'A refusal does not invent a fixed ban');
    assert.equal(refusalHint.requests.filter(row => row.options.method === 'POST').length, 1);
  }
  for (const [code, httpStatus, patterns] of [
    ['invalid_request', 400, [/提示词|参数/, /素材|文件/, /检查|修改|调整/]],
    ['quota_exhausted', 429, [/额度/, /Vids/, /检查|读取|查询/]],
    ['upstream_http_403', 403, [/管理|Vids 账号/, /权限|登录|授权|会话/, /同一|原账号|原账户/]],
    ['busy', 429, [/等待|稍后/, /原任务|检查结果/]],
    ['session_authentication_failed', 503, [/管理|Vids 账号/, /登录|会话|接入/, /修复|恢复|重新/]],
    ['session_persistence_failed', 503, [/管理/, /会话|接入/, /修复|恢复|重新/]],
  ]) {
    const errorHint = loadPage(); await errorHint.prepare('video');
    errorHint.respond(async () => json({ error: { code, message: 'Synthetic backend error' } }, httpStatus));
    await errorHint.nodes.generate.click(); actionable(errorHint, patterns, code);
    assert.equal(errorHint.requests.filter(row => row.options.method === 'POST').length, 1);
  }
  const networkHint = loadPage(); await networkHint.prepare('video');
  networkHint.respond(async () => { throw new Error('Synthetic connection lost'); }); await networkHint.nodes.generate.click();
  actionable(networkHint, [/网络|连接/, /检查结果/, /原任务|原收据/, /重复|重发|再次生成/], 'network interruption');
  const preservedUnknown = JSON.parse(networkHint.nodes.requestDetails.textContent);
  networkHint.respond(async () => json({ error: { code: 'upstream_http_503', message: 'Synthetic query unavailable' } }, 503));
  await networkHint.nodes.checkResult.click();
  const afterQueryFailure = JSON.parse(networkHint.nodes.requestDetails.textContent);
  assert.equal(afterQueryFailure.status, preservedUnknown.status, 'A read failure cannot change the saved generation outcome');
  assert.equal(afterQueryFailure.error, preservedUnknown.error, 'A read failure cannot replace the saved generation diagnostic');
  actionable(networkHint, [/查询|检查/, /连接|网络|稍后/, /原任务|原收据/], 'query failure');
  networkHint.respond(async () => { throw new TypeError('Synthetic fetch error'); }); await networkHint.nodes.checkResult.click();
  assert.equal(JSON.parse(networkHint.nodes.requestDetails.textContent).status, preservedUnknown.status);
  assert.equal(JSON.parse(networkHint.nodes.requestDetails.textContent).error, preservedUnknown.error);
  actionable(networkHint, [/查询|检查/, /网络|代理/, /重试/, /不会重新生成/], 'query fetch failure');
  networkHint.respond(async () => json({ id: 'vids_' + '0'.repeat(64), kind: 'video', status: 'unknown' }));
  await networkHint.nodes.checkResult.click();
  actionable(networkHint, [/收据/, /不匹配/, /原任务/], 'query receipt mismatch');
  assert(!networkHint.nodes.resultMessage.textContent.includes('检查网络'), 'Protocol failures must not be mislabeled as connection failures');
  assert.equal(JSON.parse(networkHint.nodes.requestDetails.textContent).error, preservedUnknown.error);
  assert.equal(networkHint.requests.filter(row => row.options.method === 'POST').length, 1);
  const authenticationHint = loadPage(); await authenticationHint.prepare('video');
  authenticationHint.respond(async () => json({ error: { code: 'invalid_api_key', message: 'Synthetic expired login' } }, 401));
  await authenticationHint.nodes.generate.click();
  actionable(authenticationHint, [/登录/, /重新/], 'expired admin login');
  assert.equal(authenticationHint.location.href, '/login');
  const previewFailure = loadPage(); await previewFailure.prepare('video');
  previewFailure.respond(async (url, options) => url.endsWith('/content') ? json({ error: { code: 'content_download_failed', message: 'Synthetic unavailable media' } }, 503) : json({ id: receipt(options.headers['Idempotency-Key']), kind: 'video', status: 'completed', error: null, content_url: `${prefix}/vids/generations/${receipt(options.headers['Idempotency-Key'])}/content` }));
  await previewFailure.nodes.generate.click();
  assert.equal(JSON.parse(previewFailure.nodes.requestDetails.textContent).status, 'completed');
  assert.equal(JSON.parse(previewFailure.nodes.requestDetails.textContent).error, null, 'Preview failure preserves the successful generation diagnostic');
  assert.equal(previewFailure.nodes.readContent.hidden, false);
  actionable(previewFailure, [/已完成/, /读取成品/, /重试|稍后/], 'completed preview failure');
  previewFailure.respond(async () => json({ error: { code: 'content_download_failed', message: 'Synthetic unavailable media again' } }, 503));
  await previewFailure.nodes.readContent.click();
  assert.equal(JSON.parse(previewFailure.nodes.requestDetails.textContent).status, 'completed');
  assert.equal(JSON.parse(previewFailure.nodes.requestDetails.textContent).error, null);
  actionable(previewFailure, [/读取成品/, /重试|稍后/], 'completed preview read failure');
  assert.equal(previewFailure.requests.filter(row => row.options.method === 'POST').length, 1);

  const pending = loadPage(); await pending.prepare('video'); let resolvePost;
  pending.respond((url, opts) => new Promise(resolve => { resolvePost = () => resolve(json({ id: receipt(opts.headers['Idempotency-Key']), status: 'unknown', vids_status: 'unknown' }, 202)); }));
  const click = pending.nodes.generate.click(); for (let wait = 0; wait < 50 && !resolvePost; wait++) await new Promise(resolve => setTimeout(resolve, 1));
  assert.equal(pending.nodes.prompt.disabled, true); assert(pending.kinds.every(button => button.disabled));
  await pending.choose('speech'); assert.equal(pending.nodes.editorTitle.textContent, '视频生成', 'The active network request still blocks model switches');
  await pending.nodes.generate.click(); assert.equal(pending.requests.length, 2, 'Double clicks submit once'); resolvePost(); await click;
  const key = pending.requests[1].options.headers['Idempotency-Key'], id = receipt(key);
  assert.equal(pending.nodes.generate.disabled, true); assert.equal(pending.nodes.retry.hidden, true);
  assert.equal(pending.nodes.prompt.disabled, false); assert.equal(pending.nodes.sessionHistory.disabled, false); assert.equal(pending.nodes.queryHistory.disabled, false);
  assert(pending.kinds.every(button => !button.disabled), 'An unresolved receipt cannot lock model switches');
  assert.equal(pending.nodes.videoSeconds.disabled, false);
  await pending.nodes.videoSeconds.change('4'); assert.equal(pending.nodes.generate.disabled, false, 'Changing parameters permits independent work');
  await pending.nodes.videoSeconds.change('3'); assert.equal(pending.nodes.generate.disabled, true, 'Restoring the unresolved request keeps duplicate protection');
  await pending.nodes.sessionHistory.change('0');
  assert.equal(pending.requests.length, 2, 'Selecting unresolved history never makes a new request');
  assert.equal(pending.nodes.checkResult.hidden, false); assert(pending.nodes.resultMessage.textContent.includes('暂不再次生成'));
  await pending.nodes.generate.click(); await pending.nodes.retry.click(); assert.equal(pending.requests.length, 2, 'Unknown acceptance never resubmits');
  pending.respond(async () => json({ id, kind: 'video', status: 'received' })); await pending.nodes.checkResult.click();
  assert.equal(pending.nodes.recover.hidden, false);
  pending.respond(async url => url.endsWith('/download') ? json({ id, kind: 'video', status: 'completed', content_url: `${prefix}/vids/generations/${id}/content` }) : binary('video/mp4'));
  await pending.nodes.recover.click(); assert.equal(pending.requests.at(-2).url, `${prefix}/vids/generations/${id}/download`); assert.equal(pending.requests.at(-2).options.method, 'POST');
  assert.equal(pending.nodes.media.children[0].tagName, 'video');

  const detached = loadPage(); await detached.prepare('video');
  detached.respond(async (url, options) => json({ id: receipt(options.headers['Idempotency-Key']), status: 'unknown' }, 202));
  await detached.nodes.generate.click(); const detachedId = receipt(detached.requests[1].options.headers['Idempotency-Key']);
  assert(detached.nodes.resultMessage.textContent.includes('直接修改内容或切换模型'));
  assert.equal(detached.requests.length, 2, 'Returned unknown never starts another request automatically');
  assert.equal(detached.nodes.prompt.disabled, false); assert.equal(detached.nodes.sessionHistory.disabled, false);
  await detached.choose('speech');
  detached.nodes.prompt.input('An independent speech request');
  detached.respond(async () => binary('audio/wav')); await detached.nodes.generate.click();
  assert.equal(detached.requests.at(-1).url, prefix + '/audio/speech');
  assert.notEqual(detached.requests.at(-1).options.headers['Idempotency-Key'], detached.requests[1].options.headers['Idempotency-Key']);
  await detached.nodes.sessionHistory.change('1');
  assert.equal(JSON.parse(detached.nodes.requestDetails.textContent).receipt_id, detachedId);
  assert.equal(detached.nodes.prompt.disabled, false); assert.equal(detached.nodes.retry.hidden, true);
  detached.respond(async () => json({ id: detachedId, kind: 'video', status: 'unknown' }));
  await detached.nodes.checkResult.click();
  assert.equal(detached.requests.at(-1).url, `${prefix}/vids/generations/${detachedId}`);
  assert.equal(detached.requests.at(-1).options.method, undefined);
  assert.equal(detached.requests.filter(item => item.options.method === 'POST').length, 2, 'History inspection cannot regenerate the unknown task');
  await detached.choose('video'); detached.nodes.prompt.input('A synthetic scene');
  assert.equal(detached.nodes.generate.disabled, true, 'An unchanged unresolved request is blocked even after another model completed');
  detached.nodes.prompt.input('A different scene');
  assert.equal(detached.nodes.generate.disabled, false, 'Changing the prompt immediately permits independent work');

  for (const failure of [() => { throw new Error('network interruption'); }, () => json({}, 500)]) {
    const uncertain = loadPage(); await uncertain.prepare(); uncertain.respond(failure); await uncertain.nodes.generate.click();
    assert.equal(uncertain.nodes.generate.disabled, true); assert.equal(uncertain.nodes.retry.hidden, true); assert.match(uncertain.nodes.resultMessage.textContent, /尚未确认|未收到可确认/);
  }
  for (const code of ['image_not_ready', 'speech_not_ready']) {
    const notReady = loadPage(); await notReady.prepare(code === 'speech_not_ready' ? 'speech' : 'image');
    notReady.respond(async () => json({ error: { message: 'Result pending without a status phrase', code } }, 409)); await notReady.nodes.generate.click();
    assert.equal(notReady.nodes.generate.disabled, true); assert.equal(notReady.nodes.retry.hidden, true, 'Not-ready codes cannot be treated as a terminal failure');
  }
  const rejected = loadPage(); await rejected.prepare(); rejected.respond(async () => json({ error: { message: 'Synthetic validation error', code: 'invalid_request' } }, 400));
  await rejected.nodes.generate.click(); const rejectedPost = rejected.requests[1]; assert.equal(rejected.nodes.retry.hidden, false); await rejected.nodes.retry.click();
  assert.equal(rejected.requests.at(-1).options.body, rejectedPost.options.body); assert.equal(rejected.requests.at(-1).options.headers['Idempotency-Key'], rejectedPost.options.headers['Idempotency-Key']);

  for (const bad of [id => `https://evil.test${prefix}/videos/${id}/content`, id => `${prefix}/videos/${id}/content?key=secret`, id => `/api/admin/vids/accounts/4/v1/videos/${id}/content`, () => `${prefix}/videos/vids_${'f'.repeat(64)}/content`]) {
    const boundary = loadPage(); await boundary.prepare('video'); boundary.respond(async (url, opts) => json({ id: receipt(opts.headers['Idempotency-Key']), status: 'completed', content_url: bad(receipt(opts.headers['Idempotency-Key'])) }));
    await boundary.nodes.generate.click(); assert.equal(boundary.requests.length, 2, 'Unsafe success links never receive cookies'); assert.equal(boundary.nodes.media.children.length, 0); assert.equal(boundary.nodes.retry.hidden, true);
  }
  const historic = loadPage(); await historic.prepare(); const historicId = 'vids_' + 'a'.repeat(64);
  historic.nodes.historyReceipt.value = historicId; historic.respond(async url => url.endsWith('/content') ? binary('image/jpeg') : json({ id: historicId, kind: 'image', status: 'completed', content_url: `${prefix}/vids/generations/${historicId}/content` }));
  await historic.nodes.queryHistory.click(); assert.equal(historic.nodes.media.children[0].tagName, 'img');
  const invalid = loadPage(); await invalid.prepare('speech'); invalid.nodes.prompt.input('x'.repeat(4097)); await invalid.nodes.generate.click(); assert.equal(invalid.requests.length, 1);
  invalid.nodes.historyReceipt.value = '../../foreign'; await invalid.nodes.queryHistory.click(); assert.equal(invalid.requests.length, 1);
  const jpeg = { name: 'synthetic.jpg', type: 'image/jpeg', size: 4, base64: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') };
  const vertical = loadPage(); await vertical.prepare('video');
  assert.equal(vertical.nodes.generationMode.children.length, 6); await vertical.nodes.videoAspect.change('720x1280');
  assert.deepEqual(vertical.nodes.videoSeconds.children.map(option => option.value), ['3','4','5','6','7','8','9','10']);
  await vertical.nodes.videoSeconds.change('10'); await vertical.nodes.videoResolution.change('1080x1920'); await vertical.nodes.videoShot.change('montage');
  assert.equal(vertical.nodes.videoAspect.value, '1080x1920', 'Changing resolution preserves the selected aspect');
  vertical.respond(async (url, opts) => json({ id: receipt(opts.headers['Idempotency-Key']), status: 'unknown' }, 202)); await vertical.nodes.generate.click();
  assert.deepEqual(JSON.parse(vertical.requests[1].options.body), { model: 'vids-video', prompt: 'A synthetic scene', seconds: 10, size: '1080x1920', shot_mode: 'montage' });

  for (const aspect of ['16:9', '9:16']) {
    const wideImage = loadPage(); await wideImage.prepare(); await wideImage.nodes.imageAspect.change(aspect); await wideImage.nodes.imageStyle.change('watercolor');
    wideImage.respond(async () => json({ data: [{ b64_json: jpeg.base64 }] })); await wideImage.nodes.generate.click();
    assert.deepEqual(JSON.parse(wideImage.requests[1].options.body), { model: 'vids-image', prompt: 'A synthetic scene', n: 1, size: 'auto', response_format: 'b64_json', aspect_ratio: aspect, style: 'watercolor' });
    assert.equal(wideImage.nodes.imageStyle.children.length, 7);
  }
  const seven = loadPage(); await seven.prepare('video'); await seven.nodes.generationMode.change('ingredients');
  seven.nodes.referenceFiles.files = Array.from({length: 7}, (_, index) => ({...jpeg, name: `reference-${index}.jpg`})); await seven.nodes.referenceFiles.change('');
  assert.equal(seven.nodes.referencePreview.children.length, 7); assert.equal(seven.nodes.generate.disabled, false);
  await seven.nodes.videoSeconds.change('8'); await seven.nodes.videoResolution.change('1920x1080');
  seven.respond(async (url, opts) => json({id: receipt(opts.headers['Idempotency-Key']), status: 'unknown'}, 202)); await seven.nodes.generate.click();
  assert.equal(JSON.parse(seven.requests.at(-1).options.body).images_base64.length, 7);
  const mp4 = { name: 'synthetic.mp4', type: 'video/mp4', size: 24, base64: Buffer.from([0,0,0,24,...Buffer.from('ftypisom'),0,0,0,0,...Buffer.from('isommp42')]).toString('base64') };
  const upload = loadPage(); await upload.prepare('video'); await upload.nodes.generationMode.change('edit');
  assert.equal(upload.nodes.videoUploadPanel.hidden, false);
  upload.nodes.videoFile.files = [mp4]; await upload.nodes.videoFile.change('');
  assert.equal(upload.nodes.generate.disabled, false); assert.equal(upload.requests.length, 1, 'Selecting a local MP4 never uploads');
  upload.respond(async (url, opts) => json({id: receipt(opts.headers['Idempotency-Key']), status: 'unknown'}, 202)); await upload.nodes.generate.click();
  assert.deepEqual(JSON.parse(upload.requests[1].options.body), {kind: 'video', mode: 'edit', prompt: 'A synthetic scene', seconds: 3, size: '1280x720', video_base64: mp4.base64});
  assert(!upload.nodes.requestDetails.textContent.includes(mp4.base64));
  assert.equal(upload.requests.length, 2, 'An MP4 edit sends one generation API request');
  assert.equal(upload.nodes.videoFile.disabled, false); assert.equal(upload.nodes.referenceFiles.disabled, false);
  upload.nodes.videoFile.files = [{ ...mp4, name: 'same-bytes.mp4' }]; await upload.nodes.videoFile.change('');
  assert.equal(upload.nodes.generate.disabled, true, 'Renaming the same material cannot bypass duplicate protection');
  upload.nodes.videoFile.files = [{ ...mp4, base64: Buffer.concat([Buffer.from(mp4.base64, 'base64'), Buffer.from([0])]).toString('base64') }]; await upload.nodes.videoFile.change('');
  assert.equal(upload.nodes.generate.disabled, false, 'Changing material permits an independent request without clearing history');
  assert.equal(upload.requests.length, 2, 'Editing parameters and material never submits automatically');

  for (const mode of ['edit', 'extend', 'upscale']) {
    const page = loadPage(); await page.prepare('video'); await page.nodes.generationMode.change(mode);
    assert(page.nodes.videoUploadHint.textContent.includes(mode === 'upscale' ? '3–10' : '3–60'));
    page.nodes.videoFile.files = [mp4]; await page.nodes.videoFile.change('');
    assert.equal(page.nodes.generate.disabled, false); assert.equal(page.requests.length, 1);
    if (mode === 'upscale') {
      assert.equal(page.nodes.videoSpec.hidden, true); assert(page.nodes.videoUploadHint.textContent.includes('1080p'));
      assert(page.nodes.videoUploadHint.textContent.includes('保留原视频方向和时长'));
    } else {
      assert.equal(page.nodes.videoSpec.hidden, false); await page.nodes.videoAspect.change('720x1280');
      await page.nodes.videoResolution.change('1080x1920'); await page.nodes.videoSeconds.change('8');
      if (mode === 'extend') assert.equal(page.nodes.videoSecondsLabel.textContent, '追加时长');
    }
    page.respond(async (url, opts) => json({ id: receipt(opts.headers['Idempotency-Key']), status: 'unknown' }, 202));
    await page.nodes.generate.click();
    assert.deepEqual(JSON.parse(page.requests[1].options.body), { kind: 'video', mode, prompt: 'A synthetic scene', ...(mode === 'upscale' ? {} : { seconds: 8, size: '1080x1920' }), video_base64: mp4.base64 });
    assert.equal(page.requests.length, 2, 'Upload video workflow sends exactly one generation request');
    assert(!page.requests.some(row => /\/videos\?/.test(row.url)), 'Video workflow never reads existing videos');
    assert(!page.nodes.requestDetails.textContent.includes(mp4.base64));
    await page.nodes.generate.click(); await page.nodes.retry.click(); assert.equal(page.requests.length, 2, 'Unknown upload acceptance never resends');
  }

  for (const mode of ['animate', 'ingredients', 'edit', 'extend', 'upscale']) {
    const page = loadPage(); await page.prepare('video'); await page.nodes.generationMode.change(mode);
    assert.equal(page.requests.length, 1, 'Changing modes never uploads or generates');
    assert.equal(page.nodes.generate.disabled, true, 'Required sources are blocked until supplied');
    assert.equal(page.nodes.videoUploadPanel.hidden, !['edit', 'extend', 'upscale'].includes(mode));
    if (['edit', 'extend', 'upscale'].includes(mode)) { page.nodes.videoFile.files = [mp4]; await page.nodes.videoFile.change(''); assert.equal(page.requests.length, 1, 'Selecting a local MP4 never uploads'); }
    if (['animate', 'ingredients'].includes(mode)) { page.nodes.referenceFiles.files = [jpeg]; await page.nodes.referenceFiles.change(''); assert.equal(page.nodes.referencePreview.children.length, 1); }
    page.respond(async (url, opts) => json({ id: receipt(opts.headers['Idempotency-Key']), status: 'unknown' }, 202)); await page.nodes.generate.click();
    const post = page.requests.at(-1); assert.equal(post.url, prefix + '/vids/generations');
    const expected = { kind: 'video', mode, prompt: 'A synthetic scene', ...(mode === 'upscale' ? {} : { seconds: 3, size: '1280x720' }) };
    if (['edit', 'extend', 'upscale'].includes(mode)) expected.video_base64 = mp4.base64;
    if (mode === 'animate') expected.image_base64 = jpeg.base64;
    if (mode === 'ingredients') expected.images_base64 = [jpeg.base64];
    assert.deepEqual(JSON.parse(post.options.body), expected);
    assert(!page.nodes.requestDetails.textContent.includes(jpeg.base64), 'Details omit image byte payloads');
  }
  const direct = loadPage(); await direct.prepare(); await direct.nodes.generationMode.change('edit');
  assert.equal(direct.nodes.imageCountField.hidden, true, 'Image editing keeps the single-output control hidden');
  assert.equal(direct.nodes.imageUploadPanel.hidden, false);
  assert.equal(direct.nodes.generate.disabled, true, 'Image edit requires a local original');
  direct.nodes.imageFile.files = [jpeg]; await direct.nodes.imageFile.change('');
  assert.equal(direct.nodes.imagePreview.children[0].tagName, 'img'); assert.equal(direct.nodes.imagePreview.children[1].textContent, jpeg.name);
  assert.equal(direct.requests.length, 1, 'Selecting the original stays local'); assert.equal(direct.nodes.generate.disabled, false);
  let finishEdit;
  direct.respond(async (url, opts) => url.endsWith('/content') ? binary('image/jpeg') : new Promise(resolve => {
    finishEdit = () => resolve(json({ id: receipt(opts.headers['Idempotency-Key']), status: 'completed', content_url: `${prefix}/vids/generations/${receipt(opts.headers['Idempotency-Key'])}/content` }));
  }));
  const directClick = direct.nodes.generate.click();
  for (let wait = 0; wait < 50 && !finishEdit; wait++) await new Promise(resolve => setTimeout(resolve, 1));
  await direct.nodes.generate.click(); assert.equal(direct.requests.length, 2, 'Double clicks cannot repeat the image API request');
  assert.equal(direct.nodes.prompt.disabled, true); assert.equal(direct.nodes.imageFile.disabled, true);
  finishEdit(); await directClick;
  const directPost = direct.requests[1], directId = receipt(directPost.options.headers['Idempotency-Key']);
  assert.deepEqual(direct.requests.slice(1).map(row => [row.url, row.options.method || 'GET']), [
    [`${prefix}/vids/generations`, 'POST'], [`${prefix}/vids/generations/${directId}/content`, 'GET']
  ], 'One click sends the original and prompt in one API request, followed only by local result preview');
  assert.deepEqual(JSON.parse(directPost.options.body), { kind: 'image', mode: 'edit', prompt: 'A synthetic scene', image_base64: jpeg.base64 });
  assert(!direct.requests.some(row => /accounts\/[0-9]+/.test(row.url)), 'The product never asks the user to select an account');
  assert.equal(direct.nodes.media.children[0].tagName, 'img'); assert.equal(direct.nodes.generate.disabled, false);
  assert(!direct.nodes.requestDetails.textContent.includes(jpeg.base64), 'Request details omit original image bytes');
  const oldPreview = direct.nodes.imagePreview.children[0].src;
  direct.nodes.imageFile.files = []; await direct.nodes.imageFile.change('');
  assert.equal(direct.nodes.imagePreview.children[0].src, oldPreview, 'Canceling the picker preserves the original');
  direct.nodes.imageFile.files = [{ ...jpeg, name: 'replacement.jpg' }]; await direct.nodes.imageFile.change('');
  assert(direct.revoked.includes(oldPreview)); assert.equal(direct.nodes.generate.disabled, false);
  direct.windowEvents.pagehide(); assert(direct.revoked.includes(direct.nodes.imagePreview.children[0].src));

  for (const failure of [
    opts => json({ id: receipt(opts.headers['Idempotency-Key']), status: 'unknown' }, 202),
    () => { throw new Error('Synthetic network interruption'); },
    () => json({}, 500)
  ]) {
    const uncertain = loadPage(); await uncertain.prepare(); await uncertain.nodes.generationMode.change('edit');
    uncertain.nodes.imageFile.files = [jpeg]; await uncertain.nodes.imageFile.change('');
    uncertain.respond(async (url, opts) => failure(opts)); await uncertain.nodes.generate.click();
    const original = uncertain.requests[1], originalId = receipt(original.options.headers['Idempotency-Key']);
    assert.equal(uncertain.requests.length, 2); assert.equal(uncertain.nodes.generate.disabled, true);
    assert.equal(uncertain.nodes.checkResult.hidden, false); assert.equal(uncertain.nodes.retry.hidden, true);
    await uncertain.nodes.generate.click(); await uncertain.nodes.retry.click();
    assert.equal(uncertain.requests.length, 2, 'Unknown image acceptance never resends the original or generation');
    uncertain.respond(async () => json({ id: originalId, kind: 'image', status: 'received' })); await uncertain.nodes.checkResult.click();
    assert.equal(uncertain.requests.at(-1).url, `${prefix}/vids/generations/${originalId}`);
    assert.equal(uncertain.requests.at(-1).options.method, undefined, 'Checking the original receipt is read only');
    uncertain.respond(async url => url.endsWith('/download') ? json({ id: originalId, kind: 'image', status: 'completed', content_url: `${prefix}/vids/generations/${originalId}/content` }) : binary('image/jpeg'));
    await uncertain.nodes.recover.click(); assert.equal(uncertain.requests.at(-2).url, `${prefix}/vids/generations/${originalId}/download`);
    assert.equal(uncertain.requests.filter(row => row.url === `${prefix}/vids/generations` && row.options.method === 'POST').length, 1, 'Receipt recovery never repeats image generation');
    assert(uncertain.nodes.requestDetails.textContent.includes(original.options.headers['Idempotency-Key']));
    assert.equal(uncertain.nodes.media.children[0].tagName, 'img');
  }
  const editRejected = loadPage(); await editRejected.prepare(); await editRejected.nodes.generationMode.change('edit');
  editRejected.nodes.imageFile.files = [jpeg]; await editRejected.nodes.imageFile.change('');
  editRejected.respond(async () => json({ error: { message: 'Synthetic validation error', code: 'invalid_request' } }, 400));
  await editRejected.nodes.generate.click(); const rejectedEdit = editRejected.requests[1]; await editRejected.nodes.retry.click();
  assert.equal(editRejected.requests.at(-1).options.body, rejectedEdit.options.body);
  assert.equal(editRejected.requests.at(-1).options.headers['Idempotency-Key'], rejectedEdit.options.headers['Idempotency-Key'], 'Retry keeps the original image request and key');

  const invalidLocal = loadPage(); await invalidLocal.prepare(); await invalidLocal.nodes.generationMode.change('edit');
  invalidLocal.nodes.imageFile.files = [{ ...jpeg, base64: Buffer.from('bad').toString('base64') }]; await invalidLocal.nodes.imageFile.change('');
  await invalidLocal.nodes.generate.click(); assert.equal(invalidLocal.requests.length, 1); assert.equal(invalidLocal.nodes.generate.disabled, true);
  const imageAuth = loadPage(); await imageAuth.prepare(); await imageAuth.nodes.generationMode.change('edit');
  imageAuth.nodes.imageFile.files = [jpeg]; await imageAuth.nodes.imageFile.change('');
  imageAuth.respond(async () => json({}, 401)); await imageAuth.nodes.generate.click();
  assert.equal(imageAuth.location.href, '/login'); assert.equal(imageAuth.requests.length, 2);

  for (const files of [[{ ...jpeg, type: 'text/plain' }], [{ ...jpeg, base64: Buffer.from('fake jpeg').toString('base64') }], [{ ...jpeg, size: 6 * 1024**2 }, { ...jpeg, size: 6 * 1024**2 }]]) {
    const bad = loadPage(); await bad.prepare('video'); await bad.nodes.generationMode.change('ingredients'); bad.nodes.referenceFiles.files = files; await bad.nodes.referenceFiles.change(''); await bad.nodes.generate.click();
    assert.equal(bad.requests.length, 1); assert.equal(bad.nodes.generate.disabled, true); assert(bad.nodes.materialError.textContent);
  }
  const released = loadPage(); await released.prepare('video'); await released.nodes.generationMode.change('animate'); released.nodes.referenceFiles.files = [jpeg]; await released.nodes.referenceFiles.change('');
  await released.nodes.generationMode.change('text'); assert.equal(released.nodes.referencePreview.children.length, 0); assert.equal(released.revoked.length, 1, 'Mode changes release local image previews');
  console.log('PASS: full Vids parameter UI, image outputs, single-request local image/video editing, upload video extension/upscale, original receipt recovery, zero startup writes, media previews and iframe resize');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
