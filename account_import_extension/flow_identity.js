(() => {
  if (window.__lay4flowIdentitySent) return;
  window.__lay4flowIdentitySent = true;
  chrome.runtime.sendMessage({
    type: "import.flow.identity",
    identity: {
      user_agent: navigator.userAgent,
      language: navigator.language,
      languages: Array.from(navigator.languages || []),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      platform: navigator.platform || "Win32",
      viewport_width: Math.max(320, Math.min(4096, innerWidth || 1280)),
      viewport_height: Math.max(240, Math.min(4096, innerHeight || 800))
    }
  });
})();
