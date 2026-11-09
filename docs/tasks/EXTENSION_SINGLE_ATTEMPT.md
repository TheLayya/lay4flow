# EXTENSION-SINGLE-ATTEMPT

- Owner: Codex
- State: done
- Goal: Extension login follows the same single-attempt admission behavior as JSON imports; no automatic resubmission after a failure/timeout and no try-stale-then-repair validation loop.
- Scope: extension background submit state, retained Profile repair, synthetic tests, local Docker update.
- Constraints: preserve fixed proxy/identity checks, encrypted durable receipt and owner isolation, normal Profile closure, source fingerprint-browser login state. Explicit manual retry remains available.
- Acceptance: timeout/transport/busy/server failures terminate submission with actionable status; accepted jobs execute once; retained Profile receives current trusted capture before one validation; no cold reopen.
- Verification: extension Chrome API simulation, queue 8 tests, extension login 12 tests, Profile transfer 39 tests, frontend 160 tests and build pass. Full offline check and deployment recorded separately.

- Extension protocol version advanced to 1.8.1, including management minimum version, so stale auto-resubmit clients are explicitly prompted to update.
- Package builder 5 tests passed; frontend rebuilt; new local Docker image built with public allowlist only.

- Independent review passed. Local Docker deployed and /health returns 200; packaged extension version 1.8.1 and Profile validation call count=1 verified inside image.
- Full offline suite ran 1605 tests; same pre-existing 2 Flow HTTP renewal failures and 2 Vids runtime errors remain, 1 skipped. No new affected test failure.
