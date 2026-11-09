# CK-JSON-IMPORT

- Owner: Codex
- State: done
- Goal: The existing account Profile import dialog accepts external JSON containing `cookies` and `email`, and imports a usable persistent Cloak account.
- Profiles: backend, web, local Docker runtime.
- Input: synthetic fixtures for automated checks; separately authorized private live acceptance input.
- Output: format adapter, existing background import integration, session persistence, UI guidance, rebuilt frontend.
- Constraints: preserve account identity checks, fixed proxy, normal Profile close/cache behavior, existing user changes, remote-only captcha. Credentials and diagnostics remain outside Git/build context.
- Acceptance: single object/array/mixed formats work; identity mismatch and malformed/expired credentials fail without enabling an account; import validates the fixed-proxy identity/Flow/credits once and closes normally; no cold reopen gate; retry retains identity and exit; existing native and legacy imports remain supported.
- Gates: focused synthetic backend tests, frontend tests, `python scripts/check.py`, frontend build, independent review, live browser/API acceptance recorded separately under ignored `tmp/`.
- Baseline: Existing worktree contains user modifications. Previous full offline check reported 19 failures and 2 errors, including skipped cold checks inconsistent with tests; no baseline changes are reset.

## Progress

- in_progress: Implement existing dialog compatibility and server-side admission, based on prior private live proof of session restore.


- Requirement update: User explicitly removed the import cold-reopen gate. JSON/Profile/extension imports validate the current fixed-proxy session and close normally; session restoration remains for future runtime starts.

- Verified: external JSON adapter 6 synthetic tests; Profile transfer 39 tests; frontend 160 tests; desktop/mobile import e2e; frontend build; Docker deployed at 127.0.0.1:38001 with successful health check.
- Live acceptance (private evidence only): provided JSON imported through authenticated /api/profile-transfer/jobs, added=1, failed=0, identity and credits verified, no cold-reopen stages/calls.
- Scope: no generation acceptance performed. Full offline suite evidence remains in ignored tmp; pre-existing unrelated failures reported separately.

- Final verification: 1605 offline tests ran; 2 failures in pre-existing Flow HTTP renewal tests, 2 errors in pre-existing Vids runtime tests, 1 skipped. New/affected Cookie and Profile import tests pass. Independent review found no critical new import issue.
- done: local runtime is healthy and the supplied account was added through the final single-check import pipeline; interface supports subsequent imports.
