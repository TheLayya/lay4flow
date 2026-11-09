# GENERATION-ATTEMPT-LOGS

- State: done
- Owner: Codex
- Goal: One log per generation request, showing total attempts/retries/accounts and each attempt's account, stage, outcome, error, rule action, delay and duration.
- Scope: request-local attempt ledger, image/video submission instrumentation, shared failover log row, nullable summary columns/migration, lightweight list and lazy detail UI.
- Preserve: retry budget and safe replay boundaries, fixed proxies, no unknown/accepted write replays, no live generation acceptance.
- Acceptance: same-account and cross-account retries persist in order; success retains earlier failures; exhausted/no-account/cancelled states are final; old records say unrecorded; no cookie/token/proxy secret in attempt ledger; list remains lightweight.
- Gates: synthetic flow/slot/DB/API tests, frontend unit and browser tests, required full check, rebuilt frontend and healthy local deployment.

- Verification: `python scripts/check.py` passed (1693 Python tests, 1 expected skip, CJS and package first-start/cold-restart; no upstream network). Earlier full runs exposed stale log assertions and pre-existing Profile/Vids regressions, retained in local diagnostics; fixed and reverified.
- Frontend: `npm.cmd --prefix frontend test` 172/172 passed; log desktop/mobile/historical browser checks 4/4 passed; frontend assets rebuilt.
- Container acceptance: 37 synthetic request-log/Flow/API/storage tests passed in the final image with network disabled and temporary databases. Independent review passed; follow-up corrected preflight timestamps to account-selection time.
- Local delivery: `lay4flow-remote:attempt-logs-20261109` deployed to the existing Compose app at port 38001, health healthy, migration columns present, served frontend and 7 key source/assets hashes match the checkout. Previous image override retained locally. No production request was generated for acceptance.
- User boundary: newly processed requests record attempt history. Existing request records keep unknown counts; no invented historical retries or production data backfill.
