(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const prefix = '/api/admin/vids/v1';
  const receiptPattern = /^vids_[a-f0-9]{64}$/;
  const models = {
    image: { title: '图片生成', model: 'vids-image', button: '生成图片', hint: '输入提示词生成图片，或上传原图后描述希望修改的内容。', endpoint: '/images/generations' },
    video: { title: '视频生成', model: 'vids-video', button: '生成视频', hint: '文生、图生、编辑、续写与放大。选择横竖比例、3–10 秒时长、720p 或 1080p。', endpoint: '/videos' },
    speech: { title: '语音生成', model: 'vids-speech', button: '生成语音', hint: '选择声线朗读，返回 WAV 音频。Neo、Saro、Jett 已通过英文实测。当前语音渠道未支持中文；可用英文等官方支持语言。', endpoint: '/audio/speech' }
  };
  const videoSizes = { '1280x720': ['16:9', '720p'], '720x1280': ['9:16', '720p'], '1920x1080': ['16:9', '1080p'], '1080x1920': ['9:16', '1080p'] };
  const durations = Array.from({ length: 8 }, (_, index) => index + 3);
  const imageStyles = [['auto', '无样式'], ['photography', '摄影'], ['background', '背景'], ['vector', '矢量图'], ['sketch', '素描'], ['watercolor', '水彩'], ['cyberpunk', '赛博朋克']];
  const voiceOptions = [
    { id: 'Achernar', name: 'Nyla', description: '柔和、音调较高' },
    { id: 'Achird', name: 'Elio', description: '亲切友好、音调中等偏低' },
    { id: 'Algieba', name: 'Knox', description: '平稳流畅、音调较低' },
    { id: 'Algenib', name: 'Jett', description: '低沉沙哑、音调较低' },
    { id: 'Alnilam', name: 'Zeno', description: '坚定、音调中等偏低' },
    { id: 'Aoede', name: 'Tova', description: '轻松活泼、音调适中' },
    { id: 'Autonoe', name: 'Kaci', description: '嘹亮、音调适中' },
    { id: 'Callirrhoe', name: 'Lani', description: '轻松、音调适中' },
    { id: 'Charon', name: 'Holt', description: '信息丰富、音调较低' },
    { id: 'Despina', name: 'Lora', description: '平稳流畅、音调适中' },
    { id: 'Enceladus', name: 'Paz', description: '带有气声、音调较低' },
    { id: 'Erinome', name: 'Tyra', description: '清晰、音调适中' },
    { id: 'Fenrir', name: 'Kero', description: '激动、音调中等偏低' },
    { id: 'Gacrux', name: 'Saro', description: '成熟、音调适中' },
    { id: 'Iapetus', name: 'Nyx', description: '清晰、音调中等偏低' },
    { id: 'Kore', name: 'Lito', description: '坚定、音调适中' },
    { id: 'Laomedeia', name: 'Peli', description: '欢快、音调较高' },
    { id: 'Leda', name: 'Fira', description: '青春洋溢、音调较高' },
    { id: 'Orus', name: 'Cale', description: '坚定、音调中等偏低' },
    { id: 'Puck', name: 'Neo', description: '欢快、音调适中' },
    { id: 'Pulcherrima', name: 'Vira', description: '激进，音调适中' },
    { id: 'Rasalgethi', name: 'Iro', description: '信息丰富、音调适中' },
    { id: 'Sadachbia', name: 'Dori', description: '生动活泼、音调较低' },
    { id: 'Sadaltager', name: 'Umi', description: '知识丰富、音调适中' },
    { id: 'Schedar', name: 'Jacy', description: '平稳、音调中等偏低' },
    { id: 'Sulafat', name: 'Fola', description: '温暖、音调适中' },
    { id: 'Umbriel', name: 'Baya', description: '轻松随意、音调中等偏低' },
    { id: 'Vindemiatrix', name: 'Orla', description: '柔和、音调适中' },
    { id: 'Zephyr', name: 'Sani', description: '嘹亮、音调较高' },
    { id: 'Zubenelgenubi', name: 'Yori', description: '闲适、音调中等偏低' }
  ];
  const state = { kind: 'image', mode: 'text', imageAspect: '1:1', imageStyle: 'auto', imageCount: 1, speechVoice: 'Puck', shotMode: 'auto', originalVideo: null, seconds: 3, size: '1280x720', configured: false, configuredCount: 0, available: {}, recoverable: {}, quota: {}, busy: false, materialBusy: false, statusBusy: false, task: null, history: [], blobUrls: [], blobReceipt: null, originalImage: null, referenceImages: [] };
  const modelButtons = () => [...document.querySelectorAll('[data-kind]')];
  const terminal = task => task && ['completed', 'failed', 'no_result', 'specification_mismatch'].includes(task.status);
  const unresolved = task => task && !terminal(task);
  function matchingRequest(body) {
    return state.history.find(task => unresolved(task) && task.kind === state.kind && task.body &&
      Object.keys(task.body).length === Object.keys(body).length && Object.entries(body).every(([key, value]) =>
        Array.isArray(value) ? Array.isArray(task.body[key]) && value.length === task.body[key].length && value.every((item, index) => item === task.body[key][index]) : value === task.body[key]));
  }
  const ready = kind => state.configured && Number(state.available[kind] || 0) > 0;
  const usable = kind => ready(kind) || state.configured && Number(state.recoverable[kind] || 0) > 0;
  function messageText(value) { return String(value || '').slice(0, 1000).replace(/https?:\/\/\S+/gi, '[地址已隐藏]'); }
  function failureMessage(error, kind = state.kind) {
    const code = error.code || error.error, upstream = error.upstreamError || error.upstream_error;
    const operation = error.errorOperation || error.error_operation;
    if (kind === 'speech' && upstream?.reason === 'LANGUAGE_NOT_SUPPORTED') return '当前 Vids 语音不支持这段文本的语言，请改用英文等受支持语言。原任务已保留，不会自动重发。';
    if (upstream?.reason === 'REQUEST_REFUSED') return '上游认为本次请求可能违反其条款（REQUEST_REFUSED），已拒绝生成。具体触发项未返回，无法确定是提示词还是素材内容。请检查并修改提示词或更换素材后再生成；这次拒绝不代表相同内容一定无法生成。原任务已保留，不会自动重发。';
    if (upstream?.status === 'RESOURCE_EXHAUSTED' || ['upstream_http_429', 'quota_exhausted'].includes(code) || error.status === 429 && code !== 'busy') return '上游额度不足或请求频率受限。请在“Vids 账号”页读取最新额度；额度不足时等待恢复，频率受限时稍后再操作。原任务已保留，不会自动重发。';
    const messages = {
      invalid_api_key: '登录已失效，请重新登录管理后台，再检查原任务。',
      authentication_required: '登录已失效，请重新登录管理后台，再检查原任务。',
      account_disabled: '当前没有可用的 Vids 账号。请在“Vids 账号”页检查接入与启用状态，恢复后再生成。',
      session_unavailable: 'Vids 会话尚未就绪。请在“Vids 账号”页恢复原账号会话；需要登录时重新登录同一账号，再检查原任务。',
      session_authentication_failed: 'Vids 会话鉴权未通过。请在“Vids 账号”页恢复原账号会话；需要登录时重新登录同一账号，再检查原任务。',
      session_persistence_failed: 'Vids 会话保存失败。请在“Vids 账号”页检查接入状态并恢复原账号会话；仍失败时联系管理员检查存储。原任务保留，恢复前请勿再次生成。',
      account_lookup_failed: '暂时无法检查账号状态。请稍后刷新接入状态，并检查原任务；仍失败时联系管理员。',
      busy: '可用账号正在处理其他任务。请等待当前任务结束，再检查原任务或生成；不要连续点击生成。',
      idempotency_conflict: '本次参数与原任务不一致。请检查原任务收据；要修改内容，请修改提示词或素材后使用“生成”提交独立任务。',
      invalid_source: '原素材不可用。请重新选择有效的图片或 MP4，并检查文件格式、大小和视频时长后再生成。',
      invalid_request: '请求参数未通过校验。请检查提示词、素材格式/大小、视频时长和画面规格，修改后再生成。',
      request_too_large: '请求或素材超过大小限制。请减小文件：MP4 最多 50 MiB，图片总量最多 10 MiB，再重新选择并生成。',
      unverified_specification: '当前规格暂不支持。请修改时长、比例或清晰度，选择支持的规格后再生成。',
      image_specification_mismatch: '生成的图片尺寸未达到要求。请检查所选比例与尺寸；需要新成品时修改规格后生成独立任务。',
      no_received_result: '原任务没有可恢复下载的接受结果。请先点击“检查结果”；结果未确认时不要重复生成相同请求。',
      not_found: '未找到这条收据。请核对请求详情中的收据 ID；仍找不到时联系管理员，勿直接重复生成。',
      expired: '成品已过期或文件缺失。请保留收据并联系管理员检查原文件；重新生成会消耗额度。',
      asset_expired: '成品已过期或文件缺失。请保留收据并联系管理员检查原文件；重新生成会消耗额度。'
    };
    if (messages[code]) return messages[code];
    if (['upstream_http_401', 'upstream_http_403'].includes(code) || [401, 403].includes(error.status) || ['UNAUTHENTICATED', 'PERMISSION_DENIED'].includes(upstream?.status)) return '上游登录或访问权限验证未通过。请在“Vids 账号”页检查权限并恢复原账号会话；需要登录时重新登录同一账号，再检查原任务。';
    if (operation?.startsWith('upload-')) return `上游拒绝了素材上传${code ? `（${code}）` : ''}。请检查文件格式、大小和视频时长，重新选择有效素材；若仍失败，在“Vids 账号”页检查会话与网络。原任务已保留，不会自动重传。`;
    if (error.status === 'no_result') return '上游没有返回可用成品。请检查提示词和素材，修改后再生成独立任务；原收据已保留，新生成会消耗额度。';
    if (error.status === 'specification_mismatch') return '成品的数量、尺寸或时长与请求不符。请检查所选数量、比例、清晰度和时长；修改规格后再生成独立任务，原收据已保留。';
    if (operation === 'generate') return `上游拒绝了生成提交${code ? `（${code}）` : ''}，没有提供可确认的具体原因。请检查提示词、素材和所选规格，修改后再生成；仍失败时保留收据并联系管理员。`;
    return `请求未完成${code ? `（${code}）` : ''}。请先检查网络和“Vids 账号”页的会话状态，再检查原任务；仍失败时保留收据并联系管理员，勿直接重复生成未确认的请求。`;
  }
  function receiptMessage(task) {
    if (task.status === 'unknown') return '上传或生成结果尚未确认，不能确定上游是否已接受。请检查网络或代理后点击“检查结果”查询原任务，暂不再次生成相同请求；也可直接修改内容或切换模型继续操作。原任务已保留，原请求不会重复提交。';
    if (task.status === 'completed') return '成品已就绪。';
    if (['received', 'download_failed'].includes(task.status)) return '上游已接受任务，成品下载尚未完成。请检查网络或代理后点击“恢复下载”，只取回原成品，不会重新生成或再次消耗生成额度。';
    return terminal(task) ? failureMessage(task, task.kind) : '任务处理中。请点击“检查结果”查看原任务，等待完成；不要重复生成相同请求。';
  }
  function reportHeight() {
    if (!document.body.classList.contains('embedded') || !window.parent || window.parent === window) return;
    window.parent.postMessage({ type: 'lay4flow.vids.resize', height: Math.min(20000, Math.max(250, Math.ceil(document.body.scrollHeight + 4))) }, window.location.origin);
  }
  const resize = () => typeof requestAnimationFrame === 'function' ? requestAnimationFrame(reportHeight) : reportHeight();
  function clearMedia() {
    $('media').replaceChildren();
    state.blobUrls.forEach(url => URL.revokeObjectURL(url));
    state.blobUrls = []; state.blobReceipt = null;
  }
  function appendMedia(blob, task, index = 0) {
    const mime = blob.type.split(';')[0].toLowerCase();
    const spec = { 'image/jpeg': ['img', 'jpg'], 'image/png': ['img', 'png'], 'image/webp': ['img', 'webp'], 'audio/wav': ['audio', 'wav'], 'audio/x-wav': ['audio', 'wav'], 'video/mp4': ['video', 'mp4'] }[mime];
    if (!spec || !blob.size) throw new Error('成品不是受支持的图片、WAV 或 MP4。');
    const url = URL.createObjectURL(blob); state.blobUrls.push(url); state.blobReceipt = task.receipt || task.key;
    const media = document.createElement(spec[0]); media.src = url;
    if (spec[0] === 'img') media.alt = 'Vids 生成图片';
    else { media.controls = true; media.preload = 'metadata'; }
    const download = document.createElement('a'); download.href = url;
    download.download = `${receiptPattern.test(task.receipt || '') ? task.receipt : 'vids-result'}${index ? `-${index + 1}` : ''}.${spec[1]}`;
    download.textContent = index ? `下载成品 ${index + 1}` : '下载成品'; $('media').appendChild(media); $('media').appendChild(download);
  }
  function renderMedia(blob, task) {
    clearMedia(); appendMedia(blob, task); resize();
  }
  function renderMediaItems(items, task) {
    clearMedia();
    try { items.forEach((item, index) => appendMedia(item.blob || item, task, item.index ?? index)); }
    catch (error) { clearMedia(); throw error; }
    resize();
  }
  function contentPath(value, task) {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || url.username || url.password || (url.search && !/^\?index=[0-3]$/.test(url.search)) || url.hash || /%(?:2e|2f|5c)/i.test(url.pathname) ||
        !new RegExp(`^${prefix}/(?:vids/generations|videos)/vids_[a-f0-9]{64}/content$`).test(url.pathname)) throw new Error('成品地址不属于当前服务的安全媒体接口。');
    const id = url.pathname.match(/vids_[a-f0-9]{64}/)[0];
    if (task.receipt && task.receipt !== id) throw new Error('成品与原任务收据不匹配。');
    task.receipt = id; return url.pathname + url.search;
  }
  async function request(path, options = {}) {
    if (!path.startsWith(prefix + '/')) throw new Error('请求地址无效。');
    const response = await fetch(path, { ...options, credentials: 'same-origin', redirect: 'error' });
    if (response.status === 401) { window.location.href = '/login'; const error = new Error('登录已失效，请重新登录。'); error.status = 401; error.code = 'authentication_required'; throw error; }
    return response;
  }
  function safeUpstreamError(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const knownStatuses = ['CANCELLED', 'UNKNOWN', 'INVALID_ARGUMENT', 'DEADLINE_EXCEEDED', 'NOT_FOUND', 'ALREADY_EXISTS', 'PERMISSION_DENIED', 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION', 'ABORTED', 'OUT_OF_RANGE', 'UNIMPLEMENTED', 'INTERNAL', 'UNAVAILABLE', 'DATA_LOSS', 'UNAUTHENTICATED'];
    const status = knownStatuses.includes(value.status) ? value.status : null;
    const reason = typeof value.reason === 'string' && /^[A-Z][A-Z0-9_]{1,63}$/.test(value.reason) ? value.reason : null;
    return status || reason ? { ...(status ? { status } : {}), ...(reason ? { reason } : {}) } : null;
  }
  function safeReceipt(value, expectedId) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || !receiptPattern.test(value.id) || expectedId && value.id !== expectedId) return null;
    const allowed = new Set(['id', 'kind', 'status', 'error', 'error_operation', 'content_url', 'upstream_error']);
    if (Object.keys(value).some(key => !allowed.has(key))) return null;
    if (value.kind !== undefined && !['image', 'video', 'speech'].includes(value.kind)) return null;
    const status = value.status;
    if (!['completed', 'unknown', 'generating', 'in_progress', 'received', 'failed', 'download_failed', 'no_result', 'specification_mismatch'].includes(status)) return null;
    const errorCode = typeof value.error === 'string' && /^[a-z0-9_]{1,80}$/.test(value.error) ? value.error : value.error && typeof value.error === 'object' && typeof value.error.code === 'string' && /^[a-z0-9_]{1,80}$/.test(value.error.code) ? value.error.code : null;
    if (value.error !== undefined && value.error !== null && !errorCode) return null;
    if (value.error_operation !== undefined && !['upload-start', 'upload-finalize', 'generate', 'getSettings'].includes(value.error_operation)) return null;
    if (status === 'failed' && !errorCode) return null;
    if (value.content_url !== undefined && value.content_url !== null && typeof value.content_url !== 'string') return null;
    return { id: value.id, kind: value.kind, status, error: errorCode, error_operation: value.error_operation, content_url: value.content_url, upstream_error: safeUpstreamError(value.upstream_error) };
  }
  async function readJSON(response, expectedReceipt) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.error || data;
      const error = new Error(messageText(detail.message || detail.detail || `请求失败（HTTP ${response.status}）。`));
      error.code = detail.code; error.status = response.status;
      const receipt = expectedReceipt && safeReceipt(data.vids_receipt, expectedReceipt);
      if (receipt) error.receipt = receipt;
      throw error;
    }
    return data;
  }
  function renderHistory() {
    $('sessionHistory').replaceChildren();
    const blank = document.createElement('option'); blank.value = ''; blank.textContent = '本页任务记录（仅保留在本页）'; $('sessionHistory').appendChild(blank);
    state.history.forEach((task, index) => { const option = document.createElement('option'); option.value = String(index); option.textContent = `${models[task.kind]?.title || '历史任务'} · ${task.time} · ${task.status === 'completed' ? '已完成' : task.status === 'unknown' ? '待确认' : task.status === 'failed' ? '失败' : '处理中'}`; $('sessionHistory').appendChild(option); });
    $('sessionHistory').value = state.task && state.history.includes(state.task) ? String(state.history.indexOf(state.task)) : '';
  }
  function renderQuota() {
    for (const kind of ['image', 'video']) {
      const quota = state.quota[kind], known = Number.isSafeInteger(quota?.remaining) && quota.remaining >= 0;
      $(`${kind}QuotaRemaining`).textContent = known ? `${quota.remaining}${Number.isSafeInteger(quota.covered) && quota.covered < state.configuredCount ? '（部分）' : ''}` : '—';
      const date = typeof quota?.queried_at === 'string' && Number.isFinite(Date.parse(quota.queried_at)) ? new Date(quota.queried_at).toLocaleString('zh-CN', { hour12: false }) : null;
      $(`${kind}QuotaUpdated`).textContent = known ? `上次读取 ${date || '时间未知'}${quota.stale ? ' · 已过期' : ''}` : '尚未查询 · 可在账号页读取';
    }
  }
  function selectOptions(id, choices, selected, emptyLabel) {
    const select = $(id); select.replaceChildren();
    if (emptyLabel) { const option = document.createElement('option'); option.value = ''; option.textContent = emptyLabel; select.appendChild(option); }
    choices.forEach(([value, label]) => { const option = document.createElement('option'); option.value = String(value); option.textContent = label; select.appendChild(option); });
    select.value = String(selected);
  }
  const localImageEdit = () => state.kind === 'image' && state.mode === 'edit';
  const uploadedVideo = () => state.kind === 'video' && ['edit', 'extend', 'upscale'].includes(state.mode);
  const supportsShot = () => ['text', 'ingredients', 'edit'].includes(state.mode);
  const needsImages = () => state.kind === 'video' && ['animate', 'ingredients', 'edit'].includes(state.mode);
  const maximumImages = () => state.mode === 'animate' ? 1 : state.mode === 'ingredients' ? 7 : 3;
  function materialValidation() {
    if (localImageEdit() && !state.originalImage) return '请选择一张原图。';
    if (uploadedVideo() && !state.originalVideo) return '请选择本地 MP4 原视频。';
    if (needsImages() && (state.referenceImages.length > maximumImages() || state.mode !== 'edit' && !state.referenceImages.length)) return state.mode === 'animate' ? '请选择一张图片。' : `请选择 1–${maximumImages()} 张参考图片。`;
    return '';
  }
  function clearReferenceImages() { state.referenceImages.forEach(image => URL.revokeObjectURL(image.url)); state.referenceImages = []; $('referencePreview').replaceChildren(); }
  function renderParameters(locked) {
    const image = state.kind === 'image', video = state.kind === 'video';
    selectOptions('generationMode', image ? [['text', '文生图'], ['edit', '图生图']] : Object.entries({ text: '文生视频', animate: '图生视频 · 单图', ingredients: '图生视频 · 多图参考', edit: '编辑视频', extend: '续写视频', upscale: '放大至 1080p' }), state.mode);
    $('modeField').hidden = state.kind === 'speech'; $('imageSpec').hidden = !image; $('imageSpecHint').hidden = !image; $('videoSpec').hidden = !video || state.mode === 'upscale'; $('speechSpec').hidden = state.kind !== 'speech';
    selectOptions('speechVoice', voiceOptions.map(voice => [voice.id, `${voice.name} · ${voice.description}`]), state.speechVoice);
    const selectedVoice = voiceOptions.find(voice => voice.id === state.speechVoice);
    $('speechVoiceHint').textContent = selectedVoice ? `${selectedVoice.name} · ${selectedVoice.description}` : '请选择声线。';
    selectOptions('imageAspect', state.mode === 'edit' ? [['auto', '自动 · 上游返回实际比例']] : [['1:1', '1:1 · 方图'], ['16:9', '16:9 · 横图'], ['9:16', '9:16 · 竖图']], state.mode === 'edit' ? 'auto' : state.imageAspect);
    selectOptions('imageStyle', state.mode === 'edit' ? [['auto', '在提示词中描述样式']] : imageStyles, state.mode === 'edit' ? 'auto' : state.imageStyle);
    selectOptions('imageCount', state.mode === 'edit' ? [['1', '1 张']] : [['1', '1 张'], ['2', '2 张'], ['3', '3 张'], ['4', '4 张']], state.mode === 'edit' ? 1 : state.imageCount);
    $('imageCountField').hidden = state.mode === 'edit';
    $('imageSpecHint').textContent = state.mode === 'edit' ? '原图随一次请求提交。编辑接口由上游返回实际尺寸；修改构图或风格请写在提示词中。' : `选择比例、样式和生成数量（${state.imageCount} 张）。横竖图返回上游实际像素尺寸。`;
    if (video) {
      const [ratio, resolution] = videoSizes[state.size];
      selectOptions('videoAspect', [resolution === '1080p' ? '1920x1080' : '1280x720', resolution === '1080p' ? '1080x1920' : '720x1280'].map(size => [size, videoSizes[size][0] === '9:16' ? '9:16 · 竖屏' : '16:9 · 横屏']), state.size);
      selectOptions('videoResolution', [ratio === '9:16' ? '720x1280' : '1280x720', ratio === '9:16' ? '1080x1920' : '1920x1080'].map(size => [size, `${videoSizes[size][1]} · ${size.replace('x', '×')}`]), state.size);
      $('videoSecondsLabel').textContent = state.mode === 'extend' ? '追加时长' : '输出时长';
      selectOptions('videoSeconds', durations.map(seconds => [seconds, state.mode === 'extend' ? `追加 ${seconds} 秒` : `${seconds} 秒`]), state.seconds);
      selectOptions('videoShot', [['auto', '自动'], ['long_take', '长镜头'], ['montage', '蒙太奇']], state.shotMode);
      $('videoShotField').hidden = !supportsShot();
    }
    $('imageUploadPanel').hidden = !localImageEdit(); $('referencePanel').hidden = !needsImages();
    $('videoUploadPanel').hidden = !uploadedVideo();
    $('videoUploadHint').textContent = state.mode === 'edit' ? 'MP4，最多 50 MiB、3–60 秒；超过 10 秒时只编辑末 10 秒。原视频随一次生成请求提交。' : state.mode === 'extend' ? 'MP4，最多 50 MiB、3–60 秒；超过 10 秒时续写末 10 秒。选择追加时长并描述后续内容，成品最多 20 秒。原视频随一次生成请求提交。' : 'MP4，最多 50 MiB、3–10 秒。放大至 1080p，保留原视频方向和时长；原视频随一次生成请求提交。';
    $('referenceFiles').multiple = maximumImages() > 1;
    $('referenceLabel').textContent = state.mode === 'animate' ? '起始图片（1 张）' : `参考图片（${state.mode === 'edit' ? '可选，' : ''}1–${maximumImages()} 张）`;
    $('referenceHint').textContent = 'PNG、JPEG、WebP，图片总量最多 10 MiB。仅保留在本页；点击生成时才随请求提交。';
    $('materialError').textContent = state.materialError || '';
    if (state.materialError) $('formError').textContent = state.materialError;
    $('promptCount').textContent = `${$('prompt').value.length} / ${state.kind === 'speech' ? 4096 : 32000}`;
    ['generationMode', 'imageAspect', 'imageStyle', 'imageCount', 'speechVoice', 'speechFormat', 'videoAspect', 'videoResolution', 'videoSeconds', 'videoShot', 'videoFile', 'referenceFiles', 'imageFile'].forEach(id => { $(id).disabled = locked; });
    if (localImageEdit()) { $('imageAspect').disabled = true; $('imageStyle').disabled = true; }
    if (video && state.mode === 'upscale') ['videoAspect', 'videoResolution', 'videoSeconds'].forEach(id => { $(id).disabled = true; });
    $('modelBadge').textContent = state.kind === 'image' ? `Vids Image${localImageEdit() ? '' : ` · ${state.imageCount} 张`}` : state.kind === 'video' ? 'Vids Video' : 'Vids Speech';
    $('costHint').textContent = state.kind === 'speech' ? '点击生成才提交语音请求。' : localImageEdit() ? '点击生成将提交原图与提示词，并消耗图片额度。' : state.kind === 'image' ? `点击生成将请求 ${state.imageCount} 张图片，并消耗图片额度。` : '点击生成将消耗视频额度。';
  }
  function render() {
    const model = models[state.kind], task = state.task;
    $('editorTitle').textContent = model.title; $('modelHint').textContent = model.hint;
    $('promptLabel').textContent = state.kind === 'speech' ? '朗读文本' : '提示词';
    $('prompt').maxLength = state.kind === 'speech' ? 4096 : 32000;
    const locked = state.busy || state.materialBusy;
    let duplicate = null;
    try { duplicate = matchingRequest(bodyFor()); } catch (_) {}
    renderParameters(locked);
    modelButtons().forEach(button => { button.classList.toggle('active', button.dataset.kind === state.kind); button.disabled = locked; button.setAttribute('aria-pressed', String(button.dataset.kind === state.kind)); });
    $('prompt').disabled = locked;
    $('generate').disabled = locked || !!duplicate || !usable(state.kind) || !$('prompt').value.trim() || !!materialValidation() || !!state.materialError;
    $('generate').textContent = state.busy ? '正在处理…' : duplicate ? '原请求待确认，请修改内容' : `${model.button}${state.kind === 'speech' ? '' : `（消耗${state.kind === 'image' ? '图片' : '视频'}额度）`}`;
    if (duplicate && !locked) $('costHint').textContent = '相同请求的结果尚未确认，请先检查原任务；也可修改提示词、素材或参数，或切换模型继续操作。';
    $('refreshStatus').disabled = state.statusBusy;
    $('availability').textContent = state.statusBusy ? '读取接入状态…' : ready(state.kind) ? '已就绪' : usable(state.kind) ? '生成前将恢复会话' : '当前模型暂无可用账号';
    $('accountState').hidden = ready(state.kind) && !state.statusBusy;
    if (!state.statusBusy) $('accountState').textContent = !state.configured ? '尚未接入 Vids 账号。Flow 账号仍需单独绑定 Vids 会话，请在管理后台完成接入。' : usable(state.kind) && !ready(state.kind) ? '生成前将恢复会话。已有绑定会先验证登录，恢复成功后才生成。' : `当前${model.title}暂无可用 Vids 账号，请在管理后台检查接入与启用状态。`;
    $('emptyResult').hidden = !!task; $('result').hidden = !task;
    if (!task) { $('emptyTitle').textContent = usable(state.kind) ? ready(state.kind) ? '等待生成' : '生成前将恢复会话' : '尚未接入 Vids 账号'; $('emptyText').textContent = usable(state.kind) ? ready(state.kind) ? '输入提示词后生成，成品会显示在这里。' : '填写内容后点击生成，后台先恢复并验证登录。' : '暂无可用 Vids 账号，请在管理后台完成接入。'; $('retry').hidden = true; $('requestDetails').textContent = '尚无任务；历史任务可使用收据恢复。'; }
    else {
      const s = task.status; $('statusText').textContent = s === 'completed' ? '生成完成' : s === 'unknown' ? '结果尚未确认' : s === 'download_failed' ? '成品下载待恢复' : terminal(task) ? '生成失败' : '正在处理';
      $('statusDot').className = `dot ${s === 'completed' ? 'done' : terminal(task) || s === 'unknown' || s === 'download_failed' ? 'error' : 'running'}`;
      $('resultMessage').textContent = messageText(task.message);
      $('checkResult').hidden = !['unknown', 'generating', 'received', 'download_failed'].includes(s) || !task.receipt;
      $('recover').hidden = !['received', 'download_failed'].includes(s) || !task.receipt;
      $('readContent').hidden = s !== 'completed' || !(task.content || task.outputs?.length) || state.blobReceipt === (task.receipt || task.key);
      $('retry').hidden = s !== 'failed' || !task.body || !task.key;
      $('requestDetails').textContent = JSON.stringify({ model: task.body?.model || models[task.kind]?.model, idempotency_key: task.key || null, receipt_id: task.receipt || null, status: s, endpoint: task.path || null, request: task.body ? Object.fromEntries(Object.entries(task.body).map(([key, value]) => [key, key === 'image_base64' ? '[图片 Base64 已省略]' : key === 'video_base64' ? '[视频 Base64 已省略]' : key === 'images_base64' ? value.map(() => '[图片 Base64 已省略]') : value])) : null, error: task.error || null, error_operation: task.errorOperation || null, upstream_error: task.upstreamError || null }, null, 2);
    }
    ['checkResult', 'recover', 'readContent', 'retry'].forEach(id => { $(id).disabled = state.busy; });
    ['queryHistory', 'sessionHistory'].forEach(id => { $(id).disabled = state.busy || state.materialBusy; });
    renderHistory(); renderQuota(); resize();
  }
  async function perform(action, label = '') {
    if (state.busy || state.materialBusy) return; state.busy = true; $('formError').textContent = ''; render();
    try { await action(); }
    catch (error) {
      const hint = error.status ? failureMessage(error) : label && error instanceof TypeError ? '请检查网络或代理后重试本次查询或下载；不会重新生成。' : messageText(error.message);
      if (state.task && label) {
        state.task.message = `${label}未完成。${hint}${state.task.status === 'completed' ? '成品生成已完成，请点击“读取成品”重试。' : '原任务状态和收据已保留。'}`;
      } else $('formError').textContent = hint;
    }
    finally { state.busy = false; render(); }
  }
  async function loadStatus() {
    if (state.statusBusy) return; state.statusBusy = true; $('accountState').textContent = '正在读取本地 Vids 接入状态…'; render();
    try {
      const response = await fetch('/api/admin/vids/status', { credentials: 'same-origin', redirect: 'error' });
      if (response.status === 401) { window.location.href = '/login'; return; }
      const data = await readJSON(response); state.configured = data.configured === true;
      state.configuredCount = Number.isSafeInteger(data.configured_count) && data.configured_count >= 0 ? data.configured_count : 0;
      state.quota = data.quota || {};
      state.available = Object.fromEntries(Object.keys(models).map(kind => [kind, Number.isSafeInteger(data.available?.[kind]) && data.available[kind] > 0 ? data.available[kind] : 0]));
      state.recoverable = Object.fromEntries(Object.keys(models).map(kind => [kind, Number.isSafeInteger(data.recoverable?.[kind]) && data.recoverable[kind] > 0 ? data.recoverable[kind] : 0]));
    } catch (_) { state.configured = false; state.available = {}; state.recoverable = {}; state.quota = Object.fromEntries(Object.entries(state.quota).map(([kind, quota]) => [kind, { ...quota, stale: true }])); $('formError').textContent = '无法读取接入状态，请刷新重试。'; }
    finally { state.statusBusy = false; render(); }
  }
  function readImage(file) {
    if (!file || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 10 * 1024 * 1024) throw new Error('请选择 PNG、JPEG 或 WebP，单张不超过 10 MiB。');
    return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(new Error('无法读取图片。')); reader.onload = () => { const base64 = String(reader.result).split(',')[1] || '', raw = atob(base64.slice(0, 32)); const valid = raw.startsWith('\x89PNG\r\n\x1a\n') || raw.startsWith('\xff\xd8\xff') || raw.startsWith('RIFF') && raw.slice(8, 12) === 'WEBP'; if (!valid) reject(new Error('图片文件内容不是 PNG、JPEG 或 WebP。')); else resolve({ name: file.name, base64, url: URL.createObjectURL(file) }); }; reader.readAsDataURL(file); });
  }
  function renderImages() { const list = $('referencePreview'); list.replaceChildren(); state.referenceImages.forEach((image, index) => { const item = document.createElement('div'); item.className = 'preview-item'; const img = document.createElement('img'); img.src = image.url; img.alt = image.name; const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', `移除 ${image.name}`); remove.addEventListener('click', () => { if (state.busy || state.materialBusy) return; URL.revokeObjectURL(image.url); state.referenceImages.splice(index, 1); state.materialError = ''; $('formError').textContent = ''; renderImages(); render(); }); const name = document.createElement('span'); name.textContent = image.name; item.append(img, remove, name); list.appendChild(item); }); }
  function newKey() { return window.crypto?.randomUUID ? window.crypto.randomUUID() : `vids-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  async function receiptFor(key) { if (!window.crypto?.subtle) return ''; const data = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(key)); return 'vids_' + Array.from(new Uint8Array(data), byte => byte.toString(16).padStart(2, '0')).join(''); }
  function bodyFor() {
    const prompt = $('prompt').value;
    if (!prompt.trim() || prompt.length > (state.kind === 'speech' ? 4096 : 32000)) throw new Error(`请输入非空内容，最多 ${state.kind === 'speech' ? 4096 : 32000} 个字符。`);
    if (!usable(state.kind)) throw new Error('当前类型暂无可用 Vids 账号，请在管理后台完成接入。');
    const error = materialValidation() || state.materialError; if (error) throw new Error(error);
    if (state.kind === 'image') {
      if (state.mode === 'edit') return { kind: 'image', mode: 'edit', prompt, image_base64: state.originalImage.base64 };
      if (!Number.isInteger(state.imageCount) || state.imageCount < 1 || state.imageCount > 4) throw new Error('请选择 1–4 张图片。');
      const body = { model: 'vids-image', prompt, n: state.imageCount, size: state.imageAspect === '1:1' ? '1024x1024' : 'auto', response_format: 'b64_json' };
      if (state.imageAspect !== '1:1') body.aspect_ratio = state.imageAspect;
      if (state.imageStyle !== 'auto') body.style = state.imageStyle;
      return body;
    }
    if (state.kind === 'video') {
      if (!durations.includes(state.seconds) || !videoSizes[state.size]) throw new Error('请选择视频时长、比例和清晰度。');
      if (state.mode === 'text') return { model: 'vids-video', prompt, seconds: state.seconds, size: state.size, ...(state.shotMode === 'auto' ? {} : { shot_mode: state.shotMode }) };
      const body = { kind: 'video', mode: state.mode, prompt, ...(state.mode === 'upscale' ? {} : { seconds: state.seconds, size: state.size }) };
      if (state.shotMode !== 'auto' && supportsShot()) body.shot_mode = state.shotMode;
      if (uploadedVideo()) body.video_base64 = state.originalVideo.base64;
      if (state.mode === 'animate') body.image_base64 = state.referenceImages[0].base64;
      else if (needsImages() && state.referenceImages.length) body.images_base64 = state.referenceImages.map(image => image.base64);
      return body;
    }
    if (!voiceOptions.some(voice => voice.id === state.speechVoice)) throw new Error('请选择预设声线。');
    return { model: 'vids-speech', input: prompt, voice: state.speechVoice, response_format: 'wav' };
  }
  function applyReceipt(task, data) {
    if (!data || !receiptPattern.test(data.id) || task.receipt && task.receipt !== data.id) throw new Error('返回收据与原任务不匹配。');
    if (data.kind !== undefined && (!['image', 'video', 'speech'].includes(data.kind) || task.body && task.kind !== data.kind)) throw new Error('返回收据的模型与原任务不匹配。');
    const s = data.vids_status || data.status;
    if (!['completed', 'unknown', 'generating', 'in_progress', 'received', 'failed', 'download_failed', 'no_result', 'specification_mismatch'].includes(s)) throw new Error('收据状态无效。');
    if (data.kind) task.kind = data.kind;
    task.receipt = data.id;
    const outputs = data.outputs;
    if (outputs !== undefined && (!Array.isArray(outputs) || outputs.length > 4 || outputs.some((item, index) => item?.index !== index || typeof item.content_url !== 'string'))) throw new Error('返回成品列表无效。');
    task.outputs = outputs ? outputs.map(item => {
      const content = contentPath(item.content_url, task), index = Number(new URL(content, window.location.origin).searchParams.get('index') || 0);
      if (index !== item.index) throw new Error('成品索引与下载地址不匹配。');
      return { index, content };
    }) : [];
    task.content = data.content_url ? contentPath(data.content_url, task) : null; task.status = s === 'in_progress' ? 'generating' : s;
    task.error = typeof data.error === 'string' && /^[a-z0-9_]{1,80}$/.test(data.error) ? data.error : typeof data.error?.code === 'string' && /^[a-z0-9_]{1,80}$/.test(data.error.code) ? data.error.code : null;
    task.errorOperation = ['upload-start', 'upload-finalize', 'generate', 'getSettings'].includes(data.error_operation) ? data.error_operation : null;
    task.upstreamError = safeUpstreamError(data.upstream_error);
    task.message = receiptMessage(task);
  }
  async function preview(task) {
    if (task.status !== 'completed' || !(task.content || task.outputs?.length)) throw new Error('任务成品尚未就绪。');
    if (task.outputs?.length && task.outputs.every(item => item.blob instanceof Blob)) {
      renderMediaItems(task.outputs, task); task.message = `成品已生成，可以预览或下载${task.outputs.length > 1 ? `全部 ${task.outputs.length} 张图片` : ''}。`; return;
    }
    const outputs = (task.outputs?.length ? task.outputs : [{ index: 0, content: task.content }]).map(item => ({ ...item, content: contentPath(item.content, task) })), items = [];
    for (const item of outputs) {
      const response = await request(item.content); if (!response.ok) await readJSON(response);
      items.push({ index: item.index, blob: await response.blob() });
    }
    renderMediaItems(items, task); task.message = `成品已生成，可以预览或下载${items.length > 1 ? `全部 ${items.length} 张图片` : ''}。`;
  }
  async function submit(retry = false) {
    if (retry && (!state.task || state.task.status !== 'failed' || !state.task.body)) return;
    let task = state.task;
    if (!retry) {
      const error = materialValidation() || state.materialError;
      if (error) throw new Error(error);
      if (!$('prompt').value.trim() || $('prompt').value.length > (state.kind === 'speech' ? 4096 : 32000) || !usable(state.kind)) throw new Error('请检查提示词和账号接入状态。');
      const body = bodyFor();
      if (matchingRequest(body)) throw new Error('相同请求的结果尚未确认，请修改内容或查询原收据；原请求不会重复提交。');
      const key = newKey();
      task = { kind: state.kind, body, key, path: prefix + (body.kind ? '/vids/generations' : models[state.kind].endpoint), receipt: await receiptFor(key), status: 'generating', time: new Date().toLocaleTimeString(), message: '正在生成，请等待服务器返回。' };
      clearMedia(); state.task = task; state.history.unshift(task);
    } else { if (!usable(task.kind)) throw new Error('原任务类型暂无可用账号，请刷新接入状态。'); task.status = 'generating'; task.message = '正在以原任务参数和原幂等键提交。'; }
    render();
    if (window.matchMedia?.('(max-width: 640px)').matches) $('resultTitle').scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    try {
      const response = await request(task.path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': task.key }, body: JSON.stringify(task.body) });
      const headerReceipt = response.headers.get('x-vids-generation-id');
      if (headerReceipt && receiptPattern.test(headerReceipt) && (!task.receipt || task.receipt === headerReceipt)) task.receipt = headerReceipt;
      if (task.kind === 'speech' && response.ok && !response.headers.get('content-type')?.includes('json')) {
        renderMedia(await response.blob(), task); task.status = 'completed'; task.message = '语音已生成，可以播放或下载。';
      } else {
        const data = await readJSON(response, task.receipt);
        if (task.kind === 'image' && !task.body.kind) {
          if (!Array.isArray(data.data) || data.data.length !== task.body.n) throw new Error('服务器返回的图片数量与请求不匹配。');
          const items = data.data.map((item, index) => {
            if (typeof item?.b64_json === 'string' && item.b64_json.length <= 90 * 1024 * 1024 && /^[A-Za-z0-9+/]+={0,2}$/.test(item.b64_json)) {
              const raw = atob(item.b64_json), bytes = Uint8Array.from(raw, char => char.charCodeAt(0));
              if (bytes[0] !== 0xff || bytes[1] !== 0xd8) throw new Error('返回图片不是受支持的 JPEG。');
              return { index, blob: new Blob([bytes], { type: 'image/jpeg' }) };
            }
            if (typeof item?.url === 'string') {
              const content = contentPath(item.url, task), selected = Number(new URL(content, window.location.origin).searchParams.get('index') || 0);
              if (selected !== index) throw new Error('成品索引与下载地址不匹配。');
              return { index, content };
            }
            throw new Error('服务器未返回有效的图片成品。');
          });
          task.outputs = items.map(item => ({ index: item.index, content: item.content || null, blob: item.blob || null }));
          task.content = task.outputs[0]?.content || null; task.status = 'completed';
          for (const item of task.outputs) if (item.content && !item.blob) { const response = await request(item.content); if (!response.ok) await readJSON(response); item.blob = await response.blob(); }
          renderMediaItems(task.outputs, task); task.message = `图片已生成，可以预览或下载${task.outputs.length > 1 ? `全部 ${task.outputs.length} 张图片` : ''}。`;
        } else { applyReceipt(task, data); if (task.status === 'completed' && (task.content || task.outputs?.length)) await preview(task); }
      }
    } catch (error) {
      if (error.receipt && error.receipt.kind && error.receipt.kind !== task.kind) error.receipt = null;
      if (error.receipt) {
        try { const updated = { ...task }; applyReceipt(updated, error.receipt); Object.assign(task, updated); return; }
        catch (_) { error.receipt = null; }
      }
      if (!task.error && task.status !== 'completed') task.error = error.code || messageText(error.message);
      if (task.status === 'completed') { task.message = '生成已完成，成品暂时无法读取。请检查网络或代理后点击“读取成品”重试，不会重新生成。'; return; }
      const found = String(error.message).match(/vids_[a-f0-9]{64}/g);
      if (found?.length === 1 && (!task.receipt || task.receipt === found[0])) task.receipt = found[0];
      const pending = /\bis (generating|received|unknown)\b/.exec(error.message || '');
      const knownFailure = !['image_not_ready', 'speech_not_ready', 'video_not_ready', 'not_ready'].includes(error.code) &&
        (error.status && error.status < 500 || ['image_failed', 'speech_failed', 'account_disabled', 'session_unavailable', 'session_authentication_failed', 'session_persistence_failed', 'account_lookup_failed'].includes(error.code));
      task.status = pending ? pending[1] : knownFailure ? 'failed' : 'unknown';
      task.message = task.status === 'failed' ? failureMessage(error, task.kind) : receiptMessage(task);
    }
  }
  async function inspect(task, autoPreview = true) {
    if (!task || !receiptPattern.test(task.receipt || '')) throw new Error('当前任务未返回有效收据。请在请求详情中保留原幂等键，填写历史收据后恢复。');
    const data = await readJSON(await request(`${prefix}/vids/generations/${task.receipt}`)); applyReceipt(task, data);
    if (autoPreview && task.status === 'completed' && (task.content || task.outputs?.length)) await preview(task);
  }
  async function recover() {
    const task = state.task;
    if (!task || !['received', 'download_failed'].includes(task.status) || !receiptPattern.test(task.receipt)) throw new Error('请先检查原任务，确认已接受且需要恢复下载。');
    applyReceipt(task, await readJSON(await request(`${prefix}/vids/generations/${task.receipt}/download`, { method: 'POST' })));
    if (task.status === 'completed' && (task.content || task.outputs?.length)) await preview(task);
  }
  function init() {
    if (new URLSearchParams(window.location.search).get('embedded') === '1') document.body.classList.add('embedded');
    const canEdit = () => !state.busy && !state.materialBusy;
    modelButtons().forEach(button => button.addEventListener('click', () => { if (canEdit()) { state.kind = button.dataset.kind; state.mode = 'text'; clearReferenceImages(); state.materialError = ''; $('formError').textContent = ''; render(); } }));
    $('generationMode').addEventListener('change', event => { if (!canEdit()) return; state.mode = event.target.value; clearReferenceImages(); state.materialError = ''; $('formError').textContent = ''; render(); });
    $('videoAspect').addEventListener('change', event => { if (canEdit()) { state.size = event.target.value; render(); } });
    $('videoResolution').addEventListener('change', event => { if (canEdit()) { state.size = event.target.value; render(); } });
    $('videoSeconds').addEventListener('change', event => { if (canEdit()) { state.seconds = Number(event.target.value); render(); } });
    $('imageAspect').addEventListener('change', event => { if (canEdit()) { state.imageAspect = event.target.value; render(); } });
    $('imageStyle').addEventListener('change', event => { if (canEdit()) { state.imageStyle = event.target.value; render(); } });
    $('imageCount').addEventListener('change', event => { if (canEdit()) { state.imageCount = Number(event.target.value); render(); } });
    $('speechVoice').addEventListener('change', event => { if (canEdit()) { state.speechVoice = event.target.value; render(); } });
    $('videoShot').addEventListener('change', event => { if (canEdit()) { state.shotMode = event.target.value; render(); } });
    $('prompt').addEventListener('input', render);
    $('referenceFiles').addEventListener('change', async event => { if (!canEdit()) return; state.materialBusy = true; render(); try { const files = [...(event.target.files || [])]; if (files.length > maximumImages()) throw new Error(`最多选择 ${maximumImages()} 张图片。`); if (files.reduce((total, file) => total + file.size, 0) > 10 * 1024**2) throw new Error('参考图片总量不能超过 10 MiB。'); clearReferenceImages(); for (const file of files) state.referenceImages.push(await readImage(file)); state.materialError = ''; renderImages(); } catch (error) { clearReferenceImages(); state.materialError = messageText(error.message); } finally { event.target.value = ''; state.materialBusy = false; render(); } });
    $('imageFile').addEventListener('change', async event => {
      if (!canEdit() || !event.target.files?.length) return;
      state.materialBusy = true; render();
      try {
        const next = await readImage(event.target.files[0]);
        if (state.originalImage?.url) URL.revokeObjectURL(state.originalImage.url);
        state.originalImage = next; state.materialError = ''; $('formError').textContent = ''; $('imagePreview').replaceChildren();
        const image = document.createElement('img'); image.src = next.url; image.alt = next.name; $('imagePreview').appendChild(image);
        const name = document.createElement('span'); name.className = 'hint'; name.textContent = next.name; $('imagePreview').appendChild(name);
        $('imageMessage').textContent = '原图已选择，填写提示词后点击生成。';
      } catch (error) { state.materialError = messageText(error.message); }
      finally { state.materialBusy = false; render(); }
    });
    $('videoFile').addEventListener('change', async event => {
      if (!canEdit() || !event.target.files?.length) return;
      state.materialBusy = true; render();
      try {
        const file = event.target.files[0];
        if (!file.size || file.size > 50 * 1024**2 || !(file.type === 'video/mp4' || /\.mp4$/i.test(file.name))) throw new Error('请选择不超过 50 MiB 的 MP4。');
        const base64 = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(new Error('无法读取视频。')); reader.onload = () => resolve(String(reader.result).split(',')[1] || ''); reader.readAsDataURL(file); });
        if (atob(base64.slice(0, 32)).slice(4, 8) !== 'ftyp') throw new Error('文件内容不是 MP4。');
        state.originalVideo = { name: file.name, base64 }; $('videoFileName').textContent = file.name; state.materialError = '';
      } catch (error) { state.materialError = messageText(error.message); }
      finally { state.materialBusy = false; render(); }
    });
    $('generate').addEventListener('click', () => perform(() => submit()));
    $('refreshStatus').addEventListener('click', loadStatus);
    $('checkResult').addEventListener('click', () => perform(() => inspect(state.task), '检查结果'));
    $('recover').addEventListener('click', () => perform(recover, '恢复下载'));
    $('readContent').addEventListener('click', () => perform(() => preview(state.task), '读取成品'));
    $('retry').addEventListener('click', () => perform(() => submit(true)));
    $('queryHistory').addEventListener('click', () => perform(async () => {
      const id = $('historyReceipt').value.trim(); if (!receiptPattern.test(id)) throw new Error('请输入 vids_ 加 64 位小写十六进制字符的收据 ID。');
      const task = state.history.find(item => item.receipt === id) || { kind: state.kind, receipt: id, status: 'generating', time: '历史收据', message: '正在读取历史收据。' };
      await inspect(task, false); if (!state.history.includes(task)) state.history.unshift(task); clearMedia(); state.task = task;
      if (task.status === 'completed' && (task.content || task.outputs?.length)) await preview(task);
    }, '查询历史收据'));
    $('sessionHistory').addEventListener('change', () => { if (state.busy || state.materialBusy || $('sessionHistory').value === '') return; const task = state.history[Number($('sessionHistory').value)]; if (task) { clearMedia(); state.task = task; render(); } });
    document.querySelector('details')?.addEventListener('toggle', resize);
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(resize).observe(document.body);
    window.addEventListener('pagehide', () => { clearMedia(); clearReferenceImages(); if (state.originalImage?.url) URL.revokeObjectURL(state.originalImage.url); });
    window.addEventListener('message', event => {
      if (event.origin === window.location.origin && event.source === window.parent && event.data?.type === 'lay4flow.vids.status.refresh') loadStatus();
    });
    render(); loadStatus();
  }
  document.addEventListener('DOMContentLoaded', init);
})();
