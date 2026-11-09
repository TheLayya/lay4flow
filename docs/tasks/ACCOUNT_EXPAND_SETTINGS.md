# ACCOUNT-EXPAND-SETTINGS

- State: done
- Owner: Codex
- Requirement: replace the oversized account row expansion with Account information / Proxy tabs; remove redundant remark/capability controls there; move session maintenance interval editing into Settings.
- Profiles: account table expansion, per-account proxy UI, global Profile maintenance settings, scheduler and existing token update routes.
- Outcome: expanded content fills usable table viewport and stays compact; tabs separate identity/session information from proxy binding/strategy/credential copy. No repeated remark/image-generation toggles in expansion. Settings provides one persistent global Profile automatic-maintenance switch and interval that actually drive all Profile schedules, not obsolete global ST config.
- Preserve: primary account enable/capability/remark semantics, fixedproxy/identity and explicitcopy, busyaccount protection, refreshschedule recalculation/stalerefresh guard, LIMIT visiblecolumn, single20budget, noprivate artifactspublic.
- Acceptance: default information tab, keyboard access and proxy trigger opening the proxy tab; no writes on expand/tab change; proxy save only submits proxy fields; panel fills available viewport width. Settings reads and saves one shared enable/interval policy, preserves rejected drafts and works on mobile. All Profile schedules use that policy, including old and newly imported accounts; old account-specific intervals do not override it. Saved policy persists across restart and an earlier refresh cannot overwrite its schedule. The modern settings UI does not read the legacy token-refresh API.
- Inputs: currentclean0bd3b36,user redbox screenshot. Outputs: publicsource/tests/docs/screenshots rebuilt frontend/currentDocker plus Git delivery.
- Gates: focused frontend/backend-contract checks and synthetic browser desktop/mobile, independentreview, python -Xutf8 scripts/check.py,npm.cmd --prefix frontend test,build,imageoffline smoke,health.

- Latest authoritative user correction: refresh interval belongs to one unified setting, not per-account editing. Previous pending per-account Settings table is superseded. Implement persistent global Profile maintenance enable/interval with real scheduler consumption and immediate due recalculation, retain per-account automatic-maintenance opt-outs and normal identity/manual refresh semantics. Previous account tabs/layout remain in scope.

## Local verification

- Root transition: in_progress -> in_review after implementing the corrected global policy and account tabs.
- One persisted admin-authenticated Profile maintenance policy defaults to enabled / 30 minutes. All Profile cadence and due recalculation use it; old account intervals remain compatibility data. Account opt-outs and legacy protocol controls retain their existing meaning.
- Global saves and late refresh/import/login/proxy callbacks are serialized or revision-guarded. Per-account success anchors and failure backoff remain separate, with bounded browser concurrency.
- Required `python -X utf8 scripts/check.py`: exit 0; 1908 Python tests, 1 skipped, all CJS checks and offline startup/restart smoke passed. `npm.cmd --prefix frontend test`: 201 passed. Production frontend build completed. Evidence: ignored `tmp/account-settings-required-check.log`, `tmp/account-settings-frontend-test.log`, `tmp/account-settings-build.log`.
- Synthetic browser acceptance covers global save/disable, failed-save draft retention, old links, desktop/mobile, account/proxy tabs and keyboard navigation, isolated proxy writes, and account editor saves that omit cadence. Evidence: `tmp/refresh-settings-browser/verification.json`, `tmp/account-expand-tabs/verification.json`, `tmp/account-editor-ui/verification.json`.
- Ten README screenshots regenerated from production assets and synthetic APIs with no writes/page errors; all 36 local README links resolve. Three new screenshots show both tabs and the unified policy. Public source/runtime allowlist excludes mutable and private state.
- Candidate image `lay4flow-remote:global-profile-maintenance-20261110` built from the public runtime inventory; offline image startup/restart smoke passed with zero upstream requests. No real Google/captcha/generation acceptance was performed.

## Review and delivery

- Independent review found no blocker: 101 focused backend tests, 5 separate migration/concurrent-write/admission audits, and both global-settings/account-tabs browser checks passed. Audit report: ignored `tmp/global-maintenance-final-independent-review.md`. Initial sibling-fixture import-path failure is retained separately and was corrected before the successful review run.
- Root transitions: in_review -> verified -> done after required gates, independent review and runtime acceptance. The 38001 service now uses `lay4flow-remote:global-profile-maintenance-20261110`; healthy startup, current production assets and authenticated policy endpoint verified. No active generation/import/maintenance work was present at replacement. The 62 account identity/fixed-proxy snapshots are unchanged; private backup remains ignored. New global policy is enabled / 30 minutes.
- README text and synthetic screenshots reflect the final global design. Existing Windows 1.6.0 installers were not rebuilt by this current source/Docker change. Public changes delivered through normal Git commit/push; private configs/databases/profiles/diagnostics remain excluded.
