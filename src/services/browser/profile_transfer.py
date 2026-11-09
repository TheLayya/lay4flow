"""Move a validated Cloak login through native, scoped browser-state JSON."""

import asyncio
import hashlib
import json
import math
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ...core.account_identity import same_google_identity
from ...core.logger import debug_logger
from ...core.models import Token
from ..flow.google_cookies import GoogleCookieJar
from ..profile_maintenance import profile_policy
from ..proxy_manager import ProxyManager
from .cloak_backend import CloakBrowserBackend
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
    "submission_proxy_mode",
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
    "account_changed",
}
VERIFICATION_MESSAGES = {
    "transient_error": "访问 Google 的网络请求超时或服务暂时异常，请检查该账号的代理后重试",
    "login_required": "Google 登录态已失效，请在账号浏览器中重新登录后导出",
    "session_invalid": "Flow 登录态校验未通过，请在源服务器刷新凭据后重新导出",
    "identity_mismatch": "浏览器中的 Google 账号与迁移账号不一致",
    "invalid_cookies": "登录凭据中的 Google 账号与迁移账号不一致",
    "environment_changed": "目标浏览器身份环境发生变化，请检查目标端浏览器配置",
    "environment_timeout": "读取浏览器身份环境超时，请重试",
    "environment_probe_error": "读取浏览器身份环境失败，请检查浏览器服务",
    "account_probe_error": "账号接口校验失败，请检查代理网络后重试",
    "credits_invalid": "账号额度接口返回异常，请稍后重试",
}
TRANSFER_STAGES = {
    "open_profile": "启动目标浏览器",
    "restore_state": "恢复登录态",
    "flow_verification": "Flow 登录态校验",
    "save_account": "保存账号",
    "prepare_workspace": "准备账号工作区",
    "finalize": "完成账号配置",
}
PROFILE_NETWORK_STEPS = {
    "flow_bootstrap": "读取 Flow 会话",
    "flow_identity": "校验 Google 账号身份",
    "flow_credits": "校验 Flow 账号额度",
    "flow_projects": "准备 Flow 项目",
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
    network_step = getattr(error, "profile_network_step", None)
    if network_step in PROFILE_NETWORK_STEPS:
        diagnostics["network_step"] = network_step
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
        if network_step in PROFILE_NETWORK_STEPS:
            message += f"（步骤：{PROFILE_NETWORK_STEPS[network_step]}）"
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

    async def existing_account(self, email):
        matches = [
            token
            for token in await self.tokens.db.get_all_tokens()
            if same_google_identity(token.email, email)
        ]
        if len(matches) > 1:
            raise ProfileTransferError(
                "ambiguous_account", "同一邮箱存在多条账号，请先整理账号记录", 409
            )
        return matches[0] if matches else None

    async def update_existing(self, token, cookies, *, on_stage=None):
        """Replace only login material on the original identity and fixed exit."""
        from .profile_update import update_profile_login

        if token.browser_profile_source != "cloak":
            raise ProfileTransferError(
                "profile_required", "已有账号不是 Cloak Profile，请在账号页处理", 409
            )
        manager = self.refresher.manager
        proxy = await self.refresher._proxy_url(token)
        refresh_lock = await self.tokens._get_token_lock(
            self.tokens._refresh_locks, self.tokens._refresh_lock_guard, token.id
        )
        cookie_lock = await self.tokens._get_token_lock(
            self.tokens._profile_cookie_locks, self.tokens._refresh_lock_guard, token.id
        )
        pending = self.tokens._refresh_futures.get(token.id)
        if refresh_lock.locked() or cookie_lock.locked() or (pending and not pending.done()):
            raise ProfileTransferError("account_busy", "账号正在刷新或使用，请稍后再导入", 409)
        maintenance = getattr(self.tokens, "account_login_maintenance", None)
        if maintenance is None:
            maintenance = self.tokens.account_proxy_maintenance
        lease = maintenance(token, proxy) if maintenance is not None else nullcontext()

        @asynccontextmanager
        async def reserved():
            try:
                async with lease:
                    yield
            except ValueError as error:
                if isinstance(error, ProfileTransferError):
                    raise
                raise ProfileTransferError(
                    "account_busy", "账号有进行中的任务或维护操作，请结束后再更新登录态", 409
                ) from None

        async with refresh_lock, cookie_lock, reserved():
            latest = await self.tokens.db.get_token(token.id)
            if latest is None or latest.model_dump() != token.model_dump():
                raise ProfileTransferError("account_changed", "账号配置已变化，请刷新后再导入", 409)
            stage = "open_profile"

            def progress(value):
                nonlocal stage
                stage = value
                if on_stage:
                    on_stage(value)

            async def validate(staged_manager, context):
                progress("restore_state")
                # Validate the submitted login itself, never fall back to old
                # cookies already present in the retained Profile.
                await context.clear_cookies()
                await context.add_cookies(cookies)
                progress("flow_verification")
                probe = TokenManagerAccountProbe(self.tokens, token)
                probe.set_context(context)
                result = await self.refresher.verifier.verify_account(token.email, probe)
                if result.reason != "account_ready" or not probe.validated_session:
                    error = ProfileNotReadyError(token.email, result.reason)
                    error.profile_network_step = probe.network_step
                    raise error
                values = {
                    **probe.validated_session,
                    "google_cookies": json.dumps(GoogleCookieJar(await context.cookies()).export()),
                }
                return values

            async def commit(validated):
                progress("save_account")
                if await self.refresher._proxy_url(token) != proxy:
                    raise ProfileTransferError(
                        "account_changed", "账号固定代理已变化，更新已回滚", 409
                    )
                now = datetime.now(timezone.utc)
                values = {
                    **validated,
                    "browser_profile_epoch": uuid.uuid4().hex,
                    "browser_profile_status": "ready",
                    "browser_profile_error": "",
                    "browser_profile_verified_at": now,
                    "browser_profile_failures": 0,
                    "refresh_status": "ready",
                    "refresh_error": "",
                    "refresh_reason": "",
                    "refresh_stage": "",
                    "refresh_network_step": "",
                    "refresh_failures": 0,
                    "last_refresh_at": now,
                    "last_st_refresh_at": now,
                    "last_st_refresh_result": "success",
                    "browser_profile_next_at": now
                    + timedelta(minutes=profile_policy(self.tokens.db)["refresh_interval_minutes"])
                    if token.auto_refresh_enabled and profile_policy(self.tokens.db)["enabled"]
                    else None,
                    "next_refresh_at": now
                    + timedelta(minutes=profile_policy(self.tokens.db)["refresh_interval_minutes"])
                    if token.auto_refresh_enabled and profile_policy(self.tokens.db)["enabled"]
                    else None,
                }
                if not await self.tokens.db.update_token_if_credentials_match(
                    token.id,
                    token.model_dump(exclude={"id"}),
                    expected_proxy_binding=(token.proxy_pool_id, token.proxy_url)
                    if token.proxy_pool_id is not None
                    else None,
                    **values,
                ):
                    raise ProfileTransferError("account_changed", "账号设置已变化，更新已回滚", 409)
                # The database commit is final. A local cache failure must not
                # roll the filesystem back to the preceding login afterwards.
                try:
                    self.tokens._clear_at_validation_cache(token.id)
                    self.tokens.mark_profile_validated(token.model_copy(update=values))
                except Exception:
                    debug_logger.log_warning("[PROFILE_IMPORT] Validation cache refresh deferred")

            try:
                progress("open_profile")
                await update_profile_login(
                    manager, token.email, proxy, self.refresher.verifier, validate, commit
                )
            except BaseException as error:
                error.transfer_stage = stage
                if isinstance(error, ValueError) and not isinstance(error, ProfileTransferError):
                    failure = ProfileTransferError(
                        "profile_update_unavailable",
                        "原 Profile 正在使用或身份配置无法确认，请关闭账号浏览器后再导入；原登录态已保留",
                        409,
                    )
                    failure.transfer_stage = stage
                    raise failure from None
                raise
        return {
            "success": True,
            "token_id": token.id,
            "email": token.email,
            "action": "updated",
            "added": 0,
            "updated": 1,
            "is_active": token.is_active,
            "validation": {"flow_session_verified": True, "identity_verified": True},
        }

    @property
    def refresher(self):
        value = getattr(self.tokens, "profile_refresher", None)
        if value is None:
            raise ProfileTransferError("profile_unavailable", "请先启用 Cloak Profile 服务", 503)
        return value

    def _launch_options(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if options is None:
            options = dict(getattr(self.refresher.manager.backend, "options", {}))
            for key in ("headless", "proxy", "env"):
                options.pop(key, None)
        if not isinstance(options, dict) or set(options) - LAUNCH_KEYS:
            raise ProfileTransferError("launch_options_unsupported", "当前启动参数需先适配迁移格式")
        args = options.get("args", [])
        if not isinstance(args, (list, tuple)):
            raise ProfileTransferError("launch_options_unsupported", "当前启动参数需先适配迁移格式")
        for arg in args:
            if not isinstance(arg, str) or not (
                arg in {"--disable-breakpad", "--disable-crash-reporter"}
                or arg.startswith(("--fingerprint-", "--lang=", "--window-size="))
            ):
                raise ProfileTransferError(
                    "launch_options_unsupported", "当前启动参数需先适配迁移格式"
                )
        return dict(options)

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
        if account.get("submission_proxy_mode", "fixed") not in {"fixed", "pool_random"}:
            raise ProfileTransferError("invalid_settings", "迁移包提交代理策略无效")
        if account.get("remark") is not None and not isinstance(account["remark"], str):
            raise ProfileTransferError("invalid_settings", "迁移包备注无效")
        manager = self.refresher.manager
        seed = manager._validate_seed(browser.get("fingerprint_seed"))
        if seed is None:
            raise ProfileTransferError("invalid_seed", "迁移包指纹 seed 无效")
        if not isinstance(browser.get("launch_options"), dict):
            raise ProfileTransferError("launch_options_unsupported", "迁移包浏览器启动参数无效")
        self._launch_options(browser["launch_options"])
        self._launch_options()
        runtime = browser.get("runtime")
        if not isinstance(runtime, dict) or any(
            not isinstance(runtime.get(key), str) or not runtime[key].strip()
            for key in ("cloakbrowser", "playwright", "chromium")
        ):
            raise ProfileTransferError("invalid_runtime", "迁移包浏览器运行环境无效")
        _runtime()
        try:
            EnvironmentSnapshot.from_json(json.dumps(browser["environment"]))
        except (KeyError, TypeError, ValueError):
            raise ProfileTransferError("invalid_environment", "迁移包浏览器环境无效") from None
        return account, browser, seed, _scoped_state(bundle.get("storage_state"))

    async def _check_context(self, context, token):
        # A new target records its own environment; subsequent opens must keep
        # that local identity stable. Source OS/runtime settings are provenance.
        spec = await self.refresher.manager.spec(token.email)
        reason = await self.refresher.verifier.verify_environment(
            context, str(spec.profile_dir), allow_viewport_change=True
        )
        if reason != "environment_stable":
            raise ProfileNotReadyError(token.email, reason)

    @staticmethod
    def _ensure_third_party_cookies(profile_dir: Any) -> None:
        """Allow Google's cross-site auth cookies so refreshes don't drop login.

        Chromium 146 blocks third-party cookies by default (Tracking Protection /
        3PCD). An extension-imported session spans accounts.google.com and
        flow.google.com, so without this exception the first auto-refresh redirects
        to accounts.google.com and the account reports ``login_required``.
        """
        pref = Path(profile_dir) / "Default" / "Preferences"
        if not pref.is_file():
            return
        try:
            data = json.loads(pref.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        profile = data.setdefault("profile", {})
        profile["cookie_controls_mode"] = 0
        exceptions = profile.setdefault("content_settings", {}).setdefault("exceptions", {})
        # Chrome ignores content-setting exceptions without a last_modified
        # timestamp. Use a permanent allow (expiration "0") for google.com in a
        # third-party context, mirroring the browser's "允许第三方 Cookie" action.
        chrome_now = str(int((time.time() + 11644473600) * 1_000_000))
        exceptions.setdefault("cookies", {})["*,https://[*.]google.com"] = {
            "setting": 1,
            "last_modified": chrome_now,
            "expiration": "0",
        }
        try:
            pref.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

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

    def _resume_transfer(self, profile_dir, normalized, seed, binding, legacy_binding, proxy_url):
        try:
            metadata = json.loads((profile_dir / "metadata.json").read_text(encoding="utf-8"))
            if (
                not isinstance(metadata, dict)
                or metadata.get("account_key") != normalized
                or metadata.get("fingerprint_seed") != seed
            ):
                raise ValueError
            failed = (
                metadata.get("state") in {"blocked", "stopped", "error"}
                and metadata.get("last_error") == "transfer_failed"
            )
            transfer_path = profile_dir / "profile_transfer.json"
            if not transfer_path.exists():
                if not failed:
                    raise ValueError
                # Legacy failures have no sidecar. The existing launch hash
                # proves their exact identity options and fixed proxy before
                # we resume their persistent state without injecting it again.
                manager = self.refresher.manager
                manager._existing_spec(normalized)
                EnvironmentSnapshot.from_json(
                    (profile_dir / "environment_snapshot.json").read_text(encoding="utf-8")
                )
                backend = manager.backend
                backend._check_launch_settings(
                    profile_dir, backend._launch_options(profile_dir, proxy_url)
                )
                return {"binding_sha256": binding, "state_imported": True, "legacy_resumed": True}
            transfer = json.loads(transfer_path.read_text(encoding="utf-8"))
            if (
                not isinstance(transfer, dict)
                or transfer.get("binding_sha256") not in {binding, legacy_binding}
                or not isinstance(transfer.get("state_imported"), bool)
                or ("completed" in transfer and transfer["completed"] is not False)
            ):
                raise ValueError
            reason = transfer.get("transfer_failure_reason")
            if reason in FAILURE_REASONS | {"transfer_error", "cancelled"}:
                if not failed:
                    raise ValueError
            elif reason is None:
                # A process can stop after restoring state, before its failure
                # handler runs. The incomplete transfer's exact identity/exit
                # binding proves ownership; ordinary initialized Profiles never
                # acquire this sidecar and cannot enter this recovery path.
                if (
                    metadata.get("state") not in {"starting", "stopped", "error", "blocked"}
                    or metadata.get("last_verified_at") is not None
                ):
                    raise ValueError
                manager = self.refresher.manager
                manager._existing_spec(normalized)
                EnvironmentSnapshot.from_json(
                    (profile_dir / "environment_snapshot.json").read_text(encoding="utf-8")
                )
                if not (profile_dir / "launch_settings.sha256").is_file():
                    raise ValueError
                backend = manager.backend
                backend._check_launch_settings(
                    profile_dir, backend._launch_options(profile_dir, proxy_url)
                )
            else:
                raise ValueError
            return {**transfer, "binding_sha256": binding}
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
        repair_existing_login: bool = False,
    ) -> dict[str, Any]:
        stage = "queued"

        def progress(value):
            nonlocal stage
            stage = value
            if on_stage is not None:
                on_stage(value)

        account, browser, seed, state = self._validate(bundle)
        # Retired Labs session cookies never enter a new or repaired Profile.
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
        async with self._import_lock:
            email = account["email"].strip()
            existing = await self.existing_account(email)
            if existing is not None:
                return await self.update_existing(existing, state["cookies"], on_stage=on_stage)
            manager = self.refresher.manager
            normalized = manager._normalize_account_key(email)
            profile_dir = manager.root_dir / normalized
            requested_proxy_url = proxy_url
            try:
                proxy_pool_id, proxy_url = await ProxyManager(self.tokens.db).select_account_proxy(
                    proxy_pool_id, proxy_url
                )
                if (
                    requested_proxy_url
                    and ProxyManager(self.tokens.db).normalize_proxy_url(requested_proxy_url)
                    != proxy_url
                ):
                    raise ValueError("proxy changed")
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
            binding = self._transfer_binding(
                email,
                seed,
                {"launch_options": self._launch_options()},
                token.proxy_url,
                proxy_pool_id,
            )
            legacy_binding = self._transfer_binding(
                email, seed, browser, token.proxy_url, proxy_pool_id
            )
            resumed = profile_dir.exists()
            if resumed:
                if profile_dir.resolve().parent != manager.root_dir.resolve():
                    raise ProfileTransferError(
                        "profile_exists", "目标 Profile 路径异常，原资料已保留", 409
                    )
                transfer = self._resume_transfer(
                    profile_dir, normalized, seed, binding, legacy_binding, token.proxy_url
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
                    CloakBrowserBackend.prepare_session_restore(profile_dir)
                manager._write_metadata_file(transfer_path, transfer)
                async with manager.session(email, proxy_url=token.proxy_url) as context:
                    await self._check_context(context, token)
                    retained_login = resumed and transfer.get("state_imported") is True
                    # Restore only portable state; admission uses the current
                    # Flow identity and credits RPCs in this fixed-proxy context.
                    if not transfer.get("state_imported"):
                        progress("restore_state")
                        if not callable(getattr(context, "set_storage_state", None)):
                            raise ProfileTransferError(
                                "native_state_unavailable", "请升级 Playwright 后再迁移", 503
                            )
                        await context.set_storage_state(state)
                        transfer["state_imported"] = True
                        manager._write_metadata_file(transfer_path, transfer)
                    progress("flow_verification")
                    if repair_existing_login and retained_login:
                        # The extension already verified this fresh capture on
                        # the retained fixed exit. Apply it before the single
                        # Profile check instead of trying stale login first.
                        add_cookies = getattr(context, "add_cookies", None)
                        if not callable(add_cookies):
                            raise ProfileTransferError(
                                "native_state_unavailable", "浏览器无法更新保留的登录态", 503
                            ) from None
                        progress("restore_state")
                        await add_cookies(state["cookies"])
                        transfer["login_repaired"] = True
                        manager._write_metadata_file(transfer_path, transfer)
                        progress("flow_verification")
                    renewed = await self.refresher.refresh_in_context(token, context, force=True)
                    if not renewed.get("_validated_session"):
                        raise ProfileTransferError(
                            "renewal_unverified", "迁移 Flow 登录态未通过校验", 422
                        )
                self._ensure_third_party_cookies(profile_dir)
                # Import performs one fixed-proxy identity, Flow and credits
                # check. Runtime refresh/error handling remains authoritative.
                validated = {
                    **renewed["_validated_session"],
                    "google_cookies": renewed.get("google_cookies") or "[]",
                }
                progress("save_account")
                now = datetime.now(timezone.utc)
                token = Token(
                    st="profile-session:" + uuid.uuid4().hex,
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
                    submission_proxy_mode=account.get("submission_proxy_mode", "fixed"),
                    login_account=email,
                    login_password="",
                    protocol_mode="session",
                    auto_refresh_enabled=False,
                    refresh_interval_minutes=30,
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
                token = await self.tokens.db.get_token(token_id)
                expected = self.tokens._refresh_expected(token, token)
                expected.update(auto_refresh_enabled=False, banned_at=token.banned_at)
                # Account admission is complete after identity verification and
                # normal close. Project creation belongs to generation:
                # an unrelated workspace outage must not quarantine this login.
                progress("finalize")
                updates = {
                    "auto_refresh_enabled": account["auto_refresh_enabled"],
                    "next_refresh_at": now
                    + timedelta(minutes=profile_policy(self.tokens.db)["refresh_interval_minutes"])
                    if account["auto_refresh_enabled"] and profile_policy(self.tokens.db)["enabled"]
                    else None,
                    "browser_profile_next_at": now
                    + timedelta(minutes=profile_policy(self.tokens.db)["refresh_interval_minutes"])
                    if account["auto_refresh_enabled"] and profile_policy(self.tokens.db)["enabled"]
                    else None,
                }
                if account["is_active"]:
                    updates.update(is_active=True, ban_reason=None, banned_at=None)
                if not await self.tokens.db.update_token_if_credentials_match(
                    token_id,
                    expected,
                    expected_proxy_binding=(token.proxy_pool_id, token.proxy_url)
                    if token.proxy_pool_id is not None
                    else None,
                    **updates,
                ):
                    raise ProfileTransferError(
                        "account_changed", "账号配置已变化，已保留当前设置", 409
                    )
                self.tokens.mark_profile_validated(token.model_copy(update=updates))
                self.tokens.notify_profile_ready(token_id)
                try:
                    manager._write_metadata_file(
                        transfer_path,
                        {
                            "binding_sha256": binding,
                            "state_imported": True,
                            "completed": True,
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                        },
                    )
                except OSError as error:
                    # The verified account is already committed. A diagnostic
                    # sidecar cannot roll back its login or refresh settings.
                    debug_logger.log_warning(
                        f"[PROFILE_TRANSFER] Token {token_id}: completion record "
                        f"could not be written ({type(error).__name__})"
                    )
                return {
                    "success": True,
                    "token_id": token_id,
                    "email": email,
                    "is_active": account["is_active"],
                    "validation": {"flow_session_verified": True, "identity_verified": True},
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
                            **(
                                {"network_step": error.profile_network_step}
                                if getattr(error, "profile_network_step", None)
                                in PROFILE_NETWORK_STEPS
                                else {}
                            ),
                            "elapsed_seconds": elapsed,
                        },
                    )
                if token_id is not None and getattr(error, "code", "") != "account_changed":
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
