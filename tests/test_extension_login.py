"""Synthetic extension login admission uses modern Flow and native Profile checks."""

import copy
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.browser.cloak_backend import CloakBrowserBackend
from src.services.browser.environment import EnvironmentSnapshot
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
        self.transfer.existing_account = AsyncMock(return_value=None)
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
        self.assertTrue(account["auto_refresh_enabled"])
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

    async def test_prepared_import_keeps_original_proxy_and_rejects_changed_pool_snapshot(self):
        prepared = await self.login.prepare_import(payload())
        self.selected.assert_awaited_once_with()
        self.assertEqual(
            prepared["_extension_proxy_binding"],
            {"proxy_pool_id": 17, "proxy_url": "socks5h://residential.invalid:1080"},
        )
        self.selected.reset_mock()
        await self.login.import_prepared(prepared)
        self.selected.assert_awaited_once_with(17, "socks5h://residential.invalid:1080")
        self.selected.return_value = (17, "socks5h://changed.invalid:1080")
        self.session.reset_mock()
        with self.assertRaisesRegex(ValueError, "代理已变化"):
            await self.login.import_prepared(prepared)
        self.session.assert_not_awaited()

    async def test_direct_untrusted_payload_cannot_supply_the_queue_proxy_binding(self):
        value = payload()
        value["_extension_proxy_binding"] = {
            "proxy_pool_id": 999,
            "proxy_url": "socks5://untrusted.invalid:1080",
        }
        await self.login.import_login(value)
        self.selected.assert_awaited_once_with(None, None)

    async def retained_profile(self):
        email = "verified@example.test"
        manager = self.transfer.refresher.manager
        manager.backend = CloakBrowserBackend(headless=False)
        spec = await manager.spec(email)
        (spec.profile_dir / "Default").mkdir()
        metadata = json.loads((spec.profile_dir / "metadata.json").read_text(encoding="utf-8"))
        metadata["state"] = "starting"
        manager._write_metadata_file(spec.profile_dir / "metadata.json", metadata)
        identity = self.login._identity(payload()["browser_identity"])
        (spec.profile_dir / "environment_snapshot.json").write_text(
            EnvironmentSnapshot(
                **{**identity, "languages": tuple(identity["languages"])}
            ).to_json(),
            encoding="utf-8",
        )
        proxy = "socks5://original.invalid:1080"
        manager.backend._check_launch_settings(
            spec.profile_dir, manager.backend._launch_options(spec.profile_dir, proxy)
        )
        binding = self.transfer._transfer_binding(
            email,
            spec.fingerprint_seed,
            {"launch_options": self.transfer._launch_options()},
            proxy,
            28,
        )
        manager._write_metadata_file(
            spec.profile_dir / "profile_transfer.json",
            {"binding_sha256": binding, "state_imported": True},
        )
        self.entries = [
            SimpleNamespace(id=17, proxy_url="socks5://new.invalid:1080", enabled=True),
            SimpleNamespace(id=28, proxy_url=proxy, enabled=True),
        ]
        self.tokens.db = SimpleNamespace(
            list_proxy_pool=AsyncMock(side_effect=lambda: self.entries)
        )
        return spec, proxy

    async def test_retained_profile_original_exit_is_reverified_before_transfer(self):
        spec, proxy = await self.retained_profile()
        before = {
            name: (spec.profile_dir / name).read_bytes()
            for name in ["metadata.json", "profile_transfer.json", "launch_settings.sha256"]
        }
        self.selected.side_effect = [
            (17, "socks5://new.invalid:1080"),
            (28, proxy),
        ]
        result = await self.login.import_login(payload())
        self.assertTrue(result["success"])
        self.assertEqual([row[2] for row in self.routed], ["socks5://new.invalid:1080", proxy])
        self.assertEqual(self.session.await_count, 2)
        self.assertEqual(
            self.import_bundle.await_args.kwargs,
            {"proxy_pool_id": 28, "proxy_url": proxy, "repair_existing_login": True},
        )
        self.assertEqual(
            before,
            {name: (spec.profile_dir / name).read_bytes() for name in before},
        )

    async def test_retained_profile_refuses_disabled_or_missing_exit_without_rebinding(self):
        spec, _ = await self.retained_profile()
        original = (spec.profile_dir / "profile_transfer.json").read_bytes()
        for entries in (
            self.entries[:1],
            [*self.entries[:1], SimpleNamespace(**{**vars(self.entries[1]), "enabled": False})],
        ):
            self.entries = entries
            self.session.reset_mock()
            self.import_bundle.reset_mock()
            with self.subTest(entries=len(entries)), self.assertRaises(ProfileTransferError):
                await self.login.import_login(payload())
            self.assertEqual(self.session.await_count, 1)
            self.import_bundle.assert_not_awaited()
            self.assertEqual((spec.profile_dir / "profile_transfer.json").read_bytes(), original)

    async def test_changed_profile_launch_or_completed_profile_cannot_adopt_an_exit(self):
        spec, _ = await self.retained_profile()
        launch = spec.profile_dir / "launch_settings.sha256"
        original_launch = launch.read_bytes()
        launch.write_text("changed", encoding="utf-8")
        with self.assertRaises(ProfileTransferError):
            await self.login.retained_proxy_binding("verified@example.test")
        launch.write_bytes(original_launch)
        sidecar = spec.profile_dir / "profile_transfer.json"
        value = json.loads(sidecar.read_text(encoding="utf-8"))
        value["completed"] = True
        sidecar.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ProfileTransferError):
            await self.login.retained_proxy_binding("verified@example.test")

    async def test_identity_mismatch_on_retained_exit_prevents_profile_import(self):
        _, proxy = await self.retained_profile()
        self.selected.side_effect = [(17, "socks5://new.invalid:1080"), (28, proxy)]
        self.session.side_effect = [
            {"email": "verified@example.test", "credits": 100},
            {"email": "foreign@example.test", "credits": 100},
        ]
        with self.assertRaisesRegex(ValueError, "账号不一致"):
            await self.login.import_login(payload())
        self.import_bundle.assert_not_awaited()

    async def test_retained_exit_is_committed_before_reverification_even_when_identity_fails(self):
        _, proxy = await self.retained_profile()
        self.selected.side_effect = [(17, "socks5://new.invalid:1080"), (28, proxy)]
        events = []

        async def commit(binding):
            events.append(("commit", binding))

        async def verify(context):
            events.append(("verify", self.routed[-1][2]))
            return {
                "email": "verified@example.test"
                if len(self.routed) == 1
                else "foreign@example.test",
                "credits": 100,
            }

        self.session.side_effect = verify
        prepared = {
            **payload(),
            "_extension_proxy_binding": {
                "proxy_pool_id": 17,
                "proxy_url": "socks5://new.invalid:1080",
            },
        }
        with self.assertRaisesRegex(ValueError, "账号不一致"):
            await self.login.import_prepared(prepared, on_proxy_binding=commit)
        self.assertEqual(
            events,
            [
                ("verify", "socks5://new.invalid:1080"),
                ("commit", {"proxy_pool_id": 28, "proxy_url": proxy}),
                ("verify", proxy),
            ],
        )
        self.import_bundle.assert_not_awaited()

    async def test_ambiguous_retained_exit_never_selects_the_first_match(self):
        _, _ = await self.retained_profile()
        self.entries.append(copy.copy(self.entries[1]))
        with self.assertRaises(ProfileTransferError):
            await self.login.retained_proxy_binding("verified@example.test")
        self.import_bundle.assert_not_awaited()

    async def test_existing_account_updates_original_profile_and_reports_updated(self):
        existing = SimpleNamespace(id=77, email="verified@example.test", proxy_pool_id=28)
        self.transfer.existing_account.return_value = existing
        self.transfer.refresher._proxy_url = AsyncMock(
            return_value="socks5://original.invalid:1080"
        )
        self.transfer.update_existing = AsyncMock(
            return_value={"success": True, "token_id": 77, "email": existing.email}
        )
        commit = AsyncMock()
        result = await self.login.import_login(payload(), on_proxy_binding=commit)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["token_id"], 77)
        self.transfer.update_existing.assert_awaited_once()
        self.assertIs(self.transfer.update_existing.await_args.args[0], existing)
        self.assertTrue(
            all(
                row["value"] == "rotated"
                for row in self.transfer.update_existing.await_args.args[1]
            )
        )
        commit.assert_awaited_once_with(
            {"proxy_pool_id": 28, "proxy_url": "socks5://original.invalid:1080"}
        )
        self.import_bundle.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
