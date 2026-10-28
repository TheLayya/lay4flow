const FLOW_URL = "https://flow.google.com/";
const IMPORT_KEY_PREFIX = "lay4flow_account_import/";
// Existing imports must finish cleanup after an extension update.
const LEGACY_IMPORT_KEY_PREFIX = "flow2api_account_import/";
const PROBE_DELAY_MS = 1500;
const ACCOUNT_COOKIE_NAMES = new Set([
  "SID", "HSID", "SSID", "APISID", "SAPISID", "OSID", "__Secure-OSID",
  "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID"
]);
const MANAGE_ORIGINS_KEY = "lay4flow_manage_origins";
const LEGACY_MANAGE_ORIGINS_KEY = "flow2api_manage_origins";
const STATIC_MANIFEST = false;
const MANAGEMENT_PATHS = new Set(["/", "/manage", "/vids-accounts", "/vids/accounts"]);

// One active import: Chrome incognito windows share a cookie store.
// Only routing metadata and a short-lived nonce enter session storage.
const activeImports = new Map();
const probeTimers = new Map();

function storageKey(id) { return activeImports.get(id)?.metadataKey || IMPORT_KEY_PREFIX + id; }
function now() { return Date.now(); }
function safeString(value) { return typeof value === "string" ? value.trim() : ""; }
function errorPayload(code, summary, id = "") {
  return {type: "import.error", error_code: code, error_summary: summary, session_id: id};
}
function validManagementUrl(value, origin, path) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password &&
      url.origin === origin && (!path || url.pathname === path);
  } catch (_) { return false; }
}
function validSender(sender, origin) {
  return sender.frameId !== undefined && sender.frameId !== 0 ? false :
    Number.isInteger(sender.tab?.id) && validAdminUrl(sender.url || sender.tab.url, origin);
}
function validAdminUrl(value, origin) {
  try { return validManagementUrl(value, origin) && MANAGEMENT_PATHS.has(new URL(value).pathname); }
  catch (_) { return false; }
}
function originPattern(origin) { return new URL(origin).origin + "/*"; }

async function ensureManagePermission(tab) {
  let url;
  try { url = new URL(tab?.url); } catch (_) { return; }
  if (!validAdminUrl(url.href, url.origin)) return;
  // Called directly from the extension action user gesture.
  const granted = await chrome.permissions.request({origins: [originPattern(url.origin)]});
  if (!granted) return;
  const data = await chrome.storage.local.get([MANAGE_ORIGINS_KEY, LEGACY_MANAGE_ORIGINS_KEY]);
  const origins = [...new Set([...(data[MANAGE_ORIGINS_KEY] || data[LEGACY_MANAGE_ORIGINS_KEY] || []), url.origin])];
  const suffix = btoa(url.origin).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
  const id = "lay4flow-manage-" + suffix;
  for (const scriptId of [id, "flow2api-manage-" + suffix]) {
    await chrome.scripting.unregisterContentScripts({ids: [scriptId]}).catch(() => {});
  }
  await chrome.scripting.registerContentScripts([{
    id, matches: [url.origin + "/*"], js: ["content.js"], runAt: "document_start",
    persistAcrossSessions: true
  }]);
  await chrome.storage.local.set({[MANAGE_ORIGINS_KEY]: origins});
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["content.js"]});
}

