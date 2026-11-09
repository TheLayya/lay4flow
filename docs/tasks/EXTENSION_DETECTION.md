# EXTENSION-DETECTION

- State: done
- Goal: Installed extension 1.8.2 must not be misdiagnosed as incompatible when the normal browser uses /login#/accounts.
- Root cause: content.js and background MANAGEMENT_PATHS omitted /login; hash router stays on entry pathname after login.
- Fix: allow all actual SPA entry routes, distinguish no connection from an incompatible version. Extension package 1.8.3; minimum remains1.8.2 so users may use existing extension at root /#/accounts.
- Verification: synthetic content handshake on root/login/manage/test/vids/api-guide including foreign origin rejection; Chrome APIs fixture; builder5 tests; frontend166 passed and rebuilt. No browser installation/permission was changed automatically.
- Output: dist/account-login-1.8.3-38001.zip; exact origin http://127.0.0.1:38001. Other port/host must download a package from that same management origin.
- Local runtime updated with generation error rules/compact settings and detection fix. Windows final package recorded separately in build artifacts.
- Final setup EXE built successfully (dist/1.5.6-rules-extension-final-20261109), SHA256SUMS verified; runtime ZIP contains final replay guard, editable-only rules, compact settings, and extension1.8.3 entry fix. Runtime health passed.

- User confirmed root127.0.0.1:38001 and loading source account_import_extension directory. Root cause for this user: generic source manifest had no management content script and required toolbar permission action, unlike downloaded static package. Source manifest1.8.4 now preloads bridge on loopback only; remote sites retain explicit toolbar permission. Discovery broadcasts without stale extension_id and clears obsolete receipt only on new compatible bridge.
- Real Chromium acceptance: static downloaded package connected1.8.3; source directory after fix connected1.8.4 at root, no page errors. No real account/cookies used. Frontend167 and builder6 passed; content-script synthetic entry/origin and Chrome API tests passed.

- Final source-mode acceptance used normal Chromium persistent context loading the real repository directory; root38001 rendered 'extension connected1.8.4'. Frontend discovery regression verifies broadcasting past stale receipt extensionID. Local running image source-extension-184-20261109 healthy.

- Final full check1653 tests: same baseline2 Flow HTTP renewal failures and2 Vids runtime errors,1skip. No new regression. All167frontend,6builder,real Chromium extension source connect and message simulation checks passed.
