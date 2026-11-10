"""Provider policy and dispatcher share mocked sessions without touching Flow state."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api.admin_routes.vids import create_vids_admin_router
from src.core.database import Database
from src.core.models import Token
from src.core.config import config
from src.services.vids.account_policy import VidsAccountPolicy
from src.services.vids.client import VidsProtocolError, VidsTransportError
from src.services.vids.dispatcher import VidsPoolDispatcher, VidsProviderDispatcher
from src.services.vids.gateway import GatewayError, VidsGateway
from tests import test_vids_dispatcher as dispatcher_fixtures
from tests.test_vids_gateway import client


class VidsSecondIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = Database(str(self.root / "synthetic.db"))
        await self.db.init_db()
        self.identifier = await self.db.add_token(
            Token(st="synthetic-session", email="synthetic@example.invalid")
        )
        self.dispatcher = VidsProviderDispatcher(self.db)
        self.policy = self.dispatcher.account_policy = VidsAccountPolicy(self.db)
        self.upstream = client()
        self.gateway = VidsGateway(self.upstream, self.root / "receipts")
        await self.gateway.start()
        self.addAsyncCleanup(self.gateway.close)
        self.dispatcher.register(
            dispatcher_fixtures.VidsDispatcherTests.binding(self.identifier, self.gateway)
        )
        self.pool = VidsPoolDispatcher(self.dispatcher)
        self.body = {"kind": "speech", "prompt": "synthetic private prompt"}

    async def test_completed_replay_updates_one_log_and_provider_stats_once(self):
        result = await self.pool.generate(0, self.body, "synthetic-once")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(await self.pool.generate(0, self.body, "synthetic-once"), result)
        logs = await self.db.get_logs(include_payload=True)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["operation"], "vids_generate_speech")
        self.assertEqual(logs[0]["status_code"], 200)
        self.assertNotIn(self.body["prompt"], json.dumps(logs))
        self.assertEqual((await self.policy.account_stats(self.identifier))["speech_count"], 1)
        self.assertEqual((await self.db.get_token_stats(self.identifier)).error_count, 0)
        self.upstream.generate_speech.assert_awaited_once()

    async def test_unknown_acceptance_does_not_strike_or_submit_again(self):
        self.upstream.generate_speech.side_effect = VidsTransportError("synthetic-private-cookie")
        result = await self.pool.generate(0, self.body, "synthetic-unknown")
        self.assertEqual(result["status"], "unknown")
        await self.pool.generate(0, self.body, "synthetic-unknown")
        self.assertEqual(
            (await self.policy.account_stats(self.identifier))["consecutive_error_count"], 0
        )
        self.upstream.generate_speech.assert_awaited_once()
        self.assertEqual(len(await self.db.get_logs()), 1)

    async def test_temporary_log_failure_is_recovered_from_new_receipt_marker(self):
        with patch.object(
            self.policy, "observe", side_effect=OSError("synthetic storage interruption")
        ):
            result = await self.pool.generate(0, self.body, "synthetic-storage-gap")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(await self.db.get_logs(), [])
        await self.dispatcher.reconcile_outcomes(self.identifier)
        self.assertEqual((await self.policy.account_stats(self.identifier))["speech_count"], 1)
        self.assertEqual(len(await self.db.get_logs()), 1)
        await self.pool.generate(0, self.body, "synthetic-storage-gap")
        self.upstream.generate_speech.assert_awaited_once()
        self.assertEqual((await self.policy.account_stats(self.identifier))["speech_count"], 1)

    async def test_vids_rule_protects_only_new_vids_work_and_clear_preserves_manual_disable(self):
        await self.policy.save_config({"rules": "upstream_http_403|DISABLE"})
        self.upstream.generate_speech.side_effect = VidsProtocolError("generate", 403)
        result = await self.pool.generate(0, self.body, "synthetic-denied")
        self.assertEqual(result["status"], "failed")
        self.assertIsNotNone(await self.policy.availability(self.identifier))
        self.assertTrue((await self.db.get_token(self.identifier)).is_active)
        with self.assertRaises(GatewayError) as failure:
            await self.pool.generate(0, self.body, "synthetic-protected")
        self.assertEqual(failure.exception.code, "vids_account_protected")
        self.assertEqual(await self.pool.generate(0, self.body, "synthetic-denied"), result)
        await self.db.update_token(self.identifier, is_active=False)
        await self.policy.clear(self.identifier)
        self.assertFalse((await self.db.get_token(self.identifier)).is_active)
        self.upstream.generate_speech.assert_awaited_once()

    @patch.dict(config._config, {"flow": {**config._config.get("flow", {}), "max_retries": 20}})
    async def test_pool_fallback_records_each_physical_receipt_without_implicit_protection(self):
        second_id = await self.db.add_token(
            Token(st="synthetic-session-2", email="synthetic2@example.invalid")
        )
        upstream = client()
        upstream.flags["docs-hue"] = "synthetic-second"
        gateway = VidsGateway(upstream, self.root / "fallback-second")
        await gateway.start()
        self.addAsyncCleanup(gateway.close)
        self.dispatcher.register(
            dispatcher_fixtures.VidsDispatcherTests.binding(second_id, gateway)
        )
        self.upstream.generate_speech.side_effect = VidsProtocolError("generate", 403)
        result = await self.pool.generate(0, self.body, "fallback-policy")
        self.assertEqual(result["status"], "completed")
        base_id = "vids_" + hashlib.sha256(b"fallback-policy").hexdigest()
        self.assertEqual(self.gateway.get(base_id)["status"], "failed")
        self.assertEqual(gateway.get(result["id"])["status"], "completed")
        first_stats = await self.policy.account_stats(self.identifier)
        second_stats = await self.policy.account_stats(second_id)
        self.assertEqual(
            (first_stats["error_count"], first_stats["consecutive_error_count"]), (1, 1)
        )
        self.assertEqual(second_stats["success_count"], 1)
        self.assertEqual(await self.policy.public_states(), {})
        self.assertTrue((await self.db.get_token(self.identifier)).is_active)
        self.assertTrue((await self.db.get_token(second_id)).is_active)
        logs = await self.db.get_logs(include_payload=True)
        self.assertEqual(len(logs), 2)
        self.assertNotIn(self.body["prompt"], json.dumps(logs))

    async def test_config_api_requires_auth_and_reports_safe_provider_diagnostics(self):
        app = FastAPI()
        app.include_router(
            create_vids_admin_router(
                self.dispatcher,
                lambda: True,
                get_maintenance=lambda: SimpleNamespace(
                    public_states=lambda: {self.identifier: {"status": "ready", "last_error": None}}
                ),
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.example.invalid"
        ) as http:
            response = await http.get("/api/admin/vids/config")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["config"]["consecutive_failure_threshold"], 3)
            response = await http.post("/api/admin/vids/config", json={"rules": "synthetic|RETRY"})
            self.assertEqual(response.status_code, 422)
            await self.pool.generate(0, self.body, "synthetic-details")
            detail = (await http.get("/api/admin/vids/accounts/detail")).json()["accounts"][0]
            self.assertEqual(detail["stats"]["speech_count"], 1)
            self.assertEqual(detail["maintenance"]["status"], "ready")
            response = await http.post(
                f"/api/admin/vids/accounts/{self.identifier}/protection/clear"
            )
            self.assertEqual(response.json(), {"success": True})


if __name__ == "__main__":
    unittest.main()
