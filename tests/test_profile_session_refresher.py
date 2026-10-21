import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.services.browser.profile_session_refresher import (
    ProfileSessionRefresher,
    _browser_cookie_rows,
)
from src.services.browser.profile_verifier import ProfileVerifier
from src.services.browser.profiles import ProfileNotReadyError
from src.services.browser.token_account_probe import SESSION_URL
from src.services.token_manager import TokenManager


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
        refresher.refresh_in_context = AsyncMock(return_value={"session_token": "synthetic"})

        async def background():
            await release.wait()
            return refresher.has_active_context(token.email)

        release = asyncio.Event()
        unrelated = asyncio.create_task(background())
        self.assertFalse(refresher.has_active_context(token.email))
        async with refresher.use_context(" USER@example.com ", context):
            self.assertTrue(refresher.has_active_context(token.email))
            self.assertFalse(refresher.has_active_context("other@example.com"))
            self.assertEqual(await refresher.refresh(token), {"session_token": "synthetic"})
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

    async def test_background_refresh_borrows_registered_viewer_context(self):
        context = object()
        entered = asyncio.Event()
        release = asyncio.Event()

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
            return {"session_token": "synthetic"}

        refresher.refresh_in_context = refresh_in_context
        operation = asyncio.create_task(refresher.refresh(token))
        await entered.wait()
        self.assertFalse(refresher.has_active_context(token.email))
        release.set()
        self.assertEqual(await operation, {"session_token": "synthetic"})
        manager.credential_session.assert_not_called()

    def test_browser_cookie_rows_uses_domain_without_url_or_leading_dot_for_host_only(self):
        rows = _browser_cookie_rows(
            [
                {
                    "name": "host",
                    "value": "one",
                    "domain": "labs.google",
                    "path": "/fx/api/auth",
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
        self.assertNotIn("url", rows[0])
        self.assertEqual(rows[1]["domain"], ".google.com")
        self.assertNotIn("url", rows[1])

    async def test_refresh_visits_session_endpoint_and_returns_cookie(self):
        page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            cookies=AsyncMock(
                return_value=[
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "rotated",
                        "domain": "labs.google",
                        "path": "/",
                    }
                ]
            ),
        )

        class Manager:
            def __init__(self):
                self.context = None

            def set_context(self, context):
                self.context = context

            async def verify_environment(self, context, profile_dir):
                self.context = context
                return "environment_stable"

            async def verify_account(self, expected_email, probe):
                return SimpleNamespace(reason="account_ready")

        class Profiles:
            def __init__(self):
                self.verifier = Manager()

            async def __aenter__(self):
                return context

            async def __aexit__(self, *args):
                return None

        profile_manager = SimpleNamespace(
            credential_session=lambda *args, **kwargs: Profiles(),
            mark_blocked=AsyncMock(),
        )
        token_manager = SimpleNamespace(
            db=SimpleNamespace(get_proxy_config=AsyncMock(return_value=None))
        )
        token = SimpleNamespace(email="user@example.com")
        result = await ProfileSessionRefresher(profile_manager, Manager(), token_manager).refresh(
            token
        )
        self.assertEqual(result, {"session_token": "rotated"})
        page.goto.assert_awaited_once_with(
            "https://labs.google/fx/api/auth/session", wait_until="domcontentloaded"
        )
        page.close.assert_awaited_once()

    def browser_refresh(self, *, expires=None, email="user@example.com", status=200):
        payload = {
            "access_token": "navigation-at",
            "expires": (expires or (datetime.now(timezone.utc) + timedelta(hours=2))).isoformat(),
            "user": {"email": email},
        }
        navigation = SimpleNamespace(
            url=SESSION_URL, status=status, json=AsyncMock(return_value=payload)
        )
        page = SimpleNamespace(goto=AsyncMock(return_value=navigation), close=AsyncMock())
        renewed = {
            **payload,
            "access_token": "renewed-at",
            "expires": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
            "user": {"email": "user@example.com"},
        }

        async def get(url, **kwargs):
            value = renewed if url == SESSION_URL else {"credits": 7}
            return SimpleNamespace(status=200, json=AsyncMock(return_value=value))

        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            cookies=AsyncMock(
                return_value=[
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "navigation-st",
                        "domain": "labs.google",
                        "path": "/",
                    }
                ]
            ),
            request=SimpleNamespace(get=AsyncMock(side_effect=get)),
        )
        manager = SimpleNamespace(mark_blocked=AsyncMock())
        token_manager = TokenManager(
            object(), SimpleNamespace(api_base_url="https://flow.google.com/api")
        )
        token_manager._validate_refresh_session = AsyncMock(
            side_effect=AssertionError("must use canonical browser context")
        )
        refresher = ProfileSessionRefresher(manager, ProfileVerifier(), token_manager)
        refresher._renew_session = AsyncMock(return_value="session_renewed")
        return refresher, context, navigation

    async def test_refresh_reuses_navigation_but_still_validates_credits(self):
        refresher, context, response = self.browser_refresh()
        result = await refresher.refresh_in_context(
            SimpleNamespace(email="user@example.com"), context
        )
        self.assertEqual(result["session_token"], "navigation-st")
        self.assertEqual(result["_validated_session"]["at"], "navigation-at")
        self.assertEqual(result["_validated_session"]["credits"], 7)
        response.json.assert_awaited_once()
        context.request.get.assert_awaited_once()
        self.assertEqual(
            context.request.get.call_args.args[0], "https://flow.google.com/api/credits"
        )
        self.assertEqual(
            context.request.get.call_args.kwargs["headers"]["Authorization"], "Bearer navigation-at"
        )
        refresher._renew_session.assert_not_awaited()
        refresher.token_manager._validate_refresh_session.assert_not_awaited()
        context.new_page.assert_awaited_once()
        context.new_page.return_value.close.assert_awaited_once()

    async def test_refresh_falls_back_when_navigation_response_is_unusable(self):
        for kind in ("absent", "redirect", "json_error", "json_shape"):
            with self.subTest(kind=kind):
                refresher, context, response = self.browser_refresh()
                page = context.new_page.return_value
                if kind == "absent":
                    page.goto.return_value = None
                elif kind == "redirect":
                    response.url = "https://accounts.google.com/signin"
                elif kind == "json_error":
                    response.json.side_effect = ValueError("synthetic-secret")
                else:
                    response.json.return_value = []
                result = await refresher.refresh_in_context(
                    SimpleNamespace(email="user@example.com"), context
                )
                self.assertEqual(result["_validated_session"]["at"], "renewed-at")
                self.assertEqual(context.request.get.await_args_list[0].args[0], SESSION_URL)
                self.assertEqual(context.request.get.await_count, 2)
                refresher._renew_session.assert_not_awaited()

    async def test_force_refresh_reads_new_session_after_oauth(self):
        refresher, context, response = self.browser_refresh()
        context.new_page.side_effect = AssertionError("skip the discarded pre-renewal navigation")
        result = await refresher.refresh_in_context(
            SimpleNamespace(email="user@example.com"), context, force=True
        )
        self.assertEqual(result["_validated_session"]["at"], "renewed-at")
        self.assertEqual(context.request.get.await_args_list[0].args[0], SESSION_URL)
        self.assertEqual(context.request.get.await_count, 2)
        response.json.assert_not_awaited()
        context.new_page.assert_not_awaited()
        refresher._renew_session.assert_awaited_once_with(context)

    async def test_automatic_oauth_discards_consumed_navigation_session(self):
        for status, minutes, expected_credits_calls in ((401, 120, 1), (200, 30, 2), (200, -1, 1)):
            with self.subTest(status=status, minutes=minutes):
                refresher, context, _ = self.browser_refresh(
                    expires=datetime.now(timezone.utc) + timedelta(minutes=minutes), status=status
                )
                result = await refresher.refresh_in_context(
                    SimpleNamespace(email="user@example.com"), context
                )
                self.assertEqual(result["_validated_session"]["at"], "renewed-at")
                urls = [call.args[0] for call in context.request.get.await_args_list]
                self.assertEqual(urls.count(SESSION_URL), 1)
                self.assertEqual(
                    urls.count("https://flow.google.com/api/credits"), expected_credits_calls
                )
                refresher._renew_session.assert_awaited_once_with(context)

    async def test_navigation_upstream_error_does_not_retry_session_or_oauth(self):
        for status in (429, 503):
            with self.subTest(status=status):
                refresher, context, response = self.browser_refresh(status=status)
                with self.assertRaises(ProfileNotReadyError) as caught:
                    await refresher.refresh_in_context(
                        SimpleNamespace(email="user@example.com"), context
                    )
                self.assertEqual(caught.exception.reason, "transient_error")
                context.request.get.assert_not_awaited()
                refresher._renew_session.assert_not_awaited()
                response.json.assert_not_awaited()

    async def test_navigation_identity_and_credits_failures_still_block_refresh(self):
        for failure, reason in (("identity", "identity_mismatch"), ("credits", "session_invalid")):
            with self.subTest(failure=failure):
                refresher, context, _ = self.browser_refresh(
                    email="other@example.com" if failure == "identity" else "user@example.com"
                )
                if failure == "credits":
                    context.request.get.side_effect = None
                    context.request.get.return_value = SimpleNamespace(status=401)
                    refresher._renew_session.return_value = "login_required"
                    reason = "login_required"
                with self.assertRaises(ProfileNotReadyError) as caught:
                    await refresher.refresh_in_context(
                        SimpleNamespace(email="user@example.com"), context
                    )
                self.assertEqual(caught.exception.reason, reason)
                refresher.manager.mark_blocked.assert_awaited_once_with("user@example.com", reason)
                refresher.token_manager._validate_refresh_session.assert_not_awaited()

    async def test_oauth_probe_requires_explicit_login_evidence(self):
        for path, text, expected in (
            ("/v3/signin/accountchooser", "Choose an account Signed out", "login_required"),
            ("/v3/signin/accountchooser", "Choose an account", "transient_error"),
            ("/v3/signin/challenge/pwd", "Enter your password", "login_required"),
            ("/fx/zh/404", "", "session_renewed"),
            ("/", "", "session_renewed"),
        ):
            with self.subTest(path=path, text=text):
                host = (
                    "labs.google"
                    if path.startswith("/fx")
                    else "flow.google.com"
                    if path == "/"
                    else "accounts.google.com"
                )
                page = SimpleNamespace(
                    url="https://" + host + path,
                    goto=AsyncMock(),
                    close=AsyncMock(),
                    wait_for_timeout=AsyncMock(),
                    locator=Mock(
                        return_value=SimpleNamespace(inner_text=AsyncMock(return_value=text))
                    ),
                )
                context = SimpleNamespace(
                    new_page=AsyncMock(return_value=page),
                    request=SimpleNamespace(
                        get=AsyncMock(
                            return_value=SimpleNamespace(
                                status=200, json=AsyncMock(return_value={"csrfToken": "synthetic"})
                            )
                        ),
                        post=AsyncMock(
                            return_value=SimpleNamespace(
                                status=200,
                                json=AsyncMock(
                                    return_value={"url": "https://accounts.google.com/oauth"}
                                ),
                            )
                        ),
                    ),
                )
                refresher = ProfileSessionRefresher(object(), object(), object())
                self.assertEqual(await refresher._renew_session(context), expected)
                page.close.assert_awaited_once()

    async def test_forced_renewal_exposes_only_the_failed_network_step(self):
        context = SimpleNamespace(
            cookies=AsyncMock(return_value=[]),
            request=SimpleNamespace(get=AsyncMock(side_effect=TimeoutError("synthetic-secret"))),
        )
        manager = SimpleNamespace(mark_blocked=AsyncMock())
        verifier = SimpleNamespace(verify_account=AsyncMock())
        refresher = ProfileSessionRefresher(manager, verifier, object())
        with self.assertRaises(ProfileNotReadyError) as caught:
            await refresher.refresh_in_context(
                SimpleNamespace(email="user@example.com"), context, force=True
            )
        self.assertEqual(caught.exception.reason, "transient_error")
        self.assertEqual(caught.exception.profile_network_step, "oauth_csrf")
        self.assertNotIn("synthetic-secret", str(caught.exception))
        verifier.verify_account.assert_not_awaited()

    async def test_invalid_session_probes_login_and_revalidates_renewal(self):
        for renewal, expected in (
            ("login_required", "login_required"),
            ("transient_error", "transient_error"),
            ("session_renewed", "success"),
        ):
            with self.subTest(renewal=renewal):
                page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
                context = SimpleNamespace(
                    new_page=AsyncMock(return_value=page),
                    cookies=AsyncMock(
                        return_value=[
                            {
                                "name": "__Secure-next-auth.session-token",
                                "value": "synthetic",
                                "domain": "labs.google",
                                "path": "/",
                            }
                        ]
                    ),
                )

                class Session:
                    async def __aenter__(self):
                        return context

                    async def __aexit__(self, *args):
                        return None

                manager = SimpleNamespace(
                    credential_session=lambda *args, **kwargs: Session(), mark_blocked=AsyncMock()
                )
                verifier = SimpleNamespace(
                    verify_account=AsyncMock(
                        side_effect=[
                            SimpleNamespace(reason="session_invalid"),
                            SimpleNamespace(reason="account_ready"),
                        ]
                    )
                )
                token_manager = SimpleNamespace(
                    db=SimpleNamespace(get_proxy_config=AsyncMock(return_value=None))
                )
                refresher = ProfileSessionRefresher(manager, verifier, token_manager)
                refresher._renew_session = AsyncMock(return_value=renewal)
                token = SimpleNamespace(email="user@example.com")
                if expected == "success":
                    self.assertEqual((await refresher.refresh(token))["session_token"], "synthetic")
                    self.assertEqual(verifier.verify_account.await_count, 2)
                    manager.mark_blocked.assert_not_awaited()
                else:
                    with self.assertRaises(ProfileNotReadyError) as caught:
                        await refresher.refresh(token)
                    self.assertEqual(caught.exception.reason, expected)
                    manager.mark_blocked.assert_awaited_once_with(token.email, expected)
                page.close.assert_awaited_once()

    async def test_missing_labs_session_cookie_uses_google_cookie_renewal(self):
        page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            cookies=AsyncMock(
                side_effect=[
                    [{"name": "SID", "value": "google", "domain": ".google.com", "path": "/"}],
                    [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "renewed",
                            "domain": "labs.google",
                            "path": "/",
                        }
                    ],
                    [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "renewed",
                            "domain": "labs.google",
                            "path": "/",
                        }
                    ],
                ]
            ),
        )

        class Session:
            async def __aenter__(self):
                return context

            async def __aexit__(self, *args):
                return None

        manager = SimpleNamespace(
            credential_session=lambda *args, **kwargs: Session(), mark_blocked=AsyncMock()
        )
        verifier = SimpleNamespace(
            verify_account=AsyncMock(return_value=SimpleNamespace(reason="account_ready"))
        )
        token_manager = SimpleNamespace(
            db=SimpleNamespace(get_proxy_config=AsyncMock(return_value=None))
        )
        refresher = ProfileSessionRefresher(manager, verifier, token_manager)
        refresher._renew_session = AsyncMock(return_value="session_renewed")
        result = await refresher.refresh(SimpleNamespace(email="user@example.com"))
        self.assertEqual(result["session_token"], "renewed")
        refresher._renew_session.assert_awaited_once_with(context)
        verifier.verify_account.assert_awaited_once()
        manager.mark_blocked.assert_not_awaited()

    async def test_bootstrap_converts_google_and_session_cookies_once(self):
        captured = {}
        context = SimpleNamespace(
            new_page=AsyncMock(side_effect=AssertionError("bootstrap must not open a UI page")),
            request=SimpleNamespace(get=AsyncMock()),
        )
        token_manager = SimpleNamespace(
            db=SimpleNamespace(get_proxy_config=AsyncMock(return_value=None))
        )
        token = SimpleNamespace(email="user@example.com")
        validated = {"st": "rotated", "at": "synthetic-at", "credits": 7}

        class Session:
            async def __aenter__(self):
                probe = captured["kwargs"]["account_probe"]
                self_test.assertIs(probe.token, token)
                self_test.assertIs(probe.token_manager, token_manager)
                probe.set_context(context)
                probe.validated_session = validated
                return context

            async def __aexit__(self, *args):
                return None

        class Profiles:
            def bootstrap_session(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return Session()

            async def mark_verified(self, email):
                captured["verified"] = email

        self_test = self
        result = await ProfileSessionRefresher(Profiles(), object(), token_manager).bootstrap(
            token,
            '{"SID": "google", "domain": "google.com"}',
            [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "session",
                    "domain": "labs.google",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "hostOnly": True,
                }
            ],
        )
        cookies = captured["args"][2]
        self.assertEqual(
            [cookie["name"] for cookie in cookies],
            ["SID", "__Secure-next-auth.session-token"],
        )
        self.assertEqual(cookies[0]["domain"], ".google.com")
        self.assertEqual(cookies[1]["domain"], "labs.google")
        self.assertEqual(captured["verified"], "user@example.com")
        self.assertIs(captured["kwargs"]["account_probe"].context, context)
        self.assertEqual(result, validated)
        self.assertIsNot(result, validated)
        context.new_page.assert_not_awaited()

    async def test_bootstrap_requires_browser_requests_before_marking_verified(self):
        @asynccontextmanager
        async def session(*args, **kwargs):
            yield SimpleNamespace(new_page=AsyncMock())

        manager = SimpleNamespace(bootstrap_session=session, mark_verified=AsyncMock())
        token_manager = SimpleNamespace(
            db=SimpleNamespace(get_proxy_config=AsyncMock(return_value=None)),
            _validate_refresh_session=AsyncMock(side_effect=AssertionError("side-channel API")),
        )
        with self.assertRaises(ProfileNotReadyError) as caught:
            await ProfileSessionRefresher(manager, object(), token_manager).bootstrap(
                SimpleNamespace(email="user@example.com"),
                '{"SID": "google", "domain": "google.com"}',
                [],
            )
        self.assertEqual(caught.exception.reason, "account_probe_error")
        manager.mark_verified.assert_not_awaited()
        token_manager._validate_refresh_session.assert_not_awaited()

    async def test_bootstrap_api_rejection_closes_profile_without_marking_verified(self):
        from src.services.browser.profiles import ProfileManager
        from src.services.token_manager import TokenManager

        for status, identity, expected in (
            (401, "user@example.com", "session_invalid"),
            (200, "other@example.com", "identity_mismatch"),
        ):
            with self.subTest(status=status, expected=expected), TemporaryDirectory() as root:
                rows = [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "synthetic-st",
                        "domain": "labs.google",
                        "path": "/",
                    }
                ]
                context = SimpleNamespace(
                    add_cookies=AsyncMock(),
                    cookies=AsyncMock(return_value=rows),
                    new_page=AsyncMock(side_effect=AssertionError("bootstrap UI probe")),
                    request=SimpleNamespace(
                        get=AsyncMock(
                            return_value=SimpleNamespace(
                                status=status,
                                json=AsyncMock(
                                    return_value={
                                        "access_token": "synthetic-at",
                                        "expires": (
                                            datetime.now(timezone.utc) + timedelta(hours=2)
                                        ).isoformat(),
                                        "user": {"email": identity},
                                    }
                                ),
                            )
                        ),
                    ),
                )
                backend = SimpleNamespace(open=AsyncMock(return_value=context), close=AsyncMock())
                manager = ProfileManager(root, backend)
                manager.mark_verified = AsyncMock()
                verifier = ProfileVerifier()
                verifier.verify_environment = AsyncMock(return_value="environment_stable")
                verifier.verify_ready = AsyncMock(side_effect=AssertionError("bootstrap UI gate"))
                token_manager = TokenManager(
                    SimpleNamespace(get_proxy_config=AsyncMock(return_value=None)),
                    SimpleNamespace(api_base_url="https://synthetic.invalid/v1"),
                )
                refresher = ProfileSessionRefresher(manager, verifier, token_manager)
                refresher._renew_session = AsyncMock(side_effect=AssertionError("bootstrap OAuth"))
                with self.assertRaises(ProfileNotReadyError) as caught:
                    await refresher.bootstrap(SimpleNamespace(email="user@example.com"), "", rows)
                self.assertEqual(caught.exception.reason, expected)
                manager.mark_verified.assert_not_awaited()
                backend.close.assert_awaited_once_with(context)
                context.new_page.assert_not_awaited()
                verifier.verify_ready.assert_not_awaited()
                refresher._renew_session.assert_not_awaited()

    async def test_refresh_without_rotated_cookie_fails_closed(self):
        page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page), cookies=AsyncMock(return_value=[])
        )

        class Profiles:
            async def mark_blocked(self, *args):
                return None

            async def __aenter__(self):
                return context

            async def __aexit__(self, *args):
                return None

        profile_manager = SimpleNamespace(
            credential_session=lambda *args, **kwargs: Profiles(),
            mark_blocked=AsyncMock(),
        )
        token = SimpleNamespace(email="user@example.com")
        with self.assertRaisesRegex(Exception, "profile is not ready: login_required"):
            token_manager = SimpleNamespace(
                db=SimpleNamespace(get_proxy_config=AsyncMock(return_value=None))
            )
            await ProfileSessionRefresher(profile_manager, object(), token_manager).refresh(token)
        page.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
