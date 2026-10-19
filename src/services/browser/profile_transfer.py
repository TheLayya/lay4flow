"""Move a validated Cloak login through native, scoped browser-state JSON."""

import asyncio
import hashlib
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable
from urllib.parse import urlsplit

from ...core.models import Token
from ..flow.google_cookies import GoogleCookieJar
from ..proxy_manager import ProxyManager
from .environment import EnvironmentSnapshot
from .profiles import ProfileNotReadyError
from .token_account_probe import TokenManagerAccountProbe

FORMAT = "lay4flow-cloak-session"
VERSION = 1
HOSTS = {"google.com", "www.google.com", "accounts.google.com", "flow.google.com", "labs.google"}
SETTINGS = (
    "remark",
    "is_active",
    "auto_refresh_enabled",
    "image_enabled",
    "video_enabled",
    "image_concurrency",
    "video_concurrency",
)
LAUNCH_KEYS = {
    "stealth_args",
    "user_agent",
    "viewport",
    "locale",
    "timezone",
    "color_scheme",
    "args",
}
FAILURE_REASONS = {
    "transient_error",
    "login_required",
    "session_invalid",
    "identity_mismatch",
    "invalid_cookies",
    "environment_changed",
    "environment_timeout",
    "environment_probe_error",
    "account_probe_error",
    "credits_invalid",
    "runtime_mismatch",
    "environment_mismatch",
    "native_state_unavailable",
    "renewal_unverified",
    "seed_conflict",
}
VERIFICATION_MESSAGES = {
    "transient_error": "访问 Google 的网络请求超时或服务暂时异常，请检查该账号的代理后重试",
    "login_required": "Google 登录态已失效，请在账号浏览器中重新登录后导出",
    "session_invalid": "登录态续签未通过，请在源服务器刷新凭据后重新导出",
    "identity_mismatch": "浏览器中的 Google 账号与迁移账号不一致",
    "invalid_cookies": "登录凭据中的 Google 账号与迁移账号不一致",
    "environment_changed": "目标浏览器身份环境发生变化，请检查源与目标的浏览器配置",
    "environment_timeout": "读取浏览器身份环境超时，请重试",
    "environment_probe_error": "读取浏览器身份环境失败，请检查浏览器服务",
    "account_probe_error": "账号接口校验失败，请检查代理网络后重试",
    "credits_invalid": "账号额度接口返回异常，请稍后重试",
}
TRANSFER_STAGES = {
    "open_profile": "启动目标浏览器",
    "restore_state": "恢复登录态",
    "oauth_renewal": "Google 登录态续签",
    "cold_check_1": "首次重开校验",
    "cold_check_2": "再次重开校验",
    "save_account": "保存账号",
    "prepare_workspace": "准备账号工作区",
    "finalize": "完成账号配置",
}


class ProfileTransferError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def transfer_error_detail(error: Exception) -> tuple[int, dict[str, Any]]:
    """Keep synchronous requests and background status equally useful and private."""
    stage = getattr(error, "transfer_stage", None)
    elapsed = getattr(error, "transfer_elapsed_seconds", None)
    diagnostics = {
        **({"stage": stage} if stage in TRANSFER_STAGES else {}),
        **({"elapsed_seconds": elapsed} if isinstance(elapsed, (int, float)) else {}),
    }
    token_id = getattr(error, "transfer_token_id", None)
    if isinstance(token_id, int) and not isinstance(token_id, bool) and 0 < token_id < 2**63:
        return 502, {
            "error_code": "profile_transfer_failed",
            "message": "账号已保存但后续配置未完成，请在账号列表检查并启用",
            "token_id": token_id,
            "requires_account_action": True,
            **diagnostics,
        }
    if isinstance(error, ProfileTransferError):
        return error.status, {"error_code": error.code, "message": str(error), **diagnostics}
    if isinstance(error, ProfileNotReadyError):
        reason = error.reason if error.reason in VERIFICATION_MESSAGES else "verification_failed"
        message = VERIFICATION_MESSAGES.get(reason, "Profile 校验未通过，请检查登录态与代理网络")
        if stage in TRANSFER_STAGES:
            message = f"{TRANSFER_STAGES[stage]}失败：{message}"
        return 422, {
            "error_code": "profile_verification_failed",
            "reason": reason,
            "message": message + "；目标资料已保留",
            **diagnostics,
        }
    return 502, {
        "error_code": "profile_transfer_failed",
        "message": "Profile 迁移未完成，现有账号与资料已保留",
        **diagnostics,
    }


