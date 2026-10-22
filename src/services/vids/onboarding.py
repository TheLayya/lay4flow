"""Explicit, zero-generation Vids onboarding through an existing Flow Profile."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from ..browser.profile_verifier import ProfileVerifier
from ..proxy_urls import normalize_proxy_url
from .auto_onboarding import read_auto_onboarding_state
from .bindings import read_bindings, session_key_file, write_bindings
from .client import VidsClient, VidsProtocolError, parse_flags
from .deletion_markers import clear_deleted, is_deleted, mark_deleted
from .dispatcher import VidsAccount
from .gateway import GatewayError, VidsGateway
from .profile_cookies import _cookie_rows
from .session_store import SessionStore

EDITOR_RE = re.compile(r"^/videos/(?:u/[0-9]+/)?d/[A-Za-z0-9_-]+/edit$")


class OnboardingError(GatewayError):
    pass


def _safe_editor(value):
    address = urlsplit(value or "")
    if (
        address.scheme != "https"
        or address.hostname != "docs.google.com"
        or address.port not in (None, 443)
        or address.username is not None
        or address.password is not None
        or address.fragment
        or not EDITOR_RE.fullmatch(address.path)
    ):
        return None
    query = parse_qs(address.query)
    if set(query) - {"authuser"}:
        return None
    auth = query.get("authuser", ["0"])
    if len(auth) != 1 or not re.fullmatch(r"[0-9]+", auth[0]):
        return None
    return f"https://docs.google.com{address.path}?authuser={auth[0]}", int(auth[0])


class _EditorLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for name, value in attrs if name == "href" and value)


async def _verify_profile_identity(response, expected_email):
    address = urlsplit(response.url)
    if address.hostname in {"accounts.google.com", "google.com", "www.google.com"}:
        raise OnboardingError(409, "login_required", "Google login is required")
    if (
        address.scheme != "https"
        or address.hostname != "docs.google.com"
        or not address.path.startswith("/videos")
    ):
        raise OnboardingError(409, "unexpected_page", "The Vids account page is unavailable")
    if response.status in {401, 403}:
        raise OnboardingError(409, "login_required", "Google login is required")
    if response.status != 200:
        raise OnboardingError(503, "onboarding_failed", "Vids initialization request failed")
    html = await response.text()
    try:
        identity = parse_flags(html).get("docs-hue")
    except ValueError:
        identity = None
    if not isinstance(identity, str) or not identity.strip():
        raise OnboardingError(409, "identity_unavailable", "Vids identity is unavailable")
    if identity.strip().casefold() != expected_email.strip().casefold():
        raise OnboardingError(
            409, "identity_mismatch", "The active Google account does not match Flow"
        )
    return html


async def inspect_profile_page(page, expected_email, *, create_blank=False, editor_url=None):
    """Verify same-Profile HTTP identity; UI labels never determine the account."""
    home = "https://docs.google.com/videos/?authuser=0"
    response = await page.context.request.get(home, timeout=45000)
    html = await _verify_profile_identity(response, expected_email)
    editor = _safe_editor(editor_url)
    if editor is not None:
        response = await page.context.request.get(editor[0], timeout=45000)
        await _verify_profile_identity(response, expected_email)
        if _safe_editor(response.url) != editor:
            raise OnboardingError(
                409, "login_required", "The saved Vids document requires renewed access"
            )
    else:
        links = _EditorLinks()
        links.feed(html)
        editor = next(
            (target for value in links.links if (target := _safe_editor(urljoin(home, value)))),
            None,
        )
    if editor is None and create_blank:
        response = await page.goto("https://vids.new", wait_until="domcontentloaded", timeout=45000)
        if response is None:
            raise OnboardingError(503, "onboarding_failed", "Vids document request failed")
        await _verify_profile_identity(response, expected_email)
        editor = _safe_editor(page.url)
    if editor is None:
        raise OnboardingError(409, "document_required", "An existing Vids document is required")
    return editor


class VidsOnboardingService:
    """Bind one Flow account after an explicit admin action."""

    def __init__(
        self,
        db,
        runtime,
        profile_manager,
        *,
        registry_path,
        secrets_dir,
        runtime_dir,
        get_settings=None,
    ):
        self.db = db
        self.runtime = runtime
        self.profile_manager = profile_manager
        self.registry_path = Path(registry_path)
        self.secrets_dir = Path(secrets_dir)
        self.runtime_dir = Path(runtime_dir)
        self.get_settings = get_settings
        self._lock = asyncio.Lock()
        self.verifier = ProfileVerifier()

    async def delete_account(self, token_id: int):
        """Remove one Vids binding and only its private artifacts, idempotently."""
        if type(token_id) is not int or token_id <= 0:
            raise OnboardingError(400, "invalid_account", "A valid Flow account is required")
        auto = getattr(self, "auto_onboarding", None)
        if auto is not None:
            await auto.cancel_account(token_id)
        async with self._lock:
            rows = read_bindings(self.registry_path)
            targets = [row for row in rows if row.get("token_id") == token_id]
            runtime_row = self.runtime.binding_rows.get(token_id)
            if runtime_row is not None and runtime_row not in targets:
                targets.append(dict(runtime_row))
            await self.runtime.remove_account(token_id)
            if targets:
                write_bindings(
                    self.registry_path,
                    [row for row in rows if row.get("token_id") != token_id],
                )
            marker = self.registry_path.parent / "auto-onboarding" / str(token_id)
            marker.unlink(missing_ok=True)
            mark_deleted(self.registry_path, token_id)
            expected_root = self.runtime_dir.resolve() / str(token_id)
            expected_key = self.secrets_dir.resolve() / f"account{token_id}.key"
            if expected_root.exists():
                if expected_root.resolve() != expected_root or expected_root.is_symlink():
                    raise OnboardingError(
                        409, "invalid_configuration", "Vids account directory is linked"
                    )
                shutil.rmtree(expected_root)
            if expected_key.is_symlink():
                raise OnboardingError(409, "invalid_configuration", "Vids account key is linked")
            expected_key.unlink(missing_ok=True)

    async def candidates(self):
        try:
            bound = {row.get("token_id") for row in read_bindings(self.registry_path)}
            if self.get_settings is not None:
                bound.update(
                    row.get("token_id")
                    for row in self.get_settings().get("accounts", [])
                    if not is_deleted(self.registry_path, row.get("token_id"))
                )
        except Exception:
            raise OnboardingError(
                503, "invalid_configuration", "Vids bindings are unavailable"
            ) from None
        bound.update(self.runtime.dispatcher._accounts)
        bound.update(self.runtime.binding_rows)
        result = []
        for token in await self.db.get_all_tokens():
            if type(token.id) is int and token.id not in bound:
                result.append(
                    {
                        "token_id": token.id,
                        "email": token.email,
                        **read_auto_onboarding_state(self.registry_path, token.id),
                    }
                )
        return result

    async def onboard(self, token_id: int, *, create_blank=False):
        if type(token_id) is not int or token_id <= 0:
            raise OnboardingError(400, "invalid_account", "A valid Flow account is required")
        async with self._lock:
            async with self.runtime._lifecycle_lock:
                if not self.runtime._started:
                    raise OnboardingError(503, "runtime_unavailable", "Vids runtime is unavailable")
                token = await self.db.get_token(token_id)
                if token is None or not getattr(token, "email", "").strip():
                    raise OnboardingError(404, "account_not_found", "Flow account not found")
                if not getattr(token, "is_active", True):
                    raise OnboardingError(409, "account_disabled", "The Flow account is disabled")
                if (
                    token_id in {row.get("token_id") for row in read_bindings(self.registry_path)}
                    or token_id in self.runtime.dispatcher._accounts
                    or token_id in self.runtime.binding_rows
                ):
                    raise OnboardingError(409, "already_bound", "Vids account is already connected")
                proxy = (
                    normalize_proxy_url(getattr(token, "proxy_url", None))
                    or await self.runtime.proxy_manager.get_request_proxy_url()
                )
                candidate = None
                published = False
                try:
                    key_path = self.secrets_dir / f"account{token_id}.key"
                    account_root = (self.runtime_dir / str(token_id)).resolve()
                    if (
                        self.runtime_dir.resolve() not in account_root.parents
                        or account_root == key_path.resolve()
                        or account_root in key_path.resolve().parents
                    ):
                        raise OnboardingError(
                            400,
                            "invalid_configuration",
                            "The session key must be stored separately",
                        )
                    previous = None
                    if key_path.exists():
                        previous = SessionStore(account_root, session_key_file(key_path)).load()
                    spec = await self.profile_manager.spec(token.email, existing_only=True)
                    async with self.profile_manager.session(
                        token.email, proxy_url=proxy, existing_only=True
                    ) as context:
                        environment = await self.verifier.verify_environment(
                            context,
                            str(spec.profile_dir),
                            allow_viewport_change=getattr(
                                self.profile_manager.backend, "options", {}
                            ).get("headless")
                            is False,
                        )
                        if environment != "environment_stable":
                            raise OnboardingError(
                                409,
                                "profile_unavailable",
                                "The existing Profile requires verification",
                            )
                        page = await context.new_page()
                        try:
                            async with asyncio.timeout(60):
                                editor_url, auth_user = await inspect_profile_page(
                                    page,
                                    token.email,
                                    create_blank=create_blank,
                                    editor_url=previous["editor_url"] if previous else None,
                                )
                            cookies = _cookie_rows(await context.cookies())
                        finally:
                            await page.close()
                    identity = await self._identity(editor_url, cookies, auth_user, proxy)
                    if identity.strip().casefold() != token.email.strip().casefold():
                        raise OnboardingError(
                            409, "identity_mismatch", "The Vids account does not match Flow"
                        )
                    identity_hash = hashlib.sha256(identity.encode()).hexdigest()
                    key = session_key_file(key_path, create=True)
                    store = SessionStore(account_root, key)
                    if previous is not None:
                        if (
                            previous["account_hash"] != identity_hash
                            or previous["editor_url"] != editor_url
                        ):
                            raise OnboardingError(
                                409, "account_mismatch", "The saved Vids binding differs"
                            )
                        store.save(
                            editor_url=editor_url,
                            auth_user=auth_user,
                            account_hash=identity_hash,
                            cookies=cookies,
                        )
                    candidate = VidsGateway(
                        None
                        if previous is not None
                        else VidsClient(editor_url, cookies, auth_user=auth_user, proxy=proxy),
                        account_root,
                        session_store=store,
                        resume=previous is not None,
                        proxy=proxy,
                        expected_account_hash=identity_hash,
                        expected_editor_url=editor_url,
                    )
                    await candidate.start()
                    current = await self.db.get_token(token_id)
                    if current is None or not getattr(current, "is_active", True):
                        raise OnboardingError(
                            409, "account_disabled", "The Flow account is disabled"
                        )
                    binding = VidsAccount(token_id, candidate, identity_hash)
                    old_rows = read_bindings(self.registry_path)
                    row = {
                        "token_id": token_id,
                        "identity_hash": identity_hash,
                        "editor_url": editor_url,
                        "runtime_dir": str(account_root),
                        "session_key_file": str(key_path.resolve()),
                    }
                    try:
                        self.runtime.adopt(
                            binding, lambda: write_bindings(self.registry_path, [*old_rows, row])
                        )
                        self.runtime.binding_rows[token_id] = dict(row)
                    except Exception:
                        write_bindings(self.registry_path, old_rows)
                        raise
                    published = True
                    clear_deleted(self.registry_path, token_id)
                    return {"token_id": token_id, "state": "ready", "identity_verified": True}
                except OnboardingError:
                    raise
                except GatewayError:
                    raise
                except Exception:
                    raise OnboardingError(
                        503, "onboarding_failed", "Vids account onboarding failed"
                    ) from None
                finally:
                    if candidate is not None and not published:
                        await self.runtime._finish_cleanup(candidate.close())

    async def reconnect(self, token_id: int):
        """Refresh one bound account's Profile cookies without replaying work.

        This path only replaces the HTTP session. It never reads or submits a
        generation receipt, and it leaves the existing gateway in place when
        the replacement cannot be authenticated.
        """
        if type(token_id) is not int or token_id <= 0:
            raise OnboardingError(400, "invalid_account", "A valid Flow account is required")
        async with self._lock:
            if not self.runtime._started:
                if self.get_settings is None or self.runtime.last_error_code not in {
                    "session_authentication_failed",
                    "session_unavailable",
                    "profile_refresh_failed",
                }:
                    raise OnboardingError(503, "runtime_unavailable", "Vids runtime is unavailable")
                try:
                    settings = self.get_settings()
                    if not isinstance(settings, dict) or not isinstance(
                        settings.get("accounts", []), list
                    ):
                        raise ValueError
                    rows = settings.get("accounts", [])
                    if settings.get("registry_path"):
                        rows = [*rows, *read_bindings(settings["registry_path"])]
                    if not all(isinstance(row, dict) for row in rows):
                        raise ValueError
                except Exception:
                    raise OnboardingError(
                        503, "invalid_configuration", "Vids bindings are unavailable"
                    ) from None
                if token_id not in {row.get("token_id") for row in rows}:
                    raise OnboardingError(
                        404, "account_not_found", "Vids account is not configured"
                    )

                await self.runtime.start(settings)
            async with self.runtime._lifecycle_lock:
                if not self.runtime._started:
                    raise OnboardingError(503, "runtime_unavailable", "Vids runtime is unavailable")
                account = self.runtime.dispatcher._accounts.get(token_id)
                token = await self.db.get_token(token_id)
                if token is None:
                    raise OnboardingError(404, "account_not_found", "Vids account is not connected")
                if account is None:
                    return await self._restore_bound_account(token)
                gateway = account.gateway
                self.runtime.dispatcher._verify_identity(
                    account, self.runtime.dispatcher._roots[token_id], check_session=False
                )
                if not gateway._ready or gateway.lock.locked():
                    raise OnboardingError(
                        429 if gateway.lock.locked() else 503,
                        "busy" if gateway.lock.locked() else "session_unavailable",
                        "Vids account is busy"
                        if gateway.lock.locked()
                        else "Vids session is unavailable",
                    )
                proxy = gateway.client.proxy
                try:
                    cookies = await self._profile_cookies(
                        token.email, gateway.client.editor_url, proxy
                    )
                    if gateway.lock.locked():
                        raise OnboardingError(429, "busy", "Vids account is busy")
                    await gateway.replace_cookies(cookies)
                    self.runtime.account_errors.pop(token_id, None)
                    return {"token_id": token_id, "state": "ready", "identity_verified": True}
                except OnboardingError:
                    raise
                except GatewayError as error:
                    if error.code in {"account_mismatch", "session_persistence_failed"}:
                        raise OnboardingError(
                            409, error.code, "Vids session identity could not be verified"
                        ) from None
                    raise OnboardingError(
                        error.status, error.code, "Vids session repair failed"
                    ) from None
                except VidsProtocolError as error:
                    if error.status in {401, 403}:
                        raise OnboardingError(
                            409, "login_required", "The saved Google session requires renewed login"
                        ) from None
                    raise OnboardingError(
                        503, "session_repair_failed", "Vids session repair failed"
                    ) from None
                except Exception:
                    raise OnboardingError(
                        503, "session_repair_failed", "Vids session repair failed"
                    ) from None

    async def _restore_bound_account(self, token):
        """Reopen only this saved binding while healthy accounts keep their gateways."""
        candidate = None
        published = False
        try:
            row = self.runtime.binding_rows.get(token.id)
            if row is None:
                raise OnboardingError(404, "account_not_found", "Vids account is not configured")
            if not getattr(token, "is_active", True):
                raise OnboardingError(409, "account_disabled", "The Flow account is disabled")
            key = (
                os.environ[row["session_key_env"]]
                if "session_key_env" in row
                else session_key_file(row["session_key_file"])
            )
            store = SessionStore(Path(row["runtime_dir"]), key)
            proxy = normalize_proxy_url(row.get("proxy", getattr(token, "proxy_url", None)))
            if "proxy" not in row and proxy is None:
                proxy = await self.runtime.proxy_manager.get_request_proxy_url()

            async def cookies(editor_url, actual_proxy):
                return await self._profile_cookies(token.email, editor_url, actual_proxy)

            candidate = VidsGateway(
                None,
                Path(row["runtime_dir"]),
                session_store=store,
                resume=True,
                proxy=proxy,
                expected_account_hash=row["identity_hash"],
                expected_editor_url=row["editor_url"],
                cookie_source=cookies,
                refresh_session=True,
            )
            await candidate.start()
            current = await self.db.get_token(token.id)
            if current is None:
                raise OnboardingError(404, "account_not_found", "Flow account not found")
            if not getattr(current, "is_active", True):
                raise OnboardingError(409, "account_disabled", "The Flow account is disabled")
            if current.email.strip().casefold() != token.email.strip().casefold():
                raise OnboardingError(409, "identity_mismatch", "The Flow account identity changed")
            self.runtime.adopt(VidsAccount(token.id, candidate, row["identity_hash"]), lambda: None)
            published = True
            return {"token_id": token.id, "state": "ready", "identity_verified": True}
        except GatewayError as error:
            self.runtime.account_errors[token.id] = error.code
            raise
        except VidsProtocolError as error:
            code = (
                "session_authentication_failed"
                if error.status in {401, 403}
                else "session_repair_failed"
            )
            self.runtime.account_errors[token.id] = code
            raise OnboardingError(503, code, "Vids account session repair failed") from None
        except Exception:
            self.runtime.account_errors[token.id] = "session_repair_failed"
            raise OnboardingError(
                503, "session_repair_failed", "Vids account session repair failed"
            ) from None
        finally:
            if candidate is not None and not published:
                await self.runtime._finish_cleanup(candidate.close())

    async def _profile_cookies(self, email, editor_url, proxy):
        spec = await self.profile_manager.spec(email, existing_only=True)
        async with self.profile_manager.session(
            email, proxy_url=proxy, existing_only=True
        ) as context:
            environment = await self.verifier.verify_environment(
                context,
                str(spec.profile_dir),
                allow_viewport_change=getattr(self.profile_manager.backend, "options", {}).get(
                    "headless"
                )
                is False,
            )
            if environment != "environment_stable":
                raise OnboardingError(
                    409, "profile_unavailable", "The existing Profile requires verification"
                )
            page = await context.new_page()
            try:
                async with asyncio.timeout(60):
                    observed, _auth_user = await inspect_profile_page(
                        page, email, editor_url=editor_url
                    )
                if observed != editor_url:
                    raise OnboardingError(
                        409, "account_mismatch", "The saved Vids document changed"
                    )
                return _cookie_rows(await context.cookies())
            finally:
                await page.close()

    async def _identity(self, editor_url, cookies, auth_user, proxy):
        client = VidsClient(editor_url, cookies, auth_user=auth_user, proxy=proxy)
        try:
            await client.bootstrap()
            identity = client.flags.get("docs-hue")
            if not isinstance(identity, str) or not identity:
                raise OnboardingError(409, "identity_unavailable", "Vids identity is unavailable")
            return identity
        except VidsProtocolError as error:
            if error.status in {401, 403}:
                raise OnboardingError(
                    409, "login_required", "The saved Google session requires renewed login"
                ) from None
            raise OnboardingError(
                503, "onboarding_failed", "Vids account onboarding failed"
            ) from None
        finally:
            await client.session.close()
