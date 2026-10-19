const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/vids-accounts.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'static/js/vids-accounts.js'), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(item => item[1]);
assert.equal(ids.length, new Set(ids).size);
assert(html.includes('共享启用') && html.includes('不是独立的 Vids 开关'));
assert(html.includes('接入所有未绑定账号') && html.includes('必要时建立空白 Vids 文档') && html.includes('不生成、不消费'));
for (const forbidden of ['innerHTML', 'localStorage', 'sessionStorage', 'document.cookie', 'Authorization', '/profile-browser']) assert(!script.includes(forbidden), forbidden);
const json = (data, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const checked = '2026-10-11T14:00:00Z';
const quota = available => ({ total: 100, available, consumed: 100 - available, queried_at: checked, error: null, stale: false });
const account = (id = 4, patch = {}) => ({ token_id: id, email: `flow-${id}@example.test`, vids_identity: `vids-${id}@example.test`, identity_verified: true, enabled: true, image_enabled: true, video_enabled: true, ready: true, session_state: 'ready', last_quota: { image: quota(12), video: quota(80) }, ...patch });
const detail = accounts => ({ configured_count: accounts.length, runtime_ready: true, accounts });
const escapeHTML = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
function element(tag = 'div') {
  const classes = new Set();
  return { tagName: tag, textContent: '', children: [], events: {}, dataset: {}, hidden: false, disabled: false,
    classList: { add: value => classes.add(value), contains: value => classes.has(value), toggle(value, active) { if (active) classes.add(value); else classes.delete(value); } },
    appendChild(child) { this.children.push(child); child.parentElement = this; }, replaceChildren() { this.children = []; }, addEventListener(name, fn) { this.events[name] = fn; },
    closest(selector) {
      if (selector === '[data-vids-action]' && this.dataset.vidsAction || selector === '.vids-account-details' && this.className?.split(' ').includes('vids-account-details')) return this;
      return this.parentElement?.closest(selector);
    },
    get outerHTML() {
      const attributes = Object.entries(this.dataset).map(([key, value]) => ` data-${key.replace(/[A-Z]/g, character => '-' + character.toLowerCase())}="${escapeHTML(value)}"`).join('');
      return `<${this.tagName}${this.className ? ` class="${escapeHTML(this.className)}"` : ''}${this.disabled ? ' disabled' : ''}${attributes}>${escapeHTML(this.textContent)}${this.children.map(child => child.outerHTML).join('')}</${this.tagName}>`;
    },
    async click() { if (!this.disabled && !this.hidden) return this.events.click?.(); } };
}
const text = node => node.textContent + node.children.map(text).join(' ');
function page(initial = detail([account()]), embedded = false, candidates = [], unified = false) {
  const pageIds = unified ? ['vidsAccountNotice', 'vidsOnboardAll', 'vidsOnboardingStatus', 'vidsCandidates'] : ids;
  const nodes = Object.fromEntries(pageIds.map(id => [id, element()])), events = {}, requests = [], posts = [];
  let unifiedRenders = 0;
  let responder = async url => json(url.endsWith('/candidates') ? { accounts: candidates } : initial);
  const location = { origin: 'http://localhost:38000', search: embedded ? '?embedded=1' : '', href: '/vids-accounts' };
  const document = { body: { ...element('body'), scrollHeight: 550 }, getElementById: id => nodes[id], createElement: element, addEventListener(name, fn) { events[name] = fn; } };
  if (unified) document.body.dataset.accountManagement = 'unified';
  const window = { location, addEventListener(){}, renderUnifiedTokenAccounts() { unifiedRenders++; }, parent: { postMessage(message, target) { posts.push({ message, target }); } } };
  const context = vm.createContext({ document, window, URLSearchParams, Date, Math, Set, requestAnimationFrame: fn => fn(),
    fetch: async (url, options = {}) => { requests.push({ url, options }); return responder(url, options); },
    setInterval() { throw new Error('No automatic polling'); } });
  if (unified) context.ResizeObserver = class { constructor() { throw new Error('Unified management must not observe the standalone iframe'); } };
  for (const target of [context, window]) for (const name of ['localStorage', 'sessionStorage']) Object.defineProperty(target, name, { get() { throw new Error('No credential storage'); } });
  Object.defineProperty(document, 'cookie', { get() { throw new Error('No Cookie reads'); } });
  vm.runInContext(script, context, { filename: 'vids-accounts.js' }); events.DOMContentLoaded();
  return { nodes, requests, posts, location, document, api: window.vidsAccounts, unifiedRenders: () => unifiedRenders, delegatedClick: target => events.click({ target }), respond(fn) { responder = fn; },
    button(id = 4) { return nodes.accounts.children.find(row => row.dataset.tokenId === String(id)).children.at(-1).children[0]; },
    recover(id = 4) { return nodes.accounts.children.find(row => row.dataset.tokenId === String(id)).children.at(-1).children.find(child => child.tagName === 'button' && child.className.includes('recover')); },
    async ready() { for (let wait = 0; wait < 30 && window.vidsAccounts.state.loading; wait++) await new Promise(resolve => setTimeout(resolve, 1)); } };
}
async function main() {
  const empty = page(detail([]), true); await empty.ready();
  assert.equal(empty.requests.length, 2); assert.equal(empty.requests[0].url, '/api/admin/vids/accounts/detail');
  assert.equal(empty.requests[0].options.credentials, 'same-origin'); assert.equal(empty.requests[0].options.redirect, 'error');
  assert.equal(empty.requests[0].options.method, undefined, 'Startup never refreshes upstream quota');
  assert.equal(empty.requests[1].url, '/api/admin/vids/onboard/candidates'); assert.equal(empty.requests[1].options.method, undefined, 'Startup only discovers candidates, never writes or opens a Profile');
  assert.equal(empty.nodes.configuredCount.textContent, '0'); assert.equal(empty.nodes.imageRemaining.textContent, '—'); assert.equal(empty.nodes.empty.hidden, false);
  assert(empty.document.body.classList.contains('embedded')); assert(empty.posts.some(post => post.message.type === 'lay4flow.vids.accounts.resize' && post.target === empty.location.origin && post.message.height < 650));

  const zero = page(detail([account(4, { last_quota: { image: quota(0), video: null } }), account(8, { enabled: false, ready: false, last_quota: {} })])); await zero.ready();
  assert.equal(zero.nodes.configuredCount.textContent, '2'); assert.equal(zero.nodes.availableCount.textContent, '1');
  assert.equal(zero.nodes.imageRemaining.textContent, '0（部分）'); assert.equal(zero.nodes.videoRemaining.textContent, '—');
  assert(text(zero.nodes.accounts.children[0]).includes('总额 100 · 已用 100')); assert(text(zero.nodes.accounts.children[0]).includes('尚未查询'));
  assert.equal(zero.button(8).disabled, true); await zero.button(8).click(); assert.equal(zero.requests.length, 2);
  const cold = page({ ...detail([account()]), runtime_ready: false, runtime_error: 'session_unavailable' }); await cold.ready();
  assert.equal(cold.nodes.availableCount.textContent, '0'); assert.equal(cold.button().disabled, true); assert(text(cold.nodes.accounts).includes('Vids 会话恢复失败，需要检查接入状态'));
  for (const unified of [false, true]) {
    const profileFailure = page({ ...detail([account(4, { ready: false, identity_verified: false, vids_identity: null })]), runtime_ready: false, runtime_error: 'profile_refresh_failed' }, false, [], unified); await profileFailure.ready();
    const row = unified ? profileFailure.api.accountDetails(profileFailure.api.state.accounts[0]) : profileFailure.nodes.accounts.children[0];
    const actions = row.children.at(-1);
    assert(text(row).includes('Vids 登录状态读取失败，请重试恢复会话'));
    assert.equal((unified ? profileFailure.nodes.vidsAccountNotice : profileFailure.nodes.notice).textContent, 'Vids 登录状态读取失败，请重试恢复会话');
    assert.equal(profileFailure.api.state.runtimeReady, false); assert(!text(row).includes('已验证'));
    assert.equal(actions.children.find(button => button.dataset.vidsAction === 'refresh').disabled, true);
    assert.equal(actions.children.find(button => button.dataset.vidsAction === 'reconnect').disabled, false);
    assert.equal(profileFailure.requests.filter(request => request.options.method === 'POST').length, 0);
  }

  const unverified = page(detail([account(4, { vids_identity: null, identity_verified: false, ready: false, session_state: 'unavailable' })])); await unverified.ready();
  assert.equal(unverified.nodes.availableCount.textContent, '0'); assert(text(unverified.nodes.accounts).includes('身份尚未验证')); assert(text(unverified.nodes.accounts).includes('关联 Flow：flow-4@example.test')); assert.equal(unverified.button().disabled, true);
  const expiredSession = page({ ...detail([account(4, { vids_identity: null, identity_verified: false, ready: false, session_state: 'authentication_expired' })]), runtime_ready: false, runtime_error: 'session_authentication_failed' }); await expiredSession.ready();
  assert(text(expiredSession.nodes.accounts).includes('Vids 登录已失效，请恢复会话')); assert(expiredSession.nodes.notice.textContent.includes('Vids 登录已失效'));
  const mismatched = page(detail([account(4, { vids_identity: null, identity_verified: false, ready: false, session_state: 'identity_mismatch', runtime_error: 'account_mismatch' })])); await mismatched.ready();
  assert(text(mismatched.nodes.accounts).includes('Vids 与关联账号不一致，已阻止使用'));
  const identityError = page({ ...detail([account(4, { vids_identity: null, identity_verified: false, ready: false })]), runtime_ready: false, runtime_error: 'session_identity_mismatch' }); await identityError.ready();
  assert(identityError.nodes.notice.textContent.includes('Vids 与关联账号不一致，已阻止使用'));
  const untrusted = page(detail([account(4, { ready: false, identity_verified: true })])); await untrusted.ready();
  assert(text(untrusted.nodes.accounts.children[0].children[0]).includes('身份尚未验证')); assert.equal(untrusted.nodes.availableCount.textContent, '0');
  assert(html.includes('已配置') && !html.includes('<span class="metric-label">已接入</span>'));
  const verified = page(); await verified.ready();
  assert(text(verified.nodes.accounts.children[0].children[0]).includes('vids-4@example.test')); assert(text(verified.nodes.accounts.children[0].children[0]).includes('已验证'));
  assert(text(verified.nodes.accounts.children[0].children[0]).includes('关联 Flow：flow-4@example.test'));
  const disabledConnected = page(detail([account(4, { enabled: false })])); await disabledConnected.ready();
  assert.equal(disabledConnected.nodes.availableCount.textContent, '1', 'A verified connected session and the shared enable switch are separate states');
  const configuredOnly = page({ ...detail([account(4, { ready: false, vids_identity: null, identity_verified: false })]), connected_count: 1 }); await configuredOnly.ready();
  assert.equal(configuredOnly.nodes.availableCount.textContent, '0', 'Configured or inconsistent aggregate counts cannot claim a verified connection');

  for (const unified of [false, true]) {
    for (const [code, expected] of [['session_authentication_failed', 'Vids 登录已失效，请恢复会话'], ['profile_refresh_failed', 'Vids 登录状态读取失败，请重试恢复会话'], ['account_mismatch', 'Vids 与关联账号不一致，已阻止使用'], ['invalid_configuration', 'Vids 接入配置无效，需要检查配置'], ['account_not_found', '关联 Flow 账号已不存在，请检查原账号配置']]) {
      const mixed = page({ ...detail([account(4, { runtime_error: null }), account(6, { runtime_error: null }), account(7, { ready: false, identity_verified: false, vids_identity: null, session_state: 'unavailable', runtime_error: code })]), runtime_error: null }, false, [], unified); await mixed.ready();
      const [healthy, otherHealthy, broken] = mixed.api.state.accounts;
      const rows = unified ? mixed.api.state.accounts.map(mixed.api.accountDetails) : mixed.nodes.accounts.children;
      const notice = unified ? mixed.nodes.vidsAccountNotice : mixed.nodes.notice;
      assert.equal(mixed.api.readyAccount(healthy), true); assert.equal(mixed.api.sessionMessage(healthy), '已就绪');
      assert.equal(mixed.api.readyAccount(broken), false); assert.equal(mixed.api.sessionMessage(broken), expected);
      assert.equal(mixed.api.readyAccount(otherHealthy), true); assert.equal(mixed.api.sessionMessage(otherHealthy), '已就绪');
      assert(!text(rows[0]).includes(expected)); assert(!text(rows[1]).includes(expected)); assert(text(rows[2]).includes(expected));
      assert.equal(rows[0].children.at(-1).children.find(button => button.dataset.vidsAction === 'refresh').disabled, false);
      assert.equal(rows[1].children.at(-1).children.find(button => button.dataset.vidsAction === 'refresh').disabled, false);
      assert.equal(rows[2].children.at(-1).children.find(button => button.dataset.vidsAction === 'refresh').disabled, true);
      assert.equal(rows[2].children.at(-1).children.find(button => button.dataset.vidsAction === 'reconnect').disabled, false);
      assert.equal(notice.textContent, '2 / 3 个 Vids 账号已就绪；异常账号可单独恢复，其余账号可继续生成。'); assert(!notice.classList.contains('error'));
      mixed.api.state.runtimeError = 'session_unavailable';
      assert.equal(mixed.api.sessionMessage(healthy), '已就绪', 'A healthy session never inherits another account\'s pool error');
      assert.equal(mixed.requests.filter(request => request.options.method === 'POST').length, 0);
    }
    const allFailed = page(detail([account(4, { ready: false, identity_verified: false, vids_identity: null, runtime_error: 'session_authentication_failed' }), account(7, { ready: false, identity_verified: false, vids_identity: null, runtime_error: 'profile_refresh_failed' })]), false, [], unified); await allFailed.ready();
    const notice = unified ? allFailed.nodes.vidsAccountNotice : allFailed.nodes.notice;
    assert.equal(notice.textContent, '0 / 2 个 Vids 账号已就绪；请分别恢复账号会话。'); assert(!notice.classList.contains('error'));
  }

  const refreshed = page(); await refreshed.ready(); let resolveRefresh;
  refreshed.respond(() => new Promise(resolve => { resolveRefresh = () => resolve(json({ account: account(4, { last_quota: { image: quota(11), video: quota(79) } }) })); }));
  const button = refreshed.button(), click = button.click();
  await button.click(); assert.equal(refreshed.requests.length, 3, 'Repeated clicks query once');
  assert.equal(refreshed.requests[2].url, '/api/admin/vids/accounts/4/quota-refresh'); assert.equal(refreshed.requests[2].options.method, 'POST');
  assert.equal(refreshed.requests[2].options.credentials, 'same-origin'); assert.equal(refreshed.requests[2].options.body, undefined);
  resolveRefresh(); await click; assert.equal(refreshed.nodes.imageRemaining.textContent, '11'); assert.equal(refreshed.nodes.videoRemaining.textContent, '79');
  assert.equal(refreshed.requests.length, 3, 'Success updates the returned account without extra upstream or polling requests');

  for (const failure of [() => { throw new Error('Synthetic network failure'); }, () => json({ detail: 'Synthetic failure' }, 502)]) {
    const cached = page(); await cached.ready(); cached.respond(failure); await cached.button().click();
    assert.equal(cached.nodes.imageRemaining.textContent, '12'); assert.equal(cached.nodes.videoRemaining.textContent, '80');
    assert(text(cached.nodes.accounts).includes('上次缓存 · 已过期')); assert(cached.nodes.notice.textContent.includes('已保留上次缓存'));
    cached.respond(failure); await cached.nodes.reload.click(); assert.equal(cached.nodes.imageRemaining.textContent, '12', 'Discovery failures also retain last known values');
  }
  const partial = page(); await partial.ready(); partial.respond(async () => json({ account: account(4, { last_quota: { image: { ...quota(12), stale: true, error: 'schema_changed' }, video: quota(78) } }) })); await partial.button().click();
  assert.equal(partial.nodes.imageRemaining.textContent, '12'); assert.equal(partial.nodes.videoRemaining.textContent, '78'); assert(partial.nodes.notice.textContent.includes('部分额度查询失败'));
  const foreign = page(); await foreign.ready(); foreign.respond(async () => json({ account: account(8) })); await foreign.button().click();
  assert.equal(foreign.nodes.imageRemaining.textContent, '12', 'Mismatched account responses cannot replace cached data');
  const malicious = page(detail([account(4, { email: '<img src=x onerror=alert(1)>', last_quota: { image: { ...quota(12), error: '<script>steal()</script>' } }, cookie: 'PRIVATE', runtime_dir: 'PRIVATE', key: 'PRIVATE' })])); await malicious.ready();
  assert(text(malicious.nodes.accounts).includes('<img src=x onerror=alert(1)>')); assert(!text(malicious.nodes.accounts).includes('PRIVATE'));
  const expired = page(); await expired.ready(); expired.respond(async () => json({}, 401)); await expired.button().click(); assert.equal(expired.location.href, '/login');

  const candidate = id => ({ token_id: id, email: `flow-${id}@example.test`, state: 'unbound' });
  const unbound = [candidate(6), candidate(8)];
  const batch = page(detail([account()]), false, unbound); await batch.ready();
  assert.equal(batch.nodes.onboardAll.disabled, false); assert(text(batch.nodes.candidates).includes('flow-6@example.test'));
  assert.equal(batch.requests.filter(request => request.options.method === 'POST').length, 0, 'Opening the management page never creates a document or binds accounts');
  let finishFirst, bound = false, active = 0, maxActive = 0;
  batch.respond(async (url, options) => {
    if (options.method !== 'POST') return json(url.endsWith('/candidates') ? { accounts: [] } : detail([account(), account(6), account(8)]));
    active++; maxActive = Math.max(maxActive, active);
    if (url.endsWith('/6')) return new Promise(resolve => { finishFirst = () => { active--; bound = true; resolve(json({ token_id: 6, state: 'ready', identity_verified: true })); }; });
    assert(bound, 'The next account waits for the previous onboarding to finish'); active--;
    return json({ token_id: 8, state: 'ready', identity_verified: true });
  });
  const onboarding = batch.nodes.onboardAll.click();
  await batch.nodes.onboardAll.click();
  assert.equal(batch.requests.filter(request => request.options.method === 'POST').length, 1, 'Repeated clicks do not start overlapping batches');
  assert.equal(batch.button().disabled, true, 'Quota refresh cannot compete with the onboarding lifecycle');
  assert.equal(batch.nodes.reload.disabled, true); assert(text(batch.nodes.candidates).includes('正在验证身份'));
  finishFirst(); await onboarding;
  const batchPosts = batch.requests.filter(request => request.options.method === 'POST');
  assert.deepEqual(batchPosts.map(request => request.url), ['/api/admin/vids/onboard/6', '/api/admin/vids/onboard/8']);
  assert.equal(maxActive, 1, 'Accounts are onboarded sequentially');
  for (const request of batchPosts) {
    assert.equal(request.options.credentials, 'same-origin'); assert.equal(request.options.redirect, 'error');
    assert.equal(request.options.headers['Content-Type'], 'application/json'); assert.deepEqual(JSON.parse(request.options.body), { create_blank: true });
    assert(!Object.keys(request.options.headers).some(name => name.toLowerCase() === 'authorization'));
  }
  assert.equal(batch.nodes.configuredCount.textContent, '3'); assert.equal(batch.nodes.availableCount.textContent, '3');
  assert.equal(batch.nodes.imageRemaining.textContent, '36'); assert.equal(batch.nodes.videoRemaining.textContent, '240');
  assert(text(batch.nodes.candidates).includes('已接入 · 身份已验证')); assert.equal(batch.nodes.onboardAll.disabled, true);
  assert.deepEqual(batch.requests.slice(-2).map(request => request.url), ['/api/admin/vids/accounts/detail', '/api/admin/vids/onboard/candidates'], 'After onboarding only safe state discovery is repeated');

  for (const [code, expected] of [['login_required', '需要重新登录 Google 账号'], ['captchaRequired', '需要完成 Google 验证码'], ['identity_mismatch', 'Vids 响应返回的账号身份与 Flow 账号不同'], ['identity_unavailable', 'Vids 响应暂未提供账号身份'], ['account_mismatch', '已保存的 Vids 绑定与当前身份不一致']]) {
    const failed = page(detail([account()]), false, unbound); await failed.ready(); let writes = 0;
    failed.respond(async (url, options) => {
      if (options.method === 'POST') { writes++; return url.endsWith('/6') ? json({ error: { code, message: 'PRIVATE https://upstream.example/secret' } }, 409) : json({ token_id: 8, state: 'ready', identity_verified: true }); }
      return json(url.endsWith('/candidates') ? { accounts: [candidate(6)] } : detail([account(), account(8)]));
    });
    await failed.nodes.onboardAll.click();
    assert.equal(writes, 2, 'A known account failure is preserved and does not prevent the next account from connecting');
    assert(text(failed.nodes.candidates).includes(expected)); assert(text(failed.nodes.candidates).includes('flow-6@example.test'));
    assert(!text(failed.nodes.candidates).includes('PRIVATE')); assert.equal(failed.nodes.onboardAll.disabled, false);
    assert.equal(failed.nodes.imageRemaining.textContent, '24', 'Existing ready account balances survive a failed new onboarding');
  }
  const denied = page(detail([account()]), false, unbound); await denied.ready();
  denied.respond(async (url, options) => options.method === 'POST' ? json({ detail: 'PRIVATE' }, 403) : json(url.endsWith('/candidates') ? { accounts: unbound } : detail([account()])));
  await denied.nodes.onboardAll.click(); assert.equal(denied.requests.filter(request => request.options.method === 'POST').length, 1);
  assert(text(denied.nodes.candidates).includes('没有管理员操作权限')); assert(!text(denied.nodes.candidates).includes('PRIVATE'));
  const authExpired = page(detail([account()]), false, unbound); await authExpired.ready();
  authExpired.respond(async (url, options) => options.method === 'POST' ? json({}, 401) : json(url.endsWith('/candidates') ? { accounts: unbound } : detail([account()])));
  await authExpired.nodes.onboardAll.click(); assert.equal(authExpired.location.href, '/login');
  assert.equal(authExpired.requests.filter(request => request.options.method === 'POST').length, 1, 'An expired admin login stops the batch');

  const unknown = page(detail([account()]), false, [candidate(6)]); await unknown.ready();
  unknown.respond(async (url, options) => { if (options.method === 'POST') throw new Error('PRIVATE'); return json(url.endsWith('/candidates') ? { accounts: [candidate(6)] } : detail([account()])); });
  await unknown.nodes.onboardAll.click(); assert(text(unknown.nodes.candidates).includes('接入结果尚未确认'));
  assert.equal(unknown.nodes.onboardAll.disabled, true); await unknown.nodes.onboardAll.click(); await unknown.nodes.reload.click();
  assert.equal(unknown.requests.filter(request => request.options.method === 'POST').length, 1, 'A lost response never automatically creates another document');
  unknown.respond(async url => json(url.endsWith('/candidates') ? { accounts: [] } : detail([account(), account(6)])));
  await unknown.nodes.reload.click(); assert(text(unknown.nodes.candidates).includes('已接入 · 身份已验证'));
  assert.equal(unknown.nodes.configuredCount.textContent, '2', 'Safe discovery can confirm a binding after a lost response');

  const notReady = page(detail([account()]), false, [candidate(6)]); await notReady.ready();
  notReady.respond(async (url, options) => options.method === 'POST' ? json({ token_id: 6, state: 'ready', identity_verified: true }) : json(url.endsWith('/candidates') ? { accounts: [] } : detail([account(), account(6, { ready: false, identity_verified: false, vids_identity: null, last_quota: { image: { ...quota(12), stale: true }, video: { ...quota(80), stale: true } } })])));
  await notReady.nodes.onboardAll.click(); assert.equal(notReady.nodes.availableCount.textContent, '1');
  assert(text(notReady.nodes.candidates).includes('已保存绑定 · Vids 会话当前未就绪')); assert.equal(notReady.button(6).disabled, true);
  assert(text(notReady.nodes.accounts).includes('上次缓存 · 已过期'), 'Cached quota does not prove the session is ready');
  const mismatchedOnboard = page(detail([]), false, [candidate(6)]); await mismatchedOnboard.ready();
  mismatchedOnboard.respond(async (url, options) => options.method === 'POST' ? json({ token_id: 8, state: 'ready', identity_verified: true }) : json(url.endsWith('/candidates') ? { accounts: [candidate(6)] } : detail([])));
  await mismatchedOnboard.nodes.onboardAll.click(); assert.equal(mismatchedOnboard.nodes.onboardAll.disabled, true);
  assert(text(mismatchedOnboard.nodes.candidates).includes('接入结果尚未确认'), 'A response for another account cannot claim successful onboarding');

  const badCandidates = page(detail([account()]), false, [candidate(6), candidate(6)]); await badCandidates.ready();
  assert.equal(badCandidates.nodes.onboardAll.disabled, true); assert(badCandidates.nodes.onboardingStatus.textContent.includes('读取失败'));
  const candidateDiscoveryDenied = page(); await candidateDiscoveryDenied.ready();
  candidateDiscoveryDenied.respond(async url => url.endsWith('/candidates') ? json({ detail: 'PRIVATE' }, 403) : json(detail([account()])));
  await candidateDiscoveryDenied.nodes.reload.click(); assert.equal(candidateDiscoveryDenied.nodes.onboardAll.disabled, true);
  assert(candidateDiscoveryDenied.nodes.onboardingStatus.textContent.includes('没有管理员操作权限')); assert.equal(candidateDiscoveryDenied.nodes.imageRemaining.textContent, '12');

  const broken = () => account(4, { ready: false, identity_verified: false, vids_identity: null, session_state: 'authentication_expired', last_quota: { image: { ...quota(12), stale: true, error: 'quota_unavailable' }, video: { ...quota(80), stale: true, error: 'quota_unavailable' } } });
  const restored = page(detail([broken()])); await restored.ready();
  assert.equal(restored.recover().disabled, false); assert.equal(restored.button().disabled, true);
  assert.equal(restored.requests.filter(request => request.options.method === 'POST').length, 0, 'A failed session never recovers on page load');
  let finishRepair, repaired = false;
  restored.respond(async (url, options) => {
    if (url.endsWith('/reconnect')) return new Promise(resolve => { finishRepair = () => { repaired = true; resolve(json({ token_id: 4, state: 'ready', identity_verified: true })); }; });
    return json(url.endsWith('/candidates') ? { accounts: [] } : detail([repaired ? account(4, { last_quota: broken().last_quota }) : broken()]));
  });
  const repairButton = restored.recover(), repair = repairButton.click(); await repairButton.click();
  assert.equal(restored.requests.filter(request => request.url.endsWith('/reconnect')).length, 1, 'Repeated recovery clicks issue one request');
  assert.equal(restored.nodes.reload.disabled, true); assert.equal(restored.nodes.onboardAll.disabled, true); assert.equal(restored.recover().disabled, true);
  finishRepair(); await repair;
  const repairPost = restored.requests.find(request => request.url.endsWith('/reconnect'));
  assert.equal(repairPost.url, '/api/admin/vids/onboard/4/reconnect'); assert.equal(repairPost.options.method, 'POST');
  assert.equal(repairPost.options.body, undefined); assert.equal(repairPost.options.headers, undefined); assert.equal(repairPost.options.credentials, 'same-origin'); assert.equal(repairPost.options.redirect, 'error');
  assert.equal(restored.nodes.availableCount.textContent, '1'); assert.equal(restored.recover(), undefined); assert.equal(restored.button().disabled, false);
  assert(text(restored.nodes.accounts).includes('会话已恢复，额度可重新查询。'));
  assert(text(restored.nodes.accounts).includes('上次缓存 · 已过期')); assert.equal(restored.nodes.imageRemaining.textContent, '12');
  assert(!restored.requests.some(request => request.url.endsWith('/quota-refresh')), 'Recovery only reloads detail and candidates; quota is a separate explicit button');

  for (const [code, expected, status] of [['busy', '账号正在使用', 429], ['identity_mismatch', 'Vids 身份与 Flow 账号不一致，恢复已阻止', 409], ['account_mismatch', '已保存会话与当前身份不一致，恢复已阻止', 409], ['login_required', '需要重新登录 Google 账号后再恢复', 409], ['profile_refresh_failed', '复用账号登录时读取状态失败，请重新恢复会话', 409], ['session_authentication_failed', '该账号的 Vids 登录已失效，请重新登录后恢复会话', 503], ['forbidden', '没有管理员操作权限', 403]]) {
    const failedRepair = page(detail([broken()])); await failedRepair.ready();
    failedRepair.respond(async (url, options) => options.method === 'POST' ? json({ error: { code, message: 'PRIVATE https://upstream.example/secret' } }, status) : json(url.endsWith('/candidates') ? { accounts: [] } : detail([broken()])));
    await failedRepair.recover().click(); assert(text(failedRepair.nodes.accounts).includes(expected));
    assert(!text(failedRepair.nodes.accounts).includes('PRIVATE')); assert.equal(failedRepair.nodes.availableCount.textContent, '0');
    assert.equal(failedRepair.nodes.imageRemaining.textContent, '12'); assert.equal(failedRepair.nodes.videoRemaining.textContent, '80');
    assert.equal(failedRepair.recover().disabled, false); assert.equal(failedRepair.button().disabled, true);
    await failedRepair.nodes.reload.click(); assert(text(failedRepair.nodes.accounts).includes(expected), 'Busy and identity errors remain visible after safe state reload');
    assert.equal(failedRepair.requests.filter(request => request.options.method === 'POST').length, 1, 'Safe reload never retries a failed repair');
  }
  const lostRepair = page(detail([broken()])); await lostRepair.ready();
  lostRepair.respond(async (url, options) => { if (options.method === 'POST') throw new Error('PRIVATE'); return json(url.endsWith('/candidates') ? { accounts: [] } : detail([broken()])); });
  await lostRepair.recover().click(); assert(text(lostRepair.nodes.accounts).includes('恢复结果尚未确认'));
  await lostRepair.nodes.reload.click(); assert.equal(lostRepair.requests.filter(request => request.options.method === 'POST').length, 1);
  const repairForeign = page(detail([broken()])); await repairForeign.ready();
  repairForeign.respond(async (url, options) => options.method === 'POST' ? json({ token_id: 8, state: 'ready', identity_verified: true }) : json(url.endsWith('/candidates') ? { accounts: [] } : detail([broken()])));
  await repairForeign.recover().click(); assert.equal(repairForeign.nodes.availableCount.textContent, '0'); assert(text(repairForeign.nodes.accounts).includes('恢复结果尚未确认'));
  const repairLoginExpired = page(detail([broken()])); await repairLoginExpired.ready();
  repairLoginExpired.respond(async (url, options) => options.method === 'POST' ? json({}, 401) : json(url.endsWith('/candidates') ? { accounts: [] } : detail([broken()])));
  await repairLoginExpired.recover().click(); assert.equal(repairLoginExpired.location.href, '/login'); assert(text(repairLoginExpired.nodes.accounts).includes('管理员登录已失效'));
  const stillBroken = page(detail([broken()])); await stillBroken.ready();
  stillBroken.respond(async (url, options) => options.method === 'POST' ? json({ token_id: 4, state: 'ready', identity_verified: true }) : json(url.endsWith('/candidates') ? { accounts: [] } : detail([broken()])));
  await stillBroken.recover().click(); assert.equal(stillBroken.nodes.availableCount.textContent, '0'); assert(text(stillBroken.nodes.accounts).includes('当前会话仍未就绪'));

  const unified = page(detail([account(4, { email: '<img src=x onerror=alert(1)>', cookie: 'PRIVATE', key: 'PRIVATE', runtime_dir: 'PRIVATE' })]), true, [candidate(6)], true); await unified.ready();
  assert.equal(unified.requests.length, 2); assert.equal(unified.posts.length, 0, 'Unified rendering never resizes or refreshes an iframe');
  assert(!unified.document.body.classList.contains('embedded')); assert(unified.unifiedRenders() > 0);
  assert.equal(unified.api.state.configured, 1); assert.equal(unified.api.state.accounts.length, 1);
  assert.equal(unified.api.readyAccount(unified.api.state.accounts[0]), true); assert.equal(unified.api.sessionMessage(unified.api.state.accounts[0]), '已就绪');
  assert.equal(unified.nodes.vidsOnboardAll.disabled, false); assert(text(unified.nodes.vidsCandidates).includes('flow-6@example.test'));
  const details = unified.api.accountDetails(unified.api.state.accounts[0]);
  assert.equal(details.className, 'vids-account-details'); assert(text(details).includes('图片额度') && text(details).includes('视频额度') && text(details).includes('上次读取'));
  const detailsHTML = unified.api.accountDetailsHTML(unified.api.state.accounts[0]);
  assert(detailsHTML.includes('vids-detail-metric') && detailsHTML.includes('vids-detail-actions') && detailsHTML.includes('data-vids-action="refresh"'));
  assert(detailsHTML.includes('&lt;img') && !detailsHTML.includes('<img') && !detailsHTML.includes('PRIVATE'), 'Serialized details contain only escaped display fields');
  let finishUnifiedRefresh;
  unified.respond(() => new Promise(resolve => { finishUnifiedRefresh = () => resolve(json({ account: account(4, { last_quota: { image: quota(9), video: quota(75) } }) })); }));
  const unifiedRefresh = details.children.at(-1).children[0], unifiedQuery = unified.delegatedClick(unifiedRefresh);
  await unified.delegatedClick(unifiedRefresh); await unified.api.load(); await unified.api.onboardAll();
  assert.equal(unified.requests.length, 3, 'Serialized detail actions use the existing refresh lock and block competing lifecycle actions');
  assert.equal(unified.nodes.vidsOnboardAll.disabled, true);
  finishUnifiedRefresh(); await unifiedQuery;
  assert.equal(unified.api.state.accounts[0].last_quota.image.available, 9); assert.equal(unified.nodes.vidsAccountNotice.textContent, '额度已更新。');
  assert.equal(unified.nodes.vidsOnboardAll.disabled, false);
  unified.respond(async (url, options) => { if (options.method === 'POST') throw new Error('PRIVATE'); return json(url.endsWith('/candidates') ? { accounts: [candidate(6)] } : detail([account()])); });
  await unified.nodes.vidsOnboardAll.click(); await unified.nodes.vidsOnboardAll.click();
  assert.equal(unified.api.state.results.get(6).state, 'unknown'); assert(text(unified.nodes.vidsCandidates).includes('接入结果尚未确认'));
  assert.equal(unified.requests.filter(request => request.url === '/api/admin/vids/onboard/6').length, 1, 'Unified onboarding also preserves lost responses without retry');

  const unifiedRepair = page(detail([broken()]), false, [], true); await unifiedRepair.ready();
  const repairDetails = unifiedRepair.api.accountDetails(unifiedRepair.api.state.accounts[0]);
  const unifiedRecover = repairDetails.children.at(-1).children.find(child => child.dataset.vidsAction === 'reconnect');
  unifiedRepair.respond(async (url, options) => options.method === 'POST' ? json({ error: { code: 'identity_mismatch', message: 'PRIVATE' } }, 409) : json(url.endsWith('/candidates') ? { accounts: [] } : detail([broken()])));
  await unifiedRepair.delegatedClick(unifiedRecover);
  assert.equal(unifiedRepair.api.state.repairs.get(4).state, 'failed'); assert(text(unifiedRepair.api.accountDetails(unifiedRepair.api.state.accounts[0])).includes('恢复已阻止'));
  assert.equal(unifiedRepair.api.readyAccount(unifiedRepair.api.state.accounts[0]), false); assert(!unifiedRepair.requests.some(request => request.url.endsWith('/quota-refresh')));

  const vidsHTML = fs.readFileSync(path.join(root, 'static/vids.html'), 'utf8');
  const cachedFailure = page(detail([]), false, [{ token_id: 6, email: 'synthetic@example.test', state: 'failed', code: 'login_required' }]);
  await cachedFailure.ready();
  assert(text(cachedFailure.nodes.candidates).includes('需要重新登录 Google 账号后再接入'));
  assert.equal(cachedFailure.requests.filter(request => request.options.method === 'POST').length, 0);
  const vidsScript = fs.readFileSync(path.join(root, 'static/js/vids.js'), 'utf8');
  assert(vidsHTML.includes('href="/vids-accounts"')); assert(vidsHTML.includes('图片剩余') && vidsHTML.includes('视频剩余'));
  assert(!vidsScript.includes('/quota-refresh'), 'Generation page only reads cached quota');
  for (const id of ['apiKey', 'tokenId', 'accountSelect']) assert(!vidsHTML.includes(`id="${id}"`));
  console.log('PASS: Vids accounts, cached quota, sequential explicit onboarding, explicit session recovery, busy/identity failures, lost-response preservation, cookie auth, iframe resize and unified controller bridge');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
