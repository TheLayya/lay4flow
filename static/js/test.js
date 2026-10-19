const MODE_LABELS = {auto: '自动识别', text_to_video: '文生视频', image_to_video: '首帧生视频', first_last_frame: '首尾帧生视频', reference_to_video: '参考素材生视频', edit: '视频编辑', extend: '视频续写（Extend）'};
const QUALITY_LABELS = {quality: '高质量', fast: '快速', lite: '轻量'};
const PRIORITY_LABELS = {standard: '标准', relaxed: 'Relaxed（需 Ultra）'};
const STATE = {selectedModel: null, generating: false, image: {images: []}, video: {images: [], source: null, durationMs: null}, mediaRevision: 0};
let ALL_MODELS = {...FALLBACK_MODELS};
const $ = id => document.getElementById(id);
const visible = (id, show) => { $(id).style.display = show ? (['imageParameters', 'videoTrimGroup'].includes(id) ? 'grid' : 'block') : 'none'; };
function currentModel() { return ALL_MODELS[STATE.selectedModel]; }
function currentImages() { return currentModel()?.type === 'image' ? STATE.image.images : STATE.video.images; }
function unique(values) { return [...new Set(values)]; }
function formatDuration(milliseconds) { const seconds = Math.max(0, Number(milliseconds || 0)) / 1000; return `${seconds.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')} 秒`; }
function fillSelect(id, values, value, label = item => item) {
  const select = $(id); select.innerHTML = '';
  for (const item of values) { const option = document.createElement('option'); option.value = String(item); option.textContent = label(item); select.appendChild(option); }
  select.value = String(values.includes(value) ? value : (values[0] ?? ''));
}
function renderSidebar() {
  $('modelSidebar').innerHTML = '';
  for (const [id, model] of Object.entries(ALL_MODELS)) {
    const button = document.createElement('button'); button.className = 'model-item'; button.dataset.model = id; button.type = 'button';
    button.appendChild(document.createTextNode(model.displayName || model.description || id));
    const small = document.createElement('small'); small.textContent = id; button.appendChild(small);
    button.onclick = () => selectModel(id); $('modelSidebar').appendChild(button);
  }
}
function selectModel(id) {
  if (!ALL_MODELS[id] || STATE.generating) return;
  STATE.selectedModel = id; STATE.image.images = []; STATE.video.images = []; STATE.video.source = null; STATE.video.durationMs = null; STATE.mediaRevision++;
  const model = currentModel(), cap = model.capabilities, defaults = model.defaults;
  const aliases = model.compatibilityAliases ? ` compatibility aliases: ${Object.entries(model.compatibilityAliases).map(([name, mapping]) => `${name} (${mapping})`).join('; ')}` : '';
  $('selectedModelName').textContent = model.displayName || id;
  $('selectedModelInfo').textContent = `${id} · ${model.usage || '按下方参数配置后生成'} · 推荐入口：${model.recommended_endpoint || '/v1/chat/completions'}`;
  $('selectedModelInfo').textContent += aliases;
  document.querySelectorAll('.model-item').forEach(el => el.classList.toggle('active', el.dataset.model === id));
  visible('imageParameters', model.type === 'image'); visible('imageSizeHint', model.type === 'image'); visible('videoParameters', model.type === 'video');
  if (model.type === 'image') {
    fillSelect('imageAspect', cap.aspectRatios, defaults.aspectRatio);
    fillSelect('imageSize', cap.imageSizes, defaults.imageSize, size => size === '1k' ? '原生（1K 档）' : size.toUpperCase());
  } else {
    fillSelect('videoMode', ['auto', ...Object.keys(cap.modes)], defaults.mode, mode => MODE_LABELS[mode] || mode);
    fillSelect('videoPriority', cap.priorities || [], defaults.priority, priority => PRIORITY_LABELS[priority] || priority);
    fillSelect('videoQuality', cap.qualities, defaults.quality, quality => QUALITY_LABELS[quality] || quality);
    fillSelect('videoAspect', cap.aspectRatios, defaults.aspectRatio);
    fillSelect('videoDuration', unique(cap.combinations.flatMap(row => row.durations)), defaults.durationSeconds, value => `${value} 秒`);
    fillSelect('videoResolution', unique(cap.combinations.flatMap(row => row.resolutions)), defaults.resolution, value => value.toUpperCase());
    visible('videoQualityGroup', cap.qualities.length > 0);
    visible('videoPriorityGroup', !!cap.priorities?.length);
    $('trimStartMs').value = '0'; $('trimEndMs').value = '10000'; $('videoMediaId').value = '';
  }
  renderImagePreviews(); renderVideoPreview(); updateParameters();
}
function selectedMode() {
  if (currentModel()?.type !== 'video') return null;
  const mode = $('videoMode').value;
  return mode !== 'auto' ? mode : STATE.video.source ? 'edit' : STATE.video.images.length ? 'reference_to_video' : 'text_to_video';
}
function inputLimits() {
  const model = currentModel(); if (!model) return {minImages: 0, maxImages: 0};
  if (model.type === 'image') return model.capabilities;
  const cap = model.capabilities;
  if ($('videoMode').value === 'auto' && !STATE.video.source && !STATE.video.images.length) return cap.modes.reference_to_video || {minImages: 0, maxImages: 0};
  return cap.modes[selectedMode()] || {minImages: 0, maxImages: 0};
}
function matchingCombinations() {
  const model = currentModel(), quality = model.capabilities.qualities.length ? $('videoQuality').value : null;
  const priority = model.capabilities.priorities?.length ? $('videoPriority').value : 'standard';
  return model.capabilities.combinations.filter(row => row.mode === selectedMode() && (row.priority || 'standard') === priority && (row.quality ?? null) === quality);
}
function parameterError() {
  const model = currentModel(); if (!model) return '请选择模型';
  if (model.type === 'image') return STATE.image.images.length > model.capabilities.maxImages ? `图片素材最多 ${model.capabilities.maxImages} 张。` : '';
  const mode = selectedMode(), rows = matchingCombinations();
  const limits = inputLimits(), count = STATE.video.images.length;
  if (count > (limits.maxImages || 0)) return `当前生成方式需要 ${limits.minImages || 0}–${limits.maxImages || 0} 张图片。`;
  if (limits.videoInput && !STATE.video.source) return '请上传要编辑的视频。';
  if (!limits.videoInput && STATE.video.source) return '当前生成方式不接受上传视频。';
  if (!rows.length) return '当前生成方式不支持所选档位，请调整生成方式或档位。';
  if (!rows.some(row => row.resolutions.includes($('videoResolution').value) && (mode === 'edit' || row.durations.includes(Number($('videoDuration').value))))) return '当前时长与清晰度组合不受支持，请选择可用选项。';
  if (STATE.video.source && STATE.video.images.length && mode !== 'edit') return '视频素材与图片素材不能同时提交。';
  if (mode === 'edit') {
    const start = Number($('trimStartMs').value), end = Number($('trimEndMs').value);
    if ($('trimStartMs').value === '' || $('trimEndMs').value === '' || !Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end - start > 10000) return '裁剪时间必须为整数毫秒，开始不小于 0，结束大于开始，片段最多 10 秒。';
    if (STATE.video.durationMs !== null && end > STATE.video.durationMs) return `片段结束不能超过源视频时长 ${formatDuration(STATE.video.durationMs)}。`;
  }
  return '';
}
function updateParameters() {
  const model = currentModel(); if (!model) return;
  const limits = inputLimits(), video = model.type === 'video', mode = selectedMode();
  visible('imageUploadGroup', limits.maxImages > 0); visible('videoUploadGroup', video && !!limits.videoInput);
  visible('videoTrimGroup', video && mode === 'edit'); visible('mediaIdGroup', video && !!limits.mediaId && !limits.videoInput);
  visible('videoDurationGroup', video && mode !== 'edit');
  $('imageLimit').textContent = `（${limits.minImages || 0}–${limits.maxImages || 0} 张）`;
  if (video) {
    const cap = model.capabilities, modeRows = cap.combinations.filter(row => row.mode === mode);
    if (cap.priorities?.length) {
      const priorities = cap.priorities.filter(priority => modeRows.some(row => (row.priority || 'standard') === priority));
      fillSelect('videoPriority', priorities, $('videoPriority').value, priority => PRIORITY_LABELS[priority] || priority);
    }
    const priority = cap.priorities?.length ? $('videoPriority').value : 'standard';
    const priorityRows = modeRows.filter(row => (row.priority || 'standard') === priority);
    const qualities = unique(priorityRows.map(row => row.quality).filter(Boolean));
    fillSelect('videoQuality', qualities, $('videoQuality').value, quality => QUALITY_LABELS[quality] || quality);
    const rows = matchingCombinations();
    fillSelect('videoDuration', unique(rows.flatMap(row => row.durations)), Number($('videoDuration').value), value => `${value} 秒`);
    fillSelect('videoResolution', unique(rows.flatMap(row => row.resolutions)), $('videoResolution').value, value => value.toUpperCase());
    const durationValid = rows.some(row => row.durations.includes(Number($('videoDuration').value))), resolutionValid = rows.some(row => row.resolutions.includes($('videoResolution').value));
    for (const option of $('videoDuration').children) option.disabled = !rows.some(row => row.durations.includes(Number(option.value)) && (!resolutionValid || row.resolutions.includes($('videoResolution').value)));
    for (const option of $('videoResolution').children) option.disabled = !rows.some(row => row.resolutions.includes(option.value) && (mode === 'edit' || !durationValid || row.durations.includes(Number($('videoDuration').value))));
    const hasUpscaling = rows.some(row => row.resolutions.some(value => ['1080p', '4k'].includes(value)));
    const hint = mode === 'edit' ? 'Omni 视频编辑支持 1 个视频和最多 5 张参考图；编辑片段最长 10 秒。' : mode === 'first_last_frame' ? '按顺序上传首帧、尾帧各 1 张。' + (qualities.includes('lite') ? '轻量档位对应插值 Lite。' : '') : mode === 'extend' ? 'Extend 使用已有 Flow 视频的媒体 ID 续写。' : $('videoMode').value === 'auto' ? '自动识别：无素材为文生视频，有图片为参考素材生视频；首帧和首尾帧请明确选择。' : '按需要选择清晰度与时长。';
    const priorityHint = priority === 'relaxed' ? ' Relaxed 需要 Ultra 账号，支持快速档位、8 秒、720P。' : cap.priorities?.includes('relaxed') && mode === 'extend' ? ' 续写仅支持标准执行模式。' : '';
    $('modeHint').textContent = hint + priorityHint + (hasUpscaling && ['1080p', '4k'].includes($('videoResolution').value) ? ' 高清输出会额外放大，需要更长时间及相应账号权限。' : '');
  }
  const error = parameterError(); $('parameterError').textContent = error;
  $('btnGenerate').disabled = STATE.generating || !!error;
  $('btnGenerate').textContent = STATE.generating ? '生成中…' : model.type === 'image' ? '生成图片' : mode === 'edit' ? '编辑视频' : mode === 'extend' ? '续写视频' : '生成视频';
}
function changeMode() {
  STATE.mediaRevision++;
  const mode = $('videoMode').value;
  if (mode !== 'auto' && mode !== 'edit') STATE.video.source = null;
  if (mode === 'extend' || mode === 'text_to_video') STATE.video.images = [];
  updateParameters();
  const rows = matchingCombinations(), duration = Number($('videoDuration').value), resolution = $('videoResolution').value;
  const row = rows.find(item => (mode === 'edit' || item.durations.includes(duration)) && item.resolutions.includes(resolution))
    || rows.find(item => item.durations.includes(duration)) || rows.find(item => item.resolutions.includes(resolution)) || rows[0];
  if (row) {
    if (row.durations.length && !row.durations.includes(duration)) $('videoDuration').value = String(row.durations[0]);
    if (!row.resolutions.includes(resolution)) $('videoResolution').value = row.resolutions[0];
  }
  renderImagePreviews(); renderVideoPreview(); updateParameters();
}
function renderImagePreviews() {
  $('imagePreviewList').innerHTML = '';
  currentImages().forEach((url, index) => {
    const item = document.createElement('div'); item.className = 'image-preview-item';
    const image = document.createElement('img'); image.src = url; image.alt = `素材 ${index + 1}`; item.appendChild(image);
    const button = document.createElement('button'); button.className = 'remove-btn'; button.textContent = '×'; button.type = 'button'; button.setAttribute('aria-label', `移除素材 ${index + 1}`); button.onclick = () => removeImage(index); item.appendChild(button);
    if (selectedMode() === 'first_last_frame') { const label = document.createElement('small'); label.textContent = index === 0 ? '首帧' : '尾帧'; item.appendChild(label); }
    $('imagePreviewList').appendChild(item);
  });
}
function removeImage(index) { currentImages().splice(index, 1); renderImagePreviews(); updateParameters(); }
async function handleFiles(files) {
  const revision = STATE.mediaRevision, limits = inputLimits();
  for (const file of Array.from(files)) {
    if (revision !== STATE.mediaRevision || currentImages().length >= limits.maxImages) break;
    if (!file.type.startsWith('image/')) continue;
    try { const url = await readFile(file); if (revision !== STATE.mediaRevision) return; if (currentImages().length < limits.maxImages) currentImages().push(url); }
    catch { alert('图片读取失败，请重新选择文件。'); }
  }
  renderImagePreviews(); updateParameters();
}
function readFile(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = event => resolve(event.target.result); reader.onerror = reject; reader.readAsDataURL(file); }); }
async function handleVideoFile(file) {
  if (!file || !inputLimits().videoInput) return;
  if (!['video/mp4', 'video/webm', 'video/quicktime'].includes(file.type)) { alert('只支持 MP4、WebM 或 QuickTime 视频。'); return; }
  if (file.size > 100 * 1024 * 1024) { alert('视频不能超过 100 MiB。'); return; }
  const revision = STATE.mediaRevision;
  try { const url = await readFile(file); if (revision !== STATE.mediaRevision) return; STATE.video.source = url; STATE.video.durationMs = null; renderVideoPreview(); updateParameters(); }
  catch { alert('视频读取失败，请重新选择文件。'); }
}
function renderVideoPreview() {
  $('videoPreviewList').innerHTML = ''; if (!STATE.video.source) return;
  const item = document.createElement('div'); item.className = 'video-preview-item';
  const video = document.createElement('video'); video.src = STATE.video.source; video.controls = true; video.preload = 'metadata';
  video.onloadedmetadata = () => {
    if (selectedMode() !== 'edit' || !Number.isFinite(video.duration) || video.duration <= 0) return;
    STATE.video.durationMs = Math.round(video.duration * 1000);
    const usableMs = Math.min(10000, STATE.video.durationMs);
    $('trimStartMs').value = '0'; $('trimEndMs').value = String(usableMs);
    $('videoHint').textContent = `已读取源视频时长 ${formatDuration(STATE.video.durationMs)}；可编辑片段范围 0–${formatDuration(usableMs)}，最长 10 秒。`;
    updateParameters();
  };
  item.appendChild(video);
  const button = document.createElement('button'); button.className = 'remove-btn'; button.textContent = '×'; button.type = 'button'; button.setAttribute('aria-label', '移除视频'); button.onclick = removeVideo; item.appendChild(button); $('videoPreviewList').appendChild(item);
}
function removeVideo() { STATE.video.source = null; STATE.video.durationMs = null; STATE.mediaRevision++; $('videoHint').textContent = 'MP4、WebM 或 QuickTime；编辑片段不超过 10 秒。'; renderVideoPreview(); updateParameters(); }
function buildRequest(prompt) {
  const model = currentModel(), error = parameterError(); if (error) throw new Error(error);
  const content = [{type: 'text', text: prompt}], generationConfig = {};
  if (model.type === 'image') {
    if (STATE.image.images.length > model.capabilities.maxImages) throw new Error('图片素材数量超出限制。');
    generationConfig.imageConfig = {aspectRatio: $('imageAspect').value, imageSize: $('imageSize').value};
    STATE.image.images.forEach(url => content.push({type: 'image_url', image_url: {url}}));
  } else {
    const mode = selectedMode(), limits = model.capabilities.modes[mode], count = STATE.video.images.length;
    if (count < (limits.minImages || 0) || count > (limits.maxImages || 0)) throw new Error(`当前生成方式需要 ${limits.minImages || 0}–${limits.maxImages || 0} 张图片。`);
    if (limits.videoInput && !STATE.video.source) throw new Error('请上传要编辑的视频。');
    if (!limits.videoInput && STATE.video.source) throw new Error('当前生成方式不接受上传视频。');
    const options = {mode: $('videoMode').value, aspectRatio: $('videoAspect').value, resolution: $('videoResolution').value};
    if (model.capabilities.priorities?.length) options.priority = $('videoPriority').value;
    if (model.capabilities.qualities.length) options.quality = $('videoQuality').value;
    if (mode === 'edit') { options.trimStartMs = Number($('trimStartMs').value); options.trimEndMs = Number($('trimEndMs').value); }
    else options.durationSeconds = Number($('videoDuration').value);
    generationConfig.videoConfig = options;
    STATE.video.images.forEach(url => content.push({type: 'image_url', image_url: {url}}));
    if (limits.videoInput) content.push({type: 'video_url', video_url: {url: STATE.video.source}});
    if (limits.mediaId && !limits.videoInput) { const id = $('videoMediaId').value.trim().replace(/^extend:\/\//, ''); if (!id || /\s/.test(id)) throw new Error('请填写有效的源视频媒体 ID。'); content.push({type: 'video_url', video_url: {url: `extend://${id}`}}); }
  }
  return {model: STATE.selectedModel, messages: [{role: 'user', content: content.length === 1 ? prompt : content}], generationConfig, stream: true};
}
async function loadModels() {
  const baseUrl = $('baseUrl').value.trim().replace(/\/$/, ''), apiKey = $('apiKey').value.trim();
  if (!baseUrl || !apiKey) { ALL_MODELS = {...FALLBACK_MODELS}; renderSidebar(); return; }
  try {
    const response = await fetch(`${baseUrl}/v1/models`, {headers: {Authorization: `Bearer ${apiKey}`}});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json(), catalog = {};
    for (const entry of payload.data || []) {
      const fallback = FALLBACK_MODELS[entry.id];
      if (entry.legacy || fallback?.legacy) continue;
      if (!entry.id || !entry.type || !entry.capabilities || !entry.defaults) continue;
      catalog[entry.id] = {...fallback, ...entry};
    }
    if (!Object.keys(catalog).length) throw new Error('服务器尚未提供公开模型列表');
    ALL_MODELS = catalog; $('catalogNote').textContent = `已连接：${Object.keys(catalog).length} 个公开模型，参数按服务器能力显示。`;
  } catch (error) { ALL_MODELS = {...FALLBACK_MODELS}; $('catalogNote').textContent = `使用内置公开模型：${error.message}。`; }
  renderSidebar(); if (STATE.selectedModel && ALL_MODELS[STATE.selectedModel]) selectModel(STATE.selectedModel);
}
async function generate() {
  if (STATE.generating || !currentModel()) return;
  const baseUrl = $('baseUrl').value.trim().replace(/\/$/, ''), apiKey = $('apiKey').value.trim(), prompt = $('promptInput').value.trim();
  if (!prompt || !apiKey || !baseUrl) { alert('请填写提示词、API Key 和服务器地址。'); return; }
  let body; try { body = buildRequest(prompt); } catch (error) { alert(error.message); return; }
  STATE.generating = true; updateParameters();
  $('statusDot').className = 'status-dot status-running'; $('statusText').textContent = '生成中…'; $('outputResult').innerHTML = '';
  const started = Date.now(), log = $('outputLog'); log.textContent = `模型：${body.model}\n参数：${JSON.stringify(body.generationConfig)}\n提示词：${prompt}\n开始请求…\n`;
  const append = text => { log.textContent += text; log.scrollTop = log.scrollHeight; };
  try {
    const response = await fetch(`${baseUrl}/v1/chat/completions`, {method: 'POST', headers: {Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '', output = '', failure = null;
    const consume = line => {
      if (!line.startsWith('data: ')) return; const text = line.slice(6).trim(); if (text === '[DONE]') return;
      let payload; try { payload = JSON.parse(text); } catch { return; }
      if (payload.error) { failure = payload.error.message || JSON.stringify(payload.error); append(`\n错误：${failure}\n`); return; }
      const delta = payload.choices?.[0]?.delta || {}, chunk = delta.reasoning_content || delta.content || ''; output += chunk; append(chunk);
    };
    while (true) { const {done, value} = await reader.read(); if (done) break; buffer += decoder.decode(value, {stream: true}); const lines = buffer.split('\n'); buffer = lines.pop(); lines.forEach(consume); }
    buffer += decoder.decode(); if (buffer) consume(buffer);
    if (failure) throw new Error(failure);
    const seconds = ((Date.now() - started) / 1000).toFixed(1); append(`\n完成，耗时 ${seconds}s\n`); renderResult(output); $('statusDot').className = 'status-dot status-done'; $('statusText').textContent = `完成（${seconds}s）`;
  } catch (error) { append(`\n请求失败：${error.message}\n`); $('statusDot').className = 'status-dot status-error'; $('statusText').textContent = '失败'; }
  finally { STATE.generating = false; updateParameters(); }
}
function renderResult(content) {
  const result = $('outputResult'); result.innerHTML = ''; let match;
  const images = /!\[.*?\]\((.*?)\)/g, videos = /<video[^>]+src=['"]([^'"]+)['"]/gi;
  while ((match = images.exec(content))) { const image = document.createElement('img'); image.src = match[1]; image.alt = '生成结果'; image.loading = 'lazy'; result.appendChild(image); }
  while ((match = videos.exec(content))) { const video = document.createElement('video'); video.src = match[1]; video.controls = true; result.appendChild(video); }
  if (!result.children.length && content.trim()) { const text = document.createElement('pre'); text.style.whiteSpace = 'pre-wrap'; text.textContent = content; result.appendChild(text); }
}
$('videoMode').onchange = changeMode;
$('videoPriority').onchange = updateParameters;
for (const id of ['videoQuality', 'videoAspect', 'videoResolution', 'videoDuration', 'trimStartMs', 'trimEndMs']) $(id).addEventListener('change', updateParameters);
$('imageFileInput').onchange = () => { handleFiles($('imageFileInput').files); $('imageFileInput').value = ''; };
$('videoFileInput').onchange = () => { handleVideoFile($('videoFileInput').files[0]); $('videoFileInput').value = ''; };
for (const [id, handler] of [['imageUploadArea', files => handleFiles(files)], ['videoUploadArea', files => handleVideoFile(files[0])]]) {
  const area = $(id); area.ondragover = event => { event.preventDefault(); area.classList.toggle('dragging', true); }; area.ondragleave = () => area.classList.toggle('dragging', false); area.ondrop = event => { event.preventDefault(); area.classList.toggle('dragging', false); handler(event.dataTransfer.files); };
}
$('btnGenerate').onclick = generate; $('baseUrl').value = window.location.origin;
$('baseUrl').addEventListener('change', loadModels); $('apiKey').addEventListener('change', loadModels);
loadModels();
