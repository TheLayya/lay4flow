# LIMIT-VISIBILITY-DEFAULT20

- State: done
- Owner: Codex
- Requirement: account management must clearly show LIMIT/cooldown and absolute unlock time in the main table; default max attempts including first must be20.
- Profiles: account table UI, protection timestamp/state, config defaults and backward compatibility, local runtime/source delivery.
- Acceptance: visible Flow/Vids protection state and local unlock date/time without hover/expand; finite cooldown separate from user enable and abnormal/disabled/manual actions; expired state not misleading; no per-row network polling; default20 across fresh config/schema/model/backend/frontend; explicit existing valid settings preserved; synthetic tests/screenshots only.
- Preserve: single shared retry budget; fixedproxy/identity/Profile semantics; existing health protection and clear actions; remote_browser captcha only; private data out ofGit.
- Inputs: clean0dcaf5f,currentAPI generation_protection/protection records. Outputs:publicsource/tests/docs and rebuilt frontend/currentDocker. User20currentsetting not rewritten.
- Gates: focusedtempDB/backendtests,frontendunit+mockbrowser,independentreview,python -Xutf8 scripts/check.py,npm.cmd --prefix frontend test,build,imageoffline smoke,localhealth.

## Implemented and verified

- Main-table column “生成保护 / 解锁时间” is visible next to enable status: Flow/Vids separate, native yellow LIMIT badge, exact local YYYY-MM-DD HH:mm:ss timestamp, countdown, reason and explicit clear action. LIMIT filter added; abnormal/rule-disabled/manual pause distinguished. One page-local clock, no per-row network. Expiry waits authoritative refresh and never enables a paused account.
- DEFAULT_FLOW_MAX_RETRIES20 shared by runtime/model/schema/settings; public TOML and UI start20. Fresh/missing/invalid fallback20, explicit saved3 preserved through migrate/reload/partial update; zero clamp1 unchanged. No actual config/database rewritten.
- Backend focused48tests, frontend195tests, four synthetic desktop/mobile LIMIT groups passed. Independent seven real temporary-SQLite audits,41backendtests and3UItests passed; reviewed no blocking finding. Evidence tmp/default20-final.md, tmp/limit-account-visibility-final.md, tmp/limit-default20-independent-review.md.
- Fullrequiredcheck python -Xutf8 scripts/check.py exit0:1902Python tests(1skipped),allCJSandpackage startup/restart smoke passed(tmp/limit-default20-required-check.txt). Production frontend rebuilt after final native badge change; seven README screenshots replayed0writes/0pageerrors and local links resolve. Readme fixture uses synthetic24hLIMIT only.
- Final image lay4flow-remote:limit-visible-default20-final-20261110 built from corrected source/assets and offline smoke passed; this checkout's38001updated afterzeroactivejobs, private account/proxy/config state retained. Earlier image did not include the last badge-render improvement and was replaced before final delivery. Existing Windows1.6.0 archives are not overwritten by this currentDocker/source update.
- Root transitions in_progress -> in_review -> verified -> done after independent review, aggregate gates and runtime/source check; public changes committed/pushed normally, private artifacts remain ignored. Live Google/solve/generation not performed.
