import asyncio
import hashlib
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from src.core.models import ProxyPoolEntry
from src.services.proxy_manager import ProxyManager
from src.services.vids.client import VidsProtocolError, VidsTransportError
from src.services.vids.dispatcher import VidsAccount, VidsDispatchError, VidsProviderDispatcher
from src.services.vids.gateway import GatewayError, VidsGateway
from src.services.vids.runtime import VidsRuntime
from src.services.vids.session_store import SessionStore
from tests.test_vids_persistence_gateway import client, cookies


class VidsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_reconciles_only_validated_receipt_bindings(self):
        self.dispatcher.account_policy = SimpleNamespace(reconcile=AsyncMock())
        await self.start()
        self.assertEqual(self.dispatcher.account_policy.reconcile.await_count, 2)
        for call in self.dispatcher.account_policy.reconcile.await_args_list:
            token_id, root = call.args
            self.assertIs(self.dispatcher._accounts[token_id].gateway.root, root)

    async def test_receipt_recovery_storage_failure_preserves_ready_sessions(self):
        self.dispatcher.account_policy = SimpleNamespace(
            reconcile=AsyncMock(side_effect=RuntimeError("Synthetic storage fault"))
        )
        await self.start()
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.assertTrue(self.runtime._started)

    async def test_read_retry_configuration_is_accepted_without_becoming_a_binding(self):
        with patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream):
            await self.runtime.start({"accounts": self.rows, "read_max_attempts": 3})
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})

    async def test_batch_preparation_reads_accounts_and_fixed_proxies_once(self):
        for identifier, token in self.tokens.items():
            token.proxy_pool_id = identifier
            token.proxy_url = f"http://synthetic-fixed-{identifier}.invalid:8080"
        self.db.get_all_tokens = AsyncMock(return_value=list(self.tokens.values()))
        self.db.list_proxy_pool = AsyncMock(
            return_value=[
                ProxyPoolEntry(id=token.id, proxy_url=token.proxy_url)
                for token in self.tokens.values()
            ]
        )
        self.db.get_proxy_pool_entry = AsyncMock(
            side_effect=AssertionError("Per-account proxy read")
        )
        self.db.get_proxy_config = AsyncMock(side_effect=AssertionError("No unbound fallback"))
        self.db.get_token.side_effect = AssertionError("Per-account token read")
        self.runtime.proxy_manager = ProxyManager(self.db)
        factory = await self.start()
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.db.get_all_tokens.assert_awaited_once()
        self.db.list_proxy_pool.assert_awaited_once()
        self.db.get_token.assert_not_awaited()
        self.db.get_proxy_pool_entry.assert_not_awaited()
        self.db.get_proxy_config.assert_not_awaited()
        self.assertEqual(
            {call.kwargs["proxy"] for call in factory.call_args_list},
            {token.proxy_url for token in self.tokens.values()},
        )

    async def test_batch_preparation_preserves_failed_proxy_error_and_receipt_root(self):
        self.tokens[1].proxy_pool_id = 1
        self.tokens[1].proxy_url = "http://synthetic-fixed.invalid:8080"
        self.tokens[2].proxy_url = "http://synthetic-own.invalid:8080"
        self.db.get_all_tokens = AsyncMock(return_value=list(self.tokens.values()))
        self.db.list_proxy_pool = AsyncMock(
            return_value=[
                ProxyPoolEntry(id=1, proxy_url=self.tokens[1].proxy_url, last_check_ok=False)
            ]
        )
        self.runtime.proxy_manager = ProxyManager(self.db)
        await self.start()
        self.assertEqual(set(self.dispatcher._accounts), {2})
        self.assertEqual(self.runtime.account_errors, {1: "proxy_unavailable"})
        self.assertEqual(
            self.dispatcher.unavailable_receipt_roots, {1: Path(self.rows[0]["runtime_dir"])}
        )

    async def test_slow_account_times_out_alone_and_healthy_account_serves_during_restore(self):
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        def upstream(editor, rows, **options):
            value = self.upstream(editor, rows, **options)
            if "synthetic-2" in editor:
                value.bootstrap.side_effect = blocked
            return value

        with (
            patch("src.services.vids.gateway.VidsClient", side_effect=upstream),
            patch("src.services.vids.runtime.ACCOUNT_STARTUP_TIMEOUT", 0.05),
        ):
            startup = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            self.assertTrue(self.runtime._started)
            self.assertFalse(self.runtime._lifecycle_lock.locked())
            self.assertEqual(set(self.dispatcher._accounts), {1})
            self.assertEqual(self.runtime.account_errors, {2: "session_starting"})
            await self.dispatcher.read_quota(1, "image")
            self.dispatcher._claims["existing-synthetic-receipt"] = 1
            await asyncio.wait_for(startup, 1)
        self.assertEqual(self.runtime.account_errors, {2: "session_start_timeout"})
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertEqual(self.dispatcher._claims, {"existing-synthetic-receipt": 1})
        self.assertIsNone(self.runtime.last_error_code)
        self.assertEqual(
            self.dispatcher.unavailable_receipt_roots, {2: Path(self.rows[1]["runtime_dir"])}
        )
        self.assertIsNone(self.runtime._startup_task)
        self.assertEqual(self.runtime._startup_accounts, {})
        self.upstreams[1].session.close.assert_awaited_once()

    async def test_remove_pending_target_does_not_wait_for_sibling_or_republish_it(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()

        def upstream(editor, rows, **options):
            value = self.upstream(editor, rows, **options)
            if "synthetic-2" in editor:
                value.bootstrap.side_effect = blocked
            return value

        with patch("src.services.vids.gateway.VidsClient", side_effect=upstream):
            startup = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            async with asyncio.timeout(1):
                async with self.runtime.login_maintenance(1):
                    self.assertTrue(self.dispatcher._accounts[1].gateway._ready)
                await self.runtime.remove_account(2)
                await startup
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertNotIn(2, self.runtime.binding_rows)
        self.assertNotIn(2, self.dispatcher.unavailable_receipt_roots)
        self.assertNotIn(2, self.runtime.account_errors)

    async def test_queued_startup_target_cancellation_closes_unstarted_gateway(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()

        def upstream(editor, rows, **options):
            value = self.upstream(editor, rows, **options)
            if "synthetic-1" in editor:
                value.bootstrap.side_effect = blocked
            return value

        with (
            patch("src.services.vids.gateway.VidsClient", side_effect=upstream),
            patch("src.services.vids.runtime.STARTUP_CONCURRENCY", 1),
        ):
            startup = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            self.assertEqual(len(self.upstreams), 1)
            await asyncio.wait_for(self.runtime.remove_account(2), 1)
            release.set()
            await asyncio.wait_for(startup, 1)
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertEqual(len(self.upstreams), 1)
        self.assertEqual(self.runtime._startup_accounts, {})

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.tokens = {
            identifier: SimpleNamespace(
                id=identifier,
                is_active=True,
                image_enabled=True,
                video_enabled=True,
                proxy_url="",
            )
            for identifier in (1, 2)
        }
        self.db = SimpleNamespace(get_token=AsyncMock(side_effect=self.tokens.get))
        self.proxy = SimpleNamespace(get_request_proxy_url=AsyncMock(return_value=None))
        self.dispatcher = VidsProviderDispatcher(self.db)
        self.runtime = VidsRuntime(self.dispatcher, self.proxy)
        self.upstreams = []
        self.rows, self.stores, self.environment = [], {}, {}
        for identifier in self.tokens:
            identity = f"synthetic-account-{identifier}"
            digest = hashlib.sha256(identity.encode()).hexdigest()
            root = self.root / str(identifier)
            key_name = f"SYNTHETIC_VIDS_SESSION_{identifier}"
            key = Fernet.generate_key().decode()
            store = SessionStore(root, key)
            editor = f"https://docs.google.com/videos/d/synthetic-{identifier}/edit?authuser=0"
            store.save(
                editor_url=editor,
                auth_user=0,
                account_hash=digest,
                cookies=cookies(f"synthetic-cookie-{identifier}"),
            )
            (root / ".account").write_text(digest, encoding="utf-8")
            self.rows.append(
                {
                    "token_id": identifier,
                    "identity_hash": digest,
                    "editor_url": editor,
                    "runtime_dir": str(root),
                    "session_key_env": key_name,
                }
            )
            self.stores[identifier] = store
            self.environment[key_name] = key
        self.environment_patch = patch.dict(os.environ, self.environment, clear=True)
        self.environment_patch.start()

    async def asyncTearDown(self):
        for upstream in self.upstreams:
            upstream.session.close.side_effect = None
        await self.runtime.close()
        self.environment_patch.stop()
        self.directory.cleanup()

    def upstream(self, editor, rows, *, auth_user, proxy):
        identifier = next(row["token_id"] for row in self.rows if row["editor_url"] == editor)
        upstream = client(rows, identity=f"synthetic-account-{identifier}")
        upstream.editor_url, upstream.auth_user, upstream.proxy = editor, auth_user, proxy
        upstream.initialization_valid = True
        self.upstreams.append(upstream)
        return upstream

    async def start(self, rows=None):
        with patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream) as factory:
            await self.runtime.start({"accounts": self.rows if rows is None else rows})
        return factory

    def assert_empty(self):
        self.assertEqual(self.dispatcher._accounts, {})
        self.assertEqual(self.dispatcher._roots, {})
        self.assertEqual(self.dispatcher._claims, {})
        self.assertEqual(self.dispatcher.unavailable_receipt_roots, {})
        self.assertEqual(self.runtime._gateways, [])
        self.assertFalse(self.runtime._started)
        self.assertEqual(self.runtime.account_errors, {})
        self.assertEqual(self.runtime.binding_rows, {})

    async def test_authentication_protocol_failure_is_recorded_for_only_that_account(self):
        row = self.rows[0]
        encrypted = self.stores[1].path.read_bytes()
        for status in (401, 403, 502):
            with self.subTest(status=status):
                upstream = self.upstream(row["editor_url"], cookies(), auth_user=0, proxy=None)
                upstream.bootstrap.side_effect = VidsProtocolError("private-editor-url", status)
                with patch("src.services.vids.gateway.VidsClient", return_value=upstream):
                    await self.runtime.start({"accounts": [row]})
                expected = (
                    "session_authentication_failed"
                    if status in {401, 403}
                    else "session_unavailable"
                )
                self.assertEqual(self.runtime.account_errors, {1: expected})
                self.assertIsNone(self.runtime.last_error_code)
                self.assertTrue(self.runtime._started)
                self.assertEqual(self.dispatcher._accounts, {})
                self.assertEqual(self.runtime._gateways, [])
                self.assertEqual(self.stores[1].path.read_bytes(), encrypted)
                upstream.session.close.assert_awaited_once()
                await self.runtime.close()
                self.assert_empty()
        await self.start()
        self.assertIsNone(self.runtime.last_error_code)

    async def test_unconfigured_runtime_does_not_read_keys_database_or_create_sessions(self):
        for settings in ({}, {"accounts": []}):
            with (
                patch("src.services.vids.runtime.SessionStore") as store,
                patch("src.services.vids.runtime.VidsGateway") as gateway,
            ):
                await self.runtime.start(settings)
                store.assert_not_called()
                gateway.assert_not_called()
                self.db.get_token.assert_not_awaited()
                self.proxy.get_request_proxy_url.assert_not_awaited()
                await self.runtime.close()
                self.assert_empty()

    async def test_validated_binding_rows_are_immutable_from_input_changes(self):
        rows = [dict(row) for row in self.rows]
        await self.start(rows)
        self.assertEqual(self.runtime.binding_rows, {row["token_id"]: row for row in rows})
        self.assertIsNot(self.runtime.binding_rows[1], rows[0])
        original = self.runtime.binding_rows[1]["editor_url"]
        rows[0]["editor_url"] = "https://docs.google.com/videos/d/synthetic-changed/edit?authuser=0"
        self.assertEqual(self.runtime.binding_rows[1]["editor_url"], original)

    async def test_startup_repair_keeps_environment_key_binding_and_refreshes_only_requested_account(
        self,
    ):
        source = AsyncMock(return_value=cookies("synthetic-renewed-cookie"))
        with patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream):
            await self.runtime.start({"accounts": self.rows}, cookie_sources={1: source})
        source.assert_awaited_once_with(self.rows[0]["editor_url"], None)
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.assertTrue(all(gateway._ready for gateway in self.runtime._gateways))
        self.assertEqual(self.stores[1].load()["cookies"][0]["value"], "synthetic-renewed-cookie")
        self.assertEqual(self.stores[2].load()["cookies"][0]["value"], "synthetic-cookie-2")

    async def test_complete_configuration_is_validated_before_any_gateway_starts(self):
        first, second = self.rows
        invalid = [
            None,
            {"enabled": True, "accounts": self.rows},
            {"accounts": {}},
            {"accounts": [first, {**second, "token_id": True}]},
            {"accounts": [first, {**second, "token_id": first["token_id"]}]},
            {"accounts": [first, {**second, "identity_hash": first["identity_hash"]}]},
            {"accounts": [first, {**second, "identity_hash": "private-invalid-hash"}]},
            {"accounts": [first, {**second, "editor_url": first["editor_url"]}]},
            {"accounts": [first, {**second, "editor_url": "http://private.invalid/edit"}]},
            {"accounts": [first, {**second, "runtime_dir": first["runtime_dir"]}]},
            {"accounts": [first, {**second, "runtime_dir": str(self.root / "1" / "child")}]},
            {"accounts": [first, {**second, "session_key_env": first["session_key_env"]}]},
            {"accounts": [first, {**second, "session_key_env": "VIDS_API_KEY"}]},
            {"accounts": [first, {**second, "proxy": ""}]},
            {"accounts": [first, {**second, "cookies": cookies()}]},
            {
                "accounts": [
                    first,
                    {key: value for key, value in second.items() if key != "token_id"},
                ]
            },
        ]
        for settings in invalid:
            with self.subTest(settings=settings):
                with patch("src.services.vids.runtime.VidsGateway") as gateway:
                    with self.assertRaises(GatewayError) as caught:
                        await self.runtime.start(settings)
                self.assertEqual(caught.exception.code, "invalid_configuration")
                self.assertNotIn("private", str(caught.exception))
                gateway.assert_not_called()
                self.db.get_token.assert_not_awaited()
                self.assert_empty()

    async def test_unknown_shared_account_does_not_block_the_other_session(self):
        self.tokens.pop(1)
        factory = await self.start()
        self.assertEqual(self.runtime.account_errors, {1: "account_not_found"})
        self.assertEqual(self.db.get_token.await_count, 2)
        factory.assert_called_once()
        self.assertEqual(set(self.dispatcher._accounts), {2})
        self.assertTrue(self.dispatcher._account(2, network=True).gateway._ready)
        self.assertIsNone(self.runtime.last_error_code)
        self.assertEqual(
            self.dispatcher.unavailable_receipt_roots, {1: Path(self.rows[0]["runtime_dir"])}
        )

    async def test_missing_or_invalid_session_key_does_not_block_the_other_session(self):
        key_name = self.rows[0]["session_key_env"]
        for invalid_key in (None, "private-invalid-key"):
            with self.subTest(invalid_key=invalid_key):
                if invalid_key is None:
                    os.environ.pop(key_name)
                else:
                    os.environ[key_name] = invalid_key
                factory = await self.start()
                factory.assert_called_once()
                self.assertEqual(self.runtime.account_errors, {1: "invalid_configuration"})
                self.assertEqual(set(self.dispatcher._accounts), {2})
                self.assertTrue(self.dispatcher._account(2, network=True).gateway._ready)
                self.assertIsNone(self.runtime.last_error_code)
                self.assertEqual(
                    self.dispatcher.unavailable_receipt_roots,
                    {1: Path(self.rows[0]["runtime_dir"])},
                )
                await self.runtime.close()
                self.assert_empty()
        os.environ[key_name] = self.environment[key_name]

    async def test_missing_session_key_file_does_not_block_the_other_session(self):
        first, second = self.rows
        missing = {key: value for key, value in first.items() if key != "session_key_env"}
        missing["session_key_file"] = str(self.root / "private-missing-key")
        factory = await self.start([missing, second])
        factory.assert_called_once()
        self.assertEqual(self.runtime.account_errors, {1: "invalid_configuration"})
        self.assertEqual(set(self.dispatcher._accounts), {2})
        self.assertIsNone(self.runtime.last_error_code)

    async def test_account_proxy_resolution_failure_does_not_block_the_other_session(self):
        first, second = self.rows
        invalid = "https://private.invalid/no-port"
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.tokens[1].proxy_url = "" if explicit else invalid
                rows = [{**first, "proxy": invalid}, second] if explicit else self.rows
                factory = await self.start(rows)
                factory.assert_called_once()
                self.assertEqual(self.runtime.account_errors, {1: "invalid_configuration"})
                self.assertEqual(set(self.dispatcher._accounts), {2})
                self.assertTrue(self.dispatcher._account(2, network=True).gateway._ready)
                self.assertIsNone(self.runtime.last_error_code)
                await self.runtime.close()
                self.assert_empty()

    async def test_cancel_during_account_lookup_closes_prepared_sibling_without_starting_it(self):
        entered = asyncio.Event()

        async def pausing(identifier):
            if identifier == 2:
                entered.set()
                await asyncio.Event().wait()
            return self.tokens.get(identifier)

        self.db.get_token.side_effect = pausing
        with patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream):
            pending = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            self.assertEqual(len(self.runtime._gateways), 1)
            candidate = self.runtime._gateways[0]
            close = AsyncMock(wraps=candidate.close)
            candidate.close = close
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assert_empty()
        close.assert_awaited_once()
        self.assertEqual(self.upstreams, [])
        self.assertFalse(candidate._ready)
        self.assertIsNone(candidate._lock_file)

    async def test_proxy_resolution_preserves_account_global_and_explicit_policy(self):
        self.tokens[1].proxy_url = "socks5h://synthetic-account-proxy:1080"
        self.proxy.get_request_proxy_url.return_value = "http://synthetic-global-proxy:8080"
        factory = await self.start()
        self.assertEqual(factory.call_args_list[0].kwargs["proxy"], self.tokens[1].proxy_url)
        self.assertEqual(
            factory.call_args_list[1].kwargs["proxy"], self.proxy.get_request_proxy_url.return_value
        )
        self.proxy.get_request_proxy_url.assert_awaited_once()
        await self.runtime.close()
        self.proxy.get_request_proxy_url.reset_mock()
        explicit = [{**row, "proxy": "http://synthetic-explicit-proxy:8081"} for row in self.rows]
        factory = await self.start(explicit)
        self.assertTrue(
            all(call.kwargs["proxy"] == explicit[0]["proxy"] for call in factory.call_args_list)
        )
        self.proxy.get_request_proxy_url.assert_not_awaited()

    async def test_complete_pool_restores_disabled_account_and_unknown_affinity_after_restart(self):
        await self.start()
        first, second = self.upstreams
        first.generate_speech.side_effect = VidsTransportError("synthetic-private-error")
        body = {"kind": "speech", "prompt": "Hello"}
        unknown = await self.dispatcher.generate(1, body, "synthetic-unknown")
        self.assertEqual(unknown["status"], "unknown")
        self.tokens[1].is_active = False
        await self.runtime.close()
        self.assert_empty()
        first.session.close.assert_awaited_once()
        second.session.close.assert_awaited_once()
        await self.start()
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.assertEqual(await self.dispatcher.generate(1, body, "synthetic-unknown"), unknown)
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(2, body, "synthetic-unknown")
        self.assertEqual(caught.exception.code, "account_affinity_conflict")
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(1, body, "synthetic-new")
        self.assertEqual(caught.exception.code, "account_disabled")
        for upstream in self.upstreams[2:]:
            upstream.generate_speech.assert_not_awaited()
        with self.assertRaises(GatewayError) as caught:
            await self.dispatcher.retry_download(1, unknown["id"])
        self.assertEqual(caught.exception.code, "no_received_result")

    async def test_saved_binding_mismatches_never_create_client_or_change_session_files(self):
        row = self.rows[0]
        root = Path(row["runtime_dir"])
        encrypted, pin = self.stores[1].path.read_bytes(), (root / ".account").read_bytes()
        for changed in (
            {**row, "identity_hash": "a" * 64},
            {**row, "editor_url": row["editor_url"].replace("synthetic-1", "synthetic-other")},
        ):
            with self.subTest(changed=changed):
                with patch("src.services.vids.gateway.VidsClient") as factory:
                    await self.runtime.start({"accounts": [changed]})
                self.assertEqual(self.runtime.account_errors, {1: "account_mismatch"})
                factory.assert_not_called()
                self.assertEqual(self.stores[1].path.read_bytes(), encrypted)
                self.assertEqual((root / ".account").read_bytes(), pin)
                self.assertEqual(self.dispatcher._accounts, {})
                await self.runtime.close()
                self.assert_empty()
        await self.start([row])

    async def test_runtime_lock_precedes_restore_and_preserves_live_pool(self):
        await self.start()
        second = VidsRuntime(VidsProviderDispatcher(self.db), self.proxy)
        with patch.object(SessionStore, "load") as load:
            await second.start({"accounts": self.rows})
        self.assertEqual(second.account_errors, {1: "runtime_busy", 2: "runtime_busy"})
        self.assertTrue(second._started)
        self.assertEqual(second.dispatcher._accounts, {})
        load.assert_not_called()
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.assertTrue(all(gateway._ready for gateway in self.runtime._gateways))
        await second.close()

    async def test_repeated_start_does_not_replace_the_running_pool(self):
        await self.start()
        gateways = list(self.runtime._gateways)
        with patch("src.services.vids.runtime.VidsGateway") as gateway:
            with self.assertRaises(GatewayError) as caught:
                await self.runtime.start({"accounts": self.rows})
        self.assertEqual(caught.exception.code, "runtime_busy")
        gateway.assert_not_called()
        self.assertEqual(self.runtime._gateways, gateways)
        self.assertTrue(all(gateway._ready for gateway in gateways))

    async def test_concurrent_start_is_rejected_before_disturbing_first_start(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_lookup(identifier):
            entered.set()
            await release.wait()
            return self.tokens.get(identifier)

        self.db.get_token.side_effect = blocked_lookup
        with patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream) as factory:
            first = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            try:
                with self.assertRaises(GatewayError) as caught:
                    await asyncio.wait_for(self.runtime.start({"accounts": self.rows}), timeout=0.2)
                self.assertEqual(caught.exception.code, "runtime_busy")
                self.assertEqual(self.db.get_token.await_count, 1)
                factory.assert_not_called()
            finally:
                release.set()
                await first
        self.assertEqual(set(self.dispatcher._accounts), {1, 2})
        self.assertEqual(factory.call_count, 2)

    async def test_bootstrap_identity_change_is_rejected_before_quota_or_cipher_replacement(self):
        row = self.rows[0]
        encrypted = self.stores[1].path.read_bytes()
        pin = (Path(row["runtime_dir"]) / ".account").read_bytes()

        def changed_account(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            upstream.flags["docs-hue"] = "synthetic-other-account"
            return upstream

        with patch("src.services.vids.gateway.VidsClient", side_effect=changed_account):
            await self.runtime.start({"accounts": [row]})
        self.assertEqual(self.runtime.account_errors, {1: "account_mismatch"})
        self.upstreams[-1].bootstrap.assert_awaited_once()
        self.upstreams[-1].quota.assert_not_awaited()
        self.assertEqual(self.stores[1].path.read_bytes(), encrypted)
        self.assertEqual((Path(row["runtime_dir"]) / ".account").read_bytes(), pin)
        self.assertEqual(self.dispatcher._accounts, {})
        self.assertEqual(self.runtime._gateways, [])
        self.assertTrue(self.runtime._started)

    async def test_second_start_failure_only_closes_failed_candidate_and_publishes_healthy(self):
        def failing(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            if "synthetic-2" in editor:
                upstream.bootstrap.side_effect = VidsTransportError("synthetic-private-error")
            return upstream

        candidates = []

        def gateway(*args, **options):
            value = VidsGateway(*args, **options)
            candidates.append(value)
            return value

        with (
            patch("src.services.vids.gateway.VidsClient", side_effect=failing),
            patch("src.services.vids.runtime.VidsGateway", side_effect=gateway),
        ):
            await self.runtime.start({"accounts": self.rows})
        self.assertEqual(self.runtime.account_errors, {2: "session_unavailable"})
        self.assertEqual(
            self.dispatcher.unavailable_receipt_roots, {2: Path(self.rows[1]["runtime_dir"])}
        )
        self.assertIsNone(self.runtime.last_error_code)
        self.assertTrue(self.runtime._started)
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertEqual(self.runtime._gateways, [candidates[0]])
        self.assertTrue(candidates[0]._ready)
        self.assertIsNotNone(candidates[0]._lock_file)
        self.assertFalse(candidates[1]._ready)
        self.assertIsNone(candidates[1]._lock_file)
        self.upstreams[0].session.close.assert_not_awaited()
        self.upstreams[1].session.close.assert_awaited_once()
        # The healthy account remains routable and can verify quota independently.
        self.assertIs(self.dispatcher._account(1, network=True).gateway, candidates[0])
        await candidates[0].quota("image")
        for upstream in self.upstreams:
            upstream.generate_speech.assert_not_awaited()
        with patch("src.services.vids.runtime.VidsGateway") as gateway_factory:
            with self.assertRaises(GatewayError):
                await self.runtime.start({"accounts": self.rows})
        gateway_factory.assert_not_called()
        self.assertEqual(self.runtime.account_errors, {2: "session_unavailable"})
        await self.runtime.close()
        self.assert_empty()
        await self.start()

    async def test_first_start_failure_still_starts_later_account(self):
        def failing(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            if "synthetic-1" in editor:
                upstream.bootstrap.side_effect = VidsProtocolError("private-editor-url", 401)
            return upstream

        with patch("src.services.vids.gateway.VidsClient", side_effect=failing):
            await self.runtime.start({"accounts": self.rows})
        self.assertEqual(self.runtime.account_errors, {1: "session_authentication_failed"})
        self.assertEqual(set(self.dispatcher._accounts), {2})
        self.assertTrue(self.dispatcher._account(2, network=True).gateway._ready)
        self.upstreams[0].session.close.assert_awaited_once()
        self.upstreams[1].session.close.assert_not_awaited()

    async def test_gateway_construction_failure_keeps_other_account_ready(self):
        def gateway(*args, **options):
            if Path(args[1]) == Path(self.rows[0]["runtime_dir"]):
                raise OSError("synthetic-private-path")
            return VidsGateway(*args, **options)

        with (
            patch("src.services.vids.runtime.VidsGateway", side_effect=gateway),
            patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream),
        ):
            await self.runtime.start({"accounts": self.rows})
        self.assertEqual(self.runtime.account_errors, {1: "session_unavailable"})
        self.assertEqual(set(self.dispatcher._accounts), {2})
        self.assertTrue(self.dispatcher._account(2, network=True).gateway._ready)
        self.assertEqual(len(self.upstreams), 1)

    async def test_all_failed_sessions_allow_one_target_to_be_adopted_without_restart(self):
        def failing(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            upstream.bootstrap.side_effect = VidsProtocolError("private-editor-url", 401)
            return upstream

        with patch("src.services.vids.gateway.VidsClient", side_effect=failing):
            await self.runtime.start({"accounts": self.rows})
        self.assertTrue(self.runtime._started)
        self.assertEqual(self.dispatcher._accounts, {})
        self.assertEqual(self.runtime._gateways, [])
        self.assertEqual(
            self.runtime.account_errors,
            {1: "session_authentication_failed", 2: "session_authentication_failed"},
        )
        self.assertIsNone(self.runtime.last_error_code)
        first, second = self.upstreams
        first.session.close.assert_awaited_once()
        second.session.close.assert_awaited_once()
        row = self.rows[0]
        recovered = VidsGateway(
            None,
            row["runtime_dir"],
            session_store=self.stores[1],
            resume=True,
            expected_account_hash=row["identity_hash"],
            expected_editor_url=row["editor_url"],
        )
        with patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream) as factory:
            await recovered.start()
        factory.assert_called_once()
        async with self.runtime._lifecycle_lock:
            self.runtime.adopt(VidsAccount(1, recovered, row["identity_hash"]), lambda: None)
        self.assertEqual(set(self.dispatcher._accounts), {1})
        self.assertEqual(self.runtime.account_errors, {2: "session_authentication_failed"})
        self.assertIs(self.dispatcher._account(1, network=True).gateway, recovered)
        self.assertEqual(
            self.dispatcher.unavailable_receipt_roots, {2: Path(self.rows[1]["runtime_dir"])}
        )
        second.bootstrap.assert_awaited_once()

    async def test_failed_account_durable_receipt_cannot_move_to_a_healthy_account(self):
        key = "synthetic-existing-paid-task"
        identifier = "vids_" + hashlib.sha256(key.encode()).hexdigest()
        (Path(self.rows[0]["runtime_dir"]) / (identifier + ".json")).write_text(
            '{"status":"unknown"}', encoding="utf-8"
        )

        def failing(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            if "synthetic-1" in editor:
                upstream.bootstrap.side_effect = VidsProtocolError("private-editor-url", 401)
            return upstream

        with patch("src.services.vids.gateway.VidsClient", side_effect=failing):
            await self.runtime.start({"accounts": self.rows})
        self.assertEqual(set(self.dispatcher._accounts), {2})
        self.assertEqual(
            self.dispatcher.unavailable_receipt_roots, {1: Path(self.rows[0]["runtime_dir"])}
        )
        with self.assertRaises(VidsDispatchError) as caught:
            await self.dispatcher.generate(2, {"kind": "speech", "prompt": "Hello"}, key)
        self.assertEqual(caught.exception.code, "account_affinity_conflict")
        for upstream in self.upstreams:
            upstream.generate_speech.assert_not_awaited()

    async def test_target_adoption_preserves_an_unhealthy_sibling_and_existing_claims(self):
        def failing(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            if "synthetic-2" in editor:
                upstream.bootstrap.side_effect = VidsProtocolError("private-editor-url", 401)
            return upstream

        with patch("src.services.vids.gateway.VidsClient", side_effect=failing):
            await self.runtime.start({"accounts": self.rows})
        sibling = self.dispatcher._accounts[1]
        sibling.gateway.client.initialization_valid = False
        sibling.gateway.client.flags["docs-hue"] = "synthetic-identity-drift"
        claim = hashlib.sha256(b"synthetic-existing-claim").hexdigest()
        self.dispatcher._claims[claim] = 1
        row = self.rows[1]
        recovered = VidsGateway(
            None,
            row["runtime_dir"],
            session_store=self.stores[2],
            resume=True,
            expected_account_hash=row["identity_hash"],
            expected_editor_url=row["editor_url"],
        )
        with patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream):
            await recovered.start()
        async with self.runtime._lifecycle_lock:
            self.runtime.adopt(VidsAccount(2, recovered, row["identity_hash"]), lambda: None)
        self.assertIs(self.dispatcher._accounts[1], sibling)
        self.assertEqual(self.dispatcher._claims, {claim: 1})
        self.assertIs(self.dispatcher._account(2, network=True).gateway, recovered)
        self.assertEqual(self.runtime.account_errors, {})
        self.assertEqual(self.dispatcher.unavailable_receipt_roots, {})
        sibling.gateway.client.session.close.assert_not_awaited()
        sibling.gateway.client.bootstrap.assert_awaited_once()
        with self.assertRaises(VidsDispatchError) as caught:
            self.dispatcher._account(1, network=True)
        self.assertEqual(caught.exception.code, "account_mismatch")

    async def test_cancel_during_failed_candidate_cleanup_closes_healthy_candidate(self):
        entered, release = asyncio.Event(), asyncio.Event()
        candidates = []

        def gateway(*args, **options):
            value = VidsGateway(*args, **options)
            candidates.append(value)
            if Path(args[1]) == Path(self.rows[1]["runtime_dir"]):
                close = value.close

                async def blocked_close():
                    entered.set()
                    await release.wait()
                    await close()

                value.start = AsyncMock(side_effect=VidsTransportError("synthetic-private-error"))
                value.close = blocked_close
            return value

        with (
            patch("src.services.vids.runtime.VidsGateway", side_effect=gateway),
            patch("src.services.vids.gateway.VidsClient", side_effect=self.upstream),
        ):
            pending = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assert_empty()
        self.assertTrue(all(value._lock_file is None and not value._ready for value in candidates))
        self.upstreams[0].session.close.assert_awaited_once()

    async def test_start_cancel_releases_all_runtime_locks_before_reraising(self):
        entered = asyncio.Event()
        candidates = []

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        def pausing(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            if "synthetic-2" in editor:
                upstream.bootstrap.side_effect = blocked
            return upstream

        def gateway(*args, **options):
            value = VidsGateway(*args, **options)
            candidates.append(value)
            return value

        with (
            patch("src.services.vids.gateway.VidsClient", side_effect=pausing),
            patch("src.services.vids.runtime.VidsGateway", side_effect=gateway),
        ):
            pending = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            self.assertEqual(set(self.dispatcher._accounts), {1})
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assert_empty()
        self.assertTrue(all(value._lock_file is None for value in candidates))
        await self.start()

    async def test_cancelled_shutdown_waits_for_active_gateway_lock_and_releases_all(self):
        await self.start()
        gateways = list(self.runtime._gateways)
        blocked = gateways[0]
        await blocked.lock.acquire()
        pending = asyncio.create_task(self.runtime.close())
        try:
            while self.dispatcher._accounts:
                await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
        finally:
            blocked.lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assert_empty()
        self.assertTrue(all(value._lock_file is None for value in gateways))
        await self.start()

    async def test_cancelled_shutdown_waiting_for_start_still_closes_the_restored_pool(self):
        entered, release = asyncio.Event(), asyncio.Event()
        candidates = []

        async def blocked():
            entered.set()
            await release.wait()

        def pausing(editor, rows, **options):
            upstream = self.upstream(editor, rows, **options)
            if "synthetic-2" in editor:
                upstream.bootstrap.side_effect = blocked
            return upstream

        def gateway(*args, **options):
            value = VidsGateway(*args, **options)
            candidates.append(value)
            return value

        with (
            patch("src.services.vids.gateway.VidsClient", side_effect=pausing),
            patch("src.services.vids.runtime.VidsGateway", side_effect=gateway),
        ):
            startup = asyncio.create_task(self.runtime.start({"accounts": self.rows}))
            await entered.wait()
            shutdown = asyncio.create_task(self.runtime.close())
            await asyncio.sleep(0)
            shutdown.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await startup
            with self.assertRaises(asyncio.CancelledError):
                await shutdown
        self.assert_empty()
        self.assertTrue(all(value._lock_file is None for value in candidates))
        await self.start()

    async def test_close_failure_still_closes_other_sessions_and_releases_every_lock(self):
        await self.start()
        gateways = list(self.runtime._gateways)
        self.upstreams[1].session.close.side_effect = OSError("synthetic-private-path")
        with self.assertRaises(GatewayError) as caught:
            await self.runtime.close()
        self.assertEqual(caught.exception.code, "session_close_failed")
        self.assertNotIn("private", str(caught.exception))
        self.assert_empty()
        self.assertTrue(all(value._lock_file is None for value in gateways))
        for upstream in self.upstreams:
            upstream.session.close.assert_awaited_once()
        self.upstreams[1].session.close.side_effect = None
        await self.start()

    async def test_proxy_switch_validates_identity_persists_and_swaps_transport(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        old_client = gateway.client
        old_snapshot = self.stores[1].path.read_bytes()
        self.dispatcher._claims["existing-claim"] = 1
        with patch("src.services.vids.runtime.VidsClient", side_effect=self.upstream) as factory:
            async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                self.assertTrue(self.runtime._lifecycle_lock.locked())
                self.assertTrue(gateway.lock.locked())
                self.assertIsNot(gateway.client, old_client)
                self.assertEqual(gateway.proxy, "http://new-proxy.invalid:8080")
                self.assertEqual(gateway.client.proxy, "http://new-proxy.invalid:8080")
            factory.assert_called_once()
        self.assertIsNot(gateway.client, old_client)
        self.assertNotEqual(self.stores[1].path.read_bytes(), old_snapshot)
        self.assertEqual(self.dispatcher._claims, {"existing-claim": 1})
        old_client.session.close.assert_awaited_once()
        gateway.client.session.close.assert_not_awaited()
        gateway.client.bootstrap.assert_awaited_once()
        gateway.client.quota.assert_awaited_once()
        gateway.client.generate_speech.assert_not_awaited()

    async def test_proxy_switch_body_failure_restores_client_proxy_and_cipher(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        old_client, old_proxy = gateway.client, gateway.proxy
        old_snapshot = self.stores[1].path.read_bytes()
        with patch("src.services.vids.runtime.VidsClient", side_effect=self.upstream):
            with self.assertRaisesRegex(RuntimeError, "abort"):
                async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                    raise RuntimeError("abort")
        self.assertIs(gateway.client, old_client)
        self.assertEqual(gateway.proxy, old_proxy)
        self.assertEqual(self.stores[1].path.read_bytes(), old_snapshot)
        old_client.session.close.assert_not_awaited()
        self.upstreams[-1].session.close.assert_awaited_once()
        self.assertFalse(gateway.lock.locked())
        self.assertFalse(self.runtime._lifecycle_lock.locked())

    async def test_proxy_switch_identity_failure_precedes_quota_and_keeps_original_session(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        old_client, encrypted = gateway.client, self.stores[1].path.read_bytes()
        candidate = self.upstream(self.rows[0]["editor_url"], cookies(), auth_user=0, proxy=None)
        candidate.flags["docs-hue"] = "another-account"
        with patch("src.services.vids.runtime.VidsClient", return_value=candidate):
            with self.assertRaises(GatewayError) as failure:
                async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                    self.fail("Identity mismatch entered the Profile/DB transaction")
        self.assertEqual(failure.exception.code, "account_mismatch")
        self.assertIs(gateway.client, old_client)
        self.assertEqual(self.stores[1].path.read_bytes(), encrypted)
        candidate.quota.assert_not_awaited()
        candidate.session.close.assert_awaited_once()
        old_client.session.close.assert_not_awaited()

    async def test_proxy_switch_busy_admission_does_not_create_transport(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        preflight = self.dispatcher._preflight_locks[1] = asyncio.Lock()
        for busy in (gateway.lock, preflight, self.runtime._lifecycle_lock):
            async with busy:
                with patch("src.services.vids.runtime.VidsClient") as factory:
                    with self.assertRaises(GatewayError) as failure:
                        async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                            self.fail("Busy account entered proxy maintenance")
                    self.assertEqual(failure.exception.status, 409)
                    factory.assert_not_called()
        self.dispatcher._pool_pending.add(1)
        with patch("src.services.vids.runtime.VidsClient") as factory:
            with self.assertRaises(GatewayError):
                async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                    self.fail("Pending admission entered proxy maintenance")
            factory.assert_not_called()

    async def test_proxy_switch_cancellation_restores_cipher_before_draining_candidate(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        old_client, encrypted = gateway.client, self.stores[1].path.read_bytes()
        entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        candidate = self.upstream(self.rows[0]["editor_url"], cookies(), auth_user=0, proxy=None)

        async def close_candidate():
            closing.set()
            await release.wait()

        candidate.session.close.side_effect = close_candidate

        async def change():
            async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                entered.set()
                await asyncio.Event().wait()

        with patch("src.services.vids.runtime.VidsClient", return_value=candidate):
            task = asyncio.create_task(change())
            await entered.wait()
            task.cancel()
            await closing.wait()
            self.assertIs(gateway.client, old_client)
            self.assertEqual(self.stores[1].path.read_bytes(), encrypted)
            self.assertTrue(gateway.lock.locked())
            self.assertFalse(task.done())
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(gateway.lock.locked())
        self.assertFalse(self.runtime._lifecycle_lock.locked())
        old_client.session.close.assert_not_awaited()

    async def test_proxy_switch_persistence_failure_does_not_enter_profile_transaction(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        old_client, encrypted = gateway.client, self.stores[1].path.read_bytes()
        with (
            patch("src.services.vids.runtime.VidsClient", side_effect=self.upstream),
            patch.object(gateway.session_store, "save", side_effect=OSError("private-path")),
        ):
            with self.assertRaises(GatewayError) as failure:
                async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                    self.fail("Failed persistence entered the Profile/DB transaction")
        self.assertEqual(failure.exception.code, "session_persistence_failed")
        self.assertNotIn("private-path", str(failure.exception))
        self.assertIs(gateway.client, old_client)
        self.assertFalse(gateway.persistence_failed)
        self.assertEqual(self.stores[1].path.read_bytes(), encrypted)

    async def test_proxy_switch_validation_error_is_private_and_does_not_rewrite_cipher(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        old_client, encrypted = gateway.client, self.stores[1].path.read_bytes()
        candidate = self.upstream(self.rows[0]["editor_url"], cookies(), auth_user=0, proxy=None)
        candidate.quota.side_effect = ValueError("synthetic-private-proxy-and-cookie")
        with (
            patch("src.services.vids.runtime.VidsClient", return_value=candidate),
            patch.object(self.runtime, "_restore_session_bytes") as restore,
        ):
            with self.assertRaises(GatewayError) as failure:
                async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                    self.fail("Unverified transport entered Profile maintenance")
            restore.assert_not_called()
        self.assertEqual(failure.exception.code, "proxy_verification_failed")
        self.assertNotIn("synthetic-private", str(failure.exception))
        self.assertIs(gateway.client, old_client)
        self.assertEqual(self.stores[1].path.read_bytes(), encrypted)
        candidate.session.close.assert_awaited_once()

    async def test_proxy_switch_close_failure_after_commit_keeps_new_transport(self):
        await self.start([self.rows[0]])
        gateway = self.dispatcher._accounts[1].gateway
        old_client = gateway.client
        old_client.session.close.side_effect = OSError("synthetic-old-session")
        with patch("src.services.vids.runtime.VidsClient", side_effect=self.upstream):
            async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                pass
        self.assertIsNot(gateway.client, old_client)
        self.assertEqual(gateway.proxy, "http://new-proxy.invalid:8080")
        self.assertFalse(gateway.persistence_failed)

    async def test_proxy_switch_unconfigured_is_noop_and_explicit_binding_is_not_overwritten(self):
        with patch("src.services.vids.runtime.VidsClient") as factory:
            async with self.runtime.proxy_switch(99, "http://new-proxy.invalid:8080"):
                pass
            factory.assert_not_called()
        await self.start([{**self.rows[0], "proxy": "http://fixed-proxy.invalid:8080"}])
        with patch("src.services.vids.runtime.VidsClient") as factory:
            with self.assertRaises(GatewayError) as failure:
                async with self.runtime.proxy_switch(1, "http://new-proxy.invalid:8080"):
                    self.fail("Explicit Vids proxy was silently overwritten")
            self.assertEqual(failure.exception.code, "fixed_proxy")
            factory.assert_not_called()


class VidsMainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src import main

        self.enterContext(patch.object(main.app.state, "vids_auto_onboarding", None))
        # This suite tests ordering and startup failures using a fake Flow
        # lifespan; filesystem recovery is covered with isolated fixtures.
        self.enterContext(
            patch.object(
                main.app.state,
                "profile_cache_maintenance",
                SimpleNamespace(start=lambda: None, close=AsyncMock()),
            )
        )
        self.enterContext(
            patch.object(
                main.app.state,
                "vids_media_cleanup",
                SimpleNamespace(start=lambda: None, close=AsyncMock()),
            )
        )

    async def test_vids_gateway_startup_failure_keeps_flow_and_management_running(self):
        from src import main

        events = []

        @asynccontextmanager
        async def flow_lifespan(app):
            events.append("flow_enter")
            try:
                yield
            finally:
                events.append("flow_exit")

        with (
            patch.object(main, "_flow_lifespan", flow_lifespan),
            patch.object(main.config, "get_raw_config", return_value={"vids": {"accounts": []}}),
            patch.object(
                main.vids_runtime,
                "start",
                side_effect=GatewayError(503, "session_unavailable", "Unavailable"),
            ),
            patch.object(main.vids_runtime, "close", AsyncMock()) as close,
            patch.object(main.vids_runtime, "last_error_code", None),
        ):
            async with main.lifespan(main.app):
                events.append("body")
                self.assertEqual(main.vids_runtime.last_error_code, "session_unavailable")
            close.assert_awaited_once_with()
        self.assertEqual(events, ["flow_enter", "body", "flow_exit"])

    async def test_network_startup_does_not_delay_serving_and_shutdown_drains_it(self):
        from src import main

        entered, cleaned = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def flow_lifespan(app):
            yield

        async def blocked_start(settings):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with (
            patch.object(main, "_flow_lifespan", flow_lifespan),
            patch.object(main.config, "get_raw_config", return_value={"vids": {"accounts": []}}),
            patch.object(main.vids_runtime, "start", side_effect=blocked_start),
            patch.object(main.vids_runtime, "close", AsyncMock()) as close,
        ):
            async with asyncio.timeout(1):
                async with main.lifespan(main.app):
                    self.assertTrue(entered.is_set())
                    self.assertFalse(cleaned.is_set())
                    self.assertFalse(main.app.state.vids_startup_task.done())
            self.assertTrue(cleaned.is_set())
            self.assertIsNone(main.app.state.vids_startup_task)
            close.assert_awaited_once_with()

    async def test_main_lifespan_passes_configuration_and_always_closes_before_flow_exit(self):
        from src import main

        settings = {"accounts": [{"token_id": 123}]}
        for failure in (None, "body", "startup"):
            with self.subTest(failure=failure):
                events = []

                @asynccontextmanager
                async def flow_lifespan(app):
                    events.append("flow_enter")
                    try:
                        yield
                    finally:
                        events.append("flow_exit")

                async def start(configured):
                    events.append("vids_start")
                    if failure == "startup":
                        raise RuntimeError("synthetic startup failure")

                async def close():
                    events.append("vids_close")

                async def exercise():
                    async with main.lifespan(main.app):
                        events.append("body")
                        await main.app.state.vids_startup_task
                        if failure == "body":
                            raise RuntimeError("synthetic body failure")

                with (
                    patch.object(main, "_flow_lifespan", flow_lifespan),
                    patch.object(main.config, "get_raw_config", return_value={"vids": settings}),
                    patch.object(main.vids_runtime, "start", side_effect=start) as startup,
                    patch.object(main.vids_runtime, "close", side_effect=close) as shutdown,
                ):
                    if failure != "body":
                        await exercise()
                        if failure == "startup":
                            self.assertEqual(
                                main.vids_runtime.last_error_code, "session_unavailable"
                            )
                    else:
                        with self.assertRaisesRegex(RuntimeError, f"synthetic {failure} failure"):
                            await exercise()
                startup.assert_awaited_once_with(settings)
                shutdown.assert_awaited_once_with()
                expected = ["flow_enter", "vids_start"]
                expected.append("body")
                self.assertEqual(events, expected + ["vids_close", "flow_exit"])

    async def test_main_factory_state_and_account_routes_share_the_same_dispatcher(self):
        from src import main
        from src.api.vids_media import PREFIX

        self.assertIs(main.app.state.vids_dispatcher, main.services.vids_dispatcher)
        self.assertIs(main.app.state.vids_runtime, main.services.vids_runtime)
        self.assertIs(main.vids_runtime.dispatcher, main.app.state.vids_dispatcher)
        self.assertIs(main.vids_runtime.proxy_manager, main.services.proxy_manager)
        for method, path in (
            ("GET", "/v1/models"),
            ("POST", "/v1/images/generations"),
            ("POST", "/v1/audio/speech"),
            ("POST", "/v1/videos"),
        ):
            matches = [
                route
                for route in main.app.routes
                if route.path == PREFIX + path and method in getattr(route, "methods", ())
            ]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].endpoint.__module__, "src.api.vids_media")
        for path in ("/v1/models", "/v1/images/generations", "/v1/videos"):
            matches = [route for route in main.app.routes if route.path == path]
            self.assertTrue(matches)
            self.assertTrue(
                all(route.endpoint.__module__ != "src.api.vids_media" for route in matches)
            )


if __name__ == "__main__":
    unittest.main()
