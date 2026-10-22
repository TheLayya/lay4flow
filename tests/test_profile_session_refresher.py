import asyncio
import unittest
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.services.browser.profile_session_refresher import (
    ProfileSessionRefresher,
    _browser_cookie_rows,
)
from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.profiles import ProfileNotReadyError
from src.services.flow.flow_rpc import FlowRPCError


class ProfileSessionRefresherTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_context_is_scoped_and_preserves_account_isolation(self):
        manager = SimpleNamespace(
            spec=AsyncMock(return_value=SimpleNamespace(profile_dir="synthetic-profile")),
            credential_session=Mock(side_effect=AssertionError("opened a second browser")),
            mark_blocked=AsyncMock(),
        )
        verifier = SimpleNamespace(verify_environment=AsyncMock(return_value="environment_stable"))
        refresher = ProfileSessionRefresher(manager, verifier, object())
        context = object()
        token = SimpleNamespace(email="user@example.com")
        refresher.refresh_in_context = AsyncMock(
            return_value={"_validated_session": {"at": None, "credits": 1}, "google_cookies": "{}"}
        )

        async def background():
            await release.wait()
            return refresher.has_active_context(token.email)

        release = asyncio.Event()
        unrelated = asyncio.create_task(background())
        self.assertFalse(refresher.has_active_context(token.email))
        async with refresher.use_context(" USER@example.com ", context):
            self.assertTrue(refresher.has_active_context(token.email))
            self.assertFalse(refresher.has_active_context("other@example.com"))
            self.assertEqual(
                await refresher.refresh(token),
                {"_validated_session": {"at": None, "credits": 1}, "google_cookies": "{}"},
            )
            release.set()
            self.assertFalse(await unrelated)
        self.assertFalse(refresher.has_active_context(token.email))
        refresher.refresh_in_context.assert_awaited_once_with(token, context)
        verifier.verify_environment.assert_awaited_once_with(
            context, "synthetic-profile", allow_viewport_change=True
        )
        manager.credential_session.assert_not_called()

    async def test_active_context_environment_failure_does_not_read_credentials(self):
        manager = SimpleNamespace(
            spec=AsyncMock(return_value=SimpleNamespace(profile_dir="synthetic-profile")),
            mark_blocked=AsyncMock(),
        )
        refresher = ProfileSessionRefresher(
            manager,
            SimpleNamespace(verify_environment=AsyncMock(return_value="environment_changed")),
            object(),
        )
        refresher.refresh_in_context = AsyncMock()
        token = SimpleNamespace(email="user@example.com")
        async with refresher.use_context(token.email, object()):
            with self.assertRaises(ProfileNotReadyError) as caught:
                await refresher.refresh(token)
            self.assertEqual(caught.exception.reason, "environment_changed")
        refresher.refresh_in_context.assert_not_awaited()
        manager.mark_blocked.assert_awaited_once_with(token.email, "environment_changed")

    async def test_active_context_passes_existing_session_intent_only_when_requested(self):
        manager = SimpleNamespace(
            spec=AsyncMock(return_value=SimpleNamespace(profile_dir="synthetic-profile")),
            mark_blocked=AsyncMock(),
        )
        refresher = ProfileSessionRefresher(
            manager,
            SimpleNamespace(verify_environment=AsyncMock(return_value="environment_stable")),
            object(),
        )
        context = object()
        token = SimpleNamespace(email="user@example.com")
        refresher.refresh_in_context = AsyncMock(
            return_value={"_validated_session": {"at": None, "credits": 1}}
        )
        async with refresher.use_context(token.email, context, accept_existing_session=True):
            await refresher.refresh(token, force=True)
        refresher.refresh_in_context.assert_awaited_once_with(
            token, context, force=True, accept_existing_session=True
        )

    async def test_refresh_in_context_uses_current_flow_probe_and_exports_google_cookies(self):
        context = SimpleNamespace(
            cookies=AsyncMock(
                return_value=[
                    {"name": "SID", "value": "google", "domain": ".google.com", "path": "/"}
                ]
            )
        )
        manager = SimpleNamespace(mark_blocked=AsyncMock())
        verifier = ProfileVerifier()
        token = SimpleNamespace(email="user@example.com")
        refresher = ProfileSessionRefresher(manager, verifier, object())
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(
                return_value={
                    "email": token.email,
                    "credits": 7,
                    "user_paygate_tier": "PAYGATE_TIER_ONE",
                }
            ),
        ):
            result = await refresher.refresh_in_context(
                token, context, force=True, accept_existing_session=True
            )
        self.assertEqual(
            result["_validated_session"],
            {"at": None, "at_expires": None, "credits": 7, "user_paygate_tier": "PAYGATE_TIER_ONE"},
        )
        self.assertIn("google_cookies", result)
        self.assertNotIn("session_token", result)
        manager.mark_blocked.assert_not_awaited()

    async def test_refresh_in_context_blocks_structured_flow_failure_without_oauth_or_navigation(
        self,
    ):
        context = SimpleNamespace(cookies=AsyncMock(return_value=[]), new_page=AsyncMock())
        manager = SimpleNamespace(mark_blocked=AsyncMock())
        verifier = SimpleNamespace(
            verify_account=AsyncMock(return_value=SimpleNamespace(reason="login_required"))
        )
        refresher = ProfileSessionRefresher(manager, verifier, object())
        with self.assertRaises(ProfileNotReadyError) as caught:
            await refresher.refresh_in_context(
                SimpleNamespace(email="user@example.com"), context, force=True
            )
        self.assertEqual(caught.exception.reason, "login_required")
        manager.mark_blocked.assert_awaited_once_with("user@example.com", "login_required")
        context.new_page.assert_not_awaited()

    async def test_refresh_force_and_normal_both_use_fresh_probe(self):
        context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        manager = SimpleNamespace(mark_blocked=AsyncMock())
        verifier = ProfileVerifier()
        refresher = ProfileSessionRefresher(manager, verifier, object())
        token = SimpleNamespace(email="user@example.com")
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(return_value={"email": token.email, "credits": 2}),
        ) as read:
            await refresher.refresh_in_context(token, context)
            await refresher.refresh_in_context(token, context, force=True)
        self.assertEqual(read.await_count, 2)

    async def test_bootstrap_returns_validated_flow_session_and_does_not_require_legacy_cookie(
        self,
    ):
        captured = {}
        context = SimpleNamespace(
            add_cookies=AsyncMock(),
            cookies=AsyncMock(return_value=[]),
            request=SimpleNamespace(get=AsyncMock()),
        )
        token_manager = SimpleNamespace(
            db=SimpleNamespace(get_proxy_config=AsyncMock(return_value=None))
        )
        token = SimpleNamespace(email="user@example.com")

        class Session:
            async def __aenter__(self):
                captured["probe"].set_context(context)
                result = await captured["probe"].verify(token.email)
                if result.get("reason"):
                    raise AssertionError(result["reason"])
                return context

            async def __aexit__(self, *args):
                return None

        class Profiles:
            def bootstrap_session(self, *args, **kwargs):
                captured["probe"] = kwargs["account_probe"]
                return Session()

            async def mark_verified(self, email):
                captured["verified"] = email

        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(return_value={"email": token.email, "credits": 3}),
        ):
            result = await ProfileSessionRefresher(
                Profiles(), ProfileVerifier(), token_manager
            ).bootstrap(token, '{"SID":"google"}', [])
        self.assertEqual(result["credits"], 3)
        self.assertEqual(captured["verified"], token.email)
        self.assertIsNone(result["at"])

    async def test_bootstrap_filters_deprecated_and_untrusted_cookie_rows(self):
        captured = {}
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock()))
        token = SimpleNamespace(email="user@example.com")
        token_manager = SimpleNamespace(
            flow_client=SimpleNamespace(
                proxy_manager=SimpleNamespace(
                    resolve_account_proxy=AsyncMock(return_value="http://proxy.invalid:1")
                )
            )
        )

        @asynccontextmanager
        async def session(email, verifier, cookies, **kwargs):
            captured["cookies"] = cookies
            kwargs["account_probe"].validated_session = {"at": None, "credits": 1}
            yield context

        manager = SimpleNamespace(bootstrap_session=session, mark_verified=AsyncMock())
        await ProfileSessionRefresher(manager, ProfileVerifier(), token_manager).bootstrap(
            token,
            '{"SID":"google"}',
            [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "legacy",
                    "domain": "labs.google",
                    "path": "/",
                    "secure": True,
                },
                {"name": "SID", "value": "bad-host", "domain": "untrusted.invalid", "path": "/"},
                {
                    "name": "OSID",
                    "value": "flow",
                    "domain": "flow.google.com",
                    "path": "/",
                    "hostOnly": True,
                },
            ],
        )
        self.assertEqual([row["name"] for row in captured["cookies"]], ["SID", "OSID"])
        self.assertEqual(captured["cookies"][1]["domain"], "flow.google.com")

    async def test_background_refresh_borrows_registered_viewer_context(self):
        context = object()
        entered, release = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def provider(token):
            self.assertEqual(token.email, "user@example.com")
            yield context

        manager = SimpleNamespace(
            spec=AsyncMock(return_value=SimpleNamespace(profile_dir="synthetic-profile")),
            credential_session=Mock(side_effect=AssertionError("opened a second browser")),
            mark_blocked=AsyncMock(),
        )
        verifier = SimpleNamespace(verify_environment=AsyncMock(return_value="environment_stable"))
        refresher = ProfileSessionRefresher(manager, verifier, object(), provider)
        token = SimpleNamespace(email="user@example.com")

        async def refresh_in_context(current_token, current_context):
            self.assertIs(current_context, context)
            entered.set()
            await release.wait()
            return {"_validated_session": {"credits": 1}}

        refresher.refresh_in_context = refresh_in_context
        operation = asyncio.create_task(refresher.refresh(token))
        await entered.wait()
        self.assertFalse(refresher.has_active_context(token.email))
        release.set()
        self.assertEqual(await operation, {"_validated_session": {"credits": 1}})
        manager.credential_session.assert_not_called()

    async def test_refresh_uses_bound_proxy_and_closes_owned_profile_on_failure(self):
        token = SimpleNamespace(email="user@example.com")
        context = object()
        closed = Mock()

        @asynccontextmanager
        async def session(email, verifier, *, proxy_url):
            self.assertEqual(email, token.email)
            self.assertEqual(proxy_url, "http://synthetic.proxy:8080")
            try:
                yield context
            finally:
                closed()

        manager = SimpleNamespace(credential_session=session, mark_blocked=AsyncMock())
        proxy = SimpleNamespace(
            resolve_account_proxy=AsyncMock(return_value="http://synthetic.proxy:8080")
        )
        token_manager = SimpleNamespace(flow_client=SimpleNamespace(proxy_manager=proxy))
        refresher = ProfileSessionRefresher(manager, ProfileVerifier(), token_manager)
        with patch(
            "src.services.browser.token_account_probe.read_flow_session",
            AsyncMock(side_effect=FlowRPCError("transient_error", "flow_credits")),
        ):
            with self.assertRaises(ProfileNotReadyError) as caught:
                await refresher.refresh(token, force=True)
        self.assertEqual(caught.exception.profile_network_step, "flow_credits")
        proxy.resolve_account_proxy.assert_awaited_once_with(token)
        closed.assert_called_once()

    async def test_warm_profile_verifies_environment_before_fresh_rpc(self):
        token = SimpleNamespace(email="user@example.com")
        context = object()

        @asynccontextmanager
        async def session(email, *, proxy_url):
            yield context

        manager = SimpleNamespace(
            warm_slots=1,
            warm_session=session,
            backend=SimpleNamespace(options={"headless": False}),
            spec=AsyncMock(return_value=SimpleNamespace(profile_dir="synthetic-profile")),
            mark_blocked=AsyncMock(),
        )
        verifier = SimpleNamespace(verify_environment=AsyncMock(return_value="environment_changed"))
        token_manager = SimpleNamespace(
            flow_client=SimpleNamespace(
                proxy_manager=SimpleNamespace(
                    resolve_account_proxy=AsyncMock(return_value="http://proxy.invalid:1")
                )
            )
        )
        refresher = ProfileSessionRefresher(manager, verifier, token_manager)
        refresher.refresh_in_context = AsyncMock()
        with self.assertRaises(ProfileNotReadyError):
            await refresher.refresh(token)
        verifier.verify_environment.assert_awaited_once_with(
            context, "synthetic-profile", allow_viewport_change=True
        )
        refresher.refresh_in_context.assert_not_awaited()

    async def test_bootstrap_rejection_closes_profile_without_marking_verified(self):
        from src.services.browser.profiles import ProfileManager

        for reason in ("login_required", "identity_mismatch"):
            with self.subTest(reason=reason), TemporaryDirectory() as root:
                context = SimpleNamespace(
                    add_cookies=AsyncMock(), request=SimpleNamespace(get=AsyncMock())
                )
                backend = SimpleNamespace(open=AsyncMock(return_value=context), close=AsyncMock())
                manager = ProfileManager(root, backend)
                manager.mark_verified = AsyncMock()
                verifier = ProfileVerifier()
                verifier.verify_environment = AsyncMock(return_value="environment_stable")
                token_manager = SimpleNamespace(
                    flow_client=SimpleNamespace(
                        proxy_manager=SimpleNamespace(
                            resolve_account_proxy=AsyncMock(return_value="http://proxy.invalid:1")
                        )
                    )
                )
                with patch(
                    "src.services.browser.token_account_probe.read_flow_session",
                    AsyncMock(side_effect=FlowRPCError(reason, "flow_identity")),
                ):
                    with self.assertRaises(ProfileNotReadyError) as caught:
                        await ProfileSessionRefresher(manager, verifier, token_manager).bootstrap(
                            SimpleNamespace(email="user@example.com"), '{"SID":"google"}', []
                        )
                self.assertEqual(caught.exception.reason, reason)
                manager.mark_verified.assert_not_awaited()
                backend.close.assert_awaited_once_with(context)

    async def test_browser_cookie_rows_preserves_host_only_and_domain_scope(self):
        rows = _browser_cookie_rows(
            [
                {
                    "name": "host",
                    "value": "one",
                    "domain": "labs.google",
                    "path": "/",
                    "hostOnly": True,
                    "secure": True,
                    "httpOnly": True,
                },
                {
                    "name": "domain",
                    "value": "two",
                    "domain": ".google.com",
                    "path": "/",
                    "hostOnly": False,
                    "secure": True,
                    "httpOnly": True,
                },
            ]
        )
        self.assertEqual(rows[0]["domain"], "labs.google")
        self.assertEqual(rows[1]["domain"], ".google.com")
        self.assertNotIn("url", rows[0])


if __name__ == "__main__":
    unittest.main()
