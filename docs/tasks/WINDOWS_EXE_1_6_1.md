# WINDOWS-EXE-1.6.1

- State: in_review
- Owner: Codex
- Requirement: build the updated Windows EXE installer, then commit and push the completed public changes from this checkout.
- Scope: version 1.6.1 includes the existing Flow submission/transport corrections, account submission-proxy settings and Vids automatic pool dispatch/diagnostics. No new provider behavior is introduced by packaging.
- Acceptance: frontend tests/build and required offline checks pass; desktop version matches VERSION; native runtime inventory/dependencies match; public runtime/Windows ZIP/setup EXE are built, inspected and installed in an isolated empty directory; normal Git commit/push succeeds.
- Boundaries: preserve existing 1.6.0 artifacts and the active 38001 service. Exclude private settings, accounts, databases, profiles, generated media, diagnostics and graphify outputs from Git and release packages. Keep MIT/NOTICE and remote_browser-only captcha. No real Google login, paid generation or captcha acceptance; no GitHub Release/tag/upload requested.
- Source evidence: prior unchanged source candidate passed 1967 Python tests (1 skip), 212 frontend tests, six synthetic Chromium checks and isolated Studio two-account completion/replay checks. This packaging run records current version/build/install evidence separately.
- Outputs: ignored dist/1.6.1 assets and SHA256SUMS/build-manifest, this public delivery record, public source commit and origin/main push. Private build/install evidence stays under tmp/exe-package-161-20261111/.


## Candidate verification

- 2026-11-11: native desktop publish passed; executable FileVersion is 1.6.1.0. The existing bundled Python 3.12.10/Cloak runtime is reused after all 2265 inventory entries and the requirements digest were checked.
- Candidate public runtime has 309 files matching current source byte-for-byte, no private runtime entries and retained MIT/NOTICE. Windows ZIP has 2407 entries, passes CRC validation and includes desktop EXE, Python and complete Cloak; its nested runtime digest matches.
- Fresh isolated installation passed archive prepare/apply, native start, health version 1.6.1, synthetic admin login, release metadata, empty Vids accounts, OpenAPI and rebuilt Studio asset. The native backend stopped normally; the isolated database contains zero accounts. No generation or captcha request was issued. The active checkout service and existing 1.6.0 artifacts were not replaced.
- Required offline checks and the candidate setup installation now pass. Clean-source canonical assets and the normal source commit/push are recorded separately below; local candidates are not published releases.
- The first current-version aggregate run reached 1967 tests but failed one cancellation-test setup deadline (1 second) and its secondary locked-temp-DB cleanup. Preserved the failed log; the test now uses a bounded 10-second wait and always cancels/awaits its task before temporary-directory cleanup, retaining all 499/attempt/rule/lease assertions. No production source was changed for this test failure. Affected regression and the final required run are recorded separately.

## Current acceptance

- Frontend: 212 tests passed, zero failures; production assets rebuilt before packaging.
- Required offline gate after the test-only correction: 1967 Python tests passed (1 skip), Ruff/import/format/generated-contract checks passed, JavaScript contract checks passed, and first-start/cold-restart package smokes passed with zero guarded network attempts. The affected cancellation module separately passed all 22 tests.
- Candidate setup EXE compiled with Inno Setup 6.7.3 and installed successfully into a separate empty directory. Actual HTTP checks passed for health/version 1.6.1, remote_browser captcha mode, synthetic admin login, release metadata, empty Vids accounts and rebuilt Studio assets. All 309 installed public runtime files match the candidate archive. Native shutdown passed and the isolated database contains zero accounts.
- No real login, generation or captcha was requested. Private command logs, the original failed run and successful rerun remain under the ignored evidence directory; no active-checkout service or previous release asset was replaced.