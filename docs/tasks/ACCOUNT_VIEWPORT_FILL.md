# ACCOUNT-VIEWPORT-FILL

- State: done
- Owner: Codex
- Requirement: Accounts table uses available viewport height instead of a fixed620px limit; tall windows display more account rows, pagination stays at the bottom.
- Scope: account page layout/default page size, desktop flex allocation and synthetic viewport checks. Existing account/proxy/login/import operations remain unchanged.
- Acceptance: table grows/shrinks with viewport height, desktop pagination stays visible near main-content bottom, no document overflow, alerts/selection/expanded rows retain access, narrow table remains horizontally scrollable.
- Gates: synthetic browser desktop/tall/short/narrow geometry, frontend tests, required offline check, rebuilt assets, healthy local Docker delivery.

- Evidence: synthetic viewport700/1500 yields table393/1169px and7/25 fully visible rows; pagination21px above main bottom and document matches viewport. Compact browser flows and account editor pass; default50 renders all31 fixture records before pagination selection. Narrow horizontal table/dark/keyboard/inline updates preserved.
- Required checks: python scripts/check.py1693 tests passed (one expected skip), CJS and guarded offline first-start/cold-restart pass; frontend172/172. Independent read-only review found no blocking issue. Final frontend rebuilt; image account-viewport-20261109.
- Delivery: final image deployed to existing local Compose app38001, HTTP health200/container healthy; served frontend entry matches rebuilt hash. Previous image override retained locally. Production account data/settings untouched.
