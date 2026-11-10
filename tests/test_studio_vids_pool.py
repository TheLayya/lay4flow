"""Studio restart follows a persisted Vids pool result without new paid work."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api import routes
from src.api.admin_routes.studio import create_studio_router
from src.services.vids.client import VidsProtocolError
from src.services.vids.gateway import GatewayError
from tests import test_vids_dispatcher as dispatcher_tests


class StudioVidsPoolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_before_final_job_save_recovers_successor_without_resubmission(self):
        fixture = dispatcher_tests.VidsDispatcherTests()
        await fixture.asyncSetUp()
        services = []
        clients = []
        handler = SimpleNamespace(db=SimpleNamespace(db_path=str(fixture.root / "flow.db")))

        def application(dispatcher):
            app = FastAPI()
            app.include_router(create_studio_router(lambda: True, lambda: dispatcher))
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://studio.example.test"
            )
            clients.append(client)
            return app, client

        fixture.first.client.generate_speech.side_effect = VidsProtocolError(
            "generate",
            403,
            upstream_status="PERMISSION_DENIED",
            upstream_reason="CROSS_PRODUCT_SHARING_DISABLED",
        )
        app, client = application(fixture.dispatcher)
        try:
            with (
                patch.object(routes, "generation_handler", handler),
                patch("src.services.vids.dispatcher.config", SimpleNamespace(flow_max_retries=2)),
            ):
                response = await client.post(
                    "/api/admin/studio/jobs",
                    headers={"Origin": "https://studio.example.test"},
                    json={
                        "provider": "vids",
                        "body": {"kind": "speech", "prompt": "Hello", "voice": "Achernar"},
                        "count": 1,
                        "request_id": "synthetic-pool-restart",
                    },
                )
                self.assertEqual(response.status_code, 202, response.text)
                admitted = response.json()["job"]
                base_receipt = admitted["items"][0]["receipt"]
                jobs = app.state.studio_jobs
                services.append(jobs)
                await jobs._queues["vids"].join()
                finished = jobs.get(admitted["id"])
                self.assertEqual(finished["status"], "completed")
                final_receipt = finished["items"][0]["receipt"]
                self.assertNotEqual(final_receipt, base_receipt)

                # Simulate the durable state immediately before execute's final event was saved.
                await jobs.close()
                saved = json.loads(jobs.path.read_text(encoding="utf-8"))
                item = saved[0]["items"][0]
                item.update(status="generating", receipt=base_receipt)
                item.pop("vids_receipt", None)
                saved[0].update(state="running", status="running")
                jobs.path.write_text(json.dumps(saved), encoding="utf-8")

                fixture.first.client.quota.reset_mock()
                fixture.second.client.quota.reset_mock()

                restarted = fixture.new_dispatcher()
                restarted._accounts.pop(2)
                restarted._roots.pop(2)
                restarted.unavailable_receipt_roots[2] = fixture.second.root
                restored_app, restored_client = application(restarted)
                response = await restored_client.get("/api/admin/studio/jobs/" + admitted["id"])
                self.assertEqual(response.status_code, 200, response.text)
                services.append(restored_app.state.studio_jobs)
                restored = response.json()["job"]
                self.assertEqual(restored["status"], "completed")
                restored_item = restored["items"][0]
                self.assertEqual(restored_item["receipt"], final_receipt)
                self.assertEqual(restored_item["vids_receipt"]["id"], final_receipt)
                self.assertTrue(
                    restored_item["vids_receipt"]["content_url"].endswith(
                        "/" + final_receipt + "/content"
                    )
                )
                self.assertEqual(fixture.first.get(base_receipt)["status"], "failed")
                self.assertEqual(fixture.second.get(final_receipt)["status"], "completed")
                with self.assertRaises(GatewayError) as caught:
                    restarted.get(2, final_receipt)
                self.assertEqual(caught.exception.code, "session_unavailable")
                fixture.first.client.generate_speech.assert_awaited_once()
                fixture.second.client.generate_speech.assert_awaited_once()
                fixture.first.client.download_audio.assert_not_awaited()
                fixture.second.client.download_audio.assert_awaited_once()
                fixture.first.client.quota.assert_not_awaited()
                fixture.second.client.quota.assert_not_awaited()
        finally:
            for service in reversed(services):
                await service.close()
            for client in clients:
                await client.aclose()
            await fixture.asyncTearDown()


if __name__ == "__main__":
    unittest.main()
