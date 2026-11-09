"""Local account summary costs stay bounded as the account pool grows."""

import hashlib
import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api.admin_routes.vids import create_vids_admin_router
from src.core.database import Database
from src.core.models import Token
from src.services.vids.account_policy import VidsAccountPolicy
from src.services.vids.dispatcher import VidsAccount, VidsProviderDispatcher


class VidsSummaryPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_switches_keep_deleted_accounts_and_identity_failures_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(str(root / "synthetic.db"))
            await database.init_db()
            token_id = await database.add_token(
                Token(st="private-synthetic", email="test@example.invalid")
            )
            identity = "synthetic-identity"
            identity_hash = hashlib.sha256(identity.encode()).hexdigest()
            (root / ".account").write_text(identity_hash, encoding="utf-8")
            gateway = SimpleNamespace(
                root=root,
                identity=identity,
                _ready=True,
                client=SimpleNamespace(flags={"docs-hue": identity}),
            )
            dispatcher = VidsProviderDispatcher(database)
            dispatcher._accounts = {token_id: VidsAccount(token_id, gateway, identity_hash)}
            dispatcher._roots = {token_id: dispatcher._root(gateway)}
            with patch.object(database, "get_token", side_effect=AssertionError("per-row lookup")):
                rows = await dispatcher.public_accounts()
                self.assertTrue(rows[0]["ready"])
                self.assertTrue(rows[0]["enabled"])
                self.assertNotIn("private", json.dumps(rows))
                (root / ".account").write_text("mismatched", encoding="utf-8")
                self.assertFalse((await dispatcher.public_accounts())[0]["ready"])
                await database.delete_token(token_id)
                removed = (await dispatcher.public_accounts())[0]
                self.assertFalse(removed["enabled"])
                self.assertEqual(removed["email"], "")

    async def test_one_hundred_local_accounts_use_bounded_database_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "synthetic.db"))
            await database.init_db()
            for index in range(100):
                await database.add_token(
                    Token(st=f"synthetic-{index}", email=f"{index}@example.invalid")
                )
            dispatcher = VidsProviderDispatcher(database)
            policy = dispatcher.account_policy = VidsAccountPolicy(database)
            await policy.get_config()
            # Stable synthetic identity; no browser, network, generation or media paths.
            dispatcher._accounts = {
                token_id: SimpleNamespace(gateway=SimpleNamespace(identity=f"synthetic-{token_id}"))
                for token_id in range(1, 101)
            }
            app = FastAPI()
            app.include_router(create_vids_admin_router(dispatcher, lambda: True))
            original = database._connect
            connection_count = 0

            @asynccontextmanager
            async def counted(*, write=False):
                nonlocal connection_count
                connection_count += 1
                async with original(write=write) as db:
                    yield db

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://console.example.invalid"
            ) as http:
                with (
                    patch.object(database, "_connect", counted),
                    patch.object(
                        dispatcher,
                        "_account",
                        side_effect=lambda token_id, **_: dispatcher._accounts[token_id],
                    ),
                ):
                    started = time.perf_counter()
                    response = await http.get("/api/admin/vids/accounts/detail")
                    elapsed = time.perf_counter() - started
            self.assertEqual(response.status_code, 200)
            accounts = response.json()["accounts"]
            self.assertEqual(len(accounts), 100)
            self.assertTrue(all(row["identity_verified"] for row in accounts))
            self.assertTrue(all(row["stats"]["success_count"] == 0 for row in accounts))
            evidence = {
                "accounts": 100,
                "connections": connection_count,
                "elapsed_seconds": elapsed,
            }
            Path("tmp").mkdir(exist_ok=True)
            Path("tmp/accounts-summary-benchmark.json").write_text(
                json.dumps(evidence, indent=2), encoding="utf-8"
            )
            self.assertLessEqual(connection_count, 3, evidence)


if __name__ == "__main__":
    unittest.main()
