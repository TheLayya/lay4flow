"""Pool editing and batch operations use synthetic SQLite and mocked health probes."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI, Header, HTTPException

from src.api.admin_routes.proxy import create_proxy_router
from src.core.database import Database
from src.services.proxy_manager import ProxyManager


class ProxyPoolRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(str(Path(self.directory.name) / "pool.db"))
        await self.database.init_db()
        self.manager = ProxyManager(self.database)

        async def verify(authorization: str | None = Header(default=None)):
            if authorization != "Bearer synthetic-session":
                raise HTTPException(status_code=401, detail="需要登录")
            return "synthetic-session"

        router, _ = create_proxy_router(lambda: self.manager, lambda: None, verify)
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://synthetic.invalid",
            headers={"Authorization": "Bearer synthetic-session"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.entry_id = await self.manager.add_proxy_pool_entry(
            "socks5h://synthetic:secret@proxy.invalid:1080", "old note"
        )

    async def edit(self, payload, entry_id=None):
        response = await self.client.put(
            f"/api/proxy/pool/{self.entry_id if entry_id is None else entry_id}", json=payload
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def batch(self, action, ids):
        response = await self.client.post(
            "/api/proxy/pool/batch", json={"action": action, "ids": ids}
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def import_proxies(self, text):
        response = await self.client.post("/api/proxy/pool/import", json={"text": text})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_import_deduplicates_normalized_lines_and_preserves_bound_existing_row(self):
        from src.core.models import Token

        await self.manager.set_proxy_pool_enabled(self.entry_id, False)
        await self.manager.update_proxy_pool_entry(self.entry_id, captcha_enabled=False)
        await self.database.update_proxy_pool_check(self.entry_id, True)
        entry = await self.database.get_proxy_pool_entry(self.entry_id)
        await self.database.add_token(
            Token(
                st="synthetic-import-st",
                email="import@example.invalid",
                proxy_pool_id=entry.id,
                proxy_url=entry.proxy_url,
            )
        )
        before = await self.database.get_proxy_pool_entry(self.entry_id)
        self.manager.check_proxy_pool_entry = AsyncMock()
        text = (
            " \r\n socks5h://proxy.invalid:1080:synthetic:secret \r\n"
            " proxy-new.invalid:8080:synthetic:new-secret \r\n\r\n"
            "socks5h://synthetic:new-secret@proxy-new.invalid:8080\r\n"
            "http://synthetic:new-secret@proxy-new.invalid:8080\r\n"
            "http://synthetic:invalid-secret@proxy.invalid:0"
        )
        result = await self.import_proxies(text)
        self.assertTrue(result["success"])
        self.assertEqual(
            (result["total"], result["added"], result["skipped"], result["failed"]), (5, 2, 2, 1)
        )
        self.assertEqual([item["line"] for item in result["results"]], [2, 3, 5, 6, 7])
        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["skipped", "added", "skipped", "added", "failed"],
        )
        self.assertEqual(result["results"][0]["id"], self.entry_id)
        self.assertEqual(result["results"][1]["id"], result["results"][2]["id"])
        self.assertNotEqual(result["results"][1]["id"], result["results"][3]["id"])
        self.assertEqual(result["results"][4]["error_code"], "invalid_format")
        self.assertEqual(await self.database.get_proxy_pool_entry(self.entry_id), before)
        added = await self.database.get_proxy_pool_entry(result["results"][1]["id"])
        self.assertEqual(added.proxy_url, "socks5h://synthetic:new-secret@proxy-new.invalid:8080")
        http_entry = await self.database.get_proxy_pool_entry(result["results"][3]["id"])
        self.assertEqual(http_entry.proxy_url, "http://synthetic:new-secret@proxy-new.invalid:8080")
        self.assertTrue(added.enabled)
        self.assertTrue(added.captcha_enabled)
        self.assertEqual(added.note, "")
        self.manager.check_proxy_pool_entry.assert_not_awaited()
        self.assertNotIn("secret", str(result))
        self.assertNotIn("proxy.invalid", str(result))

    async def test_import_defaults_to_socks5h_and_preserves_explicit_schemes(self):
        text = (
            "\n 192.0.2.10:1080:synthetic@suite:p@ss:word#/? \n\n"
            "https://synthetic:secret@https.invalid:443\n"
            "socks5://synthetic:secret@socks.invalid:1080\n"
            "socks5h://synthetic:secret@socksh.invalid:1080\n"
            "st5 legacy.invalid:1080:synthetic:secret\n"
            "bare.invalid:1080\n"
        )
        result = await self.import_proxies(text)
        self.assertEqual((result["total"], result["added"], result["failed"]), (6, 6, 0))
        self.assertEqual([item["line"] for item in result["results"]], [2, 4, 5, 6, 7, 8])
        entries = [
            await self.database.get_proxy_pool_entry(item["id"]) for item in result["results"]
        ]
        self.assertEqual(
            [entry.proxy_url for entry in entries],
            [
                "socks5h://synthetic%40suite:p%40ss%3Aword%23%2F%3F@192.0.2.10:1080",
                "https://synthetic:secret@https.invalid:443",
                "socks5://synthetic:secret@socks.invalid:1080",
                "socks5h://synthetic:secret@socksh.invalid:1080",
                "socks5://synthetic:secret@legacy.invalid:1080",
                "socks5h://bare.invalid:1080",
            ],
        )

    async def test_import_storage_failure_is_private_partial_and_retryable(self):
        text = "http://synthetic:private-secret@retry.invalid:80\nnext.invalid:80"
        original_add = self.manager.add_proxy_pool_entry

        async def add(proxy_url):
            if "retry.invalid" in proxy_url:
                raise RuntimeError(f"database write failed: {proxy_url}")
            return await original_add(proxy_url)

        with patch.object(self.manager, "add_proxy_pool_entry", side_effect=add):
            first = await self.import_proxies(text)
        self.assertEqual((first["added"], first["skipped"], first["failed"]), (1, 0, 1))
        self.assertEqual([item["status"] for item in first["results"]], ["failed", "added"])
        self.assertEqual(first["results"][0]["error_code"], "import_failed")
        self.assertNotIn("private-secret", str(first))
        self.assertNotIn("retry.invalid", str(first))
        retried = await self.import_proxies(text)
        self.assertEqual((retried["added"], retried["skipped"], retried["failed"]), (1, 1, 0))
        again = await self.import_proxies(text)
        self.assertEqual((again["added"], again["skipped"], again["failed"]), (0, 2, 0))
        self.assertEqual(len(await self.manager.list_proxy_pool()), 3)

    async def test_import_all_invalid_is_processed_without_leaking_parser_errors(self):
        result = await self.import_proxies("bad.invalid:0:synthetic:hidden-secret\nmalformed")
        self.assertTrue(result["success"])
        self.assertEqual((result["total"], result["added"], result["failed"]), (2, 0, 2))
        self.assertNotIn("hidden-secret", str(result))
        with patch.object(
            self.manager,
            "normalize_proxy_url",
            side_effect=ValueError("http://synthetic:hidden-secret@proxy.invalid:80"),
        ):
            result = await self.import_proxies("proxy-new.invalid:80")
        self.assertEqual(result["failed"], 1)
        self.assertNotIn("hidden-secret", str(result))
        self.assertEqual(len(await self.manager.list_proxy_pool()), 1)

    async def test_import_limits_are_validated_before_read_or_write_and_keep_secrets_private(self):
        original = await self.manager.list_proxy_pool()
        payloads = (
            "\r\n \t ",
            "\n".join(f"proxy-{index}.invalid:80" for index in range(501)),
            "valid.invalid:80\n" + "hidden-secret" * 400,
            " " * 2_100_001,
        )
        with (
            patch.object(self.manager, "list_proxy_pool", new_callable=AsyncMock) as list_pool,
            patch.object(self.manager, "add_proxy_pool_entry", new_callable=AsyncMock) as add,
        ):
            for text in payloads:
                with self.subTest(size=len(text)):
                    result = await self.import_proxies(text)
                    self.assertFalse(result["success"])
                    self.assertNotIn("hidden-secret", str(result))
            list_pool.assert_not_awaited()
            add.assert_not_awaited()
        self.assertEqual(await self.manager.list_proxy_pool(), original)

    async def test_import_accepts_500_entries_and_4096_character_line(self):
        long_line = "long.invalid:80:synthetic:" + "p" * (4096 - len("long.invalid:80:synthetic:"))
        text = "\r\n\r\n".join([long_line, *(f"proxy-{index}.invalid:80" for index in range(499))])
        self.manager.add_proxy_pool_entry = AsyncMock(return_value=1000)
        result = await self.import_proxies(text)
        self.assertTrue(result["success"])
        self.assertEqual((result["total"], result["added"], result["failed"]), (500, 500, 0))
        self.assertEqual(result["results"][-1]["line"], 999)
        self.assertEqual(self.manager.add_proxy_pool_entry.await_count, 500)

    async def test_import_pool_read_error_is_private_and_does_not_write(self):
        self.manager.list_proxy_pool = AsyncMock(
            side_effect=RuntimeError("http://synthetic:hidden-secret@proxy.invalid:80")
        )
        self.manager.add_proxy_pool_entry = AsyncMock()
        result = await self.import_proxies("new.invalid:80")
        self.assertFalse(result["success"])
        self.assertNotIn("hidden-secret", str(result))
        self.manager.add_proxy_pool_entry.assert_not_awaited()

    async def test_import_requires_existing_admin_auth(self):
        self.client.headers.pop("Authorization")
        response = await self.client.post("/api/proxy/pool/import", json={"text": "new.invalid:80"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(await self.manager.list_proxy_pool()), 1)

    async def test_edit_retains_id_disabled_state_and_creation_but_resets_stale_health(self):
        await self.manager.set_proxy_pool_enabled(self.entry_id, False)
        await self.database.update_proxy_pool_check(self.entry_id, True)
        before = await self.database.get_proxy_pool_entry(self.entry_id)
        result = await self.edit(
            {"proxy_url": "127.0.0.2:8123:synthetic:new-secret", "note": "  changed  "}
        )
        self.assertEqual(result, {"success": True, "id": self.entry_id})
        after = await self.database.get_proxy_pool_entry(self.entry_id)
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.created_at, before.created_at)
        self.assertFalse(after.enabled)
        self.assertEqual(after.proxy_url, "http://synthetic:new-secret@127.0.0.2:8123")
        self.assertEqual(after.note, "changed")
        self.assertIsNone(after.last_check_ok)
        self.assertIsNone(after.last_check_at)
        self.assertNotIn("new-secret", str(result))

    async def test_note_only_edit_preserves_url_health_and_can_clear_note(self):
        await self.database.update_proxy_pool_check(self.entry_id, True)
        before = await self.database.get_proxy_pool_entry(self.entry_id)
        self.assertTrue((await self.edit({"note": ""}))["success"])
        after = await self.database.get_proxy_pool_entry(self.entry_id)
        self.assertEqual(after.proxy_url, before.proxy_url)
        self.assertEqual(after.last_check_at, before.last_check_at)
        self.assertTrue(after.last_check_ok)
        self.assertEqual(after.note, "")

    async def test_edit_during_inflight_check_discards_health_for_old_url(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def probe(*args, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(status_code=200)

        session = SimpleNamespace(get=AsyncMock(side_effect=probe))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch("curl_cffi.requests.AsyncSession", return_value=context):
            pending = asyncio.create_task(self.manager.check_proxy_pool_entry(self.entry_id))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                result = await self.edit({"proxy_url": "http://new-proxy.invalid:8080"})
                self.assertTrue(result["success"])
            finally:
                release.set()
                await asyncio.wait_for(pending, timeout=2)
            edited = await self.database.get_proxy_pool_entry(self.entry_id)
            self.assertEqual(edited.proxy_url, "http://new-proxy.invalid:8080")
            self.assertIsNone(edited.last_check_ok)
            self.assertIsNone(edited.last_check_at)
            self.assertTrue(await self.manager.check_proxy_pool_entry(self.entry_id))
            fresh = await self.database.get_proxy_pool_entry(self.entry_id)
            self.assertTrue(fresh.last_check_ok)
            self.assertIsNotNone(fresh.last_check_at)

    async def test_invalid_edit_leaves_row_unchanged_and_does_not_echo_credentials(self):
        before = await self.database.get_proxy_pool_entry(self.entry_id)
        for payload in (
            {},
            {"proxy_url": None},
            {"proxy_url": ""},
            {"proxy_url": "   "},
            {"proxy_url": "socks5://synthetic:secret@badhost:0"},
        ):
            with self.subTest(payload=payload):
                result = await self.edit(payload)
                self.assertFalse(result["success"])
                self.assertNotIn("secret", str(result))
                self.assertEqual(await self.database.get_proxy_pool_entry(self.entry_id), before)

    async def test_missing_edit_does_not_create_replacement(self):
        result = await self.edit({"note": "changed"}, entry_id=999)
        self.assertFalse(result["success"])
        self.assertEqual(len(await self.manager.list_proxy_pool()), 1)

    async def test_batch_enable_disable_deduplicates_and_reports_missing_ids(self):
        result = await self.batch("disable", [self.entry_id, self.entry_id, 999])
        self.assertTrue(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual((result["total"], result["succeeded"], result["failed"]), (2, 1, 1))
        self.assertEqual([item["id"] for item in result["results"]], [self.entry_id, 999])
        self.assertFalse((await self.database.get_proxy_pool_entry(self.entry_id)).enabled)
        await self.batch("disable", [self.entry_id])
        self.assertFalse((await self.database.get_proxy_pool_entry(self.entry_id)).enabled)
        result = await self.batch("enable", [self.entry_id])
        self.assertTrue(result["results"][0]["enabled"])
        self.assertTrue((await self.database.get_proxy_pool_entry(self.entry_id)).enabled)

    async def test_batch_delete_affects_only_selected_entries(self):
        retained = await self.manager.add_proxy_pool_entry("http://retained.invalid:80")
        result = await self.batch("delete", [self.entry_id])
        self.assertTrue(result["results"][0]["deleted"])
        self.assertIsNone(await self.database.get_proxy_pool_entry(self.entry_id))
        self.assertIsNotNone(await self.database.get_proxy_pool_entry(retained))

    async def test_batch_checks_are_bounded_ordered_and_hide_transport_errors(self):
        ids = [self.entry_id]
        for index in range(9):
            ids.append(await self.manager.add_proxy_pool_entry(f"http://proxy{index}.invalid:80"))
        active = 0
        peak = 0

        async def check(entry_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                if entry_id == ids[-1]:
                    raise RuntimeError("socks5://synthetic:secret@proxy.invalid:1080")
                return entry_id != ids[1]
            finally:
                active -= 1

        self.manager.check_proxy_pool_entry = AsyncMock(side_effect=check)
        result = await self.batch("check", ids)
        self.assertEqual([item["id"] for item in result["results"]], ids)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 5)
        self.assertEqual((result["succeeded"], result["failed"]), (8, 2))
        self.assertFalse(result["results"][1]["check_ok"])
        self.assertNotIn("secret", str(result))

    async def test_invalid_batch_contract_is_rejected_before_any_mutation(self):
        before = await self.manager.list_proxy_pool()
        for payload in (
            {"action": "unsupported", "ids": [self.entry_id]},
            {"action": "delete", "ids": []},
            {"action": "delete", "ids": list(range(1, 502))},
            {"action": "delete", "ids": [-1]},
            {"action": "delete", "ids": [True]},
            {"action": "delete", "ids": ["1"]},
        ):
            with self.subTest(payload=payload):
                response = await self.client.post("/api/proxy/pool/batch", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(await self.manager.list_proxy_pool(), before)

    async def test_edit_and_batch_require_existing_admin_auth(self):
        self.client.headers.pop("Authorization")
        edit = await self.client.put(f"/api/proxy/pool/{self.entry_id}", json={"note": "changed"})
        batch = await self.client.post(
            "/api/proxy/pool/batch", json={"action": "delete", "ids": [self.entry_id]}
        )
        self.assertEqual(edit.status_code, 401)
        self.assertEqual(batch.status_code, 401)
        self.assertEqual((await self.database.get_proxy_pool_entry(self.entry_id)).note, "old note")

    async def test_pool_usage_metadata_and_bound_delete_protection(self):
        from src.core.models import Token

        entry = await self.database.get_proxy_pool_entry(self.entry_id)
        await self.database.add_token(
            Token(
                st="synthetic-bound-st",
                email="bound@example.invalid",
                proxy_pool_id=entry.id,
                proxy_url=entry.proxy_url,
            )
        )
        data = (await self.client.get("/api/proxy/pool")).json()
        self.assertEqual(data["entries"][0]["bound_account_count"], 1)
        self.assertTrue(data["entries"][0]["captcha_enabled"])
        result = await self.edit({"captcha_enabled": False, "note": "fixed account"})
        self.assertTrue(result["success"])
        self.assertFalse((await self.database.get_proxy_pool_entry(entry.id)).captcha_enabled)
        result = (await self.client.delete(f"/api/proxy/pool/{entry.id}")).json()
        self.assertFalse(result["success"])
        self.assertIsNotNone(await self.database.get_proxy_pool_entry(entry.id))


if __name__ == "__main__":
    unittest.main()
