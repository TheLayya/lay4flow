// Run with: node tests/test_veo_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../static/test.html'), 'utf8');
const code = require('./helpers/page_scripts.cjs')(html);
const elements = new Map();
function element() {
  return {
    children: [], style: {}, dataset: {}, value: '', textContent: '', disabled: false,
    classList: {toggle() {}}, addEventListener() {}, setAttribute() {},
    appendChild(child) { this.children.push(child); },
    set innerHTML(value) { this.children = []; }
  };
}
for (const [, id] of html.matchAll(/id="([^"]+)"/g)) elements.set(id, element());
let catalog;
const context = vm.createContext({
  document: {
    getElementById: id => elements.get(id), createElement: element,
    createTextNode: text => ({textContent: text}), querySelectorAll: () => []
  },
  window: {location: {origin: 'http://test.local'}},
  fetch: async () => ({ok: true, json: async () => ({data: catalog})})
});
const run = expression => vm.runInContext(expression, context);
const json = expression => JSON.parse(run(`JSON.stringify(${expression})`));
const set = (id, value) => { elements.get(id).value = String(value); };
const options = id => elements.get(id).children.map(option => option.value);
new vm.Script(code).runInContext(context);

(async () => {
  const ids = ['gemini-3.1-flash-image', 'gemini-3.0-pro-image', 'gemini-3.1-flash-image-lite',
    'imagen-4.0-generate-preview', 'veo_3_1', 'omni_1.1_flash'];
  const sidebar = () => elements.get('modelSidebar').children.map(item => item.dataset.model);
  assert.deepEqual(sidebar(), ids);
  catalog = json('Object.entries(FALLBACK_MODELS).map(([id, spec]) => ({id, ...spec}))');

  run("selectModel('veo_3_1')");
  assert.equal(elements.get('videoQuality').value, 'fast');
  assert.equal(elements.get('videoPriority').value, 'standard');
  assert.deepEqual(options('videoPriority'), ['standard', 'relaxed']);
  assert.equal(elements.get('videoPriorityGroup').style.display, 'block');
  set('videoMode', 'extend'); run('changeMode()');
  assert.deepEqual(options('videoQuality'), ['quality', 'fast', 'lite']);
  assert.equal(elements.get('videoQuality').value, 'fast');
  assert.equal(elements.get('btnGenerate').disabled, false);
  assert.equal(elements.get('mediaIdGroup').style.display, 'block');
  assert.equal(elements.get('videoUploadGroup').style.display, 'none');
  assert.equal(elements.get('videoMode').children.find(item => item.value === 'extend').textContent, '视频续写（Extend）');
  assert.doesNotMatch(elements.get('modeHint').textContent, /Remix/);
  assert.throws(() => run("buildRequest('continue')"), /媒体 ID/);
  set('videoMediaId', 'source-video');
  let body = json("buildRequest('continue')");
  assert.equal(body.model, 'veo_3_1');
  assert.deepEqual(body.generationConfig.videoConfig, {
    mode: 'extend', aspectRatio: '16:9', resolution: '720p', priority: 'standard', quality: 'fast', durationSeconds: 8
  });
  assert.equal(body.messages[0].content[1].video_url.url, 'extend://source-video');

  set('videoMode', 'first_last_frame'); run('changeMode()');
  assert.deepEqual(options('videoQuality'), ['quality', 'fast', 'lite']);
  set('videoQuality', 'lite'); set('videoDuration', 8); set('videoAspect', '9:16');
  run("STATE.video.images = ['data:image/png;base64,A', 'data:image/png;base64,B']; updateParameters()");
  body = json("buildRequest('transition')");
  assert.equal(body.model, 'veo_3_1');
  assert.deepEqual(body.generationConfig.videoConfig, {
    mode: 'first_last_frame', aspectRatio: '9:16', resolution: '720p', priority: 'standard', quality: 'lite', durationSeconds: 8
  });
  assert.equal(body.messages[0].content.filter(item => item.type === 'image_url').length, 2);
  assert.match(elements.get('modeHint').textContent, /插值 Lite/);
  run('STATE.video.images.pop()');
  assert.throws(() => run("buildRequest('transition')"), /2–2/);

  set('videoQuality', 'quality'); set('videoResolution', '4k');
  set('videoMode', 'extend'); run('changeMode()');
  assert.equal(elements.get('videoDuration').value, '8');
  assert.equal(elements.get('videoResolution').value, '720p');
  assert.equal(elements.get('btnGenerate').disabled, false);

  // Mode changes derive supported qualities from server capabilities.
  set('videoMode', 'reference_to_video'); set('videoQuality', 'quality'); run('changeMode()');
  assert.deepEqual(options('videoQuality'), ['fast', 'lite']);
  assert.equal(elements.get('videoQuality').value, 'fast');

  // Relaxed remains a Veo execution option and permits only its advertised rows.
  set('videoMode', 'text_to_video'); run('changeMode()');
  set('videoQuality', 'quality'); set('videoDuration', 8); set('videoResolution', '4k');
  set('videoPriority', 'relaxed'); elements.get('videoPriority').onchange();
  assert.deepEqual(options('videoQuality'), ['fast']);
  assert.deepEqual(options('videoDuration'), ['8']);
  assert.deepEqual(options('videoResolution'), ['720p']);
  assert.equal(elements.get('btnGenerate').disabled, false);
  assert.match(elements.get('modeHint').textContent, /Relaxed.*Ultra/);
  for (const [mode, imageCount] of Object.entries({text_to_video: 0, image_to_video: 1, first_last_frame: 2, reference_to_video: 3})) {
    set('videoMode', mode); run('changeMode()');
    run(`STATE.video.images = Array(${imageCount}).fill('data:image/png;base64,A'); updateParameters()`);
    body = json("buildRequest('relaxed video')");
    assert.equal(body.model, 'veo_3_1');
    assert.deepEqual(body.generationConfig.videoConfig, {
      mode, aspectRatio: '9:16', resolution: '720p', priority: 'relaxed', quality: 'fast', durationSeconds: 8
    });
    assert.equal(json('matchingCombinations()').length, 1);
  }
  set('videoMode', 'auto'); run('changeMode()');
  assert.equal(json("buildRequest('auto relaxed')").generationConfig.videoConfig.priority, 'relaxed');
  run('STATE.video.images = []; updateParameters()');
  assert.equal(json('matchingCombinations()')[0].mode, 'text_to_video');
  assert.equal(elements.get('videoPriority').value, 'relaxed');
  // A stale or manually altered combination must not send unsupported Relaxed values.
  set('videoDuration', 4);
  assert.throws(() => run("buildRequest('invalid relaxed duration')"), /组合不受支持/);
  set('videoDuration', 8); set('videoResolution', '1080p');
  assert.throws(() => run("buildRequest('invalid relaxed resolution')"), /组合不受支持/);
  set('videoResolution', '720p'); set('videoQuality', 'quality');
  assert.throws(() => run("buildRequest('invalid relaxed quality')"), /不支持所选档位/);
  set('videoQuality', 'fast'); set('videoMode', 'extend'); run('changeMode()');
  assert.deepEqual(options('videoQuality'), ['quality', 'fast', 'lite']);
  assert.equal(elements.get('videoQuality').value, 'fast');
  assert.deepEqual(options('videoPriority'), ['standard']);
  assert.equal(elements.get('videoPriority').value, 'standard');
  assert.match(elements.get('modeHint').textContent, /仅支持标准/);
  body = json("buildRequest('continue after relaxed')");
  assert.equal(body.generationConfig.videoConfig.priority, 'standard');
  assert.equal(body.generationConfig.videoConfig.quality, 'fast');
  set('videoMode', 'text_to_video'); run('changeMode()');
  assert.deepEqual(options('videoPriority'), ['standard', 'relaxed']);
  assert.equal(elements.get('videoPriority').value, 'standard');
  assert.deepEqual(options('videoQuality'), ['quality', 'fast', 'lite']);

  // Pick a valid row instead of mixing duration and resolution from different rows.
  run("ALL_MODELS.veo_3_1.capabilities.combinations = [{mode:'first_last_frame',quality:'lite',durations:[4],resolutions:['720p']},{mode:'first_last_frame',quality:'lite',durations:[6],resolutions:['1080p']}]");
  set('videoMode', 'first_last_frame'); set('videoDuration', 4); set('videoResolution', '1080p');
  run('changeMode()');
  assert.equal(elements.get('videoDuration').value, '4');
  assert.equal(elements.get('videoResolution').value, '720p');
  assert.equal(elements.get('btnGenerate').disabled, false);
  run("selectModel('omni_1.1_flash')");
  set('videoMode', 'edit'); run('changeMode()');
  assert.deepEqual(options('videoQuality'), []);
  assert.equal(elements.get('videoQualityGroup').style.display, 'none');
  assert.equal(elements.get('videoPriorityGroup').style.display, 'none');
  run("STATE.video.source = 'data:video/mp4;base64,A'; updateParameters()");
  assert.equal('priority' in json("buildRequest('edit video')").generationConfig.videoConfig, false);

  // Old servers listing compatibility aliases cannot restore duplicate sidebar entries.
  catalog.push(...['veo_3_1_interpolation_lite', 'veo_3_1_remix', 'veo_3_1_extend'].map(id => ({id})));
  set('apiKey', 'test-key'); await run('loadModels()');
  assert.deepEqual(sidebar(), ids);
  run("selectModel('veo_3_1')");
  assert.deepEqual(options('videoPriority'), ['standard', 'relaxed']);
  set('videoPriority', 'relaxed'); elements.get('videoPriority').onchange();
  assert.equal(json("buildRequest('server relaxed')").generationConfig.videoConfig.priority, 'relaxed');
  // Servers without a priority capability retain the previous request shape.
  const legacyVeo = catalog.find(model => model.id === 'veo_3_1');
  delete legacyVeo.capabilities.priorities; delete legacyVeo.defaults.priority;
  legacyVeo.capabilities.combinations = legacyVeo.capabilities.combinations.filter(row => !row.priority);
  await run('loadModels()');
  assert.equal(elements.get('videoPriorityGroup').style.display, 'none');
  assert.equal('priority' in json("buildRequest('legacy server')").generationConfig.videoConfig, false);
  console.log('PASS: public model entries including compatibility aliases, Veo Standard/Relaxed capabilities, canonical requests for four Relaxed modes, extend reset, Omni exclusion, alias filtering');
})().catch(error => { console.error(error); process.exitCode = 1; });