function sendAdmin(record, message) {
  chrome.tabs.sendMessage(record.adminTabId, {...message, admin_origin: record.adminOrigin}, () => {
    void chrome.runtime.lastError;
  });
}
async function focusAdmin(record) {
  try {
    const tab = await chrome.tabs.get(record.adminTabId);
    if (!validAdminUrl(tab.url, record.adminOrigin)) return;
    await chrome.tabs.update(tab.id, {active: true});
    await chrome.windows.update(tab.windowId, {focused: true});
  } catch (_) {
    // A tab move/close or focus failure must not cancel an in-flight import.
  }
}
async function saveMetadata(record) {
  const value = {
    session_id: record.sessionId, nonce: record.nonce, complete_url: record.completeUrl,
    return_url: record.returnUrl, admin_origin: record.adminOrigin, admin_tab_id: record.adminTabId,
    window_id: record.windowId, tab_id: record.tabId, cookie_store_id: record.cookieStoreId,
    expires_at: record.expiresAt, state: record.state, outcome: record.outcome,
    retry_at: record.retryAt || 0, browser_identity: record.browserIdentity || null,
    returned_to_admin: !!record.returnedToAdmin,
    browser_profile_source: "cloak", login_context: record.loginContext
  };
  await chrome.storage.session.set({[storageKey(record.sessionId)]: value});
}
async function restoreImports() {
  const stored = await chrome.storage.session.get(null);
  for (const [key, value] of Object.entries(stored)) {
    if ((!key.startsWith(IMPORT_KEY_PREFIX) && !key.startsWith(LEGACY_IMPORT_KEY_PREFIX)) || !value?.session_id) continue;
    const record = {
      metadataKey: key,
      sessionId: value.session_id, nonce: value.nonce, completeUrl: value.complete_url,
      returnUrl: value.return_url, adminOrigin: value.admin_origin, adminTabId: value.admin_tab_id,
      windowId: value.window_id, tabId: value.tab_id, cookieStoreId: value.cookie_store_id,
      expiresAt: value.expires_at, state: value.state, outcome: value.outcome,
      retryAt: value.retry_at || 0, browserIdentity: value.browser_identity || null,
      returnedToAdmin: !!value.returned_to_admin,
      browserProfileSource: "cloak", loginContext: value.login_context === "profile" ? "profile" : "isolated"
    };
    activeImports.set(record.sessionId, record);
    if (record.state === "closing" || record.state === "cleanup_failed" || record.expiresAt <= now() || record.state === "opening") {
      await finishImport(record, record.outcome || errorPayload("session_expired", "导入已过期或中断，已清理临时登录", record.sessionId));
    } else {
      // A lost response is safe to retry: the server commits a session once.
      record.state = "tab_open";
      scheduleProbe(record);
    }
  }
}

