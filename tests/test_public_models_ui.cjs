// Run with: node tests/test_public_models_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../static/test.html'), 'utf8');
const code = require('./helpers/page_scripts.cjs')(html);
const elements = new Map(), alerts = [], requests = [];
function element() {
  return {
    children: [], style: {}, dataset: {}, value: '', textContent: '', disabled: false,
    classList: {toggle() {}}, addEventListener() {}, setAttribute() {},
    appendChild(child) { this.children.push(child); },
    set innerHTML(value) { this.markup = value; this.children = []; },
    get innerHTML() { return this.markup || ''; }
  };
}
for (const [, id] of html.matchAll(/id="([^"]+)"/g)) elements.set(id, element());
let catalogResponse = null, stream = '';
const context = vm.createContext({
  console, TextDecoder,
  document: {
    getElementById(id) { assert(elements.has(id), `Missing element ${id}`); return elements.get(id); },
    createElement: element, createTextNode: text => ({textContent: text}), querySelectorAll: () => []
  },
  window: {location: {origin: 'http://test.local'}},
  alert: message => alerts.push(message),
  FileReader: class {readAsDataURL(file) { this.onload({target: {result: `data:${file.type};base64,AAAA`}}); }},
  async fetch(url, options) {
    requests.push({url, ...options});
    if (url.endsWith('/v1/models')) return {ok: true, json: async () => catalogResponse};
    let sent = false;
    return {ok: true, body: {getReader: () => ({async read() { if (sent || !stream) return {done: true}; sent = true; return {done: false, value: Buffer.from(stream)}; }})}};
  }
});
const run = expression => vm.runInContext(expression, context);
new vm.Script(code).runInContext(context);
const set = (id, value) => { elements.get(id).value = String(value); };
const json = expression => JSON.parse(run(`JSON.stringify(${expression})`));

