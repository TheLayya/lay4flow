# WINDOWS-EXTENSION-DOCS-RELEASE

- State: done
- Owner: Codex
- Requirement: verify and build the Windows native package and account login extension, update README with current screenshots, commit and push this checkout's completed changes.
- Profiles: Windows desktop/backend packaging, Chrome/Edge extension, web documentation, Git delivery.
- Release: 1.6.0 application; extension version follows its own manifest/protocol compatibility.
- Scope: preserve completed account/import/rules/logs/Vids/Omni changes; make native browser extension launch, generated assets and package inventory consistent; publish only public source and synthetic screenshots.
- Acceptance: required backend/frontend checks pass; current frontend built; native desktop compiles; Windows runtime archive/ZIP/setup are built and inspected when available; extension source and site packages handshake under mocked services; screenshots inspected and referenced locally; staged changes contain no runtime state/secrets; commit and normal push succeed.
- Boundaries: only this checkout/runtime; remote_browser exclusively for captcha; account identity, fixed proxy and Profile lifecycle retained; no real provider generation for checks; no real account cookies or media in public artifacts; MIT notices retained.
- Inputs: existing dirty checkout and completed feature task records; VERSION/CHANGELOG; public native runtimes; extension manifest/templates.
- Outputs: updated public sources/docs/images, ignored dist artifacts and hashes, validation records under tmp, Git commit and remote branch.
- Risks: shipping stale frontend/extension/desktop; omitting new docs images from package; long-path native installs; runtime state or credentials accidentally staged; dirty-build source provenance.
- Gates: python scripts/check.py; npm.cmd --prefix frontend test; frontend production build; Windows/extension focused and packaging checks; independent source/package review; diff/inventory scan; push verification.
- Authorization: user explicitly requested Windows packaging, extension clarification, illustrated README and push. Public release/tag/upload is outside the requested Git push unless separately needed.

## Verification and delivery

- 2026-11-10, Codex: in_progress -> in_review after source and illustrated docs freeze. App version 1.6.0; source/site extension manifests both 1.8.4.
- Required check: python scripts/check.py exit 0; 1860 Python tests, 1 skipped, all CJS checks and synthetic startup/restart smoke passed. Evidence: tmp/release160-required-check-accepted.txt. Frontend 191 passed and production build passed: tmp/release160-frontend-tests.txt, tmp/release160-frontend-build.txt.
- Windows: actual .NET desktop publish version 1.6.0.0; existing public backend Python3.12.10/runtime inventory verified. Candidate Windows ZIP/EXE built. Extracted native install in a fresh temp directory passed prepare/apply/start/health/model/static/extension checks and graceful shutdown, with zero accounts and no provider generation. Evidence: tmp/windows-native-installed-smoke-fixed.log, tmp/windows-candidate-audit-latest.json. Canonical package will be rebuilt from clean source commit before final delivery.
- Extension: 37 focused Python tests and Chrome API/bridge CJS passed; 6 real Chromium source/site-package detection checks passed, including loopback names and SPA entry. No writes or stored credentials during detection. Full simulated browser capture attempt was not accepted because of browser navigation instability; capture/cleanup remain covered by mocked Chrome APIs and backend tests. Real Google login was not performed.
- Docs: 7 current application screenshots plus extension setup/connection screenshots, synthetic data only; 4 lossless WebP previews. Reproducible script generated fresh sample MP4 with Pillow/FFmpeg and passed with zero writes/page errors. 46 local doc links resolve; package inventory includes every new public image and companion guide. Evidence: tmp/release160-docs-reproduction.txt, tmp/release160-links.py.
- Independent release audit: 21 release/metadata/Windows packaging tests passed; no blocking source/inventory finding. Existing runtime HTTP /docs documents are plain text; illustrated offline guides ship in ZIP and source repository.
- Git candidate: 271 public changed files reviewed/staged; runtime directories, user-supplied credential strings, private configs, databases and diagnostic artifacts excluded. Root will record source commit/push and canonical artifact results in final delivery, without committing ignored build outputs.

- 2026-11-10, Codex: in_review -> verified -> done after clean-source canonical packaging, hash verification and normal push. Source commit df328e18dda4f3ab48df1e919b819ec936e99198 is on origin/main; build-manifest records this exact source and working_tree_dirty=false. This delivery-record update does not change packaged runtime files.
- Canonical local dist/1.6.0 contains runtime archive, Windows ZIP, actual setup EXE, extension ZIP, release notes, SHA256SUMS and build-manifest. All four asset hashes verified. Setup EXE SHA256: 74b3b7a48409e2007cd77376e91bb8165143a5d06330dd8933a3a8883b00f199. Public archive's 304 files match source byte-for-byte; final Windows ZIP audit covers 2404 entries and 16 offline documentation links. Final compile passed. No GitHub Release/tag or asset upload was requested or created.
- Remote Actions status query timed out during TLS handshake; local verification results above are complete, remote CI conclusion is not claimed.
