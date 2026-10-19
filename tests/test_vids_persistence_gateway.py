import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from src.services.vids.client import cookie_jar, cookie_rows
from src.services.vids.gateway import GatewayError, VidsGateway
from src.services.vids.session_store import SessionStore, SessionStoreError
from tests.test_vids_gateway import client as mock_client


def cookies(value="synthetic-initial-cookie"):
    return [
        {
            "name": "SAPISID",
            "value": value,
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "hostOnly": False,
            "expires": 4000000000,
            "session": False,
        },
        {
            "name": "OSID",
            "value": "synthetic-host-only-cookie",
            "domain": "docs.google.com",
            "path": "/videos",
            "secure": True,
            "httpOnly": True,
            "hostOnly": True,
            "expires": 4000000000,
            "session": False,
        },
    ]


def client(rows=None, *, identity="same-account"):
    result = mock_client()
    result.editor_url += "?authuser=0"
    result.flags["docs-hue"] = identity
    result.session.cookies.jar = cookie_jar(cookies() if rows is None else rows)
    result.on_cookies_changed = None
    return result


class VidsPersistenceGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.key = Fernet.generate_key()
        self.store = SessionStore(self.root, self.key)
        self.source = AsyncMock()
        self.gateways = []
        self.client = client()
        self.gateway = self.gateway_for(self.client)
        await self.gateway.start()

    async def asyncTearDown(self):
        for gateway in reversed(self.gateways):
            await gateway.close()
        self.directory.cleanup()

    def gateway_for(self, value, **options):
        gateway = VidsGateway(
            value,
            self.root,
            cookie_source=self.source,
            session_store=options.pop("session_store", self.store),
            **options,
        )
        self.gateways.append(gateway)
        return gateway

    @staticmethod
    def rotate(value, secret="synthetic-rotated-cookie"):
        value.session.cookies.jar = cookie_jar(cookies(secret))
        value.on_cookies_changed()

    def assert_sanitized(self, error):
        self.assertNotIn("synthetic", str(error))
        self.assertNotIn(str(self.root), str(error))

    async def test_initial_start_saves_validated_encrypted_scoped_session(self):
        self.client.bootstrap.assert_awaited_once()
        self.client.quota.assert_awaited_once()
        self.source.assert_not_awaited()
        saved = self.store.load()
        self.assertEqual(saved["editor_url"], self.client.editor_url)
        self.assertEqual(saved["auth_user"], self.client.auth_user)
        digest = hashlib.sha256(b"same-account").hexdigest()
        self.assertEqual(saved["account_hash"], digest)
        self.assertEqual((self.root / ".account").read_text(encoding="utf-8"), digest)
        self.assertEqual(saved["cookies"], cookie_rows(self.client.session.cookies.jar))
        self.assertNotIn(b"synthetic-initial-cookie", self.store.path.read_bytes())
        self.assertNotIn(b"synthetic-host-only-cookie", self.store.path.read_bytes())
        self.assertIsNotNone(self.client.on_cookies_changed)
        self.assertEqual(list(self.root.glob("vids_*.json")), [])

    async def test_http_rotation_is_saved_and_resume_restores_new_jar_without_profile(self):
        self.rotate(self.client)
        saved = self.store.load()
        self.assertIn("synthetic-rotated-cookie", [row["value"] for row in saved["cookies"]])
        await self.gateway.close()
        restored = client(saved["cookies"])
        resumed = self.gateway_for(None, resume=True, proxy="http://synthetic-proxy:8080")
        with patch("src.services.vids.gateway.VidsClient", return_value=restored) as factory:
            await resumed.start()
        factory.assert_called_once_with(
            saved["editor_url"],
            saved["cookies"],
            auth_user=saved["auth_user"],
            proxy="http://synthetic-proxy:8080",
        )
        self.assertEqual(cookie_rows(restored.session.cookies.jar), saved["cookies"])
        restored.bootstrap.assert_awaited_once()
        restored.quota.assert_awaited_once()
        restored.generate_speech.assert_not_awaited()
        self.source.assert_not_awaited()

    async def test_wrong_key_preserves_files_and_releases_runtime_lock(self):
        await self.gateway.close()
        encrypted = self.store.path.read_bytes()
        pin = (self.root / ".account").read_bytes()
        broken = self.gateway_for(
            None, resume=True, session_store=SessionStore(self.root, Fernet.generate_key())
        )
        with patch("src.services.vids.gateway.VidsClient") as factory:
            with self.assertRaises(GatewayError) as caught:
                await broken.start()
        self.assertEqual(caught.exception.code, "session_restore_failed")
        self.assert_sanitized(caught.exception)
        self.assertIsNone(broken._lock_file)
        factory.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        self.assertEqual((self.root / ".account").read_bytes(), pin)
        recovered = self.gateway_for(None, resume=True)
        with patch("src.services.vids.gateway.VidsClient", return_value=client()):
            await recovered.start()
        self.source.assert_not_awaited()

    async def test_resume_identity_mismatch_preserves_files_closes_candidate_and_releases_lock(
        self,
    ):
        await self.gateway.close()
        encrypted = self.store.path.read_bytes()
        pin = (self.root / ".account").read_bytes()
        wrong = client(identity="synthetic-other-account")
        broken = self.gateway_for(None, resume=True)
        with patch("src.services.vids.gateway.VidsClient", return_value=wrong):
            with self.assertRaises(GatewayError) as caught:
                await broken.start()
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assert_sanitized(caught.exception)
        self.assertIsNone(broken._lock_file)
        wrong.session.close.assert_awaited_once()
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        self.assertEqual((self.root / ".account").read_bytes(), pin)
        recovered = self.gateway_for(None, resume=True)
        with patch("src.services.vids.gateway.VidsClient", return_value=client()):
            await recovered.start()
        self.source.assert_not_awaited()

    async def test_new_start_cannot_overwrite_existing_encrypted_session(self):
        await self.gateway.close()
        encrypted = self.store.path.read_bytes()
        candidate = client(cookies("synthetic-replacement-cookie"))
        fresh = self.gateway_for(candidate)
        with self.assertRaises(GatewayError) as caught:
            await fresh.start()
        self.assertEqual(caught.exception.code, "session_exists")
        self.assertIsNone(fresh._lock_file)
        candidate.bootstrap.assert_not_awaited()
        candidate.session.close.assert_awaited_once()
        self.assertEqual(self.store.path.read_bytes(), encrypted)

    async def test_identity_change_without_cookie_rotation_blocks_paid_submission(self):
        encrypted = self.store.path.read_bytes()
        self.client.flags["docs-hue"] = "synthetic-other-account"
        with self.assertRaises(GatewayError) as caught:
            await self.gateway.generate(
                {"kind": "speech", "prompt": "Synthetic speech"}, "changed-identity-key"
            )
        self.assert_sanitized(caught.exception)
        self.client.generate_speech.assert_not_awaited()
        self.assertEqual(list(self.root.glob("vids_*.json")), [])
        self.assertEqual(self.store.path.read_bytes(), encrypted)

    async def test_failed_bootstrap_never_persists_unvalidated_rotations_on_close(self):
        encrypted = self.store.path.read_bytes()
        self.client.initialization_valid = False
        self.rotate(self.client, "synthetic-unvalidated-rotation")
        self.assertTrue(self.gateway.persistence_failed)
        with self.assertRaises(GatewayError):
            await self.gateway.generate({"kind": "speech", "prompt": "Synthetic"}, "invalid-init")
        await self.gateway.close()
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        self.client.generate_speech.assert_not_awaited()

    async def test_resume_recovery_failure_does_not_commit_bootstrap_cookie_rotation(self):
        await self.gateway.close()
        encrypted = self.store.path.read_bytes()
        pin = (self.root / ".account").read_bytes()
        receipt = self.root / ("vids_" + "a" * 64 + ".json")
        receipt.write_text("synthetic-malformed-receipt", encoding="utf-8")
        candidate = client()

        async def bootstrap():
            candidate.session.cookies.jar = cookie_jar(cookies("synthetic-bootstrap-rotation"))

        candidate.bootstrap.side_effect = bootstrap
        broken = self.gateway_for(None, resume=True)
        with patch("src.services.vids.gateway.VidsClient", return_value=candidate):
            with self.assertRaises(Exception):
                await broken.start()
        self.assertIsNone(broken._lock_file)
        candidate.session.close.assert_awaited_once()
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        self.assertEqual((self.root / ".account").read_bytes(), pin)
        receipt.unlink()
        recovered = self.gateway_for(None, resume=True)
        with patch("src.services.vids.gateway.VidsClient", return_value=client()):
            await recovered.start()
        self.source.assert_not_awaited()

    async def test_candidate_save_failure_keeps_old_client_and_disk(self):
        encrypted = self.store.path.read_bytes()
        candidate = client(cookies("synthetic-replacement-cookie"))
        with (
            patch("src.services.vids.gateway.VidsClient", return_value=candidate),
            patch.object(
                self.store, "save", side_effect=SessionStoreError("synthetic-private-path")
            ),
        ):
            with self.assertRaises(GatewayError) as caught:
                await self.gateway.replace_cookies(cookies("synthetic-replacement-cookie"))
        self.assertEqual(caught.exception.code, "session_persistence_failed")
        self.assert_sanitized(caught.exception)
        self.assertIs(self.gateway.client, self.client)
        self.client.session.close.assert_not_awaited()
        candidate.session.close.assert_awaited_once()
        self.assertEqual(self.store.path.read_bytes(), encrypted)
        self.assertFalse(self.gateway.persistence_failed)

    async def test_paid_response_survives_rotation_save_failure_and_blocks_new_submission(self):
        body = {"kind": "speech", "prompt": "Synthetic speech"}
        response = self.client.generate_speech.return_value
        encrypted = self.store.path.read_bytes()

        async def submit(*args, **kwargs):
            self.rotate(self.client)
            return response

        self.client.generate_speech.side_effect = submit
        with patch.object(
            self.store, "save", side_effect=SessionStoreError("synthetic-private-path")
        ):
            result = await self.gateway.generate(body, "accepted-key")
            self.assertEqual(result["status"], "completed")
            record = self.gateway.get(result["id"])
            self.assertEqual(record["response"], response)
            self.assertTrue(self.gateway.persistence_failed)
            self.assertEqual(self.store.path.read_bytes(), encrypted)
            self.assertEqual(await self.gateway.generate(body, "accepted-key"), result)
            with self.assertRaises(GatewayError) as caught:
                await self.gateway.generate(body, "blocked-key")
            self.assertEqual(caught.exception.code, "session_persistence_failed")
            self.assert_sanitized(caught.exception)
            self.client.generate_speech.assert_awaited_once()
            self.assertEqual(len(list(self.root.glob("vids_*.json"))), 1)
        # A later successful checkpoint restores persistence without buying a generation.
        self.client.on_cookies_changed()
        self.assertFalse(self.gateway.persistence_failed)
        self.assertIn(
            "synthetic-rotated-cookie", [row["value"] for row in self.store.load()["cookies"]]
        )
        self.client.generate_speech.assert_awaited_once()

    async def test_completed_receipt_replays_after_resume_without_submission(self):
        body = {"kind": "speech", "prompt": "Synthetic speech"}
        result = await self.gateway.generate(body, "persistent-replay-key")
        await self.gateway.close()
        restored = client(self.store.load()["cookies"])
        resumed = self.gateway_for(None, resume=True)
        with patch("src.services.vids.gateway.VidsClient", return_value=restored):
            await resumed.start()
        self.assertEqual(await resumed.generate(body, "persistent-replay-key"), result)
        restored.generate_speech.assert_not_awaited()
        restored.download_audio.assert_not_awaited()
        self.source.assert_not_awaited()

    async def test_explicit_resume_refresh_uses_new_cookies_once_without_testing_old_session(self):
        await self.gateway.close()
        saved = self.store.load()
        new_rows = cookies("synthetic-profile-refreshed-cookie")
        self.source.return_value = new_rows
        candidate = client(new_rows)
        refreshed = self.gateway_for(
            None, resume=True, refresh_session=True, proxy="http://synthetic-proxy:8080"
        )
        with patch("src.services.vids.gateway.VidsClient", return_value=candidate) as factory:
            await refreshed.start()
        self.source.assert_awaited_once_with(saved["editor_url"], "http://synthetic-proxy:8080")
        factory.assert_called_once_with(
            saved["editor_url"],
            new_rows,
            auth_user=saved["auth_user"],
            proxy="http://synthetic-proxy:8080",
        )
        self.assertNotEqual(factory.call_args.args[1], saved["cookies"])
        self.client.bootstrap.assert_awaited_once()
        candidate.bootstrap.assert_awaited_once()
        candidate.quota.assert_awaited_once()
        candidate.generate_speech.assert_not_awaited()
        self.assertEqual(self.store.load()["cookies"], cookie_rows(candidate.session.cookies.jar))
        self.assertEqual(list(self.root.glob("vids_*.json")), [])

    async def test_refresh_rejects_wrong_key_or_account_pin_before_profile_access(self):
        await self.gateway.close()
        encrypted = self.store.path.read_bytes()
        pin = self.root / ".account"
        original_pin = pin.read_bytes()
        for failure in ("key", "pin"):
            with self.subTest(failure=failure):
                pin.write_bytes(original_pin if failure == "key" else b"synthetic-mismatched-pin")
                preserved_pin = pin.read_bytes()
                selected_store = (
                    SessionStore(self.root, Fernet.generate_key())
                    if failure == "key"
                    else self.store
                )
                broken = self.gateway_for(
                    None, resume=True, refresh_session=True, session_store=selected_store
                )
                with patch("src.services.vids.gateway.VidsClient") as factory:
                    with self.assertRaises(GatewayError) as caught:
                        await broken.start()
                self.assert_sanitized(caught.exception)
                self.assertEqual(
                    caught.exception.code,
                    "session_restore_failed" if failure == "key" else "account_mismatch",
                )
                self.source.assert_not_awaited()
                factory.assert_not_called()
                self.assertIsNone(broken._lock_file)
                self.assertEqual(self.store.path.read_bytes(), encrypted)
                self.assertEqual(pin.read_bytes(), preserved_pin)
        pin.write_bytes(original_pin)
        recovered = self.gateway_for(None, resume=True)
        with patch("src.services.vids.gateway.VidsClient", return_value=client()):
            await recovered.start()
        self.source.assert_not_awaited()

    async def test_resume_refresh_failures_keep_disk_and_release_runtime_lock(self):
        await self.gateway.close()
        encrypted = self.store.path.read_bytes()
        pin = (self.root / ".account").read_bytes()
        saved = self.store.load()
        new_rows = cookies("synthetic-profile-refreshed-cookie")
        for failure in ("source", "identity", "save"):
            with self.subTest(failure=failure):
                self.source.reset_mock()
                self.source.side_effect = (
                    RuntimeError("synthetic-private-profile-cookie")
                    if failure == "source"
                    else None
                )
                self.source.return_value = new_rows
                candidate = client(
                    new_rows,
                    identity="synthetic-other-account" if failure == "identity" else "same-account",
                )
                broken = self.gateway_for(None, resume=True, refresh_session=True)
                with patch(
                    "src.services.vids.gateway.VidsClient", return_value=candidate
                ) as factory:
                    with patch.object(self.store, "save", wraps=self.store.save) as save:
                        if failure == "save":
                            save.side_effect = SessionStoreError("synthetic-private-path")
                        with self.assertRaises(GatewayError) as caught:
                            await broken.start()
                self.assert_sanitized(caught.exception)
                self.assertEqual(
                    caught.exception.code,
                    {
                        "source": "profile_refresh_failed",
                        "identity": "account_mismatch",
                        "save": "session_persistence_failed",
                    }[failure],
                )
                self.source.assert_awaited_once_with(saved["editor_url"], None)
                if failure == "source":
                    factory.assert_not_called()
                    candidate.bootstrap.assert_not_awaited()
                else:
                    factory.assert_called_once()
                    candidate.bootstrap.assert_awaited_once()
                    if failure == "identity":
                        candidate.quota.assert_not_awaited()
                    else:
                        candidate.quota.assert_awaited_once()
                    candidate.session.close.assert_awaited_once()
                candidate.generate_speech.assert_not_awaited()
                self.assertIsNone(broken._lock_file)
                self.assertFalse(broken.lock.locked())
                self.assertEqual(self.store.path.read_bytes(), encrypted)
                self.assertEqual((self.root / ".account").read_bytes(), pin)
                self.assertEqual(list(self.root.glob("vids_*.json")), [])


if __name__ == "__main__":
    unittest.main()
