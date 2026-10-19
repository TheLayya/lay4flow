(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const unified = document.body.dataset?.accountManagement === 'unified';
  const base = '/api/admin/vids/accounts';
  const onboardingBase = '/api/admin/vids/onboard';
  const state = { accounts: [], candidates: [], results: new Map(), repairs: new Map(), reconnecting: null, candidatesLoaded: false, candidatesError: '', onboarding: false, configured: 0, runtimeReady: false, runtimeError: null, loading: false, loaded: false, refreshing: new Set(), message: '', error: false };
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const safeText = value => String(value || '').slice(0, 300).replace(/https?:\/\/\S+/gi, '[地址已隐藏]');
  const time = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '尚未查询';
  const verifiedIdentity = account => account.ready === true && account.identity_verified === true && typeof account.vids_identity === 'string' && !!account.vids_identity.trim();
  const readyAccount = account => state.runtimeReady && account.ready === true && verifiedIdentity(account);
  const repairMessage = code => ({ busy: '账号正在使用，请等待当前任务结束后恢复。', identity_mismatch: 'Vids 身份与 Flow 账号不一致，恢复已阻止。', account_mismatch: '已保存会话与当前身份不一致，恢复已阻止。', session_identity_mismatch: 'Vids 身份与 Flow 账号不一致，恢复已阻止。', login_required: '需要重新登录 Google 账号后再恢复。', captchaRequired: '需要完成 Google 验证码后再恢复。', captcha_required: '需要完成 Google 验证码后再恢复。', profile_refresh_failed: '复用账号登录时读取状态失败，请重新恢复会话。', session_authentication_failed: '该账号的 Vids 登录已失效，请重新登录后恢复会话。', session_unavailable: '会话当前无法恢复，请检查 Vids 接入状态。', session_persistence_failed: '会话保存失败，已保留原绑定，请检查接入状态。', connection_unknown: '恢复结果尚未确认，请重新读取状态；不会自动重试。', session_repair_failed: '恢复未完成，已保留原绑定，请检查登录状态后重试。' }[code] || onboardingMessage(code));
  const onboardingMessage = code => ({ login_required: '需要重新登录 Google 账号后再接入。', captchaRequired: '需要完成 Google 验证码后再接入。', captcha_required: '需要完成 Google 验证码后再接入。', identity_mismatch: 'Vids 响应返回的账号身份与 Flow 账号不同，已阻止接入。', identity_unavailable: 'Vids 响应暂未提供账号身份，接入尚未完成，请稍后重试。', account_mismatch: '已保存的 Vids 绑定与当前身份不一致，已阻止接入。', already_bound: '账号已接入，请重新读取状态。', profile_unavailable: '现有账号登录暂不可用，请在 Flow 账号管理中检查登录状态。', document_required: '未找到可用 Vids 文档，接入未完成。', unexpected_page: 'Vids 账号页面暂不可用，请检查登录状态。', invalid_configuration: 'Vids 接入配置无效，请检查配置。', runtime_unavailable: 'Vids 服务暂不可用，请稍后重新读取。', account_not_found: 'Flow 账号已不存在，请重新读取列表。', invalid_request: '接入参数无效，请刷新页面后重试。', authentication_required: '管理员登录已失效，请重新登录。', forbidden: '没有管理员操作权限，请重新登录。', connection_unknown: '接入结果尚未确认；请重新读取状态，不会自动重试。', onboarding_failed: '接入未完成，已保留账号，可检查状态后重试。' }[code] || '接入未完成，已保留账号，可检查状态后重试。');
  function sessionMessage(account) {
    const error = account.runtime_error || (!state.runtimeReady ? state.runtimeError : null);
    if (['auth_expired', 'authentication_expired'].includes(account.session_state) || error === 'session_authentication_failed') return 'Vids 登录已失效，请恢复会话';
    if (account.session_state === 'identity_mismatch' || ['account_mismatch', 'identity_mismatch', 'session_identity_mismatch'].includes(error)) return 'Vids 与关联账号不一致，已阻止使用';
    if (error === 'invalid_configuration') return 'Vids 接入配置无效，需要检查配置';
    if (error === 'account_not_found') return '关联 Flow 账号已不存在，请检查原账号配置';
    if (error === 'session_unavailable') return 'Vids 会话恢复失败，需要检查接入状态';
    if (error === 'profile_refresh_failed') return 'Vids 登录状态读取失败，请重试恢复会话';
    if (readyAccount(account)) return '已就绪';
    if (!verifiedIdentity(account)) return 'Vids 身份尚未验证';
    return state.runtimeReady ? `Vids 会话不可用${error ? `（错误：${safeText(error)}）` : ''}` : 'Vids 会话尚未恢复';
  }
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function resize() {
    if (!document.body.classList.contains('embedded') || !window.parent || window.parent === window) return;
    const report = () => window.parent.postMessage({ type: 'lay4flow.vids.accounts.resize', height: Math.min(20000, Math.max(250, Math.ceil(document.body.scrollHeight + 4))) }, window.location.origin);
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(report); else report();
  }
  async function request(path, options = {}) {
    if (path !== base + '/detail' && path !== onboardingBase + '/candidates' && !new RegExp(`^${base}/[1-9][0-9]*/quota-refresh$`).test(path) && !new RegExp(`^${onboardingBase}/[1-9][0-9]*(?:/reconnect)?$`).test(path)) throw new Error('请求地址无效。');
    const response = await fetch(path, { ...options, credentials: 'same-origin', redirect: 'error' });
    if (response.status === 401) { window.location.href = '/login'; const error = new Error('登录已失效，请重新登录。'); error.code = 'authentication_required'; throw error; }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(safeText(data.error?.message || data.detail || `读取失败（HTTP ${response.status}）。`));
      error.code = response.status === 403 ? 'forbidden' : data.error?.code || 'onboarding_failed';
      throw error;
    }
    return data;
  }
  function renderCandidates(button = $('onboardAll'), status = $('onboardingStatus'), list = $('candidates')) {
    const available = state.candidates.filter(account => { const status = state.results.get(account.token_id)?.state; return !status || status === 'failed'; });
    button.disabled = state.loading || state.onboarding || state.reconnecting !== null || state.refreshing.size > 0 || !state.candidatesLoaded || !!state.candidatesError || available.length === 0;
    button.textContent = state.onboarding ? '正在逐个接入…' : '接入所有未绑定账号';
    status.classList.toggle('error', !!state.candidatesError);
    status.textContent = state.candidatesError || (state.onboarding ? '后台正在顺序接入，完成一个再处理下一个。' : !state.candidatesLoaded ? '正在读取未绑定账号…' : state.candidates.length ? `${state.candidates.length} 个 Flow 账号尚未绑定 Vids。` : '所有 Flow 账号均已绑定 Vids。');
    const rows = new Map(state.candidates.map(account => [account.token_id, account]));
    state.results.forEach((result, id) => { if (!rows.has(id)) rows.set(id, { token_id: id, email: result.email }); });
    list.replaceChildren();
    rows.forEach(account => {
      const row = node('li'), result = state.results.get(account.token_id); row.dataset.tokenId = String(account.token_id);
      row.appendChild(node('span', safeText(account.email) || '邮箱未知'));
      row.appendChild(node('span', result?.message || '待接入 · 将复用已有登录', result?.state === 'ready' ? 'state ready' : 'muted'));
      list.appendChild(row);
    });
    list.hidden = rows.size === 0;
  }
  function quotaCell(account, kind) {
    const cell = node('td', undefined, 'quota'), quota = account.last_quota?.[kind];
    cell.appendChild(node('strong', count(quota?.available) ? String(quota.available) : '—'));
    if (count(quota?.total) && count(quota?.consumed)) cell.appendChild(node('small', `总额 ${quota.total} · 已用 ${quota.consumed}`));
    if (quota?.stale || quota?.error) cell.appendChild(node('small', '上次缓存 · 已过期', 'stale'));
    else if (!count(quota?.available)) cell.appendChild(node('small', '尚未查询'));
    if (quota?.error) cell.appendChild(node('small', safeText(quota.error), 'stale'));
    return cell;
  }
  function quotaSummary(kind) {
    const known = state.accounts.map(account => account.last_quota?.[kind]).filter(quota => count(quota?.available));
    if (!known.length) return '—';
    const remaining = known.reduce((sum, quota) => sum + quota.available, 0);
    if (!Number.isSafeInteger(remaining)) return '—';
    return `${remaining}${known.length < state.accounts.length ? '（部分）' : ''}`;
  }
  function accountRow(account) {
    const row = node('tr'); row.dataset.tokenId = String(account.token_id);
    const identity = node('td'), verified = verifiedIdentity(account);
    identity.appendChild(node('div', verified ? safeText(account.vids_identity) : '身份尚未验证'));
    if (verified) identity.appendChild(node('small', '已验证', 'state ready capabilities'));
    identity.appendChild(node('small', `关联 Flow：${safeText(account.email) || '邮箱未知'}`, 'muted capabilities'));
    row.appendChild(identity);
    const session = node('td'), ready = readyAccount(account);
    session.appendChild(node('span', sessionMessage(account), `state ${ready ? 'ready' : 'error'}`)); row.appendChild(session);
    const enabled = node('td'); enabled.appendChild(node('span', account.enabled === true ? '已启用' : '已停用', `state ${account.enabled === true ? 'ready' : 'disabled'}`));
    const capabilities = node('small', `图片${account.image_enabled === true ? '启用' : '停用'} · 视频${account.video_enabled === true ? '启用' : '停用'}`, 'muted capabilities'); enabled.appendChild(capabilities); row.appendChild(enabled);
    row.appendChild(quotaCell(account, 'image')); row.appendChild(quotaCell(account, 'video'));
    const queried = node('td', undefined, 'muted');
    queried.appendChild(node('div', `图片：${time(account.last_quota?.image?.queried_at)}`));
    queried.appendChild(node('div', `视频：${time(account.last_quota?.video?.queried_at)}`)); row.appendChild(queried);
    const action = node('td', undefined, 'account-actions'), refreshing = state.refreshing.has(account.token_id);
    const button = node('button', refreshing ? '查询中…' : '重新查询额度', 'secondary refresh'); button.type = 'button';
    button.dataset.vidsAction = 'refresh'; button.dataset.vidsTokenId = String(account.token_id);
    button.disabled = state.loading || state.onboarding || state.reconnecting !== null || refreshing || !ready || !Number.isSafeInteger(account.token_id) || account.token_id <= 0;
    if (!ready) button.title = '请先恢复 Vids 会话，再查询额度';
    if (!unified) button.addEventListener('click', () => refreshQuota(account.token_id)); action.appendChild(button);
    if (!ready) {
      const repairing = state.reconnecting === account.token_id;
      const recover = node('button', repairing ? '恢复中…' : '恢复会话', 'secondary recover'); recover.type = 'button';
      recover.dataset.vidsAction = 'reconnect'; recover.dataset.vidsTokenId = String(account.token_id);
      recover.disabled = state.loading || state.onboarding || state.reconnecting !== null || state.refreshing.size > 0 || !Number.isSafeInteger(account.token_id) || account.token_id <= 0;
      recover.title = '复用该账号已有登录，验证身份并恢复会话；不提交生成、不查询额度';
      if (!unified) recover.addEventListener('click', () => reconnect(account.token_id)); action.appendChild(recover);
    }
    const repair = state.repairs.get(account.token_id);
    if (repair) action.appendChild(node('small', repair.message, repair.state === 'failed' || repair.state === 'unknown' ? 'stale' : 'muted'));
    row.appendChild(action);
    [...row.children].forEach((cell, index) => { cell.dataset.label = ['Vids 身份 / 关联 Flow 账号', 'Vids 会话', '共享启用', '图片额度', '视频额度', '上次读取', '操作'][index]; });
    return row;
  }
  function accountDetails(account) {
    const details = node('div', undefined, 'vids-account-details'); details.dataset.tokenId = String(account.token_id);
    details.appendChild(node('h4', 'Vids', 'vids-detail-platform'));
    const cells = [...accountRow(account).children];
    [0, 1, 3, 4, 2, 5, 6].forEach(index => {
      const cell = cells[index];
      const actions = cell.dataset.label === '操作';
      const block = node('div', undefined, `${actions ? 'vids-detail-actions' : 'vids-detail-metric'} ${cell.className || ''}`.trim());
      if (!actions) block.appendChild(node('span', cell.dataset.label, 'vids-detail-label'));
      [...cell.children].forEach(child => block.appendChild(child));
      details.appendChild(block);
    });
    return details;
  }
  const accountDetailsHTML = account => accountDetails(account).outerHTML;
  function renderNotice(notice) {
    const globalError = !state.runtimeReady && state.runtimeError;
    const ready = state.accounts.filter(readyAccount).length;
    notice.classList.toggle('error', state.error || !!globalError);
    notice.textContent = state.message || (state.loading ? '正在读取 Vids 接入状态…' : !state.configured ? 'Vids 尚未接入。点击“接入所有未绑定账号”，后台会复用已有登录并验证 Vids 身份。' : globalError ? sessionMessage({}) : !state.runtimeReady ? '账号已配置，Vids 身份尚未验证，会话尚未恢复。' : ready < state.configured ? `${ready} / ${state.configured} 个 Vids 账号已就绪；${ready ? '异常账号可单独恢复，其余账号可继续生成。' : '请分别恢复账号会话。'}` : '仅已验证的 Vids 身份可用于生成。额度显示上次查询结果，点击“重新查询额度”可读取最新余额。');
  }
  function renderUnified() {
    renderNotice($('vidsAccountNotice'));
    renderCandidates($('vidsOnboardAll'), $('vidsOnboardingStatus'), $('vidsCandidates'));
    window.renderUnifiedTokenAccounts?.();
  }
  function render() {
    if (unified) { renderUnified(); return; }
    $('configuredCount').textContent = state.loaded ? String(state.configured) : '—';
    $('availableCount').textContent = state.loaded ? String(state.accounts.filter(readyAccount).length) : '—';
    $('imageRemaining').textContent = quotaSummary('image'); $('videoRemaining').textContent = quotaSummary('video');
    $('reload').disabled = state.loading || state.onboarding || state.reconnecting !== null || state.refreshing.size > 0;
    $('reload').textContent = state.loading ? '读取中…' : '重新读取';
    $('accounts').replaceChildren(); state.accounts.forEach(account => $('accounts').appendChild(accountRow(account)));
    $('empty').hidden = !state.loaded || state.accounts.length > 0; $('tableWrap').hidden = state.accounts.length === 0;
    renderNotice($('notice'));
    renderCandidates();
    resize();
  }
  async function load() {
    if (state.loading || state.onboarding || state.reconnecting !== null || state.refreshing.size) return;
    state.loading = true; state.message = ''; state.error = false; render();
    try {
      const [detail, candidates] = await Promise.allSettled([request(base + '/detail'), request(onboardingBase + '/candidates')]);
      if (candidates.status === 'fulfilled' && Array.isArray(candidates.value.accounts) && candidates.value.accounts.every(account => Number.isSafeInteger(account.token_id) && account.token_id > 0 && ['unbound', 'failed', 'unknown'].includes(account.state)) && new Set(candidates.value.accounts.map(account => account.token_id)).size === candidates.value.accounts.length) {
        state.candidates = candidates.value.accounts; state.candidatesLoaded = true; state.candidatesError = '';
        state.candidates.forEach(account => {
          if (!state.results.has(account.token_id) && account.state !== 'unbound') state.results.set(account.token_id, { email: account.email, state: account.state, message: onboardingMessage(account.code || 'connection_unknown') });
        });
      } else {
        state.candidatesLoaded = false; state.candidatesError = candidates.status === 'rejected' && candidates.reason.code === 'forbidden' ? onboardingMessage('forbidden') : '未绑定账号列表读取失败，请重新读取；不会自动接入。';
      }
      if (detail.status === 'rejected') throw detail.reason;
      const data = detail.value;
      if (!Array.isArray(data.accounts) || !count(data.configured_count)) throw new Error('账号状态返回格式无效。');
      state.accounts = data.accounts; state.configured = data.configured_count; state.runtimeReady = data.runtime_ready === true; state.runtimeError = typeof data.runtime_error === 'string' ? data.runtime_error : null; state.loaded = true;
      state.accounts.forEach(account => {
        const result = state.results.get(account.token_id);
        if (result) state.results.set(account.token_id, readyAccount(account) ? { ...result, state: 'ready', message: '已接入 · 身份已验证' } : { ...result, state: 'pending', message: '已保存绑定 · Vids 会话当前未就绪' });
        const repair = state.repairs.get(account.token_id);
        if (repair && (repair.state === 'pending' || repair.state === 'unknown')) state.repairs.set(account.token_id, readyAccount(account) ? { state: 'ready', message: '会话已恢复，额度可重新查询。' } : { ...repair, message: repair.state === 'unknown' ? repair.message : '恢复请求已完成，当前会话仍未就绪。' });
      });
    } catch (error) { state.message = safeText(error.message); state.error = true; }
    finally { state.loading = false; render(); }
  }
  async function onboardAll() {
    if (state.loading || state.onboarding || state.reconnecting !== null || state.refreshing.size || !state.candidatesLoaded || state.candidatesError) return;
    const accounts = state.candidates.filter(account => { const status = state.results.get(account.token_id)?.state; return !status || status === 'failed'; });
    if (!accounts.length) return;
    state.onboarding = true; render();
    try {
      for (const account of accounts) {
        state.results.set(account.token_id, { email: account.email, state: 'working', message: '正在验证身份并保存会话…' }); render();
        try {
          const result = await request(`${onboardingBase}/${account.token_id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ create_blank: true }) });
          if (result.token_id !== account.token_id || result.state !== 'ready' || result.identity_verified !== true) throw new Error('接入状态返回格式无效。');
          state.results.set(account.token_id, { email: account.email, state: 'pending', message: '接入完成 · 正在读取会话状态' });
        } catch (error) {
          const code = error.code || 'connection_unknown';
          state.results.set(account.token_id, { email: account.email, state: code === 'connection_unknown' ? 'unknown' : 'failed', message: onboardingMessage(code) });
          if (code === 'authentication_required' || code === 'forbidden') break;
        }
        render();
      }
    } finally { state.onboarding = false; await load(); }
  }
  async function reconnect(id) {
    if (state.loading || state.onboarding || state.reconnecting !== null || state.refreshing.size) return;
    const account = state.accounts.find(account => account.token_id === id);
    if (!account || readyAccount(account) || !Number.isSafeInteger(id) || id <= 0) return;
    state.reconnecting = id; state.repairs.set(id, { state: 'working', message: '正在复用登录并验证 Vids 身份…' }); render();
    try {
      const result = await request(`${onboardingBase}/${id}/reconnect`, { method: 'POST' });
      if (result.token_id !== id || result.state !== 'ready' || result.identity_verified !== true) throw new Error('恢复状态返回格式无效。');
      state.repairs.set(id, { state: 'pending', message: '恢复请求已完成，正在读取会话状态…' });
    } catch (error) {
      const code = error.code || 'connection_unknown';
      state.repairs.set(id, { state: code === 'connection_unknown' ? 'unknown' : 'failed', message: repairMessage(code) });
    } finally { state.reconnecting = null; await load(); }
  }
  async function refreshQuota(id) {
    if (state.loading || state.onboarding || state.reconnecting !== null || state.refreshing.has(id)) return;
    const index = state.accounts.findIndex(account => account.token_id === id), previous = state.accounts[index];
    if (!previous || !readyAccount(previous)) return;
    state.refreshing.add(id); state.message = ''; state.error = false; render();
    try {
      const data = await request(`${base}/${id}/quota-refresh`, { method: 'POST' });
      if (data.account?.token_id !== id) throw new Error('额度返回账号不匹配。');
      state.accounts[index] = data.account;
      const failed = ['image', 'video'].some(kind => data.account.last_quota?.[kind]?.error);
      state.message = failed ? '部分额度查询失败，已保留上次缓存。' : '额度已更新。'; state.error = failed;
    } catch (error) {
      const message = safeText(error.message);
      state.accounts[index] = { ...previous, last_quota: Object.fromEntries(['image', 'video'].map(kind => [kind, { ...previous.last_quota?.[kind], stale: true, error: message }])) };
      state.message = `查询失败，已保留上次缓存。${message}`; state.error = true;
    } finally { state.refreshing.delete(id); render(); }
  }
  function init() {
    if (unified) {
      $('vidsOnboardAll').addEventListener('click', onboardAll);
      document.addEventListener('click', event => {
        const button = event.target?.closest('[data-vids-action]');
        if (!button || button.disabled || !button.closest('.vids-account-details')) return;
        const id = Number(button.dataset.vidsTokenId);
        if (button.dataset.vidsAction === 'refresh') return refreshQuota(id);
        if (button.dataset.vidsAction === 'reconnect') return reconnect(id);
      });
    } else {
      if (new URLSearchParams(window.location.search).get('embedded') === '1') document.body.classList.add('embedded');
      $('reload').addEventListener('click', load);
      $('onboardAll').addEventListener('click', onboardAll);
      window.addEventListener('message', event => {
        if (event.origin === window.location.origin && event.source === window.parent && event.data?.type === 'lay4flow.vids.accounts.refresh') load();
      });
      if (typeof ResizeObserver !== 'undefined') new ResizeObserver(resize).observe(document.body);
    }
    render(); load();
  }
  window.vidsAccounts = { state, load, onboardAll, readyAccount, sessionMessage, accountDetails, accountDetailsHTML };
  document.addEventListener('DOMContentLoaded', init);
})();
