# lay4flow-remote

- This is an independent source checkout. Do not read or modify another project runtime.
- Captcha generation uses the remote headed browser service exclusively (`remote_browser`).
- Cloak is retained for account login/Profile persistence, not as a local captcha solver.
- Keep existing account identity checks, fixed proxy bindings and profile close/cache semantics.
- Private configs, credentials, databases, profiles, generated media and diagnostics stay out of Git and release packages.
- Keep MIT copyright/license notices. Product branding is lay4flow-remote.
- Run `python scripts/check.py` and `npm --prefix frontend test`; rebuild frontend assets before packaging.
- Use synthetic credentials and mocked services for checks. Real network acceptance is recorded separately.
