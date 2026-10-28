(() => {
  const managementPaths = ["/", "/manage", "/vids-accounts", "/vids/accounts"];
  if (!managementPaths.includes(location.pathname)) return;
  if (window.__lay4flowImportBridgeInstalled || window.__flow2apiImportBridgeInstalled) return;
  window.__lay4flowImportBridgeInstalled = true;
  const SOURCE = "lay4flow-manage";
  // Reply on the cached management page's bridge namespace during upgrades.
  const LEGACY_SOURCE = "flow2api-manage";
  let replySource = "lay4flow-import-extension";
  const pendingSubmissions = new Set();
  const post = (type, payload) => window.postMessage({source: replySource, type, ...(payload || {}), extension_id: chrome.runtime.id}, location.origin);
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin) return;
    const msg = event.data || {};
    if (msg.source !== SOURCE && msg.source !== LEGACY_SOURCE) return;
    if (msg.extension_id && msg.extension_id !== chrome.runtime.id) return;
    replySource = msg.source === LEGACY_SOURCE ? "flow2api-import-extension" : "lay4flow-import-extension";
    if (msg.type === "import.hello") {
      post("import.ready", {version: "1.8.0"});
      if (msg.session_id) chrome.runtime.sendMessage({type: "import.status", session_id: msg.session_id, admin_origin: location.origin}, response => {
        if (!chrome.runtime.lastError && response?.type === "import.status") post("import.status", response);
      });
      return;
    }
    if (msg.type === "import.start.v2") {
      try {
        const target = new URL(msg.payload?.complete_url);
        const back = new URL(msg.payload?.return_url);
        if (target.origin !== location.origin || target.pathname !== "/api/plugin/import/complete" ||
            back.origin !== location.origin || !managementPaths.includes(back.pathname)) return;
      } catch (_) {
        return;
      }
      chrome.runtime.sendMessage({type: "import.start", ...(msg.payload || {}), admin_origin: location.origin, admin_url: location.href}, response => {
        if (chrome.runtime.lastError || response?.type !== "import.accepted") post("import.error", {session_id: msg.payload.session_id, error_code: "extension_unavailable", error_summary: response?.error_summary || chrome.runtime.lastError?.message || "扩展未响应"});
        else post("import.accepted", response);
      });
    }
    if (msg.type === "import.cancel.v2") chrome.runtime.sendMessage({type: "import.cancel", ...(msg.payload || {})});
  });
  chrome.runtime.onMessage.addListener(message => {
    if (message?.type === "import.permission" && !message.ok) {
      post("import.error", {error_code: "extension_setup_required", error_summary: message.error || "请先授权当前管理页"});
    }
    if (message?.type === "import.submit") {
      if (message.admin_origin !== location.origin) return;
      let target;
      try {
        target = new URL(message.complete_url);
      } catch (_) {
        chrome.runtime.sendMessage({type: "import.submit.result", session_id: message.session_id, ok: false, status: 0});
        return;
      }
      if (target.origin !== location.origin || target.pathname !== "/api/plugin/import/complete") {
        chrome.runtime.sendMessage({type: "import.submit.result", session_id: message.session_id, ok: false, status: 0});
        return;
      }
      // The background retries delivery after 30s. Keep one HTTP request until
      // it settles, including while the server creates the browser Profile.
      if (pendingSubmissions.has(message.session_id)) return;
      pendingSubmissions.add(message.session_id);
      fetch(target.href, {
        method: "POST",
        credentials: "same-origin",
        redirect: "error",
        headers: {"Content-Type": "application/json", "Authorization": message.authorization || ""},
        body: JSON.stringify(message.payload || {})
      }).then(async response => {
        const result = await response.json().catch(() => ({}));
        const detail = result.detail && typeof result.detail === "object" ? result.detail : {};
        chrome.runtime.sendMessage({type: "import.submit.result", session_id: message.session_id, ok: response.ok, status: response.status, result: {
          success: result.success,
          email: typeof result.email === "string" ? result.email : "",
          action: typeof result.action === "string" ? result.action : "",
          error_code: ["profile_bootstrap_failed", "profile_unavailable"].includes(detail.error_code) ? detail.error_code : "",
          reason: typeof detail.reason === "string" && /^[a-z_]{1,40}$/.test(detail.reason) ? detail.reason : ""
        }});
      }).catch(() => chrome.runtime.sendMessage({type: "import.submit.result", session_id: message.session_id, ok: false, status: 0}))
        .finally(() => pendingSubmissions.delete(message.session_id));
      return;
    }
    if (message?.type === "import.result") post("import.result", { ...message, success: message.state === "succeeded", result: { email: message.email || "", action: message.action || "" } });
    if (message?.type === "import.error" || message?.type === "import.progress") post(message.type, message);
  });
  post("import.ready", {version: "1.8.0"});
  // Cached management pages may have sent hello before this bridge was installed.
  replySource = "flow2api-import-extension";
  post("import.ready", {version: "1.8.0"});
  replySource = "lay4flow-import-extension";
})();
