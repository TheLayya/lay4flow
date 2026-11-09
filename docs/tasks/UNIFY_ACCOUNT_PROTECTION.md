# UNIFY-ACCOUNT-PROTECTION

- State: done
- Owner: Codex
- Goal: One account protection setting/engine; remove the duplicate basic-page summary and obsolete automatic-disable path.
- Findings: selected health card is a read-only projection of accountErrorPolicy; controls under rules are the only current editor. Old TokenManager.record_error and /api/admin/config still used the retired error_ban_threshold.
- Changes: keep controls only in rules; route old API/entrypoint to AccountErrorPolicy; historical database column remains inert for compatibility; prevent double failure accounting if logging fails after recording.
- Preserve: current editable rules, fixed proxies, manual account enable state, no repeated generation on unknown/accepted result, existing user changes.
- Gates: focused single-count/shared-threshold tests, frontend tests/e2e, required full check, rebuild assets, local healthy deployment. Synthetic services only.

- Verified: policy16 tests (legacy entry respects new threshold, alias writes/read same config), generation orchestration9 (post-failure logging exception counts once), frontend168, settings overview9, rulesUI5, production frontend build, package smoke and live read-only alias equality passed.
- Full check1655 tests: same baseline2 failures/2 errors/1skip. Local image unified-protection-20261109 healthy, legacy/policy readback threshold both3.

- done: duplicated summary removed from local settings, old execution threshold no longer used, single failure accounting verified, current service health200. No live generation was performed.
