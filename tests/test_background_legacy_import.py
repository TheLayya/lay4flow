"""Background legacy imports restore metadata only to the validated identity."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import admin
from src.services.browser.profile_transfer import ProfileTransferError


class BackgroundLegacyImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_target_proxy_applies_to_validation_and_saved_legacy_item(self):
        account = SimpleNamespace(id=7, email="verified@example.test", is_active=True)
        routed = []

        async def flow_call(token, operation):
            routed.append((token.proxy_pool_id, token.proxy_url))
            return await operation()

        importer = AsyncMock(
            return_value={
                "added": 1,
                "imported": [{"token_id": 7, "email": account.email}],
            }
        )
        selector = AsyncMock(return_value=(17, "socks5h://target.invalid:1080"))
        with (
            patch.multiple(
                admin,
                token_manager=SimpleNamespace(
                    get_token=AsyncMock(return_value=account), _flow_call_for_token=flow_call
                ),
                import_tokens=importer,
            ),
            patch.object(
                admin, "ProxyManager", return_value=SimpleNamespace(select_account_proxy=selector)
            ),
        ):
            await admin.import_legacy_account(
                {"session_token": "synthetic", "proxy_url": "http://source.invalid:80"},
                proxy_pool_id=17,
            )
        self.assertEqual(routed, [(17, "socks5h://target.invalid:1080")])
        self.assertEqual(importer.await_args.args[0].tokens[0].proxy_url, routed[0][1])

    async def test_rotated_session_restores_settings_to_verified_receipt_not_email_hint(self):
        account = SimpleNamespace(id=7, email="verified@example.test", is_active=True)
        manager = SimpleNamespace(get_token=AsyncMock(return_value=account))
        importer = AsyncMock(
            return_value={
                "added": 1,
                "updated": 0,
                "imported": [{"index": 0, "token_id": 7, "email": account.email}],
            }
        )
        save, disable = AsyncMock(), AsyncMock()
        with patch.multiple(
            admin,
            token_manager=manager,
            import_tokens=importer,
            update_token=save,
            disable_token=disable,
        ):
            result = await admin.import_legacy_account(
                {
                    "email": "untrusted-hint@example.test",
                    "session_token": "synthetic-old-session",
                    "remark": "restored",
                    "project_id": "synthetic-project",
                    "is_active": False,
                }
            )
        self.assertEqual(result["email"], account.email)
        self.assertEqual(save.await_args.args[0], 7)
        self.assertEqual(save.await_args.args[1].remark, "restored")
        self.assertEqual(save.await_args.args[1].project_id, "synthetic-project")
        disable.assert_awaited_once_with(7, token="background-import")

    async def test_failed_or_changed_identity_never_restores_metadata(self):
        for outcome, account in (
            ({"errors": ["private diagnostic"]}, None),
            ({"added": 1, "imported": []}, None),
            (
                {"added": 1, "imported": [{"token_id": 7, "email": "verified@example.test"}]},
                SimpleNamespace(id=7, email="changed@example.test", is_active=True),
            ),
        ):
            with self.subTest(outcome=outcome):
                save, disable = AsyncMock(), AsyncMock()
                with patch.multiple(
                    admin,
                    token_manager=SimpleNamespace(get_token=AsyncMock(return_value=account)),
                    import_tokens=AsyncMock(return_value=outcome),
                    update_token=save,
                    disable_token=disable,
                ):
                    with self.assertRaises(ProfileTransferError) as caught:
                        await admin.import_legacy_account(
                            {
                                "session_token": "synthetic",
                                "remark": "preserved",
                                "is_active": False,
                            }
                        )
                self.assertNotIn("private diagnostic", str(caught.exception))
                save.assert_not_awaited()
                disable.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