function normalizeDomain(domain) { return safeString(domain).replace(/^\./, "").toLowerCase(); }
function googleDomain(domain) {
  const host = normalizeDomain(domain);
  return host === "google.com" || host.endsWith(".google.com") || host === "labs.google" || host.endsWith(".labs.google");
}
function cookieKey(cookie) {
  return JSON.stringify([cookie.name, cookie.domain, cookie.path, cookie.storeId, cookie.partitionKey || null]);
}
async function allScopedCookies(storeId) {
  if (!storeId) return [];
  const all = [];
  for (const query of [
    {url: FLOW_URL, storeId},
    {url: "https://accounts.google.com/", storeId},
    {domain: "google.com", storeId}
  ]) {
    all.push(...await chrome.cookies.getAll(query));
  }
  return [...new Map(all.filter(c => c.storeId === storeId && googleDomain(c.domain)).map(c => [cookieKey(c), c])).values()];
}
function selectGoogleCookies(cookies) {
  const selected = new Map();
  for (const cookie of cookies) {
    // Keep new Google cookie names without expanding the account/domain boundary.
    if (!safeString(cookie.name) || cookie.name === "foai_state" || !safeString(cookie.value) || cookie.partitionKey) continue;
    if (Number.isFinite(cookie.expirationDate) && cookie.expirationDate * 1000 <= now()) continue;
    if (!["google.com", "www.google.com", "accounts.google.com", "flow.google.com"].includes(normalizeDomain(cookie.domain))) continue;
    selected.set(cookieKey(cookie), cookie);
  }
  return [...selected.values()];
}
function hasGoogleAccountCookie(cookies) {
  // Match Token-Updater's readiness check; the server validates the identity.
  // Modern Flow does not guarantee the complete legacy five-cookie family.
  return cookies.some(c => ACCOUNT_COOKIE_NAMES.has(c.name) && c.value);
}
function reportProgress(record, state, cookieCount = 0) {
  if (record.progressState === state) return;
  record.progressState = state;
  sendAdmin(record, {type: "import.progress", session_id: record.sessionId, state, cookie_count: cookieCount});
}
function cookiePayload(cookies) {
  return JSON.stringify(cookies.map(cookie => {
    const {name, value, domain, path} = cookie;
    const row = {name, value, domain, path};
    for (const field of ["secure", "httpOnly", "hostOnly"]) {
      if (typeof cookie[field] === "boolean") row[field] = cookie[field];
    }
    if (["no_restriction", "lax", "strict", "unspecified"].includes(cookie.sameSite)) row.sameSite = cookie.sameSite;
    if (Number.isFinite(cookie.expirationDate) && cookie.expirationDate > 0) row.expirationDate = cookie.expirationDate;
    return row;
  }));
}
async function clearImportCookies(record) {
  // Existing fingerprint-profile login state belongs to the user.
  if (record.loginContext === "profile") return;
  // Inspect the live store, not the submission allowlist or an old cookie snapshot.
  // Normal-profile cookies and lay4flow's login cookie are never selected.
  const stores = await chrome.cookies.getAllCookieStores();
  if (!stores.some(store => store.id === record.cookieStoreId)) return;
  for (let pass = 0; pass < 3; pass++) {
    const cookies = await allScopedCookies(record.cookieStoreId);
    if (!cookies.length) return;
    for (const cookie of cookies) {
      const details = {
        url: (cookie.secure === false ? "http://" : "https://") + normalizeDomain(cookie.domain) + (cookie.path || "/"),
        name: cookie.name, storeId: record.cookieStoreId
      };
      if (cookie.partitionKey) details.partitionKey = cookie.partitionKey;
      await chrome.cookies.remove(details);
    }
  }
  if ((await allScopedCookies(record.cookieStoreId)).length) throw new Error("cleanup failed");
}
async function closeTabAndWindow(record) {
  if (Number.isInteger(record.windowId)) {
    try { await chrome.windows.remove(record.windowId); }
    catch (_) {
      if ((await chrome.tabs.query({windowId: record.windowId})).length) throw new Error("window still open");
    }
  } else if (Number.isInteger(record.tabId)) {
    await chrome.tabs.remove(record.tabId).catch(() => {});
  }
}
async function finishImport(record, outcome) {
  if (record.finishing) return record.finishing;
  record.finishing = (async () => {
    record.outcome = outcome || record.outcome;
    record.state = "closing";
    clearTimeout(probeTimers.get(record.sessionId));
    probeTimers.delete(record.sessionId);
    await saveMetadata(record);
    try {
      // Close owned pages before deletion so they cannot repopulate cookies.
      await closeTabAndWindow(record);
      await clearImportCookies(record);
      await chrome.storage.session.remove(storageKey(record.sessionId));
      if (chrome.alarms) await chrome.alarms.clear(storageKey(record.sessionId));
      activeImports.delete(record.sessionId);
      record.payload = null;
      if (record.outcome) sendAdmin(record, record.outcome);
      // Preserve the management page's HttpOnly session and show the result there.
      await focusAdmin(record);
    } catch (_) {
      record.state = "cleanup_failed";
      await saveMetadata(record);
      sendAdmin(record, errorPayload("cleanup_failed", "账号窗口清理未完成，正在重试，请勿开始下一个账号", record.sessionId));
      if (chrome.alarms) await chrome.alarms.create(storageKey(record.sessionId), {when: now() + 30000});
      probeTimers.set(record.sessionId, setTimeout(() => finishImport(record, record.outcome), 5000));
    }
  })();
  try { await record.finishing; } finally { delete record.finishing; }
}