(async () => {
  const ids = ['gemini-3.1-flash-image', 'gemini-3.0-pro-image', 'gemini-3.1-flash-image-lite', 'imagen-4.0-generate-preview', 'veo_3_1', 'omni_1.1_flash'];
  assert.deepEqual(json('Object.keys(FALLBACK_MODELS)'), ids);
  assert.deepEqual(elements.get('modelSidebar').children.map(item => item.dataset.model), ids);
  for (const id of ids) {
    assert(json(`FALLBACK_MODELS[${JSON.stringify(id)}].usage`), `${id} must explain its purpose`);
    assert(json(`FALLBACK_MODELS[${JSON.stringify(id)}].recommended_endpoint`), `${id} must expose a call entrypoint`);
  }
  assert.deepEqual(Object.keys(json('FALLBACK_MODELS.veo_3_1.compatibilityAliases')), [
    'veo_3_1_interpolation_lite', 'veo_3_1_remix', 'veo_3_1_extend'
  ]);
  assert(!code.includes('modelId.startsWith('), 'Input capabilities must not be guessed from model names');
  set('apiKey', 'test-key'); set('promptInput', 'Make the cup pink');

  catalogResponse = {data: json('Object.entries(FALLBACK_MODELS).map(([id, entry]) => ({id, ...entry}))')};
  await run('loadModels()'); requests.length = 0;
  const compatibilityIds = ['veo_3_1_interpolation_lite', 'veo_3_1_remix', 'veo_3_1_extend'];
  catalogResponse = {data: compatibilityIds.map(id => ({id, legacy: true, type: 'video', capabilities: {}, defaults: {}}))};
  await run('loadModels()');
  assert.deepEqual(elements.get('modelSidebar').children.map(item => item.dataset.model), ids);
  const newModel = {...json('FALLBACK_MODELS.omni_1_1_flash || FALLBACK_MODELS["omni_1.1_flash"]'), id: 'new-video-model'};
  catalogResponse.data.push(newModel);
  await run('loadModels()');
  assert(elements.get('modelSidebar').children.some(item => item.dataset.model === 'new-video-model'));
  catalogResponse = {data: json('Object.entries(FALLBACK_MODELS).map(([id, entry]) => ({id, ...entry}))')};
  await run('loadModels()'); requests.length = 0;

  for (const [mode, imageCount] of [['text_to_video', 0], ['image_to_video', 1], ['first_last_frame', 2], ['reference_to_video', 1], ['edit', 0]]) {
    run("selectModel('omni_1.1_flash')"); set('videoMode', mode); run('changeMode()');
    if (imageCount) await run(`handleFiles(Array(${imageCount}).fill({type:'image/png'}))`);
    if (mode === 'edit') await run("handleVideoFile({type:'video/mp4',size:20})");
    const options = elements.get('videoResolution').children.map(option => option.value);
    assert(options.includes('1080p') && options.includes('4k'), `${mode} must offer Omni upscaling`);
    for (const resolution of ['1080p', '4k']) {
      set('videoResolution', resolution); run('updateParameters()');
      const upscaleRequest = json("buildRequest('cat')");
      assert.equal(upscaleRequest.generationConfig.videoConfig.resolution, resolution);
      assert.equal(upscaleRequest.generationConfig.videoConfig.mode, mode);
      assert.match(elements.get('modeHint').textContent, /高清输出/);
    }
  }

  run("selectModel('omni_1.1_flash')");
  set('videoMode', 'edit'); run('changeMode()'); set('videoResolution', '360p'); run('updateParameters()');
  assert.equal(elements.get('videoUploadGroup').style.display, 'block');
  assert.equal(elements.get('imageUploadGroup').style.display, 'block');
  assert.equal(elements.get('videoDurationGroup').style.display, 'none');
  await run('generate()'); assert.equal(requests.length, 0, 'Edit requires a source video');
  await run("handleVideoFile({type:'video/mp4',size:100*1024*1024+1})"); assert.equal(run('STATE.video.source'), null);
  await run("handleVideoFile({type:'image/png',size:20})"); assert.equal(run('STATE.video.source'), null);
  await run("handleVideoFile({type:'video/mp4',size:20})");
  assert.equal(elements.get('videoPreviewList').children[0].children[0].controls, true);
  set('trimStartMs', 2000); set('trimEndMs', 6000); await run('generate()');
  let body = JSON.parse(requests.at(-1).body);
  assert.equal(body.model, 'omni_1.1_flash');
  assert.deepEqual(body.generationConfig.videoConfig, {mode: 'edit', aspectRatio: '16:9', resolution: '360p', trimStartMs: 2000, trimEndMs: 6000});
  assert.deepEqual(body.messages[0].content.map(item => item.type), ['text', 'video_url']);
  assert.equal(body.messages[0].content[1].video_url.url, 'data:video/mp4;base64,AAAA');
  await run("handleFiles([{type:'image/png'}, {type:'image/png'}, {type:'image/png'}, {type:'image/png'}, {type:'image/png'}])");
  body = json("buildRequest('edit with references')");
  assert.deepEqual(body.messages[0].content.map(item => item.type), ['text', 'image_url', 'image_url', 'image_url', 'image_url', 'image_url', 'video_url']);
  run('STATE.video.images = Array(6).fill(\'data:image/png;base64,A\')');
  assert.throws(() => run("buildRequest('too many edit references')"), /需要 0–5/);
  run('STATE.video.images = []');
  set('trimEndMs', 13000); run('updateParameters()'); assert(elements.get('btnGenerate').disabled); assert.throws(() => run("buildRequest('edit')"), /10 秒/);
  run('removeVideo()'); assert.equal(run('STATE.video.source'), null);

  run("selectModel('omni_1.1_flash')");
  await run("handleFiles([{type:'image/png'}, {type:'image/png'}])");
  assert.equal(run('selectedMode()'), 'reference_to_video', 'Auto must not guess first/last from image count');
  set('videoDuration', 6); set('videoResolution', '360p'); set('videoAspect', '9:16');
  body = json("buildRequest('cat')");
  assert.equal(body.model, 'omni_1.1_flash'); assert.equal(body.generationConfig.videoConfig.mode, 'auto');
  assert.equal(body.generationConfig.videoConfig.durationSeconds, 6);
  assert.equal(body.generationConfig.videoConfig.aspectRatio, '9:16');
  assert.equal(body.messages[0].content.filter(item => item.type === 'image_url').length, 2);
  set('videoMode', 'first_last_frame'); run('changeMode()');
  assert.equal(elements.get('imagePreviewList').children[0].children[2].textContent, '首帧');
  assert.equal(elements.get('imagePreviewList').children[1].children[2].textContent, '尾帧');
  body = json("buildRequest('cat')"); assert.equal(body.generationConfig.videoConfig.mode, 'first_last_frame');
  set('videoMode', 'image_to_video'); run('changeMode()'); assert.throws(() => run("buildRequest('cat')"), /1–1/);
  run('removeImage(1)'); assert.equal(json("buildRequest('cat')").messages[0].content.length, 2);

  run("selectModel('veo_3_1')");
  set('videoMode', 'reference_to_video'); set('videoQuality', 'quality'); run('changeMode()');
  assert.equal(elements.get('videoQuality').value, 'fast');
  assert.equal(elements.get('btnGenerate').disabled, false);
  set('videoMode', 'extend'); run('changeMode()');
  assert.equal(elements.get('videoUploadGroup').style.display, 'none'); assert.equal(elements.get('mediaIdGroup').style.display, 'block');
  assert.throws(() => run("buildRequest('continue')"), /媒体 ID/);
  set('videoMediaId', 'existing-video-id'); body = json("buildRequest('continue')");
  assert.equal(body.model, 'veo_3_1'); assert.equal(body.messages[0].content[1].video_url.url, 'extend://existing-video-id');
  set('videoMode', 'text_to_video'); run('changeMode()');
  set('videoQuality', 'fast'); set('videoDuration', 4); set('videoResolution', '4k');
  assert.throws(() => run("buildRequest('invalid fast 4k')"), /组合不受支持/);
  set('videoQuality', 'quality'); run('updateParameters()'); assert.equal(elements.get('btnGenerate').disabled, false);
  set('videoMode', 'reference_to_video'); set('videoQuality', 'fast'); set('videoDuration', 8); run('changeMode()');
  await run("handleFiles([{type:'image/png'}])");
  body = json("buildRequest('cat')"); assert.equal(body.generationConfig.videoConfig.resolution, '4k');
  assert.match(elements.get('modeHint').textContent, /账号权限/);

  run("selectModel('veo_3_1')");
  for (const alias of ['veo_3_1_interpolation_lite', 'veo_3_1_remix', 'veo_3_1_extend']) {
    assert.match(elements.get('selectedModelInfo').textContent, new RegExp(alias));
  }
  set('videoMode', 'first_last_frame'); run('changeMode()');
  set('videoQuality', 'lite'); run('updateParameters()');
  assert.equal(elements.get('videoMode').value, 'first_last_frame');
  await run("handleFiles([{type:'image/png'}, {type:'image/png'}])");
  body = json("buildRequest('transition')");
  assert.equal(body.model, 'veo_3_1');
  assert.equal(body.generationConfig.videoConfig.mode, 'first_last_frame');
  assert.equal(body.generationConfig.videoConfig.quality, 'lite');
  assert.equal(body.messages[0].content.filter(item => item.type === 'image_url').length, 2);

  run("selectModel('veo_3_1')");
  set('videoMode', 'extend'); run('changeMode()');
  assert.equal(elements.get('mediaIdGroup').style.display, 'block');
  set('videoMediaId', 'source-video'); body = json("buildRequest('remix')");
  assert.equal(body.model, 'veo_3_1');
  assert.equal(body.generationConfig.videoConfig.mode, 'extend');
  assert.equal(body.messages[0].content[1].video_url.url, 'extend://source-video');

  run("selectModel('gemini-3.1-flash-image')"); await run("handleFiles([{type:'image/png'}])");
  set('imageAspect', '1:1'); set('imageSize', '2k'); body = json("buildRequest('orange cat')");
  assert.deepEqual(body.generationConfig, {imageConfig: {aspectRatio: '1:1', imageSize: '2k'}});
  assert.equal(run('STATE.video.images.length'), 0); assert.equal(run('STATE.video.source'), null);
  run("selectModel('gemini-3.1-flash-image-lite')"); assert.deepEqual(elements.get('imageSize').children.map(option => option.value), ['1k']);

  // Server capability metadata controls limits and default options; legacy IDs never reappear.
  catalogResponse = {data: [{id: 'omni_1.1_flash', ...json("FALLBACK_MODELS['omni_1.1_flash']")}, {id: 'omni_portrait_8s'}]};
  catalogResponse.data[0].capabilities.modes.reference_to_video.maxImages = 1;
  catalogResponse.data[0].defaults.resolution = '360p'; await run('loadModels()'); run("selectModel('omni_1.1_flash')");
  assert.deepEqual(elements.get('modelSidebar').children.map(item => item.dataset.model), ['omni_1.1_flash']);
  assert.equal(elements.get('videoResolution').value, '360p'); await run("handleFiles([{type:'image/png'}, {type:'image/png'}])");
  assert.equal(run('STATE.video.images.length'), 1);

  // An upstream SSE error must finish as failed, never green success.
  stream = 'data: {"error":{"message":"Upstream failed"}}\n\ndata: [DONE]\n\n'; await run('generate()');
  assert.equal(elements.get('statusText').textContent, '失败'); assert.equal(elements.get('statusDot').className, 'status-dot status-error');
  console.log('PASS: public models, interpolation/remix controls, Omni auto/edit/frames, trim validation, Veo combination guards, image options, canonical requests, SSE errors');
})().catch(error => { console.error(error); process.exitCode = 1; });
