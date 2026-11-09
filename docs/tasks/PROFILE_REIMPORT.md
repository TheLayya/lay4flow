# PROFILE-REIMPORT

- Owner: Codex
- State: done
- Goal: JSON and extension reimport of an existing Cloak account updates the original account instead of always failing with account_exists/profile_exists.
- Scope: existing-account admission, one-shot staged Profile login replacement, JSON/extension job result accounting, local Docker update.
- Preserve: account ID, local seed, fixed proxy, projects, remarks, user capability/concurrency/enable/auto-refresh choices, normal close/cache behavior and existing workspace changes.
- Acceptance: one validation, no cold reopen/automatic retry; actual email must match; failed validation/CAS restores original Profile and DB; busy accounts report busy; successful job reports updated rather than added.
- Inputs: synthetic tests; any live acceptance recorded separately under ignored tmp and uses only previously authorized credentials.
- Gates: affected tests, full offline check, frontend tests/build, independent review, healthy local runtime.

- Verified synthetic tests: Profile update/rollback 11, reimport integration 5, native transfer 39, extension login 13, queue imports 18, Vids login maintenance 6, frontend 160; desktop/mobile import e2e and production frontend build passed.
- Full `python scripts/check.py`: 1628 tests ran, same existing 2 Flow HTTP renewal failures / 2 Vids runtime errors / 1 skip; no new affected failure.
- Local Docker image `lay4flow-remote:profile-reimport-20261109` deployed at port 38001, healthy.
- Private live acceptance: JSON reimport added=0, updated=1, failed=0; original account id 84 and all account control fields retained. Authenticated extension endpoint acceptance completed with action=updated, token_id=84, attempts=1. No generation calls were used for acceptance.

- Failure evidence retained: initial live reservation was blocked by global Vids lifecycle work; final maintenance queues that reservation once and does not retry login. Subsequent live JSON and extension updates completed.

- done: current local interface updates existing registered Cloak accounts through JSON and extension admission, with healthy runtime and successful live evidence.