async function submit(record, payload) {
  const returnToAdmin = !record.returnedToAdmin;
  record.returnedToAdmin = true;
  record.state = "submitting"; // Reserve before any awaited Chrome API.
  if (returnToAdmin) {
    // A login finished within its deadline gets a separate, bounded processing
    // budget. Re-delivery of this session never extends that budget again.
    record.expiresAt = Math.max(record.expiresAt, now() + 240000);
    if (chrome.alarms) await chrome.alarms.create(storageKey(record.sessionId), {when: record.expiresAt});
  }
  record.payload = payload;
  record.submitAt = now();
  await saveMetadata(record);
  sendAdmin(record, {type: "import.progress", session_id: record.sessionId, state: "submitting"});
  sendAdmin(record, {
    type: "import.submit", session_id: record.sessionId, complete_url: record.completeUrl,
    authorization: `Bearer import:${record.sessionId}.${record.nonce}`,
    payload: record.payload
  });
  scheduleProbe(record, 30000);
  // Login is complete, but the server still has to validate and build the
  // imported Profile. Show that work on the management page immediately.
  // Keep the owned login window/store until the final result and cleanup.
  if (returnToAdmin) await focusAdmin(record);
}
async function probe(record) {
  if (record.probing || ["opening", "closing", "cleanup_failed"].includes(record.state) || !activeImports.has(record.sessionId)) return;
  record.probing = true;
  try {
    if (now() >= record.expiresAt) {
      await finishImport(record, errorPayload("session_expired", "导入超时，临时登录已清理，请重新开始", record.sessionId));
      return;
    }
    if (record.retryAt > now()) { scheduleProbe(record, record.retryAt - now()); return; }
    if (record.state === "submitting") {
      const remaining = 30000 - (now() - record.submitAt);
      if (remaining > 0) { scheduleProbe(record, remaining); return; }
      // No result delivered (page reload/network): re-send only the same session.
      record.state = "tab_open";
    }
    if (record.payload) { await submit(record, record.payload); return; }
    const tab = await chrome.tabs.get(record.tabId);
    const page = new URL(tab.url);
    if (page.hostname === "support.google.com") {
      await finishImport(record, errorPayload("login_required", "Google 未完成授权，请重新登录后导入", record.sessionId)); return;
    }
    const cookies = await allScopedCookies(record.cookieStoreId);
    const googleCookies = selectGoogleCookies(cookies);
    const onFlow = validManagementUrl(tab.url, new URL(FLOW_URL).origin);
    if (!onFlow) {
      reportProgress(record, "waiting_flow");
      scheduleProbe(record); return;
    }
    if (!record.browserIdentity) {
      reportProgress(record, "waiting_flow", googleCookies.length);
      scheduleProbe(record); return;
    }
    if (!hasGoogleAccountCookie(googleCookies) || !googleCookies.some(cookie =>
        ["OSID", "__Secure-OSID"].includes(cookie.name) && cookie.value)) {
      reportProgress(record, "waiting_credentials", googleCookies.length);
      scheduleProbe(record); return;
    }
    const payload = {
      session_id: record.sessionId, tab_id: String(record.tabId), cookie_store_id: record.cookieStoreId,
      google_cookies: googleCookies.length ? cookiePayload(googleCookies) : "",
      browser_identity: record.browserIdentity || {}
    };
    await submit(record, payload);
    } catch (error) {
      await finishImport(record, errorPayload("browser_error", "无法读取本次登录窗口，已结束导入", record.sessionId));
  } finally { record.probing = false; }
}
function scheduleProbe(record, delay = PROBE_DELAY_MS) {
  if (!activeImports.has(record.sessionId) || ["opening", "closing", "cleanup_failed"].includes(record.state)) return;
  // Avoid postponing an existing timeout indefinitely on a stream of cookie events.
  if (probeTimers.has(record.sessionId)) return;
  probeTimers.set(record.sessionId, setTimeout(() => {
    probeTimers.delete(record.sessionId);
    return probe(record);
  }, delay));
}
async function finishSubmit(record, message) {
  const status = Number(message.status);
  if (message.ok && status >= 200 && status < 300 && message.result?.success === true) {
    await finishImport(record, {
      type: "import.result", session_id: record.sessionId, state: "succeeded",
      email: safeString(message.result.email), action: safeString(message.result.action)
    });
  } else if (["profile_bootstrap_failed", "profile_unavailable"].includes(message.result?.error_code)) {
    const reasons = {
      timeout: "服务器浏览器加载 Flow 工作台超时，请检查服务器代理和连接后再试。",
      session_invalid: "服务器浏览器会话接口验证失败，请重新登录后导入。",
      invalid_cookies: "服务器浏览器验证出的账号身份不一致，请重新登录后导入。",
      identity_mismatch: "服务器浏览器验证出的账号身份不一致，请重新登录后导入。",
      account_probe_error: "服务器浏览器账号接口校验失败，请检查代理和网络后重试。",
      credits_invalid: "服务器浏览器额度接口返回无效数据，请稍后重试。",
      transient_error: "服务器浏览器账号接口暂未响应，请检查代理和网络后重试。",
      login_required: "服务器浏览器登录未完成，请检查账号登录状态后再试。",
      public_page: "服务器浏览器仍停留在 Flow 公共页面，请检查登录状态后再试。",
      environment_changed: "服务器浏览器环境与已保存 Profile 不一致，请检查 Profile 配置。",
      cookie_injection_failed: "服务器浏览器接收登录数据失败，请检查 Profile 服务。"
    };
    const summary = message.result.error_code === "profile_unavailable"
      ? "服务器账号浏览器服务尚未配置，请启用 Profile 服务后再导入。"
      : reasons[message.result.reason] || "登录数据已读取，但服务器浏览器 Profile 初始化失败，请检查服务状态后再试。";
    await finishImport(record, errorPayload(message.result.error_code, summary, record.sessionId));
  } else if ([0, 409, 429].includes(status) || status >= 500) {
    record.state = "tab_open";
    record.retryAt = now() + 3000;
    await saveMetadata(record);
    clearTimeout(probeTimers.get(record.sessionId));
    probeTimers.delete(record.sessionId);
    scheduleProbe(record, 3000);
  } else if (status === 422) {
    // Older servers send a string detail. Keep their Profile rejection visible
    // too, instead of losing a late response behind an earlier busy reply.
    await finishImport(record, errorPayload("import_rejected", "登录数据已读取，但服务器未完成账号或 Profile 验证，请检查服务状态后再试。", record.sessionId));
  } else {
    await finishImport(record, errorPayload("import_rejected", "服务器未接受账号，临时登录已清理，请重新开始", record.sessionId));
  }
}

