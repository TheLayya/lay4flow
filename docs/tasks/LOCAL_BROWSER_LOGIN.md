# LOCAL-BROWSER-LOGIN

- Owner: Codex
- State: done
- Goal: Windows users can start extension login with ordinary local Chrome/Edge, default to an independent incognito window, and do not need a fingerprint-browser product.
- Scope: native fixed-origin browser bridge, existing extension dialog/defaults, incognito permission and shared-store guard, public documentation, desktop build/package.
- Preserve: native WebView2 remains the management UI; browser opens only this instance's configured loopback origin; no arbitrary executable/URL from renderer; fixed server proxy/identity checks; no automatic retries/cold-reopen gates.
- Acceptance: desktop button opens installed Chrome/Edge; extension defaults to isolated; current browser session remains optional; missing incognito permission gives actionable feedback; existing unrelated private sessions are not cleared.
- Verification: native build and executable-free launch-plan tests, synthetic Chrome API tests, frontend unit/e2e, full offline check, frontend rebuild, public-only Windows packaging.

- Verified: desktop .NET publish; actual C# fixed-origin launch planner test; Chrome API permission/shared-store tests; extension builder 5 tests; desktop contract 3 tests; frontend 162 tests; desktop handoff/queue e2e 5 scenarios; package smoke first start/restart without external requests; production frontend rebuilt.
- Windows installer: dist/1.5.6-local-incognito-20261109/lay4flow-remote-1.5.6-windows-x64-setup.exe; public ZIP checked for private-file exclusion and native binary match; setup SHA-256 recorded in SHA256SUMS.
- Full check: 1629 tests, existing 2 Flow HTTP renewal failures and 2 Vids runtime errors, 1 skipped. No new affected failures. Independent review passed; no live Google login was performed this turn.
- Installed user runtime was not modified; use the generated installer to obtain the new desktop bridge.