def _runtime() -> dict[str, str]:
    try:
        return {name: version(name) for name in ("cloakbrowser", "playwright")}
    except PackageNotFoundError:
        raise ProfileTransferError(
            "runtime_unavailable", "Cloak Profile 运行环境尚未就绪", 503
        ) from None


def _origin(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _scoped_state(state: Any, *, exporting: bool = False) -> dict[str, Any]:
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("cookies"), list)
        or not isinstance(state.get("origins", []), list)
    ):
        raise ProfileTransferError("invalid_state", "登录态 JSON 结构无效")
    cookies, origins = [], []
    for cookie in state["cookies"]:
        trusted = isinstance(cookie, dict) and str(cookie.get("domain", "")).lstrip(".") in HOSTS
        if not trusted:
            if exporting:
                continue
            raise ProfileTransferError("invalid_scope", "登录态包含范围外的 Cookie")
        if (
            not isinstance(cookie.get("name"), str)
            or not cookie["name"]
            or not isinstance(cookie.get("value"), str)
            or not isinstance(cookie.get("path"), str)
            or not cookie["path"].startswith("/")
            or isinstance(cookie.get("expires"), bool)
            or not isinstance(cookie.get("expires"), (int, float))
            or not math.isfinite(cookie["expires"])
            or any(not isinstance(cookie.get(field), bool) for field in ("secure", "httpOnly"))
            or cookie.get("sameSite") not in ("Strict", "Lax", "None")
            or ("partitionKey" in cookie and not _origin(cookie["partitionKey"]))
        ):
            raise ProfileTransferError("invalid_cookie", "Cookie 属性无效，请重新导出")
        cookies.append(dict(cookie))
    for entry in state.get("origins", []):
        if not isinstance(entry, dict) or not _origin(entry.get("origin")):
            if exporting:
                continue
            raise ProfileTransferError("invalid_scope", "登录态包含范围外的站点存储")
        origins.append(dict(entry))
    if not cookies or len(cookies) > 1000 or len(origins) > 50:
        raise ProfileTransferError("invalid_state", "登录态数量超出范围或 Cookie 为空")
    return {"cookies": cookies, "origins": origins}


