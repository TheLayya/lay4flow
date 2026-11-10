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
from ..flow.flow_rpc import FlowRPCError, flow_rpc, prepare_flow_rpc
from ..flow.remote_captcha import RemoteCaptchaError
from ..generation_attempts import CURRENT_ATTEMPTS, is_confirmed_flow_rejection
from ..project_pool import flow_session_binding
from .flow_session import account_http_session
from .token_account_probe import TokenManagerAccountProbe


def _milliseconds(seconds):
    return max(0, min(86_400_000, int(seconds * 1000)))


class _SubmissionDiagnostics:
    """Numeric per-attempt evidence; never retain a token, cookie or session ID."""

    def __init__(self, record=None, *, attach_budget=True):
        budget = CURRENT_ATTEMPTS.get()
        self.targets = [record] if record is not None else []
        if attach_budget and budget is not None and budget.history:
            self.targets.append(budget.history[-1])
        self.solved_at = None
        self.post_started = None
        self.submit_seconds = 0
        self.count = 0
        self.state = "not_submitted"
        self.set(submission_count=0, submission_state=self.state)

    def set(self, **values):
        for target in self.targets:
            target.update(values)

    def captcha_finished(self, started, valid):
        now = time.monotonic()
        self.set(captcha_ms=_milliseconds(now - started))
        if valid:
            self.solved_at = now

    def observe(self, event, now, http_status=None):
        if event == "started":
            if self.count == 0 and self.solved_at is not None:
                self.set(captcha_to_submit_ms=_milliseconds(now - self.solved_at))
            self.count += 1
            self.post_started = now
            self.state = "unknown"
            self.set(submission_count=self.count, submission_state=self.state)
        elif event == "finished" and self.post_started is not None:
            self.submit_seconds += max(0, now - self.post_started)
            self.post_started = None
            self.set(submit_ms=_milliseconds(self.submit_seconds))
            if type(http_status) is int and 100 <= http_status <= 599:
                self.set(http_status=http_status)

    def accepted(self):
        self.state = "accepted"
        self.set(submission_state=self.state)

    def failed(self, error):
        if is_confirmed_flow_rejection(error) or (
            isinstance(error, FlowRPCError)
            and error.submission_rejected
            and error.network_step in {"flow_upload_image", "flow_upsample_image"}
        ):
            self.state = "rejected"
        self.set(submission_state=self.state)
        public = error.public_error if isinstance(error, FlowRPCError) else None
        if public:
            self.set(public_error=public)
        if isinstance(error, FlowRPCError) and error.rpc_code is not None:
            self.set(rpc_code=error.rpc_code)

    async def release(self, client, lease):
        if not lease:
            return
        if self.state == "accepted":
            await client._notify_browser_captcha_request_finished(lease)
        else:
            reason = {"rejected": "upstream_rejected", "unknown": "submission_unknown"}.get(
                self.state, "not_submitted"
            )
            await client._notify_browser_captcha_error(lease, error_reason=reason)


