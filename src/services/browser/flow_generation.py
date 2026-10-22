"""Current Flow generation through a verified account's browser Cookie jar."""

import random
import time
import uuid
from contextlib import asynccontextmanager

from ...core.config import config
from ..flow.angular_images import (
    IMAGE_RPC,
    MODELS_RPC,
    build_image_request,
    parse_image_response,
    resolve_image_usage,
)
from ..flow.browser_request import profile_http_request
from ..flow.flow_rpc import FlowRPCError, flow_rpc
from ..project_pool import profile_project_binding
from .token_account_probe import TokenManagerAccountProbe


class FlowProfileGeneration:
    def __init__(self, client, tokens, token):
        self.client, self.tokens, self.token = client, tokens, token

    async def _call(self, context, rpcid, payload, **kwargs):
        return await flow_rpc(context, rpcid, payload, expected_email=self.token.email, **kwargs)

    async def _check_binding(self, proxy):
        current = await self.tokens.db.get_token(self.token.id)
        if (
            current is None
            or profile_project_binding(current) != profile_project_binding(self.token)
            or await self.tokens.resolve_account_proxy(current) != proxy
        ):
            raise FlowRPCError("account_changed", "flow_generation")

    @asynccontextmanager
    async def _context(self):
        refresher = getattr(self.tokens, "profile_refresher", None)
        if refresher is None:
            raise FlowRPCError("account_probe_error", "flow_generation")
        proxy = await refresher._proxy_url(self.token)
        await self._check_binding(proxy)
        provider = refresher.active_context_provider
        if provider is not None:
            async with provider(self.token) as context:
                if context is not None:
                    async with profile_http_request(context, proxy) as request_context:
                        yield request_context, proxy
                    return
        async with refresher.manager.credential_session(
            self.token.email, refresher.verifier, proxy_url=proxy, existing_only=True
        ) as context:
            async with profile_http_request(context, proxy) as request_context:
                yield request_context, proxy

    async def _verified(self, context):
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
                        getattr(error, "reason", "") != "captcha_failed"
                        or attempt + 1 >= config.flow_max_retries
                    ):
                        raise
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
            captcha, browser_id = await self.client._get_recaptcha_token(
                project_id, action="UPLOAD_IMAGE", token_id=self.token.id
            )
            if not captcha:
                if browser_id:
                    await self.client._notify_browser_captcha_request_finished(browser_id)
                raise FlowRPCError("captcha_failed", "flow_captcha")
            try:
                payload = build_upload_image_request(
                    project_id,
                    image_bytes,
                    self.client._detect_image_mime_type(image_bytes),
                    "lay4flow-upload-" + uuid.uuid4().hex,
                    captcha,
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
            finally:
                if browser_id:
                    await self.client._notify_browser_captcha_request_finished(browser_id)

    async def upsample_image(self, project_id, media_id, resolution):
        from ..flow.angular_image_assets import (
            UPSAMPLE_IMAGE_RPC,
            build_upsample_image_request,
            parse_upsample_image_response,
        )

        async with self._context() as (context, proxy):
            await self._verified(context)
            captcha, browser_id = await self.client._get_recaptcha_token(
                project_id, action="IMAGE_GENERATION", token_id=self.token.id
            )
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
            captcha, browser_id = await self.client._get_recaptcha_token(
                project_id, action="VIDEO_GENERATION", token_id=self.token.id
            )
            try:
                if not captcha:
                    raise FlowRPCError("captcha_failed", "flow_captcha")
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
            finally:
                if browser_id:
                    await self.client._notify_browser_captcha_request_finished(browser_id)

    async def get_media(self, media_name):
        from ..flow.angular_video import parse_media_response

        async with self._context() as (context, _):
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

        async with self._context() as (context, _):
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
