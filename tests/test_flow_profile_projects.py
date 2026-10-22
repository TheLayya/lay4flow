"""Project preparation uses the same verified modern Profile and fixed proxy."""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.database import Database
from src.core.models import Project, Token
from src.services.browser.flow_projects import create_profile_project
from src.services.flow.flow_rpc import FlowRPCError
from src.services.project_pool import ProjectPool
from src.services.proxy_manager import ProxyManager


class FlowProfileProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "synthetic.db"))
        await self.db.init_db()
        self.proxy_url = "socks5://fixed.invalid:1080"
        self.proxy_id = await self.db.add_proxy_pool_entry(self.proxy_url)
        self.token_id = await self.db.add_token(
            Token(
                st="profile-session:synthetic",
                email="fixture@example.invalid",
                browser_profile_source="cloak",
                proxy_pool_id=self.proxy_id,
                proxy_url=self.proxy_url,
            )
        )
        await self.db.update_token(self.token_id, browser_profile_epoch="synthetic-epoch")
        self.token = await self.db.get_token(self.token_id)
        self.context = object()
        self.opened = []
        self.close_error = False

        @asynccontextmanager
        async def session(tokens, token, proxy):
            self.opened.append((token.email, {"proxy_url": proxy, "existing_only": True}))
            yield self.context
            if self.close_error:
                raise RuntimeError("synthetic close error")

        self.enterContext(patch("src.services.browser.flow_projects.account_http_session", session))

        self.tokens = SimpleNamespace(
            db=self.db,
            profile_refresher=SimpleNamespace(
                manager=SimpleNamespace(
                    credential_session=Mock(side_effect=AssertionError("browser opened"))
                ),
                verifier=object(),
                _proxy_url=ProxyManager(self.db).resolve_account_proxy,
            ),
        )
        self.identity = self.enterContext(
            patch(
                "src.services.browser.flow_projects.TokenManagerAccountProbe.verify",
                new=AsyncMock(return_value={"email": self.token.email, "session_valid": True}),
            )
        )
        self.rpc = self.enterContext(
            patch(
                "src.services.browser.flow_projects.flow_rpc",
                new=AsyncMock(side_effect=[[[]], ["project-id"]]),
            )
        )

    async def test_verified_context_creates_current_rpc_project_before_persistence(self):
        result = await create_profile_project(self.tokens, self.token, "Fixture title")
        self.assertEqual((result.project_id, result.token_id), ("project-id", self.token_id))
        self.assertIsNotNone(result.id)
        self.assertEqual(
            self.opened,
            [
                (
                    self.token.email,
                    {"proxy_url": "socks5://fixed.invalid:1080", "existing_only": True},
                )
            ],
        )
        self.identity.assert_awaited_once_with(self.token.email, self.context)
        self.rpc.assert_awaited_with(
            self.context,
            "jHPbke",
            ["projects/*", [None, ["Fixture title"]], [None, 22]],
            network_step="flow_projects",
        )
        self.assertEqual(
            (await self.db.get_projects_by_token(self.token_id))[0].project_id, result.project_id
        )

    async def test_existing_remote_project_is_reused_before_creating_new_one(self):
        self.rpc.side_effect = None
        self.rpc.return_value = [[["remote-id", ["Existing title"]]]]
        result = await create_profile_project(self.tokens, self.token, "Fixture title")
        self.assertEqual((result.project_id, result.project_name), ("remote-id", "Existing title"))
        self.rpc.assert_awaited_once_with(
            self.context, "UpteDb", ["projects/*", 21], network_step="flow_projects"
        )

    async def test_identity_failure_prevents_write_and_database_insert(self):
        self.identity.return_value = {"reason": "identity_mismatch"}
        with self.assertRaises(FlowRPCError):
            await create_profile_project(self.tokens, self.token, "Fixture title")
        self.rpc.assert_not_awaited()
        self.assertEqual(await self.db.get_projects_by_token(self.token_id), [])

    async def test_malformed_upstream_project_is_never_saved(self):
        for payload in ([], {}, [None], [""], [False]):
            self.rpc.side_effect = [[[]], payload]
            with self.subTest(payload=payload), self.assertRaises(FlowRPCError):
                await create_profile_project(self.tokens, self.token, "Fixture title")
        self.assertEqual(await self.db.get_projects_by_token(self.token_id), [])

    async def change_binding(self, kind):
        if kind == "deleted":
            await self.db.delete_token(self.token_id)
        elif kind == "proxy_disabled":
            await self.db.set_proxy_pool_enabled(self.proxy_id, False)
        elif kind == "proxy_changed":
            # The admin route rejects edits to bound entries.  Simulate an
            # independent storage writer to exercise the atomic SQL predicate.
            async with self.db._connect(write=True) as db:
                await db.execute(
                    "UPDATE proxy_pool SET proxy_url = ? WHERE id = ?",
                    ("socks5://replacement.invalid:1080", self.proxy_id),
                )
                await db.commit()
        else:
            await self.db.update_token(
                self.token_id,
                **{
                    "email": {"email": "changed@example.invalid"},
                    "epoch": {"browser_profile_epoch": "changed-epoch"},
                    "source": {"browser_profile_source": "cookies"},
                    "disabled": {"is_active": False, "ban_reason": "manual"},
                }[kind],
            )

    async def test_changed_binding_before_create_prevents_remote_write(self):
        async def listed(*args, **kwargs):
            await self.change_binding("email")
            return [[]]

        self.rpc.side_effect = listed
        with self.assertRaises(FlowRPCError):
            await create_profile_project(self.tokens, self.token, "Fixture title")
        self.assertEqual(self.rpc.await_count, 1)
        self.assertEqual(await self.db.get_projects_by_token(self.token_id), [])

    async def test_atomic_insert_rejects_identity_epoch_source_delete_and_proxy_changes(self):
        for kind in (
            "email",
            "epoch",
            "source",
            "disabled",
            "deleted",
            "proxy_disabled",
            "proxy_changed",
        ):
            with self.subTest(kind=kind):
                original = self.token.model_dump(exclude={"id"})
                await self.db.delete_token(self.token_id)
                await self.db.update_proxy_pool_entry(self.proxy_id, self.proxy_url, "synthetic")
                await self.db.set_proxy_pool_enabled(self.proxy_id, True)
                self.token_id = await self.db.add_token(Token(**original))
                await self.db.update_token(self.token_id, browser_profile_epoch="synthetic-epoch")
                self.token = await self.db.get_token(self.token_id)

                async def rpc(context, rpcid, payload, **kwargs):
                    if rpcid == "UpteDb":
                        return [[]]
                    await self.change_binding(kind)
                    return ["late-project-id"]

                self.rpc.side_effect = rpc
                with self.assertRaises(FlowRPCError):
                    await create_profile_project(self.tokens, self.token, "Fixture title")
                self.assertEqual(await self.db.get_projects_by_token(self.token_id), [])

    async def test_reused_remote_project_also_checks_binding_before_insert(self):
        async def listed(*args, **kwargs):
            await self.change_binding("epoch")
            return [[["late-existing-id", ["Existing"]]]]

        self.rpc.side_effect = listed
        with self.assertRaises(FlowRPCError):
            await create_profile_project(self.tokens, self.token, "Fixture title")
        self.assertEqual(await self.db.get_projects_by_token(self.token_id), [])

    async def test_failed_profile_close_does_not_publish_project(self):
        self.close_error = True
        for existing in (False, True):
            self.rpc.side_effect = (
                [[[]], ["project-id"]] if not existing else [[[["existing-id", ["Existing"]]]]]
            )
            with self.subTest(existing=existing), self.assertRaisesRegex(RuntimeError, "close"):
                await create_profile_project(self.tokens, self.token, "Fixture title")
        self.assertEqual(await self.db.get_projects_by_token(self.token_id), [])

    async def test_current_project_cas_preserves_changed_binding_or_disabled_proxy(self):
        await self.db.add_project(
            Project(project_id="existing-id", token_id=self.token_id, project_name="Existing")
        )
        pool = ProjectPool(self.db, object(), lambda: 1)
        original = self.db.get_projects_by_token
        for kind in ("email", "proxy_disabled"):
            with self.subTest(kind=kind):
                await self.db.update_token(self.token_id, email=self.token.email)
                await self.db.set_proxy_pool_enabled(self.proxy_id, True)

                async def list_projects(token_id):
                    result = await original(token_id)
                    await self.change_binding(kind)
                    await self.db.update_token(token_id, current_project_id="manual-selection")
                    return result

                with patch.object(self.db, "get_projects_by_token", side_effect=list_projects):
                    with self.assertRaisesRegex(ValueError, "binding changed"):
                        await pool.ensure_project_exists(self.token_id)
                self.assertEqual(
                    (await self.db.get_token(self.token_id)).current_project_id, "manual-selection"
                )
