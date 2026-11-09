# Vids2API source comparison

Reviewed 2026-11-09. Reference: https://github.com/TheSmallHanCat/vids2api/tree/5ae0ef5a0b7bc845ca91d3e5ccd6332789983293 (commit2026-11-08). Read-only public source checkout under ignored tmp; no external app or runtime started, no account/provider calls or production changes.

## Existing common functionality

Both clients use editor bootstrap flags, scoped Google Cookies, SAPISIDHASH and X-Server-Token for HTTP genai/quota operations. Image generation/editing, video modes, speech, encrypted sessions, per-account serialization, receipt idempotency and download-only recovery are already present locally. AST comparisons found document.py, image_edit.py and video_input.py identical; client.py34/45 common functions identical and line similarity95.3%. This describes source similarity without asserting repository ancestry.

## Useful deltas

| Priority | Reference source | Local gap / proposed use |
| --- | --- | --- |
| P1 | client.py:253 video_feature | Adds feature374 fallback when flix-evg1p2ui is set without legacy flix-evga. Local falls to104; a synthetic pure-function comparison reproduces this exact difference. Verify against current captured editor flags before adopting. |
| P1 | client.py:815 _send_retry;1436 _download_media | Bounded retries for bootstrap/quota/settings/poll/download, never paid generation POST; video download follows up to5 validated HTTPS redirects. Local video download requires direct200 and does not follow redirects. Preserve hostname/cookie/header restrictions and cancellation. |
| P1 | keepalive.py; main.py warm_accounts | Dedicated idle Vids probing/checkpoint/recovery, retry failed startup registration, session startup in background. Local Flow Profile maintenance can trigger Vids repair and generation preflight repairs sessions, but no dedicated periodic Vids keepalive; local lifespan awaits Vids startup up to60s. |
| P1/P2 | load_balancer.py:59; account_manager.py:773; core/database.py:570 | Quota-weighted or polling selector skips cached zero balances. Durable receipt claims and once-only settlement supply counters/local quota/failure protection. Local Vids pool rotates available sessions then checks only selected account quota; Flow scheduling/error-rule settings do not automatically govern this pool. Use provider-specific health/quotas and preserve receipt owner. |
| P2 | generation_logger.py; api/routes.py:175 | Vids generation updates shared request logs and settles completed receipt. Local Vids has durable receipt routes but does not write the same request_logs path as Flow. Adapt into existing logs without double-counting replay, polling or download recovery. |
| P2 | gateway.py:342 get; session_store.py:24 session_io | Validate corrupt/invalid receipt structures, perform blocking persistence off event loop while cancellation waits for storage completion. Useful robustness improvements; our file stores and account locks must remain intact. |
| Separate acceptance | client.py:356; gateway.py:60 | Reference accepts3-15s; local3-10s. Documentation/code claim live15s support, but no independent live acceptance performed here. Update schemas/model capabilities/UI/tests together only after approval/verification. |

## Do not copy wholesale

- Reference transport.py yields VIDS2API_HOST_PROXY instead of the account proxy when set. Local cloak_proxy.transport_proxy builds a chain preserving account final egress. Replacing it would violate fixed bindings.
- Reference automatic login uses patchright/password/TOTP and optional YesCaptcha. Our approved login/Profile lifecycle and remote_browser-only captcha policy remain authoritative; neither browser lifecycle nor solver setup should be replaced incidentally.
- Keep true Vids identity verification. A pending/unavailable identity cannot be relabeled ready; only validated restored sessions should publish ready.
- Keep paid submission and receipt affinity semantics. Query retries/idle maintenance must not become import repeated cold reopen or blind paid generation retry.
- Reference is MIT; retain notices for any adopted code. Its reviewed tree contains no automated tests directory; README claims do not substitute for this project's mocked regressions and separate live acceptance.

## Suggested first slice

Take video flag fallback, validated video redirects, free-read bounded retries and explicit Vids error diagnostics first. Then add Vids idle maintenance, quota-aware admission and once-only receipt settlement/logs. Keep15s duration and automated credential login separate.

Evidence: tmp/vids-reference-flag-comparison.json. Source-only comparison and isolated pure-function reproduction; no live success claim. This assessment did not modify application files or deploy anything.

Implementation follow-up: [VIDS_REFERENCE_INCREMENTAL](tasks/VIDS_REFERENCE_INCREMENTAL.md) covers feature fallback, bounded free reads, validated video redirects and fresh-knownzero admission. [VIDS_MAINTENANCE_PROTECTION](tasks/VIDS_MAINTENANCE_PROTECTION.md) tracks the next slice: bound-session keepalive, durable once-only settlement/logs and independent configurable Vids protection. Fifteen-second video remains a separate acceptance item.