class ProfileTransfer:
    def __init__(self, token_manager: Any):
        self.tokens = token_manager
        self._import_lock = asyncio.Lock()

    @property
    def refresher(self):
        value = getattr(self.tokens, "profile_refresher", None)
        if value is None:
            raise ProfileTransferError("profile_unavailable", "请先启用 Cloak Profile 服务", 503)
        return value

    def _launch_options(self) -> dict[str, Any]:
        options = dict(getattr(self.refresher.manager.backend, "options", {}))
        unknown = set(options) - LAUNCH_KEYS - {"headless", "proxy", "env"}
        if unknown:
            raise ProfileTransferError("launch_options_unsupported", "当前启动参数需先适配迁移格式")
        identity = {key: value for key, value in options.items() if key in LAUNCH_KEYS}
        for arg in identity.get("args", []):
            if not isinstance(arg, str) or not (
                arg in {"--disable-breakpad", "--disable-crash-reporter"}
                or arg.startswith(("--fingerprint-", "--lang=", "--window-size="))
            ):
                raise ProfileTransferError(
                    "launch_options_unsupported", "当前启动参数需先适配迁移格式"
                )
        return identity

    @asynccontextmanager
    async def _export_context(self, token):
        provider = self.refresher.active_context_provider
        if provider is not None:
            async with provider(token) as context:
                if context is not None:
                    yield context
                    return
        async with self.refresher.manager.session(
            token.email, proxy_url=await self.refresher._proxy_url(token), existing_only=True
        ) as context:
            yield context

    async def export(self, token_id: int) -> dict[str, Any]:
        token = await self.tokens.db.get_token(token_id)
        if token is None:
            raise ProfileTransferError("account_missing", "账号不存在", 404)
        if token.browser_profile_source != "cloak":
            raise ProfileTransferError("profile_required", "请选择已登录的 Cloak Profile 账号")
        manager = self.refresher.manager
        spec = await manager.spec(token.email, existing_only=True)
        launch = self._launch_options()
        async with self._export_context(token) as context:
            if not callable(getattr(context, "storage_state", None)):
                raise ProfileTransferError(
                    "native_state_unavailable", "请升级 Playwright 后再迁移", 503
                )
            result = await self.refresher.verifier.verify_environment(
                context, str(spec.profile_dir), allow_viewport_change=True
            )
            if result != "environment_stable":
                raise ProfileNotReadyError(token.email, result)
            probe = TokenManagerAccountProbe(self.tokens, token)
            probe.set_context(context)
            account = await self.refresher.verifier.verify_account(token.email, probe)
            if account.reason != "account_ready":
                raise ProfileNotReadyError(token.email, account.reason)
            state = _scoped_state(await context.storage_state(indexed_db=True), exporting=True)
            browser_version = str(getattr(getattr(context, "browser", None), "version", ""))
        return {
            "format": FORMAT,
            "version": VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "account": {"email": token.email, **{key: getattr(token, key) for key in SETTINGS}},
            "browser": {
                "fingerprint_seed": spec.fingerprint_seed,
                "launch_options": launch,
                "environment": json.loads(
                    (spec.profile_dir / "environment_snapshot.json").read_text(encoding="utf-8")
                ),
                "runtime": {**_runtime(), "chromium": browser_version},
            },
            "storage_state": state,
        }

    def _validate(self, bundle: Any):
        try:
            size = len(json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError):
            raise ProfileTransferError("invalid_bundle", "迁移包 JSON 无效") from None
        if size > 8 * 1024 * 1024:
            raise ProfileTransferError("bundle_too_large", "迁移包超过 8 MiB")
        if (
            not isinstance(bundle, dict)
            or bundle.get("format") != FORMAT
            or isinstance(bundle.get("version"), bool)
            or bundle.get("version") != VERSION
        ):
            raise ProfileTransferError("unsupported_format", "请选择版本 1 的 Cloak 登录态迁移包")
        account, browser = bundle.get("account"), bundle.get("browser")
        if not isinstance(account, dict) or not isinstance(browser, dict):
            raise ProfileTransferError("invalid_bundle", "迁移包缺少账号或浏览器信息")
        email = account.get("email")
        if not isinstance(email, str) or not email.strip() or len(email) > 320 or "@" not in email:
            raise ProfileTransferError("invalid_account", "迁移包账号无效")
        for key in ("is_active", "auto_refresh_enabled", "image_enabled", "video_enabled"):
            if not isinstance(account.get(key), bool):
                raise ProfileTransferError("invalid_settings", "迁移包账号开关无效")
        for key in ("image_concurrency", "video_concurrency"):
            value = account.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not -1 <= value <= 1000:
                raise ProfileTransferError("invalid_settings", "迁移包并发设置无效")
        if account.get("remark") is not None and not isinstance(account["remark"], str):
            raise ProfileTransferError("invalid_settings", "迁移包备注无效")
        manager = self.refresher.manager
        seed = manager._validate_seed(browser.get("fingerprint_seed"))
        if seed is None:
            raise ProfileTransferError("invalid_seed", "迁移包指纹 seed 无效")
        if browser.get("launch_options") != self._launch_options():
            raise ProfileTransferError("launch_mismatch", "源与目标的浏览器启动身份参数不同")
        runtime = browser.get("runtime")
        if (
            not isinstance(runtime, dict)
            or not isinstance(runtime.get("chromium"), str)
            or not runtime.get("chromium")
            or any(runtime.get(key) != value for key, value in _runtime().items())
        ):
            raise ProfileTransferError(
                "runtime_mismatch", "请使用与源服务器相同版本的 Cloak 和 Playwright"
            )
        try:
            environment = EnvironmentSnapshot.from_json(json.dumps(browser["environment"]))
        except (KeyError, TypeError, ValueError):
            raise ProfileTransferError("invalid_environment", "迁移包浏览器环境无效") from None
        return account, browser, seed, environment, _scoped_state(bundle.get("storage_state"))

    async def _check_context(self, context, token, environment, browser):
        if str(getattr(getattr(context, "browser", None), "version", "")) != browser["runtime"].get(
            "chromium", ""
        ):
            raise ProfileTransferError("runtime_mismatch", "源与目标的 Chromium 版本不同")
        actual = await self.refresher.verifier.environment_snapshot(context)
        # Headed Chromium restores window bounds from its local Profile. A
        # freshly reconstructed Profile has no such presentation preferences;
        # compare the same identity fields as the existing viewer verifier.
        identity_fields = ("user_agent", "language", "languages", "timezone", "platform")
        if any(getattr(actual, field) != getattr(environment, field) for field in identity_fields):
            raise ProfileTransferError("environment_mismatch", "目标浏览器环境与源登录环境不同")
        spec = await self.refresher.manager.spec(token.email)
        reason = await self.refresher.verifier.verify_environment(
            context, str(spec.profile_dir), allow_viewport_change=True
        )
        if reason != "environment_stable":
            raise ProfileNotReadyError(token.email, reason)

    async def _cold_check(self, token, environment, browser):
        async with self.refresher.manager.session(
            token.email, proxy_url=token.proxy_url
        ) as context:
            await self._check_context(context, token, environment, browser)
            probe = TokenManagerAccountProbe(self.tokens, token)
            probe.set_context(context)
            result = await self.refresher.verifier.verify_account(token.email, probe)
            if result.reason != "account_ready" or not probe.validated_session:
                raise ProfileNotReadyError(token.email, result.reason)
            rows = []
            for cookie in await context.cookies():
                row = dict(cookie)
                row["hostOnly"] = not str(row.get("domain", "")).startswith(".")
                if row.get("expires", -1) > 0:
                    row["expirationDate"] = row["expires"]
                rows.append(row)
            return {
                **probe.validated_session,
                "google_cookies": json.dumps(GoogleCookieJar(rows).export()),
            }

    @staticmethod
    def _transfer_binding(email, seed, browser, proxy_url, proxy_pool_id):
        # Store identity and fixed-proxy equality evidence, never proxy credentials.
        identity = {
            "email": email,
            "seed": seed,
            "browser": browser,
            "proxy_url": proxy_url,
            "proxy_pool_id": proxy_pool_id,
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _resume_transfer(self, profile_dir, normalized, seed, binding, environment, proxy_url):
        try:
            metadata = json.loads((profile_dir / "metadata.json").read_text(encoding="utf-8"))
            if (
                not isinstance(metadata, dict)
                or metadata.get("account_key") != normalized
                or metadata.get("fingerprint_seed") != seed
                or metadata.get("state") != "blocked"
                or metadata.get("last_error") != "transfer_failed"
            ):
                raise ValueError
            transfer_path = profile_dir / "profile_transfer.json"
            if not transfer_path.exists():
                # Legacy failures have no sidecar. The existing launch hash
                # proves their exact identity options and fixed proxy before
                # we resume their persistent state without injecting it again.
                manager = self.refresher.manager
                manager._existing_spec(normalized)
                existing_environment = EnvironmentSnapshot.from_json(
                    (profile_dir / "environment_snapshot.json").read_text(encoding="utf-8")
                )
                identity_fields = ("user_agent", "language", "languages", "timezone", "platform")
                if any(
                    getattr(existing_environment, key) != getattr(environment, key)
                    for key in identity_fields
                ):
                    raise ValueError
                backend = manager.backend
                backend._check_launch_settings(
                    profile_dir, backend._launch_options(profile_dir, proxy_url)
                )
                return {"binding_sha256": binding, "state_imported": True, "legacy_resumed": True}
            transfer = json.loads(transfer_path.read_text(encoding="utf-8"))
            if (
                not isinstance(transfer, dict)
                or transfer.get("binding_sha256") != binding
                or not isinstance(transfer.get("state_imported"), bool)
                or transfer.get("transfer_failure_reason")
                not in FAILURE_REASONS | {"transfer_error", "cancelled"}
            ):
                raise ValueError
            return transfer
        except (OSError, TypeError, ValueError, AttributeError):
            raise ProfileTransferError(
                "profile_exists",
                "目标已有同名 Profile，原资料已保留；请检查原导入配置或通过账号浏览器继续登录",
                409,
            ) from None

    async def import_bundle(
        self,
        bundle: Any,
        proxy_url: str | None = None,
        *,
        proxy_pool_id: int | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        stage = "queued"

        def progress(value):
            nonlocal stage
            stage = value
            if on_stage is not None:
                on_stage(value)

        account, browser, seed, environment, state = self._validate(bundle)
        async with self._import_lock:
            email = account["email"].strip()
            if any(
                token.email.strip().casefold() == email.casefold()
                for token in await self.tokens.db.get_all_tokens()
            ):
                raise ProfileTransferError(
                    "account_exists", "目标服务器已有同一账号，现有登录态已保留", 409
                )
            manager = self.refresher.manager
            normalized = manager._normalize_account_key(email)
            profile_dir = manager.root_dir / normalized
            try:
                proxy_pool_id, proxy_url = await ProxyManager(self.tokens.db).select_account_proxy(
                    proxy_pool_id, proxy_url
                )
            except ValueError:
                raise ProfileTransferError(
                    "proxy_unavailable", "请选择可用的代理池出口，或先在代理池添加出口"
                ) from None
            token = Token(
                st="",
                email=email,
                proxy_url=proxy_url or "",
                proxy_pool_id=proxy_pool_id,
                browser_profile_source="cloak",
            )
            token.proxy_url = await self.refresher._proxy_url(token)
            binding = self._transfer_binding(email, seed, browser, token.proxy_url, proxy_pool_id)
            resumed = profile_dir.exists()
            if resumed:
                if profile_dir.resolve().parent != manager.root_dir.resolve():
                    raise ProfileTransferError(
                        "profile_exists", "目标 Profile 路径异常，原资料已保留", 409
                    )
                transfer = self._resume_transfer(
                    profile_dir, normalized, seed, binding, environment, token.proxy_url
                )
            else:
                profile_dir.parent.mkdir(parents=True, exist_ok=True)
                try:
                    profile_dir.mkdir()
                except FileExistsError:
                    raise ProfileTransferError(
                        "profile_exists", "目标已有同名 Profile，现有资料已保留", 409
                    ) from None
                transfer = {"binding_sha256": binding, "state_imported": False}
            transfer_path = profile_dir / "profile_transfer.json"
            started = time.monotonic()
            progress("open_profile")
            token_id = None
            try:
                try:
                    manager._claim_seed(normalized, seed)
                except ValueError:
                    if not resumed:
                        profile_dir.rmdir()
                    raise ProfileTransferError(
                        "seed_conflict", "目标指纹 seed 已被其他账号占用", 409
                    ) from None
                if not resumed:
                    manager._write_metadata_file(
                        profile_dir / "metadata.json",
                        {
                            "account_key": normalized,
                            "profile_dir": str(profile_dir),
                            "state": "new",
                            "fingerprint_seed": seed,
                        },
                    )
                manager._write_metadata_file(transfer_path, transfer)
                async with manager.session(email, proxy_url=token.proxy_url) as context:
                    await self._check_context(context, token, environment, browser)
                    # A valid exported Labs ST is useful compatibility material,
                    # but admission must prove the portable Google login can
                    # mint a fresh Labs session without replaying that ST.
                    if not transfer.get("state_imported"):
                        progress("restore_state")
                        if not callable(getattr(context, "set_storage_state", None)):
                            raise ProfileTransferError(
                                "native_state_unavailable", "请升级 Playwright 后再迁移", 503
                            )
                        state["cookies"] = [
                            row
                            for row in state["cookies"]
                            if not (
                                str(row["domain"]).lstrip(".") == "labs.google"
                                and (
                                    row["name"] == "__Secure-next-auth.session-token"
                                    or row["name"].startswith("__Secure-next-auth.session-token.")
                                )
                            )
                        ]
                        await context.set_storage_state(state)
                        transfer["state_imported"] = True
                        manager._write_metadata_file(transfer_path, transfer)
                    progress("oauth_renewal")
                    renewed = await self.refresher.refresh_in_context(token, context, force=True)
                    if not renewed.get("_validated_session"):
                        raise ProfileTransferError(
                            "renewal_unverified", "迁移登录态续签未通过校验", 422
                        )
                # Native state is injected once. Both reopen checks read only
                # the persistent target Profile after normal close and slimming.
                progress("cold_check_1")
                validated = await self._cold_check(token, environment, browser)
                progress("cold_check_2")
                validated = await self._cold_check(token, environment, browser)
                progress("save_account")
                now = datetime.now(timezone.utc)
                token = Token(
                    **validated,
                    email=email,
                    name=email.split("@", 1)[0],
                    remark=account.get("remark"),
                    is_active=False,
                    image_enabled=account["image_enabled"],
                    video_enabled=account["video_enabled"],
                    image_concurrency=account["image_concurrency"],
                    video_concurrency=account["video_concurrency"],
                    proxy_url=token.proxy_url,
                    proxy_pool_id=proxy_pool_id,
                    login_account=email,
                    login_password="",
                    protocol_mode="session",
                    auto_refresh_enabled=False,
                    browser_profile_source="cloak",
                    refresh_status="ready",
                    last_refresh_at=now,
                    last_st_refresh_at=now,
                    last_st_refresh_result="success",
                )
                token_id = await self.tokens.db.add_token(token)
                token.id = token_id
                await self.tokens.db.update_token(
                    token_id,
                    browser_profile_status="ready",
                    browser_profile_verified_at=now,
                    browser_profile_epoch=uuid.uuid4().hex,
                    browser_profile_error="",
                    browser_profile_failures=0,
                    ban_reason="manual",
                    banned_at=now,
                )
                await manager.mark_verified(email)
                if account["is_active"]:
                    progress("prepare_workspace")
                    await self.tokens._flow_call_for_token(
                        token, lambda: self.tokens.ensure_project_exists(token_id)
                    )
                    await self.tokens.enable_token(token_id)
                progress("finalize")
                await self.tokens.db.update_token(
                    token_id,
                    auto_refresh_enabled=account["auto_refresh_enabled"],
                    next_refresh_at=max(now, validated["at_expires"] - timedelta(hours=1)),
                )
                self.tokens.notify_profile_ready(token_id)
                manager._write_metadata_file(
                    transfer_path,
                    {
                        "binding_sha256": binding,
                        "state_imported": True,
                        "completed": True,
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    },
                )
                return {
                    "success": True,
                    "token_id": token_id,
                    "email": email,
                    "is_active": account["is_active"],
                    "validation": {
                        "oauth_renewal": True,
                        "cold_reopens": 2,
                        "identity_verified": True,
                    },
                }
            except BaseException as error:
                if token_id is not None:
                    error.transfer_token_id = token_id
                # Keep the newly created target for inspection. It is never
                # scheduled after a failed import, and no existing Profile moves.
                if (profile_dir / "metadata.json").exists():
                    reason = getattr(error, "reason", getattr(error, "code", "transfer_error"))
                    if isinstance(error, asyncio.CancelledError):
                        reason = "cancelled"
                    elif isinstance(error, TimeoutError):
                        reason = "transient_error"
                    elif reason not in FAILURE_REASONS:
                        reason = "transfer_error"
                    elapsed = round(time.monotonic() - started, 1)
                    error.transfer_stage = stage
                    error.transfer_elapsed_seconds = elapsed
                    await asyncio.shield(manager.mark_blocked(email, "transfer_failed"))
                    # ProfileStatus fields stay stable. Transfer diagnostics live
                    # beside its metadata, without storage state or credentials.
                    manager._write_metadata_file(
                        transfer_path,
                        {
                            **transfer,
                            "transfer_failure_reason": reason,
                            "transfer_failure_stage": stage,
                            "elapsed_seconds": elapsed,
                        },
                    )
                if token_id is not None:
                    await asyncio.shield(
                        self.tokens.db.update_token(
                            token_id,
                            is_active=False,
                            auto_refresh_enabled=False,
                            ban_reason="manual",
                            refresh_status="config_error",
                            refresh_error="Profile 迁移未完成，资料已保留供检查",
                            browser_profile_status="blocked",
                            browser_profile_error="transfer_failed",
                        )
                    )
                raise