async function startImport(message, sender, sendResponse) {
  await importsRestored;
  const sessionId = safeString(message.session_id);
  const nonce = safeString(message.nonce);
  const adminOrigin = safeString(message.admin_origin);
  if (!sessionId || !nonce || !validSender(sender, adminOrigin) ||
      !validManagementUrl(message.complete_url, adminOrigin, "/api/plugin/import/complete") ||
      !validAdminUrl(message.return_url, adminOrigin)) {
    sendResponse(errorPayload("origin_mismatch", "请从管理页开始导入")); return;
  }
  const existing = activeImports.get(sessionId);
  if (existing && existing.adminTabId === sender.tab.id && existing.nonce === nonce && !["closing", "cleanup_failed"].includes(existing.state)) {
    sendResponse(existing.openingPromise ? await existing.openingPromise : {
      type: "import.accepted", session_id: sessionId, tab_id: String(existing.tabId), cookie_store_id: existing.cookieStoreId
    }); return;
  }
  if (activeImports.size) { sendResponse(errorPayload("session_busy", "已有账号正在导入或清理", sessionId)); return; }
  const record = {
    sessionId, nonce, adminOrigin, completeUrl: message.complete_url, returnUrl: message.return_url,
    browserProfileSource: "cloak", loginContext: message.login_context === "profile" ? "profile" : "isolated",
    adminTabId: sender.tab.id, tabId: null, windowId: null, cookieStoreId: null,
    expiresAt: Math.min(Number(message.expires_at) * 1000, now() + 300000), state: "opening"
  };
  if (!Number.isFinite(record.expiresAt) || record.expiresAt <= now()) {
    sendResponse(errorPayload("session_expired", "导入会话已过期", sessionId)); return;
  }
  // Reservation MUST precede any async work, including windows.create.
  activeImports.set(sessionId, record);
  record.openingPromise = (async () => {
    try {
      await saveMetadata(record);
      if (chrome.alarms) await chrome.alarms.create(storageKey(sessionId), {when: record.expiresAt});
      let tab;
      if (record.loginContext === "profile") {
        tab = await chrome.tabs.create({url: "about:blank", active: true, windowId: sender.tab.windowId});
      } else {
        const created = await chrome.windows.create({url: "about:blank", focused: true, incognito: true, type: "normal"});
        record.windowId = created.id;
        tab = created.tabs?.[0];
      }
      record.tabId = tab?.id;
      if (!Number.isInteger(record.tabId) || record.cancelled) throw new Error("window cancelled");
      const stores = await chrome.cookies.getAllCookieStores();
      record.cookieStoreId = safeString(stores.find(store => store.tabIds.includes(tab.id))?.id);
      if (!record.cookieStoreId) throw new Error("cookie store unavailable");
      await saveMetadata(record);
      await clearImportCookies(record);
      if (record.cancelled) throw new Error("cancelled");
      record.state = "tab_open";
      await saveMetadata(record);
      await chrome.tabs.update(record.tabId, {url: FLOW_URL});
      scheduleProbe(record);
      return {type: "import.accepted", session_id: sessionId, tab_id: String(record.tabId), cookie_store_id: record.cookieStoreId};
    } catch (openError) {
      const error = errorPayload(record.cancelled ? "cancelled" : "browser_error", "导入窗口初始化未完成，已尝试清理", sessionId);
      await finishImport(record, error);
      return error;
    }
  })();
  const result = await record.openingPromise;
  delete record.openingPromise;
  sendResponse(result);
}
async function cancelImport(sessionId) {
  const record = activeImports.get(sessionId);
  if (!record) return;
  if (record.returnedToAdmin) {
    reportProgress(record, "submitting");
    return;
  }
  record.cancelled = true;
  if (record.openingPromise) { await record.openingPromise; if (!activeImports.has(sessionId)) return; }
  await finishImport(record, errorPayload("cancelled", "已取消账号导入", sessionId));
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    await importsRestored;
    if (message?.type === "import.status") {
      const record = [...activeImports.values()].find(r => r.adminTabId === sender.tab?.id && validSender(sender, r.adminOrigin));
      if (!record && !validSender(sender, safeString(message.admin_origin))) {
        sendResponse(errorPayload("origin_mismatch", "请从管理页查看导入状态")); return;
      }
      sendResponse({type: "import.status", active: !!record,
        session_id: record?.sessionId || safeString(message.session_id),
        state: record?.returnedToAdmin && ["tab_open", "submitting"].includes(record.state) ? "submitting" : record?.progressState || record?.state || "",
        expires_at: record ? record.expiresAt / 1000 : 0});
      return;
    }
    if (message?.type === "import.flow.identity") {
      const record = [...activeImports.values()].find(r => r.tabId === sender.tab?.id);
      if (record && record.state === "tab_open" && (!sender.frameId || sender.frameId === 0) && validManagementUrl(sender.url || sender.tab?.url, "https://flow.google.com")) {
        record.browserIdentity = message.identity;
        await saveMetadata(record);
      }
      sendResponse({ok: true}); return;
    }
    if (message?.type === "import.start") { await startImport(message, sender, sendResponse); return; }
    const record = activeImports.get(safeString(message?.session_id));
    if (!record || record.adminTabId !== sender.tab?.id || !validSender(sender, record.adminOrigin)) {
      sendResponse(errorPayload("session_invalid", "导入会话不存在")); return;
    }
    if (message.type === "import.submit.result" && ["tab_open", "submitting"].includes(record.state) && record.returnedToAdmin) await finishSubmit(record, message);
    else if (message.type === "import.cancel") await cancelImport(record.sessionId);
    sendResponse({ok: true});
  })().catch(() => sendResponse(errorPayload("browser_error", "扩展处理失败，请重新开始")));
  return true;
});
chrome.action.onClicked.addListener(tab => {
  if (!STATIC_MANIFEST) ensureManagePermission(tab).catch(() => {});
});
chrome.cookies.onChanged.addListener(change => {
  importsRestored.then(() => {
    for (const record of activeImports.values()) {
      if (record.cookieStoreId === change.cookie?.storeId && googleDomain(change.cookie?.domain)) scheduleProbe(record);
    }
  }).catch(() => {});
});
chrome.tabs.onUpdated.addListener(tabId => {
  importsRestored.then(() => {
    for (const record of activeImports.values()) if (record.tabId === tabId) scheduleProbe(record);
  }).catch(() => {});
});
chrome.tabs.onRemoved.addListener(tabId => {
  const impacted = [...activeImports.values()].filter(record =>
    (record.tabId === tabId || record.adminTabId === tabId) &&
    !["closing", "cleanup_failed"].includes(record.state)
  );
  importsRestored.then(async () => {
    for (const record of impacted) {
      if (activeImports.get(record.sessionId) === record &&
          !["closing", "cleanup_failed"].includes(record.state)) {
        await cancelImport(record.sessionId);
      }
    }
  }).catch(() => {});
});
if (chrome.alarms) chrome.alarms.onAlarm.addListener(alarm => {
  importsRestored.then(async () => {
    const prefix = [IMPORT_KEY_PREFIX, LEGACY_IMPORT_KEY_PREFIX].find(value => alarm.name.startsWith(value));
    if (!prefix) return;
    const record = activeImports.get(alarm.name.slice(prefix.length));
    if (!record) return;
    if (["closing", "cleanup_failed"].includes(record.state)) await finishImport(record, record.outcome);
    else await probe(record);
  }).catch(() => {});
});
// Register all event handlers synchronously; each waits for restoration.
const importsRestored = restoreImports();
importsRestored.catch(() => {});