class FlowProfileGeneration:
    def __init__(self, client, tokens, token):
        self.client, self.tokens, self.token = client, tokens, token
        self._request_context = None
        self._identity_verified = False
        self._required_video_stages = []

    async def admit_video_stages(self, stages):
        """Validate the complete paid output plan before any native submission."""
        from ..flow.angular_video import resolve_video_usage

        async with self._context() as (context, _):
            await self._verified(context)
            models = await self._call(context, MODELS_RPC, [], network_step="flow_models")
            credits = await self._call(context, "nzlxg", [], network_step="flow_credits")
            tier = credits[3] if isinstance(credits, list) and len(credits) > 3 else None
            for stage in stages:
                resolve_video_usage(models, service_tier=tier, **stage)
        self._required_video_stages = [dict(stage) for stage in stages]

    @asynccontextmanager
    async def session(self, *, proxy_url=None):
        async with self._context(proxy_url=proxy_url) as current:
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
        if not read_only:
            policy = getattr(self.tokens, "generation_error_policy", None)
            if policy is not None and await policy.availability(self.token.id):
                error = FlowRPCError("account_cooldown", "flow_generation")
                error.submission_rejected = True
                raise error

    @asynccontextmanager
    async def _context(self, *, read_only=False, proxy_url=None):
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
        # Guards keep the saved login binding; only this task's transport changes exit.
        request_proxy = proxy if proxy_url is None else proxy_url
        async with account_http_session(self.tokens, self.token, request_proxy) as context:
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
                budget = CURRENT_ATTEMPTS.get()
                if budget is not None:
                    budget.take()
                started = time.monotonic()
                browser_id = None
                record = {"attempt": attempt + 1, "recaptcha_ok": False}
                trace["generation_attempts"].append(record)
                diagnostic = _SubmissionDiagnostics(record)
                try:
                    await prepare_flow_rpc(
                        context,
                        source_path=f"/project/{project_id}",
                        expected_email=self.token.email,
                    )
                    if progress:
                        await progress("solving_image_captcha", 38)
                    captcha_started = time.monotonic()
                    try:
                        captcha, browser_id = await self.client._get_recaptcha_token(
                            project_id, action="IMAGE_GENERATION", token_id=self.token.id
                        )
                    finally:
                        diagnostic.captcha_finished(captcha_started, browser_id is not None)
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
                    if budget is not None:
                        budget.stage("submit")
                    response = await self._call(
                        context,
                        IMAGE_RPC,
                        payload,
                        network_step="flow_generate_image",
                        timeout_ms=max(20000, int(config.flow_image_request_timeout * 1000)),
                        source_path=f"/project/{project_id}",
                        submit_observer=diagnostic.observe,
                    )
                    result = parse_image_response(response)
                    diagnostic.accepted()
                    record["success"] = True
                    if budget is not None:
                        budget.finish("success", next_action="completed")
                    trace["final_success_attempt"] = attempt + 1
                    return result, batch_id, trace
                except Exception as error:
                    diagnostic.failed(error)
                    if budget is not None:
                        budget.finish("failed", error, next_action="stop")
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
                        or budget is not None
                        and budget.used >= budget.limit
                    ):
                        raise
                    if isinstance(error, RemoteCaptchaError):
                        if budget is not None:
                            budget.history[-1]["next_action"] = "retry_same_account"
                            budget.history[-1]["delay_seconds"] = min(attempt + 1, 3)
                        await asyncio.sleep(min(attempt + 1, 3))
                finally:
                    record["duration_ms"] = int((time.monotonic() - started) * 1000)
                    await diagnostic.release(self.client, browser_id)

    async def upload_image(self, project_id, image_bytes):
        from ..flow.angular_image_assets import (
            UPLOAD_IMAGE_RPC,
            build_upload_image_request,
            parse_upload_image_response,
            validate_upload_image_inputs,
        )

        async with self._context() as (context, proxy):
            await self._verified(context)
            mime_type = self.client._detect_image_mime_type(image_bytes)
            filename = "lay4flow-upload-" + uuid.uuid4().hex
            validate_upload_image_inputs(project_id, image_bytes, mime_type, filename)
            await prepare_flow_rpc(
                context,
                source_path=f"/project/{project_id}",
                expected_email=self.token.email,
            )
            # The current official UploadImage caller acquires its own action
            # token before sending the same project context as generation.
            diagnostic = _SubmissionDiagnostics(attach_budget=False)
            browser_id = None
            try:
                captcha_started = time.monotonic()
                try:
                    captcha, browser_id = await self.client._get_recaptcha_token(
                        project_id, action="UPLOAD_IMAGE", token_id=self.token.id
                    )
                finally:
                    diagnostic.captcha_finished(captcha_started, browser_id is not None)
                if not captcha:
                    raise FlowRPCError("captcha_failed", "flow_captcha")
                await self._check_binding(proxy)
                payload = build_upload_image_request(
                    project_id, image_bytes, mime_type, filename, captcha
                )
                response = await self._call(
                    context,
                    UPLOAD_IMAGE_RPC,
                    payload,
                    network_step="flow_upload_image",
                    timeout_ms=max(90000, int(config.flow_image_request_timeout * 1000)),
                    source_path=f"/project/{project_id}",
                    submit_observer=diagnostic.observe,
                )
                result = parse_upload_image_response(response)
                diagnostic.accepted()
                return result
            except BaseException as error:
                diagnostic.failed(error)
                raise
            finally:
                await diagnostic.release(self.client, browser_id)

    async def upsample_image(self, project_id, media_id, resolution):
        from ..flow.angular_image_assets import (
            UPSAMPLE_IMAGE_RPC,
            build_upsample_image_request,
            parse_upsample_image_response,
            validate_upsample_image_inputs,
        )

        async with self._context() as (context, proxy):
            await self._verified(context)
            validate_upsample_image_inputs(project_id, media_id, resolution)
            await prepare_flow_rpc(
                context,
                source_path=f"/project/{project_id}",
                expected_email=self.token.email,
            )
            diagnostic = _SubmissionDiagnostics(attach_budget=False)
            browser_id = None
            try:
                captcha_started = time.monotonic()
                try:
                    captcha, browser_id = await self._captcha(project_id, "IMAGE_GENERATION")
                finally:
                    diagnostic.captcha_finished(captcha_started, browser_id is not None)
                if not captcha:
                    raise FlowRPCError("captcha_failed", "flow_captcha")
                await self._check_binding(proxy)
                payload = build_upsample_image_request(project_id, media_id, resolution, captcha)
                response = await self._call(
                    context,
                    UPSAMPLE_IMAGE_RPC,
                    payload,
                    network_step="flow_upsample_image",
                    timeout_ms=max(20000, int(config.upsample_timeout * 1000)),
                    source_path=f"/project/{project_id}",
                    submit_observer=diagnostic.observe,
                )
                result = parse_upsample_image_response(response)
                diagnostic.accepted()
                return result
            except BaseException as error:
                diagnostic.failed(error)
                raise
            finally:
                await diagnostic.release(self.client, browser_id)

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
            # Membership/catalog permissions may change while inputs upload.
            # Check the requested final resolution again before native billing.
            if mode != "upsample":
                for stage in self._required_video_stages:
                    resolve_video_usage(models, service_tier=service_tier, **stage)
            for name in ("batch_id", "media_id", "workflow_id"):
                kwargs[name] = kwargs.get(name) or str(uuid.uuid4())
            max_attempts = max(1, config.flow_max_retries)
            trace = {"max_retries": max_attempts, "generation_attempts": []}
            for attempt in range(max_attempts):
                budget = CURRENT_ATTEMPTS.get()
                if budget is not None:
                    budget.take()
                browser_id = None
                record = {"attempt": attempt + 1, "recaptcha_ok": False}
                trace["generation_attempts"].append(record)
                diagnostic = _SubmissionDiagnostics(record)
                delay = min(attempt + 1, 3)
                try:
                    await prepare_flow_rpc(
                        context,
                        source_path=f"/project/{project_id}",
                        expected_email=self.token.email,
                    )
                    captcha_started = time.monotonic()
                    try:
                        captcha, browser_id = await self.client._get_recaptcha_token(
                            project_id, action="VIDEO_GENERATION", token_id=self.token.id
                        )
                    finally:
                        diagnostic.captcha_finished(captcha_started, browser_id is not None)
                    record["recaptcha_ok"] = bool(captcha)
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
                    if budget is not None:
                        budget.stage("submit")
                    response = await self._call(
                        context,
                        VIDEO_RPCS[mode],
                        payload,
                        network_step="flow_generate_video",
                        timeout_ms=max(90000, self.client._get_video_submit_timeout() * 1000),
                        source_path=f"/project/{project_id}",
                        submit_observer=diagnostic.observe,
                    )
                    result = parse_video_response(response, project_id)
                    diagnostic.accepted()
                    record["success"] = True
                    if budget is not None:
                        budget.stage("polling")
                        budget.finish("accepted", next_action="polling")
                    return result
                except Exception as error:
                    diagnostic.failed(error)
                    record.update(success=False)
                    error.flow_retry_trace = trace
                    if budget is not None:
                        budget.finish("failed", error, next_action="stop")
                    # A typed RPC rejection without a success payload is known
                    # not to have accepted this write. Network timeouts and
                    # mixed/unknown outcomes must never submit another video.
                    confirmed = is_confirmed_flow_rejection(error)
                    if confirmed:
                        delay = min(2 ** (attempt + 1), 10) + random.uniform(0, 1)
                    # Every confirmed rejection belongs to the outer policy,
                    # even when no custom keyword matches: each failure must
                    # advance account protection and the allocation budget.
                    policy = getattr(self.tokens, "generation_error_policy", None)
                    if confirmed and policy is not None:
                        raise
                    retryable = (
                        isinstance(error, (RemoteCaptchaError, FlowRPCError))
                        and error.retryable
                        and (
                            isinstance(error, RemoteCaptchaError)
                            or confirmed
                            and error.reason == "transient_error"
                        )
                    )
                    if (
                        not retryable
                        or attempt + 1 >= max_attempts
                        or (budget is not None and budget.used >= budget.limit)
                    ):
                        raise
                    debug_logger.log_warning(
                        f"[VIDEO_RETRY] Token {self.token.id}: {error}; "
                        f"attempt={attempt + 1}/{max_attempts}; delay={delay:.1f}s"
                    )
                    if budget is not None:
                        budget.history[-1]["next_action"] = "retry_same_account"
                        budget.history[-1]["delay_seconds"] = round(delay, 2)
                finally:
                    await diagnostic.release(self.client, browser_id)
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

    async def upload_video(
        self,
        project_id,
        video_bytes,
        mime_type,
        trim_start_ms,
        trim_end_ms,
        *,
        max_duration_ms=10000,
    ):
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
                **({"max_duration_ms": max_duration_ms} if max_duration_ms != 10000 else {}),
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
                    source_path=f"/project/{project_id}" if project_id else "/",
                ),
                project_id,
                poll=True,
            )
