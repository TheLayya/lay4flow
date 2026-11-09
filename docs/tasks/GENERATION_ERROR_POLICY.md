# GENERATION-ERROR-POLICY

- Owner: Codex
- State: done
- Goal: Flow generation handles keyword actions, excludes blocked accounts, switches accounts on confirmed rejection, and protects accounts after consecutive failures.
- Scope: persisted configurable rule policy, Flow selection/generation orchestration, Settings and account status, synthetic acceptance, local build/deployment.
- Rules: case-insensitive literal first match; stored editable text is the only active rule set. Five initial example lines may be changed, deleted or emptied without hidden fallback. LIMIT duration (0 excludes this request only), DEACTIVE until cleared, RETRY no status change, DISABLE manual re-enable, DELAY request delay. s/m/h units.
- Preserve: fixed proxies, Profile/identity semantics, remote captcha only; login imports still do not retry automatically. Do not call live generation for acceptance.
- Safety: retry only before submission or confirmed error-only rejection; no replay on unknown outcome, successful partial payload, existing operation or poll failure. One bounded retry budget includes attempts; release slots/pending/leases before another account.
- Acceptance: quota error cools current account 24h and safely tries another; all exhausted stop; cooldown survives restart/expires; disabled/abnormal excluded; success resets consecutive count; invalid rule line rejected without changing settings; UI explains rule status and attempt bounds.
- Baseline: full suite has existing 2 Flow HTTP renewal failures and 2 Vids runtime errors, 1 skipped. Preserve current user changes.

- Verified: editable rules persistence/removal/clear 15 tests; safe account failover/RETRY/single-budget/unknown outcome/slot cleanup/selection 7 tests; Flow profile generation 16; Flow RPC 30; generation concurrency 13; frontend 166 tests; compact rules UI 5 browser scenes; production frontend build and runtime smoke first start/restart passed.
- Full check ran 1651 tests with only baseline 2 failures/2 errors/1 skip. Subsequent focused changes passed affected tests. No paid/live generation was called.
- Local Docker error-policy-20261109 deployed at port38001; authenticated policy API and accounts status fields returned 200, five initial editable rules, threshold3 cooldown300sec max-account-attempts3.
- User clarified settings must be compact with top category tabs and completely editable rules. Implemented six tabs; rule deletion/empty save survives reload with no hidden fallback.

- Final review found account_cooldown post-acceptance exception could bypass replay guard. Fixed and added a regression: accepted cooldown never selects a second account. Final focused orchestration suite 8 passed; concurrency13 passed. Returned error rules now override non-account exclusions too.

- Final deployed image rules-extension-final-20261109 additionally includes 1.8.3 content-bridge entry-path fix and safe replay regression. Windows final package dist/1.5.6-rules-extension-final-20261109 includes both settings and detection changes.
