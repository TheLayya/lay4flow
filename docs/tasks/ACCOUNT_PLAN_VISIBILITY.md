# ACCOUNT-PLAN-VISIBILITY

- State: done
- Owner: Codex
- Requirement: Account table identifies Free, Pro or Ultra directly after import, without opening details.
- Scope: display existing verified Flow user_paygate_tier as a column near account identity; plan filter and accurate unknown/special-tier handling. No account generation, import, binding or tier capability policy change.
- Acceptance: exact NOT_PAID/ONE/TWO labels; missing/unspecified unknown and unsupported values distinct; no inference from credits or untrusted imported hints; Vids-only rows show no Flow plan; filtered rows preserve existing operations and viewport layout.
- Gates: synthetic frontend label/filter tests, browser table/unknown/tier filter checks, required full checks/build, healthy local delivery.

- Frontend evidence:174 tests pass, two new mapping/filter tests, synthetic browser5 checks including actual table labels and Pro filter; existing viewport/keyboard/remark/proxy/log flows pass. Independent read-only audit found no blockers. Import/probe/API retain verified tier, source JSON hints ignored; no backend changes needed.
- Local delivery: image account-plans-20261109 deployed to38001; health200/container healthy, served account plan bundle hash matches rebuilt frontend. Existing tier aggregate read-only verification11 Free/50 Pro/1 Ultra; no account mutations or live upstream probes.
- Required backend gate passed1693 tests (one expected skip), all CJS and guarded offline first-start/cold-restart checks; final source unchanged after verification.
