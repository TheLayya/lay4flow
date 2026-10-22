"""Current Flow generation over HTTP using persisted account login state."""

import asyncio
import random
import time
import uuid
from contextlib import asynccontextmanager

from ...core.config import config
from ...core.logger import debug_logger
from ..flow.angular_images import (
    IMAGE_RPC,
    MODELS_RPC,
    build_image_request,
    parse_image_response,
    resolve_image_usage,
)
from ..flow.flow_rpc import FlowRPCError, flow_rpc
from ..flow.remote_captcha import RemoteCaptchaError
from ..project_pool import flow_session_binding
from .flow_session import account_http_session
from .token_account_probe import TokenManagerAccountProbe


class FlowProfileGeneration:
    def __init__(self, client, tokens, token):
        self.client, self.tokens, self.token = client, tokens, token
        self._request_context = None
        self._identity_verified = False

    @asynccontextmanager
    async def session(self):
        async with self._context() as current:
            self._request_context = current
            try:
                yield self
            finally:
                self._request_context = None

    async def _call(self, context, rpcid, payload, **kwargs):
        try:
            return await flow_rpc(
                context, rpcid, payload, expected_email=self.token.email, **kwargs
            )
        except FlowRPCError as error:
            if error.reason in {"login_required", "session_invalid", "identity_mismatch"}:
                self._identity_verified = False
                self.tokens._clear_at_validation_cache(self.token.id)
            raise

    async def _check_binding(self, proxy, *, read_only=False):
        proxy = proxy or ""
        current = await self.tokens.db.get_token(self.token.id)
        expected = flow_session_binding(self.token, read_only=read_only)
        changed = (
            [key for key, value in expected.items() if getattr(current, key) != value]
            if current is not None
            else ["account_deleted"]
        )
        if not changed and (await self.tokens.resolve_account_proxy(current) or "") != proxy:
            changed.append("resolved_proxy")
        if changed:
            # Field names identify a stale task without disclosing credentials.
            debug_logger.log_warning(
                f"[FLOW_BINDING] Token {self.token.id}: changed_fields={','.join(changed)}"
            )
            raise FlowRPCError("account_changed", "flow_generation")

    @asynccontextmanager
    async def _context(self, *, read_only=False):
        proxy = await self.tokens.resolve_account_proxy(self.token) or ""
        await self._check_binding(proxy, read_only=read_only)
        if self._request_context is not None:
            if self._request_context[1] != proxy:
                raise FlowRPCError("account_changed", "flow_generation")
            yield self._request_context
            return
        # Read the latest jar after another task's HTTP cookie rotation.
        current = await self.tokens.db.get_token(self.token.id)
        if current is None or flow_session_binding(
            current, read_only=read_only
        ) != flow_session_binding(self.token, read_only=read_only):
            raise FlowRPCError("account_changed", "flow_generation")
        self.token.google_cookies = current.google_cookies
        async with account_http_session(self.tokens, self.token, proxy) as context:
            yield context, proxy

    async def _verified(self, context):
        if self._identity_verified:
            return
        recent = getattr(self.tokens, "is_profile_validation_recent", None)
        if callable(recent) and recent(self.token):
            return
        probe = TokenManagerAccountProbe(self.tokens, self.token)
        result = await probe.verify(self.token.email, context)
        if result.get("reason"):
            raise FlowRPCError(result["reason"], probe.network_step)
        mark = getattr(self.tokens, "mark_profile_validated", None)
        if callable(mark):
            mark(self.token)
        self._identity_verified = True

    async def _captcha(self, project_id, action):
        # Retry acquisition only. The caller submits its write once after this
        # returns; unknown upload/generation outcomes are never replayed.
        for attempt in range(max(1, config.flow_max_retries)):
            try:
                token, session_id = await self.client._get_recaptcha_token(
                    project_id, action=action, token_id=self.token.id
                )
                if token:
                    return token, session_id
                if session_id:
                    await self.client._notify_browser_captcha_request_finished(session_id)
                raise RemoteCaptchaError("MISSING_TOKEN_OR_SESSION")
            except RemoteCaptchaError as error:
                if not error.retryable or attempt + 1 >= config.flow_max_retries:
                    raise
                await asyncio.sleep(min(attempt + 1, 3))

    async def generate_image(
        self, project_id, prompt, model_name, aspect_ratio, image_inputs, progress
    ):
        trace = {"max_retries": config.flow_max_retries, "generation_attempts": []}
        async with self._context() as (context, proxy):
            await self._verified(context)
            models = await self._call(context, MODELS_RPC, [], network_step="flow_models")
            # serviceTier is GetCredits field4. The account probe's paygate tier
            # has a different enum and must not be used in its place.
            credits = await self._call(context, "nzlxg", [], network_step="flow_credits")
            service_tier = credits[3] if isinstance(credits, list) and len(credits) > 3 else None
            usage = resolve_image_usage(
                models, model_name, aspect_ratio, service_tier, bool(image_inputs)
            )
            batch_id, media_id, workflow_id = (str(uuid.uuid4()) for _ in range(3))
            for attempt in range(max(1, config.flow_max_retries)):
                started = time.monotonic()
                browser_id = None
                record = {"attempt": attempt + 1, "recaptcha_ok": False}
                trace["generation_attempts"].append(record)
                try:
                    if progress:
                        await progress("solving_image_captcha", 38)
                    captcha, browser_id = await self.client._get_recaptcha_token(
                        project_id, action="IMAGE_GENERATION", token_id=self.token.id
                    )
                    record["recaptcha_ok"] = bool(captcha)
                    if not captcha:
                        raise FlowRPCError("captcha_failed", "flow_captcha")
                    await self._check_binding(proxy)
                    payload = build_image_request(
                        project_id,
                        prompt,
                        usage,
                        aspect_ratio,
                        captcha,
                        random.randrange(2147483647),
                        batch_id,
                        media_id,
                        workflow_id,
                        image_inputs,
                    )
                    if progress:
                        await progress("submitting_image", 48)
                    response = await self._call(
                        context,
                        IMAGE_RPC,
                        payload,
                        network_step="flow_generate_image",
                        timeout_ms=max(20000, int(config.flow_image_request_timeout * 1000)),
                        source_path=f"/project/{project_id}",
                    )
                    result = parse_image_response(response)
                    record["success"] = True
                    trace["final_success_attempt"] = attempt + 1
                    return result, batch_id, trace
                except Exception as error:
                    record.update(success=False, error=str(error)[:240])
                    error.flow_retry_trace = trace
                    # An unknown submission outcome must not be posted again.
                    # Only a failed captcha acquisition is safe to retry here.
                    if (
                        not (
                            getattr(error, "reason", "") == "captcha_failed"
                            or isinstance(error, RemoteCaptchaError)
                            and error.retryable
                        )
                        or attempt + 1 >= config.flow_max_retries
                    ):
                        raise
                    if isinstance(error, RemoteCaptchaError):
                        await asyncio.sleep(min(attempt + 1, 3))
                finally:
                    record["duration_ms"] = int((time.monotonic() - started) * 1000)
                    if browser_id:
                        await self.client._notify_browser_captcha_request_finished(browser_id)

    async def upload_image(self, project_id, image_bytes):
        from ..flow.angular_image_assets import (
            UPLOAD_IMAGE_RPC,
            build_upload_image_request,
            parse_upload_image_response,
        )

        async with self._context() as (context, proxy):
            await self._verified(context)
            # Flow's UploadImage RPC accepts the project context directly and
            # does not use reCAPTCHA. Only generation/upsample requests need a
            # remote solve. Uploading a frame must not consume a separate solve.
            payload = build_upload_image_request(
                project_id,
                image_bytes,
                self.client._detect_image_mime_type(image_bytes),
                "lay4flow-upload-" + uuid.uuid4().hex,
            )
            await self._check_binding(proxy)
            return parse_upload_image_response(
                await self._call(
                    context,
                    UPLOAD_IMAGE_RPC,
                    payload,
                    network_step="flow_upload_image",
                    timeout_ms=max(90000, int(config.flow_image_request_timeout * 1000)),
                    source_path=f"/project/{project_id}",
                )
            )

    async def upsample_image(self, project_id, media_id, resolution):
        from ..flow.angular_image_assets import (
            UPSAMPLE_IMAGE_RPC,
            build_upsample_image_request,
            parse_upsample_image_response,
        )

        async with self._context() as (context, proxy):
            await self._verified(context)
            captcha, browser_id = await self._captcha(project_id, "IMAGE_GENERATION")
            try:
                if not captcha:
                    raise FlowRPCError("captcha_failed", "flow_captcha")
                await self._check_binding(proxy)
                payload = build_upsample_image_request(project_id, media_id, resolution, captcha)
                return parse_upsample_image_response(
                    await self._call(
                        context,
                        UPSAMPLE_IMAGE_RPC,
                        payload,
                        network_step="flow_upsample_image",
                        timeout_ms=max(20000, int(config.upsample_timeout * 1000)),
                        source_path=f"/project/{project_id}",
                    )
                )
            finally:
                if browser_id:
                    await self.client._notify_browser_captcha_request_finished(browser_id)

    async def generate_video_text(self, project_id, prompt, model_name, aspect_ratio):
        return await self.generate_video("text", project_id, prompt, model_name, aspect_ratio)

    async def generate_video(self, mode, project_id, prompt, model_name, aspect_ratio, **kwargs):
        from ..flow.angular_video import (
            VIDEO_RPCS,
            build_video_request,
            parse_video_response,
            resolve_video_usage,
        )

        async with self._context() as (context, proxy):
            await self._verified(context)
            models = await self._call(context, MODELS_RPC, [], network_step="flow_models")
            credits = await self._call(context, "nzlxg", [], network_step="flow_credits")
            service_tier = credits[3] if isinstance(credits, list) and len(credits) > 3 else None
            duration = kwargs.pop("duration", None)
            model_name = resolve_video_usage(
                models,
                model_name,
                aspect_ratio,
                service_tier,
                resolution=kwargs.get("resolution"),
                duration=duration,
                mode=mode,
            )
            for name in ("batch_id", "media_id", "workflow_id"):
                kwargs[name] = kwargs.get(name) or str(uuid.uuid4())
            max_attempts = max(1, config.flow_max_retries)
            for attempt in range(max_attempts):
                browser_id = None
                rejection_reason = None
                delay = min(attempt + 1, 3)
                try:
                    captcha, browser_id = await self.client._get_recaptcha_token(
                        project_id, action="VIDEO_GENERATION", token_id=self.token.id
                    )
                    if not captcha:
                        raise RemoteCaptchaError("MISSING_TOKEN_OR_SESSION")
                    await self._check_binding(proxy)
                    payload = build_video_request(
                        mode=mode,
                        project_id=project_id,
                        prompt=prompt,
                        model_name=model_name,
                        aspect_ratio=aspect_ratio,
                        captcha_token=captcha,
                        **kwargs,
                    )
                    return parse_video_response(
                        await self._call(
                            context,
                            VIDEO_RPCS[mode],
                            payload,
                            network_step="flow_generate_video",
                            timeout_ms=max(90000, self.client._get_video_submit_timeout() * 1000),
                            source_path=f"/project/{project_id}",
                        ),
                        project_id,
                    )
                except (RemoteCaptchaError, FlowRPCError) as error:
                    # A typed RPC rejection without a success payload is known
                    # not to have accepted this write. Network timeouts and
                    # mixed/unknown outcomes must never submit another video.
                    confirmed = (
                        isinstance(error, FlowRPCError)
                        and error.network_step == "flow_generate_video"
                        and error.submission_rejected
                        and error.public_error is not None
                        and error.rpc_code is not None
                    )
                    if confirmed:
                        rejection_reason = (
                            "recaptcha_rejected"
                            if error.public_error
                            in {
                                "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
                                "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC",
                            }
                            else "upstream_rejected"
                        )
                        delay = min(2 ** (attempt + 1), 10) + random.uniform(0, 1)
                    retryable = error.retryable and (
                        isinstance(error, RemoteCaptchaError)
                        or confirmed
                        and error.reason == "transient_error"
                    )
                    if not retryable or attempt + 1 >= max_attempts:
                        raise
                    debug_logger.log_warning(
                        f"[VIDEO_RETRY] Token {self.token.id}: {error}; "
                        f"attempt={attempt + 1}/{max_attempts}; delay={delay:.1f}s"
                    )
                finally:
                    if browser_id:
                        if rejection_reason:
                            await self.client._notify_browser_captcha_error(
                                browser_id, error_reason=rejection_reason
                            )
                        else:
                            await self.client._notify_browser_captcha_request_finished(browser_id)
                await asyncio.sleep(delay)

    async def get_media(self, media_name):
        from ..flow.angular_video import parse_media_response

        async with self._context(read_only=True) as (context, _):
            await self._verified(context)
            payload = await self._call(
                context,
                "as29s",
                [media_name],
                network_step="flow_get_media",
            )
            if not isinstance(payload, list) or len(payload) < 2:
                raise FlowRPCError("account_probe_error", "flow_get_media")
            project_id = payload[1]
            if not isinstance(project_id, str) or not project_id:
                project_id = self.token.current_project_id
            return parse_media_response(payload, project_id)

    async def upload_video(self, project_id, video_bytes, mime_type, trim_start_ms, trim_end_ms):
        from .flow_video_upload import upload_profile_video

        async with self._context() as (context, proxy):
            await self._verified(context)
            await self._check_binding(proxy)
            media_name = await upload_profile_video(
                context,
                project_id,
                video_bytes,
                mime_type,
                trim_start_ms,
                trim_end_ms,
            )
            return media_name

    async def check_video_status(self, operations):
        from ..flow.angular_video import (
            VIDEO_POLL_RPC,
            build_video_poll_request,
            parse_video_response,
        )

        async with self._context(read_only=True) as (context, _):
            project_id = next(
                (row.get("projectId") for row in operations if row.get("projectId")), ""
            )
            return parse_video_response(
                await self._call(
                    context,
                    VIDEO_POLL_RPC,
                    build_video_poll_request(operations),
                    network_step="flow_poll_video",
                ),
                project_id,
                poll=True,
            )
