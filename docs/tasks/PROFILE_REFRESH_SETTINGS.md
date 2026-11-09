# PROFILE-REFRESH-SETTINGS

- State: done
- Owner: Codex
- Goal: Profile maintenance controls accurately describe current Google Cookie/Profile flow, without obsolete global ST/AT claims or ineffective global interval.
- Findings: Cloak path returns at=None/at_expires=None and never uses legacy ST exchange; global protocol-refresh enabled gates only old non-Cloak protocol accounts; global interval is not scheduler input. Each account's refresh_interval_minutes is authoritative (new Profile defaults30).
- Scope: remove misleading global controls from current UI; preserve legacy API/backend compatibility with explicit scope; recompute Profile schedule on interval/auto-refresh edits and protect against stale in-flight overwrites.
- Preserve: fixed proxies, identity checks, normal Profile close/cache, per-account settings, no import cold-reopen/automatic retries; no live provider calls for acceptance.
- Acceptance: no ST/AT global switch on Profile UI; one per-account maintenance interval; shorten/lengthen/on/off schedules update immediately; edits cannot be overwritten by an earlier refresh result; tests and healthy deployment.

- Verified: interval9, tokenrefresh57, directlogin24, profiletransfer39, runtimeAPI5, frontend169, settingsUI10/accounteditor6 scenes. Production assets rebuilt; no ST global controls/API calls in modern UI. Full check1663 same baseline2 failures2errors1skip.
- Local image profile-refresh-20261109 deployed; legacyAPI includes explicit legacy_protocol_accounts scope and per-account Profile maintenance metadata. No live credentials changed or upstream generation called.

- Independent review passed. Disabled new and reimport Profiles save both maintenance due fields asNone; reimport5 tests passed. Final source image profile-refresh-final-20261109. Global120 saved legacy field remains only compatibility, not used or advertised as Profile interval. No production account settings were changed.
