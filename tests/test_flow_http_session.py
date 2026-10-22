"""Persisted Flow sessions run concurrently without launching account browsers."""

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.config import config
from src.core.database import Database
from src.core.models import Token
from src.services.browser.environment import EnvironmentSnapshot
from src.services.browser.flow_generation import FlowProfileGeneration
from src.services.browser.flow_session import account_http_session, save_session_cookies
from src.services.browser.profile_session_refresher import ProfileSessionRefresher
from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.profiles import ProfileNotReadyError
from src.services.flow.flow_rpc import FlowRPCError
from src.services.flow.google_cookies import GoogleCookieJar
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler
from src.services.proxy_manager import ProxyManager
from src.services.token_manager import TokenManager


def cookie(name, value):
    return {"name": name, "value": value, "domain": ".google.com", "path": "/", "secure": True}


class FlowHTTPSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = Database(str(self.root / "fixture.db"))
        await self.db.init_db()
        self.proxy = ProxyManager(self.db)
        self.client = FlowClient(self.proxy, self.db)
        self.tokens = TokenManager(self.db, self.client)
        self.client.token_manager = self.tokens
        self.browser_open = Mock(side_effect=AssertionError("account browser launched"))
        self.manager = SimpleNamespace(
            spec=AsyncMock(return_value=SimpleNamespace(profile_dir=self.root)),
            credential_session=self.browser_open,
            mark_blocked=AsyncMock(),
        )
        self.refresher = ProfileSessionRefresher(self.manager, ProfileVerifier(), self.tokens)
        self.tokens.profile_refresher = self.refresher
        identity = EnvironmentSnapshot(
            "Chrome/150.0.0.0", "en-US", ("en-US",), "UTC", "Win32", 1280, 720
        )
        (self.root / "environment_snapshot.json").write_text(identity.to_json(), encoding="utf-8")
        token_id = await self.db.add_token(
            Token(
                st="profile-session:fixture",
                email="fixture@example.invalid",
                browser_profile_source="cloak",
                google_cookies=json.dumps([cookie("SID", "before")]),
            )
        )
        self.token = await self.db.get_token(token_id)
        self.opened = self.closed = self.active = self.peak = 0

        @asynccontextmanager
        async def snapshot(rows, identity, proxy):
            self.opened += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.assertEqual(identity["user_agent"], "Chrome/150.0.0.0")
            self.assertEqual(proxy, "")
            context = SimpleNamespace(
                cookies=AsyncMock(return_value=GoogleCookieJar(rows).export())
            )
            try:
                await asyncio.sleep(0)
                yield context
            finally:
                self.closed += 1
                self.active -= 1

        self.enterContext(
            patch("src.services.browser.flow_session.snapshot_http_request", snapshot)
        )

    async def test_100_video_tasks_submit_poll_and_read_media_with_no_browser(self):
        self.client._get_recaptcha_token = AsyncMock(return_value=("captcha", "remote-lease"))
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        self.tokens.mark_profile_validated(self.token)

        async def rpc(context, rpcid, payload, **kwargs):
            await asyncio.sleep(0)
            return [8, 1, 2, 2] if rpcid == "nzlxg" else [[], "project"]

        async def worker():
            generation = FlowProfileGeneration(self.client, self.tokens, self.token.model_copy())
            async with generation.session():
                await generation.generate_video("text", "project", "prompt", "veo", "16:9")
                for _ in range(3):
                    await generation.check_video_status(
                        [{"name": "operation", "projectId": "project"}]
                    )
                await generation.get_media("media")

        with (
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
            patch("src.services.flow.angular_video.parse_video_response", return_value={}),
            patch("src.services.flow.angular_video.parse_media_response", return_value={}),
        ):
            await asyncio.gather(*(worker() for _ in range(100)))
        self.assertEqual((self.opened, self.closed, self.active), (100, 100, 0))
        self.assertGreater(self.peak, 1)
        self.browser_open.assert_not_called()
        self.assertEqual(self.client._get_recaptcha_token.await_count, 100)

    async def test_pipeline_reuses_session_without_leaking_context_across_yields(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = self.client

        async def pipeline():
            first = self.client._modern_generation()
            yield first
            self.assertIs(self.client._modern_generation(), first)
            yield first

        scoped = handler._account_generation_pipeline(pipeline(), self.token, "")
        first = await anext(scoped)
        self.assertIsNone(self.client._modern_generation())
        self.assertIs(await anext(scoped), first)
        await scoped.aclose()
        self.assertEqual((self.opened, self.closed), (1, 1))
        self.assertIsNone(self.client._account_generation_ctx.get())

    async def test_read_only_media_lookup_survives_status_changes_after_submission(self):
        generation = FlowProfileGeneration(self.client, self.tokens, self.token.model_copy())
        async with generation.session():
            await self.db.update_token(self.token.id, is_active=False, ban_reason="refresh_failed")
            proxy = await self.tokens.resolve_account_proxy(self.token)
            await generation._check_binding(proxy, read_only=True)
            with self.assertRaises(FlowRPCError) as caught:
                await generation._check_binding(proxy, read_only=False)
            self.assertEqual(caught.exception.reason, "account_changed")

    async def test_read_only_media_lookup_logs_only_changed_field_names(self):
        generation = FlowProfileGeneration(self.client, self.tokens, self.token.model_copy())
        with patch("src.services.browser.flow_generation.debug_logger.log_warning") as warning:
            await self.db.update_token(self.token.id, browser_profile_epoch="replacement")
            with self.assertRaises(FlowRPCError):
                await generation._check_binding("", read_only=True)
        self.assertIn("changed_fields=browser_profile_epoch", warning.call_args.args[0])

    async def test_read_only_media_lookup_still_blocks_epoch_or_proxy_changes(self):
        generation = FlowProfileGeneration(self.client, self.tokens, self.token.model_copy())
        for field, value in (
            ("browser_profile_epoch", "replacement"),
            ("email", "other@example.invalid"),
            ("st", "replacement-login"),
            ("browser_profile_source", "cookies"),
            ("proxy_url", "http://other.invalid:8080"),
        ):
            with self.subTest(field=field):
                await self.db.update_token(self.token.id, **{field: value})
                with self.assertRaises(FlowRPCError) as caught:
                    await generation._check_binding("", read_only=True)
                self.assertEqual(caught.exception.reason, "account_changed")
                await self.db.update_token(self.token.id, **{field: getattr(self.token, field)})
        await self.db.delete_token(self.token.id)
        with self.assertRaises(FlowRPCError) as caught:
            await generation._check_binding("", read_only=True)
        self.assertEqual(caught.exception.reason, "account_changed")

    async def test_i2v_collects_successful_media_after_background_cookie_and_status_update(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = self.client
        handler.db = SimpleNamespace(create_task=AsyncMock(), update_task=AsyncMock())
        handler._update_request_log_progress = AsyncMock()
        self.client._get_recaptcha_token = AsyncMock(return_value=("captcha", "remote-lease"))
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        self.tokens.mark_profile_validated(self.token)
        pending = ["media-fixture", "project", "workflow", None, None, [None] * 8 + [[2]]]
        complete = [*pending[:5], [None] * 8 + [[3]]]
        media = [*complete, None, [[None] * 8 + ["https://media.example.invalid/video.mp4"]]]
        rpcids = []

        async def rpc(context, rpcid, payload, **kwargs):
            rpcids.append(rpcid)
            if rpcid == "maseQ":
                return [["first-frame"]]
            if rpcid == "HTrJv":
                return []
            if rpcid == "nzlxg":
                return [9, 1, 2, 2]
            if kwargs["network_step"] == "flow_generate_video":
                return [None, 8, None, [pending]]
            if kwargs["network_step"] == "flow_poll_video":
                return [None, 8, [complete]]
            self.assertEqual(rpcid, "as29s")
            return media

        generation_result, response_state = {}, {}
        pipeline = handler._handle_video_generation(
            self.token,
            "project",
            {
                "video_type": "i2v",
                "min_images": 1,
                "max_images": 2,
                "model_key": "veo",
                "aspect_ratio": "16:9",
            },
            "fixture prompt",
            [b"synthetic first frame"],
            True,
            generation_result=generation_result,
            response_state=response_state,
        )
        with (
            patch("src.services.browser.flow_generation.flow_rpc", side_effect=rpc),
            patch("src.services.flow.angular_video.resolve_video_usage", return_value="veo"),
            patch.object(
                config,
                "_config",
                {
                    **config._config,
                    "cache": {"enabled": False},
                    "flow": {
                        **config._config.get("flow", {}),
                        "poll_interval": 0,
                        "max_poll_attempts": 2,
                    },
                },
            ),
        ):
            chunks = []
            async for chunk in handler._account_generation_pipeline(pipeline, self.token, ""):
                chunks.append(chunk)
                if "0%" in chunk:
                    await self.db.update_token(
                        self.token.id,
                        google_cookies=json.dumps([cookie("SID", "rotated")]),
                        is_active=False,
                        ban_reason="refresh_failed",
                        refresh_status="retrying",
                    )
        self.assertTrue(generation_result["success"], "".join(chunks))
        self.assertEqual(response_state["url"], "https://media.example.invalid/video.mp4")
        self.assertEqual(rpcids.count("as29s"), 1)
        self.client._get_recaptcha_token.assert_awaited_once_with(
            "project", action="VIDEO_GENERATION", token_id=self.token.id
        )
        self.assertEqual((self.opened, self.closed), (1, 1))
        self.browser_open.assert_not_called()
        self.assertEqual((await self.db.get_token(self.token.id)).ban_reason, "refresh_failed")

    async def test_refresh_prefers_http_and_does_not_launch_for_network_or_identity_error(self):
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(return_value={"email": self.token.email, "credits": 9}),
        ):
            result = await self.refresher.refresh(self.token)
        self.assertEqual(result["_validated_session"]["credits"], 9)
        self.browser_open.assert_not_called()
        for reason in ("transient_error", "identity_mismatch"):
            with patch(
                "src.services.browser.token_account_probe.read_flow_session",
                AsyncMock(side_effect=FlowRPCError(reason, "flow_bootstrap")),
            ):
                with self.assertRaises(ProfileNotReadyError) as caught:
                    await self.refresher.refresh(self.token)
                self.assertEqual(caught.exception.reason, reason)
        self.browser_open.assert_not_called()

    async def test_auth_failure_requests_only_one_browser_recovery(self):
        @asynccontextmanager
        async def recovery(*args, **kwargs):
            yield SimpleNamespace(cookies=AsyncMock(return_value=[cookie("SID", "recovered")]))

        self.manager.credential_session = Mock(side_effect=recovery)
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(
                side_effect=[
                    FlowRPCError("login_required", "flow_bootstrap"),
                    {"email": self.token.email, "credits": 9},
                ]
            ),
        ):
            result = await self.refresher.refresh(self.token)
        self.manager.credential_session.assert_called_once()
        self.assertIn("recovered", result["google_cookies"])

    async def test_cookie_merge_preserves_parallel_rotations_deletions_and_new_login(self):
        before = self.token.google_cookies
        await self.db.update_token(
            self.token.id,
            google_cookies=json.dumps([cookie("SID", "parallel"), cookie("OTHER", "parallel")]),
        )
        await save_session_cookies(
            self.tokens, self.token, before, [cookie("SID", "late"), cookie("NEW", "new")]
        )
        saved = await self.db.get_token(self.token.id)
        values = {r["name"]: r["value"] for r in GoogleCookieJar(saved.google_cookies).export()}
        self.assertEqual(values, {"SID": "parallel", "OTHER": "parallel", "NEW": "new"})
        await save_session_cookies(
            self.tokens, saved, saved.google_cookies, [cookie("SID", "parallel")]
        )
        self.assertEqual(
            {
                r["name"]
                for r in GoogleCookieJar(
                    (await self.db.get_token(saved.id)).google_cookies
                ).export()
            },
            {"SID"},
        )
        old = await self.db.get_token(saved.id)
        await self.tokens.update_token(
            saved.id, google_cookies=json.dumps([cookie("SID", "manual-login")])
        )
        await save_session_cookies(
            self.tokens, old, old.google_cookies, [cookie("SID", "late-old-login")]
        )
        self.assertIn("manual-login", (await self.db.get_token(saved.id)).google_cookies)

    async def test_missing_identity_or_cookie_does_not_start_browser(self):
        for value in ("", "[]"):
            with self.assertRaises(FlowRPCError) as caught:
                async with account_http_session(
                    self.tokens, self.token.model_copy(update={"google_cookies": value}), ""
                ):
                    self.fail("missing cookies entered")
            self.assertEqual(caught.exception.reason, "login_required")
        (self.root / "environment_snapshot.json").unlink()
        with self.assertRaises(FlowRPCError):
            async with account_http_session(self.tokens, self.token, ""):
                self.fail("invalid snapshot entered")
        self.browser_open.assert_not_called()

    async def test_legacy_row_missing_cookie_recovers_once_even_with_validation_cache(self):
        await self.db.update_token(
            self.token.id, google_cookies="", browser_profile_status="ready", refresh_status="ready"
        )
        token = await self.db.get_token(self.token.id)
        self.tokens.mark_profile_validated(token)
        self.tokens._profile_session_fresh = Mock(return_value=True)
        self.refresher.refresh = AsyncMock(
            return_value={
                "_validated_session": {"at": None, "at_expires": None, "credits": 9},
                "google_cookies": json.dumps([cookie("SID", "recovered")]),
            }
        )
        valid = await self.tokens.ensure_valid_token(token)
        self.assertIn("recovered", valid.google_cookies)
        self.refresher.refresh.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
