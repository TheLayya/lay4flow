"""Synthetic extension login admission uses modern Flow and native Profile checks."""

import copy
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.browser.extension_login import ExtensionLogin
from src.services.browser.profile_transfer import ProfileTransfer, ProfileTransferError
from src.services.browser.profiles import ProfileManager
from src.services.flow.flow_rpc import FlowRPCError


def cookies(value="synthetic"):
    return [
        {
            "name": "SID",
            "value": value,
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "hostOnly": False,
            "session": True,
            "sameSite": "lax",
        },
        {
            "name": "__Secure-OSID",
            "value": value,
            "domain": "flow.google.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "hostOnly": True,
            "session": True,
            "sameSite": "none",
        },
    ]


def payload():
    return {
        "google_cookies": json.dumps(cookies()),
        "browser_identity": {
            "user_agent": "Synthetic Chromium",
            "language": "en-US",
            "languages": ["en-US", "en"],
            "timezone": "UTC",
            "platform": "Linux x86_64",
            "width": 1280,
            "height": 720,
        },
        # Obsolete/untrusted inputs cannot choose local runtime settings.
        "session_token": "ignored-legacy-session",
        "fingerprint_seed": 1,
        "browser_profile_source": "cookies",
        "proxy_url": "http://untrusted.invalid:80",
    }


class ExtensionLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        manager = ProfileManager(
            Path(self.directory.name) / "profiles", SimpleNamespace(options={})
        )
        self.tokens = SimpleNamespace(
            db=object(), profile_refresher=SimpleNamespace(manager=manager)
        )
        self.transfer = ProfileTransfer(self.tokens)
        self.tokens._profile_transfer = self.transfer
        self.import_bundle = AsyncMock(
            return_value={"success": True, "email": "verified@example.test", "token_id": 7}
        )
        self.transfer.import_bundle = self.import_bundle
        self.selected = AsyncMock(return_value=(17, "socks5h://residential.invalid:1080"))
        self.context = SimpleNamespace(cookies=AsyncMock(return_value=cookies("rotated")))
        self.routed = []

        @asynccontextmanager
        async def request(raw_cookies, identity, proxy):
            self.routed.append((json.loads(raw_cookies), copy.deepcopy(identity), proxy))
            yield self.context

        self.session = AsyncMock(
            return_value={
                "email": "Verified@Example.Test",
                "credits": 100,
                "user_paygate_tier": "PAYGATE_TIER_ONE",
            }
        )
        self.patches = [
            patch(
                "src.services.browser.extension_login.ProxyManager",
                return_value=SimpleNamespace(select_account_proxy=self.selected),
            ),
            patch("src.services.browser.extension_login.snapshot_http_request", request),
            patch("src.services.browser.extension_login.read_flow_session", self.session),
            patch(
                "src.services.browser.extension_login._runtime",
                return_value={"cloakbrowser": "0.synthetic", "playwright": "1.synthetic"},
            ),
            patch(
                "src.services.browser.profile_transfer._runtime",
                return_value={"cloakbrowser": "0.synthetic", "playwright": "1.synthetic"},
            ),
        ]
        for context in self.patches:
            context.start()
            self.addCleanup(context.stop)
        self.login = ExtensionLogin(self.tokens)

    async def test_current_flow_discovers_identity_on_fixed_proxy_and_reuses_native_transfer(self):
        result = await self.login.import_login(payload(), proxy_pool_id=17)
        self.assertEqual(
            result,
            {
                "success": True,
                "action": "added",
                "email": "verified@example.test",
                "token_id": 7,
            },
        )
        self.selected.assert_awaited_once_with(17, None)
        self.session.assert_awaited_once_with(self.context)
        self.assertEqual(self.routed[0][2], "socks5h://residential.invalid:1080")
        bundle = self.import_bundle.await_args.args[0]
        # Run the real public bundle validator so invalid Chrome/Playwright
        # cookie conversions cannot be hidden by a mocked persistence call.
        account, browser, seed, state = self.transfer._validate(bundle)
        self.assertEqual(account["email"], "verified@example.test")
        self.assertEqual(account["submission_proxy_mode"], "fixed")
        self.assertTrue(account["is_active"])
        self.assertTrue(10000 <= seed <= 99999)
        self.assertEqual(browser["launch_options"], {})
        self.assertTrue(all(row["value"] == "rotated" for row in state["cookies"]))
        self.assertEqual(state["cookies"][1]["domain"], "flow.google.com")
        self.assertEqual(state["cookies"][1]["sameSite"], "None")
        self.assertTrue(all(row["expires"] == -1 for row in state["cookies"]))
        self.assertNotIn("ignored-legacy-session", json.dumps(bundle))
        self.assertEqual(
            self.import_bundle.await_args.kwargs,
            {"proxy_pool_id": 17, "proxy_url": "socks5h://residential.invalid:1080"},
        )

    async def test_failure_or_identity_mismatch_never_hands_cookies_to_profile_import(self):
        for session in (
            FlowRPCError("login_required", "flow_bootstrap"),
            {"email": "other@example.test", "credits": 100},
        ):
            self.import_bundle.reset_mock()
            self.session.side_effect = session if isinstance(session, Exception) else None
            self.session.return_value = session
            value = payload()
            value["email"] = "expected@example.test"
            with self.subTest(session=session):
                with self.assertRaises((FlowRPCError, ValueError)):
                    await self.login.import_login(value, proxy_pool_id=17)
                self.import_bundle.assert_not_awaited()

    async def test_invalid_or_unscoped_credentials_stop_before_any_network_or_profile_mutation(
        self,
    ):
        for values in (
            {"google_cookies": ""},
            {
                "google_cookies": json.dumps(
                    [{"name": "SID", "value": "x", "domain": "evil.invalid"}]
                )
            },
            {"browser_identity": {}},
            {"google_cookies": "x" * 524_289},
        ):
            value = {**payload(), **values}
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError):
                    await self.login.import_login(value, proxy_pool_id=17)
        self.selected.assert_not_awaited()
        self.session.assert_not_awaited()
        self.import_bundle.assert_not_awaited()

    async def test_duplicate_or_failed_cold_profile_check_is_not_reported_as_import_success(self):
        failure = ProfileTransferError("account_exists", "synthetic duplicate", 409)
        self.import_bundle.side_effect = failure
        with self.assertRaises(ProfileTransferError) as caught:
            await self.login.import_login(payload(), proxy_pool_id=17)
        self.assertIs(caught.exception, failure)


if __name__ == "__main__":
    unittest.main()
